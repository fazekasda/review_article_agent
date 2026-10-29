"""Minimal, pure, JSON-portable synthesis/framework/outline schema (Step 10, S10-EVIDENCE).

One bounded outcome: validate an untrusted **synthesis payload** against the
already-accepted Step 09 evidence cases, and validate the **framework** and
**outline** payloads that depend on it, returning deterministic structured maps.
It never calls a model, reads storage, resolves a workflow, adjudicates
scientific truth, pools statistics, or generates prose/figures.

Authority: ``docs/implementation/10-synthesis.md`` deliverables and acceptance
criteria; ``docs/specification.md`` workflow steps 11-13 and scientific
invariants (multiple reports of one dataset are not independent confirmation;
nulls are not automatically refutations; preserve species/outcome/assay and
causal limitations); ``docs/contracts/01-identities.md`` §5.9/§7.7 (independence
is an explicit, provenance-backed adjudication; shared underlying data is a hard
prohibition); ``docs/contracts/03-policy-decisions.md`` POL-P1 (framework
contribution and concept-admission/support/revision safeguards) and POL-P2
(applicable-context/causal-fidelity preserved through synthesis).

It is deliberately conservative:

* every original finding from every accepted case is preserved automatically,
  with its exact extraction reference, source, passage, context, limitations, and
  attribution;
* every accepted finding must be explicitly dispositioned, so contradictory,
  negative, or insufficient evidence cannot be silently omitted;
* known shared underlying studies/datasets -- even a merely *proposed* positive
  link -- collapse into one correlation group and can never be counted
  independent; absence of a link means independence is **unknown**, never proven;
* `independent` support is claimed only by an explicit, user-reviewable
  adjudication with distinct underlying sources and a rationale;
* publication counts, significance votes, or model votes never select a
  conclusion.

Public API: :func:`validate_synthesis`, :func:`validate_framework`,
:func:`validate_outline`, and the synthetic example builders
:func:`synthesis_example_cases`, :func:`synthesis_example_payload`,
:func:`synthesis_example`, :func:`framework_example`, :func:`outline_example`.

This schema is the **pure payload contract**; a repository/CLI wrapper adds the
exact ``synthesis_ref``/``framework_ref``/``outline_ref`` and ownership/gate
records around this validated payload.
"""

from __future__ import annotations

import copy

from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError, canonical_json

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1

#: Human-readable summary of exactly what structural validation guarantees.
VALIDATION_SCOPE = (
    "Structural, JSON-portable validation of an evidence map, framework, and "
    "outline that are traceable to accepted extraction findings. It does not "
    "establish scientific correctness, completeness, or that no finding was "
    "omitted; that remains a human/quality audit obligation."
)

#: Field-state vocabulary for a value that may genuinely be unavailable.
STATED_STATES = ("known", "missing", "unknown", "not_applicable")

#: Applicable-context fields carried from extraction (P2 / I4).
CONTEXT_FIELDS = ("species", "population", "assay", "outcome", "conditions")

#: Separate reported evidence from new interpretation and hypothesis (I8 / step 10).
STATEMENT_CLASSIFICATIONS = ("reported_evidence", "new_interpretation", "hypothesis")

#: A statement is a proposal until it is explicitly approved.
STATEMENT_STATUSES = ("proposed", "approved")

#: Explicit disposition of every accepted finding; the distinctions the step
#: requires (genuine disagreement, species/outcome difference, null evidence,
#: insufficient data) are dispositions, never silent omissions.
FINDING_DISPOSITIONS = (
    "incorporated",
    "genuine_disagreement",
    "species_or_outcome_difference",
    "null_evidence",
    "insufficient_data",
    "not_used",
    "out_of_scope",
)

#: Independence advisory conclusions (never inferred from distinct ids).
INDEPENDENCE_CONCLUSIONS = ("independent", "not_independent", "unknown")

#: Adjudicator classes (an agent-produced conclusion needs human confirmation).
ACTOR_KINDS = ("user", "agent")

#: Framework concept statuses.  ``unclassified`` is the fail-closed state when a
#: required POL-P1 safeguard is absent; it can never be marked approved.
CONCEPT_STATUSES = ("proposed", "established", "unclassified")

#: Novelty is labelled, never forced.
NOVELTY_VALUES = ("new", "existing", "unclear")

#: The four mandatory framework criteria (POL-P1).
FRAMEWORK_CRITERIA = ("contribution", "admission", "support", "revision")

SYNTHESIS_NOTICE = (
    "Evidence map of accepted extraction findings. Shared underlying studies/datasets "
    "are correlated and never independent; unlinked is unknown independence. "
    "Independent support requires an explicit adjudication with distinct underlying "
    "sources. Findings are preserved; every finding is explicitly dispositioned. "
    "Publication counts, significance votes, or model votes never select a conclusion."
)

FRAMEWORK_NOTICE = (
    "A framework is a proposal unless explicitly justified as established knowledge "
    "with supporting evidence. Concepts carry definition, role, original support, "
    "counterevidence, and applicability limits; a missing safeguard leaves a concept "
    "unclassified and it cannot be approved. Novelty is labelled, never forced."
)

OUTLINE_NOTICE = (
    "Outline sections and their concept/relationship/statement dependencies are "
    "explicit so a changed definition or relationship can flag dependent sections. "
    "The outline is separate from the early scope map and the Jev rubric."
)

_BANNED_TOP_KEYS = frozenset({"score", "quality_score", "global_score", "rating", "weight", "vote", "votes"})


class SynthesisError(ReviewAgentError):
    """A synthesis/framework/outline payload is malformed, ungrounded, or incomplete.

    Messages name the structural defect and, when useful, a stable identifier.
    They never embed original passage text or arbitrary untrusted values.
    """


# --------------------------------------------------------------------------- #
# Small validation helpers
# --------------------------------------------------------------------------- #
def _fail(message: str) -> "SynthesisError":
    return SynthesisError(message)


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


def _stated(value: object, *, label: str, kind: str = "str") -> dict:
    item = _keys(value, label=label, required=frozenset({"state", "value"}))
    state = _enum(item["state"], label=f"{label}.state", allowed=STATED_STATES)
    payload = item["value"]
    if state == "known":
        if kind == "str":
            _string(payload, label=f"{label}.value")
        elif kind == "number":
            _require(isinstance(payload, (int, float)) and not isinstance(payload, bool), f"{label}.value must be a number when known")
        elif kind == "bool":
            _require(isinstance(payload, bool), f"{label}.value must be a boolean when known")
        else:  # pragma: no cover - internal misuse
            raise _fail(f"{label}: unsupported stated kind")
    else:
        _require(payload is None, f"{label}.value must be null when state is {state}")
    return {"state": state, "value": payload}


def _str_list(value: object, *, label: str, allow_empty: bool = True) -> list:
    items = _list(value, label=label, allow_empty=allow_empty)
    out = []
    for index, raw in enumerate(items):
        out.append(_string(raw, label=f"{label}[{index}]"))
    return out


def _list(value: object, *, label: str, allow_empty: bool = True) -> list:
    _require(isinstance(value, list), f"{label} must be a JSON array")
    _require(allow_empty or value, f"{label} must not be empty")
    return value


def _require_no_banned(output: object, *, label: str) -> None:
    if isinstance(output, dict):
        banned = _BANNED_TOP_KEYS & set(output)
        _require(not banned, f"{label} must not carry universal score/vote keys: {sorted(banned)}")


def _applicability(value: object, *, label: str) -> dict:
    item = _keys(value, label=label, required=frozenset(CONTEXT_FIELDS), optional=frozenset({"note"}))
    out = {}
    for name in CONTEXT_FIELDS:
        out[name] = _stated(item[name], label=f"{label}.{name}", kind="str")
    note = item.get("note")
    if note is not None:
        note = _string(note, label=f"{label}.note")
    return {"context": out, "note": note}


# --------------------------------------------------------------------------- #
# Accepted Step 09 cases -> namespaced findings and correlation groups
# --------------------------------------------------------------------------- #
def _index_cases(cases: object):
    """Return ``(ordered_case_ids, cases_by_id, findings_by_key, refs_by_key)``.

    ``cases`` is the normalized ``require_batch()['cases']`` list.  Each case must
    carry ``id``, ``extraction_ref``, and an ``extraction`` mapping with
    ``findings`` and ``source_records``.  Findings are preserved verbatim and
    keyed as ``"<case_id>:<finding_id>"`` so duplicate local ids (e.g. ``F1`` in
    two cases) never collide.
    """
    raw_cases = _list(cases, label="cases", allow_empty=False)
    cases_by_id: dict[str, dict] = {}
    ordered: list[str] = []
    findings_by_key: dict[str, dict] = {}
    refs_by_key: dict[str, set] = {}
    source_exact_by_case: dict[str, set] = {}
    source_doc_by_case: dict[str, set] = {}
    for index, raw in enumerate(raw_cases):
        label = f"cases[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"id", "extraction_ref", "extraction"}),
            allow_extra=True,
        )
        case_id = _identifier(item["id"], label=f"{label}.id")
        _require(case_id not in cases_by_id, "duplicate case id in cases")
        extraction_ref = _mapping(item["extraction_ref"], label=f"{label}.extraction_ref")
        extraction = _mapping(item["extraction"], label=f"{label}.extraction")
        findings = _list(extraction.get("findings"), label=f"{label}.extraction.findings")
        sources = _list(extraction.get("source_records", []), label=f"{label}.extraction.source_records")
        source_by_key = {}
        for s_index, source in enumerate(sources):
            s_label = f"{label}.extraction.source_records[{s_index}]"
            source_map = _mapping(source, label=s_label)
            key = _identifier(source_map.get("source_key"), label=f"{s_label}.source_key")
            _require(key not in source_by_key, "duplicate source_key in a case")
            source_by_key[key] = source_map

        links = _list(extraction.get("links", []), label=f"{label}.extraction.links")
        case_refs_by_source: dict[str, set] = {}
        case_extra_refs: set = set()
        for link_index, link in enumerate(links):
            l_label = f"{label}.extraction.links[{link_index}]"
            link_map = _mapping(link, label=l_label)
            relation = link_map.get("relation")
            if relation not in ("reports", "uses_dataset", "describes_experiment", "same_study", "same_dataset"):
                continue
            from_ref = link_map.get("from")
            to_ref = link_map.get("to")
            if not isinstance(from_ref, str) or not isinstance(to_ref, str):
                continue
            if relation in ("same_study", "same_dataset"):
                # A conservative positive link regardless of ``status``: applies to
                # every finding in the case, since it names the underlying entity.
                for ref in (from_ref, to_ref):
                    if ":" in ref:
                        case_extra_refs.add(ref)
                continue
            kind, _, src_key = from_ref.partition(":")
            if kind == "source" and src_key:
                case_refs_by_source.setdefault(src_key, set()).add(to_ref)

        case_finding_keys = []
        for f_index, raw_finding in enumerate(findings):
            f_label = f"{label}.extraction.findings[{f_index}]"
            finding = _mapping(raw_finding, label=f_label)
            finding_id = _identifier(finding.get("finding_id"), label=f"{f_label}.finding_id")
            key = f"{case_id}:{finding_id}"
            _require(key not in findings_by_key, "duplicate namespaced finding key")
            source_key = finding.get("source_key")
            if not isinstance(source_key, str) or not source_key.strip():
                source_key = None
            refs = set(case_refs_by_source.get(source_key, set())) | set(case_extra_refs)
            for field, kind in (("study_id", "study"), ("dataset_id", "dataset"), ("experiment_id", "experiment")):
                value = finding.get(field)
                if isinstance(value, str) and value.strip():
                    refs.add(f"{kind}:{value}")
            findings_by_key[key] = {
                "key": key,
                "case_id": case_id,
                "finding_id": finding_id,
                "extraction_ref": copy.deepcopy(extraction_ref),
                "source_key": source_key,
                "source_record": copy.deepcopy(source_by_key.get(source_key)),
                "record": copy.deepcopy(finding),
            }
            refs_by_key[key] = refs
            case_finding_keys.append(key)

        # Exact original-source document/revision identities for this case, used to
        # correlate two accepted extractions of the *same* source even when no
        # model link was proposed.  An alternate revision of one document artifact
        # is conservatively the same underlying source document.
        source_exact = set()
        source_doc = set()
        source_refs = _list(item.get("source_refs", []), label=f"{label}.source_refs")
        for s_index, source_ref in enumerate(source_refs):
            ref_map = _mapping(source_ref, label=f"{label}.source_refs[{s_index}]")
            project_id = ref_map.get("project_id")
            artifact_id = ref_map.get("artifact_id")
            revision_id = ref_map.get("revision_id")
            project = project_id if isinstance(project_id, str) and project_id.strip() else ""
            if isinstance(artifact_id, str) and artifact_id.strip():
                source_doc.add(f"{project}|{artifact_id}")
                if isinstance(revision_id, str) and revision_id.strip():
                    source_exact.add(f"{project}|{artifact_id}|{revision_id}")

        cases_by_id[case_id] = {
            "id": case_id,
            "extraction_ref": copy.deepcopy(extraction_ref),
            "source_refs": copy.deepcopy(item.get("source_refs", [])),
            "source_keys": sorted(source_by_key),
            "finding_keys": case_finding_keys,
        }
        source_exact_by_case[case_id] = source_exact
        source_doc_by_case[case_id] = source_doc
        ordered.append(case_id)
    return ordered, cases_by_id, findings_by_key, refs_by_key, source_exact_by_case, source_doc_by_case


def _correlation_groups(findings_by_key: dict, refs_by_key: dict, source_exact_by_case: dict, source_doc_by_case: dict):
    """Union-find findings that share a source document or an underlying entity.

    Connectivity comes from (a) the same case/source document, (b) any shared
    declared ``study``/``dataset``/``experiment`` (``proposed`` or ``confirmed``),
    and (c) the same exact original-source document revision -- or an alternate
    revision of the same document artifact -- across different cases.  A group
    with more than one finding is ``not_independent`` (shared underlying data is a
    hard prohibition); a singleton is ``unknown``.  Nothing is auto-labelled
    independent.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    keys = sorted(findings_by_key)
    for key in keys:
        find(key)
    # same source document
    by_case: dict[str, list] = {}
    for key in keys:
        by_case.setdefault(findings_by_key[key]["case_id"], []).append(key)
    for members in by_case.values():
        for other in members[1:]:
            union(members[0], other)
    # shared underlying entity ref
    by_ref: dict[str, list] = {}
    for key in keys:
        for ref in refs_by_key.get(key, set()):
            by_ref.setdefault(ref, []).append(key)
    for members in by_ref.values():
        for other in members[1:]:
            union(members[0], other)
    # same exact original-source revision, or an alternate revision of the same
    # document artifact, across different cases
    for identity_map in (source_exact_by_case, source_doc_by_case):
        by_identity: dict[str, list] = {}
        for key in keys:
            for identity in identity_map.get(findings_by_key[key]["case_id"], set()):
                by_identity.setdefault(identity, []).append(key)
        for members in by_identity.values():
            for other in members[1:]:
                union(members[0], other)

    grouped: dict[str, list] = {}
    for key in keys:
        grouped.setdefault(find(key), []).append(key)
    components = sorted((sorted(members) for members in grouped.values()), key=lambda m: m[0])
    groups = []
    group_of: dict[str, str] = {}
    for index, members in enumerate(components, start=1):
        group_id = f"G{index}"
        case_ids = sorted({findings_by_key[k]["case_id"] for k in members})
        underlying = sorted({ref for k in members for ref in refs_by_key.get(k, set())})
        if len(members) == 1:
            reason = "single_observation"
            independence = "unknown"
        elif len(case_ids) > 1 and underlying:
            reason = "shared_underlying_study_or_dataset"
            independence = "not_independent"
        elif len(case_ids) > 1:
            reason = "shared_source_document"
            independence = "not_independent"
        else:
            reason = "same_source_document"
            independence = "not_independent"
        for key in members:
            group_of[key] = group_id
        groups.append(
            {
                "group_id": group_id,
                "member_findings": members,
                "case_ids": case_ids,
                "underlying_refs": underlying,
                "independence": independence,
                "basis": reason,
            }
        )
    return groups, group_of


# --------------------------------------------------------------------------- #
# Synthesis payload
# --------------------------------------------------------------------------- #
def _validate_statements(value: object, findings_by_key: dict) -> list:
    items = _list(value, label="statements")
    out = []
    seen = set()
    for index, raw in enumerate(items):
        label = f"statements[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset(
                {"statement_id", "text", "classification", "evidence",
                 "applicability", "causal_limitations", "uncertainty", "status"}
            ),
        )
        statement_id = _identifier(item["statement_id"], label=f"{label}.statement_id")
        _require(statement_id not in seen, "duplicate statement_id")
        seen.add(statement_id)
        text = _string(item["text"], label=f"{label}.text")
        classification = _enum(item["classification"], label=f"{label}.classification", allowed=STATEMENT_CLASSIFICATIONS)
        evidence = _str_list(item["evidence"], label=f"{label}.evidence", allow_empty=False)
        for ref in evidence:
            _require(ref in findings_by_key, f"{label}.evidence names an unknown finding key")
        applicability = _applicability(item["applicability"], label=f"{label}.applicability")
        causal = _stated(item["causal_limitations"], label=f"{label}.causal_limitations", kind="str")
        uncertainty = _stated(item["uncertainty"], label=f"{label}.uncertainty", kind="str")
        status = _enum(item["status"], label=f"{label}.status", allowed=STATEMENT_STATUSES)
        declared = applicability["context"]
        # Preserve the ORIGINAL extraction context of every linked finding.
        source_context = {field: {"states": set(), "known_values": set()} for field in CONTEXT_FIELDS}
        source_limitations = []
        for ref in evidence:
            record = findings_by_key[ref]["record"]
            finding_context = record.get("context")
            if isinstance(finding_context, dict):
                for field in CONTEXT_FIELDS:
                    stated = finding_context.get(field)
                    if isinstance(stated, dict):
                        source_context[field]["states"].add(stated.get("state"))
                        if stated.get("state") == "known":
                            value = stated.get("value")
                            if isinstance(value, str):
                                source_context[field]["known_values"].add(value)
            limitations = record.get("limitations")
            if isinstance(limitations, dict):
                source_limitations.append({"finding": ref, "limitations": limitations})
        missing_original = [
            field
            for field in CONTEXT_FIELDS
            if ("missing" in source_context[field]["states"]) or ("unknown" in source_context[field]["states"])
        ]
        # A statement may not silently widen beyond a known original context: a
        # reported-evidence claim whose declared known value differs from every
        # known source value is blocked (an interpretation may state a scoped
        # extension under its own classification, but never as reported evidence).
        context_mismatch = [
            field
            for field in CONTEXT_FIELDS
            if declared[field]["state"] == "known"
            and source_context[field]["known_values"]
            and declared[field]["value"] not in source_context[field]["known_values"]
        ]
        blocking_reasons = []
        if missing_original:
            blocking_reasons.append("missing_original_context")
        if context_mismatch and classification == "reported_evidence":
            blocking_reasons.append("context_mismatch")
        declared_missing = [field for field in CONTEXT_FIELDS if declared[field]["state"] in ("missing", "unknown")]
        if status == "approved" and (blocking_reasons or declared_missing):
            raise _fail(
                f"{label} is approved but has unresolved applicable-context issues"
            )
        out.append(
            {
                "statement_id": statement_id,
                "text": text,
                "classification": classification,
                "evidence": evidence,
                "applicability": declared,
                "applicability_note": applicability["note"],
                "causal_limitations": causal,
                "uncertainty": uncertainty,
                "status": status,
                # Original context is preserved beside the statement; consequential
                # missing/mismatched original context blocks the statement so it is
                # never silently generalizable (POL-P2).
                "source_context": {
                    field: {
                        "states": sorted(state for state in source_context[field]["states"] if state is not None),
                        "known_values": sorted(source_context[field]["known_values"]),
                    }
                    for field in CONTEXT_FIELDS
                },
                "source_limitations": source_limitations,
                "missing_original_context": missing_original,
                "context_mismatch": context_mismatch,
                "blocked": bool(blocking_reasons),
                "blocking_reasons": blocking_reasons,
            }
        )
    return out


def _validate_dispositions(value: object, findings_by_key: dict, statements: list) -> list:
    items = _list(value, label="dispositions")
    out = []
    seen = set()
    incorporated = set()
    for index, raw in enumerate(items):
        label = f"dispositions[{index}]"
        item = _keys(raw, label=label, required=frozenset({"finding", "disposition", "rationale"}))
        finding = _identifier(item["finding"], label=f"{label}.finding")
        _require(finding in findings_by_key, f"{label}.finding names an unknown finding key")
        _require(finding not in seen, "duplicate disposition for a finding")
        seen.add(finding)
        disposition = _enum(item["disposition"], label=f"{label}.disposition", allowed=FINDING_DISPOSITIONS)
        rationale = _string(item["rationale"], label=f"{label}.rationale")
        if disposition == "incorporated":
            incorporated.add(finding)
        out.append({"finding": finding, "disposition": disposition, "rationale": rationale})
    missing = set(findings_by_key) - seen
    _require(
        not missing,
        "every accepted finding must be explicitly dispositioned (contradictory/negative/insufficient evidence cannot be omitted)",
    )
    # A statement may trace to any *retained scientific* disposition: incorporated
    # evidence, genuine disagreement, species/outcome difference, null evidence, or
    # insufficient data.  Only `not_used`/`out_of_scope` may not be traced to.
    retained = {
        finding
        for finding, disposition in ((entry["finding"], entry["disposition"]) for entry in out)
        if disposition in ("incorporated", "genuine_disagreement", "species_or_outcome_difference", "null_evidence", "insufficient_data")
    }
    linked = {ref for statement in statements for ref in statement["evidence"]}
    not_retained = linked - retained
    _require(
        not not_retained,
        "a statement traces to a finding dispositioned 'not_used' or 'out_of_scope'",
    )
    # Every incorporated finding must actually be traced to a statement.
    unused = incorporated - linked
    _require(
        not unused,
        "a finding is dispositioned 'incorporated' but no statement traces to it",
    )
    return out


def _validate_independence(value: object, findings_by_key: dict, group_of: dict) -> list:
    items = _list(value, label="independence")
    out = []
    seen = set()
    for index, raw in enumerate(items):
        label = f"independence[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset(
                {"adjudication_id", "reviewed_a", "reviewed_b", "underlying_a",
                 "underlying_b", "conclusion", "basis", "rationale", "adjudicated_by"}
            ),
        )
        adjudication_id = _identifier(item["adjudication_id"], label=f"{label}.adjudication_id")
        _require(adjudication_id not in seen, "duplicate adjudication_id")
        seen.add(adjudication_id)
        reviewed_a = _identifier(item["reviewed_a"], label=f"{label}.reviewed_a")
        reviewed_b = _identifier(item["reviewed_b"], label=f"{label}.reviewed_b")
        _require(reviewed_a in findings_by_key, f"{label}.reviewed_a names an unknown finding key")
        _require(reviewed_b in findings_by_key, f"{label}.reviewed_b names an unknown finding key")
        _require(reviewed_a != reviewed_b, f"{label} compares a finding with itself")
        underlying_a = _string(item["underlying_a"], label=f"{label}.underlying_a")
        underlying_b = _string(item["underlying_b"], label=f"{label}.underlying_b")
        conclusion = _enum(item["conclusion"], label=f"{label}.conclusion", allowed=INDEPENDENCE_CONCLUSIONS)
        basis = _string(item["basis"], label=f"{label}.basis")
        rationale = _string(item["rationale"], label=f"{label}.rationale")
        actor = _keys(
            item["adjudicated_by"],
            label=f"{label}.adjudicated_by",
            required=frozenset({"actor_kind", "actor_id"}),
            optional=frozenset({"confirmation_ref"}),
        )
        actor_kind = _enum(actor["actor_kind"], label=f"{label}.adjudicated_by.actor_kind", allowed=ACTOR_KINDS)
        actor_id = _identifier(actor["actor_id"], label=f"{label}.adjudicated_by.actor_id")
        confirmation_ref = actor.get("confirmation_ref")
        if confirmation_ref is not None:
            confirmation_ref = _identifier(confirmation_ref, label=f"{label}.adjudicated_by.confirmation_ref")
        if conclusion == "independent":
            _require(
                underlying_a != underlying_b,
                f"{label} claims independent support but the underlying sources are not distinct",
            )
            _require(
                actor_kind == "user" or (actor_kind == "agent" and confirmation_ref),
                f"{label} claims independent support but is not user-adjudicated (or human-confirmed)",
            )
            _require(
                group_of[reviewed_a] != group_of[reviewed_b],
                f"{label} claims independent support for findings sharing a correlation group (shared underlying data is prohibited)",
            )
        out.append(
            {
                "adjudication_id": adjudication_id,
                "reviewed_a": reviewed_a,
                "reviewed_b": reviewed_b,
                "underlying_a": underlying_a,
                "underlying_b": underlying_b,
                "conclusion": conclusion,
                "basis": basis,
                "rationale": rationale,
                "adjudicated_by": {"actor_kind": actor_kind, "actor_id": actor_id, "confirmation_ref": confirmation_ref},
            }
        )
    return out


def validate_synthesis(payload, *, cases):
    """Validate and normalize one untrusted synthesis payload over accepted cases.

    ``cases`` is the normalized ``require_batch()['cases']`` list; each case has
    ``id``, ``extraction_ref``, ``source_refs``, and an ``extraction`` mapping with
    findings/source_records/links.  ``payload`` is the synthesis content::

        {
          "schema_version": 1,
          "memo": "<string>",
          "statements":  [ {statement_id, text, classification, evidence,
                            applicability, causal_limitations, uncertainty, status} ],
          "dispositions":[ {finding, disposition, rationale} ],
          "independence":[ {adjudication_id, reviewed_a, reviewed_b,
                            underlying_a, underlying_b, conclusion, basis,
                            rationale, adjudicated_by} ],   # optional
          "gaps": ["<string>"],                              # optional
          "limitations": {state, value}                      # optional
        }

    Returns a deterministic, JSON-portable ``dict`` (see
    :data:`VALIDATION_SCOPE`) or raises :class:`SynthesisError`.  Every accepted
    finding is preserved automatically; every finding must be dispositioned; a
    statement may only trace to incorporated findings; and ``independent`` support
    requires an explicit adjudication with distinct underlying sources.
    """
    _preflight(payload, "synthesis payload")
    _preflight(cases, "cases")
    _require(isinstance(payload, dict), "synthesis payload must be a JSON object")
    _require_no_banned(payload, label="synthesis payload")
    item = _keys(
        payload,
        label="synthesis payload",
        required=frozenset({"schema_version", "memo", "statements", "dispositions"}),
        optional=frozenset({"independence", "gaps", "limitations"}),
    )
    version = item["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        f"schema_version must be the integer {SCHEMA_VERSION}",
    )
    memo = _string(item["memo"], label="memo")

    _ordered, cases_by_id, findings_by_key, refs_by_key, source_exact_by_case, source_doc_by_case = _index_cases(cases)
    groups, group_of = _correlation_groups(findings_by_key, refs_by_key, source_exact_by_case, source_doc_by_case)
    statements = _validate_statements(item["statements"], findings_by_key)
    dispositions = _validate_dispositions(item["dispositions"], findings_by_key, statements)
    independence = _validate_independence(item.get("independence", []), findings_by_key, group_of)
    gaps = _str_list(item.get("gaps", []), label="gaps")
    limitations = item.get("limitations")
    if limitations is not None:
        limitations = _stated(limitations, label="limitations")

    by_disposition: dict[str, list] = {}
    for entry in dispositions:
        by_disposition.setdefault(entry["disposition"], []).append(entry["finding"])
    for values in by_disposition.values():
        values.sort()

    findings = []
    for key in sorted(findings_by_key):
        record = findings_by_key[key]
        findings.append(record)

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "synthesis",
        "memo": memo,
        "cases": [cases_by_id[case_id] for case_id in _ordered],
        "findings": findings,
        "correlation_groups": groups,
        "statements": statements,
        "dispositions": dispositions,
        "by_disposition": by_disposition,
        "independence": independence,
        "gaps": gaps,
        "limitations": limitations,
        "notice": SYNTHESIS_NOTICE,
        "scope": VALIDATION_SCOPE,
    }


# --------------------------------------------------------------------------- #
# Framework payload
# --------------------------------------------------------------------------- #
def _synthesis_finding_keys(synthesis: object) -> set:
    """Accept a validated synthesis result, a synthesis payload, or a raw key set."""
    if isinstance(synthesis, dict) and isinstance(synthesis.get("findings"), list):
        keys = set()
        for record in synthesis["findings"]:
            if isinstance(record, dict) and "key" in record:
                keys.add(record["key"])
        return keys
    if isinstance(synthesis, (set, frozenset, list, tuple)) and all(isinstance(k, str) for k in synthesis):
        return set(synthesis)
    raise _fail("synthesis must be a validated synthesis result, a payload, or a set of finding keys")


def _testable_implication(value: object, *, label: str):
    """Return ``(normalized, applicable_ok)``.

    A concept must carry either a meaningful testable implication (a non-empty
    string) or an explicit ``not_applicable`` value with a reason.
    """
    if isinstance(value, str) and value.strip():
        return value, True
    if isinstance(value, dict) and value.get("state") == "not_applicable":
        reason = _string(value.get("reason"), label=f"{label}.reason")
        return {"state": "not_applicable", "reason": reason}, True
    return None, False


def _validate_concepts(value: object, finding_keys: set) -> list:
    items = _list(value, label="concepts", allow_empty=False)
    out = []
    seen = set()
    for index, raw in enumerate(items):
        label = f"concepts[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"concept_id", "definition", "role", "original_support", "applicability_limits"}),
            optional=frozenset({"counterevidence", "counterevidence_note", "testable_implication", "status", "justification", "novelty", "notes"}),
        )
        concept_id = _identifier(item["concept_id"], label=f"{label}.concept_id")
        _require(concept_id not in seen, "duplicate concept_id")
        seen.add(concept_id)
        definition = _string(item["definition"], label=f"{label}.definition")
        role = _string(item["role"], label=f"{label}.role")
        original_support = _str_list(item["original_support"], label=f"{label}.original_support")
        for ref in original_support:
            _require(ref in finding_keys, f"{label}.original_support names an unknown finding key")
        counterevidence = _str_list(item.get("counterevidence", []), label=f"{label}.counterevidence")
        for ref in counterevidence:
            _require(ref in finding_keys, f"{label}.counterevidence names an unknown finding key")
        counterevidence_note = item.get("counterevidence_note")
        if counterevidence_note is not None:
            counterevidence_note = _string(counterevidence_note, label=f"{label}.counterevidence_note")
        # POL-P1 counterevidence safeguard: empty counterevidence is allowed only
        # with an explicit no-counterevidence rationale; a missing safeguard leaves
        # the concept unclassified and can never be established.
        counterevidence_ok = bool(counterevidence) or bool(counterevidence_note)
        applicability_limits = _string(item["applicability_limits"], label=f"{label}.applicability_limits")
        implication, implication_ok = _testable_implication(item.get("testable_implication"), label=f"{label}.testable_implication")
        novelty = item.get("novelty", "unclear")
        novelty = _enum(novelty, label=f"{label}.novelty", allowed=NOVELTY_VALUES)
        requested = item.get("status", "proposed")
        requested = _enum(requested, label=f"{label}.status", allowed=CONCEPT_STATUSES)
        admitted = bool(definition and role and applicability_limits and original_support and implication_ok and counterevidence_ok)
        if requested == "established" and not admitted:
            raise _fail(f"{label} cannot be established without its required safeguards")
        if not admitted:
            status = "unclassified"
        elif requested == "established":
            _string(item.get("justification"), label=f"{label}.justification")
            status = "established"
        else:
            status = "proposed"
        out.append(
            {
                "concept_id": concept_id,
                "definition": definition,
                "role": role,
                "original_support": original_support,
                "counterevidence": counterevidence,
                "counterevidence_note": counterevidence_note,
                "counterevidence_safeguard": counterevidence_ok,
                "applicability_limits": applicability_limits,
                "testable_implication": implication,
                "novelty": novelty,
                "status": status,
                "justification": item.get("justification"),
                "notes": item.get("notes"),
                "admitted": admitted,
            }
        )
    return out


def _validate_relationships(value: object, concepts: list, finding_keys: set) -> list:
    concept_by_id = {c["concept_id"]: c for c in concepts}
    items = _list(value, label="relationships")
    out = []
    seen = set()
    for index, raw in enumerate(items):
        label = f"relationships[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"relationship_id", "from_concept", "to_concept", "directionality", "scope"}),
            optional=frozenset({"evidence", "open_questions", "status", "justification"}),
        )
        relationship_id = _identifier(item["relationship_id"], label=f"{label}.relationship_id")
        _require(relationship_id not in seen, "duplicate relationship_id")
        seen.add(relationship_id)
        from_concept = _identifier(item["from_concept"], label=f"{label}.from_concept")
        to_concept = _identifier(item["to_concept"], label=f"{label}.to_concept")
        _require(from_concept in concept_by_id, f"{label}.from_concept is not a declared concept")
        _require(to_concept in concept_by_id, f"{label}.to_concept is not a declared concept")
        _require(from_concept != to_concept, f"{label} relates a concept to itself")
        directionality = _string(item["directionality"], label=f"{label}.directionality")
        scope = _string(item["scope"], label=f"{label}.scope")
        evidence = _str_list(item.get("evidence", []), label=f"{label}.evidence")
        for ref in evidence:
            _require(ref in finding_keys, f"{label}.evidence names an unknown finding key")
        open_questions = _str_list(item.get("open_questions", []), label=f"{label}.open_questions")
        requested = item.get("status", "proposed")
        requested = _enum(requested, label=f"{label}.status", allowed=CONCEPT_STATUSES)
        if requested == "established":
            _require(
                concept_by_id[from_concept]["admitted"] and concept_by_id[to_concept]["admitted"],
                f"{label} cannot be established from unclassified concepts",
            )
            _require(
                evidence,
                f"{label} cannot be established without non-empty traceable support",
            )
            _string(item.get("justification"), label=f"{label}.justification")
            status = "established"
        else:
            status = requested
        out.append(
            {
                "relationship_id": relationship_id,
                "from_concept": from_concept,
                "to_concept": to_concept,
                "directionality": directionality,
                "scope": scope,
                "evidence": evidence,
                "open_questions": open_questions,
                "status": status,
                "justification": item.get("justification"),
            }
        )
    return out


def _validate_hypotheses(value: object, concepts: list, finding_keys: set) -> list:
    concept_ids = {c["concept_id"] for c in concepts}
    items = _list(value, label="hypotheses")
    out = []
    seen = set()
    for index, raw in enumerate(items):
        label = f"hypotheses[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"hypothesis_id", "statement"}),
            optional=frozenset({"concepts", "evidence", "status"}),
        )
        hypothesis_id = _identifier(item["hypothesis_id"], label=f"{label}.hypothesis_id")
        _require(hypothesis_id not in seen, "duplicate hypothesis_id")
        seen.add(hypothesis_id)
        statement = _string(item["statement"], label=f"{label}.statement")
        concept_refs = _str_list(item.get("concepts", []), label=f"{label}.concepts")
        for ref in concept_refs:
            _require(ref in concept_ids, f"{label}.concepts names an unknown concept")
        evidence = _str_list(item.get("evidence", []), label=f"{label}.evidence")
        for ref in evidence:
            _require(ref in finding_keys, f"{label}.evidence names an unknown finding key")
        # A hypothesis is never admitted evidence: it stays a proposal.
        status = _enum(item.get("status", "proposed"), label=f"{label}.status", allowed=("proposed",))
        out.append(
            {
                "hypothesis_id": hypothesis_id,
                "statement": statement,
                "concepts": concept_refs,
                "evidence": evidence,
                "status": status,
            }
        )
    return out


def validate_framework(payload, *, synthesis):
    """Validate and normalize one untrusted scientific-framework payload.

    ``synthesis`` is a validated :func:`validate_synthesis` result (or its
    ``findings``/a set of finding keys) that the framework must trace to.  The
    framework is separate from the outline, the early scope map, and the Jev
    rubric, and carries the mandatory POL-P1 criteria plus a shared argument
    record.  Returns a JSON-portable ``dict`` or raises :class:`SynthesisError`.
    """
    _preflight(payload, "framework payload")
    _require(isinstance(payload, dict), "framework payload must be a JSON object")
    _require_no_banned(payload, label="framework payload")
    finding_keys = _synthesis_finding_keys(synthesis)
    item = _keys(
        payload,
        label="framework payload",
        required=frozenset({"schema_version", "title", "contribution", "criteria", "concepts"}),
        optional=frozenset({"relationships", "hypotheses", "open_questions", "limitations"}),
    )
    version = item["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        f"schema_version must be the integer {SCHEMA_VERSION}",
    )
    title = _string(item["title"], label="title")
    contribution = _string(item["contribution"], label="contribution")
    criteria_raw = _keys(
        item["criteria"],
        label="criteria",
        required=frozenset(FRAMEWORK_CRITERIA),
    )
    criteria = {name: _string(criteria_raw[name], label=f"criteria.{name}") for name in FRAMEWORK_CRITERIA}
    concepts = _validate_concepts(item["concepts"], finding_keys)
    relationships = _validate_relationships(item.get("relationships", []), concepts, finding_keys)
    hypotheses = _validate_hypotheses(item.get("hypotheses", []), concepts, finding_keys)
    open_questions = _str_list(item.get("open_questions", []), label="open_questions")
    limitations = item.get("limitations")
    if limitations is not None:
        limitations = _stated(limitations, label="limitations")
    unclassified = sorted(c["concept_id"] for c in concepts if not c["admitted"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "framework",
        "title": title,
        "contribution": contribution,
        "criteria": criteria,
        "concepts": concepts,
        "relationships": relationships,
        "hypotheses": hypotheses,
        "open_questions": open_questions,
        "limitations": limitations,
        "unclassified_concepts": unclassified,
        "notice": FRAMEWORK_NOTICE,
        "scope": VALIDATION_SCOPE,
    }


# --------------------------------------------------------------------------- #
# Outline payload
# --------------------------------------------------------------------------- #
def validate_outline(payload, *, framework, synthesis):
    """Validate and normalize one untrusted manuscript-outline payload.

    ``framework``/``synthesis`` are validated results the outline must trace to.
    The outline has actual ordered sections and explicit
    concept/relationship/statement dependencies, so a later change to a
    definition, relationship, or statement can flag the affected sections.
    Returns a JSON-portable ``dict`` or raises :class:`SynthesisError`.
    """
    _preflight(payload, "outline payload")
    _require(isinstance(payload, dict), "outline payload must be a JSON object")
    _require_no_banned(payload, label="outline payload")
    item = _keys(
        payload,
        label="outline payload",
        required=frozenset({"schema_version", "title", "sections"}),
    )
    version = item["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        f"schema_version must be the integer {SCHEMA_VERSION}",
    )
    title = _string(item["title"], label="title")
    concept_ids = {c["concept_id"] for c in framework.get("concepts", [])}
    relationship_ids = {r["relationship_id"] for r in framework.get("relationships", [])}
    statement_ids = {s["statement_id"] for s in synthesis.get("statements", [])}

    sections_raw = _list(item["sections"], label="sections", allow_empty=False)
    sections = []
    orders = set()
    seen_ids = set()
    for index, raw in enumerate(sections_raw):
        label = f"sections[{index}]"
        section = _keys(
            raw,
            label=label,
            required=frozenset({"section_id", "order", "title"}),
            optional=frozenset({"concept_dependencies", "relationship_dependencies", "statement_dependencies", "purpose"}),
        )
        section_id = _identifier(section["section_id"], label=f"{label}.section_id")
        _require(section_id not in seen_ids, "duplicate section_id")
        seen_ids.add(section_id)
        order = section["order"]
        _require(isinstance(order, int) and not isinstance(order, bool) and order >= 1, f"{label}.order must be a positive integer")
        _require(order not in orders, "duplicate section order")
        orders.add(order)
        section_title = _string(section["title"], label=f"{label}.title")
        concept_dependencies = _str_list(section.get("concept_dependencies", []), label=f"{label}.concept_dependencies")
        for ref in concept_dependencies:
            _require(ref in concept_ids, f"{label}.concept_dependencies names an unknown concept")
        relationship_dependencies = _str_list(section.get("relationship_dependencies", []), label=f"{label}.relationship_dependencies")
        for ref in relationship_dependencies:
            _require(ref in relationship_ids, f"{label}.relationship_dependencies names an unknown relationship")
        statement_dependencies = _str_list(section.get("statement_dependencies", []), label=f"{label}.statement_dependencies")
        for ref in statement_dependencies:
            _require(ref in statement_ids, f"{label}.statement_dependencies names an unknown statement")
        purpose = section.get("purpose")
        if purpose is not None:
            purpose = _string(purpose, label=f"{label}.purpose")
        sections.append(
            {
                "section_id": section_id,
                "order": order,
                "title": section_title,
                "purpose": purpose,
                "concept_dependencies": concept_dependencies,
                "relationship_dependencies": relationship_dependencies,
                "statement_dependencies": statement_dependencies,
            }
        )
    total = len(sections)
    _require(orders == set(range(1, total + 1)), "section orders must be exactly 1..N without gaps")
    sections.sort(key=lambda s: s["order"])

    dependency_index: dict[str, list] = {"concepts": {}, "relationships": {}, "statements": {}}
    for section in sections:
        for concept_id in section["concept_dependencies"]:
            dependency_index["concepts"].setdefault(concept_id, []).append(section["section_id"])
        for relationship_id in section["relationship_dependencies"]:
            dependency_index["relationships"].setdefault(relationship_id, []).append(section["section_id"])
        for statement_id in section["statement_dependencies"]:
            dependency_index["statements"].setdefault(statement_id, []).append(section["section_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "outline",
        "title": title,
        "sections": sections,
        "dependency_index": dependency_index,
        "notice": OUTLINE_NOTICE,
        "scope": VALIDATION_SCOPE,
    }


# --------------------------------------------------------------------------- #
# Synthetic example builders (fictional; never real evidence)
# --------------------------------------------------------------------------- #
def _ref(artifact_id, revision_id="r1", project_id="P-0001"):
    return {"project_id": project_id, "artifact_id": artifact_id, "revision_id": revision_id}


def _known(value):
    return {"state": "known", "value": value}


def _example_context(**overrides):
    base = {
        "species": _known("mouse"),
        "population": _known("n=8 mice"),
        "assay": _known("tumor volume"),
        "outcome": _known("tumor volume"),
        "conditions": _known("vehicle comparison"),
    }
    base.update(overrides)
    return base


def _example_finding(finding_id, source_key, snippet, **overrides):
    record = {
        "finding_id": finding_id,
        "source_key": source_key,
        "passage_id": "p0001",
        "snippet": snippet,
        "statement": "statement for %s" % finding_id,
        "attribution": "direct_observation",
        "citation_flag": "own_result",
        "context": _example_context(),
        "secondary_warning": {"state": "not_applicable", "value": None},
        "limitations": _known("single model system"),
        "numeric_details": [],
    }
    record.update(overrides)
    return record


def synthesis_example_cases():
    """Return synthetic Step 09-shaped cases.

    Reports ``A10`` and ``A11`` both report the **same** study ``S1`` (one proposed
    and one confirmed link), so they must never be counted independent; ``B7``
    reports a separate study ``S2`` and stays ``unknown``.  Local finding ids
    collide on purpose (``F1``) to exercise namespacing.  Never real evidence.
    """
    return [
        {
            "id": "A10",
            "extraction_ref": _ref("A10", "e1"),
            "source_refs": [_ref("A10", "a1")],
            "extraction": {
                "source_records": [{"source_key": "docA10", "source_kind": "document", "label": "Report A10"}],
                "findings": [
                    _example_finding("F1", "docA10", "reduced tumor volume by 12.5 mm3",
                                     numeric_details=[{"name": "tumor volume",
                                                       "value": _known(12.5),
                                                       "unit": _known("mm3"),
                                                       "denominator": _known("per mouse")}]),
                ],
                "links": [{"from": "source:docA10", "to": "study:S1", "relation": "reports", "status": "confirmed"}],
            },
        },
        {
            "id": "A11",
            "extraction_ref": _ref("A11", "e1"),
            "source_refs": [_ref("A11", "a1")],
            "extraction": {
                "source_records": [{"source_key": "docA11", "source_kind": "document", "label": "Report A11"}],
                "findings": [
                    _example_finding("F1", "docA11", "did not reduce tumor volume",
                                     context=_example_context(conditions=_known("vehicle comparison")),
                                     limitations=_known("null result in a second report of study S1")),
                ],
                "links": [{"from": "source:docA11", "to": "study:S1", "relation": "reports", "status": "proposed"}],
            },
        },
        {
            "id": "B7",
            "extraction_ref": _ref("B7", "e1"),
            "source_refs": [_ref("B7", "a1")],
            "extraction": {
                "source_records": [{"source_key": "docB7", "source_kind": "document", "label": "Report B7"}],
                "findings": [
                    _example_finding("F1", "docB7", "reduced tumor volume in an independent cohort",
                                     study_id="S2"),
                ],
                "links": [{"from": "source:docB7", "to": "study:S2", "relation": "reports", "status": "confirmed"}],
            },
        },
    ]


def synthesis_example_payload():
    """Return a valid **RAW** synthesis payload over :func:`synthesis_example_cases`.

    Every accepted finding is dispositioned: ``A10:F1`` and ``B7:F1`` are
    incorporated; the null ``A11:F1`` is retained as ``null_evidence`` (never a
    refutation and never silently dropped).  Never real evidence.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "memo": "Compound Z effect, separating measurement from a null report and stating uncertainty.",
        "statements": [
            {
                "statement_id": "S-1",
                "text": "Compound Z reduced tumor volume in a mouse model.",
                "classification": "reported_evidence",
                "evidence": ["A10:F1", "B7:F1"],
                "applicability": _example_context(),
                "causal_limitations": _known("association in a single model; no causal mechanism established"),
                "uncertainty": _known("limited to the reported model systems"),
                "status": "proposed",
            },
            {
                "statement_id": "S-2",
                "text": "A second report of the same study did not observe the reduction.",
                "classification": "reported_evidence",
                "evidence": ["A11:F1"],
                "applicability": _example_context(),
                "causal_limitations": _known("same study; not an independent replication"),
                "uncertainty": _known("null result retained, not treated as refutation"),
                "status": "proposed",
            },
            {
                "statement_id": "S-3",
                "text": "A shared upstream mechanism may explain the divergent reports.",
                "classification": "hypothesis",
                "evidence": ["A10:F1"],
                "applicability": _example_context(),
                "causal_limitations": _known("proposed only; not established"),
                "uncertainty": _known("untested"),
                "status": "proposed",
            },
        ],
        "dispositions": [
            {"finding": "A10:F1", "disposition": "incorporated", "rationale": "direct observation used in the memo"},
            {"finding": "A11:F1", "disposition": "null_evidence", "rationale": "null result retained; not a refutation"},
            {"finding": "B7:F1", "disposition": "incorporated", "rationale": "separate study used for the range of evidence"},
        ],
        "independence": [
            {
                "adjudication_id": "IND-1",
                "reviewed_a": "A10:F1",
                "reviewed_b": "B7:F1",
                "underlying_a": "study:S1",
                "underlying_b": "study:S2",
                "conclusion": "independent",
                "basis": "the two findings come from demonstrably distinct studies",
                "rationale": "reviewed the study attribution in the accepted extraction",
                "adjudicated_by": {
                    "actor_kind": "user",
                    "actor_id": "user:reviewer-1",
                    "confirmation_ref": None,
                },
            }
        ],
        "gaps": ["no independent replication of the B7 result"],
        "limitations": _known("synthetic fixture; not evidence"),
    }


def synthesis_example():
    """Return the validated synthesis result for the synthetic example."""
    return validate_synthesis(synthesis_example_payload(), cases=synthesis_example_cases())


def framework_example(synthesis=None):
    """Return a validated framework for the synthetic example."""
    synthesis = synthesis or synthesis_example()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "title": "Compound Z tumor-effect framework",
        "contribution": "Organizes the reports around one shared study and one separate study, without forcing a novel framework.",
        "criteria": {
            "contribution": "A concept must add an organizing relationship not already stated in any single report.",
            "admission": "A concept needs a definition, a role, original support, counterevidence, and applicability limits.",
            "support": "Support is the accepted finding keys tied to the concept; a null stays counterevidence.",
            "revision": "Changed definitions or relationships reopen dependent outline sections.",
        },
        "concepts": [
            {
                "concept_id": "C1",
                "definition": "Compound Z tumor-volume effect in mice.",
                "role": "The central measured outcome.",
                "original_support": ["A10:F1", "B7:F1"],
                "counterevidence": ["A11:F1"],
                "applicability_limits": "Mouse tumor models only; not human applicability.",
                "testable_implication": "A dose-response experiment would show a monotone tumor-volume reduction.",
                "status": "proposed",
                "novelty": "existing",
            },
            {
                "concept_id": "C2",
                "definition": "Report correlation by shared underlying study.",
                "role": "Explains why two reports are not independent confirmation.",
                "original_support": ["A10:F1", "A11:F1"],
                "counterevidence_note": "no counterevidence identified in the reviewed corpus",
                "applicability_limits": "Only for reports declaring a shared study/dataset.",
                "testable_implication": {"state": "not_applicable", "reason": "a methodological relation, not an empirical hypothesis"},
                "status": "proposed",
                "novelty": "existing",
            },
        ],
        "relationships": [
            {
                "relationship_id": "R1",
                "from_concept": "C2",
                "to_concept": "C1",
                "directionality": "shared-study reports constrain (do not confirm) the C1 effect",
                "scope": "within the reviewed corpus only",
                "evidence": ["A10:F1", "A11:F1"],
                "open_questions": ["whether the reports used the same cohort or different cohorts of S1"],
                "status": "proposed",
            }
        ],
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "statement": "A shared upstream mechanism explains the divergent reports.",
                "concepts": ["C1"],
                "evidence": ["A10:F1"],
                "status": "proposed",
            }
        ],
        "open_questions": ["Is there any independent replication of the effect?"],
        "limitations": _known("synthetic fixture; not evidence"),
    }
    return validate_framework(payload, synthesis=synthesis)


def outline_example(framework=None, synthesis=None):
    """Return a validated outline for the synthetic framework/synthesis."""
    synthesis = synthesis or synthesis_example()
    framework = framework or framework_example(synthesis)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "title": "Compound Z review outline",
        "sections": [
            {
                "section_id": "sec1",
                "order": 1,
                "title": "Background",
                "purpose": "Introduce the outcome.",
                "concept_dependencies": ["C1"],
            },
            {
                "section_id": "sec2",
                "order": 2,
                "title": "Evidence and correlation",
                "purpose": "Present the evidence map and its correlations.",
                "concept_dependencies": ["C1", "C2"],
                "relationship_dependencies": ["R1"],
                "statement_dependencies": ["S-1", "S-2"],
            },
            {
                "section_id": "sec3",
                "order": 3,
                "title": "Open questions",
                "purpose": "State unresolved questions.",
                "concept_dependencies": ["C2"],
                "relationship_dependencies": ["R1"],
            },
        ],
    }
    return validate_outline(payload, framework=framework, synthesis=synthesis)
