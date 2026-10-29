"""Isolated child half of the PDF input-integrity inspector (Step 06, S6-P1).

This file is executed as ``python -I _pdf_child.py MAX_PAGES MAX_TEXT_CHARS
CPU_SECONDS`` by :mod:`review_article_agent.acquisition.pdf_inspection`. It is
deliberately self-contained (standard library plus a lazily imported ``pypdf``)
so the isolated interpreter does not need the project on ``sys.path``.

Protocol: the raw PDF bytes arrive on stdin (the parent already enforced the
source cap); exactly one JSON object is written to stdout; stderr is discarded
by the parent. PDF content is untrusted data: nothing here follows
instructions found in a document, opens a network connection, or runs a tool.

Honest limits: a successful result means the container parsed and some page
text was observed. It says nothing about scientific correctness, that this is
the intended paper, or that the whole paper text was extracted (Step 07 owns
scientific parsing, locators and fidelity).
"""

from __future__ import annotations

import io
import json
import re
import sys

PREVIEW_MAX_CHARS = 8192

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>{}|\\^`]+")
_PMCID_RE = re.compile(r"\bPMC(\d{4,9})\b", re.IGNORECASE)
_PMID_RE = re.compile(r"\bPMID\s*[:#]?\s*(\d{1,9})\b", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_TRAILING_PUNCT = ".,;:)]}'\""


def _clean(text: str) -> str:
    """Drop lone surrogates and collapse control characters/whitespace."""
    text = text.encode("utf-8", "replace").decode("utf-8")
    return _CONTROL_RE.sub(" ", text)


def extract_identifiers(sources: list[str]) -> dict[str, str | None]:
    """Return doi/pmid/pmcid, each only if exactly one distinct value was seen.

    Two different values for the same kind (for example a cited reference's
    DOI next to the paper's own) are ambiguous and therefore ``None``.
    """
    dois: set[str] = set()
    pmids: set[str] = set()
    pmcids: set[str] = set()
    for source in sources:
        for match in _DOI_RE.finditer(source):
            value = match.group(0).rstrip(_TRAILING_PUNCT).lower()
            if "/" in value and len(value) > 8:
                dois.add(value)
        for match in _PMCID_RE.finditer(source):
            pmcids.add("PMC" + match.group(1))
        for match in _PMID_RE.finditer(source):
            pmids.add(match.group(1))

    def one(values: set[str]) -> str | None:
        return next(iter(values)) if len(values) == 1 else None

    return {"doi": one(dois), "pmid": one(pmids), "pmcid": one(pmcids)}


def _result(
    status: str,
    failure_code: str | None = None,
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
        "page_text_present": page_text_present or [],
        "text_preview": text_preview,
        "identifiers": identifiers or {"doi": None, "pmid": None, "pmcid": None},
        "failure_code": failure_code,
        "parser_version": parser_version,
    }


def inspect_bytes(
    data: bytes,
    max_pages: int,
    max_text_chars: int,
    pypdf: object | None = None,
) -> dict[str, object]:
    """Inspect ``data`` with pypdf; every failure maps to a static code."""
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
        return _result("failed", "encrypted", encrypted=True, parser_version=version)

    try:
        pages = reader.pages
        page_count = len(pages)
    except MemoryError:
        return _result("failed", "resource_limit", encrypted=False, parser_version=version)
    except Exception:  # noqa: BLE001
        return _result("failed", "malformed_pdf", encrypted=False, parser_version=version)
    if page_count == 0:
        return _result("failed", "no_pages", encrypted=False, page_count=0, parser_version=version)

    present: list[bool] = []
    preview_parts: list[str] = []
    preview_len = 0
    first_page_text = ""
    total = 0
    limit_code: str | None = None
    examined = min(page_count, max_pages)
    if page_count > max_pages:
        limit_code = "page_limit"
    try:
        for index in range(examined):
            try:
                text = _clean(pages[index].extract_text() or "")
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001
                text = ""
                limit_code = limit_code or "page_extract_error"
            if total + len(text) > max_text_chars:
                text = text[: max(0, max_text_chars - total)]
                limit_code = "text_limit"
                total = max_text_chars
                stop = True
            else:
                total += len(text)
                stop = False
            present.append(bool(text.strip()))
            if index == 0:
                first_page_text = text
            if preview_len < PREVIEW_MAX_CHARS and text.strip():
                snippet = " ".join(text.split())[: PREVIEW_MAX_CHARS - preview_len]
                preview_parts.append(snippet)
                preview_len += len(snippet) + 1
            if stop:
                break
    except MemoryError:
        return _result("failed", "resource_limit", encrypted=False, page_count=page_count, parser_version=version)

    sources: list[str] = [first_page_text]
    try:
        metadata = reader.metadata
        if metadata:
            for value in list(metadata.values())[:64]:
                if isinstance(value, str):
                    sources.append(_clean(value)[:4096])
    except Exception:  # noqa: BLE001
        pass

    preview = " ".join(preview_parts)[:PREVIEW_MAX_CHARS]
    return _result(
        "limited" if limit_code else "inspected",
        limit_code,
        encrypted=False,
        page_count=page_count,
        page_text_present=present,
        text_preview=preview,
        identifiers=extract_identifiers(sources),
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
                out = inspect_bytes(data, max_pages, max_text_chars)
            except MemoryError:
                out = _result("failed", "resource_limit")
            except BaseException:  # noqa: BLE001
                out = _result("failed", "internal_error")
    sys.stdout.write(json.dumps(out, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
