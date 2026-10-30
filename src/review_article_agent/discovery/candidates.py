"""Durable **cumulative publication-candidate snapshots** (Step 05, W2).

Authority
---------

* ``docs/implementation/05-discovery.md`` deliverable 3 (query runs / identity
  reconciliation) and acceptance criteria 3 ("reconstruct exact queries,
  timestamps, contributing sources, failures, counts, and deduplication
  decisions from records"), 4 (fixed fixtures for pagination, overlap, missing
  IDs, partial failures, and incremental reruns "without duplicate scientific
  candidates"), and 6 (a human freezes a specific corpus revision; later
  imports/searches never mutate it silently).
* ``docs/specification.md`` workflow steps 5 (a coverage report supports an
  explicit human decision to *search again or freeze the candidate revision*;
  never promise all papers) and 10 (exact duplicates, alternate document
  versions, linked reports of the same study, experiments, and supplements are
  **different cases**; a change creates an explicit new revision and never
  erases unaffected work), plus the gates/scientific invariants ("treat
  documents/model outputs as untrusted data"; a summary/quote/hash/approval alone
  establishes no scientific correctness).
* ``docs/contracts/01-identities.md`` §3.2 (a *logical* identity persists across
  edits; a revision is one **immutable** snapshot; a change is always a new
  revision), §3.3 (a content hash is a byte-integrity/duplicate key only, never
  an identity), and §6 (duplicate bytes, alternate versions, and linked reports
  are distinct relations).
* ``docs/contracts/03-policy-decisions.md`` ``ARCH-STORE`` (one app-owned,
  project-scoped SQLite store) and ``ARCH-MIGRATE`` (versioned, additive
  migrations; consistent verified backup/restore).

What this module **is**
-----------------------

The storage half of "turn durable, inert provider *observations* into a durable,
**cumulative publication-candidate ledger**, with stable candidate identity and
auditable occurrence/dedup decisions". It is built **on top of** two existing
immutable layers and adds **no parallel table**:

* :class:`~review_article_agent.storage.discovery.DiscoveryRepository` — the
  already-recorded, immutable per-run observations ("run ids"). This module
  reads them through their own revalidating API; every read of a snapshot is
  re-derived from these actual observations, never trusted from a caller.
* :class:`~review_article_agent.storage.artifacts.ArtifactRepository` — a
  ``report`` artifact (default logical id :data:`DEFAULT_CORPUS_ARTIFACT_ID`)
  whose **immutable revisions** are the cumulative snapshots. Each revision
  stores one canonical JSON document envelope
  ``{schema_version, document_type: "discovery_corpus", payload}``; the exact
  caller-supplied rights record is pinned to *that* revision and storage is
  allowed only when the record permits it.

:class:`CandidateRepository` exposes exactly three operations:
:meth:`~CandidateRepository.ingest` records (or replays) a cumulative snapshot
whose payload names **all** cumulative run ids, their immutable result digests,
**every** exact occurrence pin ``(run_id, record_index)``, the candidate
assignment of each occurrence, and the explicit dedup/conflict/alias decisions;
:meth:`~CandidateRepository.current` reads the current snapshot; and
:meth:`~CandidateRepository.get` reads one exact snapshot by explicit
``{project_id, artifact_id, revision_id}`` reference.

**Candidate identity is stable and opaque.** A candidate id is minted on *first
encounter* from the first occurrence pin that founds it (an opaque digest of
provenance metadata -- the run id and record index), is stored in the snapshot,
and is reused on later ingests by **occurrence and exact-token** matches against
the prior cumulative snapshot. It is never the title, never a content hash, never
a scientific identity, and it persists across reopen/backup/restore. Candidate
ids need not be equal across two independently-created fresh projects (different
project ids yield different digests); they are deterministic for a given project
and input set, independent of the order in which run ids are supplied.

**Reconciliation reuses T1.** Exact-token connected components come from
:func:`~review_article_agent.discovery.reconciliation.reconcile_searches`
(exact provider-native ``(source, source_id)`` plus namespaced ``pmid``/``pmcid``
/``doi`` tokens). **Every occurrence is retained.** A *consistent* component
(overlap, a single identified occurrence, or a missing-identifier occurrence)
dedupes into stable candidate ids. A component that carries an **identifier
conflict** is never silently merged: prior candidates are preserved, the newly
observed occurrences are assigned explicit unresolved occurrence-local
candidates, and the conflict membership is recorded. Missing-identifier records
remain explicit occurrence-local unresolved candidates -- **no title/year
matching, ever**. If a new *consistent* bridging observation connects two prior
candidate ids, a deterministic survivor (the lexicographically smallest prior id)
keeps the occurrences, the other ids become explicit aliases, and a dedup
decision records the merge -- no provenance is lost.

What this module **is not** (the boundary)
------------------------------------------

* **Not science, not studies, not evidence, not accepted context, and not an
  approval.** A candidate is only a *review* grouping of exact provider
  observations. The snapshot carries no scientific interpretation, no
  eligibility, no evidence, no accepted project context, and no authorization
  (``authorizing`` is always ``False``, ``completeness_claimed`` always
  ``False``). Every candidate starts with an **explicit unreviewed integrity
  status** (retraction / expression-of-concern / correction / publication date
  ``unknown``; provenance ``missing``) that is **never "clean"**.
* **Not a corpus freeze.** It records a candidate revision; only a human approves
  freezing one. It never claims to have frozen anything and never promises
  completeness.
* **Not a network, approval, science, or CLI actor.** It performs no network,
  provider, model, worker, or approval call. It reads local observations only.
* **Not a completeness or integrity guarantee beyond corruption detection.** The
  artifact digest validates accidental/tampered **corruption only**; it is not an
  authenticity signature.

Determinism, replay, and bounds
-------------------------------

Runs are sorted and components are ordered deterministically, so a snapshot is
independent of the order of the supplied run ids. A ``request_id`` replays its
historical revision -- even after later ingests changed the current snapshot --
when the supplied run-id set matches; a divergent reuse of the same
``request_id`` is a static :class:`CandidateConflictError`. A new ingest keeps
**all** prior cumulative runs and never mutates an earlier revision. The total
occurrence count is bounded by T1's
:data:`~review_article_agent.discovery.reconciliation.MAX_TOTAL_OCCURRENCES`
(100,000) on every build and every read; crossing it is an explicit refusal, never
a silent truncation. Every failure raises a static, value-free error: no run id,
query, source, URL, title, identifier, or payload byte is ever echoed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

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
from ..storage.discovery import DiscoveryRepository, DiscoverySearchError
from .reconciliation import (
    CLASS_IDENTIFIER_CONFLICT,
    CLASS_MISSING_IDENTIFIERS,
    DiscoveryReconciliationError,
    SearchRun,
    reconcile_searches,
)

__all__ = [
    "DOCUMENT_TYPE",
    "CORPUS_ENVELOPE_SCHEMA_VERSION",
    "CORPUS_PAYLOAD_VERSION",
    "DEFAULT_CORPUS_ARTIFACT_ID",
    "MAX_REQUEST_ID_CHARS",
    "MAX_INPUT_RUN_IDS",
    "INTEGRITY_STATUS_UNREVIEWED",
    "INTEGRITY_NEVER_CLEAN",
    "UNRESOLVED_REASONS",
    "DECISION_KINDS",
    "CORPUS_NOTICE",
    "CandidateError",
    "CandidateValidationError",
    "CandidateConflictError",
    "CandidateCorruptionError",
    "CandidateRepository",
]

#: The generic-document ``document_type`` of one cumulative candidate snapshot.
DOCUMENT_TYPE = "discovery_corpus"

#: The only supported generic-document envelope ``schema_version``.
CORPUS_ENVELOPE_SCHEMA_VERSION = 1

#: The only supported snapshot ``payload_version``.
CORPUS_PAYLOAD_VERSION = 1

#: Default logical artifact that carries the cumulative candidate snapshot.
DEFAULT_CORPUS_ARTIFACT_ID = "discovery.corpus"

#: Request replay keys follow the existing bounded opaque-identifier convention.
MAX_REQUEST_ID_CHARS = 512

#: Technical bound on the number of run ids named in one snapshot. The
#: occurrence count is bounded separately by T1.
MAX_INPUT_RUN_IDS = 100_000

#: The explicit initial integrity status of every candidate: **never** "clean".
INTEGRITY_STATUS_UNREVIEWED = "unreviewed"

#: Statuses that would (falsely) read as a clean/verified scientific check. A
#: stored snapshot asserting any of these is refused as corruption.
INTEGRITY_NEVER_CLEAN = (
    "clean",
    "verified",
    "valid",
    "ok",
    "trusted",
    "confirmed",
    "clear",
)

#: Reasons a candidate may be explicitly unresolved.
UNRESOLVED_REASONS = ("missing_identifiers", "identifier_conflict")

#: Decision kinds recorded in ``dedup_decisions``.
DECISION_KINDS = ("minted", "occurrence_attached", "merged_bridging")

#: Static, value-free non-claim attached to every snapshot.
CORPUS_NOTICE = (
    "Non-authorizing review ledger only: publication candidates are exact-token "
    "overlap groups of untrusted provider observations, not studies, evidence, or "
    "accepted project context. Integrity is unverified (unknown/date/provenance "
    "missing), publication identity is not study identity, and no completeness or "
    "freeze is claimed."
)

#: The explicit initial ``integrity`` status of every candidate.
_INTEGRITY_INITIAL = {
    "status": INTEGRITY_STATUS_UNREVIEWED,
    "retraction": "unknown",
    "expression_of_concern": "unknown",
    "correction": "unknown",
    "publication_date": "unknown",
    "provenance": "missing",
}

#: The conflict/decision id shapes used in a snapshot payload. Ids are minted
#: with a zero-padded ``:04d`` suffix (so all historical ids stay byte-identical)
#: but the *read-back* validator accepts four **or more** digits: a single
#: cumulative revision at the ~10,000-candidate target mints ``dedup-10000``,
#: which a strict four-digit pattern wrongly rejected as internal inconsistency.
_CONFLICT_ID_RE = re.compile(r"^conflict-\d{4,}$")
_DECISION_ID_RE = re.compile(r"^dedup-\d{4,}$")

_RUN_META_KEYS = frozenset(
    {
        "run_id", "request_id", "result_sha256", "result_bytes", "result_type",
        "source", "complete", "stop_reason", "failure", "page_count",
        "record_count", "attempt_count", "schema_version",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "candidate_id", "occurrences", "tokens", "resolved", "unresolved_reason",
        "conflict_refs", "aliases", "integrity",
    }
)
_CONFLICT_KEYS = frozenset(
    {
        "conflict_id", "occurrences", "namespaces", "prior_candidate_ids",
        "unresolved_candidate_ids",
    }
)
_DECISION_KEYS = frozenset(
    {"decision_id", "kind", "survivor_id", "alias_ids", "occurrences", "reason"}
)
_PAYLOAD_KEYS = frozenset(
    {
        "payload_version", "request_id", "input_run_ids", "runs", "occurrences",
        "candidates", "conflicts", "aliases", "dedup_decisions", "authorizing",
        "completeness_claimed", "notice",
    }
)


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class CandidateError(StoreError):
    """Base class for every candidate-snapshot failure.

    Every message is static and curated: it never echoes a run id, query, source,
    URL, title, identifier, payload byte, or digest.
    """


class CandidateValidationError(CandidateError, ValueError):
    """An input request key, run id, rights, or reference is malformed."""


class CandidateConflictError(CandidateError):
    """A divergent request was supplied for an already-recorded corpus request."""


class CandidateCorruptionError(CandidateError):
    """A stored snapshot fails digest/envelope/schema/membership revalidation."""


# --------------------------------------------------------------------------- #
# Static messages (never echo a value)
# --------------------------------------------------------------------------- #
_M_BAD_REQUEST_ID = "a corpus request id is not a valid bounded identifier"
_M_BAD_RUN_ID = "a corpus run id is not a valid identifier"
_M_BAD_RUN_IDS = "the corpus run id set is not a bounded iterable of identifiers"
_M_BAD_ARTIFACT_ID = "the corpus artifact id is not a valid identifier"
_M_RUN_NOT_FOUND = "a requested corpus run id is not a recorded observation of this project"
_M_DIVERGENT = "a different corpus snapshot is already recorded for this request key"
_M_BAD_REF = "the corpus snapshot reference is not a valid same-project revision reference"
_M_NOT_FOUND = "no corpus snapshot revision exists for the supplied reference"
_M_BAD_INPUT = "the corpus snapshot arguments are not acceptable to the artifact store"
_M_CORRUPT_DIGEST = "a corpus snapshot content digest does not match its stored bytes"
_M_CORRUPT_ENVELOPE = "a corpus snapshot is not a valid versioned discovery-corpus document"
_M_CORRUPT_CANONICAL = "a corpus snapshot is not stored as canonical bytes"
_M_CORRUPT_PAYLOAD = "a corpus snapshot payload is structurally invalid"
_M_CORRUPT_RUN = "a corpus snapshot disagrees with its recorded observations"
_M_CORRUPT_COVERAGE = "a corpus snapshot does not cover exactly its observed occurrences"
_M_CORRUPT_MEMBERSHIP = "a corpus snapshot candidate membership is internally inconsistent"
_M_CORRUPT_INTEGRITY = "a corpus snapshot asserts a forbidden clean integrity status"
_M_SQL = "the corpus snapshot could not be read or written safely"
_M_FROZEN_CORPUS = (
    "this corpus artifact already carries an authentic human-approved freeze and "
    "cannot accept new revisions; choose a new corpus artifact id for a new "
    "cumulative snapshot so the frozen roster stays unchanged (query observations "
    "may still be recorded, and a new corpus artifact reconciles freely)"
)


# --------------------------------------------------------------------------- #
# Small strict helpers
# --------------------------------------------------------------------------- #
def _reject_surrogates(value: str, message: str) -> None:
    for char in value:
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise CandidateValidationError(message)


def _request_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "request_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise CandidateValidationError(_M_BAD_REQUEST_ID) from exc
    if len(normalized) > MAX_REQUEST_ID_CHARS:
        raise CandidateValidationError(_M_BAD_REQUEST_ID)
    _reject_surrogates(normalized, _M_BAD_REQUEST_ID)
    return normalized


def _run_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "run_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise CandidateValidationError(_M_BAD_RUN_ID) from exc
    if not (1 <= len(normalized) <= MAX_REQUEST_ID_CHARS):
        raise CandidateValidationError(_M_BAD_RUN_ID)
    _reject_surrogates(normalized, _M_BAD_RUN_ID)
    return normalized


def _artifact_identifier(value: object) -> str:
    try:
        return validate_identifier(value, "artifact_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise CandidateValidationError(_M_BAD_ARTIFACT_ID) from exc


def _normalize_run_ids(
    run_ids: object, error: type[CandidateError] = CandidateValidationError
) -> list[str]:
    """Return a sorted, unique, validated run-id list (never a value echo)."""
    if isinstance(run_ids, (str, bytes, bytearray)):
        raise error(_M_BAD_RUN_IDS)
    try:
        items = list(run_ids)  # type: ignore[arg-type]
    except TypeError as exc:
        raise error(_M_BAD_RUN_IDS) from exc
    if len(items) > MAX_INPUT_RUN_IDS:
        raise error(_M_BAD_RUN_IDS)
    normalized: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise error(_M_BAD_RUN_IDS)
        try:
            normalized.add(_run_identifier(item))
        except CandidateValidationError as exc:
            raise error(_M_BAD_RUN_IDS) from exc
    return sorted(normalized)


def _origin_locator(request_id: str) -> str:
    """The deterministic origin locator that identifies one corpus request."""
    return "discovery-corpus:" + request_id


def _mint_candidate_id(project_id: str, pin: tuple[str, int]) -> str:
    """Mint the opaque stable candidate id founded by one occurrence pin.

    The digest is over provenance *metadata* (the project id and the first
    ``(run_id, record_index)`` occurrence that founded the candidate) -- never a
    title, abstract, or content hash -- so it is not a scientific identity. It is
    minted once on first encounter and then persisted/reused. Equal for a given
    project regardless of the order of the supplied run ids, and distinct across
    projects.
    """
    material = canonical_json(["discovery.candidate", project_id, pin[0], pin[1]])
    return "cand_" + hashlib.sha256(material).hexdigest()[:32]


def _run_meta(record) -> dict:
    """The stored, auditable metadata + immutable digest of one observation."""
    return {
        "run_id": record.run_id,
        "request_id": record.request_id,
        "result_sha256": record.result_sha256,
        "result_bytes": record.result_bytes,
        "result_type": record.result_type,
        "source": record.source,
        "complete": record.complete,
        "stop_reason": record.stop_reason,
        "failure": record.failure,
        "page_count": record.page_count,
        "record_count": record.record_count,
        "attempt_count": record.attempt_count,
        "schema_version": record.schema_version,
    }


def _report_pin_tokens(report) -> dict[tuple[str, int], tuple[tuple[str, str], ...]]:
    """Map every reported occurrence pin to its exact T1 identifier tokens."""
    mapping: dict[tuple[str, int], tuple[tuple[str, str], ...]] = {}
    for group in report.groups:
        for occurrence in group.occurrences:
            mapping[(occurrence.run_id, occurrence.record_index)] = tuple(
                (token.namespace, token.value) for token in occurrence.tokens
            )
    return mapping


def _tokens_of(
    pins: Iterable[tuple[str, int]],
    pin_tokens: dict[tuple[str, int], tuple[tuple[str, str], ...]],
) -> list[list[str]]:
    seen: set[tuple[str, str]] = set()
    for pin in pins:
        for namespace, value in pin_tokens.get(pin, ()):
            seen.add((namespace, value))
    return [[namespace, value] for namespace, value in sorted(seen)]


def _doc_pins(doc: dict) -> list[tuple[str, int]]:
    return [(pin[0], pin[1]) for pin in doc["occurrences"]]


def _numeric_ids_are_ordered(ids: list[str], prefix: str) -> bool:
    """True iff ``ids`` are unique and non-decreasing by numeric suffix.

    Conflict/decision ids are minted as ``<prefix><zero-padded int>`` but the
    suffix may exceed four digits at scale (a single revision at the ~10,000
    target mints ``dedup-10000``, ``dedup-10001``, ...). A plain lexicographic
    compare would wrongly order ``dedup-10000`` *before* ``dedup-9999``, so
    ordering is checked on the parsed integers instead. For every historical
    snapshot (fewer than 10,000 ids, all exactly four digits) this is identical
    to the previous lexicographic check.
    """
    numbers: list[int] = []
    for value in ids:
        try:
            numbers.append(int(value[len(prefix):]))
        except (TypeError, ValueError):
            return False
    return numbers == sorted(numbers) and len(set(ids)) == len(ids)


def _candidate_doc(
    candidate_id: str,
    pins: list[tuple[str, int]],
    tokens: list[list[str]],
    resolved: bool,
    unresolved_reason: str | None,
    conflict_refs: list[str],
    aliases: list[str],
) -> dict:
    return {
        "candidate_id": candidate_id,
        "occurrences": [[pin[0], pin[1]] for pin in sorted(pins)],
        "tokens": tokens,
        "resolved": bool(resolved),
        "unresolved_reason": unresolved_reason,
        "conflict_refs": sorted(conflict_refs),
        "aliases": sorted(aliases),
        "integrity": dict(_INTEGRITY_INITIAL),
    }


# --------------------------------------------------------------------------- #
# Candidate assignment (deterministic; every occurrence retained)
# --------------------------------------------------------------------------- #
def _assign_candidates(
    project_id: str, prior_candidates: dict[str, dict], report
) -> tuple[list[dict], list[dict], list[list[str]], list[dict]]:
    """Assign occurrences to stable candidates over the cumulative run set.

    ``prior_candidates`` are the candidate docs of the prior cumulative snapshot
    (empty for a first ingest). Returns ``(candidates, conflicts, aliases,
    dedup_decisions)``. The result is a deterministic function of the prior
    snapshot and the T1 report -- never of the order in which runs were supplied.
    """
    pin_owner: dict[tuple[str, int], str] = {}
    for candidate_id, doc in prior_candidates.items():
        for pin in _doc_pins(doc):
            pin_owner[pin] = candidate_id

    candidates: dict[str, dict] = {
        candidate_id: {
            "candidate_id": doc["candidate_id"],
            "occurrences": [list(pin) for pin in _doc_pins(doc)],
            "tokens": [list(token) for token in doc["tokens"]],
            "resolved": bool(doc["resolved"]),
            "unresolved_reason": doc["unresolved_reason"],
            "conflict_refs": sorted(doc["conflict_refs"]),
            "aliases": sorted(doc["aliases"]),
            "integrity": dict(doc["integrity"]),
        }
        for candidate_id, doc in prior_candidates.items()
    }

    pin_tokens = _report_pin_tokens(report)
    conflicts: list[dict] = []
    aliases: list[list[str]] = []
    decisions: list[dict] = []
    decision_no = 0

    def record_decision(
        kind: str,
        survivor: str,
        alias_ids: Iterable[str],
        occurrences: Iterable[tuple[str, int]],
        reason: str,
    ) -> None:
        nonlocal decision_no
        decision_no += 1
        decisions.append(
            {
                "decision_id": f"dedup-{decision_no:04d}",
                "kind": kind,
                "survivor_id": survivor,
                "alias_ids": sorted(alias_ids),
                "occurrences": [[pin[0], pin[1]] for pin in sorted(occurrences)],
                "reason": reason,
            }
        )

    for group in report.groups:
        pins = sorted((occ.run_id, occ.record_index) for occ in group.occurrences)
        owned = sorted({pin_owner[pin] for pin in pins if pin in pin_owner})
        new_pins = [pin for pin in pins if pin not in pin_owner]

        if group.classification == CLASS_IDENTIFIER_CONFLICT:
            # Never silently merge a conflict: preserve prior candidates, assign
            # explicit occurrence-local unresolved candidates, record membership.
            conflict_id = f"conflict-{len(conflicts) + 1:04d}"
            unresolved_ids: list[str] = []
            for pin in new_pins:
                candidate_id = _mint_candidate_id(project_id, pin)
                candidates[candidate_id] = _candidate_doc(
                    candidate_id,
                    [pin],
                    _tokens_of([pin], pin_tokens),
                    False,
                    "identifier_conflict",
                    [conflict_id],
                    [],
                )
                unresolved_ids.append(candidate_id)
            for candidate_id in owned:
                candidates[candidate_id]["conflict_refs"] = sorted(
                    set(candidates[candidate_id]["conflict_refs"]) | {conflict_id}
                )
            conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "occurrences": [[pin[0], pin[1]] for pin in pins],
                    "namespaces": {
                        namespace: list(values) for namespace, values in group.conflicts
                    },
                    "prior_candidate_ids": list(owned),
                    "unresolved_candidate_ids": sorted(unresolved_ids),
                }
            )
            continue

        if not owned:
            if group.classification == CLASS_MISSING_IDENTIFIERS:
                # A missing-identifier occurrence is its own explicit unresolved
                # candidate: no title/year/fuzzy matching is ever attempted.
                pin = pins[0]
                candidate_id = _mint_candidate_id(project_id, pin)
                candidates[candidate_id] = _candidate_doc(
                    candidate_id, [pin], [], False, "missing_identifiers", [], []
                )
            else:
                candidate_id = _mint_candidate_id(project_id, new_pins[0])
                candidates[candidate_id] = _candidate_doc(
                    candidate_id,
                    new_pins,
                    _tokens_of(new_pins, pin_tokens),
                    True,
                    None,
                    [],
                    [],
                )
                record_decision("minted", candidate_id, [], new_pins, "first_encounter")
        elif len(owned) == 1:
            candidate_id = owned[0]
            if new_pins:
                merged = sorted(set(_doc_pins(candidates[candidate_id])) | set(new_pins))
                candidates[candidate_id]["occurrences"] = [
                    [pin[0], pin[1]] for pin in merged
                ]
                record_decision(
                    "occurrence_attached",
                    candidate_id,
                    [],
                    new_pins,
                    "exact_token_occurrence_match",
                )
        else:
            # A new consistent bridging observation connects prior candidate ids:
            # a deterministic survivor keeps the occurrences and provenance, the
            # other ids become explicit aliases, and the merge is recorded.
            survivor = min(owned)
            others = sorted(set(owned) - {survivor})
            merged_set: set[tuple[str, int]] = set()
            for candidate_id in owned:
                merged_set |= set(_doc_pins(candidates[candidate_id]))
            merged_set |= set(new_pins)
            alias_set = set(candidates[survivor]["aliases"])
            for candidate_id in others:
                alias_set |= set(candidates[candidate_id]["aliases"])
                alias_set.add(candidate_id)
                candidates.pop(candidate_id, None)
                aliases.append([candidate_id, survivor])
            candidates[survivor]["occurrences"] = [
                [pin[0], pin[1]] for pin in sorted(merged_set)
            ]
            candidates[survivor]["aliases"] = sorted(alias_set)
            candidates[survivor]["resolved"] = True
            candidates[survivor]["unresolved_reason"] = None
            record_decision(
                "merged_bridging",
                survivor,
                others,
                new_pins,
                "consistent_bridging_observation",
            )

    # Re-derive every candidate's exact token set from its actual occurrences.
    for doc in candidates.values():
        doc["tokens"] = _tokens_of(_doc_pins(doc), pin_tokens)
    ordered = [candidates[candidate_id] for candidate_id in sorted(candidates)]
    return ordered, conflicts, sorted(aliases), decisions


# --------------------------------------------------------------------------- #
# Snapshot decoding + strict revalidation (from actual observations)
# --------------------------------------------------------------------------- #
def _corruption(message: str = _M_CORRUPT_PAYLOAD) -> CandidateCorruptionError:
    return CandidateCorruptionError(message)


def _load_observation(discovery: DiscoveryRepository, run_id: str):
    """Read one observation, surfacing observation corruption as snapshot corruption."""
    try:
        return discovery.get_search(run_id)
    except DiscoverySearchError as exc:
        raise CandidateCorruptionError(_M_CORRUPT_RUN) from exc


def _decode_content(
    artifacts: ArtifactRepository,
    discovery: DiscoveryRepository,
    revision: Revision,
    project_id: str,
) -> dict:
    """Read one revision's bytes, verify them, and revalidate the payload."""
    content = artifacts.read_content(revision.artifact_id, revision.revision_id)
    if sha256_digest(content) != revision.content_sha256:
        raise CandidateCorruptionError(_M_CORRUPT_DIGEST)
    try:
        data = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise CandidateCorruptionError(_M_CORRUPT_ENVELOPE) from exc
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "document_type",
        "payload",
    }:
        raise CandidateCorruptionError(_M_CORRUPT_ENVELOPE)
    if data["schema_version"] != CORPUS_ENVELOPE_SCHEMA_VERSION:
        raise CandidateCorruptionError(_M_CORRUPT_ENVELOPE)
    if data["document_type"] != DOCUMENT_TYPE:
        raise CandidateCorruptionError(_M_CORRUPT_ENVELOPE)
    try:
        if canonical_json(data) != content:
            raise CandidateCorruptionError(_M_CORRUPT_CANONICAL)
    except RecordValidationError as exc:
        raise CandidateCorruptionError(_M_CORRUPT_CANONICAL) from exc
    return _validate_payload(data["payload"], project_id, discovery)


def _as_pin(value: object) -> tuple[str, int] | None:
    """Validate one ``[run_id, record_index]`` pin; return it or ``None``."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    run_id, index = value
    if not isinstance(run_id, str):
        return None
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    try:
        return (_run_identifier(run_id), index)
    except CandidateValidationError:
        return None


def _as_sorted_pins(value: object) -> list[tuple[str, int]] | None:
    if not isinstance(value, list):
        return None
    pins: list[tuple[str, int]] = []
    for item in value:
        pin = _as_pin(item)
        if pin is None:
            return None
        pins.append(pin)
    if pins != sorted(pins) or len(set(pins)) != len(pins):
        return None
    return pins


def _as_sorted_tokens(value: object) -> list[tuple[str, str]] | None:
    if not isinstance(value, list):
        return None
    tokens: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not item[1]
        ):
            return None
        tokens.append((item[0], item[1]))
    if tokens != sorted(tokens) or len(set(tokens)) != len(tokens):
        return None
    return tokens


def _validate_payload(
    payload: object, project_id: str, discovery: DiscoveryRepository
) -> dict:
    """Fully revalidate one payload, re-deriving everything from observations.

    Nothing is trusted from the payload: the run membership, digests, metadata,
    occurrence coverage, candidate token sets, conflict membership, alias graph,
    and integrity status are all independently re-derived from the *actual*
    recorded observations (which are themselves revalidated by their own store
    API). Any disagreement is a static :class:`CandidateCorruptionError`.
    """
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise _corruption()
    if payload["payload_version"] != CORPUS_PAYLOAD_VERSION:
        raise _corruption()
    if payload["authorizing"] is not False or payload["completeness_claimed"] is not False:
        raise _corruption()
    if payload["notice"] != CORPUS_NOTICE:
        raise _corruption()
    try:
        request_id = _request_identifier(payload["request_id"])
        input_run_ids = _normalize_run_ids(
            payload["input_run_ids"], CandidateCorruptionError
        )
    except CandidateValidationError as exc:
        raise _corruption() from exc
    if payload["input_run_ids"] != input_run_ids:
        raise _corruption()

    # -- runs re-derived from the actual observations ------------------------ #
    runs = payload["runs"]
    if not isinstance(runs, list):
        raise _corruption()
    run_entries: list[dict] = []
    run_ids: list[str] = []
    run_records: dict[str, object] = {}
    for entry in runs:
        if not isinstance(entry, dict) or set(entry) != _RUN_META_KEYS:
            raise _corruption()
        try:
            run_id = _run_identifier(entry["run_id"])
        except CandidateValidationError as exc:
            raise _corruption() from exc
        run_ids.append(run_id)
        run_entries.append(entry)
    if run_ids != sorted(run_ids) or len(set(run_ids)) != len(run_ids):
        raise _corruption(_M_CORRUPT_RUN)
    if not set(input_run_ids) <= set(run_ids):
        raise _corruption(_M_CORRUPT_RUN)
    for entry, run_id in zip(run_entries, run_ids):
        record = _load_observation(discovery, run_id)
        if record is None:
            raise _corruption(_M_CORRUPT_RUN)
        if entry != _run_meta(record):
            raise _corruption(_M_CORRUPT_RUN)
        run_records[run_id] = record

    # -- occurrence coverage: exactly every record of every run -------------- #
    all_pins = _as_sorted_pins(payload["occurrences"])
    if all_pins is None:
        raise _corruption(_M_CORRUPT_COVERAGE)
    expected_pins = sorted(
        (run_id, index)
        for run_id in run_ids
        for index in range(run_records[run_id].record_count)
    )
    if all_pins != expected_pins:
        raise _corruption(_M_CORRUPT_COVERAGE)

    # -- re-derive exact tokens from T1 over the actual observations --------- #
    try:
        report = reconcile_searches(
            [SearchRun(run_id, run_records[run_id].result) for run_id in run_ids]
        )
    except DiscoveryReconciliationError as exc:
        raise _corruption(_M_CORRUPT_RUN) from exc
    pin_tokens = _report_pin_tokens(report)

    # -- candidates ---------------------------------------------------------- #
    candidates = payload["candidates"]
    if not isinstance(candidates, list):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    candidate_ids: list[str] = []
    candidate_pins: dict[str, list[tuple[str, int]]] = {}
    candidate_alias_map: dict[str, list[str]] = {}
    candidate_conflict_map: dict[str, list[str]] = {}
    candidate_resolved: dict[str, bool] = {}
    candidate_reason: dict[str, str | None] = {}
    seen_pins: set[tuple[str, int]] = set()
    for doc in candidates:
        if not isinstance(doc, dict) or set(doc) != _CANDIDATE_KEYS:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        try:
            candidate_id = _run_identifier(doc["candidate_id"])
        except CandidateValidationError as exc:
            raise _corruption(_M_CORRUPT_MEMBERSHIP) from exc
        pins = _as_sorted_pins(doc["occurrences"])
        if pins is None or not pins:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        tokens = _as_sorted_tokens(doc["tokens"])
        if tokens is None:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if tokens != [tuple(token) for token in _tokens_of(pins, pin_tokens)]:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        resolved = doc["resolved"]
        reason = doc["unresolved_reason"]
        if not isinstance(resolved, bool):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if reason is not None and reason not in UNRESOLVED_REASONS:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if resolved == (reason is not None):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if resolved and not tokens:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if reason == "missing_identifiers" and (len(pins) != 1 or tokens):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        conflict_refs = doc["conflict_refs"]
        if not isinstance(conflict_refs, list) or conflict_refs != sorted(conflict_refs):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if len(set(conflict_refs)) != len(conflict_refs):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if reason == "identifier_conflict" and not conflict_refs:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        aliases_field = doc["aliases"]
        if not isinstance(aliases_field, list) or aliases_field != sorted(aliases_field):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if len(set(aliases_field)) != len(aliases_field):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if doc["integrity"] != _INTEGRITY_INITIAL:
            raise _corruption(_M_CORRUPT_INTEGRITY)
        candidate_ids.append(candidate_id)
        candidate_pins[candidate_id] = pins
        candidate_alias_map[candidate_id] = aliases_field
        candidate_conflict_map[candidate_id] = conflict_refs
        candidate_resolved[candidate_id] = resolved
        candidate_reason[candidate_id] = reason
        for pin in pins:
            if pin in seen_pins:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            seen_pins.add(pin)
    if candidate_ids != sorted(candidate_ids) or len(set(candidate_ids)) != len(
        candidate_ids
    ):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    if seen_pins != set(all_pins):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)

    # -- aliases ------------------------------------------------------------- #
    alias_records = payload["aliases"]
    if not isinstance(alias_records, list):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    alias_pairs: list[list[str]] = []
    expected_alias_edges: set[tuple[str, str]] = set()
    for pair in alias_records:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
        ):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        alias_id, survivor_id = pair
        if alias_id in candidate_pins or survivor_id not in candidate_pins:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        alias_pairs.append([alias_id, survivor_id])
        expected_alias_edges.add((alias_id, survivor_id))
    if alias_pairs != sorted(alias_pairs) or len(expected_alias_edges) != len(alias_pairs):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    observed_alias_edges: set[tuple[str, str]] = set()
    for candidate_id in candidate_ids:
        for alias_id in candidate_alias_map[candidate_id]:
            if alias_id in candidate_pins:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            observed_alias_edges.add((alias_id, candidate_id))
    if observed_alias_edges != expected_alias_edges:
        raise _corruption(_M_CORRUPT_MEMBERSHIP)

    # -- conflicts ----------------------------------------------------------- #
    conflicts = payload["conflicts"]
    if not isinstance(conflicts, list):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    conflict_ids: list[str] = []
    for entry in conflicts:
        if not isinstance(entry, dict) or set(entry) != _CONFLICT_KEYS:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        conflict_id = entry["conflict_id"]
        if (
            not isinstance(conflict_id, str)
            or _CONFLICT_ID_RE.fullmatch(conflict_id) is None
        ):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        pins = _as_sorted_pins(entry["occurrences"])
        if pins is None or not pins:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        namespaces = entry["namespaces"]
        if not isinstance(namespaces, dict):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        for namespace, values in namespaces.items():
            if (
                not isinstance(namespace, str)
                or not namespace
                or not isinstance(values, list)
                or len(values) < 2
                or values != sorted(values)
                or len(set(values)) != len(values)
                or not all(isinstance(value, str) and value for value in values)
            ):
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if not namespaces:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        prior_ids = entry["prior_candidate_ids"]
        unresolved_ids = entry["unresolved_candidate_ids"]
        if not isinstance(prior_ids, list) or prior_ids != sorted(prior_ids):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if not isinstance(unresolved_ids, list) or unresolved_ids != sorted(
            unresolved_ids
        ):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        referenced = set(prior_ids) | set(unresolved_ids)
        if not referenced or not referenced <= set(candidate_pins):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        membership_pins: set[tuple[str, int]] = set()
        for candidate_id in referenced:
            if conflict_id not in candidate_conflict_map[candidate_id]:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            membership_pins |= set(candidate_pins[candidate_id])
        if membership_pins != set(pins):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        for candidate_id in candidate_ids:
            refs = candidate_conflict_map[candidate_id]
            if len(set(refs)) != len(refs):
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
        conflict_ids.append(conflict_id)
    if not _numeric_ids_are_ordered(conflict_ids, "conflict-"):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    for candidate_id in candidate_ids:
        for ref in candidate_conflict_map[candidate_id]:
            if ref not in conflict_ids:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)

    # -- dedup decisions ----------------------------------------------------- #
    decisions = payload["dedup_decisions"]
    if not isinstance(decisions, list):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)
    decision_ids: list[str] = []
    for entry in decisions:
        if not isinstance(entry, dict) or set(entry) != _DECISION_KEYS:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        decision_id = entry["decision_id"]
        if (
            not isinstance(decision_id, str)
            or _DECISION_ID_RE.fullmatch(decision_id) is None
        ):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if entry["kind"] not in DECISION_KINDS:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        survivor_id = entry["survivor_id"]
        if survivor_id not in candidate_pins:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        alias_ids = entry["alias_ids"]
        if not isinstance(alias_ids, list) or alias_ids != sorted(alias_ids):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if not set(alias_ids) <= set(candidate_alias_map[survivor_id]):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        occurrences = _as_sorted_pins(entry["occurrences"])
        if occurrences is None:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        if not set(occurrences) <= set(candidate_pins[survivor_id]):
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        reason = entry["reason"]
        if not isinstance(reason, str) or not reason:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        decision_ids.append(decision_id)
    if not _numeric_ids_are_ordered(decision_ids, "dedup-"):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)

    # -- re-derive the exact T1 component structure over the observations ----- #
    component_of_pin: dict[tuple[str, int], int] = {}
    component_specs: list[tuple[str, list[tuple[str, int]], dict[str, list[str]]]] = []
    for group in report.groups:
        comp_pins = sorted((occ.run_id, occ.record_index) for occ in group.occurrences)
        index = len(component_specs)
        for pin in comp_pins:
            component_of_pin[pin] = index
        value_sets: dict[str, set[str]] = {}
        for pin in comp_pins:
            for namespace, value in pin_tokens.get(pin, ()):
                value_sets.setdefault(namespace, set()).add(value)
        derived = {
            namespace: sorted(values)
            for namespace, values in value_sets.items()
            if len(values) > 1
        }
        component_specs.append((group.classification, comp_pins, derived))
    if set(component_of_pin) != set(all_pins):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)

    candidates_by_component: dict[int, list[str]] = {}
    for candidate_id in candidate_ids:
        indexes = {component_of_pin[pin] for pin in candidate_pins[candidate_id]}
        if len(indexes) != 1:
            raise _corruption(_M_CORRUPT_MEMBERSHIP)
        candidates_by_component.setdefault(indexes.pop(), []).append(candidate_id)

    matched_conflicts: set[str] = set()
    for index, (classification, comp_pins, derived) in enumerate(component_specs):
        members = candidates_by_component.get(index, [])
        comp_pin_set = set(comp_pins)
        if classification == CLASS_IDENTIFIER_CONFLICT:
            matching = [
                entry
                for entry in conflicts
                if _as_sorted_pins(entry["occurrences"]) == comp_pins
            ]
            if len(matching) != 1:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            if matching[0]["namespaces"] != derived:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            covered: set[tuple[str, int]] = set()
            for candidate_id in members:
                covered |= set(candidate_pins[candidate_id])
            if not members or covered != comp_pin_set:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            matched_conflicts.add(matching[0]["conflict_id"])
        else:
            if len(members) != 1:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            candidate_id = members[0]
            if set(candidate_pins[candidate_id]) != comp_pin_set:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
            if classification == CLASS_MISSING_IDENTIFIERS:
                if candidate_resolved[candidate_id] or (
                    candidate_reason[candidate_id] != "missing_identifiers"
                ):
                    raise _corruption(_M_CORRUPT_MEMBERSHIP)
            elif not candidate_resolved[candidate_id]:
                raise _corruption(_M_CORRUPT_MEMBERSHIP)
    if matched_conflicts != set(conflict_ids):
        raise _corruption(_M_CORRUPT_MEMBERSHIP)

    return {
        "payload_version": CORPUS_PAYLOAD_VERSION,
        "request_id": request_id,
        "input_run_ids": input_run_ids,
        "runs": run_entries,
        "occurrences": [list(pin) for pin in all_pins],
        "candidates": candidates,
        "conflicts": conflicts,
        "aliases": alias_pairs,
        "dedup_decisions": decisions,
        "authorizing": False,
        "completeness_claimed": False,
        "notice": CORPUS_NOTICE,
    }


def _assemble(revision: Revision, payload: dict) -> dict:
    """Return the validated snapshot document (revision metadata + payload)."""
    return {
        "project_id": revision.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "content_sha256": revision.content_sha256,
        "byte_size": revision.byte_size,
        "document_type": DOCUMENT_TYPE,
        "schema_version": CORPUS_ENVELOPE_SCHEMA_VERSION,
        "payload_version": payload["payload_version"],
        "request_id": payload["request_id"],
        "input_run_ids": payload["input_run_ids"],
        "runs": payload["runs"],
        "occurrences": payload["occurrences"],
        "candidates": payload["candidates"],
        "conflicts": payload["conflicts"],
        "aliases": payload["aliases"],
        "dedup_decisions": payload["dedup_decisions"],
        "authorizing": False,
        "completeness_claimed": False,
        "notice": payload["notice"],
    }


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class CandidateRepository:
    """Durable cumulative publication-candidate snapshots for one project store.

    Typical use::

        corpus = CandidateRepository(store)

        rid = corpus.new_revision_id()
        rights = build_valid_rights_record(rid)      # caller-supplied, exact
        corpus.ingest(
            ["dsr_..."], request_id="corpus-1", rights=rights, revision_id=rid,
        )

        snapshot = corpus.current()                  # dict | None
        exact = corpus.get({"project_id": store.project_id,
                            "artifact_id": "discovery.corpus",
                            "revision_id": rid})

    The repository performs **no** network, provider, approval, model, or science
    work: it reads local observations and writes local immutable revisions.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._artifacts = ArtifactRepository(store)
        self._discovery = DiscoveryRepository(store)

    @property
    def project_id(self) -> str:
        """The actual store's project id; no caller may select a project."""
        return self._store.project_id

    @staticmethod
    def new_revision_id() -> str:
        """A fresh opaque revision id (caller-assigned for exact rights binding)."""
        return ArtifactRepository.new_revision_id()

    # -- write path ---------------------------------------------------------- #
    def ingest(
        self,
        run_ids: Iterable[str],
        *,
        request_id: str,
        rights: object,
        revision_id: str | None = None,
        artifact_id: str = DEFAULT_CORPUS_ARTIFACT_ID,
        dependencies: object = (),
    ) -> Revision:
        """Record a cumulative snapshot, or replay its historical revision.

        ``run_ids`` are added to the runs already retained by the current
        snapshot; the new snapshot therefore names **all** cumulative run ids and
        **never drops** a prior run. ``rights`` (a validated ``RightsRecord`` or a
        stored ``RightsRecordRef``) is pinned to this exact revision and storage
        is allowed only when it permits it. An identical replay of ``request_id``
        with the same supplied run-id set returns the historical revision; a
        divergent reuse raises :class:`CandidateConflictError`. A genuinely new
        revision names the current snapshot as its parent, so a concurrent stale
        write is refused rather than overwriting.
        """
        artifact_id = _artifact_identifier(artifact_id)
        request_id = _request_identifier(request_id)
        supplied = _normalize_run_ids(run_ids)

        # Validate the new runs exist and are intact before any write.
        for run_id in supplied:
            if _load_observation(self._discovery, run_id) is None:
                raise CandidateValidationError(_M_RUN_NOT_FOUND)

        self._artifacts.create_artifact(
            artifact_id, kind="report", created_by="import"
        )

        # Replay: a historical revision with this exact request key.
        existing = self._find_request(artifact_id, request_id)
        if existing is not None:
            existing_revision, existing_payload = existing
            if existing_payload["input_run_ids"] == supplied:
                return existing_revision
            raise CandidateConflictError(_M_DIVERGENT)

        # Freeze immutability (Step 05 AC6): a logical corpus artifact that already
        # carries an authentic human-approved freeze must not silently accept a new
        # cumulative revision. The authoritative check runs as a trusted
        # ``write_precondition`` *inside* the artifact store's own ``BEGIN
        # IMMEDIATE`` append transaction (after the exact replay shortcut and
        # before any new content is written), so no other writer — including a
        # concurrent owner approval of a freeze — can commit between the check and
        # the INSERT. It is a plain Python closure (never read from content,
        # envelope, or configuration) that grants no authority and only vetoes the
        # append. An identical replay above is still historical-safe; only a
        # genuinely new snapshot is refused, with static guidance to choose a new
        # corpus artifact.
        from .freeze import corpus_has_authentic_freeze  # noqa: PLC0415

        def _write_precondition() -> None:
            if corpus_has_authentic_freeze(self._store, artifact_id):
                raise CandidateValidationError(_M_FROZEN_CORPUS)

        prior_revision = self._current_revision(artifact_id)
        prior_payload = None
        if prior_revision is not None:
            prior_payload = _decode_content(
                self._artifacts, self._discovery, prior_revision, self.project_id
            )

        prior_run_ids = (
            [entry["run_id"] for entry in prior_payload["runs"]]
            if prior_payload is not None
            else []
        )
        cumulative = sorted(set(prior_run_ids) | set(supplied))

        records: dict[str, object] = {}
        for run_id in cumulative:
            record = _load_observation(self._discovery, run_id)
            if record is None:
                # A prior cumulative run id that is no longer readable is a
                # corrupt/inconsistent ledger, never a silent exclusion.
                raise CandidateCorruptionError(_M_CORRUPT_RUN)
            records[run_id] = record

        try:
            report = reconcile_searches(
                [SearchRun(run_id, records[run_id].result) for run_id in cumulative]
            )
        except DiscoveryReconciliationError as exc:
            raise CandidateValidationError(_M_BAD_RUN_IDS) from exc

        prior_candidates = (
            {doc["candidate_id"]: doc for doc in prior_payload["candidates"]}
            if prior_payload is not None
            else {}
        )
        candidates, conflicts, aliases, decisions = _assign_candidates(
            self.project_id, prior_candidates, report
        )
        all_pins = sorted(
            (occ.run_id, occ.record_index)
            for group in report.groups
            for occ in group.occurrences
        )
        payload = {
            "payload_version": CORPUS_PAYLOAD_VERSION,
            "request_id": request_id,
            "input_run_ids": supplied,
            "runs": [_run_meta(records[run_id]) for run_id in cumulative],
            "occurrences": [[pin[0], pin[1]] for pin in all_pins],
            "candidates": candidates,
            "conflicts": conflicts,
            "aliases": aliases,
            "dedup_decisions": decisions,
            "authorizing": False,
            "completeness_claimed": False,
            "notice": CORPUS_NOTICE,
        }

        parent_revision_id = (
            prior_revision.revision_id if prior_revision is not None else None
        )
        try:
            return self._artifacts.import_revision(
                artifact_id=artifact_id,
                content={
                    "schema_version": CORPUS_ENVELOPE_SCHEMA_VERSION,
                    "document_type": DOCUMENT_TYPE,
                    "payload": payload,
                },
                origin_locator=_origin_locator(request_id),
                rights=rights,
                revision_id=revision_id,
                parent_revision_id=parent_revision_id,
                dependencies=dependencies,
                acquired_by="import",
                request_id=request_id,
                write_precondition=_write_precondition,
            )
        except ArtifactConflictError as exc:
            raise CandidateConflictError(_M_DIVERGENT) from exc
        except RecordValidationError as exc:
            raise CandidateValidationError(_M_BAD_INPUT) from exc

    # -- read path ----------------------------------------------------------- #
    def current(self, artifact_id: str = DEFAULT_CORPUS_ARTIFACT_ID) -> dict | None:
        """Read the current cumulative snapshot, or ``None`` when absent."""
        artifact_id = _artifact_identifier(artifact_id)
        revision = self._current_revision(artifact_id)
        if revision is None:
            return None
        payload = _decode_content(
            self._artifacts, self._discovery, revision, self.project_id
        )
        return _assemble(revision, payload)

    def get(self, ref: object) -> dict:
        """Read one exact snapshot by explicit same-project revision reference.

        ``ref`` is ``{project_id, artifact_id, revision_id}``; a cross-project
        reference is refused. A missing revision is a static
        :class:`CandidateValidationError`.
        """
        try:
            normalized = validate_ref(ref, self.project_id)
        except Exception as exc:  # RecordValidationError / TypeError
            raise CandidateValidationError(_M_BAD_REF) from exc
        revision = self._artifacts.get_revision(
            normalized["artifact_id"], normalized["revision_id"]
        )
        if revision is None:
            raise CandidateValidationError(_M_NOT_FOUND)
        payload = _decode_content(
            self._artifacts, self._discovery, revision, self.project_id
        )
        return _assemble(revision, payload)

    # -- internals ----------------------------------------------------------- #
    def _current_revision(self, artifact_id: str) -> Revision | None:
        return self._artifacts.current_revision(artifact_id)

    def _find_request(
        self, artifact_id: str, request_id: str
    ) -> tuple[Revision, dict] | None:
        """Find an existing snapshot revision for ``request_id``, if any."""
        locator = _origin_locator(request_id)
        for revision in self._artifacts.list_revisions(artifact_id):
            if revision.origin_locator != locator:
                continue
            payload = _decode_content(
                self._artifacts, self._discovery, revision, self.project_id
            )
            return revision, payload
        return None
