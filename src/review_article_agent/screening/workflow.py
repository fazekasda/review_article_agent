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
from ..extraction.runner import NON_TEXT_MATERIAL_LIMITATION
from ..parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from ..parsing.repository import ParsingRepository, _NestedStore
from ..runtime.dispatch import RuntimeDispatchRepository
from ..runtime.gates import MAX_GATE_SNAPSHOT_ENTRIES
from ..runtime.jev import MODEL, SAFE_MAX_REQUEST_BYTES, JevError, JevValidationError, QuestionDef, build_request, validate_response
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

#: Version of the deterministic whole-article windowing scheme. Recorded in
#: every case so a stored decision can be tied to the exact split algorithm.
WINDOWING_VERSION = "screening-windows/1"

#: Fixed header every window begins with. It tells the model the text is one
#: window of a larger article screened separately, so an answer must come only
#: from the window it sees. ``{k}``/``{n}`` are the 1-based window number and the
#: total window count.
WINDOW_HEADER_TEMPLATE = (
    "Article window {k} of {n}. The other windows of this article are screened "
    "separately; answer only from this text."
)


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


def aggregate_window_decisions(config, window_machines, *, context_ready=True):
    """Conservatively aggregate per-window decisions into one machine decision.

    ``window_machines`` is the ordered list of :func:`_classify_and_observe`
    results (one per window). For each question, if **every** window genuinely
    answered it with a probability, the aggregated probability is the **maximum**
    across windows and the existing thresholds pick the label; if **any** window
    refused, was malformed, was missing, or failed to validate, the question is
    retained as ``uncertain`` and can never be a proposed exclusion. An empty
    window list is uncertain for every question, and ``context_ready=False``
    (missing context) makes the whole case uncertain regardless of the answers.

    The result has the same shape as :func:`_classify_and_observe` so every
    existing consumer (pilot/batch/audit) keeps working unchanged.
    """
    decisions = []
    labels = []
    for rule in config["questions"]:
        question_id = rule["id"]
        probabilities = []
        complete = True
        for machine in window_machines:
            outcome = next(
                (d for d in machine.get("decisions", []) if d["question_id"] == question_id),
                None,
            )
            if (outcome is None or outcome.get("outcome") != "answered"
                    or outcome.get("probability") is None):
                complete = False
                break
            probabilities.append(Decimal(outcome["probability"]))
        if not complete or not probabilities:
            decisions.append({"question_id": question_id, "kind": "predicate",
                              "outcome": "uncertain", "probability": None})
            labels.append("uncertain")
            continue
        probability_max = max(probabilities)
        decisions.append({"question_id": question_id, "kind": "predicate",
                          "outcome": "answered", "probability": str(probability_max)})
        if probability_max < probability(rule["exclude_below"]):
            labels.append("proposed_exclusion")
        elif probability_max >= probability(rule["retain_above"]):
            labels.append("retain")
        else:
            labels.append("uncertain")
    label = "uncertain" if not context_ready or "uncertain" in labels else (
        "proposed_exclusion" if "proposed_exclusion" in labels else "retain")
    return {"label": label,
            "reason": "missing_context" if not context_ready else "rubric_mapping",
            "decisions": decisions}


#: The only key of a case ``response`` that references a committed decision
#: result instead of an imported response envelope.
DECISION_RESULT_REF_KEY = "decision_result_ref"
#: Ordered per-window committed-result references (one per window, window order).
DECISION_RESULT_REFS_KEY = "decision_result_refs"
#: Ordered per-window imported response envelopes (one per window, window order).
WINDOW_RESPONSES_KEY = "window_responses"


def is_decision_result_response(value):
    """True iff a case ``response`` is a single committed-result reference."""
    return isinstance(value, Mapping) and set(value) == {DECISION_RESULT_REF_KEY}


def is_decision_result_refs_response(value):
    """True iff a case ``response`` is an ordered list of committed-result refs."""
    return (isinstance(value, Mapping) and set(value) == {DECISION_RESULT_REFS_KEY}
            and isinstance(value[DECISION_RESULT_REFS_KEY], list))


def is_window_responses_response(value):
    """True iff a case ``response`` is an ordered list of imported envelopes."""
    return (isinstance(value, Mapping) and set(value) == {WINDOW_RESPONSES_KEY}
            and isinstance(value[WINDOW_RESPONSES_KEY], list))


def case_context(parsing, report_ref, passage_id, *, questions=None):
    """Assemble the exact screening context text for one case (shared).

    Returns ``{report, text, article_text, target_passage, locator, ready,
    limitation_included}``. Context is the whole parsed article, never an
    isolated sentence or a summary; oversize inputs are retained, never silently
    truncated (they are split by :func:`case_windows`, not dropped). The current
    source/report/rights are always verified, even for whole-paper cases.

    A cleanly ``parsed`` report is ready whenever its text is non-empty, even if
    it declares figures/tables/supplements or is larger than one Jev request:
    those non-text assets keep their own review flag and are disclosed by a fixed
    limitation line reserved in **every** window (:func:`case_windows`), and size
    is handled by windowing. A non-``parsed`` report
    (``needs_review``/``rejected``) is never ready.

    ``article_text`` is the whole article text (the text that is windowed);
    ``target_passage`` is the selected contextualized passage text, or ``None``.
    When the typed ``questions`` (definitions) are supplied and the report is
    ready, ``text`` is the **first window** (:func:`case_windows`), so the legacy
    single-request path sends exactly window 1; otherwise ``text`` is the full
    assembled context (``article_text``, or the ``Target passage: ...`` +
    original-article context form for a passage case).
    """
    report = parsing.read_report(report_ref)
    passages = report["payload"]["passages"]
    require(report["verifiable"])
    locator = None
    article_text = "\n\n".join(p["text"] for p in passages)
    target_passage = None
    if passage_id is not None:
        locator = parsing.make_passage_locator(report_ref, passage_id)
        target_passage = next(p["text"] for p in passages if p["passage_id"] == passage_id)
        text = ("Target passage: " + target_passage
                + "\n\nOriginal article context:\n" + article_text)
    else:
        text = article_text
    ready = report["status"] == "parsed" and bool(text.strip())
    limitation_included = bool(report["payload"].get("non_text_assets_require_review"))
    if passages:
        check = parsing.resolve_passage(
            parsing.make_passage_locator(report_ref, passages[0]["passage_id"]))
        require(not check["current"]["reasons"])
    parsing._assert_source_allows_derivative(report["source_ref"])
    context = {"report": report, "text": text, "article_text": article_text,
               "target_passage": target_passage, "locator": locator, "ready": ready,
               "limitation_included": limitation_included}
    # The legacy single-request path sends ``text`` directly, so when the typed
    # definitions are supplied ``text`` is the first window: that single request
    # is exactly window 1, which keeps the direct-dispatch binding valid for a
    # one-window case. Multi-window screening always goes through
    # :func:`case_windows` (one request per window).
    if questions is not None and ready:
        try:
            context["text"] = case_windows(questions, context)[0]
        except JevError:
            # A header/target region that cannot fit one request is not ready:
            # fail closed and retain the case (never crash, never send).
            context["ready"] = False
    return context


def _limitation_suffix(context):
    """The fixed non-text limitation line reserved in every window, or ``""``."""
    if context["report"]["payload"].get("non_text_assets_require_review"):
        return "\n\n" + NON_TEXT_MATERIAL_LIMITATION
    return ""


def _window_prefix(k, n, target_passage):
    """The header region of window ``k`` of ``n`` (1-based), with the target."""
    header = WINDOW_HEADER_TEMPLATE.format(k=k, n=n)
    if target_passage is not None:
        header += "\n\nTarget passage: " + target_passage
    return header


def _window_request_fits(definitions, prefix, body, suffix, bound):
    try:
        request = build_request(definitions, prefix + "\n\n" + body + suffix)
    except JevError:
        return False
    return len(canonical_json(request)) <= bound


def _largest_body(definitions, prefix, suffix, text, start, bound):
    """Largest end offset ``> start`` so the window fits, split at whitespace.

    Binary-searches the end offset (the serialized request size is monotone in
    the body), then backs off to the last whitespace boundary so a window does
    not cut a word; if the body has no whitespace the character boundary is used.
    Always advances by at least one character so packing terminates and loses
    nothing, even for a body that cannot itself fit ``bound``.

    If even the **empty** body (the header/target/limitation region alone) does
    not fit ``bound``, no body can ever fit, so this raises immediately instead of
    emitting thousands of one-character windows. The check is cheap (one request
    build) and is reached only for the first window of an otherwise-unpackable
    case.
    """
    if not _window_request_fits(definitions, prefix, "", suffix, bound):
        raise JevValidationError(
            "a window header/target/limitation region alone exceeds the request bound"
        )
    if not _window_request_fits(definitions, prefix, text[start:start + 1], suffix, bound):
        return start + 1
    lo, hi = start + 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _window_request_fits(definitions, prefix, text[start:mid], suffix, bound):
            lo = mid
        else:
            hi = mid - 1
    end = lo
    if end < len(text):
        for i in range(end - 1, start - 1, -1):
            if text[i].isspace():
                end = i + 1
                break
    return end


def _pack_bodies(definitions, make_prefix, text, suffix, bound):
    """Greedy document-order packing of ``text`` into request-bounded bodies."""
    bodies = []
    start = 0
    k = 1
    while start < len(text):
        end = _largest_body(definitions, make_prefix(k), suffix, text, start, bound)
        bodies.append(text[start:end])
        start = end
        k += 1
    if not bodies:
        bodies.append("")
    return bodies


def _converge_bodies(definitions, make_prefix, text, suffix, bound):
    """Fix the window count that appears in every header (monotone, bounded).

    ``make_prefix`` is ``(k, n) -> str``. The window count ``n`` is printed in
    each header, so packing depends on it; iterating from ``n = 1`` converges
    because a longer header can only ever produce more (never fewer) windows.
    """
    n = 1
    while True:
        bodies = _pack_bodies(definitions, lambda k: make_prefix(k, n), text, suffix, bound)
        if len(bodies) == n:
            return bodies, n
        n = len(bodies)


def window_bodies(definitions, context, *, bound=SAFE_MAX_REQUEST_BYTES):
    """Return the whole article text as ordered, request-bounded window bodies.

    Pure and deterministic. The bodies are contiguous document-order slices of
    ``context["article_text"]``; ``"".join(window_bodies(...))`` reproduces that
    text exactly (no character is dropped or reordered). Each body, together with
    its window header and the reserved limitation line, serializes to a request
    no larger than ``bound``.
    """
    text = context["article_text"]
    suffix = _limitation_suffix(context)
    target_passage = context.get("target_passage")
    bodies, _ = _converge_bodies(
        definitions, lambda k, n: _window_prefix(k, n, target_passage),
        text, suffix, bound)
    return bodies


def case_windows(definitions, context, *, bound=SAFE_MAX_REQUEST_BYTES):
    """Split one case context into deterministic, request-bounded window texts.

    Pure and deterministic. Every returned window text begins with the fixed
    ``Article window {k} of {n}.`` header (plus the ``Target passage: ...`` text
    in its header region for a passage case) and, when the report declares
    non-text assets that require review, ends with the reserved
    ``NON_TEXT_MATERIAL_LIMITATION`` line. The whole article text is covered
    exactly once, in document order; ``len(canonical_json(build_request(
    definitions, window))) <= bound`` holds for every window (measured with the
    real request builder, not an estimate). A single passage too large for one
    window is split at whitespace boundaries (character boundary as a fallback)
    with no characters lost.
    """
    text = context["article_text"]
    suffix = _limitation_suffix(context)
    target_passage = context.get("target_passage")
    bodies, n = _converge_bodies(
        definitions, lambda k, n: _window_prefix(k, n, target_passage),
        text, suffix, bound)
    return [_window_prefix(k + 1, n, target_passage) + "\n\n" + body + suffix
            for k, body in enumerate(bodies)]


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
        definitions = questions(config)
        context = case_context(
            self.parsing, item["report_ref"], item["passage_id"], questions=definitions
        )
        report = context["report"]
        ready = context["ready"]
        try:
            windows = case_windows(definitions, context)
            window_requests = [build_request(definitions, window) for window in windows]
        except JevError:
            windows = []
            window_requests = []
            ready = False
        window_digests = [sha256_digest(canonical_json(request)) for request in window_requests]
        window_input_digests = [sha256_digest(window.encode()) for window in windows]
        # ``request_sha256`` binds the ordered list of per-window request digests,
        # so a single window's request is never mistaken for the whole case and a
        # reordered or partial window set changes the digest.
        request_sha256 = (sha256_digest(canonical_json(window_digests))
                          if window_digests else None)
        response = item["response"]
        if is_decision_result_response(response):
            # A single committed-result reference binds only a one-window case.
            require(len(windows) == 1)
            machine, observed_model, provenance = self._verified_response(
                config, response[DECISION_RESULT_REF_KEY], window_digests[0], ready)
            window_machines = [machine]
        elif is_decision_result_refs_response(response):
            refs = response[DECISION_RESULT_REFS_KEY]
            require(len(refs) == len(windows))
            window_machines = []
            models = []
            for ref, digest in zip(refs, window_digests):
                window_machine, window_model, _ = self._verified_response(
                    config, ref, digest, ready)
                window_machines.append(window_machine)
                models.append(window_model)
            require(len(set(models)) == 1)
            observed_model = models[0]
            provenance = "verified_dispatch"
            machine = aggregate_window_decisions(config, window_machines, context_ready=ready)
        elif is_window_responses_response(response):
            responses = response[WINDOW_RESPONSES_KEY]
            require(len(responses) == len(windows))
            window_machines = []
            models = []
            for window_response in responses:
                window_machine, window_model = _classify_and_observe(
                    config, window_response, context_ready=ready)
                window_machines.append(window_machine)
                models.append(window_model)
            require(len(set(models)) == 1)
            observed_model = models[0]
            provenance = "imported_unverified_dispatch"
            machine = aggregate_window_decisions(config, window_machines, context_ready=ready)
        else:
            # A single imported response envelope is only meaningful for a
            # one-window case; a whole-article case must supply one response per
            # window (``window_responses``) or verified per-window result refs.
            require(len(windows) == 1)
            machine, observed_model = _classify_and_observe(
                config, response, context_ready=ready)
            window_machines = [machine]
            provenance = "imported_unverified_dispatch"
        # ``model`` is the requested/pinned config model; ``observed_model`` is
        # the identity the validated response actually reported. They are kept
        # separate so a dated snapshot never silently stands in for the base
        # alias (or vice versa).
        return {"id": item["id"], "report_ref": item["report_ref"], "source_ref": report["source_ref"],
                "locator": context["locator"],
                "input_sha256": sha256_digest(context["article_text"].encode()),
                "request_sha256": request_sha256,
                "windowing_version": WINDOWING_VERSION,
                "windows": [{"request_sha256": request_digest, "input_sha256": input_digest}
                            for request_digest, input_digest
                            in zip(window_digests, window_input_digests)],
                "window_decisions": [
                    [{"question_id": d["question_id"], "kind": d["kind"],
                      "outcome": d["outcome"], "probability": d["probability"]}
                     for d in window_machine.get("decisions", [])]
                    for window_machine in window_machines
                ],
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
