"""Small, immutable screening workflow over existing artifact and human gates.

Responses are imported observations, not proof of a hosted dispatch. Only
predicate eligibility questions are supported; framework/hypothesis labels are
deliberately absent. All thresholds and sample counts are project choices.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from decimal import Decimal, InvalidOperation
from typing import Mapping

from ..acquisition.manifest import AcquisitionRepository
from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier, validate_ref
from ..parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from ..parsing.repository import ParsingRepository, _NestedStore
from ..runtime.dispatch import RuntimeDispatchRepository
from ..runtime.gates import MAX_GATE_SNAPSHOT_ENTRIES
from ..runtime.jev import MODEL, SAFE_MAX_REQUEST_BYTES, JevError, QuestionDef, build_request, validate_response
from ..storage.artifacts import ArtifactRepository, DependencyRef, sha256_digest
from ..storage.decision_intents import DecisionIntentRepository
from ..storage.decision_results import RESULT_KIND, DecisionResultRepository
from ..storage.human_approvals import HumanApprovalRepository


class ScreeningError(ReviewAgentError):
    """Static diagnostics: never include source text or provider output."""


#: Explicit historical reads return *stored* evidence only. The historic
#: decision is NOT recomputed with the current model/parser/config/gates (an old
#: algorithm may no longer be available), so the result is never current
#: validation or scientific/eligibility authority.
HISTORICAL_NOTICE = ("Stored historical evidence only. Not revalidated against the "
                     "current model, parser, config, or gates, and not scientific or "
                     "eligibility authority. Inspect; do not authorize work from it.")

#: Bound the transitive dependency content-integrity walk of a historical read
#: by the number of *unique* revisions visited, reusing the gate-snapshot
#: convention. It is enforced while discovering/enqueuing unique nodes, so
#: exceeding it raises; there is no depth pruning that could silently leave
#: deeper pinned bytes unverified.
_HISTORICAL_MAX_REVISIONS = MAX_GATE_SNAPSHOT_ENTRIES


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
    after :func:`validate_response`; it is ``None`` only when that envelope
    could not be validated at all. Once a valid envelope is observed the model
    identity is returned even if individual answers are missing, refused, or
    malformed: those answers still map to ``uncertain``, but they do not erase
    the identity the envelope reported. A response whose envelope is invalid
    contributes no identity. The identity is a reported model *string*; equal
    strings (for example a base alias) cannot prove the same frozen weights.
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


#: The only key of a case ``response`` that references a committed decision
#: result instead of an imported response envelope.
DECISION_RESULT_REF_KEY = "decision_result_ref"


def is_decision_result_response(value):
    """True iff a case ``response`` is a committed-result reference."""
    return isinstance(value, Mapping) and set(value) == {DECISION_RESULT_REF_KEY}


def case_context(parsing, report_ref, passage_id):
    """Assemble the exact screening context text for one case (shared).

    Returns ``{report, text, locator, ready}``. Context is the whole parsed
    article, never an isolated sentence or a summary; oversize inputs are
    retained, never silently truncated. The current source/report/rights are
    always verified, even for whole-paper cases.
    """
    report = parsing.read_report(report_ref)
    passages = report["payload"]["passages"]
    require(report["verifiable"])
    locator = None
    text = "\n\n".join(p["text"] for p in passages)
    if passage_id is not None:
        locator = parsing.make_passage_locator(report_ref, passage_id)
        text = ("Target passage: "
                + next(p["text"] for p in passages if p["passage_id"] == passage_id)
                + "\n\nOriginal article context:\n" + text)
    ready = not report["human_action_required"] and bool(text.strip())
    if passages:
        check = parsing.resolve_passage(
            parsing.make_passage_locator(report_ref, passages[0]["passage_id"]))
        require(not check["current"]["reasons"])
    parsing._assert_source_allows_derivative(report["source_ref"])
    return {"report": report, "text": text, "locator": locator, "ready": ready}


def _response_from_observation(repository, observation):
    """Rebuild a Jev response envelope from a committed observation's records.

    Only genuinely ``answered`` predicate/choice/score answers are reconstructed
    (a predicate probability is the raw stored value). Every other outcome is
    omitted, so ``validate_response`` re-derives the same retained
    uncertainty/refusal/malformed outcome it did at dispatch time — never an
    exclusion and never a fabricated answer.
    """
    answers = {}
    for question in observation.questions:
        record = repository.get_question_record(observation.dispatch_id, question.question_id)
        require(record is not None)
        mapping = record.record
        answer = mapping.get("answer")
        if mapping.get("outcome") != "answered" or not isinstance(answer, Mapping):
            continue
        kind = answer.get("answer_kind")
        # The stored probability/score are exact canonical decimal *strings*; the
        # wire validator wants a JSON number, so hand back the exact ``Decimal``.
        if kind == "predicate":
            answers[question.question_id] = {"type": "noul", "noul": Decimal(answer["probability"])}
        elif kind == "choice":
            answers[question.question_id] = {"type": "choice", "choice": answer["choice"]}
        elif kind == "score":
            answers[question.question_id] = {"type": "score", "score": Decimal(answer["score"])}
    usage = observation.usage or {}
    return {
        "model": observation.actual_model,
        "usage": {
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
        },
        "answers": answers,
    }


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
        require(doc.get("schema_version") == 1 and doc.get("document_type") == "screening_" + kind)
        payload = doc["payload"] if kind == "config" else doc["payload"]["input"]
        expected = {canonical_json(self._ref(ref)) for ref in self._dependencies(payload)}
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(all(d.role == "derived_from" for d in live))
        require({canonical_json(d.as_ref()) for d in live} == expected)
        return doc["payload"]

    def _verify_revision_bytes(self, artifact_id, revision_id):
        """Require one stored revision's bytes to match its recorded hash/size."""
        rev = self.artifacts.get_revision(artifact_id, revision_id)
        require(rev is not None)
        data = self.artifacts.read_content(artifact_id, revision_id)
        require(sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size)

    def _verify_historical_dependencies(self, direct):
        """Bounded, fail-closed transitive content integrity for dependencies.

        Historical inspection must not rely on stale data, so every revision
        reachable from the declared dependencies must still exist with bytes
        matching its recorded hash and size. The walk is iterative and
        cycle-safe (a ``seen`` set), follows only the stored dependency edges
        (for example a screening report pins its parsing report, which pins the
        exact source bytes), and skips ``version_of`` ancestry exactly as the
        current read does. Every edge's own exact ``{project_id, artifact_id,
        revision_id}`` reference is validated with :meth:`_ref` before any skip
        or dedup, so a foreign-project edge (even one reusing local ids) is
        rejected rather than reinterpreted as a local revision. It never
        re-derives or re-classifies the old payload.

        The unique-revision bound is enforced at discovery/enqueue time: a graph
        exceeding it raises rather than silently verifying only a partial
        closure. There is no depth pruning, so deep-but-legitimate histories are
        fully checked.
        """
        seen = set()
        queue = deque()
        for ref in direct:
            key = self._ref(ref)
            node = (key["artifact_id"], key["revision_id"])
            if node not in seen:
                seen.add(node)
                require(len(seen) <= _HISTORICAL_MAX_REVISIONS)
                queue.append(node)
        while queue:
            artifact_id, revision_id = queue.popleft()
            self._verify_revision_bytes(artifact_id, revision_id)
            for dep in self.artifacts.list_dependencies(artifact_id, revision_id):
                # Validate the edge's own exact reference (including its project)
                # before any skip or dedup: a stored edge is not trusted to be
                # this project's, and a foreign edge that reuses already-seen
                # local ids must not bypass validation.
                edge = self._ref(dep.as_ref())
                if dep.role == "version_of":
                    continue
                node = (edge["artifact_id"], edge["revision_id"])
                if node not in seen:
                    seen.add(node)
                    require(len(seen) <= _HISTORICAL_MAX_REVISIONS)
                    queue.append(node)

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

    def _verified_response(self, config, ref, request_sha256, ready):
        """Load and verify a committed decision result for a case response.

        The referenced result must be the **accepted** result of a completed
        direct-Jev dispatch in this project whose recorded pre-dispatch intent
        used the pinned model and whose exact request digest equals the request
        this case builds (binding config questions, report context, and model).
        A missing/foreign/unaccepted/ambiguous result, a divergent request, or a
        model mismatch is refused. Returns ``(machine, observed_model, provenance)``.
        """
        require(isinstance(ref, Mapping) and set(ref) == {"project_id", "dispatch_id"})
        require(ref["project_id"] == self.store.project_id)
        dispatch_id = validate_identifier(ref["dispatch_id"], "dispatch_id")
        results = DecisionResultRepository(self.store)
        observation = results.get_result(dispatch_id)
        require(observation is not None)
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(dispatch_id)
        # Only an accepted, completed result is a verified response; an inert or
        # ambiguous observation (unknown dispatch, no accepted triple) is refused.
        require(dispatch is not None and dispatch.state == "completed"
                and dispatch.result_kind == RESULT_KIND
                and dispatch.result_ref == observation.result_id
                and dispatch.result_digest == observation.digest)
        intent = DecisionIntentRepository(self.store).get_intent(dispatch_id)
        require(intent is not None and intent.model == config["model"])
        # ``request_sha256`` is the canonical ``sha256:<hex>`` digest the workflow
        # records; the ledger stores the bare hex, so compare the bare form.
        bare = request_sha256.split(":", 1)[-1] if isinstance(request_sha256, str) else request_sha256
        require(observation.request_sha256 == bare)
        response = _response_from_observation(results, observation)
        machine, observed_model = _classify_and_observe(config, response, context_ready=ready)
        return machine, observed_model, "verified_dispatch"

    def _case(self, config, item):
        require(set(item) == {"id", "report_ref", "passage_id", "response"})
        require(isinstance(item["id"], str) and 0 < len(item["id"]) <= 128)
        context = case_context(self.parsing, item["report_ref"], item["passage_id"])
        report = context["report"]
        text = context["text"]
        ready = context["ready"]
        try:
            request = build_request(questions(config), text)
            require(len(canonical_json(request)) <= SAFE_MAX_REQUEST_BYTES)
        except JevError:
            request = None
            ready = False
        except ScreeningError:
            request = None
            ready = False
        request_sha256 = sha256_digest(canonical_json(request)) if request else None
        if is_decision_result_response(item["response"]):
            # A verified dispatch can only be bound when this case builds a valid
            # request; otherwise there is nothing to bind the result to.
            require(request is not None)
            machine, observed_model, provenance = self._verified_response(
                config, item["response"][DECISION_RESULT_REF_KEY], request_sha256, ready)
        else:
            machine, observed_model = _classify_and_observe(config, item["response"], context_ready=ready)
            provenance = "imported_unverified_dispatch"
        # ``model`` is the requested/pinned config model; ``observed_model`` is
        # the identity the validated response actually reported. They are kept
        # separate so a dated snapshot never silently stands in for the base
        # alias (or vice versa).
        return {"id": item["id"], "report_ref": item["report_ref"], "source_ref": report["source_ref"],
                "locator": context["locator"], "input_sha256": sha256_digest(text.encode()),
                "request_sha256": request_sha256,
                "model": config["model"], "observed_model": observed_model,
                "parser": report["parser_version"],
                "response_provenance": provenance,
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
        # The whole check is one point-in-time read transaction. BEGIN
        # IMMEDIATE (when this call owns the unit of work; a SAVEPOINT when a
        # caller already opened one) starts before the first read and holds
        # SQLite's write lock, so another connection cannot commit a new audit
        # or approval revision between the scan and the returned batch. This is
        # authorization for the caller's decision only: a writer that acts on
        # it must revalidate inside its own transaction.
        with _NestedStore(self.store).transaction():
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

    def read(self, ref, kind, *, historical=False):
        """Read a screening report; default is a current, freshly validated read.

        ``historical=True`` is an explicit, read-only inspection of a pinned old
        revision that may no longer be the artifact head or may depend on stale
        inputs. It verifies the stored target bytes/envelope, the exact declared
        dependency refs, and bounded transitive dependency content integrity, but
        it does **not** recompute the historic decision with the current
        model/parser/config/gates. The result is marked stored evidence only and
        conveys no current validation or scientific/eligibility authority.
        """
        result = self._read(ref, kind, current=not historical)
        if historical:
            source = result if kind == "config" else result["input"]
            self._verify_historical_dependencies(self._dependencies(source))
            return {"historical": True, "freshly_validated": False,
                    "current_validation": False, "authority": "stored_evidence_only",
                    "notice": HISTORICAL_NOTICE, "payload": result}
        if kind == "config":
            questions(result)
        else:
            require(getattr(self, "_" + kind)(result["input"]) == result)
        return result

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
