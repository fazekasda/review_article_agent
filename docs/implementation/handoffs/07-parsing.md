# Handoff — step 07: parsing, evidence assets, and reference structure

**Status: partial parsing MVP; step-07 row NOT ticked.** Integrated on this branch: S7-CORE `c235075` + repair `314589a`, S7-STORE `ace293b` + storage-atomicity/fail-closed-unknown-parser fix `ba760a1`, S7-CLI `ae18e9b` + summary-privacy fixes `7712b7d`/`35c4f04`. Stdlib XML-first structural core + durable report repository + personal `parsing` CLI. No overall completion or scientific claim. Sources: `docs/implementation/07-parsing.md` (deliverables 9-12, AC 16-22), `docs/specification.md`, `handoffs/06-acquisition.md`.

## Modules, features, API
- `parsing/core.py`: `parse_document(data)` → immutable report payload; JATS/TEI structural passages/references/assets, citation-marker links, explicit unresolved/malformed state; XML structural text supported.
- `parsing/repository.py`: `ParsingRepository` (`parse_stored_document`, `read_report`, `make_passage_locator`, `resolve_passage`); immutable raw + separate normalized/derived revisions, atomic rights-bound writes, historical locators + current parser/freshness check (`parser_version_unverified` on unknown parser).
- `parsing/locator.py`, `parsing/pdf_adapter.py` (diagnostic only).
- CLI `parsing` group (`cli/parsing.py`): `parse`/`show`/`locator`/`resolve`; safe summaries by default, private content only with `--content`.

## Tests (new, focused)
- Focused: `tests/test_parsing_core.py` + `tests/test_parsing_repository.py` = 36, `tests/test_cli_parsing.py` = 6 → **42 passed**. Full final: **3121 tests OK, no skips**, 399.765s at `35c4f04` via `PYTHONPATH=src /tmp/opencode/step06-pdf-venv/bin/python -m unittest discover -s tests -p 'test_*.py'`. No CI configured.

## Partial MVP / not ticked
- OCR/visual/table-cell interpretation **unsupported → human review**; synthetic self-authored XML/TEI fixtures only, no representative human-adjudicated benchmark. XML structural text is supported, and (S17-P7b) a cleanly parsed PDF report now feeds grounded extraction like an XML report.
- **Update (S17-P7, kickoff §7):** `parsing/pdf_adapter.py` extracts bounded per-page text via the optional `pypdf` extra in an isolated child process, emitting normalized paragraph passages with 1-based page locators. Usable text yields `parsed`; blank/short/garbled/limited/malformed/unavailable inputs stay `needs_review`. OCR, column order, and figure/table interpretation remain unverified → human review; no automatic exclusion.
- **Update (S17-P7b):** a cleanly `parsed` PDF report sets `human_action_required=false` (its text is usable original text) and records the exact `pypdf` version (`pdf_parser_version`) so `read_report` reproduces its passages by re-parsing the same bytes; a mismatch fails closed, while an absent/mismatched pypdf version stays `unverified`. `extraction/runner._derive_material` accepts a verified `parsed` PDF report, adds each passage's `page` locator and a fixed PDF limitation line to the material, and still refuses `needs_review` reports.
- **Update (S19-XMLFIX, kickoff §7):** the same rule now applies to XML/JATS/TEI. A cleanly `parsed` XML report sets `human_action_required=false` even when it declares figures/tables/supplements; the non-text assets keep their own `non_text_assets_require_review=true` flag and the `non_text_assets_present` reason, so a caption never substitutes for reading the asset and a needed figure/table still escalates. A non-`parsed` XML report (`needs_review`/`rejected`) still requires a human action. `PARSER_VERSION` is bumped to `step07-jats-tei/2`; an earlier `/1` report stays readable but is `parser_version_changed`/`unverified` (never silently verified under `/2`). `extraction/runner` appends a fixed non-text limitation line ("Figures, tables and supplements were not read; any value shown only in a figure/table must be marked missing, not inferred.") for such sources and still refuses `needs_review`; `screening/workflow.case_context` marks such a case ready when the text is non-empty and adds the same limitation to the context text only when the request stays within `SAFE_MAX_REQUEST_BYTES`. The PDF path is unchanged.
- Stdlib Python (PSF); optional `pypdf` (BSD-3) PDF diagnostic, no weights; ≤32 MiB local input; no live trials/network/model/approval; no migration, no new deps, no hosted calls.

## Copyable usage
```bash
export PYTHONPATH=src; P="python3 -m review_article_agent.cli --data-dir ./state parsing"
$P parse --project "$PID" --source-artifact-id art-src --source-revision-id rev-src --report-artifact-id art-rep --report-revision-id rev-rep --rights-file report-rights.json
$P show --report-artifact-id art-rep --report-revision-id rev-rep [--content]
$P locator --report-artifact-id art-rep --report-revision-id rev-rep --passage-id p0002 [--content]
$P resolve --locator-file locator.json [--content]
```
