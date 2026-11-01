"""Bounded **direct extraction/quality runner** (Step 09, S9-RUNNER).

Authority
---------

* ``docs/implementation/09-extraction.md`` (grounded extraction from *original*
  text only, explicit coverage/omission accounting, no extraction from summaries,
  tables/supplements are inputs) and ``docs/specification.md`` line 32.
* ``docs/decisions/004-step09-direct-extraction.md`` — the user-approved direct
  OpenRouter exception extension for bounded, tool-free extraction and quality
  checking. No OpenCode production restriction is relaxed.
* ``docs/contracts/02-gates-and-runtime.md`` §5.6 (reserve before dispatch;
  ambiguous reserve retained) and §6.3 (exact-decimal direct-call cost).

This module is the *application* boundary the pure adapters deliberately are not:
it owns gates, rights, budget, the durable ledger, schema validation, and human
escalation. Model output is **data only**; it is never authority. The
configuration document, its validator, the pilot gate, and the quality validator
are owned by :mod:`review_article_agent.extraction.workflow` and are **reused**,
never duplicated here.

Public seam
-----------

:func:`require_run` returns **exactly** the keys of
:data:`review_article_agent.extraction.workflow.RECEIPT_KEYS` (the workflow's
single read seam). It re-derives everything from the store under one nested
transaction and fails closed unless the receipt is bound to the exact durable
accepted dispatch, task (``input_refs``), proposal, and reconstructed request
identities. :func:`_require_run_details` exposes the runner-internal metadata for
auditing; the public projection never carries it.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping

from ..acquisition.manifest import AcquisitionRepository, ManifestNotAcquiredError
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_rights_record,
)
from ..parsing.repository import ParsingRepository, _NestedStore
from ..runtime import extraction as runtime_extraction
from ..runtime import lead
from ..runtime.dispatch import RuntimeDispatchRepository, retry_request_ids
from ..runtime.lead import LeadConfig
from ..runtime.retry import RETRY_MAX_ATTEMPTS, next_attempt
from ..runtime.roles import (
    BoundedTask,
    ModelRef,
    RoleConfig,
    RoleConfiguration,
)
from ..storage.agent_runtime import RuntimeRepository, validate_version_set
from ..storage.artifacts import ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository
from . import schema as extraction_schema
from . import workflow as extraction_workflow
from ..discovery import lead_runner as _lead
from .workflow import ExtractionWorkflowRepository

__all__ = [
    "EXTRACTION_STAGE",
    "PROMPT_VERSION",
    "ADAPTER_REVISION",
    "HOSTED_USE_DOCUMENT_TYPE",
    "RUN_DOCUMENT_TYPE",
    "HOSTED_USE_GATE_KIND",
    "HOSTED_USE_GATE_PREFIX",
    "PURPOSES",
    "MODES",
    "PURPOSE_ROLES",
    "ExtractionRunnerError",
    "ExtractionHostedUseError",
    "ExtractionAuthorizationError",
    "ExtractionRightsError",
    "ExtractionReceiptError",
    "hosted_use_gate_id",
    "validate_hosted_use_document",
    "propose_hosted_use",
    "require_pilot",
    "require_run",
    "execute_run",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1

EXTRACTION_STAGE = "extraction"
PROMPT_VERSION = "extraction.direct.v1"
ADAPTER_REVISION = "extraction.direct.v1"

HOSTED_USE_DOCUMENT_TYPE = "extraction_hosted_use"
RUN_DOCUMENT_TYPE = "extraction_run"

HOSTED_USE_GATE_KIND = "batch"
HOSTED_USE_GATE_PREFIX = "extraction.hosted_use."

PURPOSES = ("extraction", "quality")
MODES = ("pilot", "bulk")
PURPOSE_ROLES = {"extraction": "extractor", "quality": "fact_checker"}

TASK_SIZE_LIMIT = 262_144
MAX_IDENTIFIER_LEN = 512

#: The default backoff sleeper for the bounded direct-call retry (ADR 006); tests
#: inject a no-op via ``execute_run(sleep=...)`` or by patching this alias.
_SLEEP = time.sleep

#: The longest deterministic retry suffix appended to a base request id
#: (``-r{RETRY_MAX_ATTEMPTS - 1}``); a base must leave room for it so every
#: attempt's request id stays within :data:`MAX_IDENTIFIER_LEN` (ADR 006).
_RETRY_SUFFIX_LEN = len(f"-r{RETRY_MAX_ATTEMPTS - 1}")

#: Fixed limitation line added to the material when any source is a PDF report,
#: so a model can never treat extracted PDF text as reliable column-ordered text
#: or as interpreted figures/tables.
PDF_MATERIAL_LIMITATION = (
    "PDF text: column order/figures/tables unverified; figures and tables "
    "require human review"
)

#: Fixed limitation line added to the material when any source report declares
#: non-text assets that require review (figures/tables/supplements) and is not a
#: PDF (a PDF already carries :data:`PDF_MATERIAL_LIMITATION`). The structural
#: text was read, so the report is processed, but a model must never treat a
#: value that lives only in a figure/table/supplement as extracted evidence.
NON_TEXT_MATERIAL_LIMITATION = (
    "Figures, tables and supplements were not read; any value shown only in a "
    "figure/table must be marked missing, not inferred."
)

_ENVELOPE_KEYS = ("schema_version", "document_type", "payload")
_EVIDENCE_KEYS = ("ref", "verified_at")
_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_INPUT_REF_KEYS = ("project_id", "artifact_id", "revision_id", "digest")

_HOSTED_PAYLOAD_KEYS = (
    "config_ref",
    "report_refs",
    "acquisition_ref",
    "purpose",
    "extraction_ref",
    "provider_terms",
    "privacy_review",
    "pricing_evidence",
)

_RUN_PAYLOAD_KEYS = (
    "purpose",
    "config_ref",
    "report_refs",
    "source_refs",
    "extraction_ref",
    "acquisition_ref",
    "request_id",
    "dispatch_id",
    "task_id",
    "proposal_record_id",
    "proposal_digest",
    "observed_model",
    "request_sha256",
    "input_digest",
    "version_digest",
    "instructions_sha256",
    "mode",
    "pilot_ref",
    "outcome",
    "raw_output_sha256",
)

_RESULT_KIND_PROPOSAL = "extraction_proposal"
_RESULT_KIND_ESCALATION = "extraction_escalation"
_RESULT_KIND_REFUSED = "extraction_refused"
_RESULT_KIND_PROTOCOL = "extraction_protocol_error"


# --------------------------------------------------------------------------- #
# Errors (static messages; never echo input)
# --------------------------------------------------------------------------- #
class ExtractionRunnerError(ReviewAgentError):
    """A bounded extraction/quality run failed before any dispatch was issued."""


class ExtractionHostedUseError(ExtractionRunnerError):
    """An extraction hosted-use document is malformed or mismatched."""


class ExtractionAuthorizationError(ExtractionRunnerError):
    """The exact out-of-band gate, acquisition, or execution token is not active."""


class ExtractionRightsError(ExtractionRunnerError):
    """A pinned source's rights record does not permit the hosted transfer."""


class ExtractionReceiptError(ExtractionRunnerError):
    """A run receipt does not match its durable accepted ledger evidence."""


# --------------------------------------------------------------------------- #
# Reused helpers (lead runner + nested store) + this-runner error wrappers
# --------------------------------------------------------------------------- #
_read_current_revision = _lead._read_current_revision
_require_mapping = _lead._require_mapping
_require_exact_keys = _lead._require_exact_keys
_validate_evidence = _lead._validate_evidence
_parse_json_object = _lead._parse_json_object
_sha256_hex = _lead._sha256_hex


def _assert_token_active(registry, store, activation_token: str) -> None:
    try:
        _lead._assert_token_active(registry, store, activation_token)
    except _lead.HostedUseAuthorizationError as exc:
        raise ExtractionAuthorizationError(str(exc)) from exc


def _verify_hosted_rights(store, ref) -> None:
    try:
        _lead._verify_hosted_rights(store, ref)
    except _lead.HostedUseRightsError as exc:
        raise ExtractionRightsError(str(exc)) from exc


def _require_identifier(value: object, field: str) -> str:
    try:
        normalized = validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise ExtractionRunnerError(f"{field}: must be a valid opaque identifier") from exc
    if len(normalized) > MAX_IDENTIFIER_LEN:
        raise ExtractionRunnerError(f"{field}: is too long")
    return normalized


def _require_same_project_ref(store, value: object, field: str) -> dict:
    try:
        return _lead._require_same_project_ref(store, value, field)
    except ExtractionRunnerError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to this runner's error
        raise ExtractionRunnerError(f"{field}: must be an exact same-project RefRevision") from exc


def _ref_offline(value: object, field: str) -> dict:
    ref = _require_mapping(value, field)
    _require_exact_keys(ref, _REF_KEYS, field)
    return {
        "project_id": _require_identifier(ref["project_id"], f"{field}.project_id"),
        "artifact_id": _require_identifier(ref["artifact_id"], f"{field}.artifact_id"),
        "revision_id": _require_identifier(ref["revision_id"], f"{field}.revision_id"),
    }


def _assert_gate_satisfied(store, artifact_id: str, gate_kind: str, gate_id: str) -> None:
    if not HumanApprovalRepository(store).named_gate_satisfied(
        artifact_id, gate_kind=gate_kind, gate_id=gate_id
    ):
        raise ExtractionAuthorizationError(
            "the exact required out-of-band gate is not currently satisfied by an "
            "active, approved human approval"
        )


def hosted_use_gate_id(purpose: str) -> str:
    """The exact named gate id of a hosted-use purpose (``batch`` gate kind)."""
    return HOSTED_USE_GATE_PREFIX + purpose


# --------------------------------------------------------------------------- #
# Hosted-use document (this runner's only owned document type)
# --------------------------------------------------------------------------- #
def _validate_ref_list(value: object, field: str) -> list[dict]:
    if not isinstance(value, list):
        raise ExtractionHostedUseError(f"{field}: must be a list of RefRevision objects")
    refs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(value):
        item_field = f"{field}[{index}]"
        ref = _ref_offline(raw, item_field)
        key = (ref["project_id"], ref["artifact_id"], ref["revision_id"])
        if key in seen:
            raise ExtractionHostedUseError(f"{item_field}: duplicate reference")
        seen.add(key)
        refs.append(ref)
    if len({ref["project_id"] for ref in refs}) > 1:
        raise ExtractionHostedUseError(f"{field}: all references must share one project")
    return refs


def validate_hosted_use_document(document: object) -> dict:
    """Validate one extraction hosted-use document; return a plain copy.

    The payload must be **exactly** ``{config_ref, report_refs, acquisition_ref,
    purpose, extraction_ref, provider_terms, privacy_review, pricing_evidence}``.
    ``acquisition_ref`` is a same-project RefRevision or ``null`` (the explicit
    pilot exemption); ``extraction_ref`` is required for ``purpose == "quality"``
    and forbidden for ``"extraction"``. This is a *description* of an
    authorization; the out-of-band gate is the authority.
    """
    try:
        return _validate_hosted_inner(document)
    except ExtractionHostedUseError:
        raise
    except _lead.HostedUseDocumentError as exc:
        raise ExtractionHostedUseError(str(exc)) from exc


def _validate_hosted_inner(document: object) -> dict:
    envelope = _require_mapping(document, "document")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "document")
    version = envelope["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ExtractionHostedUseError("document.schema_version: must be an integer")
    if version != SCHEMA_VERSION:
        raise ExtractionHostedUseError(
            f"document.schema_version: must be the supported version {SCHEMA_VERSION}"
        )
    if envelope["document_type"] != HOSTED_USE_DOCUMENT_TYPE:
        raise ExtractionHostedUseError(
            "document.document_type: is not the extraction hosted-use document type"
        )
    payload = _require_mapping(envelope["payload"], "document.payload")
    _require_exact_keys(payload, _HOSTED_PAYLOAD_KEYS, "extraction_hosted_use.payload")

    purpose = payload["purpose"]
    if purpose not in PURPOSES:
        raise ExtractionHostedUseError(
            "extraction_hosted_use.purpose: is not an allowed purpose"
        )
    config_ref = _ref_offline(payload["config_ref"], "config_ref")
    report_refs = _validate_ref_list(payload["report_refs"], "extraction_hosted_use.report_refs")
    if not report_refs:
        raise ExtractionHostedUseError("extraction_hosted_use.report_refs: must not be empty")

    acquisition_ref = payload["acquisition_ref"]
    if acquisition_ref is not None:
        acquisition_ref = _ref_offline(acquisition_ref, "acquisition_ref")

    extraction_ref = payload["extraction_ref"]
    if extraction_ref is not None:
        extraction_ref = _ref_offline(extraction_ref, "extraction_ref")
        if purpose != "quality":
            raise ExtractionHostedUseError(
                "extraction_hosted_use.extraction_ref: is only allowed for purpose 'quality'"
            )
    elif purpose == "quality":
        raise ExtractionHostedUseError(
            "extraction_hosted_use.extraction_ref: is required for purpose 'quality'"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": HOSTED_USE_DOCUMENT_TYPE,
        "payload": {
            "config_ref": config_ref,
            "report_refs": report_refs,
            "acquisition_ref": acquisition_ref,
            "purpose": purpose,
            "extraction_ref": extraction_ref,
            "provider_terms": _validate_evidence(
                payload["provider_terms"], "extraction_hosted_use.provider_terms"
            ),
            "privacy_review": _validate_evidence(
                payload["privacy_review"], "extraction_hosted_use.privacy_review"
            ),
            "pricing_evidence": _validate_evidence(
                payload["pricing_evidence"], "extraction_hosted_use.pricing_evidence"
            ),
        },
    }


def propose_hosted_use(
    *,
    config_ref: Mapping[str, str],
    report_refs: list[Mapping[str, str]],
    purpose: str,
    extraction_ref: Mapping[str, str] | None,
    provider_terms: Mapping[str, str],
    privacy_review: Mapping[str, str],
    pricing_evidence: Mapping[str, str],
    acquisition_ref: Mapping[str, str] | None = None,
) -> dict:
    """Build (but do not persist) an extraction hosted-use document.

    Pure: no I/O, no write, no rights invented. The caller persists the returned
    document through the normal artifact repository with **caller-supplied**
    rights and pins ``config_ref`` + ``report_refs`` + ``acquisition_ref`` +
    ``extraction_ref`` as dependencies so the gate snapshot closes the closure.
    """
    document = {
        "schema_version": SCHEMA_VERSION,
        "document_type": HOSTED_USE_DOCUMENT_TYPE,
        "payload": {
            "config_ref": dict(config_ref),
            "report_refs": [dict(ref) for ref in report_refs],
            "acquisition_ref": None if acquisition_ref is None else dict(acquisition_ref),
            "purpose": purpose,
            "extraction_ref": None if extraction_ref is None else dict(extraction_ref),
            "provider_terms": dict(provider_terms),
            "privacy_review": dict(privacy_review),
            "pricing_evidence": dict(pricing_evidence),
        },
    }
    validate_hosted_use_document(document)
    return document


# --------------------------------------------------------------------------- #
# Document loading (byte-integrity, canonical, current)
# --------------------------------------------------------------------------- #
def _load_hosted_use(store, ref: object):
    normalized = _require_same_project_ref(store, ref, "ref")
    content, revision = _read_current_revision(store, normalized)
    document = validate_hosted_use_document(_parse_json_object(content, "hosted-use"))
    if canonical_json(document) != content:
        raise ExtractionRunnerError(
            "hosted-use: the stored revision is not the canonical document encoding"
        )
    return normalized, revision, document


def _load_run_receipt(store, ref: object):
    normalized = _require_same_project_ref(store, ref, "ref")
    content, revision = _read_current_revision(store, normalized)
    parsed = _parse_json_object(content, "run")
    envelope = _require_mapping(parsed, "run")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "run")
    if envelope.get("schema_version") != SCHEMA_VERSION or (
        envelope.get("document_type") != RUN_DOCUMENT_TYPE
    ):
        raise ExtractionReceiptError("run: is not an extraction run receipt")
    payload = _require_mapping(envelope.get("payload"), "run.payload")
    _require_exact_keys(payload, _RUN_PAYLOAD_KEYS, "run.payload")
    if canonical_json(parsed) != content:
        raise ExtractionReceiptError("run: the stored revision is not canonical")
    return normalized, revision, payload


# --------------------------------------------------------------------------- #
# Material derivation (passages + assets + original sources)
# --------------------------------------------------------------------------- #
def _report_passages(report: dict) -> list[dict]:
    artifact_id = report["report_ref"]["artifact_id"]
    report_ref = dict(report["report_ref"])
    report_ref_string = canonical_json(report_ref).decode("utf-8")
    source_ref = dict(report["source_ref"])
    fmt = report.get("format")
    non_text_review = bool(report["payload"].get("non_text_assets_require_review"))
    out: list[dict] = []
    for passage in report["payload"]["passages"]:
        original_id = passage["passage_id"]
        page = passage.get("page")
        page = page if isinstance(page, int) and not isinstance(page, bool) else None
        out.append(
            {
                "passage_id": f"{artifact_id}:{original_id}",
                "source_key": artifact_id,
                "text": passage["text"],
                "raw_text": passage.get("raw_text"),
                "section_path": list(passage.get("section_path", [])),
                "page": page,
                "format": fmt,
                "non_text_assets_require_review": non_text_review,
                "report_ref": report_ref_string,
                "source_ref": source_ref,
                "locator": {
                    "report_ref": report_ref,
                    "source_ref": source_ref,
                    "passage_id": original_id,
                    "section_path": list(passage.get("section_path", [])),
                },
            }
        )
    # Every declared structural asset (table/figure/supplement handle) is surfaced
    # as its own passage with a globally unique asset id, so the model must account
    # for it in coverage -- a handle is never silently dropped.
    for index, asset in enumerate(report["payload"].get("assets", [])):
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or asset_id.strip() == "":
            asset_id = f"{asset.get('kind', 'asset')}-{index}"
        caption = asset.get("caption")
        caption = caption if isinstance(caption, str) else ""
        out.append(
            {
                "passage_id": f"{artifact_id}:asset-{index}",
                "source_key": artifact_id,
                "text": caption,
                "raw_text": caption or None,
                "asset_ids": [f"{artifact_id}:{asset_id}:{index}"],
                "asset_kind": asset.get("kind"),
                "section_path": [],
                "page": None,
                "format": fmt,
                "non_text_assets_require_review": non_text_review,
                "report_ref": report_ref_string,
                "source_ref": source_ref,
                "locator": {
                    "report_ref": report_ref,
                    "source_ref": source_ref,
                    "asset_id": f"{artifact_id}:{asset_id}:{index}",
                },
            }
        )
    return out


def _check_source_keys(raw: dict, passages: list[dict]) -> None:
    """Every declared ``source_key`` must belong to an actually-read report.

    The schema treats ``source_records.report_ref`` as an opaque string, so this
    binds each model ``source_key`` to the exact verified report ref the runner
    read (and rejects an unknown key even when it carries no findings), and, when
    a ``report_ref`` is declared, requires it to be that exact report ref string.
    """
    actual: dict[str, str] = {}
    for passage in passages:
        key = passage.get("source_key")
        if key is not None:
            actual.setdefault(key, passage.get("report_ref"))
    records = raw.get("source_records")
    if not isinstance(records, list) or not records:
        raise ExtractionRunnerError("source_records: must declare at least one report")
    for record in records:
        if not isinstance(record, dict):
            raise ExtractionRunnerError("source_records: an entry is malformed")
        key = record.get("source_key")
        if key not in actual:
            raise ExtractionRunnerError(
                "source_records: declares a source_key that is not a read report"
            )
        declared = record.get("report_ref")
        if declared is not None and declared != actual[key]:
            raise ExtractionRunnerError(
                "source_records: report_ref does not match the read report"
            )


def _derive_material(store, report_refs: list[dict], *, config: dict):
    """Read each parsed report, validate it, and build the passage pool.

    Every report must be the current revision, produced by the configured current
    parser/normalization, and have parsed successfully (``status == "parsed"``).
    A PDF report must additionally be ``verifiable`` (reproducible with its
    recorded pypdf version), like an XML report's current-parser reparse. Declared
    non-text assets are included as required coverage handles; a report whose
    structural parse failed ("needs_review"/"rejected") is a human block. The
    ORIGINAL source refs are the exact ``derived_from`` documents, checked current
    with intact bytes and hosted-transfer rights.
    """
    parser = ParsingRepository(store)
    artifacts = ArtifactRepository(store)
    passages: list[dict] = []
    source_refs: list[dict] = []
    seen_sources: set[tuple[str, str, str]] = set()
    for ref in report_refs:
        report = parser.read_report(ref)
        if report["parser_version"] != config["parser"]:
            raise ExtractionRunnerError(
                "report: was produced by a different parser version than the config pins"
            )
        if report["normalization_version"] != config["normalization"]:
            raise ExtractionRunnerError(
                "report: was produced by a different normalization version than the config pins"
            )
        if report["parser_version_changed"] or report["normalization_version_changed"]:
            raise ExtractionRunnerError(
                "report: is stale relative to the current parser/normalization version"
            )
        if report["status"] != "parsed" or report["human_action_required"]:
            raise ExtractionRunnerError(
                "report: the structural parse failed; unreadable original evidence "
                "requires human action"
            )
        # A PDF report is usable original text only when this build can
        # reproduce its passages (exact recorded pypdf version). An unverified
        # PDF report (dependency absent, version drift) is refused like any other
        # unreadable original evidence; XML reports are already reproducible when
        # they pass the current-parser checks above.
        if report["format"] == "pdf" and not report["verifiable"]:
            raise ExtractionRunnerError(
                "report: the structural parse failed; unreadable original evidence "
                "requires human action"
            )
        if not report["payload"]["passages"] and not report["payload"].get("assets"):
            raise ExtractionRunnerError("report: carries no usable original material")
        source = dict(report["source_ref"])
        # The exact original source must still be current and byte-intact.
        head = artifacts.current_revision(source["artifact_id"])
        if head is None or head.revision_id != source["revision_id"]:
            raise ExtractionRunnerError(
                "source: the original source is not the artifact's current revision"
            )
        _read_current_revision(store, source)
        passages.extend(_report_passages(report))
        key = (source["project_id"], source["artifact_id"], source["revision_id"])
        if key not in seen_sources:
            seen_sources.add(key)
            source_refs.append(source)
    if not passages:
        raise ExtractionRunnerError("reports: produced no usable material")
    for source in source_refs:
        _verify_hosted_rights(store, source)
    return passages, source_refs


# --------------------------------------------------------------------------- #
# Deterministic instructions + material (shared by execute and read)
# --------------------------------------------------------------------------- #
def _assemble_material(passages: list[dict]) -> str:
    sources: dict[str, str] = {}
    for passage in passages:
        if passage.get("source_key") is not None:
            sources.setdefault(passage["source_key"], passage.get("report_ref") or "")
    lines = ["[declared sources]"]
    for key in sorted(sources):
        lines.append(f"[source {key}] report_ref={sources[key]}")
    lines.append("[original passages]")
    for passage in passages:
        section = "/".join(passage.get("section_path") or [])
        header = "[passage {pid}] source_key={sk} section={section}".format(
            pid=passage["passage_id"], sk=passage["source_key"], section=section
        )
        page = passage.get("page")
        if isinstance(page, int) and not isinstance(page, bool):
            header += f" page={page}"
        assets = passage.get("asset_ids")
        if assets:
            header += " asset_ids=[" + ",".join(assets) + "]"
        lines.append(header)
        lines.append(passage["text"])
    if any(passage.get("format") == "pdf" for passage in passages):
        lines.append("[limitations]")
        lines.append(PDF_MATERIAL_LIMITATION)
    if any(
        passage.get("non_text_assets_require_review") and passage.get("format") != "pdf"
        for passage in passages
    ):
        lines.append("[limitations]")
        lines.append(NON_TEXT_MATERIAL_LIMITATION)
    return "\n".join(lines)


def _extraction_instructions(config: dict) -> str:
    return (
        config["extraction_instructions"]
        + " (revision: "
        + config["extraction_instructions_revision"]
        + ")\n\n"
        + extraction_schema.EXTRACTION_INSTRUCTIONS
        + "\nEXAMPLE OUTPUT SHAPE (illustrative only; not evidence):\n"
        + canonical_json(extraction_schema.raw_extraction_example()).decode("utf-8")
    )


_QUALITY_CONTRACT = (
    "Task: quality checking and escalation review of a prior extraction against the "
    "ORIGINAL passages.\nReturn ONE JSON object with exactly these keys:\n"
    "- checks: an object with exactly these boolean keys: "
    + ", ".join(extraction_workflow.QUALITY_CHECKS)
    + ".\n"
    "- issues: an array; each item is {\"severity\": \"blocking\"|\"major\"|\"minor\""
    "|\"style\", \"description\": <string>, \"corrective_action\": <string or null>}.\n"
    "- escalation: \"none\"|\"human\"|\"stronger_model\".\n"
    "Explicitly seek OMISSIONS and QUALIFICATIONS (things present in the original but "
    "missing or softened in the extraction), not only re-check returned fields. Never "
    "invent a universal quality score."
)


def _quality_instructions(config: dict) -> str:
    return (
        config["quality_instructions"]
        + " (revision: "
        + config["quality_instructions_revision"]
        + ")\n\n"
        + _QUALITY_CONTRACT
    )


def _build_plan(*, purpose: str, config: dict, passages: list[dict], upstream_raw):
    model_map = config["extraction_model"] if purpose == "extraction" else config["quality_model"]
    if purpose == "extraction":
        instructions = _extraction_instructions(config)
        material = _assemble_material(passages)
    else:
        instructions = _quality_instructions(config)
        material = (
            _assemble_material(passages)
            + "\n[prior extraction output]\n"
            + canonical_json(upstream_raw).decode("utf-8")
        )
    plan = runtime_extraction.plan_extraction(
        LeadConfig.from_mapping(model_map), purpose=purpose, instructions=instructions, text=material
    )
    return plan, model_map


# --------------------------------------------------------------------------- #
# Identity digests / role config / version set
# --------------------------------------------------------------------------- #
def _input_refs(store, config_ref, report_refs, source_refs, extraction_ref) -> list[dict]:
    refs = [config_ref, *report_refs]
    if extraction_ref is not None:
        refs.append(extraction_ref)
    refs.extend(source_refs)
    out = []
    for ref in refs:
        content, _revision = _read_current_revision(store, ref)
        out.append(
            {
                "project_id": store.project_id,
                "artifact_id": ref["artifact_id"],
                "revision_id": ref["revision_id"],
                "digest": _sha256_hex(content),
            }
        )
    return out


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


def _version_digest(purpose: str, config_map: Mapping[str, Any]) -> str:
    return _sha256_hex(
        canonical_json(
            {
                "model_id": config_map["model_id"],
                "upstream_provider": config_map["upstream_provider"],
                "instructions_revision": config_map["instructions_revision"],
                "max_output_tokens": config_map["max_output_tokens"],
                "context_tokens": config_map["context_tokens"],
                "purpose": purpose,
                "prompt_version": PROMPT_VERSION,
                "endpoint": lead.ENDPOINT,
            }
        )
    )


def _reported_versions(input_digest: str, model_ref: str, request_sha256: str) -> dict:
    versions = {
        "context_kind": "model_derived",
        "input_digest": input_digest,
        "route": "direct_call",
        "model": {
            "state": "known",
            "ref": {"version": model_ref, "digest": _sha256_hex(canonical_json({"model": model_ref}))},
        },
        "prompt": {"state": "known", "ref": {"version": PROMPT_VERSION, "digest": request_sha256}},
        "rubric": {"state": "not_applicable"},
        "parser": {"state": "not_applicable"},
        "adapter": {
            "state": "known",
            "ref": {"version": ADAPTER_REVISION, "digest": _sha256_hex(canonical_json({"endpoint": lead.ENDPOINT}))},
        },
    }
    return validate_version_set(versions, consumed=("model", "prompt", "adapter"))


def _role_configuration(purpose: str, config_map: Mapping[str, Any]) -> RoleConfiguration:
    try:
        model = ModelRef(
            provider_id="openrouter", model_id=config_map["model_id"], variant="direct"
        )
        return RoleConfiguration(roles=(RoleConfig(role=PURPOSE_ROLES[purpose], model=model, steps=1),))
    except ReviewAgentError as exc:
        raise ExtractionRunnerError(
            "config: the configured model is not a usable direct-route model reference"
        ) from exc


# --------------------------------------------------------------------------- #
# Acquisition + pre-send revalidation
# --------------------------------------------------------------------------- #
def _require_config(store, config_ref, workflow):
    """The workflow's config authority, surfaced as a runner error (fail closed)."""
    try:
        return workflow.require_config(config_ref)
    except extraction_workflow.ExtractionWorkflowError as exc:
        raise ExtractionAuthorizationError(
            "config: is not the approved, valid extraction configuration"
        ) from exc


def _bare_ref(ref) -> dict:
    return {key: ref[key] for key in ("project_id", "artifact_id", "revision_id")}


def _triple(ref) -> tuple:
    return (ref["project_id"], ref["artifact_id"], ref["revision_id"])


def _require_acquired(store, acquisition_ref, source_refs) -> None:
    """Require an acquired manifest AND that the digested sources are its roster.

    The manifest being complete is not enough: every ORIGINAL source about to be
    digested must be a ``use_full_text`` entry's ``source_ref`` (or a declared
    required-asset source), so an unrelated acquired corpus cannot authorize an
    arbitrary document's digestion.
    """
    if acquisition_ref is None:
        raise ExtractionAuthorizationError(
            "acquisition: bulk extraction requires an acquired manifest reference"
        )
    try:
        acquired = AcquisitionRepository(store).require_acquired(at_ref=acquisition_ref)
    except ManifestNotAcquiredError as exc:
        raise ExtractionAuthorizationError(
            "acquisition: the referenced manifest is not currently acquired"
        ) from exc
    allowed: set[str] = set()
    for entry in acquired["payload"]["entries"]:
        if entry.get("disposition") == "use_full_text" and entry.get("source_ref"):
            allowed.add(canonical_json(_bare_ref(entry["source_ref"])))
        for asset in entry.get("required_assets") or []:
            if asset.get("source_ref"):
                allowed.add(canonical_json(_bare_ref(asset["source_ref"])))
    wanted = {canonical_json(_bare_ref(ref)) for ref in source_refs}
    if not wanted or not wanted <= allowed:
        raise ExtractionAuthorizationError(
            "acquisition: the sources to be digested are not in the acquired "
            "manifest's use_full_text roster"
        )


def _revalidate_before_send(
    store, *, registry, activation_token, hosted_ref, purpose, mode,
    config_ref, report_refs, extraction_ref, acquisition_ref, pilot_ref,
):
    """Point-in-time revalidation immediately before the POST (no lock over network).

    One nested transaction re-derives the exact config current bytes/gate, the
    original sources' current bytes and hosted rights, the upstream quality
    extraction receipt, and (in bulk mode) the approved pilot and the acquired
    manifest's source membership -- so an input, a revoked pilot, or a revised
    config edited between material derivation and the reserve cannot be digested.
    """
    with _NestedStore(store).transaction():
        workflow = ExtractionWorkflowRepository(store)
        config = _require_config(store, config_ref, workflow)
        _passages, source_refs = _derive_material(store, report_refs, config=config)
        if purpose == "quality":
            upstream = require_run(store, extraction_ref, purpose="extraction")
            if canonical_json(upstream["config_ref"]) != canonical_json(config_ref):
                raise ExtractionAuthorizationError(
                    "quality: the upstream extraction was run under a different config"
                )
            if upstream["source_refs"] != source_refs or upstream["report_refs"] != report_refs:
                raise ExtractionAuthorizationError(
                    "quality: the upstream extraction used different material"
                )
        if mode == "bulk":
            if pilot_ref is None:
                raise ExtractionAuthorizationError("bulk: an approved pilot reference is required")
            require_pilot(store, pilot_ref, config_ref=config_ref)
            _require_acquired(store, acquisition_ref, source_refs)
        _assert_gate_satisfied(
            store, hosted_ref["artifact_id"], HOSTED_USE_GATE_KIND, hosted_use_gate_id(purpose)
        )
        _assert_token_active(registry, store, activation_token)


# --------------------------------------------------------------------------- #
# Result classification
# --------------------------------------------------------------------------- #
def _classify_result(result, *, purpose, config, passages, instructions_sha256, dispatch_id, input_digest, model_ref):
    metadata = _execution_metadata(
        dispatch_id=dispatch_id, result=result, input_digest=input_digest, model_ref=model_ref
    )
    outcome = result.outcome
    if outcome == "completed":
        raw = json.loads(canonical_json(result.proposal_json).decode("utf-8"))
        try:
            if purpose == "extraction":
                extraction_schema.validate_extraction(
                    raw, passages=passages, appraisal_dimensions=config["appraisal_dimensions"]
                )
                _check_source_keys(raw, passages)
            else:
                extraction_workflow.validate_quality(raw)
        except ReviewAgentError:
            return "escalation", _content(
                purpose, "escalation", raw, instructions_sha256, "human",
                "extraction_schema_invalid", metadata,
            ), _RESULT_KIND_ESCALATION
        return "completed", _content(
            purpose, "completed", raw, instructions_sha256,
            raw.get("escalation", "none") if isinstance(raw, dict) else "none", None, metadata,
        ), _RESULT_KIND_PROPOSAL

    escalation = "human" if outcome in ("unknown", "protocol_error", "refused") else "none"
    kind = {"refused": _RESULT_KIND_REFUSED}.get(outcome, _RESULT_KIND_PROTOCOL)
    return outcome, _content(purpose, outcome, None, instructions_sha256, escalation, None, metadata), kind


def _content(purpose, outcome, raw_output, instructions_sha256, escalation, reason, metadata):
    body = {
        "purpose": purpose,
        "outcome": outcome,
        "raw_output": raw_output,
        "instructions_sha256": instructions_sha256,
        "escalation": escalation,
        "execution_metadata": metadata,
    }
    if reason is not None:
        body["reason"] = reason
    return body


def _execution_metadata(*, dispatch_id, result, input_digest, model_ref):
    versions = _reported_versions(input_digest, model_ref, result.request_sha256)
    usage = None
    if result.usage is not None:
        usage = {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
        }
    # A bounded, static parser-failure code (never peer text or a provider
    # message) so a non-completed run's reason is durable and distinguishable.
    diagnostic = result.diagnostic
    diagnostic_code = diagnostic.code if diagnostic is not None else None
    return {
        "actual_model": result.actual_model,
        "upstream_provider": result.provider,
        "response_id": result.response_id,
        "usage": usage,
        "version_set_json": canonical_json(versions).decode("utf-8"),
        "http_status": result.http_status,
        "request_sha256": result.request_sha256,
        "diagnostic_code": diagnostic_code,
        "dispatch_id": dispatch_id,
        "no_automatic_retry": True,
    }


# --------------------------------------------------------------------------- #
# require_pilot / require_run
# --------------------------------------------------------------------------- #
def require_pilot(store, pilot_ref, *, config_ref=None) -> dict:
    """Return an approved, iterative extraction/quality **pilot** (single authority).

    Delegates to :meth:`ExtractionWorkflowRepository.require_pilot` — which
    re-derives every case from the durable receipts, requires the mandatory
    human-adjudicated failure labels, rejects any blocking failure, requires the
    config, and requires the exact ``batch``/``extraction.pilot`` human gate. A
    model can never self-approve. When ``config_ref`` is supplied it must match
    the pilot's config exactly.
    """
    repo = ExtractionWorkflowRepository(store)
    try:
        pilot = repo.require_pilot(pilot_ref)
    except extraction_workflow.ExtractionWorkflowError as exc:
        raise ExtractionAuthorizationError(
            "pilot: is not an approved, successful, fully adjudicated pilot"
        ) from exc
    if config_ref is not None:
        expected = _require_same_project_ref(store, config_ref, "config_ref")
        if canonical_json(_require_same_project_ref(store, pilot["input"]["config_ref"], "pilot.config_ref")) != canonical_json(expected):
            raise ExtractionAuthorizationError("pilot: the pilot was run under a different config")
    return pilot


def require_run(store, ref, *, purpose: str) -> dict:
    """Return **exactly** the workflow ``RECEIPT_KEYS`` for one verified receipt.

    The public projection is the only shape the workflow seam accepts; the
    runner-internal metadata is available through :func:`_require_run_details`.
    """
    details = _require_run_details(store, ref, purpose=purpose)
    receipt = details["receipt"]
    return {
        "purpose": receipt["purpose"],
        "config_ref": receipt["config_ref"],
        "report_refs": receipt["report_refs"],
        "source_refs": receipt["source_refs"],
        "passages": receipt["passages"],
        "raw_output": receipt["raw_output"],
        "observed_model": receipt["observed_model"],
        "dispatch_id": receipt["dispatch_id"],
        "extraction_ref": receipt["extraction_ref"],
    }


def _require_run_details(store, ref, *, purpose: str) -> dict:
    """Verify one durable receipt under one nested transaction (fail closed)."""
    if purpose not in PURPOSES:
        raise ExtractionRunnerError("purpose must be 'extraction' or 'quality'")
    with _NestedStore(store).transaction():
        return _verify_receipt(store, ref, purpose=purpose)


def _verify_receipt(store, ref, *, purpose) -> dict:
    ref, _revision, payload = _load_run_receipt(store, ref)
    if payload["purpose"] != purpose:
        raise ExtractionReceiptError("run: purpose does not match the requested purpose")

    workflow = ExtractionWorkflowRepository(store)
    config_ref = _require_same_project_ref(store, payload["config_ref"], "config_ref")
    config = _require_config(store, config_ref, workflow)

    report_refs = [_require_same_project_ref(store, item, "report_refs[]") for item in payload["report_refs"]]
    passages, source_refs = _derive_material(store, report_refs, config=config)

    declared_sources = [_require_same_project_ref(store, item, "source_refs[]") for item in payload["source_refs"]]
    if declared_sources != source_refs:
        raise ExtractionReceiptError("run: the declared source references are not the reports' exact origins")

    extraction_ref = None
    upstream_raw = None
    if payload["extraction_ref"] is not None:
        extraction_ref = _require_same_project_ref(store, payload["extraction_ref"], "extraction_ref")
        if purpose != "quality":
            raise ExtractionReceiptError("run: an extraction_ref is only valid for quality")
    elif purpose == "quality":
        raise ExtractionReceiptError("run: quality requires an extraction_ref")
    if purpose == "quality":
        upstream = require_run(store, extraction_ref, purpose="extraction")
        if canonical_json(upstream["config_ref"]) != canonical_json(config_ref):
            raise ExtractionReceiptError("run: the quality extraction_ref was run under a different config")
        if upstream["source_refs"] != source_refs:
            raise ExtractionReceiptError("run: the quality extraction_ref used different original sources")
        upstream_raw = upstream["raw_output"]

    acquisition_ref = None
    if payload["acquisition_ref"] is not None:
        acquisition_ref = _require_same_project_ref(store, payload["acquisition_ref"], "acquisition_ref")

    # Exact dependency closure (same-project, ``derived_from`` only).
    _verify_receipt_dependencies(store, ref, config_ref, report_refs, extraction_ref, acquisition_ref, source_refs)

    # Recompute every identity and bind it to the durable task/dispatch/proposal.
    expected_inputs = _input_refs(store, config_ref, report_refs, source_refs, extraction_ref)
    plan, model_map = _build_plan(
        purpose=purpose, config=config, passages=passages, upstream_raw=upstream_raw
    )
    verified = _verify_durable_evidence(
        store, ref=ref, payload=payload, purpose=purpose, config=config, config_ref=config_ref,
        model_map=model_map, plan=plan, expected_inputs=expected_inputs, extraction_ref=extraction_ref,
        passages=passages, source_refs=source_refs,
    )

    receipt = {
        "purpose": purpose,
        "config_ref": dict(config_ref),
        "report_refs": [dict(item) for item in report_refs],
        "source_refs": [dict(item) for item in source_refs],
        "passages": passages,
        "raw_output": verified["raw_output"],
        "observed_model": verified["observed_model"],
        "dispatch_id": verified["dispatch_id"],
        "extraction_ref": None if extraction_ref is None else dict(extraction_ref),
    }
    return {
        "receipt": receipt,
        "config": config,
        "acquisition_ref": acquisition_ref,
        "run_ref": dict(ref),
        "task_id": verified["task_id"],
        "proposal_record_id": verified["proposal_record_id"],
        "cost_state": verified["cost_state"],
        "cost_amount_usd": verified["cost_amount_usd"],
        "reserve_usd": verified["reserve_usd"],
    }


def _verify_receipt_dependencies(store, ref, config_ref, report_refs, extraction_ref, acquisition_ref, source_refs):
    repository = ArtifactRepository(store)
    revision = repository.get_revision(ref["artifact_id"], ref["revision_id"])
    if revision is None:
        raise ExtractionReceiptError("run: the receipt revision is missing")
    live = [d for d in revision.dependencies if d.role != "version_of"]
    if any(d.role != "derived_from" for d in live):
        raise ExtractionReceiptError("run: the receipt has a non-derived dependency")
    expected = {_triple(config_ref)}
    expected |= {_triple(r) for r in report_refs}
    if extraction_ref is not None:
        expected.add(_triple(extraction_ref))
    if acquisition_ref is not None:
        expected.add(_triple(acquisition_ref))
    expected |= {_triple(r) for r in source_refs}
    actual = {(d.project_id, d.artifact_id, d.revision_id) for d in live}
    if any(d.project_id != store.project_id for d in live):
        raise ExtractionReceiptError("run: the receipt has a cross-project dependency")
    if actual != expected or len(live) != len(actual):
        raise ExtractionReceiptError(
            "run: the receipt dependency closure is not the exact declared material"
        )


def _task_scope(purpose, mode, artifact_id, revision_id, pilot_ref, acquisition_ref):
    scope = [EXTRACTION_STAGE, purpose, mode, f"receipt={artifact_id}:{revision_id}"]
    if pilot_ref is not None:
        scope.append(f"pilot={pilot_ref['artifact_id']}:{pilot_ref['revision_id']}")
    if acquisition_ref is not None:
        scope.append(f"acquisition={acquisition_ref['artifact_id']}:{acquisition_ref['revision_id']}")
    return scope


def _scope_tokens(task) -> dict:
    try:
        scope = json.loads(task.canonical.decode("utf-8")).get("scope", [])
    except (ValueError, UnicodeError):
        scope = []
    return {token.split("=", 1)[0]: token.split("=", 1)[1] for token in scope if "=" in token}


def _verify_durable_evidence(
    store, *, ref, payload, purpose, config, config_ref, model_map, plan,
    expected_inputs, extraction_ref, passages, source_refs,
):
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)

    dispatch_record = dispatch.get_dispatch_by_request(_require_identifier(payload["request_id"], "request_id"))
    if dispatch_record is None:
        raise ExtractionReceiptError("run: no durable dispatch is recorded for this run")
    if payload["dispatch_id"] != dispatch_record.dispatch_id:
        raise ExtractionReceiptError("run: the receipt dispatch_id does not match the dispatch")
    if dispatch_record.stage != EXTRACTION_STAGE or dispatch_record.route != "direct_call":
        raise ExtractionReceiptError("run: the dispatch stage/route is not extraction/direct_call")
    if dispatch_record.state != "completed" or dispatch_record.result_kind != _RESULT_KIND_PROPOSAL:
        raise ExtractionReceiptError("run: the dispatch is not an accepted extraction proposal")
    if dispatch_record.task_id != payload["task_id"]:
        raise ExtractionReceiptError("run: the dispatch task does not match the receipt")

    task = runtime.get_task(dispatch_record.task_id)
    if task is None:
        raise ExtractionReceiptError("run: the durable task is missing")
    if task.role != PURPOSE_ROLES[purpose]:
        raise ExtractionReceiptError("run: the task role does not match the purpose")
    if task.prompt_version != PROMPT_VERSION:
        raise ExtractionReceiptError("run: the task prompt version does not match")
    if dict(task.target) != config_ref:
        raise ExtractionReceiptError("run: the task target is not the config revision")

    # The declared purpose/mode/receipt/pilot/acquisition must be exactly what was
    # dispatched (pinned in the task scope); none may be silently substituted.
    if payload["outcome"] != "completed" or payload["mode"] not in MODES:
        raise ExtractionReceiptError("run: the outcome/mode is not a completed run")
    try:
        scope = json.loads(task.canonical.decode("utf-8")).get("scope", [])
    except (ValueError, UnicodeError):
        scope = []
    tokens = _scope_tokens(task)
    if purpose not in scope or payload["mode"] not in scope:
        raise ExtractionReceiptError("run: the task scope does not match the receipt purpose/mode")
    if tokens.get("receipt") != f"{ref['artifact_id']}:{ref['revision_id']}":
        raise ExtractionReceiptError("run: the receipt identity is not pinned in the task scope")
    if payload["mode"] == "bulk":
        if payload["pilot_ref"] is None or payload["acquisition_ref"] is None:
            raise ExtractionReceiptError("run: a bulk receipt must declare its pilot and acquisition")
        if tokens.get("pilot") != f"{payload['pilot_ref']['artifact_id']}:{payload['pilot_ref']['revision_id']}":
            raise ExtractionReceiptError("run: the declared pilot is not the dispatched pilot")
        if tokens.get("acquisition") != f"{payload['acquisition_ref']['artifact_id']}:{payload['acquisition_ref']['revision_id']}":
            raise ExtractionReceiptError("run: the declared acquisition is not the dispatched acquisition")
        require_pilot(store, payload["pilot_ref"], config_ref=config_ref)
        _require_acquired(store, payload["acquisition_ref"], source_refs)
    elif payload["pilot_ref"] is not None or payload["acquisition_ref"] is not None:
        raise ExtractionReceiptError("run: a pilot receipt must not declare bulk authority")

    expected_canonical = canonical_json(expected_inputs)
    actual_canonical = canonical_json([dict(entry) for entry in task.input_refs])
    if actual_canonical != expected_canonical:
        raise ExtractionReceiptError("run: the receipt material does not match the task input_refs")

    input_digest = _input_digest(store.project_id, expected_inputs)
    if payload["input_digest"] != input_digest or dispatch_record.input_digest != input_digest:
        raise ExtractionReceiptError("run: the recomputed input digest does not match")
    version_digest = _version_digest(purpose, model_map)
    if payload["version_digest"] != version_digest or dispatch_record.version_digest != version_digest:
        raise ExtractionReceiptError("run: the recomputed version digest does not match")
    if payload["request_sha256"] != plan.request_sha256 or payload["instructions_sha256"] != plan.instructions_sha256:
        raise ExtractionReceiptError("run: the recomputed request/instruction identities do not match")

    snapshot = runtime.get_configuration(task.config_snapshot_id)
    if snapshot is None or snapshot.role_configuration() != _role_configuration(purpose, model_map):
        raise ExtractionReceiptError("run: the task was not configured with the pinned model")

    record = runtime.get_proposal(_require_identifier(payload["proposal_record_id"], "proposal_record_id"))
    if record is None or record.task_id != task.task_id or record.stored_digest != payload["proposal_digest"]:
        raise ExtractionReceiptError("run: the accepted proposal record does not match the receipt")
    if dispatch_record.result_ref != record.record_id or dispatch_record.result_digest != record.stored_digest:
        raise ExtractionReceiptError("run: the dispatch accepted result is not this proposal")
    if record.model_id != model_map["model_id"] or record.provider_id != "openrouter":
        raise ExtractionReceiptError("run: the accepted proposal model is not the pinned model")
    if payload["observed_model"] != record.model_id:
        raise ExtractionReceiptError("run: the observed model does not match the receipt")

    content = json.loads(record.canonical.decode("utf-8")).get("content")
    if not isinstance(content, dict) or content.get("purpose") != purpose:
        raise ExtractionReceiptError("run: the accepted proposal content is malformed")
    if content.get("outcome") != "completed":
        raise ExtractionReceiptError("run: the accepted proposal is not a completed extraction")
    metadata = content.get("execution_metadata")
    if not isinstance(metadata, dict):
        raise ExtractionReceiptError("run: the accepted proposal metadata is malformed")
    if metadata.get("dispatch_id") != dispatch_record.dispatch_id:
        raise ExtractionReceiptError("run: the accepted metadata dispatch does not match")
    if metadata.get("request_sha256") != plan.request_sha256:
        raise ExtractionReceiptError("run: the accepted metadata request digest does not match")
    if metadata.get("actual_model") != record.model_id:
        raise ExtractionReceiptError("run: the accepted metadata model does not match")
    if content.get("instructions_sha256") != plan.instructions_sha256:
        raise ExtractionReceiptError("run: the accepted metadata instruction digest does not match")

    raw_output = content.get("raw_output")
    raw_digest = payload["raw_output_sha256"]
    if not isinstance(raw_output, dict) or raw_digest != _sha256_hex(canonical_json(raw_output)):
        raise ExtractionReceiptError("run: the reported output does not match the accepted proposal")
    # Re-validate the raw output at read against the public schema/quality validator.
    if purpose == "extraction":
        extraction_schema.validate_extraction(
            raw_output, passages=passages, appraisal_dimensions=config["appraisal_dimensions"]
        )
        _check_source_keys(raw_output, passages)
    else:
        extraction_workflow.validate_quality(raw_output)

    return {
        "raw_output": raw_output,
        "observed_model": record.model_id,
        "dispatch_id": dispatch_record.dispatch_id,
        "task_id": task.task_id,
        "proposal_record_id": record.record_id,
        "cost_state": dispatch_record.cost_state,
        "cost_amount_usd": dispatch_record.cost_amount_usd,
        "reserve_usd": dispatch_record.reserve_usd,
    }


# --------------------------------------------------------------------------- #
# Receipt target precheck + atomic persistence
# --------------------------------------------------------------------------- #
def _precheck_receipt_target(store, artifact_id, revision_id, rights, request_id) -> None:
    """Validate the caller's receipt ids/rights BEFORE any paid dispatch is sent."""
    artifact_id = _require_identifier(artifact_id, "artifact_id")
    revision_id = _require_identifier(revision_id, "revision_id")
    if not isinstance(rights, Mapping):
        raise ExtractionRunnerError("rights: a canonical RightsRecord is required")
    subject = {
        "kind": "source_revision",
        "project_id": store.project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    try:
        validated = validate_rights_record(rights, store.project_id, subject=subject)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise ExtractionRunnerError(
            "rights: the supplied rights record does not bind the receipt revision"
        ) from exc
    if validated["derived_status"] != "valid":
        raise ExtractionRunnerError("rights: the supplied rights record is not valid")
    operations = validated["operations"]
    if operations.get("storage") != "allowed":
        raise ExtractionRunnerError("rights: the supplied rights do not permit storage")
    privacy = validated["privacy"]
    if privacy.get("denial") is not False or privacy.get("confidentiality") == "unknown":
        raise ExtractionRunnerError("rights: the supplied rights do not permit storage")
    existing = ArtifactRepository(store).get_artifact(artifact_id)
    if existing is not None and existing.kind != "report":
        raise ExtractionRunnerError("artifact_id: already exists with a different kind")
    # A NEW request may not target an already-stored receipt revision (replay is
    # allowed: it observes the recorded dispatch instead of writing).
    if RuntimeDispatchRepository(store).get_dispatch_by_request(request_id) is None:
        if ArtifactRepository(store).get_revision(artifact_id, revision_id) is not None:
            raise ExtractionRunnerError(
                "receipt: the target revision already exists for a new request"
            )


def _receipt_payload(*, purpose, config_ref, report_refs, source_refs, extraction_ref, acquisition_ref,
                     request_id, dispatch_record, task_id, record, plan, model_map, mode, pilot_ref,
                     outcome, raw_output, input_digest):
    raw_digest = None if raw_output is None else _sha256_hex(canonical_json(raw_output))
    return {
        "purpose": purpose,
        "config_ref": dict(config_ref),
        "report_refs": [dict(ref) for ref in report_refs],
        "source_refs": [dict(ref) for ref in source_refs],
        "extraction_ref": None if extraction_ref is None else dict(extraction_ref),
        "acquisition_ref": None if acquisition_ref is None else dict(acquisition_ref),
        "request_id": request_id,
        "dispatch_id": dispatch_record.dispatch_id,
        "task_id": task_id,
        "proposal_record_id": record.record_id,
        "proposal_digest": record.stored_digest,
        "observed_model": model_map["model_id"],
        "request_sha256": plan.request_sha256,
        "input_digest": input_digest,
        "version_digest": _version_digest(purpose, model_map),
        "instructions_sha256": plan.instructions_sha256,
        "mode": mode,
        "pilot_ref": None if pilot_ref is None else dict(pilot_ref),
        "outcome": outcome,
        "raw_output_sha256": raw_digest,
    }


def _write_receipt_in_transaction(store, *, material, payload) -> dict:
    """Persist the receipt revision inside the caller's open transaction (savepoint)."""
    repository = ArtifactRepository(_NestedStore(store))
    repository.create_artifact(material["artifact_id"], kind="report", created_by="agent")
    dependencies = [
        {"project_id": store.project_id, "artifact_id": ref["artifact_id"], "revision_id": ref["revision_id"]}
        for ref in material["dependencies"]
    ]
    stored = repository.import_revision(
        artifact_id=material["artifact_id"],
        revision_id=material["revision_id"],
        content={"schema_version": SCHEMA_VERSION, "document_type": RUN_DOCUMENT_TYPE, "payload": payload},
        origin_locator=f"extraction:{payload['request_id']}",
        rights=material["rights"],
        dependencies=dependencies,
        acquired_by="agent",
    )
    return {"project_id": store.project_id, "artifact_id": stored.artifact_id, "revision_id": stored.revision_id}


# --------------------------------------------------------------------------- #
# Public runner
# --------------------------------------------------------------------------- #
def execute_run(
    store,
    *,
    registry,
    activation_token: str,
    config_ref,
    report_refs,
    purpose: str,
    request_id: str,
    hosted_ref,
    api_key: str,
    extraction_ref=None,
    transport=None,
    sleep=None,
    mode: str = "pilot",
    pilot_ref=None,
    artifact_id: str,
    revision_id: str,
    rights: object,
) -> dict:
    """Run **one** bounded extraction/quality request (single-shot, atomic commit).

    The exact config (via the workflow's single config authority) and hosted-use
    revisions are re-read, their named gates, the execution token, every original
    source's hosted-transfer rights, and (in bulk mode) the acquired manifest and
    the approved pilot are revalidated before and immediately prior to the single
    POST. The model output is schema-validated; a failure escalates to a human and
    accepts no extraction. The run receipt, the non-authorizing proposal, the
    accepted dispatch result and its settled cost are all committed in **one**
    nested transaction from caller-supplied rights. An already-recorded request is
    observed, never re-sent.

    An **ambiguous** direct-call outcome (a transport error/timeout/ambiguous
    status the adapter classifies as ``unknown``) is retried automatically and
    boundedly (ADR 006): the logical call is ``request_id``; each attempt is a new
    deterministic identity (``request_id``, ``request_id-r1``, ``request_id-r2``)
    and the same identity is never re-POSTed. ``next_attempt`` owns the
    supersede/abandon ledger transitions and the backoff; ``sleep`` is injectable
    (tests pass a no-op). A definitive non-completion is reported when every
    attempt is ambiguous or the overhead cap refuses a further retry.
    """
    request_id = _require_identifier(request_id, "request_id")
    # Leave room for the deterministic retry suffix (``-r2``) so every attempt's
    # request id stays within the runner's identifier bound (ADR 006).
    if len(request_id) > MAX_IDENTIFIER_LEN - _RETRY_SUFFIX_LEN:
        raise ExtractionRunnerError(
            "request_id: must leave room for the deterministic retry suffix"
        )
    if not isinstance(api_key, str) or api_key == "":
        raise ExtractionRunnerError("api_key: a non-empty credential is required")
    if purpose not in PURPOSES:
        raise ExtractionRunnerError("purpose must be 'extraction' or 'quality'")
    if mode not in MODES:
        raise ExtractionRunnerError("mode must be 'pilot' or 'bulk'")
    _assert_token_active(registry, store, activation_token)

    # The receipt target is validated BEFORE any paid dispatch can happen.
    _precheck_receipt_target(store, artifact_id, revision_id, rights, request_id)

    workflow = ExtractionWorkflowRepository(store)
    config_ref = _require_same_project_ref(store, config_ref, "config_ref")
    config = _require_config(store, config_ref, workflow)

    hosted_ref, _hosted_revision, hosted_doc = _load_hosted_use(store, hosted_ref)
    hosted = hosted_doc["payload"]
    if hosted["purpose"] != purpose:
        raise ExtractionHostedUseError("hosted-use: purpose does not match the request")
    if hosted["config_ref"] != config_ref:
        raise ExtractionHostedUseError("hosted-use: config_ref does not match the request")
    report_refs = [_require_same_project_ref(store, item, "report_refs[]") for item in report_refs]
    if [dict(item) for item in hosted["report_refs"]] != report_refs:
        raise ExtractionHostedUseError("hosted-use: report_refs do not match the request")
    extraction_ref = None if extraction_ref is None else _require_same_project_ref(store, extraction_ref, "extraction_ref")
    if (hosted["extraction_ref"] is None) != (extraction_ref is None) or (
        hosted["extraction_ref"] is not None and hosted["extraction_ref"] != extraction_ref
    ):
        raise ExtractionHostedUseError("hosted-use: extraction_ref does not match the request")
    acquisition_ref = hosted["acquisition_ref"]

    passages, source_refs = _derive_material(store, report_refs, config=config)

    _verify_hosted_dependencies(store, hosted_ref, config_ref, report_refs, extraction_ref, acquisition_ref, source_refs)
    _assert_gate_satisfied(store, hosted_ref["artifact_id"], HOSTED_USE_GATE_KIND, hosted_use_gate_id(purpose))

    upstream_run = None
    if purpose == "quality":
        upstream_run = require_run(store, extraction_ref, purpose="extraction")
        if canonical_json(upstream_run["config_ref"]) != canonical_json(config_ref):
            raise ExtractionRunnerError("quality: extraction_ref was run under a different config")
    if mode == "bulk":
        if pilot_ref is None:
            raise ExtractionAuthorizationError("bulk: an approved pilot reference is required")
        require_pilot(store, pilot_ref, config_ref=config_ref)
        _require_acquired(store, acquisition_ref, source_refs)

    plan, model_map = _build_plan(purpose=purpose, config=config, passages=passages, upstream_raw=None if upstream_run is None else upstream_run["raw_output"])
    role_configuration = _role_configuration(purpose, model_map)
    model_ref = role_configuration.for_role(PURPOSE_ROLES[purpose]).model.ref

    input_refs = _input_refs(store, config_ref, report_refs, source_refs, extraction_ref)
    input_digest = _input_digest(store.project_id, input_refs)
    version_digest = _version_digest(purpose, model_map)
    bound_evidence_digest = _sha256_hex(canonical_json(dict(plan.bound_evidence)))

    bounded = BoundedTask(
        project_id=store.project_id,
        role=PURPOSE_ROLES[purpose],
        target={"project_id": store.project_id, "artifact_id": config_ref["artifact_id"], "revision_id": config_ref["revision_id"]},
        input_refs=input_refs,
        # The receipt identity and the exact bulk authority are pinned into the task
        # scope: a replay with different artifact/revision ids conflicts instead of
        # returning an unrelated receipt, and the read seam can bind the declared
        # pilot/acquisition to what was actually dispatched.
        scope=_task_scope(purpose, mode, artifact_id, revision_id, pilot_ref, acquisition_ref),
        prompt_version=PROMPT_VERSION,
        size_limit=TASK_SIZE_LIMIT,
        step_limit=1,
        role_config=role_configuration.for_role(PURPOSE_ROLES[purpose]),
    )

    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    # Record (or return) the base attempt's immutable task up front. This is
    # idempotent on a replay and still refuses a divergent task under the same
    # request id (the logical call key) before any dispatch.
    base_task_record = runtime.record_task(
        bounded, request_id=request_id, configuration=role_configuration
    )

    material = {
        "artifact_id": artifact_id,
        "revision_id": revision_id,
        "rights": rights,
        "dependencies": [config_ref, *report_refs, *([extraction_ref] if extraction_ref else []),
                         *([acquisition_ref] if acquisition_ref else []), *source_refs],
    }

    sleeper = _SLEEP if sleep is None else sleep
    posted = False
    # Bounded automatic retry of an ambiguous direct call (ADR 006). The logical
    # call is ``request_id``; each attempt is a NEW deterministic identity
    # (``request_id``, ``-r1``, ``-r2``). ``next_attempt`` owns the
    # supersede/abandon transitions and the backoff, and raises on an
    # unreconciled in-flight row; this loop runs one POST per ``dispatch``
    # decision exactly as before.
    while True:
        decision = next_attempt(store, request_id, sleep=sleeper)
        if decision.outcome != "dispatch":
            # completed: a prior attempt succeeded -> read its receipt back.
            # definitive | exhausted | refused_overhead: a definitive
            # non-completion (never a completion without a completed member).
            run_ref = (
                _receipt_ref_for_dispatch(store, decision.dispatch)
                if decision.outcome == "completed"
                else None
            )
            return _readback(store, request_id, run_ref=run_ref, posted=posted)

        attempt_id = decision.request_id
        if attempt_id == request_id:
            task_record = base_task_record
        else:
            task_record = runtime.record_task(
                bounded, request_id=attempt_id, configuration=role_configuration
            )

        with registry.execution_write_guard(activation_token, store.project_id):
            dispatch_record = dispatch.reserve_dispatch(
                task_record, request_id=attempt_id, route="direct_call", stage=EXTRACTION_STAGE,
                input_digest=input_digest, version_digest=version_digest,
                reserve_usd=plan.reserve_usd, bound_evidence_digest=bound_evidence_digest,
            )
            dispatch_record = dispatch.mark_submitted(dispatch_record.dispatch_id)

        # Point-in-time revalidation immediately before the POST (no lock over network).
        _revalidate_before_send(
            store, registry=registry, activation_token=activation_token, hosted_ref=hosted_ref,
            purpose=purpose, mode=mode, config_ref=config_ref, report_refs=report_refs,
            extraction_ref=extraction_ref, acquisition_ref=acquisition_ref, pilot_ref=pilot_ref,
        )

        posted = True
        result = runtime_extraction.execute_extraction(plan, api_key=api_key, transport=transport)

        outcome, content, result_kind = _classify_result(
            result, purpose=purpose, config=config, passages=passages,
            instructions_sha256=plan.instructions_sha256, dispatch_id=dispatch_record.dispatch_id,
            input_digest=input_digest, model_ref=model_ref,
        )

        with registry.execution_write_guard(activation_token, store.project_id, allow_draining=True):
            run_ref = _commit_atomic(
                store, runtime=runtime, dispatch=dispatch, task_record=task_record,
                dispatch_record=dispatch_record, result=result, outcome=outcome, content=content,
                result_kind=result_kind, role_configuration=role_configuration, material=material,
                receipt_payload={
                    "purpose": purpose, "config_ref": config_ref, "report_refs": report_refs,
                    "source_refs": source_refs, "extraction_ref": extraction_ref,
                    "acquisition_ref": acquisition_ref, "request_id": attempt_id,
                    "task_id": task_record.task_id, "plan": plan, "model_map": model_map,
                    "mode": mode, "pilot_ref": pilot_ref, "outcome": outcome, "input_digest": input_digest,
                },
            )
        if run_ref is not None:
            # A completed proposal or an accepted diagnostic: terminal for this call.
            return _readback(store, request_id, run_ref=run_ref, posted=posted)
        # ``_commit_atomic`` returns ``None`` only for ``unknown`` (ambiguous:
        # the next iteration supersedes and retries) and ``failed`` (definitive:
        # the next iteration reports a definitive stop). Never a completion here.


def _verify_hosted_dependencies(store, hosted_ref, config_ref, report_refs, extraction_ref, acquisition_ref, source_refs):
    revision = ArtifactRepository(store).get_revision(hosted_ref["artifact_id"], hosted_ref["revision_id"])
    if revision is None:
        raise ExtractionHostedUseError("hosted-use: the pinned revision is missing")
    if any(d.project_id != store.project_id for d in revision.dependencies):
        raise ExtractionHostedUseError("hosted-use: the revision has a cross-project dependency")
    edges = {(d.project_id, d.artifact_id, d.revision_id) for d in revision.dependencies}
    expected = {_triple(config_ref)}
    expected |= {_triple(r) for r in report_refs}
    if extraction_ref is not None:
        expected.add(_triple(extraction_ref))
    if acquisition_ref is not None:
        expected.add(_triple(acquisition_ref))
    expected |= {_triple(r) for r in source_refs}
    if not expected <= edges:
        raise ExtractionHostedUseError("hosted-use: the revision does not pin its declared rights closure")


def _commit_atomic(
    store, *, runtime, dispatch, task_record, dispatch_record, result, outcome, content,
    result_kind, role_configuration, material, receipt_payload,
):
    """Commit proposal + dispatch result/cost + receipt in ONE nested transaction."""
    with _NestedStore(store).transaction():
        record = runtime._record_proposal_in_transaction(
            task_record, {"content": content}, configuration=role_configuration
        )
        fence = dispatch_record.current_fence
        if outcome == "completed":
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(dispatch_record.dispatch_id, cost_state=cost_state, amount_usd=amount)
            dispatch._accept_result_in_transaction(
                dispatch_record.dispatch_id, fence=fence, result_kind=result_kind,
                result_ref=record.record_id, result_digest=record.stored_digest,
            )
        elif outcome == "failed":
            dispatch._settle_cost_in_transaction(dispatch_record.dispatch_id, cost_state="not_incurred", allow_definitive_not_incurred=True)
            dispatch._mark_failed_in_transaction(dispatch_record.dispatch_id, definitive_rejection=True)
            return None
        elif outcome == "unknown":
            dispatch._settle_cost_in_transaction(dispatch_record.dispatch_id, cost_state="unknown")
            dispatch._recover_in_transaction(dispatch_record.dispatch_id)
            return None
        else:  # escalation / refused / protocol_error (accepted diagnostics)
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(dispatch_record.dispatch_id, cost_state=cost_state, amount_usd=amount)
            dispatch._accept_result_in_transaction(
                dispatch_record.dispatch_id, fence=fence, result_kind=result_kind,
                result_ref=record.record_id, result_digest=record.stored_digest,
            )
        raw_output = content.get("raw_output") if result_kind == _RESULT_KIND_PROPOSAL else None
        payload = _receipt_payload(
            purpose=receipt_payload["purpose"], config_ref=receipt_payload["config_ref"],
            report_refs=receipt_payload["report_refs"], source_refs=receipt_payload["source_refs"],
            extraction_ref=receipt_payload["extraction_ref"], acquisition_ref=receipt_payload["acquisition_ref"],
            request_id=receipt_payload["request_id"], dispatch_record=dispatch_record,
            task_id=receipt_payload["task_id"], record=record, plan=receipt_payload["plan"],
            model_map=receipt_payload["model_map"], mode=receipt_payload["mode"],
            pilot_ref=receipt_payload["pilot_ref"], outcome=outcome, raw_output=raw_output,
            input_digest=receipt_payload["input_digest"],
        )
        return _write_receipt_in_transaction(store, material=material, payload=payload)


# --------------------------------------------------------------------------- #
# Replay + readback
# --------------------------------------------------------------------------- #
def _resolve_chain_dispatch(dispatch, base_request_id):
    """Resolve one logical call's retry chain to its deciding dispatch.

    Prefer a **completed** member (the retry that succeeded); otherwise return the
    last durable member — the base for a fresh/definitive call, or the resolved
    last attempt of an exhausted/refused chain. ``None`` when no member exists.
    """
    rows = [dispatch.get_dispatch_by_request(rid) for rid in retry_request_ids(base_request_id)]
    for row in rows:
        if row is not None and row.state == "completed":
            return row
    last = None
    for row in rows:
        if row is not None:
            last = row
    return last


def _receipt_ref_for_dispatch(store, dispatch_record):
    """The run receipt ref bound to a completed accepted-proposal dispatch, or ``None``."""
    if (
        dispatch_record is None
        or dispatch_record.state != "completed"
        or dispatch_record.result_kind != _RESULT_KIND_PROPOSAL
    ):
        return None
    runtime = RuntimeRepository(store)
    task = runtime.get_task(dispatch_record.task_id)
    if task is None:
        return None
    for record in runtime.proposals_for_task(task.task_id):
        if record.record_id == dispatch_record.result_ref:
            content = json.loads(record.canonical.decode("utf-8")).get("content", {})
            return _receipt_ref_from_task(store, task, content)
    return None


def _replay(store, request_id):
    """An already-recorded request: observe the stored outcome; never re-send.

    The retry chain for ``request_id`` is resolved to its **completed** member (a
    retry that succeeded) or, absent a completion, the last durable member (a
    definitive failure or an exhausted/refused chain). The readback carries the
    base ``request_id``.
    """
    dispatch = RuntimeDispatchRepository(store)
    dispatch_record = _resolve_chain_dispatch(dispatch, request_id)
    return _readback(
        store,
        request_id,
        run_ref=_receipt_ref_for_dispatch(store, dispatch_record),
        posted=False,
    )


def _receipt_ref_from_task(store, task, content):
    """Recover the receipt ref pinned in the task scope (``receipt=<id>:<rev>``)."""
    try:
        scope = json.loads(task.canonical.decode("utf-8")).get("scope", [])
    except (ValueError, UnicodeError):
        return None
    for token in scope:
        if token.startswith("receipt="):
            _, _, value = token.partition("=")
            artifact_id, _, revision_id = value.partition(":")
            try:
                ref = {"project_id": store.project_id, "artifact_id": artifact_id, "revision_id": revision_id}
                _load_run_receipt(store, ref)
                return ref
            except ExtractionRunnerError:
                return None
    return None


def _readback(store, request_id: str, *, run_ref, posted: bool) -> dict:
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    dispatch_record = _resolve_chain_dispatch(dispatch, request_id)
    task = runtime.get_task_by_request(request_id)
    if dispatch_record is not None:
        task = runtime.get_task(dispatch_record.task_id)
    mapping = {
        "request_id": request_id,
        "status": "blocked",
        "outcome": None,
        "dispatch_id": None,
        "dispatch_state": None,
        "task_id": None if task is None else task.task_id,
        "proposal_record_id": None,
        "cost_state": None,
        "cost_amount_usd": None,
        "reserve_usd": None,
        "run_ref": run_ref,
        "posted": posted,
        "no_automatic_retry": True,
        "actual_model": None,
        "provider": None,
        "response_id": None,
        "usage": None,
        "http_status": None,
        "request_sha256": None,
        "diagnostic_code": None,
    }
    if dispatch_record is None:
        return mapping
    mapping.update(
        {
            "status": dispatch_record.state,
            "outcome": _outcome_from_dispatch(dispatch_record),
            "dispatch_id": dispatch_record.dispatch_id,
            "dispatch_state": dispatch_record.state,
            "task_id": dispatch_record.task_id,
            "cost_state": dispatch_record.cost_state,
            "cost_amount_usd": dispatch_record.cost_amount_usd,
            "reserve_usd": dispatch_record.reserve_usd,
        }
    )
    if task is not None:
        for record in runtime.proposals_for_task(task.task_id):
            content = json.loads(record.canonical.decode("utf-8")).get("content")
            if not isinstance(content, dict):
                continue
            metadata = content.get("execution_metadata")
            if not isinstance(metadata, dict) or metadata.get("dispatch_id") != dispatch_record.dispatch_id:
                continue
            mapping["proposal_record_id"] = record.record_id
            mapping["actual_model"] = metadata.get("actual_model")
            mapping["provider"] = metadata.get("upstream_provider")
            mapping["response_id"] = metadata.get("response_id")
            mapping["usage"] = metadata.get("usage")
            mapping["http_status"] = metadata.get("http_status")
            mapping["request_sha256"] = metadata.get("request_sha256")
            mapping["diagnostic_code"] = metadata.get("diagnostic_code")
            break
    return mapping


def _outcome_from_dispatch(dispatch_record) -> str:
    state = dispatch_record.state
    if state == "completed":
        return {
            _RESULT_KIND_PROPOSAL: "completed",
            _RESULT_KIND_ESCALATION: "escalation",
            _RESULT_KIND_REFUSED: "refused",
            _RESULT_KIND_PROTOCOL: "protocol_error",
        }.get(dispatch_record.result_kind, "completed")
    if state == "failed":
        return "failed"
    if state == "unknown":
        return "unknown"
    return state
