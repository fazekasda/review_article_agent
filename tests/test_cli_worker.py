"""CLI acceptance tests for the ``worker start|status|stop`` lifecycle surface.

Covers the Step 03 deliverable "Local worker independent of CLI lifetime;
supported start/status/.../stop behavior and authenticated local control"
(deliverable 1) and the acceptance criterion "Authentication rejects non-owner
control; no root or public unauthenticated listener is required", plus
``docs/contracts/02-gates-and-runtime.md`` §8.4 ("Close CLI (detached)": CLI
lifetime ends while an authorized background job continues) and the existing
``docs/cli.md`` output/exit/secret conventions.

Two layers:

* Most cases drive ``cli.main.main`` **in process** and substitute the public
  ``review_article_agent.worker.process`` launcher functions, so every outcome
  (running, unavailable, foreign, unconfirmed) is deterministic and no engine
  entry module is required — none is added here.
* One integration case runs a real, owned, in-process UDS fixture service
  (``review_article_agent.runtime.ipc``) to prove the CLI drives the *real*
  ``worker_status``/``stop_worker`` over a real control socket.

The real detached-subprocess lifecycle proof stays in
``tests/test_worker_process.py``; this file does not duplicate that harness.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.cli import worker as cli_worker  # noqa: E402
from review_article_agent.runtime import ipc  # noqa: E402
from review_article_agent.worker import process  # noqa: E402

SECRET = "SYNTHETIC-SECRET-DO-NOT-ECHO"


def _run(argv, *, env=None):
    """Run the CLI in process; return ``(exit_code, stdout_text, stderr_text)``."""
    out, err = io.StringIO(), io.StringIO()
    try:
        code = cli_main.main(
            list(argv), env={} if env is None else env, stdout=out, stderr=err
        )
    except SystemExit as exc:  # argparse ``--help``/``--version``
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _running_status(**overrides):
    data = {
        "running": True,
        "verified": True,
        "app": process.APP_MARKER,
        "protocol_version": process.PROTOCOL_VERSION,
        "pid": 4321,
        "state": "running",
    }
    data.update(overrides)
    return data


def _down_status(reason):
    return {
        "running": False,
        "verified": False,
        "app": None,
        "protocol_version": None,
        "pid": None,
        "state": None,
        "reason": reason,
    }


class WorkerCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-worker-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = str(self.tmp / "state")

    def _args(self, *rest: str) -> list[str]:
        return ["--data-dir", self.data_dir, *rest]


class HelpAndDispatchTest(WorkerCliTestCase):
    def test_worker_help_exits_zero(self) -> None:
        for argv in (
            ["worker", "--help"],
            ["worker", "start", "--help"],
            ["worker", "status", "--help"],
            ["worker", "stop", "--help"],
        ):
            with self.subTest(argv=argv):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf), self.assertRaises(
                    SystemExit
                ) as cm:
                    cli_main.build_parser().parse_args(argv)
                self.assertEqual(cm.exception.code, 0)
                self.assertIn("worker", buf.getvalue())

    def test_worker_without_subcommand_is_usage_error(self) -> None:
        code, _out, err = _run(["worker"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"]["category"], "usage")


class StartTest(WorkerCliTestCase):
    def test_start_reports_readiness_not_job_completion(self) -> None:
        with mock.patch.object(process, "start_worker", return_value=_running_status(started=True)) as m:
            code, out, _err = _run(self._args("worker", "start"))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(m.call_args.args, (self.data_dir,))
        worker = doc["worker"]
        self.assertTrue(worker["running"])
        self.assertTrue(worker["verified"])
        self.assertTrue(worker["started"])
        self.assertEqual(worker["readiness"], "ready")
        self.assertFalse(worker["job_completion"])
        self.assertFalse(worker["scientific_validation"])
        self.assertEqual(worker["pid"], 4321)

    def test_start_is_idempotent(self) -> None:
        first = _running_status(started=True)
        second = _running_status(started=False)
        with mock.patch.object(
            process, "start_worker", side_effect=[first, second]
        ) as m:
            code1, out1, _ = _run(self._args("worker", "start"))
            code2, out2, _ = _run(self._args("worker", "start"))
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertTrue(json.loads(out1)["worker"]["started"])
        self.assertFalse(json.loads(out2)["worker"]["started"])
        self.assertEqual(m.call_count, 2)

    def test_start_forwards_explicit_timeout_as_float(self) -> None:
        with mock.patch.object(
            process, "start_worker", return_value=_running_status(started=True)
        ) as m:
            code, _out, _err = _run(self._args("worker", "start", "--timeout", "2.5"))
        self.assertEqual(code, 0)
        self.assertEqual(m.call_args.kwargs, {"timeout": 2.5})

    def test_start_failure_is_typed_storage_and_curated(self) -> None:
        err = process.WorkerStartError(
            f"the worker did not become ready ({SECRET})", child_pid=111, returncode=3
        )
        with mock.patch.object(process, "start_worker", side_effect=err):
            code, out, errtext = _run(self._args("worker", "start"))
        self.assertEqual(code, 4)
        combined = out + errtext
        self.assertNotIn(SECRET, combined)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(json.loads(errtext)["error"]["category"], "storage")


class StatusTest(WorkerCliTestCase):
    def test_status_running(self) -> None:
        with mock.patch.object(
            process, "worker_status", return_value=_running_status()
        ) as m:
            code, out, _err = _run(self._args("worker", "status"))
        self.assertEqual(code, 0)
        self.assertEqual(m.call_args.args, (self.data_dir,))
        worker = json.loads(out)["worker"]
        self.assertTrue(worker["running"])
        self.assertTrue(worker["verified"])
        self.assertIsNone(worker["reason"])
        self.assertFalse(worker["scientific_validation"])

    def test_status_down_shows_explicit_reason(self) -> None:
        for reason in ("not_running", "foreign_endpoint", "unavailable"):
            with self.subTest(reason=reason):
                with mock.patch.object(
                    process, "worker_status", return_value=_down_status(reason)
                ):
                    code, out, _err = _run(self._args("worker", "status"))
                self.assertEqual(code, 0)
                worker = json.loads(out)["worker"]
                self.assertFalse(worker["running"])
                self.assertFalse(worker["verified"])
                self.assertEqual(worker["reason"], reason)

    def test_status_forwards_explicit_timeout(self) -> None:
        with mock.patch.object(
            process, "worker_status", return_value=_down_status("not_running")
        ) as m:
            _run(self._args("worker", "status", "--timeout", "0.5"))
        self.assertEqual(m.call_args.kwargs, {"timeout": 0.5})


class StopTest(WorkerCliTestCase):
    def _stop_result(self, **overrides):
        data = {
            "stopped": False,
            "unresolved": True,
            "was_running": True,
            "pid": 4321,
            "state": "running",
            "reason": "stop_unconfirmed",
        }
        data.update(overrides)
        return data

    def test_stop_confirmed_success(self) -> None:
        result = self._stop_result(
            stopped=True, unresolved=False, reason=None
        )
        with mock.patch.object(process, "stop_worker", return_value=result) as m:
            code, out, _err = _run(self._args("worker", "stop"))
        self.assertEqual(code, 0)
        self.assertEqual(m.call_args.args, (self.data_dir,))
        worker = json.loads(out)["worker"]
        self.assertTrue(worker["stopped"])
        self.assertFalse(worker["already_stopped"])
        self.assertTrue(worker["was_running"])
        self.assertEqual(worker["pid"], 4321)

    def test_stop_already_stopped_is_idempotent_success(self) -> None:
        result = self._stop_result(
            stopped=False,
            unresolved=True,
            was_running=False,
            pid=None,
            state=None,
            reason="not_running",
        )
        with mock.patch.object(process, "stop_worker", return_value=result):
            code, out, _err = _run(self._args("worker", "stop"))
        self.assertEqual(code, 0)
        worker = json.loads(out)["worker"]
        self.assertTrue(worker["stopped"])
        self.assertTrue(worker["already_stopped"])
        self.assertFalse(worker["was_running"])

    def test_stop_pending_is_nonzero_storage(self) -> None:
        result = self._stop_result(reason="stop_unconfirmed")
        with mock.patch.object(process, "stop_worker", return_value=result):
            code, out, errtext = _run(self._args("worker", "stop"))
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(errtext)["error"]["category"], "storage")
        self.assertNotIn("Traceback", errtext)

    def test_stop_unavailable_is_nonzero_storage(self) -> None:
        for reason in ("stop_unavailable", "unavailable"):
            with self.subTest(reason=reason):
                result = self._stop_result(reason=reason)
                with mock.patch.object(process, "stop_worker", return_value=result):
                    code, _out, errtext = _run(self._args("worker", "stop"))
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(errtext)["error"]["category"], "storage")

    def test_stop_foreign_is_nonzero_conflict(self) -> None:
        result = self._stop_result(reason="foreign_endpoint")
        with mock.patch.object(process, "stop_worker", return_value=result):
            code, _out, errtext = _run(self._args("worker", "stop"))
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(errtext)["error"]["category"], "conflict")

    def test_stop_pending_never_reports_success(self) -> None:
        # A worker that acknowledges but never confirms must not be "stopped".
        result = self._stop_result(reason="stop_unconfirmed")
        with mock.patch.object(process, "stop_worker", return_value=result):
            code, out, _err = _run(self._args("worker", "stop"))
        self.assertNotEqual(code, 0)
        self.assertNotIn("already_stopped", out)

    def test_stop_forwards_explicit_timeout(self) -> None:
        result = self._stop_result(stopped=True, unresolved=False, reason=None)
        with mock.patch.object(process, "stop_worker", return_value=result) as m:
            _run(self._args("worker", "stop", "--timeout", "3"))
        self.assertEqual(m.call_args.kwargs, {"timeout": 3.0})


class TimeoutValidationTest(WorkerCliTestCase):
    BAD = ("0", "-1", "nan", "inf", "Infinity", "1e400", "3601", "abc", "")

    def test_invalid_timeouts_are_validation(self) -> None:
        for bad in self.BAD:
            with self.subTest(timeout=bad):
                code, out, errtext = _run(
                    self._args("worker", "start", "--timeout", bad)
                )
                self.assertEqual(code, 2, bad)
                self.assertNotIn("Traceback", out + errtext)
                self.assertEqual(
                    json.loads(errtext)["error"]["category"], "validation"
                )

    def test_secret_like_timeout_is_never_echoed(self) -> None:
        for token in (SECRET, SECRET + "1", "t0ken_" + SECRET, "NaN:" + SECRET):
            with self.subTest(token=token):
                code, out, errtext = _run(
                    self._args("worker", "start", "--timeout", token)
                )
                self.assertEqual(code, 2)
                self.assertNotIn(token, out + errtext)
                self.assertEqual(
                    json.loads(errtext)["error"]["category"], "validation"
                )

    def test_valid_timeout_boundaries_accepted(self) -> None:
        for good in ("0.001", "3600", "1"):
            with self.subTest(timeout=good):
                with mock.patch.object(
                    process, "worker_status", return_value=_down_status("not_running")
                ) as m:
                    code, _out, _err = _run(
                        self._args("worker", "status", "--timeout", good)
                    )
                self.assertEqual(code, 0)
                self.assertEqual(m.call_args.kwargs, {"timeout": float(good)})


class FailureSecretNeverEchoedTest(WorkerCliTestCase):
    def test_process_failure_messages_are_not_echoed(self) -> None:
        # subcommand -> launcher function that raises a raw-message error.
        cases = {
            "status": ("worker_status", process.WorkerProcessError(f"raw {SECRET}")),
            "stop": ("stop_worker", process.WorkerProcessError(f"raw {SECRET}")),
        }
        for subcommand, (func, exc) in cases.items():
            with self.subTest(subcommand=subcommand):
                with mock.patch.object(process, func, side_effect=exc):
                    code, out, errtext = _run(self._args("worker", subcommand))
                self.assertEqual(code, 4)
                combined = out + errtext
                self.assertNotIn(SECRET, combined)
                self.assertNotIn("Traceback", combined)

    def test_unexpected_exception_is_internal_without_leak(self) -> None:
        with mock.patch.object(
            process, "worker_status", side_effect=RuntimeError(f"boom {SECRET}")
        ):
            code, out, errtext = _run(self._args("worker", "status"))
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, out + errtext)
        self.assertEqual(json.loads(errtext)["error"]["category"], "internal")


class NoExecutionFromOrdinaryCommandsTest(WorkerCliTestCase):
    def test_ordinary_commands_never_touch_the_worker(self) -> None:
        start = mock.MagicMock()
        status = mock.MagicMock()
        stop = mock.MagicMock()
        with mock.patch.object(process, "start_worker", start), mock.patch.object(
            process, "worker_status", status
        ), mock.patch.object(process, "stop_worker", stop):
            self.assertEqual(_run(self._args("create", str(self.tmp / "proj")))[0], 0)
            self.assertEqual(_run(self._args("list"))[0], 0)
            self.assertEqual(_run(self._args("status"))[0], 0)
        start.assert_not_called()
        status.assert_not_called()
        stop.assert_not_called()

    def test_global_status_keeps_running_false(self) -> None:
        _run(self._args("create", str(self.tmp / "proj")))
        code, out, _err = _run(self._args("status"))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertFalse(doc["running"])
        self.assertFalse(doc["scientific_validation"])
        self.assertNotIn("worker", doc)

    def test_list_output_unchanged(self) -> None:
        _run(self._args("create", str(self.tmp / "proj")))
        code, out, _err = _run(self._args("list"))
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertIn("projects", doc)
        self.assertIn("active_project_id", doc)
        self.assertNotIn("worker", doc)


class NoCredentialSettingsTest(WorkerCliTestCase):
    def test_unknown_worker_option_is_usage_without_echo(self) -> None:
        code, out, errtext = _run(
            self._args("worker", "start", "--api-key", SECRET)
        )
        self.assertEqual(code, 2)
        combined = out + errtext
        self.assertNotIn(SECRET, combined)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(json.loads(errtext)["error"]["category"], "usage")

    def test_config_credential_field_still_rejected_for_worker(self) -> None:
        cfg = self.tmp / "c.json"
        cfg.write_text(
            json.dumps({"schema_version": 1, "api_key": SECRET}), encoding="utf-8"
        )
        code, out, errtext = _run(["--config", str(cfg), "worker", "status"])
        self.assertEqual(code, 2)
        self.assertNotIn(SECRET, out + errtext)
        self.assertEqual(json.loads(errtext)["error"]["category"], "usage")


class ConfigDataDirForwardingTest(WorkerCliTestCase):
    def test_config_data_dir_is_forwarded_to_the_worker_api(self) -> None:
        cfg = self.tmp / "c.json"
        cfg.write_text(
            json.dumps({"schema_version": 1, "data_dir": self.data_dir}),
            encoding="utf-8",
        )
        with mock.patch.object(
            process, "worker_status", return_value=_down_status("not_running")
        ) as m:
            code, _out, _err = _run(["--config", str(cfg), "worker", "status"])
        self.assertEqual(code, 0)
        expected = os.path.realpath(self.data_dir)
        self.assertEqual(m.call_args.args, (expected,))

    def test_explicit_data_dir_overrides_config(self) -> None:
        other = str(self.tmp / "other")
        cfg = self.tmp / "c.json"
        cfg.write_text(
            json.dumps({"schema_version": 1, "data_dir": other}), encoding="utf-8"
        )
        with mock.patch.object(
            process, "worker_status", return_value=_down_status("not_running")
        ) as m:
            _run(
                [
                    "--data-dir",
                    self.data_dir,
                    "--config",
                    str(cfg),
                    "worker",
                    "status",
                ]
            )
        self.assertEqual(m.call_args.args, (os.path.realpath(self.data_dir),))


class IntegratedUdsServiceTest(WorkerCliTestCase):
    """Drive the *real* ``process`` API through the CLI over a real UDS socket.

    An owned, in-process fixture service (importing only
    ``review_article_agent.runtime.ipc``) answers the versioned control protocol.
    No engine entry module exists in this checkout and none is added.
    """

    def _start_fixture_service(self):
        stop_evt = threading.Event()
        server_holder: dict[str, ipc.WorkerIPCServer] = {}

        def handler(request, peer):
            op = request.get("op")
            if op == "status":
                return {
                    "app": process.APP_MARKER,
                    "protocol_version": process.PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "state": "running",
                }
            if op == "stop":
                stop_evt.set()
                # Remove our socket so the CLI's confirmation probe observes the
                # worker as confirmed gone (never a signal).
                server_holder["server"].close()
                return {
                    "app": process.APP_MARKER,
                    "protocol_version": process.PROTOCOL_VERSION,
                    "stopping": True,
                }
            raise ValueError("unknown operation")

        server = ipc.WorkerIPCServer(self.data_dir, handler, io_timeout=2.0)
        server_holder["server"] = server
        server.start()

        def serve():
            server.serve_forever(stop_evt, poll_timeout=0.05)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def cleanup():
            stop_evt.set()
            try:
                server.close()
            except Exception:  # pragma: no cover - defensive
                pass
            thread.join(5)

        self.addCleanup(cleanup)

    def test_status_and_stop_over_real_control_socket(self) -> None:
        self._start_fixture_service()

        code, out, _err = _run(self._args("worker", "status"))
        self.assertEqual(code, 0, out)
        worker = json.loads(out)["worker"]
        self.assertTrue(worker["running"])
        self.assertTrue(worker["verified"])
        self.assertEqual(worker["app"], process.APP_MARKER)
        self.assertEqual(worker["pid"], os.getpid())

        code, out, _err = _run(self._args("worker", "stop", "--timeout", "5"))
        self.assertEqual(code, 0, out)
        worker = json.loads(out)["worker"]
        self.assertTrue(worker["stopped"])
        self.assertFalse(worker["already_stopped"])
        self.assertTrue(worker["was_running"])

        # After a confirmed stop the worker is absent, and a second stop is an
        # idempotent success.
        code, out, _err = _run(self._args("worker", "stop", "--timeout", "5"))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["worker"]["already_stopped"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
