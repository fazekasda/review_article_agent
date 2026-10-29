"""Acquisition manifest, disposition, queue and require commands (Step 06, C2).

An addon to :mod:`review_article_agent.cli.acquisition` (the ``acquisition``
group). It owns only the **CLI surface** over the integrated
:class:`~review_article_agent.acquisition.manifest.AcquisitionRepository`:

* ``acquisition manifest propose`` -- record one **inert** manifest proposal over
  the exact frozen roster. Every disposition (``use_full_text`` / ``exclude`` /
  ``defer`` / ``no_scientific_use``), rationale, source/asset pin, human review
  and integrity review is explicit caller input inside the ``--entries-file``;
  there is no actor, ``--approved`` or ``--confirm`` shortcut. The result carries
  the exact refs/digest, blocker counts/codes and a non-authorizing
  ``approval_request`` for the named gate
  ``artifact_revision`` / ``acquisition.complete`` with the existing
  ``gate prepare`` / ``gate confirm`` guidance. No approval is recorded here.
* ``acquisition manifest show|status|history`` -- safe summaries (ids, counts,
  status, static codes); ``--content`` (``show`` only) is the explicit private
  opt-in. No rationale, terms, identifiers, source text or path is echoed by
  default.
* ``acquisition manifest require`` -- always calls
  :meth:`AcquisitionRepository.require_acquired`; a generic approval of the
  manifest artifact can never stand in for the exact named gate.
* ``acquisition queue`` -- the read-only missing / full-text / visual /
  supplement blocker queue for an exact frozen roster, even before any manifest
  exists.

Errors reuse :class:`~review_article_agent.cli.acquisition.AcquisitionControlError`
(static, value-free messages; ``validation`` / ``conflict`` / ``storage``). The
project is opened through the registry with its expected id and closed on every
path.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..acquisition import manifest as _manifest
from ..acquisition.manifest import (
    ACQUISITION_GATE_ID,
    ACQUISITION_GATE_KIND,
    DEFAULT_MANIFEST_ARTIFACT_ID,
    USE_DISPOSITION,
    AcquisitionRepository,
    ManifestConflictError,
    ManifestCorruptionError,
    ManifestError,
    ManifestNotAcquiredError,
    ManifestValidationError,
)
from ..domain.errors import StoreError
from ..domain.records import RecordValidationError
from ..storage import ArtifactRepository
from .acquisition import (
    AcquisitionControlError,
    _CATEGORY_CONFLICT,
    _CATEGORY_STORAGE,
    _CATEGORY_VALIDATION,
    _read_json_file,
    _read_rights,
    _resolve_project_id,
    _verified_store,
)

__all__ = ["build_manifest_parsers", "dispatch_manifest"]

#: Static, value-free copies of the manifest module's validation messages.
_VALIDATION_MESSAGES = {
    getattr(_manifest, name): getattr(_manifest, name)
    for name in (
        "_M_BAD_REQUEST_ID",
        "_M_BAD_ARTIFACT_ID",
        "_M_BAD_REF",
        "_M_NOT_FOUND",
        "_M_BAD_FREEZE",
        "_M_BAD_ENTRIES",
        "_M_ROSTER",
        "_M_BAD_ENTRY",
        "_M_BAD_SOURCE",
        "_M_BAD_INTEGRITY",
        "_M_BAD_INPUT",
        "_M_STATE_MOVED",
    )
}
_VALIDATION_FALLBACK = (
    "the manifest request was rejected (malformed input, a freeze that is not "
    "currently authentic, or an unusable source or asset). Nothing was written."
)
_CONFLICT_MESSAGE = (
    "a different manifest is already recorded for this request key; nothing was "
    "overwritten."
)
_CORRUPT_MESSAGE = (
    "a stored manifest, or one of its pinned inputs, failed an integrity check; "
    "nothing was changed."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."
_NOT_ACQUIRED_MESSAGE = (
    "no currently active authentic human-approved acquisition manifest makes "
    "acquisition complete; the exact named gate artifact_revision/"
    "acquisition.complete must approve the exact manifest revision out of band on "
    "the controlling terminal. Reasons: {reasons}."
)
_ENTRIES_SHAPE_MESSAGE = (
    "the entries input must be a JSON list of manifest entries. Nothing was written."
)
_NOT_FOUND_MESSAGE = (
    "no manifest revision exists for that reference in this project; nothing was "
    "read or changed."
)

#: Reason codes ``require_acquired`` may attach (the only text copied from it).
_REASON_CODES = frozenset(
    {
        "no_manifest",
        "manifest_invalid",
        "gate_missing",
        "gate_not_active",
        "freeze_not_authentic",
        "roster_mismatch",
        "blockers_present",
        "source_superseded",
    }
)

_SNAPSHOT_NOTE = (
    "An owner records the exact named gate out of band on the controlling terminal "
    "with `gate prepare`/`gate confirm` for this exact manifest artifact/revision "
    "and gate. This CLI command records no approval and introduces no parallel "
    "approval path. A manifest with blockers cannot make acquisition complete "
    "even if gated; resolve them (see `acquisition queue`) and propose a new "
    "revision."
)


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_manifest_parsers(acq_sub: "argparse._SubParsersAction") -> None:
    """Register ``manifest`` and ``queue`` on the ``acquisition`` subparsers."""
    manifest = acq_sub.add_parser(
        "manifest",
        help=(
            "revision-specific acquisition manifest over the frozen roster "
            "(propose/show/status/history/require); records an inert proposal only"
        ),
    )
    msub = manifest.add_subparsers(
        dest="acquisition_manifest_command", required=True, metavar="MANIFEST_COMMAND"
    )

    p_propose = msub.add_parser(
        "propose",
        help=(
            "record one inert manifest proposal; every disposition is explicit in "
            "--entries-file and no approval is recorded"
        ),
    )
    p_propose.add_argument("--freeze-artifact-id", required=True, metavar="ID")
    p_propose.add_argument("--freeze-revision-id", required=True, metavar="ID")
    p_propose.add_argument(
        "--entries-file",
        required=True,
        metavar="PATH",
        help=(
            "strict JSON list with exactly one entry per frozen candidate "
            "(candidate_id, disposition, rationale, source_ref, human_review, "
            "required_assets, integrity_review)"
        ),
    )
    p_propose.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for this exact manifest revision",
    )
    p_propose.add_argument("--revision-id", required=True, metavar="ID")
    p_propose.add_argument("--request-id", required=True, metavar="ID")
    p_propose.add_argument(
        "--artifact-id", default=DEFAULT_MANIFEST_ARTIFACT_ID, metavar="ID"
    )
    p_propose.add_argument("--project", metavar="ID")

    p_show = msub.add_parser(
        "show", help="read one manifest (safe summary; --content to opt in)"
    )
    p_show.add_argument(
        "--artifact-id", default=DEFAULT_MANIFEST_ARTIFACT_ID, metavar="ID"
    )
    p_show.add_argument(
        "--revision-id", metavar="ID", help="exact revision (default: current)"
    )
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly return the private validated manifest document",
    )
    p_show.add_argument("--project", metavar="ID")

    p_status = msub.add_parser(
        "status", help="current manifest, its gate state and whether acquisition holds"
    )
    p_status.add_argument(
        "--artifact-id", default=DEFAULT_MANIFEST_ARTIFACT_ID, metavar="ID"
    )
    p_status.add_argument("--project", metavar="ID")

    p_history = msub.add_parser(
        "history", help="every manifest revision, newest first (safe summaries)"
    )
    p_history.add_argument(
        "--artifact-id", default=DEFAULT_MANIFEST_ARTIFACT_ID, metavar="ID"
    )
    p_history.add_argument("--project", metavar="ID")

    p_require = msub.add_parser(
        "require",
        help=(
            "succeed only while the exact named gate currently makes acquisition "
            "complete (acquisition prerequisite only)"
        ),
    )
    p_require.add_argument(
        "--artifact-id", default=DEFAULT_MANIFEST_ARTIFACT_ID, metavar="ID"
    )
    p_require.add_argument(
        "--revision-id", metavar="ID", help="exact revision (default: current)"
    )
    p_require.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private manifest document and acquired ids",
    )
    p_require.add_argument("--project", metavar="ID")

    p_queue = acq_sub.add_parser(
        "queue",
        help=(
            "read-only missing / full-text / visual / supplement blocker queue for "
            "an exact frozen roster (works before any manifest exists)"
        ),
    )
    p_queue.add_argument("--freeze-artifact-id", required=True, metavar="ID")
    p_queue.add_argument("--freeze-revision-id", required=True, metavar="ID")
    p_queue.add_argument(
        "--artifact-id", default=DEFAULT_MANIFEST_ARTIFACT_ID, metavar="ID"
    )
    p_queue.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any]) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except AcquisitionControlError:
        raise
    except ManifestNotAcquiredError as exc:
        reasons = sorted(r for r in exc.reasons if r in _REASON_CODES)
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION,
            _NOT_ACQUIRED_MESSAGE.format(reasons=", ".join(reasons) or "unspecified"),
        ) from None
    except ManifestConflictError:
        raise AcquisitionControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except ManifestCorruptionError:
        raise AcquisitionControlError(_CATEGORY_STORAGE, _CORRUPT_MESSAGE) from None
    except ManifestValidationError as exc:
        message = _VALIDATION_MESSAGES.get(str(exc), _VALIDATION_FALLBACK)
        raise AcquisitionControlError(_CATEGORY_VALIDATION, message) from None
    except RecordValidationError:
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _VALIDATION_FALLBACK
        ) from None
    except (ManifestError, StoreError):
        raise AcquisitionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item[key]] = counts.get(item[key], 0) + 1
    return dict(sorted(counts.items()))


def _blocker_summary(blockers: list[dict]) -> dict[str, Any]:
    return {
        "blocker_count": len(blockers),
        "blockers_by_category": _count_by(blockers, "category"),
        "blockers_by_code": _count_by(blockers, "code"),
    }


def _entry_summary(entry: dict) -> dict[str, Any]:
    """Opaque ids, dispositions and static statuses: no rationale or identifiers."""
    validation = entry.get("validation")
    return {
        "candidate_id": entry["candidate_id"],
        "disposition": entry["disposition"],
        "source_ref": entry["source_ref"],
        "validation_status": (
            validation.get("status") if isinstance(validation, dict) else None
        ),
        "validation_reasons": (
            list(validation.get("reasons", [])) if isinstance(validation, dict) else []
        ),
        "human_review_present": entry["human_review"] is not None,
        "integrity_review_present": entry["integrity_review"] is not None,
        "required_assets": [
            {
                "asset_id": a["asset_id"],
                "kind": a["kind"],
                "required": a["required"],
                "status": a["status"],
                "source_ref": a["source_ref"],
            }
            for a in entry["required_assets"]
        ],
    }


def _manifest_summary(document: dict, *, entries: bool = False) -> dict[str, Any]:
    payload = document["payload"]
    entry_list = payload["entries"]
    summary: dict[str, Any] = {
        "manifest_ref": {
            "project_id": document["project_id"],
            "artifact_id": document["artifact_id"],
            "revision_id": document["revision_id"],
        },
        "content_sha256": document["content_sha256"],
        "byte_size": document["byte_size"],
        "acquired_at_utc": document["acquired_at_utc"],
        "freeze_ref": dict(payload["freeze"]["ref"]),
        "freeze_content_sha256": payload["freeze"]["content_sha256"],
        "entry_count": len(entry_list),
        "dispositions": _count_by(entry_list, "disposition"),
        **_blocker_summary(payload["blockers"]),
        "approval": document.get("approval"),
        "authorizing": False,
        "private_content_included": False,
    }
    if entries:
        summary["entries"] = [_entry_summary(e) for e in entry_list]
    return summary


def _approval_request(
    store: Any, document: dict, *, project: str | None
) -> dict[str, Any]:
    """Non-authorizing named-gate request for one manifest revision.

    ``satisfied`` is only true when this exact revision is still the artifact's
    head and carries a current, active approval at the exact named gate.
    """
    artifact_id, revision_id = document["artifact_id"], document["revision_id"]
    head = ArtifactRepository(store).current_revision(artifact_id)
    is_current = head is not None and head.revision_id == revision_id
    approval = document.get("approval")
    satisfied = bool(
        is_current and approval is not None and approval["satisfies_named_gate"]
    )
    scope = f" --project {project}" if project else ""
    base = (
        f"--artifact-id {artifact_id} --revision-id {revision_id} "
        f"--gate-kind {ACQUISITION_GATE_KIND} --gate-id {ACQUISITION_GATE_ID} "
        f"--decision approved --rationale TEXT{scope}"
    )
    return {
        "gate_kind": ACQUISITION_GATE_KIND,
        "gate_id": ACQUISITION_GATE_ID,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
        "content_sha256": document["content_sha256"],
        "satisfied": satisfied,
        "current": bool(is_current),
        "stale": not is_current,
        "blocker_count": len(document["payload"]["blockers"]),
        "approval": approval,
        "authorizes_scientific_processing": False,
        "snapshot_preparation": {
            "gate_kind": ACQUISITION_GATE_KIND,
            "gate_id": ACQUISITION_GATE_ID,
            "prepare_command": f"gate prepare {base}",
            "confirm_command": f"gate confirm {base} --snapshot-digest SHA256",
            "binds": "exact artifact/revision/content digest and pinned dependencies",
            "note": _SNAPSHOT_NOTE,
        },
    }


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict[str, str]:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def manifest_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Record one inert manifest proposal; never records an approval."""
    entries = _read_json_file(args.entries_file, what="entries")
    if not isinstance(entries, list):
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _ENTRIES_SHAPE_MESSAGE)
    rights = _read_rights(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    freeze_ref = _ref(project_id, args.freeze_artifact_id, args.freeze_revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = AcquisitionRepository(store)
        existing = ArtifactRepository(store).get_revision(
            args.artifact_id, args.revision_id
        )

        def run() -> dict[str, Any]:
            revision = repo.propose(
                freeze_ref,
                entries=entries,
                rights=rights,
                request_id=args.request_id,
                revision_id=args.revision_id,
                artifact_id=args.artifact_id,
            )
            document = repo.get(
                _ref(project_id, revision.artifact_id, revision.revision_id)
            )
            return {
                "manifest": _manifest_summary(document),
                "approval_request": _approval_request(
                    store, document, project=args.project
                ),
                "replayed": existing is not None
                or revision.revision_id != args.revision_id,
            }

        result = _guard(run)
    result.update(
        {"proposal_only": True, "authorizes_scientific_processing": False}
    )
    return result


def _read_manifest(repo: AcquisitionRepository, project_id: str, args: Any) -> dict:
    if args.revision_id is not None:
        return repo.get(_ref(project_id, args.artifact_id, args.revision_id))
    document = repo.current(args.artifact_id)
    if document is None:
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
    return document


def manifest_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one manifest; safe summary unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AcquisitionRepository(store)
        document = _guard(lambda: _read_manifest(repo, project_id, args))
    result: dict[str, Any] = {
        "manifest": _manifest_summary(document, entries=not args.content),
        "authorizes_scientific_processing": False,
    }
    if args.content:
        result["manifest"]["private_content_included"] = True
        result["document"] = document
    return result


def manifest_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Head manifest, its gate state and whether acquisition currently holds."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AcquisitionRepository(store)

        def run() -> dict[str, Any]:
            current = repo.current(args.artifact_id)
            history = repo.history(args.artifact_id)
            acquired, reasons = False, []
            if current is not None:
                try:
                    repo.require_acquired(args.artifact_id)
                    acquired = True
                except ManifestNotAcquiredError as exc:
                    reasons = sorted(r for r in exc.reasons if r in _REASON_CODES)
            return {
                "current_manifest": (
                    _manifest_summary(current) if current is not None else None
                ),
                "manifest_count": len(history),
                "acquired": acquired,
                "not_acquired_reasons": reasons,
            }

        result = _guard(run)
    result["authorizes_scientific_processing"] = False
    return result


def manifest_history(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Every manifest revision newest-first (safe summaries)."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AcquisitionRepository(store)
        documents = _guard(lambda: repo.history(args.artifact_id))
    return {
        "manifests": [_manifest_summary(d) for d in documents],
        "count": len(documents),
        "authorizes_scientific_processing": False,
    }


def manifest_require(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Succeed only while acquisition is currently complete.

    ``require_acquired`` is consulted on **both** output paths; a generic
    approval of the artifact cannot satisfy it.
    """
    project_id = _resolve_project_id(ctx, args.project)
    at_ref = (
        _ref(project_id, args.artifact_id, args.revision_id)
        if args.revision_id is not None
        else None
    )
    with _verified_store(ctx, project_id) as store:
        repo = AcquisitionRepository(store)
        document = _guard(lambda: repo.require_acquired(args.artifact_id, at_ref=at_ref))
    entries = document["payload"]["entries"]
    acquired_ids = list(document["acquired_candidate_ids"])
    result: dict[str, Any] = {
        "acquired": True,
        "manifest": _manifest_summary(document),
        "acquired_count": len(acquired_ids),
        "nonuse_count": len(entries) - len(acquired_ids),
        "use_disposition": USE_DISPOSITION,
        "authorizes_scientific_processing": False,
    }
    if args.content:
        result["manifest"]["private_content_included"] = True
        result["private"] = True
        result["document"] = document
        result["acquired_candidate_ids"] = acquired_ids
    return result


def acquisition_queue(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read-only blocker queue for an exact frozen roster."""
    project_id = _resolve_project_id(ctx, args.project)
    freeze_ref = _ref(project_id, args.freeze_artifact_id, args.freeze_revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = AcquisitionRepository(store)
        items = _guard(lambda: repo.queue(freeze_ref, artifact_id=args.artifact_id))
    return {
        "freeze_ref": freeze_ref,
        "items": items,
        "count": len(items),
        "by_category": _count_by(items, "category"),
        "by_code": _count_by(items, "code"),
        "empty_queue_is_not_approval": True,
        "authorizes_scientific_processing": False,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
_MANIFEST_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": manifest_propose,
    "show": manifest_show,
    "status": manifest_status,
    "history": manifest_history,
    "require": manifest_require,
}


def dispatch_manifest(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route ``acquisition manifest ...`` / ``acquisition queue``; ``None`` if unknown."""
    if args.acquisition_command == "queue":
        return acquisition_queue(args, ctx)
    handler = _MANIFEST_COMMANDS.get(
        getattr(args, "acquisition_manifest_command", None)
    )
    if handler is None:
        return None
    return handler(args, ctx)
