"""In-process CLI acceptance tests for ``acquisition manifest`` / ``queue`` (Step 06, C2).

Every case drives the real entry point (``cli.main.main`` with in-memory streams)
over a real registry-created project store, the integrated acquisition manifest
repository, and the genuine owner-confirmation gate seam (the mocked controlling
tty of ``test_discovery_freeze``). The CLI itself never approves; gates in these
tests are recorded through the existing prepare/owner-confirm repository path.
Synthetic data only: no network, no model call.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_cli_acquisition_manifest -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_acquisition_manifest import ManifestBase, _jats  # noqa: E402

from review_article_agent.acquisition.manifest import (  # noqa: E402
    AcquisitionRepository,
)
from review_article_agent.cli import main as cli_main  # noqa: E402
from review_article_agent.discovery.freeze import FreezeRepository  # noqa: E402
from review_article_agent.storage import ProjectStore, Registry, paths  # noqa: E402

_SECRET = "SYNTHETIC-RATIONALE-DO-NOT-ECHO"
_SECRET2 = "SYNTHETIC-SECOND-SECRET-DO-NOT-ECHO"
_OK = cli_main.EXIT_SUCCESS
_VALID = cli_main.EXIT_VALIDATION
_CONFLICT = cli_main.EXIT_CONFLICT

#: Field names that would expose a private candidate identifier or free text.
_PRIVATE_IDENTIFIER_FIELDS = frozenset(
    {
        "pmid", "pmcid", "doi", "expected_identifiers", "observed_identifiers",
        "identifiers", "tokens", "rationale",
    }
)


def _private_identifier_paths(obj, private_values, path="$"):
    """JSON paths of any private identifier *field name* or *exact value*.

    Opaque ids and hashes that merely *contain* a private value as a substring
    are not flagged (they are not private identifiers); only a field named like a
    private identifier, or a string that is exactly a private identifier value,
    counts as a real leak. This keeps the privacy check deterministic while
    still rejecting a genuine exposed ``pmid``/``expected_identifiers``.
    """
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}"
            if key in _PRIVATE_IDENTIFIER_FIELDS:
                found.append(here)
                continue
            found.extend(_private_identifier_paths(value, private_values, here))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(
                _private_identifier_paths(item, private_values, f"{path}[{i}]")
            )
    elif isinstance(obj, str) and obj in private_values:
        found.append(path)
    return found


class ManifestCliBase(ManifestBase):
    """``ManifestBase`` fixtures over a registry-created project the CLI can open."""

    def setUp(self) -> None:  # deliberately not FreezeTestBase.setUp
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-manifest-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        code, out, err = self._run("create", str(self.tmp / "proj"), "--name", "proj")
        self.assertEqual(code, 0, err)
        self.project_id = json.loads(out)["project"]["project_id"]
        info = Registry(str(self.state)).get(self.project_id)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(
            self.app_dir, expected_project_id=self.project_id
        )
        self.addCleanup(self._close_store)
        self._bind(self.store)
        self.freeze = FreezeRepository(self.store)
        self._install_owner_auth()
        self.acq = AcquisitionRepository(self.store)
        self._n = 0

    # -- CLI driving --------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = _OK, allow_secret: bool = False) -> dict:
        if "--project" not in args and args[0] == "acquisition":
            args = (*args, "--project", self.project_id)
        code, out, err = self._run(*args)
        self.assertEqual(code, expect, f"args={args} rc={code}\n{out}\n{err}")
        self.assertNotIn("Traceback", err)
        if not allow_secret:
            for blob in (out, err):
                self.assertNotIn(_SECRET, blob)
                self.assertNotIn(_SECRET2, blob)
        return json.loads(out if expect == _OK else err)

    def write_json(self, name: str, data: object) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    # -- fixtures ------------------------------------------------------------ #
    def cli_propose(
        self, entries, *, request_id="man-1", revision_id=None, artifact_id=None,
        freeze_ref=None, expect=_OK,
    ):
        rid = revision_id or AcquisitionRepository.new_revision_id()
        art = artifact_id or "acquisition.manifest"
        freeze_ref = freeze_ref or self.freeze_ref
        args = [
            "acquisition", "manifest", "propose",
            "--freeze-artifact-id", freeze_ref["artifact_id"],
            "--freeze-revision-id", freeze_ref["revision_id"],
            "--entries-file", self.write_json(f"e-{request_id}-{rid}.json", entries),
            "--rights-file", self.write_json(
                f"r-{request_id}-{rid}.json", self.rights(art, rid)
            ),
            "--revision-id", rid,
            "--request-id", request_id,
        ]
        if artifact_id:
            args += ["--artifact-id", artifact_id]
        out = self.cli(*args, expect=expect)
        return rid, out

    @staticmethod
    def rev(out):
        ref = out["manifest"]["manifest_ref"]
        return SimpleNamespace(
            artifact_id=ref["artifact_id"], revision_id=ref["revision_id"]
        )

    def secret_entries(self):
        """111 use_full_text (JATS source) + 222 exclude, with secret rationales."""
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        self.src = self.put_source(_jats("111"))
        self.a, self.b = a, b
        return [
            self.entry(a, source=self.src, rationale=_SECRET),
            self.entry(b, "exclude", rationale=_SECRET2),
        ]


@slow
class ManifestCliTests(ManifestCliBase):
    # -- queue / missing source -------------------------------------------- #
    def test_queue_without_manifest_lists_every_frozen_candidate(self):
        self.setup_frozen()
        out = self.cli(
            "acquisition", "queue",
            "--freeze-artifact-id", self.freeze_ref["artifact_id"],
            "--freeze-revision-id", self.freeze_ref["revision_id"],
        )
        self.assertEqual(out["count"], len(self.cands))
        self.assertEqual(out["by_code"], {"no_manifest_entry": len(self.cands)})
        self.assertEqual(out["by_category"], {"missing": len(self.cands)})
        self.assertEqual(
            {i["candidate_id"] for i in out["items"]}, set(self.cands.values())
        )
        self.assertTrue(out["empty_queue_is_not_approval"])
        self.assertFalse(out["authorizes_scientific_processing"])
        # No manifest means require refuses (and no write happened).
        err = self.cli(
            "acquisition", "manifest", "require", expect=_VALID
        )
        self.assertIn("no_manifest", err["error"]["message"])

    def test_queue_rejects_unfrozen_or_stale_freeze_reference(self):
        self.setup_frozen()
        err = self.cli(
            "acquisition", "queue",
            "--freeze-artifact-id", self.freeze_ref["artifact_id"],
            "--freeze-revision-id", "no-such-revision", expect=_VALID,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_missing_required_supplement_queues_and_cannot_require_even_if_gated(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src = self.put_source(_jats("111"))
        entries = [
            self.entry(
                a, source=src,
                assets=[self.asset("supp-1", "supplement", status="unavailable",
                                   rationale=_SECRET)],
            ),
            self.entry(b, "exclude", rationale=_SECRET2),
        ]
        _rid, out = self.cli_propose(entries)
        self.assertEqual(out["manifest"]["blocker_count"], 1)
        self.assertEqual(
            out["manifest"]["blockers_by_code"], {"required_asset_unavailable": 1}
        )
        self.assertEqual(out["manifest"]["blockers_by_category"], {"supplement": 1})
        queue = self.cli(
            "acquisition", "queue",
            "--freeze-artifact-id", self.freeze_ref["artifact_id"],
            "--freeze-revision-id", self.freeze_ref["revision_id"],
        )
        self.assertEqual(
            queue["items"],
            [{"candidate_id": a, "asset_id": "supp-1", "category": "supplement",
              "code": "required_asset_unavailable"}],
        )
        self.gate(self.rev(out))  # genuine owner gate on the exact revision
        err = self.cli("acquisition", "manifest", "require", expect=_VALID)
        self.assertIn("blockers_present", err["error"]["message"])
        status = self.cli("acquisition", "manifest", "status")
        self.assertFalse(status["acquired"])
        self.assertEqual(status["not_acquired_reasons"], ["blockers_present"])

    # -- authority --------------------------------------------------------- #
    def test_propose_only_records_no_authority(self):
        entries = self.secret_entries()
        rid, out = self.cli_propose(entries)
        req = out["approval_request"]
        self.assertEqual(req["gate_kind"], "artifact_revision")
        self.assertEqual(req["gate_id"], "acquisition.complete")
        self.assertEqual(req["revision_id"], rid)
        self.assertFalse(req["satisfied"])
        self.assertTrue(req["current"])
        self.assertIsNone(req["approval"])
        prep = req["snapshot_preparation"]
        self.assertIn("gate prepare", prep["prepare_command"])
        self.assertIn(rid, prep["prepare_command"])
        self.assertIn("--gate-id acquisition.complete", prep["confirm_command"])
        self.assertIn("--snapshot-digest", prep["confirm_command"])
        self.assertTrue(out["proposal_only"])
        self.assertFalse(out["authorizes_scientific_processing"])
        self.assertEqual(out["manifest"]["blocker_count"], 0)
        self.assertEqual(out["manifest"]["dispositions"],
                         {"exclude": 1, "use_full_text": 1})
        err = self.cli("acquisition", "manifest", "require", expect=_VALID)
        self.assertIn("gate_missing", err["error"]["message"])
        self.assertEqual(err["error"]["category"], "validation")

    def test_wrong_generic_unapproved_and_rejected_gates_do_not_acquire(self):
        entries = self.secret_entries()
        _rid, out = self.cli_propose(entries)
        rev = self.rev(out)
        self.gate(rev, gate_id="tool.allowed")
        self.gate(rev, gate_kind="pilot")
        self.cli("acquisition", "manifest", "require", expect=_VALID)
        self.gate(rev, decision="rejected")
        err = self.cli("acquisition", "manifest", "require", expect=_VALID)
        self.assertIn("gate_not_active", err["error"]["message"])
        status = self.cli("acquisition", "manifest", "status")
        self.assertFalse(status["acquired"])

    def test_authentic_gated_manifest_requires_safe_and_private(self):
        entries = self.secret_entries()
        _rid, out = self.cli_propose(entries)
        self.gate(self.rev(out))

        safe = self.cli("acquisition", "manifest", "require")
        self.assertTrue(safe["acquired"])
        self.assertEqual(safe["acquired_count"], 1)
        self.assertEqual(safe["nonuse_count"], 1)
        self.assertFalse(safe["authorizes_scientific_processing"])
        self.assertTrue(safe["manifest"]["approval"]["satisfies_named_gate"])
        self.assertEqual(safe["manifest"]["approval"]["gate_id"], "acquisition.complete")
        self.assertNotIn("document", safe)
        self.assertNotIn("acquired_candidate_ids", safe)
        self.assertFalse(safe["manifest"]["private_content_included"])

        private = self.cli(
            "acquisition", "manifest", "require", "--content", allow_secret=True
        )
        self.assertTrue(private["private"])
        self.assertEqual(private["acquired_candidate_ids"], [self.a])
        self.assertIn(_SECRET, json.dumps(private["document"]))
        self.assertFalse(private["authorizes_scientific_processing"])
        self.assertEqual(private["acquired_count"], 1)

        status = self.cli("acquisition", "manifest", "status")
        self.assertTrue(status["acquired"])
        self.assertEqual(status["not_acquired_reasons"], [])
        self.assertEqual(
            self.cli("acquisition", "queue",
                     "--freeze-artifact-id", self.freeze_ref["artifact_id"],
                     "--freeze-revision-id", self.freeze_ref["revision_id"])["count"],
            0,
        )

    def test_explicit_defer_needs_no_source_bytes(self):
        self.setup_frozen()
        entries = [
            self.entry(cid, "defer", rationale=_SECRET)
            for cid in self.cands.values()
        ]
        _rid, out = self.cli_propose(entries)
        self.assertEqual(out["manifest"]["blocker_count"], 0)
        self.assertEqual(out["manifest"]["dispositions"], {"defer": len(entries)})
        self.gate(self.rev(out))
        safe = self.cli("acquisition", "manifest", "require")
        self.assertEqual(safe["acquired_count"], 0)
        self.assertEqual(safe["nonuse_count"], len(entries))
        shown = self.cli("acquisition", "manifest", "show")
        self.assertTrue(all(e["source_ref"] is None for e in shown["manifest"]["entries"]))

    def test_source_revision_stales_gate_but_unrelated_manifest_is_preserved(self):
        entries = self.secret_entries()
        _rid, first = self.cli_propose(entries)
        self.gate(self.rev(first))
        # An unrelated, separately gated manifest artifact that pins no source.
        other = [self.entry(cid, "defer", rationale="explicit defer")
                 for cid in self.cands.values()]
        _r2, second = self.cli_propose(
            other, request_id="man-other", artifact_id="other.manifest"
        )
        self.gate(self.rev(second))
        self.cli("acquisition", "manifest", "require")

        self.append_source(self.src, _jats("111") + b"\n<!-- revised -->")
        err = self.cli("acquisition", "manifest", "require", expect=_VALID)
        # The genuine gate goes inactive once a pinned dependency is superseded.
        self.assertIn("gate_not_active", err["error"]["message"])
        status = self.cli("acquisition", "manifest", "status")
        self.assertEqual(status["current_manifest"]["approval"]["active"], False)
        queue = self.cli(
            "acquisition", "queue",
            "--freeze-artifact-id", self.freeze_ref["artifact_id"],
            "--freeze-revision-id", self.freeze_ref["revision_id"],
        )
        self.assertEqual(queue["by_code"], {"source_superseded": 1})
        # Earlier work retained and the unrelated manifest still holds.
        history = self.cli("acquisition", "manifest", "history")
        self.assertEqual(history["count"], 1)
        self.cli("acquisition", "manifest", "require", "--artifact-id", "other.manifest")

    def test_exact_replay_returns_same_revision_and_divergence_conflicts(self):
        entries = self.secret_entries()
        rid, first = self.cli_propose(entries, request_id="man-r")
        again_rid = AcquisitionRepository.new_revision_id()
        _r, replay = self.cli_propose(entries, request_id="man-r", revision_id=again_rid)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["manifest"]["manifest_ref"],
                         first["manifest"]["manifest_ref"])
        self.assertEqual(replay["manifest"]["manifest_ref"]["revision_id"], rid)
        changed = [dict(entries[0], rationale="a different rationale"), entries[1]]
        err = self.cli_propose(changed, request_id="man-r", expect=_CONFLICT)[1]
        self.assertEqual(err["error"]["category"], "conflict")
        self.assertEqual(self.cli("acquisition", "manifest", "history")["count"], 1)

    # -- readbacks and privacy ---------------------------------------------- #
    def test_default_readbacks_are_safe_and_content_is_private_opt_in(self):
        entries = self.secret_entries()
        _rid, out = self.cli_propose(entries)
        self.gate(self.rev(out))
        # the frozen candidates' private pmid identifiers must not be exposed
        private_values = {value for value in self.cands}
        for blob in (
            json.dumps(out),
            json.dumps(self.cli("acquisition", "manifest", "show")),
            json.dumps(self.cli("acquisition", "manifest", "status")),
            json.dumps(self.cli("acquisition", "manifest", "history")),
        ):
            self.assertNotIn(_SECRET, blob)
            self.assertNotIn(_SECRET2, blob)
            self.assertEqual(
                _private_identifier_paths(json.loads(blob), private_values),
                [],
                blob,
            )
        shown = self.cli("acquisition", "manifest", "show")
        entry = next(e for e in shown["manifest"]["entries"] if e["candidate_id"] == self.a)
        self.assertEqual(entry["validation_status"], "ready")
        self.assertFalse(shown["manifest"]["private_content_included"])
        exact = self.cli(
            "acquisition", "manifest", "show", "--revision-id",
            out["manifest"]["manifest_ref"]["revision_id"],
        )
        self.assertEqual(exact["manifest"]["content_sha256"],
                         out["manifest"]["content_sha256"])
        private = self.cli("acquisition", "manifest", "show", "--content",
                           allow_secret=True)
        self.assertTrue(private["manifest"]["private_content_included"])
        self.assertIn(_SECRET, json.dumps(private["document"]))
        self.cli("acquisition", "manifest", "show", "--revision-id", "nope",
                 expect=_VALID)

    def test_private_identifier_check_allows_opaque_ids_but_rejects_pmid(self):
        # An opaque id/hash that merely contains "111" is not a leak.
        opaque = {
            "candidate_id": "cand_1110deadbeef1111",
            "revision_id": "rev_111abcdef",
            "content_sha256": "sha256:" + "1" * 64,
        }
        self.assertEqual(_private_identifier_paths(opaque, {"111"}), [])
        # A real private field name, or an exact private value, is flagged.
        self.assertEqual(
            _private_identifier_paths({"entry": {"pmid": "111"}}, {"111"}),
            ["$.entry.pmid"],
        )
        self.assertEqual(
            _private_identifier_paths(
                {"e": {"expected_identifiers": {"pmid": "111"}}}, {"111"}
            ),
            ["$.e.expected_identifiers"],
        )
        self.assertEqual(
            _private_identifier_paths({"note": "111"}, {"111"}), ["$.note"]
        )
        self.assertEqual(
            _private_identifier_paths({"observed_identifiers": []}, {"111"}),
            ["$.observed_identifiers"],
        )

    def test_show_without_any_manifest_is_a_static_refusal(self):
        self.setup_frozen()
        err = self.cli("acquisition", "manifest", "show", expect=_VALID)
        self.assertEqual(err["error"]["category"], "validation")
        self.assertEqual(self.cli("acquisition", "manifest", "history")["count"], 0)
        status = self.cli("acquisition", "manifest", "status")
        self.assertIsNone(status["current_manifest"])
        self.assertFalse(status["acquired"])

    # -- refusals ------------------------------------------------------------ #
    def test_malformed_side_input_is_refused_without_writing(self):
        entries = self.secret_entries()
        rid = AcquisitionRepository.new_revision_id()
        rights_path = self.write_json("rights.json", self.rights("acquisition.manifest", rid))

        def args(entries_path, rights=rights_path):
            return [
                "acquisition", "manifest", "propose",
                "--freeze-artifact-id", self.freeze_ref["artifact_id"],
                "--freeze-revision-id", self.freeze_ref["revision_id"],
                "--entries-file", entries_path, "--rights-file", rights,
                "--revision-id", rid, "--request-id", "man-bad",
            ]

        # not a list, bad json, missing, symlink, oversized, forged authority key
        self.cli(*args(self.write_json("obj.json", {"a": 1})), expect=_VALID)
        bad = self.tmp / "bad.json"
        bad.write_text("{not json" + _SECRET, encoding="utf-8")
        self.cli(*args(str(bad)), expect=_VALID)
        self.cli(*args(str(self.tmp / "missing.json")), expect=_VALID)
        good = self.write_json("good.json", entries)
        link = self.tmp / "link.json"
        os.symlink(good, link)
        self.cli(*args(str(link)), expect=_VALID)
        huge = self.tmp / "huge.json"
        huge.write_text("[" + " " * (1 << 21) + "]", encoding="utf-8")
        self.cli(*args(str(huge)), expect=_VALID)
        forged = [dict(entries[0], approved=True, actor="owner"), entries[1]]
        self.cli(*args(self.write_json("forged.json", forged)), expect=_VALID)
        self.cli(*args(good, rights=self.write_json("rl.json", [1])), expect=_VALID)
        # a rights record for another revision cannot bind this one
        self.cli(*args(good, rights=self.write_json(
            "rw.json", self.rights("acquisition.manifest", "other-rev"))), expect=_VALID)
        # incomplete roster is refused
        self.cli(*args(self.write_json("short.json", entries[:1])), expect=_VALID)
        # nothing was ever written
        self.assertEqual(self.cli("acquisition", "manifest", "history")["count"], 0)

    def test_unknown_project_is_refused_and_store_is_closed_on_every_path(self):
        entries = self.secret_entries()
        opened: list[object] = []
        closed: list[object] = []
        real_open, real_close = ProjectStore.open_at, ProjectStore.close

        def spy_open(*a, **kw):
            store = real_open(*a, **kw)
            opened.append(store)
            return store

        def spy_close(store):
            closed.append(store)
            return real_close(store)

        with mock.patch.object(ProjectStore, "open_at", staticmethod(spy_open)), \
                mock.patch.object(ProjectStore, "close", spy_close):
            self.cli_propose(entries)
            self.cli("acquisition", "manifest", "require", expect=_VALID)
            self.cli("acquisition", "manifest", "show")
            self.cli("acquisition", "manifest", "show", "--revision-id", "nope",
                     expect=_VALID)
            self.cli("acquisition", "queue", "--freeze-artifact-id", "x",
                     "--freeze-revision-id", "y", expect=_VALID)
        self.assertGreaterEqual(len(opened), 5)
        self.assertEqual(len(opened), len(closed))
        code, _out, err = self._run(
            "acquisition", "manifest", "status", "--project", "no-such-project"
        )
        self.assertNotEqual(code, _OK)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
