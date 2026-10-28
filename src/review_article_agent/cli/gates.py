"""Read-only CLI preview of an exact app-owned human gate (``gate prepare``).

This module adds the ``gate`` command group to the CLI. Its single canonical
subcommand, ``gate prepare``, is a **read-only, non-authorizing presentation** of
the exact human-gate snapshot that
:func:`review_article_agent.runtime.gates.prepare_gate_snapshot` prepares for one
named gate identity. It resolves a **verified** project store, prepares the
immutable snapshot over the target plus its full transitive dependency closure,
and prints a safe JSON summary.

Design rules (``docs/implementation/04-agent-runtime.md`` deliverable 5
("App-owned human approvals presented through CLI") and acceptance criteria 1, 2,
and 4; ``docs/contracts/02-gates-and-runtime.md`` §1.3 (authority provenance is
out-of-band) and §2 (revision-specific approvals over an exact, immutable
dependency snapshot); ``docs/cli.md`` output/exit/secret conventions):

* **This is a preview, not an approval.** It records **no** approval row,
  mints **no** owner-authority capability, and authorizes **no** execution, no
  export, and no scientific validation. Every summary carries
  ``approval_recorded=false``, ``authorizes_execution=false``,
  ``authorizes_export=false`` and ``scientific_validation=false``. There is
  deliberately **no** ``gate approve`` command and **no** raw actor/authority
  flag here; those belong to a later task once the durable approval repository
  is integrated.
* **Exact, app-owned gate identity only.** ``--gate-kind`` and ``--decision``
  accept only the approved C02 enum values; a generic tool permission is never a
  gate kind. An unrecognized value is rejected by ``argparse`` and its raw value
  is **not** echoed.
* **Verified project access.** The store is opened through the registry with
  ``expected_project_id`` (mirroring the other commands), so a project-id/path
  mismatch or traversal is refused before any read.
* **Safe output.** The summary contains only identities, digests, byte sizes, and
  pinned rights *keys/digests* — never source or rights bytes, never a secret,
  never a capability.
* **Typed, curated errors.** A preparation failure is raised as
  :class:`GateControlError` carrying a stable ``category``
  (``validation`` / ``conflict`` / ``storage``) and a static, curated message
  authored here. No raw exception text, store content, or caller-supplied value
  is ever echoed — a caller may have pasted a credential into ``--gate-id``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from ..domain.errors import ReviewAgentError
from ..runtime.gates import (
    GateContentIntegrityError,
    GateDefinitionError,
    GateDependencyError,
    GateRightsError,
    GateSnapshotError,
    GateSupersededInputError,
    GateTargetError,
    PreparedGateDeniedError,
    PreparedGateInvalidError,
    PreparedGateStaleError,
    prepare_gate_snapshot,
)
from ..runtime.owner_auth import APPROVAL_DECISIONS, APPROVAL_GATE_KINDS
from ..storage import ProjectStore, paths

__all__ = [
    "GATE_COMMANDS",
    "GateControlError",
    "build_gates_parser",
    "gate_prepare",
]

#: The three stable error categories this command group can raise. They map onto
#: the CLI's existing exit taxonomy in :mod:`review_article_agent.cli.main`.
_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_GATE_VALIDATION_MESSAGE = (
    "the gate request did not match an exact, approved human gate for this "
    "project (an unknown or unapproved gate kind, a malformed gate id, decision, "
    "or rationale, a non-existent revision, or a malformed target reference). No "
    "snapshot was prepared and no approval was recorded."
)
_GATE_CONFLICT_MESSAGE = (
    "the requested revision is not the current immutable input for this gate (a "
    "stale or superseded reference, or a cross-project identity). No snapshot was "
    "prepared and no approval was recorded."
)
_GATE_STORAGE_MESSAGE = (
    "the project store could not be read to prepare the exact gate snapshot (a "
    "corrupt or missing pinned revision or rights record, or an unsupported "
    "stored schema). No snapshot was prepared and no approval was recorded."
)


class GateControlError(ReviewAgentError):
    """A read-only gate-preview command failed in a typed, curated way.

    ``category`` is one of ``"validation"``, ``"conflict"``, or ``"storage"``;
    the message is always authored in this module (a static, curated constant),
    so it never reflects a raw exception, store content, or a caller-supplied
    value.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _gate_error_category(exc: GateSnapshotError) -> str:
    """Map a typed snapshot failure to a stable CLI error category.

    A stale/superseded or cross-project reference is a ``conflict``; a corrupt or
    missing pinned revision/rights record (or an unsupported stored schema) is a
    ``storage`` failure; a malformed/unknown gate identity or target, or a
    missing revision, is a ``validation`` failure.
    """
    if isinstance(exc, (PreparedGateStaleError, GateSupersededInputError)):
        return _CATEGORY_CONFLICT
    if isinstance(exc, PreparedGateDeniedError):
        return _CATEGORY_CONFLICT
    if isinstance(
        exc, (PreparedGateInvalidError, GateContentIntegrityError, GateRightsError)
    ):
        return _CATEGORY_STORAGE
    if isinstance(exc, (GateDefinitionError, GateTargetError, GateDependencyError)):
        return _CATEGORY_VALIDATION
    return _CATEGORY_STORAGE


def _as_control_error(exc: GateSnapshotError) -> GateControlError:
    """Return a static :class:`GateControlError` for a snapshot failure.

    The underlying exception's own (already curated) message is deliberately
    **not** echoed; a static per-category message is used instead.
    """
    category = _gate_error_category(exc)
    message = {
        _CATEGORY_VALIDATION: _GATE_VALIDATION_MESSAGE,
        _CATEGORY_CONFLICT: _GATE_CONFLICT_MESSAGE,
        _CATEGORY_STORAGE: _GATE_STORAGE_MESSAGE,
    }[category]
    return GateControlError(category, message)


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_gates_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the ``gate`` command group on the top-level ``sub`` parser."""
    gate = sub.add_parser(
        "gate",
        help="read-only exact human-gate preview (prepare; never an approval)",
    )
    gate_sub = gate.add_subparsers(
        dest="gate_command", required=True, metavar="GATE_COMMAND"
    )

    p_prepare = gate_sub.add_parser(
        "prepare",
        help=(
            "read-only prepare (preview) of the exact human-gate snapshot for one "
            "named gate; records no approval and authorizes nothing"
        ),
    )
    p_prepare.add_argument("--artifact-id", required=True, metavar="ID")
    p_prepare.add_argument("--revision-id", required=True, metavar="ID")
    p_prepare.add_argument(
        "--gate-kind", required=True, choices=list(APPROVAL_GATE_KINDS)
    )
    p_prepare.add_argument("--gate-id", required=True, metavar="ID")
    p_prepare.add_argument(
        "--decision", required=True, choices=list(APPROVAL_DECISIONS)
    )
    p_prepare.add_argument("--rationale", required=True, metavar="TEXT")
    p_prepare.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _resolve_project_id(ctx: Any, explicit: str | None) -> str:
    """Return the explicit project ID or the active project's ID."""
    if explicit is not None:
        return explicit
    return ctx.registry.active_project().project_id


def _open_verified_store(ctx: Any, project_id: str) -> ProjectStore:
    """Resolve and *verify* a project's store before reading its data.

    ``Registry.get`` validates only the ID shape (it deliberately does not read
    the store), so the app-owned store is always opened with
    ``expected_project_id`` — a mismatch between the registry location and the
    store's recorded identity is refused before any read.
    """
    info = ctx.registry.get(project_id)
    return ProjectStore.open_at(
        paths.app_dir_for(info.root_path), expected_project_id=info.project_id
    )


def _plain(value: object) -> object:
    """Return a JSON-ready plain copy of a deeply-frozen (proxy) value."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _entry_summary(entry: Any) -> dict[str, object]:
    """A safe projection of one dependency pin (no source/rights bytes)."""
    return {
        "project_id": entry.project_id,
        "artifact_id": entry.artifact_id,
        "revision_id": entry.revision_id,
        "role": entry.role,
        "digest": entry.content_sha256,
        "byte_size": entry.byte_size,
        "rights_id": entry.rights_id,
        "rights_record_revision": entry.rights_record_revision,
        "rights_digest": entry.rights_digest,
    }


def _gate_summary(prepared: Any) -> dict[str, object]:
    """A safe, non-authorizing summary of one prepared human-gate snapshot.

    It carries the exact project/target, the named gate kind/id, the proposed
    decision, the snapshot id/digest, the pinned version set, and every exact
    dependency pin (identity, role, digest, byte size, rights key/digest). It
    never carries source or rights bytes, a secret, or a capability, and it
    explicitly states that nothing was approved or authorized.
    """
    entries = list(prepared.entries)
    return {
        "project_id": prepared.project_id,
        "target": dict(prepared.target),
        "gate_kind": prepared.gate_kind,
        "gate_id": prepared.gate_id,
        "proposed_decision": prepared.decision,
        "snapshot_id": prepared.snapshot_id,
        "snapshot_digest": prepared.digest,
        "snapshot_schema_version": prepared.snapshot_schema_version,
        "storage_schema_version": prepared.storage_schema_version,
        "versions": _plain(prepared.version_set),
        "dependency_count": len(entries),
        "dependencies": [_entry_summary(entry) for entry in entries],
        "approval_recorded": False,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def gate_prepare(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``gate prepare`` — read-only prepare (preview) of an exact human gate.

    Resolves the verified project store, prepares the exact snapshot for the
    named gate, and returns a safe JSON summary. It writes nothing and authorizes
    nothing; a preparation failure is a typed, curated
    :class:`GateControlError`.
    """
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        prepared = prepare_gate_snapshot(
            store,
            {
                "project_id": project_id,
                "artifact_id": args.artifact_id,
                "revision_id": args.revision_id,
            },
            gate_kind=args.gate_kind,
            gate_id=args.gate_id,
            decision=args.decision,
            rationale=args.rationale,
        )
    except GateSnapshotError as exc:
        raise _as_control_error(exc) from exc
    finally:
        store.close()
    return {"gate": _gate_summary(prepared)}


GATE_COMMANDS = {
    "prepare": gate_prepare,
}
