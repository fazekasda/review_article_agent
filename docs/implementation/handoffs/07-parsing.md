# Handoff — step 07: parsing, evidence assets, and reference structure

**Status: partial parsing MVP; step-07 row NOT ticked.** Integrated on this branch: S7-CORE `c235075` + repair `314589a`, S7-STORE `ace293b` + storage-atomicity/fail-closed-unknown-parser fix `ba760a1`, S7-CLI `ae18e9b`. Stdlib XML-first structural core + durable report repository + personal `parsing` CLI. No overall completion or scientific claim. Sources: `docs/implementation/07-parsing.md` (deliverables 9-12, AC 16-22), `docs/specification.md`, `handoffs/06-acquisition.md`.

## Modules, features, API
- `parsing/core.py`: `parse_document(data)` → immutable report payload; JATS/TEI structural passages/references/assets, citation-marker links, explicit unresolved/malformed state; XML structural text supported.
- `parsing/repository.py`: `ParsingRepository` (`parse_stored_document`, `read_report`, `make_passage_locator`, `resolve_passage`); immutable raw + separate normalized/derived revisions, atomic rights-bound writes, historical locators + current parser/freshness check (`parser_version_unverified` on unknown parser).
- `parsing/locator.py`, `parsing/pdf_adapter.py` (diagnostic only).
- CLI `parsing` group (`cli/parsing.py`): `parse`/`show`/`locator`/`resolve`; safe summaries by default, private content only with `--content`.

## Tests (new, focused)
- `tests/test_parsing_core.py` + `tests/test_parsing_repository.py` = 36; `tests/test_cli_parsing.py` = 5 → **41 passed** (combined coordinator run). Full baseline **3079 passed**; final full combined run in progress — result in the PR.

## Partial MVP / not ticked
- PDF extraction/OCR/visual/table-cell interpretation **unsupported → human review**; synthetic self-authored XML/TEI fixtures only, no representative human-adjudicated benchmark. XML structural text is supported; PDF full-text extraction is unsupported.
- Stdlib Python (PSF); optional `pypdf` (BSD-3) PDF diagnostic, no weights; ≤32 MiB local input; no live trials/network/model/approval; no migration, no new deps, no hosted calls.

## Copyable usage
```bash
export PYTHONPATH=src; P="python3 -m review_article_agent.cli --data-dir ./state parsing"
$P parse --project "$PID" --source-artifact-id art-src --source-revision-id rev-src --report-artifact-id art-rep --report-revision-id rev-rep --rights-file report-rights.json
$P show --report-artifact-id art-rep --report-revision-id rev-rep [--content]
$P locator --report-artifact-id art-rep --report-revision-id rev-rep --passage-id p0002 [--content]
$P resolve --locator-file locator.json [--content]
```
