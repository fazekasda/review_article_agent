"""Small, immutable screening workflow over existing artifact and human gates.

Responses are imported observations, not proof of a hosted dispatch. Only
predicate eligibility questions are supported; framework/hypothesis labels are
deliberately absent. All thresholds and sample counts are project choices.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from ..acquisition.manifest import AcquisitionRepository
from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_ref
from ..parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from ..parsing.repository import ParsingRepository, _NestedStore
from ..runtime.jev import MODEL, SAFE_MAX_REQUEST_BYTES, JevError, QuestionDef, build_request, validate_response
from ..storage.artifacts import ArtifactRepository, DependencyRef, sha256_digest
from ..storage.human_approvals import HumanApprovalRepository


class ScreeningError(ReviewAgentError):
    """Static diagnostics: never include source text or provider output."""


def require(condition):
    if not condition:
        raise ScreeningError("screening input, dependency, or approval is invalid or stale")


def probability(value):
    require(isinstance(value, (str, int, float)) and not isinstance(value, bool))
    try:
        require(len(str(value)) <= 32)
        number = Decimal(str(value))
        require(number.is_finite() and 0 <= number <= 1)
        return number
    except InvalidOperation:
        raise ScreeningError("invalid screening probability") from None


def questions(config):
    require(set(config) == {"questions", "pilot_size", "random_size", "risk_size", "acceptance_rule", "model", "parser", "normalization"})
    require(config["model"] == MODEL and config["parser"] == PARSER_VERSION
            and config["normalization"] == NORMALIZATION_VERSION)
    for name in ("pilot_size", "random_size", "risk_size"):
        require(type(config[name]) is int and 1 <= config[name] <= 10000)
    require(isinstance(config["acceptance_rule"], str) and 0 < len(config["acceptance_rule"]) <= 4000)
    require(isinstance(config["questions"], list) and 1 <= len(config["questions"]) <= 8)
    result = []
    for item in config["questions"]:
        require(set(item) == {"id", "instructions", "purpose", "exclude_below", "retain_above"})
        require(item["purpose"] == "eligibility")
        require(isinstance(item["instructions"], str) and 0 < len(item["instructions"]) <= 4000)
        require(probability(item["exclude_below"]) < probability(item["retain_above"]))
        result.append(QuestionDef(item["id"], "predicate", item["instructions"]))
    require(len({q.question_id for q in result}) == len(result))
    return result


def _classify_and_observe(config, response, *, context_ready=True):
    """Return the machine decision and the validated response model identity.

    The observed identity is the model the response envelope actually reported
    after :func:`validate_response`; it is ``None`` when the response (or every
    answer) could not be validated. Failure/refusal/malformed/unknown responses
    are therefore retained as ``uncertain`` and never contribute a model
    identity a pilot could use to authorize a later bulk run.
    """
    definitions = questions(config)
    decisions = []
    try:
        validated = validate_response(response, definitions)
        outcomes = [validated.outcomes[q.question_id] for q in definitions]
    except JevError:
        return {"label": "uncertain", "reason": "classifier_failure", "decisions": []}, None
    observed_model = validated.model
    labels = []
    for rule, outcome in zip(config["questions"], outcomes):
        p = outcome.answer.probability if outcome.answer else None
        decisions.append({"question_id": outcome.question_id, "kind": "predicate",
                          "outcome": outcome.outcome, "probability": str(p) if p is not None else None})
        if outcome.outcome != "answered" or p is None:
            labels.append("uncertain")
        elif p < probability(rule["exclude_below"]):
            labels.append("proposed_exclusion")
        elif p >= probability(rule["retain_above"]):
            labels.append("retain")
        else:
            labels.append("uncertain")
    label = "uncertain" if not context_ready or "uncertain" in labels else (
        "proposed_exclusion" if "proposed_exclusion" in labels else "retain")
    return {"label": label, "reason": "missing_context" if not context_ready else "rubric_mapping",
            "decisions": decisions}, observed_model


def classify(config, response, *, context_ready=True):
    """Preserve raw typed probabilities; errors/uncertainty always retain."""
    return _classify_and_observe(config, response, context_ready=context_ready)[0]


class ScreeningRepository:
    """Write proposals; existing owner-authenticated gates supply authority.

    Each action creates a report revision. Iteration uses the same artifact id
    with its current parent. Generic imports do not acquire authority: reads
    recompute proposals and all executable checks require named human gates.
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(_NestedStore(store))
        self.parsing = ParsingRepository(store)

    def _ref(self, ref):
        return validate_ref(ref, self.store.project_id)

    def _read(self, ref, kind):
        ref = self._ref(ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        head = self.artifacts.current_revision(ref["artifact_id"])
        require(rev is not None and head is not None and head.revision_id == rev.revision_id)
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(sha256_digest(data) == rev.content_sha256)
        doc = json.loads(data)
        require(doc.get("schema_version") == 1 and doc.get("document_type") == "screening_" + kind)
        payload = doc["payload"] if kind == "config" else doc["payload"]["input"]
        expected = {canonical_json(self._ref(ref)) for ref in self._dependencies(payload)}
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(all(d.role == "derived_from" for d in live))
        require({canonical_json(d.as_ref()) for d in live} == expected)
        return doc["payload"]

    @staticmethod
    def _dependencies(payload):
        return ([v for k, v in payload.items() if k.endswith("_ref")]
                + [c["report_ref"] for c in payload.get("cases", [])])

    def _approved(self, ref, kind, gate):
        require(HumanApprovalRepository(self.store).named_gate_satisfied(
            ref["artifact_id"], gate_kind=kind, gate_id=gate))

    def _config(self, ref):
        config = self._read(ref, "config")
        questions(config)
        return config

    def _case(self, config, item):
        require(set(item) == {"id", "report_ref", "passage_id", "response"})
        require(isinstance(item["id"], str) and 0 < len(item["id"]) <= 128)
        report = self.parsing.read_report(self._ref(item["report_ref"]))
        passages = report["payload"]["passages"]
        require(report["verifiable"])
        locator = None
        # Context is the whole parsed article, never an isolated sentence or a
        # summary. Oversize inputs are retained, never silently truncated.
        text = "\n\n".join(p["text"] for p in passages)
        if item["passage_id"] is not None:
            locator = self.parsing.make_passage_locator(item["report_ref"], item["passage_id"])
            text = "Target passage: " + next(p["text"] for p in passages if p["passage_id"] == item["passage_id"]) + "\n\nOriginal article context:\n" + text
        ready = not report["human_action_required"] and bool(text.strip())
        # Always verify current source/report/rights, even for whole-paper cases.
        if passages:
            check = self.parsing.resolve_passage(self.parsing.make_passage_locator(item["report_ref"], passages[0]["passage_id"]))
            require(not check["current"]["reasons"])
        self.parsing._assert_source_allows_derivative(report["source_ref"])
        try:
            request = build_request(questions(config), text)
            require(len(canonical_json(request)) <= SAFE_MAX_REQUEST_BYTES)
        except JevError:
            request = None
            ready = False
        except ScreeningError:
            request = None
            ready = False
        machine, observed_model = _classify_and_observe(config, item["response"], context_ready=ready)
        # ``model`` is the requested/pinned config model; ``observed_model`` is
        # the identity the validated response actually reported. They are kept
        # separate so a dated snapshot never silently stands in for the base
        # alias (or vice versa).
        return {"id": item["id"], "report_ref": item["report_ref"], "source_ref": report["source_ref"],
                "locator": locator, "input_sha256": sha256_digest(text.encode()),
                "request_sha256": sha256_digest(canonical_json(request)) if request else None,
                "model": config["model"], "observed_model": observed_model,
                "parser": report["parser_version"],
                "response_provenance": "imported_unverified_dispatch",
                "machine": machine}

    def _cases(self, config, cases):
        require(isinstance(cases, list) and 1 <= len(cases) <= 10000)
        require(len({c["id"] for c in cases}) == len(cases))
        require(len({canonical_json([c["report_ref"], c["passage_id"]]) for c in cases}) == len(cases))
        return [self._case(config, item) for item in cases]

    def _pilot(self, payload):
        require(set(payload) == {"config_ref", "cases", "adjudications"})
        config = self._config(payload["config_ref"])
        cases = self._cases(config, payload["cases"])
        require(len(cases) >= config["pilot_size"])
        adjudications = payload["adjudications"]
        require(set(adjudications) == {c["id"] for c in cases})
        failures = []
        for case in cases:
            human = adjudications[case["id"]]
            require(set(human) == {"label", "inspected_source", "important_failure", "rationale", "corrective_action"})
            require(human["label"] in ("retain", "exclude", "uncertain"))
            require(human["inspected_source"] is True and type(human["important_failure"]) is bool)
            require(isinstance(human["rationale"], str) and bool(human["rationale"].strip()))
            false_exclusion = case["machine"]["label"] == "proposed_exclusion" and human["label"] != "exclude"
            if human["important_failure"] or false_exclusion:
                require(isinstance(human["corrective_action"], str) and bool(human["corrective_action"].strip()))
                failures.append(case["id"])
        return {"input": payload, "cases": cases, "blocking_failures": failures,
                "claim": "Observed iterative examples only; not held-out or population accuracy.",
                "coverage": ["screening"], "authorizes_extraction": False}

    def require_pilot(self, ref):
        pilot = self._read(ref, "pilot")
        require(self._pilot(pilot["input"]) == pilot and not pilot["blocking_failures"])
        self._approved(ref, "pilot", "screening.pilot")
        return pilot

    def _batch(self, payload):
        require(set(payload) == {"config_ref", "pilot_ref", "acquisition_ref", "cases", "seed"})
        config = self._config(payload["config_ref"])
        pilot = self.require_pilot(payload["pilot_ref"])
        require(pilot["input"]["config_ref"] == payload["config_ref"])
        # The pilot pins the concrete validated response identity it observed. A
        # pilot with no usable observation, or with mixed snapshots, cannot
        # authorize a bulk run at all (fail closed; re-pilot required).
        observed = {c["observed_model"] for c in pilot["cases"] if c["observed_model"] is not None}
        require(len(observed) == 1)
        pinned_model = next(iter(observed))
        acquired = AcquisitionRepository(self.store).require_acquired(at_ref=payload["acquisition_ref"])
        cases = self._cases(config, payload["cases"])
        # Every bulk observation that reported a model must exactly match the
        # pilot identity (base alias and dated snapshot are distinct strings). A
        # retained failure/refusal/malformed observation has no identity to
        # compare and stays uncertain, never a negative label.
        require(all(c["observed_model"] in (None, pinned_model) for c in cases))
        # Ordinary corpus growth is not a dependency of the pilot itself.
        sources = [e["source_ref"] for e in acquired["payload"]["entries"] if e["disposition"] == "use_full_text"]
        require(all(c["source_ref"] in sources for c in cases))
        require(isinstance(payload["seed"], str) and 0 < len(payload["seed"]) <= 128)
        proposed = [c for c in cases if c["machine"]["label"] == "proposed_exclusion"]
        random_order = sorted(proposed, key=lambda c: hashlib.sha256((payload["seed"] + ":" + c["id"]).encode()).hexdigest())
        random_ids = [c["id"] for c in random_order[:config["random_size"]]]
        # Inspect the exclusions closest to an excluding predicate's boundary.
        def risk_margin(case):
            return min(probability(rule["exclude_below"]) - Decimal(d["probability"])
                       for rule, d in zip(config["questions"], case["machine"]["decisions"])
                       if Decimal(d["probability"]) < probability(rule["exclude_below"]))

        risk_order = sorted(proposed, key=lambda c: (risk_margin(c), c["id"]))
        risk_ids = [c["id"] for c in risk_order[:config["risk_size"]]]
        return {"input": payload, "cases": cases, "random_ids": random_ids, "risk_ids": risk_ids,
                "proposed_exclusion_ids": [c["id"] for c in proposed], "authorizes_extraction": False}

    def _audit(self, payload):
        require(set(payload) == {"batch_ref", "checks"})
        batch = self._read(payload["batch_ref"], "batch")
        require(self._batch(batch["input"]) == batch)
        selected = set(batch["random_ids"] + batch["risk_ids"])
        require(set(payload["checks"]) == selected)
        failures = []
        for key, check in payload["checks"].items():
            require(set(check) == {"inspected_source", "false_exclusion", "rationale", "corrective_action"})
            require(check["inspected_source"] is True and type(check["false_exclusion"]) is bool)
            require(isinstance(check["rationale"], str) and bool(check["rationale"].strip()))
            if check["false_exclusion"]:
                require(isinstance(check["corrective_action"], str) and bool(check["corrective_action"].strip()))
                failures.append(key)
        return {"input": payload, "paused": bool(failures), "false_exclusions": failures,
                "guarantee": "sampled_audit_not_completeness"}

    def require_exclusions(self, ref):
        audit = self._read(ref, "audit")
        require(self._audit(audit["input"]) == audit and not audit["paused"])
        self._approved(ref, "exclusion_set", "screening.exclusions")
        # An observed false exclusion permanently pauses that exact batch.
        # Correct the inputs and create a new batch, then re-audit it.
        for artifact in self.artifacts.list_artifacts():
            if artifact.kind != "report":
                continue
            for revision in self.artifacts.list_revisions(artifact.artifact_id):
                try:
                    doc = json.loads(self.artifacts.read_content(artifact.artifact_id, revision.revision_id))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(doc, dict) and doc.get("document_type") == "screening_audit":
                    previous = doc.get("payload", {})
                    if previous.get("input", {}).get("batch_ref") == audit["input"]["batch_ref"]:
                        require(not previous.get("paused", True))
        batch = self._read(audit["input"]["batch_ref"], "batch")
        return batch["proposed_exclusion_ids"]

    def read(self, ref, kind):
        payload = self._read(ref, kind)
        if kind == "config":
            questions(payload)
        else:
            require(getattr(self, "_" + kind)(payload["input"]) == payload)
        return payload

    def propose(self, kind, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert proposal, atomically validating its current inputs."""
        with self.store.transaction():
            if kind == "config":
                questions(payload)
                result = payload
            elif kind == "pilot":
                result = self._pilot(payload)
            elif kind == "batch":
                result = self._batch(payload)
            elif kind == "audit":
                result = self._audit(payload)
            else:
                raise ScreeningError("unknown screening proposal kind")
            refs = self._dependencies(payload)
            unique = {canonical_json(self._ref(ref)): ref for ref in refs}
            deps = [DependencyRef(**ref, role="derived_from") for ref in unique.values()]
            self.artifacts.create_artifact(artifact_id, kind="report", created_by="user")
            revision = self.artifacts.import_revision(
                artifact_id=artifact_id, revision_id=revision_id, rights=rights,
                content={"schema_version": 1, "document_type": "screening_" + kind, "payload": result},
                dependencies=deps, origin_locator="screening:local", acquired_by="user",
                parent_revision_id=parent_revision_id)
        return {"project_id": self.store.project_id, "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id}
