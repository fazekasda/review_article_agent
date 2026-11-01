"""Application-boundary **screening runner** bound to the decision ledger.

Authority
---------

* ``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("screening classifier
  (Jev via ``runtime/jev.py`` ... bound to the existing decision ledger in
  ``storage/decision_intents.py``/``decision_results.py``)"; "Reuse those gates
  [human hosted-use approval, rights, budget reserve] ... do not invent a
  parallel authorization path").
* ``docs/implementation/08-screening.md`` / ``docs/decisions/003``: a model
  response is an untrusted *observation*; it never auto-excludes, and a hosted
  transfer needs an out-of-band, route/purpose/revision-scoped authorization.

What this module **is**
-----------------------

:func:`execute_screening_case` runs **one** screening case through the direct Jev
decision path: it re-reads the screening config and parsing report, rebuilds the
exact request the screening workflow builds (:mod:`review_article_agent.screening.workflow`),
reuses the existing hosted-use gate/rights/token authorization, reserves a
``direct_call`` dispatch **before** the single POST, records the immutable
pre-dispatch intent, and commits the accepted result through
:class:`~review_article_agent.storage.decision_results.DecisionResultRepository`
atomically. An already-recorded ``request_id`` is observed, never re-sent; an
ambiguous outcome reconciles to ``unknown`` with the reserve retained.

The committed result is then loadable by the screening workflow as a
``{"decision_result_ref": {...}}`` case response, which records
``response_provenance: "verified_dispatch"`` (instead of the imported
``imported_unverified_dispatch``) after re-verifying the accepted result and the
exact request digest. Pilot/exclusion audit rules are unchanged: a Jev answer
never auto-excludes.

Every failure is a static, value-free :class:`ScreeningRunnerError` (or an
already typed adapter/dispatch/ledger error); no message echoes the API key or
the source text.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from ..discovery import lead_runner as _lead
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..generation import hosted_use as _hosted
from ..runtime import jev
from ..runtime.dispatch import RuntimeDispatchRepository
from ..runtime.roles import BoundedTask, ModelRef, RoleConfig, RoleConfiguration
from ..storage.agent_runtime import RuntimeRepository, validate_version_set
from ..storage.artifacts import ArtifactRepository
from ..storage.decision_intents import DecisionIntentRepository
from ..storage.decision_results import DecisionResultRepository
from . import workflow as _workflow

__all__ = [
    "SCREENING_STAGE",
    "ADAPTER_REVISION",
    "PROMPT_VERSION",
    "TASK_SIZE_LIMIT",
    "ScreeningRunnerError",
    "ScreeningAuthorizationError",
    "ScreeningDocumentError",
    "ScreeningRightsError",
    "ScreeningBudgetError",
    "MAX_REQUEST_ID_CHARS",
    "execute_screening_case",
    "readback_screening_case",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The direct-call dispatch stage these dispatches are reserved under.
SCREENING_STAGE = "screening"

#: The adapter revision label recorded in the reported ``VersionSet``.
ADAPTER_REVISION = "screening.jev.v1"

#: The prompt/rubric revision label recorded on the intent.
PROMPT_VERSION = "screening.jev.v1"

#: Bounded task size.
TASK_SIZE_LIMIT = 262_144

#: Conservative upper bound on a per-window request id. The case request id
#: carries a ``-wNN`` suffix per window; refusing an over-long id **before** any
#: reserve keeps every window's opaque identifier bounded (mirrors the
#: acquisition request-id convention) and never truncates it.
MAX_REQUEST_ID_CHARS = 200

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


# --------------------------------------------------------------------------- #
# Errors (static messages; never echo input)
# --------------------------------------------------------------------------- #
class ScreeningRunnerError(ReviewAgentError):
    """A screening run failed before any dispatch was issued."""


class ScreeningAuthorizationError(ScreeningRunnerError):
    """The execution token or the exact hosted-use gate is not active."""


class ScreeningDocumentError(ScreeningRunnerError):
    """The hosted-use document is malformed, mismatched, or superseded."""


class ScreeningRightsError(ScreeningRunnerError):
    """A pinned source's rights record does not permit the hosted transfer."""


class ScreeningBudgetError(ScreeningRunnerError):
    """The per-role budget cap would be exceeded; nothing was dispatched."""


# --------------------------------------------------------------------------- #
# Small static helpers
# --------------------------------------------------------------------------- #
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_identifier(value: object, field: str) -> str:
    try:
        return validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise ScreeningRunnerError(f"{field}: must be a valid opaque identifier") from exc


def _require_same_project_ref(store, value: object, field: str) -> dict:
    try:
        return validate_ref(value, store.project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise ScreeningRunnerError(
            f"{field}: must be an exact same-project RefRevision"
        ) from exc


def _assert_token_active(registry, store, activation_token: str) -> None:
    try:
        _lead._assert_token_active(registry, store, activation_token)
    except _lead.HostedUseAuthorizationError as exc:
        raise ScreeningAuthorizationError(str(exc)) from exc


def _verify_hosted_rights(store, ref) -> None:
    try:
        _lead._verify_hosted_rights(store, ref)
    except _lead.HostedUseRightsError as exc:
        raise ScreeningRightsError(str(exc)) from exc


def _require_hosted_use(store, ref, *, role: str) -> dict:
    try:
        return _hosted.require_hosted_use(store, ref, role=role)
    except _hosted.GenerationHostedUseAuthorizationError as exc:
        raise ScreeningAuthorizationError(str(exc)) from exc
    except _hosted.GenerationHostedUseError as exc:
        raise ScreeningDocumentError(str(exc)) from exc


def _rights_record(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """A minimal valid, storage-allowed rights record for a runner-owned ref."""
    subject = {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    operations = {op: "allowed" for op in _OPS}
    basis = {
        op: {
            "kind": "public_domain",
            "evidence": {"ref": "lic:pd-screening", "provenance": "publisher:pd-list"},
        }
        for op in _OPS
    }
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": subject,
        "operations": operations,
        "basis": basis,
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


# --------------------------------------------------------------------------- #
# Per-question definition artifacts (for the immutable pre-dispatch intent)
# --------------------------------------------------------------------------- #
def _ensure_revision(store, artifacts, artifact_id, revision_id, content: bytes) -> dict:
    """Idempotently store one immutable revision with runner-owned rights."""
    artifacts.create_artifact(artifact_id, kind="report", created_by="user")
    revision = artifacts.import_revision(
        artifact_id=artifact_id,
        revision_id=revision_id,
        content=content,
        origin_locator="screening:local",
        rights=_rights_record(store.project_id, artifact_id, revision_id),
        acquired_by="user",
    )
    return {
        "project_id": store.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
    }


def _question_artifacts(store, config_ref, definitions):
    """Store the per-question ``QuestionDef``/criteria refs the intent needs.

    A predicate question carries no explicit criteria, so the criteria revision
    is the canonical JSON ``null`` (the same default the workflow's
    ``questions(config)`` produces). Both revisions are content-addressed by the
    config ref and question id, so a replay reuses them idempotently.
    """
    artifacts = ArtifactRepository(store)
    out = {}
    for question in definitions:
        seed = _sha256_hex(canonical_json({"config": config_ref, "q": question.question_id}))[:24]
        crit_id = f"screening-crit-{seed}"
        crit_rev = f"{crit_id}-r1"
        crit_ref = _ensure_revision(
            store, artifacts, crit_id, crit_rev, canonical_json(None))
        qdef_id = f"screening-qdef-{seed}"
        qdef_rev = f"{qdef_id}-r1"
        qdef_doc = {
            "question_id": question.question_id,
            "question_kind": question.question_kind,
            "instructions": question.instructions,
            "criteria_ref": crit_ref,
        }
        qdef_ref = _ensure_revision(
            store, artifacts, qdef_id, qdef_rev, canonical_json(qdef_doc))
        out[question.question_id] = (qdef_ref, crit_ref)
    return out


def _input_digest(project_id: str, refs) -> str:
    payload = [
        {
            "project_id": project_id,
            "artifact_id": ref["artifact_id"],
            "revision_id": ref["revision_id"],
            "digest": ref["digest"],
        }
        for ref in refs
    ]
    return _sha256_hex(canonical_json(payload))


def _versions(input_digest: str, request_sha256: str) -> dict:
    versions = {
        "context_kind": "model_derived",
        "input_digest": input_digest,
        "route": "direct_call",
        "model": {
            "state": "known",
            "ref": {"version": jev.MODEL, "digest": _sha256_hex(canonical_json({"model": jev.MODEL}))},
        },
        "prompt": {"state": "known", "ref": {"version": PROMPT_VERSION, "digest": request_sha256}},
        "rubric": {"state": "not_applicable"},
        "parser": {"state": "not_applicable"},
        "adapter": {
            "state": "known",
            "ref": {"version": ADAPTER_REVISION, "digest": _sha256_hex(canonical_json({"endpoint": jev.ENDPOINT}))},
        },
    }
    return validate_version_set(versions, consumed=("model", "prompt", "adapter"))


# --------------------------------------------------------------------------- #
# Per-role budget (reads the durable dispatch ledger)
# --------------------------------------------------------------------------- #
def _scope_tokens(task) -> dict:
    try:
        scope = json.loads(task.canonical.decode("utf-8")).get("scope", [])
    except (ValueError, UnicodeError):
        scope = []
    return {
        token.split("=", 1)[0]: token.split("=", 1)[1]
        for token in scope
        if isinstance(token, str) and "=" in token
    }


def _dispatch_exposure(record) -> Decimal:
    if record.cost_state in ("known", "estimated") and record.cost_amount_usd is not None:
        return Decimal(record.cost_amount_usd)
    if record.cost_state == "not_incurred":
        return Decimal("0")
    return Decimal(record.reserve_usd)


def _role_exposure(store, *, hosted_ref) -> Decimal:
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    hosted_token = f"{hosted_ref['artifact_id']}:{hosted_ref['revision_id']}"
    total = Decimal("0")
    for record in dispatch.list_dispatches():
        if record.stage != SCREENING_STAGE:
            continue
        task = runtime.get_task(record.task_id)
        if task is None:
            continue
        tokens = _scope_tokens(task)
        if tokens.get("role") != "screening" or tokens.get("hosted") != hosted_token:
            continue
        total += _dispatch_exposure(record)
    return total


def _check_budget(store, *, hosted_ref, budget_cap_usd: str, new_reserve: str) -> None:
    exposure = _role_exposure(store, hosted_ref=hosted_ref)
    if exposure + Decimal(new_reserve) > Decimal(budget_cap_usd):
        raise ScreeningBudgetError(
            "screening: the per-role budget cap for this hosted-use revision would "
            "be exceeded; refusing to dispatch."
        )


# --------------------------------------------------------------------------- #
# Readback / replay
# --------------------------------------------------------------------------- #
def _decision_result_ref(store, dispatch_id: str) -> dict:
    return {"project_id": store.project_id, "dispatch_id": dispatch_id}


def _readback(store, request_id: str, *, replayed: bool) -> dict:
    dispatch = RuntimeDispatchRepository(store)
    results = DecisionResultRepository(store)
    record = dispatch.get_dispatch_by_request(request_id)
    mapping = {
        "request_id": request_id,
        "dispatch_id": None,
        "result_id": None,
        "status": None,
        "cost": {"state": None, "amount_usd": None},
        "decision_result_ref": None,
        "replayed": replayed,
    }
    if record is None:
        return mapping
    mapping["dispatch_id"] = record.dispatch_id
    mapping["status"] = record.state
    mapping["cost"] = {"state": record.cost_state, "amount_usd": record.cost_amount_usd}
    observation = results.get_result(record.dispatch_id)
    # Only an accepted, completed result exposes a loadable reference: an inert
    # or ambiguous observation (unknown dispatch, no accepted triple) is not a
    # verified response and must not look like one.
    if (observation is not None and record.state == "completed"
            and record.result_ref == observation.result_id):
        mapping["result_id"] = observation.result_id
        mapping["decision_result_ref"] = _decision_result_ref(store, record.dispatch_id)
    return mapping


def _window_request_ids(request_id: str, count: int) -> list[str]:
    """The ordered per-window request ids ``"{request_id}-wNN"`` (1-based).

    Every id is validated as an opaque identifier and length-bounded **before**
    any reserve, so an over-long or unrepresentable case id is refused without a
    dispatch (never truncated).
    """
    ids = []
    for k in range(1, count + 1):
        window_id = f"{request_id}-w{k:02d}"
        if len(window_id) > MAX_REQUEST_ID_CHARS:
            raise ScreeningRunnerError(
                "screening: the case request id is too long to carry a per-window "
                "suffix; refusing to dispatch."
            )
        try:
            validate_identifier(window_id, "request_id")
        except (RecordValidationError, TypeError, ValueError) as exc:
            raise ScreeningRunnerError(
                "screening: the case request id cannot form a valid per-window id; "
                "refusing to dispatch."
            ) from exc
        ids.append(window_id)
    return ids


def _window_texts(definitions, context):
    """The ordered window texts for one case context (fail closed on oversize)."""
    try:
        return _workflow.case_windows(definitions, context)
    except jev.JevError as exc:
        raise ScreeningRunnerError(
            "screening: the article could not be split into request-bounded windows "
            "within the Jev limit; refusing to dispatch."
        ) from exc


def _plan_window(definitions, window: str):
    """Build and validate one window's dispatch plan (sends nothing)."""
    try:
        request = jev.build_request(definitions, window)
        body = canonical_json(request)
        bound = jev.estimate_cost_bound(len(body))
        return jev.plan_dispatch(
            definitions, window, reserve_usd=bound["cost_upper_bound_usd"],
            server_cap=False,
        )
    except jev.JevError as exc:
        raise ScreeningRunnerError(
            "screening: a window request could not be planned within the Jev bound; "
            "refusing to dispatch."
        ) from exc


def _mark_definitively_not_sent(store, dispatch_id: str) -> None:
    """Resolve a reserved-then-revalidation-failed dispatch to a definitive pre-send
    failure.

    The dispatch was ``mark_submitted`` but the immediate pre-POST revalidation
    failed, so no request was issued and nothing was billed. The existing
    transaction-owned primitives record exactly that: the state becomes the
    terminal ``failed`` and the cost becomes ``not_incurred`` (the reserve is
    released). ``mark_failed`` first, then settle, because a ``submitted`` row may
    never carry ``not_incurred``.
    """
    dispatch = RuntimeDispatchRepository(store)
    with store.transaction():
        dispatch._mark_failed_in_transaction(dispatch_id)
        dispatch._settle_cost_in_transaction(
            dispatch_id, cost_state="not_incurred", allow_definitive_not_incurred=True
        )


def _compose_case_result(store, request_id: str, window_ids, window_results) -> dict:
    """Reduce the ordered per-window readbacks to the case-level safe mapping.

    Only safe IDs/status/cost are exposed. The overall ``status`` is ``completed``
    only when **every** window completed; otherwise it reflects the first
    unresolved/failed window (an ambiguous window is ``unknown``). The ordered
    ``decision_result_refs`` list is present only when every window completed, and
    the legacy single ``decision_result_ref`` is present only for a one-window
    case. The aggregate cost is ``known`` only when every attempted window's cost
    is known; a later window that was never sent is simply absent from ``windows``.
    """
    window_count = len(window_ids)
    statuses = [rb.get("status") for rb in window_results]
    all_completed = (len(window_results) == window_count
                     and all(status == "completed" for status in statuses))
    if all_completed:
        overall = "completed"
    elif any(status == "unknown" for status in statuses):
        overall = "unknown"
    elif any(status == "failed" for status in statuses):
        overall = "failed"
    else:
        overall = statuses[-1] if statuses else "reserved"
    refs = ([rb["decision_result_ref"] for rb in window_results]
            if all_completed else None)
    total = Decimal("0")
    known = True
    for rb in window_results:
        cost = rb.get("cost") or {}
        if cost.get("state") in ("known", "estimated") and cost.get("amount_usd") is not None:
            total += Decimal(cost["amount_usd"])
        else:
            known = False
    windows = [
        {
            "request_id": rb["request_id"],
            "dispatch_id": rb["dispatch_id"],
            "status": rb["status"],
            "cost": rb["cost"],
            "decision_result_ref": rb["decision_result_ref"],
            "result_id": rb["result_id"],
            "replayed": rb["replayed"],
        }
        for rb in window_results
    ]
    return {
        "request_id": request_id,
        "status": overall,
        "window_count": window_count,
        "windowing_version": _workflow.WINDOWING_VERSION,
        "windows": windows,
        "decision_result_refs": refs,
        "decision_result_ref": refs[0] if (all_completed and window_count == 1) else None,
        # Back-compat for a one-window case: the window's own ids.
        "dispatch_id": window_results[0]["dispatch_id"] if window_results else None,
        "result_id": window_results[0]["result_id"] if window_results else None,
        "cost": {"state": "known" if known else "unknown",
                 "amount_usd": str(total) if known else None},
        "replayed": bool(window_results) and all(rb.get("replayed") for rb in window_results),
    }


def readback_screening_case(store, *, request_id, config_ref, report_ref, passage_id) -> dict | None:
    """Prove a screening case from the durable dispatch ledger with **zero** POST.

    The per-window request ids are recomputed from the *current* inputs via the
    same :func:`case_windows` split the runner uses, so a missing/extra window is
    never silently accepted (a window with no completed dispatch leaves the case
    not-``completed``). A pre-windowing job that recorded the bare ``request_id``
    for a one-window case is still read (legacy back-compat). Returns ``None`` only
    when the window count cannot be recomputed and no legacy dispatch exists.
    """
    request_id = _require_identifier(request_id, "request_id")
    composed = None
    try:
        screening = _workflow.ScreeningRepository(store)
        config = screening._config(config_ref)
        definitions = _workflow.questions(config)
        context = _workflow.case_context(
            screening.parsing, report_ref, passage_id, questions=definitions
        )
        windows = _window_texts(definitions, context)
        window_ids = _window_request_ids(request_id, len(windows))
    except (ReviewAgentError, RecordValidationError, TypeError, ValueError):
        window_ids = None
    if window_ids is not None:
        window_results = []
        for window_id in window_ids:
            readback = _readback(store, window_id, replayed=True)
            window_results.append(readback)
            if readback["status"] != "completed":
                break
        composed = _compose_case_result(store, request_id, window_ids, window_results)
        if composed["status"] == "completed":
            return composed
    # Legacy back-compat: a pre-windowing job recorded the bare request id for a
    # one-window case. Never claims completion for a multi-window case.
    legacy = _readback(store, request_id, replayed=True)
    if legacy["status"] == "completed":
        return _compose_case_result(store, request_id, [request_id], [legacy])
    return composed


# --------------------------------------------------------------------------- #
# Public runner
# --------------------------------------------------------------------------- #
def execute_screening_case(
    store,
    *,
    registry,
    activation_token: str,
    config_ref,
    report_ref,
    passage_id,
    request_id: str,
    hosted_ref,
    api_key: str,
    transport=None,
) -> dict:
    """Run **one** screening case through the direct Jev decision path.

    The whole parsed article is split into deterministic, request-bounded windows
    (:func:`review_article_agent.screening.workflow.case_windows`) and **one**
    direct-Jev dispatch is issued per window (request id ``"{request_id}-wNN"``).
    Every window is planned, cost-bounded, and budget-checked before the first
    reserve; each window then reserves its own task/dispatch/intent **before** its
    one POST and commits its accepted result through the decision ledger. An
    already-recorded window is observed without a second POST, and processing
    stops on the first ambiguous/unknown window (later windows are not sent). The
    returned mapping carries only safe IDs/status — never the API key or the
    source text.
    """
    request_id = _require_identifier(request_id, "request_id")
    if not isinstance(api_key, str) or api_key == "":
        raise ScreeningRunnerError("api_key: a non-empty credential is required")
    _assert_token_active(registry, store, activation_token)

    hosted_ref = _require_same_project_ref(store, hosted_ref, "hosted_ref")
    document = _require_hosted_use(store, hosted_ref, role="screening")
    budget_cap_usd = document["payload"]["budget_cap_usd"]

    screening = _workflow.ScreeningRepository(store)
    config = screening._config(config_ref)
    definitions = _workflow.questions(config)

    report_ref = _require_same_project_ref(store, report_ref, "report_ref")
    context = _workflow.case_context(
        screening.parsing, report_ref, passage_id, questions=definitions
    )
    # A not-ready context (including an article whose header region alone cannot
    # fit the Jev bound) is a pre-reserve refusal: nothing is reserved or sent.
    if not context["ready"]:
        raise ScreeningRunnerError(
            "screening: the parsing report is not ready for screening; refusing to "
            "dispatch."
        )
    _verify_hosted_rights(store, context["report"]["source_ref"])

    # -- plan/validate every window before reserving anything ---------------- #
    windows = _window_texts(definitions, context)
    window_ids = _window_request_ids(request_id, len(windows))
    plans = [_plan_window(definitions, window) for window in windows]

    question_artifacts = _question_artifacts(store, config_ref, definitions)
    input_refs = []
    qdef_refs = {}
    crit_refs = {}
    for question in definitions:
        qdef_ref, crit_ref = question_artifacts[question.question_id]
        qdef_refs[question.question_id] = qdef_ref
        crit_refs[question.question_id] = crit_ref
        for ref in (qdef_ref, crit_ref):
            content = ArtifactRepository(store).read_content(ref["artifact_id"], ref["revision_id"])
            input_refs.append({**ref, "digest": _sha256_hex(content)})

    input_digest = _input_digest(store.project_id, input_refs)
    per_window_versions = []
    for plan in plans:
        version_map = _versions(input_digest, plan.request_sha256)
        version_digest = _sha256_hex(canonical_json(version_map))
        bound_evidence_digest = _sha256_hex(
            canonical_json({"request_sha256": plan.request_sha256,
                            "reserve_usd": plan.reserve_usd}))
        per_window_versions.append((version_map, version_digest, bound_evidence_digest))

    model = ModelRef(provider_id="openrouter", model_id=jev.MODEL, variant="direct")
    role_config = RoleConfig(role="screening", model=model, steps=1)
    role_set = RoleConfiguration(roles=(role_config,))
    bounded = BoundedTask(
        project_id=store.project_id,
        role="screening",
        target=dict(report_ref),
        input_refs=input_refs,
        scope=[
            SCREENING_STAGE,
            "role=screening",
            f"hosted={hosted_ref['artifact_id']}:{hosted_ref['revision_id']}",
        ],
        prompt_version=PROMPT_VERSION,
        size_limit=TASK_SIZE_LIMIT,
        step_limit=1,
        role_config=role_config,
    )

    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    intents = DecisionIntentRepository(store)
    results = DecisionResultRepository(store)

    # -- the total reserve of all not-yet-dispatched windows must fit the cap -- #
    outstanding = Decimal("0")
    for window_id, plan in zip(window_ids, plans):
        if dispatch.get_dispatch_by_request(window_id) is None:
            outstanding += Decimal(plan.reserve_usd)
    if outstanding > 0:
        _check_budget(
            store, hosted_ref=hosted_ref, budget_cap_usd=budget_cap_usd,
            new_reserve=str(outstanding),
        )

    # -- dispatch one window at a time, sequentially -------------------------- #
    window_results = []
    for window_id, plan, versions in zip(window_ids, plans, per_window_versions):
        version_map, version_digest, bound_evidence_digest = versions
        if dispatch.get_dispatch_by_request(window_id) is not None:
            readback = _readback(store, window_id, replayed=True)
            window_results.append(readback)
            if readback["status"] != "completed":
                break
            continue

        task_record = runtime.record_task(
            bounded, request_id=window_id, configuration=role_set)

        with registry.execution_write_guard(activation_token, store.project_id):
            if dispatch.get_dispatch_by_request(window_id) is not None:
                window_results.append(_readback(store, window_id, replayed=True))
                break
            # The existing per-dispatch check is retained in addition to the
            # whole-case pre-reserve check above.
            _check_budget(
                store, hosted_ref=hosted_ref, budget_cap_usd=budget_cap_usd,
                new_reserve=plan.reserve_usd,
            )
            dispatch_record = dispatch.reserve_dispatch(
                task_record,
                request_id=window_id,
                route="direct_call",
                stage=SCREENING_STAGE,
                input_digest=input_digest,
                version_digest=version_digest,
                reserve_usd=plan.reserve_usd,
                bound_evidence_digest=bound_evidence_digest,
            )
            intents.record_intent(
                task_record,
                dispatch_record,
                plan,
                question_def_ref=qdef_refs,
                criteria_ref=crit_refs,
                versions=version_map,
            )
            dispatch_record = dispatch.mark_submitted(dispatch_record.dispatch_id)

        # The registry lock is released before any network I/O: revalidate the
        # exact gate, the source rights, and the active token immediately before
        # the POST. A failure here means no request was issued, so the reserved
        # dispatch is resolved to a definitive pre-send failure (never left
        # ``submitted`` with an outstanding reserve).
        try:
            _require_hosted_use(store, hosted_ref, role="screening")
            _verify_hosted_rights(store, context["report"]["source_ref"])
            _assert_token_active(registry, store, activation_token)
        except ScreeningRunnerError:
            _mark_definitively_not_sent(store, dispatch_record.dispatch_id)
            raise

        result = jev.dispatch_plan(plan, api_key=api_key, transport=transport)

        with registry.execution_write_guard(
            activation_token, store.project_id, allow_draining=True
        ):
            results.commit_result(
                dispatch_record.dispatch_id, result, fence=dispatch_record.current_fence)

        readback = _readback(store, window_id, replayed=False)
        window_results.append(readback)
        if readback["status"] != "completed":
            # An ambiguous/unknown window outcome: stop; later windows are not sent.
            break

    return _compose_case_result(store, request_id, window_ids, window_results)
