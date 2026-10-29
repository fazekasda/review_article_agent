"""Typed discovery-context documents: read-and-gate layer (Step 05, D1).

Authority
---------

* ``docs/implementation/05-discovery.md`` deliverable 1 (brief/interview
  artifacts, seed search and individual approvals, framing challenge, provisional
  scope map, rubric/search-plan approval) and acceptance criteria 1 and 2:
  "Seed abstracts remain provisional; unapproved seeds cannot silently become
  accepted project context." and "Broad searches cannot execute unapproved
  scope/query plans. A substantive change produces an impact/approval request."
* ``docs/specification.md`` "Confirmed scientific workflow" steps 1-4 (lead
  interview, exploratory seed search with **individual** seed approval and
  provisional abstract-derived context, framing challenge, and approval of one
  flexible protocol / provisional scope map / rubric / broad-search plan with
  recorded scope/date/language/source boundaries and impact review for
  substantive amendments).
* ``docs/contracts/01-identities.md`` §3 (exact ``RefRevision`` identity; an id,
  filename, hash, or title is never a reference) and §4 (artifact kind taxonomy).
* ``docs/contracts/02-gates-and-runtime.md`` §2 (a human approval binds an exact
  artifact revision + gate kind + gate id; a generic tool permission never
  satisfies it) and §3 (a new revision of a pinned input makes an approval stale).

What this module **is**
-----------------------

A small, strict **read-and-gate** layer over the context documents the discovery
stage already mints as generic, versioned artifact revisions:

* :func:`validate_context_document` — pure, static validation of the exact
  ``{schema_version, document_type, payload}`` envelope and the exact payload of
  each supported document type. It performs no I/O and invents no defaults.
* :func:`read_context_document` — read the exact immutable revision content from
  a verified project store, prove byte-integrity and canonical-envelope form, and
  re-validate the payload. It then runs the type's on-read cross-checks: a seed's
  real run/index existence, a rubric's ``rubric`` artifact kind, and — for a plan
  and an interview — that every referenced document exists, is pinned by the
  revision as an exact dependency, and reads as its **exact** expected document
  type (a plan's ``brief_ref``→``discovery_brief``, ``interview_ref``→
  ``discovery_interview``, ``framing_ref``→``discovery_framing``,
  ``scope_ref``→``discovery_scope``, ``rubric_ref``→``discovery_rubric``, and
  every seed→``discovery_seed``; an interview's ``brief_ref`` must be a typed
  ``discovery_brief`` that the interview pins). The expected type is checked
  before the referenced document's own cross-checks, so a self-referential or
  cyclic reference is refused, never followed. With ``require_current`` every
  live reference must also be the referenced artifact's current revision; a
  historical read (``require_current=False``) preserves each pinned revision.
* :func:`require_approved_context` — as :func:`read_context_document`, but only
  if the exact named gate instance is currently satisfied by a real human
  approval. For a ``discovery_plan`` this additionally requires the protocol gate
  ``protocol``/``discovery.plan``, every seed to pass
  :func:`seed_context` (exact current typed ``discovery_seed`` + real run/index +
  its individual ``artifact_revision`` gate — never a bare gate name over an
  arbitrary artifact), and the exact brief/interview association with
  ``interview.ready_to_advance`` true.
* :func:`seed_context` — the seed documents **only** when every exact current
  seed individually passes its ``artifact_revision`` gate
  ``discovery.seed.<artifact_id>``.

What this module **is not**
---------------------------

* It performs **no** network, provider, model, or paid call, and it **mints no
  rights or approvals**. It only reads and gates documents a trusted caller
  already imported; the generic :class:`~review_article_agent.storage.artifacts.ArtifactRepository`
  and :class:`~review_article_agent.storage.human_approvals.HumanApprovalRepository`
  own storage and approval authority.
* It exposes **no** in-band authority: there is deliberately no
  ``approved``/``actor``/``authority``/``authenticated`` payload field, and any
  extra envelope or payload field is refused.
* A validated/approved context document is still **untrusted data** for
  downstream science. In particular a **seed abstract stays
  ``abstract_context = 'provisional'`` and is never "primary support"**, with or
  without its individual seed approval (D1 AC1).

Every failure is the single static :class:`DiscoveryContextError`; a message
never echoes a document value, an identifier, or stored bytes.
"""

from __future__ import annotations

import json

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..runtime.owner_auth import APPROVAL_GATE_KINDS
from ..storage.artifacts import ArtifactRepository, sha256_digest
from ..storage.discovery import DiscoveryRepository, DiscoverySearchError
from ..storage.human_approvals import HumanApprovalRepository

__all__ = [
    "DISCOVERY_CONTEXT_SCHEMA_VERSION",
    "DOCUMENT_TYPES",
    "DISCOVERY_ENVELOPE_KEYS",
    "SEED_ABSTRACT_CONTEXT",
    "RUBRIC_UNCERTAIN_ACTION",
    "PLAN_QUERY_SOURCES",
    "COMPLEMENTARY_SOURCE_MODE",
    "ARTIFACT_REVISION_GATE_KIND",
    "PROTOCOL_GATE_KIND",
    "SEED_GATE_PREFIX",
    "PLAN_GATE_ID",
    "DiscoveryContextError",
    "validate_context_document",
    "read_context_document",
    "require_approved_context",
    "seed_context",
]

# --------------------------------------------------------------------------- #
# Vocabulary constants
# --------------------------------------------------------------------------- #
#: The only supported discovery-context document-envelope schema version.
DISCOVERY_CONTEXT_SCHEMA_VERSION = 1

#: The exact, closed set of discovery-context document types. A future stage may
#: extend this vocabulary through a **separate** module; this module stays
#: context-only.
DOCUMENT_TYPES = (
    "discovery_brief",
    "discovery_interview",
    "discovery_seed",
    "discovery_framing",
    "discovery_scope",
    "discovery_rubric",
    "discovery_plan",
)

#: Exact keys of the versioned document envelope (C01 generic document shape).
DISCOVERY_ENVELOPE_KEYS = ("schema_version", "document_type", "payload")

#: The one constant abstract-context state a seed may carry (D1 AC1).
SEED_ABSTRACT_CONTEXT = "provisional"

#: The one constant uncertain-case action a rubric may carry (no numeric rule).
RUBRIC_UNCERTAIN_ACTION = "retain"

#: The only two supported query APIs a plan may program directly.
PLAN_QUERY_SOURCES = ("pubmed", "europepmc")

#: The only ratified complementary-source mode (project-approved metadata import).
COMPLEMENTARY_SOURCE_MODE = "metadata_import"

#: The gate kind under which each seed revision is individually approved (C02 §2.1).
ARTIFACT_REVISION_GATE_KIND = "artifact_revision"

#: The gate kind under which a whole combined discovery plan is approved.
PROTOCOL_GATE_KIND = "protocol"

#: Gate-id prefix for one seed's named ``artifact_revision`` gate.
SEED_GATE_PREFIX = "discovery.seed."

#: The exact protocol gate id of the combined discovery plan.
PLAN_GATE_ID = "discovery.plan"

_REF_KEYS = ("project_id", "artifact_id", "revision_id")

# Exact payload field sets, one per document type (no extra field is permitted).
_BRIEF_KEYS = ("objective", "starting_hypothesis", "questions")
_INTERVIEW_KEYS = ("brief_ref", "turns", "ready_to_advance")
_TURN_KEYS = ("question", "answer")
_SEED_KEYS = ("run_id", "record_index", "abstract", "abstract_context", "limitations")
_FRAMING_KEYS = ("alternatives", "counterevidence_queries", "limitations")
_SCOPE_KEYS = (
    "scope",
    "date_boundary",
    "language_boundary",
    "source_boundary",
    "intended_synthesis",
    "terminology",
    "exclusions",
)
_RUBRIC_KEYS = ("eligibility_rules", "uncertain_action", "limitations")
_PLAN_KEYS = (
    "protocol",
    "brief_ref",
    "interview_ref",
    "seed_refs",
    "framing_ref",
    "scope_ref",
    "rubric_ref",
    "queries",
    "complementary_sources",
    "expansion",
    "impact_review",
)
_QUERY_KEYS = ("source", "query", "page_size", "max_pages")
_COMPLEMENTARY_KEYS = ("source", "mode", "rationale")
_EXPANSION_KEYS = ("max_hops", "max_sources", "max_requests", "page_size", "max_pages")
_IMPACT_KEYS = ("previous_plan", "affected", "rationale")

#: JSON parser hook that refuses non-portable constants (NaN/Infinity).
def _reject_constant(_value: str) -> object:
    raise DiscoveryContextError("document: content is not portable canonical JSON")


# --------------------------------------------------------------------------- #
# Error
# --------------------------------------------------------------------------- #
class DiscoveryContextError(ReviewAgentError):
    """A discovery-context document failed validation, reading, or gating.

    Every message is static and curated: it names the field or the operation but
    never echoes a document value, an identifier, or stored bytes.
    """


# --------------------------------------------------------------------------- #
# Strict primitive helpers
# --------------------------------------------------------------------------- #
def _fail(message: str) -> None:
    raise DiscoveryContextError(message)


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _require_mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{field}: must be an object")
    return value


def _require_exact_keys(obj: dict, allowed: tuple[str, ...], field: str) -> None:
    for key in obj:
        if not isinstance(key, str):
            _fail(f"{field}: object keys must be strings")
    if set(obj) != set(allowed):
        _fail(f"{field}: fields must be exactly {list(allowed)}")


def _require_str(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(f"{field}: must be a string")
    if _has_surrogate(value):
        _fail(f"{field}: must not contain an invalid Unicode surrogate")
    if not allow_empty and value.strip() == "":
        _fail(f"{field}: must be a non-empty string")
    return value


def _require_identifier(value: object, field: str) -> str:
    try:
        return validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise DiscoveryContextError(f"{field}: must be a valid opaque identifier") from exc


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field}: must be a boolean")
    return value


def _require_int(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field}: must be an integer")
    if value < minimum:
        _fail(f"{field}: must be >= {minimum}")
    return value


def _require_str_list(
    value: object,
    field: str,
    *,
    min_items: int = 0,
) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{field}: must be a list of strings")
    if len(value) < min_items:
        _fail(f"{field}: requires at least {min_items} item(s)")
    return [
        _require_str(item, f"{field}[{index}]") for index, item in enumerate(value)
    ]


def _require_ref(value: object, field: str) -> dict:
    """Structurally validate an exact ``RefRevision`` (no project compare yet)."""
    ref = _require_mapping(value, field)
    _require_exact_keys(ref, _REF_KEYS, field)
    return {
        "project_id": _require_identifier(ref["project_id"], f"{field}.project_id"),
        "artifact_id": _require_identifier(ref["artifact_id"], f"{field}.artifact_id"),
        "revision_id": _require_identifier(ref["revision_id"], f"{field}.revision_id"),
    }


def _require_ref_list(value: object, field: str) -> list[dict]:
    if not isinstance(value, list):
        _fail(f"{field}: must be a list of RefRevision objects")
    return [
        _require_ref(item, f"{field}[{index}]") for index, item in enumerate(value)
    ]


def _require_optional_ref(value: object, field: str) -> dict | None:
    if value is None:
        return None
    return _require_ref(value, field)


def _require_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field}: is not one of {list(allowed)}")
    return value


def _require_same_project(refs: list[dict], field: str) -> None:
    """Every ``RefRevision`` inside one document must agree on ``project_id``.

    The store's own project is compared on read; at static-validation time this
    internal consistency check refuses a document that mixes projects.
    """
    projects = {ref["project_id"] for ref in refs}
    if len(projects) > 1:
        _fail(f"{field}: all references in one document must share one project")


# --------------------------------------------------------------------------- #
# Static per-type payload validation
# --------------------------------------------------------------------------- #
def _validate_brief(payload: dict) -> dict:
    _require_exact_keys(payload, _BRIEF_KEYS, "discovery_brief.payload")
    return {
        "objective": _require_str(payload["objective"], "discovery_brief.objective"),
        "starting_hypothesis": _require_str(
            payload["starting_hypothesis"], "discovery_brief.starting_hypothesis"
        ),
        "questions": _require_str_list(payload["questions"], "discovery_brief.questions"),
    }


def _validate_interview(payload: dict) -> dict:
    _require_exact_keys(payload, _INTERVIEW_KEYS, "discovery_interview.payload")
    brief_ref = _require_ref(payload["brief_ref"], "discovery_interview.brief_ref")
    raw_turns = payload["turns"]
    if not isinstance(raw_turns, list):
        _fail("discovery_interview.turns: must be a list of turn objects")
    turns: list[dict] = []
    for index, raw in enumerate(raw_turns):
        field = f"discovery_interview.turns[{index}]"
        turn = _require_mapping(raw, field)
        _require_exact_keys(turn, _TURN_KEYS, field)
        turns.append(
            {
                "question": _require_str(turn["question"], f"{field}.question"),
                "answer": _require_str(turn["answer"], f"{field}.answer"),
            }
        )
    return {
        "brief_ref": brief_ref,
        "turns": turns,
        "ready_to_advance": _require_bool(
            payload["ready_to_advance"], "discovery_interview.ready_to_advance"
        ),
    }


def _validate_seed(payload: dict) -> dict:
    _require_exact_keys(payload, _SEED_KEYS, "discovery_seed.payload")
    abstract = payload["abstract"]
    if abstract is not None:
        abstract = _require_str(abstract, "discovery_seed.abstract")
    abstract_context = payload["abstract_context"]
    if abstract_context != SEED_ABSTRACT_CONTEXT:
        _fail(
            "discovery_seed.abstract_context: must be the constant "
            f"{SEED_ABSTRACT_CONTEXT!r}; abstract-derived context is provisional"
        )
    return {
        "run_id": _require_str(payload["run_id"], "discovery_seed.run_id"),
        "record_index": _require_int(
            payload["record_index"], "discovery_seed.record_index", minimum=0
        ),
        "abstract": abstract,
        "abstract_context": SEED_ABSTRACT_CONTEXT,
        "limitations": _require_str_list(
            payload["limitations"], "discovery_seed.limitations"
        ),
    }


def _validate_framing(payload: dict) -> dict:
    _require_exact_keys(payload, _FRAMING_KEYS, "discovery_framing.payload")
    return {
        "alternatives": _require_str_list(
            payload["alternatives"], "discovery_framing.alternatives", min_items=1
        ),
        "counterevidence_queries": _require_str_list(
            payload["counterevidence_queries"],
            "discovery_framing.counterevidence_queries",
            min_items=1,
        ),
        "limitations": _require_str_list(
            payload["limitations"], "discovery_framing.limitations"
        ),
    }


def _validate_scope(payload: dict) -> dict:
    _require_exact_keys(payload, _SCOPE_KEYS, "discovery_scope.payload")
    return {
        "scope": _require_str(payload["scope"], "discovery_scope.scope"),
        "date_boundary": _require_str(
            payload["date_boundary"], "discovery_scope.date_boundary"
        ),
        "language_boundary": _require_str(
            payload["language_boundary"], "discovery_scope.language_boundary"
        ),
        "source_boundary": _require_str_list(
            payload["source_boundary"], "discovery_scope.source_boundary"
        ),
        "intended_synthesis": _require_str(
            payload["intended_synthesis"], "discovery_scope.intended_synthesis"
        ),
        "terminology": _require_str_list(
            payload["terminology"], "discovery_scope.terminology"
        ),
        "exclusions": _require_str_list(
            payload["exclusions"], "discovery_scope.exclusions"
        ),
    }


def _validate_rubric(payload: dict) -> dict:
    _require_exact_keys(payload, _RUBRIC_KEYS, "discovery_rubric.payload")
    uncertain_action = payload["uncertain_action"]
    if uncertain_action != RUBRIC_UNCERTAIN_ACTION:
        _fail(
            "discovery_rubric.uncertain_action: must be the constant "
            f"{RUBRIC_UNCERTAIN_ACTION!r}"
        )
    return {
        "eligibility_rules": _require_str_list(
            payload["eligibility_rules"], "discovery_rubric.eligibility_rules"
        ),
        "uncertain_action": RUBRIC_UNCERTAIN_ACTION,
        "limitations": _require_str_list(
            payload["limitations"], "discovery_rubric.limitations"
        ),
    }


def _validate_queries(value: object) -> list[dict]:
    if not isinstance(value, list):
        _fail("discovery_plan.queries: must be a list of query objects")
    queries: list[dict] = []
    for index, raw in enumerate(value):
        field = f"discovery_plan.queries[{index}]"
        query = _require_mapping(raw, field)
        _require_exact_keys(query, _QUERY_KEYS, field)
        queries.append(
            {
                "source": _require_enum(
                    query["source"], PLAN_QUERY_SOURCES, f"{field}.source"
                ),
                "query": _require_str(query["query"], f"{field}.query"),
                "page_size": _require_int(
                    query["page_size"], f"{field}.page_size", minimum=1
                ),
                "max_pages": _require_int(
                    query["max_pages"], f"{field}.max_pages", minimum=1
                ),
            }
        )
    return queries


def _validate_complementary_sources(value: object) -> list[dict]:
    if not isinstance(value, list):
        _fail(
            "discovery_plan.complementary_sources: must be a list of source objects"
        )
    sources: list[dict] = []
    for index, raw in enumerate(value):
        field = f"discovery_plan.complementary_sources[{index}]"
        source = _require_mapping(raw, field)
        _require_exact_keys(source, _COMPLEMENTARY_KEYS, field)
        mode = source["mode"]
        if mode != COMPLEMENTARY_SOURCE_MODE:
            _fail(
                f"{field}.mode: must be the constant {COMPLEMENTARY_SOURCE_MODE!r}; "
                "only a project-approved metadata import is ratified"
            )
        sources.append(
            {
                "source": _require_str(source["source"], f"{field}.source"),
                "mode": COMPLEMENTARY_SOURCE_MODE,
                "rationale": _require_str(source["rationale"], f"{field}.rationale"),
            }
        )
    return sources


def _validate_expansion(value: object) -> dict:
    expansion = _require_mapping(value, "discovery_plan.expansion")
    _require_exact_keys(expansion, _EXPANSION_KEYS, "discovery_plan.expansion")
    return {
        "max_hops": _require_int(
            expansion["max_hops"], "discovery_plan.expansion.max_hops", minimum=0
        ),
        "max_sources": _require_int(
            expansion["max_sources"], "discovery_plan.expansion.max_sources", minimum=0
        ),
        "max_requests": _require_int(
            expansion["max_requests"], "discovery_plan.expansion.max_requests", minimum=0
        ),
        "page_size": _require_int(
            expansion["page_size"], "discovery_plan.expansion.page_size", minimum=1
        ),
        "max_pages": _require_int(
            expansion["max_pages"], "discovery_plan.expansion.max_pages", minimum=1
        ),
    }


def _validate_impact_review(value: object) -> dict:
    impact = _require_mapping(value, "discovery_plan.impact_review")
    _require_exact_keys(impact, _IMPACT_KEYS, "discovery_plan.impact_review")
    previous_plan = _require_optional_ref(
        impact["previous_plan"], "discovery_plan.impact_review.previous_plan"
    )
    affected = _require_str_list(
        impact["affected"], "discovery_plan.impact_review.affected"
    )
    rationale = _require_str(
        impact["rationale"],
        "discovery_plan.impact_review.rationale",
        allow_empty=previous_plan is None,
    )
    if previous_plan is not None:
        if not affected:
            _fail(
                "discovery_plan.impact_review.affected: an amendment requires a "
                "non-empty affected list"
            )
        if rationale.strip() == "":
            _fail(
                "discovery_plan.impact_review.rationale: an amendment requires a "
                "non-empty rationale"
            )
    return {
        "previous_plan": previous_plan,
        "affected": affected,
        "rationale": rationale,
    }


def _validate_plan(payload: dict) -> dict:
    _require_exact_keys(payload, _PLAN_KEYS, "discovery_plan.payload")
    brief_ref = _require_ref(payload["brief_ref"], "discovery_plan.brief_ref")
    interview_ref = _require_ref(payload["interview_ref"], "discovery_plan.interview_ref")
    framing_ref = _require_ref(payload["framing_ref"], "discovery_plan.framing_ref")
    scope_ref = _require_ref(payload["scope_ref"], "discovery_plan.scope_ref")
    rubric_ref = _require_ref(payload["rubric_ref"], "discovery_plan.rubric_ref")
    seed_refs = _require_ref_list(payload["seed_refs"], "discovery_plan.seed_refs")
    impact_review = _validate_impact_review(payload["impact_review"])

    # The rubric (appraisal criteria) is a distinct artifact from the scientific
    # scope map and the framing challenge; the kinds must not be conflated.
    if rubric_ref["artifact_id"] in (
        scope_ref["artifact_id"],
        framing_ref["artifact_id"],
    ):
        _fail(
            "discovery_plan.rubric_ref: the rubric must be distinct from the scope "
            "and framing artifacts"
        )

    refs = [brief_ref, interview_ref, framing_ref, scope_ref, rubric_ref, *seed_refs]
    if impact_review["previous_plan"] is not None:
        refs.append(impact_review["previous_plan"])
    _require_same_project(refs, "discovery_plan")

    return {
        "protocol": _require_str(payload["protocol"], "discovery_plan.protocol"),
        "brief_ref": brief_ref,
        "interview_ref": interview_ref,
        "seed_refs": seed_refs,
        "framing_ref": framing_ref,
        "scope_ref": scope_ref,
        "rubric_ref": rubric_ref,
        "queries": _validate_queries(payload["queries"]),
        "complementary_sources": _validate_complementary_sources(
            payload["complementary_sources"]
        ),
        "expansion": _validate_expansion(payload["expansion"]),
        "impact_review": impact_review,
    }


_VALIDATORS = {
    "discovery_brief": _validate_brief,
    "discovery_interview": _validate_interview,
    "discovery_seed": _validate_seed,
    "discovery_framing": _validate_framing,
    "discovery_scope": _validate_scope,
    "discovery_rubric": _validate_rubric,
    "discovery_plan": _validate_plan,
}


# --------------------------------------------------------------------------- #
# Public: static validation
# --------------------------------------------------------------------------- #
def validate_context_document(document: object) -> dict:
    """Validate one discovery-context document; return a plain deep copy.

    The envelope must be **exactly** ``{schema_version, document_type, payload}``
    with ``schema_version == 1``, a listed ``document_type``, and a payload whose
    fields are **exactly** the type's required set. Wrong types, a bool where an
    integer/string is required, a lone surrogate, a missing/extra field, an
    unrecognized enum literal, and an inverted amendment shape are all static
    :class:`DiscoveryContextError` failures. No I/O is performed and no value is
    defaulted.
    """
    envelope = _require_mapping(document, "document")
    _require_exact_keys(envelope, DISCOVERY_ENVELOPE_KEYS, "document")

    version = envelope["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("document.schema_version: must be an integer")
    if version != DISCOVERY_CONTEXT_SCHEMA_VERSION:
        _fail(
            "document.schema_version: must be the supported version "
            f"{DISCOVERY_CONTEXT_SCHEMA_VERSION}"
        )

    document_type = envelope["document_type"]
    if not isinstance(document_type, str) or document_type not in DOCUMENT_TYPES:
        _fail("document.document_type: is not a supported discovery-context type")

    payload = _require_mapping(envelope["payload"], "document.payload")
    validated_payload = _VALIDATORS[document_type](payload)
    return {
        "schema_version": DISCOVERY_CONTEXT_SCHEMA_VERSION,
        "document_type": document_type,
        "payload": validated_payload,
    }


# --------------------------------------------------------------------------- #
# Internal: store-bound normalization / cross-checks
# --------------------------------------------------------------------------- #
def _normalize_ref(ref: object, project_id: str, field: str) -> dict:
    try:
        return validate_ref(ref, project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise DiscoveryContextError(
            f"{field}: must be an exact same-project RefRevision"
        ) from exc


def _revision_exists(repo: ArtifactRepository, ref: dict) -> bool:
    return repo.get_revision(ref["artifact_id"], ref["revision_id"]) is not None


def _pinned_refs(revision) -> set[tuple[str, str]]:
    """The ``(artifact_id, revision_id)`` pairs a revision pins as dependencies."""
    return {(edge.artifact_id, edge.revision_id) for edge in revision.dependencies}


def _check_plan_references(
    store, revision, document: dict, repo: ArtifactRepository, *, require_current: bool
) -> None:
    """On read: validate every plan reference as its exact expected document type.

    Each direct reference must exist, be pinned by the plan revision as an exact
    dependency, and read as its **exact** expected document type
    (``brief_ref``→``discovery_brief``, ``interview_ref``→``discovery_interview``,
    ``framing_ref``→``discovery_framing``, ``scope_ref``→``discovery_scope``,
    ``rubric_ref``→``discovery_rubric``); every seed must read as a
    ``discovery_seed`` with a real run/index. The expected type is checked before
    the referenced document's own cross-checks, so a self-referential or cyclic
    plan reference is **refused, never followed**. With ``require_current`` every
    live reference must also be the referenced artifact's current revision; a
    historical read (``require_current=False``) preserves the pinned revision.
    """
    project_id = repo.project_id
    payload = document["payload"]
    direct = (
        ("brief_ref", "discovery_brief"),
        ("interview_ref", "discovery_interview"),
        ("framing_ref", "discovery_framing"),
        ("scope_ref", "discovery_scope"),
        ("rubric_ref", "discovery_rubric"),
    )
    resolved: dict[str, dict] = {}
    for field, _expected_type in direct:
        ref = _normalize_ref(payload[field], project_id, f"discovery_plan.{field}")
        if not _revision_exists(repo, ref):
            _fail(
                f"discovery_plan.{field}: the referenced revision does not exist in "
                "this project"
            )
        resolved[field] = ref
    seed_refs = [
        _normalize_ref(ref, project_id, f"discovery_plan.seed_refs[{index}]")
        for index, ref in enumerate(payload["seed_refs"])
    ]
    for index, ref in enumerate(seed_refs):
        if not _revision_exists(repo, ref):
            _fail(
                f"discovery_plan.seed_refs[{index}]: the referenced revision does "
                "not exist in this project"
            )

    pinned = _pinned_refs(revision)
    for field, _expected_type in direct:
        ref = resolved[field]
        if (ref["artifact_id"], ref["revision_id"]) not in pinned:
            _fail(
                f"discovery_plan.{field}: the plan revision does not pin this "
                "reference as an exact dependency"
            )
    for index, ref in enumerate(seed_refs):
        if (ref["artifact_id"], ref["revision_id"]) not in pinned:
            _fail(
                f"discovery_plan.seed_refs[{index}]: the plan revision does not pin "
                "this seed as an exact dependency"
            )

    # Typed validation of each referenced document (expected type before traverse).
    for field, expected_type in direct:
        read_context_document(
            store,
            resolved[field],
            expected_type=expected_type,
            require_current=require_current,
        )
    for index, ref in enumerate(seed_refs):
        read_context_document(
            store,
            ref,
            expected_type="discovery_seed",
            require_current=require_current,
        )

    # Substantive-amendment shape is enforced against the real parent revision.
    previous_plan = payload["impact_review"]["previous_plan"]
    parent_revision_id = revision.parent_revision_id
    if parent_revision_id is None:
        if previous_plan is not None:
            _fail(
                "discovery_plan.impact_review.previous_plan: an initial plan "
                "(no parent revision) must have a null previous_plan"
            )
        return
    if previous_plan is None:
        _fail(
            "discovery_plan.impact_review.previous_plan: an amendment (the "
            "revision has a parent) requires the prior plan reference"
        )
    expected = {
        "project_id": project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": parent_revision_id,
    }
    if _normalize_ref(
        previous_plan, project_id, "discovery_plan.impact_review.previous_plan"
    ) != expected:
        _fail(
            "discovery_plan.impact_review.previous_plan: must reference this "
            "artifact's exact parent revision"
        )
    if not payload["impact_review"]["affected"] or (
        payload["impact_review"]["rationale"].strip() == ""
    ):
        _fail(
            "discovery_plan.impact_review: a substantive amendment requires a "
            "non-empty affected list and rationale"
        )


def _check_seed_reference(store, document: dict, repo: ArtifactRepository) -> None:
    """On read: the seed's run/index must resolve in the discovery repository.

    Provider metadata is linked through the recorded search occurrence, never
    duplicated into the (caller-authoritative) seed payload, so the check is that
    the exact run exists and the exact record index is inside its result.
    """
    payload = document["payload"]
    try:
        search = DiscoveryRepository(store).get_search(payload["run_id"])
    except DiscoverySearchError as exc:
        raise DiscoveryContextError(
            "discovery_seed.run_id: the referenced discovery run is not readable"
        ) from exc
    if search is None:
        _fail("discovery_seed.run_id: the referenced discovery run does not exist")
    if payload["record_index"] >= len(search.result.records):
        _fail(
            "discovery_seed.record_index: is outside the referenced run's records"
        )


def read_context_document(
    store,
    ref: object,
    *,
    expected_type: str | None = None,
    require_current: bool = True,
) -> dict:
    """Read, byte-integrity-check, and validate one context document revision.

    The reference is an exact same-project ``RefRevision``. When
    ``require_current`` is true (the default) the revision must be the artifact's
    **current** revision, so a superseded context document is never read as
    current state. The stored bytes must match the recorded digest and byte size
    **and** be the canonical encoding of the validated envelope. The payload is
    then validated and the type's on-read cross-checks run: a seed's real
    run/index existence, a rubric's ``rubric`` artifact kind, and a plan's or
    interview's referenced documents validated as their exact expected document
    type and pinned as exact dependencies. With ``require_current`` every live
    reference must also be the referenced artifact's current revision. A
    malformed, stale, cross-project, forged, mistyped, or dangling document is a
    static :class:`DiscoveryContextError`.
    """
    if expected_type is not None and expected_type not in DOCUMENT_TYPES:
        _fail("expected_type: is not a supported discovery-context type")

    repo = ArtifactRepository(store)
    project_id = repo.project_id
    normalized = _normalize_ref(ref, project_id, "ref")

    if require_current:
        current = repo.current_revision(normalized["artifact_id"])
        if current is None or current.revision_id != normalized["revision_id"]:
            _fail(
                "ref: the requested context revision is not the artifact's current "
                "revision"
            )

    revision = repo.get_revision(normalized["artifact_id"], normalized["revision_id"])
    if revision is None:
        _fail("ref: the requested context revision does not exist in this project")

    content = repo.read_content(normalized["artifact_id"], normalized["revision_id"])
    if (
        sha256_digest(content) != revision.content_sha256
        or len(content) != revision.byte_size
    ):
        _fail("ref: the stored context revision fails byte-integrity validation")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoveryContextError(
            "ref: the stored context revision is not valid UTF-8"
        ) from exc
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except DiscoveryContextError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise DiscoveryContextError(
            "ref: the stored context revision is not a JSON object"
        ) from exc

    document = validate_context_document(parsed)
    if canonical_json(document) != content:
        _fail(
            "ref: the stored context revision is not the canonical document-envelope "
            "encoding"
        )
    if expected_type is not None and document["document_type"] != expected_type:
        _fail("ref: the document type does not match the expected type")

    document_type = document["document_type"]
    if document_type == "discovery_plan":
        _check_plan_references(
            store, revision, document, repo, require_current=require_current
        )
    elif document_type == "discovery_seed":
        _check_seed_reference(store, document, repo)
    elif document_type == "discovery_rubric":
        artifact = repo.get_artifact(normalized["artifact_id"])
        if artifact is None or artifact.kind != "rubric":
            _fail("ref: a discovery_rubric must be stored on a 'rubric' artifact")
    elif document_type == "discovery_interview":
        brief_ref = _normalize_ref(
            document["payload"]["brief_ref"], project_id, "discovery_interview.brief_ref"
        )
        if not _revision_exists(repo, brief_ref):
            _fail(
                "discovery_interview.brief_ref: the referenced brief revision does "
                "not exist in this project"
            )
        if (brief_ref["artifact_id"], brief_ref["revision_id"]) not in _pinned_refs(
            revision
        ):
            _fail(
                "discovery_interview.brief_ref: the interview revision does not pin "
                "this reference as an exact dependency"
            )
        read_context_document(
            store,
            brief_ref,
            expected_type="discovery_brief",
            require_current=require_current,
        )

    return document


# --------------------------------------------------------------------------- #
# Public: gating
# --------------------------------------------------------------------------- #
def _gate_satisfied(approvals, artifact_id: str, gate_kind: str, gate_id: str) -> bool:
    return approvals.named_gate_satisfied(
        artifact_id, gate_kind=gate_kind, gate_id=gate_id
    )


def _seed_gate_id(artifact_id: str) -> str:
    return SEED_GATE_PREFIX + artifact_id


def require_approved_context(
    store,
    ref: object,
    *,
    gate_kind: str,
    gate_id: str,
    expected_type: str | None = None,
) -> dict:
    """Read a context document only if its exact named gate is currently satisfied.

    The document is read (and cross-checked) as in :func:`read_context_document`,
    and the exact named gate instance ``gate_kind``/``gate_id`` on the document's
    artifact must be ``active`` and ``approved`` — a missing, rejected, stale,
    superseded, or invalidated approval fails closed.

    A ``discovery_plan`` is a **combined** approval: it requires the protocol gate
    ``protocol``/``discovery.plan``; it additionally requires every seed reference
    to pass :func:`seed_context` — an exact, current, typed ``discovery_seed``
    with a real run/index that individually satisfies its ``artifact_revision``
    gate ``discovery.seed.<artifact_id>`` (a bare gate name over a non-seed
    artifact is not enough); and it requires the exact brief/interview association
    (``interview.brief_ref`` equals ``plan.brief_ref``) with
    ``interview.ready_to_advance`` true.
    """
    if gate_kind not in APPROVAL_GATE_KINDS:
        _fail("gate_kind: is not an approved gate kind")
    _require_identifier(gate_id, "gate_id")

    document = read_context_document(
        store, ref, expected_type=expected_type, require_current=True
    )
    repo = ArtifactRepository(store)
    artifact_id = _normalize_ref(ref, repo.project_id, "ref")["artifact_id"]
    approvals = HumanApprovalRepository(store)

    if not _gate_satisfied(approvals, artifact_id, gate_kind, gate_id):
        _fail("ref: the exact named gate is not currently satisfied")

    if document["document_type"] == "discovery_plan":
        if gate_kind != PROTOCOL_GATE_KIND or gate_id != PLAN_GATE_ID:
            _fail(
                "ref: an approved discovery_plan requires the protocol gate "
                f"{PROTOCOL_GATE_KIND!r}/{PLAN_GATE_ID!r}"
            )
        _require_plan_approvals(store, document, approvals)

    return document


def _require_plan_approvals(store, document: dict, approvals) -> None:
    project_id = ArtifactRepository(store).project_id
    payload = document["payload"]

    # Every seed must be an exact, current, typed ``discovery_seed`` with a real
    # run/index that individually satisfies its ``artifact_revision`` gate
    # ``discovery.seed.<artifact_id>`` -- a bare gate name over an arbitrary
    # non-seed artifact is not enough (D1 AC1).
    seed_context(store, payload["seed_refs"])

    # The interview must exactly associate this brief and be ready to advance.
    interview_ref = _normalize_ref(
        payload["interview_ref"], project_id, "discovery_plan.interview_ref"
    )
    interview = read_context_document(
        store, interview_ref, expected_type="discovery_interview", require_current=True
    )
    if interview["payload"]["brief_ref"] != payload["brief_ref"]:
        _fail(
            "discovery_plan.interview_ref: the interview does not associate this "
            "exact brief"
        )
    if interview["payload"]["ready_to_advance"] is not True:
        _fail(
            "discovery_plan.interview_ref: the interview is not ready to advance"
        )


def seed_context(store, seed_refs: object) -> tuple[dict, ...]:
    """Return the validated seed documents **only if each is individually approved**.

    Each reference must be an exact, **current** ``discovery_seed`` whose named
    ``artifact_revision`` gate ``discovery.seed.<artifact_id>`` is satisfied.
    Either every seed passes or the whole call fails; no partial result is
    returned. Every returned seed keeps ``abstract_context = 'provisional'`` and
    is **never** primary support, with or without approval (D1 AC1). An empty
    ``seed_refs`` is valid (a user may explicitly advance with no seeds).
    """
    if not isinstance(seed_refs, (list, tuple)):
        _fail("seed_refs: must be a list of RefRevision objects")

    repo = ArtifactRepository(store)
    project_id = repo.project_id
    approvals = HumanApprovalRepository(store)

    documents: list[dict] = []
    for index, ref in enumerate(seed_refs):
        normalized = _normalize_ref(ref, project_id, f"seed_refs[{index}]")
        document = read_context_document(
            store, normalized, expected_type="discovery_seed", require_current=True
        )
        seed_artifact_id = normalized["artifact_id"]
        if not _gate_satisfied(
            approvals,
            seed_artifact_id,
            ARTIFACT_REVISION_GATE_KIND,
            _seed_gate_id(seed_artifact_id),
        ):
            _fail(f"seed_refs[{index}]: the seed is not individually approved")
        if document["payload"]["abstract_context"] != SEED_ABSTRACT_CONTEXT:
            # Unreachable through validation; kept as an explicit fail-closed guard.
            _fail(f"seed_refs[{index}]: a seed abstract stays provisional")
        documents.append(document)
    return tuple(documents)
