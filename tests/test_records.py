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

    def test_rejects_malformed_values(self):
        # One table per validator: every rejected canonical_json input, as a case.
        def cyclic_dict():
            value = {}
            value["self"] = value
            return value

        def cyclic_list():
            value = []
            value.append(value)
            return value

        def deeply_nested():
            payload = []
            node = payload
            for _ in range(500):
                child = []
                node.append(child)
                node = child
            return payload

        cases = {
            "nan": lambda: {"v": math.nan},
            "positive_infinity": lambda: {"v": math.inf},
            "negative_infinity": lambda: [-math.inf],
            "non_string_dict_key": lambda: {1: "a"},
            "non_string_key_nested": lambda: {"outer": {2: "a"}},
            "unserializable_value": lambda: {"s": {1, 2, 3}},
            "object": lambda: object(),
            "surrogate_in_string": lambda: "x\ud800",
            "surrogate_in_dict_key": lambda: {"a\ud800": 1},
            "lone_high_surrogate_in_value": lambda: {"v": "x\ud800"},
            "lone_low_surrogate_in_value": lambda: {"v": "x\udfff"},
            "surrogate_in_dict_key_low": lambda: {"a\udfff": 1},
            "surrogate_nested_in_value": lambda: {"outer": [1, {"inner": "bad\ud800"}]},
            "surrogate_nested_in_dict_key": lambda: {"outer": {"bad\ud800": 1}},
            "paired_surrogate": lambda: "x\ud800\udc00",
            "cyclic_dict": cyclic_dict,
            "cyclic_list": cyclic_list,
            "deeply_nested": deeply_nested,
        }
        for label, factory in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RecordValidationError):
                    canonical_json(factory())

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

    def test_rejects_invalid_identifiers(self):
        cases = {
            "non_string": 5,
            "empty": "",
            "whitespace": "a b",
            "leading_trailing_space": " abc ",
            "forward_slash": "a/b",
            "backslash": "a\\b",
            "control_char": "a\x00b",
            "dot": ".",
            "dotdot": "..",
            "dot_traversal_inside": "a..b",
            "leading_dot": ".hidden",
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RecordValidationError):
                    validate_identifier(value)


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

    def test_rejects_malformed_records(self):
        # One table per validator: each original reject case becomes a subTest.
        def drop(*path):
            def mutate(rec):
                target = rec
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
            return mutate

        def put(value, *path):
            def mutate(rec):
                target = rec
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            return mutate

        other_subject = {
            "kind": "source_revision",
            "project_id": "proj-1",
            "artifact_id": "art-1",
            "revision_id": "rev-2",
        }
        unknown_basis = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
        user_authorization = {
            "kind": "user_authorization",
            "evidence": {"ref": "user:own", "provenance": "interview:1"},
        }

        cases = [
            ("missing_mandatory_field", drop("basis"), "proj-1", None),
            ("missing_authorization_key_entirely", drop("authorization"), "proj-1", None),
            ("illtyped_operations", put(["allowed"], "operations"), "proj-1", None),
            ("illtyped_restrictions", put("none", "restrictions"), "proj-1", None),
            ("restrictions_entry_not_string", put([42], "restrictions"), "proj-1", None),
            ("record_revision_bool_rejected", put(True, "record_revision"), "proj-1", None),
            ("record_revision_zero_rejected", put(0, "record_revision"), "proj-1", None),
            ("record_revision_str_rejected", put("1", "record_revision"), "proj-1", None),
            (
                "privacy_personal_data_bool_as_int_rejected",
                put(0, "privacy", "personal_data"),
                "proj-1",
                None,
            ),
            ("privacy_denial_bool_as_int_rejected", put(0, "privacy", "denial"), "proj-1", None),
            ("privacy_bad_confidentiality", put("secret", "privacy", "confidentiality"), "proj-1", None),
            ("unknown_top_level_key_rejected", put(True, "approval"), "proj-1", None),
            ("hidden_actor_key_rejected", put("human:someone", "approved_by"), "proj-1", None),
            ("hidden_secret_key_rejected", put("hunter2", "credential"), "proj-1", None),
            ("unknown_nested_privacy_key_rejected", put("because", "privacy", "reason"), "proj-1", None),
            ("unknown_nested_subject_key_rejected", put("x", "subject", "extra"), "proj-1", None),
            (
                "unknown_nested_operations_key_rejected",
                put("allowed", "operations", "printing"),
                "proj-1",
                None,
            ),
            ("unknown_nested_basis_key_rejected", put("x", "basis", "storage", "extra"), "proj-1", None),
            (
                "unknown_evidence_key_rejected",
                put("x", "basis", "storage", "evidence", "note"),
                "proj-1",
                None,
            ),
            ("unknown_authorization_key_rejected", put("x", "authorization", "extra"), "proj-1", None),
            ("bad_operation_status_enum", put("yes", "operations", "storage"), "proj-1", None),
            ("cross_project_record_identity", None, "proj-2", None),
            ("cross_project_subject", put("proj-2", "subject", "project_id"), "proj-1", None),
            ("missing_subject_project", drop("subject", "project_id"), "proj-1", None),
            (
                "subject_artifact_ref_with_revision_id_rejected",
                put(
                    {
                        "kind": "subject_artifact_ref",
                        "project_id": "proj-1",
                        "artifact_id": "art-1",
                        "revision_id": "rev-1",
                    },
                    "subject",
                ),
                "proj-1",
                None,
            ),
            ("subject_bad_kind_rejected", put("source", "subject", "kind"), "proj-1", None),
            ("subject_parameter_mismatch", None, "proj-1", other_subject),
            (
                "allowed_without_basis_evidence",
                put(
                    {"kind": "public_domain", "evidence": {"ref": "", "provenance": ""}},
                    "basis",
                    "storage",
                ),
                "proj-1",
                None,
            ),
            ("allowed_with_unknown_basis", put(unknown_basis, "basis", "storage"), "proj-1", None),
            ("allowed_with_bad_basis_kind", put("vibes", "basis", "storage", "kind"), "proj-1", None),
            (
                "user_authorization_cannot_grant_redistribution",
                put(user_authorization, "basis", "redistribution"),
                "proj-1",
                None,
            ),
            (
                "user_authorization_cannot_grant_hosted_transfer",
                put(user_authorization, "basis", "hosted_transfer"),
                "proj-1",
                None,
            ),
            (
                "stale_authorization_binding_rejected",
                put(2, "authorization", "against_rights_revision"),
                "proj-1",
                None,
            ),
            (
                "authorization_bool_as_int_revision_rejected",
                put(True, "authorization", "against_rights_revision"),
                "proj-1",
                None,
            ),
            (
                "authorization_subject_mismatch_rejected",
                put("rev-2", "authorization", "against_subject", "revision_id"),
                "proj-1",
                None,
            ),
            ("authorization_wrong_method_rejected", put("in_band", "authorization", "method"), "proj-1", None),
            (
                "authorization_unknown_basis_rejected",
                put(unknown_basis, "authorization", "basis"),
                "proj-1",
                None,
            ),
            ("recorded_at_without_timezone_rejected", put("2026-10-27T00:00:00", "recorded_at"), "proj-1", None),
            ("recorded_at_not_timestamp", put("yesterday", "recorded_at"), "proj-1", None),
            (
                "input_derived_status_inconsistent_rejected",
                put("inconsistent", "derived_status"),
                "proj-1",
                None,
            ),
            (
                "input_derived_status_incomplete_rejected",
                put("incomplete", "derived_status"),
                "proj-1",
                None,
            ),
        ]
        for label, mutate, project_id, subject in cases:
            with self.subTest(case=label):
                rec = build_valid()
                if mutate is not None:
                    mutate(rec)
                self.assertRejected(rec, project_id=project_id, subject=subject)

    def test_non_object_record_rejected(self):
        with self.assertRaises(RecordValidationError):
            validate_rights_record(["not", "a", "record"], "proj-1")


class StorageGateTest(unittest.TestCase):
    def test_allowed_storage_passes(self):
        rec = validate_rights_record(build_valid(), "proj-1")
        self.assertIsNone(require_storage_allowed(rec))

    def test_storage_blocked_cases(self):
        def unknown_storage(rec):
            rec["operations"]["storage"] = "unknown"
            rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            rec["authorization"] = None

        def not_allowed_storage(rec):
            rec["operations"]["storage"] = "not_allowed"
            rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            rec["authorization"] = None

        def not_applicable_storage(rec):
            rec["operations"]["storage"] = "not_applicable"
            rec["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            rec["authorization"] = None

        def private_denial(rec):
            rec["privacy"]["denial"] = True

        cases = {
            "unknown_storage_blocked": unknown_storage,
            "not_allowed_storage_blocked": not_allowed_storage,
            "not_applicable_storage_never_unblocks": not_applicable_storage,
            "private_denial_blocks_allowed_storage": private_denial,
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                rec = build_valid()
                mutate(rec)
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
