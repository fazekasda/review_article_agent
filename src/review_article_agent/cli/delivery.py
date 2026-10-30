"""Thin local CLI surface for the durable Step 14 delivery workflow.

This module adds the root ``delivery`` command group over the already integrated
:class:`~review_article_agent.delivery.workflow.DeliveryRepository`. It is a
**local, human-gated** surface: it never dispatches a model, creates a session,
approves a gate, or reaches the network.

* ``delivery start --delivery-id ... --section ...`` -- start the global review
  from recorded section-review/trace states (the gate refuses unless every
  section is review- and origin-complete).
* ``delivery freshness --cutoff-date ... --decision ...`` -- record the explicit
  freshness decision (never silently adds papers).
* ``delivery notes add --file ...`` -- store the human-notes action plan.
* ``delivery notes apply --batch-file ... --approval-id ...`` -- apply one
  human-approved notes batch; a non-empty ``--approval-id`` is required and a
  batch is never auto-applied.
* ``delivery revalidate --paragraph ...`` -- re-run the Step 12/13 checks for the
  named paragraphs from the supplied approved package (never trusts the caller).
* ``delivery approve --prepare`` -- **prepare only**: print the exact-revision
  final-approval manifest digest and the ``gate confirm`` command a human must run.
  The delivery final approval cannot yet be recorded through the CLI without new
  owner-authentication wiring, so this command records nothing and reports the gap.
* ``delivery export --delivery-id ... --out-dir ...`` -- build the bibliography
  from the approved drafting packages and write manuscript.md/references.bib/
  references.ris/companion.md/summary.md/ai_declaration.md/export_manifest.json.
  It refuses (writing nothing) on a stale/missing approval and on an out-dir
  inside the application data directory.
* ``delivery status --delivery-id ...`` -- read one delivery's safe status summary.

Privacy/safety rules mirror :mod:`review_article_agent.cli.drafting`: projects are
opened through the registry with ``expected_project_id`` and closed on every path;
side-inputs are read from a bounded regular file; and every failure raises
:class:`DeliveryControlError` with a stable category
(``validation``/``conflict``/``storage``) and a static message that never reflects
a path, id, manuscript text, or exception text.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import Any, Callable

from ..delivery.export import bibliography_from_packages
from ..delivery.schema import final_approval_manifest
from ..delivery.workflow import DeliveryRepository
from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError, canonical_json
from ..review.workflow import SectionReviewRepository
from ..storage import ArtifactConflictError, paths
from ..storage.artifacts import ArtifactRepository
from ..tracing.workflow import OriginTraceRepository, origin_coverage
from .parsing import (
    _read_json_file,
    _read_object,
    _resolve_project_id,
    _verified_store,
)

__all__ = [
    "DeliveryControlError",
    "build_delivery_parser",
    "dispatch_delivery",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: The named gate kind a human would use to authenticate a final release.
_FINAL_RELEASE_GATE_KIND = "final_release"
_FINAL_RELEASE_GATE_ID = "delivery.final_approval"

_START_REJECTED_MESSAGE = (
    "the delivery could not be started (a malformed or out-of-bounds identifier, a "
    "recorded review/trace reference that is not the current approved state, a "
    "malformed rights record, or the global-review gate is not met). Nothing was "
    "written."
)
_NOT_FOUND_MESSAGE = (
    "no delivery with that id has been started in this project; nothing was read "
    "or changed."
)
_REJECTED_MESSAGE = (
    "the delivery request was rejected (a malformed identifier, decision, notes "
    "batch, package, or rights record; or the recorded delivery state does not "
    "satisfy the request). Nothing was written."
)
_EXPORT_REFUSED_MESSAGE = (
    "the export was refused: the delivery is not approved, the approval is stale "
    "for the exact revision and artifact hashes, the bibliography is inconsistent "
    "with the manuscript, or a required input is malformed. Nothing was written."
)
_APPROVE_GAP_MESSAGE = (
    "the delivery final approval cannot be recorded through the CLI yet: it "
    "requires owner authentication (a controlling-terminal confirmation), and the "
    "named-gate ledger is not wired to DeliveryRepository.final_approval. Run "
    "`delivery approve --prepare` to print the manifest digest, then record the "
    "approval through the owner-authenticated `gate confirm` path once that wiring "
    "exists. Nothing was written."
)
_FORBIDDEN_OUTPUT_MESSAGE = (
    "the destination is inside the application data directory or a project's "
    ".review_article_agent app-owned directory; refusing to write there. Choose a "
    "destination outside the --data-dir and any project's .review_article_agent "
    "directory."
)
_OUTPUT_VERIFY_MESSAGE = (
    "could not verify the output location against registered projects; refusing "
    "to write."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."

#: Non-authorizing constants carried on every output.
_FALSE_FLAGS = {
    "authorizes_execution": False,
    "authorizes_export": False,
    "scientific_validation": False,
}


class DeliveryControlError(Exception):
    """A delivery command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_delivery_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``delivery`` command group on the top-level parser."""
    delivery = sub.add_parser(
        "delivery",
        help=(
            "global review, freshness, notes, revalidation, final-approval "
            "preparation, and the manuscript/BibTeX/RIS export (local only)"
        ),
    )
    actions = delivery.add_subparsers(
        dest="delivery_command", required=True, metavar="DELIVERY_COMMAND"
    )

    p_start = actions.add_parser(
        "start", help="start the global review from recorded review/trace states"
    )
    p_start.add_argument("--project", metavar="ID")
    p_start.add_argument("--delivery-id", required=True, metavar="ID")
    p_start.add_argument(
        "--section", action="append", required=True, metavar="ID",
        help="one section id (repeatable; paired with --review-id/--trace-id)",
    )
    p_start.add_argument(
        "--review-id", action="append", required=True, metavar="ID",
        help="the recorded section review for the matching --section (repeatable)",
    )
    p_start.add_argument(
        "--trace-id", action="append", required=True, metavar="ID",
        help="the recorded origin trace for the matching --section (repeatable)",
    )
    p_start.add_argument("--rights-file", required=True, metavar="PATH")

    p_fresh = actions.add_parser(
        "freshness", help="record the explicit freshness decision (never adds papers)"
    )
    p_fresh.add_argument("--project", metavar="ID")
    p_fresh.add_argument("--delivery-id", required=True, metavar="ID")
    p_fresh.add_argument("--cutoff-date", required=True, metavar="DATE")
    p_fresh.add_argument(
        "--decision", required=True, choices=("accept_cutoff", "authorize_topup")
    )
    p_fresh.add_argument("--approval-id", metavar="ID")
    p_fresh.add_argument("--query-window-end", metavar="DATE")
    p_fresh.add_argument("--rights-file", required=True, metavar="PATH")

    p_notes = actions.add_parser("notes", help="plan and apply human notes")
    notes_sub = p_notes.add_subparsers(
        dest="delivery_notes_command", required=True, metavar="NOTES_COMMAND"
    )
    p_notes_add = notes_sub.add_parser(
        "add", help="store the human-notes action plan from a JSON array file"
    )
    p_notes_add.add_argument("--project", metavar="ID")
    p_notes_add.add_argument("--delivery-id", required=True, metavar="ID")
    p_notes_add.add_argument("--file", required=True, metavar="PATH")
    p_notes_add.add_argument("--rights-file", required=True, metavar="PATH")
    p_notes_apply = notes_sub.add_parser(
        "apply",
        help="apply one human-approved notes batch (requires --approval-id)",
    )
    p_notes_apply.add_argument("--project", metavar="ID")
    p_notes_apply.add_argument("--delivery-id", required=True, metavar="ID")
    p_notes_apply.add_argument(
        "--batch-file", required=True, metavar="PATH",
        help='JSON object {"batch_id": ID, "edits": [...]}',
    )
    p_notes_apply.add_argument(
        "--approval-id", required=True, metavar="ID",
        help="the human approval id authorizing this exact batch (never auto-applied)",
    )
    p_notes_apply.add_argument("--rights-file", required=True, metavar="PATH")

    p_reval = actions.add_parser(
        "revalidate", help="re-run the Step 12/13 checks for the named paragraphs"
    )
    p_reval.add_argument("--project", metavar="ID")
    p_reval.add_argument("--delivery-id", required=True, metavar="ID")
    p_reval.add_argument(
        "--paragraph", action="append", required=True, metavar="ID",
        help="one paragraph id (repeatable)",
    )
    p_reval.add_argument("--package-file", required=True, metavar="PATH")
    p_reval.add_argument("--trace-id", metavar="ID")
    p_reval.add_argument("--rights-file", required=True, metavar="PATH")

    p_approve = actions.add_parser(
        "approve",
        help=(
            "prepare the exact-revision final approval manifest digest and the "
            "gate confirm command (records nothing; see --prepare)"
        ),
    )
    p_approve.add_argument("--project", metavar="ID")
    p_approve.add_argument("--delivery-id", required=True, metavar="ID")
    p_approve.add_argument(
        "--prepare",
        action="store_true",
        help="print the manifest digest and the gate confirm command; records nothing",
    )
    p_approve.add_argument("--hashes-file", metavar="PATH")
    p_approve.add_argument("--approver", metavar="LABEL")

    p_export = actions.add_parser(
        "export",
        help=(
            "write manuscript.md/references.bib/references.ris/companion.md/"
            "summary.md/ai_declaration.md/export_manifest.json after final approval"
        ),
    )
    p_export.add_argument("--project", metavar="ID")
    p_export.add_argument("--delivery-id", required=True, metavar="ID")
    p_export.add_argument("--out-dir", required=True, metavar="PATH")
    p_export.add_argument(
        "--package-file", action="append", required=True, metavar="PATH",
        help="an approved drafting package JSON (repeatable)",
    )
    p_export.add_argument("--hashes-file", metavar="PATH")
    p_export.add_argument("--policy-file", metavar="PATH")
    p_export.add_argument("--ai-use-file", metavar="PATH")
    p_export.add_argument("--venue", metavar="NAME")
    p_export.add_argument("--rights-file", required=True, metavar="PATH")

    p_status = actions.add_parser("status", help="read one delivery's status summary")
    p_status.add_argument("--project", metavar="ID")
    p_status.add_argument("--delivery-id", required=True, metavar="ID")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except DeliveryControlError:
        raise
    except ArtifactConflictError:
        raise DeliveryControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise DeliveryControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise DeliveryControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (
        ReviewAgentError,
        RecordValidationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ):
        raise DeliveryControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _rights_factory(template: dict, project_id: str) -> Callable[[str, str], dict]:
    """Rebind a rights-record template to each exact revision the workflow records.

    The delivery workflow assigns its own opaque revision ids, so a static
    ``--rights-file`` cannot pre-bind the subject. The template supplies the
    operations/basis/restrictions/privacy; the subject and ``rights_id`` are
    rebound to the exact ``(artifact_id, revision_id)`` the repository persists.
    """

    def factory(artifact_id: str, revision_id: str) -> dict:
        record = dict(template)
        record["project_id"] = project_id
        record["rights_id"] = f"RIGHTS-{revision_id}"
        record["record_revision"] = 1
        record["subject"] = {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }
        return record

    return factory


def _reject_forbidden_out_dir(ctx: Any, target: str) -> None:
    """Refuse an output directory inside app-owned state (data dir or a store)."""
    real = os.path.realpath(target)
    forbidden: list[str] = [os.path.realpath(ctx.data_dir)]
    try:
        for info in ctx.registry.list_projects():
            forbidden.append(os.path.realpath(paths.app_dir_for(info.root_path)))
    except Exception:  # noqa: BLE001 - a malformed registry must not enable a write
        raise DeliveryControlError(_CATEGORY_STORAGE, _OUTPUT_VERIFY_MESSAGE)
    for root in forbidden:
        if real == root or real.startswith(root + os.sep):
            raise DeliveryControlError(_CATEGORY_STORAGE, _FORBIDDEN_OUTPUT_MESSAGE)


def _delivery_summary(state: dict, *, content: bool) -> dict[str, Any]:
    """A safe structural projection of one delivery state (never raw content)."""
    gr = state.get("global_review") or {}
    approval = state.get("approval") or {}
    summary: dict[str, Any] = {
        "delivery_id": state.get("delivery_id"),
        "status": state.get("status"),
        "section_count": len(state.get("sections") or []),
        "global_review": {
            "status": gr.get("status"),
            "rounds_used": gr.get("rounds_used", 0),
            "reviewer_calls": gr.get("reviewer_calls", 0),
            "finding_count": len(gr.get("findings") or []),
            "required_human_action": gr.get("required_human_action"),
        },
        "freshness_status": (state.get("freshness") or {}).get("status"),
        "notes_planned": state.get("plan_notes") is not None,
        "applied_batch_count": len(state.get("applied_batches") or []),
        "revalidation_required": list(state.get("revalidation_required") or []),
        "approval_present": bool(state.get("approval")),
        "approval_manuscript_revision": approval.get("manuscript_revision"),
        "export_present": state.get("export") is not None,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if content:
        summary["private_content_included"] = True
        summary["payload"] = state
    return summary


def _origin_warnings(state: dict) -> list[dict]:
    """The secondary-origin warnings a final approval must accept (safe shape)."""
    coverage = origin_coverage(state.get("origin_state") or {})
    warnings: list[dict] = []
    for warning in coverage.get("requires_human_approval") or []:
        warnings.append(
            {
                "warning_id": warning.get("claim_id"),
                "claim_id": warning.get("claim_id"),
                "text": warning.get("warning"),
            }
        )
    return warnings


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def delivery_start(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Start the global review from recorded review/trace states."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(_read_object(args.rights_file, what="rights"), project_id)
    sections_args = list(args.section)
    review_ids = list(args.review_id)
    trace_ids = list(args.trace_id)
    if not (len(sections_args) == len(review_ids) == len(trace_ids)):
        raise DeliveryControlError(_CATEGORY_VALIDATION, _REJECTED_MESSAGE)
    with _verified_store(ctx, project_id) as store:
        sections: list[dict] = []
        origin_state: dict = {}
        for section_id, review_id, trace_id in zip(sections_args, review_ids, trace_ids):
            review_state = _guard(
                lambda rid=review_id: SectionReviewRepository(store).read(rid),
                invalid_message=_START_REJECTED_MESSAGE,
            )
            if review_state is None:
                raise DeliveryControlError(_CATEGORY_VALIDATION, _START_REJECTED_MESSAGE)
            trace_state = _guard(
                lambda tid=trace_id: OriginTraceRepository(store).read(tid),
                invalid_message=_START_REJECTED_MESSAGE,
            )
            if trace_state is None:
                raise DeliveryControlError(_CATEGORY_VALIDATION, _START_REJECTED_MESSAGE)
            coverage = origin_coverage(trace_state)
            sections.append(
                {
                    "section_id": section_id,
                    "review_status": review_state.get("status"),
                    "origin_complete": coverage["complete"],
                }
            )
            origin_state = trace_state
        state = _guard(
            lambda: DeliveryRepository(store).start_global_review(
                args.delivery_id, sections, origin_state, rights=rights
            ),
            invalid_message=_START_REJECTED_MESSAGE,
        )
    return _delivery_summary(state, content=False)


def delivery_freshness(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Record the explicit freshness decision (never silently adds papers)."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(_read_object(args.rights_file, what="rights"), project_id)
    decision: dict[str, Any] = {"action": args.decision}
    if args.approval_id is not None:
        decision["approval_id"] = args.approval_id
    if args.query_window_end is not None:
        decision["query_window_end"] = args.query_window_end
    with _verified_store(ctx, project_id) as store:
        state = _guard(
            lambda: DeliveryRepository(store).freshness(
                args.delivery_id, args.cutoff_date, decision, rights=rights
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    return _delivery_summary(state, content=False)


def delivery_notes_add(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Store the human-notes action plan from a JSON array file."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(_read_object(args.rights_file, what="rights"), project_id)
    notes = _read_json_file(args.file, what="notes")
    if not isinstance(notes, list):
        raise DeliveryControlError(_CATEGORY_VALIDATION, _REJECTED_MESSAGE)
    with _verified_store(ctx, project_id) as store:
        state = _guard(
            lambda: DeliveryRepository(store).notes(
                args.delivery_id, notes, rights=rights
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    summary = _delivery_summary(state, content=False)
    summary["plan_item_count"] = len((state.get("plan_notes") or {}).get("items") or [])
    return summary


def delivery_notes_apply(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Apply one human-approved notes batch (a non-empty approval id is required)."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(_read_object(args.rights_file, what="rights"), project_id)
    batch = _read_object(args.batch_file, what="batch")
    batch_id = batch.get("batch_id")
    edits = batch.get("edits")
    if not isinstance(batch_id, str) or not batch_id:
        raise DeliveryControlError(_CATEGORY_VALIDATION, _REJECTED_MESSAGE)
    if edits is None:
        edits = []
    if not isinstance(edits, list):
        raise DeliveryControlError(_CATEGORY_VALIDATION, _REJECTED_MESSAGE)
    with _verified_store(ctx, project_id) as store:
        state = _guard(
            lambda: DeliveryRepository(store).apply_notes(
                args.delivery_id,
                batch_id=batch_id,
                approval_id=args.approval_id,
                edits=edits,
                rights=rights,
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    return _delivery_summary(state, content=False)


def delivery_revalidate(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Re-run the Step 12/13 checks for the named paragraphs."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(_read_object(args.rights_file, what="rights"), project_id)
    package = _read_object(args.package_file, what="package")
    origin_state = None
    if args.trace_id:
        with _verified_store(ctx, project_id) as store:
            origin_state = _guard(
                lambda: OriginTraceRepository(store).read(args.trace_id),
                invalid_message=_REJECTED_MESSAGE,
            )
            if origin_state is None:
                raise DeliveryControlError(_CATEGORY_VALIDATION, _REJECTED_MESSAGE)
    with _verified_store(ctx, project_id) as store:
        state = _guard(
            lambda: DeliveryRepository(store).revalidate(
                args.delivery_id,
                list(args.paragraph),
                package=package,
                origin_state=origin_state,
                rights=rights,
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    summary = _delivery_summary(state, content=False)
    summary["revalidation_record_count"] = len(
        state.get("revalidation_records") or {}
    )
    return summary


def delivery_approve(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Prepare the exact-revision final-approval manifest digest; record nothing."""
    if not args.prepare:
        raise DeliveryControlError(_CATEGORY_VALIDATION, _APPROVE_GAP_MESSAGE)
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = DeliveryRepository(store)
        state = _guard(
            lambda: repo.read(args.delivery_id),
            invalid_message=_NOT_FOUND_MESSAGE,
        )
        if state is None:
            raise DeliveryControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
        head = ArtifactRepository(store).current_revision(args.delivery_id)
        head_revision = None if head is None else head.revision_id
    recorded_hashes = (state.get("approval") or {}).get("artifact_hashes") or {}
    hashes = (
        _read_object(args.hashes_file, what="hashes")
        if args.hashes_file
        else recorded_hashes
    )
    warnings = _origin_warnings(state)
    accepted = [warning.get("warning_id") for warning in warnings]
    approver = args.approver or "owner"
    revision_id = (state.get("manuscript") or {}).get("revision_id")
    manifest = _guard(
        lambda: final_approval_manifest(
            revision_id, hashes, warnings, accepted, [], approver
        ),
        invalid_message=_REJECTED_MESSAGE,
    )
    digest = _canonical_digest(manifest)
    next_command = (
        "review-article-agent gate confirm "
        f"--artifact-id {args.delivery_id} --revision-id {head_revision} "
        f"--gate-kind {_FINAL_RELEASE_GATE_KIND} --gate-id {_FINAL_RELEASE_GATE_ID} "
        "--decision approved --rationale <TEXT> "
        "--snapshot-digest <SHA256_FROM_GATE_PREPARE>"
    )
    return {
        "delivery_id": args.delivery_id,
        "prepare_only": True,
        "manifest_digest": digest,
        "manuscript_revision": revision_id,
        "warning_count": len(warnings),
        "approver": approver,
        "next_command": next_command,
        "human_authenticated": False,
        "approval_recorded": False,
        "gap": (
            "no CLI path records the delivery final approval yet; it needs owner "
            "authentication and a named-gate-to-DeliveryRepository.final_approval "
            "wiring. Nothing was written."
        ),
        **_FALSE_FLAGS,
    }


def delivery_export(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write the export package after the exact-revision final approval."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(_read_object(args.rights_file, what="rights"), project_id)
    packages = [_read_object(path, what="package") for path in args.package_file]
    references, corpus = _guard(
        lambda: bibliography_from_packages(packages),
        invalid_message=_EXPORT_REFUSED_MESSAGE,
    )
    policy = (
        _read_object(args.policy_file, what="policy")
        if args.policy_file
        else {"companion_accepted": False, "journal_policy_verified": False}
    )
    ai_use = (
        _read_json_file(args.ai_use_file, what="ai_use")
        if args.ai_use_file
        else []
    )
    if not isinstance(ai_use, list):
        raise DeliveryControlError(_CATEGORY_VALIDATION, _EXPORT_REFUSED_MESSAGE)
    out_dir = os.path.abspath(args.out_dir)
    _reject_forbidden_out_dir(ctx, out_dir)
    with _verified_store(ctx, project_id) as store:
        repo = DeliveryRepository(store)
        state = _guard(
            lambda: repo.read(args.delivery_id),
            invalid_message=_NOT_FOUND_MESSAGE,
        )
        if state is None:
            raise DeliveryControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
        manuscript = state.get("manuscript")
        if not isinstance(manuscript, dict) or manuscript.get("revision_id") is None:
            raise DeliveryControlError(_CATEGORY_VALIDATION, _EXPORT_REFUSED_MESSAGE)
        current_hashes = (
            _read_object(args.hashes_file, what="hashes")
            if args.hashes_file
            else (state.get("approval") or {}).get("artifact_hashes") or {}
        )
        record = _guard(
            lambda: repo.export(
                args.delivery_id,
                manuscript=manuscript,
                references=references,
                corpus=corpus,
                current_hashes=current_hashes,
                policy=policy,
                ai_use=ai_use,
                out_dir=out_dir,
                venue=args.venue,
                rights=rights,
            ),
            invalid_message=_EXPORT_REFUSED_MESSAGE,
        )
    return {
        "delivery_id": args.delivery_id,
        "status": "exported",
        "out_dir": out_dir,
        "file_hashes": dict(record["file_hashes"]),
        "consistency_ok": bool(record["checks"]["consistency"]["ok"]),
        "manuscript_revision": record.get("manuscript_revision"),
        **_FALSE_FLAGS,
    }


def delivery_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one delivery's safe status summary."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        state = _guard(
            lambda: DeliveryRepository(store).read(args.delivery_id),
            invalid_message=_NOT_FOUND_MESSAGE,
        )
    if state is None:
        raise DeliveryControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
    return _delivery_summary(state, content=False)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
DELIVERY_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "start": delivery_start,
    "freshness": delivery_freshness,
    "notes": lambda args, ctx: (
        delivery_notes_add(args, ctx)
        if getattr(args, "delivery_notes_command", None) == "add"
        else delivery_notes_apply(args, ctx)
    ),
    "revalidate": delivery_revalidate,
    "approve": delivery_approve,
    "export": delivery_export,
    "status": delivery_status,
}


def dispatch_delivery(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``delivery`` invocation; ``None`` if unknown."""
    handler = DELIVERY_COMMANDS.get(getattr(args, "delivery_command", None))
    return None if handler is None else handler(args, ctx)
