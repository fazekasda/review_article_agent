"""Typed discovery-artifact preparation/review CLI (Step 05, D1).

This module adds the root ``discovery`` command group to the CLI. It owns only
the **CLI surface** over the already-integrated discovery value layers:

* :mod:`review_article_agent.discovery.context` -- the strict typed
  discovery-context document envelope and its read-and-gate layer.
* :mod:`review_article_agent.discovery.candidates` -- durable cumulative
  publication-candidate snapshots.
* :mod:`review_article_agent.discovery.lead_runner` -- the durable, read-only
  readback of a hosted lead request (``get_lead_outcome``).

What this module **is** (``docs/implementation/05-discovery.md`` deliverable 1 and
D1 acceptance criteria 1 and 2; the personal-CLI baseline; the existing
``context``/``candidates``/``lead_runner`` APIs -- no new authority is introduced):

* ``discovery context import`` -- validate one strict typed context envelope with
  :func:`~review_article_agent.discovery.context.validate_context_document`
  **before** any write, derive the exact generic dependencies from the typed
  payload (an interview pins its ``brief_ref``; a plan pins its
  brief/interview/framing/scope/rubric and every seed), validate every referenced
  document at its exact expected type/current revision (and a seed's real
  run/index), then store the canonical envelope as one immutable
  :class:`~review_article_agent.storage.artifacts.ArtifactRepository` revision
  (``report`` kind, or ``rubric`` for a ``discovery_rubric``). It returns the
  exact ref/digest/type **and** an ``approval_request`` describing the named gate
  the new revision needs. An identical retry of a recorded logical import returns
  the historical revision (and a divergent reuse of the request key conflicts),
  reporting ``replayed`` plus a ``current``/``stale`` approval request, rather than
  being re-treated as a stale amendment. It stores a **proposal only**: it
  records no approval, fabricates no rights, accepts no actor/authentication flag,
  and never claims a gate is satisfied unless the real
  :class:`~review_article_agent.storage.human_approvals.HumanApprovalRepository`
  says so. The existing ``gate prepare``/``gate confirm`` commands remain the only
  path that records a real human approval (on the controlling terminal).
* ``discovery context show`` / ``validate`` -- read one exact stored context
  revision (current by default) through the typed store reader. The default output
  is a safe summary (type, ref, provisional and needs-approval flags -- never the
  raw abstract); ``--content`` explicitly returns the private validated document
  JSON. ``--historical`` reads an exact old revision without claiming current
  authority.
* ``discovery corpus ingest`` -- record one cumulative publication-candidate
  snapshot through :class:`~review_article_agent.discovery.candidates.CandidateRepository`
  and return the exact ref plus counts/unresolved flags. Metadata is **local only**
  and is never scientific-accepted context.
* ``discovery corpus show`` -- read one snapshot; the default summary never echoes
  raw titles/tokens, and ``--content`` is the explicit opt-in.
* ``discovery lead status`` -- the safe ``get_lead_outcome`` readback (no call).
* ``discovery lead content`` -- an explicit **PRIVATE, NON-AUTHORIZING** view of
  the stored runtime proposal text/metadata (no new call). A diagnostic outcome
  is never displayed as a successful lead proposal.

What this module is **not**:

* The discovery **background-job** submission/inspection surface
  (``search``/``expand`` jobs, ``lead configure``/``run``) lives in the addon
  :mod:`review_article_agent.cli.discovery_jobs`, and the reporting/freeze groups
  live in the sibling addon :mod:`review_article_agent.cli.discovery_reports`;
  both extend this group's parser and dispatch through the narrow hooks
  :func:`build_discovery_parser` and :func:`dispatch_discovery`. This module stays
  the owner of the context/corpus/lead-readback surface only.
* No network call, no provider/model call, no approval, no execution, and no
  scientific validation is performed or claimed.

Design rules:

* **Verified project access.** Every project is opened through the registry with
  ``expected_project_id`` (mirroring the other command groups), so a
  project-id/path mismatch or traversal is refused before any read, and the store
  is closed on every path.
* **Strict, bounded input.** A JSON file is read with a hard byte cap, decoded as
  UTF-8, and parsed with the existing strict parser
  (:func:`review_article_agent.cli.config.load_strict_json`): duplicate keys,
  non-finite numbers, and over-deep nesting are all refused, and no input value or
  path is reflected in an error.
* **No mutation on a basic validation failure.** The document, the rights record,
  the parent reference, and every referenced context document are validated
  (read-only) before the artifact/revision write.
* **Typed, curated errors.** A failure is raised as :class:`DiscoveryControlError`
  carrying a stable ``category`` (``validation`` / ``conflict`` / ``storage``) and
  a static message authored here; no raw exception text, source/title/query, path,
  or secret is echoed.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..discovery.candidates import (
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateConflictError,
    CandidateError,
    CandidateRepository,
    CandidateValidationError,
)
from ..discovery.context import (
    ARTIFACT_REVISION_GATE_KIND,
    PLAN_GATE_ID,
    PROTOCOL_GATE_KIND,
    SEED_GATE_PREFIX,
    DiscoveryContextError,
    read_context_document,
    validate_context_document,
)
from ..discovery.lead_runner import HostedLeadError, get_lead_outcome
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    require_storage_allowed,
    validate_rights_record,
)
from ..storage import ArtifactConflictError, ArtifactRepository, ProjectStore, paths
from ..storage.agent_runtime import RuntimeRepository
from ..storage.discovery import DiscoveryRepository, DiscoverySearchError
from ..storage.human_approvals import HumanApprovalRepository
from .config import STRICT_JSON_REASONS, StrictJsonError, load_strict_json

__all__ = [
    "DISCOVERY_COMMANDS",
    "DiscoveryControlError",
    "build_discovery_parser",
    "dispatch_discovery",
    "discovery_context_import",
    "discovery_context_show",
    "discovery_corpus_ingest",
    "discovery_corpus_show",
    "discovery_lead_content",
    "discovery_lead_status",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: Hard cap on any JSON file this command group will even attempt to parse.
MAX_DISCOVERY_INPUT_BYTES = 8 << 20  # 8 MiB

#: The default artifact kind for a stored context document; a ``discovery_rubric``
#: is the one type whose artifact kind must be ``rubric`` (see the on-read check in
#: :mod:`review_article_agent.discovery.context`).
_DEFAULT_CONTEXT_ARTIFACT_KIND = "report"
_RUBRIC_ARTIFACT_KIND = "rubric"

#: The expected document type of each direct reference of an interview / a plan.
_REFERENCE_EXPECTED_TYPES: dict[str, tuple[tuple[str, str], ...]] = {
    "discovery_interview": (("brief_ref", "discovery_brief"),),
    "discovery_plan": (
        ("brief_ref", "discovery_brief"),
        ("interview_ref", "discovery_interview"),
        ("framing_ref", "discovery_framing"),
        ("scope_ref", "discovery_scope"),
        ("rubric_ref", "discovery_rubric"),
    ),
}

# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_CONTEXT_INVALID_MESSAGE = (
    "the discovery-context document failed strict typed validation (a wrong or "
    "missing envelope/payload field, a wrong type, a forged approval field, an "
    "unsupported document type, a non-canonical or non-UTF-8 file, or a referenced "
    "document that is missing, stale, of the wrong type, or cross-project). "
    "Nothing was written."
)
_CONTEXT_REFERENCE_MESSAGE = (
    "a referenced discovery-context document could not be validated at its exact "
    "expected type and current revision (missing, stale, wrong-typed, cross-project, "
    "or an unresolvable seed run/index). Nothing was written."
)
_CONTEXT_PARENT_MESSAGE = (
    "the supplied parent revision is not this artifact's current head, so this is "
    "not a valid amendment; the plan impact review must reference the exact parent. "
    "Nothing was written."
)
_CONTEXT_AMENDMENT_MESSAGE = (
    "an amended plan requires a real parent revision and a non-empty impact review "
    "(a previous_plan reference, a non-empty affected list, and a rationale); an "
    "initial plan must have a null previous_plan. Nothing was written."
)
_CONTEXT_CONFLICT_MESSAGE = (
    "the artifact already exists with a different kind, or a divergent revision is "
    "already recorded; no existing data was overwritten."
)
_CONTEXT_NOT_FOUND_MESSAGE = (
    "no stored context revision matches that artifact/revision in this project; "
    "nothing was read or changed."
)
_CONTEXT_STALE_MESSAGE = (
    "the requested context revision is not the artifact's current revision; use "
    "--historical to read an exact superseded revision without claiming current "
    "authority."
)
_CONTEXT_RIGHTS_MESSAGE = (
    "the rights record does not authorize storing this exact revision (a wrong "
    "subject binding, a missing storage permission, or a malformed record). Nothing "
    "was written."
)
_CORPUS_INVALID_MESSAGE = (
    "the corpus ingest request was rejected (an unknown run id, a malformed "
    "identifier, a missing rights record, or an unacceptable snapshot argument). "
    "Nothing was written."
)
_CORPUS_NO_RUNS_MESSAGE = "at least one --run-id is required; nothing was written."
_CORPUS_CONFLICT_MESSAGE = (
    "a different corpus snapshot is already recorded for this request key; nothing "
    "was overwritten."
)
_CORPUS_CORRUPT_MESSAGE = (
    "the stored corpus snapshot could not be read safely (a corrupt or inconsistent "
    "ledger); nothing was changed."
)
_CORPUS_NOT_FOUND_MESSAGE = (
    "no corpus snapshot matches that artifact/revision in this project; nothing was "
    "read or changed."
)
_LEAD_NOT_FOUND_MESSAGE = (
    "no recorded hosted lead request matches that request id; nothing was read or "
    "sent."
)
_LEAD_NOT_PROPOSAL_MESSAGE = (
    "no completed lead proposal exists for this request; a diagnostic outcome is "
    "never displayed as a successful lead proposal."
)
_LEAD_READ_MESSAGE = (
    "the stored lead proposal could not be read safely; nothing was changed."
)
_JSON_TOO_LARGE_MESSAGE = (
    "the {what} input is larger than the permitted limit; refusing to parse it. "
    "Nothing was written."
)


class DiscoveryControlError(ReviewAgentError):
    """A discovery artifact-preparation/review command failed in a typed way.

    ``category`` is one of ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always authored in this module (a static, curated constant), so it
    never reflects a raw exception, stored content, a path, a source/query/title,
    or a caller-supplied value.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_discovery_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``discovery`` command group on the top-level parser."""
    discovery = sub.add_parser(
        "discovery",
        help=(
            "typed discovery artifact preparation/review (context import/show, "
            "corpus ingest/show, lead status/content); stores proposals only"
        ),
    )
    disc_sub = discovery.add_subparsers(
        dest="discovery_command", required=True, metavar="DISCOVERY_COMMAND"
    )

    # -- discovery context -------------------------------------------------- #
    context = disc_sub.add_parser(
        "context", help="typed discovery-context documents (import/show/validate)"
    )
    context_sub = context.add_subparsers(
        dest="discovery_context_command", required=True, metavar="CONTEXT_COMMAND"
    )

    p_import = context_sub.add_parser(
        "import",
        help=(
            "validate and store one typed context envelope as one immutable "
            "proposal revision; records no approval"
        ),
    )
    p_import.add_argument("--artifact-id", required=True, metavar="ID")
    p_import.add_argument("--revision-id", required=True, metavar="ID")
    p_import.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="strict typed context envelope JSON file",
    )
    p_import.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for this exact revision",
    )
    p_import.add_argument("--request-id", metavar="ID")
    p_import.add_argument("--parent-revision-id", metavar="ID")
    p_import.add_argument("--project", metavar="ID")

    for name, help_text in (
        ("show", "read one stored context revision (safe summary)"),
        ("validate", "alias of 'show': read and validate one stored context revision"),
    ):
        p_show = context_sub.add_parser(name, help=help_text)
        p_show.add_argument("--artifact-id", required=True, metavar="ID")
        p_show.add_argument("--revision-id", metavar="ID")
        p_show.add_argument(
            "--content",
            action="store_true",
            help="explicitly return the private validated document JSON",
        )
        p_show.add_argument(
            "--historical",
            action="store_true",
            help="read the exact revision even when superseded (no current authority)",
        )
        p_show.add_argument("--project", metavar="ID")

    # -- discovery corpus --------------------------------------------------- #
    corpus = disc_sub.add_parser(
        "corpus", help="cumulative publication-candidate snapshots (ingest/show)"
    )
    corpus_sub = corpus.add_subparsers(
        dest="discovery_corpus_command", required=True, metavar="CORPUS_COMMAND"
    )

    p_ingest = corpus_sub.add_parser(
        "ingest",
        help="record one cumulative corpus snapshot from saved observations",
    )
    p_ingest.add_argument(
        "--run-id",
        action="append",
        default=[],
        dest="run_ids",
        metavar="ID",
        help="a recorded discovery run id to add (repeatable)",
    )
    p_ingest.add_argument(
        "--artifact-id", default=DEFAULT_CORPUS_ARTIFACT_ID, metavar="ID"
    )
    p_ingest.add_argument("--revision-id", required=True, metavar="ID")
    p_ingest.add_argument("--request-id", required=True, metavar="ID")
    p_ingest.add_argument("--rights-file", required=True, metavar="PATH")
    p_ingest.add_argument("--project", metavar="ID")

    p_corpus_show = corpus_sub.add_parser(
        "show", help="read one corpus snapshot (safe summary; --content to opt in)"
    )
    p_corpus_show.add_argument(
        "--artifact-id", default=DEFAULT_CORPUS_ARTIFACT_ID, metavar="ID"
    )
    p_corpus_show.add_argument("--revision-id", metavar="ID")
    p_corpus_show.add_argument("--content", action="store_true")
    p_corpus_show.add_argument("--project", metavar="ID")

    # -- discovery lead ----------------------------------------------------- #
    lead = disc_sub.add_parser(
        "lead", help="read-only hosted lead request readback (status/content)"
    )
    lead_sub = lead.add_subparsers(
        dest="discovery_lead_command", required=True, metavar="LEAD_COMMAND"
    )

    p_status = lead_sub.add_parser(
        "status", help="safe readback of one recorded hosted lead request"
    )
    p_status.add_argument("--request-id", required=True, metavar="ID")
    p_status.add_argument("--project", metavar="ID")

    p_lead_content = lead_sub.add_parser(
        "content",
        help=(
            "explicit PRIVATE NON-AUTHORIZING view of the stored lead proposal "
            "text/metadata; issues no call"
        ),
    )
    p_lead_content.add_argument("--request-id", required=True, metavar="ID")
    p_lead_content.add_argument("--project", metavar="ID")

    # -- addon groups (search/expand jobs; lead configure/run) -------------- #
    # Imported lazily so this module has no top-level cycle with the addon, which
    # imports this module's own curated helpers. The addon extends the existing
    # ``lead`` group (never a duplicate) and is the later CLI slices' extension
    # point.
    from . import discovery_jobs as _job_commands

    _job_commands.build_discovery_jobs_parser(disc_sub, lead_sub)

    # -- addon groups (coverage reports; freeze propose/status/show/...) ----- #
    # A separate sibling addon owns the reporting/freeze surface; it too is
    # imported lazily (same reason) and adds only new ``report``/``freeze`` groups.
    from . import discovery_reports as _report_commands

    _report_commands.build_discovery_reports_parser(disc_sub)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _resolve_project_id(ctx: Any, explicit: str | None) -> str:
    """Return the explicit project ID or the active project's ID."""
    if explicit is not None:
        return explicit
    return ctx.registry.active_project().project_id


def _open_verified_store(ctx: Any, project_id: str) -> ProjectStore:
    """Resolve and *verify* a project's store before touching its data."""
    info = ctx.registry.get(project_id)
    return ProjectStore.open_at(
        paths.app_dir_for(info.root_path), expected_project_id=info.project_id
    )


def _read_strict_json_file(path: str, *, what: str) -> object:
    """Read one bounded, strict UTF-8 JSON file; never echo a value or a path."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_DISCOVERY_INPUT_BYTES + 1)
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, f"the {what} input could not be read ({reason})."
        ) from exc
    if len(raw) > MAX_DISCOVERY_INPUT_BYTES:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _JSON_TOO_LARGE_MESSAGE.format(what=what)
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, f"the {what} input is not valid UTF-8 text."
        ) from exc
    try:
        return load_strict_json(text)
    except StrictJsonError as exc:
        reason = STRICT_JSON_REASONS.get(exc.code, "is not usable")
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, f"the {what} input {reason}."
        ) from exc


def _validate_storage_rights(
    project_id: str, artifact_id: str, revision_id: str, rights: object
) -> None:
    """Fail closed on the exact caller rights record before any store write.

    The record must be a raw canonical ``RightsRecord`` whose ``source_revision``
    subject binds this exact artifact/revision and whose ``storage`` operation is
    allowed. No rights record is fabricated and none is defaulted.
    """
    if not isinstance(rights, dict) or "operations" not in rights:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _CONTEXT_RIGHTS_MESSAGE)
    subject = {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    try:
        record = validate_rights_record(rights, project_id, subject=subject)
        require_storage_allowed(record)
    except (RecordValidationError, ValueError, TypeError) as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _CONTEXT_RIGHTS_MESSAGE
        ) from exc


def _context_artifact_kind(document: dict) -> str:
    if document["document_type"] == "discovery_rubric":
        return _RUBRIC_ARTIFACT_KIND
    return _DEFAULT_CONTEXT_ARTIFACT_KIND


def _dependencies_for(document: dict) -> list[dict[str, str]]:
    """The exact generic dependencies derived from a typed context payload."""
    document_type = document["document_type"]
    payload = document["payload"]
    refs: list[dict] = []
    if document_type == "discovery_interview":
        refs.append(payload["brief_ref"])
    elif document_type == "discovery_plan":
        for field in (
            "brief_ref",
            "interview_ref",
            "framing_ref",
            "scope_ref",
            "rubric_ref",
        ):
            refs.append(payload[field])
        refs.extend(payload["seed_refs"])

    seen: set[tuple[str, str]] = set()
    dependencies: list[dict[str, str]] = []
    for ref in refs:
        key = (ref["artifact_id"], ref["revision_id"])
        if key in seen:
            continue
        seen.add(key)
        dependencies.append(
            {
                "project_id": ref["project_id"],
                "artifact_id": ref["artifact_id"],
                "revision_id": ref["revision_id"],
            }
        )
    return dependencies


def _validate_referenced_documents(store: Any, document: dict) -> None:
    """Validate every referenced document at its expected type/current revision."""
    document_type = document["document_type"]
    payload = document["payload"]
    for field, expected_type in _REFERENCE_EXPECTED_TYPES.get(document_type, ()):
        _read_reference(store, payload[field], expected_type)
    if document_type == "discovery_plan":
        for ref in payload["seed_refs"]:
            _read_reference(store, ref, "discovery_seed")
    elif document_type == "discovery_seed":
        _validate_seed_run_index(store, payload)


def _read_reference(store: Any, ref: object, expected_type: str) -> None:
    try:
        read_context_document(
            store, ref, expected_type=expected_type, require_current=True
        )
    except DiscoveryContextError as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _CONTEXT_REFERENCE_MESSAGE
        ) from exc


def _validate_seed_run_index(store: Any, payload: dict) -> None:
    """Fail closed unless a seed's recorded run/index resolve locally."""
    try:
        search = DiscoveryRepository(store).get_search(payload["run_id"])
    except DiscoverySearchError as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _CONTEXT_REFERENCE_MESSAGE
        ) from exc
    if search is None or payload["record_index"] >= len(search.result.records):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _CONTEXT_REFERENCE_MESSAGE)


def _validate_parent(
    repo: ArtifactRepository,
    artifact_id: str,
    parent_revision_id: str | None,
    document: dict,
) -> None:
    """Validate the amendment parent (real head) and the plan impact review shape."""
    is_plan = document["document_type"] == "discovery_plan"
    previous_plan = (
        document["payload"]["impact_review"]["previous_plan"] if is_plan else None
    )
    if parent_revision_id is None:
        if is_plan and previous_plan is not None:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _CONTEXT_AMENDMENT_MESSAGE
            )
        return
    try:
        current = repo.current_revision(artifact_id)
    except (RecordValidationError, ValueError) as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _CONTEXT_PARENT_MESSAGE
        ) from exc
    if current is None or current.revision_id != parent_revision_id:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _CONTEXT_PARENT_MESSAGE)
    if is_plan:
        expected = {
            "project_id": repo.project_id,
            "artifact_id": artifact_id,
            "revision_id": parent_revision_id,
        }
        if previous_plan != expected:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _CONTEXT_AMENDMENT_MESSAGE
            )


def _approval_request(
    store: Any, repo: Any, revision: Any, document: dict
) -> dict[str, object] | None:
    """The named-gate approval request for a seed/plan revision, or ``None``.

    Only a seed (its individual ``artifact_revision`` gate) and a plan (the
    combined ``protocol``/``discovery.plan`` gate) require an approval request. The
    ``satisfied`` flag is the real repository's answer **for this exact returned
    revision** and is never a fabricated claim: ``named_gate_satisfied`` reports the
    artifact's *current* gate, so a historical replay of an older revision is never
    reported satisfied merely because a newer revision at the same gate was
    approved. ``current``/``stale`` state whether the returned revision is still the
    artifact's head, so a historical replay never claims the old gate is active.
    """
    document_type = document["document_type"]
    if document_type == "discovery_seed":
        gate_kind = ARTIFACT_REVISION_GATE_KIND
        gate_id = SEED_GATE_PREFIX + revision.artifact_id
        impact_review: object = None
    elif document_type == "discovery_plan":
        gate_kind = PROTOCOL_GATE_KIND
        gate_id = PLAN_GATE_ID
        impact_review = document["payload"]["impact_review"]
    else:
        return None
    head = repo.current_revision(revision.artifact_id)
    is_current = head is not None and head.revision_id == revision.revision_id
    # ``named_gate_satisfied`` answers for the artifact's current gate; it may only
    # mean *this exact returned revision* when that revision is still the head. For
    # a historical (stale) replay it must never be reported satisfied, so an
    # approval of a newer revision at the same gate is not mis-attributed here.
    satisfied = is_current and HumanApprovalRepository(store).named_gate_satisfied(
        revision.artifact_id, gate_kind=gate_kind, gate_id=gate_id
    )
    return {
        "gate_kind": gate_kind,
        "gate_id": gate_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "impact_review": impact_review,
        "satisfied": bool(satisfied),
        "current": bool(is_current),
        "stale": not is_current,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def _imported_revision_dict(revision: Any, document_type: str) -> dict[str, object]:
    """Return ref/digest/type for one freshly stored context revision."""
    return {
        "project_id": revision.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "parent_revision_id": revision.parent_revision_id,
        "content_sha256": revision.content_sha256,
        "byte_size": revision.byte_size,
        "document_type": document_type,
        "dependencies": [dep.as_dict() for dep in revision.dependencies],
        "scientific_state": "proposal",
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def _context_gate(document_type: str, artifact_id: str) -> dict[str, str] | None:
    if document_type == "discovery_seed":
        return {
            "gate_kind": ARTIFACT_REVISION_GATE_KIND,
            "gate_id": SEED_GATE_PREFIX + artifact_id,
        }
    if document_type == "discovery_plan":
        return {"gate_kind": PROTOCOL_GATE_KIND, "gate_id": PLAN_GATE_ID}
    return None


def _context_summary(
    store: Any, ref: dict[str, str], document: dict, *, historical: bool
) -> dict[str, object]:
    """A safe, non-authorizing summary (never the raw abstract)."""
    document_type = document["document_type"]
    payload = document["payload"]
    summary: dict[str, object] = {
        "project_id": ref["project_id"],
        "artifact_id": ref["artifact_id"],
        "revision_id": ref["revision_id"],
        "document_type": document_type,
        "schema_version": document["schema_version"],
        "provisional": (
            payload.get("abstract_context") == "provisional"
            if document_type == "discovery_seed"
            else None
        ),
        "historical": bool(historical),
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }
    gate = _context_gate(document_type, ref["artifact_id"])
    if gate is None:
        summary["gate"] = None
        summary["gate_satisfied"] = False
        summary["needs_approval"] = False
    else:
        satisfied = HumanApprovalRepository(store).named_gate_satisfied(
            ref["artifact_id"], gate_kind=gate["gate_kind"], gate_id=gate["gate_id"]
        )
        summary["gate"] = gate
        summary["gate_satisfied"] = bool(satisfied)
        summary["needs_approval"] = not satisfied
    return summary


def _candidate_control_error(exc: CandidateError) -> DiscoveryControlError:
    if isinstance(exc, CandidateConflictError):
        return DiscoveryControlError(_CATEGORY_CONFLICT, _CORPUS_CONFLICT_MESSAGE)
    if isinstance(exc, CandidateValidationError):
        return DiscoveryControlError(_CATEGORY_VALIDATION, _CORPUS_INVALID_MESSAGE)
    return DiscoveryControlError(_CATEGORY_STORAGE, _CORPUS_CORRUPT_MESSAGE)


def _corpus_summary(snapshot: dict) -> dict[str, object]:
    """A safe corpus summary: counts and flags, never a title or token.

    ``run_count`` is the **cumulative** number of runs the snapshot names; the
    latest request's newly supplied set is reported separately as
    ``input_run_count``. Metadata stays local only and is never scientific
    context.
    """
    candidates = snapshot["candidates"]
    unresolved_count = sum(1 for c in candidates if not c["resolved"])
    return {
        "project_id": snapshot["project_id"],
        "artifact_id": snapshot["artifact_id"],
        "revision_id": snapshot["revision_id"],
        "content_sha256": snapshot["content_sha256"],
        "byte_size": snapshot["byte_size"],
        "document_type": snapshot["document_type"],
        "schema_version": snapshot["schema_version"],
        "payload_version": snapshot["payload_version"],
        "request_id": snapshot["request_id"],
        "run_count": len(snapshot["runs"]),
        "input_run_count": len(snapshot["input_run_ids"]),
        "occurrence_count": len(snapshot["occurrences"]),
        "candidate_count": len(candidates),
        "conflict_count": len(snapshot["conflicts"]),
        "unresolved_count": unresolved_count,
        "unresolved": unresolved_count > 0,
        "authorizing": False,
        "completeness_claimed": False,
        "scientific_validation": False,
        "notice": snapshot["notice"],
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def discovery_context_import(
    args: argparse.Namespace, ctx: Any
) -> dict[str, object]:
    """``discovery context import`` -- store one typed context revision (proposal).

    Steps: (1) strictly parse and statically validate the envelope, (2) strictly
    parse the caller's explicit rights record, (3) derive the exact generic
    dependencies, (4) **replay first**: if this exact artifact/revision is already
    recorded, the trusted :meth:`ArtifactRepository.import_revision` replay path
    runs before storage's head-freshness check, so an identical retry returns the
    historical revision (and a divergent reuse of the request key conflicts)
    without being re-treated as a stale amendment, and (5) for a genuinely new
    revision only, validate the parent and every referenced document (read-only)
    and the rights gate, then create the artifact and import one immutable
    revision. Nothing is written before every applicable validation passes.
    """
    document = _read_strict_json_file(args.file, what="context document")
    try:
        validated = validate_context_document(document)
    except DiscoveryContextError as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _CONTEXT_INVALID_MESSAGE
        ) from exc
    rights = _read_strict_json_file(args.rights_file, what="rights record")
    dependencies = _dependencies_for(validated)
    artifact_kind = _context_artifact_kind(validated)

    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        repo = ArtifactRepository(store)
        # Replay detection: storage's request-key (step 1) and explicit-id / dedup
        # (step 2) replay both run *before* its head-freshness check (step 3). So
        # when a revision for this exact id is already recorded, we must reach
        # storage with the exact original parameters *before* our amendment /
        # reference pre-checks — otherwise an identical retry (whose parent is no
        # longer the head, or whose referenced input moved on) would be wrongly
        # refused, or a duplicate would be written. Only a revision that is not
        # already recorded can be a genuinely new write, for which the rights gate
        # and the read-only pre-checks still apply.
        try:
            replayed = (
                repo.get_revision(args.artifact_id, args.revision_id) is not None
            )
        except RecordValidationError as exc:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _CONTEXT_INVALID_MESSAGE
            ) from exc
        if not replayed:
            _validate_storage_rights(
                project_id, args.artifact_id, args.revision_id, rights
            )
            _validate_parent(repo, args.artifact_id, args.parent_revision_id, validated)
            _validate_referenced_documents(store, validated)
        try:
            if not replayed:
                repo.create_artifact(args.artifact_id, kind=artifact_kind)
            revision = repo.import_revision(
                artifact_id=args.artifact_id,
                content=validated,
                origin_locator="discovery-context:" + validated["document_type"],
                rights=rights,
                revision_id=args.revision_id,
                parent_revision_id=args.parent_revision_id,
                dependencies=dependencies,
                request_id=args.request_id,
            )
        except ArtifactConflictError as exc:
            raise DiscoveryControlError(
                _CATEGORY_CONFLICT, _CONTEXT_CONFLICT_MESSAGE
            ) from exc
        except RecordValidationError as exc:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _CONTEXT_RIGHTS_MESSAGE
            ) from exc
        approval_request = _approval_request(store, repo, revision, validated)
        # A request-key replay may resolve to a different recorded revision id;
        # report a replay truthfully.
        replayed = replayed or revision.revision_id != args.revision_id
    finally:
        store.close()
    return {
        "revision": _imported_revision_dict(revision, validated["document_type"]),
        "approval_request": approval_request,
        "replayed": replayed,
    }


def discovery_context_show(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery context show`` / ``validate`` -- read one stored context revision.

    The default output is a safe summary; ``--content`` explicitly returns the
    private validated document JSON. ``--historical`` reads an exact superseded
    revision without claiming current authority.
    """
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        repo = ArtifactRepository(store)
        artifact = repo.get_artifact(args.artifact_id)
        if artifact is None:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _CONTEXT_NOT_FOUND_MESSAGE
            )
        if args.revision_id is not None:
            revision_id = args.revision_id
        else:
            current = repo.current_revision(args.artifact_id)
            if current is None:
                raise DiscoveryControlError(
                    _CATEGORY_VALIDATION, _CONTEXT_NOT_FOUND_MESSAGE
                )
            revision_id = current.revision_id
        require_current = not args.historical
        if require_current:
            head = repo.current_revision(args.artifact_id)
            if head is None or head.revision_id != revision_id:
                raise DiscoveryControlError(
                    _CATEGORY_CONFLICT, _CONTEXT_STALE_MESSAGE
                )
        ref = {
            "project_id": project_id,
            "artifact_id": args.artifact_id,
            "revision_id": revision_id,
        }
        try:
            document = read_context_document(
                store, ref, require_current=require_current
            )
        except DiscoveryContextError as exc:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _CONTEXT_INVALID_MESSAGE
            ) from exc
        if args.content:
            return {
                "document": document,
                "private": True,
                "historical": not require_current,
                "authorizes_execution": False,
                "authorizes_export": False,
                "scientific_validation": False,
            }
        return {
            "context": _context_summary(
                store, ref, document, historical=not require_current
            )
        }
    finally:
        store.close()


def discovery_corpus_ingest(
    args: argparse.Namespace, ctx: Any
) -> dict[str, object]:
    """``discovery corpus ingest`` -- record one cumulative corpus snapshot."""
    run_ids = list(args.run_ids or [])
    if not run_ids:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _CORPUS_NO_RUNS_MESSAGE)
    rights = _read_strict_json_file(args.rights_file, what="rights record")
    project_id = _resolve_project_id(ctx, args.project)
    _validate_storage_rights(project_id, args.artifact_id, args.revision_id, rights)
    store = _open_verified_store(ctx, project_id)
    try:
        repo = CandidateRepository(store)
        try:
            revision = repo.ingest(
                run_ids,
                request_id=args.request_id,
                rights=rights,
                revision_id=args.revision_id,
                artifact_id=args.artifact_id,
            )
            snapshot = repo.get(
                {
                    "project_id": project_id,
                    "artifact_id": revision.artifact_id,
                    "revision_id": revision.revision_id,
                }
            )
        except CandidateError as exc:
            raise _candidate_control_error(exc) from exc
    finally:
        store.close()
    return {"corpus": _corpus_summary(snapshot)}


def discovery_corpus_show(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery corpus show`` -- read one corpus snapshot (summary; --content)."""
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        repo = CandidateRepository(store)
        try:
            if args.revision_id is not None:
                snapshot = repo.get(
                    {
                        "project_id": project_id,
                        "artifact_id": args.artifact_id,
                        "revision_id": args.revision_id,
                    }
                )
            else:
                snapshot = repo.current(args.artifact_id)
        except CandidateError as exc:
            raise _candidate_control_error(exc) from exc
    finally:
        store.close()
    if snapshot is None:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _CORPUS_NOT_FOUND_MESSAGE)
    if args.content:
        return {
            "document": snapshot,
            "private": True,
            "authorizes_execution": False,
            "authorizes_export": False,
            "scientific_validation": False,
        }
    return {"corpus": _corpus_summary(snapshot)}


def _lead_outcome(store: Any, request_id: str) -> dict:
    try:
        outcome = get_lead_outcome(store, request_id)
    except (HostedLeadError, RecordValidationError) as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _LEAD_NOT_FOUND_MESSAGE
        ) from exc
    if outcome is None:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _LEAD_NOT_FOUND_MESSAGE)
    return outcome


def discovery_lead_status(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery lead status`` -- safe readback of one hosted lead request."""
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        outcome = _lead_outcome(store, args.request_id)
    finally:
        store.close()
    return {
        "request_id": args.request_id,
        "lead_status": outcome,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def discovery_lead_content(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery lead content`` -- private, non-authorizing proposal text/metadata.

    Reads the exact stored runtime proposal the runner committed (no call is
    issued). Only a ``completed`` outcome carries a proposal; every other outcome
    is a diagnostic machine record and is refused rather than displayed as a
    successful proposal.
    """
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        outcome = _lead_outcome(store, args.request_id)
        if (
            outcome.get("status") != "completed"
            or outcome.get("proposal_record_id") is None
        ):
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _LEAD_NOT_PROPOSAL_MESSAGE
            )
        try:
            record = RuntimeRepository(store).get_proposal(
                outcome["proposal_record_id"]
            )
        except (RecordValidationError, ValueError) as exc:
            raise DiscoveryControlError(_CATEGORY_STORAGE, _LEAD_READ_MESSAGE) from exc
        if record is None:
            raise DiscoveryControlError(_CATEGORY_STORAGE, _LEAD_READ_MESSAGE)
        try:
            payload = json.loads(record.canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise DiscoveryControlError(_CATEGORY_STORAGE, _LEAD_READ_MESSAGE) from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, dict):
            raise DiscoveryControlError(_CATEGORY_STORAGE, _LEAD_READ_MESSAGE)
        proposal = record.to_mapping()
        execution_metadata = content.get("execution_metadata")
    finally:
        store.close()
    return {
        "request_id": args.request_id,
        "private": True,
        "non_authorizing": True,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
        "proposal_text": content.get("proposal_text"),
        "proposal": proposal,
        "execution_metadata": execution_metadata,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
#: The nested leaf-command map for the ``discovery`` group: group -> command ->
#: handler. Later CLI slices extend this same map on the integrated base.
DISCOVERY_COMMANDS = {
    "context": {
        "import": discovery_context_import,
        "show": discovery_context_show,
        "validate": discovery_context_show,
    },
    "corpus": {
        "ingest": discovery_corpus_ingest,
        "show": discovery_corpus_show,
    },
    "lead": {
        "status": discovery_lead_status,
        "content": discovery_lead_content,
    },
}

#: The nested argparse ``dest`` names, one per group, holding the leaf command.
_DISCOVERY_LEAF_DESTS = {
    "context": "discovery_context_command",
    "corpus": "discovery_corpus_command",
    "lead": "discovery_lead_command",
}


def dispatch_discovery(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``discovery`` invocation to its handler.

    Returns ``None`` only if the group/leaf command is unknown (which
    ``argparse`` already prevents); the caller turns that into a usage error.
    """
    group = DISCOVERY_COMMANDS.get(args.discovery_command)
    if group is not None:
        leaf = getattr(args, _DISCOVERY_LEAF_DESTS[args.discovery_command], None)
        handler = group.get(leaf)
        if handler is not None:
            return handler(args, ctx)
    # The addon (search/expand jobs, lead configure/run) is consulted only when
    # this module's own map does not already own the leaf command, so a later CLI
    # slice can extend the group without changing its lifecycle readbacks here.
    from . import discovery_jobs as _job_commands

    addon = _job_commands.dispatch_discovery_jobs(args)
    if addon is None:
        # The reporting/freeze addon is consulted last, for the same reason.
        from . import discovery_reports as _report_commands

        addon = _report_commands.dispatch_discovery_reports(args)
    if addon is None:
        return None
    return addon(args, ctx)
