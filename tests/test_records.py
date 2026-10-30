"""Tests for the pure-stdlib domain validators and canonical JSON.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_records.py'

These tests use only synthetic data (no secrets, no licensed source text) and
assert structural/format behavior only.  They deliberately do **not** assert any
legal, ownership, or scientific conclusion, because the module under test does not
produce one.
"""

import copy
import json
import math
import unittest

from review_article_agent.domain.records import (
    ARTIFACT_KINDS,
    RecordValidationError,
    _FORBIDDEN_ID_CHARS,
    _has_surrogate,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)


def _validate_identifier_reference(value, field="identifier"):
    """A verbatim copy of the pre-fast-path implementation.

    Kept here as the oracle for the equivalence test: the optimized
    :func:`validate_identifier` must accept/reject every input exactly as this
    does, with the same error type and message and the same precedence.
    """
    if not isinstance(value, str):
        _raise_ref(field, "must be a string")
    if value == "":
        _raise_ref(field, "must be non-empty")
    if value != value.strip() or any(ch.isspace() for ch in value):
        _raise_ref(field, "must not contain whitespace")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            _raise_ref(field, "must not contain control characters")
        if ch in _FORBIDDEN_ID_CHARS:
            _raise_ref(field, f"must not contain path separator {ch!r}")
    if value in (".", "..") or ".." in value:
        _raise_ref(field, "must not contain dot-traversal segments")
    if value.startswith("."):
        _raise_ref(field, "must not start with '.'")
    return value


def _raise_ref(label, message):
    # Mirror ``records._raise`` exactly, including the empty-label case.
    raise RecordValidationError(f"{label}: {message}" if label else message)


def _pd_basis():
    return {"kind": "public_domain", "evidence": {"ref": "lic:pd-1", "provenance": "publisher:pd-list"}}


def build_valid():
    """A fresh, fully valid synthetic RightsRecord v1."""
    subject = {
        "kind": "source_revision",
        "project_id": "proj-1",
        "artifact_id": "art-1",
        "revision_id": "rev-1",
    }
    return {
        "rights_id": "RIGHTS-test-1",
        "project_id": "proj-1",
        "record_revision": 1,
        "subject": copy.deepcopy(subject),
        "operations": {
            "acquisition": "allowed",
            "storage": "allowed",
            "redistribution": "allowed",
            "hosted_transfer": "allowed",
        },
        "basis": {
            "acquisition": _pd_basis(),
            "storage": _pd_basis(),
            "redistribution": _pd_basis(),
            "hosted_transfer": _pd_basis(),
        },
        "authorization": {
            "actor": "human:user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-27T00:00:00Z",
            "against_subject": copy.deepcopy(subject),
            "against_rights_revision": 1,
            "basis": _pd_basis(),
        },
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "agent:recorder",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


class ArtifactKindsTest(unittest.TestCase):
    def test_exact_taxonomy(self):
        self.assertEqual(
            ARTIFACT_KINDS,
            (
                "rubric",
                "framework",
                "outline",
                "report",
                "document",
                "study",
                "dataset",
                "experiment",
                "finding",
            ),
        )


class CanonicalJsonTest(unittest.TestCase):
    def test_deterministic_key_order(self):
        self.assertEqual(
            canonical_json({"b": 1, "a": 2}),
            b'{"a":2,"b":1}',
        )

    def test_roundtrip_bytes_stable(self):
        value = {"nested": {"y": [1, 2, 3], "x": None}, "flag": True}
        first = canonical_json(value)
        reloaded = json.loads(first)
        self.assertEqual(canonical_json(reloaded), first)

    def test_returns_bytes(self):
        self.assertIsInstance(canonical_json({"a": 1}), bytes)

    def test_unicode_is_utf8_not_escaped(self):
        self.assertEqual(canonical_json({"k": "café"}).decode("utf-8"), '{"k":"café"}')

    def test_rejects_nan(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"v": math.nan})

    def test_rejects_positive_infinity(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"v": math.inf})

    def test_rejects_negative_infinity(self):
        with self.assertRaises(RecordValidationError):
            canonical_json([-math.inf])

    def test_rejects_non_string_dict_key(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({1: "a"})

    def test_rejects_non_string_key_nested(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"outer": {2: "a"}})

    def test_rejects_unserializable_value(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"s": {1, 2, 3}})

    def test_rejects_object(self):
        with self.assertRaises(RecordValidationError):
            canonical_json(object())

    def test_rejects_surrogate_in_string(self):
        with self.assertRaises(RecordValidationError):
            canonical_json("x\ud800")

    def test_rejects_surrogate_in_dict_key(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"a\ud800": 1})

    def test_has_surrogate_accepts_ascii_and_valid_unicode(self):
        # The isascii() fast path and the single-encode fallback must accept every
        # non-surrogate string: ASCII, BMP non-ASCII, and astral code points.
        for text in ("", "abc", "café", "漢", "😀", "a\u0000b", "x" * 5000):
            self.assertFalse(_has_surrogate(text), repr(text))

    def test_has_surrogate_rejects_lone_and_paired_surrogates(self):
        for text in (
            "\ud800",
            "\udfff",
            "\ud800\udc00",  # a paired surrogate in a Python str is still rejected
            "\udc00\ud800",
            "a\ud800b",
            "é\ud800",
        ):
            self.assertTrue(_has_surrogate(text), repr(text))

    def test_accepts_bmp_and_astral_values_and_keys(self):
        self.assertEqual(
            canonical_json({"k": "é", "漢": "😀"}).decode("utf-8"),
            '{"k":"é","漢":"😀"}',
        )

    def test_rejects_lone_high_surrogate_in_value(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"v": "x\ud800"})

    def test_rejects_lone_low_surrogate_in_value(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"v": "x\udfff"})

    def test_rejects_surrogate_in_dict_key_low(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"a\udfff": 1})

    def test_rejects_surrogate_nested_in_value(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"outer": [1, {"inner": "bad\ud800"}]})

    def test_rejects_surrogate_nested_in_dict_key(self):
        with self.assertRaises(RecordValidationError):
            canonical_json({"outer": {"bad\ud800": 1}})

    def test_rejects_paired_surrogate(self):
        with self.assertRaises(RecordValidationError):
            canonical_json("x\ud800\udc00")

    def test_rejects_cyclic_dict(self):
        cyclic = {}
        cyclic["self"] = cyclic
        with self.assertRaises(RecordValidationError):
            canonical_json(cyclic)

    def test_rejects_cyclic_list(self):
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(RecordValidationError):
            canonical_json(cyclic)

    def test_rejects_deeply_nested(self):
        payload = []
        node = payload
        for _ in range(500):
            child = []
            node.append(child)
            node = child
        with self.assertRaises(RecordValidationError):
            canonical_json(payload)

    def test_accepts_moderately_nested(self):
        payload = [1]
        for _ in range(30):
            payload = [payload]
        self.assertIsInstance(canonical_json(payload), bytes)

    def test_allows_shared_non_cyclic_reference(self):
        shared = [1, 2]
        self.assertEqual(
            canonical_json({"a": shared, "b": shared}),
            b'{"a":[1,2],"b":[1,2]}',
        )


class IdentifierTest(unittest.TestCase):
    def test_accepts_opaque_id(self):
        self.assertEqual(validate_identifier("art-syn-A_1.2"), "art-syn-A_1.2")

    def test_rejects_non_string(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier(5)

    def test_rejects_empty(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("")

    def test_rejects_whitespace(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("a b")

    def test_rejects_leading_trailing_space(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier(" abc ")

    def test_rejects_forward_slash(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("a/b")

    def test_rejects_backslash(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("a\\b")

    def test_rejects_control_char(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("a\x00b")

    def test_rejects_dot(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier(".")

    def test_rejects_dotdot(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("..")

    def test_rejects_dot_traversal_inside(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier("a..b")

    def test_rejects_leading_dot(self):
        with self.assertRaises(RecordValidationError):
            validate_identifier(".hidden")


class IdentifierFastPathEquivalenceTest(unittest.TestCase):
    """The fast path must be observationally identical to the original loops."""

    @staticmethod
    def _outcome_of(func, value, field="identifier"):
        try:
            return ("ok", func(value, field))
        except Exception as exc:  # noqa: BLE001 - compare type+message generically
            return ("err", type(exc), str(exc))

    def _corpus(self):
        values = []

        # Every printable-ASCII single character, including space, plus DEL and a
        # spread of control characters.
        values.extend(chr(c) for c in range(0x20, 0x7F))
        values.extend(chr(c) for c in (0x00, 0x01, 0x09, 0x0A, 0x0D, 0x1F, 0x7F))

        # Representative pairs, so multi-character precedence is exercised.
        seed = "aA0 .-_/:\\..\x00\x7f!~[]{}"
        values.extend(a + b for a in seed for b in seed)

        # Typical real identifiers (all should be accepted).
        values.extend(
            [
                "cand_abc123",
                "art-1",
                "art-syn-A_1.2",
                "sha256:" + "a" * 64,
                "sha256:deadbeef",
                "123e4567-e89b-12d3-a456-426614174000",
                "proj-1",
                "rev-1",
                "RIGHTS-test-1",
                "a.b.c",
                "a.",
                ".a",
                "a-b_c:d@e+f=g~h,i;j!k#l$m%n&o(p)q[r]s{t}u<v>w?x^y|z",
            ]
        )

        # Unicode letters must still be accepted via the slow path.
        values.extend(["café", "naïve", "αβγ", "日本語", "ü", "Ω"])

        # Unicode whitespace must still be rejected via the slow path.
        values.extend(["\u00a0", "a\u00a0b", "\u2003", "a\u2003b", "\u3000"])

        # Empty, whitespace-only, separators, dots, and non-strings.
        values.extend(
            [
                "",
                " ",
                "  ",
                "\t",
                "\n",
                "a b",
                " abc ",
                "abc ",
                " abc",
                "/",
                "\\",
                "a/b",
                "a\\b",
                "/..",
                "..",
                ".",
                "...",
                "a..b",
                "..a",
                "a..",
                ".hidden",
                "..hidden",
            ]
        )
        values.extend([None, 5, 5.0, b"bytes", ("t",), ["l"], {"d": 1}, object()])

        # A few long valid ids to exercise the fullmatch fast path length-wise.
        values.append("x" * 1000)
        values.append(("ab-cd_12" * 100))
        return values

    def test_fast_path_matches_reference_on_corpus(self):
        for value in self._corpus():
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    self._outcome_of(validate_identifier, value),
                    self._outcome_of(_validate_identifier_reference, value),
                )

    def test_fast_path_matches_reference_with_blank_field(self):
        # ``_raise``'s empty-label branch must match too.
        for value in ("", "a b", "a/b", ".", "..", "ok-1"):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    self._outcome_of(validate_identifier, value, field=""),
                    self._outcome_of(_validate_identifier_reference, value, field=""),
                )

    def test_accepted_typical_ids_are_returned_unchanged(self):
        for value in ("cand_abc123", "art-1", "sha256:" + "a" * 64, "a.b.c", "café"):
            self.assertEqual(validate_identifier(value), value)


class RefTest(unittest.TestCase):
    def test_valid_returns_exact_keys(self):
        ref = validate_ref(
            {"project_id": "proj-1", "artifact_id": "art-1", "revision_id": "rev-1"},
            "proj-1",
        )
        self.assertEqual(
            set(ref), {"project_id", "artifact_id", "revision_id"}
        )
        self.assertEqual(ref["revision_id"], "rev-1")

    def test_missing_key_rejected(self):
        with self.assertRaises(RecordValidationError):
            validate_ref({"project_id": "proj-1", "artifact_id": "art-1"}, "proj-1")

    def test_extra_key_rejected(self):
        with self.assertRaises(RecordValidationError):
            validate_ref(
                {
                    "project_id": "proj-1",
                    "artifact_id": "art-1",
                    "revision_id": "rev-1",
                    "extra": "x",
                },
                "proj-1",
            )

    def test_cross_project_rejected(self):
        with self.assertRaises(RecordValidationError):
            validate_ref(
                {"project_id": "proj-2", "artifact_id": "art-1", "revision_id": "rev-1"},
                "proj-1",
            )

    def test_bad_identifier_rejected(self):
        with self.assertRaises(RecordValidationError):
            validate_ref(
                {"project_id": "proj-1", "artifact_id": "../etc", "revision_id": "rev-1"},
                "proj-1",
            )


class RightsRecordValidTest(unittest.TestCase):
    def test_valid_record_accepted_and_normalized(self):
        out = validate_rights_record(build_valid(), "proj-1")
        self.assertEqual(
            set(out),
            {
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
            },
        )
        self.assertEqual(out["derived_status"], "valid")
        self.assertEqual(out["record_revision"], 1)

    def test_subject_artifact_ref_pre_acquisition_accepted(self):
        rec = build_valid()
        rec["subject"] = {
            "kind": "subject_artifact_ref",
            "project_id": "proj-1",
            "artifact_id": "art-1",
        }
        rec["authorization"] = None
        out = validate_rights_record(rec, "proj-1")
        self.assertEqual(out["subject"]["kind"], "subject_artifact_ref")
        self.assertNotIn("revision_id", out["subject"])

    def test_allowed_ops_can_use_user_authorization_for_acquisition_storage(self):
        rec = build_valid()
        rec["basis"]["acquisition"] = {
            "kind": "user_authorization",
            "evidence": {"ref": "user:own", "provenance": "interview:1"},
        }
        rec["basis"]["storage"] = {
            "kind": "user_authorization",
            "evidence": {"ref": "user:own", "provenance": "interview:1"},
        }
        out = validate_rights_record(rec, "proj-1")
        self.assertEqual(out["basis"]["acquisition"]["kind"], "user_authorization")

    def test_matching_subject_parameter_accepted(self):
        rec = build_valid()
        out = validate_rights_record(rec, "proj-1", subject=rec["subject"])
        self.assertEqual(out["subject"], rec["subject"])


class RightsRecordRejectTest(unittest.TestCase):
    def assertRejected(self, rec, project_id="proj-1", subject=None):
        with self.assertRaises(RecordValidationError):
            validate_rights_record(rec, project_id, subject=subject)

    def test_missing_mandatory_field(self):
        rec = build_valid()
        del rec["basis"]
        self.assertRejected(rec)

    def test_missing_authorization_key_entirely(self):
        rec = build_valid()
        del rec["authorization"]
        self.assertRejected(rec)

    def test_illtyped_operations(self):
        rec = build_valid()
        rec["operations"] = ["allowed"]
        self.assertRejected(rec)

    def test_illtyped_restrictions(self):
        rec = build_valid()
        rec["restrictions"] = "none"
        self.assertRejected(rec)

    def test_restrictions_entry_not_string(self):
        rec = build_valid()
        rec["restrictions"] = [42]
        self.assertRejected(rec)

    def test_record_revision_bool_rejected(self):
        rec = build_valid()
        rec["record_revision"] = True
        self.assertRejected(rec)

    def test_record_revision_zero_rejected(self):
        rec = build_valid()
        rec["record_revision"] = 0
        self.assertRejected(rec)

    def test_record_revision_str_rejected(self):
        rec = build_valid()
        rec["record_revision"] = "1"
        self.assertRejected(rec)

    def test_privacy_personal_data_bool_as_int_rejected(self):
        rec = build_valid()
        rec["privacy"]["personal_data"] = 0
        self.assertRejected(rec)

    def test_privacy_denial_bool_as_int_rejected(self):
        rec = build_valid()
        rec["privacy"]["denial"] = 0
        self.assertRejected(rec)

    def test_privacy_bad_confidentiality(self):
        rec = build_valid()
        rec["privacy"]["confidentiality"] = "secret"
        self.assertRejected(rec)

    def test_unknown_top_level_key_rejected(self):
        rec = build_valid()
        rec["approval"] = True
        self.assertRejected(rec)

    def test_hidden_actor_key_rejected(self):
        rec = build_valid()
        rec["approved_by"] = "human:someone"
        self.assertRejected(rec)

    def test_hidden_secret_key_rejected(self):
        rec = build_valid()
        rec["credential"] = "hunter2"
        self.assertRejected(rec)

    def test_unknown_nested_privacy_key_rejected(self):
        rec = build_valid()
        rec["privacy"]["reason"] = "because"
        self.assertRejected(rec)

    def test_unknown_nested_subject_key_rejected(self):
        rec = build_valid()
        rec["subject"]["extra"] = "x"
        self.assertRejected(rec)

    def test_unknown_nested_operations_key_rejected(self):
        rec = build_valid()
        rec["operations"]["printing"] = "allowed"
        self.assertRejected(rec)

    def test_unknown_nested_basis_key_rejected(self):
        rec = build_valid()
        rec["basis"]["storage"]["extra"] = "x"
        self.assertRejected(rec)

    def test_unknown_evidence_key_rejected(self):
        rec = build_valid()
        rec["basis"]["storage"]["evidence"]["note"] = "x"
        self.assertRejected(rec)

    def test_unknown_authorization_key_rejected(self):
        rec = build_valid()
        rec["authorization"]["extra"] = "x"
        self.assertRejected(rec)

    def test_bad_operation_status_enum(self):
        rec = build_valid()
        rec["operations"]["storage"] = "yes"
        self.assertRejected(rec)

    def test_cross_project_record_identity(self):
        rec = build_valid()
        self.assertRejected(rec, project_id="proj-2")

    def test_cross_project_subject(self):
        rec = build_valid()
        rec["subject"]["project_id"] = "proj-2"
        self.assertRejected(rec)

    def test_missing_subject_project(self):
        rec = build_valid()
        del rec["subject"]["project_id"]
        self.assertRejected(rec)

    def test_subject_artifact_ref_with_revision_id_rejected(self):
        rec = build_valid()
        rec["subject"] = {
            "kind": "subject_artifact_ref",
            "project_id": "proj-1",
            "artifact_id": "art-1",
            "revision_id": "rev-1",
        }
        self.assertRejected(rec)

    def test_subject_bad_kind_rejected(self):
        rec = build_valid()
        rec["subject"]["kind"] = "source"
        self.assertRejected(rec)

    def test_subject_parameter_mismatch(self):
        rec = build_valid()
        other = {"kind": "source_revision", "project_id": "proj-1", "artifact_id": "art-1", "revision_id": "rev-2"}
        self.assertRejected(rec, subject=other)

    def test_allowed_without_basis_evidence(self):
        rec = build_valid()
        rec["basis"]["storage"] = {
            "kind": "public_domain",
            "evidence": {"ref": "", "provenance": ""},
        }
        self.assertRejected(rec)

    def test_allowed_with_unknown_basis(self):
        rec = build_valid()
        rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        self.assertRejected(rec)

    def test_allowed_with_bad_basis_kind(self):
        rec = build_valid()
        rec["basis"]["storage"]["kind"] = "vibes"
        self.assertRejected(rec)

    def test_user_authorization_cannot_grant_redistribution(self):
        rec = build_valid()
        rec["basis"]["redistribution"] = {
            "kind": "user_authorization",
            "evidence": {"ref": "user:own", "provenance": "interview:1"},
        }
        self.assertRejected(rec)

    def test_user_authorization_cannot_grant_hosted_transfer(self):
        rec = build_valid()
        rec["basis"]["hosted_transfer"] = {
            "kind": "user_authorization",
            "evidence": {"ref": "user:own", "provenance": "interview:1"},
        }
        self.assertRejected(rec)

    def test_stale_authorization_binding_rejected(self):
        rec = build_valid()
        rec["authorization"]["against_rights_revision"] = 2
        self.assertRejected(rec)

    def test_authorization_bool_as_int_revision_rejected(self):
        rec = build_valid()
        rec["authorization"]["against_rights_revision"] = True
        self.assertRejected(rec)

    def test_authorization_subject_mismatch_rejected(self):
        rec = build_valid()
        rec["authorization"]["against_subject"]["revision_id"] = "rev-2"
        self.assertRejected(rec)

    def test_authorization_wrong_method_rejected(self):
        rec = build_valid()
        rec["authorization"]["method"] = "in_band"
        self.assertRejected(rec)

    def test_authorization_unknown_basis_rejected(self):
        rec = build_valid()
        rec["authorization"]["basis"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        self.assertRejected(rec)

    def test_recorded_at_without_timezone_rejected(self):
        rec = build_valid()
        rec["recorded_at"] = "2026-10-27T00:00:00"
        self.assertRejected(rec)

    def test_recorded_at_not_timestamp(self):
        rec = build_valid()
        rec["recorded_at"] = "yesterday"
        self.assertRejected(rec)

    def test_input_derived_status_inconsistent_rejected(self):
        rec = build_valid()
        rec["derived_status"] = "inconsistent"
        self.assertRejected(rec)

    def test_input_derived_status_incomplete_rejected(self):
        rec = build_valid()
        rec["derived_status"] = "incomplete"
        self.assertRejected(rec)

    def test_non_object_record_rejected(self):
        with self.assertRaises(RecordValidationError):
            validate_rights_record(["not", "a", "record"], "proj-1")


class StorageGateTest(unittest.TestCase):
    def test_allowed_storage_passes(self):
        rec = validate_rights_record(build_valid(), "proj-1")
        self.assertIsNone(require_storage_allowed(rec))

    def test_unknown_storage_blocked(self):
        rec = build_valid()
        rec["operations"]["storage"] = "unknown"
        rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        rec["authorization"] = None
        validated = validate_rights_record(rec, "proj-1")
        with self.assertRaises(RecordValidationError):
            require_storage_allowed(validated)

    def test_not_allowed_storage_blocked(self):
        rec = build_valid()
        rec["operations"]["storage"] = "not_allowed"
        rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        rec["authorization"] = None
        validated = validate_rights_record(rec, "proj-1")
        with self.assertRaises(RecordValidationError):
            require_storage_allowed(validated)

    def test_not_applicable_storage_never_unblocks(self):
        rec = build_valid()
        rec["operations"]["storage"] = "not_applicable"
        rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        rec["authorization"] = None
        validated = validate_rights_record(rec, "proj-1")
        with self.assertRaises(RecordValidationError):
            require_storage_allowed(validated)

    def test_private_denial_blocks_allowed_storage(self):
        rec = build_valid()
        rec["privacy"]["denial"] = True
        validated = validate_rights_record(rec, "proj-1")
        with self.assertRaises(RecordValidationError):
            require_storage_allowed(validated)

    def test_unknown_storage_record_remains_metadata_only_semantics(self):
        # A valid record whose storage is unknown is still a *valid* record, but it
        # authorizes no storage: this is the metadata-only quarantine path.
        rec = build_valid()
        rec["operations"] = {
            "acquisition": "unknown",
            "storage": "unknown",
            "redistribution": "unknown",
            "hosted_transfer": "unknown",
        }
        for op in rec["basis"]:
            rec["basis"][op] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        rec["authorization"] = None
        validated = validate_rights_record(rec, "proj-1")
        self.assertEqual(validated["derived_status"], "valid")
        with self.assertRaises(RecordValidationError):
            require_storage_allowed(validated)

    def test_unvalidated_record_fails_closed(self):
        with self.assertRaises(RecordValidationError):
            require_storage_allowed({"operations": {"storage": "allowed"}})

    def test_separate_operation_unknown_not_auto_granted(self):
        # redistribution unknown stays unknown; it is never widened to allowed.
        rec = build_valid()
        rec["operations"]["redistribution"] = "unknown"
        rec["basis"]["redistribution"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        rec["authorization"] = None
        validated = validate_rights_record(rec, "proj-1")
        self.assertEqual(validated["operations"]["redistribution"], "unknown")
        self.assertEqual(validated["operations"]["storage"], "allowed")


class MalformedInputBoundaryTest(unittest.TestCase):
    """Malformed/domain inputs must always raise RecordValidationError, never a raw
    TypeError/UnicodeEncodeError/RecursionError."""

    def _rve(self, fn):
        with self.assertRaises(RecordValidationError):
            fn()

    def test_ref_non_string_key(self):
        self._rve(
            lambda: validate_ref(
                {1: "bad", "project_id": "p", "artifact_id": "a", "revision_id": "r"},
                "p",
            )
        )

    def test_ref_mixed_unknown_key_types_no_crash(self):
        self._rve(
            lambda: validate_ref(
                {
                    "zzz": "x",
                    5: "y",
                    "project_id": "p",
                    "artifact_id": "a",
                    "revision_id": "r",
                },
                "p",
            )
        )

    def test_rights_non_string_top_level_key(self):
        self._rve(lambda: validate_rights_record({3: "x"}, "proj-1"))

    def test_rights_non_string_nested_key(self):
        rec = build_valid()
        rec["privacy"][4] = "x"
        self._rve(lambda: validate_rights_record(rec, "proj-1"))

    def test_whitespace_only_evidence_rejected_for_allowed(self):
        rec = build_valid()
        rec["basis"]["storage"]["evidence"] = {"ref": "  ", "provenance": "\t"}
        self._rve(lambda: validate_rights_record(rec, "proj-1"))

    def test_non_allowed_empty_evidence_allowed(self):
        rec = build_valid()
        rec["operations"]["redistribution"] = "unknown"
        rec["basis"]["redistribution"] = {
            "kind": "unknown",
            "evidence": {"ref": "", "provenance": ""},
        }
        rec["authorization"] = None
        validate_rights_record(rec, "proj-1")

    def test_unhashable_basis_kind(self):
        for bad in ([], {}, 3, None):
            rec = build_valid()
            rec["basis"]["storage"]["kind"] = bad
            self._rve(lambda rec=rec: validate_rights_record(rec, "proj-1"))

    def test_unhashable_operation_status(self):
        rec = build_valid()
        rec["operations"]["acquisition"] = []
        self._rve(lambda: validate_rights_record(rec, "proj-1"))

    def test_unhashable_confidentiality(self):
        rec = build_valid()
        rec["privacy"]["confidentiality"] = []
        self._rve(lambda: validate_rights_record(rec, "proj-1"))

    def test_unhashable_subject_kind(self):
        rec = build_valid()
        rec["subject"]["kind"] = []
        self._rve(lambda: validate_rights_record(rec, "proj-1"))

    def test_unhashable_authorization_basis_kind(self):
        rec = build_valid()
        rec["authorization"]["basis"]["kind"] = []
        self._rve(lambda: validate_rights_record(rec, "proj-1"))

    def test_only_record_validation_error_escapes(self):
        cases = [
            lambda: validate_ref({1: "x"}, "p"),
            lambda: validate_ref("not a dict", "p"),
            lambda: validate_rights_record({2: "x"}, "p"),
            lambda: canonical_json("x\ud800"),
            lambda: canonical_json({1: "x"}),
        ]
        for case in cases:
            try:
                case()
            except RecordValidationError:
                pass
            except Exception as exc:  # noqa: BLE001 - the point of the test
                self.fail(f"raw {type(exc).__name__} escaped instead of RecordValidationError")


class RoundtripImmutabilityTest(unittest.TestCase):
    def test_roundtrip_equals_and_bytes_stable(self):
        rec = build_valid()
        validated = validate_rights_record(rec, "proj-1")
        blob = canonical_json(validated)
        reloaded = json.loads(blob)
        again = validate_rights_record(reloaded, "proj-1")
        self.assertEqual(again, validated)
        self.assertEqual(canonical_json(again), blob)

    def test_validated_copy_is_independent_of_input(self):
        rec = build_valid()
        validated = validate_rights_record(rec, "proj-1")
        snapshot = copy.deepcopy(validated)
        rec["operations"]["storage"] = "not_allowed"
        rec["subject"]["revision_id"] = "rev-CHANGED"
        rec["restrictions"].append("mutated")
        self.assertEqual(validated, snapshot)

    def test_authorization_present_does_not_grant_storage(self):
        # A structurally valid authorization entry is untrusted data; it does not by
        # itself enable storage when the storage right is not allowed.
        rec = build_valid()
        rec["operations"]["storage"] = "unknown"
        rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        validated = validate_rights_record(rec, "proj-1")
        self.assertIsNotNone(validated["authorization"])
        with self.assertRaises(RecordValidationError):
            require_storage_allowed(validated)


if __name__ == "__main__":
    unittest.main()
