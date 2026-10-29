"""Immutable extraction pilots, batches, audits, and the synthesis gate.

Each case is read back through one seam, :func:`_require_run`, which delegates to
``extraction.runner.require_run`` for a verified durable dispatch receipt (the
runner worker owns that; this module never parses a source).  Only the receipt's
passages and validated raw output are used.  No generation defaults are invented:
every dimension, model map, instruction/revision, size, and acceptance rule is an
explicit project choice in an approved ``extraction.config``.  A weak study is
never auto-excluded, no global score exists, and links stay model proposals.

Stored generic documents (``document_type`` in parentheses):

* ``extraction_config``: ``{config_schema_version:1, appraisal_dimensions:
  [{dimension_id, formal_tool?, applicability?}], pilot_size, random_size,
  risk_size, acceptance_rule, extraction_model, quality_model,
  extraction_instructions(_revision), quality_instructions(_revision), parser,
  normalization}``.  A model ``tool_applied`` may only name a dimension's
  human-approved ``formal_tool`` (with its non-empty applicability); a bare
  dimension requires no known tool.
* ``extraction_pilot``: ``{config_ref, cases:[{id, extraction_ref, quality_ref}],
  adjudications}``.  ``extraction_batch``: ``{config_ref, pilot_ref,
  acquisition_ref, cases, seed}``.  ``extraction_audit``: ``{batch_ref, checks}``.

Named gates: config ``extraction.config`` and pilot ``extraction.pilot`` precede
bulk work; batch/audit also carry extra ``extraction.batch``/``extraction.audit``
gates; the final synthesis authority is the ``extraction.outputs`` gate.
"""

from __future__ import annotations

import hashlib
import json

from ..acquisition.manifest import AcquisitionRepository
from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_ref
from ..parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from ..parsing.repository import _NestedStore
from ..runtime.gates import MAX_GATE_SNAPSHOT_ENTRIES
from ..runtime.lead import LeadConfig, LeadError
from ..storage.artifacts import ArtifactRepository, DependencyRef, sha256_digest
from ..storage.human_approvals import HumanApprovalRepository
from .schema import validate_extraction


class ExtractionWorkflowError(ReviewAgentError):
    """A workflow input, dependency, receipt, or approval is invalid or stale."""


# --------------------------------------------------------------------------- #
# Fixed vocabulary
# --------------------------------------------------------------------------- #
#: The failure modes every pilot must carry an adjudicated example for (AC17/18).
MANDATORY_FAILURE_LABELS = (
    "missing_findings",
    "negation",
    "nulls",
    "numeric_units_denominators",
    "species_models",
    "author_spin",
    "tables",
    "supplements",
)

#: The exact quality-check names a quality receipt's raw output must report.
QUALITY_CHECKS = (
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

ISSUE_SEVERITIES = ("blocking", "major", "minor", "style")
ESCALATIONS = ("none", "human", "stronger_model")
PILOT_SEVERITIES = ("none", "minor", "major", "blocking")

#: Named human gates.  Gate *kind* is the shared ``batch`` capability; the id
#: names the exact extraction gate instance (``batch/<id>``).
GATE_KIND = "batch"
GATE_CONFIG = "extraction.config"
GATE_PILOT = "extraction.pilot"
GATE_BATCH = "extraction.batch"
GATE_AUDIT = "extraction.audit"
GATE_OUTPUTS = "extraction.outputs"

CONFIG_SCHEMA_VERSION = 1

#: Bound the historical audit scan by unique revisions (gate-snapshot convention).
_AUDIT_SCAN_MAX_REVISIONS = MAX_GATE_SNAPSHOT_ENTRIES

_CONFIG_KEYS = frozenset(
    {
        "config_schema_version",
        "appraisal_dimensions",
        "pilot_size",
        "random_size",
        "risk_size",
        "acceptance_rule",
        "extraction_model",
        "quality_model",
        "extraction_instructions",
        "extraction_instructions_revision",
        "quality_instructions",
        "quality_instructions_revision",
        "parser",
        "normalization",
    }
)
_CASE_KEYS = frozenset({"id", "extraction_ref", "quality_ref"})
_ADJUDICATION_KEYS = frozenset(
    {
        "labels",
        "inspected_original",
        "checked_omissions",
        "checked_qualifications",
        "checked_fields",
        "important_failure",
        "severity",
        "rationale",
        "corrective_action",
    }
)
_AUDIT_CHECK_KEYS = frozenset(
    {
        "inspected_original",
        "omissions",
        "qualifications",
        "fields",
        "important_error",
        "rationale",
        "corrective_action",
    }
)

#: Exact receipt keys returned by the runner seam (see ``runner.require_run``).
RECEIPT_KEYS = frozenset(
    {
        "purpose",
        "config_ref",
        "report_refs",
        "source_refs",
        "passages",
        "raw_output",
        "observed_model",
        "dispatch_id",
        "extraction_ref",
    }
)

SYNTHESIS_NOTICE = (
    "Structured accepted evidence only, and only after the batch/extraction.outputs "
    "human gate. It is not a claim of complete correctness, scientific truth, or "
    "population accuracy; sampled audits never establish completeness. Source/study/"
    "dataset/experiment links remain model proposals, never proof of identity. "
    "A writer acting on this evidence must revalidate inside its own transaction."
)


def require(condition, message=None):
    if not condition:
        raise ExtractionWorkflowError(
            message
            or "extraction workflow input, dependency, receipt, or approval is invalid or stale"
        )


def _text(value, label):
    require(isinstance(value, str) and value.strip() != "", label)
    return value


# --------------------------------------------------------------------------- #
# The single runner seam
# --------------------------------------------------------------------------- #
def _require_run(store, ref, *, purpose):
    """Return one verified durable receipt for ``purpose`` (the only seam).

    Delegates to ``extraction.runner.require_run`` (owned by the runner worker):
    it verifies the ref against the durable *accepted* dispatch, never an imported
    response, and supplies the original passages plus the dispatch's raw output.
    Imported lazily so this workflow module does not shadow the runner; unit tests
    may patch this one function with synthetic verified receipts (disclosed).
    """
    from . import runner

    return runner.require_run(store, ref, purpose=purpose)


# --------------------------------------------------------------------------- #
# Public quality validator (pure)
# --------------------------------------------------------------------------- #
def validate_quality(raw):
    """Validate one untrusted quality-gate raw output; normalize it.

    Returns ``{"checks", "issues", "escalation", "failed_checks", "blocked",
    "blocking_reasons"}``.  Any false/missing check, any ``blocking``/``major``
    issue, or any non-``none`` escalation marks the result ``blocked`` -- it
    requires human correction or a new (stronger) re-run; it is never auto
    accepted.  Missing numerical units/denominators may remain missing with a
    stated limitation and do not by themselves require fabrication.
    """
    require(isinstance(raw, dict) and set(raw) == {"checks", "issues", "escalation"})
    checks = raw["checks"]
    require(isinstance(checks, dict) and set(checks) == set(QUALITY_CHECKS))
    failed = []
    for name in QUALITY_CHECKS:
        require(type(checks[name]) is bool)
        if checks[name] is not True:
            failed.append(name)
    issues = raw["issues"]
    require(isinstance(issues, list))
    normalized_issues = []
    important = []
    for item in issues:
        require(isinstance(item, dict) and set(item) == {"severity", "description", "corrective_action"})
        severity = item["severity"]
        require(severity in ISSUE_SEVERITIES)
        _text(item["description"], "issue.description")
        corrective = item["corrective_action"]
        if corrective is not None:
            _text(corrective, "issue.corrective_action")
        if severity in ("blocking", "major"):
            require(corrective is not None, "important issue requires a corrective_action")
            important.append(severity)
        normalized_issues.append(
            {"severity": severity, "description": item["description"], "corrective_action": corrective}
        )
    escalation = raw["escalation"]
    require(escalation in ESCALATIONS)
    reasons = []
    if failed:
        reasons.append("failed_checks")
    if important:
        reasons.append("important_issue")
    if escalation != "none":
        reasons.append("escalation")
    return {
        "checks": {name: checks[name] for name in QUALITY_CHECKS},
        "issues": normalized_issues,
        "escalation": escalation,
        "failed_checks": failed,
        "blocked": bool(reasons),
        "blocking_reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# Case-level derivation
# --------------------------------------------------------------------------- #
def _attention(extraction):
    """Surfaced limitations for one normalized extraction (never auto-accepted)."""
    reasons = []
    if any(entry["disposition"] == "omitted" for entry in extraction["coverage"]):
        reasons.append("omitted_coverage")
    if extraction["omissions"]:
        reasons.append("declared_omissions")
    if any(entry["state"] == "unassessed" for entry in extraction["appraisal"]):
        reasons.append("unassessed_appraisal")
    for finding in extraction["findings"]:
        if any(state["state"] in ("missing", "unknown") for state in finding["context"].values()):
            reasons.append("missing_context")
            break
    return reasons


#: Attention reasons that must be escalated by the quality gate, not auto-accepted.
_ESCALATION_ATTENTION = frozenset({"omitted_coverage", "declared_omissions", "unassessed_appraisal"})


def _check_tools(extraction, formal_tools):
    """A known ``tool_applied`` must name the dimension's approved formal tool."""
    for entry in extraction["appraisal"]:
        tool = entry["tool_applied"]
        approved = formal_tools.get(entry["dimension_id"])
        if tool is not None and tool["state"] == "known":
            require(approved is not None, "appraisal reports an unapproved formal tool")
            require(tool["value"] == approved, "appraisal tool does not match the approved formal_tool")


def _numeric_incomplete(numeric):
    return numeric["unit"]["state"] in ("missing", "unknown") or numeric["denominator"]["state"] in (
        "missing",
        "unknown",
    )


def _risk_reasons(case):
    """Deterministic, model-free risk signals for one derived case."""
    reasons = []
    extraction = case["extraction"]
    if "missing_context" in case["attention"]:
        reasons.append("missing_context")
    if any(_numeric_incomplete(detail) for finding in extraction["findings"] for detail in finding["numeric_details"]):
        reasons.append("numeric_units_denominators")
    if any(
        finding["attribution"] == "secondary_account"
        or finding["citation_flag"] == "secondary_account"
        or (finding["secondary_warning"]["state"] == "known" and finding["secondary_warning"]["value"] is True)
        for finding in extraction["findings"]
    ):
        reasons.append("secondary")
    if case["quality"]["blocked"]:
        reasons.append("quality_issue")
    if _ESCALATION_ATTENTION & set(case["attention"]):
        reasons.append("omission")
    return reasons


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class ExtractionWorkflowRepository:
    """Write inert proposals; existing owner-authenticated gates supply authority.

    Each action creates a report revision.  Iteration reuses the same artifact id
    with its current parent.  Generic imports do not acquire authority: reads
    recompute from durable receipts and every executable check requires a named
    human gate.
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(_NestedStore(store))

    # -- refs / reads --------------------------------------------------------- #
    def _ref(self, ref):
        return validate_ref(ref, self.store.project_id)

    def _verify_revision_bytes(self, artifact_id, revision_id):
        """Require one stored revision's bytes to match its recorded hash/size."""
        rev = self.artifacts.get_revision(artifact_id, revision_id)
        require(rev is not None)
        data = self.artifacts.read_content(artifact_id, revision_id)
        require(sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size)

    def _read(self, ref, kind, *, current=True):
        ref = self._ref(ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        require(rev is not None)
        if current:
            head = self.artifacts.current_revision(ref["artifact_id"])
            require(head is not None and head.revision_id == rev.revision_id)
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size)
        doc = json.loads(data)
        require(doc.get("schema_version") == 1 and doc.get("document_type") == "extraction_" + kind)
        payload = doc["payload"] if kind == "config" else doc["payload"]["input"]
        expected = {canonical_json(self._ref(r)) for r in self._dependencies(payload)}
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(all(d.role == "derived_from" for d in live))
        require({canonical_json(d.as_ref()) for d in live} == expected)
        return doc["payload"]

    @staticmethod
    def _dependencies(payload):
        deps = [value for key, value in payload.items() if key.endswith("_ref")]
        for case in payload.get("cases", []):
            deps.append(case["extraction_ref"])
            deps.append(case["quality_ref"])
        return deps

    def _approved(self, ref, gate_id):
        ref = self._ref(ref)
        require(
            HumanApprovalRepository(self.store).named_gate_satisfied(
                ref["artifact_id"], gate_kind=GATE_KIND, gate_id=gate_id
            )
        )

    # -- config (no generation defaults) ------------------------------------- #
    def _validate_config(self, payload):
        require(isinstance(payload, dict) and set(payload) == _CONFIG_KEYS)
        require(type(payload["config_schema_version"]) is int)
        require(payload["config_schema_version"] == CONFIG_SCHEMA_VERSION)
        require(
            payload["parser"] == PARSER_VERSION and payload["normalization"] == NORMALIZATION_VERSION,
        )
        for name in ("pilot_size", "random_size", "risk_size"):
            require(type(payload[name]) is int and 1 <= payload[name] <= 10000)
        _text(payload["acceptance_rule"], "acceptance_rule")
        _text(payload["extraction_instructions"], "extraction_instructions")
        _text(payload["extraction_instructions_revision"], "extraction_instructions_revision")
        _text(payload["quality_instructions"], "quality_instructions")
        _text(payload["quality_instructions_revision"], "quality_instructions_revision")
        dimensions = payload["appraisal_dimensions"]
        require(isinstance(dimensions, list))
        seen = set()
        for index, raw in enumerate(dimensions):
            require(isinstance(raw, dict), f"appraisal_dimensions[{index}] must be an object")
            dimension_id = raw.get("dimension_id")
            require(isinstance(dimension_id, str) and dimension_id.strip() != "", "appraisal dimension_id")
            require(dimension_id not in seen, "duplicate appraisal dimension")
            require("score" not in raw, "appraisal dimension must not define a score")
            provider = "formal_tool" in raw
            rationale = "applicability" in raw
            require(provider == rationale, "a formal_tool and its applicability must be declared together")
            if provider:
                _text(raw["formal_tool"], "appraisal formal_tool")
                _text(raw["applicability"], "appraisal applicability")
            seen.add(dimension_id)
        for field in ("extraction_model", "quality_model"):
            try:
                LeadConfig.from_mapping(payload[field])
            except LeadError:
                raise ExtractionWorkflowError("extraction workflow input, dependency, receipt, or approval is invalid or stale") from None
        return payload

    @staticmethod
    def _formal_tools(config):
        return {
            raw["dimension_id"]: raw.get("formal_tool")
            for raw in config["appraisal_dimensions"]
        }

    def require_config(self, ref):
        config = self._validate_config(self._read(ref, "config"))
        self._approved(ref, GATE_CONFIG)
        return config

    # -- receipts ------------------------------------------------------------- #
    def _receipt(self, ref, purpose):
        normalized = self._ref(ref)
        receipt = _require_run(self.store, normalized, purpose=purpose)
        require(isinstance(receipt, dict) and set(receipt) == RECEIPT_KEYS)
        require(receipt["purpose"] == purpose)
        require(isinstance(receipt["observed_model"], str) and receipt["observed_model"].strip() != "")
        require(isinstance(receipt["dispatch_id"], str) and receipt["dispatch_id"].strip() != "")
        require(isinstance(receipt["report_refs"], list))
        require(isinstance(receipt["source_refs"], list))
        require(isinstance(receipt["passages"], list))
        require(isinstance(receipt["raw_output"], dict))
        config_ref = canonical_json(self._ref(receipt["config_ref"]))
        if purpose == "extraction":
            require(receipt["extraction_ref"] is None)
        else:
            require(receipt["extraction_ref"] is not None)
        return receipt, config_ref

    def _case(self, config, config_id, item):
        require(isinstance(item, dict) and set(item) == _CASE_KEYS)
        case_id = _text(item["id"], "case.id")
        require(len(case_id) <= 128)
        extraction_ref = canonical_json(self._ref(item["extraction_ref"]))
        quality_ref = canonical_json(self._ref(item["quality_ref"]))
        extraction, extraction_config_id = self._receipt(item["extraction_ref"], "extraction")
        quality, quality_config_id = self._receipt(item["quality_ref"], "quality")
        require(extraction_config_id == config_id and quality_config_id == config_id)
        require(canonical_json(self._ref(quality["extraction_ref"])) == extraction_ref)
        require(extraction["observed_model"] == config["extraction_model"]["model_id"])
        require(quality["observed_model"] == config["quality_model"]["model_id"])
        normalized = validate_extraction(
            extraction["raw_output"],
            passages=extraction["passages"],
            appraisal_dimensions=config["appraisal_dimensions"],
        )
        quality_result = validate_quality(quality["raw_output"])
        _check_tools(normalized, self._formal_tools(config))
        attention = _attention(normalized)
        requires_escalation = bool(_ESCALATION_ATTENTION & set(attention))
        blocked = bool(quality_result["blocked"] or requires_escalation)
        return {
            "id": case_id,
            "extraction_ref": json.loads(extraction_ref),
            "quality_ref": json.loads(quality_ref),
            "config_ref": json.loads(config_id),
            "source_refs": list(extraction["source_refs"]),
            "observed_model": extraction["observed_model"],
            "quality_model": quality["observed_model"],
            "extraction": normalized,
            "quality": quality_result,
            "attention": attention,
            "requires_escalation": requires_escalation,
            "blocked": blocked,
        }

    def _cases(self, config, config_id, cases):
        require(isinstance(cases, list) and 1 <= len(cases) <= 10000)
        require(len({c["id"] for c in cases}) == len(cases))
        require(
            len({canonical_json([self._ref(c["extraction_ref"]), self._ref(c["quality_ref"])]) for c in cases})
            == len(cases)
        )
        return [self._case(config, config_id, item) for item in cases]

    # -- pilot ---------------------------------------------------------------- #
    def _pilot(self, payload):
        require(isinstance(payload, dict) and set(payload) == {"config_ref", "cases", "adjudications"})
        config = self._validate_config(self._read(payload["config_ref"], "config"))
        config_id = canonical_json(self._ref(payload["config_ref"]))
        cases = self._cases(config, config_id, payload["cases"])
        require(len(cases) >= config["pilot_size"])
        adjudications = payload["adjudications"]
        require(isinstance(adjudications, dict) and set(adjudications) == {c["id"] for c in cases})
        failures = []
        covered = set()
        for case in cases:
            human = adjudications[case["id"]]
            require(isinstance(human, dict) and set(human) == _ADJUDICATION_KEYS)
            labels = human["labels"]
            require(isinstance(labels, list) and len(set(labels)) == len(labels))
            for label in labels:
                require(label in MANDATORY_FAILURE_LABELS)
            covered.update(labels)
            require(human["inspected_original"] is True)
            for field in ("checked_omissions", "checked_qualifications", "checked_fields"):
                require(human[field] is True, "human inspection statement must be true")
            require(type(human["important_failure"]) is bool)
            require(human["severity"] in PILOT_SEVERITIES)
            _text(human["rationale"], "adjudication.rationale")
            blocking = human["important_failure"] or human["severity"] in ("major", "blocking") or case["blocked"]
            if blocking:
                _text(human["corrective_action"], "adjudication.corrective_action")
                failures.append(case["id"])
        # Every mandatory failure mode must carry an adjudicated example.
        require(covered == set(MANDATORY_FAILURE_LABELS), "pilot is missing mandatory failure-case labels")
        return {
            "input": payload,
            "cases": cases,
            "blocking_failures": failures,
            "mandatory_labels": sorted(covered),
            "claim": "Observed iterative examples only; not held-out or population accuracy.",
            "coverage": ["extraction", "quality"],
            "authorizes_extraction": False,
        }

    def require_pilot(self, ref):
        pilot = self._read(ref, "pilot")
        require(self._pilot(pilot["input"]) == pilot)
        require(not pilot["blocking_failures"])
        self.require_config(pilot["input"]["config_ref"])
        self._approved(ref, GATE_PILOT)
        return pilot

    # -- batch ---------------------------------------------------------------- #
    def _batch(self, payload):
        require(
            isinstance(payload, dict)
            and set(payload) == {"config_ref", "pilot_ref", "acquisition_ref", "cases", "seed"}
        )
        config = self.require_config(payload["config_ref"])
        config_id = canonical_json(self._ref(payload["config_ref"]))
        pilot = self.require_pilot(payload["pilot_ref"])
        require(canonical_json(self._ref(pilot["input"]["config_ref"])) == config_id)
        pilot_extraction_models = {c["observed_model"] for c in pilot["cases"]}
        pilot_quality_models = {c["quality_model"] for c in pilot["cases"]}
        require(len(pilot_extraction_models) == 1 and len(pilot_quality_models) == 1)
        pinned_extraction = next(iter(pilot_extraction_models))
        pinned_quality = next(iter(pilot_quality_models))
        acquired = AcquisitionRepository(self.store).require_acquired(at_ref=payload["acquisition_ref"])
        cases = self._cases(config, config_id, payload["cases"])
        require(all(c["observed_model"] == pinned_extraction for c in cases))
        require(all(c["quality_model"] == pinned_quality for c in cases))
        # Acquisition must be currently complete over the exact manifest and every
        # case's original source must be one of its use_full_text sources.
        sources = {
            canonical_json(entry["source_ref"])
            for entry in acquired["payload"]["entries"]
            if entry["disposition"] == "use_full_text"
        }
        for case in cases:
            require(case["source_refs"], "case has no original source reference")
            for source in case["source_refs"]:
                require(canonical_json(source) in sources)
        _text(payload["seed"], "seed")
        require(len(payload["seed"]) <= 128)
        reasons = {c["id"]: _risk_reasons(c) for c in cases}
        random_order = sorted(cases, key=lambda c: hashlib.sha256((payload["seed"] + ":" + c["id"]).encode()).hexdigest())
        random_ids = [c["id"] for c in random_order[: config["random_size"]]]
        risk_order = sorted(cases, key=lambda c: (-len(reasons[c["id"]]), c["id"]))
        risk_ids = [c["id"] for c in risk_order[: config["risk_size"]]]
        return {
            "input": payload,
            "cases": cases,
            "random_ids": random_ids,
            "risk_ids": risk_ids,
            "risk_signals": reasons,
            "pinned_extraction_model": pinned_extraction,
            "pinned_quality_model": pinned_quality,
            "authorizes_extraction": False,
        }

    def require_batch(self, ref):
        batch = self._read(ref, "batch")
        require(self._batch(batch["input"]) == batch)
        self._approved(ref, GATE_BATCH)
        return batch

    # -- audit ---------------------------------------------------------------- #
    def _audit(self, payload):
        require(isinstance(payload, dict) and set(payload) == {"batch_ref", "checks"})
        batch = self.require_batch(payload["batch_ref"])
        selected = set(batch["random_ids"] + batch["risk_ids"])
        checks = payload["checks"]
        require(isinstance(checks, dict) and set(checks) == selected)
        important_errors = []
        for case_id, check in checks.items():
            require(isinstance(check, dict) and set(check) == _AUDIT_CHECK_KEYS)
            require(check["inspected_original"] is True)
            for field in ("omissions", "qualifications", "fields"):
                require(check[field] is True, "human inspection statement must be true")
            require(type(check["important_error"]) is bool)
            _text(check["rationale"], "audit.rationale")
            if check["important_error"]:
                _text(check["corrective_action"], "audit.corrective_action")
                important_errors.append(case_id)
        return {
            "input": payload,
            "paused": bool(important_errors),
            "important_errors": important_errors,
            "guarantee": "sampled_audit_not_completeness",
            "authorizes_synthesis": False,
        }

    def require_audit(self, ref):
        audit = self._read(ref, "audit")
        require(self._audit(audit["input"]) == audit)
        require(not audit["paused"])
        self._approved(ref, GATE_AUDIT)
        return audit

    # -- synthesis gate ------------------------------------------------------- #
    def require_for_synthesis(self, audit_ref):
        """Return accepted evidence only inside one atomic, gated transaction.

        The single transaction checks the current config, pilot, batch, and audit
        (with the ``batch/extraction.outputs`` named gate), requires every batch
        case resolved with no blocking failure, and scans **every historical
        audit revision** of the same batch so an earlier important error
        permanently pauses that exact batch -- a later clean audit can never erase
        it; correction means a new batch and a fresh re-audit.
        """
        ref = self._ref(audit_ref)
        with _NestedStore(self.store).transaction():
            audit = self._read(ref, "audit")
            require(self._audit(audit["input"]) == audit and not audit["paused"])
            self._approved(ref, GATE_OUTPUTS)
            batch_ref = self._ref(audit["input"]["batch_ref"])
            batch = self.require_batch(batch_ref)
            blocked = [c["id"] for c in batch["cases"] if c["blocked"]]
            require(not blocked, "a batch case is blocked")
            batch_id = canonical_json(batch_ref)
            scanned = 0
            for artifact in self.artifacts.list_artifacts():
                if artifact.kind != "report":
                    continue
                for revision in self.artifacts.list_revisions(artifact.artifact_id):
                    # Content integrity is verified before the document is
                    # trusted, so tampered/corrupt stored bytes can never silently
                    # authorize current outputs.
                    self._verify_revision_bytes(artifact.artifact_id, revision.revision_id)
                    try:
                        doc = json.loads(self.artifacts.read_content(artifact.artifact_id, revision.revision_id))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(doc, dict) or doc.get("document_type") != "extraction_audit":
                        continue
                    scanned += 1
                    require(scanned <= _AUDIT_SCAN_MAX_REVISIONS)
                    previous = doc.get("payload", {})
                    # The stored audit's batch_ref must be an exact same-project
                    # ref; a malformed/foreign one fails closed rather than being
                    # skipped.
                    previous_batch = canonical_json(
                        self._ref(previous.get("input", {}).get("batch_ref"))
                    )
                    if previous_batch == batch_id:
                        require(not previous.get("paused", True), "this batch was permanently paused by an important error")
            limitations = sorted({reason for case in batch["cases"] for reason in case["attention"]})
            escalations = sorted({case["quality"]["escalation"] for case in batch["cases"]})
            return {
                "accepted": True,
                "audit_ref": ref,
                "batch_ref": batch_ref,
                "config_ref": self._ref(batch["input"]["config_ref"]),
                "pilot_ref": self._ref(batch["input"]["pilot_ref"]),
                "acquisition_ref": self._ref(batch["input"]["acquisition_ref"]),
                "case_ids": [c["id"] for c in batch["cases"]],
                "audited_ids": sorted(set(batch["random_ids"] + batch["risk_ids"])),
                "limitations": limitations,
                "escalations": escalations,
                "gates_satisfied": [GATE_OUTPUTS],
                "guarantee": "sampled_audit_not_completeness",
                "authority": "named_gate_only",
                "notice": SYNTHESIS_NOTICE,
            }

    # -- writes --------------------------------------------------------------- #
    def read(self, ref, kind):
        """Read one extraction document; default is a current, freshly validated read."""
        result = self._read(ref, kind)
        if kind == "config":
            self._validate_config(result)
        else:
            require(getattr(self, "_" + kind)(result["input"]) == result)
        return result

    def propose(self, kind, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert proposal, atomically validating its current inputs."""
        with self.store.transaction():
            if kind == "config":
                result = self._validate_config(payload)
            elif kind == "pilot":
                result = self._pilot(payload)
            elif kind == "batch":
                result = self._batch(payload)
            elif kind == "audit":
                result = self._audit(payload)
            else:
                raise ExtractionWorkflowError("unknown extraction proposal kind")
            refs = self._dependencies(payload)
            unique = {canonical_json(self._ref(r)): r for r in refs}
            deps = [DependencyRef(**ref, role="derived_from") for ref in unique.values()]
            self.artifacts.create_artifact(artifact_id, kind="report", created_by="user")
            revision = self.artifacts.import_revision(
                artifact_id=artifact_id,
                revision_id=revision_id,
                rights=rights,
                content={"schema_version": 1, "document_type": "extraction_" + kind, "payload": result},
                dependencies=deps,
                origin_locator="extraction:local",
                acquired_by="user",
                parent_revision_id=parent_revision_id,
            )
        return {
            "project_id": self.store.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }
