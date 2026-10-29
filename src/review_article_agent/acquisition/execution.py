"""Durable authorized official acquisition execution library (Step 06 slice E1).

Authority
---------

``docs/implementation/06-acquisition.md`` deliverable 1 (official/authorized
resolution waterfall, source/version provenance, missing-paper queue) and
acceptance criteria AC1 (every frozen candidate gets full text or an explicit
disposition), AC6 (no unofficial acquisition, mirror, or paywall bypass) and AC7
(storage/redistribution/hosted-transfer stay distinct from free-to-read);
``docs/specification.md`` workflow step 6 and the approvals/invariants sections;
C03 (rights are per-operation and bind the exact source revision).

What it is
----------

A trusted-caller **library** that composes the already integrated pieces for
ONE frozen candidate:

* :meth:`~review_article_agent.discovery.freeze.FreezeRepository.require_frozen`
  at the caller's exact ``freeze_ref`` (a stale/unapproved/forged freeze yields
  zero I/O);
* the frozen :class:`~review_article_agent.discovery.candidates.CandidateRepository`
  corpus, from which the candidate's **actual** ``pmid``/``pmcid``/``doi`` tokens
  are read (the caller can never inject an identifier or a URL);
* :func:`~review_article_agent.acquisition.resolution.resolve_full_text` and
  :class:`~review_article_agent.acquisition.transport.FullTextTransport` (the
  two fixed official XML endpoints, bounded retries), with the resolver's
  authorization booleans **derived here from the validated canonical
  RightsRecord** -- never from a caller-supplied flag;
* :class:`~review_article_agent.acquisition.imports.AssetImportRepository` for
  the (only) place retrieved bytes are retained, and
* :func:`~review_article_agent.acquisition.validation.validate_asset` (recomputed
  against the candidate's actual identifiers) for the observation.

The outcome is one immutable ``acquisition_retrieval`` v1 envelope stored as a
``report`` artifact revision. It pins the exact freeze revision, plus the exact
imported source revision and import-occurrence revision when bytes were
retained. It carries exact attempts/failures/provider/version/limits/status, the
validation result, the request digest, ``authorizing=False`` and
``eligibility="eligibility_not_assessed"``. It never contains source bytes,
base64 or a text preview. The only copy of the bytes is the source document
revision, and only when the validated RightsRecord allows acquisition AND
storage.

Public API (for the dependent worker/CLI task)
----------------------------------------------

``AcquisitionExecutor(store, *, registry, activation_token, lowlevel=None,
transport_options=None, stop_requested=None, before_attempt=None)``

``AcquisitionExecutor.acquire(freeze_ref, candidate_id, *, request_id,
source_artifact_id, source_revision_id, source_rights, occurrence_revision_id,
occurrence_rights, report_revision_id, report_rights, terms_evidence,
parent_revision_id=None) -> dict``

``AcquisitionExecutor.report_artifact_id(request_id) -> str`` and
``AcquisitionExecutor.occurrence_artifact_id(request_id) -> str`` (the report
and occurrence rights subjects must name these artifact ids with the caller's
revision ids), ``AcquisitionExecutor.find_request(request_id)`` and
``AcquisitionExecutor.get_report(report_ref)`` (verified reads). The request
digest is stored in the report as ``request_digest``.

Request schema (all keyword/positional arguments are explicit; nothing is
inferred or invented):

* ``freeze_ref`` -- exact ``{project_id, artifact_id, revision_id}`` of the
  ``discovery_freeze_decision`` revision.
* ``candidate_id`` -- must be in that freeze's ``included`` selection.
* ``request_id`` -- bounded opaque identifier (<= 200 chars); the durable
  replay key. A divergent request under the same key is a conflict. An
  ``interrupted`` report is also durable, so a resumed acquisition needs a NEW
  ``request_id``.
* ``source_artifact_id`` / ``source_revision_id`` / ``source_rights`` -- the
  document artifact, the exact revision to create and its canonical
  ``source_revision`` RightsRecord (acquisition AND storage ``allowed``, privacy
  denial false). The source rights are only persisted if bytes really exist.
* ``occurrence_revision_id`` / ``occurrence_rights`` -- rights (``source_revision``
  subject = ``occurrence_artifact_id(request_id)``, ``occurrence_revision_id``)
  for the import-occurrence record; storage ``allowed``.
* ``report_revision_id`` / ``report_rights`` -- the same for the retrieval
  report (subject = ``report_artifact_id(request_id)``, ``report_revision_id``).
* ``terms_evidence`` -- ``{"acquisition": {"terms_ref", "checked_at"},
  "storage": {...}}``: per-operation terms reference (must equal the validated
  rights record's basis evidence ref for that operation) and the actual UTC
  check timestamp.
* ``parent_revision_id`` -- the artifact head when the source artifact already
  has revisions.

Return value (JSON-safe, no bytes): ``{"report_ref", "replayed", "status",
"source_ref", "occurrence_ref", "disposition", "validation_status",
"attempt_count", "report"}`` where ``report`` is the verified payload.
``status`` is ``retrieved`` | ``manual_required`` | ``interrupted``.

Fencing seam
------------

``stop_requested`` (``() -> bool``) and ``before_attempt`` (``(dict) -> None``)
are **trusted Python callables** passed at construction (never data). Immediately
before **every** low-level GET (including each retry) the executor re-asserts the
exact one-project activation (a draining or stale lease stops the request), the
exact freeze (``require_frozen`` at ``freeze_ref``) and ``stop_requested()``,
and then calls ``before_attempt({"attempt_index", "source", "url"})`` so the
engine can persist a may-have-dispatched checkpoint. A refusal (or an exception
from either callable) is converted to the transport's "interrupted" signal: no
socket is opened, every already-observed attempt is preserved, and no fallback
step is contacted. If some request was observed the result is a durable
``interrupted`` report (nothing is imported); if none was, nothing is persisted
and :class:`AcquisitionExecutionBlockedError` is raised. Resolver failure
entries of the form ``attempts_exhausted`` that follow an interruption denote
steps that were refused locally.

Limitations (disclosed)
-----------------------

* The socket transport bounds connect/read *inactivity*, not total wall-clock
  time (see :mod:`~review_article_agent.acquisition.transport`); the detached
  parent worker owns lifecycle and kills.
* No lock is held across network work: the registry write guard and the SQLite
  transaction wrap only the local commit (source, rights, occurrence and report
  are one atomic transaction).
* If the local commit is rejected (for example a rights conflict discovered on
  a duplicate-byte reuse), nothing is retained and the observed attempts are not
  recorded; the error is raised and the caller may retry with a new key.
* Like the underlying transport, an instance is **not** thread-safe.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from ..domain.errors import ProjectConflict, ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..discovery.candidates import CandidateError, CandidateRepository
from ..discovery.freeze import FreezeError, FreezeNotAuthorizedError, FreezeRepository
from ..storage.artifacts import (
    ArtifactConflictError,
    ArtifactRepository,
    ArtifactRepositoryError,
    DependencyRef,
    RightsRef,
    sha256_digest,
)
from .imports import (
    AssetImportConflictError,
    AssetImportError,
    AssetImportRepository,
    _NestedStore,
)
from .resolution import MAX_REQUESTS, RESOLVER_VERSION, resolve_full_text
from .transport import (
    FullTextRequestInterrupted,
    FullTextTransport,
    FullTextTransportError,
    http_get,
)
from .validation import (
    VALIDATOR_VERSION,
    AcquisitionValidationError,
    validate_asset,
)

__all__ = [
    "REPORT_DOCUMENT_TYPE",
    "REPORT_RECORD_VERSION",
    "REPORT_STATUSES",
    "MAX_REQUEST_ID_CHARS",
    "AcquisitionExecutionError",
    "AcquisitionExecutionBlockedError",
    "AcquisitionExecutionConflictError",
    "AcquisitionExecutor",
]

REPORT_DOCUMENT_TYPE = "acquisition_retrieval"
REPORT_RECORD_VERSION = 1
REPORT_STATUSES = ("retrieved", "manual_required", "interrupted")
MAX_REQUEST_ID_CHARS = 200

_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
_MEDIA_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,60}/[a-z0-9][a-z0-9!#$&^_.+-]{0,60}$"
)
_TOKEN_NAMESPACES = ("pmid", "pmcid", "doi")
_OPS = ("acquisition", "storage")

_MESSAGES = {
    "invalid_request": "the acquisition request is invalid or out of bounds.",
    "rights_invalid": "a supplied rights record is invalid for its exact subject.",
    "rights_not_eligible": "the source rights do not allow acquisition and storage.",
    "rights_conflict": "a supplied rights record conflicts with stored rights.",
    "terms_invalid": "the source terms evidence is missing or does not match the rights.",
    "request_conflict": "request_id was already used with different arguments.",
    "identity_conflict": "a prospective artifact or revision identity is already in use.",
    "freeze_not_current": "the exact freeze is not a current authentic human-approved freeze.",
    "candidate_not_frozen": "the candidate is not in the frozen selection or corpus.",
    "not_active": "execution is not active for this project or the token is stale.",
    "interrupted": "execution was stopped before any provider request was observed.",
    "write_not_authorized": "the local commit is not authorized for this project.",
    "import_rejected": "the source import was rejected; nothing was retained.",
    "report_invalid": "the stored acquisition report failed validation.",
    "storage_failure": "the acquisition report could not be stored safely.",
}
_CONFLICT_CODES = frozenset(
    {"rights_conflict", "request_conflict", "identity_conflict"}
)
_BLOCKED_CODES = frozenset(
    {
        "freeze_not_current", "candidate_not_frozen", "not_active", "interrupted",
        "write_not_authorized",
    }
)


class AcquisitionExecutionError(ReviewAgentError):
    """An acquisition failed; carries a static ``code`` and a peer-text-free message."""

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code


class AcquisitionExecutionBlockedError(AcquisitionExecutionError):
    """The exact scope, freeze, or activation does not permit this operation."""


class AcquisitionExecutionConflictError(AcquisitionExecutionError, ArtifactConflictError):
    """The request conflicts with already-recorded immutable state."""


def _fail(code: str) -> AcquisitionExecutionError:
    if code in _CONFLICT_CODES:
        return AcquisitionExecutionConflictError(code)
    if code in _BLOCKED_CODES:
        return AcquisitionExecutionBlockedError(code)
    return AcquisitionExecutionError(code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _no_dup(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _ident(value: object) -> str:
    try:
        validate_identifier(value, "identifier")
    except (RecordValidationError, TypeError, ValueError):
        raise _fail("invalid_request") from None
    if len(value) > MAX_REQUEST_ID_CHARS:  # type: ignore[arg-type]
        raise _fail("invalid_request")
    return value  # type: ignore[return-value]


def _ref_dict(value: object, project_id: str) -> dict:
    try:
        ref = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        raise _fail("invalid_request") from None
    for part in ref.values():
        _ident(part)
    return {k: ref[k] for k in ("project_id", "artifact_id", "revision_id")}


def _safe_media(value: object) -> str | None:
    if isinstance(value, str) and _MEDIA_RE.fullmatch(value):
        return value
    return None


def _digest(material: object) -> str:
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise _fail("terms_invalid")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _fail("terms_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _fail("terms_invalid")
    return parsed


class AcquisitionExecutor:
    """Execute one authorized official acquisition for a frozen candidate."""

    def __init__(
        self,
        store,
        *,
        registry,
        activation_token,
        lowlevel=None,
        transport_options=None,
        stop_requested=None,
        before_attempt=None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._token = activation_token
        self._project_id = store.project_id
        low = http_get if lowlevel is None else lowlevel
        if not callable(low):
            raise _fail("invalid_request")
        for hook in (stop_requested, before_attempt):
            if hook is not None and not callable(hook):
                raise _fail("invalid_request")
        self._lowlevel = low
        self._stop_requested = stop_requested
        self._before_attempt = before_attempt
        options = dict(transport_options or {})
        options.pop("transport", None)  # the fenced low-level is the only one
        self._ctx: dict | None = None
        try:
            self._transport = FullTextTransport(transport=self._fenced, **options)
        except (FullTextTransportError, TypeError):
            raise _fail("invalid_request") from None
        self._artifacts = ArtifactRepository(store)

    # -- identity helpers ---------------------------------------------------- #
    def report_artifact_id(self, request_id: str) -> str:
        """Deterministic ``report`` artifact id of the retrieval report."""
        _ident(request_id)
        material = f"acquisition-retrieval\x00{self._project_id}\x00{request_id}"
        return "acr_" + hashlib.sha256(material.encode()).hexdigest()[:40]

    def occurrence_artifact_id(self, request_id: str) -> str:
        """Deterministic artifact id of the import-occurrence record."""
        return AssetImportRepository(self._store).occurrence_artifact_id(request_id)

    # -- guards -------------------------------------------------------------- #
    def _activation_ok(self) -> str | None:
        """``None`` when active for this project/token, else a static reason."""
        try:
            activation = self._registry.activation()
        except Exception:  # noqa: BLE001 - fail closed
            return "activation_unreadable"
        token = self._token
        if not (
            isinstance(token, str)
            and token
            and activation.active_project_id == self._project_id
            and secrets.compare_digest(activation.activation_token or "", token)
        ):
            return "activation_stale"
        if activation.state == "active":
            return None
        return "draining" if activation.state == "draining" else "activation_stale"

    def _freeze_ok(self, freeze_ref: dict) -> dict | None:
        try:
            return FreezeRepository(self._store).require_frozen(
                freeze_ref["artifact_id"], at_ref=freeze_ref
            )
        except (FreezeNotAuthorizedError, FreezeError):
            return None
        except Exception:  # noqa: BLE001 - fail closed
            return None

    def _fenced(self, url, *, timeout, max_response_bytes):
        """The only low-level GET: every attempt is re-fenced first."""
        ctx = self._ctx
        if ctx is None or ctx["interrupted"] is not None:
            raise FullTextRequestInterrupted("interrupted")
        reason = self._activation_ok()
        if reason is None and self._freeze_ok(ctx["freeze_ref"]) is None:
            reason = "freeze_not_current"
        if reason is None and self._stop_requested is not None:
            try:
                if self._stop_requested():
                    reason = "stop_requested"
            except Exception:  # noqa: BLE001
                reason = "callback_failed"
        if reason is None and self._before_attempt is not None:
            try:
                self._before_attempt(
                    {
                        "attempt_index": ctx["dispatched"] + 1,
                        "source": ctx["step"],
                        "url": url,
                    }
                )
            except Exception:  # noqa: BLE001
                reason = "callback_failed"
        if reason is not None:
            ctx["interrupted"] = reason
            raise FullTextRequestInterrupted("interrupted")
        ctx["dispatched"] += 1
        return self._lowlevel(
            url, timeout=timeout, max_response_bytes=max_response_bytes
        )

    # -- rights --------------------------------------------------------------- #
    def _norm_rights(self, record: object, subject: dict) -> dict:
        if not isinstance(record, dict):
            raise _fail("rights_invalid")
        try:
            return validate_rights_record(record, self._project_id, subject=subject)
        except (RecordValidationError, TypeError, ValueError):
            raise _fail("rights_invalid") from None

    def _subject(self, artifact_id: str, revision_id: str) -> dict:
        return {
            "kind": "source_revision", "project_id": self._project_id,
            "artifact_id": artifact_id, "revision_id": revision_id,
        }

    def _meta_rights(self, record, artifact_id: str, revision_id: str) -> dict:
        validated = self._norm_rights(record, self._subject(artifact_id, revision_id))
        if (
            validated["operations"]["storage"] != "allowed"
            or validated["privacy"]["denial"] is not False
        ):
            raise _fail("rights_not_eligible")
        return validated

    def _check_rights_identity(self, record: dict) -> None:
        """Fail closed on a superseded or divergent stored identity (no writes)."""
        rows = self._store.connection.execute(
            "SELECT MAX(record_revision) AS newest FROM rights_record "
            "WHERE project_id = ? AND rights_id = ?",
            (self._project_id, record["rights_id"]),
        ).fetchone()
        newest = None if rows is None else rows["newest"]
        if newest is None:
            return
        if newest > record["record_revision"]:
            raise _fail("rights_conflict")
        stored = self._artifacts.get_rights_record(
            RightsRef(self._project_id, record["rights_id"], newest)
        )
        if stored is None or stored["subject"] != record["subject"]:
            raise _fail("rights_conflict")
        if newest == record["record_revision"] and stored != record:
            raise _fail("rights_conflict")

    def _terms(self, evidence: object, source_rights: dict) -> dict:
        if not isinstance(evidence, dict) or set(evidence) != set(_OPS):
            raise _fail("terms_invalid")
        now = datetime.now(timezone.utc)
        out: dict = {}
        for op in _OPS:
            entry = evidence[op]
            if not isinstance(entry, dict) or set(entry) != {"terms_ref", "checked_at"}:
                raise _fail("terms_invalid")
            ref, checked = entry["terms_ref"], entry["checked_at"]
            expected = source_rights["basis"][op]["evidence"]["ref"]
            if (
                not isinstance(ref, str) or ref == "" or ref != expected
                or len(ref) > 512
            ):
                raise _fail("terms_invalid")
            if _parse_utc(checked) > now:
                raise _fail("terms_invalid")
            out[op] = {"terms_ref": ref, "checked_at": checked}
        return out

    # -- public: acquire ------------------------------------------------------ #
    def acquire(
        self,
        freeze_ref,
        candidate_id,
        *,
        request_id,
        source_artifact_id,
        source_revision_id,
        source_rights,
        occurrence_revision_id,
        occurrence_rights,
        report_revision_id,
        report_rights,
        terms_evidence,
        parent_revision_id=None,
    ) -> dict:
        """Acquire the official full text of one frozen candidate (see module doc)."""
        project = self._project_id
        # -- 1. pure validation: no DB state, no I/O -------------------------- #
        request_id = _ident(request_id)
        candidate_id = _ident(candidate_id)
        freeze = _ref_dict(freeze_ref, project)
        source_artifact_id = _ident(source_artifact_id)
        source_revision_id = _ident(source_revision_id)
        occurrence_revision_id = _ident(occurrence_revision_id)
        report_revision_id = _ident(report_revision_id)
        parent = None if parent_revision_id is None else _ident(parent_revision_id)
        report_id = self.report_artifact_id(request_id)
        occ_id = self.occurrence_artifact_id(request_id)
        if len({source_artifact_id, report_id, occ_id}) != 3:
            raise _fail("invalid_request")

        src = self._norm_rights(source_rights, self._subject(source_artifact_id, source_revision_id))
        if (
            src["privacy"]["denial"] is not False
            or src["operations"]["acquisition"] != "allowed"
            or src["operations"]["storage"] != "allowed"
        ):
            raise _fail("rights_not_eligible")
        occ = self._meta_rights(occurrence_rights, occ_id, occurrence_revision_id)
        rep = self._meta_rights(report_rights, report_id, report_revision_id)
        if len({src["rights_id"], occ["rights_id"], rep["rights_id"]}) != 3:
            raise _fail("invalid_request")
        terms = self._terms(terms_evidence, src)

        freeze_revision = self._artifacts.get_revision(
            freeze["artifact_id"], freeze["revision_id"]
        )
        if freeze_revision is None:
            raise _fail("freeze_not_current")
        digest = _digest(
            {
                "kind": "acquisition_retrieval",
                "project_id": project,
                "request_id": request_id,
                "freeze_ref": freeze,
                "freeze_content_sha256": freeze_revision.content_sha256,
                "candidate_id": candidate_id,
                "source_artifact_id": source_artifact_id,
                "source_revision_id": source_revision_id,
                "source_rights_sha256": sha256_digest(canonical_json(src)),
                "parent_revision_id": parent,
                "occurrence_revision_id": occurrence_revision_id,
                "occurrence_rights_sha256": sha256_digest(canonical_json(occ)),
                "report_revision_id": report_revision_id,
                "report_rights_sha256": sha256_digest(canonical_json(rep)),
                "terms_evidence": terms,
            }
        )

        # -- 2. lease + exact replay (zero I/O) -------------------------------- #
        if self._activation_ok() is not None:
            raise _fail("not_active")
        replay = self._replay(report_id, request_id, digest)
        if replay is not None:
            return replay

        # -- 3. authority: exact freeze, frozen candidate, actual identifiers --- #
        freeze_doc = self._freeze_ok(freeze)
        if freeze_doc is None:
            raise _fail("freeze_not_current")
        if candidate_id not in freeze_doc["included_candidate_ids"]:
            raise _fail("candidate_not_frozen")
        identifiers, conflicts = self._candidate_identifiers(freeze_doc, candidate_id)

        # -- 4. prospective identity / rights conflicts (still zero I/O) ------- #
        for record in (src, occ, rep):
            self._check_rights_identity(record)
        self._check_identities(
            source_artifact_id, source_revision_id, report_id, occ_id
        )

        # -- 5. network: only through the fenced transport -------------------- #
        authorization = {
            "acquisition": {
                "allowed": src["operations"]["acquisition"] == "allowed",
                **terms["acquisition"],
            },
            "storage": {
                "allowed": src["operations"]["storage"] == "allowed",
                **terms["storage"],
            },
            "privacy_denied": src["privacy"]["denial"] is not False,
        }
        self._ctx = {
            "freeze_ref": freeze, "interrupted": None, "dispatched": 0,
            "step": "europepmc_oa_fulltext_xml",
        }
        try:
            result = resolve_full_text(
                identifiers, authorization=authorization, transport=self._steps_transport()
            )
            interrupted = self._ctx["interrupted"]
        finally:
            self._ctx = None
        attempts = [self._attempt(a) for a in result["attempts"]]
        if interrupted is not None and not attempts:
            raise _fail("interrupted")

        body = None
        if result["status"] == "retrieved" and interrupted is None:
            body = base64.b64decode(result["content"].encode("ascii"))

        # -- 6. durable commit, fenced and atomic ----------------------------- #
        return self._commit(
            result=result, attempts=attempts, interrupted=interrupted, body=body,
            identifiers=identifiers, conflicts=conflicts, freeze=freeze,
            freeze_doc=freeze_doc, freeze_sha=freeze_revision.content_sha256,
            candidate_id=candidate_id, request_id=request_id, digest=digest,
            report_id=report_id, report_revision_id=report_revision_id, report_rights=rep,
            source_artifact_id=source_artifact_id, source_revision_id=source_revision_id,
            source_rights=src, parent=parent, occ_revision_id=occurrence_revision_id,
            occ_rights=occ, terms=terms,
        )

    # -- transport shim: tracks which official step is being fetched ------------ #
    def _steps_transport(self):
        outer = self

        class _Steps:
            max_attempts = outer._transport.max_attempts
            timeout = outer._transport.timeout
            max_response_bytes = outer._transport.max_response_bytes
            rate_spacing = outer._transport.rate_spacing

            def get(self, url, params=None):
                ctx = outer._ctx
                if ctx is not None:
                    ctx["step"] = (
                        "europepmc_oa_fulltext_xml"
                        if "europepmc" in url
                        else "pmc_efetch_pmc"
                    )
                return outer._transport.get(url, params)

        return _Steps()

    @staticmethod
    def _attempt(item: dict) -> dict:
        return {
            "attempt_no": item["attempt_no"],
            "requested_url": item["requested_url"],
            "requested_at": item["requested_at"],
            "completed_at": item["completed_at"],
            "status": item["status"],
            "body_bytes": item["body_bytes"],
            "body_sha256": item["body_sha256"],
            "outcome": item["outcome"],
            "media_type": _safe_media(item["media_type"]),
        }

    @staticmethod
    def _failure(item: dict) -> dict:
        out: dict = {"code": item.get("code")}
        if "source" in item:
            out["source"] = item["source"]
        if "status" in item:
            out["status"] = item["status"]
        if "media_type" in item:
            out["media_type"] = _safe_media(item["media_type"])
        return out

    # -- candidate identifiers ---------------------------------------------------- #
    def _candidate_identifiers(self, freeze_doc: dict, candidate_id: str):
        corpus = freeze_doc["payload"]["corpus"]
        try:
            snapshot = CandidateRepository(self._store).get(corpus["ref"])
        except (CandidateError, RecordValidationError, TypeError, ValueError):
            raise _fail("candidate_not_frozen") from None
        if snapshot["content_sha256"] != corpus["content_sha256"]:
            raise _fail("freeze_not_current")
        doc = next(
            (c for c in snapshot["candidates"] if c["candidate_id"] == candidate_id),
            None,
        )
        if doc is None:
            raise _fail("candidate_not_frozen")
        values: dict[str, set] = {}
        for namespace, value in doc["tokens"]:
            if namespace in _TOKEN_NAMESPACES:
                values.setdefault(namespace, set()).add(value)
        identifiers = {n: next(iter(v)) for n, v in sorted(values.items()) if len(v) == 1}
        conflicts = sorted(n for n, v in values.items() if len(v) > 1)
        return identifiers, conflicts

    def _check_identities(self, source_id, source_rev, report_id, occ_id) -> None:
        art = self._artifacts
        existing = art.get_artifact(source_id)
        if existing is not None:
            if existing.kind != "document":
                raise _fail("identity_conflict")
            if art.get_revision(source_id, source_rev) is not None:
                raise _fail("identity_conflict")
        if art.get_artifact(occ_id) is not None or art.get_artifact(report_id) is not None:
            raise _fail("identity_conflict")

    # -- commit --------------------------------------------------------------------- #
    def _commit(self, *, result, attempts, interrupted, body, identifiers, conflicts,
                freeze, freeze_doc, freeze_sha, candidate_id, request_id, digest,
                report_id, report_revision_id, report_rights, source_artifact_id,
                source_revision_id, source_rights, parent, occ_revision_id, occ_rights,
                terms) -> dict:
        store = self._store
        status = result["status"]
        if interrupted is None and body is not None:
            # Scope must still be the exact approved freeze before bytes are kept.
            if self._freeze_ok(freeze) is None:
                interrupted, body, status = "freeze_changed_before_commit", None, "interrupted"
        if interrupted is not None:
            status = "interrupted"
        validation = None
        if body is not None:
            validation = self._validate(body, identifiers, result["media_type"])
        try:
            with self._registry.execution_write_guard(
                self._token, self._project_id, allow_draining=True
            ):
                with store.transaction():
                    source_ref = occ_ref = disposition = None
                    if body is not None:
                        importer = AssetImportRepository(_NestedStore(store))
                        try:
                            imported = importer.import_asset(
                                artifact_id=source_artifact_id,
                                content=body,
                                origin_locator=result["origin_locator"],
                                rights=source_rights,
                                revision_id=source_revision_id,
                                request_id=request_id,
                                occurrence_rights=occ_rights,
                                occurrence_revision_id=occ_revision_id,
                                parent_revision_id=parent,
                            )
                        except AssetImportConflictError:
                            raise _fail("rights_conflict") from None
                        except AssetImportError:
                            raise _fail("import_rejected") from None
                        source_ref = imported["source_ref"]
                        occ_ref = imported["occurrence_ref"]
                        disposition = imported["disposition"]
                    payload = self._payload(
                        status=status, interrupted=interrupted, result=result,
                        attempts=attempts, identifiers=identifiers, conflicts=conflicts,
                        freeze=freeze, freeze_sha=freeze_sha, candidate_id=candidate_id,
                        request_id=request_id, digest=digest, terms=terms,
                        source_ref=source_ref, occ_ref=occ_ref, disposition=disposition,
                        validation=validation, body=body,
                    )
                    deps = [DependencyRef(
                        freeze["project_id"], freeze["artifact_id"],
                        freeze["revision_id"], "derived_from",
                    )]
                    for ref in (source_ref, occ_ref):
                        if ref is not None:
                            deps.append(DependencyRef(
                                ref["project_id"], ref["artifact_id"],
                                ref["revision_id"], "derived_from",
                            ))
                    repo = ArtifactRepository(_NestedStore(store))
                    try:
                        repo.create_artifact(report_id, kind="report", created_by="import")
                        repo.import_revision(
                            artifact_id=report_id,
                            content={
                                "schema_version": 1,
                                "document_type": REPORT_DOCUMENT_TYPE,
                                "payload": payload,
                            },
                            origin_locator="acquisition-retrieval:" + request_id,
                            rights=report_rights, revision_id=report_revision_id,
                            dependencies=deps, acquired_by="import",
                        )
                    except ArtifactConflictError:
                        raise _fail("identity_conflict") from None
                    except (RecordValidationError, ArtifactRepositoryError):
                        raise _fail("storage_failure") from None
        except ProjectConflict:
            raise _fail("write_not_authorized") from None
        return self._summary(
            {"project_id": self._project_id, "artifact_id": report_id,
             "revision_id": report_revision_id},
            self._load(report_id, report_revision_id), replayed=False,
        )

    def _validate(self, body: bytes, identifiers: dict, media_type) -> dict:
        try:
            return validate_asset(
                body, expected_identifiers=identifiers,
                declared_media_type=_safe_media(media_type),
            )
        except AcquisitionValidationError:
            return {"status": "not_validated", "reason": "expected_identifier_invalid"}

    def _payload(self, *, status, interrupted, result, attempts, identifiers, conflicts,
                 freeze, freeze_sha, candidate_id, request_id, digest, terms, source_ref,
                 occ_ref, disposition, validation, body) -> dict:
        retained = body is not None
        return {
            "record_type": REPORT_DOCUMENT_TYPE,
            "record_version": REPORT_RECORD_VERSION,
            "authorizing": False,
            "eligibility": "eligibility_not_assessed",
            "scientific_acceptance": "not_assessed",
            "project_id": self._project_id,
            "request_id": request_id,
            "request_digest": digest,
            "freeze_ref": freeze,
            "freeze_content_sha256": freeze_sha,
            "candidate_id": candidate_id,
            "identifiers": dict(identifiers),
            "identifier_conflicts": list(conflicts),
            "status": status,
            "interrupted_reason": interrupted,
            "resolver_version": RESOLVER_VERSION,
            "source": result["source"] if retained else None,
            "source_version": result["source_version"] if retained else None,
            "media_type": _safe_media(result["media_type"]) if retained else None,
            "origin_locator": result["origin_locator"] if retained else None,
            "content_sha256": sha256_digest(body) if retained else None,
            "content_bytes": result["content_bytes"] if retained else None,
            "attempts": attempts,
            "failures": [self._failure(f) for f in result["failures"]],
            "limits": {
                "max_requests": MAX_REQUESTS,
                "max_attempts": self._transport.max_attempts,
                "timeout_seconds": self._transport.timeout,
                "max_response_bytes": self._transport.max_response_bytes,
                "rate_spacing_seconds": self._transport.rate_spacing,
                "wall_clock_bounded": False,
            },
            "terms_evidence": terms,
            "unofficial_locator_present": bool(result["unofficial_locator_present"]),
            "source_ref": source_ref,
            "occurrence_ref": occ_ref,
            "import_disposition": disposition,
            "validation": validation,
            "validator_version": VALIDATOR_VERSION,
            "created_at": _utc_now(),
        }

    # -- reads (never trust stored JSON) --------------------------------------------- #
    def find_request(self, request_id: str) -> dict | None:
        """Return the verified historical result for ``request_id``, or ``None``."""
        report_id = self.report_artifact_id(request_id)
        if self._artifacts.get_artifact(report_id) is None:
            return None
        revisions = self._artifacts.list_revisions(report_id)
        if len(revisions) != 1:
            raise _fail("report_invalid")
        return self._summary(
            {"project_id": self._project_id, "artifact_id": report_id,
             "revision_id": revisions[0].revision_id},
            self._load(report_id, revisions[0].revision_id), replayed=True,
        )

    def get_report(self, report_ref) -> dict:
        """Load and fully re-verify one exact report revision."""
        ref = _ref_dict(report_ref, self._project_id)
        return self._summary(
            ref, self._load(ref["artifact_id"], ref["revision_id"]), replayed=True
        )

    def _replay(self, report_id: str, request_id: str, digest: str) -> dict | None:
        if self._artifacts.get_artifact(report_id) is None:
            return None
        revisions = self._artifacts.list_revisions(report_id)
        if len(revisions) != 1:
            raise _fail("request_conflict")
        payload = self._load(report_id, revisions[0].revision_id)
        if payload["request_id"] != request_id or payload["request_digest"] != digest:
            raise _fail("request_conflict")
        return self._summary(
            {"project_id": self._project_id, "artifact_id": report_id,
             "revision_id": revisions[0].revision_id},
            payload, replayed=True,
        )

    @staticmethod
    def _summary(ref: dict, payload: dict, *, replayed: bool) -> dict:
        validation = payload["validation"]
        return {
            "report_ref": ref,
            "replayed": replayed,
            "status": payload["status"],
            "source_ref": payload["source_ref"],
            "occurrence_ref": payload["occurrence_ref"],
            "disposition": payload["import_disposition"],
            "validation_status": None if validation is None else validation.get("status"),
            "attempt_count": len(payload["attempts"]),
            "report": payload,
        }

    _KEYS = frozenset({
        "record_type", "record_version", "authorizing", "eligibility",
        "scientific_acceptance", "project_id", "request_id", "request_digest",
        "freeze_ref", "freeze_content_sha256", "candidate_id", "identifiers",
        "identifier_conflicts", "status", "interrupted_reason", "resolver_version",
        "source", "source_version", "media_type", "origin_locator", "content_sha256",
        "content_bytes", "attempts", "failures", "limits", "terms_evidence",
        "unofficial_locator_present", "source_ref", "occurrence_ref",
        "import_disposition", "validation", "validator_version", "created_at",
    })
    _ATTEMPT_KEYS = frozenset({
        "attempt_no", "requested_url", "requested_at", "completed_at", "status",
        "body_bytes", "body_sha256", "outcome", "media_type",
    })

    def _load(self, report_id: str, revision_id: str) -> dict:
        bad = _fail("report_invalid")
        art, project = self._artifacts, self._project_id
        artifact = art.get_artifact(report_id)
        revision = art.get_revision(report_id, revision_id)
        if artifact is None or artifact.kind != "report" or revision is None:
            raise bad
        raw = art.read_content(report_id, revision_id)
        if sha256_digest(raw) != revision.content_sha256 or len(raw) != revision.byte_size:
            raise bad
        try:
            envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup)
            if canonical_json(envelope) != raw:
                raise bad
        except (UnicodeError, ValueError, RecordValidationError):
            raise bad from None
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema_version", "document_type", "payload"}
            or type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != 1
            or envelope["document_type"] != REPORT_DOCUMENT_TYPE
            or not isinstance(envelope["payload"], dict)
        ):
            raise bad
        p = envelope["payload"]
        try:
            self._verify(p, report_id, revision, bad)
        except (KeyError, TypeError, ValueError, AssetImportError, AcquisitionExecutionError,
                RecordValidationError, ArtifactRepositoryError):
            raise bad from None
        return p

    def _verify(self, p: dict, report_id: str, revision, bad) -> None:
        art, project = self._artifacts, self._project_id
        if (
            set(p) != self._KEYS
            or p["record_type"] != REPORT_DOCUMENT_TYPE
            or type(p["record_version"]) is not int
            or p["record_version"] != REPORT_RECORD_VERSION
            or p["authorizing"] is not False
            or p["eligibility"] != "eligibility_not_assessed"
            or p["scientific_acceptance"] != "not_assessed"
            or p["project_id"] != project
            or p["status"] not in REPORT_STATUSES
            or self.report_artifact_id(p["request_id"]) != report_id
            or not isinstance(p["request_digest"], str)
            or art.get_rights_record(revision.rights_ref) is None
        ):
            raise bad
        for item in p["attempts"]:
            if not isinstance(item, dict) or set(item) != self._ATTEMPT_KEYS:
                raise bad
        # freeze pin: exact revision and digest
        freeze = _ref_dict(p["freeze_ref"], project)
        fr = art.get_revision(freeze["artifact_id"], freeze["revision_id"])
        if fr is None or fr.content_sha256 != p["freeze_content_sha256"]:
            raise bad
        expected = {(freeze["artifact_id"], freeze["revision_id"])}
        source_ref, occ_ref = p["source_ref"], p["occurrence_ref"]
        if p["status"] == "retrieved":
            if source_ref is None or occ_ref is None or p["validation"] is None:
                raise bad
            sref, oref = _ref_dict(source_ref, project), _ref_dict(occ_ref, project)
            source = art.get_revision(sref["artifact_id"], sref["revision_id"])
            if (
                source is None
                or source.content_sha256 != p["content_sha256"]
                or source.byte_size != p["content_bytes"]
            ):
                raise bad
            data = art.read_content(sref["artifact_id"], sref["revision_id"])
            if sha256_digest(data) != p["content_sha256"]:
                raise bad
            occurrence = AssetImportRepository(self._store).get_occurrence(oref)
            if (
                occurrence.request_id != p["request_id"]
                or occurrence.source_ref is None
                or occurrence.source_ref.as_dict() != sref
                or occurrence.content_hash != p["content_sha256"]
                or occurrence.disposition != p["import_disposition"]
                or occurrence.source_revision_rights_ref != source.rights_ref
            ):
                raise bad
            if p["validator_version"] == VALIDATOR_VERSION:
                again = self._validate(data, p["identifiers"], p["media_type"])
                if again != p["validation"]:
                    raise bad
            expected |= {
                (sref["artifact_id"], sref["revision_id"]),
                (oref["artifact_id"], oref["revision_id"]),
            }
        elif (
            source_ref is not None or occ_ref is not None or p["validation"] is not None
            or p["import_disposition"] is not None or p["content_sha256"] is not None
            or p["content_bytes"] is not None or p["source"] is not None
        ):
            raise bad
        if p["status"] == "interrupted":
            if not isinstance(p["interrupted_reason"], str) or not p["attempts"]:
                raise bad
        elif p["interrupted_reason"] is not None:
            raise bad
        deps = {(d.artifact_id, d.revision_id) for d in revision.dependencies}
        if deps != expected or any(d.role != "derived_from" for d in revision.dependencies):
            raise bad
