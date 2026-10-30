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
    "execute_screening_case",
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

    The screening config and parsing report are re-read, the exact Jev request
    is rebuilt, the hosted-use gate/rights/token are revalidated, the per-role
    budget is enforced, and a ``direct_call`` dispatch (stage ``screening``) is
    reserved **before** the one POST. The accepted result is committed through
    the decision ledger atomically. An already-recorded ``request_id`` is
    observed without a second POST; an ambiguous outcome retains its reserve and
    is never re-sent. The returned mapping carries only safe IDs/status — never
    the API key or the source text.
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
    context = _workflow.case_context(screening.parsing, report_ref, passage_id)
    text = context["text"]
    _verify_hosted_rights(store, context["report"]["source_ref"])

    request = jev.build_request(definitions, text)
    body = canonical_json(request)
    bound = jev.estimate_cost_bound(len(body))
    reserve_usd = bound["cost_upper_bound_usd"]
    plan = jev.plan_dispatch(definitions, text, reserve_usd=reserve_usd, server_cap=False)

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
    version_map = _versions(input_digest, plan.request_sha256)
    version_digest = _sha256_hex(canonical_json(version_map))
    bound_evidence_digest = _sha256_hex(
        canonical_json({"request_sha256": plan.request_sha256, "reserve_usd": reserve_usd}))

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
    task_record = runtime.record_task(bounded, request_id=request_id, configuration=role_set)

    with registry.execution_write_guard(activation_token, store.project_id):
        if dispatch.get_dispatch_by_request(request_id) is not None:
            return _readback(store, request_id, replayed=True)
        _check_budget(
            store, hosted_ref=hosted_ref, budget_cap_usd=budget_cap_usd,
            new_reserve=plan.reserve_usd,
        )
        dispatch_record = dispatch.reserve_dispatch(
            task_record,
            request_id=request_id,
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

    # The registry lock is released before any network I/O: revalidate the exact
    # gate, the source rights, and the active token immediately before the POST.
    _require_hosted_use(store, hosted_ref, role="screening")
    _verify_hosted_rights(store, context["report"]["source_ref"])
    _assert_token_active(registry, store, activation_token)

    result = jev.dispatch_plan(plan, api_key=api_key, transport=transport)

    with registry.execution_write_guard(
        activation_token, store.project_id, allow_draining=True
    ):
        results.commit_result(
            dispatch_record.dispatch_id, result, fence=dispatch_record.current_fence)
    return _readback(store, request_id, replayed=False)
