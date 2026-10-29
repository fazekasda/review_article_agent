"""Bounded **direct extraction/quality runner** (Step 09, S9-RUNNER).

Authority
---------

* ``docs/implementation/09-extraction.md`` (grounded extraction from *original*
  text only, explicit coverage/omission accounting, no extraction from summaries,
  tables/supplements are inputs, human block on unreadable necessary assets) and
  ``docs/specification.md`` (line 32: a configurable smaller model extracts from
  original text).
* ``docs/decisions/004-step09-direct-extraction.md`` — the user-approved direct
  OpenRouter architecture exception extension for bounded, tool-free extraction
  and quality checking. No OpenCode production restriction is relaxed.
* ``docs/contracts/02-gates-and-runtime.md`` §5.6 (reserve before dispatch; an
  unknown reserve is retained, never silently released), §6.3 (exact-decimal
  direct-call cost, accounted separately), §8.1.1/§8.3 (one logical request per
  durable job; no blind retry) and ``docs/contracts/03-policy-decisions.md``
  §5.2/POL-P5 (rights eligibility is **not** final authorization; a hosted
  transfer needs an out-of-band scoped authorization and verified provider
  terms).

This module is the *application* boundary the pure adapters deliberately are not:
it owns gates, rights, budget, the durable ledger, schema validation, and the
human escalation decision. The model output is **data only**; it is never
authority.

What this module is
-------------------

* :func:`validate_extraction_config_document` / :func:`validate_hosted_use_document`
  — pure, static validation of the two generic document envelopes.
* :func:`propose_hosted_use` — a pure builder the CLI can persist through the
  normal artifact repository with **caller-supplied** rights (this module never
  fabricates rights).
* :func:`require_run` — the workflow read seam: it re-derives every byte from the
  store and returns the exact durable, accepted dispatch/task/proposal evidence
  plus the model output. A generic imported receipt alone is **never** authority.
* :func:`require_pilot` — the bulk-mode pilot gate.
* :func:`execute_run` — the single-shot runner. It re-reads the exact config and
  hosted-use revisions, revalidates the named gates, activation token, hosted
  rights, and (in bulk mode) the approved pilot, reserves a ``direct_call``
  dispatch **before** the one POST, validates the model output against the
  extraction/quality schema, and commits the proposal together with the accepted
  result and settled cost.

What this module is deliberately **not**
----------------------------------------

* **Not an authorizer.** The config and hosted-use documents *describe* an
  authorization; the real authority is the out-of-band owner-authenticated
  ``batch``/``extraction.config`` and ``batch``/``extraction.hosted_use.<purpose>``
  gates. No document field (or model reply) can create, widen, or substitute for
  one.
* **Not a retry engine.** One logical request per durable job; a replay observes,
  an ambiguous outcome retains its reserve and is never re-sent.
* **Not a scientific verifier.** Structural schema validation cannot prove
  completeness or correctness; a schema failure escalates to a human and accepts
  no extraction.

Every failure is a static, value-free error; no message echoes an API key, source
text, identifier, or stored byte.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
)
from ..parsing.repository import ParsingRepository
from ..runtime import extraction as runtime_extraction
from ..runtime import lead
from ..runtime.dispatch import RuntimeDispatchRepository
from ..runtime.lead import LeadConfig
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
from ..discovery import lead_runner as _lead

__all__ = [
    "EXTRACTION_STAGE",
    "PROMPT_VERSION",
    "ADAPTER_REVISION",
    "CONFIG_DOCUMENT_TYPE",
    "HOSTED_USE_DOCUMENT_TYPE",
    "RUN_DOCUMENT_TYPE",
    "CONFIG_GATE_KIND",
    "CONFIG_GATE_ID",
    "HOSTED_USE_GATE_KIND",
    "HOSTED_USE_GATE_PREFIX",
    "PURPOSES",
    "MODES",
    "OUTCOMES",
    "PURPOSE_ROLES",
    "QUALITY_CHECK_KEYS",
    "QUALITY_SEVERITIES",
    "QUALITY_ESCALATIONS",
    "ExtractionRunnerError",
    "ExtractionConfigDocumentError",
    "ExtractionHostedUseError",
    "ExtractionAuthorizationError",
    "ExtractionRightsError",
    "ExtractionReceiptError",
    "validate_extraction_config_document",
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

CONFIG_DOCUMENT_TYPE = "extraction_config"
HOSTED_USE_DOCUMENT_TYPE = "extraction_hosted_use"
RUN_DOCUMENT_TYPE = "extraction_run"

CONFIG_GATE_KIND = "batch"
CONFIG_GATE_ID = "extraction.config"
HOSTED_USE_GATE_KIND = "batch"
HOSTED_USE_GATE_PREFIX = "extraction.hosted_use."

PURPOSES = ("extraction", "quality")
MODES = ("pilot", "bulk")

PURPOSE_ROLES = {"extraction": "extractor", "quality": "fact_checker"}

OUTCOMES = ("completed", "escalation", "refused", "protocol_error", "failed", "unknown")

QUALITY_CHECK_KEYS = (
    "omissions",
    "qualifications",
    "field_correctness",
    "negations",
    "nulls",
    "numerical_units_denominators",
    "species_models",
    "author_spin",
    "tables_supplements",
)
QUALITY_SEVERITIES = ("blocking", "major", "minor", "style")
QUALITY_ESCALATIONS = ("none", "human", "stronger_model")

TASK_SIZE_LIMIT = 262_144
MAX_IDENTIFIER_LEN = 512

_CONFIG_PAYLOAD_KEYS = (
    "extraction_model",
    "quality_model",
    "instructions",
    "quality_instructions",
    "appraisal_dimensions",
    "parser",
    "normalization",
    "schema_version",
    "pilot_size",
    "random_size",
    "risk_size",
    "acceptance_rule",
)
_HOSTED_PAYLOAD_KEYS = (
    "config_ref",
    "report_refs",
    "purpose",
    "extraction_ref",
    "provider_terms",
    "privacy_review",
    "pricing_evidence",
)
_ENVELOPE_KEYS = ("schema_version", "document_type", "payload")
_EVIDENCE_KEYS = ("ref", "verified_at")
_REF_KEYS = ("project_id", "artifact_id", "revision_id")

_RUN_PAYLOAD_KEYS = (
    "purpose",
    "config_ref",
    "report_refs",
    "source_refs",
    "extraction_ref",
    "request_id",
    "dispatch_id",
    "task_id",
    "proposal_record_id",
    "proposal_digest",
    "observed_model",
    "request_sha256",
    "input_digest",
    "instructions_sha256",
    "mode",
    "pilot_ref",
    "outcome",
    "raw_output_sha256",
)

#: Result-kind labels the runner records on a terminal dispatch.
_RESULT_KIND_PROPOSAL = "extraction_proposal"
_RESULT_KIND_ESCALATION = "extraction_escalation"
_RESULT_KIND_REFUSED = "extraction_refused"
_RESULT_KIND_PROTOCOL = "extraction_protocol_error"


# --------------------------------------------------------------------------- #
# Errors (static messages; never echo input)
# --------------------------------------------------------------------------- #
class ExtractionRunnerError(ReviewAgentError):
    """A bounded extraction/quality run failed before any dispatch was issued."""


class ExtractionConfigDocumentError(ExtractionRunnerError):
    """An extraction config document is malformed or not canonical."""


class ExtractionHostedUseError(ExtractionRunnerError):
    """An extraction hosted-use document is malformed or mismatched."""


class ExtractionAuthorizationError(ExtractionRunnerError):
    """The exact out-of-band gate or the execution token is not active."""


class ExtractionRightsError(ExtractionRunnerError):
    """A pinned source's rights record does not permit the hosted transfer."""


class ExtractionReceiptError(ExtractionRunnerError):
    """A run receipt does not match its durable accepted ledger evidence."""


# --------------------------------------------------------------------------- #
# Small static helpers (reused from the lead runner where identical)
# --------------------------------------------------------------------------- #
_sha256_hex = _lead._sha256_hex
_require_mapping = _lead._require_mapping
_require_exact_keys = _lead._require_exact_keys
_require_plain_str = _lead._require_plain_str
_require_evidence_ref = _lead._require_evidence_ref
_require_utc_timestamp = _lead._require_utc_timestamp
_validate_evidence = _lead._validate_evidence
_parse_json_object = _lead._parse_json_object
_read_current_revision = _lead._read_current_revision


def _verify_hosted_rights(store, ref) -> None:
    """Require a source's pinned rights to permit the hosted transfer."""
    try:
        _lead._verify_hosted_rights(store, ref)
    except _lead.HostedUseRightsError as exc:
        raise ExtractionRightsError(str(exc)) from exc


def _assert_token_active(registry, store, activation_token: str) -> None:
    """Require the exact active, non-draining execution holder (this runner's error)."""
    try:
        _lead._assert_token_active(registry, store, activation_token)
    except _lead.HostedUseAuthorizationError as exc:
        raise ExtractionAuthorizationError(str(exc)) from exc


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


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExtractionConfigDocumentError(f"{field}: must be a positive integer")
    return value


def _assert_gate_satisfied(store, artifact_id: str, gate_kind: str, gate_id: str) -> None:
    approvals = HumanApprovalRepository(store)
    if not approvals.named_gate_satisfied(
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
# Config document validation
# --------------------------------------------------------------------------- #
def _validate_appraisal_dimensions(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ExtractionConfigDocumentError(
            "appraisal_dimensions: must be a list of dimension descriptors"
        )
    out: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        field = f"appraisal_dimensions[{index}]"
        item = _require_mapping(raw, field)
        dimension_id = _require_plain_str(item.get("dimension_id"), f"{field}.dimension_id")
        if dimension_id in seen:
            raise ExtractionConfigDocumentError(f"{field}: duplicate dimension_id")
        if "score" in item:
            raise ExtractionConfigDocumentError(f"{field}: must not define a numeric score")
        seen.add(dimension_id)
        out.append({"dimension_id": dimension_id})
    return out


def _validate_lead_config(value: object, field: str) -> dict:
    try:
        config = LeadConfig.from_mapping(value)
    except (lead.LeadError, RecordValidationError, TypeError, ValueError) as exc:
        raise ExtractionConfigDocumentError(
            f"{field}: is not a valid hosted-use configuration"
        ) from exc
    return config.to_mapping()


def validate_extraction_config_document(document: object) -> dict:
    """Validate one extraction config document; return a plain, independent copy.

    The envelope must be **exactly** ``{schema_version, document_type, payload}``
    with ``schema_version == 1`` and ``document_type == "extraction_config"``. The
    payload must be **exactly** the configured keys (see module docstring). This
    performs no I/O, invents no default, and grants **nothing**.
    """
    try:
        return _validate_config_inner(document)
    except ExtractionConfigDocumentError:
        raise
    except _lead.HostedUseDocumentError as exc:
        raise ExtractionConfigDocumentError(str(exc)) from exc


def _validate_config_inner(document: object) -> dict:
    envelope = _require_mapping(document, "document")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "document")
    version = envelope["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ExtractionConfigDocumentError("document.schema_version: must be an integer")
    if version != SCHEMA_VERSION:
        raise ExtractionConfigDocumentError(
            f"document.schema_version: must be the supported version {SCHEMA_VERSION}"
        )
    if envelope["document_type"] != CONFIG_DOCUMENT_TYPE:
        raise ExtractionConfigDocumentError(
            "document.document_type: is not the extraction config document type"
        )
    payload = _require_mapping(envelope["payload"], "document.payload")
    _require_exact_keys(payload, _CONFIG_PAYLOAD_KEYS, "extraction_config.payload")

    inner_version = payload["schema_version"]
    if isinstance(inner_version, bool) or inner_version != SCHEMA_VERSION:
        raise ExtractionConfigDocumentError(
            f"extraction_config.schema_version: must be {SCHEMA_VERSION}"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": CONFIG_DOCUMENT_TYPE,
        "payload": {
            "extraction_model": _validate_lead_config(
                payload["extraction_model"], "extraction_config.extraction_model"
            ),
            "quality_model": _validate_lead_config(
                payload["quality_model"], "extraction_config.quality_model"
            ),
            "instructions": _require_plain_str(
                payload["instructions"], "extraction_config.instructions"
            ),
            "quality_instructions": _require_plain_str(
                payload["quality_instructions"], "extraction_config.quality_instructions"
            ),
            "appraisal_dimensions": _validate_appraisal_dimensions(
                payload["appraisal_dimensions"]
            ),
            "parser": _require_plain_str(payload["parser"], "extraction_config.parser"),
            "normalization": _require_plain_str(
                payload["normalization"], "extraction_config.normalization"
            ),
            "schema_version": SCHEMA_VERSION,
            "pilot_size": _require_positive_int(
                payload["pilot_size"], "extraction_config.pilot_size"
            ),
            "random_size": _require_positive_int(
                payload["random_size"], "extraction_config.random_size"
            ),
            "risk_size": _require_positive_int(
                payload["risk_size"], "extraction_config.risk_size"
            ),
            "acceptance_rule": _require_plain_str(
                payload["acceptance_rule"], "extraction_config.acceptance_rule"
            ),
        },
    }


# --------------------------------------------------------------------------- #
# Hosted-use document validation
# --------------------------------------------------------------------------- #
def _validate_ref_list(value: object, field: str) -> list[dict]:
    if not isinstance(value, list):
        raise ExtractionHostedUseError(f"{field}: must be a list of RefRevision objects")
    refs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(value):
        item_field = f"{field}[{index}]"
        ref = _require_mapping(raw, item_field)
        _require_exact_keys(ref, _REF_KEYS, item_field)
        normalized = {
            "project_id": _require_identifier(ref["project_id"], f"{item_field}.project_id"),
            "artifact_id": _require_identifier(ref["artifact_id"], f"{item_field}.artifact_id"),
            "revision_id": _require_identifier(ref["revision_id"], f"{item_field}.revision_id"),
        }
        key = (normalized["project_id"], normalized["artifact_id"], normalized["revision_id"])
        if key in seen:
            raise ExtractionHostedUseError(f"{item_field}: duplicate reference")
        seen.add(key)
        refs.append(normalized)
    projects = {ref["project_id"] for ref in refs}
    if len(projects) > 1:
        raise ExtractionHostedUseError(f"{field}: all references must share one project")
    return refs


def validate_hosted_use_document(document: object) -> dict:
    """Validate one extraction hosted-use document; return a plain copy.

    The payload must be **exactly** ``{config_ref, report_refs, purpose,
    extraction_ref, provider_terms, privacy_review, pricing_evidence}``. For
    ``purpose == "quality"`` an ``extraction_ref`` is required; for
    ``purpose == "extraction"`` it must be ``null``. This is a *description* of an
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

    config_ref = _require_same_project_ref_offline(payload["config_ref"], "config_ref")
    report_refs = _validate_ref_list(payload["report_refs"], "extraction_hosted_use.report_refs")
    if not report_refs:
        raise ExtractionHostedUseError(
            "extraction_hosted_use.report_refs: must not be empty"
        )

    extraction_ref = payload["extraction_ref"]
    if extraction_ref is not None:
        extraction_ref = _require_same_project_ref_offline(
            extraction_ref, "extraction_ref"
        )
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


def _require_same_project_ref_offline(value: object, field: str) -> dict:
    """Static ref-shape validation (no store): a same-project RefRevision."""
    ref = _require_mapping(value, field)
    _require_exact_keys(ref, _REF_KEYS, field)
    return {
        "project_id": _require_identifier(ref["project_id"], f"{field}.project_id"),
        "artifact_id": _require_identifier(ref["artifact_id"], f"{field}.artifact_id"),
        "revision_id": _require_identifier(ref["revision_id"], f"{field}.revision_id"),
    }


def propose_hosted_use(
    *,
    config_ref: Mapping[str, str],
    report_refs: list[Mapping[str, str]],
    source_refs: list[Mapping[str, str]],
    purpose: str,
    extraction_ref: Mapping[str, str] | None,
    provider_terms: Mapping[str, str],
    privacy_review: Mapping[str, str],
    pricing_evidence: Mapping[str, str],
) -> dict:
    """Build (but do not persist) an extraction hosted-use document.

    Pure: no I/O, no write, no rights invented. The caller persists the returned
    document through the normal artifact repository with **caller-supplied**
    rights and pins ``config_ref`` + ``report_refs`` + ``source_refs`` +
    ``extraction_ref`` as dependencies so the gate snapshot closes the rights
    closure. ``source_refs`` are the ORIGINAL parsed-report sources.
    """
    document = {
        "schema_version": SCHEMA_VERSION,
        "document_type": HOSTED_USE_DOCUMENT_TYPE,
        "payload": {
            "config_ref": dict(config_ref),
            "report_refs": [dict(ref) for ref in report_refs],
            "purpose": purpose,
            "extraction_ref": None if extraction_ref is None else dict(extraction_ref),
            "provider_terms": dict(provider_terms),
            "privacy_review": dict(privacy_review),
            "pricing_evidence": dict(pricing_evidence),
        },
    }
    # Validate eagerly so the CLI cannot persist an invalid document.
    validate_hosted_use_document(document)
    return document


# --------------------------------------------------------------------------- #
# Document loading (byte-integrity, canonical, current)
# --------------------------------------------------------------------------- #
def _load_current_document(store, ref: object, *, document_type: str, validate):
    normalized = _require_same_project_ref(store, ref, "ref")
    content, revision = _read_current_revision(store, normalized)
    parsed = _parse_json_object(content, "document")
    document = validate(parsed)
    if canonical_json(document) != content:
        raise ExtractionRunnerError(
            "document: the stored revision is not the canonical document encoding"
        )
    return normalized, revision, document


def _load_config(store, ref: object):
    return _load_current_document(
        store, ref, document_type=CONFIG_DOCUMENT_TYPE,
        validate=validate_extraction_config_document,
    )


def _load_hosted_use(store, ref: object):
    return _load_current_document(
        store, ref, document_type=HOSTED_USE_DOCUMENT_TYPE,
        validate=validate_hosted_use_document,
    )


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
# Passage pool + source derivation
# --------------------------------------------------------------------------- #
def _report_passages(report: dict) -> list[dict]:
    artifact_id = report["report_ref"]["artifact_id"]
    out: list[dict] = []
    for passage in report["payload"]["passages"]:
        out.append(
            {
                "passage_id": f"{artifact_id}:{passage['passage_id']}",
                "source_key": artifact_id,
                "text": passage["text"],
                "raw_text": passage.get("raw_text"),
                "section_path": list(passage.get("section_path", [])),
                "report_ref": dict(report["report_ref"]),
            }
        )
    return out


def _derive_material(store, report_refs: list[dict], *, config: dict):
    """Read each parsed report, validate it, and build the passage pool.

    Every report must be the current revision, produced by the configured
    current parser/normalization, require no human action, and carry at least one
    passage. The ORIGINAL source refs are the exact ``derived_from`` documents.
    """
    parser = ParsingRepository(store)
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
        if report["human_action_required"]:
            raise ExtractionRunnerError(
                "report: requires human action; a necessary asset may be unreadable"
            )
        if not report["payload"]["passages"]:
            raise ExtractionRunnerError("report: carries no usable original passages")
        # The report must still be the artifact's current revision.
        _read_current_revision(store, ref)
        passages.extend(_report_passages(report))
        source = dict(report["source_ref"])
        key = (source["project_id"], source["artifact_id"], source["revision_id"])
        if key not in seen_sources:
            seen_sources.add(key)
            source_refs.append(source)
    if not passages:
        raise ExtractionRunnerError("reports: produced no usable original passages")
    return passages, source_refs


def _assemble_material(passages: list[dict]) -> str:
    lines = ["[original passages]"]
    for passage in passages:
        section = "/".join(passage.get("section_path") or [])
        lines.append(
            "[passage {pid}] source_key={sk} section={section}".format(
                pid=passage["passage_id"], sk=passage["source_key"], section=section
            )
        )
        lines.append(passage["text"])
    return "\n".join(lines)


def _example_shape() -> dict:
    """The raw model-output shape (normalized derived fields stripped)."""
    example = extraction_schema.extraction_example()
    return {
        key: value
        for key, value in example.items()
        if key not in ("link_summary", "validation")
    }


def _extraction_instructions(config: dict) -> str:
    return (
        config["instructions"]
        + "\n\n"
        + extraction_schema.EXTRACTION_INSTRUCTIONS
        + "\nEXAMPLE OUTPUT SHAPE (illustrative only; not evidence):\n"
        + canonical_json(_example_shape()).decode("utf-8")
    )


_QUALITY_CONTRACT = (
    "Task: quality checking and escalation review of a prior extraction against the "
    "ORIGINAL passages.\nReturn ONE JSON object with exactly these keys:\n"
    "- checks: an object with exactly these boolean keys (true = checked and "
    "reported): " + ", ".join(QUALITY_CHECK_KEYS) + ".\n"
    "- issues: an array; each item is {\"severity\": \"blocking\"|\"major\"|\"minor\""
    "|\"style\", \"description\": <string>, \"corrective_action\": <string or null>}.\n"
    "- escalation: \"none\"|\"human\"|\"stronger_model\".\n"
    "Explicitly seek OMISSIONS and QUALIFICATIONS (things present in the original but "
    "missing or softened in the extraction), not only re-check returned fields. Never "
    "invent a universal quality score."
)


def _quality_instructions(config: dict) -> str:
    return config["quality_instructions"] + "\n\n" + _QUALITY_CONTRACT


def _validate_quality_output(output: object) -> dict:
    if not isinstance(output, dict):
        raise ExtractionRunnerError("quality output must be a JSON object")
    if set(output) != {"checks", "issues", "escalation"}:
        raise ExtractionRunnerError(
            "quality output must carry exactly checks, issues, escalation"
        )
    checks = output["checks"]
    if not isinstance(checks, dict) or set(checks) != set(QUALITY_CHECK_KEYS):
        raise ExtractionRunnerError("quality checks are malformed")
    for key in QUALITY_CHECK_KEYS:
        if not isinstance(checks[key], bool):
            raise ExtractionRunnerError(f"quality check {key!r} must be a boolean")
    issues = output["issues"]
    if not isinstance(issues, list):
        raise ExtractionRunnerError("quality issues must be an array")
    normalized_issues = []
    for index, raw in enumerate(issues):
        if not isinstance(raw, dict) or set(raw) != {
            "severity", "description", "corrective_action"
        }:
            raise ExtractionRunnerError(f"quality issues[{index}] is malformed")
        severity = raw["severity"]
        if severity not in QUALITY_SEVERITIES:
            raise ExtractionRunnerError(f"quality issues[{index}].severity is invalid")
        description = raw["description"]
        if not isinstance(description, str) or not description.strip():
            raise ExtractionRunnerError(f"quality issues[{index}].description is empty")
        corrective = raw["corrective_action"]
        if corrective is not None and not isinstance(corrective, str):
            raise ExtractionRunnerError(
                f"quality issues[{index}].corrective_action must be a string or null"
            )
        normalized_issues.append(
            {
                "severity": severity,
                "description": description,
                "corrective_action": corrective,
            }
        )
    escalation = output["escalation"]
    if escalation not in QUALITY_ESCALATIONS:
        raise ExtractionRunnerError("quality escalation is invalid")
    return {
        "checks": {key: checks[key] for key in QUALITY_CHECK_KEYS},
        "issues": normalized_issues,
        "escalation": escalation,
    }


# --------------------------------------------------------------------------- #
# Identity digests / version set
# --------------------------------------------------------------------------- #
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
            "ref": {
                "version": model_ref,
                "digest": _sha256_hex(canonical_json({"model": model_ref})),
            },
        },
        "prompt": {
            "state": "known",
            "ref": {"version": PROMPT_VERSION, "digest": request_sha256},
        },
        "rubric": {"state": "not_applicable"},
        "parser": {"state": "not_applicable"},
        "adapter": {
            "state": "known",
            "ref": {
                "version": ADAPTER_REVISION,
                "digest": _sha256_hex(canonical_json({"endpoint": lead.ENDPOINT})),
            },
        },
    }
    return validate_version_set(versions, consumed=("model", "prompt", "adapter"))


def _role_configuration(purpose: str, config_map: Mapping[str, Any]) -> RoleConfiguration:
    role = PURPOSE_ROLES[purpose]
    try:
        model = ModelRef(
            provider_id="openrouter", model_id=config_map["model_id"], variant="direct"
        )
        return RoleConfiguration(roles=(RoleConfig(role=role, model=model, steps=1),))
    except ReviewAgentError as exc:
        raise ExtractionConfigDocumentError(
            "config: the configured model is not a usable direct-route model reference"
        ) from exc


# --------------------------------------------------------------------------- #
# Shared store-derived context
# --------------------------------------------------------------------------- #
def _derive_context(store, *, config, report_refs):
    """Reports + passages + source refs + codec identities (pure local reads)."""
    passages, source_refs = _derive_material(store, report_refs, config=config)
    for source in source_refs:
        _verify_hosted_rights(store, source)
    return passages, source_refs


# --------------------------------------------------------------------------- #
# require_pilot / require_run
# --------------------------------------------------------------------------- #
def require_pilot(store, pilot_ref, *, config_ref=None) -> dict:
    """Read back an approved **pilot** extraction run (bulk-mode gate).

    The pilot is a completed extraction run receipt whose ``mode`` is ``pilot``
    and whose config matches ``config_ref`` when supplied. It re-derives every
    byte through :func:`require_run`; a generic imported artifact alone never
    authorizes bulk execution.
    """
    run = require_run(store, pilot_ref, purpose="extraction")
    payload = run["run_payload"]
    if payload["mode"] != "pilot":
        raise ExtractionAuthorizationError("pilot: the referenced run is not a pilot")
    if payload["outcome"] != "completed":
        raise ExtractionAuthorizationError("pilot: the referenced pilot did not complete")
    if config_ref is not None:
        expected = _require_same_project_ref(store, config_ref, "config_ref")
        if payload["config_ref"] != expected:
            raise ExtractionAuthorizationError(
                "pilot: the pilot was run under a different config"
            )
    return run


def require_run(store, ref, *, purpose: str) -> dict:
    """Read and fully verify one durable extraction run receipt (read-only).

    Re-derives the exact config, parsed reports, original source rights, pinned
    model, input/request/instructions digests, and the durable accepted
    dispatch/task/proposal from the store, and returns the model output plus the
    safe evidence mapping. A receipt that does not match its accepted ledger
    evidence fails closed; an imported receipt alone is never authority.
    """
    if purpose not in PURPOSES:
        raise ExtractionRunnerError("purpose must be 'extraction' or 'quality'")
    ref, revision, payload = _load_run_receipt(store, ref)
    if payload["purpose"] != purpose:
        raise ExtractionReceiptError("run: purpose does not match the requested purpose")

    config_ref = _require_same_project_ref(store, payload["config_ref"], "config_ref")
    config_ref, config_revision, config_doc = _load_config(store, config_ref)
    config = config_doc["payload"]

    report_refs = [
        _require_same_project_ref(store, item, "report_refs[]")
        for item in payload["report_refs"]
    ]
    passages, source_refs = _derive_context(store, config=config, report_refs=report_refs)
    declared_sources = [
        _require_same_project_ref(store, item, "source_refs[]")
        for item in payload["source_refs"]
    ]
    if declared_sources != source_refs:
        raise ExtractionReceiptError(
            "run: the declared source references are not the reports' exact origins"
        )

    extraction_ref = None
    if payload["extraction_ref"] is not None:
        extraction_ref = _require_same_project_ref(
            store, payload["extraction_ref"], "extraction_ref"
        )
        if purpose != "quality":
            raise ExtractionReceiptError("run: an extraction_ref is only valid for quality")
    elif purpose == "quality":
        raise ExtractionReceiptError("run: quality requires an extraction_ref")

    if purpose == "quality":
        upstream = require_run(store, extraction_ref, purpose="extraction")
        if upstream["run_payload"]["config_ref"] != config_ref:
            raise ExtractionReceiptError(
                "run: the quality extraction_ref was run under a different config"
            )
        if upstream["source_refs"] != source_refs:
            raise ExtractionReceiptError(
                "run: the quality extraction_ref used different original sources"
            )

    return _verify_durable_evidence(
        store,
        ref=ref,
        payload=payload,
        purpose=purpose,
        config_ref=config_ref,
        config=config,
        config_revision=config_revision,
        report_refs=report_refs,
        source_refs=source_refs,
        passages=passages,
        extraction_ref=extraction_ref,
    )


def _verify_durable_evidence(
    store,
    *,
    ref,
    payload,
    purpose,
    config_ref,
    config,
    config_revision,
    report_refs,
    source_refs,
    passages,
    extraction_ref,
):
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)

    request_id = _require_identifier(payload["request_id"], "request_id")
    dispatch_record = dispatch.get_dispatch_by_request(request_id)
    if dispatch_record is None:
        raise ExtractionReceiptError("run: no durable dispatch is recorded for this run")
    if dispatch_record.stage != EXTRACTION_STAGE:
        raise ExtractionReceiptError("run: the dispatch stage is not extraction")
    if dispatch_record.route != "direct_call":
        raise ExtractionReceiptError("run: the dispatch route is not direct_call")
    if dispatch_record.state != "completed":
        raise ExtractionReceiptError("run: the dispatch is not completed")
    if dispatch_record.task_id != payload["task_id"]:
        raise ExtractionReceiptError("run: the dispatch task does not match the receipt")
    if dispatch_record.input_digest != payload["input_digest"]:
        raise ExtractionReceiptError("run: the dispatch input digest does not match")

    task = runtime.get_task(dispatch_record.task_id)
    if task is None:
        raise ExtractionReceiptError("run: the durable task is missing")
    if task.role != PURPOSE_ROLES[purpose]:
        raise ExtractionReceiptError("run: the task role does not match the purpose")
    if task.prompt_version != PROMPT_VERSION:
        raise ExtractionReceiptError("run: the task prompt version does not match")
    if dict(task.target) != config_ref:
        raise ExtractionReceiptError("run: the task target is not the config revision")

    configured = config["extraction_model"] if purpose == "extraction" else config["quality_model"]
    try:
        snapshot = runtime.get_configuration(task.config_snapshot_id)
    except Exception as exc:  # noqa: BLE001 - fail closed on a corrupt config snapshot
        raise ExtractionReceiptError("run: the task config snapshot could not be read") from exc
    if snapshot is None or snapshot.role_configuration() != _role_configuration(purpose, configured):
        raise ExtractionReceiptError("run: the task was not configured with the pinned model")

    proposal_record_id = payload["proposal_record_id"]
    proposal_digest = payload["proposal_digest"]
    if not isinstance(proposal_record_id, str) or not isinstance(proposal_digest, str):
        raise ExtractionReceiptError("run: the accepted proposal reference is missing")
    record = runtime.get_proposal(proposal_record_id)
    if record is None or record.task_id != task.task_id:
        raise ExtractionReceiptError("run: the accepted proposal record is missing")
    if record.stored_digest != proposal_digest:
        raise ExtractionReceiptError("run: the accepted proposal digest does not match")
    if (
        dispatch_record.result_ref != record.record_id
        or dispatch_record.result_digest != record.stored_digest
    ):
        raise ExtractionReceiptError(
            "run: the dispatch accepted result is not this proposal"
        )
    if record.model_id != configured["model_id"] or record.provider_id != "openrouter":
        raise ExtractionReceiptError("run: the accepted proposal model is not the pinned model")
    if payload["observed_model"] != record.model_id:
        raise ExtractionReceiptError("run: the observed model does not match the receipt")

    content = json.loads(record.canonical.decode("utf-8")).get("content")
    if not isinstance(content, dict):
        raise ExtractionReceiptError("run: the accepted proposal content is malformed")
    if content.get("purpose") != purpose:
        raise ExtractionReceiptError("run: the accepted proposal purpose does not match")
    raw_output = content.get("raw_output")
    raw_digest = payload["raw_output_sha256"]
    if raw_output is None:
        if raw_digest is not None:
            raise ExtractionReceiptError("run: the receipt declares output that does not exist")
    else:
        if raw_digest != _sha256_hex(canonical_json(raw_output)):
            raise ExtractionReceiptError("run: the reported output digest does not match")
    instructions_sha256 = content.get("instructions_sha256")
    if instructions_sha256 != payload["instructions_sha256"]:
        raise ExtractionReceiptError("run: the accepted instructions digest does not match")

    return {
        "purpose": purpose,
        "run_ref": dict(ref),
        "run_payload": payload,
        "config_ref": dict(config_ref),
        "config": config,
        "report_refs": [dict(item) for item in report_refs],
        "source_refs": [dict(item) for item in source_refs],
        "passages": passages,
        "raw_output": raw_output,
        "observed_model": record.model_id,
        "provider": record.provider_id,
        "dispatch_id": dispatch_record.dispatch_id,
        "task_id": task.task_id,
        "extraction_ref": None if extraction_ref is None else dict(extraction_ref),
        "cost_state": dispatch_record.cost_state,
        "cost_amount_usd": dispatch_record.cost_amount_usd,
        "reserve_usd": dispatch_record.reserve_usd,
        "no_automatic_retry": True,
    }


# --------------------------------------------------------------------------- #
# Outcome mapping
# --------------------------------------------------------------------------- #
def _execution_metadata(*, dispatch_id, result, input_digest, model_ref):
    versions = _reported_versions(input_digest, model_ref, result.request_sha256)
    usage = None
    if result.usage is not None:
        usage = {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
        }
    return {
        "actual_model": result.actual_model,
        "upstream_provider": result.provider,
        "response_id": result.response_id,
        "usage": usage,
        "version_set_json": canonical_json(versions).decode("utf-8"),
        "http_status": result.http_status,
        "request_sha256": result.request_sha256,
        "dispatch_id": dispatch_id,
        "no_automatic_retry": True,
    }


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
    mode: str = "pilot",
    pilot_ref=None,
    artifact_id: str,
    revision_id: str,
    rights: object,
) -> dict:
    """Run **one** bounded extraction/quality request (single-shot).

    The exact config and hosted-use revisions are re-read from the store, their
    named gates, the execution token, every original source's hosted-transfer
    rights, and (in bulk mode) the approved pilot are revalidated, a one-step task
    is recorded, and a ``direct_call`` dispatch is reserved **before** the single
    POST. The model output is schema-validated; a failure escalates to a human and
    accepts no extraction. The run receipt is persisted as a new artifact
    revision from **caller-supplied** rights (this module fabricates no rights).
    An already-recorded request is observed, never re-sent.
    """
    request_id = _require_identifier(request_id, "request_id")
    if not isinstance(api_key, str) or api_key == "":
        raise ExtractionRunnerError("api_key: a non-empty credential is required")
    if purpose not in PURPOSES:
        raise ExtractionRunnerError("purpose must be 'extraction' or 'quality'")
    if mode not in MODES:
        raise ExtractionRunnerError("mode must be 'pilot' or 'bulk'")
    _assert_token_active(registry, store, activation_token)

    config_ref = _require_same_project_ref(store, config_ref, "config_ref")
    config_ref, config_revision, config_doc = _load_config(store, config_ref)
    config = config_doc["payload"]

    hosted_ref, _hosted_revision, hosted_doc = _load_hosted_use(store, hosted_ref)
    hosted = hosted_doc["payload"]
    if hosted["purpose"] != purpose:
        raise ExtractionHostedUseError("hosted-use: purpose does not match the request")
    if hosted["config_ref"] != config_ref:
        raise ExtractionHostedUseError("hosted-use: config_ref does not match the request")
    report_refs = [
        _require_same_project_ref(store, item, "report_refs[]") for item in report_refs
    ]
    if [dict(item) for item in hosted["report_refs"]] != report_refs:
        raise ExtractionHostedUseError("hosted-use: report_refs do not match the request")
    extraction_ref = (
        None
        if extraction_ref is None
        else _require_same_project_ref(store, extraction_ref, "extraction_ref")
    )
    if (hosted["extraction_ref"] is None) != (extraction_ref is None) or (
        hosted["extraction_ref"] is not None
        and hosted["extraction_ref"] != extraction_ref
    ):
        raise ExtractionHostedUseError("hosted-use: extraction_ref does not match the request")

    # (1) The named config gate must be active for this config revision.
    _assert_gate_satisfied(store, config_ref["artifact_id"], CONFIG_GATE_KIND, CONFIG_GATE_ID)

    # Derive the exact material + ORIGINAL sources (rights verified) before the
    # hosted-use closure is checked, so nothing can be digested unapproved.
    passages, source_refs = _derive_context(store, config=config, report_refs=report_refs)

    # (2) The hosted-use revision must pin exactly its declared closure.
    _verify_hosted_dependencies(
        store, hosted_ref, config_ref, report_refs, extraction_ref, source_refs
    )
    # (3) The named hosted-use gate must be active for this hosted revision.
    _assert_gate_satisfied(
        store, hosted_ref["artifact_id"], HOSTED_USE_GATE_KIND, hosted_use_gate_id(purpose)
    )

    upstream_run = None
    if purpose == "quality":
        upstream_run = require_run(store, extraction_ref, purpose="extraction")
        if upstream_run["run_payload"]["config_ref"] != config_ref:
            raise ExtractionRunnerError("quality: extraction_ref was run under a different config")
        if upstream_run["raw_output"] is None:
            raise ExtractionRunnerError("quality: extraction_ref accepted no extraction output")
    if mode == "bulk":
        if pilot_ref is None:
            raise ExtractionAuthorizationError("bulk: an approved pilot reference is required")
        require_pilot(store, pilot_ref, config_ref=config_ref)

    # (4) Build the deterministic trusted instructions and untrusted material.
    model_map = config["extraction_model"] if purpose == "extraction" else config["quality_model"]
    if purpose == "extraction":
        instructions = _extraction_instructions(config)
        material = _assemble_material(passages)
    else:
        instructions = _quality_instructions(config)
        material = (
            _assemble_material(passages)
            + "\n[prior extraction output]\n"
            + canonical_json(upstream_run["raw_output"]).decode("utf-8")
        )

    lead_config = LeadConfig.from_mapping(model_map)
    plan = runtime_extraction.plan_extraction(
        lead_config, purpose=purpose, instructions=instructions, text=material
    )

    role_configuration = _role_configuration(purpose, model_map)
    model_ref = role_configuration.for_role(PURPOSE_ROLES[purpose]).model.ref

    input_refs = _build_input_refs(
        store, config_ref, report_refs, source_refs, extraction_ref
    )
    input_digest = _input_digest(store.project_id, input_refs)
    version_digest = _version_digest(purpose, model_map)
    bound_evidence_digest = _sha256_hex(canonical_json(dict(plan.bound_evidence)))

    bounded = BoundedTask(
        project_id=store.project_id,
        role=PURPOSE_ROLES[purpose],
        target={
            "project_id": store.project_id,
            "artifact_id": config_ref["artifact_id"],
            "revision_id": config_ref["revision_id"],
        },
        input_refs=input_refs,
        scope=[EXTRACTION_STAGE, purpose, mode],
        prompt_version=PROMPT_VERSION,
        size_limit=TASK_SIZE_LIMIT,
        step_limit=1,
        role_config=role_configuration.for_role(PURPOSE_ROLES[purpose]),
    )

    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    task_record = runtime.record_task(
        bounded, request_id=request_id, configuration=role_configuration
    )

    with registry.execution_write_guard(activation_token, store.project_id):
        existing = dispatch.get_dispatch_by_request(request_id)
        if existing is not None:
            return _finalize_replay(
                store,
                ref_material=_ReceiptMaterial(
                    artifact_id=artifact_id, revision_id=revision_id, rights=rights,
                    purpose=purpose, config_ref=config_ref, report_refs=report_refs,
                    source_refs=source_refs, extraction_ref=extraction_ref,
                    request_id=request_id, mode=mode, pilot_ref=pilot_ref,
                ),
            )
        dispatch_record = dispatch.reserve_dispatch(
            task_record,
            request_id=request_id,
            route="direct_call",
            stage=EXTRACTION_STAGE,
            input_digest=input_digest,
            version_digest=version_digest,
            reserve_usd=plan.reserve_usd,
            bound_evidence_digest=bound_evidence_digest,
        )
        dispatch_record = dispatch.mark_submitted(dispatch_record.dispatch_id)

    # Revalidate the exact gates and the active execution token immediately before
    # the POST (the registry lock is released during network I/O).
    _assert_gate_satisfied(store, config_ref["artifact_id"], CONFIG_GATE_KIND, CONFIG_GATE_ID)
    _assert_gate_satisfied(
        store, hosted_ref["artifact_id"], HOSTED_USE_GATE_KIND, hosted_use_gate_id(purpose)
    )
    _assert_token_active(registry, store, activation_token)

    result = runtime_extraction.execute_extraction(
        plan, api_key=api_key, transport=transport
    )

    with registry.execution_write_guard(
        activation_token, store.project_id, allow_draining=True
    ):
        outcome, content, result_kind = _classify_result(
            result, purpose=purpose, config=config, passages=passages,
            instructions_sha256=plan.instructions_sha256,
            dispatch_id=dispatch_record.dispatch_id, input_digest=input_digest,
            model_ref=model_ref,
        )
        record = _commit_outcome(
            store, runtime=runtime, dispatch=dispatch,
            task_record=task_record, dispatch_id=dispatch_record.dispatch_id,
            fence=dispatch_record.current_fence, result=result,
            outcome=outcome, content=content, result_kind=result_kind,
            role_configuration=role_configuration,
        )

    material_ref = _ReceiptMaterial(
        artifact_id=artifact_id, revision_id=revision_id, rights=rights,
        purpose=purpose, config_ref=config_ref, report_refs=report_refs,
        source_refs=source_refs, extraction_ref=extraction_ref,
        request_id=request_id, mode=mode, pilot_ref=pilot_ref,
    )
    receipt = _write_run_receipt(
        store, material=material_ref, dispatch_record=dispatch_record,
        task_id=task_record.task_id, outcome=outcome, content=content,
        proposal_record=record, plan=plan, input_digest=input_digest,
    )
    return _readback(store, request_id, run_ref=receipt, posted=True)


class _ReceiptMaterial:
    __slots__ = (
        "artifact_id", "revision_id", "rights", "purpose", "config_ref",
        "report_refs", "source_refs", "extraction_ref", "request_id", "mode",
        "pilot_ref",
    )

    def __init__(self, **kwargs):
        for key in self.__slots__:
            setattr(self, key, kwargs[key])


def _build_input_refs(store, config_ref, report_refs, source_refs, extraction_ref):
    refs = [config_ref]
    refs.extend(report_refs)
    if extraction_ref is not None:
        refs.append(extraction_ref)
    refs.extend(source_refs)
    out = []
    for ref in refs:
        content, revision = _read_current_revision(store, ref)
        out.append(
            {
                "project_id": store.project_id,
                "artifact_id": ref["artifact_id"],
                "revision_id": ref["revision_id"],
                "digest": _sha256_hex(content),
            }
        )
    return out


def _verify_hosted_dependencies(
    store, hosted_ref, config_ref, report_refs, extraction_ref, source_refs
):
    revision = ArtifactRepository(store).get_revision(
        hosted_ref["artifact_id"], hosted_ref["revision_id"]
    )
    if revision is None:
        raise ExtractionHostedUseError("hosted-use: the pinned revision is missing")
    edges = {(d.artifact_id, d.revision_id) for d in revision.dependencies}
    expected = {(config_ref["artifact_id"], config_ref["revision_id"])}
    expected |= {(r["artifact_id"], r["revision_id"]) for r in report_refs}
    if extraction_ref is not None:
        expected.add((extraction_ref["artifact_id"], extraction_ref["revision_id"]))
    expected |= {(r["artifact_id"], r["revision_id"]) for r in source_refs}
    if not expected <= edges:
        raise ExtractionHostedUseError(
            "hosted-use: the revision does not pin its declared rights closure"
        )


# --------------------------------------------------------------------------- #
# Result classification + commit
# --------------------------------------------------------------------------- #
def _classify_result(
    result, *, purpose, config, passages, instructions_sha256, dispatch_id,
    input_digest, model_ref,
):
    metadata = _execution_metadata(
        dispatch_id=dispatch_id, result=result, input_digest=input_digest,
        model_ref=model_ref,
    )
    outcome = result.outcome
    if outcome == "completed":
        # Normalize the frozen adapter output back to plain JSON containers so the
        # proposal envelope never shares interned empty containers (which the
        # data-only structural walk correctly refuses as ambiguous identity).
        raw = json.loads(canonical_json(result.proposal_json).decode("utf-8"))
        if purpose == "extraction":
            try:
                extraction_schema.validate_extraction(
                    raw, passages=passages,
                    appraisal_dimensions=config["appraisal_dimensions"],
                )
            except extraction_schema.ExtractionError:
                body = {
                    "purpose": purpose,
                    "outcome": "escalation",
                    "raw_output": raw,
                    "instructions_sha256": instructions_sha256,
                    "escalation": "human",
                    "reason": "extraction_schema_invalid",
                    "execution_metadata": metadata,
                }
                return "escalation", body, _RESULT_KIND_ESCALATION
            body = {
                "purpose": purpose,
                "outcome": "completed",
                "raw_output": raw,
                "instructions_sha256": instructions_sha256,
                "escalation": "none",
                "execution_metadata": metadata,
            }
            return "completed", body, _RESULT_KIND_PROPOSAL
        try:
            _validate_quality_output(raw)
        except ExtractionRunnerError:
            body = {
                "purpose": purpose,
                "outcome": "escalation",
                "raw_output": raw,
                "instructions_sha256": instructions_sha256,
                "escalation": "human",
                "reason": "quality_schema_invalid",
                "execution_metadata": metadata,
            }
            return "escalation", body, _RESULT_KIND_ESCALATION
        body = {
            "purpose": purpose,
            "outcome": "completed",
            "raw_output": raw,
            "instructions_sha256": instructions_sha256,
            "escalation": raw["escalation"],
            "execution_metadata": metadata,
        }
        return "completed", body, _RESULT_KIND_PROPOSAL

    # Any non-completed adapter outcome is a diagnostic machine record.
    body = {
        "purpose": purpose,
        "outcome": outcome,
        "raw_output": None,
        "instructions_sha256": instructions_sha256,
        "escalation": "human" if outcome in ("unknown", "protocol_error", "refused") else "none",
        "execution_metadata": metadata,
    }
    kind = {
        "refused": _RESULT_KIND_REFUSED,
        "protocol_error": _RESULT_KIND_PROTOCOL,
    }.get(outcome, _RESULT_KIND_PROTOCOL)
    return outcome, body, kind


def _commit_outcome(
    store, *, runtime, dispatch, task_record, dispatch_id, fence, result,
    outcome, content, result_kind, role_configuration,
):
    with store.transaction():
        record = runtime._record_proposal_in_transaction(
            task_record, {"content": content}, configuration=role_configuration
        )
        if outcome == "completed":
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(
                dispatch_id, cost_state=cost_state, amount_usd=amount
            )
            dispatch._accept_result_in_transaction(
                dispatch_id, fence=fence, result_kind=result_kind,
                result_ref=record.record_id, result_digest=record.stored_digest,
            )
        elif outcome == "failed":
            dispatch._settle_cost_in_transaction(
                dispatch_id, cost_state="not_incurred", allow_definitive_not_incurred=True
            )
            dispatch._mark_failed_in_transaction(dispatch_id, definitive_rejection=True)
        elif outcome == "unknown":
            dispatch._settle_cost_in_transaction(dispatch_id, cost_state="unknown")
            dispatch._recover_in_transaction(dispatch_id)
        else:  # escalation / refused / protocol_error (accepted diagnostics)
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(
                dispatch_id, cost_state=cost_state, amount_usd=amount
            )
            dispatch._accept_result_in_transaction(
                dispatch_id, fence=fence, result_kind=result_kind,
                result_ref=record.record_id, result_digest=record.stored_digest,
            )
    return record


# --------------------------------------------------------------------------- #
# Run receipt artifact + readback
# --------------------------------------------------------------------------- #
def _write_run_receipt(
    store, *, material, dispatch_record, task_id, outcome, content,
    proposal_record, plan, input_digest,
):
    raw_output = content["raw_output"]
    raw_digest = None if raw_output is None else _sha256_hex(canonical_json(raw_output))
    payload = {
        "purpose": material.purpose,
        "config_ref": dict(material.config_ref),
        "report_refs": [dict(ref) for ref in material.report_refs],
        "source_refs": [dict(ref) for ref in material.source_refs],
        "extraction_ref": None if material.extraction_ref is None else dict(material.extraction_ref),
        "request_id": material.request_id,
        "dispatch_id": dispatch_record.dispatch_id,
        "task_id": task_id,
        "proposal_record_id": proposal_record.record_id,
        "proposal_digest": proposal_record.stored_digest,
        "observed_model": proposal_record.model_id,
        "request_sha256": plan.request_sha256,
        "input_digest": input_digest,
        "instructions_sha256": plan.instructions_sha256,
        "mode": material.mode,
        "pilot_ref": None if material.pilot_ref is None else dict(material.pilot_ref),
        "outcome": outcome,
        "raw_output_sha256": raw_digest,
    }
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "document_type": RUN_DOCUMENT_TYPE,
        "payload": payload,
    }
    repository = ArtifactRepository(store)
    repository.create_artifact(material.artifact_id, kind="document", created_by="agent")
    dependencies = [
        {"project_id": store.project_id, "artifact_id": ref["artifact_id"], "revision_id": ref["revision_id"]}
        for ref in material.report_refs
    ]
    dependencies.append(
        {"project_id": store.project_id, "artifact_id": material.config_ref["artifact_id"], "revision_id": material.config_ref["revision_id"]}
    )
    if material.extraction_ref is not None:
        dependencies.append(
            {"project_id": store.project_id, "artifact_id": material.extraction_ref["artifact_id"], "revision_id": material.extraction_ref["revision_id"]}
        )
    for ref in material.source_refs:
        dependencies.append(
            {"project_id": store.project_id, "artifact_id": ref["artifact_id"], "revision_id": ref["revision_id"]}
        )
    stored = repository.import_revision(
        artifact_id=material.artifact_id,
        revision_id=material.revision_id,
        content=envelope,
        origin_locator=f"extraction:{material.request_id}",
        rights=material.rights,
        dependencies=dependencies,
        acquired_by="agent",
    )
    return {"project_id": store.project_id, "artifact_id": stored.artifact_id, "revision_id": stored.revision_id}


def _finalize_replay(store, *, ref_material):
    """An already-recorded request: observe the stored outcome and return it.

    No second POST is ever issued. The existing run receipt (when it was written)
    is returned by reference; it is never re-created from the replay.
    """
    run_ref = None
    candidate = {
        "project_id": store.project_id,
        "artifact_id": ref_material.artifact_id,
        "revision_id": ref_material.revision_id,
    }
    try:
        _load_run_receipt(store, candidate)
        run_ref = candidate
    except ExtractionRunnerError:
        run_ref = None
    return _readback(store, ref_material.request_id, run_ref=run_ref, posted=False)


def _readback(store, request_id: str, *, run_ref, posted: bool) -> dict:
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    task = runtime.get_task_by_request(request_id)
    dispatch_record = dispatch.get_dispatch_by_request(request_id)
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
    }
    if dispatch_record is None:
        return mapping
    outcome = _outcome_from_dispatch(dispatch_record)
    mapping.update(
        {
            "status": outcome,
            "outcome": outcome,
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
            if not isinstance(metadata, dict):
                continue
            if metadata.get("dispatch_id") != dispatch_record.dispatch_id:
                continue
            mapping["proposal_record_id"] = record.record_id
            mapping["actual_model"] = metadata.get("actual_model")
            mapping["provider"] = metadata.get("upstream_provider")
            mapping["response_id"] = metadata.get("response_id")
            mapping["usage"] = metadata.get("usage")
            mapping["http_status"] = metadata.get("http_status")
            mapping["request_sha256"] = metadata.get("request_sha256")
            break
    return mapping


def _outcome_from_dispatch(dispatch_record) -> str:
    state = dispatch_record.state
    if state == "completed":
        kind = dispatch_record.result_kind
        if kind == _RESULT_KIND_PROPOSAL:
            return "completed"
        if kind == _RESULT_KIND_ESCALATION:
            return "escalation"
        if kind == _RESULT_KIND_REFUSED:
            return "refused"
        if kind == _RESULT_KIND_PROTOCOL:
            return "protocol_error"
        return "completed"
    if state == "failed":
        return "failed"
    if state == "unknown":
        return "unknown"
    return state
