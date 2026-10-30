"""Pure, deterministic Step 14 delivery rules (MVP).

Like :mod:`review_article_agent.review.schema` and
:mod:`review_article_agent.tracing.schema` this module is deliberately a small,
dependency-light MVP: plain JSON-portable ``dict``-in/``dict``-out with **no**
storage, network, or model calls.

Authority: ``docs/implementation/14-delivery.md`` (spec items 17-19);
``docs/contracts/03-policy-decisions.md`` POL-OPS-SECONDARY.

Obligations encoded:

* a global review cannot start while any required section review/origin check is
  incomplete; it inspects consequential omitted contrary/qualifying evidence, not
  just what is already cited;
* a global review gets at most ``GLOBAL_ROUND_LIMIT`` (2) repair rounds and never
  auto-passes an open/disputed major finding;
* freshness never silently adds papers and never loops: the user either accepts
  the existing cutoff or authorizes at most one top-up;
* scope/source-changing notes require impact approval (routed to an earlier step)
  rather than a forced local edit; ambiguous scientific notes require clarification;
* a notes batch is approved once and applied once; any factual/citation edit
  (including number/marker changes smuggled into a "wording" edit) marks affected
  checks stale for revalidation -- no final edit bypasses checks;
* warnings and unsupported major claims are explicit: unsupported major claims
  cannot be waived, every remaining warning must be accepted, the emitted status is
  ``secondary_accepted`` (never ``primary_verified``/``verified``);
* a subsequent artifact change invalidates a final approval.

Nothing here judges arbitrary prose semantics.
"""

from __future__ import annotations

import re

from ..domain.errors import ReviewAgentError

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
#: Maximum whole-manuscript repair rounds before the reviewer must stop (spec).
GLOBAL_ROUND_LIMIT = 2

#: Evidence directions that can be omitted (spec item 17).
DIRECTIONS = ("supporting", "contrary", "qualifying")

#: Finding severities that block global completion until resolved.
BLOCKING_SEVERITIES = ("blocking", "major", "critical")

#: Incremental revalidation triggered by a claim edit (support then origin).
CLAIM_REVALIDATE = ("support", "origin")

#: Routes for notes that cannot be applied as a local edit.
ROUTE_SCOPE = "step11_drafting"
ROUTE_SOURCE = "step05_discovery/step06_acquisition"

#: Recognised note categories.
NOTE_CATEGORIES = ("wording", "claim", "scope", "source")

#: Recognised edit kinds for an approved notes batch.
EDIT_KINDS = ("wording", "claim", "citation")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_CITATION_RE = re.compile(r"\[@[^\]]+\]")


class DeliveryError(ReviewAgentError):
    """A delivery payload/decision is malformed or refused.

    Messages name the structural defect only; they never embed manuscript text.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeliveryError(message)


def _numbers(text: str) -> list:
    return _NUMBER_RE.findall(text or "")


def _citations(text: str) -> list:
    return _CITATION_RE.findall(text or "")


# --------------------------------------------------------------------------- #
# 1. global_review_ready
# --------------------------------------------------------------------------- #
def global_review_ready(sections) -> dict:
    """Report whether the global review may start.

    ``sections`` is a list of ``{"section_id", "review_status",
    "origin_complete"}``.  Ready only when **every** section is complete
    (``review_status == "complete"``, Step 12) **and** its origin tracing is
    complete (``origin_complete is True``, Step 13).  An empty list is not ready.
    ``incomplete`` lists the section ids that still block the start.
    """
    _require(isinstance(sections, list), "sections must be a JSON array")
    incomplete = []
    for section in sections:
        _require(isinstance(section, dict), "sections entries must be JSON objects")
        section_id = section.get("section_id")
        _require(
            isinstance(section_id, str) and section_id != "",
            "section.section_id must be a non-empty string",
        )
        if not (section.get("review_status") == "complete" and section.get("origin_complete") is True):
            incomplete.append(section_id)
    return {"ready": bool(sections) and not incomplete, "incomplete": incomplete}


# --------------------------------------------------------------------------- #
# 2. inspect_omissions
# --------------------------------------------------------------------------- #
def inspect_omissions(cited_source_keys, evidence) -> list:
    """Find consequential contrary/qualifying evidence that is not cited.

    ``evidence`` entries are ``{"evidence_id", "source_key", "direction",
    "consequential"}`` with ``direction`` in ``DIRECTIONS``.  Every *consequential*
    contrary/qualifying item whose ``source_key`` is not in ``cited_source_keys``
    yields a ``major`` finding, inspecting evidence beyond what is already cited.
    """
    cited = set(cited_source_keys or [])
    _require(isinstance(evidence, list), "evidence must be a JSON array")
    findings = []
    for item in evidence:
        _require(isinstance(item, dict), "evidence entries must be JSON objects")
        direction = item.get("direction")
        _require(direction in DIRECTIONS, f"unknown evidence direction {direction!r}")
        if direction not in ("contrary", "qualifying"):
            continue
        if item.get("consequential") is not True:
            continue
        source_key = item.get("source_key")
        if source_key in cited:
            continue
        findings.append(
            {
                "evidence_id": item.get("evidence_id"),
                "source_key": source_key,
                "kind": "omitted_contrary" if direction == "contrary" else "omitted_qualifying",
                "severity": "major",
            }
        )
    return findings


# --------------------------------------------------------------------------- #
# 3. evaluate_global
# --------------------------------------------------------------------------- #
def evaluate_global(findings, rounds_used: int) -> dict:
    """Decide the whole-manuscript outcome; open/disputed majors never auto-pass.

    ``findings`` have ``severity`` and ``status`` in ``open``/``resolved``/
    ``disputed``.  With an open/disputed severe (blocking/major/critical) finding:
    below :data:`GLOBAL_ROUND_LIMIT` rounds -> ``needs_repair``; once the limit is
    reached -> ``blocked`` with an explicit ``required_human_action``.  No severe
    finding remaining -> ``complete``.  ``rounds_used`` above the limit is a
    programming error and raises.
    """
    _require(
        isinstance(rounds_used, int) and not isinstance(rounds_used, bool) and rounds_used >= 0,
        "rounds_used must be a non-negative integer",
    )
    _require(
        rounds_used <= GLOBAL_ROUND_LIMIT,
        f"rounds_used={rounds_used} exceeds the global limit of {GLOBAL_ROUND_LIMIT}",
    )
    _require(isinstance(findings, list), "findings must be a JSON array")

    unresolved = [
        finding
        for finding in findings
        if isinstance(finding, dict)
        and finding.get("status") in ("open", "disputed")
        and finding.get("severity") in BLOCKING_SEVERITIES
    ]
    if not unresolved:
        return {"status": "complete"}
    if rounds_used < GLOBAL_ROUND_LIMIT:
        return {"status": "needs_repair"}
    return {
        "status": "blocked",
        "required_human_action": (
            "The global repair budget is exhausted with unresolved (open or "
            "disputed) major findings; a human must resolve or re-authorize them."
        ),
    }


# --------------------------------------------------------------------------- #
# 4. freshness_checkpoint
# --------------------------------------------------------------------------- #
def freshness_checkpoint(cutoff_date, decision, prior=None) -> dict:
    """Adjudicate the explicit freshness decision; never adds papers.

    ``decision`` is ``{"action": "accept_cutoff"}`` or ``{"action":
    "authorize_topup", "approval_id", "query_window_end"}``.  A missing/unknown
    action yields ``decision_required`` (no silent proceed).  ``accept_cutoff`` is
    ``cutoff_accepted``.  ``authorize_topup`` requires a non-empty ``approval_id``
    and returns ``topup_authorized`` with ``papers_added`` 0 (routing only).  At
    most one top-up per delivery: if ``prior["topups_used"] >= 1`` a further
    top-up raises, while ``accept_cutoff`` remains available.  ``topups_used`` is
    always reported.
    """
    prior_topups = 0
    if prior is not None:
        _require(isinstance(prior, dict), "prior must be a JSON object or absent")
        prior_topups = prior.get("topups_used", 0)
        _require(
            isinstance(prior_topups, int) and not isinstance(prior_topups, bool) and prior_topups >= 0,
            "prior.topups_used must be a non-negative integer",
        )

    if not isinstance(decision, dict):
        return {"status": "decision_required", "topups_used": prior_topups}
    action = decision.get("action")

    if action == "accept_cutoff":
        return {"status": "cutoff_accepted", "cutoff": cutoff_date, "topups_used": prior_topups}

    if action == "authorize_topup":
        if prior_topups >= 1:
            raise DeliveryError(
                "a top-up has already been used for this delivery; at most one "
                "top-up is allowed (accept the cutoff instead)"
            )
        approval_id = decision.get("approval_id")
        _require(
            isinstance(approval_id, str) and approval_id.strip() != "",
            "authorize_topup requires a non-empty approval_id",
        )
        return {
            "status": "topup_authorized",
            "route_to": "step05_discovery/step06_acquisition",
            "papers_added": 0,
            "approval_id": approval_id,
            "query_window_end": decision.get("query_window_end"),
            "topups_used": prior_topups + 1,
        }

    return {"status": "decision_required", "topups_used": prior_topups}


# --------------------------------------------------------------------------- #
# 5. reopen_for_import
# --------------------------------------------------------------------------- #
def reopen_for_import(artifacts, imported_source_keys) -> list:
    """Return the artifact revisions reopened by a later source import.

    An artifact ``{"artifact_id", "revision_id", "depends_on_sources": [...]}`` is
    reopened when its dependencies intersect ``imported_source_keys``; artifacts
    that do not depend on the new sources are left untouched.
    """
    imported = set(imported_source_keys or [])
    _require(isinstance(artifacts, list), "artifacts must be a JSON array")
    reopened = []
    for artifact in artifacts:
        _require(isinstance(artifact, dict), "artifact entries must be JSON objects")
        depends = set(artifact.get("depends_on_sources") or [])
        if depends & imported:
            reopened.append(
                {
                    "artifact_id": artifact.get("artifact_id"),
                    "revision_id": artifact.get("revision_id"),
                    "reason": "new_import",
                }
            )
    return reopened


# --------------------------------------------------------------------------- #
# 6. plan_notes
# --------------------------------------------------------------------------- #
def plan_notes(notes) -> dict:
    """Turn human notes into an action plan.

    A note is ``{"note_id", "text", "category"?}`` with ``category`` in
    ``wording``/``claim``/``scope``/``source``.  A missing category, or a
    ``claim`` note without a concrete ``target``, requires clarification.  A
    ``wording`` note is a ``local_edit``.  A ``claim`` note (with target) is a
    ``claim_edit`` that revalidates support and origin.  ``scope``/``source``
    notes are ``impact_approval_required`` (no local edit) routed to an earlier
    step.
    """
    _require(isinstance(notes, list), "notes must be a JSON array")
    items = []
    for note in notes:
        _require(isinstance(note, dict), "notes entries must be JSON objects")
        note_id = note.get("note_id")
        _require(
            isinstance(note_id, str) and note_id != "",
            "note.note_id must be a non-empty string",
        )
        _require(isinstance(note.get("text"), str), "note.text must be a string")
        category = note.get("category")
        target = note.get("target")

        if category is None:
            items.append(
                {
                    "note_id": note_id,
                    "action": "clarification_required",
                    "reason": "note category is missing",
                }
            )
        elif category == "wording":
            items.append({"note_id": note_id, "action": "local_edit", "category": "wording"})
        elif category == "claim":
            if target in (None, "", [], {}):
                items.append(
                    {
                        "note_id": note_id,
                        "action": "clarification_required",
                        "reason": "claim note has no concrete target",
                    }
                )
            else:
                items.append(
                    {
                        "note_id": note_id,
                        "action": "claim_edit",
                        "category": "claim",
                        "target": target,
                        "revalidate": list(CLAIM_REVALIDATE),
                    }
                )
        elif category == "scope":
            items.append(
                {
                    "note_id": note_id,
                    "action": "impact_approval_required",
                    "category": "scope",
                    "route_to": ROUTE_SCOPE,
                }
            )
        elif category == "source":
            items.append(
                {
                    "note_id": note_id,
                    "action": "impact_approval_required",
                    "category": "source",
                    "route_to": ROUTE_SOURCE,
                }
            )
        else:
            raise DeliveryError(f"unknown note category {category!r}")

    return {"items": items, "requires_approval": True}


# --------------------------------------------------------------------------- #
# 7. apply_notes_batch
# --------------------------------------------------------------------------- #
def apply_notes_batch(state, batch_id, approval_id, edits) -> dict:
    """Apply one human-approved notes batch and return a NEW state.

    ``state`` holds ``applied_batches`` (list) and ``paragraphs``
    (``{paragraph_id: {"text", "checks": "valid"|"stale"}}``).  A non-empty
    ``approval_id`` is required; a ``batch_id`` already in ``applied_batches``
    raises (each batch is applied at most once).  Edits are ``{"paragraph_id",
    "new_text", "kind"}``.  A ``claim``/``citation`` edit -- and a ``wording``
    edit whose text changes numbers or ``[@key]`` citation markers -- marks the
    paragraph ``stale`` and records it in ``revalidation_required``; the input
    ``state`` is never mutated.
    """
    _require(isinstance(state, dict), "state must be a JSON object")
    _require(
        isinstance(batch_id, str) and batch_id != "",
        "batch_id must be a non-empty string",
    )
    _require(
        isinstance(approval_id, str) and approval_id.strip() != "",
        "applying a notes batch requires a non-empty approval_id",
    )

    applied = list(state.get("applied_batches") or [])
    if batch_id in applied:
        raise DeliveryError(f"notes batch {batch_id!r} has already been applied")

    paragraphs_in = state.get("paragraphs") or {}
    _require(isinstance(paragraphs_in, dict), "state.paragraphs must be a JSON object")
    paragraphs = {
        pid: (dict(paragraph) if isinstance(paragraph, dict) else paragraph)
        for pid, paragraph in paragraphs_in.items()
    }
    revalidation = list(state.get("revalidation_required") or [])

    _require(edits is None or isinstance(edits, list), "edits must be a JSON array")
    for edit in edits or []:
        _require(isinstance(edit, dict), "edit entries must be JSON objects")
        paragraph_id = edit.get("paragraph_id")
        kind = edit.get("kind")
        new_text = edit.get("new_text")
        _require(
            isinstance(paragraph_id, str) and paragraph_id != "",
            "edit.paragraph_id must be a non-empty string",
        )
        _require(kind in EDIT_KINDS, f"unknown edit kind {kind!r}")
        _require(isinstance(new_text, str), "edit.new_text must be a string")
        paragraph = paragraphs.get(paragraph_id)
        _require(isinstance(paragraph, dict), f"edit names unknown paragraph {paragraph_id!r}")

        old_text = paragraph.get("text", "")
        stale = kind in ("claim", "citation")
        if kind == "wording" and (
            _numbers(old_text) != _numbers(new_text) or _citations(old_text) != _citations(new_text)
        ):
            stale = True

        paragraph["text"] = new_text
        if stale:
            paragraph["checks"] = "stale"
            if paragraph_id not in revalidation:
                revalidation.append(paragraph_id)

    new_state = dict(state)
    new_state["paragraphs"] = paragraphs
    new_state["applied_batches"] = applied + [batch_id]
    new_state["revalidation_required"] = revalidation
    new_state["status"] = "returned_to_user"
    return new_state


# --------------------------------------------------------------------------- #
# 8. final_approval_manifest
# --------------------------------------------------------------------------- #
def final_approval_manifest(
    manuscript_revision,
    artifact_hashes,
    warnings,
    accepted_warning_ids,
    unsupported_major_claims,
    approver,
) -> dict:
    """Build the exact-revision final human approval manifest.

    Refuses to approve (raises :class:`DeliveryError`) if any
    ``unsupported_major_claims`` remain (they cannot be waived) or any
    ``warnings`` entry's ``warning_id`` is not in ``accepted_warning_ids``.  The
    released status of an accepted warning is ``secondary_accepted``;
    ``primary_verified``/``verified`` is never emitted.
    """
    _require(isinstance(artifact_hashes, dict), "artifact_hashes must be a JSON object")
    _require(isinstance(warnings, list), "warnings must be a JSON array")

    if unsupported_major_claims:
        raise DeliveryError("cannot approve: unsupported major claims cannot be waived")

    accepted = set(accepted_warning_ids or [])
    unaccepted = [
        warning
        for warning in warnings
        if isinstance(warning, dict) and warning.get("warning_id") not in accepted
    ]
    if unaccepted:
        raise DeliveryError("cannot approve: secondary-origin warnings remain unaccepted")

    accepted_warnings = [
        {
            "warning_id": warning.get("warning_id"),
            "claim_id": warning.get("claim_id"),
            "text": warning.get("text"),
            "status": "secondary_accepted",
        }
        for warning in warnings
    ]
    return {
        "manuscript_revision": manuscript_revision,
        "artifact_hashes": dict(artifact_hashes),
        "accepted_warnings": accepted_warnings,
        "approver": approver,
        "approved": True,
    }


# --------------------------------------------------------------------------- #
# 9. approval_current
# --------------------------------------------------------------------------- #
def approval_current(manifest, current_hashes) -> dict:
    """Report whether an approval still matches the current artifact hashes.

    Any differing or missing hash, or an extra artifact, means the manifest is no
    longer current; ``changed`` lists the artifact ids that differ.
    """
    _require(isinstance(manifest, dict), "manifest must be a JSON object")
    _require(isinstance(current_hashes, dict), "current_hashes must be a JSON object")
    recorded = manifest.get("artifact_hashes") or {}

    changed = []
    for artifact_id, digest in recorded.items():
        if current_hashes.get(artifact_id) != digest:
            changed.append(artifact_id)
    for artifact_id in current_hashes:
        if artifact_id not in recorded and artifact_id not in changed:
            changed.append(artifact_id)
    return {"current": not changed, "changed": changed}


__all__ = [
    "BLOCKING_SEVERITIES",
    "CLAIM_REVALIDATE",
    "DIRECTIONS",
    "DeliveryError",
    "EDIT_KINDS",
    "GLOBAL_ROUND_LIMIT",
    "NOTE_CATEGORIES",
    "ROUTE_SCOPE",
    "ROUTE_SOURCE",
    "apply_notes_batch",
    "approval_current",
    "evaluate_global",
    "final_approval_manifest",
    "freshness_checkpoint",
    "global_review_ready",
    "inspect_omissions",
    "plan_notes",
    "reopen_for_import",
]
