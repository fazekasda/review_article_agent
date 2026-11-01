"""Minimal, strict, JSON-portable extraction-evidence schema (Step 09, S9-SCHEMA).

One bounded outcome: turn one untrusted, model-produced extraction document into
a *normalized, structurally validated* mapping, or refuse it.  It never calls a
model, reads storage, resolves a workflow, or decides scientific truth.

Authority: ``docs/implementation/09-extraction.md`` deliverables 9-11 and
AC16-18, AC21-22; ``docs/specification.md`` invariants (no extraction from
summaries alone; missing data stays missing; separate source/study/dataset/
experiment/result identities; no global quality score; tables/supplements are
inputs); ``docs/contracts/01-identities.md`` I4/I6/I8; POL-P2/POL-OPS-*.

Non-claims: it **cannot** prove scientific correctness or completeness (a
human/quality audit still owns semantic omission/error detection); it invents no
default, threshold, score, unit, or value (missing stays ``missing``/``unknown``
with ``None``); it defines no exclusion semantics (a weak study is retained and
universal score/exclusion keys are rejected).

Public API: :data:`EXTRACTION_INSTRUCTIONS` (trusted grounding guidance for the
runner), :func:`validate_extraction`, :func:`extraction_example` (synthetic
reusable fixture; never real evidence).
"""

from __future__ import annotations

import copy
import math

from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError, canonical_json

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1
DOCUMENT_TYPE = "extraction"

#: Human-readable summary of exactly what structural validation guarantees.
VALIDATION_SCOPE = (
    "Structural, JSON-portable schema validation, exact original-snippet "
    "grounding, and explicit coverage/omission accounting only. It does not "
    "establish scientific correctness, completeness, appraisal, or that no "
    "finding was omitted; that remains a human/quality audit obligation."
)

#: Field-state vocabulary for a value that may genuinely be unavailable.
STATED_STATES = ("known", "missing", "unknown", "not_applicable")

#: I8 attribution classes (``EvidenceRecord.attribution``).
ATTRIBUTIONS = (
    "direct_observation",
    "author_interpretation",
    "secondary_account",
    "mixed",
    "unclear",
    "proposed_synthesis",
)

#: Citation-origin classification for a finding (``specification.md`` §44-50).
CITATION_FLAGS = (
    "own_result",
    "original_contribution",
    "secondary_account",
    "mixed",
    "unclear",
)

#: Link relations between distinct identity records.
LINK_RELATIONS = (
    "reports",
    "uses_dataset",
    "describes_experiment",
    "same_study",
    "same_dataset",
)

#: Link status describes the *strength of the model's proposal only*.  Even a
#: ``confirmed`` link is a model proposal: it cannot prove identity, and human
#: appraisal is still required.  Absence of a link means linkage is **unknown**,
#: never proven independence; only explicit proven metadata may assert
#: independence (I6: reports of one dataset are not independent confirmation).
LINK_STATUSES = ("proposed", "confirmed")

#: Statement carried with every normalized result about link semantics.
LINKAGE_NOTICE = (
    "Source/study/dataset/experiment links are model proposals, not proven identity; "
    "human appraisal is required. Unlinked records have unknown linkage and are never "
    "reported as independent by absence of a link."
)

#: Entity-ref namespaces kept deliberately separate (AC22).
REF_KINDS = ("source", "study", "dataset", "experiment", "finding")

#: Coverage dispositions for every required passage/asset id.
COVERAGE_DISPOSITIONS = ("finding", "no_finding", "omitted")

#: Context fields that MUST be carried at extraction (P2 / I4).
CONTEXT_FIELDS = ("species", "population", "assay", "outcome", "conditions")

#: Appraisal states; ``unassessed`` is an explicit missing check, not a pass.
APPRAISAL_STATES = ("assessed", "not_applicable", "unassessed")

#: Top-level keys that would encode an inappropriate universal quality score or
#: an automatic exclusion; they are rejected outright (AC21).
_BANNED_TOP_KEYS = frozenset(
    {"score", "quality_score", "global_score", "rating", "weight", "excluded", "exclusion"}
)

_REQUIRED_TOP_KEYS = frozenset({"schema_version", "summary", "source_records", "findings", "coverage"})
_OPTIONAL_TOP_KEYS = frozenset(
    {"studies", "datasets", "experiments", "links", "appraisal", "omissions", "limitations"}
)


class ExtractionError(ReviewAgentError):
    """A model extraction is malformed, ungrounded, or incompletely accounted.

    Messages are actionable and never embed original passage text or model
    output: they name the structural defect and the stable identifier involved.
    """


# --------------------------------------------------------------------------- #
# Trusted instruction (included verbatim with the original passages at runtime)
# --------------------------------------------------------------------------- #
EXTRACTION_INSTRUCTIONS = """\
You extract evidence from the ORIGINAL passages supplied with this request. Return ONE JSON object only.

GROUNDING (hard rules)
- Every finding MUST cite a passage_id that appears in the supplied passages and a `snippet` that is an EXACT substring of that passage's original text. Never paraphrase, translate, clean up, correct, abbreviate, or re-order the snippet. If you cannot find an exact snippet, do not report the finding; record the passage as omitted with a reason.
- Never extract from a summary, abstract-only context, prior model output, or Jev decisions. The supplied original text is the only source.
- Preserve negation. "did not reduce", "no effect", "not significant", "failed to" MUST remain verbatim in the snippet; never drop a negative.
- Do not upgrade attribution. A claim stated by the authors is `author_interpretation`; a result measured in this report is `direct_observation`; a cited third-party claim is `secondary_account`. Never present author spin as a direct observation, and never present a secondary account as verified primary support.

MISSING VALUES (never invent)
- Represent every context field, number, unit, and denominator as {"state": ..., "value": ...}. `state` is one of: known, missing, unknown, not_applicable.
- When a value is not stated in the original, use state missing or unknown and value null. NEVER guess a value, unit, denominator, species, sample size, or effect size. A missing value is not zero and not a null result.
- A null/negative result in the original is `known` data (e.g. "no significant difference"); do not silently convert a reported null into a missing value, and do not treat a null as a refutation.

NUMBERS
- For each numeric detail give the reported value, its unit, and its denominator (e.g. per 1000, per 100 cells, total N). If the original gives a value without a unit or denominator, mark that part missing rather than inventing one.
- Keep the number exactly as reported; do not round, convert, or normalise.
- TYPES: a known `value.value` MUST be a JSON number, never a string: write {"state": "known", "value": 15.35}, NOT {"state": "known", "value": "15.35"}. Units such as "%" belong in `unit`, not in the value.
- QUALIFIED VALUES: a range ("2-3"), inequality ("P<0.01", "at least twenty"), approximation ("~5"), list ("0, 10, 20, and 40"), or a number written in words ("Thirty") is NOT a single known number. Either give one numeric detail per stated number (for a list), or use {"state": "not_applicable", "value": null} and copy the exact original text into `reported_as`. `reported_as` MUST be an exact substring of the finding's passage. Never silently turn a qualified value into a plain number.

CONTEXT (mandatory per finding)
- Always fill species, population, assay, outcome, and conditions; use missing/unknown/not_applicable where the original does not state them. A finding whose applicable context is not carried cannot support a generalised claim.

ATTRIBUTION AND CITATION
- attribution: direct_observation | author_interpretation | secondary_account | mixed | unclear | proposed_synthesis.
- citation_flag classifies the cited support: own_result | original_contribution | secondary_account | mixed | unclear.
- secondary_warning: {"state": "known", "value": true} when a tracing/origin limit applies; otherwise state known false or not_applicable.

COVERAGE AND OMISSIONS (completeness, not just quote correctness)
- `coverage` MUST account for EVERY supplied passage_id and every declared asset_id, exactly once, with disposition finding | no_finding | omitted.
- `finding` requires the matching finding_id. `omitted` requires a non-empty reason (e.g. not_scientific_content, table_not_acquired, unreadable, missing_context). An omitted item is a known omission for a later human gate -- it is never silently dropped.
- `omissions` records additional known omissions/escalations that are not a supplied passage id. Its optional `target_kind` is ONLY "passage" or "asset" (with the matching `target_id`); for a topic-level omission (e.g. a pathway component no passage addresses), omit both `target_kind` and `target_id` and explain it in `note`.

DISTINCT IDENTITIES (do not conflate)
- source_records: the document(s)/report(s) actually read; each has a unique source_key.
- studies, datasets, experiments: separate identity namespaces with unique ids.
- findings: result/finding ids in their own namespace, each tied to source_key + passage_id.
- `links` express relations (reports -> study, uses_dataset -> dataset, describes_experiment -> experiment, same_study, same_dataset) with status proposed|confirmed. Both are model proposals and neither proves identity; a human appraisal is still required. Absence of a link means linkage is UNKNOWN, never proven independence; only explicit proven metadata may assert independence. Multiple reports of one dataset or study are NOT independent confirmation.

APPRAISAL
- Report only the configured appraisal dimensions, each with a judgement; use state unassessed when not assessed. Do NOT output any numeric score or global quality rating. A weak study is retained with its limitations -- never excluded automatically.

OUTPUT
- Keys: schema_version (1, integer), summary (readable string), source_records, findings, coverage, and optionally studies, datasets, experiments, links, appraisal, omissions, limitations.
- Use the raw example (EXTRACTION_OUTPUT_EXAMPLE, grounded in EXTRACTION_EXAMPLE_PASSAGES) as the exact field-shape reference for source_records, findings, context, coverage, numeric_details, and appraisal.
- No commentary outside the JSON object.
"""


# --------------------------------------------------------------------------- #
# Small validation helpers
# --------------------------------------------------------------------------- #
def _fail(message: str) -> "ExtractionError":
    return ExtractionError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _fail(message)


def _is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


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
    # Required names are our own vocabulary; unknown names are caller-supplied and
    # are never echoed (privacy: no untrusted identifiers in diagnostics).
    _require(not missing, f"{label} is missing required keys: {sorted(missing)}")
    if not allow_extra:
        unknown = present - required - optional
        _require(not unknown, f"{label} has unknown keys")
    return value


def _string(value: object, *, label: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{label} must be a non-empty string")
    return value


def _identifier(value: object, *, label: str) -> str:
    return _string(value, label=label)


def _optional_string(value: object, *, label: str):
    """Return ``value`` when it is a non-empty string, or ``None`` when absent.

    Freeform note fields accept only a string or ``null``: an arbitrary object,
    number, list, or non-finite value is refused rather than passed through.
    """
    if value is None:
        return None
    return _string(value, label=label)


def _enum(value: object, *, label: str, allowed: tuple) -> str:
    _require(value in allowed, f"{label} must be one of {sorted(allowed)}")
    return value  # type: ignore[return-value]


def _stated(value: object, *, label: str, kind: str = "str") -> dict:
    """Validate a ``{state, value}`` pair; ``value`` is ``None`` unless known."""
    item = _keys(value, label=label, required=frozenset({"state", "value"}))
    state = _enum(item["state"], label=f"{label}.state", allowed=STATED_STATES)
    payload = item["value"]
    if state == "known":
        if kind == "str":
            _string(payload, label=f"{label}.value")
        elif kind == "number":
            _require(_is_number(payload), f"{label}.value must be a finite number when known")
        elif kind == "bool":
            _require(isinstance(payload, bool), f"{label}.value must be a boolean when known")
        else:  # pragma: no cover - internal misuse
            raise _fail(f"{label}: unsupported stated kind")
    else:
        _require(payload is None, f"{label}.value must be null when state is {state}")
    return {"state": state, "value": payload}


def _list(value: object, *, label: str, allow_empty: bool = True) -> list:
    _require(isinstance(value, list), f"{label} must be a JSON array")
    _require(allow_empty or value, f"{label} must not be empty")
    return value


# --------------------------------------------------------------------------- #
# Passage pool
# --------------------------------------------------------------------------- #
def _index_passages(passages: object) -> tuple[dict, dict, set]:
    """Return ``(by_id, by_asset, required)`` for the supplied passage pool.

    ``required`` is the set of ``(target_kind, target_id)`` pairs that coverage
    MUST account for: every non-empty passage plus every declared asset id.
    Empty passages carry no evidence and are not required.
    """
    pool = _list(passages, label="passages")
    by_id: dict[str, dict] = {}
    by_asset: dict[str, str] = {}
    required: set = set()
    for index, raw in enumerate(pool):
        label = f"passages[{index}]"
        # Passages are workflow-supplied parsed output (trusted provenance), not
        # model output, so extra parsed metadata (locator, section_path, digests,
        # ...) is accepted and ignored; only the keys below are read.
        item = _keys(
            raw,
            label=label,
            required=frozenset({"passage_id", "text"}),
            allow_extra=True,
        )
        passage_id = _identifier(item["passage_id"], label=f"{label}.passage_id")
        _require(passage_id not in by_id, "duplicate passage_id in passage pool")
        _require(isinstance(item["text"], str), f"{label}.text must be a string")
        source_key = item.get("source_key")
        if source_key is not None:
            source_key = _identifier(source_key, label=f"{label}.source_key")
        raw_text = item.get("raw_text")
        if raw_text is not None:
            _require(isinstance(raw_text, str), f"{label}.raw_text must be a string")
        asset_ids = item.get("asset_ids", [])
        asset_ids = _list(asset_ids, label=f"{label}.asset_ids")
        assets = []
        for asset in asset_ids:
            aid = _identifier(asset, label=f"{label}.asset_ids[]")
            _require(aid not in by_asset, "duplicate asset id in passage pool")
            by_asset[aid] = passage_id
            assets.append(aid)
            required.add(("asset", aid))
        by_id[passage_id] = {
            "text": item["text"],
            "raw_text": raw_text,
            "source_key": source_key,
            "assets": assets,
        }
        if item["text"].strip() or raw_text:
            required.add(("passage", passage_id))
    return by_id, by_asset, required


def _snippet_grounded(snippet: str, passage: dict) -> bool:
    if snippet in passage["text"]:
        return True
    raw = passage["raw_text"]
    return bool(raw is not None and snippet in raw)


# --------------------------------------------------------------------------- #
# Section validators
# --------------------------------------------------------------------------- #
def _validate_sources(value: object) -> dict[str, dict]:
    records = _list(value, label="source_records", allow_empty=False)
    out: dict[str, dict] = {}
    for index, raw in enumerate(records):
        label = f"source_records[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"source_key", "source_kind"}),
            optional=frozenset({"label", "report_ref"}),
        )
        key = _identifier(item["source_key"], label=f"{label}.source_key")
        _require(key not in out, "duplicate source_key in source_records")
        kind = _enum(item["source_kind"], label=f"{label}.source_kind", allowed=("document", "report"))
        label_value = item.get("label")
        if label_value is not None:
            _string(label_value, label=f"{label}.label")
        report_ref = item.get("report_ref")
        if report_ref is not None:
            _string(report_ref, label=f"{label}.report_ref")
        out[key] = {"source_key": key, "source_kind": kind, "label": label_value, "report_ref": report_ref}
    return out


def _validate_context(value: object, *, label: str) -> dict:
    item = _keys(value, label=label, required=frozenset(CONTEXT_FIELDS), optional=frozenset({"note"}))
    out = {}
    for name in CONTEXT_FIELDS:
        out[name] = _stated(item[name], label=f"{label}.{name}", kind="str")
    note = item.get("note")
    return {"context": out, "note": _optional_string(note, label=f"{label}.note")}


def _validate_numeric(value: object, *, label: str, passage: dict) -> list:
    """Validate numeric details; ``reported_as`` carries a qualified value verbatim.

    A known ``value`` is a single JSON number. A range, inequality, list,
    approximation, or other qualified value is never coerced into one: its
    ``value`` is ``not_applicable`` and the exact original text (an exact
    substring of the finding's passage) is carried in ``reported_as``.
    """
    items = _list(value, label=label)
    out = []
    for index, raw in enumerate(items):
        item_label = f"{label}[{index}]"
        item = _keys(
            raw,
            label=item_label,
            required=frozenset({"name", "value", "unit", "denominator"}),
            optional=frozenset({"reported_as"}),
        )
        detail = {
            "name": _string(item["name"], label=f"{item_label}.name"),
            "value": _stated(item["value"], label=f"{item_label}.value", kind="number"),
            "unit": _stated(item["unit"], label=f"{item_label}.unit", kind="str"),
            "denominator": _stated(item["denominator"], label=f"{item_label}.denominator", kind="str"),
        }
        reported_as = item.get("reported_as")
        if reported_as is not None:
            reported_as = _string(reported_as, label=f"{item_label}.reported_as")
            _require(
                _snippet_grounded(reported_as, passage),
                f"{item_label}.reported_as is not an exact substring of its passage",
            )
            detail["reported_as"] = reported_as
        _require(
            detail["value"]["state"] != "not_applicable" or reported_as is not None,
            f"{item_label}.reported_as is required when value.state is not_applicable",
        )
        out.append(detail)
    return out


def _validate_findings(value: object, sources: dict, passages: dict, id_lists: dict) -> dict[str, dict]:
    items = _list(value, label="findings")
    out: dict[str, dict] = {}
    for index, raw in enumerate(items):
        label = f"findings[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset(
                {"finding_id", "source_key", "passage_id", "snippet", "statement",
                 "attribution", "citation_flag", "context", "secondary_warning", "limitations"}
            ),
            optional=frozenset(
                {"numeric_details", "uncertainty", "experiment_id", "study_id", "dataset_id", "notes"}
            ),
        )
        finding_id = _identifier(item["finding_id"], label=f"{label}.finding_id")
        _require(finding_id not in out, "duplicate finding_id in findings")
        source_key = _identifier(item["source_key"], label=f"{label}.source_key")
        _require(source_key in sources, f"{label}.source_key is not declared in source_records")
        passage_id = _identifier(item["passage_id"], label=f"{label}.passage_id")
        _require(passage_id in passages, f"{label}.passage_id is not in the supplied passages")
        passage = passages[passage_id]
        if passage["source_key"] is not None:
            _require(
                passage["source_key"] == source_key,
                f"{label}.source_key does not match the cited passage's source",
            )
        snippet = _string(item["snippet"], label=f"{label}.snippet")
        _require(
            _snippet_grounded(snippet, passage),
            f"{label}.snippet is not an exact substring of its passage",
        )
        statement = _string(item["statement"], label=f"{label}.statement")
        attribution = _enum(item["attribution"], label=f"{label}.attribution", allowed=ATTRIBUTIONS)
        citation_flag = _enum(item["citation_flag"], label=f"{label}.citation_flag", allowed=CITATION_FLAGS)
        context = _validate_context(item["context"], label=f"{label}.context")
        secondary_warning = _stated(item["secondary_warning"], label=f"{label}.secondary_warning", kind="bool")
        limitations = _stated(item["limitations"], label=f"{label}.limitations", kind="str")
        numeric = _validate_numeric(
            item.get("numeric_details", []), label=f"{label}.numeric_details", passage=passage
        )
        uncertainty = item.get("uncertainty")
        if uncertainty is not None:
            uncertainty = _stated(uncertainty, label=f"{label}.uncertainty", kind="str")
        refs = {}
        for field, target, plural in (
            ("experiment_id", "experiment", "experiments"),
            ("study_id", "study", "studies"),
            ("dataset_id", "dataset", "datasets"),
        ):
            ref = item.get(field)
            if ref is not None:
                ref = _identifier(ref, label=f"{label}.{field}")
                _require(ref in id_lists[target], f"{label}.{field} is not declared in {plural}")
                refs[field] = ref
        out[finding_id] = {
            "finding_id": finding_id,
            "source_key": source_key,
            "passage_id": passage_id,
            "snippet": snippet,
            "statement": statement,
            "attribution": attribution,
            "citation_flag": citation_flag,
            "context": context["context"],
            "context_note": context["note"],
            "secondary_warning": secondary_warning,
            "limitations": limitations,
            "numeric_details": numeric,
            "uncertainty": uncertainty,
            "notes": _optional_string(item.get("notes"), label=f"{label}.notes"),
            **refs,
        }
    return out


def _validate_id_records(value: object, *, label: str, id_field: str, optional: frozenset) -> dict[str, dict]:
    items = _list(value, label=label)
    out: dict[str, dict] = {}
    for index, raw in enumerate(items):
        item_label = f"{label}[{index}]"
        item = _keys(raw, label=item_label, required=frozenset({id_field}), optional=optional)
        identity = _identifier(item[id_field], label=f"{item_label}.{id_field}")
        _require(identity not in out, f"duplicate {id_field} in {label}")
        record = {id_field: identity}
        for extra in optional:
            record[extra] = item.get(extra)
            if record[extra] is not None:
                _string(record[extra], label=f"{item_label}.{extra}")
        out[identity] = record
    return out


def _ref_exists(ref: str, names: dict) -> bool:
    _require(isinstance(ref, str) and ":" in ref, "entity ref must be 'kind:id'")
    kind, _, identity = ref.partition(":")
    _require(kind in REF_KINDS, "entity ref has an unknown kind")
    return identity in names[kind]


def _validate_links(value: object, names: dict) -> list:
    items = _list(value, label="links")
    out = []
    for index, raw in enumerate(items):
        label = f"links[{index}]"
        item = _keys(raw, label=label, required=frozenset({"from", "to", "relation", "status"}))
        from_ref = _string(item["from"], label=f"{label}.from")
        to_ref = _string(item["to"], label=f"{label}.to")
        relation = _enum(item["relation"], label=f"{label}.relation", allowed=LINK_RELATIONS)
        status = _enum(item["status"], label=f"{label}.status", allowed=LINK_STATUSES)
        _require(_ref_exists(from_ref, names), f"{label}.from is not a declared record")
        _require(_ref_exists(to_ref, names), f"{label}.to is not a declared record")
        from_kind = from_ref.split(":", 1)[0]
        to_kind = to_ref.split(":", 1)[0]
        expected = {
            "reports": ("source", "study"),
            "uses_dataset": ("source", "dataset"),
            "describes_experiment": ("source", "experiment"),
            "same_study": ("study", "study"),
            "same_dataset": ("dataset", "dataset"),
        }[relation]
        _require(
            (from_kind, to_kind) == expected,
            f"{label}.relation does not match its source/target kinds",
        )
        out.append({"from": from_ref, "to": to_ref, "relation": relation, "status": status})
    return out


def _validate_appraisal(value: object, dimensions: tuple) -> list:
    items = _list(value, label="appraisal")
    by_id = {}
    for index, raw in enumerate(items):
        label = f"appraisal[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"dimension_id", "state", "judgement"}),
            optional=frozenset({"rationale", "tool_applied"}),
        )
        for banned in ("score", "rating", "weight", "value"):
            _require(banned not in item, f"{label} must not carry a numeric '{banned}' (no universal score)")
        dimension_id = _identifier(item["dimension_id"], label=f"{label}.dimension_id")
        _require(dimension_id in dimensions, f"{label}.dimension_id is not an approved dimension")
        _require(dimension_id not in by_id, "duplicate appraisal dimension")
        state = _enum(item["state"], label=f"{label}.state", allowed=APPRAISAL_STATES)
        judgement = _string(item["judgement"], label=f"{label}.judgement")
        rationale = item.get("rationale")
        if state == "assessed":
            _string(rationale, label=f"{label}.rationale")
        tool = item.get("tool_applied")
        if tool is not None:
            tool = _stated(tool, label=f"{label}.tool_applied", kind="str")
        by_id[dimension_id] = {
            "dimension_id": dimension_id,
            "state": state,
            "judgement": judgement,
            "rationale": rationale,
            "tool_applied": tool,
        }
    missing = set(dimensions) - set(by_id)
    _require(not missing, "appraisal is missing approved dimension(s)")
    return [by_id[d] for d in dimensions]


def _validate_omissions(value: object) -> list:
    items = _list(value, label="omissions")
    out = []
    for index, raw in enumerate(items):
        label = f"omissions[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"note"}),
            optional=frozenset({"target_kind", "target_id", "important"}),
        )
        note = _string(item["note"], label=f"{label}.note")
        target_kind = item.get("target_kind")
        target_id = item.get("target_id")
        if target_kind is not None:
            _enum(target_kind, label=f"{label}.target_kind", allowed=("passage", "asset"))
            _string(target_id, label=f"{label}.target_id")
        else:
            _require(target_id is None, f"{label}.target_id requires target_kind")
        important = item.get("important")
        if important is not None:
            _require(isinstance(important, bool), f"{label}.important must be a boolean")
        out.append({"note": note, "target_kind": target_kind, "target_id": target_id, "important": important})
    return out


def _validate_coverage(value: object, findings: dict, required: set) -> tuple[list, set]:
    items = _list(value, label="coverage")
    out = []
    covered: set = set()
    for index, raw in enumerate(items):
        label = f"coverage[{index}]"
        item = _keys(
            raw,
            label=label,
            required=frozenset({"target_kind", "target_id", "disposition"}),
            optional=frozenset({"finding_id", "reason"}),
        )
        target_kind = _enum(item["target_kind"], label=f"{label}.target_kind", allowed=("passage", "asset"))
        target_id = _identifier(item["target_id"], label=f"{label}.target_id")
        key = (target_kind, target_id)
        _require(key in required, f"{label} references an unknown required item")
        _require(key not in covered, "duplicate coverage entry")
        disposition = _enum(item["disposition"], label=f"{label}.disposition", allowed=COVERAGE_DISPOSITIONS)
        finding_id = item.get("finding_id")
        reason = item.get("reason")
        if disposition == "finding":
            finding_id = _identifier(finding_id, label=f"{label}.finding_id")
            _require(finding_id in findings, f"{label}.finding_id is not a declared finding")
            if target_kind == "passage":
                _require(
                    findings[finding_id]["passage_id"] == target_id,
                    f"{label}.finding_id cites a different passage",
                )
            _require(reason is None, f"{label}.reason is only allowed when omitted")
        else:
            _require(finding_id is None, f"{label}.finding_id is only allowed with disposition 'finding'")
            if disposition == "omitted":
                _string(reason, label=f"{label}.reason")
        covered.add(key)
        out.append(
            {"target_kind": target_kind, "target_id": target_id, "disposition": disposition,
             "finding_id": finding_id, "reason": reason}
        )
    # Every finding must be claimed by exactly one coverage entry (completeness).
    claimed = {entry["finding_id"] for entry in out if entry["disposition"] == "finding"}
    unclaimed = set(findings) - claimed
    _require(not unclaimed, "some findings are not accounted in coverage")
    # Every required passage/asset id must be accounted (fail closed).
    missing = required - covered
    _require(not missing, f"coverage does not account for {len(missing)} required item(s)")
    return out, covered


# --------------------------------------------------------------------------- #
# Linkage summary (never an independence claim by absence)
# --------------------------------------------------------------------------- #
def _link_summary(source_keys: list, links: list) -> dict:
    """Group links by proposal strength and list sources with *unknown* linkage.

    A link (proposed or confirmed) is a model proposal and cannot prove identity;
    human appraisal is still required.  Sources not connected by any link have
    **unknown** linkage -- this function never claims they are independent.
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

    nodes = {f"source:{key}" for key in source_keys}
    for link in links:
        nodes.add(link["from"])
        nodes.add(link["to"])
    for node in nodes:
        find(node)
    for link in links:
        union(link["from"], link["to"])
    unlinked = []
    ordered = sorted(source_keys)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if find(f"source:{a}") != find(f"source:{b}"):
                unlinked.append([a, b])
    return {
        "confirmed_links": [dict(link) for link in links if link["status"] == "confirmed"],
        "proposed_links": [dict(link) for link in links if link["status"] == "proposed"],
        "unlinked_source_pairs": unlinked,
        "notice": LINKAGE_NOTICE,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def validate_extraction(output, *, passages, appraisal_dimensions):
    """Validate and normalize one untrusted extraction document.

    ``passages`` is the pool for a single paper plus its linked supplements;
    each entry is a workflow-supplied parsed passage mapping.  Only
    ``passage_id`` and ``text`` are required; ``raw_text``, ``source_key`` and
    ``asset_ids`` are read when present, and any other parsed metadata
    (locator, section_path, digests, ...) is accepted and ignored -- the model
    never sees this pool, so no projection step is needed.
    ``appraisal_dimensions`` is the workflow-approved list of dimension
    descriptors ``{dimension_id, ...}``; an empty list means no appraisal is
    configured (and ``appraisal`` must then be empty/absent).

    Returns a new normalized ``dict`` (JSON-portable) or raises
    :class:`ExtractionError`.  Structural validation cannot prove scientific
    correctness or completeness (see :data:`VALIDATION_SCOPE`).
    """
    # Whole-document canonical-JSON preflight (shared domain gate): rejects
    # non-finite floats, non-string keys, invalid surrogates, over-deep nesting
    # (depth bound), circular references, and any non-JSON value.  The failure is
    # re-raised as a static ExtractionError with no value leak.
    try:
        canonical_json(output)
    except (RecordValidationError, ValueError, TypeError):
        raise _fail("extraction output is not canonical JSON-portable") from None
    _require(isinstance(output, dict), "extraction output must be a JSON object")
    banned = _BANNED_TOP_KEYS & set(output)
    _require(not banned, f"extraction output must not carry universal score/exclusion keys: {sorted(banned)}")
    item = _keys(output, label="extraction", required=_REQUIRED_TOP_KEYS, optional=_OPTIONAL_TOP_KEYS)
    version = item["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        f"schema_version must be the integer {SCHEMA_VERSION}",
    )

    summary = _string(item["summary"], label="summary")
    passages_by_id, _passages_by_asset, required = _index_passages(passages)
    sources = _validate_sources(item["source_records"])

    dimensions = _dimension_ids(appraisal_dimensions)
    names = {
        "source": set(sources),
        "study": set(),
        "dataset": set(),
        "experiment": set(),
        "finding": set(),
    }
    studies = _validate_id_records(item.get("studies", []), label="studies", id_field="study_id", optional=frozenset({"label"}))
    datasets = _validate_id_records(item.get("datasets", []), label="datasets", id_field="dataset_id", optional=frozenset({"label"}))
    experiments = _validate_id_records(
        item.get("experiments", []), label="experiments", id_field="experiment_id",
        optional=frozenset({"label", "study_id"}),
    )
    for experiment in experiments.values():
        if experiment.get("study_id") is not None:
            _require(
                experiment["study_id"] in studies,
                "experiments: a declared study_id is not in studies",
            )
    names["study"] = set(studies)
    names["dataset"] = set(datasets)
    names["experiment"] = set(experiments)

    findings = _validate_findings(item["findings"], sources, passages_by_id, names)
    names["finding"] = set(findings)

    links = _validate_links(item.get("links", []), names)
    appraisal = _validate_appraisal(item.get("appraisal", []), dimensions)
    omissions = _validate_omissions(item.get("omissions", []))
    limitations = item.get("limitations")
    if limitations is not None:
        limitations = _stated(limitations, label="limitations", kind="str")

    coverage, _covered = _validate_coverage(item["coverage"], findings, required)

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "source_records": [sources[key] for key in sorted(sources)],
        "studies": [studies[key] for key in sorted(studies)],
        "datasets": [datasets[key] for key in sorted(datasets)],
        "experiments": [experiments[key] for key in sorted(experiments)],
        "findings": [findings[key] for key in sorted(findings)],
        "coverage": coverage,
        "links": links,
        "appraisal": appraisal,
        "omissions": omissions,
        "limitations": limitations,
        "link_summary": _link_summary(sorted(sources), links),
        "validation": {
            "scope": VALIDATION_SCOPE,
            "required_items": len(required),
            "accounted_items": len(coverage),
        },
    }
    return normalized


def _dimension_ids(appraisal_dimensions) -> tuple:
    _require(isinstance(appraisal_dimensions, list), "appraisal_dimensions must be a list")
    seen = []
    for index, raw in enumerate(appraisal_dimensions):
        label = f"appraisal_dimensions[{index}]"
        _require(isinstance(raw, dict), f"{label} must be a JSON object")
        dimension_id = _identifier(raw.get("dimension_id"), label=f"{label}.dimension_id")
        _require(dimension_id not in seen, "duplicate appraisal dimension")
        _require("score" not in raw, f"{label} must not define a numeric score")
        seen.append(dimension_id)
    return tuple(seen)


#: Original passage pool for the raw example below (workflow-supplied projection).
EXTRACTION_EXAMPLE_PASSAGES = [
    {
        "passage_id": "p0001",
        "source_key": "docA",
        "text": "In mice, compound Z did not reduce tumor volume compared with vehicle.",
        "asset_ids": ["tbl1"],
    },
    {
        "passage_id": "p0002",
        "source_key": "docA",
        "text": "The 50 mg dose reduced tumor volume by 12.5 mm3 per mouse (n=8; P<0.01) over 2-3 weeks.",
    },
    {
        "passage_id": "p0003",
        "source_key": "docB",
        "text": "We conclude that compound Z is broadly effective.",
    },
]

#: A valid **RAW** (unnormalized) model-output example, grounded in
#: :data:`EXTRACTION_EXAMPLE_PASSAGES`.  The runtime runner can include this (with
#: the passages) in the prompt as the exact shape reference instead of inventing
#: its own conversion; every snippet and number is an exact substring of a
#: supplied passage.  Fictional, never real evidence.
EXTRACTION_OUTPUT_EXAMPLE = {
    "schema_version": SCHEMA_VERSION,
    "summary": "Compound Z effects in a mouse model, with a separate author interpretation.",
    "source_records": [
        {"source_key": "docA", "source_kind": "document", "label": "Study A", "report_ref": "report:A"},
        {"source_key": "docB", "source_kind": "document", "label": "Study B", "report_ref": "report:B"},
    ],
    "studies": [{"study_id": "S1", "label": "Z tumor study"}],
    "datasets": [{"dataset_id": "D1", "label": "Z measurements"}],
    "experiments": [{"experiment_id": "E1", "study_id": "S1", "label": "mouse efficacy"}],
    "findings": [
        {
            "finding_id": "F1",
            "source_key": "docA",
            "passage_id": "p0001",
            "snippet": "did not reduce tumor volume",
            "statement": "In mice, compound Z did not reduce tumor volume versus vehicle.",
            "attribution": "direct_observation",
            "citation_flag": "own_result",
            "context": {
                "species": {"state": "known", "value": "mouse"},
                "population": {"state": "missing", "value": None},
                "assay": {"state": "known", "value": "tumor volume"},
                "outcome": {"state": "known", "value": "tumor volume"},
                "conditions": {"state": "known", "value": "vehicle comparison"},
            },
            "secondary_warning": {"state": "not_applicable", "value": None},
            "limitations": {"state": "known", "value": "single model system"},
            "numeric_details": [],
            "experiment_id": "E1",
        },
        {
            "finding_id": "F2",
            "source_key": "docA",
            "passage_id": "p0002",
            "snippet": "reduced tumor volume by 12.5 mm3",
            "statement": "The 50 mg dose reduced tumor volume by 12.5 mm3 per mouse.",
            "attribution": "direct_observation",
            "citation_flag": "own_result",
            "context": {
                "species": {"state": "known", "value": "mouse"},
                "population": {"state": "known", "value": "n=8 mice"},
                "assay": {"state": "known", "value": "tumor volume"},
                "outcome": {"state": "known", "value": "tumor volume"},
                "conditions": {"state": "known", "value": "50 mg dose"},
            },
            "secondary_warning": {"state": "not_applicable", "value": None},
            "limitations": {"state": "known", "value": "single model system"},
            "numeric_details": [
                {"name": "tumor volume", "value": {"state": "known", "value": 12.5},
                 "unit": {"state": "known", "value": "mm3"},
                 "denominator": {"state": "known", "value": "per mouse"}},
                {"name": "mice per group", "value": {"state": "known", "value": 8},
                 "unit": {"state": "known", "value": "mice"},
                 "denominator": {"state": "not_applicable", "value": None}},
                {"name": "P value", "value": {"state": "not_applicable", "value": None},
                 "reported_as": "P<0.01",
                 "unit": {"state": "not_applicable", "value": None},
                 "denominator": {"state": "not_applicable", "value": None}},
                {"name": "treatment duration", "value": {"state": "not_applicable", "value": None},
                 "reported_as": "2-3",
                 "unit": {"state": "known", "value": "weeks"},
                 "denominator": {"state": "not_applicable", "value": None}},
            ],
            "experiment_id": "E1",
        },
        {
            "finding_id": "F3",
            "source_key": "docB",
            "passage_id": "p0003",
            "snippet": "compound Z is broadly effective",
            "statement": "The authors assert compound Z is broadly effective.",
            "attribution": "author_interpretation",
            "citation_flag": "mixed",
            "context": {
                "species": {"state": "unknown", "value": None},
                "population": {"state": "unknown", "value": None},
                "assay": {"state": "missing", "value": None},
                "outcome": {"state": "missing", "value": None},
                "conditions": {"state": "missing", "value": None},
            },
            "secondary_warning": {"state": "known", "value": True},
            "limitations": {"state": "known", "value": "author interpretation, not measured"},
            "numeric_details": [],
        },
    ],
    "coverage": [
        {"target_kind": "passage", "target_id": "p0001", "disposition": "finding", "finding_id": "F1"},
        {"target_kind": "passage", "target_id": "p0002", "disposition": "finding", "finding_id": "F2"},
        {"target_kind": "passage", "target_id": "p0003", "disposition": "finding", "finding_id": "F3"},
        {"target_kind": "asset", "target_id": "tbl1", "disposition": "omitted", "reason": "table_not_acquired"},
    ],
    "links": [{"from": "source:docA", "to": "study:S1", "relation": "reports", "status": "confirmed"}],
    "appraisal": [],
    "omissions": [
        {"note": "table tbl1 not acquired as an input", "target_kind": "asset", "target_id": "tbl1", "important": True}
    ],
    "limitations": {"state": "known", "value": "synthetic fixture; not evidence"},
}


def raw_extraction_example():
    """Return a fresh **RAW** (unnormalized) model-output example.

    This is the shape the runtime runner should show the model (with
    :data:`EXTRACTION_EXAMPLE_PASSAGES`), not the normalized result.  Pass it to
    :func:`validate_extraction` to obtain the normalized form.
    """
    return copy.deepcopy(EXTRACTION_OUTPUT_EXAMPLE)


def extraction_example():
    """Return the validated, normalized form of the raw example.

    It demonstrates exact-snippet grounding, a grounded numeric detail, a
    missing context value, an `author_interpretation`, a model-proposed link, and
    a coverage omission -- never real evidence and never a correctness claim.
    """
    return validate_extraction(
        raw_extraction_example(),
        passages=copy.deepcopy(EXTRACTION_EXAMPLE_PASSAGES),
        appraisal_dimensions=[],
    )
