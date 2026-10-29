"""Minimal, pure, JSON-portable Step 11 section-blueprint/draft validation schema.

One bounded outcome: validate an untrusted **section blueprint** and an untrusted
**section draft** against an already-approved drafting **package** (the outline
section, approved framework/synthesis results, authorized evidence, and verified
corpus citations), returning deterministic structured maps.  It never calls a
model, reads storage, resolves a workflow, adjudicates scientific truth, or
generates prose/figures.  It performs **no persistence** (a repository/CLI wrapper
adds the exact refs, ownership, and gate records around the validated payload).

Authority: ``docs/implementation/11-drafting.md`` deliverables and acceptance
criteria; ``docs/specification.md`` workflow steps 13-14 (approve sources and
paragraph blueprints before drafting; draft from summaries **and linked original
evidence**) and the scientific invariants (separate direct observations, author
interpretations, secondary reports, and proposed synthesis; carry
species/population/assay/outcome and causal limitations into conclusions;
citations come from verified corpus metadata, never a model-generated
bibliography); ``docs/contracts/01-identities.md`` §5.4 (``EvidenceRecord``:
attribution classes and the ``secondary_warning`` state) and §7.5 (missing full
text is an explicit state); ``docs/contracts/03-policy-decisions.md`` POL-P2
(applicable context/causal fidelity preserved downstream; unsupported
generalization is blocked) and POL-OPS-SECONDARY (a warned secondary claim is
never presented as verified primary support and stays unapproved pending exact
human approval).

It is deliberately conservative:

* every claim is a single **atomic** assertion grounded in one or more authorized
  accepted findings, so a compound claim cannot be only partially supported;
* a ``reported_evidence`` claim may not widen the source's known context, nor be
  produced as generalizable when a grounded finding's original context is
  ``missing``/``unknown`` (POL-P2);
* a finding whose attribution is a secondary account (or carries a
  ``secondary_warning``) can only be used with an explicit ``secondary_account``
  attribution; it is warned and never primary-verified;
* a novel author proposal is labelled ``proposed_synthesis`` -- a proposal, never
  established evidence;
* citations are **derived** from the verified-corpus records carried in the
  package; a model-supplied bibliography, study-detail, or override of the
  immutable framework/scope context is rejected;
* the artifact stays ``provisional`` with ``authorizes_export`` false and pending
  source checks / origin tracing; it cannot accept a ``final``/``approved`` flag.

This is a deliberately **constrained MVP** for drafting: no semantic validator can
judge arbitrary prose, so a draft sentence's ``text`` must exactly equal the
ordered approved atomic claim text(s) joined with a single space, and prose edits
require a new blueprint approval.  The approved **package** is trusted, internally
constructed context (from ``require_for_blueprints`` plus the accepted extraction):
its readable string summary, exact global refs, ``section_id``/``document_type``,
inert authority flags, and verified-receipt originals are accepted rather than
treated as model output.  Later critique/edit stages may broaden the draft text
rule when authorized.

Public API: :class:`DraftingError`, :func:`validate_blueprint`,
:func:`validate_draft`, and the synthetic example builders
:func:`drafting_example_package`, :func:`blueprint_example`,
:func:`draft_example`.
"""

from __future__ import annotations

from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError, canonical_json
from ..synthesis.schema import CONTEXT_FIELDS, STATED_STATES

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1

#: Human-readable summary of exactly what structural validation guarantees.
VALIDATION_SCOPE = (
    "Structural, JSON-portable validation that a section blueprint and draft cover "
    "the approved framework/synthesis/outline context and are grounded in authorized "
    "accepted evidence plus verified-corpus citations. It does not establish "
    "scientific correctness, prose quality, or that free text is semantically true; "
    "structured support is required and the text remains a human/quality-audit "
    "obligation."
)

#: Attribution classes carried from ``EvidenceRecord`` (01-identities §5.4).
ATTRIBUTION_CLASSES = (
    "direct_observation",
    "author_interpretation",
    "secondary_account",
    "mixed",
    "unclear",
    "proposed_synthesis",
)

#: Primary attribution classes ordered strongest (most direct) to weakest.  A
#: claim may never be attributed more directly than its weakest supporting
#: finding (a reported ``direct_observation`` may not be relabelled as the
#: writer's own direct result).  ``secondary_account``/``proposed_synthesis`` are
#: handled separately as explicit states.
_PRIMARY_ATTRIBUTION_ORDER = (
    "direct_observation",
    "mixed",
    "author_interpretation",
    "unclear",
)

#: A claim is reported evidence, a new interpretation, a hypothesis, or a proposal.
CLAIM_CLASSIFICATIONS = (
    "reported_evidence",
    "new_interpretation",
    "hypothesis",
    "proposed_synthesis",
)

#: Checks that must still run before the artifact could ever be approved/exported.
PENDING_CHECKS = ("source_checks", "origin_tracing")

DRAFTING_NOTICE = (
    "A section blueprint/draft is provisional: it is grounded in authorized accepted "
    "evidence (summaries plus original evidence) and preserves applicable context, "
    "attribution, and causal limits. A secondary source carries an explicit warning "
    "and is never presented as verified primary support. A novel author proposal is "
    "labelled proposed_synthesis, not established evidence. Citations are derived "
    "from the verified corpus, never model-generated. Source checks and origin tracing "
    "are pending; this artifact is not exportable as final approved output."
)

#: A model payload must not self-authorize, override the immutable package context,
#: or carry a model-generated bibliography/study details.
_FORBIDDEN_KEYS = frozenset(
    {
        # universal score/vote keys are never a selection mechanism
        "score",
        "quality_score",
        "global_score",
        "rating",
        "weight",
        "vote",
        "votes",
        # immutable package context -- reject overrides
        "framework",
        "scope",
        "definitions",
        "methods",
        "outline",
        "synthesis",
        "package",
        "global_refs",
        "context",
        # provisional status -- cannot accept final/approved flags
        "status",
        "approved",
        "final",
        "authorizes_export",
        "export",
        # citations/study details are derived from the package, never supplied
        "bibliography",
        "references",
        "reference_list",
        "study_details",
        "doi",
        "authors",
        "journal",
        "year",
        "publication",
        "citation",
        "citations",
        "citation_keys",
    }
)


class DraftingError(ReviewAgentError):
    """A blueprint/draft payload is malformed, ungrounded, overstated, or incomplete.

    Messages name the structural defect and, when useful, a stable identifier.
    They never embed original passage text or arbitrary untrusted values.
    """


# --------------------------------------------------------------------------- #
# Small validation helpers (mirrors the Step 10 synthesis schema patterns)
# --------------------------------------------------------------------------- #
def _fail(message: str) -> "DraftingError":
    return DraftingError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _fail(message)


def _preflight(value: object, label: str) -> None:
    """Reject anything that is not canonical JSON-portable, with a static error."""
    try:
        canonical_json(value)
    except (RecordValidationError, ValueError, TypeError):
        raise _fail(f"{label} is not canonical JSON-portable") from None


def _keys(
    value: object,
    *,
    label: str,
    required: frozenset,
    optional: frozenset = frozenset(),
    allow_extra: bool = False,
) -> dict:
    _require(isinstance(value, dict), f"{label} must be a JSON object")
    present = set(value)
    missing = required - present
    _require(not missing, f"{label} is missing required keys: {sorted(missing)}")
    if not allow_extra:
        unknown = present - required - optional
        _require(not unknown, f"{label} has unknown keys")
    return value


def _mapping(value: object, *, label: str) -> dict:
    _require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _string(value: object, *, label: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{label} must be a non-empty string")
    return value


def _identifier(value: object, *, label: str) -> str:
    return _string(value, label=label)


def _enum(value: object, *, label: str, allowed: tuple) -> str:
    _require(value in allowed, f"{label} must be one of {sorted(allowed)}")
    return value  # type: ignore[return-value]


def _list(value: object, *, label: str, allow_empty: bool = True) -> list:
    _require(isinstance(value, list), f"{label} must be a JSON array")
    _require(allow_empty or value, f"{label} must not be empty")
    return value


def _str_list(value: object, *, label: str, allow_empty: bool = True) -> list:
    items = _list(value, label=label, allow_empty=allow_empty)
    out = []
    for index, raw in enumerate(items):
        out.append(_string(raw, label=f"{label}[{index}]"))
    return out


def _stated(value: object, *, label: str) -> dict:
    item = _keys(value, label=label, required=frozenset({"state", "value"}))
    state = _enum(item["state"], label=f"{label}.state", allowed=STATED_STATES)
    payload = item["value"]
    if state == "known":
        _string(payload, label=f"{label}.value")
    else:
        _require(payload is None, f"{label}.value must be null when state is {state}")
    return {"state": state, "value": payload}


def _applicability(value: object, *, label: str) -> dict:
    item = _keys(value, label=label, required=frozenset(CONTEXT_FIELDS))
    return {name: _stated(item[name], label=f"{label}.{name}") for name in CONTEXT_FIELDS}


def _require_no_forbidden(value: object, label: str) -> None:
    """Recursively reject self-authorization, context overrides, and bibliographies."""
    if isinstance(value, dict):
        banned = _FORBIDDEN_KEYS & set(value)
        _require(not banned, f"{label} must not carry forbidden keys: {sorted(banned)}")
        for key, nested in value.items():
            _require_no_forbidden(nested, f"{label}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _require_no_forbidden(nested, f"{label}[{index}]")


# --------------------------------------------------------------------------- #
# Package indexing (authorized context; immutable)
# --------------------------------------------------------------------------- #
def _finding_context(finding: dict) -> dict:
    """Return ``{field: (state, value)}`` for the finding's original context."""
    out: dict = {}
    context = finding.get("context")
    if isinstance(context, dict):
        for field in CONTEXT_FIELDS:
            stated = context.get(field)
            if isinstance(stated, dict):
                out[field] = (stated.get("state"), stated.get("value"))
    return out


def _is_secondary(finding: dict) -> bool:
    """A finding is secondary if its attribution says so or it carries a warning."""
    if finding.get("attribution") == "secondary_account":
        return True
    warning = finding.get("secondary_warning")
    return isinstance(warning, dict) and warning.get("state") == "known" and warning.get("value") is True


def _readable_summary(value: object, *, label: str):
    """Accept the accepted-extraction readable summary **string** (dict tolerated)."""
    if isinstance(value, str):
        return _string(value, label=label)
    return _mapping(value, label=label)


def _index_package(package: object) -> dict:
    """Validate and index the approved drafting package (the authorized context).

    The package is trusted, internally constructed context (from
    ``require_for_blueprints`` plus the accepted extraction) -- not model output --
    so permitted metadata (exact global refs, ``section_id``, ``schema_version``/
    ``document_type``, inert authority flags, ``methods``, ...) is accepted rather
    than rejected, and an evidence entry may carry verified-receipt originals
    (``original_passage``, ``source_context``).  The accepted-extraction summary is
    a readable string.
    """
    _preflight(package, "drafting package")
    item = _keys(
        package,
        label="drafting package",
        required=frozenset({"evidence", "citations"}),
        allow_extra=True,
    )
    section = item.get("section")
    section_id = None
    section_title = None
    if section is not None:
        section = _mapping(section, label="drafting package.section")
        section_id = _identifier(section.get("section_id"), label="drafting package.section.section_id")
        section_title = _string(section.get("title"), label="drafting package.section.title")
    top_section_id = item.get("section_id")
    if top_section_id is not None:
        top_section_id = _identifier(top_section_id, label="drafting package.section_id")
        if section_id is not None:
            _require(
                top_section_id == section_id,
                "drafting package section_id disagrees with section.section_id",
            )
        else:
            section_id = top_section_id
    _require(section_id is not None, "drafting package must carry a section_id")
    if section_title is None:
        section_title = _string(item.get("section_title", section_id), label="drafting package.section_title")
    framework = item.get("framework")
    if framework is not None:
        framework = _mapping(framework, label="drafting package.framework")
    synthesis = item.get("synthesis")
    if synthesis is not None:
        synthesis = _mapping(synthesis, label="drafting package.synthesis")

    evidence_by_key: dict = {}
    for index, raw in enumerate(_list(item["evidence"], label="drafting package.evidence", allow_empty=False)):
        label = f"drafting package.evidence[{index}]"
        entry = _keys(
            raw,
            label=label,
            required=frozenset(
                {"finding_key", "finding", "summary", "extraction_ref", "source_refs", "citation_keys"}
            ),
            allow_extra=True,
        )
        finding_key = _identifier(entry["finding_key"], label=f"{label}.finding_key")
        _require(finding_key not in evidence_by_key, "duplicate finding_key in the drafting package")
        finding = _mapping(entry["finding"], label=f"{label}.finding")
        summary = _readable_summary(entry["summary"], label=f"{label}.summary")
        extraction_ref = _mapping(entry["extraction_ref"], label=f"{label}.extraction_ref")
        source_refs = _list(entry["source_refs"], label=f"{label}.source_refs", allow_empty=False)
        citation_keys = _str_list(entry["citation_keys"], label=f"{label}.citation_keys", allow_empty=False)
        original_passage = entry.get("original_passage")
        if original_passage is not None:
            original_passage = _mapping(original_passage, label=f"{label}.original_passage")
        source_context = entry.get("source_context")
        if source_context is not None:
            source_context = _mapping(source_context, label=f"{label}.source_context")
        evidence_by_key[finding_key] = {
            "finding_key": finding_key,
            "finding": finding,
            "summary": summary,
            "extraction_ref": extraction_ref,
            "source_refs": source_refs,
            "citation_keys": citation_keys,
            "original_passage": original_passage,
            "source_context": source_context,
        }

    citations_raw = _mapping(item["citations"], label="drafting package.citations")
    citation_by_key: dict = {}
    for key, record in citations_raw.items():
        _string(key, label="drafting package.citations key")
        citation_by_key[key] = _mapping(record, label=f"drafting package.citations[{key}]")
    for key, evidence in evidence_by_key.items():
        for citation_key in evidence["citation_keys"]:
            _require(
                citation_key in citation_by_key,
                f"drafting package.evidence[{key}] cites an unknown verified-corpus record",
            )
    return {
        "section_id": section_id,
        "section_title": section_title,
        "framework": framework,
        "synthesis": synthesis,
        "evidence_by_key": evidence_by_key,
        "citation_by_key": citation_by_key,
    }


# --------------------------------------------------------------------------- #
# Blueprint
# --------------------------------------------------------------------------- #
def _validate_claim(raw: object, *, label: str, package: dict) -> dict:
    item = _keys(
        raw,
        label=label,
        required=frozenset(
            {"claim_id", "text", "finding_keys", "attribution", "classification",
             "applicability", "causal_limitations"}
        ),
    )
    claim_id = _identifier(item["claim_id"], label=f"{label}.claim_id")
    text = _string(item["text"], label=f"{label}.text")
    finding_keys = _str_list(item["finding_keys"], label=f"{label}.finding_keys", allow_empty=False)
    seen: set = set()
    for ref in finding_keys:
        _require(
            ref in package["evidence_by_key"],
            f"{label}.finding_keys names an unknown or unauthorized finding key",
        )
        _require(ref not in seen, f"{label}.finding_keys repeats a finding key")
        seen.add(ref)
    attribution = _enum(item["attribution"], label=f"{label}.attribution", allowed=ATTRIBUTION_CLASSES)
    classification = _enum(
        item["classification"], label=f"{label}.classification", allowed=CLAIM_CLASSIFICATIONS
    )
    proposed = attribution == "proposed_synthesis"
    _require(
        proposed == (classification == "proposed_synthesis"),
        f"{label} must label a novel author proposal as proposed_synthesis consistently",
    )
    applicability = _applicability(item["applicability"], label=f"{label}.applicability")
    causal = _stated(item["causal_limitations"], label=f"{label}.causal_limitations")

    records = [package["evidence_by_key"][ref]["finding"] for ref in finding_keys]

    # Attribution fidelity.  A secondary source can only be used with explicit
    # secondary attribution (POL-OPS-SECONDARY); a novel author proposal only as
    # proposed_synthesis; and a claim may never be attributed more directly than
    # its weakest supporting finding -- a reported ``author_interpretation``,
    # ``mixed``, or ``unclear`` finding cannot be relabelled as the writer's own
    # ``direct_observation``.
    secondary = any(_is_secondary(record) for record in records)
    if secondary:
        _require(
            attribution == "secondary_account",
            f"{label} presents a secondary source as primary support",
        )
    if any(record.get("attribution") == "proposed_synthesis" for record in records):
        _require(
            attribution == "proposed_synthesis",
            f"{label} presents a novel proposal as reported evidence",
        )
    primary_attrs = [record.get("attribution") for record in records]
    primary_attrs = [a for a in primary_attrs if a in _PRIMARY_ATTRIBUTION_ORDER]
    if primary_attrs and attribution in _PRIMARY_ATTRIBUTION_ORDER and not secondary:
        weakest = max(_PRIMARY_ATTRIBUTION_ORDER.index(a) for a in primary_attrs)
        _require(
            _PRIMARY_ATTRIBUTION_ORDER.index(attribution) >= weakest,
            f"{label} overstates the source attribution (a source class is relabelled as more direct)",
        )

    # Context fidelity (POL-P2).  A consequential missing/unknown original
    # context blocks the affected claim for *every* classification -- factual,
    # interpretive, or proposed -- so it cannot be bypassed by renaming the
    # classification.  ``not_applicable`` is an explicit, allowed state.
    known_per_finding: list = []
    missing_original: set = set()
    for record in records:
        context = _finding_context(record)
        known: dict = {}
        for field in CONTEXT_FIELDS:
            state, value = context.get(field, (None, None))
            if state == "known" and isinstance(value, str):
                known[field] = value
            elif state == "not_applicable":
                pass
            else:  # missing / unknown / absent is consequential
                missing_original.add(field)
        known_per_finding.append(known)
    missing_original_sorted = sorted(missing_original)
    _require(
        not missing_original_sorted,
        f"{label} is blocked by missing/unknown original context: {missing_original_sorted}",
    )
    # A declared known value must be consistent with EACH supporting finding; a
    # union of heterogeneous source contexts would silently generalize.
    context_mismatch = []
    for field in CONTEXT_FIELDS:
        if applicability[field]["state"] != "known":
            continue
        for known in known_per_finding:
            value = known.get(field)
            if value is not None and value != applicability[field]["value"]:
                context_mismatch.append(field)
                break
    if classification == "reported_evidence":
        _require(
            not context_mismatch,
            f"{label} overstates the source context (unsupported generalization)",
        )

    warnings = []
    if secondary:
        warnings.append("secondary_origin_warning")
    if classification != "reported_evidence" and context_mismatch:
        # A proposed context extension is a warning only; the underlying original
        # context is fully present (missing context already blocked above).
        warnings.append("context_extension")
    if proposed:
        warnings.append("author_proposal")

    # Citations are derived from the verified corpus, never supplied by the model.
    citation_keys = sorted({key for ref in finding_keys for key in package["evidence_by_key"][ref]["citation_keys"]})
    for citation_key in citation_keys:
        _require(citation_key in package["citation_by_key"], f"{label} references an unauthorized citation key")

    return {
        "claim_id": claim_id,
        "text": text,
        "finding_keys": finding_keys,
        "attribution": attribution,
        "classification": classification,
        "applicability": applicability,
        "causal_limitations": causal,
        "citation_keys": citation_keys,
        "secondary": secondary,
        "proposed_synthesis": proposed,
        # No factual/origin check has run at blueprint time, so no claim is ever
        # reported as primary-verified; that remains a later review obligation.
        "primary_verified": False,
        "warnings": warnings,
    }


def validate_blueprint(payload, *, package):
    """Validate and normalize one untrusted section blueprint over a package.

    ``package`` is the approved drafting context (see
    :func:`drafting_example_package`): the outline ``section``, the approved
    ``framework``/``synthesis`` results, the authorized ``evidence`` list (each
    entry carrying the accepted ``finding``, its ``summary``, exact
    ``extraction_ref``/``source_refs``, and verified-corpus ``citation_keys``),
    and the ``citations`` map of verified bibliographic records.  ``payload`` is
    the blueprint content::

        {
          "schema_version": 1,
          "section_id": "<must match the package section>",
          "source_suggestions": ["<authorized finding_key>", ...],   # optional
          "paragraphs": [
            {"paragraph_id": "...",
             "source_suggestions": [...],                             # optional
             "intents": [
               {"intent_id": "...", "intent": "<sentence intent>",
                "claims": [
                  {"claim_id": "...", "text": "<atomic assertion>",
                   "finding_keys": ["A10:F1", ...], "attribution": "...",
                   "classification": "...", "applicability": {context},
                   "causal_limitations": {state, value}}
                ]}
             ]}
          ]
        }

    Returns a deterministic, JSON-portable ``dict`` or raises
    :class:`DraftingError`.  Every claim is atomic and separately grounded; a
    ``reported_evidence`` claim may not overstate context or attribution; source
    suggestions and citations are authorized by the package, never invented.
    """
    _preflight(payload, "blueprint payload")
    _require(isinstance(payload, dict), "blueprint payload must be a JSON object")
    _require_no_forbidden(payload, "blueprint payload")
    package = _index_package(package)
    item = _keys(
        payload,
        label="blueprint payload",
        required=frozenset({"schema_version", "section_id", "paragraphs"}),
        optional=frozenset({"source_suggestions"}),
    )
    version = item["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        f"schema_version must be the integer {SCHEMA_VERSION}",
    )
    section_id = _identifier(item["section_id"], label="section_id")
    _require(
        section_id == package["section_id"],
        "blueprint section_id does not match the immutable package section",
    )
    source_suggestions = _str_list(item.get("source_suggestions", []), label="source_suggestions")
    for ref in source_suggestions:
        _require(ref in package["evidence_by_key"], "source_suggestions names an unauthorized source")

    paragraphs = []
    seen_paragraphs: set = set()
    seen_intents: set = set()
    seen_claims: set = set()
    for index, raw in enumerate(_list(item["paragraphs"], label="paragraphs", allow_empty=False)):
        label = f"paragraphs[{index}]"
        paragraph = _keys(
            raw, label=label, required=frozenset({"paragraph_id", "intents"}),
            optional=frozenset({"source_suggestions"}),
        )
        paragraph_id = _identifier(paragraph["paragraph_id"], label=f"{label}.paragraph_id")
        _require(paragraph_id not in seen_paragraphs, "duplicate paragraph_id")
        seen_paragraphs.add(paragraph_id)
        paragraph_sources = _str_list(paragraph.get("source_suggestions", []), label=f"{label}.source_suggestions")
        for ref in paragraph_sources:
            _require(ref in package["evidence_by_key"], f"{label}.source_suggestions names an unauthorized source")
        intents = []
        for j_index, raw_intent in enumerate(_list(paragraph["intents"], label=f"{label}.intents", allow_empty=False)):
            i_label = f"{label}.intents[{j_index}]"
            intent = _keys(raw_intent, label=i_label, required=frozenset({"intent_id", "intent", "claims"}))
            intent_id = _identifier(intent["intent_id"], label=f"{i_label}.intent_id")
            _require(intent_id not in seen_intents, "duplicate intent_id")
            seen_intents.add(intent_id)
            intent_text = _string(intent["intent"], label=f"{i_label}.intent")
            claims = []
            for k_index, raw_claim in enumerate(_list(intent["claims"], label=f"{i_label}.claims", allow_empty=False)):
                c_label = f"{i_label}.claims[{k_index}]"
                claim = _validate_claim(raw_claim, label=c_label, package=package)
                _require(claim["claim_id"] not in seen_claims, "duplicate claim_id")
                seen_claims.add(claim["claim_id"])
                claims.append(claim)
            intents.append({"intent_id": intent_id, "intent": intent_text, "claims": claims})
        paragraphs.append(
            {"paragraph_id": paragraph_id, "source_suggestions": paragraph_sources, "intents": intents}
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "drafting_blueprint",
        "section_id": section_id,
        "section_title": package["section_title"],
        "paragraphs": paragraphs,
        "source_suggestions": source_suggestions,
        "status": "provisional",
        "authorizes_export": False,
        "pending": list(PENDING_CHECKS),
        "notice": DRAFTING_NOTICE,
        "scope": VALIDATION_SCOPE,
    }


# --------------------------------------------------------------------------- #
# Draft
# --------------------------------------------------------------------------- #
def _index_blueprint(blueprint: object) -> dict:
    """Index a validated blueprint, preserving the approved paragraph/intent/claim order."""
    _preflight(blueprint, "validated blueprint")
    _require(isinstance(blueprint, dict), "blueprint must be a validated blueprint result")
    _require(blueprint.get("kind") == "drafting_blueprint", "blueprint is not a validated blueprint result")
    paragraphs: list = []
    claim_info: dict = {}
    for paragraph in blueprint.get("paragraphs", []):
        intents: list = []
        for intent in paragraph["intents"]:
            claims = [(claim["claim_id"], claim) for claim in intent["claims"]]
            intents.append({"intent_id": intent["intent_id"], "claims": claims})
            for claim_id, claim in claims:
                claim_info[claim_id] = claim
        paragraphs.append({"paragraph_id": paragraph["paragraph_id"], "intents": intents})
    return {"section_id": blueprint["section_id"], "paragraphs": paragraphs, "claim_info": claim_info}


def validate_draft(payload, *, blueprint, package):
    """Validate and normalize one untrusted section draft over a blueprint.

    This is a deliberately constrained MVP: it does not judge arbitrary free prose
    (no semantic validator can).  A sentence's ``text`` must **exactly** equal the
    ordered approved atomic claim text(s) of its intent joined with a single
    space, so a changed number, condition, or added assertion is rejected; prose
    editing requires a new blueprint approval.  ``payload`` is the draft content::

        {
          "schema_version": 1,
          "section_id": "<must match the blueprint/package section>",
          "paragraphs": [
            {"paragraph_id": "...",
             "sentences": [
               {"intent_id": "...", "claim_ids": ["K1", ...], "text": "<approved text>"}
             ]}
          ]
        }

    ``claim_ids`` must equal the intent's approved claim ids in order.  The output
    is derived deterministically from the validated blueprint (paragraphs/intents/
    claims normalized to approved order) and carries per-sentence attribution/
    classification warnings so assembly can label secondary accounts and author
    proposals accurately -- there are no scientific verified flags.  Returns a
    deterministic, JSON-portable ``dict`` or raises :class:`DraftingError`.
    """
    _preflight(payload, "draft payload")
    _require(isinstance(payload, dict), "draft payload must be a JSON object")
    _require_no_forbidden(payload, "draft payload")
    package = _index_package(package)
    index = _index_blueprint(blueprint)
    _require(
        index["section_id"] == package["section_id"],
        "blueprint section does not match the drafting package section",
    )
    item = _keys(
        payload,
        label="draft payload",
        required=frozenset({"schema_version", "section_id", "paragraphs"}),
    )
    version = item["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        f"schema_version must be the integer {SCHEMA_VERSION}",
    )
    section_id = _identifier(item["section_id"], label="section_id")
    _require(section_id == index["section_id"], "draft section_id does not match the blueprint section")

    # Index the untrusted draft by id, rejecting duplicates.
    draft_paragraphs: dict = {}
    for p_index, raw in enumerate(_list(item["paragraphs"], label="paragraphs", allow_empty=False)):
        label = f"paragraphs[{p_index}]"
        paragraph = _keys(raw, label=label, required=frozenset({"paragraph_id", "sentences"}))
        paragraph_id = _identifier(paragraph["paragraph_id"], label=f"{label}.paragraph_id")
        _require(paragraph_id not in draft_paragraphs, "duplicate paragraph_id in draft")
        sentences: dict = {}
        for s_index, raw_sentence in enumerate(
            _list(paragraph["sentences"], label=f"{label}.sentences", allow_empty=False)
        ):
            s_label = f"{label}.sentences[{s_index}]"
            sentence = _keys(
                raw_sentence, label=s_label,
                required=frozenset({"intent_id", "claim_ids", "text"}),
            )
            intent_id = _identifier(sentence["intent_id"], label=f"{s_label}.intent_id")
            _require(intent_id not in sentences, "an intent is covered more than once in the draft")
            sentences[intent_id] = sentence
        draft_paragraphs[paragraph_id] = sentences

    approved_paragraph_ids = [paragraph["paragraph_id"] for paragraph in index["paragraphs"]]
    _require(
        set(draft_paragraphs) == set(approved_paragraph_ids),
        "the draft does not cover exactly the blueprint paragraphs",
    )

    # Normalize to the approved order and derive the output from the blueprint.
    paragraphs = []
    for paragraph in index["paragraphs"]:
        paragraph_id = paragraph["paragraph_id"]
        sentences_in = draft_paragraphs[paragraph_id]
        approved_intents = {intent["intent_id"]: intent for intent in paragraph["intents"]}
        _require(
            set(sentences_in) == set(approved_intents),
            f"draft paragraph {paragraph_id!r} does not cover exactly the blueprint intents",
        )
        sentences = []
        for intent in paragraph["intents"]:
            intent_id = intent["intent_id"]
            sentence = sentences_in[intent_id]
            s_label = f"paragraph {paragraph_id!r} intent {intent_id!r}"
            approved_ids = [claim_id for claim_id, _ in intent["claims"]]
            claim_ids = _str_list(sentence["claim_ids"], label=f"{s_label}.claim_ids", allow_empty=False)
            _require(
                claim_ids == approved_ids,
                f"{s_label}.claim_ids must equal the intent's approved claim ids in order",
            )
            approved_text = " ".join(index["claim_info"][claim_id]["text"] for claim_id in approved_ids)
            _require(
                sentence["text"] == approved_text,
                f"{s_label}.text must exactly match the approved atomic claim text(s)",
            )
            citation_keys = sorted(
                {key for claim_id in approved_ids for key in index["claim_info"][claim_id]["citation_keys"]}
            )
            sentences.append(
                {
                    "intent_id": intent_id,
                    "claim_ids": approved_ids,
                    "text": approved_text,
                    "citation_keys": citation_keys,
                    "attributions": sorted({index["claim_info"][c]["attribution"] for c in approved_ids}),
                    "classifications": sorted({index["claim_info"][c]["classification"] for c in approved_ids}),
                    "warnings": sorted({w for c in approved_ids for w in index["claim_info"][c]["warnings"]}),
                    "secondary": any(index["claim_info"][c]["secondary"] for c in approved_ids),
                    "proposed_synthesis": any(index["claim_info"][c]["proposed_synthesis"] for c in approved_ids),
                }
            )
        paragraphs.append({"paragraph_id": paragraph_id, "sentences": sentences})

    used_citations = sorted({key for p in paragraphs for s in p["sentences"] for key in s["citation_keys"]})
    citations = {key: package["citation_by_key"][key] for key in used_citations}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "section_draft",
        "section_id": section_id,
        "paragraphs": paragraphs,
        "citations": citations,
        "status": "provisional",
        "authorizes_export": False,
        "pending": list(PENDING_CHECKS),
        "notice": DRAFTING_NOTICE,
        "scope": VALIDATION_SCOPE,
    }


# --------------------------------------------------------------------------- #
# Synthetic example builders (fictional; never real evidence)
# --------------------------------------------------------------------------- #
def _ref(artifact_id, revision_id="r1", project_id="P-0001"):
    return {"project_id": project_id, "artifact_id": artifact_id, "revision_id": revision_id}


def _known(value):
    return {"state": "known", "value": value}


def _example_context(species="mouse", population="n=8 mice", assay="tumor volume",
                     outcome="tumor volume", conditions="vehicle comparison"):
    return {
        "species": _known(species),
        "population": _known(population),
        "assay": _known(assay),
        "outcome": _known(outcome),
        "conditions": _known(conditions),
    }


def _example_finding(finding_id, source_key, statement, attribution="direct_observation", **overrides):
    record = {
        "finding_id": finding_id,
        "source_key": source_key,
        "statement": statement,
        "attribution": attribution,
        "context": _example_context(),
        "limitations": _known("single model system"),
        "secondary_warning": {"state": "not_applicable", "value": None},
    }
    record.update(overrides)
    return record


def drafting_example_package():
    """Return a synthetic approved drafting package (never real evidence).

    Carries two primary findings (``A10:F1``, ``B7:F1``) from distinct studies and
    one secondary account (``C3:F1``) that must be attributed as such.  Citations
    map to verified-corpus records keyed by stable citation keys.
    """
    return {
        # Trusted, internally constructed metadata (exact global refs, inert
        # authority flags) is accepted alongside the authorized context.
        "schema_version": 1,
        "document_type": "drafting_package",
        "section_id": "sec2",
        "outline_ref": _ref("outline", "r1"),
        "framework_ref": _ref("framework", "r1"),
        "synthesis_ref": _ref("synthesis", "r1"),
        "audit_ref": _ref("audit", "r1"),
        "acquisition_ref": _ref("acquisition", "r1"),
        "freeze_ref": _ref("freeze", "r1"),
        "corpus_ref": _ref("corpus", "r1"),
        "authority": "named_gate_only",
        "authorizes_export": False,
        "section": {
            "section_id": "sec2",
            "order": 2,
            "title": "Evidence and correlation",
            "purpose": "Present the evidence map and its correlations.",
            "concept_dependencies": ["C1", "C2"],
            "relationship_dependencies": ["R1"],
            "statement_dependencies": ["S-1", "S-2"],
        },
        "framework": {
            "kind": "framework",
            "title": "Compound Z tumor-effect framework",
            "contribution": "Organizes the reports around two distinct studies.",
            "criteria": {
                "contribution": "adds an organizing relationship",
                "admission": "definition, role, support, counterevidence, limits",
                "support": "accepted finding keys",
                "revision": "changed definitions reopen sections",
            },
            "concepts": [{"concept_id": "C1", "definition": "Compound Z tumor-volume effect in mice."}],
        },
        "synthesis": {
            "kind": "synthesis",
            "statements": [
                {"statement_id": "S-1", "text": "Compound Z reduced tumor volume in a mouse model."},
                {"statement_id": "S-2", "text": "A secondary account repeats the reduction."},
            ],
        },
        "evidence": [
            {
                "finding_key": "A10:F1",
                "finding": _example_finding("F1", "docA10", "reduced tumor volume by 12.5 mm3"),
                "summary": "A mouse study reported a reduced tumor volume.",
                # A verified-receipt original may accompany the evidence; it is
                # accepted, not forbidden.
                "original_passage": {
                    "locator": {"page": 3, "paragraph": 2},
                    "source_ref": _ref("A10", "a1"),
                    "text": "tumor volume was reduced in the treated cohort",
                },
                "source_context": {"species": "mouse"},
                "extraction_ref": _ref("A10", "e1"),
                "source_refs": [_ref("A10", "a1")],
                "citation_keys": ["CIT-A10"],
            },
            {
                "finding_key": "B7:F1",
                "finding": _example_finding("F1", "docB7", "reduced tumor volume in an independent cohort"),
                "summary": "A separate mouse study reported a reduced tumor volume.",
                "extraction_ref": _ref("B7", "e1"),
                "source_refs": [_ref("B7", "a1")],
                "citation_keys": ["CIT-B7"],
            },
            {
                "finding_key": "C3:F1",
                "finding": _example_finding(
                    "F1", "docC3", "a review repeats the reduction",
                    attribution="secondary_account",
                    secondary_warning={"state": "known", "value": True},
                ),
                "summary": "A review article repeats the reported reduction.",
                "extraction_ref": _ref("C3", "e1"),
                "source_refs": [_ref("C3", "a1")],
                "citation_keys": ["CIT-C3"],
            },
        ],
        "citations": {
            "CIT-A10": {"citation_key": "CIT-A10", "title": "Report A10", "verified": True},
            "CIT-B7": {"citation_key": "CIT-B7", "title": "Report B7", "verified": True},
            "CIT-C3": {"citation_key": "CIT-C3", "title": "Review C3", "verified": True},
        },
        "methods": {"note": "synthetic fixture; not evidence"},
    }


def blueprint_example(package=None):
    """Return a validated synthetic blueprint (never real evidence)."""
    package = package or drafting_example_package()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "section_id": package["section"]["section_id"],
        "source_suggestions": ["A10:F1", "B7:F1", "C3:F1"],
        "paragraphs": [
            {
                "paragraph_id": "P1",
                "intents": [
                    {
                        "intent_id": "I1",
                        "intent": "State the multi-source reported effect with its limits.",
                        "claims": [
                            {
                                "claim_id": "K1",
                                "text": "Compound Z reduced tumor volume in a mouse model.",
                                "finding_keys": ["A10:F1", "B7:F1"],
                                "attribution": "direct_observation",
                                "classification": "reported_evidence",
                                "applicability": _example_context(),
                                "causal_limitations": _known("association only; no mechanism established"),
                            }
                        ],
                    }
                ],
            },
            {
                "paragraph_id": "P2",
                "intents": [
                    {
                        "intent_id": "I2",
                        "intent": "Attribute the secondary account explicitly.",
                        "claims": [
                            {
                                "claim_id": "K2",
                                "text": "A secondary account repeats the reduction.",
                                "finding_keys": ["C3:F1"],
                                "attribution": "secondary_account",
                                "classification": "reported_evidence",
                                "applicability": _example_context(),
                                "causal_limitations": _known("secondary source; upstream origin unavailable"),
                            }
                        ],
                    },
                    {
                        "intent_id": "I3",
                        "intent": "Offer a labelled author proposal.",
                        "claims": [
                            {
                                "claim_id": "K3",
                                "text": "A shared upstream mechanism may explain the effect.",
                                "finding_keys": ["A10:F1"],
                                "attribution": "proposed_synthesis",
                                "classification": "proposed_synthesis",
                                "applicability": _example_context(),
                                "causal_limitations": _known("proposed only; not established"),
                            }
                        ],
                    },
                ],
            },
        ],
    }
    return validate_blueprint(payload, package=package)


def draft_example(blueprint=None, package=None):
    """Return a validated synthetic draft whose text is derived from the blueprint."""
    package = package or drafting_example_package()
    blueprint = blueprint or blueprint_example(package)
    paragraphs = []
    for paragraph in blueprint["paragraphs"]:
        sentences = []
        for intent in paragraph["intents"]:
            claim_ids = [claim["claim_id"] for claim in intent["claims"]]
            text = " ".join(claim["text"] for claim in intent["claims"])
            sentences.append({"intent_id": intent["intent_id"], "claim_ids": claim_ids, "text": text})
        paragraphs.append({"paragraph_id": paragraph["paragraph_id"], "sentences": sentences})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "section_id": blueprint["section_id"],
        "paragraphs": paragraphs,
    }
    return validate_draft(payload, blueprint=blueprint, package=package)
