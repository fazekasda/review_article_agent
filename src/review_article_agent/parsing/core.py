"""Bounded, stdlib-only XML-first parsing core (Step 07, S7-CORE).

This module owns **one bounded outcome**: given the exact bytes of a stored
full-text source, produce a *structural* map -- ordered passages with their
section path, tables/figures/supplement references with captions, and local
citation-marker -> bibliography links -- together with explicit parser and
normalization versions and per-passage digests.

Authority: ``docs/implementation/07-parsing.md`` deliverables (lines 9-12) and
acceptance criteria (lines 16-22); ``docs/specification.md`` scientific
invariants (raw assets are immutable and normalized text is a separate,
documented revision; citation markers link to entries only when tagged;
failed/absent evidence produces a human action, never an empty-paper
exclusion); ``docs/implementation/handoffs/06-acquisition.md`` constraints
(official/authorized sources only; a PDF preview is never promoted to full
text).

What this module is:

* A pure function :func:`parse_document` from ``bytes`` to a JSON-safe mapping.
* XML-first: JATS ``article`` and TEI ``TEI`` are recognized; a benign
  publisher external ``DOCTYPE`` (no internal subset) is safely *removed* and
  never fetched, while internal subsets / entity declarations / a second
  ``DOCTYPE`` are rejected.  Bounded node / depth / text limits defend against
  hostile XML.
* Conservative normalization (Unicode NFC, presentation ligature folding,
  soft-hyphen removal, whitespace collapsing).  It **never** removes negation,
  rewrites numbers or units, or aggressively joins hyphenated words; an
  ambiguous hyphen and an en-dash are preserved.
* Explicit citation-link states (``resolved`` / ``unresolved`` / ``ambiguous``
  / ``malformed``) so a marker with no tag, a dangling target, a duplicated
  bibliography id, or an unrelated malformed bibliography entry cannot
  silently fabricate or globally invalidate a passage.

What this module is **not**:

* No scientific correctness, completeness, or originality claim; no screening
  or extraction.  A ``parsed`` status means only that the structure was read.
* No storage, CLI, network, model, or approval; no PDF adapter (see
  :mod:`review_article_agent.parsing.pdf_adapter`).  Nothing here authorises
  scientific digestion.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter

from ..domain.errors import ReviewAgentError

# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1
DOCUMENT_TYPE = "parsed_passages"
PARSER_VERSION = "step07-jats-tei/1"
NORMALIZATION_VERSION = "nfc-ligature-ws/1"

#: Hard cap on accepted bytes (32 MiB); crossing it is an explicit rejection,
#: never a silent truncation.
MAX_BYTE_SIZE = 32 * 1024 * 1024

#: Bounds applied to a parsed XML tree (defence against hostile XML).
MAX_XML_NODES = 100_000
MAX_XML_DEPTH = 200
MAX_XML_TEXT_CHARS = 8 * 1024 * 1024

FORMATS = ("jats", "tei", "unsupported", "pdf")
STATUSES = ("parsed", "needs_review", "rejected")
MARKER_STATES = ("resolved", "unresolved", "ambiguous", "malformed")
ASSET_KINDS = ("figure", "table", "supplement")

REASONS = frozenset(
    {
        "empty_input",
        "byte_cap_exceeded",
        "xml_not_utf8",
        "xml_malformed",
        "xml_undefined_entity",
        "xml_entity_declaration",
        "xml_internal_subset",
        "xml_doctype_rejected",
        "xml_nodes_limit_exceeded",
        "xml_depth_limit_exceeded",
        "xml_text_limit_exceeded",
        "xml_not_jats_or_tei",
        "jats_doctype_removed",
        "non_text_assets_present",
    }
)

LIMITATIONS = (
    "Structural parsing and locator stability only. Passage text, section "
    "paths, captions and citation links are read from tagged XML; this result "
    "does not assert scientific correctness, completeness, originality, or "
    "that every reference, number, unit, or table cell was interpreted. It "
    "never authorises scientific digestion or exclusion."
)


class ParsingRequestError(ReviewAgentError, ValueError):
    """A parsing *request* was malformed (bad argument types/shapes).

    A caller/programming error about the request; content-level problems are
    reported through the returned mapping, never by raising.
    """


# --------------------------------------------------------------------------- #
# Normalization (documented, conservative)
# --------------------------------------------------------------------------- #
#: Presentation-form Latin ligatures only.  Deliberately excludes letter pairs
#: such as ``ae``/``oe`` that are not pure presentation ligatures, to avoid
#: silently changing a word.
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}

_SOFT_HYPHEN = "\u00ad"
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> tuple[str, list[str]]:
    """Return ``(normalized, transformations)`` for one raw string.

    Transformations are applied in a fixed order and documented by name:
    ``unicode_nfc``, ``ligature_fold``, ``soft_hyphen_removed``,
    ``whitespace_collapsed``.  Negation words (``no``/``not``/``n't``), digits,
    units, dashes and hyphens are **never** removed or rewritten -- a
    hyphenated or en-dashed token stays exactly as the source had it (an
    ambiguous hyphen is preserved rather than guessed at).
    """
    if not isinstance(text, str):
        raise ParsingRequestError("text must be a string")
    transformations: list[str] = []

    working = unicodedata.normalize("NFC", text)
    if working != text:
        transformations.append("unicode_nfc")

    if any(ligature in working for ligature in _LIGATURES):
        for ligature, replacement in _LIGATURES.items():
            working = working.replace(ligature, replacement)
        transformations.append("ligature_fold")

    if _SOFT_HYPHEN in working:
        working = working.replace(_SOFT_HYPHEN, "")
        transformations.append("soft_hyphen_removed")

    collapsed = _WS_RE.sub(" ", working).strip()
    if collapsed != working:
        transformations.append("whitespace_collapsed")

    return collapsed, transformations


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# XML preparation / bounds
# --------------------------------------------------------------------------- #
def _strip_safe_doctype(text: str) -> tuple[str, bool, str | None]:
    """Remove a benign external ``DOCTYPE``; refuse every other declaration.

    Returns ``(text, removed, refusal_reason_or_None)``.  A declaration with an
    internal subset ``[`` is refused (entity declaration vs. plain subset), as
    is a stray ``<!ENTITY`` or a second ``DOCTYPE``.  No external resource is
    ever fetched.
    """
    idx = text.find("<!DOCTYPE")
    removed = False
    if idx != -1:
        i = idx
        quote: str | None = None
        depth = 0
        end = -1
        while i < len(text):
            ch = text[i]
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == ">" and depth == 0:
                end = i + 1
                break
            i += 1
        declaration = text[idx:end] if end != -1 else text[idx:]
        if "[" in declaration:
            if "<!ENTITY" in declaration:
                return text, False, "xml_entity_declaration"
            return text, False, "xml_internal_subset"
        if end == -1:
            return text, False, "xml_doctype_rejected"
        text = text[:idx] + text[end:]
        removed = True

    upper = text
    if "<!ENTITY" in upper:
        return text, False, "xml_entity_declaration"
    if "<!DOCTYPE" in text:
        return text, False, "xml_doctype_rejected"
    return text, removed, None


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _measure(root) -> tuple[int, int, int, str | None]:
    """Bounded iterative traversal: ``(nodes, max_depth, text_chars, breach)``."""
    nodes = 0
    max_depth = 0
    text_chars = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        nodes += 1
        if nodes > MAX_XML_NODES:
            return nodes, max_depth, text_chars, "nodes"
        if depth > max_depth:
            max_depth = depth
            if max_depth > MAX_XML_DEPTH:
                return nodes, max_depth, text_chars, "depth"
        if element.text:
            text_chars += len(element.text)
        if element.tail:
            text_chars += len(element.tail)
        if text_chars > MAX_XML_TEXT_CHARS:
            return nodes, max_depth, text_chars, "text"
        for child in element:
            stack.append((child, depth + 1))
    return nodes, max_depth, text_chars, None


# --------------------------------------------------------------------------- #
# Small element helpers
# --------------------------------------------------------------------------- #
def _direct(element, name: str) -> list:
    return [child for child in element if _local(child.tag) == name]


def _first(element, name: str):
    found = _direct(element, name)
    return found[0] if found else None


def _flatten(element) -> str:
    parts = [element.text or ""]
    for child in element:
        parts.append(_flatten(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _flatten_children(element, skip_names: tuple = ()) -> str:
    parts = [element.text or ""]
    for child in element:
        if _local(child.tag) in skip_names:
            parts.append(child.tail or "")
            continue
        parts.append(_flatten(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _text_of(element) -> str:
    if element is None:
        return ""
    text, _ = normalize_text(_flatten(element))
    return text


def _element_id(element) -> str | None:
    value = element.get("id")
    if value is None:
        value = element.get("{http://www.w3.org/XML/1998/namespace}id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _href(element) -> str | None:
    for node in element.iter():
        for key, value in node.attrib.items():
            if key == "href" or key.endswith("}href"):
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


# --------------------------------------------------------------------------- #
# Inline text + citation markers
# --------------------------------------------------------------------------- #
def _is_citation(element, fmt: str) -> bool:
    name = _local(element.tag)
    if fmt == "jats" and name == "xref":
        return (element.get("ref-type") or "").strip().lower() in (
            "bibr",
            "bibliographic",
            "citation",
        )
    if fmt == "tei" and name == "ref":
        return "bibr" in (element.get("type") or "").strip().lower()
    return False


def _citation_target(element, fmt: str) -> str | None:
    if fmt == "jats":
        return element.get("rid")
    if fmt == "tei":
        return element.get("target")
    return None


def _parse_targets(target: object) -> list[str]:
    if not isinstance(target, str):
        return []
    targets: list[str] = []
    for token in target.split():
        token = token.strip()
        if token.startswith("#"):
            token = token[1:]
        if token:
            targets.append(token)
    return targets


def _inline(element, fmt: str) -> tuple[str, list[dict]]:
    """Return ``(raw_inline_text, markers)`` for one block element.

    A tagged citation marker contributes its own rendered text inline and is
    recorded separately with its raw target ids.
    """
    text_parts: list[str] = []
    markers: list[dict] = []

    def rec(node) -> None:
        if node.text:
            text_parts.append(node.text)
        for child in node:
            if _is_citation(child, fmt):
                label = _flatten(child)
                markers.append(
                    {"marker": label.strip(), "targets": _parse_targets(
                        _citation_target(child, fmt)
                    )}
                )
                text_parts.append(label)
            else:
                rec(child)
            if child.tail:
                text_parts.append(child.tail)

    rec(element)
    return "".join(text_parts), markers


# --------------------------------------------------------------------------- #
# Passages
# --------------------------------------------------------------------------- #
def _make_passage(
    passage_id: str, kind: str, section_path: list[str], raw_text: str
) -> dict:
    text, transformations = normalize_text(raw_text)
    return {
        "passage_id": passage_id,
        "kind": kind,
        "section_path": list(section_path),
        "raw_text": raw_text,
        "text": text,
        "text_sha256": _sha256(text),
        "normalization_version": NORMALIZATION_VERSION,
        "transformations": transformations,
    }


def _next_id(passages: list) -> str:
    return f"p{len(passages) + 1:04d}"


# --------------------------------------------------------------------------- #
# JATS extraction
# --------------------------------------------------------------------------- #
def _walk_jats(container, path, passages, markers) -> None:
    for child in container:
        name = _local(child.tag)
        if name == "sec":
            title = _text_of(_first(child, "title"))
            _walk_jats(child, path + ([title] if title else []), passages, markers)
        elif name == "p":
            raw, inline_markers = _inline(child, "jats")
            passage_id = _next_id(passages)
            passages.append(_make_passage(passage_id, "paragraph", path, raw))
            for marker in inline_markers:
                markers.append({"passage_id": passage_id, **marker})


def _jats_passages(root):
    passages: list[dict] = []
    markers: list[dict] = []
    front = _first(root, "front")
    abstract = None
    if front is not None:
        abstract = _first(front, "abstract")
        if abstract is None:
            meta = _first(front, "article-meta")
            abstract = _first(meta, "abstract") if meta is not None else None
    if abstract is not None:
        _walk_jats(abstract, ["Abstract"], passages, markers)
    body = _first(root, "body")
    if body is not None:
        _walk_jats(body, [], passages, markers)
    return passages, markers


def _jats_references(root) -> list[dict]:
    refs: list[dict] = []
    for ref_list in [e for e in root.iter() if _local(e.tag) == "ref-list"]:
        for ref in [e for e in ref_list.iter() if _local(e.tag) == "ref"]:
            ref_id = _element_id(ref)
            label = _text_of(_first(ref, "label"))
            text = _text_of(ref)
            refs.append(
                {
                    "ref_id": ref_id,
                    "label": label or None,
                    "text": text,
                    "malformed": ref_id is None,
                }
            )
    return refs


def _jats_assets(root) -> list[dict]:
    assets: list[dict] = []
    for element in root.iter():
        name = _local(element.tag)
        if name == "fig":
            assets.append(_asset("figure", element, "label", "caption"))
        elif name == "table-wrap":
            assets.append(_asset("table", element, "label", "caption"))
        elif name in ("supplementary-material", "media"):
            assets.append(_asset("supplement", element, "label", "caption"))
    return assets


def _asset(kind: str, element, label_name: str, caption_name: str) -> dict:
    return {
        "kind": kind,
        "id": _element_id(element),
        "label": _text_of(_first(element, label_name)),
        "caption": _text_of(_first(element, caption_name)),
        "href": _href(element),
    }


# --------------------------------------------------------------------------- #
# TEI extraction
# --------------------------------------------------------------------------- #
def _walk_tei(container, path, passages, markers) -> None:
    for child in container:
        name = _local(child.tag)
        if name == "div":
            title = _text_of(_first(child, "head"))
            _walk_tei(child, path + ([title] if title else []), passages, markers)
        elif name == "p":
            raw, inline_markers = _inline(child, "tei")
            passage_id = _next_id(passages)
            passages.append(_make_passage(passage_id, "paragraph", path, raw))
            for marker in inline_markers:
                markers.append({"passage_id": passage_id, **marker})


def _tei_passages(root):
    passages: list[dict] = []
    markers: list[dict] = []
    text_el = _first(root, "text")
    if text_el is not None:
        body = _first(text_el, "body")
        if body is None:
            body = text_el
        _walk_tei(body, [], passages, markers)
    return passages, markers


def _tei_references(root) -> list[dict]:
    refs: list[dict] = []
    for list_bibl in [e for e in root.iter() if _local(e.tag) == "listBibl"]:
        for bibl in [e for e in list_bibl.iter() if _local(e.tag) == "bibl"]:
            ref_id = _element_id(bibl)
            label = _text_of(_first(bibl, "label"))
            refs.append(
                {
                    "ref_id": ref_id,
                    "label": label or None,
                    "text": _text_of(bibl),
                    "malformed": ref_id is None,
                }
            )
    return refs


def _tei_assets(root) -> list[dict]:
    assets: list[dict] = []
    for element in root.iter():
        name = _local(element.tag)
        if name == "figure":
            assets.append(_asset("figure", element, "head", "figDesc"))
        elif name == "table":
            assets.append(_asset("table", element, "head", "head"))
        elif name in ("media", "graphic"):
            assets.append(_asset("supplement", element, "head", "head"))
    return assets


# --------------------------------------------------------------------------- #
# Citation links
# --------------------------------------------------------------------------- #
def _link_state(targets: list[str], ref_counts: Counter) -> str:
    if not targets:
        return "malformed"
    states: list[str] = []
    for target in targets:
        count = ref_counts.get(target, 0)
        if count == 0:
            states.append("unresolved")
        elif count > 1:
            states.append("ambiguous")
        else:
            states.append("resolved")
    if "ambiguous" in states:
        return "ambiguous"
    if "unresolved" in states:
        return "unresolved"
    return "resolved"


def _build_links(markers: list[dict], refs: list[dict]) -> list[dict]:
    ref_counts: Counter = Counter(
        ref["ref_id"] for ref in refs if ref["ref_id"] is not None
    )
    links: list[dict] = []
    for marker in markers:
        targets = marker.get("targets") or []
        state = _link_state(targets, ref_counts)
        resolved = (
            list(targets) if state == "resolved" else []
        )
        links.append(
            {
                "passage_id": marker["passage_id"],
                "marker": marker.get("marker", ""),
                "targets": list(targets),
                "state": state,
                "resolved_ref_ids": resolved,
            }
        )
    return links


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _dedupe(reasons) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


def _finalize(
    *,
    digest: str,
    byte_size: int,
    fmt: str,
    status: str,
    passages: list,
    references: list,
    citation_links: list,
    assets: list,
    reasons: list,
) -> dict:
    counts = {"resolved": 0, "unresolved": 0, "ambiguous": 0, "malformed": 0}
    for link in citation_links:
        counts[link["state"]] = counts.get(link["state"], 0) + 1

    non_text = any(asset["kind"] in ASSET_KINDS for asset in assets)
    ordered_reasons = _dedupe(reasons)
    if non_text:
        ordered_reasons = _dedupe(ordered_reasons + ["non_text_assets_present"])

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "format": fmt,
        "status": status,
        "source": {"sha256": digest, "byte_size": byte_size},
        "passages": passages,
        "references": references,
        "citation_links": citation_links,
        "citations": counts,
        "assets": assets,
        "non_text_assets_require_review": non_text,
        "human_action_required": status != "parsed" or non_text,
        "excludes_candidate": False,
        "reasons": ordered_reasons,
        "authorizes_scientific_processing": False,
        "limitations": LIMITATIONS,
    }


def _reject(digest: str, byte_size: int, reasons: list[str]) -> dict:
    return _finalize(
        digest=digest,
        byte_size=byte_size,
        fmt="unsupported",
        status="rejected",
        passages=[],
        references=[],
        citation_links=[],
        assets=[],
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def parse_document(data: bytes, *, source_digest: str | None = None) -> dict:
    """Parse one XML full-text source into a JSON-safe structural map.

    ``data`` is the exact stored bytes; ``source_digest`` optionally overrides
    the computed SHA-256 (for a caller that already has the raw digest).  The
    result follows the schema documented in this module's docstring and is
    always JSON-compatible.  Raises :class:`ParsingRequestError` only when the
    *request* is malformed.
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise ParsingRequestError("data must be bytes")
    if source_digest is not None and (
        not isinstance(source_digest, str) or not source_digest
    ):
        raise ParsingRequestError("source_digest must be a non-empty string or None")

    digest = source_digest if source_digest is not None else hashlib.sha256(data).hexdigest()
    byte_size = len(data)
    if byte_size == 0:
        return _reject(digest, byte_size, ["empty_input"])
    if byte_size > MAX_BYTE_SIZE:
        return _reject(digest, byte_size, ["byte_cap_exceeded"])

    try:
        raw_text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _reject(digest, byte_size, ["xml_not_utf8"])
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text[1:]

    prepared, doctype_removed, refusal = _strip_safe_doctype(raw_text)
    if refusal is not None:
        return _reject(digest, byte_size, [refusal])

    try:
        root = ET.fromstring(prepared.encode("utf-8"))
    except ET.ParseError as exc:
        low = str(exc).lower()
        reason = "xml_undefined_entity" if "undefined entity" in low else "xml_malformed"
        return _reject(digest, byte_size, [reason])

    _nodes, _depth, _text, breach = _measure(root)
    if breach == "nodes":
        return _reject(digest, byte_size, ["xml_nodes_limit_exceeded"])
    if breach == "depth":
        return _reject(digest, byte_size, ["xml_depth_limit_exceeded"])
    if breach == "text":
        return _reject(digest, byte_size, ["xml_text_limit_exceeded"])

    fmt = _detect_format(root)
    if fmt == "unsupported":
        return _finalize(
            digest=digest,
            byte_size=byte_size,
            fmt="unsupported",
            status="needs_review",
            passages=[],
            references=[],
            citation_links=[],
            assets=[],
            reasons=["xml_not_jats_or_tei"],
        )

    if fmt == "jats":
        passages, markers = _jats_passages(root)
        references = _jats_references(root)
        assets = _jats_assets(root)
    else:
        passages, markers = _tei_passages(root)
        references = _tei_references(root)
        assets = _tei_assets(root)

    citation_links = _build_links(markers, references)
    reasons: list[str] = ["jats_doctype_removed"] if doctype_removed else []

    return _finalize(
        digest=digest,
        byte_size=byte_size,
        fmt=fmt,
        status="parsed",
        passages=passages,
        references=references,
        citation_links=citation_links,
        assets=assets,
        reasons=reasons,
    )


def _detect_format(root) -> str:
    name = _local(root.tag)
    if name == "article":
        return "jats"
    if name.lower() == "tei":
        return "tei"
    return "unsupported"
