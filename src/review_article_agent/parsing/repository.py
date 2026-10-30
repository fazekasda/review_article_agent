"""Durable minimal parsing repository (Step 07, S7-STORE).

One bounded outcome: bind the pure Step 07 parsers (``core`` / ``pdf_adapter``)
to immutable storage.  A stored ``document`` source is parsed and its structural
result is persisted as a **distinct** ``report`` artifact revision (never an
in-place edit of the source), carrying an exact ``derived_from`` dependency on
the source revision and a caller-supplied ``RightsRecord``.  A passage locator is
bound to that exact report revision and the exact source revision, so it keeps
resolving the historical bytes after a restart while reporting a changed current
source/report head or a changed parser/normalization version **separately** --
nothing is silently rebound to the latest revision.

Authority: ``docs/implementation/07-parsing.md`` AC16/AC20/AC21/AC22 and
``docs/specification.md`` (raw assets immutable; normalized text a separate
revision; readback binds exact immutable revisions and dependencies; failed
parsing yields a human action, never an exclusion).

Not in scope: no CLI, acquisition, schema change, network/hosted approval, or
scientific-correctness claim.  A persisted report means only that structural
parsing was read; it authorizes no scientific digestion.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..storage import opmemo
from ..storage.artifacts import (
    ArtifactConflictError,
    ArtifactRepository,
    DependencyRef,
    RightsRef,
    sha256_digest,
)
from .core import (
    DOCUMENT_TYPE,
    FORMATS,
    NORMALIZATION_VERSION,
    PARSER_VERSION,
    SCHEMA_VERSION,
    STATUSES,
    normalize_text,
    parse_document,
)
from .locator import make_locator, resolve_locator
from .pdf_adapter import (
    PDF_PARSER_VERSION,
    parse_pdf,
    pdf_passages_digest,
    pypdf_installed_version,
)

#: Generic-envelope document type written for a parsed report revision.
REPORT_DOCUMENT_TYPE = DOCUMENT_TYPE

#: Version family a stored report's ``parser_version`` must belong to, by format.
_PARSER_FAMILIES = {"pdf": "step07-pdf-manual/"}
_PARSER_FAMILIES.update({fmt: "step07-jats-tei/" for fmt in ("jats", "tei", "unsupported")})

#: Static, peer-free error messages (never embed source text).
_MESSAGES = {
    "invalid_input": "parsing repository input is invalid or out of bounds.",
    "source_not_document": "source_ref does not name a stored document artifact in this project.",
    "source_not_found": "source_ref does not name an existing revision of the document.",
    "source_content_hash_mismatch": "stored source bytes do not match the revision's recorded digest.",
    "source_rights_blocked": "the source's latest rights do not permit derivative processing.",
    "report_overwrites_source": "the report must differ from the source artifact (no self-overwrite).",
    "report_not_found": "the report reference does not name an existing revision.",
    "report_kind_invalid": "the referenced revision is not a report artifact.",
    "report_content_hash_mismatch": "stored report bytes do not match the revision's recorded digest.",
    "report_envelope_invalid": "report content is not the expected versioned document envelope.",
    "report_not_canonical": "report content is not in canonical encoding.",
    "report_payload_invalid": "report payload is not a well-formed parsing result.",
    "report_source_dependency_invalid": "report lacks exactly one derived_from document dependency.",
    "report_source_hash_mismatch": "report source digest does not match the exact source bytes.",
    "report_payload_mismatch": "report payload disagrees with a deterministic reparse of the source.",
    "passage_not_found": "the requested passage id is not in the report.",
    "locator_invalid": "locator is not a bound passage locator.",
    "locator_source_mismatch": "locator source_ref does not match the report's derived source.",
}


class ParsingRepositoryError(ReviewAgentError):
    """A durable parsing/report operation failed with a stable ``code``."""

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES.get(code, f"parsing repository error: {code}"))
        self.code = code


def _err(code: str) -> ParsingRepositoryError:
    return ParsingRepositoryError(code)


def _current_versions(fmt: str) -> tuple[str, str]:
    if fmt == "pdf":
        return PDF_PARSER_VERSION, NORMALIZATION_VERSION
    return PARSER_VERSION, NORMALIZATION_VERSION


class _NestedStore:
    """Adapt a store so ``ArtifactRepository`` writes nest as SAVEPOINTs.

    The caller owns one outer ``ProjectStore.transaction`` (``BEGIN IMMEDIATE``);
    each inner repository write becomes a savepoint, so a report artifact and its
    revision are committed together or not at all.  ``ProjectStore.transaction``
    itself cannot nest, hence this adapter.
    """

    def __init__(self, store) -> None:
        self._store = store

    @property
    def project_id(self) -> str:
        return self._store.project_id

    @property
    def connection(self):
        return self._store.connection

    @contextmanager
    def transaction(self):
        conn = self._store.connection
        if not conn.in_transaction:
            with self._store.transaction():
                yield conn
            return
        name = "sp_" + uuid.uuid4().hex
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            # A savepoint rollback reverts writes without decreasing
            # ``total_changes``; drop the memo so reverted state is never
            # served from a stale entry.
            opmemo.invalidate(conn)
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")


class ParsingRepository:
    """Parse stored documents into durable report revisions, and read them back.

    ``store`` is an already-verified :class:`ProjectStore`; this class reuses
    :class:`ArtifactRepository` and the generic versioned JSON envelope and adds
    no schema.  Typical use::

        repo = ParsingRepository(store)
        report = repo.parse_stored_document(
            {"project_id": pid, "artifact_id": src_art, "revision_id": src_rev},
            report_artifact_id="art_report", report_revision_id="rev_report",
            rights=<canonical RightsRecord bound to that report revision>,
        )
        locator = repo.make_passage_locator(report["report_ref"], "p0002")
        resolved = repo.resolve_passage(locator)
    """

    def __init__(self, store) -> None:
        self._store = store
        self._repo = ArtifactRepository(_NestedStore(store))

    @property
    def project_id(self) -> str:
        return self._store.project_id

    # -- write ---------------------------------------------------------------- #
    def parse_stored_document(
        self,
        source_ref: dict,
        *,
        report_artifact_id: str,
        report_revision_id: str,
        rights: dict,
        format: str | None = None,
        inspector=None,
        parent_revision_id: str | None = None,
    ) -> dict:
        """Parse an exact stored document revision into a new report revision.

        ``source_ref`` must name a same-project ``document`` revision whose bytes
        match the recorded digest.  ``format`` (``"xml"``/``"pdf"``) overrides
        inference from media type / ``%PDF`` magic.  ``rights`` is the caller's
        canonical ``RightsRecord`` bound to the exact report revision (never
        invented here).  A later revision must name the current head as
        ``parent_revision_id``.
        """
        src = self._ref(source_ref, "source_ref")
        report_artifact_id = validate_identifier(report_artifact_id, "report_artifact_id")
        report_revision_id = validate_identifier(report_revision_id, "report_revision_id")
        if report_artifact_id == src["artifact_id"]:
            raise _err("report_overwrites_source")

        # Parse outside the write transaction: the source bytes are immutable and
        # pure parsing must not hold the database lock.
        source_bytes, revision = self._load_document_source(src)
        chosen = self._choose_format(format, source_bytes, revision)
        if chosen == "pdf":
            result = parse_pdf(source_bytes, inspector=inspector)
        else:
            digest_hex = revision.content_sha256.split(":", 1)[-1]
            result = parse_document(source_bytes, source_digest=digest_hex)

        envelope = {"schema_version": SCHEMA_VERSION, "document_type": REPORT_DOCUMENT_TYPE, "payload": result}
        deps = [DependencyRef(src["project_id"], src["artifact_id"], src["revision_id"], "derived_from")]
        try:
            # One outer BEGIN IMMEDIATE transaction: re-check the exact source's
            # LATEST rights (serialized, so they cannot be revoked between the
            # check and the write) and create the report artifact + revision
            # together.  A failure rolls back to no new artifact or revision.
            with self._store.transaction():
                self._assert_source_allows_derivative(src)
                self._repo.create_artifact(report_artifact_id, kind="report", created_by="agent")
                stored = self._repo.import_revision(
                    artifact_id=report_artifact_id,
                    revision_id=report_revision_id,
                    content=envelope,
                    origin_locator=f"parsing:{src['artifact_id']}/{src['revision_id']}",
                    rights=rights,
                    dependencies=deps,
                    acquired_by="agent",
                    parent_revision_id=parent_revision_id,
                )
        except ArtifactConflictError as exc:
            raise _err("report_overwrites_source") from exc
        except ParsingRepositoryError:
            raise
        except (RecordValidationError, ReviewAgentError) as exc:
            raise _err("report_payload_invalid") from exc

        return {
            "report_ref": {"project_id": self.project_id, "artifact_id": stored.artifact_id, "revision_id": stored.revision_id},
            "source_ref": dict(src),
            "status": result["status"],
            "human_action_required": result["human_action_required"],
            "excludes_candidate": result["excludes_candidate"],
            "parser_version": result["parser_version"],
            "normalization_version": result["normalization_version"],
            "content_sha256": stored.content_sha256,
        }

    # -- readback ------------------------------------------------------------- #
    def read_report(self, report_ref: dict) -> dict:
        """Read and fully validate one parsed report revision.

        Validates the revision digest, the canonical versioned envelope, the
        payload's internal consistency (passage text digests, no exclusion
        claim), the single exact ``derived_from`` document dependency, and the
        payload source digest against the stored source bytes.  A current-parser
        XML report is reparsed deterministically and must match; a PDF report is
        reparsed with the recorded pypdf version and its passage digests must
        match (otherwise it stays ``unverified``, or fails closed when the parser
        is available but the passages disagree).  Untrusted generic-imported bogus
        content raises a static error and is never trusted.
        """
        return self._read_validated(self._ref(report_ref, "report_ref"))

    def make_passage_locator(self, report_ref: dict, passage_id: str) -> dict:
        """Bound locator for one passage of the exact report revision."""
        record = self.read_report(report_ref)
        if not isinstance(passage_id, str) or not passage_id:
            raise _err("passage_not_found")
        try:
            core = make_locator(record["payload"], passage_id)
        except (ReviewAgentError, ValueError) as exc:
            raise _err("passage_not_found") from exc
        locator = dict(core)
        locator["report_ref"] = dict(record["report_ref"])
        locator["source_ref"] = dict(record["source_ref"])
        return locator

    def resolve_passage(self, locator: dict) -> dict:
        """Resolve a bound locator against its exact historical report revision.

        The exact report/source revisions in the locator are read (never the
        latest head), so an old locator keeps resolving historical bytes after a
        restart.  A changed current source/report head or a current
        parser/normalization difference is reported **separately** and the
        passage is never rebound.
        """
        if not isinstance(locator, dict):
            raise _err("locator_invalid")
        report_ref = self._ref(locator.get("report_ref"), "report_ref")
        source_ref = self._ref(locator.get("source_ref"), "source_ref")
        record = self._read_validated(report_ref)
        if record["source_ref"] != source_ref:
            raise _err("locator_source_mismatch")

        base = resolve_locator(locator, record["payload"])
        source_head = self._head(source_ref["artifact_id"])
        report_head = self._head(report_ref["artifact_id"])
        current_parser, current_norm = _current_versions(record["format"])
        differences: list[str] = []
        if source_head != source_ref["revision_id"]:
            differences.append("source_head_changed")
        if report_head != report_ref["revision_id"]:
            differences.append("report_head_changed")
        if record["parser_version"] != current_parser:
            differences.append("parser_version_changed")
        if record["normalization_version"] != current_norm:
            differences.append("normalization_version_changed")

        # A report produced by a parser/normalization version this build cannot
        # reproduce cannot be asserted as a resolved source/text match; it is
        # explicitly unverified while its metadata and current diff stay visible.
        verifiable = record["verifiable"]
        status = base["status"] if verifiable else "unverified"
        reasons = list(base["reasons"])
        if not verifiable:
            reasons.append("parser_version_unverified")
        return {
            "passage_id": base["passage_id"],
            "status": status,
            "reasons": reasons,
            "locator_ok": base["locator_ok"] and verifiable,
            "verifiable": verifiable,
            "citation_check": base["citation_check"],
            "report_ref": dict(record["report_ref"]),
            "source_ref": dict(record["source_ref"]),
            "parser_version": record["parser_version"],
            "normalization_version": record["normalization_version"],
            "format": record["format"],
            "historical_resolved": status == "resolved",
            "current": {
                "source_head": source_head,
                "report_head": report_head,
                "source_head_changed": source_head != source_ref["revision_id"],
                "report_head_changed": report_head != report_ref["revision_id"],
                "parser_version_changed": record["parser_version"] != current_parser,
                "normalization_version_changed": record["normalization_version"] != current_norm,
                "reasons": differences,
            },
        }

    # -- internals ------------------------------------------------------------ #
    def _ref(self, value, field: str) -> dict:
        try:
            return validate_ref(value, self.project_id)
        except RecordValidationError as exc:
            raise _err("invalid_input") from exc

    def _head(self, artifact_id: str) -> str | None:
        revision = self._repo.current_revision(artifact_id)
        return None if revision is None else revision.revision_id

    def _load_document_source(self, src: dict):
        artifact = self._repo.get_artifact(src["artifact_id"])
        if artifact is None or artifact.kind != "document":
            raise _err("source_not_document")
        revision = self._repo.get_revision(src["artifact_id"], src["revision_id"])
        if revision is None:
            raise _err("source_not_found")
        data = self._repo.read_content(src["artifact_id"], src["revision_id"])
        if sha256_digest(data) != revision.content_sha256 or len(data) != revision.byte_size:
            raise _err("source_content_hash_mismatch")
        return data, revision

    def _assert_source_allows_derivative(self, src: dict) -> None:
        """Require the source's *latest* rights to still permit derivatives.

        Run **inside** the write transaction (after ``BEGIN IMMEDIATE``), so the
        latest-rights decision is serialized against any concurrent rights change
        and cannot be revoked between this check and the report write.  Follows
        the existing latest-rights convention: fail closed if the exact source
        revision is not bound by the newest record for its ``rights_id``, or if
        privacy denial is set, or if storage is not ``allowed``.  No
        hosted-transfer/network approval is required or granted.
        """
        revision = self._repo.get_revision(src["artifact_id"], src["revision_id"])
        if revision is None:
            raise _err("source_not_found")
        rights_id = revision.rights_ref.rights_id
        row = self._store.connection.execute(
            "SELECT MAX(record_revision) AS newest FROM rights_record WHERE project_id = ? AND rights_id = ?",
            (self.project_id, rights_id),
        ).fetchone()
        if row is None or row["newest"] is None:
            raise _err("source_rights_blocked")
        record = self._repo.get_rights_record(RightsRef(self.project_id, rights_id, row["newest"]))
        exact = {"kind": "source_revision", "project_id": self.project_id,
                 "artifact_id": src["artifact_id"], "revision_id": src["revision_id"]}
        try:
            validated = validate_rights_record(record, self.project_id, subject=exact)
        except RecordValidationError as exc:
            raise _err("source_rights_blocked") from exc
        if validated["privacy"]["denial"] is not False or validated["operations"]["storage"] != "allowed":
            raise _err("source_rights_blocked")

    @staticmethod
    def _choose_format(format: str | None, data: bytes, revision) -> str:
        if format is not None:
            if format not in ("xml", "pdf"):
                raise _err("invalid_input")
            return format
        media = (revision.media_type or "").lower()
        if "pdf" in media or data.startswith(b"%PDF"):
            return "pdf"
        return "xml"

    def _read_validated(self, ref: dict) -> dict:
        artifact = self._repo.get_artifact(ref["artifact_id"])
        revision = self._repo.get_revision(ref["artifact_id"], ref["revision_id"])
        if revision is None:
            raise _err("report_not_found")
        if artifact is None or artifact.kind != "report":
            raise _err("report_kind_invalid")
        content = self._repo.read_content(ref["artifact_id"], ref["revision_id"])
        if sha256_digest(content) != revision.content_sha256 or len(content) != revision.byte_size:
            raise _err("report_content_hash_mismatch")
        payload = self._validate_payload(self._decode_envelope(content)["payload"])
        source_ref, source_bytes = self._resolve_source_dependency(revision)
        if (
            payload["source"]["sha256"] != hashlib.sha256(source_bytes).hexdigest()
            or payload["source"]["byte_size"] != len(source_bytes)
        ):
            raise _err("report_source_hash_mismatch")
        fmt = payload["format"]
        current_parser, current_norm = _current_versions(fmt)
        if fmt == "pdf":
            reproducible = self._pdf_reproducible(payload, source_bytes)
        else:
            reproducible = (
                payload["parser_version"] == PARSER_VERSION
                and payload["normalization_version"] == NORMALIZATION_VERSION
            )
            if reproducible and parse_document(source_bytes, source_digest=payload["source"]["sha256"]) != payload:
                raise _err("report_payload_mismatch")
        return {
            "project_id": self.project_id,
            "report_ref": dict(ref),
            "source_ref": source_ref,
            "content_sha256": revision.content_sha256,
            "byte_size": revision.byte_size,
            "parser_version": payload["parser_version"],
            "normalization_version": payload["normalization_version"],
            "format": fmt,
            "status": payload["status"],
            "human_action_required": payload["human_action_required"],
            "excludes_candidate": payload["excludes_candidate"],
            "verifiable": reproducible,
            "parser_version_changed": payload["parser_version"] != current_parser,
            "normalization_version_changed": payload["normalization_version"] != current_norm,
            "payload": payload,
        }

    def _pdf_reproducible(self, payload: dict, source_bytes: bytes) -> bool:
        """Reparse a PDF report and compare passages; fail closed on a mismatch.

        A PDF report is reproducible only when this build has the exact ``pypdf``
        distribution version the report recorded **and** the report was produced
        by the current PDF parser/normalization. Otherwise it stays unverified
        (never asserted as a resolved source/text match), exactly like an
        unknown-version XML report. When it *is* reproducible but a fresh reparse
        of the same bytes disagrees on the status or the passage identity/locator
        digests, the stored report is untrusted and raises a static error.
        """
        recorded = payload.get("pdf_parser_version")
        if not isinstance(recorded, str) or not recorded:
            return False
        if payload["parser_version"] != PDF_PARSER_VERSION:
            return False
        if payload["normalization_version"] != NORMALIZATION_VERSION:
            return False
        installed = pypdf_installed_version()
        if installed is None or installed != recorded:
            return False
        fresh = parse_pdf(source_bytes)
        if fresh.get("status") != payload["status"]:
            raise _err("report_payload_mismatch")
        if pdf_passages_digest(fresh.get("passages")) != pdf_passages_digest(payload["passages"]):
            raise _err("report_payload_mismatch")
        return True

    def _decode_envelope(self, content: bytes) -> dict:
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _err("report_envelope_invalid") from exc
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"schema_version", "document_type", "payload"}
            or type(parsed["schema_version"]) is not int
            or parsed["schema_version"] != SCHEMA_VERSION
            or parsed["document_type"] != REPORT_DOCUMENT_TYPE
        ):
            raise _err("report_envelope_invalid")
        try:
            if canonical_json(parsed) != content:
                raise _err("report_not_canonical")
        except RecordValidationError as exc:
            raise _err("report_not_canonical") from exc
        return parsed

    def _validate_payload(self, payload) -> dict:
        if not isinstance(payload, dict) or payload.get("document_type") != REPORT_DOCUMENT_TYPE:
            raise _err("report_payload_invalid")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != SCHEMA_VERSION:
            raise _err("report_payload_invalid")
        fmt, status = payload.get("format"), payload.get("status")
        if fmt not in FORMATS or status not in STATUSES:
            raise _err("report_payload_invalid")
        parser_version = payload.get("parser_version")
        if not isinstance(parser_version, str) or not parser_version.startswith(_PARSER_FAMILIES[fmt]):
            raise _err("report_payload_invalid")
        if not isinstance(payload.get("normalization_version"), str) or not payload["normalization_version"]:
            raise _err("report_payload_invalid")
        source = payload.get("source")
        if (
            not isinstance(source, dict)
            or set(source) != {"sha256", "byte_size"}
            or not isinstance(source["sha256"], str)
            or len(source["sha256"]) != 64
            or type(source["byte_size"]) is not int
            or source["byte_size"] < 0
        ):
            raise _err("report_payload_invalid")
        if payload.get("excludes_candidate") is not False:
            raise _err("report_payload_invalid")
        if payload.get("authorizes_scientific_processing") is not False:
            raise _err("report_payload_invalid")
        if not isinstance(payload.get("human_action_required"), bool):
            raise _err("report_payload_invalid")
        passages = payload.get("passages")
        if not isinstance(passages, list):
            raise _err("report_payload_invalid")
        for passage in passages:
            self._validate_passage(passage)
        if fmt == "pdf":
            # A PDF report carries either usable extracted text (``parsed`` with
            # page-locatored passages) or an explicit human-review state
            # (``needs_review``). It is never ``rejected``. A ``parsed`` report
            # does not itself force a per-passage human action -- its text is
            # usable original text -- but it must record the exact pypdf version
            # so read-back can reproduce it; a ``needs_review`` report still
            # requires a human action. The global
            # ``excludes_candidate``/``authorizes_scientific_processing`` checks
            # above still apply, and every passage digest is validated.
            if status not in ("parsed", "needs_review"):
                raise _err("report_payload_invalid")
            if status == "parsed":
                if payload["human_action_required"] is not False:
                    raise _err("report_payload_invalid")
                recorded = payload.get("pdf_parser_version")
                if not isinstance(recorded, str) or not recorded:
                    raise _err("report_payload_invalid")
            elif payload["human_action_required"] is not True:
                raise _err("report_payload_invalid")
        return payload

    @staticmethod
    def _validate_passage(passage) -> None:
        if not isinstance(passage, dict):
            raise _err("report_payload_invalid")
        passage_id, text, digest = passage.get("passage_id"), passage.get("text"), passage.get("text_sha256")
        if not isinstance(passage_id, str) or not passage_id:
            raise _err("report_payload_invalid")
        if not isinstance(text, str) or not isinstance(digest, str):
            raise _err("report_payload_invalid")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise _err("report_payload_invalid")
        raw = passage.get("raw_text")
        if (
            isinstance(raw, str)
            and passage.get("normalization_version") == NORMALIZATION_VERSION
            and normalize_text(raw)[0] != text
        ):
            raise _err("report_payload_invalid")

    def _resolve_source_dependency(self, revision) -> tuple[dict, bytes]:
        deps = revision.dependencies
        derived = [dep for dep in deps if dep.role == "derived_from"]
        if len(derived) != 1 or len(deps) != 1:
            raise _err("report_source_dependency_invalid")
        dep = derived[0]
        artifact = self._repo.get_artifact(dep.artifact_id)
        if dep.project_id != self.project_id or artifact is None or artifact.kind != "document":
            raise _err("report_source_dependency_invalid")
        source_revision = self._repo.get_revision(dep.artifact_id, dep.revision_id)
        if source_revision is None:
            raise _err("report_source_dependency_invalid")
        source_bytes = self._repo.read_content(dep.artifact_id, dep.revision_id)
        if sha256_digest(source_bytes) != source_revision.content_sha256:
            raise _err("report_source_hash_mismatch")
        source_ref = {"project_id": dep.project_id, "artifact_id": dep.artifact_id, "revision_id": dep.revision_id}
        return source_ref, source_bytes


__all__ = ["REPORT_DOCUMENT_TYPE", "ParsingRepository", "ParsingRepositoryError"]
