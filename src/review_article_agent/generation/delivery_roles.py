"""Real model-backed **global reviewer**, **global writer**, and **notes planner**
ports for the Step 14 delivery workflow (kickoff 16 §5).

Authority
---------

* ``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("One real model port";
  the global reviewer must use a different concrete model than the global writer;
  versioned prompt templates in the code with ``prompt_version`` recorded on
  outputs; model output stays a proposal and still passes the existing validators
  and human gates).
* ``docs/implementation/14-delivery.md`` (whole-manuscript review with a bounded
  repair budget of at most two rounds; a human notes action plan with a
  once-per-batch approved edit; author-note edits are proposed, then applied only
  under an explicit human ``approval_id``; no model-invented citation).
* :mod:`review_article_agent.delivery.workflow` — the durable, resumable workflow
  whose injected ports these are; :mod:`review_article_agent.delivery.schema` —
  the pure rules (``EDIT_KINDS``, ``BLOCKING_SEVERITIES``, the once-per-batch
  apply) these ports feed; :mod:`review_article_agent.delivery.export` —
  ``cited_keys`` (the citation-key vocabulary the ports enforce).

What this module **is**
-----------------------

* :func:`make_global_review_ports` — builds the two concrete port callables for
  :meth:`~review_article_agent.delivery.workflow.DeliveryRepository.run_global_review`:

  * ``reviewer(manuscript_text) -> list[raw finding]`` — one bounded
    ``global_reviewer`` generation request whose untrusted material is
    ``{"manuscript": ..., "paragraphs": [...]}``. Its JSON object is
    ``{"findings": [...]}``; each finding names an existing paragraph
    (``paragraph_id``) and carries a ``severity`` (one of
    :data:`GLOBAL_SEVERITIES`) and a ``status`` (one of :data:`FINDING_STATUSES`).
    Only an ``open``/``disputed`` finding of a
    :data:`~review_article_agent.delivery.schema.BLOCKING_SEVERITIES` severity
    blocks completion.
  * ``writer(manuscript_text, findings) -> new_text`` — one bounded
    ``global_writer`` generation request whose untrusted material is
    ``{"manuscript": ..., "findings": ..., "paragraphs": [...]}``. Its JSON
    object is ``{"paragraphs": [{"paragraph_id": ..., "text": ...}]}``. A
    replacement ``text`` is applied **only** to a paragraph that is the target of
    one of the findings being addressed; an untargeted paragraph is never
    touched, and a replacement that would introduce a citation key **not already
    present** in the manuscript is rejected (the old paragraph text is kept).
    The workflow still runs ``evaluate_global`` and its bounded loop.

* :func:`plan_note_edits` — reads the recorded author notes and the current
  manuscript from the delivery state, runs one bounded ``notes_planner``
  generation request, validates every proposed edit against the delivery schema
  (known note, known paragraph, a valid ``kind``, and no new citation key), and
  returns a **proposal** batch. It **never** calls ``apply_notes``: the human
  approves the batch (an ``approval_id``), and only then does the caller apply it.

The concrete models are validated with
:func:`~review_article_agent.generation.hosted_use.role_models_distinct`, which
enforces the spec's distinct-model rule (``global_reviewer`` must not share the
``global_writer``'s concrete ``model_id``).

Every model call runs through the single authorized, budgeted, ledgered
:func:`~review_article_agent.generation.runner.execute_generation_task`. Each call
derives a **deterministic** request id
``f"{delivery_id}.{role}.{sha256(canonical material)[:16]}"`` from the exact
untrusted material, so a resumed delivery replays the recorded dispatch (same
material → same id → observed, never re-sent) instead of paying for a second POST.

What this module **is not**
---------------------------

* **Not an authorizer.** Model output is data only: findings and edits are
  proposals that still pass the workflow's deterministic rules; an author-note
  edit is applied only after an explicit human ``approval_id``.
* Not the runner, the store, the workflow, or the CLI. It owns no gate, budget,
  dispatch, or rights decision — those are the runner's, reused verbatim.

A different model with a separate context is **not** independent human
verification. Every failure is a static, value-free error; no message ever echoes
the API key, the material, or provider text.
"""

from __future__ import annotations

import hashlib
import re

from ..delivery.export import cited_keys
from ..delivery.schema import (
    BLOCKING_SEVERITIES,
    EDIT_KINDS,
)
from ..delivery.workflow import DeliveryRepository
from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from . import hosted_use as _hosted
from .runner import execute_generation_task

__all__ = [
    "GLOBAL_REVIEWER_ROLE",
    "GLOBAL_WRITER_ROLE",
    "NOTES_PLANNER_ROLE",
    "GLOBAL_REVIEWER_PROMPT_VERSION",
    "GLOBAL_WRITER_PROMPT_VERSION",
    "NOTES_PLANNER_PROMPT_VERSION",
    "GLOBAL_REVIEWER_INSTRUCTIONS",
    "GLOBAL_WRITER_INSTRUCTIONS",
    "NOTES_PLANNER_INSTRUCTIONS",
    "GLOBAL_SEVERITIES",
    "FINDING_STATUSES",
    "DeliveryRoleError",
    "make_global_review_ports",
    "plan_note_edits",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The global reviewer role (a different concrete model than the global writer).
GLOBAL_REVIEWER_ROLE = "global_reviewer"

#: The global writer role (proposes bounded whole-manuscript edits).
GLOBAL_WRITER_ROLE = "global_writer"

#: The notes planner role (proposes an author-note edit batch for human approval).
NOTES_PLANNER_ROLE = "notes_planner"

#: The versioned prompt identifiers recorded on every output (the pins exist).
GLOBAL_REVIEWER_PROMPT_VERSION = "global_reviewer.generation.v1"
GLOBAL_WRITER_PROMPT_VERSION = "global_writer.generation.v1"
NOTES_PLANNER_PROMPT_VERSION = "notes_planner.generation.v1"

#: The finding severities the reviewer may use: the schema's blocking severities
#: plus the non-blocking ones. Only :data:`BLOCKING_SEVERITIES` block completion.
GLOBAL_SEVERITIES = tuple(BLOCKING_SEVERITIES) + ("minor", "style")

#: The finding lifecycle states ``evaluate_global`` recognises.
FINDING_STATUSES = ("open", "disputed", "resolved")

#: Note plan actions that may be turned into an edit (mirrors the workflow's
#: ``_EDITABLE_ACTIONS``; a note with any other action is not editable).
_EDITABLE_ACTIONS = ("local_edit", "claim_edit")

#: A blank-line paragraph separator, used to derive stable paragraph ids.
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


class DeliveryRoleError(ReviewAgentError):
    """A real global-reviewer/writer/notes-planner call did not yield a proposal."""


# --------------------------------------------------------------------------- #
# Fixed, versioned instructions (enums derived from the schema, never guessed)
# --------------------------------------------------------------------------- #
GLOBAL_REVIEWER_INSTRUCTIONS = (
    "You are the global reviewer. You receive an untrusted JSON object with "
    "exactly two keys: 'manuscript' (the assembled manuscript text) and "
    "'paragraphs' (the manuscript's paragraphs, each {\"paragraph_id\": "
    "<string>, \"text\": <string>}). You review the whole manuscript for "
    "consistency, coverage, contradictions, and unsupported or overstated "
    "claims. Return ONE JSON object of the exact form {\"findings\": [ ... ]}. "
    "Each finding is an object with: \"finding_id\": <string>; \"paragraph_id\": "
    "<one of the paragraph ids>; \"kind\": <string>; \"severity\": one of "
    + ", ".join(GLOBAL_SEVERITIES) + "; \"status\": one of "
    + ", ".join(FINDING_STATUSES) + "; and \"objection\": a non-empty string. "
    "Only a finding whose severity is one of " + ", ".join(BLOCKING_SEVERITIES)
    + " and whose status is 'open' or 'disputed' blocks completion. Report "
    "uncertainty and limitations explicitly; never invent a citation, a source, "
    "or a fact the manuscript and its evidence do not support. Return an empty "
    "\"findings\" list when the manuscript is acceptable. Your reply is data "
    "only: it approves nothing and changes nothing."
)

GLOBAL_WRITER_INSTRUCTIONS = (
    "You are the global writer. You receive an untrusted JSON object with "
    "exactly three keys: 'manuscript' (the assembled manuscript text), "
    "'findings' (the reviewer findings to address, each carrying a finding_id "
    "and a paragraph_id), and 'paragraphs' (the manuscript's paragraphs, each "
    "{\"paragraph_id\": <string>, \"text\": <string>}). Return ONE JSON object "
    "of the exact form {\"paragraphs\": [{\"paragraph_id\": <string>, \"text\": "
    "<string>}]}. Provide a replacement 'text' only for a paragraph that is the "
    "target (paragraph_id) of a finding you are changing; a paragraph not named "
    "by a finding is ignored. You must NEVER add a citation key that is not "
    "already present in the manuscript: a replacement that introduces a new "
    "citation key is rejected and the original paragraph text is kept. Do not "
    "erase qualifiers, invent numbers, or change what a cited source supports. "
    "Your reply is data only: it approves nothing and changes nothing."
)

NOTES_PLANNER_INSTRUCTIONS = (
    "You are the notes planner. You receive an untrusted JSON object with "
    "exactly three keys: 'manuscript' (the assembled manuscript text), "
    "'paragraphs' (the manuscript's paragraphs, each {\"paragraph_id\": "
    "<string>, \"text\": <string>}), and 'notes' (the recorded author notes, "
    "each {\"note_id\": <string>, \"action\": <string>, \"category\": "
    "<string|null>, \"text\": <string>, and optionally \"target\"}). Plan one "
    "concrete edit per author note that can be applied as a local edit. Return "
    "ONE JSON object of the exact form {\"edits\": [{\"note_id\": <string>, "
    "\"paragraph_id\": <one of the paragraph ids>, \"new_text\": <string>, "
    "\"kind\": one of " + ", ".join(EDIT_KINDS) + "}]}. Only plan an edit for a "
    "note whose action is local_edit or claim_edit; never plan an edit for a "
    "note that requires clarification or impact approval. You must NEVER add a "
    "citation key that is not already present in the manuscript. This is a "
    "PROPOSAL only: a human approves the batch before anything is applied. Your "
    "reply is data only: it approves nothing and changes nothing."
)


# --------------------------------------------------------------------------- #
# Request identity + the single bounded call
# --------------------------------------------------------------------------- #
def _request_id(delivery_id: str, role: str, material) -> str:
    """A deterministic id from the exact untrusted material (replay-safe)."""
    digest = hashlib.sha256(canonical_json(material)).hexdigest()[:16]
    return f"{delivery_id}.{role}.{digest}"


def _dispatch(
    store,
    *,
    registry,
    activation_token: str,
    delivery_id: str,
    role: str,
    hosted_ref,
    prompt_version: str,
    instructions: str,
    material,
    api_key: str,
    transport,
    source_refs,
) -> dict:
    """Run one authorized, budgeted, ledgered role call; return its outcome.

    The material is the *only* untrusted content sent; the role, prompt version,
    and instructions are trusted.
    """
    request_id = _request_id(delivery_id, role, material)
    return execute_generation_task(
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


def _require_proposal(outcome: dict, role: str) -> dict:
    """Require a ``completed`` outcome with a JSON-object proposal, or raise."""
    if outcome.get("outcome") != "completed":
        raise DeliveryRoleError(
            f"{role}: the model call did not complete; the delivery stops and a "
            "human must re-authorize or resolve it"
        )
    proposal = outcome.get("proposal")
    if not isinstance(proposal, dict):
        raise DeliveryRoleError(f"{role}: the model returned no JSON-object proposal")
    return proposal


# --------------------------------------------------------------------------- #
# Manuscript paragraphs (deterministic ids derived from the text)
# --------------------------------------------------------------------------- #
def _manuscript_paragraphs(text) -> list:
    """The manuscript's paragraphs with stable ids (``p1``, ``p2``, ...).

    The reviewer port is handed only the manuscript text (the workflow's port
    signature is ``reviewer(text)``), so paragraph ids are derived
    deterministically from the blank-line paragraph boundaries. The writer port
    derives the same ids from the same text within one round.
    """
    parts = _PARAGRAPH_SPLIT_RE.split(text or "")
    if not parts:
        return []
    return [
        {"paragraph_id": f"p{index + 1}", "text": part}
        for index, part in enumerate(parts)
    ]


def _paragraph_texts(paragraphs) -> dict:
    return {
        paragraph["paragraph_id"]: paragraph["text"]
        for paragraph in paragraphs
        if isinstance(paragraph, dict)
        and isinstance(paragraph.get("paragraph_id"), str)
        and isinstance(paragraph.get("text"), str)
    }


# --------------------------------------------------------------------------- #
# Reviewer output normalisation
# --------------------------------------------------------------------------- #
def _reviewer_findings(findings) -> list:
    """Normalise the reviewer's raw findings into the workflow's shape.

    Every item that is not an object is dropped. A missing ``status`` defaults to
    ``open`` (fail-closed: an unstated status never silently clears a finding). A
    missing ``finding_id`` is synthesised from the item's position. The workflow
    then runs ``evaluate_global`` over the result.
    """
    raw = []
    for index, finding in enumerate(findings or []):
        if not isinstance(finding, dict):
            continue
        mapped = dict(finding)
        if not isinstance(mapped.get("finding_id"), str):
            mapped["finding_id"] = f"gr-{index + 1}"
        if mapped.get("status") not in FINDING_STATUSES:
            mapped["status"] = "open"
        raw.append(mapped)
    return raw


# --------------------------------------------------------------------------- #
# Writer application (targeted paragraphs only; no new citation keys)
# --------------------------------------------------------------------------- #
def _targeted_paragraphs(findings) -> list:
    """The ordered paragraph ids targeted by the findings (never duplicated)."""
    targeted: list = []
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        paragraph_id = finding.get("paragraph_id")
        if not isinstance(paragraph_id, str):
            target = finding.get("target")
            paragraph_id = target.get("paragraph_id") if isinstance(target, dict) else None
        if isinstance(paragraph_id, str) and paragraph_id not in targeted:
            targeted.append(paragraph_id)
    return targeted


def _writer_text(text: str, findings, proposal) -> str:
    """Apply a global-writer proposal; return the new manuscript text.

    A replacement ``text`` is applied only to a paragraph that is the target of
    one of the findings being addressed. A replacement that would introduce a
    citation key not already present in the manuscript is rejected (the original
    paragraph text is kept). Untargeted paragraphs are never touched.
    """
    proposed = proposal.get("paragraphs")
    if not isinstance(proposed, list):
        raise DeliveryRoleError(
            "global_writer: the model output must be a JSON object with a "
            "'paragraphs' list"
        )

    replacements: dict = {}
    for item in proposed:
        if not isinstance(item, dict):
            continue
        paragraph_id = item.get("paragraph_id")
        new_text = item.get("text")
        if isinstance(paragraph_id, str) and isinstance(new_text, str):
            replacements[paragraph_id] = new_text

    allowed_keys = set(cited_keys(text))
    old_texts = _paragraph_texts(_manuscript_paragraphs(text))

    new_text = text
    for paragraph_id in _targeted_paragraphs(findings):
        if paragraph_id not in replacements:
            continue
        candidate = replacements[paragraph_id]
        if set(cited_keys(candidate)) - allowed_keys:
            # The edit would add a citation key absent from the manuscript: the
            # original paragraph text is kept (no invented citation).
            continue
        old = old_texts.get(paragraph_id)
        if old and old in new_text:
            new_text = new_text.replace(old, candidate, 1)
    return new_text


# --------------------------------------------------------------------------- #
# Notes planner validation
# --------------------------------------------------------------------------- #
def _validate_edit(edit, *, known_notes, paragraphs, allowed_keys) -> tuple:
    """Validate one proposed edit; return ``(clean_edit, None)`` or ``(None, reason)``.

    The reason is a small, static, value-free code drawn from the delivery schema:
    ``malformed_edit``, ``unknown_note``, ``note_not_editable``,
    ``unknown_paragraph``, ``bad_kind``, or ``new_citation_key``.
    """
    if not isinstance(edit, dict):
        return None, "malformed_edit"
    note_id = edit.get("note_id")
    paragraph_id = edit.get("paragraph_id")
    kind = edit.get("kind")
    new_text = edit.get("new_text")
    if not (
        isinstance(note_id, str)
        and isinstance(paragraph_id, str)
        and isinstance(kind, str)
        and isinstance(new_text, str)
    ):
        return None, "malformed_edit"
    note = known_notes.get(note_id)
    if note is None:
        return None, "unknown_note"
    if note.get("action") not in _EDITABLE_ACTIONS:
        return None, "note_not_editable"
    if paragraph_id not in paragraphs:
        return None, "unknown_paragraph"
    if kind not in EDIT_KINDS:
        return None, "bad_kind"
    if set(cited_keys(new_text)) - allowed_keys:
        return None, "new_citation_key"
    return {
        "note_id": note_id,
        "paragraph_id": paragraph_id,
        "new_text": new_text,
        "kind": kind,
    }, None


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #
def make_global_review_ports(
    store,
    *,
    registry,
    activation_token: str,
    delivery_id: str,
    reviewer_hosted_ref,
    writer_hosted_ref,
    api_key: str,
    transport=None,
    source_refs=None,
):
    """Build the real global reviewer/writer ports.

    ``reviewer_hosted_ref`` is the exact current ``global_reviewer`` hosted-use
    revision and ``writer_hosted_ref`` the exact current ``global_writer``
    revision. Both documents are read, validated, and required to carry the role
    they are keyed by, and the distinct-concrete-model rule is enforced: a
    reviewer that shares the writer's concrete ``model_id`` is refused with
    :class:`~review_article_agent.generation.hosted_use.GenerationRoleModelError`.
    Returns ``(reviewer, writer)``.

    ``source_refs`` are the exact same-project original sources whose
    hosted-transfer rights must permit the call (checked by the runner).
    """
    delivery_id = validate_identifier(delivery_id, "delivery_id")
    _hosted.role_models_distinct(
        store,
        {
            GLOBAL_REVIEWER_ROLE: reviewer_hosted_ref,
            GLOBAL_WRITER_ROLE: writer_hosted_ref,
        },
    )
    sources = list(source_refs or [])

    def reviewer(manuscript_text):
        paragraphs = _manuscript_paragraphs(manuscript_text)
        material = {"manuscript": manuscript_text, "paragraphs": paragraphs}
        outcome = _dispatch(
            store,
            registry=registry,
            activation_token=activation_token,
            delivery_id=delivery_id,
            role=GLOBAL_REVIEWER_ROLE,
            hosted_ref=reviewer_hosted_ref,
            prompt_version=GLOBAL_REVIEWER_PROMPT_VERSION,
            instructions=GLOBAL_REVIEWER_INSTRUCTIONS,
            material=material,
            api_key=api_key,
            transport=transport,
            source_refs=sources,
        )
        proposal = _require_proposal(outcome, GLOBAL_REVIEWER_ROLE)
        findings = proposal.get("findings")
        if not isinstance(findings, list):
            raise DeliveryRoleError(
                "global_reviewer: the model output is not a JSON object with a "
                "'findings' list"
            )
        return _reviewer_findings(findings)

    def writer(manuscript_text, findings):
        paragraphs = _manuscript_paragraphs(manuscript_text)
        material = {
            "manuscript": manuscript_text,
            "findings": findings,
            "paragraphs": paragraphs,
        }
        outcome = _dispatch(
            store,
            registry=registry,
            activation_token=activation_token,
            delivery_id=delivery_id,
            role=GLOBAL_WRITER_ROLE,
            hosted_ref=writer_hosted_ref,
            prompt_version=GLOBAL_WRITER_PROMPT_VERSION,
            instructions=GLOBAL_WRITER_INSTRUCTIONS,
            material=material,
            api_key=api_key,
            transport=transport,
            source_refs=sources,
        )
        proposal = _require_proposal(outcome, GLOBAL_WRITER_ROLE)
        return _writer_text(manuscript_text, findings, proposal)

    return reviewer, writer


# --------------------------------------------------------------------------- #
# Notes planner (proposal only; never applies)
# --------------------------------------------------------------------------- #
def plan_note_edits(
    store,
    *,
    registry,
    activation_token: str,
    delivery_id: str,
    hosted_ref,
    api_key: str,
    transport=None,
    source_refs=None,
):
    """Propose an author-note edit batch from the recorded notes and manuscript.

    Reads the recorded notes (``state["plan_notes"]["items"]``) and the current
    manuscript (``state["manuscript"]["text"]`` plus ``state["paragraphs"]``) from
    the delivery state, runs one bounded ``notes_planner`` generation request, and
    validates every proposed edit against the delivery schema. Each invalid edit
    is dropped with a static reason; only a valid edit is proposed. The result is
    a **PROPOSAL batch** — it never calls ``apply_notes``. The human approves the
    batch (an ``approval_id``), and only then is ``apply_notes`` called with it.

    Returns ``{"status": "proposed", "edits": [...], "dropped": [...],
    "generation": <outcome>}`` on success; ``{"status": "model_not_completed",
    "edits": [], "generation": <outcome>}`` when the model call did not complete;
    or ``{"status": "invalid_proposal", "edits": [], "reason": ...,
    "generation": <outcome>}`` when the output envelope is malformed.
    """
    delivery_id = validate_identifier(delivery_id, "delivery_id")
    state = DeliveryRepository(store).read(delivery_id)
    if not isinstance(state, dict):
        raise DeliveryRoleError(
            "notes_planner: the delivery state is unavailable; nothing is proposed"
        )
    notes = list((state.get("plan_notes") or {}).get("items") or [])
    manuscript = state.get("manuscript") or {}
    text = manuscript.get("text", "")
    if not isinstance(text, str):
        text = ""
    state_paragraphs = state.get("paragraphs") or {}
    paragraphs = [
        {"paragraph_id": paragraph_id, "text": (paragraph or {}).get("text", "")}
        for paragraph_id, paragraph in state_paragraphs.items()
        if isinstance(paragraph_id, str) and isinstance(paragraph, dict)
    ]

    material = {"manuscript": text, "paragraphs": paragraphs, "notes": notes}
    outcome = _dispatch(
        store,
        registry=registry,
        activation_token=activation_token,
        delivery_id=delivery_id,
        role=NOTES_PLANNER_ROLE,
        hosted_ref=hosted_ref,
        prompt_version=NOTES_PLANNER_PROMPT_VERSION,
        instructions=NOTES_PLANNER_INSTRUCTIONS,
        material=material,
        api_key=api_key,
        transport=transport,
        source_refs=list(source_refs or []),
    )
    if outcome.get("outcome") != "completed":
        return {
            "status": "model_not_completed",
            "edits": [],
            "generation": outcome,
        }
    proposal = outcome.get("proposal")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("edits"), list):
        return {
            "status": "invalid_proposal",
            "edits": [],
            "reason": "malformed_proposal",
            "generation": outcome,
        }

    known_notes = {
        note.get("note_id"): note
        for note in notes
        if isinstance(note, dict) and isinstance(note.get("note_id"), str)
    }
    known_paragraphs = {paragraph["paragraph_id"] for paragraph in paragraphs}
    allowed_keys = set(cited_keys(text))

    edits: list = []
    dropped: list = []
    for proposed in proposal["edits"]:
        clean, reason = _validate_edit(
            proposed,
            known_notes=known_notes,
            paragraphs=known_paragraphs,
            allowed_keys=allowed_keys,
        )
        if clean is None:
            dropped.append({"reason": reason})
        else:
            edits.append(clean)
    return {
        "status": "proposed",
        "edits": edits,
        "dropped": dropped,
        "generation": outcome,
    }
