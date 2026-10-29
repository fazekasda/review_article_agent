"""Durable per-section planner/writer workspace identity (Step 11, S11-WORKSPACE).

One bounded outcome: bind the pure Step 11 workspace identity contract to
immutable storage, an **exact current approved outline revision**, and the
existing app-owned runtime session mappings.

Source authorities (read-only):

* ``docs/specification.md`` line 36 ("Each section has a persistent planner/writer
  workspace; approve sources and paragraph blueprints before drafting. Sections
  may progress independently after global outline approval; all must finish
  before global review. Approved artifacts, not chat history, are
  authoritative.").
* ``docs/implementation/11-drafting.md`` deliverable 1 ("Persistent per-section
  planner/writer workspaces mapped to project/role/session IDs") and acceptance
  criterion 2 ("Reconstruct context after worker restart from approved artifacts;
  no sole dependence on raw session history.").

What this module is
-------------------

A workspace revision is a generic ``report`` document
(``document_type = "drafting_section_workspace"``) whose stored envelope is::

    {"input": {outline_ref, section_id, planner_mapping_id, writer_mapping_id},
     "result": <normalized mapping>}

``result`` records immutable, normalized pins derived **anew** on every read from
the store's own rows:

* ``project_id`` — always ``store.project_id`` (never a caller value);
* ``section_id`` — the section this workspace is for, required to exist in the
  exact approved outline revision;
* ``outline_ref`` — the exact same-project outline revision (also the single
  ``derived_from`` dependency edge);
* ``planner`` / ``writer`` — the pinned app-owned runtime session mapping for
  each slot (``mapping_id``, ``session_id``, service/opencode ids, concrete
  ``provider_id``/``model_id``/``variant``, ``config_digest``,
  ``config_snapshot_id``).

Role/session pins
-----------------

The **planner** uses the existing ``synthesizer`` scientific role for a *planning*
purpose that is separate from any execution role; the **writer** uses the existing
``writer`` role. Both must be pre-existing :class:`RuntimeSessionMapping` rows in
the **same** project (the app owns their creation; this module never creates,
dispatches, or executes a session). The two sessions must be distinct.

Per-section exclusivity
-----------------------

A workspace is genuinely **per-section**: the same planner/writer runtime mappings
may not be reused by a *different* ``section_id``. ``propose`` scans the bounded,
integrity-checked workspace history and refuses a proposal whose mappings are
already bound to another section; reusing the same section's mappings across an
accepted outline revision (an update) stays allowed. The scan ignores other
generic ``report`` documents, but a *matching* workspace document is
integrity-checked and its same-project refs validated; a corrupt or foreign
matching document fails closed (never silently erases a recorded binding).

Advancing the outline
---------------------

An edit (``parent_revision_id`` given) may advance to a **new revision of the same
logical outline artifact** while retaining the exact section and planner/writer
mapping pins (the new outline must be current and approved, and the section must
still exist in it). Swapping the section or the mapping pins on an edit is
refused, so a persistent workspace identity is never silently retargeted. Old
revisions stay immutable and inspectable (``current=False``); the module never
auto-approves blueprints.

Not in scope / non-claims
-------------------------

No new scientific role, no default model, no network or model call, no session
execution, no migration, no new dependency, and **no workflow authority**. A
workspace revision is a local binding over existing app-owned mappings and an
approved outline; it is **not** an approval, dispatch, or execution grant. An
unavailable raw mapping **fails closed** (a missing mapping is refused; a model is
never fabricated). A historical revision (``current=False``) stays inspectable
after global changes but **never** carries execution authority.

Fixture disclosure: the tests exercise this module with synthetic runtime
mappings recorded through the real :class:`RuntimeRepository`; no live OpenCode
service/session is created (the production route is out of scope here).
"""

from __future__ import annotations

import json

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..parsing.repository import _NestedStore
from ..runtime.gates import MAX_GATE_SNAPSHOT_ENTRIES
from ..storage.agent_runtime import RuntimeRepository
from ..storage.artifacts import ArtifactRepository, DependencyRef, sha256_digest
from ..synthesis.workflow import SynthesisWorkflowError, SynthesisWorkflowRepository

#: The generic document envelope type this repository writes and reads.
DOCUMENT_TYPE = "drafting_section_workspace"

#: The generic artifact kind for a workspace revision (C01 §5.1).
ARTIFACT_KIND = "report"

#: The exact, ordered payload keys this repository accepts.
PAYLOAD_KEYS = ("outline_ref", "section_id", "planner_mapping_id", "writer_mapping_id")

#: The concrete configured scientific role bound to each slot. The planner reuses
#: the existing ``synthesizer`` role for a planning purpose distinct from any
#: execution role; no new scientific role is introduced.
PLANNER_ROLE = "synthesizer"
WRITER_ROLE = "writer"

#: The two slot names, in canonical order.
SLOTS = ("planner", "writer")

#: The stored envelope's exact keys.
_ENVELOPE_KEYS = ("input", "result")

NOTICE = (
    "Per-section planner/writer workspace identity: a local binding over existing "
    "app-owned runtime session mappings (immutable project/section/session/role/"
    "model/config pins) and one exact approved outline revision. It is not an "
    "approval, dispatch, or execution grant; a historical revision is inspectable "
    "but never authorizes execution. The planner reuses the 'synthesizer' role for "
    "planning and the writer the 'writer' role; both are app-owned configured "
    "mappings created elsewhere. Approved artifacts, not chat history, are "
    "authoritative."
)


class SectionWorkspaceError(ReviewAgentError):
    """A workspace input, dependency, runtime mapping, or stored document is invalid."""


def require(condition, message=None):
    if not condition:
        raise SectionWorkspaceError(
            message or "section workspace input, dependency, or mapping is invalid"
        )


class SectionWorkspaceRepository:
    """Write and read durable per-section planner/writer workspace identities.

    ``store`` is an already-verified :class:`ProjectStore`. Typical use::

        repo = SectionWorkspaceRepository(store)
        ref = repo.propose(
            {"outline_ref": outline_ref, "section_id": "sec1",
             "planner_mapping_id": planner.mapping_id,
             "writer_mapping_id": writer.mapping_id},
            artifact_id="ws-sec1", revision_id="ws-sec1-r1",
            rights=<RightsRecord bound to this revision>,
        )
        payload = repo.read(ref)            # current, re-derived
        history = repo.read(ref, current=False)   # inspectable, non-authorizing
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(_NestedStore(store))

    # -- refs ---------------------------------------------------------------- #
    def _ref(self, ref):
        return validate_ref(ref, self.store.project_id)

    def _mapping_id(self, value):
        try:
            return validate_identifier(value, "mapping_id")
        except RecordValidationError as exc:
            raise SectionWorkspaceError(str(exc)) from exc

    def _section_id(self, value):
        try:
            return validate_identifier(value, "section_id")
        except RecordValidationError as exc:
            raise SectionWorkspaceError(str(exc)) from exc

    # -- runtime mapping pin ------------------------------------------------- #
    def _pin_mapping(self, mapping_id, *, role, purpose):
        """Pin one existing app-owned runtime mapping to a slot (fail closed).

        The mapping is re-read through :class:`RuntimeRepository` (which revalidates
        its row and its configuration link), so a missing mapping or a mapping for a
        **foreign role** is refused, and the concrete configured model is taken from
        the stored row — never supplied in-band and never defaulted.
        """
        mapping_id = self._mapping_id(mapping_id)
        mapping = RuntimeRepository(self.store).get_session_mapping(mapping_id)
        require(
            mapping is not None,
            "the referenced runtime session mapping does not exist",
        )
        require(
            mapping.role == role,
            "the referenced runtime mapping has a foreign role for this slot",
        )
        return {
            "purpose": purpose,
            "role": mapping.role,
            "mapping_id": mapping.mapping_id,
            "session_id": mapping.session_id,
            "service_instance_id": mapping.service_instance_id,
            "opencode_project_id": mapping.opencode_project_id,
            "model": {
                "provider_id": mapping.provider_id,
                "model_id": mapping.model_id,
                "variant": mapping.variant,
            },
            "model_ref": mapping.model_ref,
            "config_digest": mapping.config_digest,
            "config_snapshot_id": mapping.config_snapshot_id,
        }

    # -- outline section existence ------------------------------------------- #
    def _current_outline_section_ids(self, outline_ref):
        """Return the section ids of the exact *current approved* outline.

        Fails closed (wrapped as :class:`SectionWorkspaceError`) when the outline is
        superseded, its gate is stale, or the accepted chain is otherwise invalid.
        """
        try:
            context = SynthesisWorkflowRepository(self.store).require_for_blueprints(
                outline_ref
            )
        except SynthesisWorkflowError as exc:
            raise SectionWorkspaceError(str(exc)) from exc
        sections = context["outline"].get("sections") or []
        return {s.get("section_id") for s in sections if isinstance(s, dict)}

    def _stored_outline_section_ids(self, outline_ref):
        """Return the section ids of the exact *stored* outline revision.

        Used for a historical read: it verifies the outline revision's bytes and
        envelope directly (no gate/current requirement), so an old workspace stays
        inspectable after a global change without conferring authority.
        """
        ref = self._ref(outline_ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        require(rev is not None, "the outline dependency revision is missing")
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(
            sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size,
            "the outline dependency revision fails content integrity",
        )
        try:
            doc = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise SectionWorkspaceError(
                "the outline dependency revision is not decodable"
            ) from None
        require(
            isinstance(doc, dict)
            and doc.get("schema_version") == 1
            and doc.get("document_type") == "synthesis_outline",
            "the outline dependency is not a synthesis outline revision",
        )
        payload = doc.get("payload")
        result = payload.get("result") if isinstance(payload, dict) else None
        sections = (result or {}).get("sections") or []
        return {s.get("section_id") for s in sections if isinstance(s, dict)}

    # -- bounded per-section workspace history ------------------------------- #
    def _workspace_history(self):
        """Return ``(section_id, planner_mapping_id, writer_mapping_id)`` entries.

        A bounded, integrity-checked scan of every stored report revision, used to
        enforce per-section exclusivity. **Every** scanned revision's bytes are
        first verified against its immutable metadata (so a tampered row fails
        closed); a verified raw-byte receipt or another document type is then
        ignored, while a matching workspace document has its same-project refs
        validated and any malformed/foreign/corrupt matching document **fails
        closed**. The scan is bounded by :data:`MAX_GATE_SNAPSHOT_ENTRIES` matching
        workspace revisions.
        """
        entries = []
        scanned = 0
        for artifact in self.artifacts.list_artifacts():
            if artifact.kind != ARTIFACT_KIND:
                continue
            for revision in self.artifacts.list_revisions(artifact.artifact_id):
                stored = self.artifacts.get_revision(
                    artifact.artifact_id, revision.revision_id
                )
                if stored is None:
                    raise SectionWorkspaceError("a stored report revision is missing")
                data = self.artifacts.read_content(
                    artifact.artifact_id, revision.revision_id
                )
                # Verify the stored bytes against the revision's immutable metadata
                # for EVERY scanned report revision, BEFORE any decode/filter.
                # ``read_content`` returns bytes without hash validation, so a
                # tampered row — bytes replaced with raw bytes or with another
                # document_type — must fail closed here rather than silently vanish
                # from the scan (which would otherwise allow a cross-section reuse).
                require(
                    sha256_digest(data) == stored.content_sha256
                    and len(data) == stored.byte_size,
                    "a stored report revision fails content integrity",
                )
                try:
                    doc = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    # A verified raw-byte report (e.g., a dispatch receipt) is not a
                    # document and is ignored only after the integrity pass above.
                    continue
                if not isinstance(doc, dict) or doc.get("document_type") != DOCUMENT_TYPE:
                    continue
                scanned += 1
                require(
                    scanned <= MAX_GATE_SNAPSHOT_ENTRIES,
                    "section workspace history exceeds the scan bound",
                )
                payload = doc.get("payload")
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("input"), dict)
                ):
                    raise SectionWorkspaceError(
                        "a stored workspace has a malformed payload"
                    )
                input_payload = payload["input"]
                if set(input_payload) != set(PAYLOAD_KEYS):
                    raise SectionWorkspaceError(
                        "a stored workspace has a malformed input"
                    )
                try:
                    self._ref(input_payload["outline_ref"])
                    section_id = self._section_id(input_payload["section_id"])
                    planner_id = self._mapping_id(input_payload["planner_mapping_id"])
                    writer_id = self._mapping_id(input_payload["writer_mapping_id"])
                except (RecordValidationError, SectionWorkspaceError):
                    raise SectionWorkspaceError(
                        "a stored workspace carries a foreign or invalid reference"
                    ) from None
                entries.append((section_id, planner_id, writer_id))
        return entries

    def _require_per_section_exclusive(self, result):
        """Refuse mappings already bound to a *different* section (per-section)."""
        used_by_other_sections = set()
        for section_id, planner_id, writer_id in self._workspace_history():
            if section_id != result["section_id"]:
                used_by_other_sections.add(planner_id)
                used_by_other_sections.add(writer_id)
        require(
            result["planner"]["mapping_id"] not in used_by_other_sections
            and result["writer"]["mapping_id"] not in used_by_other_sections,
            "a runtime mapping/session is already bound to a different section",
        )

    # -- normalized derivation (re-run against durable rows) ----------------- #
    def _normalize(self, input_payload, *, require_current):
        require(
            isinstance(input_payload, dict)
            and set(input_payload) == set(PAYLOAD_KEYS),
            "a workspace payload must be exactly outline_ref, section_id, "
            "planner_mapping_id, writer_mapping_id",
        )
        outline_ref = self._ref(input_payload["outline_ref"])
        section_id = self._section_id(input_payload["section_id"])

        if require_current:
            section_ids = self._current_outline_section_ids(outline_ref)
            require(
                section_id in section_ids,
                "the section is not present in the approved outline",
            )
        else:
            section_ids = self._stored_outline_section_ids(outline_ref)
            require(
                section_id in section_ids,
                "the section is not present in the stored outline revision",
            )

        planner = self._pin_mapping(
            input_payload["planner_mapping_id"], role=PLANNER_ROLE, purpose="planning"
        )
        writer = self._pin_mapping(
            input_payload["writer_mapping_id"], role=WRITER_ROLE, purpose="drafting"
        )
        require(
            planner["mapping_id"] != writer["mapping_id"]
            and planner["session_id"] != writer["session_id"],
            "the planner and writer must be distinct app-owned sessions",
        )
        return {
            "schema_version": 1,
            "project_id": self.store.project_id,
            "section_id": section_id,
            "outline_ref": outline_ref,
            "planner": planner,
            "writer": writer,
            "distinct_sessions": True,
            "grants_execution": False,
            "notice": NOTICE,
        }

    # -- integrity-checked stored read --------------------------------------- #
    def _read(self, ref, *, current=True):
        """Read one stored document, verifying bytes, envelope, and dependency set."""
        ref = self._ref(ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        require(rev is not None, "the workspace revision does not exist")
        if current:
            head = self.artifacts.current_revision(ref["artifact_id"])
            require(
                head is not None and head.revision_id == rev.revision_id,
                "the workspace revision is not the current head",
            )
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(
            sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size,
            "the workspace revision fails content integrity",
        )
        doc = json.loads(data)
        require(
            doc.get("schema_version") == 1 and doc.get("document_type") == DOCUMENT_TYPE,
            "the stored document is not a section workspace",
        )
        payload = doc["payload"]
        require(
            isinstance(payload, dict) and set(payload) == set(_ENVELOPE_KEYS),
            "the stored workspace payload is malformed",
        )
        expected = {canonical_json(self._ref(payload["input"]["outline_ref"]))}
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(all(d.role == "derived_from" for d in live))
        require(
            {canonical_json(d.as_ref()) for d in live} == expected,
            "the stored workspace dependency set does not match its outline ref",
        )
        return payload

    # -- authority reads ----------------------------------------------------- #
    def read(self, ref, current=True):
        """Read one workspace revision, re-deriving its mapping from durable rows.

        ``current`` defaults to ``True`` (the exact current head, over the *current
        approved* outline). ``current=False`` inspects a historical revision: it
        re-validates the stored bytes, the runtime mapping pins, and the exact
        stored outline revision, but requires no live gate — and never confers
        execution authority.
        """
        payload = self._read(ref, current=current)
        require(
            self._normalize(payload["input"], require_current=current)
            == payload["result"],
            "the stored workspace mapping does not re-derive from durable rows",
        )
        return payload

    def require_for_drafting(self, ref):
        """Require the current workspace identity and its approved outline context.

        Returns the normalized mapping plus the accepted outline/framework/synthesis
        context and the exact refs a later drafting step would pin. This confers no
        execution authority.
        """
        payload = self.read(ref, current=True)
        outline_ref = self._ref(payload["result"]["outline_ref"])
        try:
            context = SynthesisWorkflowRepository(self.store).require_for_blueprints(
                outline_ref
            )
        except SynthesisWorkflowError as exc:
            raise SectionWorkspaceError(str(exc)) from exc
        return {
            "workspace": payload["result"],
            "outline_ref": context["outline_ref"],
            "framework_ref": context["framework_ref"],
            "synthesis_ref": context["synthesis_ref"],
            "section_id": payload["result"]["section_id"],
            "grants_execution": False,
            "notice": NOTICE,
        }

    # -- writes -------------------------------------------------------------- #
    def propose(self, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert workspace revision, atomically validating its inputs.

        The current approved outline, the section, and both runtime mapping pins are
        validated before anything is written. The mappings must not already be bound
        to a **different** section (per-section exclusivity). On an edit
        (``parent_revision_id`` is given) the section and the planner/writer pins must
        be retained; the outline ref may advance only to a **new revision of the same
        logical outline artifact** (the new outline must be current and approved and
        the section must still exist in it). A persistent workspace identity is never
        silently retargeted, and no blueprint is auto-approved.
        """
        with self.store.transaction():
            require(
                isinstance(payload, dict) and set(payload) == set(PAYLOAD_KEYS),
                "a workspace payload must be exactly outline_ref, section_id, "
                "planner_mapping_id, writer_mapping_id",
            )
            result = self._normalize(payload, require_current=True)
            self._require_per_section_exclusive(result)
            if parent_revision_id is not None:
                parent_ref = {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id,
                    "revision_id": parent_revision_id,
                }
                parent = self._read(parent_ref, current=False)
                parent_result = parent["result"]
                parent_outline = self._ref(parent_result["outline_ref"])
                new_outline = self._ref(result["outline_ref"])
                require(
                    parent_result["section_id"] == result["section_id"],
                    "an edit may not diverge the workspace section",
                )
                require(
                    parent_outline["artifact_id"] == new_outline["artifact_id"],
                    "an edit may only advance to a new revision of the same logical outline",
                )
                for slot in SLOTS:
                    require(
                        parent_result[slot]["mapping_id"] == result[slot]["mapping_id"],
                        "an edit may not diverge the workspace runtime mapping pins",
                    )
            outline_ref = self._ref(payload["outline_ref"])
            deps = [DependencyRef(**outline_ref, role="derived_from")]
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
                    "payload": {"input": payload, "result": result},
                },
                dependencies=deps,
                origin_locator="drafting:workspace",
                acquired_by="user",
                parent_revision_id=parent_revision_id,
            )
        return {
            "project_id": self.store.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }


#: Convenience alias used by callers that prefer the shorter name.
SectionWorkspace = SectionWorkspaceRepository

__all__ = [
    "ARTIFACT_KIND",
    "DOCUMENT_TYPE",
    "NOTICE",
    "PAYLOAD_KEYS",
    "PLANNER_ROLE",
    "SLOTS",
    "SectionWorkspace",
    "SectionWorkspaceError",
    "SectionWorkspaceRepository",
    "WRITER_ROLE",
]
