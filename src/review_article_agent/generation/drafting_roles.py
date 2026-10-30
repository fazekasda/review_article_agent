"""Model-backed **section blueprint planner** and **section writer (draft)**.

Authority
---------

``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("One real model port":
every model-backed role runs through one small adapter; reuse the existing
human hosted-use approval, rights, and budget gates; "Model per role is
configurable"), and the Step 11 drafting contract
(``docs/implementation/11-drafting.md``): approve sources and paragraph
blueprints before drafting, and draft only from the approved blueprint and the
approved drafting package (summaries plus linked original evidence), citing only
verified-corpus records.

What this module is
-------------------

Two thin, model-backed role drivers over the application-boundary runner
(:func:`review_article_agent.generation.runner.execute_generation_task`):

* :func:`propose_blueprint` — the ``blueprint`` generation role plans one
  section blueprint for the exact current per-section workspace and its
  reconstructible approved drafting package.
* :func:`propose_draft` — the ``section_writer`` generation role drafts one
  section from the exact current *approved* blueprint and package.

Both:

* run through the one model port (the direct route authorized by the per-role
  ``generation_hosted_use`` document and its ``generation.hosted_use.<role>``
  gate), with a fixed versioned prompt (``PROMPT_VERSION`` recorded on the
  durable request) and fixed instructions derived from
  :mod:`review_article_agent.drafting.schema`;
* build their material from durable state only (the workspace, the approved
  package, or the approved blueprint) and return ``(material, input_refs,
  source_refs)`` — ``source_refs`` being the original sources behind the
  package's evidence, so the runner's hosted-transfer rights check is applied to
  them;
* keep the model output a **proposal**: it is validated through the existing
  :func:`~review_article_agent.drafting.schema.validate_blueprint` /
  :func:`~review_article_agent.drafting.schema.validate_draft` and stored through
  the existing :class:`~review_article_agent.drafting.workflow.DraftingWorkflowRepository`
  (a blueprint over the current workspace; a draft only over an already-approved
  blueprint).  Nothing is ever approved, and no invented citation is possible:
  the writer may cite only the package's ``citations`` keys and evidence
  ``finding_key``\\ s, which the schema enforces.

Return statuses (never an exception for a model/schema outcome):

* ``{"status": "model_not_completed", "outcome": ..., "generation": ...}`` — the
  runner did not complete; **nothing is written**.
* ``{"status": "invalid_proposal", "reason": "<static code>", "generation": ...}``
  — the completed proposal failed the existing schema validator; **nothing is
  written**.
* ``{"status": "proposed", "ref": ..., "generation": ...}`` — the proposal was
  stored as an inert revision (never approved).

Not in scope: approval, dispatch policy, budget/gate logic (owned by the runner),
critique/repair/global stages, and any new schema or storage.  No network call is
made by this module; the runner performs the single POST.
"""

from __future__ import annotations

from ..domain.records import canonical_json, validate_ref
from ..drafting.package import DraftingPackageRepository
from ..drafting.schema import DraftingError, validate_blueprint, validate_draft
from ..drafting.workflow import DraftingWorkflowRepository
from ..drafting.workspace import SectionWorkspaceRepository
from .runner import execute_generation_task

__all__ = [
    "BLUEPRINT_INSTRUCTIONS",
    "BLUEPRINT_PROMPT_VERSION",
    "BLUEPRINT_ROLE",
    "INVALID_PROPOSAL_REASON",
    "SECTION_WRITER_INSTRUCTIONS",
    "SECTION_WRITER_PROMPT_VERSION",
    "SECTION_WRITER_ROLE",
    "blueprint_material",
    "draft_material",
    "propose_blueprint",
    "propose_draft",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The exact generation roles (:data:`review_article_agent.runtime.generation.ROLES`).
BLUEPRINT_ROLE = "blueprint"
SECTION_WRITER_ROLE = "section_writer"

#: The versioned prompt identifiers recorded on the durable request.
BLUEPRINT_PROMPT_VERSION = "drafting.blueprint.v1"
SECTION_WRITER_PROMPT_VERSION = "drafting.section_writer.v1"

#: The single static reason code returned for a schema-invalid proposal.
INVALID_PROPOSAL_REASON = "schema_invalid"

#: The package's global/evidence dependency ref keys (the exact drafting-package
#: global refs), used to pin the model input.
_PACKAGE_REF_KEYS = (
    "outline_ref",
    "framework_ref",
    "synthesis_ref",
    "audit_ref",
    "acquisition_ref",
    "freeze_ref",
    "corpus_ref",
)

# --------------------------------------------------------------------------- #
# Fixed output contracts + rules (derived from drafting/schema.py, not guessed)
# --------------------------------------------------------------------------- #
#: The exact blueprint output contract (see ``validate_blueprint``).
BLUEPRINT_OUTPUT_CONTRACT = (
    'Return exactly ONE JSON object of this shape and nothing else:\n'
    '{\n'
    '  "schema_version": 1,\n'
    '  "section_id": "<the exact package section_id>",\n'
    '  "source_suggestions": ["<authorized finding_key>", "..."],   // optional\n'
    '  "paragraphs": [\n'
    '    {\n'
    '      "paragraph_id": "<unique id>",\n'
    '      "source_suggestions": ["<authorized finding_key>", "..."],   // optional\n'
    '      "intents": [\n'
    '        {\n'
    '          "intent_id": "<unique id>",\n'
    '          "intent": "<one sentence stating the intent>",\n'
    '          "claims": [\n'
    '            {\n'
    '              "claim_id": "<unique id>",\n'
    '              "text": "<one atomic assertion>",\n'
    '              "finding_keys": ["<authorized finding_key>", "..."],\n'
    '              "attribution": "direct_observation | author_interpretation | '
    'secondary_account | mixed | unclear | proposed_synthesis",\n'
    '              "classification": "reported_evidence | new_interpretation | '
    'hypothesis | proposed_synthesis",\n'
    '              "applicability": {\n'
    '                "species": {"state": "known|missing|unknown|not_applicable", "value": "<string or null>"},\n'
    '                "population": {"state": "...", "value": "..."},\n'
    '                "assay": {"state": "...", "value": "..."},\n'
    '                "outcome": {"state": "...", "value": "..."},\n'
    '                "conditions": {"state": "...", "value": "..."}\n'
    '              },\n'
    '              "causal_limitations": {"state": "known|missing|unknown|not_applicable", "value": "<string or null>"}\n'
    '            }\n'
    '          ]\n'
    '        }\n'
    '      ]\n'
    '    }\n'
    '  ]\n'
    '}\n'
)

#: The exact draft output contract (see ``validate_draft``).
SECTION_WRITER_OUTPUT_CONTRACT = (
    'Return exactly ONE JSON object of this shape and nothing else:\n'
    '{\n'
    '  "schema_version": 1,\n'
    '  "section_id": "<the exact blueprint section_id>",\n'
    '  "paragraphs": [\n'
    '    {\n'
    '      "paragraph_id": "<the exact blueprint paragraph_id>",\n'
    '      "sentences": [\n'
    '        {\n'
    '          "intent_id": "<the exact blueprint intent_id>",\n'
    '          "claim_ids": ["<the blueprint intent\'s claim ids, in order>"],\n'
    '          "text": "<EXACTLY the approved claim texts joined by a single space>"\n'
    '        }\n'
    '      ]\n'
    '    }\n'
    '  ]\n'
    '}\n'
)

#: The shared rules: authorized keys only, honest uncertainty, no invented claims.
_COMMON_RULES = (
    "- Return ONE JSON object only: no prose, no markdown fences, no extra keys.\n"
    "- Cite ONLY the authorized finding keys and citation keys supplied in the "
    "material; NEVER invent a finding, citation, study, author, journal, year, or "
    "DOI, and never emit a bibliography/references/citations block.\n"
    "- Every claim must be atomic and grounded in one or more supplied finding "
    "keys; never add a claim beyond the supplied evidence.\n"
    "- Preserve the source attribution and context exactly; mark uncertainty "
    "honestly and never present a secondary account as primary support.\n"
    "- A novel author proposal must be labelled proposed_synthesis (both "
    "attribution and classification).\n"
    "- Never emit status/approved/final/authorizes_export or override the "
    "supplied framework/synthesis/outline context.\n"
)

#: An illustrative-only blueprint example (fictional; never real evidence).
_BLUEPRINT_EXAMPLE = (
    '{\n'
    '  "schema_version": 1,\n'
    '  "section_id": "sec-example",\n'
    '  "source_suggestions": ["A10:F1"],\n'
    '  "paragraphs": [\n'
    '    {"paragraph_id": "P1", "intents": [\n'
    '      {"intent_id": "I1", "intent": "State the reported effect with its limits.",\n'
    '       "claims": [\n'
    '         {"claim_id": "K1", "text": "Compound Z reduced tumor volume in a mouse model.",\n'
    '          "finding_keys": ["A10:F1"], "attribution": "direct_observation",\n'
    '          "classification": "reported_evidence",\n'
    '          "applicability": {\n'
    '            "species": {"state": "known", "value": "mouse"},\n'
    '            "population": {"state": "known", "value": "n=8 mice"},\n'
    '            "assay": {"state": "known", "value": "tumor volume"},\n'
    '            "outcome": {"state": "known", "value": "tumor volume"},\n'
    '            "conditions": {"state": "known", "value": "vehicle comparison"}},\n'
    '          "causal_limitations": {"state": "known", "value": "association only; no mechanism established"}}\n'
    '       ]}\n'
    '    ]}\n'
    '  ]\n'
    '}\n'
)

#: An illustrative-only draft example (fictional; never real evidence).
_DRAFT_EXAMPLE = (
    '{\n'
    '  "schema_version": 1,\n'
    '  "section_id": "sec-example",\n'
    '  "paragraphs": [\n'
    '    {"paragraph_id": "P1", "sentences": [\n'
    '      {"intent_id": "I1", "claim_ids": ["K1"],\n'
    '       "text": "Compound Z reduced tumor volume in a mouse model."}\n'
    '    ]}\n'
    '  ]\n'
    '}\n'
)

#: The fixed, versioned planner instructions.
BLUEPRINT_INSTRUCTIONS = (
    "You are the section-blueprint planner for one approved outline section. "
    "Propose a section blueprint (paragraph/intent/claim plan) grounded ONLY in "
    "the supplied authorized evidence.\n\n"
    "OUTPUT CONTRACT (exact):\n"
    + BLUEPRINT_OUTPUT_CONTRACT
    + "\nRULES:\n"
    + _COMMON_RULES
    + "\nILLUSTRATIVE EXAMPLE (illustrative only; fictional; do not copy its "
    "values -- use only the supplied authorized keys):\n"
    + _BLUEPRINT_EXAMPLE
)

#: The fixed, versioned writer instructions.
SECTION_WRITER_INSTRUCTIONS = (
    "You are the section writer for one approved section blueprint. Draft the "
    "section from the approved blueprint and its approved evidence; the draft "
    "must cover exactly the blueprint paragraphs and intents.\n\n"
    "OUTPUT CONTRACT (exact):\n"
    + SECTION_WRITER_OUTPUT_CONTRACT
    + "\nRULES:\n"
    + _COMMON_RULES
    + "\nILLUSTRATIVE EXAMPLE (illustrative only; fictional; do not copy its "
    "values -- use only the supplied approved blueprint):\n"
    + _DRAFT_EXAMPLE
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _dedupe_refs(store, refs):
    """Return the exact, deterministic, deduplicated same-project refs."""
    unique = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        normalized = validate_ref(ref, store.project_id)
        unique[canonical_json(normalized)] = normalized
    return [unique[key] for key in sorted(unique)]


def _package_refs(package):
    """Return the package's own global dependency refs."""
    return [package[key] for key in _PACKAGE_REF_KEYS if isinstance(package.get(key), dict)]


def _evidence_source_refs(package):
    """Return every original source ref behind the package's evidence."""
    refs = []
    for entry in package.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        for ref in entry.get("source_refs") or []:
            refs.append(ref)
    return refs


def _authorized_keys(package):
    """Return the sorted authorized finding keys and citation keys."""
    finding_keys = sorted(
        entry["finding_key"]
        for entry in package.get("evidence") or []
        if isinstance(entry, dict) and isinstance(entry.get("finding_key"), str)
    )
    citation_keys = sorted(package.get("citations") or {})
    return finding_keys, citation_keys


# --------------------------------------------------------------------------- #
# Material builders
# --------------------------------------------------------------------------- #
def blueprint_material(store, workspace_ref, workspace, package):
    """Build the planner material from durable state.

    Returns ``(material, input_refs, source_refs)``.  ``material`` is the
    JSON-safe prompt payload (section, approved framework/synthesis, the
    authorized evidence and verified-corpus citations, the output contract, and
    an illustrative example); ``input_refs`` are the exact workspace + package
    global revisions whose bytes the runner pins; ``source_refs`` are the
    original sources behind the package evidence, for the runner's
    hosted-transfer rights check.
    """
    finding_keys, citation_keys = _authorized_keys(package)
    material = {
        "task": "propose a section blueprint for the supplied section",
        "output_contract": BLUEPRINT_OUTPUT_CONTRACT,
        "rules": _COMMON_RULES,
        "illustrative_example": {
            "label": "illustrative only",
            "example": _BLUEPRINT_EXAMPLE,
        },
        "section_id": workspace["section_id"],
        "section": package.get("section"),
        "outline_ref": package.get("outline_ref"),
        "framework": package.get("framework"),
        "synthesis": package.get("synthesis"),
        "authorized_finding_keys": finding_keys,
        "authorized_citation_keys": citation_keys,
        "evidence": list(package.get("evidence") or []),
        "citations": dict(package.get("citations") or {}),
    }
    input_refs = _dedupe_refs(store, [workspace_ref, *_package_refs(package)])
    source_refs = _dedupe_refs(store, _evidence_source_refs(package))
    return material, input_refs, source_refs


def draft_material(store, blueprint_ref, blueprint, package):
    """Build the writer material from the approved blueprint and package.

    Returns ``(material, input_refs, source_refs)`` (see
    :func:`blueprint_material`).
    """
    finding_keys, citation_keys = _authorized_keys(package)
    material = {
        "task": "draft the supplied section from the approved blueprint",
        "output_contract": SECTION_WRITER_OUTPUT_CONTRACT,
        "rules": _COMMON_RULES,
        "illustrative_example": {
            "label": "illustrative only",
            "example": _DRAFT_EXAMPLE,
        },
        "section_id": blueprint["section_id"],
        "blueprint": blueprint,
        "authorized_finding_keys": finding_keys,
        "authorized_citation_keys": citation_keys,
        "evidence": list(package.get("evidence") or []),
        "citations": dict(package.get("citations") or {}),
    }
    input_refs = _dedupe_refs(store, [blueprint_ref, *_package_refs(package)])
    source_refs = _dedupe_refs(store, _evidence_source_refs(package))
    return material, input_refs, source_refs


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #
def _not_completed(request_id, generation):
    return {
        "status": "model_not_completed",
        "request_id": request_id,
        "outcome": generation.get("outcome"),
        "generation": generation,
    }


def _invalid_proposal(request_id, generation):
    return {
        "status": "invalid_proposal",
        "reason": INVALID_PROPOSAL_REASON,
        "request_id": request_id,
        "generation": generation,
    }


# --------------------------------------------------------------------------- #
# Public proposal drivers
# --------------------------------------------------------------------------- #
def propose_blueprint(
    store,
    *,
    registry,
    activation_token,
    request_id,
    workspace_ref,
    hosted_ref,
    api_key,
    artifact_id,
    revision_id,
    rights,
    transport=None,
    parent_revision_id=None,
):
    """Plan one section blueprint through the model port; never approve.

    The exact current per-section workspace and its reconstructible approved
    drafting package are re-derived first; the planner material is sent through
    the single generation runner (role ``blueprint``), and a completed proposal is
    validated with :func:`validate_blueprint` and stored through the existing
    drafting workflow (over the current workspace) as an inert revision.  A
    non-completed run or a schema-invalid proposal writes nothing.
    """
    workspace = SectionWorkspaceRepository(store).read(workspace_ref, current=True)[
        "result"
    ]
    package = DraftingPackageRepository(store).build(
        workspace["outline_ref"], workspace["section_id"]
    )
    material, input_refs, source_refs = blueprint_material(
        store, workspace_ref, workspace, package
    )
    generation = execute_generation_task(
        store,
        registry=registry,
        activation_token=activation_token,
        role=BLUEPRINT_ROLE,
        request_id=request_id,
        hosted_ref=hosted_ref,
        prompt_version=BLUEPRINT_PROMPT_VERSION,
        instructions=BLUEPRINT_INSTRUCTIONS,
        material=material,
        input_refs=input_refs,
        source_refs=source_refs,
        api_key=api_key,
        transport=transport,
    )
    if generation.get("outcome") != "completed":
        return _not_completed(request_id, generation)
    content = generation.get("proposal")
    if not isinstance(content, dict):
        return _invalid_proposal(request_id, generation)
    try:
        validate_blueprint(content, package=package)
    except DraftingError:
        return _invalid_proposal(request_id, generation)
    ref = DraftingWorkflowRepository(store).propose(
        "blueprint",
        {"workspace_ref": workspace_ref, "content": content},
        artifact_id=artifact_id,
        revision_id=revision_id,
        rights=rights,
        parent_revision_id=parent_revision_id,
    )
    return {
        "status": "proposed",
        "request_id": request_id,
        "ref": ref,
        "generation": generation,
    }


def propose_draft(
    store,
    *,
    registry,
    activation_token,
    request_id,
    blueprint_ref,
    hosted_ref,
    api_key,
    artifact_id,
    revision_id,
    rights,
    transport=None,
    parent_revision_id=None,
):
    """Draft one section through the model port; never approve.

    The exact current *approved* blueprint (its own named owner gate) and its
    package are re-derived first; the writer material is sent through the single
    generation runner (role ``section_writer``), and a completed proposal is
    validated with :func:`validate_draft` and stored through the existing drafting
    workflow (only over an approved blueprint) as an inert, provisional revision.
    A non-completed run or a schema-invalid proposal writes nothing.
    """
    approved = DraftingWorkflowRepository(store).require_blueprint(blueprint_ref)
    blueprint = approved["result"]
    package = approved["package"]
    material, input_refs, source_refs = draft_material(
        store, blueprint_ref, blueprint, package
    )
    generation = execute_generation_task(
        store,
        registry=registry,
        activation_token=activation_token,
        role=SECTION_WRITER_ROLE,
        request_id=request_id,
        hosted_ref=hosted_ref,
        prompt_version=SECTION_WRITER_PROMPT_VERSION,
        instructions=SECTION_WRITER_INSTRUCTIONS,
        material=material,
        input_refs=input_refs,
        source_refs=source_refs,
        api_key=api_key,
        transport=transport,
    )
    if generation.get("outcome") != "completed":
        return _not_completed(request_id, generation)
    content = generation.get("proposal")
    if not isinstance(content, dict):
        return _invalid_proposal(request_id, generation)
    try:
        validate_draft(content, blueprint=blueprint, package=package)
    except DraftingError:
        return _invalid_proposal(request_id, generation)
    ref = DraftingWorkflowRepository(store).propose(
        "draft",
        {"blueprint_ref": blueprint_ref, "content": content},
        artifact_id=artifact_id,
        revision_id=revision_id,
        rights=rights,
        parent_revision_id=parent_revision_id,
    )
    return {
        "status": "proposed",
        "request_id": request_id,
        "ref": ref,
        "generation": generation,
    }
