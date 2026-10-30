"""Per-role **hosted-use authorization document + named gate** (kickoff 16 §5).

Authority
---------

``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 (one real model port on
the direct OpenRouter route; "Reuse those gates [human hosted-use approval,
rights, budget reserve via ``discovery lead configure`` / ``gate confirm``]; do
not invent a parallel authorization path"; "Model per role is configurable").

This module builds **only** the authorization document and the named human-gate
layer for the generative roles (:data:`review_article_agent.runtime.generation.ROLES`).
A later task builds the runner that consumes it. The tested precedents it mirrors
and reuses are :mod:`review_article_agent.discovery.lead_runner`
(``validate_hosted_use_document``, ``hosted_use_gate_id``,
``_assert_gate_satisfied``, ``_validate_evidence``, ``_read_current_revision``)
and :mod:`review_article_agent.extraction.runner` (``propose_hosted_use``,
``_load_hosted_use``, ``require_run``).

What this module **is**
-----------------------

* :func:`validate_hosted_use_document` — pure, static validation of the
  ``generation_hosted_use`` envelope ``{schema_version, document_type, payload}``
  with the exact payload ``{role, config, budget_cap_usd, provider_terms,
  privacy_review, pricing_evidence, notes}``.
* :func:`hosted_use_gate_id` — the exact named gate id
  ``generation.hosted_use.<role>`` (``batch`` gate kind), confirmed through the
  **existing** ``gate prepare`` / ``gate confirm`` CLI with no CLI change.
* :func:`propose_hosted_use` — store the document as an immutable artifact
  revision with caller-supplied rights (the same pattern extraction uses).
* :func:`require_hosted_use` — read the exact current revision, validate it,
  require the role match, and require the named gate is satisfied for that exact
  revision (fail closed on a superseded revision, changed bytes, or a missing
  gate).
* :func:`role_models_distinct` — the spec's distinct-model rule: ``section_critic``
  must use a different concrete model (``model_id``) than ``section_writer``, and
  ``global_reviewer`` a different one than ``global_writer``.

What this module **is not**
---------------------------

* **Not an authorizer.** The document is a *description* of an authorization; the
  out-of-band, owner-authenticated ``batch``/``generation.hosted_use.<role>`` gate
  recorded through :class:`~review_article_agent.storage.human_approvals.HumanApprovalRepository`
  is the authority. No field of the document can create, widen, or substitute for
  that gate.
* Not the runner, the store writer beyond :func:`propose_hosted_use`, or the
  worker/CLI. It never dispatches a call.
* Not a legal or scientific verifier: provider-term, pricing, and privacy-review
  evidence references are only checked to be present, non-empty, and time-valid
  (exactly as the lead/extraction documents do); they are human-reviewed
  evidence, never a machine determination.

Every failure is a static, value-free :class:`GenerationHostedUseError`; no
message ever echoes a secret, a stored byte, or caller text.
"""

from __future__ import annotations

from typing import Mapping

from ..discovery import lead_runner as _lead_runner
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_ref,
)
from ..parsing.repository import _NestedStore
from ..runtime import generation as runtime_generation
from ..runtime import lead as runtime_lead
from ..runtime.lead import LeadConfig, LeadError
from ..storage.artifacts import ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository

__all__ = [
    "SCHEMA_VERSION",
    "HOSTED_USE_DOCUMENT_TYPE",
    "HOSTED_USE_GATE_KIND",
    "HOSTED_USE_GATE_PREFIX",
    "GenerationHostedUseError",
    "GenerationHostedUseDocumentError",
    "GenerationHostedUseAuthorizationError",
    "GenerationRoleModelError",
    "validate_hosted_use_document",
    "hosted_use_gate_id",
    "propose_hosted_use",
    "require_hosted_use",
    "role_models_distinct",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The only supported hosted-use document-envelope schema version.
SCHEMA_VERSION = 1

#: The one document type this module accepts as a generation hosted-use document.
HOSTED_USE_DOCUMENT_TYPE = "generation_hosted_use"

#: The gate kind and gate-id prefix of the out-of-band hosted-use authorization.
#: The kind is the shared, already-approved C02 ``batch`` capability; only the id
#: names the exact per-role generation gate instance.
HOSTED_USE_GATE_KIND = "batch"
HOSTED_USE_GATE_PREFIX = "generation.hosted_use."

#: Bounded size of the optional ``notes`` string.
MAX_NOTES_LEN = 2000

#: The exact envelope and payload keys (no other key is permitted).
_ENVELOPE_KEYS = ("schema_version", "document_type", "payload")
_PAYLOAD_KEYS = (
    "role",
    "config",
    "budget_cap_usd",
    "provider_terms",
    "privacy_review",
    "pricing_evidence",
    "notes",
)

#: The spec's distinct-concrete-model pairs ("section critic (different concrete
#: model, separate context)"; a global reviewer must likewise differ from the
#: global writer).
_DISTINCT_MODEL_PAIRS = (
    ("section_writer", "section_critic"),
    ("global_writer", "global_reviewer"),
)

# Reused, tested static helpers from the lead runner (never duplicated).
_require_mapping = _lead_runner._require_mapping
_require_exact_keys = _lead_runner._require_exact_keys
_validate_evidence = _lead_runner._validate_evidence
_read_current_revision = _lead_runner._read_current_revision
_parse_json_object = _lead_runner._parse_json_object
_has_surrogate = _lead_runner._has_surrogate


# --------------------------------------------------------------------------- #
# Errors (static messages; never echo a secret or stored bytes)
# --------------------------------------------------------------------------- #
class GenerationHostedUseError(ReviewAgentError):
    """A generation hosted-use document is malformed, mismatched, or unauthorized."""


class GenerationHostedUseDocumentError(GenerationHostedUseError):
    """A generation hosted-use document is malformed or not canonical."""


class GenerationHostedUseAuthorizationError(GenerationHostedUseError):
    """The exact out-of-band generation hosted-use gate is not active."""


class GenerationRoleModelError(GenerationHostedUseError):
    """A required per-role distinct-model rule is violated."""


# --------------------------------------------------------------------------- #
# Static helpers
# --------------------------------------------------------------------------- #
def _require_role(role: object) -> str:
    if not isinstance(role, str) or role not in runtime_generation.ROLES:
        raise GenerationHostedUseError("role: is not a recognized generation role")
    return role


def _require_same_project_ref(store, value: object, field: str) -> dict:
    try:
        return validate_ref(value, store.project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise GenerationHostedUseDocumentError(
            f"{field}: must be an exact same-project RefRevision"
        ) from exc


def _require_budget_cap_usd(value: object) -> str:
    """Validate an exact decimal string ``> 0``; return its canonical form.

    A non-string (``bool``/``int``/``float``/``Decimal``), a non-decimal string,
    a non-finite value, or a value ``<= 0`` is refused. The message never echoes
    the offending value.
    """
    field = "generation_hosted_use.budget_cap_usd"
    if not isinstance(value, str):
        raise GenerationHostedUseDocumentError(
            f"{field}: must be an exact decimal string"
        )
    try:
        amount = runtime_lead._bounded_decimal(value, field, strictly_positive=True)
    except LeadError as exc:
        raise GenerationHostedUseDocumentError(
            f"{field}: must be an exact decimal string greater than zero"
        ) from exc
    return runtime_lead._exact_decimal_string(amount)


def _require_notes(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GenerationHostedUseDocumentError(
            "generation_hosted_use.notes: must be a string or null"
        )
    if len(value) > MAX_NOTES_LEN:
        raise GenerationHostedUseDocumentError(
            "generation_hosted_use.notes: is too long"
        )
    if _has_surrogate(value):
        raise GenerationHostedUseDocumentError(
            "generation_hosted_use.notes: must not contain an invalid surrogate"
        )
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise GenerationHostedUseDocumentError(
                "generation_hosted_use.notes: must not contain control characters"
            )
    return value


# --------------------------------------------------------------------------- #
# Hosted-use document (this module's only owned document type)
# --------------------------------------------------------------------------- #
def validate_hosted_use_document(document: object) -> dict:
    """Validate one generation hosted-use document; return a plain copy.

    The envelope must be **exactly** ``{schema_version, document_type, payload}``
    with ``schema_version == 1`` and ``document_type == "generation_hosted_use"``.
    The payload must be **exactly** ``{role, config, budget_cap_usd,
    provider_terms, privacy_review, pricing_evidence, notes}``: ``role`` is one of
    :data:`review_article_agent.runtime.generation.ROLES`, ``config`` is a strict
    :class:`~review_article_agent.runtime.lead.LeadConfig` mapping (the concrete
    model for this role; no default), ``budget_cap_usd`` is an exact decimal
    string ``> 0`` (the maximum cumulative reserve the runner may hold for this
    role under this authorization), each evidence field is exactly
    ``{ref, verified_at}`` with a non-empty reference and an RFC 3339 UTC
    ``verified_at``, and ``notes`` is an optional bounded string or ``null``.

    This performs no I/O, invents no default, and grants **nothing**: the document
    is a *description* of an authorization, never authority.
    """
    try:
        return _validate_hosted_inner(document)
    except GenerationHostedUseError:
        raise
    except _lead_runner.HostedUseDocumentError as exc:
        raise GenerationHostedUseDocumentError(str(exc)) from exc


def _validate_hosted_inner(document: object) -> dict:
    envelope = _require_mapping(document, "document")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "document")

    version = envelope["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise GenerationHostedUseDocumentError(
            "document.schema_version: must be an integer"
        )
    if version != SCHEMA_VERSION:
        raise GenerationHostedUseDocumentError(
            f"document.schema_version: must be the supported version {SCHEMA_VERSION}"
        )
    if envelope["document_type"] != HOSTED_USE_DOCUMENT_TYPE:
        raise GenerationHostedUseDocumentError(
            "document.document_type: is not the generation hosted-use document type"
        )

    payload = _require_mapping(envelope["payload"], "document.payload")
    _require_exact_keys(payload, _PAYLOAD_KEYS, "generation_hosted_use.payload")

    role = payload["role"]
    if not isinstance(role, str) or role not in runtime_generation.ROLES:
        raise GenerationHostedUseDocumentError(
            "generation_hosted_use.role: is not a recognized generation role"
        )

    try:
        config = LeadConfig.from_mapping(payload["config"])
    except (LeadError, RecordValidationError, TypeError, ValueError) as exc:
        raise GenerationHostedUseDocumentError(
            "generation_hosted_use.config: is not a valid hosted-use configuration"
        ) from exc

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": HOSTED_USE_DOCUMENT_TYPE,
        "payload": {
            "role": role,
            "config": config.to_mapping(),
            "budget_cap_usd": _require_budget_cap_usd(payload["budget_cap_usd"]),
            "provider_terms": _validate_evidence(
                payload["provider_terms"], "generation_hosted_use.provider_terms"
            ),
            "privacy_review": _validate_evidence(
                payload["privacy_review"], "generation_hosted_use.privacy_review"
            ),
            "pricing_evidence": _validate_evidence(
                payload["pricing_evidence"], "generation_hosted_use.pricing_evidence"
            ),
            "notes": _require_notes(payload["notes"]),
        },
    }


def hosted_use_gate_id(role: str) -> str:
    """The exact named gate id of a generation role (``batch`` gate kind)."""
    return HOSTED_USE_GATE_PREFIX + _require_role(role)


# --------------------------------------------------------------------------- #
# Persistence (immutable artifact revision; same pattern as extraction)
# --------------------------------------------------------------------------- #
def propose_hosted_use(
    store,
    document: object,
    *,
    artifact_id: str,
    revision_id: str,
    rights: object,
) -> dict:
    """Validate and store a generation hosted-use document as an immutable revision.

    Pure validation first (nothing is written on a malformed document); then the
    validated envelope is imported as one immutable artifact revision with
    **caller-supplied** rights (rights eligibility is not authority — the
    out-of-band gate is). Returns the exact same-project ``RefRevision`` of the
    new revision.
    """
    validated = validate_hosted_use_document(document)
    nested = _NestedStore(store)
    artifacts = ArtifactRepository(nested)
    with nested.transaction():
        artifacts.create_artifact(artifact_id, kind="report", created_by="user")
        revision = artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=rights,
            content=validated,
            origin_locator="generation:local",
            acquired_by="user",
        )
    return {
        "project_id": store.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
    }


# --------------------------------------------------------------------------- #
# Reads (canonical, byte-checked, current) + the named gate
# --------------------------------------------------------------------------- #
def _load_hosted_use(store, ref: object):
    try:
        normalized = _require_same_project_ref(store, ref, "ref")
        content, revision = _read_current_revision(store, normalized)
        document = validate_hosted_use_document(
            _parse_json_object(content, "hosted-use")
        )
    except GenerationHostedUseError:
        raise
    except _lead_runner.HostedLeadError as exc:
        raise GenerationHostedUseDocumentError(str(exc)) from exc
    if canonical_json(document) != content:
        raise GenerationHostedUseDocumentError(
            "hosted-use: the stored revision is not the canonical document encoding"
        )
    return normalized, revision, document


def _assert_gate_satisfied(store, artifact_id: str, role: str) -> None:
    if not HumanApprovalRepository(store).named_gate_satisfied(
        artifact_id, gate_kind=HOSTED_USE_GATE_KIND, gate_id=hosted_use_gate_id(role)
    ):
        raise GenerationHostedUseAuthorizationError(
            "hosted-use: the exact generation hosted-use gate is not currently "
            "satisfied by an active, approved human approval"
        )


def require_hosted_use(store, ref: object, *, role: str) -> dict:
    """Return the validated document of the exact current, gate-approved revision.

    Reads the **exact current** revision (byte-integrity checked), validates the
    canonical document, requires the document's ``role`` to match the requested
    ``role``, and requires the named ``batch``/``generation.hosted_use.<role>``
    gate to be satisfied for that exact revision. A superseded revision, changed
    bytes, a role mismatch, or a missing gate fails closed with a typed static
    error.
    """
    role = _require_role(role)
    normalized, _revision, document = _load_hosted_use(store, ref)
    if document["payload"]["role"] != role:
        raise GenerationHostedUseDocumentError(
            "hosted-use: the document role does not match the requested role"
        )
    _assert_gate_satisfied(store, normalized["artifact_id"], role)
    return document


def role_models_distinct(store, refs: Mapping[str, object]) -> dict:
    """Enforce the spec's distinct-concrete-model rule across role documents.

    ``refs`` maps each role to the exact current hosted-use document revision for
    that role. Every referenced document is read, validated, and required to carry
    the role it is keyed by. Then ``section_critic`` must use a different concrete
    ``model_id`` than ``section_writer``, and ``global_reviewer`` a different one
    than ``global_writer``; an equal pair raises :class:`GenerationRoleModelError`.
    Returns the ``{role: model_id}`` mapping it validated.
    """
    if not isinstance(refs, Mapping):
        raise GenerationHostedUseError("refs: must be a mapping of role to reference")
    models: dict[str, str] = {}
    for role, ref in refs.items():
        normalized_role = _require_role(role)
        _normalized, _revision, document = _load_hosted_use(store, ref)
        if document["payload"]["role"] != normalized_role:
            raise GenerationHostedUseDocumentError(
                "hosted-use: the document role does not match the requested role"
            )
        models[normalized_role] = document["payload"]["config"]["model_id"]
    for left, right in _DISTINCT_MODEL_PAIRS:
        if left in models and right in models and models[left] == models[right]:
            raise GenerationRoleModelError(
                "generation: the roles must use different concrete models "
                f"({left} vs {right})"
            )
    return models
