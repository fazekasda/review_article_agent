"""CLI surface for exact, app-owned human gates (``prepare``/``confirm``/``status``).

This module adds the ``gate`` command group to the CLI:

* ``gate prepare`` — a **read-only, non-authorizing presentation** of the exact
  human-gate snapshot that
  :func:`review_article_agent.runtime.gates.prepare_gate_snapshot` prepares for
  one named gate identity. It resolves a **verified** project store, prepares the
  immutable snapshot over the target plus its full transitive dependency closure,
  and prints a safe JSON summary.
* ``gate confirm`` — the **only** CLI path that records a real human approval. It
  re-prepares the exact snapshot, refuses unless the caller-supplied
  ``--snapshot-digest`` equals the freshly prepared snapshot digest (so a
  ``prepare``→``confirm`` sequence cannot silently bless different pins), then
  obtains a real out-of-band owner confirmation on the process's **controlling
  terminal** (``/dev/tty``; never standard input, never a ``--yes``/actor/
  authenticated/capability flag), and finally hands the exact prepared snapshot
  and the minted capability to
  :meth:`review_article_agent.storage.human_approvals.HumanApprovalRepository.record_decision`,
  which revalidates the snapshot **inside one write transaction** before it
  writes anything.
* ``gate status`` / ``gate list`` — read-only views of the computed lifecycle
  status of one or every human approval, carrying a safe identity only.

Design rules (``docs/implementation/04-agent-runtime.md`` deliverable 5
("App-owned human approvals presented through CLI") and acceptance criteria 1, 2,
and 4; ``docs/contracts/02-gates-and-runtime.md`` §1.3 (authority provenance is
out-of-band), §2 (revision-specific approvals over an exact, immutable
dependency snapshot), and §3 (stale/invalidated/superseded); ``docs/cli.md``
output/exit/secret conventions):

* **``prepare`` is a preview, not an approval.** It records **no** approval row,
  mints **no** owner-authority capability, and authorizes **no** execution, no
  export, and no scientific validation. Every summary carries
  ``approval_recorded``, ``authorizes_execution=false``,
  ``authorizes_export=false`` and ``scientific_validation=false``. There is
  deliberately **no** raw actor/authority flag anywhere in this command group.
* **``confirm`` is a local metadata approval, not remote execution.** It records
  one exact named-gate human approval through the integrated repository; it does
  **not** start a worker, dispatch a model/tool call, require a runtime
  activation, or hold the registry execution guard. The owner confirmation is
  bound to the **displayed exact snapshot** on the controlling terminal.
* **Exact, app-owned gate identity only.** ``--gate-kind`` and ``--decision``
  accept only the approved C02 enum values; a generic tool permission is never a
  gate kind. An unrecognized value is rejected by ``argparse`` and its raw value
  is **not** echoed.
* **Verified project access.** The store is opened through the registry with
  ``expected_project_id`` (mirroring the other commands), so a project-id/path
  mismatch or traversal is refused before any read, and it is closed on every
  path.
* **No store transaction is held while waiting on the terminal.** The snapshot is
  prepared read-only, the store is closed, the terminal confirmation is taken,
  and only then is a fresh verified store opened to record the approval.
* **Safe output.** A summary contains only identities, digests, byte sizes, and
  pinned rights *keys/digests* — never source or rights bytes, never a secret,
  never a capability, and never the caller's rationale text.
* **Typed, curated errors.** A failure is raised as :class:`GateControlError`
  carrying a stable ``category`` (``validation`` / ``conflict`` / ``storage``)
  and a static, curated message authored here. No raw exception text, store
  content, or caller-supplied value is ever echoed — a caller may have pasted a
  credential into ``--gate-id``.
"""

from __future__ import annotations

import argparse
import hmac
import re
from collections.abc import Mapping
from typing import Any

from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError
from ..runtime.gates import (
    GateContentIntegrityError,
    GateDefinitionError,
    GateDependencyError,
    GateRightsError,
    GateSnapshotError,
    GateSupersededInputError,
    GateTargetError,
    PreparedGate,
    PreparedGateDeniedError,
    PreparedGateInvalidError,
    PreparedGateStaleError,
    prepare_gate_snapshot,
)
from ..runtime.owner_auth import (
    APPROVAL_DECISIONS,
    APPROVAL_GATE_KINDS,
    OwnerAuthConfigError,
    OwnerAuthError,
    OwnerAuthTtyUnavailable,
    OwnerChallengeError,
    request_owner_confirmation,
)
from ..storage import ProjectStore, paths
from ..storage.human_approvals import (
    HumanApprovalDeniedError,
    HumanApprovalError,
    HumanApprovalRecord,
    HumanApprovalRepository,
    HumanApprovalStatus,
)

__all__ = [
    "GATE_COMMANDS",
    "GateControlError",
    "build_gates_parser",
    "gate_confirm",
    "gate_list",
    "gate_prepare",
    "gate_status",
]

#: A lowercase hex SHA-256 digest (the contract's single digest algorithm).
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

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

# --------------------------------------------------------------------------- #
# ``gate confirm`` — curated, value-free messages (never built from a caller
# value, an exception, or store content).
# --------------------------------------------------------------------------- #
_GATE_DIGEST_FORMAT_MESSAGE = (
    "the expected snapshot digest is not a lowercase hex SHA-256 digest; no "
    "confirmation was requested and no approval was recorded."
)
_GATE_DIGEST_MISMATCH_MESSAGE = (
    "the freshly prepared gate snapshot does not match the expected snapshot "
    "digest you previewed, so the pinned inputs may have changed; no confirmation "
    "was requested and no approval was recorded. Re-run `gate prepare` and "
    "confirm the new digest."
)
_GATE_NOT_CONFIRMED_MESSAGE = (
    "the owner confirmation was not completed on the controlling terminal (the "
    "exact challenge was not entered within the bound, or the confirmation was "
    "cancelled); no approval was recorded."
)
_GATE_TTY_UNAVAILABLE_MESSAGE = (
    "no usable controlling terminal was available for the owner confirmation "
    "(owner confirmation opens /dev/tty and never reads standard input); no "
    "approval was recorded."
)
_GATE_OWNER_AUTH_MESSAGE = (
    "the owner confirmation could not be established on the controlling terminal "
    "(the owner identity is unavailable or elevated, or the terminal could not be "
    "used safely); no approval was recorded."
)
_GATE_OWNER_AUTH_CONFIG_MESSAGE = (
    "the confirmation timeout is out of range or not a positive finite number; no "
    "confirmation was requested and no approval was recorded."
)
_GATE_DENIED_CONFLICT_MESSAGE = (
    "the exact prepared gate could not be recorded as a human approval (the "
    "snapshot is stale or superseded, the pins changed since the preview, or the "
    "owner confirmation was expired, already used, or did not match the exact "
    "action); no approval was recorded. Re-prepare the gate and confirm again."
)
_GATE_DENIED_STORAGE_MESSAGE = (
    "the exact prepared gate could not be revalidated against the project store "
    "(a pinned revision or rights record is missing, corrupt, or unsupported); no "
    "approval was recorded."
)
_GATE_APPROVAL_NOT_FOUND_MESSAGE = (
    "no human approval with that id exists in this project; nothing was read or "
    "changed."
)
_GATE_APPROVAL_READ_MESSAGE = (
    "the stored human-approval record could not be read or revalidated safely; "
    "nothing was changed."
)

#: ``record_decision`` denial reasons (see
#: :func:`review_article_agent.storage.human_approvals._capability_deny_reason`)
#: that represent a conflict with current state rather than a storage/integrity
#: failure: a stale/superseded/cross-project snapshot, or an expired/replayed/
#: mismatched/forged owner confirmation. Everything else is a storage failure.
_CONFIRM_CONFLICT_REASONS = frozenset(
    {
        "prepared_stale",
        "prepared_denied",
        "action_mismatch",
        "approval_expired",
        "approval_replayed",
        "capability_invalid",
        "capability_foreign_process",
        "owner_uid_mismatch",
        "owner_identity",
        "capability_rejected",
    }
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

    p_confirm = gate_sub.add_parser(
        "confirm",
        help=(
            "prepare the exact named gate, confirm it out-of-band on the "
            "controlling terminal, and record one durable human approval"
        ),
    )
    p_confirm.add_argument("--artifact-id", required=True, metavar="ID")
    p_confirm.add_argument("--revision-id", required=True, metavar="ID")
    p_confirm.add_argument(
        "--gate-kind", required=True, choices=list(APPROVAL_GATE_KINDS)
    )
    p_confirm.add_argument("--gate-id", required=True, metavar="ID")
    p_confirm.add_argument(
        "--decision", required=True, choices=list(APPROVAL_DECISIONS)
    )
    p_confirm.add_argument("--rationale", required=True, metavar="TEXT")
    # The exact snapshot digest printed by ``gate prepare``. It is required so a
    # ``prepare``→``confirm`` sequence cannot silently bless different pins; the
    # argument itself is never authority.
    p_confirm.add_argument("--snapshot-digest", required=True, metavar="SHA256")
    # Optional; bounded by the owner-auth validators (never a fixed unvalidated
    # value). Omitting it uses the owner-auth default deadline.
    p_confirm.add_argument("--timeout-seconds", type=float, metavar="SECONDS")
    p_confirm.add_argument("--project", metavar="ID")

    p_status = gate_sub.add_parser(
        "status",
        help="read-only computed lifecycle status of one human approval",
    )
    p_status.add_argument("--approval-id", required=True, metavar="ID")
    p_status.add_argument("--project", metavar="ID")

    p_list = gate_sub.add_parser(
        "list",
        help="read-only list of this project's human approvals and their status",
    )
    p_list.add_argument("--project", metavar="ID")


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


def _gate_summary(prepared: Any, *, approval_recorded: bool = False) -> dict[str, object]:
    """A safe, non-authorizing summary of one prepared human-gate snapshot.

    It carries the exact project/target, the named gate kind/id, the proposed
    decision, the snapshot id/digest, the pinned version set, and every exact
    dependency pin (identity, role, digest, byte size, rights key/digest). It
    never carries source or rights bytes, a secret, a capability, or the caller's
    rationale text, and it explicitly states whether an approval was recorded and
    that nothing was authorized.
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
        "approval_recorded": bool(approval_recorded),
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def _normalize_expected_digest(value: object) -> str:
    """Return the bare lowercase-hex expected snapshot digest, or fail curated.

    A malformed value (or a non-string) is a ``validation`` failure whose message
    is static: the caller's raw value is never echoed.
    """
    if not isinstance(value, str):
        raise GateControlError(_CATEGORY_VALIDATION, _GATE_DIGEST_FORMAT_MESSAGE)
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise GateControlError(_CATEGORY_VALIDATION, _GATE_DIGEST_FORMAT_MESSAGE)
    return text


def _prepare_exact_gate(
    store: ProjectStore,
    project_id: str,
    *,
    artifact_id: object,
    revision_id: object,
    gate_kind: object,
    gate_id: object,
    decision: object,
    rationale: object,
) -> PreparedGate:
    """Prepare one exact gate snapshot, mapping any failure to a curated error."""
    try:
        return prepare_gate_snapshot(
            store,
            {
                "project_id": project_id,
                "artifact_id": artifact_id,
                "revision_id": revision_id,
            },
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale=rationale,
        )
    except GateSnapshotError as exc:
        raise _as_control_error(exc) from exc


def _confirm_denial_category(reason: object) -> str:
    """Map a ``record_decision`` denial reason to a stable CLI error category."""
    if isinstance(reason, str) and reason in _CONFIRM_CONFLICT_REASONS:
        return _CATEGORY_CONFLICT
    return _CATEGORY_STORAGE


def _confirm_denial_message(category: str) -> str:
    return {
        _CATEGORY_CONFLICT: _GATE_DENIED_CONFLICT_MESSAGE,
        _CATEGORY_STORAGE: _GATE_DENIED_STORAGE_MESSAGE,
    }[category]


def _approval_summary(
    record: HumanApprovalRecord, status: HumanApprovalStatus
) -> dict[str, object]:
    """A safe view of one recorded human approval plus its computed status.

    Only identities, the out-of-band ``owner_auth_ref`` pointer, the snapshot
    digest, and the named-gate satisfaction are exposed. The rationale text is
    deliberately **not** echoed (it is caller-authored and bound into the digest
    but never displayed), and ``authorizes_execution``/``authorizes_export``/
    ``scientific_validation`` are always ``False``.
    """
    return {
        "approval_id": record.approval_id,
        "project_id": record.project_id,
        "target": dict(record.target),
        "gate_kind": record.gate_kind,
        "gate_id": record.gate_id,
        "decision": record.decision,
        "snapshot_id": record.snapshot_id,
        "snapshot_digest": record.snapshot_digest,
        "snapshot_schema_version": record.snapshot_schema_version,
        "storage_schema_version": record.storage_schema_version,
        "granted_at_utc": record.granted_at_utc,
        "granted_by": {
            "actor_kind": record.actor_kind,
            "authority": record.authority,
            "owner_auth_ref": record.owner_auth_id,
        },
        "status": status.status,
        "satisfies_named_gate": status.satisfies_named_gate,
        "superseded_by": status.superseded_by,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
    }


def _status_summary(status: HumanApprovalStatus) -> dict[str, object]:
    """A safe, read-only view of one approval's computed lifecycle status.

    ``satisfies_named_gate`` is the *only* satisfaction claim: it is ``True`` iff
    the exact named gate is ``active`` and ``approved``. A historical
    ``stale``/``superseded``/``invalidated`` row is still returned (retained for
    audit) with the flags ``authorizes_execution=false``,
    ``authorizes_export=false`` and ``scientific_validation=false``: no named-gate
    claim — including ``final_release`` — bypasses scientific validation.
    """
    return {
        "approval_id": status.approval_id,
        "status": status.status,
        "decision": status.decision,
        "gate_kind": status.gate_kind,
        "gate_id": status.gate_id,
        "target": dict(status.target),
        "snapshot_digest": status.snapshot_digest,
        "owner_auth_ref": status.owner_auth_ref,
        "superseded_by": status.superseded_by,
        "satisfies_named_gate": status.satisfies_named_gate,
        "authorizes_execution": False,
        "authorizes_export": False,
        "scientific_validation": False,
        "reasons": list(status.reasons),
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
        prepared = _prepare_exact_gate(
            store,
            project_id,
            artifact_id=args.artifact_id,
            revision_id=args.revision_id,
            gate_kind=args.gate_kind,
            gate_id=args.gate_id,
            decision=args.decision,
            rationale=args.rationale,
        )
    finally:
        store.close()
    return {"gate": _gate_summary(prepared)}


def gate_confirm(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``gate confirm`` — record one real, exact human approval.

    Steps, in order:

    1. Prepare the exact snapshot from a **verified** store and close it, so no
       store transaction is held while the terminal is awaited.
    2. Refuse, before any terminal I/O, unless ``--snapshot-digest`` equals the
       freshly prepared snapshot digest (the expected digest is a *check*, never
       authority).
    3. Obtain a real out-of-band owner confirmation on the controlling terminal
       (``request_owner_confirmation`` opens ``/dev/tty``; there is no
       ``--yes``/actor/authenticated/capability flag and standard input is never
       read).
    4. Open a fresh verified store and hand the exact prepared snapshot and the
       minted capability to ``HumanApprovalRepository.record_decision``, which
       revalidates the snapshot **inside one write transaction** and consumes the
       capability once. A stale/superseded/cross-project snapshot, or an
       expired/replayed/mismatched/forged confirmation, denies before any row is
       written.

    It records a local metadata approval only: it starts no worker, dispatches no
    model/tool call, requires no runtime activation, and holds no registry guard.
    """
    project_id = _resolve_project_id(ctx, args.project)
    expected_digest = _normalize_expected_digest(args.snapshot_digest)

    # (1) Read-only prepare; close the store before any terminal wait.
    store = _open_verified_store(ctx, project_id)
    try:
        prepared = _prepare_exact_gate(
            store,
            project_id,
            artifact_id=args.artifact_id,
            revision_id=args.revision_id,
            gate_kind=args.gate_kind,
            gate_id=args.gate_id,
            decision=args.decision,
            rationale=args.rationale,
        )
    finally:
        store.close()

    # (2) Bind the confirmation to the exact previewed snapshot: refuse a
    #     mismatch *before* opening the controlling terminal.
    if not hmac.compare_digest(expected_digest, prepared.digest):
        raise GateControlError(_CATEGORY_CONFLICT, _GATE_DIGEST_MISMATCH_MESSAGE)

    # (3) Real out-of-band confirmation. No store transaction and no registry
    #     guard is held here.
    confirm_kwargs: dict[str, object] = {}
    if args.timeout_seconds is not None:
        confirm_kwargs["timeout_seconds"] = args.timeout_seconds
    try:
        capability = request_owner_confirmation(prepared.owner_action(), **confirm_kwargs)
    except OwnerAuthTtyUnavailable as exc:
        raise GateControlError(
            _CATEGORY_STORAGE, _GATE_TTY_UNAVAILABLE_MESSAGE
        ) from exc
    except OwnerChallengeError as exc:
        raise GateControlError(_CATEGORY_CONFLICT, _GATE_NOT_CONFIRMED_MESSAGE) from exc
    except OwnerAuthConfigError as exc:
        raise GateControlError(
            _CATEGORY_VALIDATION, _GATE_OWNER_AUTH_CONFIG_MESSAGE
        ) from exc
    except OwnerAuthError as exc:
        raise GateControlError(_CATEGORY_STORAGE, _GATE_OWNER_AUTH_MESSAGE) from exc

    # (4) Record durably: revalidate inside one write transaction, consume once.
    store = _open_verified_store(ctx, project_id)
    try:
        approvals = HumanApprovalRepository(store)
        try:
            record = approvals.record_decision(prepared, capability)
            status = approvals.evaluate(record.approval_id)
        except HumanApprovalDeniedError as exc:
            category = _confirm_denial_category(exc.reason)
            raise GateControlError(
                category, _confirm_denial_message(category)
            ) from exc
        except HumanApprovalError as exc:
            raise GateControlError(_CATEGORY_STORAGE, _GATE_STORAGE_MESSAGE) from exc
    finally:
        store.close()

    return {
        "gate": _gate_summary(prepared, approval_recorded=True),
        "approval": _approval_summary(record, status),
    }


def gate_status(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``gate status`` — read-only computed status of one human approval.

    Returns the derived lifecycle status (``active``/``stale``/``invalidated``/
    ``superseded``) and a safe identity of the approval. Historical rows are
    retained and still returned. ``satisfies_named_gate`` is the only satisfaction
    claim; ``authorizes_execution``/``authorizes_export``/
    ``scientific_validation`` are always ``False``.
    """
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        approvals = HumanApprovalRepository(store)
        try:
            status = approvals.evaluate(args.approval_id)
        except RecordValidationError as exc:
            raise GateControlError(
                _CATEGORY_VALIDATION, _GATE_APPROVAL_NOT_FOUND_MESSAGE
            ) from exc
        except HumanApprovalError as exc:
            raise GateControlError(
                _CATEGORY_STORAGE, _GATE_APPROVAL_READ_MESSAGE
            ) from exc
    finally:
        store.close()
    return {"approval": _status_summary(status)}


def gate_list(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``gate list`` — read-only list of a project's human approvals.

    Returns each approval's derived status and safe identity. A corrupt stored row
    fails the whole read closed (``storage``) rather than being silently skipped.
    """
    project_id = _resolve_project_id(ctx, args.project)
    store = _open_verified_store(ctx, project_id)
    try:
        approvals = HumanApprovalRepository(store)
        try:
            records = approvals.list_approvals()
            items = [
                _status_summary(approvals.evaluate(record.approval_id))
                for record in records
            ]
        except HumanApprovalError as exc:
            raise GateControlError(
                _CATEGORY_STORAGE, _GATE_APPROVAL_READ_MESSAGE
            ) from exc
    finally:
        store.close()
    return {
        "project_id": project_id,
        "approval_count": len(items),
        "approvals": items,
    }


GATE_COMMANDS = {
    "prepare": gate_prepare,
    "confirm": gate_confirm,
    "status": gate_status,
    "list": gate_list,
}
