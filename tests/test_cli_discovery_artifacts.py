"""In-process CLI acceptance tests for the ``discovery`` artifact group (T-CLI0).

Covers ``src/review_article_agent/cli/discovery.py`` against Step 05 D1
acceptance criteria 1 and 2 ("Seed abstracts remain provisional; unapproved seeds
cannot silently become accepted project context" and "Broad searches cannot
execute unapproved scope/query plans. A substantive change produces an
impact/approval request"), the personal-CLI baseline, and the existing
``context``/``candidates``/``lead_runner`` APIs.

Every case drives the real CLI entry point in-process (``cli.main.main`` with
in-memory streams) over a **real temporary registry, project store, and the
integrated discovery value layers**. Synthetic data only: no network, no provider,
no model call, and no real approval.

Asserted here:

* importing and reading every context document type (brief/interview/seed/
  framing/scope/rubric/plan), with the exact derived dependencies and the
  ``rubric`` artifact kind;
* a seed's pending ``artifact_revision``/``discovery.seed.<id>`` approval request
  and provisional summary, and a plan's ``protocol``/``discovery.plan`` request
  with its impact review;
* malformed / in-band-approved / wrong-typed / cross-project / stale inputs and a
  mismatched rights record all fail cleanly and write nothing;
* plan amendments require a real parent and a non-empty impact review, and a
  superseded revision is readable only with ``--historical``;
* corpus ingest counts/replay/conflict and summary privacy (no titles/tokens);
* lead status/content: a diagnostic outcome is never shown as a proposal, and a
  completed proposal is private and non-authorizing.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p \\
        'test_cli_discovery_artifacts.py'
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from review_article_agent.cli import main as cli_main  # noqa: E402
from review_article_agent.discovery import lead_runner as runner  # noqa: E402
from review_article_agent.discovery.records import (  # noqa: E402
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)

import test_discovery_lead_runner as leadfx  # noqa: E402
import test_discovery_serialization as fx  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_SEED_ABSTRACT = "SYNTHETIC-ABSTRACT-DO-NOT-ECHO"
_CORPUS_TITLE = "SYNTHETIC-TITLE-DO-NOT-ECHO"
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000


# --------------------------------------------------------------------------- #
# synthetic document builders
# --------------------------------------------------------------------------- #
def _envelope(document_type: str, payload: dict) -> dict:
    return {"schema_version": 1, "document_type": document_type, "payload": payload}


def _brief_payload(objective: str = "obj") -> dict:
    return {"objective": objective, "starting_hypothesis": "hyp", "questions": ["q?"]}


def _interview_payload(brief_ref: dict, *, ready_to_advance: bool = True) -> dict:
    return {
        "brief_ref": brief_ref,
        "turns": [{"question": "q", "answer": "a"}],
        "ready_to_advance": ready_to_advance,
    }


def _seed_payload(run_id: str, *, record_index: int = 0, abstract=_SEED_ABSTRACT) -> dict:
    return {
        "run_id": run_id,
        "record_index": record_index,
        "abstract": abstract,
        "abstract_context": "provisional",
        "limitations": ["synthetic"],
    }


def _framing_payload() -> dict:
    return {"alternatives": ["alt"], "counterevidence_queries": ["q"], "limitations": []}


def _scope_payload() -> dict:
    return {
        "scope": "s",
        "date_boundary": "none",
        "language_boundary": "english",
        "source_boundary": ["none"],
        "intended_synthesis": "syn",
        "terminology": [],
        "exclusions": [],
    }


def _rubric_payload() -> dict:
    return {"eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": []}


def _plan_payload(refs: dict, *, impact_review: dict | None = None) -> dict:
    return {
        "protocol": "protocol-1",
        "brief_ref": refs["brief_ref"],
        "interview_ref": refs["interview_ref"],
        "seed_refs": list(refs["seed_refs"]),
        "framing_ref": refs["framing_ref"],
        "scope_ref": refs["scope_ref"],
        "rubric_ref": refs["rubric_ref"],
        "queries": [{"source": "pubmed", "query": "q", "page_size": 20, "max_pages": 2}],
        "complementary_sources": [],
        "expansion": {
            "max_hops": 1,
            "max_sources": 5,
            "max_requests": 10,
            "page_size": 20,
            "max_pages": 3,
        },
        "impact_review": impact_review
        or {"previous_plan": None, "affected": [], "rationale": ""},
    }


def _rights_record(project_id: str, artifact_id: str, revision_id: str, **overrides) -> dict:
    record = {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------- #
# base
# --------------------------------------------------------------------------- #
class DiscoveryCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-disc-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.cwd = self.tmp / "elsewhere"
        self.cwd.mkdir()

    # -- CLI driving --------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(
            code, expect, f"args={args} rc={code}\nstdout={out}\nstderr={err}"
        )
        if expect == 0:
            return json.loads(out)
        self.assertNotIn("Traceback", err)
        return json.loads(err)

    def cli_err(self, *args: str, expect: int) -> dict:
        return self.cli(*args, expect=expect)

    # -- fixtures ------------------------------------------------------------ #
    def write_json(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def create_project(self, name: str = "proj") -> str:
        out = self.cli("create", str(self.tmp / name), "--name", name)
        return out["project"]["project_id"]

    def ref(self, pid: str, artifact_id: str, revision_id: str) -> dict:
        return {
            "project_id": pid,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def store(self, pid: str) -> ProjectStore:
        info = Registry(str(self.state)).get(pid)
        return ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=pid
        )

    def with_store(self, pid: str, fn):
        store = self.store(pid)
        try:
            return fn(store)
        finally:
            store.close()

    def record_run(self, pid: str, request_id: str, records):
        def _record(store) -> str:
            page = SearchPage(
                source="pubmed",
                query="q",
                requested_url="https://eutils.ncbi.nlm.nih.gov/fixture",
                retrieved_at="2026-01-01T00:00:00+00:00",
                page_index=0,
                records=tuple(records),
                page_size=20,
                total_count=len(records),
                complete=True,
                stop_reason="completed",
            )
            result = SearchResult.from_pages(
                source="pubmed",
                query="q",
                pages=[page],
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
                stop_reason="completed",
                total_count=len(records),
            )
            return DiscoveryRepository(store).record_search(
                request_id=request_id, result=result
            ).run_id

        return self.with_store(pid, _record)

    # -- discovery wrappers -------------------------------------------------- #
    def context_import(
        self,
        *,
        pid,
        artifact_id,
        revision_id,
        document,
        rights=None,
        parent=None,
        request_id=None,
        extra=(),
        expect=0,
    ):
        doc_path = self.write_json(f"ctx-{artifact_id}-{revision_id}.json", document)
        rights = (
            rights
            if rights is not None
            else _rights_record(pid, artifact_id, revision_id)
        )
        rights_path = self.write_json(f"rights-{artifact_id}-{revision_id}.json", rights)
        args = [
            "discovery",
            "context",
            "import",
            "--project",
            pid,
            "--artifact-id",
            artifact_id,
            "--revision-id",
            revision_id,
            "--file",
            str(doc_path),
            "--rights-file",
            str(rights_path),
        ]
        if parent is not None:
            args += ["--parent-revision-id", parent]
        if request_id is not None:
            args += ["--request-id", request_id]
        args += list(extra)
        return self.cli(*args, expect=expect)

    def context_show(self, *, pid, artifact_id, expect=0, extra=()):
        return self.cli(
            "discovery",
            "context",
            "show",
            "--project",
            pid,
            "--artifact-id",
            artifact_id,
            *extra,
            expect=expect,
        )

    def corpus_ingest(
        self, *, pid, revision_id, request_id, run_ids, rights=None, expect=0
    ):
        rights = (
            rights
            if rights is not None
            else _rights_record(pid, "discovery.corpus", revision_id)
        )
        rights_path = self.write_json(f"corpus-rights-{revision_id}.json", rights)
        args = [
            "discovery",
            "corpus",
            "ingest",
            "--project",
            pid,
            "--revision-id",
            revision_id,
            "--request-id",
            request_id,
            "--rights-file",
            str(rights_path),
        ]
        for run_id in run_ids:
            args += ["--run-id", run_id]
        return self.cli(*args, expect=expect)

    # -- a full context fixture ---------------------------------------------- #
    def build_plan_fixture(self, pid: str, *, plan_revision: str = "rev-plan", seed_revision: str = "rev-seed"):
        run_id = self.record_run(pid, "dsr_r1", (fx.record(),))
        brief = self.context_import(
            pid=pid,
            artifact_id="a-brief",
            revision_id="rev-brief",
            document=_envelope("discovery_brief", _brief_payload()),
        )
        self.assertEqual(brief["revision"]["document_type"], "discovery_brief")
        interview = self.context_import(
            pid=pid,
            artifact_id="a-interview",
            revision_id="rev-interview",
            document=_envelope(
                "discovery_interview",
                _interview_payload(self.ref(pid, "a-brief", "rev-brief")),
            ),
        )
        seed = self.context_import(
            pid=pid,
            artifact_id="a-seed",
            revision_id=seed_revision,
            document=_envelope("discovery_seed", _seed_payload(run_id)),
        )
        self.assertIsNotNone(seed["approval_request"])
        for artifact_id, document_type, payload in (
            ("a-framing", "discovery_framing", _framing_payload()),
            ("a-scope", "discovery_scope", _scope_payload()),
            ("a-rubric", "discovery_rubric", _rubric_payload()),
        ):
            self.context_import(
                pid=pid,
                artifact_id=artifact_id,
                revision_id="rev-" + artifact_id[2:],
                document=_envelope(document_type, payload),
            )
        refs = {
            "brief_ref": self.ref(pid, "a-brief", "rev-brief"),
            "interview_ref": self.ref(pid, "a-interview", "rev-interview"),
            "seed_refs": [self.ref(pid, "a-seed", seed_revision)],
            "framing_ref": self.ref(pid, "a-framing", "rev-framing"),
            "scope_ref": self.ref(pid, "a-scope", "rev-scope"),
            "rubric_ref": self.ref(pid, "a-rubric", "rev-rubric"),
        }
        return refs, interview


# --------------------------------------------------------------------------- #
# context import / show / validate over every type
# --------------------------------------------------------------------------- #
class ContextReadAllTypesTest(DiscoveryCliTestCase):
    def test_import_and_read_every_context_type(self) -> None:
        pid = self.create_project()
        refs, _interview = self.build_plan_fixture(pid)
        plan = self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan",
            document=_envelope("discovery_plan", _plan_payload(refs)),
        )
        self.assertEqual(plan["revision"]["document_type"], "discovery_plan")
        self.assertEqual(plan["revision"]["scientific_state"], "proposal")

        expected = {
            "a-brief": "discovery_brief",
            "a-interview": "discovery_interview",
            "a-seed": "discovery_seed",
            "a-framing": "discovery_framing",
            "a-scope": "discovery_scope",
            "a-rubric": "discovery_rubric",
            "a-plan": "discovery_plan",
        }
        for artifact_id, document_type in expected.items():
            shown = self.context_show(pid=pid, artifact_id=artifact_id)["context"]
            self.assertEqual(shown["document_type"], document_type)
            self.assertEqual(shown["artifact_id"], artifact_id)
            self.assertFalse(shown["authorizes_execution"])
            self.assertFalse(shown["scientific_validation"])
            # ``validate`` is the same typed store read.
            self.assertEqual(
                self.cli(
                    "discovery",
                    "context",
                    "validate",
                    "--project",
                    pid,
                    "--artifact-id",
                    artifact_id,
                )["context"]["document_type"],
                document_type,
            )

    def test_seed_approval_request_pending_and_provisional(self) -> None:
        pid = self.create_project()
        run_id = self.record_run(pid, "dsr_r1", (fx.record(),))
        out = self.context_import(
            pid=pid,
            artifact_id="a-seed",
            revision_id="rev-seed",
            document=_envelope("discovery_seed", _seed_payload(run_id)),
        )
        request = out["approval_request"]
        self.assertEqual(request["gate_kind"], "artifact_revision")
        self.assertEqual(request["gate_id"], "discovery.seed.a-seed")
        self.assertEqual(request["revision_id"], "rev-seed")
        self.assertIs(request["satisfied"], False)
        self.assertIsNone(request["impact_review"])

        summary = self.context_show(pid=pid, artifact_id="a-seed")["context"]
        self.assertIs(summary["provisional"], True)
        self.assertIs(summary["needs_approval"], True)
        self.assertIs(summary["gate_satisfied"], False)
        # The default summary never echoes the raw abstract.
        self.assertNotIn(_SEED_ABSTRACT, json.dumps(summary))
        # ``--content`` is the explicit opt-in and returns the document.
        content = self.cli(
            "discovery",
            "context",
            "show",
            "--project",
            pid,
            "--artifact-id",
            "a-seed",
            "--content",
        )
        self.assertIs(content["private"], True)
        self.assertEqual(content["document"]["payload"]["abstract"], _SEED_ABSTRACT)

    def test_plan_approval_request_and_exact_dependencies(self) -> None:
        pid = self.create_project()
        refs, _interview = self.build_plan_fixture(pid)
        out = self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan",
            document=_envelope("discovery_plan", _plan_payload(refs)),
        )
        request = out["approval_request"]
        self.assertEqual(request["gate_kind"], "protocol")
        self.assertEqual(request["gate_id"], "discovery.plan")
        self.assertIs(request["satisfied"], False)
        self.assertEqual(
            request["impact_review"],
            {"previous_plan": None, "affected": [], "rationale": ""},
        )
        # The plan pins exactly its brief/interview/framing/scope/rubric and seed.
        deps = {
            (d["artifact_id"], d["revision_id"])
            for d in out["revision"]["dependencies"]
        }
        self.assertEqual(
            deps,
            {
                ("a-brief", "rev-brief"),
                ("a-interview", "rev-interview"),
                ("a-seed", "rev-seed"),
                ("a-framing", "rev-framing"),
                ("a-scope", "rev-scope"),
                ("a-rubric", "rev-rubric"),
            },
        )
        # The rubric is stored on a ``rubric``-kind artifact.
        artifacts = self.cli("artifact", "list", "--project", pid)["artifacts"]
        kinds = {a["artifact_id"]: a["kind"] for a in artifacts}
        self.assertEqual(kinds["a-rubric"], "rubric")
        self.assertEqual(kinds["a-brief"], "report")


# --------------------------------------------------------------------------- #
# validation / rejection
# --------------------------------------------------------------------------- #
class ContextRejectionTest(DiscoveryCliTestCase):
    def test_inband_approval_and_malformed_json_are_rejected(self) -> None:
        pid = self.create_project()
        forged = _envelope("discovery_brief", {**_brief_payload(), "approved": True})
        err = self.context_import(
            pid=pid,
            artifact_id="a-brief",
            revision_id="rev-brief",
            document=forged,
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")
        # A forged approval field means the artifact was never written.
        self.assertEqual(
            self.cli("artifact", "list", "--project", pid)["artifacts"], []
        )

        # Duplicate JSON keys are refused by the strict parser (no payload echo).
        bad = self.tmp / "dup.json"
        bad.write_text(
            '{"schema_version":1,"document_type":"discovery_brief",'
            '"document_type":"x","payload":{}}',
            encoding="utf-8",
        )
        rights = self.write_json("rights-dup.json", _rights_record(pid, "a-x", "rev-x"))
        proc = self._run(
            "discovery", "context", "import", "--project", pid,
            "--artifact-id", "a-x", "--revision-id", "rev-x",
            "--file", str(bad), "--rights-file", str(rights),
        )
        self.assertEqual(proc[0], 2)
        self.assertNotIn("Traceback", proc[2])
        self.assertEqual(json.loads(proc[2])["error"]["category"], "validation")

    def test_wrong_typed_and_missing_references_are_rejected(self) -> None:
        pid = self.create_project()
        # An interview that references a non-brief document is refused.
        self.context_import(
            pid=pid,
            artifact_id="a-scope",
            revision_id="rev-scope",
            document=_envelope("discovery_scope", _scope_payload()),
        )
        err = self.context_import(
            pid=pid,
            artifact_id="a-interview",
            revision_id="rev-interview",
            document=_envelope(
                "discovery_interview",
                _interview_payload(self.ref(pid, "a-scope", "rev-scope")),
            ),
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")
        # A missing reference is likewise refused.
        err = self.context_import(
            pid=pid,
            artifact_id="a-interview2",
            revision_id="rev-interview2",
            document=_envelope(
                "discovery_interview",
                _interview_payload(self.ref(pid, "a-brief", "rev-absent")),
            ),
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_seed_run_index_must_resolve(self) -> None:
        pid = self.create_project()
        err = self.context_import(
            pid=pid,
            artifact_id="a-seed",
            revision_id="rev-seed",
            document=_envelope("discovery_seed", _seed_payload("dsr_missing")),
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_rights_mismatch_is_rejected_and_revision_not_written(self) -> None:
        pid = self.create_project()
        # A record whose subject binds a *different* revision is refused.
        wrong = _rights_record(pid, "a-brief", "rev-other")
        err = self.context_import(
            pid=pid,
            artifact_id="a-brief",
            revision_id="rev-brief",
            document=_envelope("discovery_brief", _brief_payload()),
            rights=wrong,
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")
        revisions = self.artifact_revisions(pid, "a-brief")
        self.assertEqual(revisions, [])

    def test_rubric_on_non_rubric_artifact_is_refused(self) -> None:
        pid = self.create_project()
        # An existing report-kind artifact cannot take a rubric document.
        self.cli(
            "artifact", "create", "--project", pid,
            "--artifact-id", "a-report", "--kind", "report",
        )
        err = self.context_import(
            pid=pid,
            artifact_id="a-report",
            revision_id="rev-rubric",
            document=_envelope("discovery_rubric", _rubric_payload()),
            expect=3,
        )
        self.assertEqual(err["error"]["category"], "conflict")
        # A rubric stored on a non-rubric artifact via the generic import also
        # fails the typed read.
        doc = self.write_json(
            "rubric-generic.json", _envelope("discovery_rubric", _rubric_payload())
        )
        rights = self.write_json(
            "rights-generic.json", _rights_record(pid, "a-report", "rev-generic")
        )
        self.cli(
            "artifact", "import", "--project", pid, "--artifact-id", "a-report",
            "--revision-id", "rev-generic", "--file", str(doc),
            "--rights-file", str(rights),
        )
        err = self.context_show(pid=pid, artifact_id="a-report", expect=2)
        self.assertEqual(err["error"]["category"], "validation")

    def artifact_revisions(self, pid: str, artifact_id: str):
        out = self.cli(
            "artifact", "revisions", artifact_id, "--project", pid
        )
        return out["revisions"]


# --------------------------------------------------------------------------- #
# plan amendments + historical reads
# --------------------------------------------------------------------------- #
class PlanAmendmentTest(DiscoveryCliTestCase):
    def test_amended_plan_requires_real_parent_and_nonempty_impact(self) -> None:
        pid = self.create_project()
        refs, _ = self.build_plan_fixture(pid)
        self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan",
            document=_envelope("discovery_plan", _plan_payload(refs)),
        )
        plan_ref = self.ref(pid, "a-plan", "rev-plan")

        # A parent with a null previous_plan is refused.
        err = self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan2",
            document=_envelope("discovery_plan", _plan_payload(refs)),
            parent="rev-plan",
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

        # A previous_plan that is not the exact parent is refused.
        err = self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan3",
            document=_envelope(
                "discovery_plan",
                _plan_payload(
                    refs,
                    impact_review={
                        "previous_plan": self.ref(pid, "a-plan", "rev-other"),
                        "affected": ["scope"],
                        "rationale": "r",
                    },
                ),
            ),
            parent="rev-plan",
            expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

        # A correct substantive amendment is stored and requests reapproval.
        out = self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan4",
            document=_envelope(
                "discovery_plan",
                _plan_payload(
                    refs,
                    impact_review={
                        "previous_plan": plan_ref,
                        "affected": ["scope"],
                        "rationale": "broaden the scope",
                    },
                ),
            ),
            parent="rev-plan",
        )
        self.assertEqual(out["revision"]["parent_revision_id"], "rev-plan")
        self.assertIs(out["approval_request"]["satisfied"], False)
        self.assertEqual(
            out["approval_request"]["impact_review"]["previous_plan"], plan_ref
        )

    def test_stale_revision_is_readable_only_historically(self) -> None:
        pid = self.create_project()
        refs, _ = self.build_plan_fixture(pid)
        self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan",
            document=_envelope("discovery_plan", _plan_payload(refs)),
        )
        plan_ref = self.ref(pid, "a-plan", "rev-plan")
        self.context_import(
            pid=pid,
            artifact_id="a-plan",
            revision_id="rev-plan2",
            document=_envelope(
                "discovery_plan",
                _plan_payload(
                    refs,
                    impact_review={
                        "previous_plan": plan_ref,
                        "affected": ["scope"],
                        "rationale": "r",
                    },
                ),
            ),
            parent="rev-plan",
        )
        # The superseded revision is a stale reference by default ...
        err = self.cli(
            "discovery", "context", "show", "--project", pid,
            "--artifact-id", "a-plan", "--revision-id", "rev-plan", expect=3,
        )
        self.assertEqual(err["error"]["category"], "conflict")
        # ... and is readable with --historical (no current authority).
        historical = self.cli(
            "discovery", "context", "show", "--project", pid,
            "--artifact-id", "a-plan", "--revision-id", "rev-plan", "--historical",
        )["context"]
        self.assertEqual(historical["revision_id"], "rev-plan")
        self.assertIs(historical["historical"], True)
        self.assertIs(historical["authorizes_execution"], False)


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #
class CorpusTest(DiscoveryCliTestCase):
    def test_corpus_ingest_summary_replay_and_conflict(self) -> None:
        pid = self.create_project()
        run_a = self.record_run(pid, "dsr_r1", (fx.record(title=_CORPUS_TITLE),))
        run_b = self.record_run(
            pid,
            "dsr_r2",
            (fx.record(), fx.record(source_id="999999", pmid="999999", title="b")),
        )
        out = self.corpus_ingest(
            pid=pid, revision_id="rev-c1", request_id="c-1", run_ids=[run_a]
        )
        summary = out["corpus"]
        self.assertEqual(summary["revision_id"], "rev-c1")
        self.assertEqual(summary["run_count"], 1)
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual(summary["conflict_count"], 0)
        self.assertIs(summary["unresolved"], False)
        # The default summary never echoes a raw title.
        self.assertNotIn(_CORPUS_TITLE, json.dumps(out))

        # An identical replay of the same request key returns the same revision.
        replay = self.corpus_ingest(
            pid=pid, revision_id="rev-c1", request_id="c-1", run_ids=[run_a]
        )
        self.assertEqual(replay["corpus"]["revision_id"], "rev-c1")
        self.assertEqual(
            replay["corpus"]["content_sha256"], summary["content_sha256"]
        )

        # A divergent reuse of the same request key conflicts.
        err = self.corpus_ingest(
            pid=pid,
            revision_id="rev-c2",
            request_id="c-1",
            run_ids=[run_b],
            expect=3,
        )
        self.assertEqual(err["error"]["category"], "conflict")

    def test_corpus_show_content_opt_in_and_missing(self) -> None:
        pid = self.create_project()
        run_a = self.record_run(pid, "dsr_r1", (fx.record(title=_CORPUS_TITLE),))
        self.corpus_ingest(
            pid=pid, revision_id="rev-c1", request_id="c-1", run_ids=[run_a]
        )
        summary = self.cli("discovery", "corpus", "show", "--project", pid)
        self.assertNotIn("candidates", summary["corpus"])
        self.assertNotIn(_CORPUS_TITLE, json.dumps(summary))
        content = self.cli(
            "discovery", "corpus", "show", "--project", pid, "--content"
        )
        self.assertIs(content["private"], True)
        self.assertIn("candidates", content["document"])
        # No snapshot exists for an unknown revision.
        err = self.cli(
            "discovery", "corpus", "show", "--project", pid,
            "--revision-id", "rev-absent", expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_corpus_ingest_requires_a_run(self) -> None:
        pid = self.create_project()
        err = self.corpus_ingest(
            pid=pid, revision_id="rev-c1", request_id="c-1", run_ids=[], expect=2
        )
        self.assertEqual(err["error"]["category"], "validation")


# --------------------------------------------------------------------------- #
# lead status / content
# --------------------------------------------------------------------------- #
class LeadStatusContentTest(DiscoveryCliTestCase):
    def _install_owner_auth(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: _FIXED_CHALLENGE),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_real_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_utcnow", lambda: leadfx._BASE_TIME),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: leadfx._FakeTty(replies=[_FIXED_CHALLENGE]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run_lead(self, pid: str, request_id: str, *, refusal: bool = False) -> None:
        """Build a synthetic hosted-use request and run it (no network)."""
        self._install_owner_auth()
        registry = Registry(str(self.state))
        brief_ref = self.ref(pid, "a-brief", "rev-brief")
        self.context_import(
            pid=pid,
            artifact_id="a-brief",
            revision_id="rev-brief",
            document=_envelope("discovery_brief", _brief_payload("synthetic objective")),
            rights=leadfx._rights(pid, "a-brief", "rev-brief"),
        )
        batch_ref = self.ref(pid, "a-batch", "rev-batch")
        store = self.store(pid)
        try:
            artifacts = ArtifactRepository(store)
            artifacts.create_artifact("a-batch", kind="report")
            artifacts.import_revision(
                artifact_id="a-batch",
                content=leadfx._hosted_use_document(
                    "interview", leadfx._config_map(), [brief_ref]
                ),
                origin_locator="import:synthetic",
                rights=leadfx._rights(pid, "a-batch", "rev-batch"),
                revision_id="rev-batch",
                dependencies=[brief_ref],
            )
            prepared = prepare_gate_snapshot(
                store,
                batch_ref,
                gate_kind="batch",
                gate_id=runner.hosted_use_gate_id("interview"),
                decision="approved",
                rationale="synthetic hosted-use gate",
            )
            capability = owner_auth.request_owner_confirmation(prepared.owner_action())
            assert isinstance(capability, OwnerApproval)
            HumanApprovalRepository(store).record_decision(prepared, capability)
            token = registry.activate_execution(pid).activation_token
            transport = leadfx._FakeTransport()
            if refusal:
                transport.responses.append(
                    leadfx.TransportResponse(
                        status=200,
                        body_text=json.dumps(
                            {
                                "id": "gen-refuse",
                                "model": leadfx._MODEL_ID,
                                "choices": [
                                    {
                                        "finish_reason": "content_filter",
                                        "message": {"role": "assistant", "content": ""},
                                    }
                                ],
                            }
                        ),
                    )
                )
            else:
                transport.responses.append(
                    leadfx.TransportResponse(
                        status=200, body_text=leadfx._completion_body()
                    )
                )
            runner.execute_lead_proposal(
                store,
                registry=registry,
                activation_token=token,
                batch_ref=batch_ref,
                request_id=request_id,
                api_key="sk-or-test-key",
                transport=transport,
            )
        finally:
            store.close()

    def test_unknown_request_is_a_clean_error(self) -> None:
        pid = self.create_project()
        err = self.cli(
            "discovery", "lead", "status", "--project", pid,
            "--request-id", "req-unknown", expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")
        err = self.cli(
            "discovery", "lead", "content", "--project", pid,
            "--request-id", "req-unknown", expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_diagnostic_outcome_is_never_a_proposal(self) -> None:
        pid = self.create_project()
        self._run_lead(pid, "req-refused", refusal=True)
        status = self.cli(
            "discovery", "lead", "status", "--project", pid,
            "--request-id", "req-refused",
        )["lead_status"]
        self.assertEqual(status["status"], "refused")
        self.assertIsNone(status["proposal_id"])
        self.assertIsNone(status["proposal_record_id"])
        self.assertIs(status["no_automatic_retry"], True)
        # The private proposal text is refused for a diagnostic outcome.
        err = self.cli(
            "discovery", "lead", "content", "--project", pid,
            "--request-id", "req-refused", expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_completed_proposal_content_is_private_and_non_authorizing(self) -> None:
        pid = self.create_project()
        self._run_lead(pid, "req-ok")
        out = self.cli(
            "discovery", "lead", "content", "--project", pid,
            "--request-id", "req-ok",
        )
        self.assertIs(out["private"], True)
        self.assertIs(out["non_authorizing"], True)
        self.assertIs(out["authorizes_execution"], False)
        self.assertIs(out["authorizes_export"], False)
        self.assertIs(out["scientific_validation"], False)
        self.assertEqual(out["proposal_text"], "synthetic lead proposal")
        self.assertEqual(out["proposal"]["role"], "lead")
        # Neither the API key nor the source text leaks into the output.
        blob = json.dumps(out)
        self.assertNotIn("sk-or-test-key", blob)
        self.assertNotIn("synthetic objective", blob)


# --------------------------------------------------------------------------- #
# dispatch / help / no approval surface
# --------------------------------------------------------------------------- #
class DiscoveryDispatchTest(DiscoveryCliTestCase):
    def _help(self, *args: str) -> str:
        """Return the help text argparse prints for ``args`` (help exits via
        ``SystemExit``, which is captured here rather than escaping the test)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as caught:
                cli_main.main(
                    ["--data-dir", str(self.state), *args],
                    env={},
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
        self.assertEqual(caught.exception.code, 0)
        return buf.getvalue()

    def test_help_and_command_tree(self) -> None:
        self.assertIn("discovery", self._help("--help"))
        for args, needle in (
            (("discovery", "--help"), "context"),
            (("discovery", "context", "--help"), "import"),
            (("discovery", "corpus", "--help"), "ingest"),
            (("discovery", "lead", "--help"), "status"),
            (("discovery", "context", "import", "--help"), "--rights-file"),
        ):
            self.assertIn(needle, self._help(*args))

    def test_no_approval_or_actor_flag_surface_exists(self) -> None:
        pid = self.create_project()
        # No ``discovery ... approve`` command exists.
        err = self.cli("discovery", "context", "approve", expect=2)
        self.assertEqual(err["error"]["category"], "usage")
        # A stray actor/approved flag is rejected as an unknown argument.
        proc = self._run(
            "discovery", "context", "show", "--project", pid,
            "--artifact-id", "a-brief", "--approved",
        )
        self.assertEqual(proc[0], 2)
        self.assertNotIn("Traceback", proc[2])
        self.assertEqual(json.loads(proc[2])["error"]["category"], "usage")

    def test_discovery_without_subcommand_is_usage_error(self) -> None:
        err = self.cli("discovery", expect=2)
        self.assertEqual(err["error"]["category"], "usage")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
