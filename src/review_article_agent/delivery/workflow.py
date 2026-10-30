"""Durable, resumable Step 14 delivery workflow (MVP).

This module binds the pure Step 14 rules in
:mod:`review_article_agent.delivery.schema` and the pure render/check rules in
:mod:`review_article_agent.delivery.export` to durable storage, and gates a
global review on Step 13's read-only ``origin_coverage``.  It is a small,
deliberately minimal MVP: no CLI, no storage schema change, no new dependency,
and no real model/network call.  The reviewer and writer are **injected ports**,
so a test supplies fakes that count calls.

What one workflow does (per ``docs/implementation/14-delivery.md``)
-------------------------------------------------------------------

* **Global review gate.** :meth:`DeliveryRepository.start_global_review` refuses
  (persisting nothing) unless every section is review-complete *and* origin
  complete (``global_review_ready``) and the persisted origin trace is
  ``complete`` (``origin_coverage``).
* **Bounded global review.** :meth:`DeliveryRepository.run_global_review` calls
  the injected ``reviewer`` port, appends
  :func:`~review_article_agent.delivery.schema.inspect_omissions` findings, and
  loops with :func:`~review_article_agent.delivery.schema.evaluate_global` for at
  most ``GLOBAL_ROUND_LIMIT`` writer repair rounds.  It records ``reviewer_calls``
  and ``repair_rounds`` and **persists the full state as a new immutable artifact
  revision after every call**; a re-run resumes from the persisted counters
  (rounds never reset) and a completed/blocked review is returned unchanged
  (no extra reviewer call).
* **Freshness, notes, revalidation, approval, export** are recorded as new
  revisions too.  Export refuses (writing nothing) when the approval is no longer
  current, and re-checks hashes with
  :func:`~review_article_agent.delivery.export.build_export_package`
  **immediately before** writing the files.

Non-claims: this proves the mechanical obligations only.  It cannot establish
scientific truth, and the injected ports are deterministic test fixtures, not
live model validation.
"""

from __future__ import annotations

import hashlib
import json
import os

from ..domain.records import validate_identifier
from ..review.schema import fact_check
from ..storage.artifacts import ArtifactRepository
from ..tracing.workflow import origin_coverage
from .export import build_disclosures, build_export_package, cited_keys
from .schema import (
    DeliveryError,
    apply_notes_batch,
    approval_current,
    evaluate_global,
    final_approval_manifest,
    freshness_checkpoint,
    global_review_ready,
    inspect_omissions,
    plan_notes,
)

#: The generic artifact kind for a delivery revision (C01 §5.1).
ARTIFACT_KIND = "report"

#: The generic document envelope type for a delivery revision.
DOCUMENT_TYPE = "delivery"

#: The supported generic JSON document-envelope schema version.
SCHEMA_VERSION = 1

#: Plan actions whose items may be applied as an edit.
_EDITABLE_ACTIONS = ("local_edit", "claim_edit")


class DeliveryWorkflowError(DeliveryError):
    """A delivery workflow input or resume state is invalid."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeliveryWorkflowError(message)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_state(delivery_id) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "document_type": DOCUMENT_TYPE,
        "status": "in_progress",
        "sections": [],
        "origin_state": {},
        "manuscript": None,
        "global_review": {
            "status": "in_progress",
            "rounds_used": 0,
            "reviewer_calls": 0,
            "findings": [],
            "required_human_action": None,
        },
        "freshness": None,
        "plan_notes": None,
        "paragraphs": {},
        "applied_batches": [],
        "revalidation_required": [],
        "revalidation_records": {},
        "approval": None,
        "invalidated_approvals": [],
        "export": None,
    }


class DeliveryRepository:
    """Durable, resumable Step 14 delivery over a :class:`ProjectStore`.

    Typical use::

        repo = DeliveryRepository(store)
        state = repo.start_global_review("delivery-1", sections, origin_state, rights)
        state = repo.run_global_review(
            "delivery-1", manuscript, evidence, reviewer, writer, rights
        )
        ...

    ``rights`` is a callable ``(artifact_id, revision_id) -> RightsRecord`` used to
    authorise each persisted revision.  ``reviewer(manuscript_text) -> findings``
    and ``writer(manuscript_text, findings) -> new_text`` are injected ports; no
    real model call is made here.
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(store)
        self._rights = None

    # -- rights -------------------------------------------------------------- #
    def _set_rights(self, rights):
        if rights is not None:
            self._rights = rights
        _require(self._rights is not None, "rights factory is not configured")

    # -- read / persist ------------------------------------------------------ #
    def read(self, delivery_id):
        """Return the latest persisted delivery state (or ``None``)."""
        artifact_id = validate_identifier(delivery_id, "delivery_id")
        head = self.artifacts.current_revision(artifact_id)
        if head is None:
            return None
        data = self.artifacts.read_content(artifact_id, head.revision_id)
        try:
            document = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise DeliveryWorkflowError(
                "the stored delivery document is not decodable"
            ) from None
        _require(
            isinstance(document, dict)
            and document.get("schema_version") == SCHEMA_VERSION
            and document.get("document_type") == DOCUMENT_TYPE,
            "the stored document is not a delivery document",
        )
        payload = document.get("payload")
        _require(isinstance(payload, dict), "the stored delivery payload is malformed")
        return payload

    def _persist(self, state):
        """Append the full delivery state as a new immutable revision."""
        _require(self._rights is not None, "rights factory is not configured")
        artifact_id = validate_identifier(state["delivery_id"], "delivery_id")
        self.artifacts.create_artifact(
            artifact_id, kind=ARTIFACT_KIND, created_by="user"
        )
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
            origin_locator="delivery:workflow",
            acquired_by="user",
            rights=self._rights(artifact_id, revision_id),
            parent_revision_id=parent,
        )
        return revision_id

    def _load(self, delivery_id):
        artifact_id = validate_identifier(delivery_id, "delivery_id")
        state = self.read(artifact_id)
        _require(state is not None, f"delivery {delivery_id!r} has not been started")
        return state

    def _invalidate_approval(self, state):
        """A later change invalidates a recorded approval (AC7)."""
        if state.get("approval") is not None:
            state.setdefault("invalidated_approvals", []).append(state["approval"])
            state["approval"] = None
            state["status"] = "in_progress"
        return state

    # -- 1. global review gate ----------------------------------------------- #
    def start_global_review(self, delivery_id, sections, origin_state, rights=None):
        """Start the global review, persisting nothing if the gate is not met."""
        self._set_rights(rights)
        artifact_id = validate_identifier(delivery_id, "delivery_id")

        ready = global_review_ready(sections)
        _require(
            ready["ready"],
            "global review cannot start: incomplete sections "
            + repr(sorted(ready["incomplete"])),
        )
        coverage = origin_coverage(origin_state)
        _require(
            coverage["complete"],
            "global review cannot start: origin tracing is incomplete "
            + repr([blocker.get("reason") for blocker in coverage["blockers"]]),
        )

        state = self.read(artifact_id) or _new_state(artifact_id)
        state["sections"] = list(sections or [])
        state["origin_state"] = origin_state
        state["status"] = "in_progress"
        self._persist(state)
        return state

    # -- 2. bounded global review -------------------------------------------- #
    def run_global_review(
        self, delivery_id, manuscript, evidence, reviewer, writer, rights=None
    ):
        """Run (or resume) the bounded global review; return the final state."""
        self._set_rights(rights)
        state = self._load(delivery_id)
        gr = state["global_review"]
        if gr["status"] in ("complete", "blocked"):
            return state

        if "paragraphs" in manuscript:
            state["paragraphs"] = dict(manuscript.get("paragraphs") or {})
        text = (state.get("manuscript") or {}).get("text")
        if text is None:
            text = manuscript.get("text", "")
        revision_id = manuscript.get("revision_id")

        while True:
            raw = list(reviewer(text) or [])
            gr["reviewer_calls"] += 1
            # Consequential omitted contrary/qualifying evidence is a real,
            # blocking open major finding (inspected beyond what is already cited).
            omitted = [
                dict(finding, status="open")
                for finding in inspect_omissions(cited_keys(text), evidence)
            ]
            findings = raw + omitted
            gr["findings"] = findings
            state["manuscript"] = {"revision_id": revision_id, "text": text}
            result = evaluate_global(findings, gr["rounds_used"])

            if result["status"] == "complete":
                gr["status"] = "complete"
                gr["required_human_action"] = None
                self._persist(state)
                return state
            if result["status"] == "blocked":
                gr["status"] = "blocked"
                gr["required_human_action"] = result.get("required_human_action")
                self._persist(state)
                return state

            self._persist(state)
            text = writer(text, findings)
            gr["rounds_used"] += 1
            state["manuscript"] = {"revision_id": revision_id, "text": text}
            self._persist(state)

    # -- 3. freshness -------------------------------------------------------- #
    def freshness(self, delivery_id, cutoff_date, decision, rights=None):
        """Record the explicit freshness decision; never adds papers."""
        self._set_rights(rights)
        state = self._load(delivery_id)
        prior = {"topups_used": (state.get("freshness") or {}).get("topups_used", 0)}
        result = freshness_checkpoint(cutoff_date, decision, prior)
        state["freshness"] = result
        self._invalidate_approval(state)
        self._persist(state)
        return state

    # -- 4. notes ------------------------------------------------------------ #
    def notes(self, delivery_id, notes, rights=None):
        """Store the human-notes action plan."""
        self._set_rights(rights)
        state = self._load(delivery_id)
        state["plan_notes"] = plan_notes(notes)
        self._persist(state)
        return state

    def apply_notes(self, delivery_id, batch_id, approval_id, edits, rights=None):
        """Apply one approved notes batch (only local_edit/claim_edit items)."""
        self._set_rights(rights)
        state = self._load(delivery_id)
        allowed = {
            item.get("note_id")
            for item in (state.get("plan_notes") or {}).get("items", [])
            if item.get("action") in _EDITABLE_ACTIONS
        }
        for edit in edits or []:
            note_id = edit.get("note_id") if isinstance(edit, dict) else None
            if note_id not in allowed:
                raise DeliveryError(
                    "edit refused: only notes whose plan action is local_edit/"
                    "claim_edit may be applied as an edit"
                )
        state = apply_notes_batch(state, batch_id, approval_id, edits)
        state["status"] = "in_progress"
        self._invalidate_approval(state)
        self._persist(state)
        return state

    def revalidate(
        self,
        delivery_id,
        paragraph_ids,
        *,
        package,
        origin_state=None,
        rights=None,
    ):
        """Re-run the Step 12/13 checks for the named paragraphs; never trust the caller.

        For every named paragraph this recomputes, from the persisted paragraph
        text and the supplied approved drafting *package*:

        * **(a) citations** -- every key cited in the paragraph text must exist in
          the package's approved ``citations`` (no invented citation);
        * **(b) support** -- the deterministic Step 12 fact check
          (:func:`review_article_agent.review.schema.fact_check`) must return a
          ``supported`` verdict for the paragraph; a paragraph that cites nothing
          cannot be mapped to package evidence and stays required;
        * **(c) origin** -- when *origin_state* is supplied, Step 13
          ``origin_coverage`` must be complete.

        Only a paragraph that passes every check becomes ``checks="valid"`` and
        leaves ``revalidation_required``; any other paragraph stays required with
        the recorded reasons.  The recomputed evidence (per-paragraph checks and
        digests) is persisted, and any recorded approval is invalidated.
        """
        self._set_rights(rights)
        state = self._load(delivery_id)
        ids = list(paragraph_ids or [])
        _require(ids, "revalidate requires at least one paragraph_id")

        citations = (package or {}).get("citations")
        _require(
            isinstance(citations, dict),
            "package.citations must be a JSON object",
        )
        approved_keys = set(citations)
        coverage = origin_coverage(origin_state) if origin_state is not None else None

        still_required = list(state.get("revalidation_required") or [])
        records = dict(state.get("revalidation_records") or {})

        for paragraph_id in ids:
            paragraph = (state.get("paragraphs") or {}).get(paragraph_id)
            _require(
                isinstance(paragraph, dict),
                f"revalidate names unknown paragraph {paragraph_id!r}",
            )
            text = paragraph.get("text", "")
            keys = cited_keys(text)
            reasons: list = []
            checks: list = []

            # (a) no invented citation: every cited key is in the approved package.
            unknown = [key for key in keys if key not in approved_keys]
            if unknown:
                reasons.append("unknown_citation:" + ",".join(unknown))

            # (b) re-run the deterministic Step 12 fact check from the paragraph text.
            if not keys:
                reasons.append("no_citable_evidence")
            else:
                draft = {
                    "paragraphs": [
                        {
                            "paragraph_id": paragraph_id,
                            "sentences": [
                                {
                                    "intent_id": paragraph.get("intent_id")
                                    or f"revalidate:{paragraph_id}",
                                    "text": text,
                                    "citation_keys": keys,
                                }
                            ],
                        }
                    ]
                }
                checks = fact_check(draft, package)
                for check in checks:
                    if check.get("verdict") not in (None, "not_applicable", "supported"):
                        reasons.append("fact_check:" + str(check.get("verdict")))

            # (c) Step 13 origin coverage, when an origin state is supplied.
            if coverage is not None and not coverage.get("complete"):
                reasons.append("origin_incomplete")

            records[paragraph_id] = {
                "paragraph_id": paragraph_id,
                "text_digest": _sha256(text),
                "citation_keys": keys,
                "fact_checks": checks,
                "checks_digest": _sha256(json.dumps(checks, sort_keys=True)),
                "coverage_complete": (
                    None if coverage is None else coverage.get("complete")
                ),
                "reasons": reasons,
                "valid": not reasons,
            }

            if reasons:
                paragraph["checks"] = "stale"
                if paragraph_id not in still_required:
                    still_required.append(paragraph_id)
            else:
                paragraph["checks"] = "valid"
                still_required = [
                    pid for pid in still_required if pid != paragraph_id
                ]

        state["revalidation_records"] = records
        state["revalidation_required"] = still_required
        self._invalidate_approval(state)
        self._persist(state)
        return state

    # -- 5. final approval --------------------------------------------------- #
    def final_approval(
        self,
        delivery_id,
        artifact_hashes,
        warnings,
        accepted_warning_ids,
        unsupported_major_claims,
        approver,
        rights=None,
    ):
        """Record the exact-revision final human approval manifest."""
        self._set_rights(rights)
        state = self._load(delivery_id)

        _require(
            state["global_review"]["status"] == "complete",
            "final approval requires a complete global review",
        )
        _require(
            (state.get("freshness") or {}).get("status") == "cutoff_accepted",
            "final approval requires an accepted freshness cutoff",
        )
        _require(
            not state.get("revalidation_required"),
            "final approval blocked: stale paragraphs require revalidation "
            + repr(list(state.get("revalidation_required"))),
        )

        revision_id = (state.get("manuscript") or {}).get("revision_id")
        manifest = final_approval_manifest(
            revision_id,
            artifact_hashes,
            warnings,
            accepted_warning_ids,
            unsupported_major_claims,
            approver,
        )
        state["approval"] = manifest
        state["status"] = "approved"
        self._persist(state)
        return state

    # -- 6. export ----------------------------------------------------------- #
    def export(
        self,
        delivery_id,
        manuscript,
        references,
        corpus,
        current_hashes,
        policy,
        ai_use,
        out_dir,
        venue=None,
        rights=None,
    ):
        """Validate the exact revision/hashes, then write the export package."""
        self._set_rights(rights)
        state = self._load(delivery_id)

        approval = state.get("approval")
        _require(approval is not None, "export refused: the delivery is not approved")
        current = approval_current(approval, current_hashes)
        _require(
            current["current"],
            "export refused: the approval is stale (changed artifacts "
            + repr(sorted(current["changed"]))
            + ")",
        )

        origin_warnings = origin_coverage(state.get("origin_state") or {}).get(
            "requires_human_approval"
        ) or []
        warnings = [
            {
                "claim_id": warning.get("claim_id"),
                "cite_source_key": warning.get("cite_source_key"),
                "text": warning.get("warning"),
            }
            for warning in origin_warnings
        ]
        disclosures = build_disclosures(policy, ai_use, venue=venue, warnings=warnings)

        # Re-check the approval and hashes IMMEDIATELY before writing.
        package = build_export_package(
            manuscript, references, corpus, approval, current_hashes, disclosures
        )

        os.makedirs(str(out_dir), exist_ok=True)
        file_hashes: dict = {}
        for name, content in package["files"].items():
            with open(os.path.join(str(out_dir), name), "w", encoding="utf-8") as handle:
                handle.write(content)
            file_hashes[name] = _sha256(content)

        state["export"] = {
            "manuscript_revision": manuscript.get("revision_id"),
            "file_hashes": file_hashes,
            "checks": package["checks"],
        }
        state["status"] = "exported"
        self._persist(state)
        return state["export"]


__all__ = [
    "ARTIFACT_KIND",
    "DOCUMENT_TYPE",
    "SCHEMA_VERSION",
    "DeliveryWorkflowError",
    "DeliveryRepository",
]
