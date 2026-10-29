"""Personal CLI for discovery **reporting** and **freeze** readbacks (Step 05, CLI1B).

This module adds the reporting/freeze surface over the already-integrated,
already-validated value layers:

* :mod:`review_article_agent.discovery.reporting` -- the deterministic
  coverage-report builder and the immutable
  :class:`~review_article_agent.discovery.reporting.CoverageRepository`.
* :mod:`review_article_agent.discovery.freeze` -- the inert freeze/search-again
  proposal repository (:class:`~review_article_agent.discovery.freeze.FreezeRepository`)
  and its read-only status/history/require helpers.

It extends the existing root ``discovery`` command group cleanly through an
**addon** builder + command map (the CLI1A pattern), so the job addon
(:mod:`review_article_agent.cli.discovery_jobs`) and this module stay independent
and the parent group's own lifecycle readbacks are unchanged.

What this module **is** (the personal-CLI baseline -- **no new authority is
introduced**):

* ``discovery report create`` -- build and record **one** immutable coverage-report
  revision (:meth:`CoverageRepository.record_report`) from an exact corpus and
  plan revision, an optional set of **real completed** citation-expansion jobs
  (each resolved through
  :func:`~review_article_agent.worker.discovery.persisted_expansion_report`, never
  a caller flag), an optional strict known-relevant token list, and an optional
  strict limitations list. Every logical input is validated **before** the write,
  an identical replay is historical, and a divergent reuse of the request key
  conflicts. The returned document is a **safe summary** (exact ref/digest,
  counts, explicit gap flags, cumulative work and known/unknown cost) -- never the
  raw queries, identifiers, sources, or proposals -- and makes no
  completeness/eligibility claim.
* ``discovery report show`` -- read one exact report revision or the current
  coverage artifact; a safe summary by default and the **private** full validated
  document only with ``--content``.
* ``discovery freeze propose`` -- record (or replay) **one inert** freeze /
  search-again proposal (:meth:`FreezeRepository.propose`). It **never** confirms,
  never mints authority, accepts no ``--yes``/``--approve``/actor flag, and returns
  the exact named-gate ``approval_request`` (fixed
  ``artifact_revision``/``discovery.freeze`` gate) plus the out-of-band
  ``gate prepare``/``gate confirm`` guidance. An old frozen artifact that refuses
  a new ingest yields static guidance to choose a **new** corpus + coverage id.
* ``discovery freeze status`` / ``freeze show`` / ``freeze history`` /
  ``freeze require`` -- read-only readbacks. ``status`` distinguishes an approved
  ``search_again`` from an active freeze and from an unapproved new proposal
  without ever auto-selecting a new corpus; ``require`` reads
  :meth:`FreezeRepository.require_frozen` and fails cleanly when no authentic,
  currently-active freeze authorizes the selection.

What this module **is not**:

* **No approval, no network, no provider, no model, no worker, and no ensure.**
  No command here ensures a worker, issues an IPC/network call, or records a human
  approval; the existing controlling-terminal ``gate prepare``/``gate confirm``
  remains the only approval path.
* **No scientific validation and no completeness claim.** ``--content`` output is
  private, non-authorizing data.

Design rules:

* **Verified project access.** Every project is opened through the registry with
  ``expected_project_id`` before any store read/write.
* **Strict, bounded input.** Every JSON file is read with the parent group's hard
  byte cap and strict parser; no input value or path is reflected.
* **Reused control helpers.** The parent group's verified-store, strict-file,
  project-resolution, and rights helpers are reused verbatim.
* **Typed, curated errors.** Failures are the parent group's
  :class:`~review_article_agent.cli.discovery.DiscoveryControlError` with a
  static, value-free message; coverage/freeze validation, conflict, and
  corruption are mapped distinctly (2/3/4), and no error string from a source
  file, query, provenance, or locator is ever echoed.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..discovery.freeze import (
    DEFAULT_FREEZE_ARTIFACT_ID,
    FREEZE_GATE_ID,
    FREEZE_GATE_KIND,
    FreezeConflictError,
    FreezeCorruptionError,
    FreezeError,
    FreezeNotAuthorizedError,
    FreezeRepository,
    FreezeValidationError,
)
from ..discovery.reporting import (
    DEFAULT_COVERAGE_ARTIFACT_ID,
    CoverageConflictError,
    CoverageCorruptionError,
    CoverageError,
    CoverageRepository,
    CoverageValidationError,
)
from ..domain.records import (
    RecordValidationError,
    validate_identifier,
)
from ..storage import ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository
from ..worker import discovery as worker_discovery
from .discovery import (
    DiscoveryControlError,
    _open_verified_store,
    _read_strict_json_file,
    _resolve_project_id as _resolve_store_project_id,
    _validate_storage_rights,
)

__all__ = [
    "DISCOVERY_REPORT_COMMANDS",
    "DISCOVERY_REPORT_LEAF_DESTS",
    "build_discovery_reports_parser",
    "dispatch_discovery_reports",
    "discovery_freeze_history",
    "discovery_freeze_propose",
    "discovery_freeze_require",
    "discovery_freeze_show",
    "discovery_freeze_status",
    "discovery_report_create",
    "discovery_report_show",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: The one freeze decision enum (mirrors the library).
_FREEZE_DECISIONS = ("freeze", "search_again")

#: Fixed origin locator prefix for a coverage report (mirrors the library).
_REPORT_ORIGIN_PREFIX = "coverage:"


# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_REF_MESSAGE = (
    "invalid artifact or revision identifier for a corpus/plan/coverage "
    "reference; it must be a bounded opaque id. The supplied value is not shown. "
    "Run --help for usage."
)
_JOB_ID_MESSAGE = (
    "invalid value for --expansion-job-id; it must be a bounded opaque job id. "
    "The supplied value is not shown. Run --help for usage."
)
_DECISION_MESSAGE = (
    "invalid value for --decision; choose exactly one of freeze, search_again. "
    "The supplied value is not shown. Run --help for usage."
)
_KNOWN_RELEVANT_MESSAGE = (
    "the known-relevant file must be a strict JSON list of {\"namespace\", "
    "\"value\"} token objects. Nothing was written."
)
_LIMITATIONS_MESSAGE = (
    "the limitations file must be a strict JSON list of non-empty strings. "
    "Nothing was written."
)
_ENTRY_REVIEWS_MESSAGE = (
    "the entry-reviews file must be a strict JSON list matching the freeze "
    "entry-review schema (one review per pinned candidate: candidate_id, "
    "checked_at, provenance, the four integrity statuses, a disposition, and a "
    "non-empty rationale). Nothing was written."
)
_NO_EXPANSION_MESSAGE = (
    "no completed citation-expansion job matches that --expansion-job-id in this "
    "project; only a real typed completed expansion report may be pinned. Nothing "
    "was written."
)
_REPORT_SOURCE_MESSAGE = (
    "a supplied coverage-report input was rejected: the corpus/plan reference is "
    "not this project's exact revision, a referenced document is missing or "
    "stale, or the file content does not match its expected shape. Nothing was "
    "written."
)
_REPORT_CONFLICT_MESSAGE = (
    "a different coverage report is already recorded for this request key; no "
    "existing data was overwritten."
)
_REPORT_CORRUPT_MESSAGE = (
    "a stored coverage report, or one of its pinned inputs, could not be read "
    "safely (a corrupt or inconsistent ledger); nothing was changed."
)
_REPORT_NOT_FOUND_MESSAGE = (
    "no coverage report revision exists for the supplied reference in this "
    "project; nothing was read or changed."
)
_FREEZE_SOURCE_MESSAGE = (
    "the freeze proposal was rejected: the corpus/coverage reference is not this "
    "project's exact revision, the coverage report does not pin that corpus, the "
    "pinned plan is not a currently approved discovery plan, the entry reviews are "
    "not a valid per-candidate review set, or a freeze over explicit coverage gaps "
    "needs an explicit --accept-coverage-limitations explanation. Nothing was "
    "written."
)
_FREEZE_CONFLICT_MESSAGE = (
    "a different freeze decision is already recorded for this request key; no "
    "existing data was overwritten."
)
_FREEZE_CORRUPT_MESSAGE = (
    "a stored freeze decision, or one of its pinned inputs, could not be read "
    "safely (a corrupt or inconsistent ledger); nothing was changed."
)
_FREEZE_NOT_FOUND_MESSAGE = (
    "no freeze decision revision exists for the supplied reference in this "
    "project; nothing was read or changed."
)
_FREEZE_NOT_ACTIVE_MESSAGE = (
    "no currently active authentic human-approved freeze authorizes this "
    "selection; the exact named gate artifact_revision/discovery.freeze must "
    "approve the exact decision revision out of band on the controlling terminal. "
    "The corpus is not frozen."
)
_SNAPSHOT_NOTE = (
    "An owner records the exact named gate out of band on the controlling terminal "
    "with `gate prepare`/`gate confirm` for this exact artifact/revision and gate "
    "(the decision binds the exact revision, its content digest, and its pinned "
    "corpus/coverage/plan revisions). This CLI command records no approval and "
    "introduces no parallel approval path."
)
_INGEST_GUIDANCE = (
    "If a cumulative ingest into an old corpus artifact is refused because that "
    "artifact already carries an authentic freeze, do not modify or reopen it: "
    "choose a NEW corpus artifact id and record a NEW coverage report (and, if "
    "needed, a new freeze decision) so the previously frozen roster stays exactly "
    "as approved."
)


# --------------------------------------------------------------------------- #
# argument tree (addon)
# --------------------------------------------------------------------------- #
def build_discovery_reports_parser(disc_sub: "argparse._SubParsersAction") -> None:
    """Extend the root ``discovery`` parser with the report + freeze groups."""
    # -- report ------------------------------------------------------------- #
    report = disc_sub.add_parser(
        "report",
        help=(
            "deterministic coverage reports (create/show); records a proposal "
            "report only, never a freeze"
        ),
    )
    report_sub = report.add_subparsers(
        dest="discovery_report_command", required=True, metavar="REPORT_COMMAND"
    )

    p_create = report_sub.add_parser(
        "create",
        help=(
            "build and record one immutable coverage-report revision from exact "
            "corpus/plan refs; no worker, no network"
        ),
    )
    p_create.add_argument("--corpus-artifact-id", required=True, metavar="ID")
    p_create.add_argument("--corpus-revision-id", required=True, metavar="ID")
    p_create.add_argument("--plan-artifact-id", required=True, metavar="ID")
    p_create.add_argument("--plan-revision-id", required=True, metavar="ID")
    p_create.add_argument(
        "--artifact-id", default=DEFAULT_COVERAGE_ARTIFACT_ID, metavar="ID"
    )
    p_create.add_argument("--revision-id", required=True, metavar="ID")
    p_create.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for this exact revision",
    )
    p_create.add_argument("--request-id", required=True, metavar="ID")
    p_create.add_argument(
        "--expansion-job-id",
        action="append",
        default=[],
        dest="expansion_job_ids",
        metavar="ID",
        help=(
            "a completed citation-expansion job id to pin (repeatable); resolved "
            "through the persisted, validated expansion report only"
        ),
    )
    p_create.add_argument(
        "--known-relevant-file",
        metavar="PATH",
        help="strict JSON list of {namespace, value} known-relevant tokens",
    )
    p_create.add_argument(
        "--limitations-file",
        metavar="PATH",
        help="strict JSON list of non-empty limitation strings",
    )
    p_create.add_argument("--project", metavar="ID")

    p_report_show = report_sub.add_parser(
        "show",
        help="read one coverage report (safe summary; --content to opt in)",
    )
    p_report_show.add_argument(
        "--artifact-id", default=DEFAULT_COVERAGE_ARTIFACT_ID, metavar="ID"
    )
    p_report_show.add_argument("--revision-id", metavar="ID")
    p_report_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly return the private, non-authorizing validated report JSON",
    )
    p_report_show.add_argument("--project", metavar="ID")

    # -- freeze ------------------------------------------------------------- #
    freeze = disc_sub.add_parser(
        "freeze",
        help=(
            "exact corpus freeze / search-again decisions (propose/status/show/"
            "history/require); records an inert proposal only"
        ),
    )
    freeze_sub = freeze.add_subparsers(
        dest="discovery_freeze_command", required=True, metavar="FREEZE_COMMAND"
    )

    p_propose = freeze_sub.add_parser(
        "propose",
        help=(
            "record (or replay) one inert freeze/search-again proposal; records no "
            "approval and confirms nothing"
        ),
    )
    p_propose.add_argument("--corpus-artifact-id", required=True, metavar="ID")
    p_propose.add_argument("--corpus-revision-id", required=True, metavar="ID")
    p_propose.add_argument("--coverage-artifact-id", required=True, metavar="ID")
    p_propose.add_argument("--coverage-revision-id", required=True, metavar="ID")
    p_propose.add_argument(
        "--decision", required=True, choices=list(_FREEZE_DECISIONS), metavar="DECISION"
    )
    p_propose.add_argument(
        "--entry-reviews-file",
        required=True,
        metavar="PATH",
        help="strict JSON list of per-candidate entry reviews",
    )
    p_propose.add_argument("--rationale", required=True, metavar="TEXT")
    p_propose.add_argument("--request-id", required=True, metavar="ID")
    p_propose.add_argument(
        "--artifact-id", default=DEFAULT_FREEZE_ARTIFACT_ID, metavar="ID"
    )
    p_propose.add_argument("--revision-id", required=True, metavar="ID")
    p_propose.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for this exact revision",
    )
    p_propose.add_argument(
        "--accept-coverage-limitations",
        metavar="EXPLANATION",
        help=(
            "a literal explanation acknowledging the explicit coverage gaps (a "
            "string, never a boolean approval)"
        ),
    )
    p_propose.add_argument("--project", metavar="ID")

    for name, help_text in (
        ("status", "current decision + last approved freeze roster (safe summary)"),
        ("history", "immutable previous decisions newest-first (safe summaries)"),
        ("show", "read one freeze decision (safe summary; --content to opt in)"),
        ("require", "read the selection only when an active authentic freeze exists"),
    ):
        p = freeze_sub.add_parser(name, help=help_text)
        p.add_argument(
            "--artifact-id", default=DEFAULT_FREEZE_ARTIFACT_ID, metavar="ID"
        )
        if name in ("show",):
            p.add_argument("--revision-id", metavar="ID")
            p.add_argument(
                "--content",
                action="store_true",
                help="explicitly return the private, non-authorizing validated document",
            )
        if name == "require":
            p.add_argument(
                "--require-active",
                action="store_true",
                help="alias: read require_frozen (fails cleanly when not active)",
            )
        p.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _revision_ref(project_id: str, artifact_id: object, revision_id: object) -> dict:
    """Build one exact same-project ``RefRevision`` (validated locally)."""
    try:
        artifact = validate_identifier(artifact_id, "artifact_id")
        revision = validate_identifier(revision_id, "revision_id")
    except (RecordValidationError, TypeError, ValueError):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _REF_MESSAGE) from None
    return {
        "project_id": project_id,
        "artifact_id": artifact,
        "revision_id": revision,
    }


def _parse_optional_identifier(raw: object, message: str) -> str:
    if raw is None:
        return None  # type: ignore[return-value]
    try:
        return validate_identifier(raw, "identifier")
    except (RecordValidationError, TypeError, ValueError):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, message) from None


def _read_token_list(path: str):
    """Read one strict JSON known-relevant token list (shape only; library revalidates)."""
    value = _read_strict_json_file(path, what="known-relevant token list")
    if not isinstance(value, list):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _KNOWN_RELEVANT_MESSAGE)
    return value


def _read_string_list(path: str):
    """Read one strict JSON list of non-empty strings (shape only)."""
    value = _read_strict_json_file(path, what="limitations list")
    if not isinstance(value, list):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _LIMITATIONS_MESSAGE)
    for item in value:
        if not isinstance(item, str) or item.strip() == "":
            raise DiscoveryControlError(_CATEGORY_VALIDATION, _LIMITATIONS_MESSAGE)
    return value


def _read_entry_reviews(path: str):
    """Read one strict JSON entry-review list (shape only; library revalidates)."""
    value = _read_strict_json_file(path, what="entry-review list")
    if not isinstance(value, list) or not value:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _ENTRY_REVIEWS_MESSAGE)
    for item in value:
        if not isinstance(item, dict):
            raise DiscoveryControlError(_CATEGORY_VALIDATION, _ENTRY_REVIEWS_MESSAGE)
    return value


def _coverage_error(exc: CoverageError) -> DiscoveryControlError:
    if isinstance(exc, CoverageConflictError):
        return DiscoveryControlError(_CATEGORY_CONFLICT, _REPORT_CONFLICT_MESSAGE)
    if isinstance(exc, CoverageCorruptionError):
        return DiscoveryControlError(_CATEGORY_STORAGE, _REPORT_CORRUPT_MESSAGE)
    if isinstance(exc, CoverageValidationError):
        return DiscoveryControlError(_CATEGORY_VALIDATION, _REPORT_SOURCE_MESSAGE)
    return DiscoveryControlError(_CATEGORY_STORAGE, _REPORT_CORRUPT_MESSAGE)


def _freeze_error(exc: FreezeError) -> DiscoveryControlError:
    if isinstance(exc, FreezeConflictError):
        return DiscoveryControlError(_CATEGORY_CONFLICT, _FREEZE_CONFLICT_MESSAGE)
    if isinstance(exc, FreezeNotAuthorizedError):
        return DiscoveryControlError(_CATEGORY_VALIDATION, _FREEZE_NOT_ACTIVE_MESSAGE)
    if isinstance(exc, FreezeCorruptionError):
        return DiscoveryControlError(_CATEGORY_STORAGE, _FREEZE_CORRUPT_MESSAGE)
    if isinstance(exc, FreezeValidationError):
        return DiscoveryControlError(_CATEGORY_VALIDATION, _FREEZE_SOURCE_MESSAGE)
    return DiscoveryControlError(_CATEGORY_STORAGE, _FREEZE_CORRUPT_MESSAGE)


# --------------------------------------------------------------------------- #
# safe summaries (never raw queries / identifiers / sources / proposals)
# --------------------------------------------------------------------------- #
def _report_gaps(payload: dict) -> dict:
    """The explicit gap flags for one validated coverage payload."""
    known = payload["known_relevant"]
    citations = payload["citations"]
    counts = payload["corpus"]["counts"]
    sources = payload["sources"]["totals"]
    return {
        "known_relevant_missing": known["missing"],
        "known_relevant_not_assessed": known["status"] == "not_assessed",
        "citation_gaps": len(citations["gaps"]),
        "expansion_not_performed": not citations["expansion_performed"],
        "source_failures": sources["failures"],
        "missing_identifier_candidates": counts["missing_identifier_candidates"],
        "identifier_conflict_candidates": counts["identifier_conflict_candidates"],
    }


def _work_summary(work: dict) -> dict:
    totals = work["totals"]
    return {
        "observations": totals["observations"],
        "pages": totals["pages"],
        "http_attempts": totals["http_attempts"],
        "response_bytes": totals["response_bytes"],
        "failures": totals["failures"],
        "metadata_imports": totals["metadata_imports"],
        "imported_occurrences": totals["imported_occurrences"],
        "started_at": totals["started_at"],
        "completed_at": totals["completed_at"],
    }


def _money_summary(monetary: dict) -> dict:
    routes = {}
    for route, bucket in monetary["runtime_dispatch"].items():
        routes[route] = {
            "dispatches": bucket["dispatches"],
            "known_dispatches": bucket["known_dispatches"],
            "estimated_dispatches": bucket["estimated_dispatches"],
            "unknown_dispatches": bucket["unknown_dispatches"],
            "not_incurred_dispatches": bucket["not_incurred_dispatches"],
            "known_amount_usd": bucket["known_amount_usd"],
            "estimated_amount_usd": bucket["estimated_amount_usd"],
            "unknown_reserve_usd": bucket["unknown_reserve_usd"],
            "reserve_usd": bucket["reserve_usd"],
        }
    return {
        "basis": monetary["basis"],
        "routes": routes,
        "synthetic_jobs": dict(monetary["synthetic_jobs"]),
        "shared_exposure": dict(monetary["shared_exposure"]),
    }


def _report_summary(document: dict) -> dict:
    """A safe, non-authorizing coverage summary (no query/identifier/source)."""
    payload = document["payload"]
    known = payload["known_relevant"]
    citations = payload["citations"]
    return {
        "project_id": document["project_id"],
        "artifact_id": document["artifact_id"],
        "revision_id": document["revision_id"],
        "content_sha256": document["content_sha256"],
        "byte_size": document["byte_size"],
        "document_type": document["document_type"],
        "schema_version": document["schema_version"],
        "corpus": {
            "ref": dict(payload["corpus"]["ref"]),
            "content_sha256": payload["corpus"]["content_sha256"],
            "counts": dict(payload["corpus"]["counts"]),
        },
        "plan": {"ref": dict(payload["plan"]["ref"])},
        "totals": dict(payload["totals"]),
        "gaps": _report_gaps(payload),
        "citations": {
            "expansion_performed": citations["expansion_performed"],
            "reports": len(citations["reports"]),
            "directions": list(citations["directions"]),
            "counts": dict(citations["counts"]),
        },
        "known_relevant": {
            "status": known["status"],
            "matched": known["matched"],
            "missing": known["missing"],
        },
        "work": _work_summary(payload["work"]),
        "monetary": _money_summary(payload["monetary"]),
        "limitations_count": len(payload["limitations_summary"]),
        "authorizing": False,
        "completeness_claimed": False,
        "scientific_validation": False,
        "notice": payload["notice"],
    }


def _approval_summary(status: Any) -> dict | None:
    if status is None:
        return None
    return {
        "approval_id": status.approval_id,
        "status": status.status,
        "decision": status.decision,
        "active": status.is_active,
        "satisfies_named_gate": status.satisfies_named_gate,
        "gate_kind": status.gate_kind,
        "gate_id": status.gate_id,
    }


def _decision_summary(document: dict) -> dict:
    """A safe freeze-decision summary (never the candidate reviews/ids by default)."""
    payload = document["payload"]
    selection = payload["selection"]
    return {
        "project_id": document["project_id"],
        "artifact_id": document["artifact_id"],
        "revision_id": document["revision_id"],
        "content_sha256": document["content_sha256"],
        "byte_size": document.get("byte_size"),
        "document_type": document["document_type"],
        "schema_version": document["schema_version"],
        "decision": payload["decision"],
        "corpus_ref": dict(payload["corpus"]["ref"]),
        "coverage_ref": dict(payload["coverage"]["ref"]),
        "plan_ref": dict(payload["plan"]["ref"]),
        "gaps": dict(payload["coverage"]["gaps"]),
        "coverage_acceptance_acknowledged": payload["coverage_acceptance"][
            "acknowledged"
        ],
        "selection_counts": {
            "included": len(selection["included"]),
            "excluded": len(selection["excluded"]),
            "deferred": len(selection["deferred"]),
            "no_scientific_use": len(selection["no_scientific_use"]),
        },
        "approval": document.get("approval"),
        "frozen": bool(document.get("frozen", False)),
        "authorizing": False,
        "completeness_claimed": False,
        "scientific_validation": False,
        "notice": payload["notice"],
    }


# --------------------------------------------------------------------------- #
# report handlers
# --------------------------------------------------------------------------- #
def _resolve_expansion_reports(store: Any, job_ids: list) -> list:
    """Resolve each repeated ``--expansion-job-id`` to one validated mapping."""
    reports = []
    for raw in job_ids:
        job_id = _parse_optional_identifier(raw, _JOB_ID_MESSAGE)
        mapping = worker_discovery.persisted_expansion_report(store, job_id=job_id)
        if mapping is None:
            raise DiscoveryControlError(_CATEGORY_VALIDATION, _NO_EXPANSION_MESSAGE)
        reports.append(mapping)
    return reports


def discovery_report_create(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery report create`` -- record one immutable coverage report.

    Every input is read and validated (strictly and locally) **before** any write;
    a bad value is a typed error and nothing is recorded. The report is a proposal
    artifact only: it freezes nothing and claims no completeness.
    """
    rights = _read_strict_json_file(args.rights_file, what="rights record")
    known_relevant = (
        _read_token_list(args.known_relevant_file)
        if args.known_relevant_file is not None
        else []
    )
    limitations = (
        _read_string_list(args.limitations_file)
        if args.limitations_file is not None
        else []
    )
    job_ids = list(args.expansion_job_ids or [])

    project_id = _resolve_store_project_id(ctx, args.project)
    corpus_ref = _revision_ref(
        project_id, args.corpus_artifact_id, args.corpus_revision_id
    )
    plan_ref = _revision_ref(project_id, args.plan_artifact_id, args.plan_revision_id)
    # Fail closed on the exact caller rights record before any store write.
    _validate_storage_rights(project_id, args.artifact_id, args.revision_id, rights)

    store = _open_verified_store(ctx, project_id)
    try:
        expansion_reports = _resolve_expansion_reports(store, job_ids)
        coverage = CoverageRepository(store)
        # Replay detection: a revision for this exact id already recorded is a
        # historical replay (record_report returns it when the logical binding is
        # identical and conflicts when it diverges); a request-key replay that
        # resolved to a different revision id is also reported truthfully below.
        existing = ArtifactRepository(store).get_revision(
            args.artifact_id, args.revision_id
        )
        try:
            revision = coverage.record_report(
                corpus_ref,
                plan_ref=plan_ref,
                request_id=args.request_id,
                rights=rights,
                expansion_reports=expansion_reports,
                known_relevant=known_relevant,
                limitations=limitations,
                revision_id=args.revision_id,
                artifact_id=args.artifact_id,
            )
        except CoverageError as exc:
            raise _coverage_error(exc) from exc
        ref = {
            "project_id": project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }
        document = coverage.get(ref)
    finally:
        store.close()
    return {
        "report": _report_summary(document),
        "replayed": existing is not None
        or revision.revision_id != args.revision_id,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def discovery_report_show(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery report show`` -- read one coverage report (summary; --content)."""
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        coverage = CoverageRepository(store)
        try:
            if args.revision_id is not None:
                _parse_optional_identifier(args.revision_id, _REF_MESSAGE)
                document = coverage.get(
                    {
                        "project_id": project_id,
                        "artifact_id": args.artifact_id,
                        "revision_id": args.revision_id,
                    }
                )
            else:
                document = coverage.current(args.artifact_id)
        except CoverageError as exc:
            raise _coverage_error(exc) from exc
    finally:
        store.close()
    if document is None:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _REPORT_NOT_FOUND_MESSAGE
        )
    if args.content:
        return {
            "document": document,
            "private": True,
            "non_authorizing": True,
            "authorizes_execution": False,
            "authorizes_export": False,
            "scientific_validation": False,
        }
    return {"report": _report_summary(document)}


# --------------------------------------------------------------------------- #
# freeze handlers
# --------------------------------------------------------------------------- #
def _freeze_approval_request(store: Any, revision: Any) -> dict[str, object]:
    """The non-authorizing named-gate request for one freeze revision.

    ``satisfied`` is the real repository's answer **for this exact returned
    revision** and may only be true when the returned revision is still the
    artifact's current head with a current, active approval at the exact named
    gate; a historical replay is never reported satisfied merely because a newer
    revision at the same gate was approved.
    """
    artifacts = ArtifactRepository(store)
    head = artifacts.current_revision(revision.artifact_id)
    is_current = head is not None and head.revision_id == revision.revision_id
    approvals = HumanApprovalRepository(store)
    status = None
    if is_current:
        record = approvals.current_approval(
            revision.artifact_id, gate_kind=FREEZE_GATE_KIND, gate_id=FREEZE_GATE_ID
        )
        if record is not None and record.target["revision_id"] == revision.revision_id:
            status = approvals.evaluate(record.approval_id)
    satisfied = bool(status is not None and status.satisfies_named_gate)
    return {
        "gate_kind": FREEZE_GATE_KIND,
        "gate_id": FREEZE_GATE_ID,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "content_sha256": revision.content_sha256,
        "satisfied": satisfied,
        "current": bool(is_current),
        "stale": not is_current,
        "approval": _approval_summary(status),
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
        "snapshot_preparation": {
            "gate_kind": FREEZE_GATE_KIND,
            "gate_id": FREEZE_GATE_ID,
            "prepare_command": "gate prepare",
            "confirm_command": "gate confirm",
            "binds": "exact artifact/revision/content digest and pinned inputs",
            "note": _SNAPSHOT_NOTE,
        },
        "ingest_guidance": _INGEST_GUIDANCE,
    }


def discovery_freeze_propose(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery freeze propose`` -- record one inert freeze/search-again proposal.

    Steps: (1) strictly read the entry-review file and the caller rights record,
    (2) fail closed on the exact rights binding, (3) call
    :meth:`FreezeRepository.propose` (which re-reads/re-derives every durable input,
    keeps an identical replay historical, and refuses a divergent reuse), and (4)
    return the exact named-gate ``approval_request``. It records **no** approval and
    confirms nothing.
    """
    entry_reviews = _read_entry_reviews(args.entry_reviews_file)
    rights = _read_strict_json_file(args.rights_file, what="rights record")

    project_id = _resolve_store_project_id(ctx, args.project)
    corpus_ref = _revision_ref(
        project_id, args.corpus_artifact_id, args.corpus_revision_id
    )
    coverage_ref = _revision_ref(
        project_id, args.coverage_artifact_id, args.coverage_revision_id
    )
    _validate_storage_rights(project_id, args.artifact_id, args.revision_id, rights)

    store = _open_verified_store(ctx, project_id)
    try:
        freeze = FreezeRepository(store)
        existing = ArtifactRepository(store).get_revision(
            args.artifact_id, args.revision_id
        )
        try:
            revision = freeze.propose(
                corpus_ref,
                coverage_ref,
                decision=args.decision,
                entry_reviews=entry_reviews,
                rationale=args.rationale,
                request_id=args.request_id,
                rights=rights,
                revision_id=args.revision_id,
                artifact_id=args.artifact_id,
                accept_coverage_limitations=args.accept_coverage_limitations,
            )
        except FreezeError as exc:
            raise _freeze_error(exc) from exc
        approval_request = _freeze_approval_request(store, revision)
        ref = {
            "project_id": project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }
        document = freeze.get(ref)
    finally:
        store.close()
    return {
        "decision": _decision_summary(document),
        "approval_request": approval_request,
        "replayed": existing is not None
        or revision.revision_id != args.revision_id,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def discovery_freeze_status(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery freeze status`` -- current decision + last approved freeze roster."""
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        freeze = FreezeRepository(store)
        try:
            current = freeze.current_decision(args.artifact_id)
            frozen = freeze.frozen_corpus(args.artifact_id)
            history = freeze.history(args.artifact_id)
        except FreezeError as exc:
            raise _freeze_error(exc) from exc
    finally:
        store.close()
    current_summary = _decision_summary(current) if current is not None else None
    frozen_summary = None
    if frozen is not None:
        frozen_summary = {
            "revision_id": frozen["revision_id"],
            "corpus_ref": dict(frozen["payload"]["corpus"]["ref"]),
            "included_count": len(frozen["included_candidate_ids"]),
            "frozen": bool(frozen["frozen"]),
            "approval": frozen.get("approval"),
        }
    newest = history[0] if history else None
    newest_summary = _decision_summary(newest) if newest is not None else None
    return {
        "current_decision": current_summary,
        "last_frozen_corpus": frozen_summary,
        "newest_revision": newest_summary,
        "decision_count": len(history),
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
        "ingest_guidance": _INGEST_GUIDANCE,
    }


def discovery_freeze_show(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery freeze show`` -- read one freeze decision (summary; --content)."""
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        freeze = FreezeRepository(store)
        try:
            if args.revision_id is not None:
                _parse_optional_identifier(args.revision_id, _REF_MESSAGE)
                document = freeze.get(
                    {
                        "project_id": project_id,
                        "artifact_id": args.artifact_id,
                        "revision_id": args.revision_id,
                    }
                )
            else:
                history = freeze.history(args.artifact_id)
                document = history[0] if history else None
        except FreezeError as exc:
            raise _freeze_error(exc) from exc
    finally:
        store.close()
    if document is None:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _FREEZE_NOT_FOUND_MESSAGE
        )
    if args.content:
        return {
            "document": document,
            "private": True,
            "non_authorizing": True,
            "authorizes_execution": False,
            "authorizes_export": False,
            "scientific_validation": False,
        }
    return {"decision": _decision_summary(document)}


def discovery_freeze_history(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery freeze history`` -- immutable previous decisions (safe summaries)."""
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        freeze = FreezeRepository(store)
        try:
            history = freeze.history(args.artifact_id)
        except FreezeError as exc:
            raise _freeze_error(exc) from exc
    finally:
        store.close()
    return {
        "decisions": [_decision_summary(document) for document in history],
        "count": len(history),
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def discovery_freeze_require(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery freeze require`` -- the selection only under an active freeze."""
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        freeze = FreezeRepository(store)
        try:
            document = freeze.require_frozen(args.artifact_id)
        except FreezeError as exc:
            raise _freeze_error(exc) from exc
    finally:
        store.close()
    return {
        "frozen": True,
        "decision": _decision_summary(document),
        "corpus_ref": dict(document["payload"]["corpus"]["ref"]),
        "included_candidate_ids": list(document["included_candidate_ids"]),
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# dispatch (addon)
# --------------------------------------------------------------------------- #
#: The addon's nested leaf-command map: group -> command -> handler.
DISCOVERY_REPORT_COMMANDS = {
    "report": {
        "create": discovery_report_create,
        "show": discovery_report_show,
    },
    "freeze": {
        "propose": discovery_freeze_propose,
        "status": discovery_freeze_status,
        "show": discovery_freeze_show,
        "history": discovery_freeze_history,
        "require": discovery_freeze_require,
    },
}

#: The addon's nested argparse ``dest`` names, one per group.
DISCOVERY_REPORT_LEAF_DESTS = {
    "report": "discovery_report_command",
    "freeze": "discovery_freeze_command",
}


def dispatch_discovery_reports(args: argparse.Namespace) -> Any:
    """Route one parsed addon invocation to its handler, or return ``None``."""
    group = DISCOVERY_REPORT_COMMANDS.get(
        getattr(args, "discovery_command", None)
    )
    if group is None:
        return None
    leaf = getattr(args, DISCOVERY_REPORT_LEAF_DESTS[args.discovery_command], None)
    return group.get(leaf)
