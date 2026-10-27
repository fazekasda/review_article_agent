"""Local domain validators and canonical JSON for Step 02 storage.

This module is **pure standard library** and holds no I/O, no database, and no
scientific processing.  It provides the exact-reference boundary that the storage
layer imports when it records immutable identities and canonical ``RightsRecord``
v1 documents.

Authoritative sources (read-only):

* ``docs/implementation/02-project-foundation.md`` AC2/AC3 (path/identity separation,
  traversal safety) and the immutable-asset requirement.
* ``docs/contracts/01-identities.md`` sections 2-5 (type grammar, value states,
  ``RefArtifact``/``RefRevision`` exact keys, artifact kind taxonomy).
* ``docs/contracts/03-policy-decisions.md`` section 5 (canonical ``RightsRecord``
  v1 identity/typing, status semantics, eligibility/authorization boundary).
* ``docs/decisions/002-step01-contracts-and-policies.md`` (approved behavior).

What this module **is not**
---------------------------

* It is **not** a scientific validator: passing here means *format and structural
  consistency only*, never correctness, legal clearance, ownership, or scientific
  support.
* ``validate_rights_record`` returning a record carrying an ``authorization`` entry
  **does not verify any owner authorization**.  The validator only checks that an
  authorization object is *structurally* a well-formed out-of-band binding of the
  exact subject and the exact ``record_revision``.  It neither manufactures nor
  trusts that anyone actually authenticated: an ``authorization`` value is
  untrusted data recorded by a caller, and this API **grants nothing**.
* ``require_storage_allowed`` is a fail-closed *format* gate; it is **not** a legal
  decision and does not authorize storing third-party source bytes unconditionally.

All public functions raise :class:`RecordValidationError` (a ``ValueError``) on any
problem; a record is accepted only in full, never partially.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

#: Canonical artifact kinds (C01 section 4 taxonomy).  Each artifact has exactly one.
ARTIFACT_KINDS = (
    "rubric",
    "framework",
    "outline",
    "report",
    "document",
    "study",
    "dataset",
    "experiment",
    "finding",
)

#: The four separate rights operations (C03 section 5.1).
OPERATIONS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: Canonical operation-status enum shared across the Step 01 contracts (C03 5.1).
OPERATION_STATUSES = ("allowed", "not_allowed", "unknown", "not_applicable")

#: Per-operation basis evidence kinds (C03 section 5.1).
BASIS_KINDS = (
    "license",
    "public_domain",
    "statutory",
    "publisher_permission",
    "user_authorization",
    "unknown",
)

#: Privacy confidentiality enum (C03 section 5.1).
CONFIDENTIALITY_VALUES = ("public", "confidential", "unknown")

#: Derived record statuses (computed, never trusted from input).
DERIVED_STATUSES = ("incomplete", "valid", "inconsistent")

#: Bases that can make a third-party *redistribution* or *hosted_transfer* `allowed`
#: (C03 section 5.1 rule 4: `user_authorization` cannot confer a nonexistent right).
THIRD_PARTY_LEGAL_BASES = frozenset(
    {"license", "public_domain", "publisher_permission"}
)

_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_RIGHTS_TOP_KEYS = (
    "rights_id",
    "project_id",
    "record_revision",
    "subject",
    "operations",
    "basis",
    "authorization",
    "restrictions",
    "privacy",
    "recorded_by",
    "recorded_at",
    "derived_status",
)
_AUTH_KEYS = (
    "actor",
    "method",
    "authorized_at",
    "against_subject",
    "against_rights_revision",
    "basis",
)
_PRIVACY_KEYS = ("personal_data", "confidentiality", "denial")
_EVIDENCE_KEYS = ("ref", "provenance")

# Control characters that must never appear in an opaque identifier.
_FORBIDDEN_ID_CHARS = frozenset("/\\")


class RecordValidationError(ValueError):
    """A record, identifier, reference, or JSON value failed validation."""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _raise(label, message):
    raise RecordValidationError(f"{label}: {message}" if label else message)


def _require_nonempty_str(value, field):
    if not isinstance(value, str):
        _raise(field, "must be a string")
    if value == "" or value != value.strip():
        _raise(field, "must be non-empty with no leading/trailing whitespace")
    return value


def _require_bool(value, field):
    if not isinstance(value, bool):
        _raise(field, "must be a boolean")
    return value


def _require_positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        _raise(field, "must be a positive integer (booleans are not integers here)")
    if value < 1:
        _raise(field, "must be a positive integer (>= 1)")
    return value


def _reject_unknown_keys(obj, allowed, field):
    extra = sorted(set(obj) - set(allowed))
    if extra:
        _raise(field, f"unknown field(s) not permitted: {', '.join(extra)}")


def _validate_timestamp(value, field):
    if not isinstance(value, str) or value == "":
        _raise(field, "must be an RFC 3339 timestamp string")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _raise(field, f"is not a valid RFC 3339 timestamp: {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _raise(field, "must carry an explicit timezone offset")
    return value


def _check_jsonable(value, label):
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _raise(label, "NaN and Infinity are not permitted in canonical JSON")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_jsonable(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _raise(label, f"object key {key!r} is not a string")
            _check_jsonable(item, f"{label}.{key}")
        return
    _raise(label, f"value of type {type(value).__name__} is not JSON-serializable")


# --------------------------------------------------------------------------- #
# Canonical JSON
# --------------------------------------------------------------------------- #
def canonical_json(value):
    """Return deterministic canonical JSON ``bytes`` for ``value``.

    Deterministic byte encoding (sorted keys, compact separators, UTF-8) so the
    same logical value always produces the same bytes, which the storage layer can
    hash as an immutable revision key.

    Strictly rejects anything that is not portable JSON: ``NaN``/``Infinity``
    (and non-finite floats), non-string object keys, dict keys that JSON would
    silently coerce, and any unserializable value.  Never mutates ``value``.
    """
    _check_jsonable(value, "canonical_json")
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # defensive: _check_jsonable covers these
        raise RecordValidationError(f"canonical_json: not serializable: {exc}") from exc
    return text.encode("utf-8")


# --------------------------------------------------------------------------- #
# Opaque identifiers
# --------------------------------------------------------------------------- #
def validate_identifier(value, field="identifier"):
    """Validate one opaque id and return it unchanged.

    Safe opaque ids are non-empty strings with no leading/trailing whitespace, no
    path separators (``/`` or ``\\``), no dot-traversal (``.`` / ``..`` segments),
    and no control characters.  This is the traversal/identity guard required by
    ``docs/implementation/02-project-foundation.md`` AC3: an id can never select a
    path or escape a project namespace.
    """
    if not isinstance(value, str):
        _raise(field, "must be a string")
    if value == "":
        _raise(field, "must be non-empty")
    if value != value.strip() or any(ch.isspace() for ch in value):
        _raise(field, "must not contain whitespace")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            _raise(field, "must not contain control characters")
        if ch in _FORBIDDEN_ID_CHARS:
            _raise(field, f"must not contain path separator {ch!r}")
    if value in (".", "..") or ".." in value:
        _raise(field, "must not contain dot-traversal segments")
    if value.startswith("."):
        _raise(field, "must not start with '.'")
    return value


# --------------------------------------------------------------------------- #
# Exact revision/artifact references
# --------------------------------------------------------------------------- #
def validate_ref(value, project_id):
    """Validate an exact ``RefRevision`` (``{project_id, artifact_id, revision_id}``).

    All three keys are required together and no other keys are permitted; a bare
    filename, path, hash, or title is not a reference (C01 sections 3.1-3.3).  The
    reference's ``project_id`` MUST equal the ``project_id`` argument, so a
    cross-project reference is rejected (AC3).  Returns a fresh normalized dict.
    """
    if not isinstance(value, dict):
        _raise("validate_ref", "reference must be an object")
    _reject_unknown_keys(value, _REF_KEYS, "validate_ref")
    for key in _REF_KEYS:
        if key not in value:
            _raise("validate_ref", f"missing required key {key!r}")
    ref_project = validate_identifier(value["project_id"], "project_id")
    expected = validate_identifier(project_id, "project_id (expected)")
    if ref_project != expected:
        _raise(
            "validate_ref",
            f"cross-project reference: {ref_project!r} does not match {expected!r}",
        )
    return {
        "project_id": ref_project,
        "artifact_id": validate_identifier(value["artifact_id"], "artifact_id"),
        "revision_id": validate_identifier(value["revision_id"], "revision_id"),
    }


# --------------------------------------------------------------------------- #
# RightsRecord v1
# --------------------------------------------------------------------------- #
def _validate_subject(subject, project_id):
    if not isinstance(subject, dict):
        _raise("subject", "must be an object")
    kind = subject.get("kind")
    if kind == "source_revision":
        _reject_unknown_keys(subject, ("kind", *_REF_KEYS), "subject")
        for key in _REF_KEYS:
            if key not in subject:
                _raise("subject", f"source_revision subject missing {key!r}")
        normalized = {
            "kind": "source_revision",
            "project_id": validate_identifier(subject["project_id"], "subject.project_id"),
            "artifact_id": validate_identifier(subject["artifact_id"], "subject.artifact_id"),
            "revision_id": validate_identifier(subject["revision_id"], "subject.revision_id"),
        }
    elif kind == "subject_artifact_ref":
        _reject_unknown_keys(
            subject, ("kind", "project_id", "artifact_id"), "subject"
        )
        for key in ("project_id", "artifact_id"):
            if key not in subject:
                _raise("subject", f"subject_artifact_ref subject missing {key!r}")
        normalized = {
            "kind": "subject_artifact_ref",
            "project_id": validate_identifier(subject["project_id"], "subject.project_id"),
            "artifact_id": validate_identifier(subject["artifact_id"], "subject.artifact_id"),
        }
    else:
        _raise("subject", f"kind {kind!r} is not the canonical discriminated union")
    if normalized["project_id"] != project_id:
        _raise(
            "subject",
            f"cross-project subject: {normalized['project_id']!r} does not match {project_id!r}",
        )
    return normalized


def _validate_evidence(evidence, field, require_nonempty):
    if not isinstance(evidence, dict):
        _raise(field, "must be an object with 'ref' and 'provenance'")
    _reject_unknown_keys(evidence, _EVIDENCE_KEYS, field)
    for key in _EVIDENCE_KEYS:
        if key not in evidence:
            _raise(field, f"missing {key!r}")
    ref = evidence["ref"]
    provenance = evidence["provenance"]
    if not isinstance(ref, str) or not isinstance(provenance, str):
        _raise(field, "'ref' and 'provenance' must be strings")
    if require_nonempty and (ref == "" or provenance == ""):
        _raise(field, "an 'allowed' operation needs non-empty 'ref' and 'provenance'")
    return {"ref": ref, "provenance": provenance}


def _validate_basis_entry(basis, operation, status):
    field = f"basis.{operation}"
    if not isinstance(basis, dict):
        _raise(field, "must be a typed object {kind, evidence}")
    _reject_unknown_keys(basis, ("kind", "evidence"), field)
    if "kind" not in basis:
        _raise(field, "missing 'kind'")
    kind = basis["kind"]
    if kind not in BASIS_KINDS:
        _raise(field, f"kind {kind!r} is not a canonical basis kind")
    require_nonempty = status == "allowed"
    if status == "allowed":
        if kind == "unknown":
            _raise(field, "an 'allowed' operation cannot rest on an 'unknown' basis")
        if operation in ("redistribution", "hosted_transfer") and (
            kind not in THIRD_PARTY_LEGAL_BASES
        ):
            _raise(
                field,
                f"{operation!r} cannot be 'allowed' on a {kind!r} basis; "
                f"it needs one of {sorted(THIRD_PARTY_LEGAL_BASES)}",
            )
    evidence = _validate_evidence(
        basis.get("evidence"), f"{field}.evidence", require_nonempty
    )
    return {"kind": kind, "evidence": evidence}


def _validate_authorization(auth, subject, record_revision):
    if not isinstance(auth, dict):
        _raise("authorization", "must be an object or null")
    _reject_unknown_keys(auth, _AUTH_KEYS, "authorization")
    for key in _AUTH_KEYS:
        if key not in auth:
            _raise("authorization", f"missing {key!r}")
    actor = _require_nonempty_str(auth["actor"], "authorization.actor")
    if auth["method"] != "out_of_band_authenticated":
        _raise(
            "authorization",
            "method must be 'out_of_band_authenticated' (in-band recording grants nothing)",
        )
    authorized_at = _validate_timestamp(auth["authorized_at"], "authorization.authorized_at")
    against_subject = _validate_subject(auth["against_subject"], subject["project_id"])
    if against_subject != subject:
        _raise("authorization", "must bind the exact source subject")
    against_revision = _require_positive_int(
        auth["against_rights_revision"], "authorization.against_rights_revision"
    )
    if against_revision != record_revision:
        _raise(
            "authorization",
            "stale binding: must bind this record's exact record_revision",
        )
    basis = auth["basis"]
    if not isinstance(basis, dict):
        _raise("authorization.basis", "must be a typed object {kind, evidence}")
    _reject_unknown_keys(basis, ("kind", "evidence"), "authorization.basis")
    if basis.get("kind") not in BASIS_KINDS:
        _raise("authorization.basis", f"kind {basis.get('kind')!r} is not canonical")
    if basis.get("kind") == "unknown":
        _raise("authorization.basis", "an authorization basis cannot be 'unknown'")
    evidence = _validate_evidence(
        basis.get("evidence"), "authorization.basis.evidence", True
    )
    return {
        "actor": actor,
        "method": "out_of_band_authenticated",
        "authorized_at": authorized_at,
        "against_subject": against_subject,
        "against_rights_revision": against_revision,
        "basis": {"kind": basis["kind"], "evidence": evidence},
    }


def validate_rights_record(record, project_id, subject=None):
    """Validate a canonical ``RightsRecord`` v1 and return a normalized deep copy.

    Accepted **only in full**: any missing or ill-typed mandatory field, any
    bool-as-int, NaN/Infinity, unknown field (top-level or nested), cross-project
    subject/evidence, stale authorization binding, or inconsistent operation/basis
    combination raises :class:`RecordValidationError` and authorizes nothing.

    ``derived_status`` is **computed** from the validated record; a caller-supplied
    value is never trusted to override it.  Because this validator returns only
    fully valid records, the returned record's ``derived_status`` is ``"valid"``;
    an input ``derived_status`` of ``incomplete``/``inconsistent`` (raw/diagnostic
    input per C03 section 5.1) is rejected rather than laundered into a valid record.

    ``subject``, when given, must equal the record's subject exactly.

    The returned object is a **new** structure, so mutating the input afterwards
    cannot alter it.  It grants **nothing**: an ``authorization`` entry is only
    structurally checked, and this function neither performs nor verifies owner
    authentication or any legal/scientific decision.
    """
    if not isinstance(record, dict):
        _raise("rights_record", "must be a JSON object")
    project_id = validate_identifier(project_id, "project_id (expected)")
    _reject_unknown_keys(record, _RIGHTS_TOP_KEYS, "rights_record")

    for key in _RIGHTS_TOP_KEYS:
        if key not in record:
            _raise("rights_record", f"missing mandatory field {key!r}")

    rights_id = validate_identifier(record["rights_id"], "rights_id")
    record_project = validate_identifier(record["project_id"], "project_id")
    if record_project != project_id:
        _raise(
            "rights_record",
            f"cross-project record: {record_project!r} does not match {project_id!r}",
        )
    record_revision = _require_positive_int(record["record_revision"], "record_revision")

    norm_subject = _validate_subject(record["subject"], project_id)
    if subject is not None:
        expected_subject = _validate_subject(subject, project_id)
        if norm_subject != expected_subject:
            _raise("rights_record", "supplied subject does not match record subject")

    operations = record["operations"]
    if not isinstance(operations, dict):
        _raise("operations", "must be an object")
    _reject_unknown_keys(operations, OPERATIONS, "operations")
    basis = record["basis"]
    if not isinstance(basis, dict):
        _raise("basis", "must be an object")
    _reject_unknown_keys(basis, OPERATIONS, "basis")

    norm_operations = {}
    norm_basis = {}
    for op in OPERATIONS:
        if op not in operations:
            _raise("operations", f"missing operation {op!r}")
        status = operations[op]
        if status not in OPERATION_STATUSES:
            _raise("operations", f"{op!r} status {status!r} is not canonical")
        norm_operations[op] = status
        if op not in basis:
            _raise("basis", f"missing basis entry for {op!r}")
        norm_basis[op] = _validate_basis_entry(basis[op], op, status)

    auth = record["authorization"]
    if auth is None:
        norm_auth = None
    else:
        norm_auth = _validate_authorization(auth, norm_subject, record_revision)

    restrictions = record["restrictions"]
    if not isinstance(restrictions, list):
        _raise("restrictions", "must be a list of strings")
    norm_restrictions = []
    for index, item in enumerate(restrictions):
        if not isinstance(item, str) or item == "":
            _raise("restrictions", f"entry {index} must be a non-empty string")
        norm_restrictions.append(item)

    privacy = record["privacy"]
    if not isinstance(privacy, dict):
        _raise("privacy", "must be an object")
    _reject_unknown_keys(privacy, _PRIVACY_KEYS, "privacy")
    for key in _PRIVACY_KEYS:
        if key not in privacy:
            _raise("privacy", f"missing {key!r}")
    personal_data = _require_bool(privacy["personal_data"], "privacy.personal_data")
    confidentiality = privacy["confidentiality"]
    if confidentiality not in CONFIDENTIALITY_VALUES:
        _raise("privacy", f"confidentiality {confidentiality!r} is not canonical")
    denial = _require_bool(privacy["denial"], "privacy.denial")

    recorded_by = _require_nonempty_str(record["recorded_by"], "recorded_by")
    recorded_at = _validate_timestamp(record["recorded_at"], "recorded_at")

    # derived_status is computed, never trusted from input.
    supplied_status = record.get("derived_status")
    if supplied_status != "valid":
        _raise(
            "rights_record",
            f"derived_status must be the computed value 'valid' (got {supplied_status!r}); "
            "incomplete/inconsistent input is rejected, not accepted as a valid record",
        )

    return {
        "rights_id": rights_id,
        "project_id": record_project,
        "record_revision": record_revision,
        "subject": norm_subject,
        "operations": norm_operations,
        "basis": norm_basis,
        "authorization": norm_auth,
        "restrictions": norm_restrictions,
        "privacy": {
            "personal_data": personal_data,
            "confidentiality": confidentiality,
            "denial": denial,
        },
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "derived_status": "valid",
    }


# --------------------------------------------------------------------------- #
# Storage gate
# --------------------------------------------------------------------------- #
def require_storage_allowed(record):
    """Fail-closed check that a validated record permits ``storage``.

    Returns ``None`` only when the record is a fully valid ``RightsRecord`` whose
    ``operations.storage`` is ``allowed`` and whose ``privacy.denial`` is ``False``.
    Everything else raises :class:`RecordValidationError`:

    * ``unknown`` / ``not_allowed`` / ``not_applicable`` storage is rejected —
      ``not_applicable`` never unblocks a real operation (C03 section 5.2);
    * ``privacy.denial`` (or a missing/ill-typed ``privacy``) blocks even an
      ``allowed`` right;
    * a non-``valid`` ``derived_status`` is rejected.

    The caller MUST pass a record from :func:`validate_rights_record` (or one with
    the same enforced shape).  This is a metadata/structure gate only: when storage
    is ``unknown``, only source **metadata** may be retained pending a lawful
    quarantine disposition — source bytes are never retained unconditionally, and
    this function authorizes **no** legal or scientific decision.
    """
    if not isinstance(record, dict):
        _raise("require_storage_allowed", "record must be a validated rights object")
    if record.get("derived_status") != "valid":
        _raise("require_storage_allowed", "record is not a valid RightsRecord")
    operations = record.get("operations")
    if not isinstance(operations, dict):
        _raise("require_storage_allowed", "record has no operations map")
    status = operations.get("storage")
    if status != "allowed":
        _raise(
            "require_storage_allowed",
            f"storage is {status!r}, not 'allowed' (fail closed)",
        )
    privacy = record.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("denial") is not False:
        _raise(
            "require_storage_allowed",
            "privacy denial is set or privacy is malformed; storage blocked",
        )
    return None


__all__ = [
    "ARTIFACT_KINDS",
    "OPERATIONS",
    "OPERATION_STATUSES",
    "BASIS_KINDS",
    "CONFIDENTIALITY_VALUES",
    "DERIVED_STATUSES",
    "THIRD_PARTY_LEGAL_BASES",
    "RecordValidationError",
    "canonical_json",
    "validate_identifier",
    "validate_ref",
    "validate_rights_record",
    "require_storage_allowed",
]
