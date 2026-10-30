"""Isolated child half of the PDF full-text extractor (Step 07, S17-P7).

This file is executed as ``python -I _pdf_text_child.py MAX_PAGES MAX_TEXT_CHARS
CPU_SECONDS`` by :mod:`review_article_agent.parsing.pdf_adapter`. It is
deliberately self-contained (standard library plus a lazily imported ``pypdf``)
so the isolated interpreter does not need the project on ``sys.path``.

Protocol: the raw PDF bytes arrive on stdin (the parent already enforced the
source cap); exactly one JSON object is written to stdout; stderr is discarded
by the parent. PDF content is untrusted data: nothing here follows instructions
found in a document, opens a network connection, or runs a tool.

What it does: returns the extracted text of each examined page, bounded by a
page count and a total character cap, with per-page extraction failures mapped
to a static code. It makes **no** claim about column order, scientific
correctness, completeness, or that this is the intended paper -- the parent
adapter owns quality states and always keeps a human action.
"""

from __future__ import annotations

import io
import json
import re
import sys

#: A single page's text is truncated to this many characters before the total
#: cap is applied, so one pathological page cannot exhaust memory.
MAX_PAGE_TEXT_CHARS = 200_000

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str) -> str:
    """Drop lone surrogates and replace control characters with spaces."""
    text = text.encode("utf-8", "replace").decode("utf-8")
    return _CONTROL_RE.sub(" ", text)


def _result(
    status: str,
    failure_code: str | None = None,
    *,
    page_count: int | None = None,
    pages: list[dict[str, object]] | None = None,
    parser_version: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "failure_code": failure_code,
        "page_count": page_count,
        "pages": pages or [],
        "parser_version": parser_version,
    }


def extract_pages(
    data: bytes,
    max_pages: int,
    max_text_chars: int,
    pypdf: object | None = None,
) -> dict[str, object]:
    """Extract per-page text from ``data``; every failure maps to a static code.

    ``pages`` is one ``{"page": <1-based>, "text": <str>}`` entry per *examined*
    page (blank pages carry ``""``). ``status`` is ``ok`` (all pages under the
    caps), ``limited`` (a page/text cap or a per-page extraction error), or a
    terminal ``failed``/``unavailable``.
    """
    if pypdf is None:
        try:
            import pypdf as _pypdf  # type: ignore[import-not-found]
        except ImportError:
            return _result("unavailable", "dependency_missing")
        pypdf = _pypdf
    version = getattr(pypdf, "__version__", None)
    version = version if isinstance(version, str) else None

    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)  # type: ignore[attr-defined]
        encrypted = bool(reader.is_encrypted)
    except MemoryError:
        return _result("failed", "resource_limit", parser_version=version)
    except Exception:  # noqa: BLE001 - messages from hostile input never leave
        return _result("failed", "malformed_pdf", parser_version=version)
    if encrypted:
        # No decryption is attempted: no password is known and none is guessed.
        return _result("failed", "encrypted", parser_version=version)

    try:
        page_objects = reader.pages
        page_count = len(page_objects)
    except MemoryError:
        return _result("failed", "resource_limit", parser_version=version)
    except Exception:  # noqa: BLE001
        return _result("failed", "malformed_pdf", parser_version=version)
    if page_count == 0:
        return _result("failed", "no_pages", page_count=0, parser_version=version)

    out: list[dict[str, object]] = []
    total = 0
    limit_code: str | None = None
    examined = min(page_count, max_pages)
    if page_count > max_pages:
        limit_code = "page_limit"
    try:
        for index in range(examined):
            try:
                text = _clean(page_objects[index].extract_text() or "")
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001
                text = ""
                limit_code = limit_code or "page_extract_error"
            if len(text) > MAX_PAGE_TEXT_CHARS:
                text = text[:MAX_PAGE_TEXT_CHARS]
                limit_code = limit_code or "text_limit"
            if total + len(text) > max_text_chars:
                text = text[: max(0, max_text_chars - total)]
                limit_code = "text_limit"
                total = max_text_chars
                stop = True
            else:
                total += len(text)
                stop = False
            out.append({"page": index + 1, "text": text})
            if stop:
                break
    except MemoryError:
        return _result(
            "failed", "resource_limit", page_count=page_count, parser_version=version
        )

    return _result(
        "limited" if limit_code else "ok",
        limit_code,
        page_count=page_count,
        pages=out,
        parser_version=version,
    )


def apply_limits(cpu_seconds: int) -> bool:
    """Apply POSIX CPU/address-space/file/core limits; False when unsupported."""
    try:
        import resource
    except ImportError:
        return False
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (ValueError, OSError):
        return False
    return True


def main(argv: list[str]) -> int:
    try:
        max_pages, max_text_chars, cpu_seconds = (int(item) for item in argv[1:4])
    except (ValueError, IndexError):
        out = _result("failed", "invalid_parameters")
    else:
        if not apply_limits(cpu_seconds):
            out = _result("unavailable", "platform_unsupported")
        else:
            try:
                data = sys.stdin.buffer.read()
                out = extract_pages(data, max_pages, max_text_chars)
            except MemoryError:
                out = _result("failed", "resource_limit")
            except BaseException:  # noqa: BLE001
                out = _result("failed", "internal_error")
    sys.stdout.write(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
