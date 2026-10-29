"""Passage locators and staleness resolution (Step 07, S7-CORE).

Authority: ``docs/implementation/07-parsing.md`` AC16 ("Resolve a stored passage
against its exact source/parsed revision after restart; detect changed
parser/text/source dependencies") and AC19 ("relevant unresolved links remain a
check failure") together with ``docs/specification.md``'s rule that approvals
bind to immutable artifact revisions and dependencies.

A locator is the small, JSON-safe pointer recorded with a finding: the passage
id, its section path, the digest of its *normalized* text, the source digest,
and the parser / normalization versions.  :func:`resolve_locator` re-checks that
pointer against a freshly parsed document and reports an explicit ``stale`` or
``missing`` status when any dependency changed, so a passage is never silently
re-used against a different revision.

The citation check is deliberately **local**: only the links belonging to the
requested passage are examined, so one unresolved or ambiguous link (or an
unrelated malformed bibliography entry) fails that passage -- not every
passage -- matching AC19.
"""

from __future__ import annotations

import hashlib
import json

from .core import ParsingRequestError

#: Locator statuses, in the order the resolver reports them.
LOCATOR_STATUSES = ("resolved", "stale", "missing")


def _locator_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_passage(parsed: dict, passage_id: str) -> dict | None:
    for passage in parsed.get("passages", []):
        if passage.get("passage_id") == passage_id:
            return passage
    return None


def make_locator(parsed: dict, passage_id: str) -> dict:
    """Build the immutable locator for one passage of ``parsed``.

    Raises :class:`ParsingRequestError` when ``parsed`` is not a mapping or the
    passage id is absent -- a locator must never be fabricated.
    """
    if not isinstance(parsed, dict):
        raise ParsingRequestError("parsed must be a mapping")
    if not isinstance(passage_id, str) or not passage_id:
        raise ParsingRequestError("passage_id must be a non-empty string")
    passage = _find_passage(parsed, passage_id)
    if passage is None:
        raise ParsingRequestError(f"passage {passage_id!r} is not in the document")

    source = parsed.get("source") or {}
    payload = {
        "passage_id": passage["passage_id"],
        "section_path": list(passage.get("section_path", [])),
        "text_sha256": passage["text_sha256"],
        "source_sha256": source.get("sha256"),
        "parser_version": parsed.get("parser_version"),
        "normalization_version": parsed.get("normalization_version"),
    }
    locator = dict(payload)
    locator["locator_sha256"] = _locator_digest(payload)
    return locator


def passage_citation_check(parsed: dict, passage_id: str) -> dict:
    """Report the citation-link states of *one* passage only.

    ``ok`` is ``False`` when that passage has any unresolved, ambiguous, or
    malformed link.  Links of other passages and unrelated malformed
    bibliography entries are never counted here.
    """
    if not isinstance(parsed, dict):
        raise ParsingRequestError("parsed must be a mapping")
    links = [
        link
        for link in parsed.get("citation_links", [])
        if link.get("passage_id") == passage_id
    ]
    unresolved = [link for link in links if link.get("state") == "unresolved"]
    ambiguous = [link for link in links if link.get("state") == "ambiguous"]
    malformed = [link for link in links if link.get("state") == "malformed"]
    return {
        "passage_id": passage_id,
        "ok": not (unresolved or ambiguous or malformed),
        "total": len(links),
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "malformed": malformed,
    }


def _locator_payload(locator: dict) -> dict:
    return {
        "passage_id": locator.get("passage_id"),
        "section_path": list(locator.get("section_path", [])),
        "text_sha256": locator.get("text_sha256"),
        "source_sha256": locator.get("source_sha256"),
        "parser_version": locator.get("parser_version"),
        "normalization_version": locator.get("normalization_version"),
    }


def resolve_locator(locator: dict, parsed: dict) -> dict:
    """Re-check ``locator`` against a freshly parsed ``parsed`` document.

    Validates the locator's own ``locator_sha256`` and every dependency: the
    source digest, the parser and normalization versions, the section path, and
    the passage's *actual* normalized-text hash (recomputed from
    ``passage['text']``, cross-checked against the passage's internal
    ``text_sha256``).  Returns a JSON-safe mapping:

    ``status`` is ``resolved`` only when everything matches, ``stale`` when the
    passage exists but the locator or a dependency changed (including an
    invalid ``locator_sha256``), and ``missing`` when the passage id is gone.
    ``citation_check`` is always the local check for that passage.
    """
    if not isinstance(locator, dict):
        raise ParsingRequestError("locator must be a mapping")
    if not isinstance(parsed, dict):
        raise ParsingRequestError("parsed must be a mapping")

    passage_id = locator.get("passage_id")
    reasons: list[str] = []

    if _locator_digest(_locator_payload(locator)) != locator.get("locator_sha256"):
        reasons.append("locator_digest_invalid")

    source = parsed.get("source") or {}
    if source.get("sha256") != locator.get("source_sha256"):
        reasons.append("source_digest_changed")
    if parsed.get("parser_version") != locator.get("parser_version"):
        reasons.append("parser_version_changed")
    if parsed.get("normalization_version") != locator.get("normalization_version"):
        reasons.append("normalization_version_changed")

    passage = _find_passage(parsed, passage_id) if passage_id else None
    if passage is None:
        return {
            "passage_id": passage_id,
            "status": "missing",
            "reasons": ["passage_not_found"],
            "locator_ok": False,
            "citation_check": passage_citation_check(parsed, passage_id or ""),
        }

    if list(passage.get("section_path", [])) != list(locator.get("section_path", [])):
        reasons.append("section_path_changed")
    actual = hashlib.sha256(
        (passage.get("text") or "").encode("utf-8")
    ).hexdigest()
    if actual != locator.get("text_sha256"):
        reasons.append("passage_text_changed")
    if passage.get("text_sha256") != actual:
        reasons.append("passage_text_hash_inconsistent")

    return {
        "passage_id": passage_id,
        "status": "stale" if reasons else "resolved",
        "reasons": reasons,
        "locator_ok": not reasons,
        "citation_check": passage_citation_check(parsed, passage_id),
    }
