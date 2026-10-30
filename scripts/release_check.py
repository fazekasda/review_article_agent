#!/usr/bin/env python3
"""Clean-environment package install, documented-recovery, and secret-leak check.

Step 15 acceptance criterion: *"Package installation and documented recovery work
in a clean test environment; secrets are excluded from logs, fixtures and
exports."* This script is the executable evidence for that criterion. It is
standard-library only, makes **no** model call, and never prints a value from the
ambient environment -- the only credential-shaped strings it handles are the
explicit, synthetic *canaries* below.

What it does
------------

1. **Clean install.** Create a fresh virtual environment in a temporary
   directory (`python -m venv`), then install this repository with
   ``pip install --no-deps --no-build-isolation``. If that fails because the
   build backend (setuptools) is not present in the fresh venv, fall back to a
   normal ``pip install .`` (build isolation), which may need the network only
   to fetch setuptools. The path actually used is reported.
2. **Documented recovery via the installed console script.** From a directory
   *outside* the repository, using a temporary ``--data-dir``, run the installed
   ``review-article-agent``: ``--help``, ``create`` a project, ``list``, then the
   documented private-snapshot recovery path ``backup create`` -> ``backup
   verify`` -> ``backup restore`` into a *fresh* data directory, then ``list`` and
   ``status`` confirm the project. The ``export`` alias is exercised too.
3. **Secret scan.** Synthetic canaries are exported into the CLI subprocess
   environment; every captured stdout/stderr, the data directories, the backup
   and export directories are then searched for those canary values. Tracked
   files under ``tests/`` and ``docs/`` are scanned for high-confidence secret
   patterns (allowing obvious ``.env.example`` placeholders). Findings are
   redacted so a real, accidentally committed secret is never echoed to stdout.

It prints one JSON summary to stdout and exits non-zero on any failure.

Run::

    python3 scripts/release_check.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Synthetic canaries. These are ours; they are the *only* credential-shaped
#: values this script is ever allowed to print. A real environment secret is
#: never read and never emitted.
CANARIES = {
    "OPENROUTER_API_KEY": "sk-canary-RAA0123456789abcdefghijklmnopqrst",
    "OPENCODE_SERVER_PASSWORD": "canary-RAA-password-0123456789abcdef",
}

#: High-confidence credential/private-key patterns for tracked files.
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    ("openai_style_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)

#: Markers that identify an obvious documentation placeholder rather than a real
#: credential. Matches containing any marker are ignored.
PLACEHOLDER_MARKERS = (
    "your",
    "example",
    "placeholder",
    "redacted",
    "xxxx",
    "sample",
    "...",
    "<",
    ">",
)


# --------------------------------------------------------------------------- #
# secret scanning (pure, unit-testable)
# --------------------------------------------------------------------------- #
def find_canaries(text: str, canaries) -> list[str]:
    """Return the canary values present in ``text`` (empty when clean)."""
    return [value for value in canaries if value and value in text]


def is_placeholder_match(matched: str) -> bool:
    """True when a pattern match is an obvious documentation placeholder."""
    low = matched.lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def _redact(matched: str) -> str:
    """Never echo a candidate secret; keep only a short, non-reversible hint."""
    head = matched[:3]
    return f"{head}<redacted:{len(matched)}>"


def find_secret_patterns(text: str, path: str = "") -> list[dict]:
    """High-confidence secret findings in ``text``; ``.env.example`` is exempt."""
    name = Path(path).name
    if name == ".env.example" or name.endswith(".env.example"):
        return []
    findings: list[dict] = []
    for pattern_name, rx in HIGH_CONFIDENCE_SECRET_PATTERNS:
        for match in rx.finditer(text):
            matched = match.group(0)
            if is_placeholder_match(matched):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                {
                    "kind": pattern_name,
                    "path": path,
                    "line": line,
                    "match": _redact(matched),
                }
            )
    return findings


def scan_text(text: str, *, canaries=(), patterns: bool = False, path: str = "") -> list[dict]:
    """Scan one text blob for canaries and (optionally) secret patterns."""
    findings: list[dict] = []
    for value in find_canaries(text, canaries):
        findings.append({"kind": "canary", "path": path, "canary": value})
    if patterns:
        findings.extend(find_secret_patterns(text, path=path))
    return findings


def scan_directory(root: Path, *, canaries=(), patterns: bool = False) -> tuple[list[dict], int]:
    """Walk ``root`` and scan every regular file. Returns (findings, files)."""
    findings: list[dict] = []
    files = 0
    if not root.exists():
        return findings, files
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            file_path = Path(dirpath) / filename
            if not file_path.is_file():
                continue
            files += 1
            try:
                data = file_path.read_bytes()
            except OSError:
                continue
            # latin-1 maps every byte 1:1 so a binary store can be searched too.
            text = data.decode("latin-1", errors="replace")
            findings.extend(scan_text(text, canaries=canaries, patterns=patterns, path=str(file_path)))
    return findings, files


def tracked_files(paths=("tests", "docs"), repo: Path = REPO) -> list[str]:
    """Repository-relative tracked paths under ``paths`` (via ``git ls-files``)."""
    proc = subprocess.run(
        ["git", "ls-files", "--", *paths],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def scan_tracked_files(paths=("tests", "docs"), repo: Path = REPO) -> tuple[list[dict], int]:
    """Scan tracked files under ``paths`` for high-confidence secret patterns."""
    findings: list[dict] = []
    files = 0
    for rel in tracked_files(paths, repo=repo):
        file_path = repo / rel
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files += 1
        findings.extend(find_secret_patterns(text, path=rel))
    return findings, files


# --------------------------------------------------------------------------- #
# clean-environment install + console-script run
# --------------------------------------------------------------------------- #
def _run(cmd, *, cwd, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)


#: Official get-pip bootstrap, used only when the interpreter's ``ensurepip`` is
#: unavailable (e.g. a Debian/Ubuntu Python without ``python3.x-venv``).
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def make_venv(base: Path) -> tuple[Path, dict]:
    """Create a fresh venv; fall back to ``--without-pip`` if ensurepip is absent."""
    venv_dir = base / "venv"
    proc = _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=base)
    if proc.returncode == 0:
        return venv_dir, {"method": "ensurepip", "ok": True}
    # ``ensurepip`` may be missing; still create the environment without pip.
    proc2 = _run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], cwd=base)
    if proc2.returncode != 0:
        raise RuntimeError(
            "python -m venv failed (with and without --without-pip): "
            + ((proc2.stderr or proc.stderr).strip() or "no detail")
        )
    return venv_dir, {
        "method": "without_pip",
        "ok": True,
        "detail": "`python -m venv` could not bootstrap pip (ensurepip unavailable); "
                  "created the venv with --without-pip and will bootstrap pip next.",
    }


def ensure_pip(venv_dir: Path) -> dict:
    """Ensure the venv has pip, bootstrapping it if the interpreter lacks ensurepip."""
    python = venv_dir / "bin" / "python"
    if _run([str(python), "-m", "pip", "--version"], cwd=venv_dir).returncode == 0:
        return {"ok": True, "method": "present", "network_required": False}
    if _run([str(python), "-m", "ensurepip", "--upgrade"], cwd=venv_dir).returncode == 0:
        return {"ok": True, "method": "ensurepip", "network_required": False}
    # Last resort: fetch the official get-pip bootstrap (network required).
    try:
        import urllib.request

        get_pip = venv_dir / "get-pip.py"
        with urllib.request.urlopen(GET_PIP_URL, timeout=60) as response:  # noqa: S310
            get_pip.write_bytes(response.read())
    except Exception as exc:  # noqa: BLE001 - report, never traceback
        return {"ok": False, "method": "get_pip", "network_required": True,
                "detail": f"pip unavailable and get-pip download failed: {type(exc).__name__}"}
    proc = _run([str(python), str(get_pip)], cwd=venv_dir)
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        return {"ok": False, "method": "get_pip", "network_required": True,
                "detail": "get-pip bootstrap failed", "failure": lines[-1] if lines else ""}
    return {
        "ok": True,
        "method": "get_pip",
        "network_required": True,
        "detail": "ensurepip was unavailable; bootstrapped pip via the official "
                  "get-pip script (network used beyond setuptools).",
    }


def pip_install(venv_dir: Path) -> dict:
    """Install the repo; prefer --no-build-isolation, fall back to normal."""
    python = venv_dir / "bin" / "python"
    primary = [
        str(python), "-m", "pip", "install",
        "--no-deps", "--no-build-isolation", str(REPO),
    ]
    proc = _run(primary, cwd=venv_dir)
    if proc.returncode == 0:
        return {
            "path_used": "no_build_isolation",
            "ok": True,
            "network_required": False,
            "detail": "pip install --no-deps --no-build-isolation succeeded; "
                      "the venv already provided the build backend (setuptools).",
        }
    primary_detail = (proc.stderr or proc.stdout).strip().splitlines()
    primary_detail = primary_detail[-1] if primary_detail else "unknown error"

    fallback = [str(python), "-m", "pip", "install", str(REPO)]
    proc2 = _run(fallback, cwd=venv_dir)
    if proc2.returncode == 0:
        return {
            "path_used": "build_isolation_fallback",
            "ok": True,
            "network_required": True,
            "detail": "no-build-isolation failed (setuptools missing in the fresh "
                      "venv); fell back to `pip install .` with build isolation, "
                      "which may fetch setuptools from the network.",
            "primary_failure": primary_detail,
        }
    fb_detail = (proc2.stderr or proc2.stdout).strip().splitlines()
    return {
        "path_used": "failed",
        "ok": False,
        "network_required": True,
        "detail": "both install paths failed.",
        "primary_failure": primary_detail,
        "fallback_failure": fb_detail[-1] if fb_detail else "unknown error",
    }


def cli_env() -> dict:
    """A subprocess environment carrying only the synthetic canaries.

    ``PYTHONPATH`` is removed so the console script must resolve the *installed*
    package, not the source tree; ``XDG_DATA_HOME`` is removed for determinism.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("XDG_DATA_HOME", None)
    env.update(CANARIES)
    return env


def main() -> int:
    checks: list[dict] = []
    captured: list[str] = []
    scan_roots: list[Path] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    install_report: dict = {"path_used": "not_run", "ok": False}
    recovery: dict = {}
    secret_scan: dict = {}
    exit_code = 0

    with tempfile.TemporaryDirectory(prefix="raa-release-check-") as tmp_name:
        tmp = Path(tmp_name)
        workdir = tmp / "cwd"           # outside the repo
        workdir.mkdir()
        data = tmp / "data"
        data2 = tmp / "data2"           # fresh registry for restore
        snap = tmp / "snap1"            # backup destination
        exp = tmp / "snap2"             # export-alias destination
        restored_root = tmp / "restored"

        try:
            venv_dir, venv_report = make_venv(tmp)
            record("venv_create", venv_report["ok"], venv_report.get("detail", venv_report["method"]))
            pip_report = ensure_pip(venv_dir)
            record("pip_available", pip_report["ok"], pip_report.get("detail", pip_report["method"]))
            install_report = pip_install(venv_dir) if pip_report["ok"] else {
                "path_used": "no_pip", "ok": False,
                "detail": "pip is unavailable in the fresh venv",
            }
            install_report["venv"] = venv_report
            install_report["pip"] = pip_report
            record("install", install_report["ok"], install_report["detail"])

            console = venv_dir / "bin" / "review-article-agent"
            env = cli_env()

            def cli(args, expect=0):
                proc = _run([str(console), *args], cwd=workdir, env=env)
                captured.append(proc.stdout or "")
                captured.append(proc.stderr or "")
                return proc, proc.returncode == expect

            if not install_report["ok"]:
                record("console_script_present", console.exists(), str(console))
            else:
                record("console_script_present", console.exists())

                # (1) --help
                proc, ok = cli(["--help"])
                record("cli_help", ok, f"exit={proc.returncode}")

                # (2) create + list
                proc, ok = cli(["--data-dir", str(data), "create", str(tmp / "proj"), "--name", "ReleaseCheck"])
                project_id = ""
                if ok:
                    try:
                        project_id = json.loads(proc.stdout)["project"]["project_id"]
                    except (ValueError, KeyError):
                        ok = False
                record("cli_create", ok and bool(project_id), f"project_id={'set' if project_id else 'missing'}")

                proc, ok = cli(["--data-dir", str(data), "list"])
                listed_members = []
                if ok:
                    try:
                        listed_members = [p.get("project_id") for p in json.loads(proc.stdout).get("projects", [])]
                    except ValueError:
                        ok = False
                record("cli_list", ok and project_id in listed_members, "list shows the created project")

                # Select is part of the documented sequence; it is not execution.
                proc, ok = cli(["--data-dir", str(data), "select", project_id])
                selected = ""
                if ok:
                    try:
                        selected = json.loads(proc.stdout)["selected"]["project_id"]
                    except (ValueError, KeyError):
                        ok = False
                record("cli_select", ok and selected == project_id, "selected project id")

                proc, ok = cli(["--data-dir", str(data), "list"])
                listed_active = ""
                if ok:
                    try:
                        listed_active = json.loads(proc.stdout).get("active_project_id", "")
                    except ValueError:
                        ok = False
                record("cli_list_after_select", ok and listed_active == project_id,
                       "list lists the created project as active")

                # (3) documented recovery: backup -> verify -> restore -> list/status
                proc, ok = cli(["--data-dir", str(data), "backup", "create", str(snap), "--project", project_id])
                record("recovery_backup_create", ok, f"exit={proc.returncode}")

                proc, ok = cli(["--data-dir", str(data), "backup", "verify", str(snap)])
                verified = False
                if ok:
                    try:
                        verified = bool(json.loads(proc.stdout)["backup"]["ok"])
                    except (ValueError, KeyError):
                        pass
                record("recovery_backup_verify", ok and verified, "backup.ok == true")

                proc, ok = cli(["--data-dir", str(data2), "backup", "restore", str(snap), str(restored_root)])
                restored_pid = ""
                if ok:
                    try:
                        restored_pid = json.loads(proc.stdout)["project"]["project_id"]
                    except (ValueError, KeyError):
                        ok = False
                record("recovery_backup_restore", ok and restored_pid == project_id,
                       "restored project_id preserved")

                proc, ok = cli(["--data-dir", str(data2), "list"])
                restored_members = []
                if ok:
                    try:
                        restored_members = [p.get("project_id") for p in json.loads(proc.stdout).get("projects", [])]
                    except ValueError:
                        ok = False
                record("recovery_restored_list", ok and project_id in restored_members,
                       "restored registry lists the project")

                proc, ok = cli(["--data-dir", str(data2), "status", "--project", project_id])
                status_pid = ""
                if ok:
                    try:
                        status_pid = json.loads(proc.stdout).get("project_id", "")
                    except ValueError:
                        ok = False
                record("recovery_restored_status", ok and status_pid == project_id,
                       "restored status shows the project")

                # (4) export alias (also gives us an export directory to scan)
                proc, ok = cli(["--data-dir", str(data), "export", str(exp), "--project", project_id])
                record("export_alias", ok, f"exit={proc.returncode}")

                recovery = {
                    "project_id_present": bool(project_id),
                    "backup_dir": str(snap),
                    "export_dir": str(exp),
                    "restored_data_dir": str(data2),
                }

            # ---- secret scan ----
            canary_values = list(CANARIES.values())
            scan_roots = [data, data2, snap, exp, restored_root]

            captured_findings = scan_text("\n".join(captured), canaries=canary_values, path="<captured stdout/stderr>")
            file_findings: list[dict] = []
            files_scanned = 0
            for root in scan_roots:
                found, count = scan_directory(root, canaries=canary_values)
                file_findings.extend(found)
                files_scanned += count

            tracked_findings, tracked_scanned = scan_tracked_files()
            canary_findings = captured_findings + [f for f in file_findings if f["kind"] == "canary"]

            record("secret_scan_canary", not canary_findings,
                   "no synthetic canary in captured output or temp stores")
            record("secret_scan_tracked_files", not tracked_findings,
                   "no high-confidence secret pattern in tracked tests/ and docs/")

            secret_scan = {
                "canaries_used": sorted(CANARIES.keys()),
                "captured_streams": len(captured),
                "temp_files_scanned": files_scanned,
                "tracked_files_scanned": tracked_scanned,
                "canary_findings": canary_findings,
                "tracked_pattern_findings": tracked_findings,
                "ok": not canary_findings and not tracked_findings,
            }
        except Exception as exc:  # noqa: BLE001 - report, never traceback
            record("release_check", False, f"{type(exc).__name__}: {exc}")
            if not secret_scan:
                secret_scan = {"ok": False, "error": f"{type(exc).__name__}"}

    ok = all(check["ok"] for check in checks)
    summary = {
        "ok": ok,
        "python": sys.version.split()[0],
        "repo": str(REPO),
        "install": install_report,
        "recovery": recovery,
        "secret_scan": secret_scan,
        "checks": checks,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
