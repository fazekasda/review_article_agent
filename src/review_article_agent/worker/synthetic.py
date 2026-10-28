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
* Existing paths are validated **read-only before any chmod/pragma/migration**:
  the data directory and runtime directory must not be symlinks of the wrong
  type, and an existing runtime directory / journal must already be
  owner-private (``0o700`` / ``0o600``).
* An existing journal is proven ours by **all** of: the exact marker table
  *value*, the exact ``PRAGMA user_version``, a canonical schema fingerprint
  (the SQLite schema must equal a freshly migrated reference), exactly one valid
  ``backend_identity`` row, and well-formed dispatch rows (bounded numerics,
  known mode, expected result marker/cost). A foreign, malformed, or newer-schema
  file is refused and left **byte-identical**.
* Inputs are ids and bounded integers only; there is **no** file/shell/code/
  string-document payload, and none is executed.

Identity continuity
-------------------

Absence of a journal may only be read as "definitively absent" when nothing could
have been admitted. An engine should call :meth:`SyntheticBackend.initialize`
**before** it sends any work: it creates or validates the (possibly empty)
journal and returns a durable ``continuity_id`` the engine persists before its
first dispatch checkpoint, so a crash after the first admission but before the
engine could record the generation is still recoverable.

The constructor stays read-only. If a caller knows the journal's
``continuity_id`` (``expected_continuity_id``) or the journal was observed in this
process, then a lost/replaced journal makes ``initialize``/``admit`` refuse and
``lookup`` return ``unknown`` rather than ``absent`` — the absence is **not**
proof. A ``dispatch_id`` belongs to exactly one project, so a lookup by a
**different** project is an identity conflict (``unknown``, escalate), never a
definitive absence. :meth:`SyntheticBackend.same_identity_dispatch_allowed` is
``True`` only for a **definitively absent** identity.
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
    "REQUIRED_TABLES",
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
#: write. Checked read-only together with ``PRAGMA user_version`` and a schema
#: fingerprint.
MARKER_TABLE = "synthetic_marker"
MARKER_VALUE = "review_article_agent.synthetic_backend.v1"

#: Tables a valid journal must contain.
REQUIRED_TABLES = frozenset({MARKER_TABLE, "backend_identity", "dispatch"})

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

#: Bounded continuity-id shape. Continuity ids are minted by this backend as
#: ``uuid4().hex`` (32 lowercase hex characters); an existing journal whose
#: identity is anything else is malformed and refused.
_CONTINUITY_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


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

    A dispatch must **not** be (re-)admitted — and a replacement journal must
    **not** be minted — when the durable journal that recorded prior work cannot
    be shown to be the same one, because that could duplicate external work. The
    engine should reconcile or escalate instead.
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
# Canonical schema fingerprint (read-only), derived from our own migration
# --------------------------------------------------------------------------- #
def _canonical_schema(conn: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    """Return a canonical fingerprint of a journal's SQLite schema.

    Using ``sqlite_master`` (type, name, SQL) rather than only table *names*
    means a foreign file that merely reuses a table name — or a malformed schema
    with missing constraints/columns — cannot masquerade as our journal.
    """
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "ORDER BY type, name"
    ).fetchall()
    return tuple((str(r[0]), str(r[1]), str(r[2])) for r in rows)


_REFERENCE_SCHEMA: tuple[tuple[str, str, str], ...] | None = None


def _reference_schema() -> tuple[tuple[str, str, str], ...]:
    """Lazily migrate a throwaway in-memory DB to fingerprint our own schema.

    This avoids hand-maintaining a second copy of the DDL: the expected schema is
    always exactly what :data:`SYNTHETIC_MIGRATIONS` produces.
    """
    global _REFERENCE_SCHEMA
    if _REFERENCE_SCHEMA is None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            schema.migrate(
                conn,
                target_version=SYNTHETIC_SCHEMA_VERSION,
                migrations=SYNTHETIC_MIGRATIONS,
                label=_LABEL,
            )
            _REFERENCE_SCHEMA = _canonical_schema(conn)
        finally:
            conn.close()
    return _REFERENCE_SCHEMA


# --------------------------------------------------------------------------- #
# Journal content validation (read-only)
# --------------------------------------------------------------------------- #
def _validate_continuity_value(value: object) -> str:
    if not isinstance(value, str) or _CONTINUITY_ID_PATTERN.fullmatch(value) is None:
        raise StoreError(
            "the synthetic provider journal records a malformed continuity id; "
            "refusing to use it."
        )
    return value


def _validate_identity_rows(conn: sqlite3.Connection) -> str:
    """Return the journal's single, valid ``continuity_id`` or raise."""
    rows = conn.execute(
        "SELECT singleton, continuity_id, created_at_utc FROM backend_identity"
    ).fetchall()
    if len(rows) != 1:
        raise StoreError(
            "the synthetic provider journal must contain exactly one backend "
            "identity row; refusing to use it."
        )
    singleton, continuity_id, created_at = rows[0]
    if singleton != 1:
        raise StoreError(
            "the synthetic provider journal's backend identity singleton must be "
            "1; refusing to use it."
        )
    _validate_continuity_value(continuity_id)
    if not isinstance(created_at, str) or not created_at:
        raise StoreError(
            "the synthetic provider journal's backend identity is missing its "
            "created timestamp; refusing to use it."
        )
    return str(continuity_id)


def _validate_dispatch_row(row: sqlite3.Row) -> None:
    """Validate one dispatch row's identity, bounds, mode, and numeric fields."""

    def _bad(what: str) -> None:
        raise StoreError(
            f"the synthetic provider journal contains a malformed dispatch row "
            f"({what}); refusing to use it."
        )

    dispatch_id = row["dispatch_id"]
    project_id = row["project_id"]
    if not isinstance(dispatch_id, str) or DISPATCH_ID_PATTERN.fullmatch(dispatch_id) is None:
        _bad("dispatch_id")
    if (
        not isinstance(project_id, str)
        or paths.PROJECT_ID_PATTERN.fullmatch(project_id) is None
    ):
        _bad("project_id")
    if row["mode"] not in DISPATCH_MODES:
        _bad("mode")
    steps = row["steps"]
    delay_ms = row["delay_ms"]
    if isinstance(steps, bool) or not isinstance(steps, int) or not (MIN_STEPS <= steps <= MAX_STEPS):
        _bad("steps")
    if (
        isinstance(delay_ms, bool)
        or not isinstance(delay_ms, int)
        or not (MIN_DELAY_MS <= delay_ms <= MAX_DELAY_MS)
    ):
        _bad("delay_ms")
    if row["result_marker"] != RESULT_MARKER:
        _bad("result_marker")
    if row["actual_external_cost_usd"] != ACTUAL_EXTERNAL_COST_USD_ZERO:
        _bad("actual_external_cost_usd")
    if row["synthetic"] != 1 or row["not_a_scientific_result"] != 1:
        _bad("synthetic flags")
    result_value = row["result_value"]
    if (
        isinstance(result_value, bool)
        or not isinstance(result_value, int)
        or not (0 <= result_value < 1_000_000)
    ):
        _bad("result_value")
    admitted_epoch = row["admitted_epoch_s"]
    ready_epoch = row["ready_epoch_s"]
    for name, value in (("admitted_epoch_s", admitted_epoch), ("ready_epoch_s", ready_epoch)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _bad(name)
        if value != value or value in (float("inf"), float("-inf")):
            _bad(name)
    if float(ready_epoch) < float(admitted_epoch):
        _bad("ready_epoch_s < admitted_epoch_s")
    for name in ("admitted_at_utc", "ready_at_utc"):
        value = row[name]
        if not isinstance(value, str) or not value:
            _bad(name)


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
    journal, an ambiguous outcome, or an identity owned by another project is
    ``definitive = False`` and requires escalation.
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
    #: ``True`` when the ``dispatch_id`` exists but belongs to a different project.
    identity_conflict: bool = False


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
    existing journal is validated (marker value + exact ``user_version`` + schema
    fingerprint + identity + rows) and never written. Pass
    ``expected_continuity_id`` when the engine knows the journal's continuity id
    from a previous run, so a lost/foreign journal is *not* read as absent. Pass
    ``clock`` (seconds float) to make time deterministic in tests. Call
    :meth:`initialize` before the first dispatch to obtain the durable continuity
    id.
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
            self._observed_continuity_id = self._preflight_readonly()

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

    # -- layout (read-only) / preflight ------------------------------------- #
    def _validate_layout_readonly(self) -> None:
        """Validate the data/runtime directories read-only before mutating them.

        Never creates, chmods, or follows a symlink. This runs *before* any
        ``ensure``/open so an existing hostile or malformed layout is refused
        rather than being repaired.
        """
        data_dir = self._data_dir
        if os.path.lexists(data_dir):
            if os.path.islink(data_dir):
                raise StoreError(
                    f"the synthetic backend data directory {data_dir!r} is a "
                    f"symlink; refusing to use it."
                )
            if not os.path.isdir(data_dir):
                raise StoreError(
                    f"the synthetic backend data path {data_dir!r} is not a "
                    f"directory; refusing to use it."
                )
        runtime_dir = self._runtime_dir
        if os.path.lexists(runtime_dir):
            if os.path.islink(runtime_dir):
                raise StoreError(
                    f"the synthetic backend runtime directory {runtime_dir!r} is a "
                    f"symlink; refusing to use it."
                )
            if not os.path.isdir(runtime_dir):
                raise StoreError(
                    f"the synthetic backend runtime path {runtime_dir!r} is not a "
                    f"directory; refusing to use it."
                )
            paths.assert_owner_private_dir(
                runtime_dir, what="synthetic backend runtime directory"
            )

    def _preflight_readonly(self) -> str | None:
        """Validate an existing journal **read-only**; return its continuity id.

        Never writes, migrates, or chmods. A symlink, a non-regular or
        non-owner-private file, a non-SQLite file, a wrong marker *value*, a
        missing required table, a non-canonical schema, a wrong ``user_version``,
        a malformed/multiple/absent identity, or a malformed dispatch row is
        refused so a foreign target is left byte-identical. Returns ``None`` when
        no journal exists yet.
        """
        path = self._journal_path
        if not os.path.lexists(path):
            return None
        paths.assert_not_symlink(path, what="synthetic provider journal")
        if not os.path.isfile(path):
            raise StoreError(
                f"the synthetic provider journal path {path!r} is not a regular "
                f"file; refusing to use it."
            )
        paths.assert_owner_private_file(path, what="synthetic provider journal")

        conn = _readonly_connect(path)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not REQUIRED_TABLES <= tables:
                raise StoreError(
                    f"the file at {path!r} is not a review_article_agent synthetic "
                    f"provider journal (required tables missing); refusing to read "
                    f"or write it."
                )
            if version > SYNTHETIC_SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    label=_LABEL,
                    found=version,
                    supported=SYNTHETIC_SCHEMA_VERSION,
                )
            if version != SYNTHETIC_SCHEMA_VERSION:
                raise StoreError(
                    f"the file at {path!r} records unsupported synthetic provider "
                    f"journal schema version {version} (expected "
                    f"{SYNTHETIC_SCHEMA_VERSION}); refusing to use it."
                )
            if _canonical_schema(conn) != _reference_schema():
                raise StoreError(
                    f"the file at {path!r} has a non-canonical synthetic provider "
                    f"journal schema; refusing to use it."
                )
            marker = conn.execute(
                "SELECT value FROM synthetic_marker WHERE name = 'backend'"
            ).fetchone()
            if marker is None or str(marker[0]) != MARKER_VALUE:
                raise StoreError(
                    f"the file at {path!r} is not a review_article_agent synthetic "
                    f"provider journal (marker value mismatch); refusing to read or "
                    f"write it."
                )
            continuity_id = _validate_identity_rows(conn)
            for row in conn.execute("SELECT * FROM dispatch"):
                _validate_dispatch_row(row)
            return continuity_id
        finally:
            conn.close()

    def _read_continuity_id(self) -> str | None:
        if not os.path.lexists(self._journal_path):
            return None
        conn = _readonly_connect(self._journal_path)
        try:
            return _validate_identity_rows(conn)
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
        try:
            current = self._read_continuity_id()
        except ReviewAgentError:
            return "broken"
        if expected is not None and current != expected:
            return "broken"
        if observed is not None and current != observed:
            return "broken"
        return "ok"

    # -- initialize ---------------------------------------------------------- #
    def initialize(self) -> str:
        """Create or validate the journal and return its durable continuity id.

        The engine calls this **before** it sends any work, then persists the
        returned id before its first dispatch/attempt checkpoint. This closes the
        crash-after-first-admission window. It never creates a dispatch row, and
        it never mints a replacement journal when a journal was expected or was
        previously observed but is now lost/changed.
        """
        self._validate_layout_readonly()
        if self.journal_present:
            current = self._preflight_readonly()
            assert current is not None  # present ⇒ preflight returns an id
            if self._expected_continuity_id is not None and current != self._expected_continuity_id:
                raise SyntheticContinuityError(
                    f"the provider journal's continuity id ({current!r}) does not "
                    f"match the expected one ({self._expected_continuity_id!r}); "
                    f"refusing to initialize over a replaced journal."
                )
            if self._observed_continuity_id is not None and current != self._observed_continuity_id:
                raise SyntheticContinuityError(
                    f"the provider journal's continuity id changed since it was "
                    f"observed ({self._observed_continuity_id!r} -> {current!r}); "
                    f"refusing to initialize over a replaced journal."
                )
            self._observed_continuity_id = current
            return current
        # No journal: only a truly fresh backend may create one.
        if self._expected_continuity_id is not None:
            raise SyntheticContinuityError(
                f"no provider journal exists but continuity id "
                f"{self._expected_continuity_id!r} was expected; refusing to mint a "
                f"replacement journal (reconcile or escalate instead)."
            )
        if self._observed_continuity_id is not None:
            raise SyntheticContinuityError(
                f"the provider journal was observed ({self._observed_continuity_id!r}) "
                f"but is now missing; refusing to mint a replacement journal."
            )
        return self._create_journal()

    def _create_journal(self) -> str:
        """Create a fresh owner-private journal with a new durable identity."""
        paths.ensure_owner_private_dir(self._runtime_dir)
        conn = schema.open_connection(self._journal_path)
        try:
            schema.migrate(
                conn,
                target_version=SYNTHETIC_SCHEMA_VERSION,
                migrations=SYNTHETIC_MIGRATIONS,
                label=_LABEL,
            )
            paths.ensure_owner_private_file(self._journal_path)
            with transaction(conn):
                continuity_id = self._mint_identity(conn)
            self._observed_continuity_id = continuity_id
            return continuity_id
        finally:
            conn.close()

    def _mint_identity(self, conn: sqlite3.Connection) -> str:
        """Insert a new identity row. The caller must hold a transaction."""
        continuity_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO backend_identity (singleton, continuity_id, created_at_utc) "
            "VALUES (1, ?, ?)",
            (continuity_id, _iso_from_epoch(float(self._clock()))),
        )
        return continuity_id

    def _ensure_identity_locked(self, conn: sqlite3.Connection, *, allow_mint: bool) -> str:
        """Return the existing identity, minting only for a brand-new journal.

        A pre-existing journal with zero, multiple, or malformed identity rows is
        **never** silently repaired or re-minted.
        """
        rows = conn.execute(
            "SELECT singleton, continuity_id, created_at_utc FROM backend_identity"
        ).fetchall()
        if len(rows) == 1:
            singleton, continuity_id, created_at = rows[0]
            if singleton != 1:
                raise StoreError(
                    "the synthetic provider journal's backend identity singleton "
                    "must be 1; refusing to repair it."
                )
            _validate_continuity_value(continuity_id)
            if not isinstance(created_at, str) or not created_at:
                raise StoreError(
                    "the synthetic provider journal's backend identity is missing "
                    "its created timestamp; refusing to repair it."
                )
            return str(continuity_id)
        if len(rows) == 0:
            if allow_mint:
                return self._mint_identity(conn)
            raise StoreError(
                "the existing synthetic provider journal has no backend identity; "
                "refusing to mint a replacement identity for it."
            )
        raise StoreError(
            "the synthetic provider journal must contain exactly one backend "
            "identity row; refusing to repair it."
        )

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
        parameters) is idempotent even under concurrency: the identity
        initialization, the existing-record read, and the insert all run inside a
        single ``BEGIN IMMEDIATE`` transaction, and a unique-key race re-reads to
        return the idempotent/conflicting outcome instead of failing. A divergent
        project or divergent parameters under the same ``dispatch_id`` raises
        :class:`SyntheticConflict`. If the journal cannot be shown to be the same
        one that recorded prior work, :class:`SyntheticContinuityError` is raised
        instead of duplicating work.
        """
        did = _validate_dispatch_id(dispatch_id)
        pid = paths.validate_project_id(project_id)
        steps = _bounded_int(steps, name="steps", lo=MIN_STEPS, hi=MAX_STEPS)
        delay_ms = _bounded_int(delay_ms, name="delay_ms", lo=MIN_DELAY_MS, hi=MAX_DELAY_MS)
        mode = _validate_mode(outcome)

        self._validate_layout_readonly()
        # Read-only validation of an existing journal before any chmod/pragma.
        self._preflight_readonly()
        if self._continuity_status() == "broken":
            raise SyntheticContinuityError(
                f"the provider journal for dispatch {did!r} is missing or is not "
                f"the journal that recorded prior work; refusing to re-admit "
                f"(reconcile or escalate instead of duplicating external work)."
            )
        existed = self.journal_present

        paths.ensure_owner_private_dir(self._runtime_dir)
        conn = schema.open_connection(self._journal_path)
        try:
            schema.migrate(
                conn,
                target_version=SYNTHETIC_SCHEMA_VERSION,
                migrations=SYNTHETIC_MIGRATIONS,
                label=_LABEL,
            )
            paths.ensure_owner_private_file(self._journal_path)

            with transaction(conn):
                continuity_id = self._ensure_identity_locked(
                    conn, allow_mint=not existed
                )
                self._observed_continuity_id = continuity_id

                existing = conn.execute(
                    "SELECT * FROM dispatch WHERE dispatch_id = ?", (did,)
                ).fetchone()
                if existing is not None:
                    return self._replay_or_conflict(
                        existing, pid=pid, mode=mode, steps=steps, delay_ms=delay_ms
                    )

                now = float(self._clock())
                ready = now + delay_ms / 1000.0
                value = _result_value(did, pid, steps)
                try:
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
                except sqlite3.IntegrityError:
                    # A concurrent identical admission won the unique key; re-read
                    # and return the idempotent (or conflicting) outcome.
                    raced = conn.execute(
                        "SELECT * FROM dispatch WHERE dispatch_id = ?", (did,)
                    ).fetchone()
                    if raced is None:
                        raise
                    return self._replay_or_conflict(
                        raced, pid=pid, mode=mode, steps=steps, delay_ms=delay_ms
                    )
                row = conn.execute(
                    "SELECT * FROM dispatch WHERE dispatch_id = ?", (did,)
                ).fetchone()
                return _receipt_from_row(row, idempotent=False)
        finally:
            conn.close()

    def _replay_or_conflict(
        self,
        row: sqlite3.Row,
        *,
        pid: str,
        mode: str,
        steps: int,
        delay_ms: int,
    ) -> AdmissionReceipt:
        _validate_dispatch_row(row)
        self._assert_same_request(row, pid=pid, mode=mode, steps=steps, delay_ms=delay_ms)
        return _receipt_from_row(row, idempotent=True)

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

    # -- lookup -------------------------------------------------------------- #
    def lookup(self, dispatch_id: str, project_id: str) -> LookupResult:
        """Authoritatively look up a dispatch identity, read-only.

        Returns ``absent`` / ``admitted`` / ``completed`` / ``failed`` /
        ``unknown``. Never initializes, migrates, or overwrites the journal; a
        foreign target is refused and left untouched. A ``dispatch_id`` that
        belongs to another project is an ``unknown`` identity conflict, never a
        definitive ``absent``.
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

        if row is None:
            return LookupResult(
                dispatch_id=did, project_id=pid, status="absent", definitive=True
            )
        if row["project_id"] != pid:
            # The identity exists but is owned by another project: absence is not
            # proof, and the engine must not re-dispatch under this identity.
            return LookupResult(
                dispatch_id=did,
                project_id=pid,
                status="unknown",
                definitive=False,
                reason="identity_owned_by_another_project",
                outcome_unresolved=True,
                escalation_required=True,
                identity_conflict=True,
            )

        _validate_dispatch_row(row)
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
        identity that may already exist. A lost journal, an identity conflict, or
        an indeterminate outcome returns ``False``.
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
