"""Durable, owner-gated section-blueprint/draft/manuscript-assembly workflow.

This module owns three bounded Step 11 outcomes over one durable workflow
repository:

* ``blueprint`` (S11-BLUEPRINT): bind the pure Step 11 section-blueprint contract
  (:func:`review_article_agent.drafting.schema.validate_blueprint`) to immutable
  storage, the integrated Step 11 per-section workspace identity
  (:class:`review_article_agent.drafting.workspace.SectionWorkspaceRepository`)
  and drafting package
  (:class:`review_article_agent.drafting.package.DraftingPackageRepository`), and
  the existing owner-authenticated named gates.  A blueprint revision may be
  created **only** over the exact *current approved* global chain (accepted
  synthesis -> framework -> outline), the exact current per-section workspace, and
  the reconstructible approved drafting package for that section.  Authority to
  **consume** a blueprint is only its own exact named owner gate.
* ``draft`` (S11-DRAFT): a durable, provisional section draft.  A draft revision
  may be created **only** by atomically consuming the exact current *approved*
  blueprint (its own named owner gate) and then validating the draft content
  (:func:`review_article_agent.drafting.schema.validate_draft`) against the
  approved blueprint result and package.  A draft is inert and provisional: it
  carries no gate of its own, never authorizes export, and never claims final or
  scientific approval.
* ``assembly`` (S11-ASSEMBLY): a durable provisional manuscript assembly over one
  exact approved outline revision.  It consumes exactly one *current valid* draft
  per approved outline section (no duplicate/missing/foreign section, no mixed
  outline/framework revisions), derives the section order from the outline (never
  the caller order), and stores a normalized Markdown-plus-structured result with
  per-sentence attribution/proposal labels and stable verified-corpus citation
  keys.  There is no final release/export API.

Stored generic documents:

* ``document_type = "drafting_blueprint"`` (artifact kind ``report``)::

    {"input": {workspace_ref, content},
     "workspace": <the normalized current workspace mapping>,
     "package": <the full approved drafting-package snapshot>,
     "result": <the validated blueprint>}

* ``document_type = "drafting_draft"`` (artifact kind ``report``)::

    {"input": {blueprint_ref, content},
     "blueprint": <the approved validated blueprint result>,
     "package": <the approved drafting-package snapshot>,
     "result": <the validated draft>}

* ``document_type = "drafting_assembly"`` (artifact kind ``report``)::

    {"input": {outline_ref, draft_refs},
     "context": <the approved outline/framework/synthesis context snapshot>,
     "drafts": [<the exact stored draft envelopes, in outline order>],
     "result": <the normalized assembly result>}

Authority to **consume** a blueprint is only its own exact named owner gate
(``artifact_revision`` / ``drafting.blueprint``).  In-band ``status``/``approved``
fields, an actor flag, a raw imported JSON document, or a *generic* artifact
approval confer **no** authority.  A changed shared argument (a new framework/
synthesis/outline/workspace revision, or a changed source) conservatively stales
the affected blueprint/draft/assembly; **only a blueprint's own gate** is
consulted, so an unapproved *other* section never blocks an approved section.

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

Not in scope: final release/export, scientific approval, origin tracing, and
source checks (all pending).  No model call, no network, no session execution, no
schema change, no new dependency, no migration, no CLI.  There is deliberately
**no** final release/export API: an assembly is a provisional local proposal.

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
from ..synthesis.workflow import SynthesisWorkflowError, SynthesisWorkflowRepository
from .package import DraftingPackageError, DraftingPackageRepository
from .schema import DraftingError, validate_blueprint, validate_draft
from .workspace import SectionWorkspaceError, SectionWorkspaceRepository

#: The exact proposal kinds this repository accepts.  All three are implemented.
KINDS = ("blueprint", "draft", "assembly")

#: The generic artifact kind for a drafting revision (C01 §5.1).
ARTIFACT_KIND = "report"

#: The generic document envelope type for a blueprint revision.
DOCUMENT_TYPE = "drafting_blueprint"

#: The generic document envelope type for a section-draft revision.
DRAFT_DOCUMENT_TYPE = "drafting_draft"

#: The generic document envelope type for a manuscript-assembly revision.
ASSEMBLY_DOCUMENT_TYPE = "drafting_assembly"

#: The exact, ordered payload keys a blueprint proposal accepts.
PAYLOAD_KEYS = ("workspace_ref", "content")

#: The exact, ordered payload keys a draft proposal accepts.
DRAFT_PAYLOAD_KEYS = ("blueprint_ref", "content")

#: The exact, ordered payload keys an assembly proposal accepts.
ASSEMBLY_PAYLOAD_KEYS = ("outline_ref", "draft_refs")

#: The exact named owner gate (``gate_kind``, ``gate_id``) for a blueprint.
GATE_KIND = "artifact_revision"
GATE_ID = "drafting.blueprint"

#: The stored blueprint envelope's exact keys.
_ENVELOPE_KEYS = frozenset({"input", "workspace", "package", "result"})

#: The stored draft envelope's exact keys.
_DRAFT_ENVELOPE_KEYS = frozenset({"input", "blueprint", "package", "result"})

#: The stored assembly envelope's exact keys.
_ASSEMBLY_ENVELOPE_KEYS = frozenset({"input", "context", "drafts", "result"})

#: A drafting artifact is always provisional with these checks pending.
PENDING_CHECKS = ("source_checks", "origin_tracing")

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

DRAFT_NOTICE = (
    "A section draft is provisional and inert: it is derived only from its exact "
    "current approved blueprint and approved drafting package (readable summary "
    "plus linked original evidence), preserves per-sentence attribution and "
    "proposal labels, and cites only verified-corpus records. It carries no gate "
    "of its own, never authorizes execution or export, and never claims final or "
    "scientific approval. Source checks and origin tracing are pending; this "
    "artifact is not exportable as final approved output."
)

ASSEMBLY_NOTICE = (
    "A manuscript assembly is provisional: it consumes exactly one current valid "
    "section draft per approved outline section (order derived from the outline, "
    "never the caller) and renders per-sentence attribution/proposal labels with "
    "stable verified-corpus citation keys. It is not a final release or export, "
    "carries no scientific approval, and there is deliberately no final/export "
    "API. Source checks and origin tracing are pending; this artifact is not "
    "exportable as final approved output."
)


def _sentence_labels(sentence):
    """Return visible attribution/proposal labels from the draft sentence metadata.

    Derived only from the schema's per-sentence metadata (attributions,
    classifications, ``secondary``/``proposed_synthesis`` flags) so a secondary
    account, a novel author proposal, an author interpretation, a hypothesis, or a
    mixed/unclear attribution is labelled -- never inferred from prose.
    """
    labels = []
    attributions = sentence.get("attributions") or []
    classifications = sentence.get("classifications") or []
    if sentence.get("secondary") or "secondary_account" in attributions:
        labels.append("secondary account")
    if sentence.get("proposed_synthesis") or "proposed_synthesis" in attributions:
        labels.append("author proposal")
    if "author_interpretation" in attributions:
        labels.append("author interpretation")
    if "mixed" in attributions:
        labels.append("mixed attribution")
    if "unclear" in attributions:
        labels.append("unclear attribution")
    if "hypothesis" in classifications:
        labels.append("hypothesis")
    if "new_interpretation" in classifications:
        labels.append("new interpretation")
    seen = set()
    return [label for label in labels if not (label in seen or seen.add(label))]


def _render_markdown(result):
    """Render the normalized assembly result as provisional, labelled Markdown.

    The heading is visibly ``PROVISIONAL`` and states the pending source checks and
    origin tracing.  Each sentence carries its exact approved text, a citation
    marker built from the stable verified-corpus keys, and any per-sentence
    attribution/proposal/warning labels.
    """
    lines = [
        "# PROVISIONAL MANUSCRIPT ASSEMBLY",
        "",
        "> **PROVISIONAL.** Source checks and origin tracing are pending; this is "
        "not exportable as final approved output.",
        "",
    ]
    for section in result["sections"]:
        lines.append(f"## {section['order']}. {section['title']}")
        lines.append("")
        for paragraph in section["paragraphs"]:
            rendered = []
            for sentence in paragraph["sentences"]:
                line = sentence["text"]
                keys = sentence.get("citation_keys") or []
                if keys:
                    line += " [" + "; ".join(keys) + "]"
                annotations = list(sentence.get("labels") or [])
                annotations.extend(sentence.get("warnings") or [])
                if annotations:
                    line += " (" + "; ".join(annotations) + ")"
                rendered.append(line)
            # One approved paragraph stays one Markdown paragraph: its sentences
            # are joined with a single space, and a blank line separates only
            # consecutive paragraphs (never two sentences of the same paragraph).
            lines.append(" ".join(rendered))
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


class DraftingWorkflowError(ReviewAgentError):
    """A blueprint/draft/assembly input, dependency, or approval is invalid or stale."""


def require(condition, message=None):
    if not condition:
        raise DraftingWorkflowError(
            message
            or "drafting input, dependency, or approval is invalid or stale"
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
        if kind not in KINDS:
            raise DraftingWorkflowError("unknown drafting proposal kind")

    # -- dependency set ------------------------------------------------------ #
    def _package_refs(self, package):
        """Return the package's own global/evidence dependency refs (no workspace).

        The package's global refs (outline/framework/synthesis/audit/acquisition/
        freeze/corpus) plus each evidence entry's exact extraction and source refs.
        """
        refs = []
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
        return refs

    def _dedupe_refs(self, refs):
        """Return the exact, deterministic, deduplicated same-project refs."""
        unique = {}
        for ref in refs:
            unique[canonical_json(self._ref(ref))] = self._ref(ref)
        return [unique[key] for key in sorted(unique)]

    def _dependencies(self, package, workspace_ref):
        """Return the exact, deduplicated dependency refs for one blueprint.

        Includes the exact workspace revision plus the package's global refs and
        each evidence entry's exact extraction and source refs.  Deterministic
        order (sorted by canonical form) and deduplicated, so the stored dependency
        set is byte-for-byte reproducible from the stored envelope.
        """
        return self._dedupe_refs([workspace_ref, *self._package_refs(package)])

    def _draft_dependencies(self, package, blueprint_ref):
        """Return the exact, deduplicated dependency refs for one draft.

        Includes the exact approved blueprint revision plus the package's global
        and evidence refs, so a changed blueprint or shared global argument is a
        recorded dependency.
        """
        return self._dedupe_refs([blueprint_ref, *self._package_refs(package)])

    def _assembly_dependencies(self, context, draft_refs, drafts):
        """Return the exact, deduplicated dependency refs for one assembly.

        Includes the exact approved outline/framework/synthesis refs, every
        consumed draft revision, every consumed draft's blueprint revision, and the
        package refs of each consumed draft -- so a changed draft, blueprint, or
        shared global argument is a recorded dependency.  ``drafts`` is a list of
        ``{"draft_ref", "draft"}`` entries.
        """
        refs = [context["outline_ref"], context["framework_ref"], context["synthesis_ref"]]
        refs.extend(draft_refs)
        for entry in drafts:
            draft = entry["draft"]
            refs.append(draft["input"]["blueprint_ref"])
            refs.extend(self._package_refs(draft["package"]))
        return self._dedupe_refs(refs)

    # -- integrity-checked stored read --------------------------------------- #
    def _load_document(self, ref, *, current, document_type, envelope_keys):
        """Read one stored document, verifying bytes, type, and envelope keys."""
        ref = self._ref(ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        require(rev is not None, "the drafting revision does not exist")
        if current:
            head = self.artifacts.current_revision(ref["artifact_id"])
            require(
                head is not None and head.revision_id == rev.revision_id,
                "the drafting revision is not the current head",
            )
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(
            sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size,
            "the drafting revision fails content integrity",
        )
        try:
            doc = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise DraftingWorkflowError(
                "the stored drafting document is not decodable"
            ) from None
        require(
            isinstance(doc, dict)
            and doc.get("schema_version") == 1
            and doc.get("document_type") == document_type,
            "the stored document is not the expected drafting document",
        )
        payload = doc.get("payload")
        require(
            isinstance(payload, dict) and set(payload) == set(envelope_keys),
            "the stored drafting payload is malformed",
        )
        return ref, payload

    def _check_deps(self, ref, expected):
        """Verify the stored dependency edges are exactly the derived set."""
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(
            all(d.role == "derived_from" for d in live),
            "the stored drafting document has a dependency role outside the approved vocabulary",
        )
        require(
            {canonical_json(d.as_ref()) for d in live} == expected,
            "the stored drafting dependency set does not match its envelope",
        )

    def _read(self, ref, *, current=True):
        """Read one stored blueprint, verifying bytes, envelope, and dependency set."""
        ref, payload = self._load_document(
            ref, current=current, document_type=DOCUMENT_TYPE, envelope_keys=_ENVELOPE_KEYS
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
        self._check_deps(ref, expected)
        return payload

    def _read_draft(self, ref, *, current=True):
        """Read one stored draft, verifying bytes, envelope, and dependency set."""
        ref, payload = self._load_document(
            ref,
            current=current,
            document_type=DRAFT_DOCUMENT_TYPE,
            envelope_keys=_DRAFT_ENVELOPE_KEYS,
        )
        input_payload = payload.get("input")
        require(
            isinstance(input_payload, dict)
            and set(input_payload) == set(DRAFT_PAYLOAD_KEYS),
            "the stored draft input is malformed",
        )
        try:
            expected = {
                canonical_json(r)
                for r in self._draft_dependencies(
                    payload["package"], self._ref(input_payload["blueprint_ref"])
                )
            }
        except (RecordValidationError, TypeError, KeyError):
            raise DraftingWorkflowError(
                "the stored draft carries a foreign or invalid dependency"
            ) from None
        self._check_deps(ref, expected)
        return payload

    def _read_assembly(self, ref, *, current=True):
        """Read one stored assembly, verifying bytes, envelope, and dependency set."""
        ref, payload = self._load_document(
            ref,
            current=current,
            document_type=ASSEMBLY_DOCUMENT_TYPE,
            envelope_keys=_ASSEMBLY_ENVELOPE_KEYS,
        )
        input_payload = payload.get("input")
        require(
            isinstance(input_payload, dict)
            and set(input_payload) == set(ASSEMBLY_PAYLOAD_KEYS),
            "the stored assembly input is malformed",
        )
        drafts = payload.get("drafts")
        require(
            isinstance(drafts, list) and drafts,
            "the stored assembly carries no draft dependencies",
        )
        for entry in drafts:
            require(
                isinstance(entry, dict)
                and set(entry) == {"draft_ref", "draft"}
                and isinstance(entry["draft"], dict),
                "the stored assembly draft entry is malformed",
            )
        try:
            draft_refs = [self._ref(r) for r in input_payload["draft_refs"]]
            stored_refs = [self._ref(entry["draft_ref"]) for entry in drafts]
            require(
                {canonical_json(r) for r in draft_refs}
                == {canonical_json(r) for r in stored_refs},
                "the stored assembly draft refs do not match its draft snapshots",
            )
            expected = {
                canonical_json(r)
                for r in self._assembly_dependencies(payload["context"], draft_refs, drafts)
            }
        except (RecordValidationError, TypeError, KeyError):
            raise DraftingWorkflowError(
                "the stored assembly carries a foreign or invalid dependency"
            ) from None
        self._check_deps(ref, expected)
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

    # -- draft re-derivation ------------------------------------------------- #
    def _rederive_draft_current(self, payload):
        """Re-derive the current approved blueprint/package and the draft result.

        Fails closed when the stored blueprint is no longer the current approved
        head (its own gate is re-checked by :meth:`require_blueprint`) or the
        stored package/result no longer re-derives.
        """
        blueprint_ref = self._ref(payload["input"]["blueprint_ref"])
        approved = self.require_blueprint(blueprint_ref)
        require(
            approved["result"] == payload["blueprint"],
            "the current approved blueprint does not match the stored draft snapshot",
        )
        require(
            approved["package"] == payload["package"],
            "the current approved drafting package does not match the stored draft snapshot",
        )
        try:
            result = validate_draft(
                payload["input"]["content"],
                blueprint=payload["blueprint"],
                package=payload["package"],
            )
        except DraftingError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            result == payload["result"],
            "the stored draft result does not re-validate from durable inputs",
        )

    def _rederive_draft_historical(self, payload):
        """Re-validate a historical draft against its blueprint history + snapshot.

        The stored blueprint is re-read via ``read(current=False)`` (blueprint
        history: no live global approval) and the draft is re-validated against the
        **stored** blueprint/package snapshot.  No live gate is required.
        """
        blueprint_ref = self._ref(payload["input"]["blueprint_ref"])
        blueprint = self.read(blueprint_ref, "blueprint", current=False)
        require(
            blueprint["result"] == payload["blueprint"]
            and blueprint["package"] == payload["package"],
            "the stored draft snapshot does not match its blueprint history",
        )
        try:
            result = validate_draft(
                payload["input"]["content"],
                blueprint=payload["blueprint"],
                package=payload["package"],
            )
        except DraftingError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            result == payload["result"],
            "the stored draft result does not re-validate against its snapshot",
        )

    # -- assembly re-derivation ---------------------------------------------- #
    def _ordered_assembly_drafts(self, context, drafts):
        """Return ``(section, entry)`` pairs in approved outline order.

        The caller's order is deliberately discarded: the canonical order is
        derived from the approved outline.  Every approved outline section must be
        covered by exactly one draft, with no duplicate, missing, or foreign
        section; the returned list is the normalized, immutable snapshot order that
        is stored in the envelope.
        """
        sections = context["outline"]["sections"]
        outline_ids = [section["section_id"] for section in sections]
        require(
            len(outline_ids) == len(set(outline_ids)),
            "the approved outline has duplicate section ids",
        )
        by_section = {}
        for entry in drafts:
            section_id = entry["draft"]["blueprint"]["section_id"]
            require(
                section_id not in by_section,
                "the assembly has more than one draft for one approved section",
            )
            by_section[section_id] = entry
        require(
            set(by_section) == set(outline_ids),
            "the assembly must cover exactly the approved outline sections "
            "(no missing or foreign section)",
        )
        return [
            (section, by_section[section["section_id"]])
            for section in sorted(sections, key=lambda s: s["order"])
        ]

    def _assemble(self, context, drafts):
        """Return the normalized assembly result for one context and draft set.

        Every approved outline section must be covered by exactly one draft, in
        outline order (never the input order), with no duplicate, missing, or
        foreign section and no mixed outline/framework revision.  Per-sentence
        attribution/proposal labels are derived from the schema metadata; citations
        are the stable verified-corpus records of each draft's approved package.
        """
        citations = {}
        all_warnings = set()
        ordered_sections = []
        for section, entry in self._ordered_assembly_drafts(context, drafts):
            draft = entry["draft"]
            package = draft["package"]
            require(
                canonical_json(package["outline_ref"])
                == canonical_json(context["outline_ref"]),
                "a section draft belongs to a different outline revision",
            )
            require(
                canonical_json(package["framework_ref"])
                == canonical_json(context["framework_ref"]),
                "a section draft belongs to a different framework revision",
            )
            paragraphs = []
            for paragraph in draft["result"]["paragraphs"]:
                sentences = []
                for sentence in paragraph["sentences"]:
                    for key in sentence["citation_keys"]:
                        require(
                            key in package["citations"],
                            "a sentence cites a key absent from its approved package",
                        )
                        citations[key] = package["citations"][key]
                    labels = _sentence_labels(sentence)
                    warnings = list(sentence.get("warnings") or [])
                    all_warnings.update(warnings)
                    sentences.append(
                        {
                            "intent_id": sentence["intent_id"],
                            "claim_ids": list(sentence["claim_ids"]),
                            "text": sentence["text"],
                            "citation_keys": list(sentence["citation_keys"]),
                            "attributions": list(sentence["attributions"]),
                            "classifications": list(sentence["classifications"]),
                            "warnings": warnings,
                            "labels": labels,
                            "secondary": bool(sentence["secondary"]),
                            "proposed_synthesis": bool(sentence["proposed_synthesis"]),
                        }
                    )
                paragraphs.append(
                    {"paragraph_id": paragraph["paragraph_id"], "sentences": sentences}
                )
            ordered_sections.append(
                {
                    "section_id": section["section_id"],
                    "order": section["order"],
                    "title": section["title"],
                    "draft_ref": dict(entry["draft_ref"]),
                    "blueprint_ref": dict(draft["input"]["blueprint_ref"]),
                    "paragraphs": paragraphs,
                }
            )

        result = {
            "schema_version": 1,
            "kind": "manuscript_assembly",
            "outline_ref": dict(context["outline_ref"]),
            "framework_ref": dict(context["framework_ref"]),
            "synthesis_ref": dict(context["synthesis_ref"]),
            "sections": ordered_sections,
            "citations": {key: citations[key] for key in sorted(citations)},
            "warnings": sorted(all_warnings),
            "status": "provisional",
            "authorizes_export": False,
            "scientific_validation": False,
            "final": False,
            "pending": list(PENDING_CHECKS),
            "notice": ASSEMBLY_NOTICE,
        }
        result["markdown"] = _render_markdown(result)
        return result

    def _require_assembly_outline_identity(self, payload):
        """Require the stored context outline to be the same project/ref as input."""
        outline_ref = self._ref(payload["input"]["outline_ref"])
        require(
            canonical_json(self._ref(payload["context"]["outline_ref"]))
            == canonical_json(outline_ref),
            "the stored assembly context outline does not match its input outline",
        )
        return outline_ref

    def _stored_drafts_by_ref(self, payload):
        """Index the stored draft entries by their exact same-project ref."""
        stored = {}
        for entry in payload["drafts"]:
            stored[canonical_json(self._ref(entry["draft_ref"]))] = entry
        return stored

    def _rederive_assembly_current(self, payload):
        """Re-derive the current approved context/drafts and the assembly result.

        Each stored draft snapshot is compared **exactly** to the actual
        referenced immutable draft (re-read current) -- a forged, internally
        self-consistent embedded snapshot is refused rather than inspected as real.
        """
        outline_ref = self._require_assembly_outline_identity(payload)
        try:
            context = SynthesisWorkflowRepository(self._nested).require_for_blueprints(
                outline_ref
            )
        except SynthesisWorkflowError as exc:
            raise DraftingWorkflowError(str(exc)) from exc
        require(
            context == payload["context"],
            "the current approved outline context does not match the stored snapshot",
        )
        stored = self._stored_drafts_by_ref(payload)
        drafts = []
        for ref in payload["input"]["draft_refs"]:
            ref = self._ref(ref)
            actual = {"draft_ref": ref, "draft": self.read(ref, "draft", current=True)}
            require(
                stored.get(canonical_json(ref)) == actual,
                "the stored assembly draft snapshot does not match the referenced "
                "immutable draft",
            )
            drafts.append(actual)
        result = self._assemble(context, drafts)
        require(
            result == payload["result"],
            "the stored assembly result does not re-derive from durable rows",
        )

    def _rederive_assembly_historical(self, payload):
        """Re-validate a historical assembly against its stored snapshot.

        Each stored draft snapshot is compared **exactly** to the actual
        referenced immutable draft re-read via ``read(current=False)`` (draft
        history + stored snapshot, no live approval), so a forged internally
        self-consistent embedded snapshot is refused.  The stored outline context
        and the verified draft envelopes are then re-assembled.
        """
        self._require_assembly_outline_identity(payload)
        for entry in payload["drafts"]:
            ref = self._ref(entry["draft_ref"])
            actual = self.read(ref, "draft", current=False)
            require(
                actual == entry["draft"],
                "the stored assembly draft snapshot does not match the referenced "
                "immutable draft",
            )
        result = self._assemble(payload["context"], payload["drafts"])
        require(
            result == payload["result"],
            "the stored assembly result does not re-validate against its snapshot",
        )

    # -- authority reads ----------------------------------------------------- #
    def read(self, ref, kind="blueprint", *, current=True):
        """Read one drafting revision, re-deriving it from durable rows.

        ``kind`` selects the document type: ``blueprint`` (default), ``draft``, or
        ``assembly``.  ``current`` defaults to ``True`` (the exact current head,
        re-derived over the current approved chain).  ``current=False`` inspects a
        historical revision against its stored snapshot, requiring no live global
        approval -- and never conferring authority.
        """
        self._require_kind(kind)
        if kind == "blueprint":
            payload = self._read(ref, current=current)
            if current:
                self._rederive_current(payload)
            else:
                self._rederive_historical(payload)
            return payload
        if kind == "draft":
            payload = self._read_draft(ref, current=current)
            if current:
                self._rederive_draft_current(payload)
            else:
                self._rederive_draft_historical(payload)
            return payload
        payload = self._read_assembly(ref, current=current)
        if current:
            self._rederive_assembly_current(payload)
        else:
            self._rederive_assembly_historical(payload)
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
        """Return a safe, structured status for one current drafting revision.

        ``kind`` selects the document type (``blueprint``/``draft``/``assembly``).
        The stored revision's bytes/envelope/dependency set are verified first (a
        corrupt row fails closed with a static error rather than reporting a benign
        status).  Then the current approved chain is re-derived; any staleness is
        reported as structured ``reasons``.  A drafting artifact is always
        ``provisional`` and never authorizes export, final release, or scientific
        approval.
        """
        self._require_kind(kind)
        ref = self._ref(ref)
        if kind == "blueprint":
            return self._status_blueprint(ref)
        if kind == "draft":
            return self._status_draft(ref)
        return self._status_assembly(ref)

    def _status_blueprint(self, ref):
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
            "kind": "blueprint",
            "blueprint_ref": ref,
            "gate_kind": GATE_KIND,
            "gate_id": GATE_ID,
            "stale": stale,
            "reasons": reasons,
            "provisional": True,
            "pending": list(result.get("pending") or []),
            "authorizes_export": False,
        }

    def _status_draft(self, ref):
        payload = self._read_draft(ref, current=True)
        reasons = []
        stale = False
        try:
            self._rederive_draft_current(payload)
        except (DraftingWorkflowError, SectionWorkspaceError, DraftingPackageError):
            stale = True
            reasons.append(
                "the current approved blueprint/package chain no longer "
                "re-derives the stored draft snapshot"
            )
        result = payload["result"]
        return {
            "kind": "draft",
            "draft_ref": ref,
            "blueprint_ref": self._ref(payload["input"]["blueprint_ref"]),
            "stale": stale,
            "reasons": reasons,
            "provisional": True,
            "pending": list(result.get("pending") or []),
            "authorizes_export": False,
            "final": False,
            "scientific_validation": False,
        }

    def _status_assembly(self, ref):
        payload = self._read_assembly(ref, current=True)
        reasons = []
        stale = False
        try:
            self._rederive_assembly_current(payload)
        except (DraftingWorkflowError, SectionWorkspaceError, DraftingPackageError):
            stale = True
            reasons.append(
                "the current approved outline/draft chain no longer "
                "re-derives the stored assembly snapshot"
            )
        result = payload["result"]
        return {
            "kind": "assembly",
            "assembly_ref": ref,
            "outline_ref": self._ref(payload["input"]["outline_ref"]),
            "stale": stale,
            "reasons": reasons,
            "provisional": True,
            "pending": list(result.get("pending") or []),
            "authorizes_export": False,
            "final": False,
            "scientific_validation": False,
        }

    # -- writes -------------------------------------------------------------- #
    def propose(self, kind, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert blueprint/draft/assembly revision, atomically.

        ``kind`` selects the document type.  A ``blueprint`` is created only over
        the exact current approved global chain, workspace, and package; a
        ``draft`` only by atomically consuming its exact current approved blueprint
        (its own named owner gate) and validating the draft against it; an
        ``assembly`` only over exactly one current valid draft per approved outline
        section.  Nothing is ever auto-approved.
        """
        self._require_kind(kind)
        if kind == "blueprint":
            return self._propose_blueprint(
                payload, artifact_id=artifact_id, revision_id=revision_id,
                rights=rights, parent_revision_id=parent_revision_id,
            )
        if kind == "draft":
            return self._propose_draft(
                payload, artifact_id=artifact_id, revision_id=revision_id,
                rights=rights, parent_revision_id=parent_revision_id,
            )
        return self._propose_assembly(
            payload, artifact_id=artifact_id, revision_id=revision_id,
            rights=rights, parent_revision_id=parent_revision_id,
        )

    def _propose_blueprint(self, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
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

    def _propose_draft(self, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert section-draft revision, atomically.

        The exact current *approved* blueprint is consumed via
        :meth:`require_blueprint` (re-deriving its current workspace/package and
        checking **only** its own named owner gate); the draft content is then
        validated against that approved blueprint result and package.  On an edit
        (``parent_revision_id`` is given) the draft may rebind only to a **new
        revision of the same logical blueprint artifact** and may not diverge the
        section.  A draft carries no gate of its own and is never auto-approved.
        """
        with self._nested.transaction():
            require(
                isinstance(payload, dict) and set(payload) == set(DRAFT_PAYLOAD_KEYS),
                "a draft payload must be exactly blueprint_ref, content",
            )
            blueprint_ref = self._ref(payload["blueprint_ref"])
            approved = self.require_blueprint(blueprint_ref)
            blueprint = approved["result"]
            package = approved["package"]
            try:
                result = validate_draft(
                    payload["content"], blueprint=blueprint, package=package
                )
            except DraftingError as exc:
                raise DraftingWorkflowError(str(exc)) from exc
            require(
                result["section_id"] == blueprint["section_id"],
                "the draft section does not match the approved blueprint section",
            )
            if parent_revision_id is not None:
                parent_ref = {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id,
                    "revision_id": parent_revision_id,
                }
                parent = self._read_draft(parent_ref, current=False)
                parent_blueprint_ref = parent["input"]["blueprint_ref"]
                require(
                    parent_blueprint_ref["artifact_id"] == blueprint_ref["artifact_id"],
                    "an edit may not switch the logical blueprint identity",
                )
                require(
                    parent["blueprint"]["section_id"] == blueprint["section_id"],
                    "an edit may not diverge the draft section",
                )
            deps = [
                DependencyRef(**ref, role="derived_from")
                for ref in self._draft_dependencies(package, blueprint_ref)
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
                    "document_type": DRAFT_DOCUMENT_TYPE,
                    "payload": {
                        "input": payload,
                        "blueprint": blueprint,
                        "package": package,
                        "result": result,
                    },
                },
                dependencies=deps,
                origin_locator="drafting:draft",
                acquired_by="user",
                parent_revision_id=parent_revision_id,
            )
        return {
            "project_id": self.store.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }

    def _propose_assembly(self, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert manuscript-assembly revision, atomically.

        Consumes the exact current approved outline context and exactly one
        *current valid* draft per approved outline section (no duplicate, missing,
        or foreign section and no mixed outline/framework revision).  The section
        order is derived from the outline, never the caller's ``draft_refs`` order,
        and the stored draft snapshots are canonically normalized to that order.
        The result stores the exact draft dependencies and a normalized
        Markdown-plus-structured manuscript with stable verified-corpus citations.
        On an edit (``parent_revision_id`` is given) the parent is validated via
        its historical assembly read; the new revision may advance to a **new
        current revision of the same logical outline artifact** but may not switch
        the logical outline identity.  No assembly is auto-approved.
        """
        with self._nested.transaction():
            require(
                isinstance(payload, dict)
                and set(payload) == set(ASSEMBLY_PAYLOAD_KEYS),
                "an assembly payload must be exactly outline_ref, draft_refs",
            )
            outline_ref = self._ref(payload["outline_ref"])
            raw_refs = payload["draft_refs"]
            require(
                isinstance(raw_refs, list) and raw_refs,
                "an assembly requires a non-empty draft_refs list",
            )
            draft_refs = [self._ref(ref) for ref in raw_refs]
            require(
                len({canonical_json(ref) for ref in draft_refs}) == len(draft_refs),
                "an assembly draft_refs list must not repeat a draft revision",
            )
            try:
                context = SynthesisWorkflowRepository(
                    self._nested
                ).require_for_blueprints(outline_ref)
            except SynthesisWorkflowError as exc:
                raise DraftingWorkflowError(str(exc)) from exc
            drafts = []
            for ref in draft_refs:
                draft = self.read(ref, "draft", current=True)
                drafts.append({"draft_ref": ref, "draft": draft})
            # Canonical normalize to the approved outline order ONCE, and store it
            # in that order (never the caller's order).
            ordered_drafts = [
                entry for _section, entry in self._ordered_assembly_drafts(context, drafts)
            ]
            result = self._assemble(context, ordered_drafts)
            if parent_revision_id is not None:
                parent_ref = {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id,
                    "revision_id": parent_revision_id,
                }
                parent = self._read_assembly(parent_ref, current=False)
                parent_outline_ref = self._ref(parent["context"]["outline_ref"])
                require(
                    parent_outline_ref["artifact_id"]
                    == context["outline_ref"]["artifact_id"],
                    "an edit may not switch the logical outline identity",
                )
            deps = [
                DependencyRef(**ref, role="derived_from")
                for ref in self._assembly_dependencies(
                    context, draft_refs, ordered_drafts
                )
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
                    "document_type": ASSEMBLY_DOCUMENT_TYPE,
                    "payload": {
                        "input": payload,
                        "context": context,
                        "drafts": ordered_drafts,
                        "result": result,
                    },
                },
                dependencies=deps,
                origin_locator="drafting:assembly",
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
    "ASSEMBLY_DOCUMENT_TYPE",
    "ASSEMBLY_NOTICE",
    "ASSEMBLY_PAYLOAD_KEYS",
    "DOCUMENT_TYPE",
    "DRAFT_DOCUMENT_TYPE",
    "DRAFT_NOTICE",
    "DRAFT_PAYLOAD_KEYS",
    "DraftingWorkflow",
    "DraftingWorkflowError",
    "DraftingWorkflowRepository",
    "GATE_ID",
    "GATE_KIND",
    "KINDS",
    "NOTICE",
    "PAYLOAD_KEYS",
    "PENDING_CHECKS",
]
