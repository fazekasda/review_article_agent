"""Application-boundary **generation runner** (kickoff 16 §5).

Authority
---------

* ``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("One real model port":
  every model-backed role runs through one small adapter; "Reuse those gates
  [human hosted-use approval, rights, budget reserve] ; do not invent a parallel
  authorization path"; "Model per role is configurable").
* ``docs/contracts/02-gates-and-runtime.md`` §5.6 (reserve before dispatch; an
  unknown reserve is retained and never silently released) and §6.3 (exact-decimal
  direct-call cost, accounted separately from OpenCode session statistics).

This is the *application* boundary the pure
:mod:`review_article_agent.runtime.generation` adapter deliberately is not: it
owns the exact hosted-use gate, the per-role budget, the durable dispatch ledger,
the single POST, and the atomic commit. Model output is **data only**; it is never
authority.

What this module **is**
-----------------------

* :func:`execute_generation_task` — the single-shot runner. It re-derives every
  byte from the store, revalidates the exact current ``generation_hosted_use``
  revision and its named ``batch``/``generation.hosted_use.<role>`` gate, verifies
  every input revision's intact bytes, verifies every ORIGINAL source's
  hosted-transfer rights, enforces the per-role budget cap, reserves a
  ``direct_call`` dispatch (stage ``generation``) **before** the one POST, and
  commits the non-authoritative proposal together with the dispatch's accepted
  result and settled cost in one nested transaction. An already-recorded
  ``request_id`` is observed, never re-sent.
* :func:`get_generation_outcome` — a read-only readback of a durable logical
  request.

It reuses, by import, the tested helpers of
:mod:`review_article_agent.discovery.lead_runner` (activation-token check,
hosted-transfer rights check, byte-checked revision reads) and the
:mod:`review_article_agent.generation.hosted_use` require seam, so the gate/rights
logic is never duplicated.

Every failure is a static, value-free :class:`GenerationRunnerError` (or an
already typed dispatch/runtime error); no message ever echoes the API key, the
material, or provider text.
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
from ..runtime import generation as runtime_generation
from ..runtime.dispatch import RuntimeDispatchRepository
from ..runtime.lead import LeadConfig
from ..runtime.roles import (
    BoundedTask,
    ModelRef,
    RoleConfig,
    RoleConfiguration,
)
from ..storage.agent_runtime import RuntimeRepository, validate_version_set
from ..storage.artifacts import ArtifactRepository, sha256_digest
from . import hosted_use as _hosted

__all__ = [
    "GENERATION_STAGE",
    "ADAPTER_REVISION",
    "MAX_IDENTIFIER_LEN",
    "TASK_SIZE_LIMIT",
    "GenerationRunnerError",
    "GenerationAuthorizationError",
    "GenerationDocumentError",
    "GenerationRightsError",
    "GenerationBudgetError",
    "execute_generation_task",
    "get_generation_outcome",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The direct-call dispatch stage these dispatches are reserved under.
GENERATION_STAGE = "generation"

#: The adapter revision label recorded in the reported ``VersionSet``.
ADAPTER_REVISION = "generation.direct.v1"

#: Bounded sizes.
MAX_IDENTIFIER_LEN = 512
TASK_SIZE_LIMIT = 262_144

#: The non-authoritative proposal result kinds recorded on the dispatch.
_RESULT_KIND_PROPOSAL = "generation_proposal"
_RESULT_KIND_REFUSED = "generation_refused"
_RESULT_KIND_PROTOCOL = "generation_protocol_error"
_RESULT_KIND_OVERSIZED = "generation_oversized_output"

#: A completed response too large to store as a bounded proposal is recorded as
#: this diagnostic outcome (metadata + cost preserved; no proposal text).
OUTCOME_OVERSIZED = "oversized_output"

#: The set of terminal diagnostic outcomes that are *accepted* (their metadata and
#: settled cost are recorded) but carry no proposal.
_DIAGNOSTIC_KINDS = {
    "refused": _RESULT_KIND_REFUSED,
    "protocol_error": _RESULT_KIND_PROTOCOL,
    OUTCOME_OVERSIZED: _RESULT_KIND_OVERSIZED,
}

_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_COST_STATES_WITH_AMOUNT = frozenset({"known", "estimated"})


# --------------------------------------------------------------------------- #
# Errors (static messages; never echo input)
# --------------------------------------------------------------------------- #
class GenerationRunnerError(ReviewAgentError):
    """A generation run failed before any dispatch was issued."""


class GenerationAuthorizationError(GenerationRunnerError):
    """The execution token or the exact hosted-use gate is not active."""


class GenerationDocumentError(GenerationRunnerError):
    """The hosted-use document is malformed, mismatched, or superseded."""


class GenerationRightsError(GenerationRunnerError):
    """A pinned source's rights record does not permit the hosted transfer."""


class GenerationBudgetError(GenerationRunnerError):
    """The per-role budget cap would be exceeded; nothing was dispatched."""


# --------------------------------------------------------------------------- #
# Small static helpers
# --------------------------------------------------------------------------- #
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_role(role: object) -> str:
    if not isinstance(role, str) or role not in runtime_generation.ROLES:
        raise GenerationRunnerError("role: is not a recognized generation role")
    return role


def _require_identifier(value: object, field: str) -> str:
    try:
        normalized = validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise GenerationRunnerError(f"{field}: must be a valid opaque identifier") from exc
    if len(normalized) > MAX_IDENTIFIER_LEN:
        raise GenerationRunnerError(f"{field}: is too long")
    return normalized


def _require_nonempty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GenerationRunnerError(f"{field}: must be a non-empty string")
    return value


def _require_same_project_ref(store, value: object, field: str) -> dict:
    try:
        return validate_ref(value, store.project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise GenerationRunnerError(
            f"{field}: must be an exact same-project RefRevision"
        ) from exc


def _assert_token_active(registry, store, activation_token: str) -> None:
    try:
        _lead._assert_token_active(registry, store, activation_token)
    except _lead.HostedUseAuthorizationError as exc:
        raise GenerationAuthorizationError(str(exc)) from exc


def _verify_hosted_rights(store, ref) -> None:
    try:
        _lead._verify_hosted_rights(store, ref)
    except _lead.HostedUseRightsError as exc:
        raise GenerationRightsError(str(exc)) from exc


def _require_hosted_use(store, ref, *, role: str) -> dict:
    """The current, gate-approved hosted-use document (fail closed)."""
    try:
        return _hosted.require_hosted_use(store, ref, role=role)
    except _hosted.GenerationHostedUseAuthorizationError as exc:
        raise GenerationAuthorizationError(str(exc)) from exc
    except _hosted.GenerationHostedUseError as exc:
        raise GenerationDocumentError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Reads (byte-checked) + digests
# --------------------------------------------------------------------------- #
def _read_ref_digest(store, ref: Mapping[str, str]) -> str:
    """Read one exact revision's bytes (integrity checked); return its digest.

    The reference need only exist with intact bytes; the task boundary separately
    requires its live inputs to be the artifact's current revision.
    """
    repository = ArtifactRepository(store)
    revision = repository.get_revision(ref["artifact_id"], ref["revision_id"])
    if revision is None:
        raise GenerationRunnerError("ref: the requested revision does not exist in this project")
    content = repository.read_content(ref["artifact_id"], ref["revision_id"])
    if (
        sha256_digest(content) != revision.content_sha256
        or len(content) != revision.byte_size
    ):
        raise GenerationRunnerError("ref: the stored revision fails byte-integrity validation")
    digest = revision.content_sha256
    return digest[7:] if digest.startswith("sha256:") else digest


def _normalize_input_refs(store, input_refs: object) -> list[dict]:
    """Validate exact same-project input refs and bind each to its stored digest."""
    if not isinstance(input_refs, (list, tuple)):
        raise GenerationRunnerError("input_refs: must be a list of RefRevision objects")
    normalized: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(input_refs):
        field = f"input_refs[{index}]"
        ref = _require_same_project_ref(store, raw, field)
        key = (ref["artifact_id"], ref["revision_id"])
        if key in seen:
            raise GenerationRunnerError(f"{field}: duplicate reference")
        seen.add(key)
        normalized.append(
            {
                "project_id": store.project_id,
                "artifact_id": ref["artifact_id"],
                "revision_id": ref["revision_id"],
                "digest": _read_ref_digest(store, ref),
            }
        )
    return normalized


def _normalize_source_refs(store, source_refs: object) -> list[dict]:
    """Validate exact same-project source refs (empty is allowed)."""
    if not isinstance(source_refs, (list, tuple)):
        raise GenerationRunnerError("source_refs: must be a list of RefRevision objects")
    normalized: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(source_refs):
        field = f"source_refs[{index}]"
        ref = _require_same_project_ref(store, raw, field)
        key = (ref["artifact_id"], ref["revision_id"])
        if key in seen:
            raise GenerationRunnerError(f"{field}: duplicate reference")
        seen.add(key)
        normalized.append(ref)
    return normalized


def _input_digest(project_id: str, refs: list[dict]) -> str:
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


def _version_digest(role: str, prompt_version: str, config_map: Mapping[str, Any]) -> str:
    return _sha256_hex(
        canonical_json(
            {
                "model_id": config_map["model_id"],
                "upstream_provider": config_map["upstream_provider"],
                "instructions_revision": config_map["instructions_revision"],
                "max_output_tokens": config_map["max_output_tokens"],
                "context_tokens": config_map["context_tokens"],
                "role": role,
                "prompt_version": prompt_version,
                "endpoint": runtime_generation.ENDPOINT,
            }
        )
    )


def _reported_versions(
    input_digest: str, model_ref: str, prompt_version: str, request_sha256: str
) -> dict:
    """A validated C02 §6.1 ``VersionSet`` for a direct generation dispatch."""
    versions = {
        "context_kind": "model_derived",
        "input_digest": input_digest,
        "route": "direct_call",
        "model": {
            "state": "known",
            "ref": {
                "version": model_ref,
                "digest": _sha256_hex(canonical_json({"model": model_ref})),
            },
        },
        "prompt": {
            "state": "known",
            "ref": {"version": prompt_version, "digest": request_sha256},
        },
        "rubric": {"state": "not_applicable"},
        "parser": {"state": "not_applicable"},
        "adapter": {
            "state": "known",
            "ref": {
                "version": ADAPTER_REVISION,
                "digest": _sha256_hex(
                    canonical_json({"endpoint": runtime_generation.ENDPOINT})
                ),
            },
        },
    }
    return validate_version_set(versions, consumed=("model", "prompt", "adapter"))


# --------------------------------------------------------------------------- #
# Material serialisation
# --------------------------------------------------------------------------- #
def _material_text(material: object) -> str:
    """Serialise the caller's JSON-safe material into the untrusted ``text``."""
    if not isinstance(material, Mapping):
        raise GenerationRunnerError("material: must be a JSON-safe mapping")
    try:
        return canonical_json(dict(material)).decode("utf-8")
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise GenerationRunnerError("material: must be a JSON-safe mapping") from exc


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
    """This role's contribution: a settled known cost, else a still-held reserve.

    A definitively-failed (``not_incurred``) dispatch holds no reserve and no known
    cost, so it contributes nothing; a ``reserved``/``submitted``/``unknown``
    dispatch still holds its full reserve; a ``known``/``estimated`` dispatch
    contributes its settled amount.
    """
    if record.cost_state in _COST_STATES_WITH_AMOUNT and record.cost_amount_usd is not None:
        return Decimal(record.cost_amount_usd)
    if record.cost_state == "not_incurred":
        return Decimal("0")
    return Decimal(record.reserve_usd)


def _role_exposure(store, *, role: str, hosted_ref: Mapping[str, str]) -> Decimal:
    """Sum the exposure of this role's generation dispatches under this hosted ref."""
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    hosted_token = f"{hosted_ref['artifact_id']}:{hosted_ref['revision_id']}"
    total = Decimal("0")
    for record in dispatch.list_dispatches():
        if record.stage != GENERATION_STAGE:
            continue
        task = runtime.get_task(record.task_id)
        if task is None:
            continue
        tokens = _scope_tokens(task)
        if tokens.get("role") != role or tokens.get("hosted") != hosted_token:
            continue
        total += _dispatch_exposure(record)
    return total


def _check_budget(
    store, *, role: str, hosted_ref, budget_cap_usd: str, new_reserve: str
) -> None:
    exposure = _role_exposure(store, role=role, hosted_ref=hosted_ref)
    if exposure + Decimal(new_reserve) > Decimal(budget_cap_usd):
        raise GenerationBudgetError(
            "generation: the per-role budget cap for this hosted-use revision would "
            "be exceeded; refusing to dispatch."
        )


# --------------------------------------------------------------------------- #
# Outcome mapping / readback
# --------------------------------------------------------------------------- #
def _usage_mapping(result) -> dict | None:
    if result is None or result.usage is None:
        return None
    usage = result.usage
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _execution_metadata(
    *, dispatch_id: str, result, input_digest: str, model_ref: str, prompt_version: str
) -> dict:
    """The application-attached machine-observation metadata envelope (data only).

    Monetary cost is deliberately absent: the authoritative cost is the
    direct-dispatch ledger (C02 §6.3). The validated ``VersionSet`` is stored as its
    canonical JSON **string**, so the envelope carries no nested ``model`` /
    ``provider`` / ``role`` / ``prompt_version`` key that the role content envelope
    forbids; the string is re-parsed and re-validated on read.
    """
    versions = _reported_versions(
        input_digest, model_ref, prompt_version, result.request_sha256
    )
    return {
        "actual_model": result.actual_model,
        "upstream_provider": result.provider,
        "response_id": result.response_id,
        "usage": _usage_mapping(result),
        "version_set_json": canonical_json(versions).decode("utf-8"),
        "http_status": result.http_status,
        "request_sha256": result.request_sha256,
        "dispatch_id": dispatch_id,
        "no_automatic_retry": True,
    }


def _outcome_from_dispatch(record) -> str:
    state = record.state
    if state == "completed":
        return {
            _RESULT_KIND_PROPOSAL: "completed",
            _RESULT_KIND_REFUSED: "refused",
            _RESULT_KIND_PROTOCOL: "protocol_error",
            _RESULT_KIND_OVERSIZED: OUTCOME_OVERSIZED,
        }.get(record.result_kind, "completed")
    if state == "failed":
        return "failed"
    if state == "unknown":
        return "unknown"
    return state  # "reserved" / "submitted"


def _readback(store, request_id: str, *, replayed: bool) -> dict:
    """Build the durable outcome mapping from stored rows (no key, no material)."""
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    task = runtime.get_task_by_request(request_id)
    record = dispatch.get_dispatch_by_request(request_id)

    mapping = {
        "request_id": request_id,
        "role": None,
        "prompt_version": None,
        "outcome": None,
        "proposal": None,
        "dispatch_id": None,
        "cost": {"state": None, "amount_usd": None},
        "proposal_ref": None,
        "replayed": replayed,
    }
    if task is None:
        return mapping
    mapping["role"] = _scope_tokens(task).get("role")
    mapping["prompt_version"] = task.prompt_version
    if record is None:
        return mapping

    outcome = _outcome_from_dispatch(record)
    mapping["outcome"] = outcome
    mapping["dispatch_id"] = record.dispatch_id
    mapping["cost"] = {
        "state": record.cost_state,
        "amount_usd": record.cost_amount_usd,
    }

    for proposal in runtime.proposals_for_task(task.task_id):
        try:
            content = json.loads(proposal.canonical.decode("utf-8")).get("content")
        except (ValueError, UnicodeError):
            continue
        if not isinstance(content, dict):
            continue
        metadata = content.get("execution_metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("dispatch_id") != record.dispatch_id:
            continue
        if record.result_ref is not None and (
            proposal.record_id != record.result_ref
            or proposal.stored_digest != record.result_digest
        ):
            raise GenerationRunnerError(
                "proposal: the stored proposal does not match the accepted dispatch "
                "result; refusing to report it"
            )
        mapping["proposal_ref"] = {
            "project_id": store.project_id,
            "record_id": proposal.record_id,
            "proposal_id": proposal.proposal_id,
        }
        if outcome == "completed":
            proposal_json = content.get("proposal")
            if isinstance(proposal_json, dict):
                mapping["proposal"] = proposal_json
        break
    return mapping


def get_generation_outcome(store, request_id: str) -> dict | None:
    """Read back a durable logical generation request (read-only).

    Returns the same safe mapping :func:`execute_generation_task` returns, or
    ``None`` when nothing was ever recorded for ``request_id``. It never issues a
    call and never includes an API key or material.
    """
    request_id = _require_identifier(request_id, "request_id")
    if RuntimeRepository(store).get_task_by_request(request_id) is None:
        return None
    return _readback(store, request_id, replayed=False)


# --------------------------------------------------------------------------- #
# Atomic result commit
# --------------------------------------------------------------------------- #
def _commit_result(
    store,
    *,
    runtime,
    dispatch,
    task_record,
    dispatch_record,
    role_set,
    plan,
    role,
    prompt_version,
    input_digest,
    result,
) -> dict:
    """Commit the proposal, the dispatch result and the settled cost atomically."""
    outcome = result.outcome
    model_ref = plan.config.model_id
    metadata = _execution_metadata(
        dispatch_id=dispatch_record.dispatch_id,
        result=result,
        input_digest=input_digest,
        model_ref=model_ref,
        prompt_version=prompt_version,
    )
    proposal_json = result.proposal_json if outcome == "completed" else None
    content = {"outcome": outcome, "proposal": proposal_json, "execution_metadata": metadata}
    if outcome == "completed" and len(canonical_json(content)) > TASK_SIZE_LIMIT:
        # The completed output cannot be stored as a bounded proposal: record an
        # oversized-output diagnostic instead (metadata and cost preserved).
        outcome = OUTCOME_OVERSIZED
        content = {
            "outcome": OUTCOME_OVERSIZED,
            "proposal": None,
            "execution_metadata": metadata,
        }

    with store.transaction():
        proposal = runtime._record_proposal_in_transaction(
            task_record, {"content": content}, configuration=role_set
        )
        if outcome == "completed":
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(
                dispatch_record.dispatch_id, cost_state=cost_state, amount_usd=amount
            )
            dispatch._accept_result_in_transaction(
                dispatch_record.dispatch_id,
                fence=dispatch_record.current_fence,
                result_kind=_RESULT_KIND_PROPOSAL,
                result_ref=proposal.record_id,
                result_digest=proposal.stored_digest,
            )
        elif outcome == "failed":
            dispatch._settle_cost_in_transaction(
                dispatch_record.dispatch_id,
                cost_state="not_incurred",
                allow_definitive_not_incurred=True,
            )
            dispatch._mark_failed_in_transaction(
                dispatch_record.dispatch_id, definitive_rejection=True
            )
        elif outcome == "unknown":
            # Ambiguous: retain the reserve and the remote occupancy; never a
            # definitive failure and never a retry.
            dispatch._settle_cost_in_transaction(
                dispatch_record.dispatch_id, cost_state="unknown"
            )
            dispatch._recover_in_transaction(dispatch_record.dispatch_id)
        else:  # refused / protocol_error / oversized_output (accepted diagnostics)
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(
                dispatch_record.dispatch_id, cost_state=cost_state, amount_usd=amount
            )
            dispatch._accept_result_in_transaction(
                dispatch_record.dispatch_id,
                fence=dispatch_record.current_fence,
                result_kind=_DIAGNOSTIC_KINDS[outcome],
                result_ref=proposal.record_id,
                result_digest=proposal.stored_digest,
            )
    return _readback(store, task_record.request_id, replayed=False)


# --------------------------------------------------------------------------- #
# Public runner
# --------------------------------------------------------------------------- #
def execute_generation_task(
    store,
    *,
    registry,
    activation_token: str,
    role: str,
    request_id: str,
    hosted_ref,
    prompt_version: str,
    instructions: str,
    material,
    input_refs,
    source_refs,
    api_key: str,
    transport=None,
) -> dict:
    """Run **one** bounded generation request (single-shot, atomic commit).

    The exact hosted-use document and its named gate are re-read, the execution
    token is revalidated, every input revision's bytes and every original source's
    hosted-transfer rights are checked, and the per-role budget cap is enforced
    **before** a ``direct_call`` dispatch (stage ``generation``) is reserved. The
    registry lock is released before the single POST and the gate/token are
    revalidated immediately before it. An already-recorded ``request_id`` is
    observed without a second POST; an ambiguous outcome retains its reserve and is
    never re-sent. The returned mapping carries only safe IDs/status — never the
    API key or the material.
    """
    request_id = _require_identifier(request_id, "request_id")
    role = _require_role(role)
    if not isinstance(api_key, str) or api_key == "":
        raise GenerationRunnerError("api_key: a non-empty credential is required")
    prompt_version = _require_nonempty_str(prompt_version, "prompt_version")
    instructions = _require_nonempty_str(instructions, "instructions")
    _assert_token_active(registry, store, activation_token)

    hosted_ref = _require_same_project_ref(store, hosted_ref, "hosted_ref")
    document = _require_hosted_use(store, hosted_ref, role=role)
    payload = document["payload"]
    config_map = payload["config"]
    budget_cap_usd = payload["budget_cap_usd"]
    try:
        config = LeadConfig.from_mapping(config_map)
    except (ReviewAgentError, RecordValidationError, TypeError, ValueError) as exc:
        raise GenerationDocumentError(
            "hosted-use: the configured model is not a usable hosted-use configuration"
        ) from exc

    normalized_inputs = _normalize_input_refs(store, input_refs)
    input_digest = _input_digest(store.project_id, normalized_inputs)

    normalized_sources = _normalize_source_refs(store, source_refs)
    for source in normalized_sources:
        _verify_hosted_rights(store, source)

    text = _material_text(material)
    plan = runtime_generation.plan_generation(
        config,
        role=role,
        prompt_version=prompt_version,
        instructions=instructions,
        text=text,
    )

    try:
        model = ModelRef(
            provider_id="openrouter", model_id=config.model_id, variant="direct"
        )
        role_config = RoleConfig(role=role, model=model, steps=1)
        role_set = RoleConfiguration(roles=(role_config,))
    except ReviewAgentError as exc:
        raise GenerationDocumentError(
            "hosted-use: the configured model is not a usable direct-route model reference"
        ) from exc

    version_digest = _version_digest(role, prompt_version, config_map)
    bound_evidence_digest = _sha256_hex(canonical_json(dict(plan.bound_evidence)))

    bounded = BoundedTask(
        project_id=store.project_id,
        role=role,
        target={
            "project_id": store.project_id,
            "artifact_id": hosted_ref["artifact_id"],
            "revision_id": hosted_ref["revision_id"],
        },
        input_refs=normalized_inputs,
        # The generation role and the exact hosted-use revision are pinned in the
        # task scope, so the per-role budget sum is computable and a replay with a
        # different authorization conflicts rather than returning an unrelated run.
        scope=[
            GENERATION_STAGE,
            f"role={role}",
            f"hosted={hosted_ref['artifact_id']}:{hosted_ref['revision_id']}",
        ],
        prompt_version=prompt_version,
        size_limit=TASK_SIZE_LIMIT,
        step_limit=1,
        role_config=role_config,
    )

    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    task_record = runtime.record_task(bounded, request_id=request_id, configuration=role_set)

    # The single exclusive **new-send** decision, the budget check, the reserve and
    # the send claim all happen under the registry write guard, so two concurrent
    # callers can never both proceed to a POST. A recorded logical request is never
    # re-sent.
    with registry.execution_write_guard(activation_token, store.project_id):
        existing = dispatch.get_dispatch_by_request(request_id)
        if existing is not None:
            return _readback(store, request_id, replayed=True)
        _check_budget(
            store,
            role=role,
            hosted_ref=hosted_ref,
            budget_cap_usd=budget_cap_usd,
            new_reserve=plan.reserve_usd,
        )
        dispatch_record = dispatch.reserve_dispatch(
            task_record,
            request_id=request_id,
            route="direct_call",
            stage=GENERATION_STAGE,
            input_digest=input_digest,
            version_digest=version_digest,
            reserve_usd=plan.reserve_usd,
            bound_evidence_digest=bound_evidence_digest,
        )
        dispatch_record = dispatch.mark_submitted(dispatch_record.dispatch_id)

    # The registry lock is released before any network I/O: revalidate the exact
    # gate and the active execution token immediately before the POST.
    _require_hosted_use(store, hosted_ref, role=role)
    for source in normalized_sources:
        _verify_hosted_rights(store, source)
    _assert_token_active(registry, store, activation_token)

    result = runtime_generation.execute_generation(
        plan, api_key=api_key, transport=transport
    )

    # Completing/reconciling already-accepted in-flight work must still commit if
    # the owner began a drain while the request was in flight.
    with registry.execution_write_guard(
        activation_token, store.project_id, allow_draining=True
    ):
        return _commit_result(
            store,
            runtime=runtime,
            dispatch=dispatch,
            task_record=task_record,
            dispatch_record=dispatch_record,
            role_set=role_set,
            plan=plan,
            role=role,
            prompt_version=prompt_version,
            input_digest=input_digest,
            result=result,
        )
