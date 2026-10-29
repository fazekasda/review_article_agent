"""Exact, human-approved **corpus freeze / search-again decisions** (Step 05, W3).

Authority
---------

* ``docs/implementation/05-discovery.md`` deliverable 3 (query runs, identity
  reconciliation, citation expansion, coverage report, and an **explicit**
  freeze / search-again decision) and acceptance criterion 6 ("Human approval
  freezes a specific corpus revision; subsequent imports/searches do not mutate
  it silently").
* ``docs/implementation/05-discovery.md`` acceptance criterion 5 (a
  known-relevant fixture set and forward/backward citation expansion expose
  explicit coverage gaps; reporting **never claims absolute completeness**).
* ``docs/specification.md`` confirmed workflow step 5 ("a coverage report
  supports an explicit human decision to search again or freeze the candidate
  revision; never promise all papers") and the gates/scientific invariants
  ("approvals bind to immutable artifact revisions ... never filenames or
  generic tool permissions"; "material edits invalidate affected checks").
* ``docs/contracts/03-policy-decisions.md`` ``POL-OPS-COVERAGE`` (the report
  records sources/queries/dates/terminology/citation expansion/yields/
  duplicates/gaps; **no universal numeric stopping/completeness rule**; the
  exact candidate revision is frozen by **explicit human approval**) and
  ``POL-P3`` (integrity-status checks at corpus entry: the retraction /
  expression-of-concern / correction statuses are **distinguished** with
  statuses/provenance/date; an **unknown** integrity status is **not** clean;
  non-replication is **not** conflated with retraction; historical discussion is
  **not** automatically prohibited).
* ``docs/contracts/01-identities.md`` §3.2 (a revision is one **immutable**
  snapshot; a change is always a new revision) and §3.3 (a content hash is a
  byte-integrity key only, never an identity).
* ``docs/contracts/02-gates-and-runtime.md`` §1.3 (authority provenance is
  out-of-band; a payload cannot mint authority), §2 (a human approval binds an
  exact artifact revision + gate kind + gate id; a generic tool permission never
  satisfies it), and §3 (stale vs invalidated vs superseded).

What this module **is**
-----------------------

The **decision half** of Step 05 that turns the already-durable coverage report
and cumulative candidate corpus into one exact, human-approved freeze record. It
is built **on top of** existing immutable layers and adds **no parallel table**:

* :class:`~review_article_agent.discovery.candidates.CandidateRepository` supplies
  the exact cumulative candidate snapshot (its stable candidate ids and exact
  membership);
* :class:`~review_article_agent.discovery.reporting.CoverageRepository` supplies
  the exact coverage report whose ``plan``/``corpus`` references and digests are
  the freeze's own pins;
* :func:`~review_article_agent.discovery.context.require_approved_context` proves
  the plan behind that coverage report is **currently** approved;
* :class:`~review_article_agent.storage.artifacts.ArtifactRepository` stores the
  decision as an immutable ``report`` revision (default logical id
  :data:`DEFAULT_FREEZE_ARTIFACT_ID`) whose canonical document is
  ``{schema_version, document_type: "discovery_freeze_decision", payload}``;
* the **real** human gate (:func:`prepare_gate_snapshot` +
  :meth:`HumanApprovalRepository.record_decision`, controlling terminal) is the
  only thing that authorizes a freeze. The exact named gate instance is
  ``gate_kind = "artifact_revision"`` with the fixed ``gate_id = "discovery.freeze"``
  (:data:`FREEZE_GATE_KIND` / :data:`FREEZE_GATE_ID`).

:class:`FreezeRepository` exposes exactly the operations below (plus a history
helper):

* :meth:`FreezeRepository.propose` records (or replays) an **inert** decision
  proposal: the proposed ``decision`` (``"freeze"`` / ``"search_again"``), the
  exact corpus and coverage references and digests, the exact plan reference, the
  **explicit per-candidate entry reviews** (checked-at UTC + provenance
  reference, the four distinguished integrity statuses, a disposition, and a
  rationale), the explicit coverage-limitation acknowledgement, and the
  rationale. It grants **no** authority: there is deliberately no
  ``approved``/``actor``/``authority`` field.
* :meth:`FreezeRepository.get` reads one exact decision revision by explicit
  same-project ``{project_id, artifact_id, revision_id}`` reference, fully
  re-derived from durable state.
* :meth:`FreezeRepository.current_decision` returns the **currently authorized**
  decision document (freeze or search-again) or ``None``.
* :meth:`FreezeRepository.frozen_corpus` returns the **last human-approved
  freeze selection** (never the latest raw proposal), with the computed approval
  status visible, or ``None``.
* :meth:`FreezeRepository.require_frozen` returns the selection **only** when the
  exact revision carries a currently *active* authentic human approval, the plan
  is still approved, and every pinned input is intact. Any stale/forged/
  wrong-gate/missing-coverage state is refused.
* :meth:`FreezeRepository.history` lists the decisions newest-first with their
  computed approval status (a read-only aid).

**Freeze immutability (corpus side).** :func:`corpus_has_authentic_freeze` is a
small **read-only** helper that :class:`CandidateRepository` calls so a default
ingest can never silently append a new revision to a logical corpus artifact
that already carries an authentic approved freeze; the caller must choose a
**new** corpus artifact. Later observations still record, and a new corpus
artifact reconciles freely, leaving the old frozen roster unchanged.

What this module **is not** (the boundary)
------------------------------------------

* **Not science, not eligibility, not verified support, and not a completeness
  claim.** A frozen selection is only an explicit human *selection* of exact
  publication candidates; ``authorizing`` and ``completeness_claimed`` are always
  ``False``, and no candidate's integrity is asserted clean by storing a freeze.
* **Not an approval, a network, a model, a worker, or a CLI actor.** It performs
  no network/provider/model/paid call, and it does **not** mint authority: a
  stored proposal cannot freeze anything, and only the exact named human gate
  over the exact revision authorizes a freeze.
* **Not a guarantee beyond corruption detection.** The revision digest validates
  accidental/tampered **corruption only**; it is not an authenticity signature.

Determinism, replay, and bounds
-------------------------------

The builder never reads a wall clock: its output is a pure function of durable
inputs, so an identical replay produces byte-identical content. Replay / conflict
uses the existing :class:`~review_article_agent.storage.artifacts.ArtifactRepository`
request key: an identical ``request_id`` replay returns the historical revision;
a divergent reuse is a static :class:`FreezeConflictError`. A genuinely new
revision names the artifact's current head as its exact parent, so a concurrent
stale write is refused rather than overwriting.

Every failure raises a static, value-free error: no run id, candidate id, query,
source, URL, title, identifier, digest, or payload byte is ever echoed.

Payload schema (version 1)
--------------------------

``payload`` is a JSON object with exactly these keys::

    {
      "payload_version": 1,
      "request_id": <opaque id>,
      "input_binding": { ...logical replay identity... },
      "decision": "freeze" | "search_again",
      "corpus": {
        "ref": {project_id, artifact_id, revision_id},
        "content_sha256": <str>,
        "document_type": "discovery_corpus",
        "request_id": <str>,
        "candidate_ids": [<sorted ids>],
        "resolved_ids": [<sorted ids>],
        "unresolved_ids": [<sorted ids>]
      },
      "coverage": {
        "ref": {...}, "content_sha256": <str>,
        "document_type": "discovery_coverage",
        "plan_ref": {...}, "plan_content_sha256": <str>,
        "corpus_ref": {...}, "corpus_content_sha256": <str>,
        "gaps": { ...derived gap counters/flags... },
        "limitations_count": <int>
      },
      "plan": {"ref": {...}, "content_sha256": <str>,
               "document_type": "discovery_plan"},
      "entry_reviews": [
        {"candidate_id": <id>,
         "checked_at": <RFC3339 UTC timestamp>,
         "provenance": {"ref": <str>, "source": <str>},
         "retraction": "unknown"|"none"|"flagged",
         "expression_of_concern": "unknown"|"none"|"flagged",
         "correction": "unknown"|"none"|"flagged",
         "non_replication": "unknown"|"none"|"flagged",
         "disposition": "include"|"exclude"|"defer"|"no_scientific_use",
         "rationale": <non-empty str>}
      ],
      "selection": {"included": [...], "excluded": [...],
                    "deferred": [...], "no_scientific_use": [...]},
      "coverage_acceptance": {"acknowledged": <bool>,
                              "text": <str|null>, "gaps": {...}},
      "rationale": <non-empty str>,
      "authorizing": false,
      "completeness_claimed": false,
      "notice": <str>
    }

Entry-review rules (all enforced on write **and** re-derived on read):

* every candidate in the pinned corpus is represented exactly once (a missing or
  extra review is refused);
* an ``include`` disposition requires a **resolved** candidate and all four
  integrity statuses **known** (not ``"unknown"``) — an unknown integrity status
  is never silently clean;
* an unresolved (missing-identifier) or identifier-conflict candidate can never
  be ``include``; it needs an explicit ``exclude``/``defer``/``no_scientific_use``;
* a ``flagged`` status still requires a human rationale but does **not** force an
  exclusion (historical discussion is not automatically prohibited), and
  non-replication is a distinct status, never conflated with retraction;
* a rationale is required on every entry.

A ``freeze`` whose coverage report exposes any gap (known-relevant misses,
citation gaps, an unperformed expansion, an unassessed known-relevant set, a
source failure, or missing/conflict candidates) is refused unless the exact
decision explicitly acknowledges the coverage limitations
(``accept_coverage_limitations``); there is **no** numeric completeness
threshold. ``search_again`` may carry unresolved-review gaps and an explicit
next-search rationale.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..domain.errors import StoreError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..storage.artifacts import (
    ArtifactConflictError,
    ArtifactRepository,
    Revision,
    sha256_digest,
)
from ..storage.human_approvals import HumanApprovalRepository
from .candidates import (
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
)
from .context import (
    PLAN_GATE_ID,
    PROTOCOL_GATE_KIND,
    require_approved_context,
)
from .reporting import (
    DEFAULT_COVERAGE_ARTIFACT_ID,
    CoverageRepository,
)

__all__ = [
    "DOCUMENT_TYPE",
    "FREEZE_ENVELOPE_SCHEMA_VERSION",
    "FREEZE_PAYLOAD_VERSION",
    "DEFAULT_FREEZE_ARTIFACT_ID",
    "DEFAULT_CORPUS_ARTIFACT_ID",
    "DEFAULT_COVERAGE_ARTIFACT_ID",
    "FREEZE_GATE_KIND",
    "FREEZE_GATE_ID",
    "MAX_REQUEST_ID_CHARS",
    "DECISIONS",
    "DISPOSITIONS",
    "INCLUDABLE_DISPOSITIONS",
    "INTEGRITY_STATUSES",
    "INTEGRITY_FIELDS",
    "GAP_KEYS",
    "FREEZE_NOTICE",
    "FreezeError",
    "FreezeValidationError",
    "FreezeConflictError",
    "FreezeCorruptionError",
    "FreezeNotAuthorizedError",
    "FreezeRepository",
    "corpus_has_authentic_freeze",
]

#: The generic-document ``document_type`` of one freeze decision record.
DOCUMENT_TYPE = "discovery_freeze_decision"

#: The only supported generic-document envelope ``schema_version``.
FREEZE_ENVELOPE_SCHEMA_VERSION = 1

#: The only supported decision ``payload_version``.
FREEZE_PAYLOAD_VERSION = 1

#: Default logical artifact that carries the freeze decision revision.
DEFAULT_FREEZE_ARTIFACT_ID = "discovery.freeze"

#: The exact named human gate instance that authorizes a freeze (C02 §2.1).
FREEZE_GATE_KIND = "artifact_revision"
FREEZE_GATE_ID = "discovery.freeze"

#: Request replay keys follow the existing bounded opaque-identifier convention.
MAX_REQUEST_ID_CHARS = 512

#: Origin-locator prefix shared by every freeze-decision revision (cheap filter).
_ORIGIN_PREFIX = "discovery-freeze:"

#: The exact decision enum.
DECISIONS = ("freeze", "search_again")

#: The exact per-candidate disposition enum.
DISPOSITIONS = ("include", "exclude", "defer", "no_scientific_use")

#: Dispositions that keep a candidate out of the downstream usable selection.
INCLUDABLE_DISPOSITIONS = ("include",)

#: The exact integrity-status enum. ``"unknown"`` is explicitly **not** clean.
INTEGRITY_STATUSES = ("unknown", "none", "flagged")

#: The four **distinguished** integrity checks (non-replication is never retraction).
INTEGRITY_FIELDS = (
    "retraction",
    "expression_of_concern",
    "correction",
    "non_replication",
)

#: The exact keys of the derived coverage-gap object.
GAP_KEYS = (
    "known_relevant_missing",
    "known_relevant_not_assessed",
    "citation_gaps",
    "expansion_not_performed",
    "source_failures",
    "missing_identifier_candidates",
    "identifier_conflict_candidates",
)

#: Static, value-free non-claim attached to every freeze decision record.
FREEZE_NOTICE = (
    "Non-authorizing freeze-decision record: it pins one exact corpus and coverage "
    "revision and the explicit human entry-review dispositions for that corpus. It "
    "asserts no scientific eligibility, no verified support, and no literature "
    "completeness, and it freezes nothing by itself; a corpus is frozen only while "
    "the exact named human gate instance artifact_revision/discovery.freeze approves "
    "this exact revision."
)

# --------------------------------------------------------------------------- #
# Payload key sets
# --------------------------------------------------------------------------- #
_PAYLOAD_KEYS = frozenset(
    {
        "payload_version",
        "request_id",
        "input_binding",
        "decision",
        "corpus",
        "coverage",
        "plan",
        "entry_reviews",
        "selection",
        "coverage_acceptance",
        "rationale",
        "authorizing",
        "completeness_claimed",
        "notice",
    }
)
_CORPUS_KEYS = frozenset(
    {
        "ref",
        "content_sha256",
        "document_type",
        "request_id",
        "candidate_ids",
        "resolved_ids",
        "unresolved_ids",
    }
)
_COVERAGE_KEYS = frozenset(
    {
        "ref",
        "content_sha256",
        "document_type",
        "plan_ref",
        "plan_content_sha256",
        "corpus_ref",
        "corpus_content_sha256",
        "gaps",
        "limitations_count",
    }
)
_PLAN_KEYS = frozenset({"ref", "content_sha256", "document_type"})
_ENTRY_KEYS = frozenset(
    {
        "candidate_id",
        "checked_at",
        "provenance",
        "retraction",
        "expression_of_concern",
        "correction",
        "non_replication",
        "disposition",
        "rationale",
    }
)
_PROVENANCE_KEYS = frozenset({"ref", "source"})
_SELECTION_KEYS = frozenset(
    {"included", "excluded", "deferred", "no_scientific_use"}
)
_ACCEPTANCE_KEYS = frozenset({"acknowledged", "text", "gaps"})

_REF_KEYS = ("project_id", "artifact_id", "revision_id")

#: Guarantee keys that would (falsely) read as authority or completeness.
_FORBIDDEN_TRUE = ("authorizing", "completeness_claimed")


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class FreezeError(StoreError):
    """Base class for every freeze-decision failure.

    Every message is static and curated: it never echoes a candidate id, run id,
    query, source, URL, title, identifier, digest, or payload byte.
    """


class FreezeValidationError(FreezeError, ValueError):
    """The supplied freeze inputs are malformed, missing, or unacceptable."""


class FreezeConflictError(FreezeError):
    """A divergent reuse of an existing freeze request key."""


class FreezeCorruptionError(FreezeError):
    """A stored freeze decision fails a digest, envelope, payload, or pin check."""


class FreezeNotAuthorizedError(FreezeError):
    """No currently-active authentic freeze authorizes the requested selection."""


_M_BAD_REQUEST_ID = "a freeze request id is not a valid bounded identifier"
_M_BAD_ARTIFACT_ID = "the freeze artifact id is not a valid identifier"
_M_BAD_REF = "the freeze reference is not a valid same-project revision reference"
_M_NOT_FOUND = "no freeze decision revision exists for the supplied reference"
_M_BAD_CORPUS = "the freeze corpus reference or snapshot is not valid"
_M_BAD_COVERAGE = "the freeze coverage reference or report is not valid"
_M_BAD_PLAN = "the freeze plan reference or document is not valid"
_M_MISMATCH = "the freeze coverage report does not pin the supplied corpus exactly"
_M_PLAN_NOT_APPROVED = "the freeze plan is not a currently approved discovery plan"
_M_BAD_DECISION = "the freeze decision is not one of the approved decisions"
_M_BAD_ENTRY_REVIEWS = "the freeze entry reviews are not a valid per-candidate review set"
_M_BAD_ACCEPTANCE = "the freeze coverage-limitation acknowledgement is not acceptable"
_M_NEEDS_ACK = (
    "a freeze over a coverage report with explicit gaps requires an explicit "
    "coverage-limitation acknowledgement; no numeric completeness threshold exists"
)
_M_BAD_RATIONALE = "the freeze rationale is not a valid non-empty bounded string"
_M_BAD_INPUT = "the freeze arguments are not acceptable to the artifact store"
_M_DIVERGENT = "a different freeze decision is already recorded for this request key"
_M_CORRUPT_DIGEST = "a freeze decision content digest does not match its stored bytes"
_M_CORRUPT_ENVELOPE = "a freeze decision is not a valid versioned freeze document"
_M_CORRUPT_CANONICAL = "a freeze decision is not stored as canonical bytes"
_M_CORRUPT_PAYLOAD = "a freeze decision payload is structurally invalid"
_M_CORRUPT_PIN = "a freeze decision pin disagrees with its stored revision"
_M_NOT_FROZEN = (
    "no currently active authentic human-approved freeze authorizes this selection; "
    "the corpus is not frozen"
)


# --------------------------------------------------------------------------- #
# Small strict helpers
# --------------------------------------------------------------------------- #
def _fail(message: str, error=FreezeValidationError) -> None:
    raise error(message)


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _corruption(message: str = _M_CORRUPT_PAYLOAD) -> FreezeCorruptionError:
    return FreezeCorruptionError(message)


def _require_str(
    value: object,
    message: str = _M_CORRUPT_PAYLOAD,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        _fail(message)
    if not allow_empty and value.strip() == "":
        _fail(message)
    return value


def _require_timestamp(value: object, message: str) -> str:
    text = _require_str(value, message)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        _fail(message)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(message)
    return text


def _request_identifier(value: object) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        raise FreezeValidationError(_M_BAD_REQUEST_ID)
    if len(value) > MAX_REQUEST_ID_CHARS:
        raise FreezeValidationError(_M_BAD_REQUEST_ID)
    try:
        return validate_identifier(value, "request_id")
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise FreezeValidationError(_M_BAD_REQUEST_ID) from exc


def _artifact_identifier(value: object, error=FreezeValidationError) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        raise error(_M_BAD_ARTIFACT_ID)
    try:
        return validate_identifier(value, "artifact_id")
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise error(_M_BAD_ARTIFACT_ID) from exc


def _ref_from_mapping(value: object, project_id: str, message: str, error=FreezeValidationError) -> dict:
    if not isinstance(value, dict) or set(value) != set(_REF_KEYS):
        raise error(message)
    try:
        normalized = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise error(message) from exc
    return {
        "project_id": normalized["project_id"],
        "artifact_id": normalized["artifact_id"],
        "revision_id": normalized["revision_id"],
    }


def _ref_dict(value: dict) -> dict:
    return {
        "project_id": value["project_id"],
        "artifact_id": value["artifact_id"],
        "revision_id": value["revision_id"],
    }


def _require_rationale(value: object, message: str = _M_BAD_RATIONALE, error=FreezeValidationError) -> str:
    if not isinstance(value, str) or _has_surrogate(value) or value.strip() == "":
        raise error(message)
    return value


# --------------------------------------------------------------------------- #
# Derived coverage gaps + logical binding
# --------------------------------------------------------------------------- #
def _coverage_gaps(payload: dict) -> dict:
    """Derive the explicit gap counters/flags from one validated coverage payload."""
    known = payload["known_relevant"]
    citations = payload["citations"]
    sources = payload["sources"]["totals"]
    counts = payload["corpus"]["counts"]
    return {
        "known_relevant_missing": known["missing"],
        "known_relevant_not_assessed": known["status"] == "not_assessed",
        "citation_gaps": len(citations["gaps"]),
        "expansion_not_performed": not citations["expansion_performed"],
        "source_failures": sources["failures"],
        "missing_identifier_candidates": counts["missing_identifier_candidates"],
        "identifier_conflict_candidates": counts["identifier_conflict_candidates"],
    }


def _gaps_present(gaps: dict) -> bool:
    return any(
        bool(gaps[key]) for key in GAP_KEYS
    )


def _corpus_section(snapshot: dict) -> dict:
    candidates = snapshot["candidates"]
    return {
        "ref": {
            "project_id": snapshot["project_id"],
            "artifact_id": snapshot["artifact_id"],
            "revision_id": snapshot["revision_id"],
        },
        "content_sha256": snapshot["content_sha256"],
        "document_type": snapshot["document_type"],
        "request_id": snapshot["request_id"],
        "candidate_ids": sorted(doc["candidate_id"] for doc in candidates),
        "resolved_ids": sorted(
            doc["candidate_id"] for doc in candidates if doc["resolved"]
        ),
        "unresolved_ids": sorted(
            doc["candidate_id"] for doc in candidates if not doc["resolved"]
        ),
    }


def _coverage_section(coverage: dict) -> dict:
    payload = coverage["payload"]
    return {
        "ref": {
            "project_id": coverage["project_id"],
            "artifact_id": coverage["artifact_id"],
            "revision_id": coverage["revision_id"],
        },
        "content_sha256": coverage["content_sha256"],
        "document_type": coverage["document_type"],
        "plan_ref": _ref_dict(payload["plan"]["ref"]),
        "plan_content_sha256": payload["plan"]["content_sha256"],
        "corpus_ref": _ref_dict(payload["corpus"]["ref"]),
        "corpus_content_sha256": payload["corpus"]["content_sha256"],
        "gaps": _coverage_gaps(payload),
        "limitations_count": len(payload["limitations_summary"]),
    }


def _plan_section(revision: Revision) -> dict:
    return {
        "ref": {
            "project_id": revision.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        },
        "content_sha256": revision.content_sha256,
        "document_type": "discovery_plan",
    }


def _logical_binding(
    decision: str,
    corpus_section: dict,
    coverage_section: dict,
    plan_section: dict,
    entry_reviews: list,
    acceptance: dict,
    rationale: str,
) -> dict:
    """The deterministic logical identity of one freeze decision request.

    It covers exactly the caller/durable logical inputs (decision, exact corpus /
    coverage / plan refs + digests, the ordered entry reviews, the explicit
    coverage-limitation acknowledgement, and the rationale) and deliberately
    excludes any later mutable state, so an identical logical replay is
    recognised.
    """
    return {
        "decision": decision,
        "corpus": {
            "artifact_id": corpus_section["ref"]["artifact_id"],
            "revision_id": corpus_section["ref"]["revision_id"],
            "content_sha256": corpus_section["content_sha256"],
        },
        "coverage": {
            "artifact_id": coverage_section["ref"]["artifact_id"],
            "revision_id": coverage_section["ref"]["revision_id"],
            "content_sha256": coverage_section["content_sha256"],
        },
        "plan": {
            "artifact_id": plan_section["ref"]["artifact_id"],
            "revision_id": plan_section["ref"]["revision_id"],
            "content_sha256": plan_section["content_sha256"],
        },
        "entry_reviews": entry_reviews,
        "coverage_acceptance": acceptance,
        "rationale": rationale,
    }


# --------------------------------------------------------------------------- #
# Entry-review normalization (shared by write and read)
# --------------------------------------------------------------------------- #
def _normalize_entry_review(raw: object, candidate_ids: set[str], idx: int) -> dict:
    """Validate and normalize one entry review; enforce every disposition rule."""
    field = f"entry_reviews[{idx}]"
    if not isinstance(raw, dict) or set(raw) != _ENTRY_KEYS:
        _fail(_M_BAD_ENTRY_REVIEWS)
    try:
        candidate_id = validate_identifier(raw["candidate_id"], "candidate_id")
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise FreezeValidationError(_M_BAD_ENTRY_REVIEWS) from exc
    if candidate_id not in candidate_ids:
        _fail(_M_BAD_ENTRY_REVIEWS)
    checked_at = _require_timestamp(raw["checked_at"], _M_BAD_ENTRY_REVIEWS)
    provenance = raw["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
        _fail(_M_BAD_ENTRY_REVIEWS)
    provenance = {
        "ref": _require_str(provenance["ref"], _M_BAD_ENTRY_REVIEWS),
        "source": _require_str(provenance["source"], _M_BAD_ENTRY_REVIEWS),
    }
    statuses = {}
    for name in INTEGRITY_FIELDS:
        status = raw[name]
        if status not in INTEGRITY_STATUSES:
            _fail(_M_BAD_ENTRY_REVIEWS)
        statuses[name] = status
    disposition = raw["disposition"]
    if disposition not in DISPOSITIONS:
        _fail(_M_BAD_ENTRY_REVIEWS)
    rationale = _require_rationale(raw["rationale"], _M_BAD_ENTRY_REVIEWS)
    return {
        "candidate_id": candidate_id,
        "checked_at": checked_at,
        "provenance": provenance,
        **statuses,
        "disposition": disposition,
        "rationale": rationale,
    }


def _normalize_entry_reviews(
    entry_reviews: object, candidate_docs: list[dict]
) -> list[dict]:
    """Normalize the full review set and enforce the per-candidate rules.

    Rules (see the module docstring): every candidate represented exactly once;
    ``include`` only for a resolved candidate whose four statuses are all known;
    an unresolved/conflict candidate never ``include``; a flagged status keeps its
    human rationale (and does not force an exclusion).
    """
    if isinstance(entry_reviews, (str, bytes, bytearray)):
        _fail(_M_BAD_ENTRY_REVIEWS)
    try:
        items = list(entry_reviews)  # type: ignore[arg-type]
    except TypeError as exc:
        raise FreezeValidationError(_M_BAD_ENTRY_REVIEWS) from exc

    by_id = {doc["candidate_id"]: doc for doc in candidate_docs}
    candidate_ids = set(by_id)
    normalized: dict[str, dict] = {}
    for idx, raw in enumerate(items):
        entry = _normalize_entry_review(raw, candidate_ids, idx)
        if entry["candidate_id"] in normalized:
            _fail(_M_BAD_ENTRY_REVIEWS)
        normalized[entry["candidate_id"]] = entry

    if set(normalized) != candidate_ids:
        # A missing review blocks a freeze decision; an extra one is refused too.
        _fail(_M_BAD_ENTRY_REVIEWS)

    for candidate_id, entry in normalized.items():
        doc = by_id[candidate_id]
        unresolved = (not doc["resolved"]) or doc["unresolved_reason"] is not None
        conflicted = bool(doc.get("conflict_refs"))
        flagged = any(entry[name] == "flagged" for name in INTEGRITY_FIELDS)
        unknown = any(entry[name] == "unknown" for name in INTEGRITY_FIELDS)
        if entry["disposition"] == "include":
            if unresolved or conflicted:
                _fail(_M_BAD_ENTRY_REVIEWS)
            if unknown:
                _fail(_M_BAD_ENTRY_REVIEWS)
        if flagged and entry["rationale"].strip() == "":
            # Unreachable through _normalize_entry_review; kept as a fail-closed guard.
            _fail(_M_BAD_ENTRY_REVIEWS)

    return [normalized[candidate_id] for candidate_id in sorted(normalized)]


def _selection_from_reviews(entry_reviews: list[dict]) -> dict:
    buckets = {
        "included": [],
        "excluded": [],
        "deferred": [],
        "no_scientific_use": [],
    }
    mapping = {
        "include": "included",
        "exclude": "excluded",
        "defer": "deferred",
        "no_scientific_use": "no_scientific_use",
    }
    for entry in entry_reviews:
        buckets[mapping[entry["disposition"]]].append(entry["candidate_id"])
    return {key: sorted(value) for key, value in buckets.items()}


def _coverage_acceptance(
    gaps: dict,
    decision: str,
    accept_text: object,
) -> dict:
    """Build and enforce the explicit coverage-limitation acknowledgement."""
    if accept_text is None:
        text = None
    elif isinstance(accept_text, str) and not _has_surrogate(accept_text) and accept_text.strip() != "":
        text = accept_text
    else:
        _fail(_M_BAD_ACCEPTANCE)
    if decision == "freeze" and _gaps_present(gaps) and text is None:
        _fail(_M_NEEDS_ACK)
    return {"acknowledged": text is not None, "text": text, "gaps": dict(gaps)}


# --------------------------------------------------------------------------- #
# Payload validation (full re-derivation from durable state)
# --------------------------------------------------------------------------- #
def _validate_corpus_section(section: object, project_id: str, store) -> dict:
    if not isinstance(section, dict) or set(section) != _CORPUS_KEYS:
        raise _corruption()
    ref = _ref_from_mapping(section["ref"], project_id, _M_CORRUPT_PAYLOAD, FreezeCorruptionError)
    try:
        snapshot = CandidateRepository(store).get(ref)
    except Exception as exc:  # CandidateError / validate_ref
        raise FreezeCorruptionError(_M_CORRUPT_PIN) from exc
    if _corpus_section(snapshot) != section:
        raise _corruption(_M_CORRUPT_PIN)
    return snapshot


def _validate_coverage_section(section: object, project_id: str, store) -> dict:
    if not isinstance(section, dict) or set(section) != _COVERAGE_KEYS:
        raise _corruption()
    ref = _ref_from_mapping(section["ref"], project_id, _M_CORRUPT_PAYLOAD, FreezeCorruptionError)
    try:
        coverage = CoverageRepository(store).get(ref)
    except Exception as exc:  # CoverageError / validate_ref
        raise FreezeCorruptionError(_M_CORRUPT_PIN) from exc
    if _coverage_section(coverage) != section:
        raise _corruption(_M_CORRUPT_PIN)
    return coverage


def _validate_plan_section(section: object, project_id: str, store) -> None:
    if not isinstance(section, dict) or set(section) != _PLAN_KEYS:
        raise _corruption()
    ref = _ref_from_mapping(section["ref"], project_id, _M_CORRUPT_PAYLOAD, FreezeCorruptionError)
    revision = ArtifactRepository(store).get_revision(ref["artifact_id"], ref["revision_id"])
    if revision is None:
        raise FreezeCorruptionError(_M_CORRUPT_PIN)
    if section["document_type"] != "discovery_plan" or section["content_sha256"] != revision.content_sha256:
        raise _corruption(_M_CORRUPT_PIN)


def _validate_selection(selection: object, entry_reviews: list[dict]) -> None:
    if not isinstance(selection, dict) or set(selection) != _SELECTION_KEYS:
        _fail(_M_CORRUPT_PAYLOAD)
    for key in _SELECTION_KEYS:
        values = selection[key]
        if not isinstance(values, list) or values != sorted(values) or len(set(values)) != len(values):
            _fail(_M_CORRUPT_PAYLOAD)
    expected = _selection_from_reviews(entry_reviews)
    if selection != expected:
        _fail(_M_CORRUPT_PAYLOAD)


def _validate_acceptance(acceptance: object, decision: str, gaps: dict) -> None:
    if not isinstance(acceptance, dict) or set(acceptance) != _ACCEPTANCE_KEYS:
        _fail(_M_CORRUPT_PAYLOAD)
    acknowledged = acceptance["acknowledged"]
    if not isinstance(acknowledged, bool):
        _fail(_M_CORRUPT_PAYLOAD)
    text = acceptance["text"]
    if text is not None:
        _require_str(text, _M_CORRUPT_PAYLOAD)
    if acknowledged != (text is not None):
        _fail(_M_CORRUPT_PAYLOAD)
    if acceptance["gaps"] != gaps:
        _fail(_M_CORRUPT_PAYLOAD)
    if decision == "freeze" and _gaps_present(gaps) and not acknowledged:
        _fail(_M_CORRUPT_PAYLOAD)


def _validate_payload(payload: object, project_id: str, store) -> dict:
    """Fully revalidate one freeze payload, re-deriving everything durable."""
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise _corruption()
    if payload["payload_version"] != FREEZE_PAYLOAD_VERSION:
        raise _corruption()
    if payload["notice"] != FREEZE_NOTICE:
        raise _corruption()
    if payload["authorizing"] is not False or payload["completeness_claimed"] is not False:
        raise _corruption()
    decision = payload["decision"]
    if decision not in DECISIONS:
        raise _corruption()
    rationale = _require_rationale(
        payload["rationale"], _M_CORRUPT_PAYLOAD, FreezeCorruptionError
    )

    snapshot = _validate_corpus_section(payload["corpus"], project_id, store)
    coverage = _validate_coverage_section(payload["coverage"], project_id, store)
    _validate_plan_section(payload["plan"], project_id, store)

    # The coverage report's own corpus/plan pins must agree with the freeze pins.
    cov = coverage["payload"]
    if _ref_dict(cov["corpus"]["ref"]) != _ref_dict(payload["corpus"]["ref"]):
        raise _corruption(_M_CORRUPT_PIN)
    if cov["corpus"]["content_sha256"] != payload["corpus"]["content_sha256"]:
        raise _corruption(_M_CORRUPT_PIN)
    if _ref_dict(cov["plan"]["ref"]) != _ref_dict(payload["plan"]["ref"]):
        raise _corruption(_M_CORRUPT_PIN)
    if cov["plan"]["content_sha256"] != payload["plan"]["content_sha256"]:
        raise _corruption(_M_CORRUPT_PIN)

    entry_reviews = _normalize_entry_reviews(
        payload["entry_reviews"], snapshot["candidates"]
    )
    if entry_reviews != payload["entry_reviews"]:
        raise _corruption()
    _validate_selection(payload["selection"], entry_reviews)
    gaps = _coverage_gaps(cov)
    _validate_acceptance(payload["coverage_acceptance"], decision, gaps)

    expected_binding = _logical_binding(
        decision,
        payload["corpus"],
        payload["coverage"],
        payload["plan"],
        entry_reviews,
        payload["coverage_acceptance"],
        rationale,
    )
    if payload["input_binding"] != expected_binding:
        raise _corruption()

    return {
        "payload_version": FREEZE_PAYLOAD_VERSION,
        "request_id": _request_identifier_corruption(payload["request_id"]),
        "input_binding": expected_binding,
        "decision": decision,
        "corpus": payload["corpus"],
        "coverage": payload["coverage"],
        "plan": payload["plan"],
        "entry_reviews": entry_reviews,
        "selection": payload["selection"],
        "coverage_acceptance": payload["coverage_acceptance"],
        "rationale": rationale,
        "authorizing": False,
        "completeness_claimed": False,
        "notice": FREEZE_NOTICE,
    }


def _request_identifier_corruption(value: object) -> str:
    try:
        return _request_identifier(value)
    except FreezeValidationError as exc:
        raise FreezeCorruptionError(_M_CORRUPT_PAYLOAD) from exc


def _assemble(revision: Revision, payload: dict) -> dict:
    return {
        "project_id": revision.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "content_sha256": revision.content_sha256,
        "byte_size": revision.byte_size,
        "acquired_at_utc": revision.acquired_at_utc,
        "document_type": DOCUMENT_TYPE,
        "schema_version": FREEZE_ENVELOPE_SCHEMA_VERSION,
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# Approval lookups (read-only)
# --------------------------------------------------------------------------- #
def _approval_summary(status) -> dict:
    return {
        "approval_id": status.approval_id,
        "status": status.status,
        "decision": status.decision,
        "active": status.is_active,
        "satisfies_named_gate": status.satisfies_named_gate,
        "gate_kind": status.gate_kind,
        "gate_id": status.gate_id,
    }


def _newest_approved_status(approvals, artifact_id: str, revision_id: str):
    """Return the computed status of the newest approval for one exact revision."""
    found = None
    for record in approvals.list_approvals():
        if record.gate_kind != FREEZE_GATE_KIND or record.gate_id != FREEZE_GATE_ID:
            continue
        if record.target["artifact_id"] != artifact_id:
            continue
        if record.target["revision_id"] != revision_id:
            continue
        if record.decision != "approved":
            continue
        found = record
    if found is None:
        return None
    return approvals.evaluate(found.approval_id)


def _current_status(approvals, artifact_id: str, revision_id: str):
    """Return the computed status of the *current* approval over one artifact.

    ``None`` unless the current (newest) approval for the exact named gate
    instance targets exactly this revision.
    """
    record = approvals.current_approval(
        artifact_id, gate_kind=FREEZE_GATE_KIND, gate_id=FREEZE_GATE_ID
    )
    if record is None or record.target["revision_id"] != revision_id:
        return None
    return approvals.evaluate(record.approval_id)


# --------------------------------------------------------------------------- #
# Decode helper (full revalidation)
# --------------------------------------------------------------------------- #
def _read_document(artifacts: ArtifactRepository, revision: Revision) -> dict:
    content = artifacts.read_content(revision.artifact_id, revision.revision_id)
    if sha256_digest(content) != revision.content_sha256 or len(content) != revision.byte_size:
        raise FreezeCorruptionError(_M_CORRUPT_DIGEST)
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FreezeCorruptionError(_M_CORRUPT_ENVELOPE)
    if not isinstance(parsed, dict) or set(parsed) != {
        "schema_version",
        "document_type",
        "payload",
    }:
        raise FreezeCorruptionError(_M_CORRUPT_ENVELOPE)
    if parsed["schema_version"] != FREEZE_ENVELOPE_SCHEMA_VERSION:
        raise FreezeCorruptionError(_M_CORRUPT_ENVELOPE)
    if parsed["document_type"] != DOCUMENT_TYPE:
        raise FreezeCorruptionError(_M_CORRUPT_ENVELOPE)
    try:
        if canonical_json(parsed) != content:
            raise FreezeCorruptionError(_M_CORRUPT_CANONICAL)
    except RecordValidationError as exc:
        raise FreezeCorruptionError(_M_CORRUPT_CANONICAL) from exc
    return parsed


def _safe_envelope(artifacts: ArtifactRepository, revision: Revision):
    """Decode an envelope read-only; return ``None`` on any problem (guard only)."""
    try:
        return _read_document(artifacts, revision)
    except FreezeError:
        return None


# --------------------------------------------------------------------------- #
# Read-only corpus freeze guard (used by CandidateRepository.ingest)
# --------------------------------------------------------------------------- #
def corpus_has_authentic_freeze(store, corpus_artifact_id: str) -> bool:
    """``True`` iff a logical corpus artifact already carries an authentic freeze.

    Read-only: it scans the freeze decision documents (stored as ``report``
    artifacts) and returns ``True`` only when one pins this exact corpus artifact
    **and** its exact revision carries a currently-active, human-approved
    ``artifact_revision``/``discovery.freeze`` gate. It writes nothing and never
    follows into a circular write; a forged proposal with no real human gate is
    never treated as a freeze.
    """
    try:
        corpus_artifact_id = _artifact_identifier(corpus_artifact_id)
    except FreezeValidationError:
        return False
    artifacts = ArtifactRepository(store)
    approvals = HumanApprovalRepository(store)
    for artifact in artifacts.list_artifacts():
        for revision in artifacts.list_revisions(artifact.artifact_id):
            if not revision.origin_locator.startswith(_ORIGIN_PREFIX):
                continue
            document = _safe_envelope(artifacts, revision)
            if document is None or document["document_type"] != DOCUMENT_TYPE:
                continue
            payload = document["payload"]
            if not isinstance(payload, dict) or payload.get("decision") != "freeze":
                continue
            corpus = payload.get("corpus")
            if not isinstance(corpus, dict):
                continue
            ref = corpus.get("ref")
            if not isinstance(ref, dict) or ref.get("artifact_id") != corpus_artifact_id:
                continue
            status = _newest_approved_status(approvals, artifact.artifact_id, revision.revision_id)
            if status is not None and status.satisfies_named_gate:
                return True
    return False


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class FreezeRepository:
    """Durable, human-approved corpus freeze / search-again decisions.

    Typical use::

        freeze = FreezeRepository(store)

        rid = FreezeRepository.new_revision_id()
        rights = build_valid_rights_record(rid)      # caller-supplied, exact
        freeze.propose(
            corpus_ref, coverage_ref,
            decision="freeze",
            entry_reviews=[...],
            rationale="...",
            request_id="freeze-1",
            rights=rights,
            revision_id=rid,
            accept_coverage_limitations="accepted: ...",   # when gaps are exposed
        )

        # The human confirms out-of-band, then:
        prepared = prepare_gate_snapshot(
            store, freeze_ref, gate_kind="artifact_revision",
            gate_id="discovery.freeze", decision="approved", rationale="...",
        )
        HumanApprovalRepository(store).record_decision(prepared, capability)

        selection = freeze.require_frozen()          # dict, or raises

    The repository performs **no** network, provider, model, worker, or paid work:
    it reads local durable state and writes local immutable revisions. It mints
    **no** authority.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._artifacts = ArtifactRepository(store)

    @property
    def project_id(self) -> str:
        """The actual store's project id; no caller may select a project."""
        return self._store.project_id

    @staticmethod
    def new_revision_id() -> str:
        """A fresh opaque revision id (caller-assigned for exact rights binding)."""
        return ArtifactRepository.new_revision_id()

    # -- write path ---------------------------------------------------------- #
    def propose(
        self,
        corpus_ref,
        coverage_ref,
        *,
        decision,
        entry_reviews,
        rationale,
        request_id,
        rights,
        revision_id: str | None = None,
        artifact_id: str = DEFAULT_FREEZE_ARTIFACT_ID,
        accept_coverage_limitations: str | None = None,
    ) -> Revision:
        """Record (or replay) one **inert** freeze / search-again proposal.

        Every durable/logical input is re-read and re-derived from the store: the
        exact corpus snapshot (via :class:`CandidateRepository`), the exact
        coverage report (via :class:`CoverageRepository`) whose own ``corpus`` and
        ``plan`` references must pin the supplied corpus and an **approved** plan
        exactly, the exact plan revision, and the explicit per-candidate entry
        reviews. ``rights`` (a validated ``RightsRecord`` or a stored
        ``RightsRecordRef``) is pinned to this exact revision. An identical
        ``request_id`` replay returns the historical revision; a divergent reuse
        raises :class:`FreezeConflictError`.

        This records a **proposal only**: it freezes nothing and grants no
        authority. A corpus is frozen **only** when the exact named human gate
        instance ``artifact_revision``/``discovery.freeze`` approves this exact
        revision.
        """
        artifact_id = _artifact_identifier(artifact_id)
        request_id = _request_identifier(request_id)
        decision = self._validate_decision(decision)
        rationale = _require_rationale(rationale)

        prepared = self._prepare_logical(
            request_id, corpus_ref, coverage_ref, decision, entry_reviews, rationale,
            accept_coverage_limitations,
        )
        binding = prepared["binding"]

        self._artifacts.create_artifact(artifact_id, kind="report", created_by="import")

        existing = self._find_request(artifact_id, request_id)
        if existing is not None:
            existing_revision, existing_binding = existing
            if existing_binding == binding:
                return existing_revision
            raise FreezeConflictError(_M_DIVERGENT)

        payload = prepared["payload"]
        try:
            _validate_payload(payload, self.project_id, self._store)
        except FreezeError as exc:
            raise FreezeValidationError(_M_BAD_INPUT) from exc
        document = {
            "schema_version": FREEZE_ENVELOPE_SCHEMA_VERSION,
            "document_type": DOCUMENT_TYPE,
            "payload": payload,
        }
        dependencies = [
            {**payload["corpus"]["ref"], "role": "derived_from"},
            {**payload["coverage"]["ref"], "role": "derived_from"},
            {**payload["plan"]["ref"], "role": "derived_from"},
        ]
        current = self._artifacts.current_revision(artifact_id)
        parent_revision_id = current.revision_id if current is not None else None
        try:
            return self._artifacts.import_revision(
                artifact_id=artifact_id,
                content=document,
                origin_locator=_origin_locator(request_id),
                rights=rights,
                revision_id=revision_id,
                parent_revision_id=parent_revision_id,
                dependencies=dependencies,
                acquired_by="import",
                request_id=request_id,
            )
        except ArtifactConflictError as exc:
            raise FreezeConflictError(_M_DIVERGENT) from exc
        except RecordValidationError as exc:
            raise FreezeValidationError(_M_BAD_INPUT) from exc

    # -- read path ----------------------------------------------------------- #
    def get(self, ref: object) -> dict:
        """Read one exact decision by explicit same-project revision reference."""
        normalized = self._normalize_ref(ref, FreezeValidationError, _M_BAD_REF)
        revision = self._artifacts.get_revision(
            normalized["artifact_id"], normalized["revision_id"]
        )
        if revision is None:
            raise FreezeValidationError(_M_NOT_FOUND)
        document = self._read(revision)
        approvals = HumanApprovalRepository(self._store)
        status = _current_status(
            approvals, revision.artifact_id, revision.revision_id
        )
        document["approval"] = _approval_summary(status) if status is not None else None
        return document

    def current_decision(self, artifact_id: str = DEFAULT_FREEZE_ARTIFACT_ID) -> dict | None:
        """Return the currently authorized decision document, or ``None``.

        ``None`` unless the artifact's current revision is a decision whose exact
        revision carries a currently-*active*, human-approved
        ``artifact_revision``/``discovery.freeze`` gate. A raw proposal with no
        gate, a stale approval, or a rejected one is never returned.
        """
        artifact_id = _artifact_identifier(artifact_id)
        revision = self._artifacts.current_revision(artifact_id)
        if revision is None:
            return None
        document = self._read(revision)
        status = _current_status(
            HumanApprovalRepository(self._store), artifact_id, revision.revision_id
        )
        if status is None or not status.satisfies_named_gate:
            return None
        document["approval"] = _approval_summary(status)
        return document

    def frozen_corpus(self, artifact_id: str = DEFAULT_FREEZE_ARTIFACT_ID) -> dict | None:
        """Return the last human-approved freeze selection, or ``None``.

        This is the exact **last approved freeze** (never the latest raw
        proposal): it scans the artifact's decisions newest-first and returns the
        newest ``freeze`` decision whose exact revision carries a human approval
        (``approved``). The computed approval status is left visible, so a freeze
        later made stale by a plan amendment or a deliberate generic edit (or
        superseded by a later ``search_again``) is **retained**, not erased —
        :meth:`require_frozen` is the operation that *blocks* a stale freeze.
        """
        artifact_id = _artifact_identifier(artifact_id)
        approvals = HumanApprovalRepository(self._store)
        for revision in reversed(self._artifacts.list_revisions(artifact_id)):
            document = self._read(revision)
            if document["payload"]["decision"] != "freeze":
                continue
            status = _newest_approved_status(
                approvals, artifact_id, revision.revision_id
            )
            if status is None:
                continue
            document["approval"] = _approval_summary(status)
            document["frozen"] = status.satisfies_named_gate
            document["included_candidate_ids"] = list(
                document["payload"]["selection"]["included"]
            )
            return document
        return None

    def history(self, artifact_id: str = DEFAULT_FREEZE_ARTIFACT_ID) -> list[dict]:
        """List every decision revision, newest-first, with its approval status."""
        artifact_id = _artifact_identifier(artifact_id)
        approvals = HumanApprovalRepository(self._store)
        out: list[dict] = []
        for revision in reversed(self._artifacts.list_revisions(artifact_id)):
            document = self._read(revision)
            status = _newest_approved_status(
                approvals, artifact_id, revision.revision_id
            )
            document["approval"] = _approval_summary(status) if status is not None else None
            out.append(document)
        return out

    def require_frozen(self, artifact_id: str = DEFAULT_FREEZE_ARTIFACT_ID, *, at_ref=None) -> dict:
        """Return the frozen selection **only** for an active authentic freeze.

        The exact revision (``at_ref`` or the artifact's current head) must be a
        ``freeze`` decision; its exact revision must carry a currently-*active*,
        human-approved ``artifact_revision``/``discovery.freeze`` gate that targets
        exactly this revision; the plan behind the coverage report must still be
        **currently approved**; and every pinned input must still be intact (the
        corpus/coverage/plan revisions must exist and their digests must match). A
        stale, forged, wrong-gate, missing-coverage, or raw (unapproved) state
        raises :class:`FreezeNotAuthorizedError`.
        """
        artifact_id = _artifact_identifier(artifact_id)
        if at_ref is None:
            revision = self._artifacts.current_revision(artifact_id)
            if revision is None:
                raise FreezeNotAuthorizedError(_M_NOT_FROZEN)
        else:
            normalized = self._normalize_ref(at_ref, FreezeNotAuthorizedError, _M_NOT_FROZEN)
            revision = self._artifacts.get_revision(
                normalized["artifact_id"], normalized["revision_id"]
            )
            if revision is None:
                raise FreezeNotAuthorizedError(_M_NOT_FROZEN)

        try:
            document = self._read(revision)
        except FreezeError as exc:
            raise FreezeNotAuthorizedError(_M_NOT_FROZEN) from exc
        payload = document["payload"]
        if payload["decision"] != "freeze":
            raise FreezeNotAuthorizedError(_M_NOT_FROZEN)

        status = _current_status(
            HumanApprovalRepository(self._store), artifact_id, revision.revision_id
        )
        if status is None or not status.satisfies_named_gate:
            raise FreezeNotAuthorizedError(_M_NOT_FROZEN)

        try:
            require_approved_context(
                self._store,
                payload["plan"]["ref"],
                gate_kind=PROTOCOL_GATE_KIND,
                gate_id=PLAN_GATE_ID,
                expected_type="discovery_plan",
            )
        except Exception as exc:  # DiscoveryContextError / anything -> not frozen
            raise FreezeNotAuthorizedError(_M_NOT_FROZEN) from exc

        document["approval"] = _approval_summary(status)
        document["frozen"] = True
        document["included_candidate_ids"] = list(payload["selection"]["included"])
        return document

    # -- internals ----------------------------------------------------------- #
    @staticmethod
    def _validate_decision(value: object) -> str:
        if value not in DECISIONS:
            raise FreezeValidationError(_M_BAD_DECISION)
        return value  # type: ignore[return-value]

    def _normalize_ref(self, ref: object, error, message: str) -> dict:
        if not isinstance(ref, dict) or set(ref) != set(_REF_KEYS):
            raise error(message)
        try:
            normalized = validate_ref(ref, self.project_id)
        except (RecordValidationError, TypeError, ValueError) as exc:
            raise error(message) from exc
        return {
            "project_id": normalized["project_id"],
            "artifact_id": normalized["artifact_id"],
            "revision_id": normalized["revision_id"],
        }

    def _prepare_logical(
        self,
        request_id: str,
        corpus_ref,
        coverage_ref,
        decision: str,
        entry_reviews,
        rationale: str,
        accept_coverage_limitations,
    ) -> dict:
        corpus = self._read_corpus(corpus_ref)
        coverage = self._read_coverage(coverage_ref)
        cov_payload = coverage["payload"]
        corpus_ref_norm = {
            "project_id": corpus["project_id"],
            "artifact_id": corpus["artifact_id"],
            "revision_id": corpus["revision_id"],
        }

        # The coverage report must pin the supplied corpus and an approved plan.
        if _ref_dict(cov_payload["corpus"]["ref"]) != corpus_ref_norm:
            raise FreezeValidationError(_M_MISMATCH)
        if cov_payload["corpus"]["content_sha256"] != corpus["content_sha256"]:
            raise FreezeValidationError(_M_MISMATCH)

        plan_ref = _ref_dict(cov_payload["plan"]["ref"])
        plan_revision = self._artifacts.get_revision(
            plan_ref["artifact_id"], plan_ref["revision_id"]
        )
        if plan_revision is None:
            raise FreezeValidationError(_M_BAD_PLAN)
        if plan_revision.content_sha256 != cov_payload["plan"]["content_sha256"]:
            raise FreezeValidationError(_M_BAD_PLAN)

        # Require the exact approved plan via the coverage report's plan ref.
        try:
            require_approved_context(
                self._store,
                plan_ref,
                gate_kind=PROTOCOL_GATE_KIND,
                gate_id=PLAN_GATE_ID,
                expected_type="discovery_plan",
            )
        except Exception as exc:  # DiscoveryContextError
            raise FreezeValidationError(_M_PLAN_NOT_APPROVED) from exc

        entry_reviews_norm = _normalize_entry_reviews(
            entry_reviews, corpus["candidates"]
        )
        selection = _selection_from_reviews(entry_reviews_norm)

        corpus_section = _corpus_section(corpus)
        coverage_section = _coverage_section(coverage)
        plan_section = _plan_section(plan_revision)
        acceptance = _coverage_acceptance(
            coverage_section["gaps"], decision, accept_coverage_limitations
        )
        binding = _logical_binding(
            decision,
            corpus_section,
            coverage_section,
            plan_section,
            entry_reviews_norm,
            acceptance,
            rationale,
        )
        payload = {
            "payload_version": FREEZE_PAYLOAD_VERSION,
            "request_id": request_id,
            "input_binding": binding,
            "decision": decision,
            "corpus": corpus_section,
            "coverage": coverage_section,
            "plan": plan_section,
            "entry_reviews": entry_reviews_norm,
            "selection": selection,
            "coverage_acceptance": acceptance,
            "rationale": rationale,
            "authorizing": False,
            "completeness_claimed": False,
            "notice": FREEZE_NOTICE,
        }
        return {"payload": payload, "binding": binding}

    def _read_corpus(self, corpus_ref) -> dict:
        try:
            normalized = self._normalize_ref(
                corpus_ref, FreezeValidationError, _M_BAD_CORPUS
            )
            return CandidateRepository(self._store).get(normalized)
        except FreezeError:
            raise
        except Exception as exc:  # CandidateError / validate_ref
            raise FreezeValidationError(_M_BAD_CORPUS) from exc

    def _read_coverage(self, coverage_ref) -> dict:
        try:
            normalized = self._normalize_ref(
                coverage_ref, FreezeValidationError, _M_BAD_COVERAGE
            )
            return CoverageRepository(self._store).get(normalized)
        except FreezeError:
            raise
        except Exception as exc:  # CoverageError / validate_ref
            raise FreezeValidationError(_M_BAD_COVERAGE) from exc

    def _find_request(self, artifact_id: str, request_id: str):
        locator = _origin_locator(request_id)
        for revision in self._artifacts.list_revisions(artifact_id):
            if revision.origin_locator != locator:
                continue
            document = self._read(revision)
            return revision, document["payload"]["input_binding"]
        return None

    def _read(self, revision: Revision) -> dict:
        document = _read_document(self._artifacts, revision)
        try:
            payload = _validate_payload(document["payload"], self.project_id, self._store)
        except FreezeCorruptionError:
            raise
        except FreezeValidationError as exc:
            raise FreezeCorruptionError(_M_CORRUPT_PAYLOAD) from exc
        assembled = _assemble(revision, payload)
        assembled["request_id"] = document["payload"]["request_id"]
        return assembled


def _origin_locator(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return _ORIGIN_PREFIX + digest
