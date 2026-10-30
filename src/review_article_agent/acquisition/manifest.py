"""Revision-specific acquisition manifest + fail-closed human gate (Step 06, M1).

Authority
---------

``docs/implementation/06-acquisition.md`` deliverables 3/4 and acceptance
criteria 1/2/4/5/7; ``docs/specification.md`` workflow step 6 ("acquire full
text or an explicit human disposition for **every** frozen candidate"); C01
(immutable exact revision references, a content hash is a byte-integrity key
only); C02 (an approval binds one exact artifact revision + gate kind + gate id,
authority provenance is out-of-band and a payload can never mint it); C03
``POL-P3`` (an *unknown* integrity status is not clean; retraction,
expression-of-concern, correction and non-replication stay distinct; a flagged
status needs an explicit per-use rationale but is not an automatic ban).

What it is
----------

One immutable ``report`` revision (default logical id
:data:`DEFAULT_MANIFEST_ARTIFACT_ID`) whose canonical document is
``{schema_version: 1, document_type: "acquisition_manifest", payload}``. It
covers **exactly** the roster the human-approved freeze selected
(``FreezeRepository.require_frozen(...)["included_candidate_ids"]``) with one
entry per frozen candidate. It is *inert*: the module mints no authority. The
manifest is effective only while the exact named human gate instance
``artifact_revision`` / ``acquisition.complete`` (:data:`ACQUISITION_GATE_KIND`
/ :data:`ACQUISITION_GATE_ID`) approves this exact revision through the existing
gate prepare / owner-confirmation flow. :meth:`AcquisitionRepository.require_acquired`
is **only** an acquisition prerequisite; it never authorises screening, pilot,
or extraction.

Public entry schema (the ``entries`` argument of ``propose``)
-------------------------------------------------------------

Exactly these keys per entry (anything else, including any ``approved`` /
``actor`` / ``authority`` field, is refused)::

    {
      "candidate_id": <id from the frozen roster>,
      "disposition": "use_full_text" | "exclude" | "defer" | "no_scientific_use",
      "rationale": <non-empty str>,
      "source_ref": {project_id, artifact_id, revision_id} | None,
      "human_review": None | {
          "identity_confirmed": True, "full_text_readable": True,
          "complete_article": True, "checked_at": <RFC3339 with offset>,
          "provenance": <non-empty str>, "rationale": <non-empty str>},
      "required_assets": [ {
          "asset_id": <id>, "kind": "figure"|"table"|"supplement",
          "required": <bool>, "source_ref": <ref>|None,
          "status": "available"|"unavailable"|"unknown",
          "rationale": <non-empty str>} ],
      "integrity_review": None | {
          "checked_at": <RFC3339 with offset>,
          "provenance": {"ref": <str>, "source": <str>},
          "retraction": "unknown"|"none"|"flagged",
          "expression_of_concern": ..., "correction": ..., "non_replication": ...,
          "rationale": <non-empty str>},
    }

* ``use_full_text`` needs the source document revision (kind ``document``) whose
  **actual bytes are read from the store** and re-validated with
  :func:`~review_article_agent.acquisition.validation.validate_asset` against the
  identifiers of the *actual frozen candidate* (never caller-supplied ids). A
  ``ready`` JATS satisfies syntax/identity; a ``needs_review`` source (PDF,
  missing identifiers, ...) additionally needs an explicit ``human_review`` of
  those exact pinned bytes; a ``rejected`` source (HTML/login, wrong paper,
  corrupt, identifier conflict) can **never** be waived. A human review is a
  factual statement and never proves scientific correctness.
* every ``required`` asset needs ``status == "available"`` with existing bytes
  (``source_ref``); ``unavailable`` / ``unknown`` is a **blocker** (absence is
  never negative evidence). A non-required asset never blocks.
* integrity: ``integrity_review: None`` reuses the freeze's own entry review for
  that candidate (all four statuses known by freeze construction); a given
  review replaces it (never dated before the frozen review). Any ``unknown``
  status blocks use; any ``flagged`` status needs an explicit per-use
  ``integrity_review`` rationale (the flag itself is not an automatic ban).
* ``exclude`` / ``defer`` / ``no_scientific_use`` need only a rationale; they
  must carry no source, review, asset or integrity claim (no pretend source).
  ``defer`` is an explicit human disposition, not a blocker; only
  ``use_full_text`` candidates are reported as acquired.

Persistence, re-derivation and blockers
---------------------------------------

The payload pins the freeze (ref + digest + corpus pin + included ids) and per
entry the exact source / asset pins (digest, size, media type), the expected
identifiers derived from the candidate snapshot, the full independent
validation result, and the effective integrity review. **Blockers** are derived,
persisted for the queue and re-derived on every read; a proposal that has
blockers is stored (the queue needs it) but :meth:`require_acquired` can never
succeed for it. ``dependencies`` are exactly ``derived_from`` the freeze and
every source / asset revision, and a read recomputes the whole payload from the
store and compares: a tampered document, a forged / missing dependency, or a
tampered source byte is a :class:`ManifestCorruptionError`.

``require_acquired`` additionally re-evaluates *now*: the gate is the current
authentic ``acquisition.complete`` approval of this exact revision; the freeze is
still authentic and the artifact's head; the roster is unchanged; every pinned
source / asset is still its artifact's current revision (a newer source revision
stales only the manifests that pin it); source rights still allow storage; and
no blocker remains. Replay is historical-safe: an identical ``request_id`` +
inputs returns the historical revision without touching current state, a
divergent reuse is a static :class:`ManifestConflictError`. A new proposal
re-derives everything inside the artifact store's ``BEGIN IMMEDIATE`` import
transaction (``write_precondition``) so a racing source / freeze change cannot
slip in.

Every error message is static and value-free.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..discovery.candidates import CandidateRepository
from ..discovery.freeze import (
    DEFAULT_FREEZE_ARTIFACT_ID,
    INTEGRITY_FIELDS,
    INTEGRITY_STATUSES,
    FreezeRepository,
)
from ..domain.errors import StoreError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..storage import opmemo
from ..storage.artifacts import (
    ArtifactConflictError,
    ArtifactRepository,
    Revision,
    sha256_digest,
)
from ..storage.human_approvals import HumanApprovalRepository
from .validation import (
    IDENTIFIER_NAMESPACES,
    MAX_BYTE_SIZE,
    AcquisitionValidationError,
    validate_asset,
)

__all__ = [
    "DOCUMENT_TYPE",
    "MANIFEST_ENVELOPE_SCHEMA_VERSION",
    "MANIFEST_PAYLOAD_VERSION",
    "DEFAULT_MANIFEST_ARTIFACT_ID",
    "ACQUISITION_GATE_KIND",
    "ACQUISITION_GATE_ID",
    "DISPOSITIONS",
    "USE_DISPOSITION",
    "ASSET_KINDS",
    "ASSET_STATUSES",
    "BLOCKER_CODES",
    "BLOCKER_CATEGORIES",
    "MANIFEST_NOTICE",
    "ManifestError",
    "ManifestValidationError",
    "ManifestConflictError",
    "ManifestCorruptionError",
    "ManifestNotAcquiredError",
    "AcquisitionRepository",
]

DOCUMENT_TYPE = "acquisition_manifest"
MANIFEST_ENVELOPE_SCHEMA_VERSION = 1
MANIFEST_PAYLOAD_VERSION = 1
DEFAULT_MANIFEST_ARTIFACT_ID = "acquisition.manifest"

#: The exact named human gate instance that authorises an acquisition manifest.
ACQUISITION_GATE_KIND = "artifact_revision"
ACQUISITION_GATE_ID = "acquisition.complete"

MAX_REQUEST_ID_CHARS = 512
MAX_TEXT_CHARS = 4000
_ORIGIN_PREFIX = "acquisition-manifest:"

USE_DISPOSITION = "use_full_text"
DISPOSITIONS = (USE_DISPOSITION, "exclude", "defer", "no_scientific_use")
ASSET_KINDS = ("figure", "table", "supplement")
ASSET_STATUSES = ("available", "unavailable", "unknown")

BLOCKER_CATEGORIES = ("missing", "fulltext", "visual", "supplement", "integrity")
BLOCKER_CODES = (
    "no_manifest_entry",
    "source_missing",
    "source_rejected",
    "source_superseded",
    "human_review_required",
    "candidate_identity_ambiguous",
    "integrity_unknown",
    "integrity_use_rationale_required",
    "required_asset_unavailable",
    "required_asset_unknown",
    "required_asset_bytes_missing",
    "required_asset_unreadable",
    "required_asset_rejected",
    "required_asset_superseded",
)

MANIFEST_NOTICE = (
    "Non-authorizing acquisition-manifest record: it pins one exact frozen roster "
    "and the exact source / asset revisions and explicit human dispositions for it. "
    "It asserts no scientific correctness, eligibility, or completeness, and a "
    "human review is only a factual statement; it takes effect only while the exact "
    "named human gate instance artifact_revision/acquisition.complete approves this "
    "exact revision, and even then it authorizes no screening, pilot, or extraction."
)

_SHAPE_KEYS = (
    "candidate_id",
    "disposition",
    "rationale",
    "source_ref",
    "human_review",
    "required_assets",
    "integrity_review",
)
_DERIVED_KEYS = (
    "expected_identifiers",
    "source_pin",
    "validation",
    "human_review_binding",
    "effective_integrity",
)
_ENTRY_KEYS = frozenset(_SHAPE_KEYS + _DERIVED_KEYS)
_HUMAN_REVIEW_KEYS = frozenset(
    {
        "identity_confirmed",
        "full_text_readable",
        "complete_article",
        "checked_at",
        "provenance",
        "rationale",
    }
)
_ASSET_KEYS = frozenset(
    {"asset_id", "kind", "required", "source_ref", "status", "rationale"}
)
_INTEGRITY_REVIEW_KEYS = frozenset(
    {"checked_at", "provenance", *INTEGRITY_FIELDS, "rationale"}
)
_PAYLOAD_KEYS = frozenset(
    {
        "payload_version",
        "request_id",
        "input_binding",
        "freeze",
        "entries",
        "blockers",
        "authorizing",
        "completeness_claimed",
        "notice",
    }
)
_FREEZE_KEYS = frozenset(
    {"ref", "content_sha256", "corpus_ref", "corpus_content_sha256", "included_candidate_ids"}
)
_REF_KEYS = ("project_id", "artifact_id", "revision_id")


# --------------------------------------------------------------------------- #
# Errors (static, value-free)
# --------------------------------------------------------------------------- #
class ManifestError(StoreError):
    """Base class for every acquisition-manifest failure (static messages)."""


class ManifestValidationError(ManifestError, ValueError):
    """The supplied manifest inputs are malformed, stale, or unacceptable."""


class ManifestConflictError(ManifestError):
    """A divergent reuse of an existing manifest request key."""


class ManifestCorruptionError(ManifestError):
    """A stored manifest fails a digest, envelope, derivation, or pin check."""


class ManifestNotAcquiredError(ManifestError):
    """No currently-authentic gated manifest makes acquisition complete.

    ``reasons`` is a tuple of static reason codes (never a value).
    """

    def __init__(self, message: str, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons)


_M_BAD_REQUEST_ID = "a manifest request id is not a valid bounded identifier"
_M_BAD_ARTIFACT_ID = "the manifest artifact id is not a valid identifier"
_M_BAD_REF = "the manifest reference is not a valid same-project revision reference"
_M_NOT_FOUND = "no manifest revision exists for the supplied reference"
_M_BAD_FREEZE = "the freeze reference is not a currently authentic human-approved freeze"
_M_BAD_ENTRIES = "the manifest entries are not a valid exact-key entry set"
_M_ROSTER = "the manifest entries do not cover exactly the frozen roster"
_M_BAD_ENTRY = "a manifest entry is not valid for its disposition"
_M_BAD_SOURCE = "a manifest source or asset reference is not a usable current document revision"
_M_BAD_INTEGRITY = "a manifest integrity review is not valid"
_M_BAD_INPUT = "the manifest arguments are not acceptable to the artifact store"
_M_DIVERGENT = "a different manifest is already recorded for this request key"
_M_STATE_MOVED = "the freeze or a pinned source changed while the manifest was written"
_M_CORRUPT_DIGEST = "a manifest content digest does not match its stored bytes"
_M_CORRUPT_ENVELOPE = "a manifest is not a valid versioned acquisition-manifest document"
_M_CORRUPT_CANONICAL = "a manifest is not stored as canonical bytes"
_M_CORRUPT_PAYLOAD = "a manifest payload is structurally invalid or disagrees with durable state"
_M_CORRUPT_DEPENDENCIES = "a manifest dependency closure does not exactly pin its freeze and sources"
_M_NOT_ACQUIRED = (
    "no currently active authentic human-approved acquisition manifest makes "
    "acquisition complete"
)


# --------------------------------------------------------------------------- #
# Small strict helpers
# --------------------------------------------------------------------------- #
def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _text(value: object, error, message: str) -> str:
    if (
        not isinstance(value, str)
        or _has_surrogate(value)
        or value.strip() == ""
        or len(value) > MAX_TEXT_CHARS
    ):
        raise error(message)
    return value


def _timestamp(value: object, error, message: str) -> str:
    text = _text(value, error, message)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise error(message) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise error(message)
    return text


def _parse_timestamp(text: str) -> datetime:
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    return datetime.fromisoformat(candidate)


def _identifier(value: object, name: str, error, message: str) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        raise error(message)
    try:
        return validate_identifier(value, name)
    except (RecordValidationError, TypeError, ValueError):
        raise error(message) from None


def _request_identifier(value: object) -> str:
    if isinstance(value, str) and len(value) > MAX_REQUEST_ID_CHARS:
        raise ManifestValidationError(_M_BAD_REQUEST_ID)
    return _identifier(value, "request_id", ManifestValidationError, _M_BAD_REQUEST_ID)


def _artifact_identifier(value: object, error=ManifestValidationError) -> str:
    return _identifier(value, "artifact_id", error, _M_BAD_ARTIFACT_ID)


def _ref(value: object, project_id: str, error, message: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(_REF_KEYS):
        raise error(message)
    try:
        normalized = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        raise error(message) from None
    return {key: normalized[key] for key in _REF_KEYS}


def _opt_ref(value: object, project_id: str, error, message: str):
    if value is None:
        return None
    return _ref(value, project_id, error, message)


def _origin_locator(request_id: str) -> str:
    return _ORIGIN_PREFIX + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _memo_canonical_key(value: object) -> str | None:
    """A stable string key for ``value``, or ``None`` when it is not keyable.

    Used only to key the operation-scoped validation memo.  An unkeyable
    argument simply skips memoization, so the original call runs unchanged and
    no exception type or message is altered by the key construction.
    """
    try:
        return canonical_json(value).decode("utf-8")
    except Exception:  # noqa: BLE001 - any non-canonical value just skips the memo
        return None


def _approval_summary(status) -> dict:
    return {
        "approval_id": status.approval_id,
        "status": status.status,
        "decision": status.decision,
        "active": status.is_active,
        "satisfies_named_gate": status.satisfies_named_gate,
        "gate_kind": status.gate_kind,
        "gate_id": status.gate_id,
    }


def _blocker(candidate_id: str, category: str, code: str, asset_id=None) -> dict:
    return {
        "candidate_id": candidate_id,
        "asset_id": asset_id,
        "category": category,
        "code": code,
    }


def _asset_category(kind: str) -> str:
    return "supplement" if kind == "supplement" else "visual"


# --------------------------------------------------------------------------- #
# Entry shape normalization (store-free; shared by write and read)
# --------------------------------------------------------------------------- #
def _normalize_human_review(raw: object, error) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != _HUMAN_REVIEW_KEYS:
        raise error(_M_BAD_ENTRY)
    for key in ("identity_confirmed", "full_text_readable", "complete_article"):
        if raw[key] is not True:
            raise error(_M_BAD_ENTRY)
    return {
        "identity_confirmed": True,
        "full_text_readable": True,
        "complete_article": True,
        "checked_at": _timestamp(raw["checked_at"], error, _M_BAD_ENTRY),
        "provenance": _text(raw["provenance"], error, _M_BAD_ENTRY),
        "rationale": _text(raw["rationale"], error, _M_BAD_ENTRY),
    }


def _normalize_integrity_review(raw: object, error) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != _INTEGRITY_REVIEW_KEYS:
        raise error(_M_BAD_INTEGRITY)
    provenance = raw["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"ref", "source"}:
        raise error(_M_BAD_INTEGRITY)
    out = {
        "checked_at": _timestamp(raw["checked_at"], error, _M_BAD_INTEGRITY),
        "provenance": {
            "ref": _text(provenance["ref"], error, _M_BAD_INTEGRITY),
            "source": _text(provenance["source"], error, _M_BAD_INTEGRITY),
        },
    }
    for name in INTEGRITY_FIELDS:
        if raw[name] not in INTEGRITY_STATUSES:
            raise error(_M_BAD_INTEGRITY)
        out[name] = raw[name]
    out["rationale"] = _text(raw["rationale"], error, _M_BAD_INTEGRITY)
    return out


def _normalize_asset(raw: object, project_id: str, error) -> dict:
    if not isinstance(raw, dict) or set(raw) != _ASSET_KEYS:
        raise error(_M_BAD_ENTRY)
    kind, status = raw["kind"], raw["status"]
    if kind not in ASSET_KINDS or status not in ASSET_STATUSES:
        raise error(_M_BAD_ENTRY)
    if not isinstance(raw["required"], bool):
        raise error(_M_BAD_ENTRY)
    source_ref = _opt_ref(raw["source_ref"], project_id, error, _M_BAD_ENTRY)
    if source_ref is not None and status != "available":
        raise error(_M_BAD_ENTRY)  # contradictory: bytes exist but not "available"
    return {
        "asset_id": _identifier(raw["asset_id"], "asset_id", error, _M_BAD_ENTRY),
        "kind": kind,
        "required": raw["required"],
        "source_ref": source_ref,
        "status": status,
        "rationale": _text(raw["rationale"], error, _M_BAD_ENTRY),
    }


def _normalize_shape(raw: object, project_id: str, error) -> dict:
    """Validate one entry's caller-supplied shape; enforce disposition rules."""
    if not isinstance(raw, dict) or set(raw) != set(_SHAPE_KEYS):
        raise error(_M_BAD_ENTRIES)
    disposition = raw["disposition"]
    if disposition not in DISPOSITIONS:
        raise error(_M_BAD_ENTRY)
    assets_raw = raw["required_assets"]
    if not isinstance(assets_raw, list):
        raise error(_M_BAD_ENTRY)
    assets = [_normalize_asset(item, project_id, error) for item in assets_raw]
    ids = [asset["asset_id"] for asset in assets]
    if len(set(ids)) != len(ids):
        raise error(_M_BAD_ENTRY)
    shape = {
        "candidate_id": _identifier(
            raw["candidate_id"], "candidate_id", error, _M_BAD_ENTRY
        ),
        "disposition": disposition,
        "rationale": _text(raw["rationale"], error, _M_BAD_ENTRY),
        "source_ref": _opt_ref(raw["source_ref"], project_id, error, _M_BAD_ENTRY),
        "human_review": _normalize_human_review(raw["human_review"], error),
        "required_assets": sorted(assets, key=lambda item: item["asset_id"]),
        "integrity_review": _normalize_integrity_review(
            raw["integrity_review"], error
        ),
    }
    if disposition != USE_DISPOSITION:
        # A non-use disposition carries no pretend source / review / asset claim.
        if (
            shape["source_ref"] is not None
            or shape["human_review"] is not None
            or shape["required_assets"]
            or shape["integrity_review"] is not None
        ):
            raise error(_M_BAD_ENTRY)
    elif shape["source_ref"] is None and shape["human_review"] is not None:
        raise error(_M_BAD_ENTRY)  # a review must name the exact bytes it reviewed
    return shape


def _shape_of(entry: dict) -> dict:
    shape = {key: entry[key] for key in _SHAPE_KEYS}
    assets = entry["required_assets"]
    if isinstance(assets, list):
        shape["required_assets"] = [
            {k: v for k, v in a.items() if k not in ("source_pin", "availability")} if isinstance(a, dict) else a
            for a in assets
        ]
    return shape


def _binding(freeze_ref: dict, shapes: list[dict]) -> dict:
    return {
        "freeze": {
            "artifact_id": freeze_ref["artifact_id"],
            "revision_id": freeze_ref["revision_id"],
        },
        "entries": shapes,
    }


def _flat_blockers(entries: list[dict]) -> list[dict]:
    return sorted(
        (item for entry in entries for item in entry.pop("_blockers")),
        key=lambda b: (b["candidate_id"], b["asset_id"] or "", b["code"]),
    )


def _dependency_refs(payload: dict) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    refs = [payload["freeze"]["ref"]]
    for entry in payload["entries"]:
        if entry["source_ref"] is not None:
            refs.append(entry["source_ref"])
        for asset in entry["required_assets"]:
            if asset["source_ref"] is not None:
                refs.append(asset["source_ref"])
    for ref in refs:
        seen[(ref["artifact_id"], ref["revision_id"])] = ref
    return [dict(seen[key]) for key in sorted(seen)]


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class AcquisitionRepository:
    """Durable, human-gated acquisition manifests over the frozen roster.

    Typical use::

        acq = AcquisitionRepository(store)
        rid = AcquisitionRepository.new_revision_id()
        acq.propose(
            freeze_ref, entries=[...], rights=rights_record, revision_id=rid,
            request_id="manifest-1",
        )
        # the human gates the exact revision out-of-band (prepare_gate_snapshot
        # artifact_revision/acquisition.complete + owner confirmation), then:
        acq.require_acquired()      # dict, or ManifestNotAcquiredError

    No network, provider, model, worker, or CLI actor is involved and no
    authority is minted here.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._artifacts = ArtifactRepository(store)

    @property
    def project_id(self) -> str:
        return self._store.project_id

    @staticmethod
    def new_revision_id() -> str:
        return ArtifactRepository.new_revision_id()

    # -- write path ---------------------------------------------------------- #
    def propose(
        self,
        freeze_ref,
        *,
        entries,
        rights,
        request_id,
        revision_id: str | None = None,
        artifact_id: str = DEFAULT_MANIFEST_ARTIFACT_ID,
    ) -> Revision:
        """Record (or replay) one **inert** manifest proposal over the frozen roster.

        A genuinely new proposal requires a currently authentic freeze that is
        its artifact's head, entries covering exactly the frozen roster, and every
        declared source / asset to be an actual same-project *current* document
        revision whose stored bytes match their digest. The derivation runs again
        inside the import transaction. A proposal with blockers is stored (for the
        queue) but can never satisfy :meth:`require_acquired`.
        """
        artifact_id = _artifact_identifier(artifact_id)
        request_id = _request_identifier(request_id)
        freeze_ref = _ref(freeze_ref, self.project_id, ManifestValidationError, _M_BAD_FREEZE)
        if isinstance(entries, (str, bytes, bytearray, dict)):
            raise ManifestValidationError(_M_BAD_ENTRIES)
        try:
            items = list(entries)
        except TypeError:
            raise ManifestValidationError(_M_BAD_ENTRIES) from None
        shapes = [
            _normalize_shape(item, self.project_id, ManifestValidationError)
            for item in items
        ]
        shapes.sort(key=lambda shape: shape["candidate_id"])
        if len({s["candidate_id"] for s in shapes}) != len(shapes):
            raise ManifestValidationError(_M_BAD_ENTRIES)
        binding = _binding(freeze_ref, shapes)

        self._artifacts.create_artifact(artifact_id, kind="report", created_by="import")

        existing = self._find_request(artifact_id, request_id)
        if existing is not None:
            revision, document = existing
            if document["payload"]["input_binding"] == binding:
                # Historical replay: carries no new authority, so current state is
                # deliberately not re-required.
                return revision
            raise ManifestConflictError(_M_DIVERGENT)

        payload = self._build_payload(
            freeze_ref, shapes, request_id, binding,
            current=True, error=ManifestValidationError,
        )

        def _write_precondition() -> None:
            rebuilt = self._build_payload(
                freeze_ref, shapes, request_id, binding,
                current=True, error=ManifestValidationError,
            )
            if rebuilt != payload:
                raise ManifestValidationError(_M_STATE_MOVED)

        document = {
            "schema_version": MANIFEST_ENVELOPE_SCHEMA_VERSION,
            "document_type": DOCUMENT_TYPE,
            "payload": payload,
        }
        dependencies = [
            {**ref, "role": "derived_from"} for ref in _dependency_refs(payload)
        ]
        head = self._artifacts.current_revision(artifact_id)
        try:
            return self._artifacts.import_revision(
                artifact_id=artifact_id,
                content=document,
                origin_locator=_origin_locator(request_id),
                rights=rights,
                revision_id=revision_id,
                parent_revision_id=head.revision_id if head is not None else None,
                dependencies=dependencies,
                acquired_by="import",
                request_id=request_id,
                write_precondition=_write_precondition,
            )
        except ArtifactConflictError as exc:
            raise ManifestConflictError(_M_DIVERGENT) from exc
        except RecordValidationError as exc:
            raise ManifestValidationError(_M_BAD_INPUT) from exc

    # -- read path ----------------------------------------------------------- #
    def get(self, ref: object) -> dict:
        """Read one exact manifest by explicit same-project revision reference."""
        normalized = _ref(ref, self.project_id, ManifestValidationError, _M_BAD_REF)
        revision = self._artifacts.get_revision(
            normalized["artifact_id"], normalized["revision_id"]
        )
        if revision is None:
            raise ManifestValidationError(_M_NOT_FOUND)
        return self._with_approval(self._read(revision), revision)

    def current(self, artifact_id: str = DEFAULT_MANIFEST_ARTIFACT_ID) -> dict | None:
        """The artifact's head manifest document (any approval state), or ``None``."""
        artifact_id = _artifact_identifier(artifact_id)
        revision = self._artifacts.current_revision(artifact_id)
        if revision is None:
            return None
        return self._with_approval(self._read(revision), revision)

    def history(self, artifact_id: str = DEFAULT_MANIFEST_ARTIFACT_ID) -> list[dict]:
        """Every manifest revision newest-first (earlier work is retained)."""
        artifact_id = _artifact_identifier(artifact_id)
        return [
            self._with_approval(self._read(revision), revision)
            for revision in reversed(self._artifacts.list_revisions(artifact_id))
        ]

    def queue(
        self, freeze_ref, *, artifact_id: str = DEFAULT_MANIFEST_ARTIFACT_ID
    ) -> list[dict]:
        """Read-only missing / full-text / visual / supplement blockers.

        Items are ``{candidate_id, asset_id, category, code}``. Frozen candidates
        without an entry in the head manifest, every persisted blocker, and any
        pinned source that is no longer current are listed. It writes nothing and
        grants nothing; an empty queue is **not** an acquisition approval.
        """
        artifact_id = _artifact_identifier(artifact_id)
        freeze_ref = _ref(freeze_ref, self.project_id, ManifestValidationError, _M_BAD_FREEZE)
        roster = self._authentic_freeze(freeze_ref)["included_candidate_ids"]
        items: list[dict] = []
        head = self._artifacts.current_revision(artifact_id)
        payload = None
        if head is not None:
            candidate = self._read(head)["payload"]
            if candidate["freeze"]["ref"] == freeze_ref:
                payload = candidate
        covered = (
            {entry["candidate_id"] for entry in payload["entries"]}
            if payload is not None
            else set()
        )
        for candidate_id in roster:
            if candidate_id not in covered:
                items.append(_blocker(candidate_id, "missing", "no_manifest_entry"))
        if payload is not None:
            items.extend(dict(item) for item in payload["blockers"])
            items.extend(self._superseded_items(payload))
        return sorted(
            items,
            key=lambda b: (b["candidate_id"], b["asset_id"] or "", b["code"]),
        )

    def require_acquired(
        self, artifact_id: str = DEFAULT_MANIFEST_ARTIFACT_ID, *, at_ref=None
    ) -> dict:
        """Return the manifest **only** while acquisition is currently complete.

        This is an acquisition prerequisite only: the result states
        ``authorizes_scientific_processing: False`` and no downstream route is
        implemented here. It raises :class:`ManifestNotAcquiredError` (with static
        ``reasons``) unless the exact revision carries a currently-active
        authentic ``acquisition.complete`` approval, is fully valid and
        re-derivable, its freeze is still authentic and current with an unchanged
        roster, every pinned source / asset is still current with intact bytes
        and storage rights, and no blocker remains.

        The result is a pure function of the transaction snapshot and the exact
        ``(artifact_id, at_ref)`` inputs, so within one operation it is memoized
        on those plus the connection's ``total_changes``.  A superseding/revoking
        approval, a new freeze revision, or any other write the operation makes
        changes ``total_changes`` and the gate is re-derived exactly as before
        (fail-closed); the raised :class:`ManifestNotAcquiredError` is never
        cached.
        """
        at_key = "<current>" if at_ref is None else _memo_canonical_key(at_ref)
        if at_key is None:
            return self._require_acquired(artifact_id, at_ref=at_ref)
        key = (
            "manifest.require_acquired",
            self.project_id,
            str(artifact_id),
            at_key,
        )
        return opmemo.memoize(
            self._store.connection,
            key,
            lambda: self._require_acquired(artifact_id, at_ref=at_ref),
        )

    def _require_acquired(
        self, artifact_id: str = DEFAULT_MANIFEST_ARTIFACT_ID, *, at_ref=None
    ) -> dict:
        """Uncached body of :meth:`require_acquired` (see it for the memo contract)."""
        artifact_id = _artifact_identifier(artifact_id, ManifestNotAcquiredError)
        if at_ref is None:
            revision = self._artifacts.current_revision(artifact_id)
        else:
            normalized = _ref(at_ref, self.project_id, ManifestNotAcquiredError, _M_NOT_ACQUIRED)
            revision = self._artifacts.get_revision(
                normalized["artifact_id"], normalized["revision_id"]
            )
        if revision is None:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("no_manifest",))
        try:
            document = self._read(revision)
        except ManifestError as exc:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("manifest_invalid",)) from exc

        approvals = HumanApprovalRepository(self._store)
        record = approvals.current_approval(
            revision.artifact_id,
            gate_kind=ACQUISITION_GATE_KIND,
            gate_id=ACQUISITION_GATE_ID,
        )
        if record is None or record.target["revision_id"] != revision.revision_id:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("gate_missing",))
        status = approvals.evaluate(record.approval_id)
        if not status.satisfies_named_gate:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("gate_not_active",))

        payload = document["payload"]
        freeze_ref = payload["freeze"]["ref"]
        try:
            freeze = self._authentic_freeze(freeze_ref)
        except ManifestError as exc:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("freeze_not_authentic",)) from exc
        if freeze["included_candidate_ids"] != [e["candidate_id"] for e in payload["entries"]]:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("roster_mismatch",))
        if payload["blockers"]:
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("blockers_present",))
        if self._superseded_items(payload):
            raise ManifestNotAcquiredError(_M_NOT_ACQUIRED, ("source_superseded",))

        document["approval"] = _approval_summary(status)
        document["acquired"] = True
        document["authorizes_scientific_processing"] = False
        document["acquired_candidate_ids"] = [
            e["candidate_id"] for e in payload["entries"]
            if e["disposition"] == USE_DISPOSITION
        ]
        return document

    # -- internals ----------------------------------------------------------- #
    def _with_approval(self, document: dict, revision: Revision) -> dict:
        approvals = HumanApprovalRepository(self._store)
        record = approvals.current_approval(
            revision.artifact_id,
            gate_kind=ACQUISITION_GATE_KIND,
            gate_id=ACQUISITION_GATE_ID,
        )
        if record is None or record.target["revision_id"] != revision.revision_id:
            document["approval"] = None
        else:
            document["approval"] = _approval_summary(approvals.evaluate(record.approval_id))
        return document

    def _authentic_freeze(self, freeze_ref: dict) -> dict:
        """Current authentic freeze at ``freeze_ref`` that is also its artifact head.

        Memoized per operation on the canonical ``freeze_ref`` plus the
        connection's ``total_changes``: it is a pure read of durable state, so a
        write the operation makes re-derives it exactly as before (fail-closed).
        """
        ref_key = _memo_canonical_key(freeze_ref)
        if ref_key is None:
            return self._authentic_freeze_uncached(freeze_ref)
        key = ("manifest._authentic_freeze", self.project_id, ref_key)
        return opmemo.memoize(
            self._store.connection,
            key,
            lambda: self._authentic_freeze_uncached(freeze_ref),
        )

    def _authentic_freeze_uncached(self, freeze_ref: dict) -> dict:
        """Uncached body of :meth:`_authentic_freeze`."""
        freeze = FreezeRepository(self._store)
        try:
            document = freeze.require_frozen(freeze_ref["artifact_id"], at_ref=freeze_ref)
        except Exception as exc:  # FreezeNotAuthorizedError / FreezeError
            raise ManifestValidationError(_M_BAD_FREEZE) from exc
        head = self._artifacts.current_revision(freeze_ref["artifact_id"])
        if head is None or head.revision_id != freeze_ref["revision_id"]:
            raise ManifestValidationError(_M_BAD_FREEZE)
        return document

    def _read_source(self, ref: dict, *, current: bool, error) -> tuple[Revision, bytes]:
        artifact = self._artifacts.get_artifact(ref["artifact_id"])
        revision = self._artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        if artifact is None or artifact.kind != "document" or revision is None:
            raise error(_M_BAD_SOURCE)
        if current:
            head = self._artifacts.current_revision(ref["artifact_id"])
            if head is None or head.revision_id != revision.revision_id:
                raise error(_M_BAD_SOURCE)
        content = self._artifacts.read_content(revision.artifact_id, revision.revision_id)
        if sha256_digest(content) != revision.content_sha256 or len(content) != revision.byte_size:
            raise error(_M_BAD_SOURCE)
        if revision.rights_ref.project_id != self.project_id:
            raise error(_M_BAD_SOURCE)
        rights = self._artifacts.get_rights_record(revision.rights_ref)
        if rights is None:
            raise error(_M_BAD_SOURCE)
        subject = {
            "kind": "source_revision",
            "project_id": self.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }
        try:
            validated = validate_rights_record(rights, self.project_id, subject=subject)
            require_storage_allowed(validated)
        except RecordValidationError:
            raise error(_M_BAD_SOURCE) from None
        return revision, content

    @staticmethod
    def _pin(revision: Revision) -> dict:
        return {
            "content_sha256": revision.content_sha256,
            "byte_size": revision.byte_size,
            "media_type": revision.media_type,
        }

    @staticmethod
    def _availability(content: bytes) -> str:
        """Conservative availability-only check of an asset's bytes.

        Never parses figures / images / opaque supplements; it only recognises
        known-inaccessible evidence: no bytes, over the byte cap, or an HTML /
        login response (reusing the container sniff of ``validate_asset``).
        """
        if len(content) == 0:
            return "empty"
        if len(content) > MAX_BYTE_SIZE:
            return "oversize"
        probe = content[3:] if content.startswith(b"\xef\xbb\xbf") else content
        if validate_asset(probe, expected_identifiers={})["media_kind"] == "html":
            return "html"
        return "readable"

    @staticmethod
    def _expected_identifiers(candidate: dict) -> tuple[dict, bool]:
        expected: dict[str, str] = {}
        for token in candidate["tokens"]:
            namespace, value = token[0], token[1]
            if namespace not in IDENTIFIER_NAMESPACES:
                continue
            if namespace in expected and expected[namespace] != value:
                return {}, True
            expected.setdefault(namespace, value)
        return expected, False

    def _derive_entry(self, shape: dict, candidate: dict, frozen_review: dict, *, current: bool, error) -> dict:
        entry = dict(shape)
        entry["required_assets"] = [dict(a) for a in shape["required_assets"]]
        blockers: list[dict] = []
        cid = shape["candidate_id"]
        entry.update(
            expected_identifiers={},
            source_pin=None,
            validation=None,
            human_review_binding=None,
            effective_integrity=None,
        )
        for asset in entry["required_assets"]:
            asset["source_pin"] = None
            asset["availability"] = None
        if shape["disposition"] != USE_DISPOSITION:
            entry["_blockers"] = blockers
            return entry

        # -- full-text source: actual bytes, independently validated ---------- #
        expected, ambiguous = self._expected_identifiers(candidate)
        if ambiguous:
            blockers.append(_blocker(cid, "fulltext", "candidate_identity_ambiguous"))
        entry["expected_identifiers"] = expected
        if shape["source_ref"] is None:
            blockers.append(_blocker(cid, "missing", "source_missing"))
        else:
            revision, content = self._read_source(shape["source_ref"], current=current, error=error)
            entry["source_pin"] = self._pin(revision)
            if not ambiguous:
                try:
                    result = validate_asset(
                        content,
                        expected_identifiers=expected,
                        declared_media_type=revision.media_type,
                    )
                except AcquisitionValidationError:
                    raise error(_M_BAD_SOURCE) from None
                entry["validation"] = result
                if result["status"] == "rejected":
                    blockers.append(_blocker(cid, "fulltext", "source_rejected"))
                elif result["status"] == "needs_review" and shape["human_review"] is None:
                    blockers.append(_blocker(cid, "fulltext", "human_review_required"))
            if shape["human_review"] is not None:
                entry["human_review_binding"] = {
                    "source_ref": dict(shape["source_ref"]),
                    "content_sha256": revision.content_sha256,
                }

        # -- required assets -------------------------------------------------- #
        for asset in entry["required_assets"]:
            category = _asset_category(asset["kind"])
            if asset["source_ref"] is not None:
                rev, content = self._read_source(asset["source_ref"], current=current, error=error)
                asset["source_pin"] = self._pin(rev)
                asset["availability"] = self._availability(content)
            if not asset["required"]:
                continue
            if asset["availability"] == "empty":
                blockers.append(_blocker(cid, category, "required_asset_unreadable", asset["asset_id"]))
            elif asset["availability"] in ("html", "oversize"):
                blockers.append(_blocker(cid, category, "required_asset_rejected", asset["asset_id"]))
            elif asset["status"] == "unavailable":
                blockers.append(_blocker(cid, category, "required_asset_unavailable", asset["asset_id"]))
            elif asset["status"] == "unknown":
                blockers.append(_blocker(cid, category, "required_asset_unknown", asset["asset_id"]))
            elif asset["source_ref"] is None:
                blockers.append(_blocker(cid, category, "required_asset_bytes_missing", asset["asset_id"]))

        # -- integrity (POL-P3): reuse the frozen review unless a new one is given #
        review = shape["integrity_review"]
        if review is None:
            effective = {
                "basis": "frozen_entry_review",
                **{key: frozen_review[key] for key in ("checked_at", "provenance", *INTEGRITY_FIELDS)},
                "rationale": None,
            }
        else:
            if _parse_timestamp(review["checked_at"]) < _parse_timestamp(frozen_review["checked_at"]):
                raise error(_M_BAD_INTEGRITY)
            effective = {"basis": "manifest_review", **review}
        entry["effective_integrity"] = effective
        statuses = [effective[name] for name in INTEGRITY_FIELDS]
        if "unknown" in statuses:
            blockers.append(_blocker(cid, "integrity", "integrity_unknown"))
        elif "flagged" in statuses and review is None:
            blockers.append(_blocker(cid, "integrity", "integrity_use_rationale_required"))

        entry["_blockers"] = blockers
        return entry

    def _build_payload(self, freeze_ref, shapes, request_id, binding, *, current: bool, error) -> dict:
        """Derive the full payload from durable state (shared by write and read)."""
        freeze = FreezeRepository(self._store)
        try:
            if current:
                document = self._authentic_freeze(freeze_ref)
            else:
                document = freeze.get(freeze_ref)
                if document["payload"]["decision"] != "freeze":
                    raise error(_M_BAD_FREEZE)
        except ManifestError:
            raise error(_M_BAD_FREEZE) from None
        except Exception:
            raise error(_M_BAD_FREEZE) from None
        fp = document["payload"]
        included = list(fp["selection"]["included"])
        if [s["candidate_id"] for s in shapes] != sorted(included):
            raise error(_M_ROSTER)
        try:
            snapshot = CandidateRepository(self._store).get(fp["corpus"]["ref"])
        except Exception:
            raise error(_M_BAD_FREEZE) from None
        candidates = {doc["candidate_id"]: doc for doc in snapshot["candidates"]}
        reviews = {item["candidate_id"]: item for item in fp["entry_reviews"]}
        entries = []
        for shape in shapes:
            candidate = candidates.get(shape["candidate_id"])
            if candidate is None or not candidate["resolved"] or shape["candidate_id"] not in reviews:
                raise error(_M_ROSTER)
            entries.append(
                self._derive_entry(
                    shape, candidate, reviews[shape["candidate_id"]],
                    current=current, error=error,
                )
            )
        blockers = _flat_blockers(entries)
        return {
            "payload_version": MANIFEST_PAYLOAD_VERSION,
            "request_id": request_id,
            "input_binding": binding,
            "freeze": {
                "ref": dict(freeze_ref),
                "content_sha256": document["content_sha256"],
                "corpus_ref": dict(fp["corpus"]["ref"]),
                "corpus_content_sha256": fp["corpus"]["content_sha256"],
                "included_candidate_ids": sorted(included),
            },
            "entries": entries,
            "blockers": blockers,
            "authorizing": False,
            "completeness_claimed": False,
            "notice": MANIFEST_NOTICE,
        }

    def _find_request(self, artifact_id: str, request_id: str):
        locator = _origin_locator(request_id)
        for revision in self._artifacts.list_revisions(artifact_id):
            if revision.origin_locator == locator:
                return revision, self._read(revision)
        return None

    def _read(self, revision: Revision) -> dict:
        """Decode and fully re-derive one manifest revision, memoized per operation.

        A revision is immutable and the re-derivation reads only durable state,
        so the assembled document is a pure function of the transaction snapshot
        and the exact ``(artifact_id, revision_id)``.  It is memoized on those
        plus the connection's ``total_changes``; any write the operation makes
        (a superseding revision, a savepoint rollback) misses the entry and the
        payload is re-derived exactly as before.  A raised
        :class:`ManifestCorruptionError` is never cached.
        """
        key = (
            "manifest._read",
            self.project_id,
            revision.artifact_id,
            revision.revision_id,
        )
        return opmemo.memoize(
            self._store.connection, key, lambda: self._read_uncached(revision)
        )

    def _read_uncached(self, revision: Revision) -> dict:
        """Uncached body of :meth:`_read` (see it for the memo contract)."""
        content = self._artifacts.read_content(revision.artifact_id, revision.revision_id)
        if sha256_digest(content) != revision.content_sha256 or len(content) != revision.byte_size:
            raise ManifestCorruptionError(_M_CORRUPT_DIGEST)
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ManifestCorruptionError(_M_CORRUPT_ENVELOPE) from None
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"schema_version", "document_type", "payload"}
            or type(parsed["schema_version"]) is not int
            or parsed["schema_version"] != MANIFEST_ENVELOPE_SCHEMA_VERSION
            or parsed["document_type"] != DOCUMENT_TYPE
        ):
            raise ManifestCorruptionError(_M_CORRUPT_ENVELOPE)
        try:
            if canonical_json(parsed) != content:
                raise ManifestCorruptionError(_M_CORRUPT_CANONICAL)
        except RecordValidationError:
            raise ManifestCorruptionError(_M_CORRUPT_CANONICAL) from None
        payload = self._validate_payload(parsed["payload"])

        expected = {(ref["artifact_id"], ref["revision_id"]) for ref in _dependency_refs(payload)}
        stored = revision.dependencies
        if (
            any(dep.role != "derived_from" for dep in stored)
            or any(dep.project_id != self.project_id for dep in stored)
            or {(dep.artifact_id, dep.revision_id) for dep in stored} != expected
            or len(stored) != len(expected)
        ):
            raise ManifestCorruptionError(_M_CORRUPT_DEPENDENCIES)
        return {
            "project_id": revision.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
            "content_sha256": revision.content_sha256,
            "byte_size": revision.byte_size,
            "acquired_at_utc": revision.acquired_at_utc,
            "document_type": DOCUMENT_TYPE,
            "schema_version": MANIFEST_ENVELOPE_SCHEMA_VERSION,
            "payload": payload,
        }

    def _validate_payload(self, payload: object) -> dict:
        """Re-derive the whole payload from durable state and require equality."""
        error = ManifestCorruptionError
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
            raise error(_M_CORRUPT_PAYLOAD)
        freeze = payload["freeze"]
        entries = payload["entries"]
        if (
            type(payload["payload_version"]) is not int
            or payload["payload_version"] != MANIFEST_PAYLOAD_VERSION
            or not isinstance(freeze, dict)
            or set(freeze) != _FREEZE_KEYS
            or not isinstance(entries, list)
        ):
            raise error(_M_CORRUPT_PAYLOAD)
        freeze_ref = _ref(freeze["ref"], self.project_id, error, _M_CORRUPT_PAYLOAD)
        try:
            request_id = _request_identifier(payload["request_id"])
        except ManifestValidationError:
            raise error(_M_CORRUPT_PAYLOAD) from None
        shapes = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
                raise error(_M_CORRUPT_PAYLOAD)
            shapes.append(_normalize_shape(_shape_of(entry), self.project_id, error))
        if shapes != sorted(shapes, key=lambda s: s["candidate_id"]):
            raise error(_M_CORRUPT_PAYLOAD)
        rebuilt = self._build_payload(
            freeze_ref, shapes, request_id, _binding(freeze_ref, shapes),
            current=False, error=error,
        )
        if rebuilt != payload:
            raise error(_M_CORRUPT_PAYLOAD)
        return payload

    def _superseded_items(self, payload: dict) -> list[dict]:
        items: list[dict] = []

        def is_stale(ref: dict) -> bool:
            head = self._artifacts.current_revision(ref["artifact_id"])
            return head is None or head.revision_id != ref["revision_id"]

        for entry in payload["entries"]:
            cid = entry["candidate_id"]
            if entry["source_ref"] is not None and is_stale(entry["source_ref"]):
                items.append(_blocker(cid, "fulltext", "source_superseded"))
            for asset in entry["required_assets"]:
                if asset["source_ref"] is not None and is_stale(asset["source_ref"]):
                    items.append(
                        _blocker(
                            cid, _asset_category(asset["kind"]),
                            "required_asset_superseded", asset["asset_id"],
                        )
                    )
        return items
