"""Deterministic, durable **synthetic** external-admission backend.

This module is a Step 03 (``docs/implementation/03-durable-worker.md``)
worker-test double. It lets restart/recovery tests exercise the *shape* of a
durable external provider — durable admission that is **separate from
completion**, a stable dispatch identity, and recovery after external admission
and before a local result commit — **without any real OpenCode session, network,
model, or service call**. It makes **no exactly-once external billing claim**
(C02 §8.1/§8.2/§8.3).

What it models
--------------

A caller submits a *dispatch* under a stable, caller-supplied identity
(``dispatch_id`` + ``project_id``) plus a couple of bounded numeric synthetic
parameters (``steps`` 1..1000, ``delay_ms`` 0..60000). The backend writes a
durable record into its **own** provider journal
(``<data_dir>/runtime/synthetic.sqlite3``) that captures:

* the observed admission time and the computed **ready-at** time, so a fresh
  process can decide the outcome later from the persisted timestamp alone — the
  original worker need not stay alive;
* a deterministic *synthetic* result (a numeric value plus a synthetic marker),
  so a ready terminal is reproducible across restarts; and
* the intended synthetic mode (``complete``/``failure``/``ambiguous``).

:meth:`SyntheticBackend.lookup` then materializes the outcome read-only:

* before ``ready_at`` → ``admitted`` (a durable admission, **never** a
  completion);
* at/after ``ready_at`` with mode ``complete`` → ``completed`` (with the
  deterministic synthetic result);
* at/after ``ready_at`` with mode ``failure`` → ``failed`` (an **explicit**
  definitive, non-billing failure — a synthetic provider rejection that incurred
  no external cost);
* at/after ``ready_at`` with mode ``ambiguous`` → ``unknown`` with
  ``outcome_unresolved = true``: this mode **intentionally cannot prove a
  terminal or quiescent outcome**, so the engine must escalate and must **not**
  re-dispatch under a new identity.

This is **not** a scientific result and **not** an external side-effect fence.
The backend never claims to have fenced arbitrary external side effects that
already occurred; it only simulates durable admission over its own local journal.

Safety
------

* The journal is a **separate** SQLite database owned by this backend — never the
  project store — because it represents *external* state.
* The runtime directory and journal file are created **owner-private** (``0o700``
  / ``0o600``); a symlinked or group/world-accessible journal is refused.
* A marker table plus ``PRAGMA user_version`` are checked **read-only before any
  write/migration**. A foreign file, a newer schema, or a non-SQLite file is
  refused and left **byte-identical**.
* Inputs are ids and bounded integers only; there is **no** file/shell/code/
  string-document payload, and none is executed.

Identity continuity
-------------------

Absence of a journal may only be read as "definitively absent" when nothing could
have been admitted. If a journal was previously observed (in this process) or the
caller knows the journal's ``continuity_id`` (``expected_continuity_id``), then a
lost/foreign journal makes :meth:`SyntheticBackend.lookup` return ``unknown``
rather than ``absent`` — the absence is **not** proof, and the engine should
escalate. :meth:`SyntheticBackend.same_identity_dispatch_allowed` is ``True`` only
when the lookup is **definitively** ``absent``.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import quote

from ..domain.errors import (
    ReviewAgentError,
    StoreError,
    UnsupportedSchemaVersion,
)
from ..storage import paths, schema
from ..storage.store import transaction

__all__ = [
    "AdmissionReceipt",
    "DISPATCH_ID_PATTERN",
    "DISPATCH_MODES",
    "JOURNAL_FILENAME",
    "MAX_DELAY_MS",
    "MAX_STEPS",
    "MIN_DELAY_MS",
    "MIN_STEPS",
    "RUNTIME_DIR_NAME",
    "SyntheticBackend",
    "SyntheticBackendError",
    "SyntheticConflict",
    "SyntheticContinuityError",
    "SyntheticInputError",
    "SyntheticResult",
    "SYNTHETIC_SCHEMA_VERSION",
    "LookupResult",
]

# --------------------------------------------------------------------------- #
# Layout and bounds
# --------------------------------------------------------------------------- #
#: App-owned subdirectory (inside the caller's data directory) holding the
#: backend journal. Mirrors the "runtime" side of the app-owned layout.
RUNTIME_DIR_NAME = "runtime"

#: Journal filename inside the runtime directory.
JOURNAL_FILENAME = "synthetic.sqlite3"

#: Schema version of the synthetic provider journal (its own, independent of the
#: project store and the application registry).
SYNTHETIC_SCHEMA_VERSION = 1

#: Marker table/value proving the journal belongs to this backend before any
#: write. Checked read-only together with ``PRAGMA user_version``.
MARKER_TABLE = "synthetic_marker"
MARKER_VALUE = "review_article_agent.synthetic_backend.v1"

#: Bounded synthetic parameter ranges (no document/payload parameters exist).
MIN_STEPS = 1
MAX_STEPS = 1000
MIN_DELAY_MS = 0
MAX_DELAY_MS = 60_000

#: The only synthetic dispatch modes. ``complete`` and ``failure`` are terminal;
#: ``ambiguous`` deliberately cannot prove a terminal/quiescent outcome.
DISPATCH_MODES = ("complete", "failure", "ambiguous")

#: Route label: this is a synthetic local double, never a live provider.
_SYNTHETIC_ROUTE = "synthetic_provider"

#: The synthetic marker attached to a materialized terminal result. It is not a
#: scientific result and it records no real external effect.
RESULT_MARKER = "synthetic:no-external-effect:not-a-scientific-result"

#: Exact, known-zero actual external cost (synthetic only). Stored as a decimal
#: string, never a float, and never anything but zero here.
ACTUAL_EXTERNAL_COST_USD_ZERO = "0"

#: Safe, bounded dispatch-id shape: no path separators or payload characters.
DISPATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SyntheticBackendError(ReviewAgentError):
    """Base class for synthetic-backend errors (not a storage/OS failure)."""


class SyntheticInputError(SyntheticBackendError, ValueError):
    """A caller-supplied id or bounded synthetic parameter was out of bounds."""


class SyntheticConflict(SyntheticBackendError):
    """A divergent ``admit`` reused a dispatch identity already recorded.

    Repeated admission of the *identical* request is idempotent; a different
    project or different parameters under the same ``dispatch_id`` is a conflict
    and is surfaced, never silently overwritten.
    """


class SyntheticContinuityError(SyntheticBackendError):
    """The provider journal was lost or replaced; identity continuity is broken.

    A dispatch must **not** be (re-)admitted when the durable journal that
    recorded it cannot be shown to be the same one, because that could duplicate
    external work. The engine should reconcile or escalate instead.
    """


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _validate_dispatch_id(value: object) -> str:
    if not isinstance(value, str) or DISPATCH_ID_PATTERN.fullmatch(value) is None:
        raise SyntheticInputError(
            f"{value!r} is not a valid dispatch_id (expected 1..128 characters "
            f"from [A-Za-z0-9._:-], starting alphanumeric)."
        )
    return value


def _bounded_int(value: object, *, name: str, lo: int, hi: int) -> int:
    # ``bool`` is an ``int`` subclass; reject it explicitly so ``True`` cannot
    # pass as ``1``.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyntheticInputError(f"{name} must be an integer, not {type(value).__name__}.")
    if not (lo <= value <= hi):
        raise SyntheticInputError(f"{name} must be in [{lo}, {hi}], got {value}.")
    return value


def _validate_mode(value: object) -> str:
    if value not in DISPATCH_MODES:
        raise SyntheticInputError(
            f"mode must be one of {DISPATCH_MODES}, got {value!r}."
        )
    return value  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Time helpers (exact epochs, clearly labelled UTC)
# --------------------------------------------------------------------------- #
def _iso_from_epoch(epoch_s: float) -> str:
    """Return an ISO-8601 UTC timestamp (``...\u00b1hh:mm``) for an epoch."""
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Deterministic synthetic result
# --------------------------------------------------------------------------- #
def _result_value(dispatch_id: str, project_id: str, steps: int) -> int:
    """Return a deterministic synthetic numeric result.

    The value is a pure function of the stable identity and the bounded ``steps``
    parameter, so it is reproducible across restarts *and never* an external
    observation. It is deliberately **not** a scientific measurement.
    """
    seed = f"{dispatch_id}\x00{project_id}\x00{steps}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return int(digest[:12], 16) % 1_000_000


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def _synthetic_v1(conn: sqlite3.Connection) -> None:
    """Create the journal's marker, identity, and dispatch tables.

    Only tables/rows are created; no project data is touched. Later versions add
    numbered migrations without rewriting this one.
    """
    conn.execute(
        """
        CREATE TABLE synthetic_marker (
            name  TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO synthetic_marker (name, value) VALUES ('backend', ?)",
        (MARKER_VALUE,),
    )
    conn.execute(
        """
        CREATE TABLE backend_identity (
            singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
            continuity_id   TEXT NOT NULL,
            created_at_utc  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE dispatch (
            dispatch_id                TEXT PRIMARY KEY,
            project_id                 TEXT NOT NULL,
            mode                       TEXT NOT NULL
                                       CHECK (mode IN ('complete', 'failure', 'ambiguous')),
            steps                      INTEGER NOT NULL,
            delay_ms                   INTEGER NOT NULL,
            admitted_at_utc            TEXT NOT NULL,
            ready_at_utc               TEXT NOT NULL,
            admitted_epoch_s           REAL NOT NULL,
            ready_epoch_s              REAL NOT NULL,
            result_value               INTEGER NOT NULL,
            result_marker              TEXT NOT NULL,
            actual_external_cost_usd   TEXT NOT NULL,
            synthetic                  INTEGER NOT NULL DEFAULT 1
                                       CHECK (synthetic = 1),
            not_a_scientific_result    INTEGER NOT NULL DEFAULT 1
                                       CHECK (not_a_scientific_result = 1)
        )
        """
    )


SYNTHETIC_MIGRATIONS: Mapping[int, "schema.Migration"] = {1: _synthetic_v1}

_LABEL = "synthetic provider journal"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AdmissionReceipt:
    """A durable admission receipt. **Never** a completion (``has_outcome`` false).

    The intended synthetic ``mode`` is recorded so the caller can reason about
    what a *future* lookup may materialize, but the receipt asserts nothing about
    a terminal outcome.
    """

    dispatch_id: str
    project_id: str
    mode: str
    steps: int
    delay_ms: int
    admitted_at_utc: str
    ready_at_utc: str
    admitted_epoch_s: float
    ready_epoch_s: float
    idempotent: bool
    #: Always ``False``: a receipt is durable admission, not a completion.
    has_outcome: bool = False
    #: Always ``False``: same as ``has_outcome``, named for emphasis.
    is_completion: bool = False
    route: str = _SYNTHETIC_ROUTE
    synthetic: bool = True


@dataclass(frozen=True)
class SyntheticResult:
    """A deterministic *synthetic* terminal result (not a scientific result)."""

    value: int
    marker: str
    synthetic: bool = True
    not_a_scientific_result: bool = True
    #: Known, exact zero: this synthetic provider incurred no real external cost.
    actual_external_cost_usd: str = ACTUAL_EXTERNAL_COST_USD_ZERO
    actual_external_cost_known_zero: bool = True


@dataclass(frozen=True)
class LookupResult:
    """Authoritative lookup outcome for a dispatch identity.

    ``status`` is one of ``absent`` / ``admitted`` / ``completed`` / ``failed`` /
    ``unknown``. ``definitive`` is ``True`` only when the absence or terminal is
    *proven* (a definitively absent record, or a proven terminal outcome); a lost
    or ambiguous case is ``definitive = False`` and requires escalation.
    """

    dispatch_id: str
    project_id: str
    status: str
    definitive: bool
    reason: str | None = None
    admitted_at_utc: str | None = None
    ready_at_utc: str | None = None
    ready_epoch_s: float | None = None
    result: SyntheticResult | None = None
    mode: str | None = None
    outcome_unresolved: bool = False
    escalation_required: bool = False
    definitive_failure: bool = False
    non_billing: bool = False
    actual_external_cost_known_zero: bool = False


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #
def _readonly_connect(db_path: str) -> sqlite3.Connection:
    uri = "file:" + quote(db_path) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:  # pragma: no cover - environment dependent
        raise StoreError(
            f"could not open the synthetic provider journal at {db_path!r} "
            f"read-only."
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


class SyntheticBackend:
    """A durable synthetic external-admission backend over its own journal.

    ``SyntheticBackend(data_dir)`` stores its journal at
    ``<data_dir>/runtime/synthetic.sqlite3``. Construction is **read-only**: an
    existing journal is validated (marker + ``user_version``) and never written.
    Pass ``expected_continuity_id`` when the engine knows the journal's
    continuity id from a previous run, so a lost/foreign journal is *not* read as
    absent. Pass ``clock`` (seconds float) to make time deterministic in tests.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        expected_continuity_id: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        raw = os.fspath(data_dir)
        if not raw:
            raise SyntheticBackendError("a data_dir path is required.")
        self._data_dir = os.path.abspath(raw)
        self._runtime_dir = os.path.join(self._data_dir, RUNTIME_DIR_NAME)
        self._journal_path = os.path.join(self._runtime_dir, JOURNAL_FILENAME)
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        if expected_continuity_id is not None:
            if not isinstance(expected_continuity_id, str) or not expected_continuity_id:
                raise SyntheticInputError(
                    "expected_continuity_id must be a non-empty string or None."
                )
        self._expected_continuity_id = expected_continuity_id
        #: Per-process instance marker; distinguishes backend process instances.
        self._instance_generation_id = uuid.uuid4().hex
        #: Continuity id observed at construction (None if no journal was present).
        self._observed_continuity_id: str | None = None

        if os.path.lexists(self._journal_path):
            self._preflight_readonly()
            self._observed_continuity_id = self._read_continuity_id()

    # -- paths / identity ---------------------------------------------------- #
    @property
    def data_dir(self) -> str:
        return self._data_dir

    @property
    def runtime_dir(self) -> str:
        return self._runtime_dir

    @property
    def journal_path(self) -> str:
        return self._journal_path

    @property
    def instance_generation_id(self) -> str:
        """A per-process instance id (not persisted)."""
        return self._instance_generation_id

    @property
    def continuity_id(self) -> str | None:
        """The journal's durable continuity id, or ``None`` if no journal exists."""
        if not os.path.lexists(self._journal_path):
            return None
        try:
            return self._read_continuity_id()
        except ReviewAgentError:
            return None

    @property
    def journal_present(self) -> bool:
        return os.path.lexists(self._journal_path)

    # -- preflight / continuity --------------------------------------------- #
    def _preflight_readonly(self) -> None:
        """Validate an existing journal **read-only**; refuse foreign/unsafe ones.

        Never writes, migrates, or chmods. A symlink, a non-regular file, a
        non-owner-private file, a non-SQLite file, a file without our marker, or a
        newer schema is refused so a foreign target is left byte-identical.
        """
        path = self._journal_path
        if not os.path.lexists(path):
            return
        paths.assert_not_symlink(path, what="synthetic provider journal")
        if not os.path.isfile(path):
            raise StoreError(
                f"the synthetic provider journal path {path!r} is not a regular "
                f"file; refusing to use it."
            )
        paths.assert_owner_private_file(path, what="synthetic provider journal")
        version, tables = schema.read_only_probe(path)
        if MARKER_TABLE not in tables:
            raise StoreError(
                f"the file at {path!r} is not a review_article_agent synthetic "
                f"provider journal (marker table {MARKER_TABLE!r} missing); "
                f"refusing to read or write it."
            )
        if version > SYNTHETIC_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                label=_LABEL,
                found=version,
                supported=SYNTHETIC_SCHEMA_VERSION,
            )

    def _read_continuity_id(self) -> str | None:
        if not os.path.lexists(self._journal_path):
            return None
        conn = _readonly_connect(self._journal_path)
        try:
            row = conn.execute(
                "SELECT continuity_id FROM backend_identity WHERE singleton = 1"
            ).fetchone()
            return None if row is None else str(row[0])
        except sqlite3.DatabaseError:
            return None
        finally:
            conn.close()

    def _continuity_status(self) -> str:
        """Return ``ok`` or ``broken``.

        Broken means a journal we should be able to identify is missing or has a
        different continuity id than the one observed/expected — so absence is
        not proof and admission must not proceed under the same identity.
        """
        expected = self._expected_continuity_id
        observed = self._observed_continuity_id
        if expected is None and observed is None:
            return "ok"
        current = self._read_continuity_id()
        if expected is not None and current != expected:
            return "broken"
        if observed is not None and current != observed:
            return "broken"
        return "ok"

    # -- admit --------------------------------------------------------------- #
    def admit(
        self,
        dispatch_id: str,
        project_id: str,
        *,
        steps: int,
        delay_ms: int,
        outcome: str = "complete",
    ) -> AdmissionReceipt:
        """Durably admit a synthetic dispatch and return a non-completion receipt.

        Identical repeated admission (same ``dispatch_id``, same project, same
        parameters) is idempotent. A divergent project or divergent parameters
        under the same ``dispatch_id`` raises :class:`SyntheticConflict`. If the
        journal cannot be shown to be the same one that recorded prior work,
        :class:`SyntheticContinuityError` is raised instead of duplicating work.
        """
        did = _validate_dispatch_id(dispatch_id)
        pid = paths.validate_project_id(project_id)
        steps = _bounded_int(steps, name="steps", lo=MIN_STEPS, hi=MAX_STEPS)
        delay_ms = _bounded_int(delay_ms, name="delay_ms", lo=MIN_DELAY_MS, hi=MAX_DELAY_MS)
        mode = _validate_mode(outcome)

        if self._continuity_status() == "broken":
            raise SyntheticContinuityError(
                f"the provider journal for dispatch {did!r} is missing or is not "
                f"the journal that recorded prior work; refusing to re-admit "
                f"(reconcile or escalate instead of duplicating external work)."
            )

        paths.ensure_owner_private_dir(self._runtime_dir)
        self._preflight_readonly()

        conn = schema.open_connection(self._journal_path)
        try:
            schema.migrate(
                conn,
                target_version=SYNTHETIC_SCHEMA_VERSION,
                migrations=SYNTHETIC_MIGRATIONS,
                label=_LABEL,
            )
            paths.ensure_owner_private_file(self._journal_path)

            continuity_id = self._ensure_identity(conn)
            self._observed_continuity_id = continuity_id

            existing = conn.execute(
                "SELECT * FROM dispatch WHERE dispatch_id = ?", (did,)
            ).fetchone()
            if existing is not None:
                self._assert_same_request(existing, pid=pid, mode=mode, steps=steps, delay_ms=delay_ms)
                return _receipt_from_row(existing, idempotent=True)

            now = float(self._clock())
            ready = now + delay_ms / 1000.0
            value = _result_value(did, pid, steps)
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO dispatch (
                        dispatch_id, project_id, mode, steps, delay_ms,
                        admitted_at_utc, ready_at_utc, admitted_epoch_s,
                        ready_epoch_s, result_value, result_marker,
                        actual_external_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        did,
                        pid,
                        mode,
                        steps,
                        delay_ms,
                        _iso_from_epoch(now),
                        _iso_from_epoch(ready),
                        now,
                        ready,
                        value,
                        RESULT_MARKER,
                        ACTUAL_EXTERNAL_COST_USD_ZERO,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM dispatch WHERE dispatch_id = ?", (did,)
            ).fetchone()
            return _receipt_from_row(row, idempotent=False)
        finally:
            conn.close()

    @staticmethod
    def _assert_same_request(
        row: sqlite3.Row, *, pid: str, mode: str, steps: int, delay_ms: int
    ) -> None:
        if row["project_id"] != pid:
            raise SyntheticConflict(
                f"dispatch_id {row['dispatch_id']!r} is already admitted for "
                f"project {row['project_id']!r}, not {pid!r}; refusing to reuse "
                f"the identity across projects."
            )
        if (row["steps"], row["delay_ms"], row["mode"]) != (steps, delay_ms, mode):
            raise SyntheticConflict(
                f"dispatch_id {row['dispatch_id']!r} is already admitted with "
                f"different parameters (steps={row['steps']}, "
                f"delay_ms={row['delay_ms']}, mode={row['mode']!r}); refusing to "
                f"overwrite a divergent request."
            )

    def _ensure_identity(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT continuity_id FROM backend_identity WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            return str(row[0])
        continuity_id = uuid.uuid4().hex
        with transaction(conn):
            conn.execute(
                "INSERT INTO backend_identity (singleton, continuity_id, created_at_utc) "
                "VALUES (1, ?, ?)",
                (continuity_id, _iso_from_epoch(float(self._clock()))),
            )
        return continuity_id

    # -- lookup -------------------------------------------------------------- #
    def lookup(self, dispatch_id: str, project_id: str) -> LookupResult:
        """Authoritatively look up a dispatch identity, read-only.

        Returns ``absent`` / ``admitted`` / ``completed`` / ``failed`` /
        ``unknown``. Never initializes, migrates, or overwrites the journal; a
        foreign target is refused and left untouched.
        """
        did = _validate_dispatch_id(dispatch_id)
        pid = paths.validate_project_id(project_id)

        if self._continuity_status() == "broken":
            return LookupResult(
                dispatch_id=did,
                project_id=pid,
                status="unknown",
                definitive=False,
                reason="journal_lost",
                outcome_unresolved=True,
                escalation_required=True,
            )

        if not os.path.lexists(self._journal_path):
            # No journal could ever have recorded an admission here: absence is
            # definitive (and same-identity dispatch is permitted).
            return LookupResult(
                dispatch_id=did, project_id=pid, status="absent", definitive=True
            )

        self._preflight_readonly()
        conn = _readonly_connect(self._journal_path)
        try:
            row = conn.execute(
                "SELECT * FROM dispatch WHERE dispatch_id = ?", (did,)
            ).fetchone()
        finally:
            conn.close()

        if row is None or row["project_id"] != pid:
            return LookupResult(
                dispatch_id=did, project_id=pid, status="absent", definitive=True
            )

        return self._materialize(row)

    def _materialize(self, row: sqlite3.Row) -> LookupResult:
        did = str(row["dispatch_id"])
        pid = str(row["project_id"])
        mode = str(row["mode"])
        now = float(self._clock())
        ready_epoch = float(row["ready_epoch_s"])

        common = dict(
            dispatch_id=did,
            project_id=pid,
            admitted_at_utc=str(row["admitted_at_utc"]),
            ready_at_utc=str(row["ready_at_utc"]),
            ready_epoch_s=ready_epoch,
            mode=mode,
        )

        if now < ready_epoch:
            return LookupResult(status="admitted", definitive=False, reason="in_flight", **common)

        if mode == "complete":
            return LookupResult(
                status="completed",
                definitive=True,
                reason="terminal_complete",
                result=SyntheticResult(
                    value=int(row["result_value"]),
                    marker=str(row["result_marker"]),
                    actual_external_cost_usd=str(row["actual_external_cost_usd"]),
                ),
                actual_external_cost_known_zero=True,
                **common,
            )
        if mode == "failure":
            return LookupResult(
                status="failed",
                definitive=True,
                reason="definitive_failure_non_billing",
                definitive_failure=True,
                non_billing=True,
                actual_external_cost_known_zero=True,
                outcome_unresolved=False,
                **common,
            )
        # mode == "ambiguous": a terminal/quiescent outcome cannot be proven.
        return LookupResult(
            status="unknown",
            definitive=False,
            reason="ambiguous_outcome_unresolved",
            outcome_unresolved=True,
            escalation_required=True,
            **common,
        )

    # -- absence proof ------------------------------------------------------- #
    def same_identity_dispatch_allowed(self, dispatch_id: str, project_id: str) -> bool:
        """``True`` only when the dispatch is **definitively** absent.

        Proving durable absence is the *only* safe basis for dispatching under an
        identity that may already exist. A lost journal or an indeterminate
        outcome returns ``False``.
        """
        result = self.lookup(dispatch_id, project_id)
        return result.status == "absent" and result.definitive


# --------------------------------------------------------------------------- #
# Receipt helpers
# --------------------------------------------------------------------------- #
def _receipt_from_row(row: sqlite3.Row, *, idempotent: bool) -> AdmissionReceipt:
    return AdmissionReceipt(
        dispatch_id=str(row["dispatch_id"]),
        project_id=str(row["project_id"]),
        mode=str(row["mode"]),
        steps=int(row["steps"]),
        delay_ms=int(row["delay_ms"]),
        admitted_at_utc=str(row["admitted_at_utc"]),
        ready_at_utc=str(row["ready_at_utc"]),
        admitted_epoch_s=float(row["admitted_epoch_s"]),
        ready_epoch_s=float(row["ready_epoch_s"]),
        idempotent=idempotent,
    )
