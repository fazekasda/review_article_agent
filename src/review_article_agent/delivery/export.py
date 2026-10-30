"""Pure, deterministic Step 14 export rendering and pre-export checks (MVP).

Plain ``dict``-in/``dict``-out with no storage, network, or model calls -- the
same style as :mod:`review_article_agent.tracing.schema`.  Everything here is a
*mechanical* consistency/render step.  It cannot establish scientific truth, and
the generated text never asserts independent or held-out validation, nor
submission compliance, without an explicit human verification basis.

Authority: ``docs/implementation/14-delivery.md`` (deliverable: Markdown, BibTeX,
RIS, disclosures; acceptance criterion: citation keys/bibliography
identities/versions/RIS remain consistent, no duplicate-as-independent evidence or
model-invented reference is introduced during export, hashes are validated
immediately before export, export material is emitted only under the ratified
contract and applicable venue policy).  ``docs/contracts/03-policy-decisions.md``
POL-P4 (companion fail-closed), POL-P5 (AI-use declaration + human
accountability), POL-P6 (venue-neutral output; no compliance assertion without a
selected, human-verified venue), POL-OPS-SECONDARY (visible secondary-origin
warnings).

Reference record (approved corpus)::

    {"key", "identity": {"doi"?, "pmid"?}, "version", "title",
     "authors": [...], "year", "journal"?}

Manuscript: ``{"revision_id", "text"}`` with Pandoc citations ``[@key]`` /
``[@a; @b]``.
"""

from __future__ import annotations

import hashlib
import json
import re

from ..domain.errors import ReviewAgentError

#: Accepted companion + summary is the ratified POL-P4 reporting format.
_REPORTING_MET = "met"
_REPORTING_UNMET = "unmet"

#: Explicit fail-closed marker (POL-P4): absent companion, the export records that
#: the accepted reporting requirement is *unmet* rather than silently omitting it.
_UNMET_MARKER = "REPORTING REQUIREMENT UNMET"

#: A citation bracket token; keys are taken from the ``@key`` form.
_CITATION_RE = re.compile(r"\[([^\]]*)\]")

#: Phrases that must never be asserted without a human verification basis.
_FORBIDDEN_CLAIM_SUBSTRINGS = (
    "independently validated",
    "held-out validation",
    "submission compliance",
)


class ExportError(ReviewAgentError):
    """An export request is malformed, stale, or inconsistent.

    Messages name the structural defect only; they never embed manuscript text.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity_pair(identity) -> tuple:
    """Normalised ``(doi, pmid)`` pair for identity comparison."""
    if not isinstance(identity, dict):
        return (None, None)
    return (identity.get("doi"), identity.get("pmid"))


def _canonical_json_text(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# cited_keys
# --------------------------------------------------------------------------- #
def cited_keys(text: str) -> list:
    """Return the ordered, unique citation keys of *text*.

    Keys are the ``@key`` tokens inside Pandoc-style brackets (``[@a]``,
    ``[@a; @b]``, ``[@a, p. 3]``); a locator after the key is ignored.  Order of
    first appearance is preserved.
    """
    seen: set = set()
    ordered: list = []
    for match in _CITATION_RE.finditer(text or ""):
        for part in match.group(1).split(";"):
            part = part.strip()
            if not part.startswith("@"):
                continue
            key = re.split(r"[\s,]", part[1:].strip(), maxsplit=1)[0].strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


# --------------------------------------------------------------------------- #
# check_consistency
# --------------------------------------------------------------------------- #
def check_consistency(manuscript_text: str, references, corpus) -> dict:
    """Check manuscript citations, bibliography, and approved corpus agree.

    ``corpus`` maps each approved key to ``{"identity", "version"}``.  Returns
    ``{"ok": bool, "errors": [...]}`` where each error is
    ``{"code", "key", "detail"}``.  Error codes:

    * ``missing_reference`` -- a cited key has no bibliography entry;
    * ``invented_reference`` -- a bibliography key is not in the approved corpus;
    * ``version_mismatch`` / ``identity_mismatch`` -- a bibliography entry
      disagrees with the approved corpus record;
    * ``duplicate_identity`` -- two keys share a DOI or PMID (a duplicate
      presented as independent evidence);
    * ``uncited_reference`` -- a bibliography entry is never cited.
    """
    _require(isinstance(corpus, dict), "corpus must be a JSON object")
    references = list(references or [])
    cited = cited_keys(manuscript_text)

    ref_by_key: dict = {}
    errors: list = []
    for ref in references:
        _require(isinstance(ref, dict), "reference must be a JSON object")
        key = ref.get("key")
        ref_by_key[key] = ref

    for key in cited:
        if key not in ref_by_key:
            errors.append(
                {"code": "missing_reference", "key": key, "detail": "cited key has no bibliography entry"}
            )

    for ref in references:
        key = ref.get("key")
        if key not in corpus:
            errors.append(
                {"code": "invented_reference", "key": key, "detail": "bibliography key is not in the approved corpus"}
            )
            continue
        approved = corpus.get(key) or {}
        if ref.get("version") != approved.get("version"):
            errors.append(
                {
                    "code": "version_mismatch",
                    "key": key,
                    "detail": "bibliography version differs from the approved corpus record",
                }
            )
        if _identity_pair(ref.get("identity")) != _identity_pair(approved.get("identity")):
            errors.append(
                {
                    "code": "identity_mismatch",
                    "key": key,
                    "detail": "bibliography identity differs from the approved corpus record",
                }
            )

    # Duplicate-as-independent: the same DOI/PMID attributed to two keys.
    by_doi: dict = {}
    by_pmid: dict = {}
    for ref in references:
        identity = ref.get("identity") if isinstance(ref.get("identity"), dict) else {}
        key = ref.get("key")
        if identity.get("doi"):
            by_doi.setdefault(identity["doi"], []).append(key)
        if identity.get("pmid"):
            by_pmid.setdefault(identity["pmid"], []).append(key)
    for field, groups in (("doi", by_doi), ("pmid", by_pmid)):
        for value in sorted(groups):
            keys = sorted(set(groups[value]))
            if len(keys) > 1:
                errors.append(
                    {
                        "code": "duplicate_identity",
                        "key": keys[0],
                        "detail": f"keys {keys} share {field}={value}; duplicate presented as independent evidence",
                    }
                )

    cited_set = set(cited)
    for ref in references:
        key = ref.get("key")
        if key not in cited_set:
            errors.append(
                {"code": "uncited_reference", "key": key, "detail": "bibliography entry is never cited"}
            )

    return {"ok": not errors, "errors": errors}


# --------------------------------------------------------------------------- #
# render_bibtex / render_ris
# --------------------------------------------------------------------------- #
def render_bibtex(references) -> str:
    """Render deterministic BibTeX, one ``@article`` per reference.

    Keys and identities are written exactly as given; fields absent from a record
    are never invented.
    """
    entries: list = []
    for ref in references or []:
        _require(isinstance(ref, dict), "reference must be a JSON object")
        fields: list = []
        if ref.get("authors"):
            fields.append("  author = {" + " and ".join(str(a) for a in ref["authors"]) + "}")
        if ref.get("title") is not None:
            fields.append("  title = {" + str(ref["title"]) + "}")
        if ref.get("journal") is not None:
            fields.append("  journal = {" + str(ref["journal"]) + "}")
        if ref.get("year") is not None:
            fields.append("  year = {" + str(ref["year"]) + "}")
        identity = ref.get("identity") if isinstance(ref.get("identity"), dict) else {}
        if identity.get("doi"):
            fields.append("  doi = {" + str(identity["doi"]) + "}")
        body = ",\n".join(fields)
        entries.append("@article{" + str(ref.get("key")) + ",\n" + body + "\n}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def render_ris(references) -> str:
    """Render deterministic RIS, one ``TY  - JOUR`` entry per reference.

    Tags are emitted only for fields present in the record; the DOI/PMID are taken
    from the record's identity exactly as given.
    """
    entries: list = []
    for ref in references or []:
        _require(isinstance(ref, dict), "reference must be a JSON object")
        lines: list = ["TY  - JOUR", "ID  - " + str(ref.get("key"))]
        if ref.get("title") is not None:
            lines.append("TI  - " + str(ref["title"]))
        for author in ref.get("authors") or []:
            lines.append("AU  - " + str(author))
        if ref.get("journal") is not None:
            lines.append("JO  - " + str(ref["journal"]))
        if ref.get("year") is not None:
            lines.append("PY  - " + str(ref["year"]))
        identity = ref.get("identity") if isinstance(ref.get("identity"), dict) else {}
        if identity.get("doi"):
            lines.append("DO  - " + str(identity["doi"]))
        if identity.get("pmid"):
            lines.append("AN  - " + str(identity["pmid"]))
        lines.append("ER  - ")
        entries.append("\n".join(lines))
    return "\n".join(entries) + ("\n" if entries else "")


# --------------------------------------------------------------------------- #
# build_disclosures
# --------------------------------------------------------------------------- #
def _ai_declaration(ai_use) -> str:
    lines = [
        "# AI-use declaration",
        "",
        "This declaration records the actual AI use in producing this draft. "
        "It does not assert validation beyond the recorded AI use.",
        "",
    ]
    if ai_use:
        lines.append("| Role | Model | Version |")
        lines.append("| --- | --- | --- |")
        for entry in ai_use:
            lines.append(
                "| {role} | {model} | {version} |".format(
                    role=entry.get("role", ""),
                    model=entry.get("model", ""),
                    version=entry.get("version", ""),
                )
            )
    else:
        lines.append("No AI use was recorded for this draft.")
    lines += [
        "",
        "## Accountability",
        "",
        "A named human author remains accountable for every claim, citation, and "
        "disclosure in the final manuscript, and for any use of these outputs.",
        "",
    ]
    return "\n".join(lines)


def build_disclosures(policy, ai_use, venue=None, warnings=None) -> dict:
    """Build the reporting companion, summary, and AI-use declaration.

    ``policy`` is ``{"companion_accepted", "journal_policy_verified"}``; ``ai_use``
    is a list of ``{"role", "model", "version"}``.  If the companion was not
    accepted the companion text carries an explicit unmet marker (POL-P4
    fail-closed).  The venue compliance field is ``not_asserted`` unless a venue is
    selected *and* its journal policy is human-verified (POL-P5/POL-P6); the
    generated text never claims independent validation or submission compliance.
    """
    _require(isinstance(policy, dict), "policy must be a JSON object")
    companion_accepted = bool(policy.get("companion_accepted"))
    journal_verified = bool(policy.get("journal_policy_verified"))

    reporting_requirement = _REPORTING_MET if companion_accepted else _REPORTING_UNMET

    companion: list = ["# Methods and limitations companion", ""]
    if companion_accepted:
        companion.append(
            "This companion reports the workflow's selection, synthesis, and "
            "limitations, as accepted under POL-P4."
        )
    else:
        companion.append(
            f"{_UNMET_MARKER}: the accepted methods/limitations companion was not "
            "produced, so this export must not be treated as meeting the reporting "
            "requirement."
        )
    companion += [
        "",
        "## Limitations",
        "",
        "- Selection and synthesis use deterministic heuristics and sampled audits; "
        "these are not a held-out evaluation.",
        "- Reported origin/attribution states are mechanical and are not a claim of "
        "scientific correctness.",
    ]

    if warnings:
        companion += [
            "",
            "## Secondary-origin warnings",
            "",
            "The following claims rest on a secondary source that was actually read; "
            "they remain warned and are not primary verified. Final human approval "
            "against the exact revision is required.",
            "",
        ]
        for warning in warnings:
            companion.append(
                "- claim `{claim}` via `{src}`: {text}".format(
                    claim=warning.get("claim_id", ""),
                    src=warning.get("cite_source_key", ""),
                    text=warning.get("text", ""),
                )
            )

    summary_md = "\n".join(
        [
            "# Summary",
            "",
            f"Reporting requirement: {reporting_requirement}.",
            "",
            "Selection/synthesis decisions and limitations are reported in the "
            "companion; the manuscript text is unchanged. No validation beyond the "
            "recorded mechanical checks is asserted by this export.",
            "",
        ]
    )

    ai_declaration_md = _ai_declaration(list(ai_use or []))

    if venue is None:
        venue_compliance = "not_asserted"
    elif journal_verified:
        venue_compliance = "verified_by_human"
    else:
        venue_compliance = "not_asserted"

    companion_md_text = "\n".join(companion)
    for text in (companion_md_text, summary_md, ai_declaration_md):
        lowered = text.lower()
        for phrase in _FORBIDDEN_CLAIM_SUBSTRINGS:
            _require(phrase not in lowered, f"disclosure text must not assert {phrase!r}")

    return {
        "companion_md": companion_md_text,
        "summary_md": summary_md,
        "ai_declaration_md": ai_declaration_md,
        "reporting_requirement": reporting_requirement,
        "venue_compliance": venue_compliance,
    }


# --------------------------------------------------------------------------- #
# build_export_package
# --------------------------------------------------------------------------- #
def build_export_package(manuscript, references, corpus, approval, current_hashes, disclosures) -> dict:
    """Validate the exact revision/hashes, check consistency, and render the export.

    Immediately before export the approval must hold for the current manuscript
    revision and its approved artifact hashes must equal ``current_hashes``
    exactly; otherwise a stale-approval :class:`ExportError` is raised.  Any
    consistency error also aborts the export.
    """
    _require(isinstance(manuscript, dict), "manuscript must be a JSON object")
    _require(isinstance(approval, dict), "approval must be a JSON object")
    _require(approval.get("approved") is True, "stale approval: export is not approved")
    _require(
        approval.get("manuscript_revision") == manuscript.get("revision_id"),
        "stale approval: approved manuscript revision does not match the current revision",
    )
    _require(
        approval.get("artifact_hashes") == current_hashes,
        "stale approval: approved artifact hashes do not match the current hashes",
    )

    check = check_consistency(manuscript.get("text", ""), references, corpus)
    if not check["ok"]:
        raise ExportError("export aborted: consistency errors: " + _canonical_json_text(check["errors"]))

    files = {
        "manuscript.md": manuscript.get("text", ""),
        "references.bib": render_bibtex(references),
        "references.ris": render_ris(references),
        "companion.md": disclosures.get("companion_md", ""),
        "summary.md": disclosures.get("summary_md", ""),
        "ai_declaration.md": disclosures.get("ai_declaration_md", ""),
    }

    file_hashes = {name: _sha256(content) for name, content in files.items()}
    manifest = {
        "manuscript_revision": manuscript.get("revision_id"),
        "approved_hashes": current_hashes,
        "file_hashes": file_hashes,
    }
    files["export_manifest.json"] = _canonical_json_text(manifest) + "\n"

    checks = {
        "consistency": check,
        "approval": {
            "manuscript_revision": approval.get("manuscript_revision"),
            "artifact_hashes": approval.get("artifact_hashes"),
        },
    }
    return {"files": files, "checks": checks}


__all__ = [
    "ExportError",
    "build_disclosures",
    "build_export_package",
    "check_consistency",
    "cited_keys",
    "render_bibtex",
    "render_ris",
]
