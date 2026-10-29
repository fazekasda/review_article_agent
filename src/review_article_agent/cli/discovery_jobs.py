"""Personal CLI for the implemented discovery **background jobs** (Step 05, J1).

This module adds the job-submission/inspection surface over the already-integrated
discovery background-job routes
(:mod:`review_article_agent.worker.discovery`) reached through the owner-only
control transport (:mod:`review_article_agent.runtime.ipc`) and ensured through
the detached-process launcher (:mod:`review_article_agent.worker.process`). It
extends the existing root ``discovery`` command group cleanly through an **addon**
builder + command map, so a later CLI slice can add reporting/freeze groups
without a widespread refactor of :mod:`review_article_agent.cli.discovery`.

It owns only the *CLI surface* over the existing value layers:

* :mod:`review_article_agent.worker.discovery` -- strict submission validation,
  the five exact operation modes (``query`` / ``seed`` / ``metadata_import`` /
  ``expand`` / ``lead``), the safe result projections, and
  :func:`~review_article_agent.worker.discovery.persisted_expansion_report`.
* :mod:`review_article_agent.discovery.lead_runner` -- the pure hosted-use
  document validator (:func:`validate_hosted_use_document`), the exact named
  gate id, and the read-only ``get_lead_outcome`` readback.
* :mod:`review_article_agent.cli.jobs` -- the existing worker-ensure /
  authenticated-control / IPC-error-mapping patterns, reused unchanged.

What this module **is** (``docs/implementation/05-discovery.md`` deliverables
D1-D4; ADR 003 D-2/D-3/D-4/D-5; the personal-CLI baseline -- no new authority is
introduced):

* ``discovery search run`` -- validate an exact plan artifact/revision, a query
  index, and a request id locally, then submit one ``query`` job (version-1
  ``discovery_submit``). It returns the **queued admission only** -- never a
  completion, never a provider call.
* ``discovery search seed`` -- submit one ``seed`` job from an explicit
  source/query/page-size/max-pages/request id. The worker enforces every hard
  ceiling; the CLI only parses shape. The result is labelled **provisional**.
* ``discovery search import`` -- read one strict, local metadata-record array
  (``--file``, a user-chosen local file only) plus source/origin_locator/
  observed_at/plan-ref/request id, and submit one ``metadata_import`` job. Every
  value is validated and the payload is capped at the control-transport byte
  bound **before** a worker is ensured; no arbitrary download is performed.
* ``discovery expand run`` -- submit one ``expand`` job from an exact
  plan + corpus refs plus a strict roots file (``[{source,id}]``). The only
  accepted limits are the approved plan's; there is **no** CLI override.
* ``discovery expand show`` -- read one completed expand job's report: a safe
  summary by default, and the **full** persisted mapping only with ``--content``
  (through the read-only, validating
  :func:`persisted_expansion_report`; the raw graph is never the default IPC
  status).
* ``discovery lead configure`` -- validate one strict ``discovery_hosted_use``
  envelope with :func:`validate_hosted_use_document`, pin its exact declared
  source dependencies (plus a valid own ``version_of`` lineage edge when an
  amendment parent is named), and store it as one immutable proposal revision.
  It fabricates no rights and performs no terms verification; it returns the
  exact ref, an **non-authorizing** ``approval_request`` (``batch`` gate kind,
  ``discovery.hosted_use.<purpose>`` gate id) and the exact snapshot-preparation
  guidance. An identical retry replays; a substantive change needs a new
  revision and a fresh gate; a historical replay never inherits a newer approval.
* ``discovery lead run`` -- submit one ``lead`` job from an exact batch ref and a
  request id with the **mandatory** ``--credential-env OPENROUTER_API_KEY``. The
  credential **value** is never accepted, sent over IPC, persisted, or logged;
  the worker reads the explicitly named environment variable at dispatch. The
  CLI may ensure a worker inherits its environment (existing process semantics)
  and warns when the named key is not present -- it never restarts a user's
  worker.

What this module **is not**:

* No new lifecycle authority: ``job status|list|attach|pause|resume|cancel|
  drain|switch|reconcile`` remain the only lifecycle controls and apply to every
  new job unchanged.
* No ``--wait``, no default broad search, no model route, no paid call: a
  submission returns a durable queued admission only. ``expand show`` and the
  lead readbacks issue no call.
* No approval, no scientific validation, and no completeness claim is made by
  any command here. ``--content`` output is private, non-authorizing data.

Design rules:

* **Verified project access.** Every project is opened through the registry with
  ``expected_project_id`` before any store read/write, so a project-id/path
  mismatch or traversal is refused first.
* **Strict, bounded input.** Every JSON file is read with the parent group's
  hard byte cap and the existing strict parser (duplicate keys, non-finite
  numbers, over-deep nesting refused); no input value or path is reflected.
* **Reused control.** Worker-ensure, PID verification, request-bound IPC, and the
  engine error-code taxonomy are reused verbatim from :mod:`review_article_agent
  .cli.jobs`; only a queued response is ever surfaced.
* **Typed, curated errors.** Failures are the parent group's
  :class:`~review_article_agent.cli.discovery.DiscoveryControlError` (local
  validation/inspection) or the reused
  :class:`~review_article_agent.cli.jobs.JobControlError` (worker control), each
  a static, value-free message -- no raw exception, path, source/query/title, or
  secret is echoed.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from ..discovery.lead_runner import (
    HOSTED_USE_GATE_KIND,
    HostedLeadError,
    HostedUseDocumentError,
    hosted_use_gate_id,
    validate_hosted_use_document,
)
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
)
from ..runtime import ipc
from ..storage import ArtifactConflictError, ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository
from ..worker import discovery as worker_discovery
from ..worker.engine import PROTOCOL_VERSION
from .discovery import (
    DiscoveryControlError,
    _open_verified_store,
    _read_strict_json_file,
    _resolve_project_id as _resolve_store_project_id,
    _validate_storage_rights,
)
from .jobs import (
    JobControlError,
    _expect_ok,
    _job_projection,
    _parse_bounded_int,
    _parse_identifier,
    _resolve_project_id,
    _rpc,
    _start_worker,
)
from .worker import _resolve_timeout

__all__ = [
    "DISCOVERY_JOB_COMMANDS",
    "DISCOVERY_JOB_LEAF_DESTS",
    "build_discovery_jobs_parser",
    "dispatch_discovery_jobs",
    "discovery_expand_run",
    "discovery_expand_show",
    "discovery_lead_configure",
    "discovery_lead_run",
    "discovery_search_import",
    "discovery_search_run",
    "discovery_search_seed",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: The local upper bound for a plain non-negative integer argument. The worker's
#: *hard ceilings* (a seed's record/page budget, the import record count, the
#: expansion roots) are enforced by the worker, not duplicated here; this only
#: bounds the raw token so ``int()`` can never be tripped by a pathological value.
_MAX_INT = 1_000_000_000

#: The one hosted-use document type and its artifact kind.
_HOSTED_USE_ARTIFACT_KIND = "report"
#: The lineage role of an amendment's same-artifact parent edge.
_VERSION_OF_ROLE = "version_of"

# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_REF_MESSAGE = (
    "invalid artifact or revision identifier for a plan/corpus/batch reference; "
    "it must be a bounded opaque id. The supplied value is not shown. Run --help "
    "for usage."
)
_QUERY_INDEX_MESSAGE = (
    "invalid value for --query-index; it must be a non-negative integer. The "
    "supplied value is not shown. Run --help for usage."
)
_BOUND_MESSAGE = (
    "invalid value for a search-bound argument; it must be a positive integer. "
    "The supplied value is not shown. Run --help for usage."
)
_SOURCE_MESSAGE = (
    "invalid value for --source; it must be a non-empty source name. The supplied "
    "value is not shown. Run --help for usage."
)
_QUERY_MESSAGE = (
    "invalid value for --query; it must be a non-empty query string. The supplied "
    "value is not shown. Run --help for usage."
)
_LOCATOR_MESSAGE = (
    "invalid value for --origin-locator; it must be a non-empty opaque locator. "
    "The supplied value is not shown. Run --help for usage."
)
_OBSERVED_AT_MESSAGE = (
    "invalid value for --observed-at; it must be a non-empty UTC instant. The "
    "supplied value is not shown. Run --help for usage."
)
_CREDENTIAL_ENV_MESSAGE = (
    "invalid value for --credential-env; only the one approved hosted-lead "
    "environment variable name is accepted, and the credential value is never "
    "supplied on the command line. The supplied value is not shown. Run --help "
    "for usage."
)
_RECORDS_MESSAGE = (
    "the metadata record file must be a strict JSON array of record objects "
    "(each record an object of the exact supported fields). Nothing was sent."
)
_ROOTS_MESSAGE = (
    "the roots file must be a strict JSON list of {\"source\", \"id\"} objects "
    "with non-empty string values. Nothing was sent."
)
_PAYLOAD_TOO_LARGE_MESSAGE = (
    "the discovery parameters are larger than the control-transport byte bound; "
    "refusing to send them. Nothing was sent."
)
_BATCH_REF_MESSAGE = (
    "the hosted-use batch reference is not this project's exact artifact/revision. "
    "Nothing was sent."
)
_NO_EXPAND_FOUND_MESSAGE = (
    "no completed expansion report matches that job id or request id in this "
    "project; nothing was read or sent."
)
_EXPAND_TARGET_MESSAGE = (
    "choose exactly one of --job-id and --request-id. The supplied values are not "
    "shown. Run --help for usage."
)
_HOSTED_USE_INVALID_MESSAGE = (
    "the hosted-use document failed strict typed validation (a wrong or missing "
    "envelope/payload field, an unsupported purpose, an invalid configuration, a "
    "malformed or cross-project source reference, or a malformed evidence "
    "reference). Nothing was written."
)
_HOSTED_USE_PARENT_MESSAGE = (
    "the supplied parent revision is not this artifact's current head, so this is "
    "not a valid amendment; an amendment must reference the exact head. Nothing "
    "was written."
)
_HOSTED_USE_CONFLICT_MESSAGE = (
    "the hosted-use artifact already exists with a different kind, or a divergent "
    "revision is already recorded; no existing data was overwritten."
)
_HOSTED_USE_RIGHTS_MESSAGE = (
    "the rights record does not authorize storing this exact hosted-use revision "
    "(a wrong subject binding, a missing storage permission, or a malformed "
    "record). Nothing was written."
)
_MISSING_KEY_NOTICE = (
    "the named hosted-lead credential is not present in this CLI's environment; "
    "the worker reads the environment variable at dispatch, so configure the "
    "worker's environment before it runs the job (this command never restarts or "
    "reconfigures a user's worker). The job is admitted and queued; inspect it "
    "with `job status`."
)
_SNAPSHOT_NOTE = (
    "An owner records the exact named gate out of band on the controlling "
    "terminal with `gate prepare`/`gate confirm` for this exact artifact/revision "
    "and gate (the batch binds the exact revision, its content digest, and its "
    "pinned sources). This CLI command records no approval."
)


# --------------------------------------------------------------------------- #
# argument tree (addon)
# --------------------------------------------------------------------------- #
def build_discovery_jobs_parser(
    disc_sub: "argparse._SubParsersAction", lead_sub: "argparse._SubParsersAction"
) -> None:
    """Extend the root ``discovery`` parser with the job addon groups.

    Adds a ``search`` and an ``expand`` sub-group to ``disc_sub`` and extends the
    **existing** ``lead`` sub-group (created by
    :func:`review_article_agent.cli.discovery.build_discovery_parser`) with
    ``configure`` and ``run`` -- never registering a duplicate ``lead`` group, so
    the existing ``lead status``/``lead content`` readbacks are preserved.
    """
    # -- search ------------------------------------------------------------- #
    search = disc_sub.add_parser(
        "search",
        help=(
            "submit discovery search background jobs (exact run / provisional "
            "seed / local metadata import); returns queued admission only"
        ),
    )
    search_sub = search.add_subparsers(
        dest="discovery_search_command", required=True, metavar="SEARCH_COMMAND"
    )

    p_run = search_sub.add_parser(
        "run",
        help=(
            "submit one exact approved-plan query job; returns queued admission, "
            "never completion"
        ),
    )
    p_run.add_argument("--artifact-id", required=True, metavar="ID")
    p_run.add_argument("--revision-id", required=True, metavar="ID")
    p_run.add_argument("--query-index", required=True, metavar="N")
    p_run.add_argument("--request-id", required=True, metavar="ID")
    p_run.add_argument("--project", metavar="ID")
    p_run.add_argument("--timeout", metavar="SECONDS")

    p_seed = search_sub.add_parser(
        "seed",
        help=(
            "submit one provisional seed search job (worker-enforced ceilings); "
            "the result stays provisional"
        ),
    )
    p_seed.add_argument("--source", required=True, metavar="SOURCE")
    p_seed.add_argument("--query", required=True, metavar="TEXT")
    p_seed.add_argument("--page-size", required=True, metavar="N")
    p_seed.add_argument("--max-pages", required=True, metavar="N")
    p_seed.add_argument("--request-id", required=True, metavar="ID")
    p_seed.add_argument("--project", metavar="ID")
    p_seed.add_argument("--timeout", metavar="SECONDS")

    p_import = search_sub.add_parser(
        "import",
        help=(
            "submit one local metadata-import job from a strict record array "
            "file; no arbitrary download"
        ),
    )
    p_import.add_argument("--artifact-id", required=True, metavar="ID")
    p_import.add_argument("--revision-id", required=True, metavar="ID")
    p_import.add_argument(
        "--file", required=True, metavar="PATH", help="strict JSON metadata record array"
    )
    p_import.add_argument("--source", required=True, metavar="SOURCE")
    p_import.add_argument("--origin-locator", required=True, metavar="LOCATOR")
    p_import.add_argument("--observed-at", required=True, metavar="INSTANT")
    p_import.add_argument("--request-id", required=True, metavar="ID")
    p_import.add_argument("--project", metavar="ID")
    p_import.add_argument("--timeout", metavar="SECONDS")

    # -- expand ------------------------------------------------------------- #
    expand = disc_sub.add_parser(
        "expand", help="bounded citation-expansion background jobs (run/show)"
    )
    expand_sub = expand.add_subparsers(
        dest="discovery_expand_command", required=True, metavar="EXPAND_COMMAND"
    )

    p_expand_run = expand_sub.add_parser(
        "run",
        help=(
            "submit one bounded citation-expansion job; the only limits are the "
            "approved plan's"
        ),
    )
    p_expand_run.add_argument("--artifact-id", required=True, metavar="ID")
    p_expand_run.add_argument("--revision-id", required=True, metavar="ID")
    p_expand_run.add_argument("--corpus-artifact-id", required=True, metavar="ID")
    p_expand_run.add_argument("--corpus-revision-id", required=True, metavar="ID")
    p_expand_run.add_argument(
        "--roots-file", required=True, metavar="PATH", help="strict JSON [{source,id}] list"
    )
    p_expand_run.add_argument("--request-id", required=True, metavar="ID")
    p_expand_run.add_argument("--project", metavar="ID")
    p_expand_run.add_argument("--timeout", metavar="SECONDS")

    p_expand_show = expand_sub.add_parser(
        "show",
        help=(
            "read one completed expansion report (safe summary; --content is the "
            "explicit private full mapping)"
        ),
    )
    p_expand_show.add_argument("--job-id", metavar="ID")
    p_expand_show.add_argument("--request-id", metavar="ID")
    p_expand_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly return the private, non-authorizing full expansion mapping",
    )
    p_expand_show.add_argument("--project", metavar="ID")

    # -- lead (extend the existing group; never duplicate it) ---------------- #
    p_lconf = lead_sub.add_parser(
        "configure",
        help=(
            "validate and store one hosted-use batch as a proposal revision; "
            "records no approval"
        ),
    )
    p_lconf.add_argument("--artifact-id", required=True, metavar="ID")
    p_lconf.add_argument("--revision-id", required=True, metavar="ID")
    p_lconf.add_argument(
        "--file", required=True, metavar="PATH", help="strict discovery_hosted_use envelope"
    )
    p_lconf.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for this exact revision",
    )
    p_lconf.add_argument("--request-id", metavar="ID")
    p_lconf.add_argument("--parent-revision-id", metavar="ID")
    p_lconf.add_argument("--project", metavar="ID")

    p_lrun = lead_sub.add_parser(
        "run",
        help=(
            "submit one scoped hosted lead-proposal job (mandatory --credential-env); "
            "returns queued admission only"
        ),
    )
    p_lrun.add_argument("--batch-artifact-id", required=True, metavar="ID")
    p_lrun.add_argument("--batch-revision-id", required=True, metavar="ID")
    p_lrun.add_argument(
        "--credential-env",
        required=True,
        metavar="NAME",
        help="the one approved environment variable name (the value is never accepted)",
    )
    p_lrun.add_argument("--request-id", required=True, metavar="ID")
    p_lrun.add_argument("--project", metavar="ID")
    p_lrun.add_argument("--timeout", metavar="SECONDS")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _revision_ref(project_id: str, artifact_id: object, revision_id: object) -> dict:
    """Build one exact same-project ``RefRevision`` (validated locally)."""
    try:
        artifact = validate_identifier(artifact_id, "artifact_id")
        revision = validate_identifier(revision_id, "revision_id")
    except (RecordValidationError, TypeError, ValueError):
        raise JobControlError(_CATEGORY_VALIDATION, _REF_MESSAGE) from None
    return {
        "project_id": project_id,
        "artifact_id": artifact,
        "revision_id": revision,
    }


def _require_nonempty(raw: object, message: str) -> str:
    if not isinstance(raw, str) or raw.strip() == "":
        raise JobControlError(_CATEGORY_VALIDATION, message)
    return raw


def _check_payload_size(parameters: dict) -> None:
    """Refuse a parameter payload larger than the control-transport byte bound."""
    try:
        encoded = canonical_json(parameters)
    except (RecordValidationError, TypeError, ValueError):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _RECORDS_MESSAGE) from None
    if len(encoded) > ipc.MAX_REQUEST_BYTES:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _PAYLOAD_TOO_LARGE_MESSAGE
        )


def _read_record_array(path: str) -> list:
    """Read one strict JSON array of record objects (local; never a download)."""
    value = _read_strict_json_file(path, what="metadata record array")
    if not isinstance(value, list) or not value:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _RECORDS_MESSAGE)
    for item in value:
        if not isinstance(item, dict):
            raise DiscoveryControlError(_CATEGORY_VALIDATION, _RECORDS_MESSAGE)
    return value


def _read_roots(path: str) -> list:
    """Read one strict JSON ``[{source, id}]`` list of expandable identities."""
    value = _read_strict_json_file(path, what="roots")
    if not isinstance(value, list) or not value:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _ROOTS_MESSAGE)
    roots: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"source", "id"}:
            raise DiscoveryControlError(_CATEGORY_VALIDATION, _ROOTS_MESSAGE)
        source, native_id = item["source"], item["id"]
        if (
            not isinstance(source, str)
            or not isinstance(native_id, str)
            or source == ""
            or native_id == ""
        ):
            raise DiscoveryControlError(_CATEGORY_VALIDATION, _ROOTS_MESSAGE)
        roots.append({"source": source, "id": native_id})
    return roots


def _submit(
    ctx: Any,
    *,
    mode: str,
    parameters: dict,
    request_id: str,
    project_id: str,
    timeout: float | None,
) -> dict[str, object]:
    """Ensure a worker and submit one discovery job; return the queued admission."""
    request = {
        "version": PROTOCOL_VERSION,
        "op": "discovery_submit",
        "project_id": project_id,
        "request_id": request_id,
        "mode": mode,
        "parameters": parameters,
    }
    pid = _start_worker(ctx, timeout)
    result = _expect_ok(_rpc(ctx, pid, request, timeout))
    job = _job_projection(result.get("job"))
    return {
        "mode": mode,
        "result_kind": worker_discovery.result_kind_for_mode(mode),
        "job": job,
        "job_id": job.get("job_id"),
        "submitted": True,
        "queued": True,
        "admission_only": True,
        "job_completion": False,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# search handlers
# --------------------------------------------------------------------------- #
def discovery_search_run(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery search run`` -- submit one exact approved-plan query job.

    Every argument is validated locally **before** a worker is ensured, so a bad
    value is a typed ``validation`` error and no daemon is started. Only the
    queued admission is returned; the search itself runs later in the worker.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    query_index = _parse_bounded_int(
        args.query_index, lo=0, hi=_MAX_INT, message=_QUERY_INDEX_MESSAGE
    )
    parameters = {
        "plan_ref": _revision_ref(project_id, args.artifact_id, args.revision_id),
        "query_index": query_index,
    }
    return _submit(
        ctx,
        mode=worker_discovery.MODE_QUERY,
        parameters=parameters,
        request_id=request_id,
        project_id=project_id,
        timeout=timeout,
    )


def discovery_search_seed(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery search seed`` -- submit one provisional seed search job.

    The worker enforces the hard record/page ceilings and the exact query source;
    the CLI only parses shape (and never echoes a query). The returned admission
    is labelled **provisional**: a seed abstract never becomes accepted project
    context here.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    source = _require_nonempty(args.source, _SOURCE_MESSAGE)
    query = _require_nonempty(args.query, _QUERY_MESSAGE)
    page_size = _parse_bounded_int(
        args.page_size, lo=1, hi=_MAX_INT, message=_BOUND_MESSAGE
    )
    max_pages = _parse_bounded_int(
        args.max_pages, lo=1, hi=_MAX_INT, message=_BOUND_MESSAGE
    )
    parameters = {
        "source": source,
        "query": query,
        "page_size": page_size,
        "max_pages": max_pages,
    }
    result = _submit(
        ctx,
        mode=worker_discovery.MODE_SEED,
        parameters=parameters,
        request_id=request_id,
        project_id=project_id,
        timeout=timeout,
    )
    result["provisional"] = True
    return result


def discovery_search_import(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery search import`` -- submit one local metadata-import job.

    The record array is read from a **user-chosen local file only** (never
    fetched); every value is validated (shape here, exact field/type/source in the
    worker) and the payload is capped at the control-transport byte bound before a
    worker is ensured.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    records = _read_record_array(args.file)
    source = _require_nonempty(args.source, _SOURCE_MESSAGE)
    origin_locator = _require_nonempty(args.origin_locator, _LOCATOR_MESSAGE)
    observed_at = _require_nonempty(args.observed_at, _OBSERVED_AT_MESSAGE)
    parameters = {
        "plan_ref": _revision_ref(project_id, args.artifact_id, args.revision_id),
        "source": source,
        "records": records,
        "origin_locator": origin_locator,
        "observed_at": observed_at,
    }
    _check_payload_size(parameters)
    return _submit(
        ctx,
        mode=worker_discovery.MODE_METADATA_IMPORT,
        parameters=parameters,
        request_id=request_id,
        project_id=project_id,
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# expand handlers
# --------------------------------------------------------------------------- #
def discovery_expand_run(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery expand run`` -- submit one bounded citation-expansion job.

    The roots come from a strict roots file; the plan and corpus are exact
    same-project refs. There is **no** CLI limits override: the only accepted
    limits are the approved plan's, revalidated in the worker before any request.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    roots = _read_roots(args.roots_file)
    parameters = {
        "plan_ref": _revision_ref(project_id, args.artifact_id, args.revision_id),
        "corpus_ref": _revision_ref(
            project_id, args.corpus_artifact_id, args.corpus_revision_id
        ),
        "roots": roots,
    }
    _check_payload_size(parameters)
    return _submit(
        ctx,
        mode=worker_discovery.MODE_EXPAND,
        parameters=parameters,
        request_id=request_id,
        project_id=project_id,
        timeout=timeout,
    )


def discovery_expand_show(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery expand show`` -- read one completed expansion report.

    The default is a bounded, safe summary (counts, limits, stop reasons, a report
    digest -- never the graph). ``--content`` explicitly returns the full
    persisted, validated, non-authorizing mapping through the read-only
    :func:`persisted_expansion_report`. No worker call is made.
    """
    job_id = args.job_id
    request_id = args.request_id
    if (job_id is None) == (request_id is None):
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _EXPAND_TARGET_MESSAGE)
    if job_id is not None:
        job_id = _parse_identifier(job_id)
    if request_id is not None:
        request_id = _parse_identifier(request_id)
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        report = worker_discovery.persisted_expansion_report(
            store, job_id=job_id, request_id=request_id
        )
    finally:
        store.close()
    if report is None:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _NO_EXPAND_FOUND_MESSAGE)
    if args.content:
        return {
            "mode": worker_discovery.MODE_EXPAND,
            "result_kind": worker_discovery.RESULT_KIND_EXPANSION,
            "document": report,
            "private": True,
            "non_authorizing": True,
            "authorizes_execution": False,
            "authorizes_export": False,
            "scientific_validation": False,
        }
    return {
        "mode": worker_discovery.MODE_EXPAND,
        "result_kind": worker_discovery.RESULT_KIND_EXPANSION,
        "expansion": worker_discovery.status_projection(
            worker_discovery.RESULT_KIND_EXPANSION, report
        ),
    }


# --------------------------------------------------------------------------- #
# lead handlers
# --------------------------------------------------------------------------- #
def _hosted_use_dependencies(
    project_id: str, source_refs: list[dict], parent_revision_id: str | None, artifact_id: str
) -> list[dict[str, str]]:
    """The exact generic dependencies of a hosted-use batch.

    Each declared source is pinned as a ``derived_from`` dependency, and a valid
    amendment adds its own artifact's historical parent as a ``version_of``
    lineage edge (the generic repository validates that the edge targets an older
    revision of the same artifact).
    """
    seen: set[tuple[str, str]] = set()
    dependencies: list[dict[str, str]] = []
    for ref in source_refs:
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
    if parent_revision_id is not None:
        dependencies.append(
            {
                "project_id": project_id,
                "artifact_id": artifact_id,
                "revision_id": parent_revision_id,
                "role": _VERSION_OF_ROLE,
            }
        )
    return dependencies


def _hosted_use_approval_request(
    store: Any, repo: Any, revision: Any, purpose: str
) -> dict[str, object]:
    """The non-authorizing named-gate request for one hosted-use revision.

    The ``satisfied`` flag is the real repository's answer **for this exact
    returned revision**: it may only be true when the returned revision is still
    the artifact's current head (``current``). A historical replay of an older
    revision is therefore never reported satisfied merely because a newer
    revision at the same gate was approved.
    """
    gate_id = hosted_use_gate_id(purpose)
    head = repo.current_revision(revision.artifact_id)
    is_current = head is not None and head.revision_id == revision.revision_id
    satisfied = is_current and HumanApprovalRepository(store).named_gate_satisfied(
        revision.artifact_id, gate_kind=HOSTED_USE_GATE_KIND, gate_id=gate_id
    )
    return {
        "gate_kind": HOSTED_USE_GATE_KIND,
        "gate_id": gate_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "content_sha256": revision.content_sha256,
        "source_refs": [
            {
                "project_id": dep.project_id,
                "artifact_id": dep.artifact_id,
                "revision_id": dep.revision_id,
            }
            for dep in revision.dependencies
            if not (
                dep.role == _VERSION_OF_ROLE and dep.artifact_id == revision.artifact_id
            )
        ],
        "satisfied": bool(satisfied),
        "current": bool(is_current),
        "stale": not is_current,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
        "snapshot_preparation": {
            "gate_kind": HOSTED_USE_GATE_KIND,
            "gate_id": gate_id,
            "prepare_command": "gate prepare",
            "confirm_command": "gate confirm",
            "binds": "exact artifact/revision/content digest and pinned sources",
            "note": _SNAPSHOT_NOTE,
        },
    }


def _hosted_use_revision_dict(revision: Any, purpose: str) -> dict[str, object]:
    return {
        "project_id": revision.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "parent_revision_id": revision.parent_revision_id,
        "content_sha256": revision.content_sha256,
        "byte_size": revision.byte_size,
        "document_type": "discovery_hosted_use",
        "purpose": purpose,
        "dependencies": [dep.as_dict() for dep in revision.dependencies],
        "scientific_state": "proposal",
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def discovery_lead_configure(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery lead configure`` -- store one hosted-use batch (proposal).

    Steps: (1) strictly parse and statically validate the envelope, (2) strictly
    parse the caller's explicit rights record, (3) derive the exact source
    dependencies (plus a ``version_of`` lineage edge for an amendment), (4)
    **replay first** -- an identical recorded logical import returns the
    historical revision and a divergent reuse of the request key conflicts, (5)
    for a genuinely new revision only, validate the parent and the rights gate,
    then create the artifact and import one immutable revision. It records **no**
    approval and verifies **no** provider term.
    """
    document = _read_strict_json_file(args.file, what="hosted-use document")
    try:
        validated = validate_hosted_use_document(document)
    except (HostedUseDocumentError, HostedLeadError) as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE
        ) from exc
    rights = _read_strict_json_file(args.rights_file, what="rights record")
    purpose = validated["payload"]["purpose"]
    project_id = _resolve_store_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        repo = ArtifactRepository(store)
        try:
            replayed = repo.get_revision(args.artifact_id, args.revision_id) is not None
        except RecordValidationError as exc:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE
            ) from exc
        dependencies = _hosted_use_dependencies(
            project_id, validated["payload"]["source_refs"], args.parent_revision_id,
            args.artifact_id,
        )
        if not replayed:
            _validate_storage_rights(
                project_id, args.artifact_id, args.revision_id, rights
            )
            _validate_hosted_use_parent(
                repo, args.artifact_id, args.parent_revision_id
            )
        try:
            if not replayed:
                repo.create_artifact(args.artifact_id, kind=_HOSTED_USE_ARTIFACT_KIND)
            revision = repo.import_revision(
                artifact_id=args.artifact_id,
                content=validated,
                origin_locator="discovery-hosted-use:" + purpose,
                rights=rights,
                revision_id=args.revision_id,
                parent_revision_id=args.parent_revision_id,
                dependencies=dependencies,
                request_id=args.request_id,
            )
        except ArtifactConflictError as exc:
            raise DiscoveryControlError(
                _CATEGORY_CONFLICT, _HOSTED_USE_CONFLICT_MESSAGE
            ) from exc
        except RecordValidationError as exc:
            raise DiscoveryControlError(
                _CATEGORY_VALIDATION, _HOSTED_USE_RIGHTS_MESSAGE
            ) from exc
        approval_request = _hosted_use_approval_request(store, repo, revision, purpose)
        replayed = replayed or revision.revision_id != args.revision_id
    finally:
        store.close()
    return {
        "revision": _hosted_use_revision_dict(revision, purpose),
        "approval_request": approval_request,
        "replayed": replayed,
    }


def _validate_hosted_use_parent(
    repo: ArtifactRepository, artifact_id: str, parent_revision_id: str | None
) -> None:
    if parent_revision_id is None:
        return
    try:
        current = repo.current_revision(artifact_id)
    except (RecordValidationError, ValueError) as exc:
        raise DiscoveryControlError(
            _CATEGORY_VALIDATION, _HOSTED_USE_PARENT_MESSAGE
        ) from exc
    if current is None or current.revision_id != parent_revision_id:
        raise DiscoveryControlError(_CATEGORY_VALIDATION, _HOSTED_USE_PARENT_MESSAGE)


def discovery_lead_run(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``discovery lead run`` -- submit one scoped hosted lead-proposal job.

    The batch ref and request id are validated locally; ``--credential-env`` must
    name the one approved variable, and the credential **value** is never accepted
    here. The CLI ensures a worker (which inherits this process's environment) and
    submits the job; it never POSTs, never restarts a user's worker, and warns
    when the named key is absent so the operator can configure the worker's
    environment. Only the queued admission is returned.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    if args.credential_env != worker_discovery.LEAD_CREDENTIAL_ENV:
        raise JobControlError(_CATEGORY_VALIDATION, _CREDENTIAL_ENV_MESSAGE)
    parameters = {
        "batch_ref": _revision_ref(
            project_id, args.batch_artifact_id, args.batch_revision_id
        ),
        "credential_env": worker_discovery.LEAD_CREDENTIAL_ENV,
    }
    result = _submit(
        ctx,
        mode=worker_discovery.MODE_LEAD,
        parameters=parameters,
        request_id=request_id,
        project_id=project_id,
        timeout=timeout,
    )
    result["key_value_accepted"] = False
    # The worker reads the named variable from its own environment at dispatch.
    # A worker started by this command inherits this environment (existing
    # process semantics); an already-running worker keeps its own. Never start a
    # second worker or restart the user's worker to inject a credential.
    if not os.environ.get(worker_discovery.LEAD_CREDENTIAL_ENV):
        result["configuration_required"] = True
        result["notice"] = _MISSING_KEY_NOTICE
    else:
        result["configuration_required"] = False
    return result


# --------------------------------------------------------------------------- #
# dispatch (addon)
# --------------------------------------------------------------------------- #
#: The addon's nested leaf-command map: group -> command -> handler.
DISCOVERY_JOB_COMMANDS = {
    "search": {
        "run": discovery_search_run,
        "seed": discovery_search_seed,
        "import": discovery_search_import,
    },
    "expand": {
        "run": discovery_expand_run,
        "show": discovery_expand_show,
    },
    "lead": {
        "configure": discovery_lead_configure,
        "run": discovery_lead_run,
    },
}

#: The addon's nested argparse ``dest`` names, one per group.
DISCOVERY_JOB_LEAF_DESTS = {
    "search": "discovery_search_command",
    "expand": "discovery_expand_command",
    "lead": "discovery_lead_command",
}


def dispatch_discovery_jobs(args: argparse.Namespace) -> Any:
    """Route one parsed addon invocation to its handler, or return ``None``.

    ``None`` means the group/leaf is not an addon command (which ``argparse``
    already prevents); the caller turns that into a usage error.
    """
    group = DISCOVERY_JOB_COMMANDS.get(getattr(args, "discovery_command", None))
    if group is None:
        return None
    leaf = getattr(args, DISCOVERY_JOB_LEAF_DESTS[args.discovery_command], None)
    return group.get(leaf)
