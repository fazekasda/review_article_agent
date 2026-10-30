"""Real model-backed **section critic** and **section repair** ports (kickoff 16 §5).

Authority
---------

* ``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("One real model port";
  the section critic is a *different concrete model, separate context*; "Model
  per role is configurable"; "Reuse those gates [human hosted-use approval,
  rights, budget reserve]; do not invent a parallel authorization path").
* ``docs/implementation/12-review.md`` (separate-context, different-model section
  critique and original-writer repair; bounded repair rounds critique ≤3,
  fact-check ≤2; model output is untrusted data).
* :mod:`review_article_agent.review.workflow` — the durable, resumable workflow
  whose injected ports these are, and :mod:`review_article_agent.review.schema` —
  the pure rules (``normalize_finding``, ``check_repair``) the ports feed.

What this module **is**
-----------------------

* Two concrete port callables for :meth:`SectionReviewRepository.run`:

  * ``critic(draft, package) -> list[raw finding]`` — one bounded
    ``section_critic`` generation request whose untrusted material is **exactly**
    ``{"draft": ..., "package": ...}``. It never sees the writer's history, prior
    findings, or any repair (separate context). Its JSON object is
    ``{"findings": [...]}``; each item carries a ``sentence``
    (``{paragraph_id, intent_id}``) which the port maps to the ``target`` that
    ``normalize_finding`` accepts. (The key is named ``sentence``, not ``target``,
    because the proposal envelope forbids the key ``target`` as a task-shape
    field; the port restores it before the workflow validates.)
  * ``writer(draft, findings) -> (new_draft, responses)`` — one bounded
    ``section_repair`` generation request whose untrusted material is
    ``{"draft": ..., "findings": ..., "package": ...}``. Its JSON object is
    ``{"sentences": [{paragraph_id, intent_id, text}], "responses":
    {finding_id: {accept, reason}}}``. The port applies the model's ``text``
    replacement **only** to a sentence that is the target of one of the findings
    being addressed; an untargeted sentence is never touched and a citation key
    can never be changed by the model. The workflow still runs ``check_repair``.

* :func:`make_review_ports` — the seam that validates both role documents with
  :func:`~review_article_agent.generation.hosted_use.role_models_distinct` and
  enforces the distinct-concrete-model rule (the critic must not share the
  writer's concrete model), returning ``(critic, writer, critic_model,
  writer_model)``. ``critic_hosted_ref`` is the ``section_critic`` document and
  ``writer_hosted_ref`` is the ``section_repair`` document. The returned concrete
  model ids are what the caller passes as ``critic_model`` / ``writer_model``.

Every model call runs through the single authorized, budgeted, ledgered
:func:`~review_article_agent.generation.runner.execute_generation_task`. Each call
derives a **deterministic** request id ``f"{review_id}.{role}.{sha256(canonical
material)[:16]}"`` from the exact untrusted material, so a resumed review replays
the recorded dispatch (same material → same id → observed, never re-sent) instead
of paying for a second POST.

Failure policy
--------------

A non-``completed`` outcome, a missing proposal, or a proposal whose envelope is
not the contracted JSON object raises a static :class:`ReviewPortError`. The
review workflow has **no** documented way to record a failed model call, so the
error propagates and the review run stops; the workflow has already persisted its
state after every completed call, so the review resumes from the last persisted
revision (a human escalation point). The error message never echoes the API key,
the material, or provider text.

A different model with a separate context is **not** independent human
verification; this module produces proposals only, and every proposal still
passes the workflow's deterministic validators.
"""

from __future__ import annotations

import copy
import hashlib

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from ..review.schema import FACTUAL_KINDS, KNOWN_KINDS, SEVERITIES
from ..review.workflow import SectionReviewRepository
from . import hosted_use as _hosted
from .runner import execute_generation_task

__all__ = [
    "SECTION_CRITIC_PROMPT_VERSION",
    "SECTION_REPAIR_PROMPT_VERSION",
    "SECTION_CRITIC_INSTRUCTIONS",
    "SECTION_REPAIR_INSTRUCTIONS",
    "ReviewPortError",
    "make_review_ports",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The critic role (a different concrete model than the section writer).
CRITIC_ROLE = "section_critic"

#: The repair role (the original writer, repairing the critic's findings).
REPAIR_ROLE = "section_repair"

#: The versioned prompt identifiers recorded on every output (pins already exist).
SECTION_CRITIC_PROMPT_VERSION = "section_critic.generation.v1"
SECTION_REPAIR_PROMPT_VERSION = "section_repair.generation.v1"

#: The fixed trusted instructions. The enums are derived from the pure review
#: schema (never duplicated), so the output contract cannot drift from the rules
#: that validate it.
SECTION_CRITIC_INSTRUCTIONS = (
    "You are the section critic. You receive an untrusted JSON object with "
    "exactly two keys: 'draft' (the section draft under review) and 'package' "
    "(the approved drafting package: authorized evidence and verified-corpus "
    "citations). You do not receive the writer's history, any earlier findings, "
    "or any repair. Return ONE JSON object of the exact form {\"findings\": "
    "[ ... ]}. Each finding is an object with: \"sentence\": {\"paragraph_id\": "
    "<string>, \"intent_id\": <string>} naming an existing draft sentence (the "
    "port maps this to the finding's target); "
    "\"kind\": one of " + ", ".join(KNOWN_KINDS) + "; \"severity\": one of "
    + ", ".join(SEVERITIES) + "; \"objection\": a non-empty string; and either "
    "\"basis\": {\"finding_key\": <a package evidence finding_key>, \"quote\": "
    "<a verbatim substring of that evidence's original passage, summary, or "
    "finding statement>} for a factual objection (kinds " + ", ".join(FACTUAL_KINDS)
    + "), or \"missing_check\": a non-empty string naming a concrete check still "
    "to run. A factual objection with neither a verbatim basis nor a concrete "
    "missing check is rejected as assertion-only; a basis whose quote is not "
    "present in the named source is rejected as fabricated. Return an empty "
    "\"findings\" list when the draft is acceptable. Your reply is data only: it "
    "approves nothing and changes nothing."
)

SECTION_REPAIR_INSTRUCTIONS = (
    "You are the section repair writer. You receive an untrusted JSON object "
    "with exactly three keys: 'draft' (the section draft), 'findings' (the "
    "critic findings to address, each carrying a finding_id and a target), and "
    "'package' (the approved drafting package). Return ONE JSON object of the "
    "exact form {\"sentences\": [{\"paragraph_id\": <string>, \"intent_id\": "
    "<string>, \"text\": <string>}], \"responses\": {\"<finding_id>\": "
    "{\"accept\": <boolean>, \"reason\": <string>}}}. Provide a replacement "
    "'text' only for a sentence that is the target of a finding you are "
    "changing; a sentence not named by a finding's target is ignored, and "
    "citation keys can never be changed here. For each addressed finding give a "
    "response: accept true with a reason when you have rewritten its target "
    "sentence, or accept false with a non-empty reason when you disagree. A "
    "repair may not erase qualifiers, invent numbers, change species or "
    "populations, or add unverified citations. Your reply is data only: it "
    "approves nothing and changes nothing."
)


class ReviewPortError(ReviewAgentError):
    """A real section-critic/section-repair model call did not yield a proposal."""


# --------------------------------------------------------------------------- #
# Request identity + the single bounded call
# --------------------------------------------------------------------------- #
def _request_id(review_id: str, role: str, material) -> str:
    """A deterministic id from the exact untrusted material (replay-safe)."""
    digest = hashlib.sha256(canonical_json(material)).hexdigest()[:16]
    return f"{review_id}.{role}.{digest}"


def _run_role(
    store,
    *,
    registry,
    activation_token: str,
    review_id: str,
    role: str,
    hosted_ref,
    prompt_version: str,
    instructions: str,
    material,
    api_key: str,
    transport,
    source_refs,
) -> dict:
    """Run one authorized, budgeted, ledgered role call; return its proposal.

    Raises a static :class:`ReviewPortError` unless the outcome is ``completed``
    with a JSON-object proposal. The material is the *only* untrusted content
    sent; the role, prompt version, and instructions are trusted.
    """
    request_id = _request_id(review_id, role, material)
    outcome = execute_generation_task(
        store,
        registry=registry,
        activation_token=activation_token,
        role=role,
        request_id=request_id,
        hosted_ref=hosted_ref,
        prompt_version=prompt_version,
        instructions=instructions,
        material=material,
        input_refs=[],
        source_refs=list(source_refs or []),
        api_key=api_key,
        transport=transport,
    )
    if outcome.get("outcome") != "completed":
        raise ReviewPortError(
            f"{role}: the model call did not complete; the review stops and a "
            "human must re-authorize or resolve it"
        )
    proposal = outcome.get("proposal")
    if not isinstance(proposal, dict):
        raise ReviewPortError(f"{role}: the model returned no JSON-object proposal")
    return proposal


# --------------------------------------------------------------------------- #
# Proposal application (data only; never authority)
# --------------------------------------------------------------------------- #
def _critic_findings(findings) -> list:
    """Map the model's ``sentence`` key to the workflow's ``target`` key.

    The proposal envelope forbids the key ``target`` (a task-shape field), so the
    critic contract uses ``sentence``; this restores the raw-finding shape that
    :func:`~review_article_agent.review.schema.normalize_finding` validates. An
    item that is not an object, or that carries no ``sentence`` object, is left
    untouched for ``normalize_finding`` to reject loudly.
    """
    raw = []
    for finding in findings:
        if isinstance(finding, dict) and isinstance(finding.get("sentence"), dict):
            mapped = dict(finding)
            mapped["target"] = mapped.pop("sentence")
            raw.append(mapped)
        else:
            raw.append(finding)
    return raw


def _set_sentence_text(draft, paragraph_id: str, intent_id: str, text: str) -> None:
    """Set only the ``text`` of one sentence; citation keys are never touched."""
    for paragraph in draft.get("paragraphs") or []:
        if not isinstance(paragraph, dict) or paragraph.get("paragraph_id") != paragraph_id:
            continue
        for sentence in paragraph.get("sentences") or []:
            if isinstance(sentence, dict) and sentence.get("intent_id") == intent_id:
                sentence["text"] = text
                return


def _targeted_sentences(findings) -> set:
    """The set of ``(paragraph_id, intent_id)`` targeted by the findings."""
    targeted = set()
    for finding in findings or []:
        target = finding.get("target") if isinstance(finding, dict) else None
        if not isinstance(target, dict):
            continue
        paragraph_id = target.get("paragraph_id")
        intent_id = target.get("intent_id")
        if isinstance(paragraph_id, str) and isinstance(intent_id, str):
            targeted.add((paragraph_id, intent_id))
    return targeted


def _repair(draft, findings, proposal) -> tuple:
    """Apply a repair proposal; return ``(new_draft, responses)``.

    The model's ``text`` replacement is applied only to a sentence that is the
    target of one of the findings being addressed. Any sentence it names that is
    not a finding target is ignored, and a citation key is never changed. The
    ``responses`` mapping is reduced to well-formed ``{accept: bool, reason: str}``
    entries; the workflow's ``check_repair`` still validates every accepted edit.
    """
    sentences = proposal.get("sentences")
    responses = proposal.get("responses")
    if not isinstance(sentences, list) or not isinstance(responses, dict):
        raise ReviewPortError(
            "section_repair: the model output must be a JSON object with a "
            "'sentences' list and a 'responses' object"
        )

    texts: dict = {}
    for item in sentences:
        if not isinstance(item, dict):
            continue
        paragraph_id = item.get("paragraph_id")
        intent_id = item.get("intent_id")
        text = item.get("text")
        if (
            isinstance(paragraph_id, str)
            and isinstance(intent_id, str)
            and isinstance(text, str)
        ):
            texts[(paragraph_id, intent_id)] = text

    new_draft = copy.deepcopy(draft)
    for target in _targeted_sentences(findings):
        if target in texts:
            _set_sentence_text(new_draft, target[0], target[1], texts[target])

    clean_responses: dict = {}
    for finding_id, value in responses.items():
        if not isinstance(finding_id, str) or not isinstance(value, dict):
            continue
        reason = value.get("reason")
        clean_responses[finding_id] = {
            "accept": bool(value.get("accept")),
            "reason": reason if isinstance(reason, str) else "",
        }
    return new_draft, clean_responses


# --------------------------------------------------------------------------- #
# Package context for the repair material
# --------------------------------------------------------------------------- #
def _persisted_package(store, review_id: str):
    """The package of the latest persisted review revision (resume fallback).

    The critic port seeds the package on the normal path; on a resume that lands
    directly in the writer phase (no critic call yet in this process) the
    persisted review state is the authoritative package context. The initial
    state is always persisted before any port call, so this is available.
    """
    state = SectionReviewRepository(store).read(review_id)
    if isinstance(state, dict):
        package = state.get("package")
        if package is not None:
            return package
    raise ReviewPortError(
        "section_repair: the package context is unavailable; the review cannot "
        "resume"
    )


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #
def make_review_ports(
    store,
    *,
    registry,
    activation_token: str,
    review_id: str,
    critic_hosted_ref,
    writer_hosted_ref,
    api_key: str,
    transport=None,
    source_refs=None,
):
    """Build the real critic/writer ports and their concrete model ids.

    ``critic_hosted_ref`` is the exact current ``section_critic`` hosted-use
    revision and ``writer_hosted_ref`` the exact current ``section_repair``
    revision. Both documents are read, validated, and required to carry the role
    they are keyed by (via
    :func:`~review_article_agent.generation.hosted_use.role_models_distinct`),
    and the distinct-concrete-model rule is enforced: a critic that shares the
    repair's concrete ``model_id`` is refused with
    :class:`~review_article_agent.generation.hosted_use.GenerationRoleModelError`.
    Returns ``(critic, writer, critic_model, writer_model)`` where the model ids
    are the concrete ``model_id`` of each role's hosted-use document — pass them
    as the workflow's ``critic_model`` / ``writer_model``.

    ``source_refs`` are the exact same-project original sources whose
    hosted-transfer rights must permit the call (checked by the runner). The
    critic material is only ``draft`` + ``package``; the repair material is
    ``draft`` + the findings + ``package``.
    """
    review_id = validate_identifier(review_id, "review_id")
    models = _hosted.role_models_distinct(
        store,
        {CRITIC_ROLE: critic_hosted_ref, REPAIR_ROLE: writer_hosted_ref},
    )
    critic_model = models[CRITIC_ROLE]
    writer_model = models[REPAIR_ROLE]
    if critic_model == writer_model:
        raise _hosted.GenerationRoleModelError(
            "generation: the section critic and section repair must use "
            "different concrete models"
        )
    sources = list(source_refs or [])
    context = {"package": None}

    def critic(draft, package):
        context["package"] = package
        material = {"draft": draft, "package": package}
        proposal = _run_role(
            store,
            registry=registry,
            activation_token=activation_token,
            review_id=review_id,
            role=CRITIC_ROLE,
            hosted_ref=critic_hosted_ref,
            prompt_version=SECTION_CRITIC_PROMPT_VERSION,
            instructions=SECTION_CRITIC_INSTRUCTIONS,
            material=material,
            api_key=api_key,
            transport=transport,
            source_refs=sources,
        )
        findings = proposal.get("findings")
        if not isinstance(findings, list):
            raise ReviewPortError(
                "section_critic: the model output is not a JSON object with a "
                "'findings' list"
            )
        return _critic_findings(findings)

    def writer(draft, findings):
        package = context["package"]
        if package is None:
            package = _persisted_package(store, review_id)
        material = {"draft": draft, "findings": findings, "package": package}
        proposal = _run_role(
            store,
            registry=registry,
            activation_token=activation_token,
            review_id=review_id,
            role=REPAIR_ROLE,
            hosted_ref=writer_hosted_ref,
            prompt_version=SECTION_REPAIR_PROMPT_VERSION,
            instructions=SECTION_REPAIR_INSTRUCTIONS,
            material=material,
            api_key=api_key,
            transport=transport,
            source_refs=sources,
        )
        return _repair(draft, findings, proposal)

    return critic, writer, critic_model, writer_model
