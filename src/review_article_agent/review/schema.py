"""Pure, deterministic review rules for the Step 12 critique/fact-check stage.

This module is deliberately a small, dependency-light MVP.  It contains **no**
storage, network, or model calls: every function takes plain JSON-portable
``dict`` inputs and returns plain JSON-portable ``dict`` outputs.  It encodes
only the mechanical obligations from ``docs/implementation/12-review.md`` and
``docs/contracts/03-policy-decisions.md`` POL-OPS-SEVERITY:

* a critic cannot pass a factual objection by assertion alone -- it must carry a
  verbatim source basis or a concrete missing check;
* a false or fabricated objection is rejected *with a reason*;
* repairs may not silently erase qualifiers, invent numbers, change
  species/population, or add unverified citations;
* a real (verified-corpus) citation reopens checks instead of passing;
* a sentence is never labelled ``primary_verified`` here -- origin tracing is a
  later stage; secondary support is explicitly flagged;
* unresolved blocking/major findings never auto-pass: they end ``needs_repair``
  or, when the budget is exhausted or a blocking finding is disputed, ``blocked``
  with an explicit human action.

The rule set is intentionally conservative and deterministic so it can be
fixtured and traced; it does not judge arbitrary prose semantics.
"""

from __future__ import annotations

import hashlib
import re

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: Severity scale (POL-OPS-SEVERITY).  ``blocking``/``major`` prevent completion.
SEVERITIES = ("blocking", "major", "minor", "style")

#: Severities that block completion until resolved (blocking + major).
BLOCKING_SEVERITIES = ("blocking", "major")

#: Repair budgets from the specification (3 critique, 2 fact-check rounds).
ROUND_LIMITS = {"critique": 3, "fact_check": 2}

#: Objection kinds whose truth is a factual/evidence question.
FACTUAL_KINDS = ("unsupported_claim", "numeric_mismatch", "context_loss", "attribution")

#: Non-factual objection kinds (scope/preference); never required to carry basis.
NON_FACTUAL_KINDS = ("scope", "style")

#: All recognised objection kinds.
KNOWN_KINDS = FACTUAL_KINDS + NON_FACTUAL_KINDS

#: Findings whose disposition is still awaiting a writer/human decision.
_UNRESOLVED_DISPOSITIONS = ("confirmed", "disputed", "open")

#: Hedging/qualifier markers; their removal in a repair is a regression.
_QUALIFIER_PATTERNS = (
    r"\bmay\b",
    r"\bmight\b",
    r"\bcould\b",
    r"\bsuggest",
    r"\bpossibly\b",
    r"\blikely\b",
    r"\bpreliminary\b",
    r"\bassociated\b",
    r"\bcorrelat",
    r"\bin mice\b",
    r"\bmouse model\b",
)

#: Species/population term groups; a change of group name is a regression.
_SPECIES_GROUPS = (
    ("mouse", r"\bmouse\b|\bmice\b"),
    ("rat", r"\brats?\b"),
    ("human", r"\bhumans?\b|\bpatients?\b|\badults?\b|\bmen\b|\bwomen\b"),
    ("children", r"\bchildren\b|\bpediatric\b"),
    ("cells", r"\bcells?\b|\bin vitro\b"),
    ("animal", r"\bin vivo\b"),
)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class ReviewError(ReviewAgentError):
    """A review payload is malformed (unknown kind, bad target, missing basis).

    Messages name the structural defect; they never embed source text.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReviewError(message)


def _digest(payload) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _numbers(text: str) -> list:
    return _NUMBER_RE.findall(text or "")


def _text_of(value) -> str:
    """A readable string for a summary that may be a string or a mapping."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values() if isinstance(v, str))
    return ""


def _evidence_texts(entry: dict) -> list:
    """Every source text an evidence entry can ground a quote or number in."""
    texts = []
    passage = entry.get("original_passage")
    if isinstance(passage, dict) and isinstance(passage.get("text"), str):
        texts.append(passage["text"])
    summary = _text_of(entry.get("summary"))
    if summary:
        texts.append(summary)
    finding = entry.get("finding")
    if isinstance(finding, dict) and isinstance(finding.get("statement"), str):
        texts.append(finding["statement"])
    return texts


# --------------------------------------------------------------------------- #
# Package / draft indexing
# --------------------------------------------------------------------------- #
def _index_package(package) -> dict:
    _require(isinstance(package, dict), "package must be a JSON object")
    evidence = package.get("evidence")
    _require(isinstance(evidence, list), "package.evidence must be a JSON array")
    evidence_by_key: dict = {}
    for entry in evidence:
        _require(isinstance(entry, dict), "package.evidence entries must be JSON objects")
        key = entry.get("finding_key")
        _require(isinstance(key, str) and key, "package.evidence entry missing finding_key")
        evidence_by_key[key] = entry
    citations = package.get("citations")
    _require(isinstance(citations, dict), "package.citations must be a JSON object")
    verified = {
        key
        for key, record in citations.items()
        if isinstance(record, dict) and record.get("verified") is True
    }
    return {"evidence_by_key": evidence_by_key, "citations": citations, "verified_citations": verified}


def _evidence_for_citations(package_index: dict, citation_keys) -> list:
    wanted = set(citation_keys or [])
    return [
        entry
        for entry in package_index["evidence_by_key"].values()
        if wanted & set(entry.get("citation_keys") or [])
    ]


def _index_draft(draft) -> dict:
    _require(isinstance(draft, dict), "draft must be a JSON object")
    paragraphs = draft.get("paragraphs")
    _require(isinstance(paragraphs, list), "draft.paragraphs must be a JSON array")
    sentences: dict = {}
    for paragraph in paragraphs:
        _require(isinstance(paragraph, dict), "draft paragraphs must be JSON objects")
        paragraph_id = paragraph.get("paragraph_id")
        for sentence in paragraph.get("sentences") or []:
            _require(isinstance(sentence, dict), "draft sentences must be JSON objects")
            intent_id = sentence.get("intent_id")
            _require(
                isinstance(paragraph_id, str) and isinstance(intent_id, str),
                "draft sentence must carry paragraph_id/intent_id",
            )
            sentences[(paragraph_id, intent_id)] = sentence
    return sentences


def _sentence_at(draft_index: dict, target: dict) -> dict:
    _require(isinstance(target, dict), "target must be a JSON object")
    paragraph_id = target.get("paragraph_id")
    intent_id = target.get("intent_id")
    _require(
        isinstance(paragraph_id, str) and isinstance(intent_id, str),
        "target must carry paragraph_id and intent_id",
    )
    sentence = draft_index.get((paragraph_id, intent_id))
    _require(sentence is not None, "finding target does not exist in the draft")
    return sentence


# --------------------------------------------------------------------------- #
# normalize_finding
# --------------------------------------------------------------------------- #
def _validate_basis(basis, package_index: dict):
    """Return ``(finding_key, quote, evidence_entry)`` for a valid basis or ``None``.

    A basis is valid only when its quote is a verbatim substring of that evidence
    entry's original passage, summary, or finding statement.  A present-but-
    unfound quote is a *fabricated* basis (``valid=False``) and rejected later.
    """
    if basis is None:
        return None
    _require(isinstance(basis, dict), "basis must be a JSON object or absent")
    finding_key = basis.get("finding_key")
    quote = basis.get("quote")
    _require(
        isinstance(finding_key, str) and isinstance(quote, str) and quote != "",
        "basis must carry finding_key and a non-empty quote",
    )
    entry = package_index["evidence_by_key"].get(finding_key)
    _require(entry is not None, "basis names an unknown evidence finding_key")
    found = any(quote in text for text in _evidence_texts(entry))
    return {"finding_key": finding_key, "quote": quote, "found": found, "entry": entry}


def normalize_finding(raw, *, draft, package, stage, round_no) -> dict:
    """Validate one critic objection into a deterministic finding record.

    ``raw`` is ``{target:{paragraph_id,intent_id}, kind, severity, objection,
    basis?:{finding_key,quote}, missing_check?:str}``.  Raises :class:`ReviewError`
    on malformed input.  Disposition is one of ``confirmed`` (evidence-backed
    factual objection or a non-factual finding), ``open`` (concrete missing check
    still to run), or ``rejected`` (assertion-only / fabricated / false objection).
    """
    _require(isinstance(raw, dict), "finding must be a JSON object")
    _require(stage in ROUND_LIMITS, f"unknown review stage {stage!r}")
    _require(isinstance(round_no, int) and not isinstance(round_no, bool) and round_no >= 0,
             "round_no must be a non-negative integer")

    draft_index = _index_draft(draft)
    package_index = _index_package(package)
    target = raw.get("target")
    _sentence_at(draft_index, target)  # validates existence
    paragraph_id = target["paragraph_id"]
    intent_id = target["intent_id"]

    kind = raw.get("kind")
    _require(kind in KNOWN_KINDS, f"unknown finding kind {kind!r}")
    severity = raw.get("severity")
    _require(severity in SEVERITIES, f"unknown finding severity {severity!r}")
    objection = raw.get("objection")
    _require(isinstance(objection, str) and objection.strip() != "", "objection must be a non-empty string")

    missing_check = raw.get("missing_check")
    if missing_check is not None:
        _require(isinstance(missing_check, str), "missing_check must be a string when present")
        missing_check = missing_check.strip() or None

    basis_raw = raw.get("basis")
    basis = _validate_basis(basis_raw, package_index) if basis_raw is not None else None

    reason = None
    if kind in FACTUAL_KINDS:
        if basis is not None and not basis["found"]:
            disposition = "rejected"
            reason = "fabricated basis: the quoted text is not in the named source"
        elif basis is not None and _false_numeric_objection(kind, target, draft_index, basis["entry"]):
            disposition = "rejected"
            reason = "false objection: every number in the target sentence appears in the basis source"
        elif basis is not None:
            disposition = "confirmed"
        elif missing_check:
            disposition = "open"
            reason = f"concrete missing check recorded: {missing_check}"
        else:
            disposition = "rejected"
            reason = "objection by assertion only: no source basis and no concrete missing check"
    else:
        disposition = "confirmed"

    finding_id = _digest(
        [stage, round_no, [paragraph_id, intent_id], kind, objection]
    )
    return {
        "finding_id": finding_id,
        "stage": stage,
        "round_no": round_no,
        "target": {"paragraph_id": paragraph_id, "intent_id": intent_id},
        "kind": kind,
        "severity": severity,
        "objection": objection,
        "basis": None if basis is None else {"finding_key": basis["finding_key"], "quote": basis["quote"]},
        "missing_check": missing_check,
        "disposition": disposition,
        "reason": reason,
    }


def _false_numeric_objection(kind: str, target: dict, draft_index: dict, evidence_entry: dict) -> bool:
    if kind != "numeric_mismatch":
        return False
    sentence = _sentence_at(draft_index, target)
    sentence_numbers = set(_numbers(sentence.get("text", "")))
    if not sentence_numbers:
        return False
    evidence_numbers = set()
    for text in _evidence_texts(evidence_entry):
        evidence_numbers.update(_numbers(text))
    return sentence_numbers <= evidence_numbers


# --------------------------------------------------------------------------- #
# writer_response
# --------------------------------------------------------------------------- #
def writer_response(finding, *, accept: bool, reason: str) -> dict:
    """Apply the original writer's disposition to a finding.

    Accepting marks the finding ``fixed``.  A writer may reject a confirmed
    finding only with a non-empty reason: rejecting a confirmed blocking/major
    finding yields ``disputed`` (faithful disagreement, left for a human); a
    minor/style or non-confirmed finding is ``rejected``.
    """
    _require(isinstance(finding, dict), "finding must be a normalised finding object")
    _require(isinstance(accept, bool), "accept must be a boolean")
    _require(isinstance(reason, str), "reason must be a string")

    out = dict(finding)
    if accept:
        out["disposition"] = "fixed"
        out["reason"] = reason or None
        out["writer_response"] = {"accept": True, "reason": reason or None}
        return out

    _require(reason.strip() != "", "a writer rejecting a finding must give a non-empty reason")
    if finding.get("disposition") == "confirmed" and finding.get("severity") in BLOCKING_SEVERITIES:
        disposition = "disputed"
    else:
        disposition = "rejected"
    out["disposition"] = disposition
    out["reason"] = reason
    out["writer_response"] = {"accept": False, "reason": reason}
    return out


# --------------------------------------------------------------------------- #
# check_repair
# --------------------------------------------------------------------------- #
def _sentence_view(sentence) -> tuple:
    if isinstance(sentence, str):
        return sentence, []
    if isinstance(sentence, dict):
        keys = sentence.get("citation_keys") or []
        _require(isinstance(keys, list), "sentence.citation_keys must be a JSON array")
        return sentence.get("text", ""), list(keys)
    raise ReviewError("sentence must be a string or a draft sentence object")


def _species_groups(text: str) -> set:
    found = set()
    for name, pattern in _SPECIES_GROUPS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.add(name)
    return found


def check_repair(old_sentence, new_sentence, *, package) -> dict:
    """Compare a repaired sentence against its original and the package.

    Returns ``{"violations":[...], "reopen_checks": bool}``.  Violation codes:
    ``qualifier_removed``, ``invented_number``, ``species_changed``,
    ``unverified_citation``.  A newly added citation key that *is* in the
    verified corpus adds no violation but sets ``reopen_checks`` true.
    """
    package_index = _index_package(package)
    old_text, old_keys = _sentence_view(old_sentence)
    new_text, new_keys = _sentence_view(new_sentence)

    violations = []

    for pattern in _QUALIFIER_PATTERNS:
        if re.search(pattern, old_text, flags=re.IGNORECASE) and not re.search(
            pattern, new_text, flags=re.IGNORECASE
        ):
            violations.append("qualifier_removed")
            break

    if _species_groups(old_text) != _species_groups(new_text):
        violations.append("species_changed")

    evidence_entries = _evidence_for_citations(package_index, new_keys)
    allowed_numbers = set(_numbers(old_text))
    for entry in evidence_entries:
        for text in _evidence_texts(entry):
            allowed_numbers.update(_numbers(text))
    for number in _numbers(new_text):
        if number not in allowed_numbers:
            violations.append("invented_number")
            break

    reopen_checks = False
    for key in new_keys:
        if key in old_keys:
            continue
        if key in package_index["verified_citations"]:
            reopen_checks = True
        else:
            violations.append("unverified_citation")

    return {"violations": violations, "reopen_checks": reopen_checks}


# --------------------------------------------------------------------------- #
# fact_check
# --------------------------------------------------------------------------- #
def _support_label(sentence: dict) -> tuple:
    """Return ``(label, origin_field, origin_value)``; never a primary claim."""
    secondary = sentence.get("secondary") is True or "secondary_account" in (
        sentence.get("attributions") or []
    )
    if secondary:
        return "secondary_support", "origin_flag", "unresolved_secondary_origin"
    return "source_supported", "origin_status", "pending_origin_tracing"


def _check_sentence(paragraph_id: str, sentence: dict, package_index: dict) -> dict:
    intent_id = sentence.get("intent_id")
    citation_keys = sorted(set(sentence.get("citation_keys") or []))
    if sentence.get("proposed_synthesis") is True:
        check = {
            "check_id": f"fc:{paragraph_id}:{intent_id}",
            "paragraph_id": paragraph_id,
            "intent_id": intent_id,
            "citation_keys": citation_keys,
            "verdict": "not_applicable",
            "support_label": None,
            "fingerprint": None,
        }
        return check

    entries = _evidence_for_citations(package_index, citation_keys)
    verdict = "supported"
    if not entries:
        verdict = "unsupported"
    else:
        sentence_text = sentence.get("text", "")
        sentence_numbers = set(_numbers(sentence_text))
        evidence_numbers = set()
        for entry in entries:
            for text in _evidence_texts(entry):
                evidence_numbers.update(_numbers(text))
        sentence_groups = _species_groups(sentence_text)
        evidence_groups = set()
        for entry in entries:
            context = entry.get("finding", {}).get("context") if isinstance(entry.get("finding"), dict) else None
            if isinstance(context, dict):
                species = context.get("species")
                value = species.get("value") if isinstance(species, dict) else None
                if isinstance(value, str):
                    evidence_groups |= _species_groups(value)
        if sentence_groups and evidence_groups and not (sentence_groups & evidence_groups):
            verdict = "context_mismatch"
        elif sentence_numbers and not sentence_numbers <= evidence_numbers:
            verdict = "numeric_mismatch"

    label, origin_field, origin_value = _support_label(sentence)
    fingerprint = _digest(_fingerprint_payload(sentence, citation_keys, entries))
    check = {
        "check_id": f"fc:{paragraph_id}:{intent_id}",
        "paragraph_id": paragraph_id,
        "intent_id": intent_id,
        "citation_keys": citation_keys,
        "verdict": verdict,
        "support_label": label,
        origin_field: origin_value,
        "fingerprint": fingerprint,
    }
    return check


def _fingerprint_payload(sentence: dict, citation_keys, entries) -> list:
    matched = []
    for entry in sorted(entries, key=lambda e: e.get("finding_key", "")):
        revisions = []
        for ref in entry.get("source_refs") or []:
            if isinstance(ref, dict):
                revisions.append(ref.get("revision_id"))
        matched.append(
            {
                "finding_key": entry.get("finding_key"),
                "texts": _evidence_texts(entry),
                "revisions": revisions,
            }
        )
    return [sentence.get("text", ""), citation_keys, matched]


def fact_check(draft, package) -> list:
    """Fact-check every draft sentence against the package's verified evidence.

    Returns one check per sentence, in draft order.  A ``proposed_synthesis``
    sentence is recorded ``not_applicable``.  Verdicts are ``supported``,
    ``unsupported``, ``numeric_mismatch``, or ``context_mismatch``; the support
    label is ``source_supported`` (pending origin tracing) or, for secondary
    sentences/attributions, ``secondary_support`` with an unresolved-origin flag.
    The string ``primary_verified`` is never emitted.
    """
    package_index = _index_package(package)
    checks = []
    for paragraph in draft.get("paragraphs"):
        paragraph_id = paragraph.get("paragraph_id")
        for sentence in paragraph.get("sentences") or []:
            checks.append(_check_sentence(paragraph_id, sentence, package_index))
    return checks


def stale_checks(checks, draft, package) -> list:
    """Return check ids whose fingerprint no longer matches the current draft/package.

    A changed sentence text, citation set, evidence passage, or source revision
    changes the recomputed fingerprint, invalidating the earlier check.
    """
    package_index = _index_package(package)
    draft_index = _index_draft(draft)
    stale = []
    for check in checks:
        if check.get("verdict") == "not_applicable":
            continue
        sentence = draft_index.get((check.get("paragraph_id"), check.get("intent_id")))
        if sentence is None:
            stale.append(check.get("check_id"))
            continue
        entries = _evidence_for_citations(package_index, sorted(set(sentence.get("citation_keys") or [])))
        fingerprint = _digest(
            _fingerprint_payload(sentence, sorted(set(sentence.get("citation_keys") or [])), entries)
        )
        if fingerprint != check.get("fingerprint"):
            stale.append(check.get("check_id"))
    return stale


# --------------------------------------------------------------------------- #
# evaluate
# --------------------------------------------------------------------------- #
def evaluate(findings, checks, rounds_used: dict) -> dict:
    """Decide the stage outcome; unresolved blockers never auto-pass.

    ``complete`` requires no unresolved blocking/major finding and every
    non-``not_applicable`` check ``supported``; minor/style findings may remain.
    Round exhaustion or a disputed blocking/major finding yields ``blocked`` with
    an explicit ``required_human_action`` -- never a silent pass.
    """
    _require(isinstance(rounds_used, dict), "rounds_used must be a JSON object")

    unresolved_ids = []
    blockers = []
    disputed_blockers = []
    for finding in findings:
        disposition = finding.get("disposition")
        if disposition not in _UNRESOLVED_DISPOSITIONS:
            continue
        unresolved_ids.append(finding.get("finding_id"))
        if finding.get("severity") in BLOCKING_SEVERITIES:
            blockers.append(finding)
            if disposition == "disputed":
                disputed_blockers.append(finding)

    unresolved_checks = [
        check
        for check in checks
        if check.get("verdict") not in (None, "not_applicable", "supported")
    ]
    unresolved_ids.extend(check.get("check_id") for check in unresolved_checks)

    if not blockers and not unresolved_checks:
        return {"status": "complete", "unresolved": unresolved_ids, "required_human_action": None}

    if disputed_blockers:
        return {
            "status": "blocked",
            "unresolved": unresolved_ids,
            "required_human_action": (
                "A blocking/major finding is disputed by the writer; a human must "
                "adjudicate the disagreement (consensus is not forced)."
            ),
        }

    def _exhausted(stage: str) -> bool:
        limit = ROUND_LIMITS.get(stage)
        return limit is not None and rounds_used.get(stage, 0) >= limit

    exhausted = any(_exhausted(finding.get("stage")) for finding in blockers)
    if unresolved_checks and _exhausted("fact_check"):
        exhausted = True

    if exhausted:
        return {
            "status": "blocked",
            "unresolved": unresolved_ids,
            "required_human_action": (
                "Repair budget exhausted with unresolved blocking/major findings or "
                "failed checks; a human must resolve or re-authorize the work."
            ),
        }

    return {"status": "needs_repair", "unresolved": unresolved_ids, "required_human_action": None}
