"""Durable, owner-gated section-blueprint workflow (Step 11, S11-BLUEPRINT).

One bounded outcome: bind the pure Step 11 section-blueprint contract
(:func:`review_article_agent.drafting.schema.validate_blueprint`) to immutable
storage, the integrated Step 11 per-section workspace identity
(:class:`review_article_agent.drafting.workspace.SectionWorkspaceRepository`) and
drafting package
(:class:`review_article_agent.drafting.package.DraftingPackageRepository`), and
the existing owner-authenticated named gates.  A blueprint revision may be
created **only** over the exact *current approved* global chain (accepted
synthesis -> framework -> outline), the exact current per-section workspace, and
the reconstructible approved drafting package for that section.  Authority to
**consume** a blueprint is only its own exact named owner gate.

Stored generic documents (``document_type = "drafting_blueprint"``, artifact
kind ``report``) carry the envelope::

    {"input": {workspace_ref, content},
     "workspace": <the normalized current workspace mapping>,
     "package": <the full approved drafting-package snapshot>,
     "result": <the validated blueprint>}

Authority is *only* the exact named owner gate ``artifact_revision`` /
``drafting.blueprint``.  In-band ``status``/``approved`` fields, an actor flag, a
raw imported JSON document, or a *generic* artifact approval confer **no**
authority.  A changed shared argument (a new framework/synthesis/outline/
workspace revision, or a changed source) conservatively stales this blueprint's
gate; **only this blueprint's own gate** is consulted, so an unapproved *other*
section never blocks an approved section.

Every live check and the write (or the gate consumption) run inside **one**
transaction, so no other writer can commit between the re-derivation and the
append/consume (no TOCTOU).  ``ProjectStore.transaction`` cannot nest, so the
repository uses the :class:`~review_article_agent.parsing.repository._NestedStore`
adapter (nested calls become SAVEPOINTs).

Historical reads (``current=False``) re-validate the stored bytes, the stored
schema against the **stored package snapshot**, and the exact stored workspace
revision (``workspace.read(current=False)``) **without** requiring live global
approvals, so every historical blueprint revision stays inspectable after an
edit.  A historical read never confers authority.

Not in scope (next bounded tasks): draft and manuscript assembly.  No model call,
no network, no session execution, no schema change, no new dependency, no
migration, no CLI.

Fixture disclosure: the tests exercise this module with synthetic runtime
mappings and the accepted synthetic Step 09 receipt seam inherited from
``test_synthesis_workflow``; no live OpenCode service/session is created and no
model/network call is made.
"""

from __future__ import annotations

import json

from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError, canonical_json, validate_ref
from ..parsing.repository import _NestedStore
from ..storage.artifacts import ArtifactRepository, DependencyRef, sha256_digest
from ..storage.human_approvals import HumanApprovalRepository
from .package import DraftingPackageError, DraftingPackageRepository
from .schema import DraftingError, validate_blueprint
from .workspace import SectionWorkspaceError, SectionWorkspaceRepository

#: The exact proposal kinds this repository accepts.  Only ``blueprint`` is
#: implemented now; ``draft`` and ``assembly`` are deliberately left to later
#: bounded tasks (the API is shaped to extend).
KINDS = ("blueprint",)

#: The generic artifact kind for a blueprint revision (C01 §5.1).
ARTIFACT_KIND = "report"

#: The generic document envelope type this repository writes and reads.
DOCUMENT_TYPE = "drafting_blueprint"

#: The exact, ordered payload keys this repository accepts.
PAYLOAD_KEYS = ("workspace_ref", "content")

#: The exact named owner gate (``gate_kind``, ``gate_id``) for a blueprint.
GATE_KIND = "artifact_revision"
GATE_ID = "drafting.blueprint"

#: The stored envelope's exact keys.
_ENVELOPE_KEYS = frozenset({"input", "workspace", "package", "result"})

#: The package ref fields that are pinned as blueprint dependencies.
_PACKAGE_REF_KEYS = (
    "outline_ref",
    "framework_ref",
    "synthesis_ref",
    "audit_ref",
    "acquisition_ref",
    "freeze_ref",
    "corpus_ref",
)

NOTICE = (
    "A section blueprint is provisional: it is grounded in the exact current "
    "approved synthesis/framework/outline chain, the per-section workspace, and the "
    "reconstructible approved drafting package (accepted evidence plus verified "
    "corpus citations). Authority to consume a blueprint is only its own named "
    "owner gate (artifact_revision / drafting.blueprint); an in-band status/actor "
    "flag or a generic artifact approval is not approval. Source checks and origin "
    "tracing are pending; this artifact is not exportable as final approved output."
)


class DraftingWorkflowError(ReviewAgentError):
    """A blueprint workflow input, dependency, or approval is invalid or stale."""


def require(condition, message=None):
    if not condition:
        raise DraftingWorkflowError(
            message
            or "drafting blueprint input, dependency, or approval is invalid or stale"
        )


class DraftingWorkflowRepository:
    """Write and read durable, owner-gated section-blueprint revisions.

    ``store`` is an already-verified :class:`ProjectStore`.  Typical use::

        repo = DraftingWorkflowRepository(store)
        ref = repo.propose(
            "blueprint",
            {"workspace_ref": workspace_ref, "content": blueprint_content},
            artifact_id="bp-sec1", revision_id="bp-sec1-r1",
            rights=<RightsRecord bound to this revision>,
        )
        envelope = repo.read(ref)                 # current, re-derived
        envelope = repo.read(ref, current=False)  # inspectable, non-authorizing
        approved = repo.require_blueprint(ref)    # only if this blueprint's gate is active
        report = repo.status(ref)
    """

    def __init__(self, store):
        self.store = store
        self._nested = _NestedStore(store)
        self.artifacts = ArtifactRepository(self._nested)

    # -- refs ---------------------------------------------------------------- #
    def _ref(self, ref):
        return validate_ref(ref, self.store.project_id)

    def _require_kind(self, kind):
        if kind != "blueprint":
            raise DraftingWorkflowError(
                "only the 'blueprint' proposal kind is implemented"
            )

    # -- dependency set ------------------------------------------------------ #
    def _dependencies(self, package, workspace_ref):
        """Return the exact, deduplicated dependency refs for one blueprint.

        Includes the exact workspace revision plus the package's global refs
        (outline/framework/synthesis/audit/acquisition/freeze/corpus) and each
        evidence entry's exact extraction and source refs.  Deterministic order
        (sorted by canonical form) and deduplicated, so the stored dependency set
        is byte-for-byte reproducible from the stored envelope.
        """
        refs = [workspace_ref]
        for key in _PACKAGE_REF_KEYS:
            ref = package.get(key)
            if isinstance(ref, dict):
                refs.append(ref)
        for item in package.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("extraction_ref"), dict):
                refs.append(item["extraction_ref"])
            for ref in item.get("source_refs") or []:
                if isinstance(ref, dict):
                    refs.append(ref)
        unique = {}
        for ref in refs:
            unique[canonical_json(self._ref(ref))] = self._ref(ref)
        return [unique[key] for key in sorted(unique)]

    # -- integrity-checked stored read --------------------------------------- #
    def _read(self, ref, *, current=True):
        """Read one stored document, verifying bytes, envelope, and dependency set."""
        ref = self._ref(ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        require(rev is not None, "the blueprint revision does not exist")
        if current:
            head = self.artifacts.current_revision(ref["artifact_id"])
            require(
                head is not None and head.revision_id == rev.revision_id,
                "the blueprint revision is not the current head",
            )
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(
            sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size,
            "the blueprint revision fails content integrity",
        )
        try:
            doc = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise DraftingWorkflowError("the stored blueprint is not decodable") from None
        require(
            isinstance(doc, dict)
            and doc.get("schema_version") == 1
            and doc.get("document_type") == DOCUMENT_TYPE,
            "the stored document is not a drafting blueprint",
        )
        payload = doc.get("payload")
        require(
            isinstance(payload, dict) and set(payload) == _ENVELOPE_KEYS,
            "the stored blueprint payload is malformed",
        )
        input_payload = payload.get("input")
        require(
            isinstance(input_payload, dict)
            and set(input_payload) == set(PAYLOAD_KEYS),
            "the stored blueprint input is malformed",
        )
        try:
            expected = {
                canonical_json(r)
                for r in self._dependencies(
                    payload["package"], self._ref(input_payload["workspace_ref"])
                )
            }
        except (RecordValidationError, TypeError, KeyError):
            raise DraftingWorkflowError(
                "the stored blueprint carries a foreign or invalid dependency"
            ) from None
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(
            all(d.role == "derived_from" for d in live),
            "the stored blueprint has a dependency role outside the approved vocabulary",
        )
        require(
            {canonical_json(d.as_ref()) for d in live} == expected,
            "the stored blueprint dependency set does not match its envelope",
        )
        return payload

    # -- live re-derivation -------------------------------------------------- #
    def _rederive_current(self, payload):
        """Re-derive the current workspace/package/result and compare to the stored.

        Fails closed when the workspace is no longer the current approved
        identity, the current approved chain no longer produces the stored
        package snapshot, or the blueprint no longer re-validates.
        """
        workspace_ref = self._ref(payload["input"]["workspace_ref"])
        try:
            workspace = SectionWorkspaceRepository(self._nested).read(
                workspace_ref, current=True
            )["result"]
        except SectionWorkspaceError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            workspace == payload["workspace"],
            "the stored blueprint workspace pins do not re-derive from durable rows",
        )
        section_id = workspace["section_id"]
        outline_ref = workspace["outline_ref"]
        try:
            package = DraftingPackageRepository(self._nested).build(
                outline_ref, section_id
            )
        except DraftingPackageError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            package == payload["package"],
            "the current approved drafting package does not match the stored snapshot",
        )
        try:
            result = validate_blueprint(payload["input"]["content"], package=package)
        except DraftingError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            result == payload["result"],
            "the stored blueprint result does not re-validate from durable inputs",
        )

    def _rederive_historical(self, payload):
        """Re-validate a historical blueprint against its stored snapshot.

        The stored schema is re-checked against the **stored package snapshot**,
        and the stored workspace is re-derived via ``workspace.read(current=False)``
        (which requires no live global approval).  No live gate is required.
        """
        try:
            result = validate_blueprint(
                payload["input"]["content"], package=payload["package"]
            )
        except DraftingError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            result == payload["result"],
            "the stored blueprint result does not re-validate against its snapshot",
        )
        try:
            workspace = SectionWorkspaceRepository(self._nested).read(
                payload["input"]["workspace_ref"], current=False
            )["result"]
        except SectionWorkspaceError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            workspace == payload["workspace"],
            "the stored blueprint workspace pins do not re-derive historically",
        )

    # -- authority reads ----------------------------------------------------- #
    def read(self, ref, kind="blueprint", *, current=True):
        """Read one blueprint revision, re-deriving it from durable rows.

        ``current`` defaults to ``True`` (the exact current head, re-derived over
        the current approved workspace/package/global chain).  ``current=False``
        inspects a historical revision against its stored snapshot and the exact
        stored workspace revision, requiring no live global approval -- and never
        conferring authority.
        """
        self._require_kind(kind)
        payload = self._read(ref, current=current)
        if current:
            self._rederive_current(payload)
        else:
            self._rederive_historical(payload)
        return payload

    def _gate_satisfied(self, ref):
        """``True`` iff **this** blueprint's own exact named gate is satisfied."""
        return HumanApprovalRepository(self.store).named_gate_satisfied(
            ref["artifact_id"], gate_kind=GATE_KIND, gate_id=GATE_ID
        )

    def require_blueprint(self, ref):
        """Require the current blueprint and its own approved named gate.

        Re-derives the current workspace/package/result (comparing the stored
        snapshot), then checks **only** this blueprint's own
        ``artifact_revision`` / ``drafting.blueprint`` gate.  An unapproved other
        section never blocks; a generic artifact approval or an in-band status
        never satisfies the gate.  Everything runs in one transaction.
        """
        with self._nested.transaction():
            ref = self._ref(ref)
            payload = self.read(ref, "blueprint", current=True)
            require(
                self._gate_satisfied(ref),
                "the blueprint's own named owner gate is not active and approved",
            )
            return {
                **payload,
                "blueprint_ref": ref,
                "gate_kind": GATE_KIND,
                "gate_id": GATE_ID,
                "approved": True,
                "authorizes_execution": False,
                "authorizes_export": False,
                "notice": NOTICE,
            }

    def status(self, ref, kind="blueprint"):
        """Return a safe, structured status for one current blueprint revision.

        The stored revision's bytes/envelope/dependency set are verified first
        (a corrupt row fails closed with a static error rather than reporting a
        benign status).  Then the current workspace/package/result is re-derived
        and this blueprint's own gate is consulted; any staleness is reported as
        structured ``reasons``.  A drafting artifact is always ``provisional`` and
        never authorizes export.
        """
        self._require_kind(kind)
        ref = self._ref(ref)
        payload = self._read(ref, current=True)
        reasons = []
        stale = False
        try:
            self._rederive_current(payload)
        except (DraftingWorkflowError, SectionWorkspaceError, DraftingPackageError):
            stale = True
            reasons.append(
                "the current approved workspace/package chain no longer "
                "re-derives the stored snapshot"
            )
        if not self._gate_satisfied(ref):
            stale = True
            reasons.append(
                "the blueprint's own named owner gate is not active and approved"
            )
        result = payload["result"]
        return {
            "blueprint_ref": ref,
            "gate_kind": GATE_KIND,
            "gate_id": GATE_ID,
            "stale": stale,
            "reasons": reasons,
            "provisional": True,
            "pending": list(result.get("pending") or []),
            "authorizes_export": False,
        }

    # -- writes -------------------------------------------------------------- #
    def propose(self, kind, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert blueprint revision, atomically validating its inputs.

        The current approved global chain, the exact current per-section
        workspace, and the reconstructible approved drafting package are all
        re-derived and validated before anything is written; the blueprint content
        is validated against that package.  On an edit (``parent_revision_id`` is
        given) the section and the workspace/outline identities and mapping pins
        are retained -- a blueprint may rebind to a **new current revision of the
        same workspace artifact**, but never silently switch section or workspace
        identity.  No blueprint is auto-approved.
        """
        self._require_kind(kind)
        with self._nested.transaction():
            require(
                isinstance(payload, dict) and set(payload) == set(PAYLOAD_KEYS),
                "a blueprint payload must be exactly workspace_ref, content",
            )
            workspace_ref = self._ref(payload["workspace_ref"])
            try:
                workspace = SectionWorkspaceRepository(self._nested).read(
                    workspace_ref, current=True
                )["result"]
            except SectionWorkspaceError as exc:
                raise DraftingWorkflowError(str(exc)) from exc
            section_id = workspace["section_id"]
            outline_ref = workspace["outline_ref"]
            try:
                package = DraftingPackageRepository(self._nested).build(
                    outline_ref, section_id
                )
            except DraftingPackageError as exc:
                raise DraftingWorkflowError(str(exc)) from exc
            try:
                result = validate_blueprint(payload["content"], package=package)
            except DraftingError as exc:
                raise DraftingWorkflowError(str(exc)) from exc
            require(
                result["section_id"] == section_id,
                "the blueprint section does not match the workspace section",
            )
            if parent_revision_id is not None:
                parent_ref = {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id,
                    "revision_id": parent_revision_id,
                }
                parent = self._read(parent_ref, current=False)
                parent_ws = parent["workspace"]
                require(
                    parent_ws["section_id"] == section_id,
                    "an edit may not diverge the blueprint section",
                )
                require(
                    parent_ws["outline_ref"]["artifact_id"]
                    == outline_ref["artifact_id"],
                    "an edit may not switch the logical outline identity",
                )
                require(
                    parent["input"]["workspace_ref"]["artifact_id"]
                    == workspace_ref["artifact_id"],
                    "an edit may not switch the logical workspace identity",
                )
                for slot in ("planner", "writer"):
                    require(
                        parent_ws[slot]["mapping_id"] == workspace[slot]["mapping_id"]
                        and parent_ws[slot]["session_id"]
                        == workspace[slot]["session_id"],
                        "an edit may not diverge the workspace runtime mapping pins",
                    )
            deps = [
                DependencyRef(**ref, role="derived_from")
                for ref in self._dependencies(package, workspace_ref)
            ]
            self.artifacts.create_artifact(
                artifact_id, kind=ARTIFACT_KIND, created_by="user"
            )
            revision = self.artifacts.import_revision(
                artifact_id=artifact_id,
                revision_id=revision_id,
                rights=rights,
                content={
                    "schema_version": 1,
                    "document_type": DOCUMENT_TYPE,
                    "payload": {
                        "input": payload,
                        "workspace": workspace,
                        "package": package,
                        "result": result,
                    },
                },
                dependencies=deps,
                origin_locator="drafting:blueprint",
                acquired_by="user",
                parent_revision_id=parent_revision_id,
            )
        return {
            "project_id": self.store.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }


#: Convenience alias used by callers that prefer the shorter name.
DraftingWorkflow = DraftingWorkflowRepository

__all__ = [
    "ARTIFACT_KIND",
    "DOCUMENT_TYPE",
    "DraftingWorkflow",
    "DraftingWorkflowError",
    "DraftingWorkflowRepository",
    "GATE_ID",
    "GATE_KIND",
    "KINDS",
    "NOTICE",
    "PAYLOAD_KEYS",
]
