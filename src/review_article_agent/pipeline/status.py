"""Read-only pipeline stage resolver (kickoff 16 §6, ``pipeline status``).

For one already-verified project store this module determines the project's
**current pipeline stage** from durable state alone and reports the exact next
action a human must take. It is deliberately read-only:

* it never writes any row, file, or revision;
* it never calls a model, a provider, or the network;
* it holds no authoritative state of its own -- every answer is re-derived from
  the same repositories the stages already use.

Determining a stage's state
---------------------------

The resolver walks :data:`STAGES` in pipeline order. Each stage names the
``document_type`` values that identify its proposal revisions and a ``check``
callable that delegates to the stage's **existing** ``require_*``/read API
(``discovery.freeze.require_frozen``, ``acquisition.manifest.require_acquired``,
``screening.workflow.require_pilot``, ``synthesis.workflow.require_outline``,
``drafting.workflow.require_blueprint``, ...). A stage is **complete** only when
its check passes.

The first stage that is not complete is the **current** stage:

* no proposal revision exists for the stage -- and no *later* stage has begun --
  -> ``not_started`` (``blocked_on`` is ``None`` and ``next_action`` describes how
  to produce the proposal);
* a proposal exists but its exact named human gate is not currently active and
  approved -> ``blocked_on.kind == "human_gate"`` with the exact ``gate confirm``
  command line;
* a proposal exists, its gate (if any) is satisfied, but the stage still does not
  validate (a stale/invalid dependency, or a gate-less stage that is not yet
  complete) -> ``blocked_on.kind == "failure"`` with a curated detail and a
  warning.

A stage with **no** proposal is treated as *skipped* (not current) when a later
stage has already begun; this mirrors the real workflow, where some stages
(parsing, screening) are optional for a given project. When no later stage has
begun, the first stage without a proposal is the current one.

Exact CLI commands are reported only for actions that already exist in
:mod:`review_article_agent.cli`; where no CLI exists yet (review, tracing,
delivery) ``next_command`` is ``None`` and the detail names the library entry
point.

Re-running the resolver is idempotent: it writes nothing, so two calls over an
unchanged store return equal output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..acquisition.manifest import (
    ACQUISITION_GATE_ID,
    ACQUISITION_GATE_KIND,
    AcquisitionRepository,
)
from ..discovery.context import (
    PLAN_GATE_ID,
    PROTOCOL_GATE_KIND,
    require_approved_context,
)
from ..discovery.freeze import (
    FREEZE_GATE_ID,
    FREEZE_GATE_KIND,
    FreezeRepository,
)
from ..discovery.reporting import CoverageRepository
from ..drafting.workflow import (
    GATE_ID as BLUEPRINT_GATE_ID,
    GATE_KIND as BLUEPRINT_GATE_KIND,
    DraftingWorkflowRepository,
)
from ..extraction.workflow import (
    GATE_BATCH,
    GATE_CONFIG,
    GATE_KIND as EXTRACTION_GATE_KIND,
    GATE_OUTPUTS,
    GATE_PILOT,
    ExtractionWorkflowRepository,
)
from ..parsing.repository import ParsingRepository
from ..screening.workflow import ScreeningRepository
from ..storage.artifacts import ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository
from ..synthesis.workflow import (
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
    SynthesisWorkflowRepository,
)

__all__ = ["STAGES", "Stage", "resolve"]

#: The generic document type of a parsed-passages report (Step 07).
_PARSING_DOCUMENT_TYPE = "parsed_passages"

#: The generic document types of the review/tracing/delivery revisions.
_REVIEW_DOCUMENT_TYPE = "section_review"
_TRACING_DOCUMENT_TYPE = "origin_trace"
_DELIVERY_DOCUMENT_TYPE = "delivery"


class _Incomplete(Exception):
    """A stage is not complete (never surfaced to the caller directly)."""


@dataclass(frozen=True)
class Stage:
    """One pipeline stage descriptor.

    ``document_types`` identifies the stage's proposal revisions; ``check``
    validates the stage (raising when incomplete); ``gate`` is the exact named
    owner gate instance as ``(gate_kind, gate_id)`` (or ``None`` for a gate-less
    stage); ``produce`` is the next-action descriptor used when the stage has not
    started.
    """

    id: str
    title: str
    document_types: tuple[str, ...]
    gate: tuple[str, str] | None
    check: Callable[[Any, list], None]
    produce: dict


# --------------------------------------------------------------------------- #
# head-document scan
# --------------------------------------------------------------------------- #
def _ref(entry: dict) -> dict:
    return {
        "project_id": entry["project_id"],
        "artifact_id": entry["artifact_id"],
        "revision_id": entry["revision_id"],
    }


def _head_documents(store: Any) -> list[dict]:
    """Return the current head document of every artifact that decodes as JSON.

    A corrupt or non-JSON head is skipped: the resolver never trusts a stored
    document it cannot decode, and it never fails the whole read because one
    unrelated artifact is malformed.
    """
    artifacts = ArtifactRepository(store)
    project_id = store.project_id
    heads: list[dict] = []
    for artifact in artifacts.list_artifacts():
        head = artifacts.current_revision(artifact.artifact_id)
        if head is None:
            continue
        try:
            raw = artifacts.read_content(artifact.artifact_id, head.revision_id)
            document = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - a non-JSON head is simply not a proposal
            continue
        if not isinstance(document, dict):
            continue
        heads.append(
            {
                "project_id": project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": head.revision_id,
                "document_type": document.get("document_type"),
                "document": document,
            }
        )
    return heads


# --------------------------------------------------------------------------- #
# small helpers used by the check callables
# --------------------------------------------------------------------------- #
def _require_any(entries: list, fn: Callable[[dict], Any]) -> Any:
    """Return the first successful ``fn(entry)``; re-raise the last failure.

    An empty list raises :class:`_Incomplete` so the caller classifies the stage
    as ``not_started``.
    """
    last: Exception | None = None
    for entry in entries:
        try:
            return fn(entry)
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            last = exc
    if last is not None:
        raise last
    raise _Incomplete("no proposal revision exists for this stage")


# --------------------------------------------------------------------------- #
# per-stage checks (delegate to the stages' existing read/require APIs)
# --------------------------------------------------------------------------- #
def _check_discovery_plan(store: Any, entries: list) -> None:
    _require_any(
        entries,
        lambda e: require_approved_context(
            store,
            _ref(e),
            gate_kind=PROTOCOL_GATE_KIND,
            gate_id=PLAN_GATE_ID,
            expected_type="discovery_plan",
        ),
    )


def _check_discovery_coverage(store: Any, entries: list) -> None:
    repo = CoverageRepository(store)

    def _one(entry: dict) -> None:
        coverage = repo.get(_ref(entry))
        require_approved_context(
            store,
            coverage["payload"]["plan"]["ref"],
            gate_kind=PROTOCOL_GATE_KIND,
            gate_id=PLAN_GATE_ID,
            expected_type="discovery_plan",
        )

    _require_any(entries, _one)


def _check_discovery_freeze(store: Any, entries: list) -> None:
    repo = FreezeRepository(store)
    _require_any(entries, lambda e: repo.require_frozen(e["artifact_id"]))


def _check_acquisition(store: Any, entries: list) -> None:
    repo = AcquisitionRepository(store)
    _require_any(entries, lambda e: repo.require_acquired(e["artifact_id"]))


def _check_parsing(store: Any, entries: list) -> None:
    repo = ParsingRepository(store)

    def _one(entry: dict) -> None:
        report = repo.read_report(_ref(entry))
        if not report.get("verifiable"):
            raise _Incomplete("a parsing report exists but is not verifiable")

    _require_any(entries, _one)


def _check_screening_pilot(store: Any, entries: list) -> None:
    repo = ScreeningRepository(store)
    _require_any(entries, lambda e: repo.require_pilot(_ref(e)))


def _check_screening_exclusions(store: Any, entries: list) -> None:
    repo = ScreeningRepository(store)
    _require_any(entries, lambda e: repo.require_exclusions(_ref(e)))


def _check_extraction_config(store: Any, entries: list) -> None:
    repo = ExtractionWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_config(_ref(e)))


def _check_extraction_pilot(store: Any, entries: list) -> None:
    repo = ExtractionWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_pilot(_ref(e)))


def _check_extraction_batch(store: Any, entries: list) -> None:
    repo = ExtractionWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_batch(_ref(e)))


def _check_extraction_outputs(store: Any, entries: list) -> None:
    repo = ExtractionWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_for_synthesis(_ref(e)))


def _check_synthesis(store: Any, entries: list) -> None:
    repo = SynthesisWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_synthesis(_ref(e)))


def _check_framework(store: Any, entries: list) -> None:
    repo = SynthesisWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_framework(_ref(e)))


def _check_outline(store: Any, entries: list) -> None:
    repo = SynthesisWorkflowRepository(store)
    _require_any(entries, lambda e: repo.require_outline(_ref(e)))


def _check_drafting(store: Any, entries: list) -> None:
    if not entries:
        raise _Incomplete("no section blueprint exists yet")
    repo = DraftingWorkflowRepository(store)
    for entry in entries:
        repo.require_blueprint(_ref(entry))


def _check_review(store: Any, entries: list) -> None:
    if not entries:
        raise _Incomplete("no section review exists yet")
    for entry in entries:
        payload = entry["document"].get("payload") or {}
        if payload.get("status") != "complete":
            raise _Incomplete("a section review is not complete; human action may be required")


def _check_tracing(store: Any, entries: list) -> None:
    if not entries:
        raise _Incomplete("no origin trace exists yet")
    for entry in entries:
        payload = entry["document"].get("payload") or {}
        if payload.get("status") != "complete":
            raise _Incomplete("an origin trace is not complete; human action may be required")


def _check_delivery(store: Any, entries: list) -> None:
    if not entries:
        raise _Incomplete("no delivery state exists yet")
    for entry in entries:
        payload = entry["document"].get("payload") or {}
        if payload.get("status") not in ("approved", "exported"):
            raise _Incomplete("the delivery is not finally approved")


# --------------------------------------------------------------------------- #
# next-action descriptors (per stage, used when the stage has not started)
# --------------------------------------------------------------------------- #
def _human(role: str, command: str | None, library: str | None = None, inputs: dict | None = None) -> dict:
    data: dict[str, Any] = {"kind": "human", "role": role}
    if inputs:
        data["inputs"] = inputs
    else:
        data["inputs"] = {"command": command, "library": library}
    return data


#: The ordered stage table. The gate ``(kind, id)`` values are the exact named
#: owner gates the stages already define.
STAGES: tuple[Stage, ...] = (
    Stage(
        id="discovery.plan",
        title="brief/context and search plan",
        document_types=("discovery_plan",),
        gate=(PROTOCOL_GATE_KIND, PLAN_GATE_ID),
        check=_check_discovery_plan,
        produce=_human(
            "author",
            "review-article-agent discovery context import --artifact-id <PLAN_ARTIFACT_ID> "
            "--revision-id <REVISION_ID> --file <PLAN_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "discovery context import",
                "library": "review_article_agent.discovery.context.seed_context",
                "note": "import the brief/interview/seed/framework/scope/rubric/plan context documents, "
                "approve each seed and the combined plan through `gate prepare`/`gate confirm`",
            },
        ),
    ),
    Stage(
        id="discovery.coverage",
        title="discovery searches and coverage report",
        document_types=("discovery_coverage",),
        gate=None,
        check=_check_discovery_coverage,
        produce=_human(
            "author",
            "review-article-agent discovery report create --corpus-artifact-id <ID> "
            "--corpus-revision-id <ID> --plan-artifact-id <ID> --plan-revision-id <ID> "
            "--revision-id <ID> --request-id <ID> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "discovery report create",
                "library": "review_article_agent.discovery.reporting.CoverageRepository.record_report",
            },
        ),
    ),
    Stage(
        id="discovery.freeze",
        title="corpus freeze",
        document_types=("discovery_freeze_decision",),
        gate=(FREEZE_GATE_KIND, FREEZE_GATE_ID),
        check=_check_discovery_freeze,
        produce=_human(
            "author",
            "review-article-agent discovery freeze propose --corpus-artifact-id <ID> "
            "--corpus-revision-id <ID> --coverage-artifact-id <ID> --coverage-revision-id <ID> "
            "--decision freeze --entry-reviews-file <REVIEWS_JSON> --rationale <TEXT> "
            "--request-id <ID> --revision-id <ID> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "discovery freeze propose",
                "library": "review_article_agent.discovery.freeze.FreezeRepository.propose",
            },
        ),
    ),
    Stage(
        id="acquisition.manifest",
        title="acquisition manifest",
        document_types=("acquisition_manifest",),
        gate=(ACQUISITION_GATE_KIND, ACQUISITION_GATE_ID),
        check=_check_acquisition,
        produce=_human(
            "author",
            "review-article-agent acquisition manifest propose --freeze-artifact-id <ID> "
            "--freeze-revision-id <ID> --entries-file <ENTRIES_JSON> --rights-file <RIGHTS_JSON> "
            "--revision-id <ID> --request-id <ID>",
            inputs={
                "command": "acquisition manifest propose",
                "library": "review_article_agent.acquisition.manifest.AcquisitionRepository.propose",
            },
        ),
    ),
    Stage(
        id="parsing.reports",
        title="parsing reports",
        document_types=(_PARSING_DOCUMENT_TYPE,),
        gate=None,
        check=_check_parsing,
        produce=_human(
            "author",
            "review-article-agent parsing parse --source-artifact-id <ID> --source-revision-id <ID> "
            "--report-artifact-id <ID> --report-revision-id <ID> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "parsing parse",
                "library": "review_article_agent.parsing.repository.ParsingRepository.parse_stored_document",
            },
        ),
    ),
    Stage(
        id="screening.pilot",
        title="screening pilot",
        document_types=("screening_pilot",),
        gate=("pilot", "screening.pilot"),
        check=_check_screening_pilot,
        produce=_human(
            "author",
            "review-article-agent screening propose --kind pilot --artifact-id <ID> "
            "--revision-id <ID> --input-file <PILOT_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "screening propose --kind pilot",
                "library": "review_article_agent.screening.workflow.ScreeningRepository.propose",
            },
        ),
    ),
    Stage(
        id="screening.exclusions",
        title="screening batch and exclusion audit",
        document_types=("screening_audit",),
        gate=("exclusion_set", "screening.exclusions"),
        check=_check_screening_exclusions,
        produce=_human(
            "author",
            "review-article-agent screening propose --kind audit --artifact-id <ID> "
            "--revision-id <ID> --input-file <AUDIT_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "screening propose --kind audit",
                "library": "review_article_agent.screening.workflow.ScreeningRepository.propose",
            },
        ),
    ),
    Stage(
        id="extraction.config",
        title="extraction config",
        document_types=("extraction_config",),
        gate=(EXTRACTION_GATE_KIND, GATE_CONFIG),
        check=_check_extraction_config,
        produce=_human(
            "author",
            "review-article-agent extraction propose --kind config --artifact-id <ID> "
            "--revision-id <ID> --input-file <CONFIG_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "extraction propose --kind config",
                "library": "review_article_agent.extraction.workflow.ExtractionWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="extraction.pilot",
        title="extraction pilot",
        document_types=("extraction_pilot",),
        gate=(EXTRACTION_GATE_KIND, GATE_PILOT),
        check=_check_extraction_pilot,
        produce=_human(
            "author",
            "review-article-agent extraction propose --kind pilot --artifact-id <ID> "
            "--revision-id <ID> --input-file <PILOT_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "extraction propose --kind pilot",
                "library": "review_article_agent.extraction.workflow.ExtractionWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="extraction.batch",
        title="extraction batch",
        document_types=("extraction_batch",),
        gate=(EXTRACTION_GATE_KIND, GATE_BATCH),
        check=_check_extraction_batch,
        produce=_human(
            "author",
            "review-article-agent extraction propose --kind batch --artifact-id <ID> "
            "--revision-id <ID> --input-file <BATCH_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "extraction propose --kind batch",
                "library": "review_article_agent.extraction.workflow.ExtractionWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="extraction.outputs",
        title="extraction output audit",
        document_types=("extraction_audit",),
        gate=(EXTRACTION_GATE_KIND, GATE_OUTPUTS),
        check=_check_extraction_outputs,
        produce=_human(
            "author",
            "review-article-agent extraction propose --kind audit --artifact-id <ID> "
            "--revision-id <ID> --input-file <AUDIT_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "extraction propose --kind audit",
                "library": "review_article_agent.extraction.workflow.ExtractionWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="synthesis.memo",
        title="synthesis memo",
        document_types=("synthesis_synthesis",),
        gate=(GATE_SYNTHESIS_KIND, GATE_SYNTHESIS),
        check=_check_synthesis,
        produce=_human(
            "author",
            "review-article-agent synthesis propose --kind synthesis --artifact-id <ID> "
            "--revision-id <ID> --input-file <SYNTHESIS_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "synthesis propose --kind synthesis",
                "library": "review_article_agent.synthesis.workflow.SynthesisWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="synthesis.framework",
        title="framework",
        document_types=("synthesis_framework",),
        gate=(GATE_FRAMEWORK_KIND, GATE_FRAMEWORK),
        check=_check_framework,
        produce=_human(
            "author",
            "review-article-agent synthesis propose --kind framework --artifact-id <ID> "
            "--revision-id <ID> --input-file <FRAMEWORK_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "synthesis propose --kind framework",
                "library": "review_article_agent.synthesis.workflow.SynthesisWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="synthesis.outline",
        title="outline",
        document_types=("synthesis_outline",),
        gate=(GATE_OUTLINE_KIND, GATE_OUTLINE),
        check=_check_outline,
        produce=_human(
            "author",
            "review-article-agent synthesis propose --kind outline --artifact-id <ID> "
            "--revision-id <ID> --input-file <OUTLINE_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "synthesis propose --kind outline",
                "library": "review_article_agent.synthesis.workflow.SynthesisWorkflowRepository.propose",
            },
        ),
    ),
    Stage(
        id="drafting.blueprint",
        title="per-section workspace/blueprint/draft",
        document_types=("drafting_blueprint",),
        gate=(BLUEPRINT_GATE_KIND, BLUEPRINT_GATE_ID),
        check=_check_drafting,
        produce=_human(
            "author",
            "review-article-agent drafting propose --kind blueprint --artifact-id <ID> "
            "--revision-id <ID> --input-file <BLUEPRINT_JSON> --rights-file <RIGHTS_JSON>",
            inputs={
                "command": "drafting propose --kind blueprint",
                "library": "review_article_agent.drafting.workflow.DraftingWorkflowRepository.propose",
                "note": "the blueprint consumes the current approved outline, the section workspace, "
                "and the approved drafting package; its own gate is artifact_revision/drafting.blueprint",
            },
        ),
    ),
    Stage(
        id="review.sections",
        title="per-section review",
        document_types=(_REVIEW_DOCUMENT_TYPE,),
        gate=None,
        check=_check_review,
        produce=_human(
            "author",
            None,
            inputs={
                "command": None,
                "library": "review_article_agent.review.workflow.SectionReviewRepository.run",
                "note": "no CLI exists yet for review (kickoff 16 §9); run the review library directly",
            },
        ),
    ),
    Stage(
        id="tracing.origins",
        title="origin tracing",
        document_types=(_TRACING_DOCUMENT_TYPE,),
        gate=None,
        check=_check_tracing,
        produce=_human(
            "author",
            None,
            inputs={
                "command": None,
                "library": "review_article_agent.tracing.workflow.OriginTraceRepository.run",
                "note": "no CLI exists yet for tracing (kickoff 16 §9); run the tracing library directly",
            },
        ),
    ),
    Stage(
        id="delivery",
        title="delivery global review/approval/export",
        document_types=(_DELIVERY_DOCUMENT_TYPE,),
        gate=None,
        check=_check_delivery,
        produce=_human(
            "author",
            None,
            inputs={
                "command": None,
                "library": "review_article_agent.delivery.workflow.DeliveryRepository.start_global_review",
                "note": "no CLI exists yet for delivery (kickoff 16 §9); run the delivery library directly",
            },
        ),
    ),
)


# --------------------------------------------------------------------------- #
# classification + public entry point
# --------------------------------------------------------------------------- #
def _prepare_command(project_id: str, ref: dict, gate: tuple[str, str]) -> str:
    kind, gate_id = gate
    return (
        "review-article-agent gate prepare "
        f"--artifact-id {ref['artifact_id']} --revision-id {ref['revision_id']} "
        f"--gate-kind {kind} --gate-id {gate_id} "
        "--decision approved --rationale <TEXT> "
        f"--project {project_id}"
    )


def _gate_command(project_id: str, ref: dict, gate: tuple[str, str]) -> str:
    kind, gate_id = gate
    return (
        "review-article-agent gate confirm "
        f"--artifact-id {ref['artifact_id']} --revision-id {ref['revision_id']} "
        f"--gate-kind {kind} --gate-id {gate_id} "
        "--decision approved --rationale <TEXT> "
        "--snapshot-digest <SHA256_FROM_GATE_PREPARE> "
        f"--project {project_id}"
    )


def _failure_detail(stage: Stage) -> str:
    """A curated, value-free detail for a present-but-incomplete stage."""
    if stage.gate is not None:
        return (
            f"the {stage.title} proposal exists and its named gate "
            f"{stage.gate[0]}/{stage.gate[1]} is satisfied, but the stage's inputs "
            f"are stale or invalid; re-check the exact upstream revisions and any "
            f"recorded human action"
        )
    return (
        f"the {stage.title} proposal exists but the stage is not yet complete "
        f"(a stale/invalid input or an outstanding human action); re-check the "
        f"stage's inputs"
    )


def resolve(store: Any, registry: Any = None) -> dict:
    """Determine the current pipeline stage from durable state (read-only).

    ``store`` is an already-verified :class:`ProjectStore`. ``registry`` is
    accepted for signature compatibility with the future ``pipeline run``
    orchestrator and is not read here. Returns a JSON-ready dict::

        {
          "stage": <stage id> | "complete",
          "completed": [<stage id>, ...],
          "blocked_on": None | {"kind": ..., "detail": ..., "next_command": ...},
          "next_action": None | {"kind": "job"|"human", "role"/"job": ..., "inputs": {...}},
          "warnings": [...],
        }

    Never writes anything and never calls a model or the network.
    """
    heads = _head_documents(store)
    present_types = {head["document_type"] for head in heads}

    completed: list[str] = []
    warnings: list[str] = []
    current: Stage | None = None
    blocked_on: dict | None = None
    next_action: dict | None = None

    for index, stage in enumerate(STAGES):
        stage_entries = [
            head for head in heads if head["document_type"] in stage.document_types
        ]
        later_started = any(
            other.document_types and any(
                doc_type in present_types for doc_type in other.document_types
            )
            for other in STAGES[index + 1 :]
        )

        if not stage_entries:
            if later_started:
                warnings.append(
                    f"{stage.id}: no proposal revision found; treated as skipped "
                    f"because a later stage has already begun"
                )
                continue
            current = stage
            next_action = dict(stage.produce)
            blocked_on = None
            break

        try:
            stage.check(store, stage_entries)
        except Exception:  # noqa: BLE001 - every incomplete stage is classified
            current = stage
            if stage.gate is not None:
                approvals = HumanApprovalRepository(store)
                gate_ref = None
                for entry in stage_entries:
                    if not approvals.named_gate_satisfied(
                        entry["artifact_id"],
                        gate_kind=stage.gate[0],
                        gate_id=stage.gate[1],
                    ):
                        gate_ref = _ref(entry)
                        break
                if gate_ref is not None:
                    blocked_on = {
                        "kind": "human_gate",
                        "detail": (
                            f"the {stage.title} proposal exists but its exact named "
                            f"gate {stage.gate[0]}/{stage.gate[1]} is not currently "
                            f"active and approved. Run the prepare command first to "
                            f"obtain the snapshot digest: `{_prepare_command(store.project_id, gate_ref, stage.gate)}`"
                        ),
                        "next_command": _gate_command(
                            store.project_id, gate_ref, stage.gate
                        ),
                    }
                    next_action = {
                        "kind": "human",
                        "role": "gate",
                        "inputs": {
                            "artifact_id": gate_ref["artifact_id"],
                            "revision_id": gate_ref["revision_id"],
                            "gate_kind": stage.gate[0],
                            "gate_id": stage.gate[1],
                        },
                    }
                    break
            detail = _failure_detail(stage)
            blocked_on = {
                "kind": "failure",
                "detail": detail,
                "next_command": stage.produce.get("inputs", {}).get("command"),
            }
            next_action = dict(stage.produce)
            warnings.append(f"{stage.id}: {detail}")
            break

        completed.append(stage.id)

    return {
        "stage": current.id if current is not None else "complete",
        "completed": completed,
        "blocked_on": blocked_on,
        "next_action": next_action,
        "warnings": warnings,
        "project_id": store.project_id,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }
