"""Durable, resumable Step 12 section review workflow (MVP).

This module binds the pure Step 12 rules in
:mod:`review_article_agent.review.schema` to durable storage.  It is a small,
deliberately minimal MVP: no CLI, no storage schema change, no new dependency,
and no real model call.  The critic and writer are **injected ports**, so a test
supplies fakes that count calls.

What one ``run`` does (per ``docs/implementation/12-review.md``)
-----------------------------------------------------------------

* **Separate context, different model.** ``critic_model`` and ``writer_model``
  must be concrete and differ; the critic port receives only the draft and the
  package (never the writer's history).  The persisted state records explicitly
  that a different model is **not** independent human verification.
* **Critique stage -- up to 3 rounds.** A round is one critic findings pass
  (normalised with :func:`normalize_finding`) plus one writer repair pass *only*
  when a confirmed blocking/major finding exists.  Writer dispositions are
  applied with :func:`writer_response`; every repaired sentence is checked with
  :func:`check_repair` against its old text.  Any violation rejects the repair
  (the old sentence is kept, the violation recorded, and the finding stays
  unresolved); ``reopen_checks`` marks the sentence for a fact-check recheck.
* **Fact-check stage -- up to 2 rounds**, using the deterministic
  :func:`fact_check` and writer repair of the failing sentences (same repair
  checks).
* **Persist after every model call result**, as a new immutable artifact
  revision.  Re-running with an existing ``review_id`` resumes from the latest
  persisted revision: no completed round is repeated, no accepted repair is
  re-applied, and round budgets never reset because the draft changed.
* **Recheck on change.** A changed package/claim is detected with
  :func:`stale_checks`; the affected checks are invalidated and recomputed.
* **Read-only final validation.** :func:`fact_check` + :func:`evaluate` only (no
  writer/critic calls).  The final status is ``complete`` or ``blocked`` with an
  explicit ``required_human_action`` -- never an automatic pass.

Non-claims: this proves the mechanical obligations only.  It cannot establish
scientific truth, and a different model is not independent human verification.
"""

from __future__ import annotations

import copy
import json

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from ..storage.artifacts import ArtifactRepository
from .schema import (
    BLOCKING_SEVERITIES,
    ROUND_LIMITS,
    ReviewError,
    check_repair,
    evaluate,
    fact_check,
    normalize_finding,
    stale_checks,
    writer_response,
)

#: The generic artifact kind for a review revision (C01 §5.1).
ARTIFACT_KIND = "report"

#: The generic document envelope type for a section-review revision.
DOCUMENT_TYPE = "section_review"

#: The supported generic JSON document-envelope schema version.
SCHEMA_VERSION = 1

#: Human-readable reminder stored in every review state.
INDEPENDENT_VERIFICATION_NOTICE = (
    "A different concrete model with a separate context is not independent "
    "human verification; human adjudication remains required."
)

#: The stages, in order.
STAGES = ("critique", "fact_check", "final")


class SectionReviewError(ReviewAgentError):
    """A review input, resume state, or model identity is invalid."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SectionReviewError(message)


def _sentence(draft, target):
    """Return the draft sentence at ``target`` or ``None``."""
    for paragraph in draft.get("paragraphs") or []:
        if paragraph.get("paragraph_id") != target.get("paragraph_id"):
            continue
        for sentence in paragraph.get("sentences") or []:
            if sentence.get("intent_id") == target.get("intent_id"):
                return sentence
    return None


def _set_sentence(draft, target, source) -> None:
    """Overwrite the text/citation keys of the sentence at ``target``."""
    sentence = _sentence(draft, target)
    if sentence is None:
        return
    sentence["text"] = source.get("text", "")
    if "citation_keys" in source:
        sentence["citation_keys"] = list(source.get("citation_keys") or [])


def _new_state(review_id, draft, package, critic_model, writer_model):
    return {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "document_type": DOCUMENT_TYPE,
        "engine": {
            "critic_model": critic_model,
            "writer_model": writer_model,
            "separate_context": True,
            "different_model": True,
            "independent_human_verification": False,
            "notice": INDEPENDENT_VERIFICATION_NOTICE,
        },
        "draft": draft,
        "package": package,
        "findings": [],
        "checks": [],
        "rounds_used": {"critique": 0, "fact_check": 0},
        "critic_calls": 0,
        "repair_calls": 0,
        "invalidated_checks": [],
        "violations": [],
        "recheck_sentences": [],
        "phase": "critic",
        "status": "in_progress",
        "required_human_action": None,
    }


class SectionReviewRepository:
    """Durable, resumable Step 12 section review over a :class:`ProjectStore`.

    Typical use::

        repo = SectionReviewRepository(store)
        state = repo.run(
            "review-sec2",
            draft=draft, package=package,
            writer_model="openrouter/anthropic/claude-sonnet-5-5#high",
            critic_model="openrouter/deepseek/v4.1-flash#high",
            critic=my_critic_port, writer=my_writer_port,
            rights=lambda artifact_id, revision_id: my_rights(artifact_id, revision_id),
        )

    ``rights`` is a callable ``(artifact_id, revision_id) -> RightsRecord`` used
    to authorise each persisted revision (the record's subject binds that exact
    revision).  ``critic(draft, package) -> list[raw findings]`` and
    ``writer(draft, findings) -> {"draft": revised, "responses": {id: {...}}}``
    are injected ports; no real model call is made here.
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(store)
        self._rights = None

    # -- read / persist ------------------------------------------------------ #
    def read(self, review_id):
        """Return the latest persisted review state (or ``None``)."""
        artifact_id = validate_identifier(review_id, "review_id")
        head = self.artifacts.current_revision(artifact_id)
        if head is None:
            return None
        data = self.artifacts.read_content(artifact_id, head.revision_id)
        try:
            document = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise SectionReviewError("the stored review document is not decodable") from None
        _require(
            isinstance(document, dict)
            and document.get("schema_version") == SCHEMA_VERSION
            and document.get("document_type") == DOCUMENT_TYPE,
            "the stored document is not a section-review document",
        )
        payload = document.get("payload")
        _require(isinstance(payload, dict), "the stored review payload is malformed")
        return payload

    def _persist(self, state):
        """Append the full review state as a new immutable revision."""
        _require(self._rights is not None, "rights factory is not configured")
        artifact_id = validate_identifier(state["review_id"], "review_id")
        self.artifacts.create_artifact(artifact_id, kind=ARTIFACT_KIND, created_by="user")
        head = self.artifacts.current_revision(artifact_id)
        parent = None if head is None else head.revision_id
        revision_id = ArtifactRepository.new_revision_id()
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content={
                "schema_version": SCHEMA_VERSION,
                "document_type": DOCUMENT_TYPE,
                "payload": state,
            },
            origin_locator="review:section",
            acquired_by="user",
            rights=self._rights(artifact_id, revision_id),
            parent_revision_id=parent,
        )
        return revision_id

    # -- model identity ------------------------------------------------------ #
    @staticmethod
    def _require_models(critic_model, writer_model):
        for name, value in (("critic_model", critic_model), ("writer_model", writer_model)):
            _require(
                isinstance(value, str) and value.strip() != "",
                f"{name} must be a concrete non-empty model identifier",
            )
        _require(
            critic_model != writer_model,
            "the section critic must be a different concrete model than the writer",
        )

    def _require_same_engine(self, state, critic_model, writer_model):
        engine = state.get("engine") or {}
        _require(
            engine.get("critic_model") == critic_model
            and engine.get("writer_model") == writer_model,
            "the resumed review was created with different critic/writer models",
        )

    # -- staleness ----------------------------------------------------------- #
    def _refresh_checks(self, state, fact_checker):
        """Recompute the deterministic checks and record invalidated check ids."""
        computed = fact_checker(state["draft"], state["package"])
        previous = state.get("checks") or []
        if previous:
            for check_id in stale_checks(previous, state["draft"], state["package"]):
                if check_id not in state["invalidated_checks"]:
                    state["invalidated_checks"].append(check_id)
        state["checks"] = computed
        return computed

    # -- critique stage ------------------------------------------------------ #
    @staticmethod
    def _has_confirmed_blocker(state):
        return any(
            finding.get("disposition") == "confirmed"
            and finding.get("severity") in BLOCKING_SEVERITIES
            for finding in state["findings"]
        )

    @staticmethod
    def _round_repairs(state, round_no):
        return [
            finding
            for finding in state["findings"]
            if finding.get("stage") == "critique"
            and finding.get("round_no") == round_no
            and finding.get("disposition") == "confirmed"
            and finding.get("severity") in BLOCKING_SEVERITIES
        ]

    def _run_critique(self, state, *, critic, writer):
        limit = ROUND_LIMITS["critique"]
        while state["rounds_used"]["critique"] < limit:
            round_no = state["rounds_used"]["critique"]
            if (
                state["phase"] != "writer"
                and round_no > 0
                and not self._has_confirmed_blocker(state)
            ):
                break
            self._critique_round(state, round_no, critic=critic, writer=writer)

    def _critique_round(self, state, round_no, *, critic, writer):
        if state["phase"] != "writer":
            raw_findings = critic(state["draft"], state["package"])
            state["critic_calls"] += 1
            findings = [
                normalize_finding(
                    raw,
                    draft=state["draft"],
                    package=state["package"],
                    stage="critique",
                    round_no=round_no,
                )
                for raw in raw_findings
            ]
            state["findings"].extend(findings)
            state["phase"] = "writer"
            self._persist(state)
        repairs = self._round_repairs(state, round_no)
        if not repairs:
            state["rounds_used"]["critique"] += 1
            state["phase"] = "critic"
            self._persist(state)
            return
        old_draft = state["draft"]
        new_draft, responses = writer(old_draft, repairs)
        state["repair_calls"] += 1
        self._apply_repair(state, old_draft, new_draft, responses, repairs, stage="critique")
        state["rounds_used"]["critique"] += 1
        state["phase"] = "critic"
        self._persist(state)

    # -- repair application -------------------------------------------------- #
    def _replace_finding(self, state, finding_id, updated):
        for index, existing in enumerate(state["findings"]):
            if existing.get("finding_id") == finding_id:
                state["findings"][index] = updated
                return
        state["findings"].append(updated)

    def _mark_target_fixed(self, state, finding):
        if finding.get("stage") != "critique":
            return
        for existing in state["findings"]:
            if (
                existing.get("stage") == "critique"
                and existing.get("target") == finding.get("target")
                and existing.get("disposition") == "confirmed"
            ):
                existing["disposition"] = "fixed"
                existing["reason"] = "superseded by an accepted repair of the same sentence"

    def _record_violation(self, state, finding, violations, old_sentence):
        record = {
            "finding_id": finding.get("finding_id"),
            "target": finding.get("target"),
            "violations": list(violations),
            "old_text": old_sentence.get("text", ""),
            "rejected_repair": True,
        }
        state["violations"].append(record)
        for existing in state["findings"]:
            if existing.get("finding_id") == finding.get("finding_id"):
                existing["repair_violations"] = list(violations)
                existing["reason"] = "repair rejected: " + ", ".join(violations)

    def _apply_repair(self, state, old_draft, new_draft, responses, findings, *, stage):
        """Apply a writer result; reject any violating repair (keep the old text)."""
        new_draft = copy.deepcopy(new_draft)
        for finding in findings:
            response = responses.get(finding.get("finding_id"))
            if response is None:
                continue
            accept = bool(response.get("accept"))
            updated = writer_response(finding, accept=accept, reason=response.get("reason") or "")
            if not accept:
                self._replace_finding(state, finding.get("finding_id"), updated)
                continue
            old_sentence = _sentence(old_draft, finding.get("target"))
            new_sentence = _sentence(new_draft, finding.get("target"))
            if new_sentence is None:
                self._record_violation(state, finding, ["missing_target"], old_sentence or {})
                continue
            result = check_repair(old_sentence, new_sentence, package=state["package"])
            if result["violations"]:
                _set_sentence(new_draft, finding.get("target"), old_sentence or {})
                self._record_violation(state, finding, result["violations"], old_sentence or {})
                continue
            self._replace_finding(state, finding.get("finding_id"), updated)
            if stage == "critique":
                self._mark_target_fixed(state, finding)
            if result["reopen_checks"]:
                target = finding.get("target")
                if target not in state["recheck_sentences"]:
                    state["recheck_sentences"].append(target)
        state["draft"] = new_draft
        return state

    # -- fact-check stage ---------------------------------------------------- #
    def _check_finding(self, state, check):
        return {
            "finding_id": "fc-repair:" + str(check.get("check_id")),
            "stage": "fact_check",
            "round_no": state["rounds_used"]["fact_check"],
            "target": {
                "paragraph_id": check.get("paragraph_id"),
                "intent_id": check.get("intent_id"),
            },
            "kind": "unsupported_claim",
            "severity": "major",
            "objection": f"fact-check verdict: {check.get('verdict')}",
            "basis": None,
            "missing_check": None,
            "disposition": "confirmed",
            "reason": None,
        }

    def _run_fact_check(self, state, *, writer, fact_checker):
        self._refresh_checks(state, fact_checker)
        limit = ROUND_LIMITS["fact_check"]
        while state["rounds_used"]["fact_check"] < limit:
            failing = [
                check
                for check in state["checks"]
                if check.get("verdict") not in ("supported", "not_applicable")
            ]
            if not failing:
                break
            findings = [self._check_finding(state, check) for check in failing]
            old_draft = state["draft"]
            new_draft, responses = writer(old_draft, findings)
            state["repair_calls"] += 1
            self._apply_repair(state, old_draft, new_draft, responses, findings, stage="fact_check")
            state["rounds_used"]["fact_check"] += 1
            self._refresh_checks(state, fact_checker)
            self._persist(state)

    # -- final validation ---------------------------------------------------- #
    def _finalize(self, state, fact_checker):
        checks = fact_checker(state["draft"], state["package"])
        result = evaluate(state["findings"], checks, state["rounds_used"])
        state["checks"] = checks
        status = result["status"]
        required = result["required_human_action"]
        if status == "needs_repair":
            status = "blocked"
            required = (
                "Unresolved blocking/major findings or failed checks remain; a "
                "human must resolve them before completion."
            )
        state["status"] = status
        state["required_human_action"] = required if status == "blocked" else None
        state["validation"] = {
            "status": status,
            "unresolved": list(result["unresolved"]),
            "required_human_action": state["required_human_action"],
            "read_only": True,
        }
        self._persist(state)
        return state

    # -- entry point --------------------------------------------------------- #
    def run(
        self,
        review_id,
        *,
        draft,
        package,
        writer_model,
        critic_model,
        critic,
        writer,
        fact_checker=None,
        rights,
    ):
        """Run (or resume) one durable section review; return the final state."""
        self._require_models(critic_model, writer_model)
        fact_checker = fact_checker or fact_check
        self._rights = rights
        artifact_id = validate_identifier(review_id, "review_id")

        persisted = self.read(artifact_id)
        if persisted is None:
            state = _new_state(artifact_id, draft, package, critic_model, writer_model)
            self._persist(state)
        else:
            state = persisted
            self._require_same_engine(state, critic_model, writer_model)
        # The current package is authoritative: a changed passage is detected by
        # ``_refresh_checks`` inside the fact-check stage.
        state["package"] = package

        self._run_critique(state, critic=critic, writer=writer)
        self._run_fact_check(state, writer=writer, fact_checker=fact_checker)
        self._finalize(state, fact_checker)
        return state


__all__ = [
    "ARTIFACT_KIND",
    "DOCUMENT_TYPE",
    "INDEPENDENT_VERIFICATION_NOTICE",
    "SCHEMA_VERSION",
    "STAGES",
    "SectionReviewError",
    "SectionReviewRepository",
]
