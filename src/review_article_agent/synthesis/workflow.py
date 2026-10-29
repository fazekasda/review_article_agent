"""Durable synthesis/framework/outline workflow (Step 10, S10-WORKFLOW).

One bounded outcome: bind the pure Step 10 payload contract
(:mod:`review_article_agent.synthesis.schema`) to immutable storage, the accepted
Step 09 evidence gate, and the existing owner-authenticated human gates.  A
synthesis proposal may be created **only** over verified, accepted Step 09
evidence (``ExtractionWorkflowRepository.require_for_synthesis`` then
``require_batch``); a framework proposal may only trace to an *accepted*
synthesis; an outline proposal may only trace to an accepted synthesis and
accepted framework.

Stored generic documents (``document_type`` in parentheses), artifact kind:

* ``synthesis_synthesis`` (``report``): ``{input: {audit_ref, content},
  result: <validated synthesis>, impact: <report or null>}``.
* ``synthesis_framework`` (``framework``): ``{input: {synthesis_ref, content},
  result, impact}``.
* ``synthesis_outline`` (``outline``): ``{input: {synthesis_ref, framework_ref,
  content}, result, impact}``.

Authority is *only* the existing named owner gates: ``artifact_revision`` /
``synthesis.memo``; ``framework`` / ``synthesis.framework``; ``outline`` /
``synthesis.outline``.  In-band ``status: approved`` fields, actor flags in a
payload, or a raw imported JSON document confer **no** authority.  A changed
synthesis or framework revision conservatively stales a dependent
framework/outline gate (the gate pins the exact dependency revisions); the
per-section impact report is advisory precision only.

Not in scope: no CLI, no model call, no blueprint generation, no schema change,
no new dependency or migration.  Human workflow approval is never established
scientific truth.
"""

from __future__ import annotations

import json

from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError, canonical_json, validate_ref
from ..extraction.workflow import (
    ExtractionWorkflowRepository,
    ExtractionWorkflowError,
)
from ..parsing.repository import _NestedStore
from ..runtime.gates import MAX_GATE_SNAPSHOT_ENTRIES
from ..storage.artifacts import ArtifactRepository, DependencyRef, sha256_digest
from ..storage.human_approvals import HumanApprovalRepository
from .schema import (
    SynthesisError,
    validate_framework,
    validate_outline,
    validate_synthesis,
)

#: The exact proposal kinds this repository accepts.
KINDS = ("synthesis", "framework", "outline")

#: Generic artifact kind per proposal kind (C01 §5.1).
_ARTIFACT_KIND = {"synthesis": "report", "framework": "framework", "outline": "outline"}

#: Exact named owner gate (``gate_kind``, ``gate_id``) per proposal kind.
GATE_SYNTHESIS_KIND = "artifact_revision"
GATE_SYNTHESIS = "synthesis.memo"
GATE_FRAMEWORK_KIND = "framework"
GATE_FRAMEWORK = "synthesis.framework"
GATE_OUTLINE_KIND = "outline"
GATE_OUTLINE = "synthesis.outline"

_GATES = {
    "synthesis": (GATE_SYNTHESIS_KIND, GATE_SYNTHESIS),
    "framework": (GATE_FRAMEWORK_KIND, GATE_FRAMEWORK),
    "outline": (GATE_OUTLINE_KIND, GATE_OUTLINE),
}

NOTICE = (
    "Synthesis/framework/outline revisions over verified accepted Step 09 evidence. "
    "Authority is only the named owner gate; an in-band status/actor flag or an "
    "imported JSON document is not approval. Shared underlying data is not "
    "independent; a null is not a refutation. A framework is a proposal unless "
    "justified as established knowledge. A changed dependency conservatively "
    "stales the dependent gate; section-level impact is advisory precision only. "
    "Human workflow approval is not scientific truth."
)

_IMPACT_NOTE = (
    "A changed revision conservatively stales the dependent outline gate (the gate "
    "pins the exact dependency revisions). Section precision below is advisory and "
    "does not claim the unaffected sections are globally accepted."
)


class SynthesisWorkflowError(ReviewAgentError):
    """A synthesis workflow input, dependency, or approval is invalid or stale."""


def require(condition, message=None):
    if not condition:
        raise SynthesisWorkflowError(
            message
            or "synthesis workflow input, dependency, or approval is invalid or stale"
        )


# --------------------------------------------------------------------------- #
# Imperative helpers
# --------------------------------------------------------------------------- #
def _changed_ids(old_items, new_items, key):
    old_by = {item[key]: canonical_json(item) for item in old_items}
    new_by = {item[key]: canonical_json(item) for item in new_items}
    changed = set()
    for name in set(old_by) | set(new_by):
        if old_by.get(name) != new_by.get(name):
            changed.add(name)
    return changed


class SynthesisWorkflowRepository:
    """Write inert proposals; existing owner-authenticated gates supply authority.

    Each action creates a report/framework/outline revision over an exact
    dependency.  An edit reuses the same artifact id with its current parent and
    stores an advisory impact report.  Reads recompute the normalized result from
    the durable inputs and every executable check requires the exact named human
    gate.
    """

    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactRepository(_NestedStore(store))

    # -- refs ---------------------------------------------------------------- #
    def _ref(self, ref):
        return validate_ref(ref, self.store.project_id)

    def _dependencies(self, kind, input_payload):
        if kind == "synthesis":
            return [input_payload["audit_ref"]]
        if kind == "framework":
            return [input_payload["synthesis_ref"]]
        if kind == "outline":
            return [input_payload["synthesis_ref"], input_payload["framework_ref"]]
        raise SynthesisWorkflowError("unknown synthesis proposal kind")

    def _read(self, ref, kind, *, current=True):
        """Read one stored document, verifying bytes, envelope, and dependency set."""
        if kind not in KINDS:
            raise SynthesisWorkflowError("unknown synthesis proposal kind")
        ref = self._ref(ref)
        rev = self.artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        require(rev is not None)
        if current:
            head = self.artifacts.current_revision(ref["artifact_id"])
            require(head is not None and head.revision_id == rev.revision_id)
        data = self.artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        require(sha256_digest(data) == rev.content_sha256 and len(data) == rev.byte_size)
        doc = json.loads(data)
        require(
            doc.get("schema_version") == 1
            and doc.get("document_type") == "synthesis_" + kind
        )
        payload = doc["payload"]
        require(set(payload) == {"input", "result", "impact"})
        expected = {canonical_json(self._ref(r)) for r in self._dependencies(kind, payload["input"])}
        actual = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in actual if d.role != "version_of"]
        require(all(d.role == "derived_from" for d in live))
        require({canonical_json(d.as_ref()) for d in live} == expected)
        return payload

    def _approved(self, ref, kind):
        ref = self._ref(ref)
        gate_kind, gate_id = _GATES[kind]
        require(
            HumanApprovalRepository(self.store).named_gate_satisfied(
                ref["artifact_id"], gate_kind=gate_kind, gate_id=gate_id
            )
        )

    # -- normalized derivation (re-run the pure schema on durable inputs) ----- #
    def _normalize(self, kind, input_payload):
        if kind == "synthesis":
            evidence = ExtractionWorkflowRepository(self.store).require_for_synthesis(
                input_payload["audit_ref"]
            )
            batch = ExtractionWorkflowRepository(self.store).require_batch(evidence["batch_ref"])
            return validate_synthesis(input_payload["content"], cases=batch["cases"])
        if kind == "framework":
            synthesis = self.require_synthesis(input_payload["synthesis_ref"])
            return validate_framework(input_payload["content"], synthesis=synthesis)
        if kind == "outline":
            synthesis_ref = self._ref(input_payload["synthesis_ref"])
            framework_ref = self._ref(input_payload["framework_ref"])
            synthesis = self.require_synthesis(synthesis_ref)
            framework = self.require_framework(framework_ref)
            # An outline must not combine a framework from one accepted synthesis
            # with a different accepted synthesis (overlapping finding ids would
            # otherwise be trusted).  The framework's own exact synthesis
            # dependency must equal the outline's, canonically.
            framework_synthesis_ref = canonical_json(
                self._ref(self._read(framework_ref, "framework")["input"]["synthesis_ref"])
            )
            require(
                framework_synthesis_ref == canonical_json(synthesis_ref),
                "outline framework and synthesis are from different accepted syntheses",
            )
            return validate_outline(input_payload["content"], framework=framework, synthesis=synthesis)
        raise SynthesisWorkflowError("unknown synthesis proposal kind")

    # -- impact -------------------------------------------------------------- #
    def _dependent_outlines(self, kind, parent_ref):
        """Integrity-checked historical outlines that depend on ``parent_ref``.

        Each stored ``synthesis_outline`` revision is decoded and its exact
        dependency ref is verified.  A detected integrity failure, an
        undecodable document, a malformed payload, or a foreign/invalid
        dependency ref on matching synthesis-outline history **fails closed**
        (a static :class:`SynthesisWorkflowError`), so a corrupt or foreign
        stored outline can never silently erase a recorded impact.  A
        verified-decode document that is not a synthesis outline (a generic
        outline of another document type) is ignored.  The scan is bounded by
        :data:`MAX_GATE_SNAPSHOT_ENTRIES` distinct synthesis-outline revisions.
        """
        link_key = "synthesis_ref" if kind == "synthesis" else "framework_ref"
        target = canonical_json(self._ref(parent_ref))
        found = []
        scanned = 0
        for artifact in self.artifacts.list_artifacts():
            if artifact.kind != "outline":
                continue
            for revision in self.artifacts.list_revisions(artifact.artifact_id):
                stored = self.artifacts.get_revision(artifact.artifact_id, revision.revision_id)
                if stored is None:
                    raise SynthesisWorkflowError("a stored outline revision is missing")
                data = self.artifacts.read_content(artifact.artifact_id, revision.revision_id)
                if (
                    sha256_digest(data) != stored.content_sha256
                    or len(data) != stored.byte_size
                ):
                    raise SynthesisWorkflowError("a stored outline revision fails content integrity")
                try:
                    doc = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise SynthesisWorkflowError("a stored outline revision is not decodable") from None
                if not isinstance(doc, dict) or doc.get("document_type") != "synthesis_outline":
                    # Verified decode of a non-synthesis generic outline: ignored.
                    continue
                scanned += 1
                require(
                    scanned <= MAX_GATE_SNAPSHOT_ENTRIES,
                    "synthesis outline history exceeds the scan bound",
                )
                payload = doc.get("payload")
                if not isinstance(payload, dict) or not isinstance(payload.get("input"), dict):
                    raise SynthesisWorkflowError("a stored synthesis outline has a malformed payload")
                input_payload = payload["input"]
                if link_key not in input_payload:
                    raise SynthesisWorkflowError(
                        "a stored synthesis outline is missing its dependency ref"
                    )
                try:
                    linked = self._ref(input_payload[link_key])
                except RecordValidationError:
                    raise SynthesisWorkflowError(
                        "a stored synthesis outline carries a foreign or invalid dependency ref"
                    ) from None
                if canonical_json(linked) == target:
                    found.append(
                        {
                            "outline_ref": {
                                "project_id": self.store.project_id,
                                "artifact_id": artifact.artifact_id,
                                "revision_id": revision.revision_id,
                            },
                            "result": payload.get("result"),
                        }
                    )
        return found

    @staticmethod
    def _affected_sections(outline_result, changed_concepts, changed_relationships, changed_statements):
        index = (outline_result or {}).get("dependency_index", {})
        affected = {}
        for concept_id in changed_concepts:
            for section_id in index.get("concepts", {}).get(concept_id, []):
                affected.setdefault(section_id, set()).add("concept:" + concept_id)
        for relationship_id in changed_relationships:
            for section_id in index.get("relationships", {}).get(relationship_id, []):
                affected.setdefault(section_id, set()).add("relationship:" + relationship_id)
        for statement_id in changed_statements:
            for section_id in index.get("statements", {}).get(statement_id, []):
                affected.setdefault(section_id, set()).add("statement:" + statement_id)
        return affected

    def _impact(self, kind, parent_ref, old_payload, new_result):
        old_result = old_payload.get("result") or {}
        if kind == "framework":
            changed_concepts = _changed_ids(old_result.get("concepts", []), new_result["concepts"], "concept_id")
            direct_relationships = _changed_ids(
                old_result.get("relationships", []), new_result["relationships"], "relationship_id"
            )
            changed_statements = set()
            old_rels = old_result.get("relationships", [])
            new_rels = new_result["relationships"]
            # A changed concept affects every relationship that names it (old or
            # new), so a section depending only on that relationship is flagged.
            touching = {}
            endpoints = {}
            for rel in list(old_rels) + list(new_rels):
                touching.setdefault(rel["from_concept"], set()).add(rel["relationship_id"])
                touching.setdefault(rel["to_concept"], set()).add(rel["relationship_id"])
                endpoints.setdefault(rel["relationship_id"], set()).update(
                    {rel["from_concept"], rel["to_concept"]}
                )
            changed_relationships = set(direct_relationships)
            for concept_id in changed_concepts:
                changed_relationships |= touching.get(concept_id, set())
            # A changed relationship flags the sections of its concept endpoints.
            for relationship_id in direct_relationships:
                changed_concepts = changed_concepts | endpoints.get(relationship_id, set())
            global_changed = any(
                canonical_json(old_result.get(field)) != canonical_json(new_result.get(field))
                for field in ("title", "contribution", "criteria", "open_questions", "limitations")
            )
        elif kind == "synthesis":
            changed_statements = _changed_ids(
                old_result.get("statements", []), new_result["statements"], "statement_id"
            )
            changed_concepts = set()
            changed_relationships = set()
            global_changed = any(
                canonical_json(old_result.get(field)) != canonical_json(new_result.get(field))
                for field in ("memo", "gaps", "limitations", "independence", "correlation_groups")
            )
        elif kind == "outline":
            changed_sections = _changed_ids(
                old_result.get("sections", []), new_result["sections"], "section_id"
            )
            global_changed = canonical_json(old_result.get("title")) != canonical_json(new_result["title"])
            all_section_ids = [section["section_id"] for section in new_result["sections"]]
            if global_changed:
                affected_sections = {section_id: ["global_change"] for section_id in all_section_ids}
                unaffected_sections = []
            else:
                affected_sections = {section_id: ["section_changed"] for section_id in sorted(changed_sections)}
                unaffected_sections = sorted(set(all_section_ids) - changed_sections)
            return {
                "schema_version": 1,
                "kind": "outline",
                "parent_ref": self._ref(parent_ref),
                "changed": {"sections": sorted(changed_sections)},
                "global_changed": bool(global_changed),
                "dependent_outlines": [],
                "affected_sections": affected_sections,
                "unaffected_sections": unaffected_sections,
                "stale_gates": [GATE_OUTLINE],
                "note": _IMPACT_NOTE,
            }
        else:
            raise SynthesisWorkflowError("impact is only defined for synthesis/framework/outline edits")

        dependent = []
        for entry in self._dependent_outlines(kind, parent_ref):
            all_sections = [
                section["section_id"]
                for section in (entry["result"] or {}).get("sections", [])
            ]
            if global_changed:
                # A global framework/synthesis change conservatively flags every
                # dependent section; nothing is labelled unaffected.
                affected = {section_id: {"global_change"} for section_id in all_sections}
                unaffected = []
            else:
                affected = self._affected_sections(
                    entry["result"], changed_concepts, changed_relationships, changed_statements
                )
                unaffected = sorted(set(all_sections) - set(affected))
            dependent.append(
                {
                    "outline_ref": entry["outline_ref"],
                    "affected_sections": {
                        section_id: sorted(reasons) for section_id, reasons in sorted(affected.items())
                    },
                    "unaffected_sections": unaffected,
                }
            )

        return {
            "schema_version": 1,
            "kind": kind,
            "parent_ref": self._ref(parent_ref),
            "changed": {
                "concepts": sorted(changed_concepts),
                "relationships": sorted(changed_relationships),
                "statements": sorted(changed_statements),
            },
            "global_changed": bool(global_changed),
            "dependent_outlines": dependent,
            "stale_gates": sorted({GATE_FRAMEWORK, GATE_OUTLINE}),
            "note": _IMPACT_NOTE,
        }

    # -- authority reads ----------------------------------------------------- #
    def read(self, ref, kind, *, current=True):
        """Read one document; re-derive its normalized result from durable inputs.

        ``current`` defaults to ``True`` (the exact current revision head).  A
        historical read (``current=False``) is for impact/history inspection, not
        authority.
        """
        payload = self._read(ref, kind, current=current)
        require(self._normalize(kind, payload["input"]) == payload["result"])
        return payload

    def read_impact(self, ref, kind):
        """Return the stored advisory impact report (integrity-checked history)."""
        return self._read(ref, kind, current=False)["impact"]

    def require_synthesis(self, ref):
        """Require one accepted synthesis: normalized, unblocked, and gate-approved."""
        with _NestedStore(self.store).transaction():
            payload = self._read(ref, "synthesis")
            require(self._normalize("synthesis", payload["input"]) == payload["result"])
            # A blocked statement result is refused regardless of any in-band
            # ``status`` value; authority is the named gate only.
            blocked = [s["statement_id"] for s in payload["result"]["statements"] if s["blocked"]]
            require(not blocked, "synthesis has a blocked statement result")
            self._approved(ref, "synthesis")
            return payload["result"]

    def require_framework(self, ref):
        """Require an accepted framework: accepted synthesis, no unclassified, gate."""
        with _NestedStore(self.store).transaction():
            payload = self._read(ref, "framework")
            require(self._normalize("framework", payload["input"]) == payload["result"])
            self.require_synthesis(payload["input"]["synthesis_ref"])
            require(
                not payload["result"]["unclassified_concepts"],
                "framework has unclassified concepts",
            )
            self._approved(ref, "framework")
            return payload["result"]

    def require_outline(self, ref):
        """Require an accepted outline over the same accepted synthesis/framework."""
        with _NestedStore(self.store).transaction():
            payload = self._read(ref, "outline")
            require(self._normalize("outline", payload["input"]) == payload["result"])
            self.require_synthesis(payload["input"]["synthesis_ref"])
            self.require_framework(payload["input"]["framework_ref"])
            self._approved(ref, "outline")
            return payload["result"]

    def require_for_blueprints(self, outline_ref):
        """Return exact approved refs and structured context for future Step 11."""
        with _NestedStore(self.store).transaction():
            outline_ref = self._ref(outline_ref)
            outline_payload = self._read(outline_ref, "outline")
            self.require_outline(outline_ref)
            synthesis_ref = self._ref(outline_payload["input"]["synthesis_ref"])
            framework_ref = self._ref(outline_payload["input"]["framework_ref"])
            synthesis_payload = self._read(synthesis_ref, "synthesis")
            framework_payload = self._read(framework_ref, "framework")
            return {
                "accepted": True,
                "outline_ref": dict(outline_ref),
                "framework_ref": framework_ref,
                "synthesis_ref": synthesis_ref,
                "audit_ref": self._ref(synthesis_payload["input"]["audit_ref"]),
                "outline": outline_payload["result"],
                "framework": framework_payload["result"],
                "synthesis": synthesis_payload["result"],
                "gates_satisfied": [GATE_SYNTHESIS, GATE_FRAMEWORK, GATE_OUTLINE],
                "authority": "named_gate_only",
                "notice": NOTICE,
            }

    # -- writes -------------------------------------------------------------- #
    def propose(self, kind, payload, *, artifact_id, revision_id, rights, parent_revision_id=None):
        """Create an inert proposal, atomically validating its current inputs."""
        if kind not in KINDS:
            raise SynthesisWorkflowError("unknown synthesis proposal kind")
        with self.store.transaction():
            require(isinstance(payload, dict), "proposal payload must be a JSON object")
            if kind == "synthesis":
                require(set(payload) == {"audit_ref", "content"})
            elif kind == "framework":
                require(set(payload) == {"synthesis_ref", "content"})
            else:
                require(set(payload) == {"synthesis_ref", "framework_ref", "content"})
            try:
                result = self._normalize(kind, payload)
            except SynthesisError as exc:
                raise SynthesisWorkflowError(str(exc)) from exc
            except ExtractionWorkflowError:
                raise
            impact = None
            if parent_revision_id is not None:
                parent_ref = {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id,
                    "revision_id": parent_revision_id,
                }
                parent = self._read(parent_ref, kind, current=False)
                impact = self._impact(kind, parent_ref, parent, result)
            refs = self._dependencies(kind, payload)
            unique = {canonical_json(self._ref(r)): r for r in refs}
            deps = [DependencyRef(**ref, role="derived_from") for ref in unique.values()]
            self.artifacts.create_artifact(
                artifact_id, kind=_ARTIFACT_KIND[kind], created_by="user"
            )
            revision = self.artifacts.import_revision(
                artifact_id=artifact_id,
                revision_id=revision_id,
                rights=rights,
                content={
                    "schema_version": 1,
                    "document_type": "synthesis_" + kind,
                    "payload": {"input": payload, "result": result, "impact": impact},
                },
                dependencies=deps,
                origin_locator="synthesis:local",
                acquired_by="user",
                parent_revision_id=parent_revision_id,
            )
        return {
            "project_id": self.store.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }


#: Convenience alias used by callers that prefer the shorter name.
SynthesisRepository = SynthesisWorkflowRepository

__all__ = [
    "KINDS",
    "GATE_SYNTHESIS",
    "GATE_SYNTHESIS_KIND",
    "GATE_FRAMEWORK",
    "GATE_FRAMEWORK_KIND",
    "GATE_OUTLINE",
    "GATE_OUTLINE_KIND",
    "NOTICE",
    "SynthesisRepository",
    "SynthesisWorkflowError",
    "SynthesisWorkflowRepository",
]
