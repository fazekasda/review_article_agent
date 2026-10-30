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
from ..review.workflow import SectionReviewRepository
from ..storage.artifacts import ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository
from ..tracing.workflow import origin_coverage
from .export import build_disclosures, build_export_package, cited_keys
from .manuscript import MANUSCRIPT_DOCUMENT_TYPE, build_manuscript
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

#: The artifact-id suffix of the durable manuscript revision a delivery records.
#: The manuscript is a separate durable artifact (not the delivery state) so its
#: revision id is content-bound: a change of manuscript content produces a new
#: revision id, which makes the exact-revision final approval stale.
MANUSCRIPT_ARTIFACT_SUFFIX = ".manuscript"

#: The exact named owner gate (``gate_kind``, ``gate_id``) for the delivery final
#: approval.  The manifest is bound to the exact delivery-state revision that
#: carries it; the owner records this gate over that revision through the
#: owner-authenticated ``gate prepare``/``gate confirm`` flow (never a
#: caller-supplied approver string).
FINAL_APPROVAL_GATE_KIND = "artifact_revision"
FINAL_APPROVAL_GATE_ID = "delivery.final_approval"

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
        "prepared_approval": None,
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

    # -- manuscript ---------------------------------------------------------- #
    def _manuscript_artifact_id(self, delivery_id):
        artifact_id = validate_identifier(delivery_id, "delivery_id")
        return artifact_id + MANUSCRIPT_ARTIFACT_SUFFIX

    def _manuscript_head(self, delivery_id):
        return self.artifacts.current_revision(self._manuscript_artifact_id(delivery_id))

    def _manuscript_from_sections(self, sections):
        """Build the delivery manuscript from the named review states, or ``None``.

        Every section that names a ``review_id`` is read through the durable
        section-review repository and rendered in outline section order by
        :func:`~review_article_agent.delivery.manuscript.build_manuscript`.  If no
        section names a review id, ``None`` is returned (the caller has no
        reviewed sections to assemble); a named review that cannot be read is a
        fail-closed error.
        """
        entries: list = []
        for section in sections or []:
            review_id = (section or {}).get("review_id")
            if not review_id:
                return None
            review_state = SectionReviewRepository(self.store).read(review_id)
            if not isinstance(review_state, dict):
                raise DeliveryWorkflowError(
                    f"the section review {review_id!r} is unavailable; the delivery "
                    "manuscript cannot be assembled"
                )
            entries.append(
                {
                    "section_id": section.get("section_id"),
                    "title": section.get("title") or section.get("section_id"),
                    "review_state": review_state,
                }
            )
        if not entries:
            return None
        return build_manuscript(entries)

    def _record_manuscript(self, state, manuscript):
        """Persist (or reuse) the durable manuscript revision and bind it in state.

        The manuscript is a separate durable artifact; its revision id is bound to
        its exact content.  When the current head already carries the same payload
        the head revision id is reused (so an unchanged manuscript does not churn
        the id and does not spuriously stale an approval); otherwise a new immutable
        revision is appended.  ``state["manuscript"]`` records the exact revision id
        and text, and ``state["paragraphs"]`` the delivery paragraph model.
        """
        artifact_id = self._manuscript_artifact_id(state["delivery_id"])
        text = manuscript.get("text", "")
        if not isinstance(text, str):
            text = ""
        paragraphs = {
            str(paragraph_id): dict(paragraph)
            for paragraph_id, paragraph in (manuscript.get("paragraphs") or {}).items()
        }
        sections = list(manuscript.get("sections") or [])
        payload = {"text": text, "paragraphs": paragraphs, "sections": sections}

        head = self.artifacts.current_revision(artifact_id)
        if head is not None:
            try:
                current = json.loads(
                    self.artifacts.read_content(artifact_id, head.revision_id)
                )
            except (ValueError, UnicodeDecodeError):
                current = None
            if isinstance(current, dict) and current.get("payload") == payload:
                state["manuscript"] = {"revision_id": head.revision_id, "text": text}
                state["paragraphs"] = paragraphs
                return head.revision_id

        self.artifacts.create_artifact(artifact_id, kind=ARTIFACT_KIND, created_by="user")
        head = self.artifacts.current_revision(artifact_id)
        parent = None if head is None else head.revision_id
        revision_id = ArtifactRepository.new_revision_id()
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content={
                "schema_version": SCHEMA_VERSION,
                "document_type": MANUSCRIPT_DOCUMENT_TYPE,
                "payload": payload,
            },
            origin_locator="delivery:manuscript",
            acquired_by="user",
            rights=self._rights(artifact_id, revision_id),
            parent_revision_id=parent,
        )
        state["manuscript"] = {"revision_id": revision_id, "text": text}
        state["paragraphs"] = paragraphs
        return revision_id

    def set_manuscript(self, delivery_id, manuscript, rights=None):
        """Record a durable, content-bound manuscript revision on the delivery.

        The manuscript ``{"text", "paragraphs", "sections"}`` is persisted as a
        new immutable revision of the delivery's manuscript artifact (or the
        existing revision id is reused when the content is unchanged).  Any
        recorded approval is invalidated, because the approved manuscript revision
        no longer matches.
        """
        self._set_rights(rights)
        state = self._load(delivery_id)
        self._record_manuscript(state, manuscript)
        self._invalidate_approval(state)
        self._persist(state)
        return state

    # -- 1. global review gate ----------------------------------------------- #
    def start_global_review(
        self, delivery_id, sections, origin_state, rights=None, manuscript=None
    ):
        """Start the global review, persisting nothing if the gate is not met.

        The delivery manuscript is recorded from the reviewed section drafts: each
        section's durable ``review_id`` is read and rendered in outline section
        order (or an explicit ``manuscript`` may be supplied).  A section list
        without review ids records no manuscript (backward compatible with callers
        that supply the manuscript later to :meth:`run_global_review`).
        """
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
        if manuscript is None:
            manuscript = self._manuscript_from_sections(state["sections"])
        if manuscript is not None:
            self._record_manuscript(state, manuscript)
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
            if self._manuscript_head(state["delivery_id"]) is not None:
                # A durable manuscript revision exists: a writer edit changes the
                # content, so record a new content-bound revision id (an exact
                # approval of the old revision is thereby made stale).
                self._record_manuscript(
                    state,
                    {"text": text, "paragraphs": state.get("paragraphs") or {}},
                )
            else:
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
    @staticmethod
    def _require_final_approval_ready(state):
        """The preconditions a final approval manifest requires (spec item 18)."""
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
        """Record the exact-revision final human approval manifest.

        This is the **library** recording path used by the focused workflow
        tests; it records a caller-supplied ``approver`` label and therefore is
        **not** the CLI/export authority.  The CLI flow uses
        :meth:`prepare_final_approval` plus the owner-authenticated named gate
        (:meth:`final_approval_gate_satisfied`) so no agent/model path can record
        the approval by naming an approver.
        """
        self._set_rights(rights)
        state = self._load(delivery_id)
        self._require_final_approval_ready(state)

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

    def prepare_final_approval(
        self,
        delivery_id,
        artifact_hashes,
        warnings,
        accepted_warning_ids,
        unsupported_major_claims,
        rights=None,
    ):
        """Persist the exact-revision final-approval manifest as a new revision.

        The manifest is stored on a **new delivery-state revision** (the exact
        revision a human then approves), mirroring how every other stage binds a
        named gate to its exact proposal revision.  The caller supplies no
        approver: the approval is the owner-authenticated named gate
        (``artifact_revision`` / ``delivery.final_approval``) recorded over the
        returned ``revision_id`` through ``gate prepare``/``gate confirm``.

        Returns ``{"state", "manifest", "revision_id"}`` where ``revision_id`` is
        the persisted head revision the gate must target.
        """
        self._set_rights(rights)
        state = self._load(delivery_id)
        self._require_final_approval_ready(state)

        revision_id = (state.get("manuscript") or {}).get("revision_id")
        manifest = final_approval_manifest(
            revision_id,
            artifact_hashes,
            warnings,
            accepted_warning_ids,
            unsupported_major_claims,
            None,
        )
        # A re-prepare invalidates any earlier library approval; any earlier
        # *gate* approval becomes stale automatically because the head changes.
        self._invalidate_approval(state)
        state["prepared_approval"] = manifest
        state["status"] = "awaiting_final_approval"
        self._persist(state)
        head = self.artifacts.current_revision(
            validate_identifier(delivery_id, "delivery_id")
        )
        return {
            "state": state,
            "manifest": manifest,
            "revision_id": None if head is None else head.revision_id,
        }

    def final_approval_gate_satisfied(self, delivery_id):
        """``True`` iff the delivery's exact final-approval named gate is active.

        Satisfied only when the current approval for
        ``(delivery artifact, artifact_revision, delivery.final_approval)`` is
        ``active`` and ``approved`` for the **current** delivery revision; any
        later revision (a notes edit, a re-prepare, an export) makes it stale and
        returns ``False``.
        """
        artifact_id = validate_identifier(delivery_id, "delivery_id")
        return HumanApprovalRepository(self.store).named_gate_satisfied(
            artifact_id,
            gate_kind=FINAL_APPROVAL_GATE_KIND,
            gate_id=FINAL_APPROVAL_GATE_ID,
        )

    def export_approved(
        self,
        delivery_id,
        references,
        corpus,
        policy,
        ai_use,
        out_dir,
        venue=None,
        rights=None,
        current_hashes=None,
    ):
        """Write the export package after the exact-revision named final-approval gate.

        Unlike :meth:`export` (the library path used by focused workflow tests),
        this **requires the owner-authenticated named gate**
        (``artifact_revision`` / ``delivery.final_approval``) to be active and
        approved for the exact current manifest revision.  No caller-supplied
        approver or approval manifest is accepted.  If the manifest revision
        changed after approval (a stale gate) or no manifest was prepared, it
        refuses and writes nothing.
        """
        self._set_rights(rights)
        state = self._load(delivery_id)

        manifest = state.get("prepared_approval")
        _require(
            isinstance(manifest, dict),
            "export refused: no prepared final-approval manifest for the exact "
            "revision",
        )
        _require(
            self.final_approval_gate_satisfied(delivery_id),
            "export refused: the delivery final-approval named gate is not active "
            "and approved for the exact revision",
        )

        manuscript = state.get("manuscript")
        _require(
            isinstance(manuscript, dict) and manuscript.get("revision_id") is not None,
            "export refused: no current manuscript revision",
        )

        hashes = dict(manifest.get("artifact_hashes") or {})
        if current_hashes is not None:
            _require(
                current_hashes == hashes,
                "export refused: the supplied artifact hashes differ from the "
                "approved manifest",
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

        # Re-check the exact manifest and hashes IMMEDIATELY before writing.
        package = build_export_package(
            manuscript, references, corpus, manifest, hashes, disclosures
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
    "FINAL_APPROVAL_GATE_ID",
    "FINAL_APPROVAL_GATE_KIND",
    "MANUSCRIPT_ARTIFACT_SUFFIX",
    "MANUSCRIPT_DOCUMENT_TYPE",
    "SCHEMA_VERSION",
    "DeliveryWorkflowError",
    "DeliveryRepository",
]
