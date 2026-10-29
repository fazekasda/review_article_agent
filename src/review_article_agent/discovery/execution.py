"""Approved-plan exact search and metadata-import execution library (Step 05, D2/D3).

Authority
---------
* ``docs/implementation/05-discovery.md`` deliverable 2 ("PubMed and Europe PMC
  adapters plus complementary sources ratified in the project plan"), deliverable
  4 ("bounded network retries/rate handling") and acceptance criteria 1, 2, 3, 4,
  and 7: seeds stay provisional; broad searches cannot execute an **unapproved**
  scope/query plan; exact queries/timestamps/sources/failures/counts are
  reconstructable; fixed fixtures cover pagination/overlap/missing IDs/partial
  failures/incremental reruns; a public result cap is never hidden as complete.
* ``docs/specification.md`` "Confirmed scientific workflow" steps 2-5 (bounded
  exploratory seed search with individual approval and provisional context; one
  approved flexible protocol + broad-search plan; documented query execution)
  and the gates/safety invariants (approvals bind exact artifact revisions;
  treat provider output as untrusted data; one execution-active project).
* The user decision of record: query execution uses **PubMed and Europe PMC
  only**, plus complementary sources **ratified per project in the approved plan
  as a ``metadata_import``**; no other API is contacted.

What this module **is**
-----------------------
A trusted-caller **library** that executes exactly what an already-approved
discovery plan authorises, one bounded operation at a time, and durably records
the resulting *untrusted provider observation*:

* :class:`DiscoveryExecutor.execute_query` — run the **exact** query at a given
  ``query_index`` of the currently-approved plan (``protocol``/``discovery.plan``
  via :func:`~review_article_agent.discovery.context.require_approved_context`).
  The caller can never override the source, query text, or page budget: they are
  read from the pinned plan payload. The plan **and** the execution activation
  are revalidated immediately before **every** low-level HTTP attempt (so a
  retry or a second page cannot continue after the plan is superseded, a typed
  dependency changes, or execution is drained/paused), and again after the
  response and before anything is persisted.
* :class:`DiscoveryExecutor.seed_search` — a **bounded exploratory** search with
  an explicit caller-chosen source/query and hard operational ceilings
  (``<= SEED_MAX_RECORDS`` records over ``<= SEED_MAX_PAGES`` page). A seed
  result is always **provisional**; no plan is inferred and no accepted context
  is created.
* :class:`DiscoveryExecutor.import_metadata` — record an already-read,
  project-approved complementary-source metadata set as an honest **metadata-only
  local observation**. The source must appear *exactly* in the approved plan's
  ``complementary_sources`` with mode ``metadata_import``. This library performs
  **no** URL download and **no** file read: the caller supplies data it has
  already read.

What this module **is not**
---------------------------
* It performs **no** approval, freeze, scientific identity, eligibility,
  deduplication, coverage, or cost decision, and it fabricates **no** rights,
  provider attempt, query translation, or completeness claim. A recorded result
  is private, inert provenance.
* It **never activates execution** (the outer worker owns activation) and it
  holds no registry/SQLite write lock across network work: the guard is taken
  only around the local commit.
* It is not thread-safe: like the underlying transport (per-instance rate
  spacing), a caller **serializes** its calls.

Durable binding decision — execution receipt artifact vs derived request id
---------------------------------------------------------------------------
The task asked to prefer a generic **execution receipt artifact** (an immutable
artifact recording the exact result ``run_id`` plus a plan/query fingerprint and
the caller request binding). That is **not safely implementable in this task**:
every :meth:`~review_article_agent.storage.artifacts.ArtifactRepository.import_revision`
path requires a rights record whose ``subject`` must bind the exact revision, and
this library is explicitly forbidden to fabricate rights; there is no
rights-free artifact path, and adding one (or a dedicated receipt table) is a
**schema/repository change outside this task's allowed files**. The concrete
split, if a receipt artifact is wanted later, is a dedicated task that adds
either a rights-free provenance artifact kind or a new additive schema table
(migration 12+) storing ``(result_run_id, plan_ref, plan_content_sha256,
query_index, query_fingerprint, request_id)``.

This task therefore takes the **sanctioned alternative**: the durable
``request_id`` replay key is **derived** from the caller's opaque request key
plus the exact plan binding (artifact id + revision id + pinned plan
``content_sha256``) plus the query index plus a stable :func:`query_fingerprint`.
Because :class:`~review_article_agent.storage.discovery.DiscoveryRepository`
derives its ``run_id`` deterministically from ``(project_id, request_id)`` and
replays the identical historical row, the observation's **identity** durably
encodes the plan revision, its content digest, the query index, and the query
parameters. The exact caller request key alone is **not** persisted as a
cross-plan uniqueness constraint: a divergent plan/query under the *same* caller
key derives a *different* identity and produces a *separate* observation. This
library does **not** claim to enforce a same-external-request-key constraint
across divergent plans; that cross-plan logical-key enforcement is the outer
durable ``JobRepository`` request key's responsibility (which fixes the plan
parameters). :func:`query_fingerprint` is exposed so a caller/worker can persist
and compare the same stable fingerprint. For
:meth:`DiscoveryExecutor.import_metadata` the derived key additionally folds in
a stable **operation fingerprint** over the exact supplied
``(source, origin_locator, observed_at, records)`` set, so a divergent import
under the same caller key is likewise a distinct observation.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from ..domain.errors import ProjectConflict, ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..storage.artifacts import ArtifactRepository
from ..storage.discovery import DiscoveryRepository, DiscoverySearchError
from .context import (
    COMPLEMENTARY_SOURCE_MODE,
    PLAN_GATE_ID,
    PLAN_QUERY_SOURCES,
    PROTOCOL_GATE_KIND,
    DiscoveryContextError,
    require_approved_context,
)
from .europepmc import EuropePMCClient
from .pubmed import PubMedClient
from .records import MetadataRecord, SearchPage, SearchResult
from .transport import (
    DiscoveryRequestInterrupted,
    DiscoveryTransport,
    http_get,
)

__all__ = [
    "QUERY_FINGERPRINT_SCHEMA_VERSION",
    "MAX_REQUEST_ID_CHARS",
    "SEED_MAX_RECORDS",
    "SEED_MAX_PAGES",
    "MAX_IMPORT_RECORDS",
    "MAX_ORIGIN_LOCATOR_CHARS",
    "METADATA_IMPORT_QUERY_PREFIX",
    "PLAN_QUERY_SOURCES",
    "METADATA_IMPORT_MODE",
    "DiscoveryExecutionError",
    "DiscoveryExecutionBlockedError",
    "query_fingerprint",
    "DiscoveryExecutor",
]

#: Version of the :func:`query_fingerprint` representation.
QUERY_FINGERPRINT_SCHEMA_VERSION = 1

#: The caller request key follows the existing bounded opaque-identifier rule.
MAX_REQUEST_ID_CHARS = 512

#: Hard operational ceilings for exploratory :meth:`DiscoveryExecutor.seed_search`.
SEED_MAX_RECORDS = 20
SEED_MAX_PAGES = 1

#: Technical bound on one metadata import (explicit refusal, never silent cap).
MAX_IMPORT_RECORDS = 100_000

#: Bound on an opaque import ``origin_locator`` string.
MAX_ORIGIN_LOCATOR_CHARS = 4096

#: The approved complementary-source mode (project-approved metadata import).
METADATA_IMPORT_MODE = COMPLEMENTARY_SOURCE_MODE

#: The exact local query descriptor an import observation carries.
METADATA_IMPORT_QUERY_PREFIX = "metadata_import:"

#: Sleep used between transport attempts (test-injectable module seam).
_SLEEP = time.sleep

_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: Exact accepted MetadataRecord field names for a strict mapping import.
_RECORD_FIELDS = (
    "source",
    "source_id",
    "pmid",
    "pmcid",
    "doi",
    "title",
    "publication_year",
)


# --------------------------------------------------------------------------- #
# Errors (static, curated, value-free)
# --------------------------------------------------------------------------- #
class DiscoveryExecutionError(ReviewAgentError):
    """An approved-plan search / metadata-import execution failed.

    Every message is static and curated: it names the field or operation but
    never echoes a query, source, identifier, locator, timestamp, or stored byte.
    """


class DiscoveryExecutionBlockedError(DiscoveryExecutionError):
    """Execution was refused because its approved scope or activation changed.

    Raised when the exact plan is missing/unapproved/stale/dependency-changed,
    when the execution activation is not ``active`` for this project (wrong
    project, stale token, or draining/paused), or when the local commit is no
    longer fenced in. It is an explicit, fail-closed blocked state: no provider
    I/O continues and no accepted observation is written by that call.
    """


_M_BLOCKED_PLAN = (
    "the exact approved discovery plan is not current and approved; execution blocked"
)
_M_INTERRUPTED_DRAIN = (
    "execution is draining for this project; further provider requests are stopped"
)
_M_BLOCKED_STATE = (
    "execution is not active for this project or the activation token is stale; blocked"
)
_M_BLOCKED_WRITE = "the local observation commit is not authorized for this project"
_M_BAD_REQUEST_ID = "request_id must be a bounded opaque identifier"
_M_BAD_PLAN_REF = "plan_ref must be an exact same-project revision reference"
_M_BAD_QUERY_INDEX = "query_index is outside the approved plan's query list"
_M_BAD_SOURCE = "source is not one of the two supported query APIs"
_M_BAD_SEED_QUERY = "query must be a non-empty query string"
_M_BAD_SEED_BOUND = "seed search budgets exceed the hard exploratory ceiling"
_M_BAD_IMPORT_SOURCE = (
    "source is not an exact approved complementary metadata-import source"
)
_M_BAD_IMPORT_RECORDS = "import records are not strict MetadataRecord data for this source"
_M_BAD_IMPORT_BOUND = "the metadata import exceeds the permitted record bound"
_M_BAD_IMPORT_TIME = "observed_at must be an RFC 3339 UTC instant"
_M_BAD_LOCATOR = "origin_locator must be a bounded non-empty opaque locator"
_M_BAD_TRANSPORT = "transport must be a callable low-level transport"
_M_BAD_STORE = "the project store identity is not readable"
_M_BAD_PLAN_QUERY = "the approved plan query cannot be executed as specified"
_M_STORE = "the discovery observation could not be read or recorded safely"


# --------------------------------------------------------------------------- #
# Strict helpers
# --------------------------------------------------------------------------- #
def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _fingerprint(material: object) -> str:
    """Canonical SHA-256 hex digest of a portable JSON material object."""
    try:
        return hashlib.sha256(canonical_json(material)).hexdigest()
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise DiscoveryExecutionError(_M_BAD_REQUEST_ID) from exc


def query_fingerprint(
    *, source: object, query: object, page_size: object, max_pages: object
) -> str:
    """Return the stable fingerprint of one exact query specification.

    The fingerprint is the canonical SHA-256 hex digest of the exact
    ``(source, query, page_size, max_pages)`` values. It is deterministic and
    reveals nothing (the material is hashed, never echoed), so a caller/worker
    may persist and compare it as the cross-call operation key. Invalid values
    (a non-portable string such as a lone surrogate) are a static
    :class:`DiscoveryExecutionError`.
    """
    return _fingerprint(
        {
            "schema_version": QUERY_FINGERPRINT_SCHEMA_VERSION,
            "source": source,
            "query": query,
            "page_size": page_size,
            "max_pages": max_pages,
        }
    )


def _derived_request_id(prefix: str, material: object) -> str:
    """A deterministic, bounded, valid opaque request id for one operation."""
    return f"{prefix}-{_fingerprint(material)}"


def _derived_run_id(project_id: str, request_id: str) -> str:
    """Mirror the repository's deterministic ``(project, request)`` run id.

    This is the documented derivation in
    :mod:`review_article_agent.storage.discovery` (``"dsr_" +
    sha256(canonical_json([project_id, request_id]))[:32]``); the executor uses it
    only to look a replay up **before** any provider I/O, never to write a row.
    """
    material = canonical_json([project_id, request_id])
    return "dsr_" + hashlib.sha256(material).hexdigest()[:32]


def _request_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "request_id")
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise DiscoveryExecutionError(_M_BAD_REQUEST_ID) from exc
    if len(normalized) > MAX_REQUEST_ID_CHARS or _has_surrogate(normalized):
        raise DiscoveryExecutionError(_M_BAD_REQUEST_ID)
    return normalized


def _require_bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryExecutionError(_M_BAD_SEED_BOUND)
    if value < minimum or value > maximum:
        raise DiscoveryExecutionError(_M_BAD_SEED_BOUND)
    return value


def _require_utc_instant(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise DiscoveryExecutionError(_M_BAD_IMPORT_TIME)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DiscoveryExecutionError(_M_BAD_IMPORT_TIME) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DiscoveryExecutionError(_M_BAD_IMPORT_TIME)
    return value


def _require_locator(value: object) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise DiscoveryExecutionError(_M_BAD_LOCATOR)
    if len(value) > MAX_ORIGIN_LOCATOR_CHARS or _has_surrogate(value):
        raise DiscoveryExecutionError(_M_BAD_LOCATOR)
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise DiscoveryExecutionError(_M_BAD_LOCATOR)
    return value


def _record_material(record: MetadataRecord) -> list:
    return [
        record.source,
        record.source_id,
        record.pmid,
        record.pmcid,
        record.doi,
        record.title,
        record.publication_year,
    ]


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #
class DiscoveryExecutor:
    """Execute approved-plan queries and bounded seed/import operations.

    Construction performs **no** I/O and does **not** activate execution; the
    outer worker owns activation and passes the current ``activation_token``.
    ``transport`` is the optional **low-level** callable used for each HTTP
    attempt (the :class:`~review_article_agent.discovery.transport.DiscoveryTransport`
    ``transport`` protocol ``(url, *, timeout, max_response_bytes)``); when
    omitted the real :func:`~review_article_agent.discovery.transport.http_get`
    is used. Tests inject a deterministic fake here — there are no ambient
    "fake mode" flags.
    """

    def __init__(self, store, *, registry, activation_token, transport=None) -> None:
        self._store = store
        self._registry = registry
        self._token = activation_token
        try:
            self._project_id = store.project_id
        except Exception as exc:  # pragma: no cover - a broken store is a caller bug
            raise DiscoveryExecutionError(_M_BAD_STORE) from exc
        lowlevel = http_get if transport is None else transport
        if not callable(lowlevel):
            raise DiscoveryExecutionError(_M_BAD_TRANSPORT)
        self._lowlevel = lowlevel

    # -- guard seams --------------------------------------------------------- #
    def _normalize_plan_ref(self, plan_ref: object) -> dict:
        try:
            return validate_ref(plan_ref, self._project_id)
        except (RecordValidationError, TypeError, ValueError) as exc:
            raise DiscoveryExecutionError(_M_BAD_PLAN_REF) from exc

    def _approved_plan(self, ref: dict) -> dict:
        """Read the exact current plan **only if** its protocol gate holds.

        Any missing, rejected, stale, superseded, dependency-changed, mistyped,
        forged, or cross-project plan is a static
        :class:`DiscoveryExecutionBlockedError` (fail-closed) with **zero**
        provider I/O.
        """
        try:
            return require_approved_context(
                self._store,
                ref,
                gate_kind=PROTOCOL_GATE_KIND,
                gate_id=PLAN_GATE_ID,
                expected_type="discovery_plan",
            )
        except DiscoveryContextError as exc:
            raise DiscoveryExecutionBlockedError(_M_BLOCKED_PLAN) from exc

    def _plan_content_digest(self, ref: dict) -> str:
        revision = ArtifactRepository(self._store).get_revision(
            ref["artifact_id"], ref["revision_id"]
        )
        if revision is None:
            raise DiscoveryExecutionBlockedError(_M_BLOCKED_PLAN)
        return revision.content_sha256

    def _assert_active(self, *, interrupt_on_draining: bool = False) -> None:
        """Fail-closed unless execution is ``active`` for this exact project.

        Reads the durable activation under the registry lock for the duration of
        the check only; it never holds the lock across network work. A wrong
        project, a stale/unknown token, or an unreadable activation is blocked.

        Before an **actual** attempt (``interrupt_on_draining=True``) a matched
        but ``draining`` activation for this exact project/token raises the opt-in
        :class:`~review_article_agent.discovery.transport.DiscoveryRequestInterrupted`
        instead of the hard block: no request is sent, yet the already-observed
        pages of an in-flight search are preserved as an explicit partial result
        (the shared transport turns it into a failed page with no fabricated
        attempt). Every other non-active state stays a hard
        :class:`DiscoveryExecutionBlockedError`.
        """
        try:
            activation = self._registry.activation()
        except Exception as exc:  # noqa: BLE001 - any registry failure is fail-closed
            raise DiscoveryExecutionBlockedError(_M_BLOCKED_STATE) from exc
        token = self._token
        token_ok = isinstance(token, str) and bool(token)
        matches_project = activation.active_project_id == self._project_id
        matches_token = token_ok and secrets.compare_digest(
            activation.activation_token or "", token
        )
        if activation.state == "active" and matches_project and matches_token:
            return
        if (
            interrupt_on_draining
            and activation.state == "draining"
            and matches_project
            and matches_token
        ):
            raise DiscoveryRequestInterrupted(_M_INTERRUPTED_DRAIN)
        raise DiscoveryExecutionBlockedError(_M_BLOCKED_STATE)

    def _guard(self, plan_ref: dict | None) -> None:
        """Revalidate the exact plan (when any) *and* the active activation.

        Called immediately before **every** low-level HTTP attempt, so changed
        scope or a pause/drain stops further attempts. Only an ``active``
        activation permits a real attempt; a matched ``draining`` one raises the
        opt-in interrupt so already-observed pages survive.
        """
        if plan_ref is not None:
            self._approved_plan(plan_ref)
        self._assert_active(interrupt_on_draining=True)

    def _guard_completion(self, plan_ref: dict | None) -> None:
        """Revalidate the exact approved plan before accepting a received result.

        The activation is **not** re-checked here: the fenced persist below uses
        the same token with ``allow_draining=True``, so a drain that began *after*
        the last request cannot discard an observation already received (no
        network happens after this point). A missing/stale/superseded/changed plan
        is still a hard :class:`DiscoveryExecutionBlockedError`, so a post-network
        scope change can never accept a scientific-workflow result — the outcome
        stays inert provenance and the caller escalates.
        """
        if plan_ref is not None:
            self._approved_plan(plan_ref)

    def _build_transport(self, plan_ref: dict | None):
        """A fresh transport whose every attempt re-runs the guard first.

        The guard raising a :class:`DiscoveryExecutionBlockedError` propagates
        through :class:`DiscoveryTransport` (only transport/network errors are
        retried) and through the provider adapters, so a blocked attempt stops
        the retry loop and any later page rather than being retried.
        """

        def lowlevel(url, *, timeout, max_response_bytes):
            self._guard(plan_ref)
            return self._lowlevel(
                url, timeout=timeout, max_response_bytes=max_response_bytes
            )

        return DiscoveryTransport(transport=lowlevel, sleep=_SLEEP)

    def _lookup_replay(self, repo: DiscoveryRepository, derived_request_id: str):
        try:
            return repo.get_search(_derived_run_id(self._project_id, derived_request_id))
        except DiscoverySearchError as exc:
            raise DiscoveryExecutionError(_M_STORE) from exc

    def _persist(self, repo: DiscoveryRepository, derived_request_id: str, result):
        """Record the observation under the fenced registry write guard.

        This is the **completion** commit: it uses the same project/token with
        ``allow_draining=True`` so a drain that began after the last request still
        commits the already-received observation (reconciliation-only; no new
        request is made here). A stale/unknown token or a different project is
        still refused by the registry guard.
        """
        try:
            with self._registry.execution_write_guard(
                self._token, self._project_id, allow_draining=True
            ):
                return repo.record_search(
                    request_id=derived_request_id, result=result
                )
        except ProjectConflict as exc:
            raise DiscoveryExecutionBlockedError(_M_BLOCKED_WRITE) from exc
        except DiscoverySearchError as exc:
            raise DiscoveryExecutionError(_M_STORE) from exc

    def _run_adapter(self, source: str, transport, query: str, page_size: int, max_pages: int):
        try:
            if source == "pubmed":
                return PubMedClient(transport=transport).search(
                    query, page_size, max_pages
                )
            if source == "europepmc":
                return EuropePMCClient(transport).search(query, page_size, max_pages)
        except DiscoveryExecutionError:
            raise
        except ReviewAgentError as exc:
            # Adapter/transport validation of the exact plan tuple failed before
            # any accepted provider observation: a static, value-free refusal.
            raise DiscoveryExecutionError(_M_BAD_PLAN_QUERY) from exc
        raise DiscoveryExecutionError(_M_BAD_SOURCE)

    # -- query execution ----------------------------------------------------- #
    def execute_query(self, plan_ref, query_index, *, request_id):
        """Execute the exact approved-plan query at ``query_index``.

        The source, exact query text, ``page_size``, and ``max_pages`` are read
        from the currently-approved pinnned plan; the caller supplies **no**
        override. The plan and activation are revalidated before every HTTP
        attempt and again before the local commit. An identical replay (same
        caller key, same plan binding, same query index) returns the original
        historical observation **without** any provider I/O.
        """
        request_id = _request_identifier(request_id)
        if isinstance(query_index, bool) or not isinstance(query_index, int):
            raise DiscoveryExecutionError(_M_BAD_QUERY_INDEX)
        if query_index < 0:
            raise DiscoveryExecutionError(_M_BAD_QUERY_INDEX)

        ref = self._normalize_plan_ref(plan_ref)
        document = self._approved_plan(ref)
        self._assert_active()

        queries = document["payload"]["queries"]
        if query_index >= len(queries):
            raise DiscoveryExecutionError(_M_BAD_QUERY_INDEX)
        spec = queries[query_index]
        source = spec["source"]
        query = spec["query"]
        page_size = spec["page_size"]
        max_pages = spec["max_pages"]

        plan_digest = self._plan_content_digest(ref)
        derived = _derived_request_id(
            "dxq",
            {
                "kind": "execute_query",
                "project_id": self._project_id,
                "caller_request_id": request_id,
                "plan_artifact_id": ref["artifact_id"],
                "plan_revision_id": ref["revision_id"],
                "plan_content_sha256": plan_digest,
                "query_index": query_index,
                "query_fingerprint": query_fingerprint(
                    source=source, query=query, page_size=page_size, max_pages=max_pages
                ),
            },
        )
        repo = DiscoveryRepository(self._store)
        replay = self._lookup_replay(repo, derived)
        if replay is not None:
            return replay

        transport = self._build_transport(ref)
        result = self._run_adapter(source, transport, query, page_size, max_pages)
        # Revalidate the approved scope after the response; a drain that began
        # mid-search still commits the received observation (drain-allowed fence).
        self._guard_completion(ref)
        return self._persist(repo, derived, result)

    # -- bounded exploratory seed search ------------------------------------- #
    def seed_search(self, source, query, *, request_id, page_size, max_pages):
        """Run a bounded, explicit exploratory seed search (always provisional).

        The caller chooses the exact source/query, but the operation is fenced by
        hard ceilings (``page_size * max_pages <= SEED_MAX_RECORDS`` and
        ``max_pages <= SEED_MAX_PAGES``) and by the execution activation. No plan
        is inferred, no accepted context is produced, and the exact query stays
        the recorded provenance.
        """
        request_id = _request_identifier(request_id)
        if source not in PLAN_QUERY_SOURCES:
            raise DiscoveryExecutionError(_M_BAD_SOURCE)
        if not isinstance(query, str) or query.strip() == "" or _has_surrogate(query):
            raise DiscoveryExecutionError(_M_BAD_SEED_QUERY)
        page_size = _require_bounded_int(page_size, minimum=1, maximum=SEED_MAX_RECORDS)
        max_pages = _require_bounded_int(max_pages, minimum=1, maximum=SEED_MAX_PAGES)
        if page_size * max_pages > SEED_MAX_RECORDS:
            raise DiscoveryExecutionError(_M_BAD_SEED_BOUND)

        self._assert_active()

        derived = _derived_request_id(
            "dxs",
            {
                "kind": "seed_search",
                "project_id": self._project_id,
                "caller_request_id": request_id,
                "source": source,
                "query_fingerprint": query_fingerprint(
                    source=source, query=query, page_size=page_size, max_pages=max_pages
                ),
            },
        )
        repo = DiscoveryRepository(self._store)
        replay = self._lookup_replay(repo, derived)
        if replay is not None:
            return replay

        transport = self._build_transport(None)
        result = self._run_adapter(source, transport, query, page_size, max_pages)
        self._guard_completion(None)
        return self._persist(repo, derived, result)

    # -- approved complementary metadata import ------------------------------ #
    def import_metadata(
        self, plan_ref, *, source, records, origin_locator, observed_at, request_id
    ):
        """Record an already-read, project-approved complementary metadata set.

        ``source`` must appear **exactly** in the approved plan's
        ``complementary_sources`` (mode ``metadata_import``). ``records`` are
        exact :class:`~review_article_agent.discovery.records.MetadataRecord`
        values or strict mappings, and every record's ``source`` must match.
        This library downloads **nothing** and reads **no** file: the caller
        supplies data it has already read.

        The observation is an honest metadata-only local observation — its query
        descriptor is ``metadata_import:<source>``, its ``requested_url`` is the
        actual opaque ``origin_locator``, its timestamps are the explicit
        ``observed_at``, it carries **no** attempt and **no** provider/scientific
        claim, and ``complete`` means only that every supplied row was retained,
        never that the source or the literature is exhausted.
        """
        request_id = _request_identifier(request_id)
        ref = self._normalize_plan_ref(plan_ref)
        document = self._approved_plan(ref)
        self._assert_active()

        approved = {
            entry["source"] for entry in document["payload"]["complementary_sources"]
        }
        if not isinstance(source, str) or source not in approved:
            raise DiscoveryExecutionBlockedError(_M_BAD_IMPORT_SOURCE)

        materialized = self._materialize_records(records, source)
        observed_at = _require_utc_instant(observed_at)
        origin_locator = _require_locator(origin_locator)

        operation_fingerprint = _fingerprint(
            {
                "kind": "metadata_import",
                "source": source,
                "origin_locator": origin_locator,
                "observed_at": observed_at,
                "records": [_record_material(record) for record in materialized],
            }
        )
        plan_digest = self._plan_content_digest(ref)
        derived = _derived_request_id(
            "dxi",
            {
                "kind": "import_metadata",
                "project_id": self._project_id,
                "caller_request_id": request_id,
                "plan_artifact_id": ref["artifact_id"],
                "plan_revision_id": ref["revision_id"],
                "plan_content_sha256": plan_digest,
                "operation_fingerprint": operation_fingerprint,
            },
        )
        repo = DiscoveryRepository(self._store)
        replay = self._lookup_replay(repo, derived)
        if replay is not None:
            return replay

        result = self._import_result(source, materialized, origin_locator, observed_at)
        self._guard_completion(ref)
        return self._persist(repo, derived, result)

    def _materialize_records(self, records, source: str) -> tuple[MetadataRecord, ...]:
        if isinstance(records, (str, bytes, bytearray)):
            raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS)
        try:
            iterator = iter(records)
        except TypeError as exc:
            raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS) from exc
        out: list[MetadataRecord] = []
        for item in iterator:
            if len(out) >= MAX_IMPORT_RECORDS:
                raise DiscoveryExecutionError(_M_BAD_IMPORT_BOUND)
            out.append(self._strict_record(item, source))
        return tuple(out)

    @staticmethod
    def _strict_record(item: object, source: str) -> MetadataRecord:
        if isinstance(item, MetadataRecord):
            record = item
        elif isinstance(item, dict):
            for key in item:
                if not isinstance(key, str) or key not in _RECORD_FIELDS:
                    raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS)
            try:
                record = MetadataRecord(**item)
            except (RecordValidationError, TypeError, ValueError) as exc:
                raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS) from exc
        else:
            raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS)
        if record.source != source:
            raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS)
        # Portable canonical encoding is required *before* the store sees the row,
        # so a non-portable string (a lone surrogate, ...) is refused here with a
        # clear import message rather than surfacing later as a storage fault.
        try:
            canonical_json(_record_material(record))
        except (RecordValidationError, TypeError, ValueError) as exc:
            raise DiscoveryExecutionError(_M_BAD_IMPORT_RECORDS) from exc
        return record

    @staticmethod
    def _import_result(
        source: str,
        records: tuple[MetadataRecord, ...],
        origin_locator: str,
        observed_at: str,
    ) -> SearchResult:
        page = SearchPage(
            source=source,
            query=METADATA_IMPORT_QUERY_PREFIX + source,
            requested_url=origin_locator,
            retrieved_at=observed_at,
            page_index=0,
            records=records,
            page_size=None,
            total_count=None,
            next_cursor=None,
            complete=True,
            stop_reason=None,
            failure=None,
            attempts=(),
        )
        return SearchResult.from_pages(
            source=source,
            query=METADATA_IMPORT_QUERY_PREFIX + source,
            pages=[page],
            started_at=observed_at,
            completed_at=observed_at,
            stop_reason="completed",
            total_count=None,
            failure=None,
        )
