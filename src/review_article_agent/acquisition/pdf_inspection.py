"""Optional, bounded PDF *input-integrity* inspector (Step 06, S6-P1).

Authority: ``docs/implementation/06-acquisition.md`` acceptance criterion 2
(scan / wrong-paper / corruption / incomplete-text conditions must be explicit
review conditions). Step 07 keeps scientific parser selection, locators and
fidelity; nothing here chooses a scientific parser.

What it does: given the bytes of a candidate PDF it reports whether a ``pypdf``
child process could parse the container, whether it is encrypted, how many
pages there are, which pages yielded any text, a bounded private text preview,
and DOI/PMID/PMCID identifiers *only* when exactly one distinct value is found
in the metadata and first-page text.

What it does **not** claim: that the file is the intended paper, that the text
is scientifically correct, or that the whole paper was extracted. A result of
status ``inspected`` means only "container parsed and text observed". Callers
must still treat ``page_text_present`` containing ``False`` (scan / blank page),
status ``limited`` (a page/text cap or per-page extraction error), identifier
mismatches and every non-``inspected`` status as explicit review conditions.

Safety design:

* ``pypdf`` is an *optional* dependency (``pip install '.[pdf]'``). When it is
  not importable the inspector fails closed with status ``unavailable``.
* The PDF is parsed in an isolated child (``python -I``, no shell, empty
  environment, new session) with a wall-clock timeout, POSIX CPU / address-space
  / file-size / core limits, a 32 MiB source cap, page and text caps, and a
  bounded JSON reply. The parent never imports ``pypdf`` or touches PDF content.
* Exception messages from the parser are never propagated; failures map to the
  static ``failure_code`` values in :data:`FAILURE_CODES`.
* ``text_preview`` contains private source text; do not log or publish it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_CHARS = 8192
MAX_CHILD_OUTPUT_BYTES = 256 * 1024
MAX_TIMEOUT_SECONDS = 120
MAX_PAGES_CEILING = 10_000
MAX_TEXT_CHARS_CEILING = 20_000_000

STATUSES = ("inspected", "unavailable", "failed", "limited")
FAILURE_CODES = frozenset(
    {
        # failed
        "invalid_parameters",
        "invalid_input",
        "empty_input",
        "input_too_large",
        "not_pdf",
        "malformed_pdf",
        "encrypted",
        "no_pages",
        "timeout",
        "resource_limit",
        "child_crashed",
        "child_output_invalid",
        "internal_error",
        "spawn_failed",
        # unavailable
        "dependency_missing",
        "platform_unsupported",
        # limited
        "page_limit",
        "text_limit",
        "page_extract_error",
    }
)

_CHILD_SCRIPT = Path(__file__).with_name("_pdf_child.py")
_DOI_VALUE = re.compile(r"^10\.\d{4,9}/\S+$")
_PMID_VALUE = re.compile(r"^\d{1,9}$")
_PMCID_VALUE = re.compile(r"^PMC\d{4,9}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _pypdf_available() -> bool:
    """Seam: whether the optional parser can be imported (tests patch this)."""
    try:
        return importlib.util.find_spec("pypdf") is not None
    except (ImportError, ValueError):
        return False


def _empty_ids() -> dict[str, str | None]:
    return {"doi": None, "pmid": None, "pmcid": None}


def _result(
    status: str,
    failure_code: str | None,
    *,
    encrypted: bool | None = None,
    page_count: int | None = None,
    page_text_present: list[bool] | None = None,
    text_preview: str = "",
    identifiers: dict[str, str | None] | None = None,
    parser_version: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "encrypted": encrypted,
        "page_count": page_count,
        "page_text_present": list(page_text_present or []),
        "text_preview": text_preview,
        "identifiers": identifiers or _empty_ids(),
        "failure_code": failure_code,
        "parser_version": parser_version,
    }


def _fail(code: str) -> dict[str, object]:
    return _result("failed", code)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _run_child(
    data: bytes,
    max_pages: int,
    max_text_chars: int,
    timeout_seconds: float,
    *,
    script: Path | None = None,
    max_output_bytes: int = MAX_CHILD_OUTPUT_BYTES,
) -> tuple[str, bytes]:
    """Run the child; return ``(outcome, stdout)``.

    Outcome is one of ``ok``, ``timeout``, ``output_limit``, ``child_crashed``
    or ``spawn_failed``. Everything is bounded: wall time, stdout size and the
    child's own POSIX resource limits. The whole process group is killed on any
    abnormal end.

    ``script`` defaults to the Step 06 integrity child; the Step 07 full-text
    extractor passes its own self-contained child and a larger output bound.
    """
    script = _CHILD_SCRIPT if script is None else script
    cpu_seconds = int(timeout_seconds) + 2
    argv = [
        sys.executable,
        "-I",
        str(script),
        str(max_pages),
        str(max_text_chars),
        str(cpu_seconds),
    ]
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PYTHONIOENCODING": "utf-8"},
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return "spawn_failed", b""

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    def write_stdin() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                assert proc.stdin is not None
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    out = bytearray()
    overflow = threading.Event()

    def read_stdout() -> None:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            out.extend(chunk)
            if len(out) > max_output_bytes:
                overflow.set()
                kill()
                return

    writer = threading.Thread(target=write_stdin, daemon=True)
    reader = threading.Thread(target=read_stdout, daemon=True)
    writer.start()
    reader.start()
    reader.join(timeout_seconds)
    timed_out = reader.is_alive()
    if timed_out:
        kill()
        reader.join(5)
    try:
        returncode = proc.wait(timeout=5 if timed_out or overflow.is_set() else timeout_seconds)
    except subprocess.TimeoutExpired:
        kill()
        timed_out = True
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            returncode = -9
    writer.join(1)
    for stream in (proc.stdout, proc.stdin):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass
    if overflow.is_set():
        return "output_limit", b""
    if timed_out:
        return "timeout", b""
    if returncode != 0:
        return "child_crashed", b""
    return "ok", bytes(out)


_FIELDS = frozenset(
    {"status", "encrypted", "page_count", "page_text_present", "text_preview",
     "identifiers", "failure_code", "parser_version"}
)
_ID_KEYS = frozenset({"doi", "pmid", "pmcid"})
_LIMITED_CODES = frozenset({"page_limit", "text_limit", "page_extract_error"})
_UNAVAILABLE_CODES = frozenset({"dependency_missing", "platform_unsupported"})
_FAILED_CODES = FAILURE_CODES - _LIMITED_CODES - _UNAVAILABLE_CODES


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite number")


def _validate_child(raw: bytes) -> dict[str, object] | None:
    """Strictly validate the child's JSON; ``None`` means unusable.

    Every scalar is type-checked *before* any membership test or lookup, the
    key set must be exact, duplicate keys and NaN/Infinity are rejected, and
    status / failure_code / encrypted / page data must be mutually consistent.
    Never raises.
    """
    try:
        return _validate_child_unsafe(raw)
    except Exception:  # noqa: BLE001 - defence in depth; hostile input only
        return None


def _validate_child_unsafe(raw: bytes) -> dict[str, object] | None:
    obj = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_no_duplicates,
        parse_constant=_reject_constant,
    )
    if not isinstance(obj, dict) or set(obj) != _FIELDS:
        return None
    status = obj["status"]
    code = obj["failure_code"]
    if not isinstance(status, str) or status not in STATUSES:
        return None
    if code is not None and (not isinstance(code, str) or code not in FAILURE_CODES):
        return None
    encrypted = obj["encrypted"]
    if encrypted is not None and not isinstance(encrypted, bool):
        return None
    page_count = obj["page_count"]
    if page_count is not None and (not _is_int(page_count) or not 0 <= page_count <= 10_000_000):
        return None
    present = obj["page_text_present"]
    if not isinstance(present, list) or len(present) > MAX_PAGES_CEILING:
        return None
    if not all(isinstance(item, bool) for item in present):
        return None
    preview = obj["text_preview"]
    if not isinstance(preview, str):
        return None
    preview = _CONTROL_RE.sub(" ", preview.encode("utf-8", "replace").decode("utf-8"))[:MAX_PREVIEW_CHARS]
    ids = obj["identifiers"]
    if not isinstance(ids, dict) or set(ids) != _ID_KEYS:
        return None
    clean_ids = _empty_ids()
    for key, pattern in (("doi", _DOI_VALUE), ("pmid", _PMID_VALUE), ("pmcid", _PMCID_VALUE)):
        value = ids[key]
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 256 or not pattern.match(value):
            return None
        clean_ids[key] = value
    version = obj["parser_version"]
    if version is not None and (not isinstance(version, str) or len(version) > 64):
        return None

    # Cross-field consistency.
    if (code == "encrypted") != (encrypted is True):
        return None
    if status in ("inspected", "limited"):
        if encrypted is not False or page_count is None or page_count < 1:
            return None
        if not 1 <= len(present) <= page_count:
            return None
        if status == "inspected" and (code is not None or len(present) != page_count):
            return None
        if status == "limited" and code not in _LIMITED_CODES:
            return None
    else:
        if status == "unavailable" and (
            code not in _UNAVAILABLE_CODES or encrypted is not None or page_count is not None
        ):
            return None
        if status == "failed" and code not in _FAILED_CODES:
            return None
        if present or preview or any(v is not None for v in clean_ids.values()):
            return None
    return _result(
        status,
        code,
        encrypted=encrypted,
        page_count=page_count,
        page_text_present=present,
        text_preview=preview,
        identifiers=clean_ids,
        parser_version=version,
    )


def inspect_pdf(
    data: bytes,
    *,
    timeout_seconds: float = 10,
    max_pages: int = 500,
    max_text_chars: int = 2_000_000,
) -> dict[str, object]:
    """Inspect PDF bytes for container integrity; never raises on bad input.

    Returns a dict with ``status`` (``inspected``/``unavailable``/``failed``/
    ``limited``), ``encrypted`` (bool or None), ``page_count`` (int or None),
    ``page_text_present`` (one bool per *examined* page), ``text_preview``
    (<= 8192 chars of private source text), ``identifiers`` (``doi``/``pmid``/
    ``pmcid``, each a string only if unambiguous else None), ``failure_code``
    (static string or None) and ``parser_version``.

    ``inspected`` means the container parsed and text was observed on the
    examined pages; it is not a correctness or completeness claim. A page with
    ``False`` in ``page_text_present`` (blank/scanned) and every ``limited`` or
    ``failed`` result are explicit conditions the caller must surface.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        or not _is_int(max_pages)
        or not 1 <= max_pages <= MAX_PAGES_CEILING
        or not _is_int(max_text_chars)
        or not 1 <= max_text_chars <= MAX_TEXT_CHARS_CEILING
    ):
        return _fail("invalid_parameters")
    if not isinstance(data, bytes):
        return _fail("invalid_input")
    if not data:
        return _fail("empty_input")
    if len(data) > MAX_SOURCE_BYTES:
        return _fail("input_too_large")
    if b"%PDF-" not in data[:1024]:
        return _fail("not_pdf")
    if not _pypdf_available():
        return _result("unavailable", "dependency_missing")

    outcome, raw = _run_child(data, max_pages, max_text_chars, float(timeout_seconds))
    if outcome == "timeout":
        return _fail("timeout")
    if outcome == "output_limit":
        return _fail("child_output_invalid")
    if outcome == "spawn_failed":
        return _fail("spawn_failed")
    if outcome != "ok":
        return _fail("child_crashed")
    validated = _validate_child(raw)
    if validated is None:
        return _fail("child_output_invalid")
    return validated
