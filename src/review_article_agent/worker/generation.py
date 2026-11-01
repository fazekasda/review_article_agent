"""Owner-controlled detached **generation role** jobs (kickoff 16 §5, S17-O2a).

Authority
---------
``docs/generation.md`` (the one direct OpenRouter model port and the role
adapters) and ``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("One real
model port": every model-backed role runs through one small adapter; model output
stays a proposal and the existing human gates remain required). The durable job
plumbing mirrors the Step 09 **extraction_run** job (``worker/extraction.py``):
this module is only the thin glue between the durable worker engine and the
already-integrated role adapters in
:mod:`review_article_agent.generation` and
:mod:`review_article_agent.screening`.

What this module is
-------------------
* a strict, **pure** validator/canonicaliser for one ``generation_run``
  submission — a closed ``operation`` plus the exact per-operation parameters
  (refs, the artifact/revision the adapter writes, the caller's ``rights``
  record, and the one approved credential **environment-variable name**) — into
  the immutable ``JobRepository`` parameters: no store read, no I/O, and no
  credential in the parameters;
* a dispatch that calls the exact role adapter for the operation with the
  credential supplied by the worker (from the one named environment variable) and
  the job request id as the replay key;
* a safe accepted-result projection that references what the adapter wrote (a
  workflow ref / a review state id / a proposed batch / a decision-result ref)
  plus the adapter ``status`` (``proposed`` / ``invalid_proposal`` /
  ``model_not_completed`` / ``complete`` / ``blocked``) — never the credential,
  the model output, the material, or any private content;
* a replay-only proof that reads a recorded run back from durable rows with
  **zero** POST, so an ambiguous outcome is never re-sent on recovery.

What this module is **not**
---------------------------
* It performs **no** approval, gate check, rights check, reserve, or scientific
  decision: the role adapter (through
  :func:`~review_article_agent.generation.runner.execute_generation_task`) owns
  all of those and revalidates them before and immediately prior to its single
  POST. A recorded run is inert provenance.
* It never accepts, sends, persists, or logs the credential **value**; only the
  one fixed environment-variable **name** is carried in the parameters.
* It supports every documented role adapter; an operation name outside the closed
  set is refused with a static validation error (nothing is written and no job is
  created).

The operations wired here are ``synthesis`` / ``framework`` / ``outline``
(``generation/synthesis_roles``), ``blueprint`` / ``draft``
(``generation/drafting_roles``), ``section_review``
(``review/workflow.SectionReviewRepository.run`` with ports from
``generation/review_roles.make_review_ports``), ``global_review`` / ``notes_plan``
(``generation/delivery_roles`` over ``delivery/workflow.DeliveryRepository``), and
``screening_case`` (``screening/runner.execute_screening_case``). Every documented
operation is now wired; there is no recognized-but-refused operation.
"""

from __future__ import annotations

import copy
from typing import Any

from ..domain.records import (
    RecordValidationError,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)

__all__ = [
    "MODE_GENERATION",
    "TASK_KIND_GENERATION",
    "RESULT_KIND_GENERATION",
    "GENERATION_CREDENTIAL_ENV",
    "SUPPORTED_OPERATIONS",
    "RECOGNIZED_OPERATIONS",
    "OPERATION_SPECS",
    "GENERATION_REQUIRED_KEYS",
    "GENERATION_OPTIONAL_KEYS",
    "GenerationRun",
    "validate_generation",
    "dispatch_generation",
    "replay_generation",
    "accepted_result",
    "status_projection",
    "is_terminal",
    "persisted_generation_run",
]

#: The one durable operation mode (and its task kind) for a generation run.
MODE_GENERATION = "generation_run"
TASK_KIND_GENERATION = "generation_run"
#: The safe job-accepted-result kind of one generation run.
RESULT_KIND_GENERATION = "generation_run"

#: The one, fixed environment-variable name a generation run may name. It is the
#: same hosted direct-OpenRouter credential the lead/extraction jobs use; the
#: value is never accepted over IPC, persisted, or logged.
GENERATION_CREDENTIAL_ENV = "OPENROUTER_API_KEY"

#: The operations actually wired to a role adapter on this branch (all of them).
SUPPORTED_OPERATIONS = (
    "synthesis",
    "framework",
    "outline",
    "blueprint",
    "draft",
    "section_review",
    "global_review",
    "notes_plan",
    "screening_case",
)
#: No recognized operation is refused any more (kept for a stable public name).
UNSUPPORTED_OPERATIONS: tuple[str, ...] = ()
#: Every recognized operation name.
RECOGNIZED_OPERATIONS = SUPPORTED_OPERATIONS + UNSUPPORTED_OPERATIONS

_OPS_STORAGE = "storage"

#: The per-operation parameter schema. Each entry is ``(required, optional)``
#: where a key is ``(name, kind)`` and ``kind`` is one of ``ref`` (an exact
#: same-project revision reference), ``ident`` (a bounded opaque id), ``rights``
#: (a caller RightsRecord), or ``ref_list`` (a list of same-project refs). The
#: ``credential_env`` and ``operation`` keys are common to every operation.
OPERATION_SPECS: dict[str, tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]] = {
    "synthesis": (
        (
            ("audit_ref", "ref"),
            ("hosted_ref", "ref"),
            ("artifact_id", "ident"),
            ("revision_id", "ident"),
            ("rights", "rights"),
        ),
        (),
    ),
    "framework": (
        (
            ("synthesis_ref", "ref"),
            ("hosted_ref", "ref"),
            ("artifact_id", "ident"),
            ("revision_id", "ident"),
            ("rights", "rights"),
        ),
        (),
    ),
    "outline": (
        (
            ("synthesis_ref", "ref"),
            ("framework_ref", "ref"),
            ("hosted_ref", "ref"),
            ("artifact_id", "ident"),
            ("revision_id", "ident"),
            ("rights", "rights"),
        ),
        (),
    ),
    "blueprint": (
        (
            ("workspace_ref", "ref"),
            ("hosted_ref", "ref"),
            ("artifact_id", "ident"),
            ("revision_id", "ident"),
            ("rights", "rights"),
        ),
        (("parent_revision_id", "ident"),),
    ),
    "draft": (
        (
            ("blueprint_ref", "ref"),
            ("hosted_ref", "ref"),
            ("artifact_id", "ident"),
            ("revision_id", "ident"),
            ("rights", "rights"),
        ),
        (("parent_revision_id", "ident"),),
    ),
    "section_review": (
        (
            ("review_id", "ident"),
            ("draft_ref", "ref"),
            ("critic_hosted_ref", "ref"),
            ("writer_hosted_ref", "ref"),
            ("rights", "rights"),
        ),
        (("source_refs", "ref_list"),),
    ),
    "global_review": (
        (
            ("delivery_id", "ident"),
            ("reviewer_hosted_ref", "ref"),
            ("writer_hosted_ref", "ref"),
            ("rights", "rights"),
        ),
        (("source_refs", "ref_list"),),
    ),
    "notes_plan": (
        (
            ("delivery_id", "ident"),
            ("hosted_ref", "ref"),
        ),
        (("source_refs", "ref_list"),),
    ),
    "screening_case": (
        (
            ("config_ref", "ref"),
            ("report_ref", "ref"),
            ("hosted_ref", "ref"),
        ),
        (("passage_id", "ident"),),
    ),
}

#: The common keys every operation carries (never a parameter the adapter owns).
_COMMON_KEYS = frozenset({"operation", "credential_env"})

#: The exact, strict required/optional parameter keys (no actor/authority flag).
GENERATION_REQUIRED_KEYS = _COMMON_KEYS | frozenset(
    name for spec in OPERATION_SPECS.values() for name, _kind in spec[0]
)
GENERATION_OPTIONAL_KEYS = frozenset(
    name for spec in OPERATION_SPECS.values() for name, _kind in spec[1]
)

_REF_KEYS = ("project_id", "artifact_id", "revision_id")

_M_BAD_PARAMETERS = "parameters must be an exact object for the generation_run mode"
_M_BAD_KEY = "parameters contained an unknown or missing field"
_M_BAD_OPERATION = "operation is not a supported generation operation"
_M_UNSUPPORTED_OPERATION = (
    "the requested generation operation is recognized but not yet supported by the "
    "worker job kind; nothing was sent and no job was created"
)
_M_BAD_CREDENTIAL_ENV = "credential_env must name the one approved environment variable"
_M_MISSING_CREDENTIAL = "the named generation credential is not available to the worker"
_M_BAD_REF = "a reference must be an exact same-project revision reference"
_M_BAD_REFS = "source_refs must be a list of distinct same-project references"
_M_BAD_IDENT = "an artifact/revision/operation identifier is invalid or out of bounds"
_M_BAD_RIGHTS = "a rights record is invalid for its exact subject or not eligible"


class GenerationRun:
    """The typed name for one canonicalised ``generation_run`` submission."""

    __slots__ = ("parameters",)

    def __init__(self, parameters: dict[str, Any]) -> None:
        self.parameters = parameters


def _error(code: str, message: str):
    # Local import: ``discovery`` imports this module for delegation.
    from .discovery import DiscoveryJobValidationError

    return DiscoveryJobValidationError(code, message)


def _bad_value(message: str):
    return _error("bad_value", message)


def _bad_type(message: str):
    return _error("bad_type", message)


# --------------------------------------------------------------------------- #
# Pure validation
# --------------------------------------------------------------------------- #
def _ident(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _bad_type(_M_BAD_IDENT)
    try:
        return validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_IDENT) from None


def _ref(value: object, project_id: str) -> dict[str, str]:
    try:
        ref = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_REF) from None
    return {key: ref[key] for key in _REF_KEYS}


def _ref_list(value: object, project_id: str) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise _bad_type(_M_BAD_REFS)
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        ref = _ref(item, project_id)
        key = (ref["project_id"], ref["artifact_id"], ref["revision_id"])
        if key in seen:
            raise _bad_value(_M_BAD_REFS)
        seen.add(key)
        refs.append(ref)
    return refs


def _subject(project_id: str, artifact_id: str, revision_id: str) -> dict[str, str]:
    return {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


def _rights(record: object, project_id: str, subject: dict[str, str] | None) -> dict:
    if not isinstance(record, dict):
        raise _bad_type(_M_BAD_RIGHTS)
    try:
        validated = validate_rights_record(record, project_id, subject=subject)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_RIGHTS) from None
    if validated["derived_status"] != "valid":
        raise _bad_value(_M_BAD_RIGHTS)
    if validated["operations"].get(_OPS_STORAGE) != "allowed":
        raise _bad_value(_M_BAD_RIGHTS)
    privacy = validated["privacy"]
    if privacy.get("denial") is not False or privacy.get("confidentiality") == "unknown":
        raise _bad_value(_M_BAD_RIGHTS)
    return validated


def _validate_op(
    operation: str, parameters: dict[str, Any], project_id: str
) -> dict[str, Any]:
    required, optional = OPERATION_SPECS[operation]
    keys = set(parameters)
    required_names = _COMMON_KEYS | frozenset(name for name, _ in required)
    optional_names = frozenset(name for name, _ in optional)
    if not required_names <= keys or not keys <= (required_names | optional_names):
        raise _bad_value(_M_BAD_KEY)

    canonical: dict[str, Any] = {
        "operation": operation,
        "credential_env": GENERATION_CREDENTIAL_ENV,
    }
    artifact_id = revision_id = None
    for name, kind in required:
        value = parameters[name]
        if kind == "ref":
            canonical[name] = _ref(value, project_id)
        elif kind == "ident":
            canonical[name] = _ident(value, name)
            if name == "artifact_id":
                artifact_id = canonical[name]
            elif name == "revision_id":
                revision_id = canonical[name]
        elif kind == "rights":
            # Deferred: a rights record is validated after every ref/ident is
            # canonicalised (its subject may bind the write target).
            canonical[name] = value
        else:  # pragma: no cover - the spec table is closed
            raise _bad_value(_M_BAD_KEY)
    for name, kind in optional:
        if name not in parameters:
            continue
        value = parameters[name]
        if kind == "ident":
            # An optional identifier (e.g. ``passage_id``) may be explicitly null.
            canonical[name] = None if value is None else _ident(value, name)
        elif kind == "ref_list":
            canonical[name] = _ref_list(value, project_id)
        else:  # pragma: no cover - the spec table is closed
            raise _bad_value(_M_BAD_KEY)

    if "rights" in parameters:
        if artifact_id is not None and revision_id is not None:
            # A single-shot operation names the exact artifact/revision it writes,
            # so its rights record must bind that exact subject.
            subject = _subject(project_id, artifact_id, revision_id)
        else:
            # section_review writes its own artifact (``review_id``) with generated
            # revision ids, and the delivery/screening operations own their own
            # writes, so the base record is validated generically (project-scoped,
            # storage-allowed, eligible) and any per-revision subject is rebound by
            # the adapter at write time.
            subject = None
        canonical["rights"] = _rights(parameters["rights"], project_id, subject)
    return canonical


def validate_generation(
    parameters: object, project_id: str, request_id: object
) -> dict[str, Any]:
    """Strictly validate and canonicalise one ``generation_run`` submission.

    Pure: no store read, no I/O, no credential. Raises a
    ``DiscoveryJobValidationError`` (``bad_type``/``bad_value``) before any durable
    write, activation change, or POST. The authoritative gate/rights/budget checks
    are repeated by the role adapter before its one POST.
    """
    if not isinstance(parameters, dict):
        raise _bad_type(_M_BAD_PARAMETERS)
    operation = parameters.get("operation")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        raise _bad_value(_M_BAD_OPERATION)
    if parameters.get("credential_env") != GENERATION_CREDENTIAL_ENV:
        raise _bad_value(_M_BAD_CREDENTIAL_ENV)
    return _validate_op(operation, parameters, project_id)


# --------------------------------------------------------------------------- #
# Rights rebinding (section_review only)
# --------------------------------------------------------------------------- #
def _rebind_rights(
    base: dict, project_id: str, artifact_id: str, revision_id: str
) -> dict:
    """Return a copy of ``base`` whose subject binds ``(artifact_id, revision_id)``.

    The review workflow generates its own revision ids and asks the injected
    rights factory for a record bound to each one; a static base record cannot
    name a not-yet-existing revision, so its subject (and the authorization's
    ``against_subject``) is rebound to the exact requested revision. Nothing else
    changes; the rebind grants no authority beyond the caller's record.
    """
    record = copy.deepcopy(base)
    subject = _subject(project_id, artifact_id, revision_id)
    record["subject"] = subject
    record["rights_id"] = f"RIGHTS-{artifact_id}-{revision_id}"
    authorization = record.get("authorization")
    if isinstance(authorization, dict):
        authorization["against_subject"] = subject
    return record


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _run_role(store, registry, token, job, api_key, transport, operation: str, fn):
    raw = fn()
    if not isinstance(raw, dict):
        raw = {"status": "model_not_completed"}
    raw = dict(raw)
    raw["operation"] = operation
    return raw


def _dispatch_synthesis(store, registry, token, job, api_key, transport):
    from ..generation.synthesis_roles import propose_synthesis

    p = job.parameters

    def fn():
        return propose_synthesis(
            store,
            registry=registry,
            activation_token=token,
            request_id=job.request_id,
            hosted_ref=p["hosted_ref"],
            api_key=api_key,
            transport=transport,
            audit_ref=p["audit_ref"],
            artifact_id=p["artifact_id"],
            revision_id=p["revision_id"],
            rights=p["rights"],
        )

    return _run_role(store, registry, token, job, api_key, transport, "synthesis", fn)


def _dispatch_framework(store, registry, token, job, api_key, transport):
    from ..generation.synthesis_roles import propose_framework

    p = job.parameters

    def fn():
        return propose_framework(
            store,
            registry=registry,
            activation_token=token,
            request_id=job.request_id,
            hosted_ref=p["hosted_ref"],
            api_key=api_key,
            transport=transport,
            synthesis_ref=p["synthesis_ref"],
            artifact_id=p["artifact_id"],
            revision_id=p["revision_id"],
            rights=p["rights"],
        )

    return _run_role(store, registry, token, job, api_key, transport, "framework", fn)


def _dispatch_outline(store, registry, token, job, api_key, transport):
    from ..generation.synthesis_roles import propose_outline

    p = job.parameters

    def fn():
        return propose_outline(
            store,
            registry=registry,
            activation_token=token,
            request_id=job.request_id,
            hosted_ref=p["hosted_ref"],
            api_key=api_key,
            transport=transport,
            synthesis_ref=p["synthesis_ref"],
            framework_ref=p["framework_ref"],
            artifact_id=p["artifact_id"],
            revision_id=p["revision_id"],
            rights=p["rights"],
        )

    return _run_role(store, registry, token, job, api_key, transport, "outline", fn)


def _dispatch_blueprint(store, registry, token, job, api_key, transport):
    from ..generation.drafting_roles import propose_blueprint

    p = job.parameters

    def fn():
        return propose_blueprint(
            store,
            registry=registry,
            activation_token=token,
            request_id=job.request_id,
            workspace_ref=p["workspace_ref"],
            hosted_ref=p["hosted_ref"],
            api_key=api_key,
            artifact_id=p["artifact_id"],
            revision_id=p["revision_id"],
            rights=p["rights"],
            transport=transport,
            parent_revision_id=p.get("parent_revision_id"),
        )

    return _run_role(store, registry, token, job, api_key, transport, "blueprint", fn)


def _dispatch_draft(store, registry, token, job, api_key, transport):
    from ..generation.drafting_roles import propose_draft

    p = job.parameters

    def fn():
        return propose_draft(
            store,
            registry=registry,
            activation_token=token,
            request_id=job.request_id,
            blueprint_ref=p["blueprint_ref"],
            hosted_ref=p["hosted_ref"],
            api_key=api_key,
            artifact_id=p["artifact_id"],
            revision_id=p["revision_id"],
            rights=p["rights"],
            transport=transport,
            parent_revision_id=p.get("parent_revision_id"),
        )

    return _run_role(store, registry, token, job, api_key, transport, "draft", fn)


def _dispatch_section_review(store, registry, token, job, api_key, transport):
    """Run one durable section review through the real critic/repair ports.

    The exact current draft result and its approved package are read from the
    drafting workflow; the critic and repair ports are built with the caller's
    critic/writer hosted-use revisions (the distinct-concrete-model rule is
    enforced by ``make_review_ports``); and the review is run (or resumed) as a
    durable, resumable workflow. The returned mapping references the review state
    id and its final ``status`` (``complete``/``blocked``).
    """
    from ..drafting.workflow import DraftingWorkflowRepository
    from ..generation.review_roles import make_review_ports
    from ..review.workflow import SectionReviewRepository

    p = job.parameters
    review_id = p["review_id"]
    payload = DraftingWorkflowRepository(store).read(p["draft_ref"], "draft", current=True)
    draft = payload["result"]
    package = payload["package"]

    critic, writer, critic_model, writer_model = make_review_ports(
        store,
        registry=registry,
        activation_token=token,
        review_id=review_id,
        critic_hosted_ref=p["critic_hosted_ref"],
        writer_hosted_ref=p["writer_hosted_ref"],
        api_key=api_key,
        transport=transport,
        source_refs=list(p.get("source_refs") or []),
    )

    base_rights = p["rights"]
    project_id = store.project_id

    def rights_factory(artifact_id, revision_id):
        return _rebind_rights(base_rights, project_id, artifact_id, revision_id)

    state = SectionReviewRepository(store).run(
        review_id,
        draft=draft,
        package=package,
        writer_model=writer_model,
        critic_model=critic_model,
        critic=critic,
        writer=writer,
        rights=rights_factory,
    )
    status = state.get("status") if isinstance(state, dict) else None
    return {
        "operation": "section_review",
        "status": status if status in ("complete", "blocked") else "model_not_completed",
        "review_id": review_id,
        "review_status": status,
    }


def _dispatch_global_review(store, registry, token, job, api_key, transport):
    """Run one durable bounded global review from the recorded delivery state.

    The current manuscript text/paragraphs and any recorded evidence are read from
    the delivery state (never passed in by the caller); the real global
    reviewer/writer ports are built with the caller's reviewer/writer hosted-use
    revisions (the distinct-concrete-model rule is enforced by
    ``make_global_review_ports``); and ``DeliveryRepository.run_global_review``
    runs (or resumes) the bounded, resumable workflow. A model call that does not
    complete is a **terminal** ``model_not_completed`` result, never a crash. The
    returned mapping references the delivery id and the final
    ``complete``/``blocked`` status with the rounds used.
    """
    from ..delivery.workflow import DeliveryRepository
    from ..generation.delivery_roles import DeliveryRoleError, make_global_review_ports

    p = job.parameters
    delivery_id = p["delivery_id"]
    repo = DeliveryRepository(store)
    state = repo.read(delivery_id)
    if not isinstance(state, dict):
        return {
            "operation": "global_review",
            "status": "model_not_completed",
            "reason": "delivery_unavailable",
            "delivery_id": delivery_id,
        }
    manuscript = state.get("manuscript") or {}
    text = manuscript.get("text") if isinstance(manuscript, dict) else None
    if not isinstance(text, str):
        text = ""
    paragraphs = state.get("paragraphs") or {}
    evidence = state.get("evidence") or []

    reviewer, writer = make_global_review_ports(
        store,
        registry=registry,
        activation_token=token,
        delivery_id=delivery_id,
        reviewer_hosted_ref=p["reviewer_hosted_ref"],
        writer_hosted_ref=p["writer_hosted_ref"],
        api_key=api_key,
        transport=transport,
        source_refs=list(p.get("source_refs") or []),
    )

    base_rights = p["rights"]
    project_id = store.project_id

    def rights_factory(artifact_id, revision_id):
        return _rebind_rights(base_rights, project_id, artifact_id, revision_id)

    try:
        final = repo.run_global_review(
            delivery_id,
            {
                "revision_id": manuscript.get("revision_id") if isinstance(manuscript, dict) else None,
                "text": text,
                "paragraphs": paragraphs,
            },
            evidence,
            reviewer,
            writer,
            rights=rights_factory,
        )
    except DeliveryRoleError:
        return {
            "operation": "global_review",
            "status": "model_not_completed",
            "delivery_id": delivery_id,
        }
    gr = final.get("global_review") if isinstance(final, dict) else None
    status = gr.get("status") if isinstance(gr, dict) else None
    return {
        "operation": "global_review",
        "status": status if status in ("complete", "blocked") else "model_not_completed",
        "delivery_id": delivery_id,
        "review_status": status,
        "rounds_used": gr.get("rounds_used") if isinstance(gr, dict) else None,
    }


def _dispatch_notes_plan(store, registry, token, job, api_key, transport):
    """Propose one author-note edit batch from the recorded delivery state.

    ``generation/delivery_roles.plan_note_edits`` reads the recorded notes and the
    current manuscript from the delivery state, runs one bounded ``notes_planner``
    request, and returns a **proposal** batch (never applied): the human approves
    the batch and only then is ``apply_notes`` called. A non-completed model call
    or a malformed envelope is a terminal result.
    """
    from ..generation.delivery_roles import DeliveryRoleError, plan_note_edits

    p = job.parameters
    try:
        result = plan_note_edits(
            store,
            registry=registry,
            activation_token=token,
            delivery_id=p["delivery_id"],
            hosted_ref=p["hosted_ref"],
            api_key=api_key,
            transport=transport,
            source_refs=list(p.get("source_refs") or []),
        )
    except DeliveryRoleError:
        return {
            "operation": "notes_plan",
            "status": "model_not_completed",
            "delivery_id": p["delivery_id"],
        }
    status = result.get("status")
    return {
        "operation": "notes_plan",
        "status": status
        if status in ("proposed", "model_not_completed", "invalid_proposal")
        else "model_not_completed",
        "delivery_id": p["delivery_id"],
        "edits": result.get("edits") or [],
        "dropped": result.get("dropped") or [],
        "reason": result.get("reason"),
        "generation": result.get("generation"),
    }


def _dispatch_screening_case(store, registry, token, job, api_key, transport):
    """Run one screening case through the direct Jev decision path.

    ``screening/runner.execute_screening_case`` re-reads the screening config and
    parsing report, reuses the hosted-use gate/rights/token authorization, enforces
    the per-role budget, reserves a ``direct_call`` dispatch before the one POST,
    and commits the accepted result through the decision ledger. The returned
    mapping references the committed ``decision_result_ref`` and the dispatch
    status (a terminal result either way).
    """
    from ..screening.runner import execute_screening_case

    p = job.parameters
    result = execute_screening_case(
        store,
        registry=registry,
        activation_token=token,
        config_ref=p["config_ref"],
        report_ref=p["report_ref"],
        passage_id=p.get("passage_id"),
        request_id=job.request_id,
        hosted_ref=p["hosted_ref"],
        api_key=api_key,
        transport=transport,
    )
    status = result.get("status") if isinstance(result, dict) else None
    return {
        "operation": "screening_case",
        "status": "complete" if status == "completed" else "model_not_completed",
        "decision_result_ref": result.get("decision_result_ref") if isinstance(result, dict) else None,
        "decision_result_refs": result.get("decision_result_refs") if isinstance(result, dict) else None,
        "window_count": result.get("window_count") if isinstance(result, dict) else None,
        "screening_status": status,
    }


_DISPATCHERS = {
    "synthesis": _dispatch_synthesis,
    "framework": _dispatch_framework,
    "outline": _dispatch_outline,
    "blueprint": _dispatch_blueprint,
    "draft": _dispatch_draft,
    "section_review": _dispatch_section_review,
    "global_review": _dispatch_global_review,
    "notes_plan": _dispatch_notes_plan,
    "screening_case": _dispatch_screening_case,
}


def dispatch_generation(
    store: Any,
    registry: Any,
    token: str,
    job: Any,
    *,
    api_key: str | None,
    transport: Any = None,
) -> dict[str, Any]:
    """Execute one generation role operation through its role adapter.

    A missing credential is refused here with **zero** POST. The adapter owns
    every gate/rights/budget check and the single POST; it returns a safe mapping
    carrying the adapter ``status`` and what it wrote. An already-recorded request
    is observed by the adapter, never re-sent.
    """
    from ..generation.runner import GenerationRunnerError

    params = job.parameters if isinstance(job.parameters, dict) else {}
    if not isinstance(api_key, str) or api_key == "":
        raise GenerationRunnerError(_M_MISSING_CREDENTIAL)
    operation = params.get("operation")
    if operation not in _DISPATCHERS:
        raise _bad_value(_M_UNSUPPORTED_OPERATION)
    return _DISPATCHERS[operation](store, registry, token, job, api_key, transport)


# --------------------------------------------------------------------------- #
# Safe result projection
# --------------------------------------------------------------------------- #
#: The adapter statuses that are terminal, resolved outcomes (never a crash).
_TERMINAL_STATUSES = frozenset(
    {"proposed", "invalid_proposal", "model_not_completed", "complete", "blocked"}
)


def is_terminal(raw: Any) -> bool:
    """True when an adapter outcome is a resolved, persistable lifecycle result."""
    return isinstance(raw, dict) and raw.get("status") in _TERMINAL_STATUSES


#: The safe generation-readback keys (no credential, model output, or material).
_GENERATION_SAFE_KEYS = (
    "role",
    "prompt_version",
    "outcome",
    "dispatch_id",
    "cost",
    "proposal_ref",
    "replayed",
)


def _generation_summary(generation: Any) -> Any:
    if not isinstance(generation, dict):
        return None
    return {key: generation[key] for key in _GENERATION_SAFE_KEYS if key in generation}


def accepted_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce an adapter outcome to the safe accepted-result mapping.

    Only the operation, the request id, the adapter ``status`` and ``reason``, the
    written reference (a workflow ref, a review state id, or a proposed-batch
    marker), and a safe generation readback (role/prompt/outcome/dispatch/cost/
    proposal-ref). Never the credential, the raw model output, the material, or
    any private content.
    """
    dropped = raw.get("dropped")
    return {
        "kind": RESULT_KIND_GENERATION,
        "mode": MODE_GENERATION,
        "operation": raw.get("operation"),
        "request_id": raw.get("request_id"),
        "status": raw.get("status"),
        "outcome": raw.get("outcome"),
        "reason": raw.get("reason"),
        "ref": raw.get("ref"),
        "review_id": raw.get("review_id"),
        "review_status": raw.get("review_status"),
        "delivery_id": raw.get("delivery_id"),
        "rounds_used": raw.get("rounds_used"),
        "decision_result_ref": raw.get("decision_result_ref"),
        "decision_result_refs": raw.get("decision_result_refs"),
        "window_count": raw.get("window_count"),
        "screening_status": raw.get("screening_status"),
        "edits_count": len(raw.get("edits") or []) if isinstance(raw.get("edits"), list) else None,
        "dropped_count": len(dropped) if isinstance(dropped, list) else None,
        "generation": _generation_summary(raw.get("generation")),
        "authorizing": False,
    }


_SAFE_KEYS = (
    "kind", "mode", "operation", "request_id", "status", "outcome", "reason",
    "ref", "review_id", "review_status", "delivery_id", "rounds_used",
    "decision_result_ref", "decision_result_refs", "window_count",
    "screening_status", "edits_count", "dropped_count",
    "generation", "authorizing",
)


def status_projection(result: Any) -> Any:
    """Bound a saved accepted result for ``job_status`` (safe keys only)."""
    if not isinstance(result, dict):
        return result
    return {key: result[key] for key in _SAFE_KEYS if key in result}


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def _replay_request_id_operation(job: Any) -> str | None:
    params = job.parameters if isinstance(job.parameters, dict) else {}
    operation = params.get("operation")
    return operation if isinstance(operation, str) else None


def replay_generation(store: Any, registry: Any, token: str, job: Any):
    """Prove an existing generation run from durable rows with **zero** POST.

    Returns ``(mapping, complete)``. For the single-shot synthesis/drafting
    operations the durable generation readback of the job request id proves a
    terminal dispatch. For a section review the persisted review state proves a
    terminal ``complete``/``blocked`` outcome. Anything missing, or a
    non-terminal/``unknown``/``submitted`` outcome, is never a silent completion
    (``None`` or ``complete=False``); no request is ever issued.
    """
    from ..generation.runner import get_generation_outcome

    operation = _replay_request_id_operation(job)
    if operation in (None, "section_review"):
        if operation == "section_review":
            return _replay_section_review(store, job)
        return None
    if operation == "global_review":
        return _replay_global_review(store, job)
    if operation == "screening_case":
        return _replay_screening_case(store, job)

    try:
        mapping = get_generation_outcome(store, job.request_id)
    except Exception:  # noqa: BLE001 - not a proven present outcome
        return None
    if not isinstance(mapping, dict) or mapping.get("dispatch_id") is None:
        return None
    outcome = mapping.get("outcome")
    complete = outcome in ("completed", "refused", "protocol_error", "oversized_output", "failed")
    raw = {
        "operation": operation,
        "status": "completed" if outcome == "completed" else "model_not_completed",
        "outcome": outcome,
        "generation": mapping,
    }
    return accepted_result(raw), complete


def _replay_section_review(store: Any, job: Any):
    from ..review.workflow import SectionReviewRepository

    params = job.parameters if isinstance(job.parameters, dict) else {}
    review_id = params.get("review_id")
    if not isinstance(review_id, str):
        return None
    try:
        state = SectionReviewRepository(store).read(review_id)
    except Exception:  # noqa: BLE001 - not a proven present outcome
        return None
    if not isinstance(state, dict):
        return None
    status = state.get("status")
    if status not in ("complete", "blocked"):
        return None
    raw = {
        "operation": "section_review",
        "status": status,
        "review_id": review_id,
        "review_status": status,
    }
    return accepted_result(raw), True


def _replay_global_review(store: Any, job: Any):
    """Prove a completed global review from the recorded delivery state (no POST)."""
    from ..delivery.workflow import DeliveryRepository

    params = job.parameters if isinstance(job.parameters, dict) else {}
    delivery_id = params.get("delivery_id")
    if not isinstance(delivery_id, str):
        return None
    try:
        state = DeliveryRepository(store).read(delivery_id)
    except Exception:  # noqa: BLE001 - not a proven present outcome
        return None
    gr = state.get("global_review") if isinstance(state, dict) else None
    status = gr.get("status") if isinstance(gr, dict) else None
    if status not in ("complete", "blocked"):
        return None
    raw = {
        "operation": "global_review",
        "status": status,
        "delivery_id": delivery_id,
        "review_status": status,
        "rounds_used": gr.get("rounds_used") if isinstance(gr, dict) else None,
    }
    return accepted_result(raw), True


def _replay_screening_case(store: Any, job: Any):
    """Prove a resolved screening case from the dispatch ledger (no POST).

    The per-window request ids are recomputed from the current inputs (the same
    ``case_windows`` split the runner uses) and every window's durable dispatch is
    read back; a missing or non-completed window means the case is **not**
    complete (``None``), so nothing is silently accepted. A pre-windowing job that
    recorded the bare request id for a one-window case is still read.
    """
    from ..screening.runner import readback_screening_case

    params = job.parameters if isinstance(job.parameters, dict) else {}
    try:
        mapping = readback_screening_case(
            store,
            request_id=job.request_id,
            config_ref=params.get("config_ref"),
            report_ref=params.get("report_ref"),
            passage_id=params.get("passage_id"),
        )
    except Exception:  # noqa: BLE001 - not a proven present outcome
        return None
    status = mapping.get("status") if isinstance(mapping, dict) else None
    if status not in ("completed", "unknown"):
        return None
    raw = {
        "operation": "screening_case",
        "status": "complete" if status == "completed" else "model_not_completed",
        "decision_result_ref": mapping.get("decision_result_ref"),
        "decision_result_refs": mapping.get("decision_result_refs"),
        "window_count": mapping.get("window_count"),
        "screening_status": status,
    }
    return accepted_result(raw), True


def persisted_generation_run(store: Any, *, request_id: str):
    """Return the saved accepted run mapping for a completed job, or ``None``.

    A thin, validating reader for a readback CLI: it refuses anything that is not
    a ``generation_run`` job whose accepted result is the safe run projection. It
    performs no POST and no execution.
    """
    from ..runtime.jobs import JobRepository

    repo = JobRepository(store)
    job = None
    for candidate in repo.list():
        if candidate.request_id == request_id and candidate.task_kind == TASK_KIND_GENERATION:
            job = candidate
            break
    if job is None:
        return None
    accepted = repo.accepted_result(job.job_id)
    if accepted is None or accepted.result_kind != RESULT_KIND_GENERATION:
        return None
    result = accepted.result
    if not isinstance(result, dict):
        return None
    return result
