"""Read-only source-revision dependency-impact report (Step 06 slice D1).

Authority
---------

``docs/implementation/06-acquisition.md`` acceptance criterion AC4 ("New evidence
flags affected artifacts without deleting unaffected earlier work");
``docs/specification.md`` workflow 10 and the gates invariant ("material edits
invalidate affected checks"); C01 exact-revision dependencies.

What it does
------------

:func:`source_impact` takes a verified :class:`ProjectStore` and the exact
``{project_id, artifact_id, revision_id}`` of a source artifact revision. If that
revision is the artifact's **current** head and has earlier revisions, it walks
the **actual** generic dependency records through
:meth:`ArtifactRepository.dependents` (never raw tables) to find every *current*
revision with a live (non-``version_of``) use of an **earlier** revision of this
source artifact, then follows live uses of those flagged revisions transitively.
It reports the direct and transitive affected refs, the explicit unaffected refs
and count, and (read-only, informational) the local approval statuses recomputed
by the generic approval freshness logic.

What it is **not**
------------------

* It never writes, deletes, supersedes or invalidates any artifact or approval.
  Approval freshness (``evaluate``) stays the authority; this report only says
  which artifacts to re-check. ``authorizing`` is always ``False``.
* It does not infer scientific relatedness. A brand-new source artifact that no
  recorded dependency links to anything affects nothing: the caller must add a
  dependency or revise the corpus explicitly. No relevance scoring exists here.
* Historical (non-head) revisions are retained and never flagged:
  ``historical_work_retained`` is always ``True``.
* Output holds opaque ids, kinds, roles and statuses only: no source text, titles
  or labels.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..domain.records import RecordValidationError, validate_identifier
from ..storage.approvals import ApprovalRepository
from ..storage.artifacts import ArtifactRepository
from ..domain.errors import StoreError

_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_LINEAGE_ROLE = "version_of"
DEFAULT_MAX_NODES = 1000
_MAX_NODES_CEILING = 100000


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict[str, str]:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


def _key(ref: Mapping[str, str]) -> tuple[str, str]:
    return (ref["artifact_id"], ref["revision_id"])


def _parse_source_ref(source_ref: object, project_id: str) -> tuple[str, str]:
    if not isinstance(source_ref, Mapping):
        raise RecordValidationError("source_ref: must be an exact revision ref mapping")
    for key in source_ref:
        if key not in _REF_KEYS:
            raise RecordValidationError(f"source_ref: unknown field {key!r}")
    for key in _REF_KEYS:
        if key not in source_ref:
            raise RecordValidationError(f"source_ref: missing field {key!r}")
    if source_ref["project_id"] != project_id:
        raise RecordValidationError("source_ref: project_id is not this project")
    artifact_id = validate_identifier(source_ref["artifact_id"], "source_ref.artifact_id")
    revision_id = validate_identifier(source_ref["revision_id"], "source_ref.revision_id")
    return artifact_id, revision_id


def _approval_summaries(approvals: Any, targets: set[str]) -> dict[str, list[dict]]:
    """Read-only recomputed approval statuses grouped by target artifact id."""
    out: dict[str, list[dict]] = {}
    for approval in approvals.list_approvals():
        target = approval.target
        artifact_id = target["artifact_id"]
        if artifact_id not in targets:
            continue
        try:
            status = approvals.evaluate(approval.approval_id).status
        except (StoreError, RecordValidationError):
            status = "unverifiable"
        out.setdefault(artifact_id, []).append(
            {
                "approval_id": approval.approval_id,
                "target_revision_id": target["revision_id"],
                "gate_kind": approval.gate_kind,
                "gate_id": approval.gate_id,
                "status": status,
            }
        )
    return out


def source_impact(
    store: Any,
    source_ref: Mapping[str, str],
    *,
    approvals: Any = None,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    """Return a JSON-safe, read-only impact report for one source revision."""
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not (
        1 <= max_nodes <= _MAX_NODES_CEILING
    ):
        raise RecordValidationError(
            f"max_nodes: must be an integer in 1..{_MAX_NODES_CEILING}"
        )
    repo = ArtifactRepository(store)
    project_id = repo.project_id
    artifact_id, revision_id = _parse_source_ref(source_ref, project_id)
    source = _ref(project_id, artifact_id, revision_id)

    revisions = repo.list_revisions(artifact_id)
    ids = [rev.revision_id for rev in revisions]
    if revision_id not in ids:
        raise RecordValidationError("source_ref: revision does not exist in this project")
    head = revisions[-1]
    current_ref = _ref(project_id, artifact_id, head.revision_id)
    earlier = ids[: ids.index(revision_id)]
    is_current = head.revision_id == revision_id

    if not is_current:
        note = "source_ref is not the current revision; no change is claimed"
    elif not earlier:
        note = "source_ref has no earlier revision; no change is claimed"
    else:
        note = "newer source revision; flagged artifacts need re-check"
    claims_change = is_current and bool(earlier)

    all_artifacts = repo.list_artifacts()
    current_heads: dict[str, str] = {}
    for art in all_artifacts:
        head_rev = repo.current_revision(art.artifact_id)
        if head_rev is not None:
            current_heads[art.artifact_id] = head_rev.revision_id
    kinds = {a.artifact_id: a.kind for a in all_artifacts}

    affected: dict[tuple[str, str], dict[str, Any]] = {}
    if claims_change:
        frontier: list[tuple[str, str]] = []
        visited: set[tuple[str, str]] = set()

        def visit(target: tuple[str, str], relation: str) -> None:
            for edge in repo.dependents(*target):
                if edge["role"] == _LINEAGE_ROLE:
                    continue
                dep = (edge["artifact_id"], edge["revision_id"])
                if dep[0] == artifact_id:
                    continue
                if current_heads.get(dep[0]) != dep[1]:
                    continue  # historical use: retained, never flagged
                entry = affected.get(dep)
                if entry is None:
                    if len(affected) >= max_nodes:
                        raise RecordValidationError(
                            f"impact traversal exceeded max_nodes={max_nodes}"
                        )
                    entry = affected[dep] = {
                        "ref": _ref(project_id, *dep),
                        "kind": kinds.get(dep[0]),
                        "relation": relation,
                        "depends_on": [],
                    }
                    frontier.append(dep)
                via = {**_ref(project_id, *target), "role": edge["role"]}
                if via not in entry["depends_on"]:
                    entry["depends_on"].append(via)

        for old in earlier:
            visit((artifact_id, old), "direct")
        while frontier:
            node = frontier.pop(0)
            if node in visited:
                continue
            visited.add(node)
            visit(node, "transitive")

    affected_list = sorted(
        affected.values(), key=lambda e: (e["relation"] != "direct", _key(e["ref"]))
    )
    if approvals is None:
        approvals = ApprovalRepository(store, repo)
    summaries = _approval_summaries(approvals, {k[0] for k in affected})
    for entry in affected_list:
        entry["approvals"] = summaries.get(entry["ref"]["artifact_id"], [])
        entry["needs_recheck"] = True

    affected_artifacts = {k[0] for k in affected}
    unaffected = [
        _ref(project_id, aid, rid)
        for aid, rid in sorted(current_heads.items())
        if aid != artifact_id and aid not in affected_artifacts
    ]
    return {
        "report": "source_impact",
        "project_id": project_id,
        "changed_source": source,
        "current_source": current_ref,
        "source_is_current": is_current,
        "claims_change": claims_change,
        "earlier_source_revision_ids": list(earlier),
        "note": note,
        "affected": affected_list,
        "affected_count": len(affected_list),
        "unaffected": unaffected,
        "unaffected_count": len(unaffected),
        "historical_work_retained": True,
        "authorizing": False,
        "relatedness_inferred": False,
    }
