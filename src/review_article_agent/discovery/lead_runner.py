"""Revision-scoped **hosted lead proposal runner** (Step 05, M2).

Authority
---------

* ``docs/implementation/05-discovery.md`` deliverable 1 (brief/interview
  artifacts, seed search and **individual** seed approvals, framing challenge,
  provisional scope map, rubric/search-plan approval) and acceptance criterion 1
  ("Seed abstracts remain provisional; unapproved seeds cannot silently become
  accepted project context") and step 3 of the confirmed scientific workflow
  (``docs/specification.md`` lines 24-27: an advanced lead model interviews the
  user about a brief and challenges terminology/frameworks).
* ``docs/contracts/02-gates-and-runtime.md`` §5.6 (reserve before dispatch; an
  unknown reserve is retained and never silently released) and §6.3 (exact-decimal
  direct-call cost, accounted separately from OpenCode session statistics), and
  §8 (post messages carry no API key; one bounded logical request per durable
  job, no blind retry).
* ``docs/contracts/03-policy-decisions.md`` §5.2 (rights eligibility is **not**
  final authorization; a hosted transfer needs an out-of-band scoped
  authorization) and POL-P5 (provider-term verification gates hosted use).

The user explicitly approved an architecture exception on **2026-10-28**: one
bounded **direct** OpenRouter tool-free lead proposal request per durable job,
via the existing pure adapter (:mod:`review_article_agent.runtime.lead`). The
runner performs **no live call, upload, or paid request unless the caller
supplies a real ``api_key`` and omits ``transport``**; deterministic tests inject
a fake ``transport`` so no test performs a network call.

What this module **is**
-----------------------

* :func:`validate_hosted_use_document` — pure, static validation of the generic
  hosted-use **report** envelope ``{schema_version, document_type,
  payload}`` with ``document_type == "discovery_hosted_use"`` and the exact
  payload ``{purpose, config, source_refs, provider_terms, privacy_review,
  pricing_evidence}``.
* :func:`execute_lead_proposal` — the revision-scoped, single-shot runner. It
  re-derives every byte from the store, revalidates the exact named ``batch``
  gate and the execution activation, reserves a ``direct_call`` dispatch
  **before** the one POST, and commits the non-authoritative proposal together
  with the dispatch's accepted result and cost settlement.
* :func:`get_lead_outcome` — a read-only readback of a durable logical request.

What this module deliberately **is not**
----------------------------------------

* **Not an authorizer.** The hosted-use batch is a *description* of an
  authorization; the out-of-band authority is the real owner-authenticated
  ``batch``/``discovery.hosted_use.<purpose>`` gate recorded through
  :class:`~review_article_agent.storage.human_approvals.HumanApprovalRepository`.
  No field of the batch document (or the model's reply) can create, widen, or
  substitute for that gate.
* **Not a retry engine.** One logical request per durable job: a completed
  request returns its stored outcome; an interrupted one recovers to ``unknown``
  and is never re-sent. It never activates execution itself.
* **Not a legal or scientific verifier.** Provider-term, pricing, and
  privacy-review evidence references are only checked to be present, non-empty,
  and time-valid; they are human-reviewed evidence, never a machine legal
  determination.

Every failure is a static, value-free :class:`HostedLeadError` (or an already
typed dispatch/approval error); no message ever echoes an API key, source text,
identifier, or stored byte.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..runtime import lead
from ..runtime.dispatch import RuntimeDispatchRepository
from ..runtime.lead import LeadConfig, TransportResponse
from ..runtime.roles import (
    BoundedTask,
    ModelRef,
    RoleConfig,
    RoleConfiguration,
    submit_proposal,
)
from ..storage.agent_runtime import RuntimeRepository, validate_version_set
from ..storage.artifacts import ArtifactRepository, sha256_digest
from ..storage.human_approvals import HumanApprovalRepository
from . import context as discovery_context

__all__ = [
    "HOSTED_USE_DOCUMENT_TYPE",
    "HOSTED_USE_SCHEMA_VERSION",
    "HOSTED_USE_PURPOSES",
    "HOSTED_USE_GATE_PREFIX",
    "HOSTED_USE_GATE_KIND",
    "DISCOVERY_STAGE",
    "LEAD_ROLE",
    "LEAD_PROMPT_VERSION",
    "HostedLeadError",
    "HostedUseDocumentError",
    "HostedUseAuthorizationError",
    "HostedUseRightsError",
    "validate_hosted_use_document",
    "execute_lead_proposal",
    "get_lead_outcome",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The only supported hosted-use document-envelope schema version.
HOSTED_USE_SCHEMA_VERSION = 1

#: The one document type this runner accepts as a hosted-use batch.
HOSTED_USE_DOCUMENT_TYPE = "discovery_hosted_use"

#: The two lead purposes a hosted-use batch may describe.
HOSTED_USE_PURPOSES = tuple(lead.PURPOSES)

#: The gate kind and gate-id prefix of the out-of-band hosted-use authorization.
HOSTED_USE_GATE_KIND = "batch"
HOSTED_USE_GATE_PREFIX = "discovery.hosted_use."

#: The direct-call dispatch stage this runner reserves under.
DISCOVERY_STAGE = "discovery"

#: The scientific role the lead adapter acts as.
LEAD_ROLE = "lead"

#: The bounded, whitespace-free prompt version pinned on the lead task.
LEAD_PROMPT_VERSION = "lead.hosted_use.v1"

#: The adapter revision label recorded in the reported ``VersionSet``.
LEAD_ADAPTER_REVISION = "lead.direct.v1"

#: Bounded sizes for the evidence references and identifiers.
MAX_EVIDENCE_REF_LEN = 2048
MAX_IDENTIFIER_LEN = 512

#: The exact envelope and payload keys (no other key is permitted).
_ENVELOPE_KEYS = ("schema_version", "document_type", "payload")
_PAYLOAD_KEYS = (
    "purpose",
    "config",
    "source_refs",
    "provider_terms",
    "privacy_review",
    "pricing_evidence",
)
_EVIDENCE_KEYS = ("ref", "verified_at")

#: An exact RFC 3339 UTC timestamp (with an explicit UTC designator).
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: The result-kind labels the runner records on a terminal dispatch.
RESULT_KIND_PROPOSAL = "lead_proposal"
RESULT_KIND_REFUSED = "lead_refused"
RESULT_KIND_PROTOCOL = "lead_protocol_error"

#: The bounded scope of the lead task (never free-form caller text).
_TASK_SCOPE = ("discovery.hosted_use",)
_TASK_SIZE_LIMIT = 262_144


# --------------------------------------------------------------------------- #
# Errors (static messages; never echo input)
# --------------------------------------------------------------------------- #
class HostedLeadError(ReviewAgentError):
    """A hosted lead-proposal run failed before any dispatch was issued."""


class HostedUseDocumentError(HostedLeadError):
    """A hosted-use batch document is malformed or not canonical."""


class HostedUseAuthorizationError(HostedLeadError):
    """The exact out-of-band hosted-use gate or the execution token is not active."""


class HostedUseRightsError(HostedLeadError):
    """A pinned source's rights record does not permit the hosted transfer."""


# --------------------------------------------------------------------------- #
# Small static helpers
# --------------------------------------------------------------------------- #
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scoped_id(prefix: str, project_id: str, key: str) -> str:
    digest = _sha256_hex((project_id + "\x00" + key).encode("utf-8"))
    return f"{prefix}_{digest[:32]}"


def _require_mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise HostedUseDocumentError(f"{field}: must be an object")
    return value


def _require_exact_keys(obj: object, allowed: tuple[str, ...], field: str) -> None:
    if not isinstance(obj, Mapping):
        raise HostedUseDocumentError(f"{field}: must be an object")
    for key in obj:
        if not isinstance(key, str):
            raise HostedUseDocumentError(f"{field}: object keys must be strings")
    if set(obj) != set(allowed):
        raise HostedUseDocumentError(f"{field}: fields must be exactly {list(allowed)}")


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _require_plain_str(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise HostedUseDocumentError(f"{field}: must be a non-empty string")
    if _has_surrogate(value):
        raise HostedUseDocumentError(f"{field}: must not contain an invalid surrogate")
    return value


def _require_evidence_ref(value: object, field: str) -> str:
    text = _require_plain_str(value, field)
    if text != text.strip() or len(text) > MAX_EVIDENCE_REF_LEN:
        raise HostedUseDocumentError(f"{field}: must be a bounded, trimmed reference")
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise HostedUseDocumentError(
                f"{field}: must not contain control characters"
            )
    return text


def _require_utc_timestamp(value: object, field: str) -> str:
    text = _require_plain_str(value, field)
    if _UTC_TIMESTAMP_RE.fullmatch(text) is None:
        raise HostedUseDocumentError(f"{field}: must be an RFC 3339 UTC timestamp")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HostedUseDocumentError(f"{field}: must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise HostedUseDocumentError(f"{field}: must carry an explicit UTC offset")
    return text


def _require_identifier(value: object, field: str) -> str:
    try:
        normalized = validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise HostedLeadError(f"{field}: must be a valid opaque identifier") from exc
    if len(normalized) > MAX_IDENTIFIER_LEN:
        raise HostedLeadError(f"{field}: is too long")
    return normalized


def _require_same_project_ref(store, value: object, field: str) -> dict:
    try:
        return validate_ref(value, store.project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise HostedLeadError(
            f"{field}: must be an exact same-project RefRevision"
        ) from exc


# --------------------------------------------------------------------------- #
# Static document validation
# --------------------------------------------------------------------------- #
def _validate_evidence(value: object, field: str) -> dict:
    _require_exact_keys(value, _EVIDENCE_KEYS, field)
    return {
        "ref": _require_evidence_ref(value["ref"], f"{field}.ref"),
        "verified_at": _require_utc_timestamp(
            value["verified_at"], f"{field}.verified_at"
        ),
    }


def _validate_source_refs(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise HostedUseDocumentError(
            "discovery_hosted_use.source_refs: must be a list of RefRevision objects"
        )
    refs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(value):
        field = f"discovery_hosted_use.source_refs[{index}]"
        ref = _require_mapping(raw, field)
        _require_exact_keys(ref, ("project_id", "artifact_id", "revision_id"), field)
        normalized = {
            "project_id": _require_identifier(ref["project_id"], f"{field}.project_id"),
            "artifact_id": _require_identifier(ref["artifact_id"], f"{field}.artifact_id"),
            "revision_id": _require_identifier(ref["revision_id"], f"{field}.revision_id"),
        }
        key = (
            normalized["project_id"],
            normalized["artifact_id"],
            normalized["revision_id"],
        )
        if key in seen:
            raise HostedUseDocumentError(f"{field}: duplicate source reference")
        seen.add(key)
        refs.append(normalized)
    projects = {ref["project_id"] for ref in refs}
    if len(projects) > 1:
        raise HostedUseDocumentError(
            "discovery_hosted_use.source_refs: all references must share one project"
        )
    return refs


def validate_hosted_use_document(document: object) -> dict:
    """Validate one hosted-use batch document; return a plain, independent copy.

    The envelope must be **exactly** ``{schema_version, document_type, payload}``
    with ``schema_version == 1`` and ``document_type == "discovery_hosted_use"``.
    The payload must be **exactly**
    ``{purpose, config, source_refs, provider_terms, privacy_review,
    pricing_evidence}``: ``purpose`` is ``interview``/``framing``, ``config`` is a
    strict :class:`~review_article_agent.runtime.lead.LeadConfig` mapping,
    ``source_refs`` is a list of exact same-project ``RefRevision`` objects, and
    each evidence field is exactly ``{ref, verified_at}`` with a non-empty
    reference and an RFC 3339 UTC ``verified_at``.

    This performs no I/O, invents no default, and grants **nothing**: the batch
    is a *description* of an authorization, never authority.
    """
    envelope = _require_mapping(document, "document")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "document")

    version = envelope["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise HostedUseDocumentError("document.schema_version: must be an integer")
    if version != HOSTED_USE_SCHEMA_VERSION:
        raise HostedUseDocumentError(
            f"document.schema_version: must be the supported version "
            f"{HOSTED_USE_SCHEMA_VERSION}"
        )

    if envelope["document_type"] != HOSTED_USE_DOCUMENT_TYPE:
        raise HostedUseDocumentError(
            "document.document_type: is not the hosted-use document type"
        )

    payload = _require_mapping(envelope["payload"], "document.payload")
    _require_exact_keys(payload, _PAYLOAD_KEYS, "discovery_hosted_use.payload")

    purpose = payload["purpose"]
    if purpose not in HOSTED_USE_PURPOSES:
        raise HostedUseDocumentError(
            "discovery_hosted_use.purpose: is not an allowed lead purpose"
        )

    try:
        config = LeadConfig.from_mapping(payload["config"])
    except (lead.LeadError, RecordValidationError, TypeError, ValueError) as exc:
        raise HostedUseDocumentError(
            "discovery_hosted_use.config: is not a valid hosted-use configuration"
        ) from exc

    source_refs = _validate_source_refs(payload["source_refs"])

    return {
        "schema_version": HOSTED_USE_SCHEMA_VERSION,
        "document_type": HOSTED_USE_DOCUMENT_TYPE,
        "payload": {
            "purpose": purpose,
            "config": config.to_mapping(),
            "source_refs": source_refs,
            "provider_terms": _validate_evidence(
                payload["provider_terms"], "discovery_hosted_use.provider_terms"
            ),
            "privacy_review": _validate_evidence(
                payload["privacy_review"], "discovery_hosted_use.privacy_review"
            ),
            "pricing_evidence": _validate_evidence(
                payload["pricing_evidence"], "discovery_hosted_use.pricing_evidence"
            ),
        },
    }


def hosted_use_gate_id(purpose: str) -> str:
    """The exact named gate id of a hosted-use purpose (``batch`` gate kind)."""
    return HOSTED_USE_GATE_PREFIX + purpose


# --------------------------------------------------------------------------- #
# Store reads (canonical, byte-checked, current)
# --------------------------------------------------------------------------- #
def _read_current_revision(store, ref: Mapping[str, str]):
    """Read one exact **current** revision's stored bytes (byte-integrity checked)."""
    repo = ArtifactRepository(store)
    revision = repo.get_revision(ref["artifact_id"], ref["revision_id"])
    if revision is None:
        raise HostedLeadError("ref: the requested revision does not exist in this project")
    current = repo.current_revision(ref["artifact_id"])
    if current is None or current.revision_id != ref["revision_id"]:
        raise HostedLeadError(
            "ref: the requested revision is not the artifact's current revision"
        )
    content = repo.read_content(ref["artifact_id"], ref["revision_id"])
    if (
        sha256_digest(content) != revision.content_sha256
        or len(content) != revision.byte_size
    ):
        raise HostedLeadError(
            "ref: the stored revision fails byte-integrity validation"
        )
    return content, revision


def _parse_json_object(content: bytes, field: str) -> object:
    def _reject(_token: str) -> object:
        raise HostedUseDocumentError(f"{field}: is not portable canonical JSON")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostedUseDocumentError(f"{field}: is not valid UTF-8") from exc
    try:
        return json.loads(text, parse_constant=_reject)
    except HostedUseDocumentError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise HostedUseDocumentError(f"{field}: is not a JSON document") from exc


def _load_batch(store, batch_ref: object):
    """Read and validate the current hosted-use batch revision."""
    ref = _require_same_project_ref(store, batch_ref, "batch_ref")
    content, revision = _read_current_revision(store, ref)
    parsed = _parse_json_object(content, "batch")
    document = validate_hosted_use_document(parsed)
    if canonical_json(document) != content:
        raise HostedUseDocumentError(
            "batch: the stored revision is not the canonical document encoding"
        )
    return ref, revision, document


def _verify_batch_sources(revision, source_refs: list[dict]) -> None:
    """The batch revision must pin **exactly** its declared source refs.

    A source ref that is not an exact generic artifact dependency (or a pinned
    dependency that is not declared) is refused, so the human-approved closure
    and the in-band manifest can never disagree.
    """
    pinned = {(d.artifact_id, d.revision_id) for d in revision.dependencies}
    declared = {(r["artifact_id"], r["revision_id"]) for r in source_refs}
    if pinned != declared:
        raise HostedLeadError(
            "batch: the declared source references are not exactly the revision's "
            "pinned dependencies"
        )


def _read_source(store, ref: Mapping[str, str]):
    """Read one exact current source revision and validate any context document.

    Returns ``(text, digest_hex, context_document, document_type)``. A source
    whose bytes decode as a discovery-context envelope is validated through
    :func:`~review_article_agent.discovery.context.read_context_document` (never
    silently accepted); a ``discovery_seed`` additionally requires its individual
    approval and keeps its provisional label.
    """
    content, revision = _read_current_revision(store, ref)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostedLeadError("source: the stored source is not valid UTF-8") from exc

    context_document = None
    document_type = None
    parsed: object = None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        parsed = None
    if isinstance(parsed, dict) and "document_type" in parsed:
        candidate = parsed.get("document_type")
        if not isinstance(candidate, str) or candidate not in discovery_context.DOCUMENT_TYPES:
            raise HostedUseDocumentError(
                "source: a document that declares a type is not a supported "
                "discovery-context document"
            )
        try:
            context_document = discovery_context.read_context_document(
                store, ref, require_current=True
            )
        except discovery_context.DiscoveryContextError as exc:
            raise HostedUseDocumentError(
                "source: a discovery-context document fails validation"
            ) from exc
        document_type = context_document["document_type"]
        if document_type == "discovery_seed":
            try:
                discovery_context.seed_context(store, [dict(ref)])
            except discovery_context.DiscoveryContextError as exc:
                raise HostedUseAuthorizationError(
                    "source: a discovery_seed is not individually approved"
                ) from exc

    digest_hex = revision.content_sha256
    if digest_hex.startswith("sha256:"):
        digest_hex = digest_hex[7:]
    return text, digest_hex, context_document, document_type


def _verify_hosted_rights(store, ref: Mapping[str, str]) -> None:
    """Fail closed unless a source's pinned rights permit the hosted transfer.

    Requires a valid, **current** (not superseded) rights record whose
    ``hosted_transfer`` **and** ``redistribution`` are ``allowed``, whose
    ``privacy.denial`` is ``False`` and ``confidentiality`` is not ``unknown``,
    and that carries an exact-subject/rights-revision ``authorization``. Rights
    eligibility is **not** authority; the out-of-band batch gate is.
    """
    repo = ArtifactRepository(store)
    revision = repo.get_revision(ref["artifact_id"], ref["revision_id"])
    if revision is None:
        raise HostedUseRightsError("source: the pinned source revision is missing")
    rights_ref = revision.rights_ref
    row = store.connection.execute(
        "SELECT canonical FROM rights_record WHERE project_id = ? AND rights_id = ? "
        "AND record_revision = ?",
        (store.project_id, rights_ref.rights_id, rights_ref.record_revision),
    ).fetchone()
    if row is None:
        raise HostedUseRightsError("source: the pinned rights record is missing")
    try:
        record = json.loads(bytes(row["canonical"]).decode("utf-8"))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise HostedUseRightsError("source: the pinned rights record is malformed") from exc
    try:
        validated = validate_rights_record(record, store.project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise HostedUseRightsError(
            "source: the pinned rights record is not a valid record"
        ) from exc
    if validated["derived_status"] != "valid":
        raise HostedUseRightsError("source: the pinned rights record is not valid")
    subject = validated["subject"]
    if (
        subject.get("kind") != "source_revision"
        or subject.get("artifact_id") != ref["artifact_id"]
        or subject.get("revision_id") != ref["revision_id"]
    ):
        raise HostedUseRightsError(
            "source: the pinned rights record does not bind this revision"
        )
    operations = validated["operations"]
    if operations.get("hosted_transfer") != "allowed":
        raise HostedUseRightsError(
            "source: the pinned rights do not permit a hosted transfer"
        )
    if operations.get("redistribution") != "allowed":
        raise HostedUseRightsError(
            "source: the pinned rights do not permit redistribution"
        )
    if operations.get("storage") != "allowed":
        raise HostedUseRightsError("source: the pinned rights do not permit storage")
    privacy = validated["privacy"]
    if privacy.get("denial") is not False:
        raise HostedUseRightsError("source: the pinned rights deny hosted use")
    if privacy.get("confidentiality") == "unknown":
        raise HostedUseRightsError(
            "source: the pinned rights have unknown confidentiality"
        )
    if validated["authorization"] is None:
        raise HostedUseRightsError(
            "source: the pinned rights carry no authorization binding"
        )
    newest = store.connection.execute(
        "SELECT MAX(record_revision) AS newest FROM rights_record "
        "WHERE project_id = ? AND rights_id = ?",
        (store.project_id, rights_ref.rights_id),
    ).fetchone()
    if newest is not None and newest["newest"] is not None and (
        rights_ref.record_revision < newest["newest"]
    ):
        raise HostedUseRightsError(
            "source: the pinned rights record has been superseded"
        )


def _assert_gate_satisfied(store, batch_artifact_id: str, purpose: str) -> None:
    approvals = HumanApprovalRepository(store)
    gate_id = hosted_use_gate_id(purpose)
    if not approvals.named_gate_satisfied(
        batch_artifact_id, gate_kind=HOSTED_USE_GATE_KIND, gate_id=gate_id
    ):
        raise HostedUseAuthorizationError(
            "batch: the exact hosted-use gate is not currently satisfied by an "
            "active, approved human approval"
        )


def _assert_token_active(registry, store, activation_token: str) -> None:
    if not isinstance(activation_token, str) or activation_token == "":
        raise HostedUseAuthorizationError("activation_token: a non-empty token is required")
    try:
        activation = registry.activation()
    except Exception as exc:  # noqa: BLE001 - a missing activation must fail closed
        raise HostedUseAuthorizationError(
            "activation: the execution activation could not be read"
        ) from exc
    token = activation.activation_token or ""
    if (
        activation.state != "active"
        or activation.active_project_id != store.project_id
        or not hmac.compare_digest(token, activation_token)
    ):
        raise HostedUseAuthorizationError(
            "activation: the project is not the active, non-draining execution holder "
            "for this token"
        )


# --------------------------------------------------------------------------- #
# Deterministic prompt + identities
# --------------------------------------------------------------------------- #
def _assemble_prompt(entries: list[tuple[dict, str]]) -> str:
    """Assemble the lead prompt deterministically from exact pinned bytes.

    Only fixed labels plus the exact pinned source text are used: there is no
    caller-supplied prompt text outside the approved manifest.
    """
    parts: list[str] = []
    for index, (ref, text) in enumerate(entries):
        parts.append(
            "[source {i}] artifact={a} revision={r}".format(
                i=index, a=ref["artifact_id"], r=ref["revision_id"]
            )
        )
        parts.append(text)
    return "\n".join(parts)


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
                "prompt_version": LEAD_PROMPT_VERSION,
                "endpoint": lead.ENDPOINT,
            }
        )
    )


def _reported_versions(input_digest: str, model_ref: str, request_sha256: str) -> dict:
    """A validated C02 §6.1 ``VersionSet`` for a direct lead dispatch."""
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
            "ref": {
                "version": LEAD_PROMPT_VERSION,
                "digest": request_sha256,
            },
        },
        "rubric": {"state": "not_applicable"},
        "parser": {"state": "not_applicable"},
        "adapter": {
            "state": "known",
            "ref": {
                "version": LEAD_ADAPTER_REVISION,
                "digest": _sha256_hex(canonical_json({"endpoint": lead.ENDPOINT})),
            },
        },
    }
    return validate_version_set(
        versions, consumed=("model", "prompt", "adapter")
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


def _outcome_from_dispatch(dispatch_record) -> str:
    state = dispatch_record.state
    if state == "completed":
        kind = dispatch_record.result_kind
        if kind == RESULT_KIND_PROPOSAL:
            return "completed"
        if kind == RESULT_KIND_REFUSED:
            return "refused"
        if kind == RESULT_KIND_PROTOCOL:
            return "protocol_error"
        return "completed"
    if state == "failed":
        return "failed"
    if state == "unknown":
        return "unknown"
    return state  # "reserved" / "submitted"


def _readback(store, request_id: str, *, result=None, posted: bool = False) -> dict:
    """Build the durable outcome mapping from stored rows (no API key, no text)."""
    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)
    task = runtime.get_task_by_request(request_id)
    dispatch_record = dispatch.get_dispatch_by_request(request_id)
    proposal = None
    if task is not None:
        proposals = runtime.proposals_for_task(task.task_id)
        proposal = proposals[0] if proposals else None

    if dispatch_record is None:
        # A request that never reserved anything (blocked before dispatch).
        return {
            "request_id": request_id,
            "status": "blocked",
            "outcome": None,
            "dispatch_id": None,
            "dispatch_state": None,
            "task_id": None if task is None else task.task_id,
            "proposal_id": None,
            "proposal_record_id": None,
            "cost_state": None,
            "cost_amount_usd": None,
            "reserve_usd": None,
            "posted": posted,
            "no_automatic_retry": True,
            "actual_model": None,
            "provider": None,
            "response_id": None,
            "usage": None,
            "versions": None,
        }

    mapping = {
        "request_id": request_id,
        "status": _outcome_from_dispatch(dispatch_record),
        "outcome": _outcome_from_dispatch(dispatch_record),
        "dispatch_id": dispatch_record.dispatch_id,
        "dispatch_state": dispatch_record.state,
        "task_id": dispatch_record.task_id,
        "proposal_id": None if proposal is None else proposal.proposal_id,
        "proposal_record_id": None if proposal is None else proposal.record_id,
        "cost_state": dispatch_record.cost_state,
        "cost_amount_usd": dispatch_record.cost_amount_usd,
        "reserve_usd": dispatch_record.reserve_usd,
        "posted": posted,
        "no_automatic_retry": True,
        "actual_model": None,
        "provider": None,
        "response_id": None,
        "usage": None,
        "versions": None,
    }
    if result is not None:
        mapping["actual_model"] = result.actual_model
        mapping["provider"] = result.provider
        mapping["response_id"] = result.response_id
        mapping["usage"] = _usage_mapping(result)
        mapping["versions"] = _reported_versions(
            _input_digest(
                store.project_id,
                [dict(ref) for ref in (task.input_refs if task is not None else [])],
            ),
            result.model,
            result.request_sha256,
        )
    return mapping


def get_lead_outcome(store, request_id: str) -> dict | None:
    """Read back a durable logical hosted lead request (read-only).

    Returns the same safe mapping :func:`execute_lead_proposal` returns, or
    ``None`` when nothing was ever recorded for ``request_id``. It never issues a
    call and never includes an API key or source text.
    """
    request_id = _require_identifier(request_id, "request_id")
    runtime = RuntimeRepository(store)
    if runtime.get_task_by_request(request_id) is None:
        return None
    return _readback(store, request_id)


# --------------------------------------------------------------------------- #
# Pre-flight (pure reads; no network, no write)
# --------------------------------------------------------------------------- #
def _preflight(store, *, batch_ref, request_id, api_key):
    """Validate inputs and derive the task, plan and exact identities.

    Performs only local reads. Returns a namespace-like dict of the derived
    values. Nothing is sent and nothing is written.
    """
    request_id = _require_identifier(request_id, "request_id")
    if not isinstance(api_key, str) or api_key == "":
        raise HostedLeadError("api_key: a non-empty credential is required")

    ref, revision, document = _load_batch(store, batch_ref)
    payload = document["payload"]
    purpose = payload["purpose"]
    config_map = payload["config"]
    source_refs = payload["source_refs"]

    # (1) The exact named out-of-band gate must be active for this batch.
    _assert_gate_satisfied(store, ref["artifact_id"], purpose)

    # (2) The batch revision must pin exactly its declared sources.
    _verify_batch_sources(revision, source_refs)

    # (3) Every source: current bytes, hosted-transfer rights, context valid.
    entries: list[tuple[dict, str]] = []
    input_refs: list[dict] = []
    for source in source_refs:
        _verify_hosted_rights(store, source)
        text, digest_hex, _ctx, _dtype = _read_source(store, source)
        entries.append((source, text))
        input_refs.append(
            {
                "project_id": store.project_id,
                "artifact_id": source["artifact_id"],
                "revision_id": source["revision_id"],
                "digest": digest_hex,
            }
        )

    config = LeadConfig.from_mapping(config_map)
    try:
        model = ModelRef(
            provider_id="openrouter", model_id=config.model_id, variant="direct"
        )
        role_config = RoleConfig(role=LEAD_ROLE, model=model, steps=1)
        role_set = RoleConfiguration(roles=(role_config,))
    except ReviewAgentError as exc:
        raise HostedUseDocumentError(
            "discovery_hosted_use.config: the configured model is not a usable "
            "direct-route model reference"
        ) from exc

    bounded = BoundedTask(
        project_id=store.project_id,
        role=LEAD_ROLE,
        target={
            "project_id": store.project_id,
            "artifact_id": ref["artifact_id"],
            "revision_id": ref["revision_id"],
        },
        input_refs=input_refs,
        scope=list(_TASK_SCOPE) + [purpose],
        prompt_version=LEAD_PROMPT_VERSION,
        size_limit=_TASK_SIZE_LIMIT,
        step_limit=1,
        role_config=role_config,
    )

    prompt = _assemble_prompt(entries)
    plan = lead.plan_lead(config, purpose=purpose, text=prompt)

    return {
        "request_id": request_id,
        "api_key": api_key,
        "batch_ref": ref,
        "batch_revision": revision,
        "document": document,
        "purpose": purpose,
        "config": config,
        "config_map": config_map,
        "role_set": role_set,
        "bounded": bounded,
        "plan": plan,
        "input_refs": input_refs,
        "input_digest": _input_digest(store.project_id, input_refs),
        "version_digest": _version_digest(purpose, config_map),
        "bound_evidence_digest": _sha256_hex(
            canonical_json(dict(plan.bound_evidence))
        ),
        "model_ref": model.ref,
    }


# --------------------------------------------------------------------------- #
# Result commit (one project transaction, under the registry write guard)
# --------------------------------------------------------------------------- #
def _result_envelope_digest(prepared: dict, result) -> str:
    """Canonical digest of a non-success outcome envelope (data only)."""
    return _sha256_hex(
        canonical_json(
            {
                "purpose": prepared["purpose"],
                "outcome": result.outcome,
                "cost_state": result.cost.state,
                "cost_amount_usd": result.cost.amount_usd,
                "actual_model": result.actual_model,
                "provider": result.provider,
                "response_id": result.response_id,
                "http_status": result.http_status,
                "request_sha256": result.request_sha256,
            }
        )
    )


def _commit_result(
    store, *, runtime, dispatch, prepared: dict, task_record, dispatch_id, fence, result
) -> dict:
    """Commit the proposal/result + cost settlement atomically, then read back.

    A successful completion records the non-authoritative proposal, accepts the
    dispatch's single local result, and settles a conservative cost in one
    project transaction. A refusal / protocol error records a non-proposal
    terminal result (never an acceptance or exclusion). A definitive HTTP
    rejection is recorded ``failed`` with ``not_incurred``. An ambiguous
    (unknown) outcome recovers the submission to ``unknown`` and retains the
    reserve — never a blind retry.
    """
    proposal_record = None
    outcome = result.outcome

    with store.transaction():
        if outcome == "completed":
            content = {
                "purpose": prepared["purpose"],
                "proposal_text": result.proposal_text,
            }
            proposal_record = runtime._record_proposal_in_transaction(
                task_record, {"content": content}, configuration=prepared["role_set"]
            )
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            dispatch._settle_cost_in_transaction(
                dispatch_id, cost_state=cost_state, amount_usd=amount
            )
            dispatch._accept_result_in_transaction(
                dispatch_id,
                fence=fence,
                result_kind=RESULT_KIND_PROPOSAL,
                result_ref=proposal_record.record_id,
                result_digest=proposal_record.stored_digest,
            )
        elif outcome == "failed":
            dispatch._settle_cost_in_transaction(
                dispatch_id,
                cost_state="not_incurred",
                allow_definitive_not_incurred=True,
            )
            dispatch._mark_failed_in_transaction(
                dispatch_id, definitive_rejection=True
            )
        elif outcome == "unknown":
            # Ambiguous: retain the reserve and the remote occupancy; never a
            # definitive failure and never a retry.
            dispatch._settle_cost_in_transaction(dispatch_id, cost_state="unknown")
            dispatch._recover_in_transaction(dispatch_id)
        else:  # refused / protocol_error
            cost_state = "known" if result.cost.state == "known" else "unknown"
            amount = result.cost.amount_usd if cost_state == "known" else None
            kind = (
                RESULT_KIND_REFUSED if outcome == "refused" else RESULT_KIND_PROTOCOL
            )
            dispatch._settle_cost_in_transaction(
                dispatch_id, cost_state=cost_state, amount_usd=amount
            )
            dispatch._accept_result_in_transaction(
                dispatch_id,
                fence=fence,
                result_kind=kind,
                result_ref=_scoped_id(
                    "lres", store.project_id, prepared["request_id"] + "\x00" + kind
                ),
                result_digest=_result_envelope_digest(prepared, result),
            )

    mapping = _readback(
        store, prepared["request_id"], result=result, posted=True
    )
    if proposal_record is not None:
        mapping["proposal_id"] = proposal_record.proposal_id
        mapping["proposal_record_id"] = proposal_record.record_id
    return mapping


# --------------------------------------------------------------------------- #
# Public runner
# --------------------------------------------------------------------------- #
def execute_lead_proposal(
    store,
    *,
    registry,
    activation_token: str,
    batch_ref,
    request_id: str,
    api_key: str,
    transport=None,
) -> dict:
    """Run **one** revision-scoped hosted lead proposal request (single-shot).

    The exact approved batch is re-read from the store, its named
    ``batch``/``discovery.hosted_use.<purpose>`` gate and every pinned source's
    hosted-transfer rights and context are revalidated, a one-step lead task is
    recorded, and a ``direct_call`` dispatch is reserved **before** the single
    POST. A completed request is returned from storage without a second POST; an
    interrupted one recovers to ``unknown`` and is never re-sent. The returned
    mapping carries only safe IDs/status — never the API key or source text.
    """
    request_id = _require_identifier(request_id, "request_id")
    if not isinstance(api_key, str) or api_key == "":
        raise HostedLeadError("api_key: a non-empty credential is required")
    # The exact active, non-draining execution holder is required before any local
    # write or read: a released/draining token cannot start (or resume) a run.
    _assert_token_active(registry, store, activation_token)

    prepared = _preflight(store, batch_ref=batch_ref, request_id=request_id, api_key=api_key)

    runtime = RuntimeRepository(store)
    dispatch = RuntimeDispatchRepository(store)

    # Idempotent task record (a divergent request under the same key conflicts).
    task_record = runtime.record_task(
        prepared["bounded"],
        request_id=prepared["request_id"],
        configuration=prepared["role_set"],
    )

    # Replay / reconnect: a request that already exists never POSTs again.
    existing = dispatch.get_dispatch_by_request(prepared["request_id"])
    if existing is not None:
        if existing.state == "submitted":
            # A restart-orphaned submission recovers to ``unknown``; never re-sent.
            with store.transaction():
                dispatch._recover_in_transaction(existing.dispatch_id)
        return _readback(store, prepared["request_id"], posted=False)

    # Reserve + claim the single send, under the short local write guard.
    with registry.execution_write_guard(activation_token, store.project_id):
        dispatch_record = dispatch.reserve_dispatch(
            task_record,
            request_id=prepared["request_id"],
            route="direct_call",
            stage=DISCOVERY_STAGE,
            input_digest=prepared["input_digest"],
            version_digest=prepared["version_digest"],
            reserve_usd=prepared["plan"].reserve_usd,
            bound_evidence_digest=prepared["bound_evidence_digest"],
        )
        dispatch_record = dispatch.mark_submitted(dispatch_record.dispatch_id)

    # The registry lock is released before any network I/O (no lock over network):
    # revalidate the exact gate and the active execution token immediately before
    # the POST.
    _assert_gate_satisfied(
        store, prepared["batch_ref"]["artifact_id"], prepared["purpose"]
    )
    _assert_token_active(registry, store, activation_token)

    result = lead.execute_lead(
        prepared["plan"], api_key=prepared["api_key"], transport=transport
    )

    with registry.execution_write_guard(activation_token, store.project_id):
        return _commit_result(
            store,
            runtime=runtime,
            dispatch=dispatch,
            prepared=prepared,
            task_record=task_record,
            dispatch_id=dispatch_record.dispatch_id,
            fence=dispatch_record.current_fence,
            result=result,
        )
