"""Durable, inert project-local **search observation** repository (Step 05, T2).

Authority
---------

* ``docs/implementation/05-discovery.md`` deliverable 3 and acceptance criterion
  3 ("Reconstruct exact queries, timestamps, contributing sources, failures,
  counts, and deduplication decisions from records") and acceptance criterion 4
  (fixed fixtures for pagination, overlap, missing IDs, partial failures, and
  incremental reruns).
* ``docs/specification.md`` (persisted, project-local product state and the
  untrusted-provider-data safety invariant).
* ``docs/contracts/03-policy-decisions.md`` ``ARCH-STORE`` (one app-owned,
  project-scoped SQLite store) and ``ARCH-MIGRATE`` (versioned, additive
  migrations; consistent verified backup/restore).

What this module **is**
-----------------------

The storage half of "record the exact provider observation a trusted caller
already obtained". Given the exact request replay key supplied by the caller and
the already-parsed :class:`~review_article_agent.discovery.records.SearchResult`
(or :class:`~review_article_agent.discovery.pubmed.PubMedSearchResult`), it
records **one immutable observation row** (schema migration 11) holding:

* the exact canonical bytes produced by the T1 codec
  (:func:`~review_article_agent.discovery.serialization.encode_search_result`),
  their recomputed SHA-256 digest and byte size;
* a small canonical *binding* object tying the store's actual ``project_id``,
  the deterministic project-local ``run_id``, the caller's ``request_id``, the
  app-recorded UTC time, and the result digest/type together;
* projected, queryable source/query/time/count/failure columns that are
  **re-derived from the canonical result bytes** on every read.

A **stable run id** is derived from the store's own project id and the bounded
request id (``dsr_`` + SHA-256), so identity is deterministic per
``(project, request)`` and never caller-selectable. The API takes no project id:
the repository is bound to one :class:`~review_article_agent.storage.store.ProjectStore`.

An identical replay for the same project/request and canonical result returns
the **historical** row (including its original recorded time), even after a
reopen; a changed result under the same key is a static
:class:`DiscoverySearchConflictError` and never overwrites. Different request
keys are separate observation runs even when the search payload is identical.

What this module **is not** (the boundary)
------------------------------------------

* It performs **no** network, provider, transport, CLI, worker, approval, or
  model call. It records an observation of a result it is handed.
* It **grants no authority** and makes **no** scientific decision. A recorded
  search is **untrusted provenance**, never accepted project context, an
  approval, a candidate identity, a deduplication decision, a coverage claim, or
  a corpus freeze. Overlapping and duplicate records are preserved verbatim.
* It **invents no rights, policy, cost, or completeness rule**. The adapter's
  original ``complete``/``stop_reason``/``failure``/counts are preserved exactly,
  including incomplete or failed observations.
* It makes **no secret-scanner claim**. The canonical bytes are private local
  provenance and may contain the exact query, source URLs, titles, and provider
  identifiers; they are never logged or echoed in an error.

Every failure is a static, value-free error: a message never echoes a query,
source, URL, title, identifier, payload byte, or digest.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..discovery.pubmed import PubMedSearchResult
from ..discovery.records import SearchResult
from ..discovery.serialization import (
    MAX_SEARCH_RESULT_BYTES,
    DiscoverySerializationError,
    decode_search_result,
    encode_search_result,
)
from ..domain.errors import StoreError
from ..domain.records import canonical_json, validate_identifier

__all__ = [
    "DISCOVERY_SEARCH_SCHEMA_VERSION",
    "MAX_DISCOVERY_REQUEST_ID_CHARS",
    "MAX_DISCOVERY_BINDING_BYTES",
    "DiscoverySearchError",
    "DiscoverySearchValidationError",
    "DiscoverySearchConflictError",
    "DiscoverySearchCorruptionError",
    "DiscoverySearchRecord",
    "DiscoveryRepository",
    "discovery_integrity_problems",
]

#: Version of the *recorded search observation representation*. Independent of
#: the store schema version and of the embedded T1 codec version.
DISCOVERY_SEARCH_SCHEMA_VERSION = 1

#: Request replay keys follow the existing bounded opaque-identifier convention.
MAX_DISCOVERY_REQUEST_ID_CHARS = 512

#: The binding is a handful of ids/digests; a generous technical bound guards a
#: corrupted or hostile blob without admitting an unbounded parse.
MAX_DISCOVERY_BINDING_BYTES = 16_384

#: The two supported concrete result types, as recorded.
_RESULT_TYPE_SEARCH = "search_result"
_RESULT_TYPE_PUBMED = "pubmed_search_result"

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: Projected numeric columns that must be plain integers (never booleans).
_COUNT_COLUMNS = (
    "schema_version",
    "page_count",
    "record_count",
    "attempt_count",
    "complete",
    "result_bytes",
    "binding_bytes",
)


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class DiscoverySearchError(StoreError):
    """Base class for every recorded discovery search-observation failure.

    Every message is static and curated: it never echoes a query, source, URL,
    title, identifier, payload byte, or digest.
    """


class DiscoverySearchValidationError(DiscoverySearchError, ValueError):
    """An input request key/result is malformed, mutated, or unsupported."""


class DiscoverySearchConflictError(DiscoverySearchError):
    """A divergent result was supplied for an already-recorded request key."""


class DiscoverySearchCorruptionError(DiscoverySearchError):
    """A stored observation row fails structural/hash/projection revalidation."""


# --------------------------------------------------------------------------- #
# Static messages (never echo a value)
# --------------------------------------------------------------------------- #
_M_BAD_REQUEST_ID = "a discovery request id is not a valid bounded identifier"
_M_BAD_RUN_ID = "a discovery run id is not a valid identifier"
_M_BAD_PROJECT = "the project store identity is not a valid discovery project identifier"
_M_BAD_RESULT = "the supplied discovery search result is not a supported immutable result"
_M_MUTATED_RESULT = "the supplied discovery search result does not round-trip losslessly"
_M_UNSUPPORTED_RESULT = "the supplied discovery search result type is unsupported"
_M_BAD_ROW = "a discovery search observation row is structurally invalid"
_M_FOREIGN = "a discovery search observation row belongs to a foreign project"
_M_DIGEST = "a discovery search observation digest is not a lowercase hex SHA-256 digest"
_M_TIMESTAMP = "a discovery search observation timestamp is not an RFC 3339 UTC instant"
_M_CANONICAL_SIZE = "a discovery search observation canonical result is outside the permitted size"
_M_BINDING_SIZE = "a discovery search observation binding is outside the permitted size"
_M_DIGEST_MISMATCH = "a discovery search observation digest does not match its bytes"
_M_PROJECTION = "a projected discovery search observation column disagrees with its canonical result"
_M_DIVERGENT = "a different discovery search result is already recorded for this request key"
_M_SQL = "the discovery search observation could not be read or written safely"
_M_PARTIAL = "a discovery search observation was not visible after insertion"


# --------------------------------------------------------------------------- #
# Small strict helpers
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise DiscoverySearchValidationError(_M_TIMESTAMP)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DiscoverySearchValidationError(_M_TIMESTAMP) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DiscoverySearchValidationError(_M_TIMESTAMP)
    return value


def _bare_hex(value: object) -> str:
    if not isinstance(value, str):
        raise DiscoverySearchValidationError(_M_DIGEST)
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise DiscoverySearchValidationError(_M_DIGEST)
    return text


def _request_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "request_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise DiscoverySearchValidationError(_M_BAD_REQUEST_ID) from exc
    if len(normalized) > MAX_DISCOVERY_REQUEST_ID_CHARS:
        raise DiscoverySearchValidationError(_M_BAD_REQUEST_ID)
    for char in normalized:
        if 0xD800 <= ord(char) <= 0xDFFF:
            # A lone surrogate is not portably encodable into the canonical
            # identity material; refuse it as a bad key rather than leaking a
            # JSON-serialization failure.
            raise DiscoverySearchValidationError(_M_BAD_REQUEST_ID)
    return normalized


def _run_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "run_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise DiscoverySearchValidationError(_M_BAD_RUN_ID) from exc
    if len(normalized) > MAX_DISCOVERY_REQUEST_ID_CHARS or len(normalized) < 1:
        raise DiscoverySearchValidationError(_M_BAD_RUN_ID)
    return normalized


def _project_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "project_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise DiscoverySearchValidationError(_M_BAD_PROJECT) from exc
    if len(normalized) > MAX_DISCOVERY_REQUEST_ID_CHARS:
        raise DiscoverySearchValidationError(_M_BAD_PROJECT)
    return normalized


def _run_id(project_id: str, request_id: str) -> str:
    """Derive the stable, project-local run id for one request replay key."""
    material = canonical_json([project_id, request_id])
    return "dsr_" + hashlib.sha256(material).hexdigest()[:32]


def _result_type(result: SearchResult) -> str:
    if type(result) is PubMedSearchResult:
        return _RESULT_TYPE_PUBMED
    if type(result) is SearchResult:
        return _RESULT_TYPE_SEARCH
    raise DiscoverySearchValidationError(_M_UNSUPPORTED_RESULT)


def _canonical_result(result: object) -> bytes:
    """Encode one exact immutable result and prove a lossless, canonical roundtrip."""
    if type(result) not in (SearchResult, PubMedSearchResult):
        raise DiscoverySearchValidationError(_M_BAD_RESULT)
    try:
        canonical = encode_search_result(result)
        decoded = decode_search_result(canonical)
        if type(decoded) is not type(result) or decoded != result:
            raise DiscoverySearchValidationError(_M_MUTATED_RESULT)
        if encode_search_result(decoded) != canonical:
            raise DiscoverySearchValidationError(_M_MUTATED_RESULT)
    except DiscoverySerializationError as exc:
        raise DiscoverySearchValidationError(_M_BAD_RESULT) from exc
    return canonical


def _binding_canonical(
    *,
    project_id: str,
    run_id: str,
    request_id: str,
    recorded_at_utc: str,
    result_type: str,
    result_sha256: str,
    result_bytes: int,
) -> bytes:
    return canonical_json(
        {
            "schema_version": DISCOVERY_SEARCH_SCHEMA_VERSION,
            "project_id": project_id,
            "run_id": run_id,
            "request_id": request_id,
            "recorded_at_utc": recorded_at_utc,
            "result_type": result_type,
            "result_sha256": result_sha256,
            "result_bytes": result_bytes,
        }
    )


# --------------------------------------------------------------------------- #
# Frozen value returned to callers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DiscoverySearchRecord:
    """One historical, losslessly reconstructed search observation.

    ``result`` is decoded from the exact stored canonical bytes, so it includes
    the PubMed ``query_translation`` and every overlapping/duplicate/missing-ID
    record exactly as observed. ``canonical`` is the immutable T1 result encoding
    and ``binding`` is the canonical metadata/content binding.
    """

    project_id: str
    run_id: str
    request_id: str
    schema_version: int
    recorded_at_utc: str
    result_type: str
    source: str
    query: str
    started_at: str
    completed_at: str
    stop_reason: str
    total_count: int | None
    complete: bool
    failure: str | None
    page_count: int
    record_count: int
    attempt_count: int
    result_sha256: str
    result_bytes: int
    canonical: bytes
    binding_sha256: str
    binding_bytes: int
    binding: bytes
    result: SearchResult


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class DiscoveryRepository:
    """Record and reload inert search observations for one project store."""

    def __init__(self, store) -> None:
        self._store = store

    @property
    def project_id(self) -> str:
        """The actual store's project id; no caller may select a project."""
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- record/replay (one atomic BEGIN IMMEDIATE transaction) -------------- #
    def record_search(self, *, request_id: str, result: SearchResult) -> DiscoverySearchRecord:
        """Record one exact observation or replay the identical historical row.

        The canonical result is encoded/validated **before** the transaction; the
        row is then inserted and immediately re-read through the same shared read
        validator inside one ``BEGIN IMMEDIATE`` transaction. An identical replay
        returns the stored row (including its original ``recorded_at_utc``); a
        divergent result for the same key raises
        :class:`DiscoverySearchConflictError`; a refused call writes nothing and a
        failure leaves zero rows.
        """
        request_id = _request_identifier(request_id)
        canonical = _canonical_result(result)
        project_id = _project_identifier(self.project_id)
        run_id = _run_id(project_id, request_id)
        recorded_at = _utc_now()
        result_type = _result_type(result)
        result_sha256 = _sha256_hex(canonical)
        binding = _binding_canonical(
            project_id=project_id,
            run_id=run_id,
            request_id=request_id,
            recorded_at_utc=recorded_at,
            result_type=result_type,
            result_sha256=result_sha256,
            result_bytes=len(canonical),
        )

        try:
            with self._store.transaction():
                existing = self._run_row(run_id)
                if existing is not None:
                    stored = self._validate_row(existing)
                    if stored.canonical != canonical:
                        raise DiscoverySearchConflictError(_M_DIVERGENT)
                    return stored

                self._insert_run(
                    project_id=project_id,
                    run_id=run_id,
                    request_id=request_id,
                    recorded_at=recorded_at,
                    result_type=result_type,
                    canonical=canonical,
                    result_sha256=result_sha256,
                    binding=binding,
                    result=result,
                )
                row = self._run_row(run_id)
                if row is None:  # pragma: no cover - just inserted in this transaction
                    raise DiscoverySearchCorruptionError(_M_PARTIAL)
                return self._validate_row(row)
        except DiscoverySearchError:
            raise
        except sqlite3.Error as exc:
            # Never echo SQL text: it could name a column but never a value.
            raise DiscoverySearchError(_M_SQL) from exc

    def _insert_run(
        self,
        *,
        project_id: str,
        run_id: str,
        request_id: str,
        recorded_at: str,
        result_type: str,
        canonical: bytes,
        result_sha256: str,
        binding: bytes,
        result: SearchResult,
    ) -> None:
        binding_sha256 = _sha256_hex(binding)
        self._conn().execute(
            "INSERT INTO discovery_search_run "
            "(project_id, run_id, request_id, schema_version, recorded_at_utc, "
            " result_type, source, query, started_at, completed_at, stop_reason, "
            " total_count, complete, failure, page_count, record_count, "
            " attempt_count, result_sha256, result_bytes, canonical, "
            " binding_sha256, binding_bytes, binding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                run_id,
                request_id,
                DISCOVERY_SEARCH_SCHEMA_VERSION,
                recorded_at,
                result_type,
                result.source,
                result.query,
                result.started_at,
                result.completed_at,
                result.stop_reason,
                result.total_count,
                1 if result.complete else 0,
                result.failure,
                len(result.pages),
                len(result.records),
                len(result.attempts),
                result_sha256,
                len(canonical),
                bytes(canonical),
                binding_sha256,
                len(binding),
                bytes(binding),
            ),
        )

    # -- reads --------------------------------------------------------------- #
    def get_search(self, run_id: str) -> DiscoverySearchRecord | None:
        """Read one observation by its stable run id, revalidating it in full."""
        run_id = _run_identifier(run_id)
        try:
            row = self._run_row(run_id)
        except sqlite3.Error as exc:
            raise DiscoverySearchCorruptionError(_M_SQL) from exc
        if row is None:
            return None
        return self._validate_row(row)

    def list_searches(self) -> list[DiscoverySearchRecord]:
        """Every observation of this project in deterministic insertion order."""
        try:
            rows = self._conn().execute(
                "SELECT * FROM discovery_search_run WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DiscoverySearchCorruptionError(_M_SQL) from exc
        return [self._validate_row(row) for row in rows]

    # -- shared whole-ledger integrity (read-only) --------------------------- #
    def verify_all_integrity(self) -> list[str]:
        """Return static integrity problems across every observation (empty = ok)."""
        return self._integrity_problems()

    def _integrity_problems(self) -> list[str]:
        problems: list[str] = []
        project_id = _project_identifier(self.project_id)
        try:
            rows = self._conn().execute(
                "SELECT * FROM discovery_search_run ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DiscoverySearchCorruptionError(_M_SQL) from exc
        for index, row in enumerate(rows):
            if row["project_id"] != project_id:
                problems.append(
                    f"discovery_search_run: row {index} is a foreign project row"
                )
                continue
            try:
                self._validate_row(row)
            except Exception:  # noqa: BLE001 - one corrupt row must not abort the sweep
                problems.append(
                    f"discovery_search_run: row {index} failed integrity revalidation"
                )
        return problems

    # -- internal row access + independent revalidation ---------------------- #
    def _run_row(self, run_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM discovery_search_run WHERE project_id = ? AND run_id = ?",
            (self.project_id, run_id),
        ).fetchone()

    def _validate_row(self, row: sqlite3.Row) -> DiscoverySearchRecord:
        """Re-derive every stored value from the immutable canonical bytes.

        Nothing is trusted from the projected columns: the request key, run id,
        timestamps, result hash/size, binding, type, source/query, counts,
        completeness and failure are all independently revalidated. Any mismatch
        is a static :class:`DiscoverySearchCorruptionError`.
        """
        project_id = _project_identifier(self.project_id)
        if row["project_id"] != project_id:
            raise DiscoverySearchCorruptionError(_M_FOREIGN)
        try:
            request_id = _request_identifier(row["request_id"])
        except DiscoverySearchValidationError as exc:
            raise DiscoverySearchCorruptionError(_M_BAD_ROW) from exc
        expected_run_id = _run_id(project_id, request_id)
        if row["run_id"] != expected_run_id:
            raise DiscoverySearchCorruptionError(_M_BAD_ROW)
        for column in _COUNT_COLUMNS:
            if not _plain_int(row[column]):
                raise DiscoverySearchCorruptionError(_M_BAD_ROW)
        if row["schema_version"] != DISCOVERY_SEARCH_SCHEMA_VERSION:
            raise DiscoverySearchCorruptionError(_M_BAD_ROW)
        if row["complete"] not in (0, 1):
            raise DiscoverySearchCorruptionError(_M_BAD_ROW)
        if row["page_count"] < 0 or row["record_count"] < 0 or row["attempt_count"] < 0:
            raise DiscoverySearchCorruptionError(_M_BAD_ROW)
        if row["total_count"] is not None and (
            not _plain_int(row["total_count"]) or row["total_count"] < 0
        ):
            raise DiscoverySearchCorruptionError(_M_BAD_ROW)
        try:
            recorded_at = _require_utc_timestamp(row["recorded_at_utc"])
        except DiscoverySearchValidationError as exc:
            raise DiscoverySearchCorruptionError(_M_TIMESTAMP) from exc

        canonical = _row_bytes(row["canonical"], _M_CANONICAL_SIZE)
        if len(canonical) == 0 or len(canonical) > MAX_SEARCH_RESULT_BYTES:
            raise DiscoverySearchCorruptionError(_M_CANONICAL_SIZE)
        if not _plain_int(row["result_bytes"]) or row["result_bytes"] != len(canonical):
            raise DiscoverySearchCorruptionError(_M_CANONICAL_SIZE)
        try:
            result_sha256 = _bare_hex(row["result_sha256"])
        except DiscoverySearchValidationError as exc:
            raise DiscoverySearchCorruptionError(_M_DIGEST) from exc
        if _sha256_hex(canonical) != result_sha256:
            raise DiscoverySearchCorruptionError(_M_DIGEST_MISMATCH)

        binding = _row_bytes(row["binding"], _M_BINDING_SIZE)
        if len(binding) == 0 or len(binding) > MAX_DISCOVERY_BINDING_BYTES:
            raise DiscoverySearchCorruptionError(_M_BINDING_SIZE)
        if not _plain_int(row["binding_bytes"]) or row["binding_bytes"] != len(binding):
            raise DiscoverySearchCorruptionError(_M_BINDING_SIZE)
        try:
            binding_sha256 = _bare_hex(row["binding_sha256"])
        except DiscoverySearchValidationError as exc:
            raise DiscoverySearchCorruptionError(_M_DIGEST) from exc
        if _sha256_hex(binding) != binding_sha256:
            raise DiscoverySearchCorruptionError(_M_DIGEST_MISMATCH)

        try:
            result = decode_search_result(canonical)
            if encode_search_result(result) != canonical:
                raise DiscoverySearchCorruptionError(_M_PROJECTION)
        except DiscoverySerializationError as exc:
            raise DiscoverySearchCorruptionError(_M_BAD_ROW) from exc

        expected_type = _result_type(result)
        expected_binding = _binding_canonical(
            project_id=project_id,
            run_id=expected_run_id,
            request_id=request_id,
            recorded_at_utc=recorded_at,
            result_type=expected_type,
            result_sha256=result_sha256,
            result_bytes=len(canonical),
        )
        if binding != expected_binding:
            raise DiscoverySearchCorruptionError(_M_PROJECTION)

        expected = {
            "result_type": expected_type,
            "source": result.source,
            "query": result.query,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "stop_reason": result.stop_reason,
            "total_count": result.total_count,
            "complete": 1 if result.complete else 0,
            "failure": result.failure,
            "page_count": len(result.pages),
            "record_count": len(result.records),
            "attempt_count": len(result.attempts),
            "result_sha256": result_sha256,
            "result_bytes": len(canonical),
            "binding_sha256": binding_sha256,
            "binding_bytes": len(binding),
            "recorded_at_utc": recorded_at,
        }
        for column, value in expected.items():
            if row[column] != value:
                raise DiscoverySearchCorruptionError(_M_PROJECTION)

        return DiscoverySearchRecord(
            project_id=project_id,
            run_id=expected_run_id,
            request_id=request_id,
            schema_version=row["schema_version"],
            recorded_at_utc=recorded_at,
            result_type=expected_type,
            source=result.source,
            query=result.query,
            started_at=result.started_at,
            completed_at=result.completed_at,
            stop_reason=result.stop_reason,
            total_count=result.total_count,
            complete=result.complete,
            failure=result.failure,
            page_count=len(result.pages),
            record_count=len(result.records),
            attempt_count=len(result.attempts),
            result_sha256=result_sha256,
            result_bytes=len(canonical),
            canonical=canonical,
            binding_sha256=binding_sha256,
            binding_bytes=len(binding),
            binding=binding,
            result=result,
        )


def _row_bytes(value: object, message: str) -> bytes:
    """Return stored blob bytes, refusing a non-blob value without echoing it."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise DiscoverySearchCorruptionError(message)


# --------------------------------------------------------------------------- #
# Read-only entry point (for the backup verifier)
# --------------------------------------------------------------------------- #
class _ReadOnlyDiscoveryStoreView:
    """The minimal read-only surface the integrity sweep reads from a store."""

    __slots__ = ("connection", "project_id", "schema_version")

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.connection = conn
        self.project_id = project_id
        self.schema_version = 0


def discovery_integrity_problems(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Revalidate every recorded search observation for ``project_id`` (read-only).

    Reusable by the backup verifier. Returns a list of static, value-free problem
    descriptions (empty when every row is intact, its stable run id matches its
    project/request key, its canonical result/hash/size and binding revalidate,
    and every projected column matches the decoded result). Foreign-project rows
    and corrupt rows are reported. The connection is never written to.
    """
    repo = DiscoveryRepository(_ReadOnlyDiscoveryStoreView(conn, project_id))
    return repo.verify_all_integrity()