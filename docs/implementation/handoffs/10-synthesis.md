# Step 10 — synthesis, framework, and outline MVP

Status: **complete as a locally verified MVP.** The coordinator's full suite at exact code/test/docs head `c214be1` (`PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`) passed **3,365 tests in 497.967s, 7 skips** (all optional pypdf absent; non-fatal existing SQLite/subprocess ResourceWarnings), plus 15 contract checks, 20 script tests, `compileall`, and `git diff --check`; unchanged spikes at `21e6901` (Python 79 + Jev offline 51 + integration 7 + TS 58, `tsc` clean). Source base `f697548`; authority `10-synthesis.md`, `docs/specification.md`, C01 §5.9/§7.7, ratified POL-P1/POL-P2 (decision 002, accepted 2026-10-27). Local proposal/gate workflow only: no scientific/live-model validation, no blueprint generation, no paid model calls, storage migrations, or external dependencies; **no STORAGE-schema change** (a new synthesis payload schema does exist). No CI workflows are configured, so no CI is claimed.

## Interfaces, gates, and CLI

- `synthesis/schema.py` (pure, JSON-portable): `validate_synthesis` / `validate_framework` / `validate_outline`. Every accepted finding is preserved with its `extraction_ref`/source/context/limitations; every finding must be dispositioned; a statement may trace only to a *retained* disposition (`incorporated`/`genuine_disagreement`/`species_or_outcome_difference`/`null_evidence`/`insufficient_data`). Shared underlying studies/datasets — even a merely `proposed` link — collapse into one correlation group marked `not_independent`; unlinked is `unknown`, never proven independent. POL-P1 concepts require contribution/admission/support/revision; a missing safeguard leaves a concept `unclassified` and it can never be `established`. Synthetic builders `synthesis_example_cases`, `synthesis_example_payload`, `synthesis_example`, `framework_example`, `outline_example` are the machine-checked schema example.
- `synthesis/workflow.py`: `SynthesisWorkflowRepository.propose/read/require_synthesis/require_framework/require_outline/require_for_blueprints`. A synthesis proposal requires the accepted Step 09 outputs gate (`ExtractionWorkflowRepository.require_for_synthesis` + `require_batch`); a framework requires the accepted synthesis; an outline requires an accepted synthesis and framework from the *same* synthesis. Edits store an advisory impact report. Authority is only the named owner gates (`artifact_revision`/`synthesis.memo`, `framework`/`synthesis.framework`, `outline`/`synthesis.outline`); in-band `status` flags and imported JSON are not authority.
- `cli/synthesis.py` (+ registration in `cli/main.py`): local read/propose only — `synthesis propose|show|accepted|impact`. JSON `--input-file`/`--rights-file`, explicit artifact/revision ids; `show|accepted|impact --content` is the private opt-in. Default output is safe structural counts plus the exact `gate_kind`/`gate_id` for the existing `gate` CLI; every response sets `approved`, `authorizes_execution`, `authorizes_export`, `scientific_validation` false; failures are static `SynthesisControlError` categories (`validation`/`conflict`/`storage`).

## Acceptance criteria

- AC1 (several reports of one dataset ≠ independent confirmations): `_correlation_groups`; `CorrelationTests`.
- AC2 (disagreement; species/outcome, null, insufficient; no vote): `FINDING_DISPOSITIONS`, `_validate_dispositions`, `_require_no_banned`, `context_mismatch`; `DispositionTests`, `StatementTests.test_context_mismatch_*`.
- AC3 (trace to findings/sources; separate interpretation): statement `evidence` keys + `classification`; `PreserveEvidenceTests`, `StatementTests`.
- AC4 (no framework/outline before accepted synthesis; P1 fields not omitted): `require_framework`/`require_outline`, `FRAMEWORK_CRITERIA`, `unclassified_concepts`; `FrameworkTests`, workflow `test_framework_cannot_precede_accepted_synthesis`, `test_outline_requires_framework_gate`.
- AC5 (edits → revisions/impact; changed definitions/relationships flag sections): `propose(parent_revision_id=…)`, `_impact`, `_affected_sections`, `dependency_index`; `ImpactTests` (incl. relationship-only and global-change cases).
- AC6 (relationships inspectable; no tables/figures): validated relationship records + `dependency_index`; nothing generates tables/figures.
- AC7 (framework stays a proposal unless justified): `established` needs admission + `justification`, `novelty` labelled; `FrameworkTests`.
- Representative approved chain: `test_approved_chain_and_blueprints_after_reopen` approves synthesis→framework→outline, reopens the store, and consumes `require_for_blueprints` (exact refs + structured context Step 11 needs). No blueprint is generated here; the schema synthetic example is `synthesis_example`/`framework_example`/`outline_example`.

## Verification

- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` at head `c214be1` — **3,365 passed, 7 skips** (497.967s); focused Step 10 suites `test_synthesis_schema` 49, `test_synthesis_workflow` 23, `test_cli_synthesis` 7 (**79 passed**). No model/network/provider call.
- `git diff --check` — clean. This finalization is documentation-only; the contract-doc checker is optional and the full suite is not repeated.
- Full implementation surface: `synthesis/schema.py`, `synthesis/workflow.py`, `cli/synthesis.py` (+ `cli/main.py`), covered by `test_synthesis_schema`, `test_synthesis_workflow`, `test_cli_synthesis`, and the synthesis case in `test_extraction_runner`.

## Disclosures

- Workflow unit tests inherit the **synthetic Step 09 receipt seam** (`extraction.workflow._require_run` patched as in `test_extraction_workflow`). Only `SynthesisOverRealRunnerTest.test_step09_outputs_gate_consumed` extends the **real-store / fake-transport** `test_extraction_runner.FullFlowIntegrationTest` and consumes the real Step 09 outputs gate; no live transport.
- Human approvals use the real owner-capability flow with the **controlling-tty seam mocked**, as the accepted freeze tests do.
- **No scientific/live-model validation**; approval is workflow authority, not established truth. No blueprint generation (Step 11 consumes `require_for_blueprints`). No actual model generation, storage migration, or external dependency was added; the representative fixtures are synthetic.
- Fixture accuracy: `tests/test_synthesis_workflow.py` `synthesis_content` statement text faithfully says the accepted finding "did not reduce"; no production schema change.
