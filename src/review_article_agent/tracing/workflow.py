"""Durable, resumable Step 13 citation-origin tracing workflow (MVP).

This module binds the pure tracing rules in
:mod:`review_article_agent.tracing.schema` to durable storage and to the Step 12
section checks.  It is a small, deliberately minimal MVP: no CLI, no storage
schema change, no new dependency, and no real acquisition call.  The
``acquire`` port is **injected**, so a test supplies a fake that counts calls.

What one ``run`` does (per ``docs/implementation/13-origin-tracing.md``)
------------------------------------------------------------------------

* **Dependency-valid caching.**  A claim's fingerprint is the digest of the
  per-atom :func:`trace_fingerprint` (over the claim atom, the depth-0 source
  identity, the policy, the cached upstream digests, and the Step 12 depth-0
  verdict) plus that verdict.  If a persisted claim entry has an equal
  fingerprint it is reused with **no** acquisition calls; otherwise the claim is
  retraced and the previous fingerprint is recorded as invalidated.  A changed
  claim text, depth-0 source ``sha256``, policy, or Step 12 verdict changes the
  fingerprint.  Additionally, an optional ``source_digests`` mapping invalidates a
  cached entry when a cached upstream node's sha no longer matches the current
  digest supplied for that source.
* **Persist after every claim**, as a new immutable artifact revision;
  :meth:`OriginTraceRepository.read` returns the latest.
* **Section-check integration**: every Step 12 check is copied and annotated
  with ``origin_outcome``, ``requires_final_human_approval`` and
  ``origin_warning`` only -- the check's ``verdict``/citations are never changed
  and ``primary_verified`` is never emitted.
* **Read-only coverage.**  :func:`origin_coverage` is a pure audit: it never
  mutates its input and never persists.

Non-claims: this proves the mechanical obligations only.  It cannot establish
scientific truth, and a cached/reused trace is not a fresh read of the upstream
source.
"""

from __future__ import annotations

import copy
import hashlib
import json

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from ..storage.artifacts import ArtifactRepository
from .schema import trace_claim, trace_fingerprint, validate_policy

#: The generic artifact kind for an origin-trace revision (C01 §5.1).
ARTIFACT_KIND = "report"

#: The generic document envelope type for an origin-trace revision.
DOCUMENT_TYPE = "origin_trace"

#: The supported generic JSON document-envelope schema version.
SCHEMA_VERSION = 1


class OriginTraceError(ReviewAgentError):
    """A tracing workflow input or resume state is invalid."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OriginTraceError(message)


def _digest(payload) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _depth0_verdict(review_checks, claim) -> str:
    """The Step 12 verdict for the check matching the claim's target.

    A missing check is ``"unchecked"`` -- which is **not** ``"supported"``.
    """
    for check in review_checks or []:
        if (
            check.get("paragraph_id") == claim.get("paragraph_id")
            and check.get("intent_id") == claim.get("intent_id")
        ):
            return check.get("verdict")
    return "unchecked"


def _claim_fingerprint(claim, root_source, policy, depth0_verdict, previous) -> str:
    """Fingerprint a claim from its atoms, the depth-0 source, policy, cached upstream digests, and Step 12 verdict."""
    cached_digests: dict = {}
    if isinstance(previous, dict):
        for atom_trace in previous.get("atoms") or []:
            nodes = (atom_trace.get("graph") or {}).get("nodes") or []
            cached_digests[atom_trace.get("atom_id")] = {
                node.get("source_key"): node.get("sha256")
                for node in nodes
                if isinstance(node, dict)
                and node.get("depth", 0) > 0
                and node.get("source_key") is not None
            }
    per_atom = [
        trace_fingerprint(
            atom, root_source, policy, cached_digests.get(atom.get("atom_id"), {})
        )
        for atom in claim.get("atoms") or []
    ]
    return _digest([per_atom, depth0_verdict])


def _current_source_digests(sources, source_digests) -> dict:
    """Map ``source_key -> current sha256`` for depth-0 and known upstream sources."""
    current: dict = {}
    for key, source in (sources or {}).items():
        inner = source.get("source") if isinstance(source, dict) else None
        sha = inner.get("sha256") if isinstance(inner, dict) else None
        if sha is not None:
            current[key] = sha
    if source_digests:
        current.update(source_digests)
    return current


def _upstream_digest_changed(previous, current_digests) -> bool:
    """True when a cached upstream (depth>0) node's sha differs from the current one."""
    if not current_digests or not isinstance(previous, dict):
        return False
    for atom_trace in previous.get("atoms") or []:
        nodes = (atom_trace.get("graph") or {}).get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict) or node.get("depth", 0) <= 0:
                continue
            key = node.get("source_key")
            if key in current_digests and node.get("sha256") != current_digests[key]:
                return True
    return False


def _config_required_entry(claim, fingerprint) -> dict:
    """A placeholder claim entry for an unconfigured policy (nothing traced)."""
    return {
        "claim_id": claim.get("claim_id"),
        "paragraph_id": claim.get("paragraph_id"),
        "intent_id": claim.get("intent_id"),
        "root_source_key": claim.get("root_source_key"),
        "fingerprint": fingerprint,
        "cache": "miss",
        "outcome": "configuration_required",
        "sources_used": 0,
        "requires_final_human_approval": False,
        "origin_warning": None,
        "atoms": [],
    }


def _claim_entry(claim, trace, fingerprint) -> dict:
    warnings = [atom.get("warning") for atom in trace["atoms"] if atom.get("warning")]
    return {
        "claim_id": claim.get("claim_id"),
        "paragraph_id": claim.get("paragraph_id"),
        "intent_id": claim.get("intent_id"),
        "root_source_key": claim.get("root_source_key"),
        "fingerprint": fingerprint,
        "cache": "miss",
        "outcome": trace["outcome"],
        "sources_used": trace.get("sources_used", 0),
        "requires_final_human_approval": any(
            atom.get("requires_final_human_approval") for atom in trace["atoms"]
        ),
        "origin_warning": warnings[0] if warnings else None,
        "atoms": trace["atoms"],
    }


def _section_checks(review_checks, entries) -> list:
    """Copy each Step 12 check, annotating it with origin fields only.

    The check's ``verdict`` and citations are never changed, and
    ``primary_verified`` is never emitted.
    """
    by_target = {(e.get("paragraph_id"), e.get("intent_id")): e for e in entries}
    annotated = []
    for check in review_checks or []:
        copy_check = copy.deepcopy(check)
        entry = by_target.get((check.get("paragraph_id"), check.get("intent_id")))
        copy_check["origin_outcome"] = None if entry is None else entry.get("outcome")
        copy_check["requires_final_human_approval"] = bool(
            entry and entry.get("requires_final_human_approval")
        )
        copy_check["origin_warning"] = None if entry is None else entry.get("origin_warning")
        annotated.append(copy_check)
    return annotated


def origin_coverage(state) -> dict:
    """Read-only audit of a persisted trace state; never mutates and never persists.

    Counts claim outcomes, lists the secondary warnings that still require final
    human approval (with the ``cite_source_key`` of the depth-0 source actually
    read), and lists the blockers with the step each routes back to.  ``complete``
    is ``True`` when there are no blockers; ``export_ready`` additionally requires
    no pending approvals.  ``edits_applied`` is always ``False``: this operation
    never edits a claim.
    """
    entries = state.get("claims") or []
    counts: dict = {}
    requires_human_approval: list = []
    blockers: list = []
    seen_blockers: set = set()

    def add_blocker(claim_id, reason, route_to):
        key = (claim_id, reason)
        if key in seen_blockers:
            return
        seen_blockers.add(key)
        blockers.append({"claim_id": claim_id, "reason": reason, "route_to": route_to})

    for entry in entries:
        outcome = entry.get("outcome")
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "secondary_warning":
            requires_human_approval.append(
                {
                    "claim_id": entry.get("claim_id"),
                    "cite_source_key": entry.get("root_source_key"),
                    "warning": entry.get("origin_warning"),
                }
            )
        if outcome == "unsupported":
            add_blocker(entry.get("claim_id"), "unsupported", "step11_drafting/step12_review")
        elif outcome == "correction_proposed":
            add_blocker(
                entry.get("claim_id"), "correction_proposed", "step11_drafting/step12_review"
            )
        elif outcome == "configuration_required":
            add_blocker(entry.get("claim_id"), "configuration_required", "project_policy")
        for atom in entry.get("atoms") or []:
            for reason in atom.get("stopping_reasons") or []:
                if reason in ("awaiting_disposition", "full_text_unavailable"):
                    add_blocker(entry.get("claim_id"), reason, "step06_acquisition")

    complete = not blockers
    return {
        "trace_id": state.get("trace_id"),
        "counts": counts,
        "requires_human_approval": requires_human_approval,
        "blockers": blockers,
        "complete": complete,
        "export_ready": complete and not requires_human_approval,
        "edits_applied": False,
    }


class OriginTraceRepository:
    """Durable, resumable Step 13 origin tracing over a :class:`ProjectStore`.

    Typical use::

        repo = OriginTraceRepository(store)
        state = repo.run(
            "trace-1",
            claims=claims, sources=sources, policy=policy,
            review_checks=checks, acquire=my_acquire_port,
            rights=lambda artifact_id, revision_id: my_rights(artifact_id, revision_id),
        )

    ``rights`` is a callable ``(artifact_id, revision_id) -> RightsRecord`` used to
    authorise each persisted revision.  ``acquire(request)`` is the injected
    Step 06 acquisition port (see :func:`trace_atom`); no real acquisition is made
    here.
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(store)
        self._rights = None

    # -- read / persist ------------------------------------------------------ #
    def read(self, trace_id):
        """Return the latest persisted trace state (or ``None``)."""
        artifact_id = validate_identifier(trace_id, "trace_id")
        head = self.artifacts.current_revision(artifact_id)
        if head is None:
            return None
        data = self.artifacts.read_content(artifact_id, head.revision_id)
        try:
            document = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise OriginTraceError("the stored trace document is not decodable") from None
        _require(
            isinstance(document, dict)
            and document.get("schema_version") == SCHEMA_VERSION
            and document.get("document_type") == DOCUMENT_TYPE,
            "the stored document is not an origin-trace document",
        )
        payload = document.get("payload")
        _require(isinstance(payload, dict), "the stored trace payload is malformed")
        return payload

    def _persist(self, state):
        """Append the full trace state as a new immutable revision."""
        _require(self._rights is not None, "rights factory is not configured")
        artifact_id = validate_identifier(state["trace_id"], "trace_id")
        self.artifacts.create_artifact(artifact_id, kind=ARTIFACT_KIND, created_by="user")
        head = self.artifacts.current_revision(artifact_id)
        parent = None if head is None else head.revision_id
        revision_id = ArtifactRepository.new_revision_id()
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content={
                "schema_version": SCHEMA_VERSION,
                "document_type": DOCUMENT_TYPE,
                "payload": state,
            },
            origin_locator="trace:origin",
            acquired_by="user",
            rights=self._rights(artifact_id, revision_id),
            parent_revision_id=parent,
        )
        return revision_id

    # -- state ---------------------------------------------------------------- #
    @staticmethod
    def _build_state(trace_id, policy, entries, project_sources_used, acquire_calls,
                     invalidated, review_checks):
        state = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace_id,
            "document_type": DOCUMENT_TYPE,
            "policy": policy,
            "claims": entries,
            "project_sources_used": project_sources_used,
            "acquire_calls": acquire_calls,
            "invalidated": list(invalidated),
            "section_checks": _section_checks(review_checks, entries),
            "status": "in_progress",
        }
        state["status"] = (
            "complete" if origin_coverage(state)["export_ready"] else "blocked"
        )
        return state

    # -- entry point --------------------------------------------------------- #
    def run(self, trace_id, *, claims, sources, policy, review_checks, acquire, rights,
            source_digests=None):
        """Run (or resume) one durable origin trace; return the final state.

        ``source_digests`` is an optional ``source_key -> current sha256`` mapping
        covering *known upstream* sources (the depth-0 ``sources`` dict sha values
        also count).  A cached claim whose graph references one of these sources
        with a different sha is a cache miss: the old fingerprint is recorded in
        ``invalidated`` and the claim is retraced.
        """
        self._rights = rights
        artifact_id = validate_identifier(trace_id, "trace_id")
        claims = list(claims or [])

        persisted = self.read(artifact_id)
        previous_claims: dict = {}
        if persisted is not None:
            for entry in persisted.get("claims") or []:
                previous_claims[entry.get("claim_id")] = entry

        configured = validate_policy(policy)["status"] == "configured"
        current_digests = _current_source_digests(sources, source_digests)

        calls = {"n": 0}

        def counting_acquire(request):
            calls["n"] += 1
            return acquire(request)

        invalidated: list = []
        entries: list = []
        project_sources_used = 0

        for claim in claims:
            root_source = (sources or {}).get(claim.get("root_source_key"))
            _require(
                isinstance(root_source, dict),
                "claim root source is missing from sources",
            )
            depth0 = _depth0_verdict(review_checks, claim)
            previous = previous_claims.get(claim.get("claim_id"))
            candidate = _claim_fingerprint(claim, root_source, policy, depth0, previous)

            if not configured:
                entry = _config_required_entry(claim, candidate)
            elif (
                previous is not None
                and previous.get("fingerprint") == candidate
                and not _upstream_digest_changed(previous, current_digests)
            ):
                entry = copy.deepcopy(previous)
                entry["cache"] = "hit"
            else:
                if previous is not None and previous.get("fingerprint") is not None:
                    invalidated.append(previous.get("fingerprint"))
                trace = trace_claim(
                    claim,
                    root_source=root_source,
                    policy=policy,
                    project_sources_used=project_sources_used,
                    acquire=counting_acquire,
                    depth0_verdict=depth0,
                )
                # Store the fingerprint recomputed from the graph actually traced
                # so an unchanged rerun compares equal and hits the cache.
                fingerprint = _claim_fingerprint(
                    claim, root_source, policy, depth0, {"atoms": trace["atoms"]}
                )
                entry = _claim_entry(claim, trace, fingerprint)

            entries.append(entry)
            project_sources_used += entry.get("sources_used", 0)

            state = self._build_state(
                artifact_id, policy, entries, project_sources_used,
                calls["n"], invalidated, review_checks,
            )
            if not configured:
                state["status"] = "configuration_required"
            self._persist(state)

        if not claims:
            state = self._build_state(
                artifact_id, policy, entries, project_sources_used,
                calls["n"], invalidated, review_checks,
            )
            if not configured:
                state["status"] = "configuration_required"
            self._persist(state)
        return state


__all__ = [
    "ARTIFACT_KIND",
    "DOCUMENT_TYPE",
    "SCHEMA_VERSION",
    "OriginTraceError",
    "OriginTraceRepository",
    "origin_coverage",
]
