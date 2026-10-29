# Handoff — step 06: full-text acquisition, imports, and reconciliation

**Status: implemented MVP (minimal) — Step 06 is ticked `[x]` in
[`docs/implementation/README.md`](../README.md).** Each acceptance criterion AC1–AC7
has integrated source and named tests and was exercised by the earlier integrated
full checkpoint `dbe613a` (**3,027 core tests, no skips**). This handoff is written
at the docs base `5b61ccfe039210763c1e64868ba79dc38a70eb0c`; the **final
latest-head full suite and the retained-suite rollup are pending the coordinator's
verification run**, so no green final-suite claim is made and no test count is
invented for the docs head. No CI is configured (GitHub workflows
`total_count = 0`).

This is the scoped, **minimal** acquisition MVP only: no scientific parsing,
screening, extraction, synthesis, drafting, review, or writer capability (Step 07+),
and **no human approval is invented** by any code or doc here. All evidence is
fixtures and local files; **no live provider/paid/hosted call or upload was made**,
and no 10k-scale operation was run.

## 1. Scope, base, and authority

- **Step document (authoritative, unedited):** [`06-acquisition.md`](../06-acquisition.md)
  — deliverables and AC1–AC7, not weakened. This task changes **documentation only**.
- **Baseline/contracts:** [`specification.md`](../../specification.md) steps 6/10;
  [C01 v1.4](../../contracts/01-identities.md) (esp. §6.1 **R1**, §5.8), [C02 v3.2](../../contracts/02-gates-and-runtime.md),
  [C03 v1](../../contracts/03-policy-decisions.md); decisions [002](../../decisions/002-step01-contracts-and-policies.md)
  and [003](../../decisions/003-step05-discovery-and-direct-lead.md).
- **This branch:** `task/step06-mvp-handoff`; starting HEAD `5b61ccfe039210763c1e64868ba79dc38a70eb0c`.
- **Integrated at base:** local import/`missing`/occurrence readback, `inspect`
  (validation + optional PDF inspector), read-only `impact`, manifest
  `propose/show/status/history/require`, blocker `queue`, the exact gate
  `artifact_revision`/`acquisition.complete`, the atomic `acquisition folder import`
  CLI, and the detached official retrieval job with the `acquisition retrieve
  id|run|show` CLI.
- **Parallel slice, integrated separately:** the **global cross-artifact exact-byte
  guard** (commit `4e1e5a37fbd2e1702d20fdb92c4dd169e907d5dc` + a narrow repair) is
  **not** in this docs base and is **not** part of the `dbe613a` evidence; it is
  documented here and in [`acquisition.md`](../../acquisition.md) §8 as required
  Step 06 behavior (C01 §6.1 R1).

## 2. Acceptance criteria → evidence pointers

| AC | Named tests |
| --- | --- |
| **1** frozen candidate without full text/disposition blocks the revision | `Ac1FrozenCandidateTests` in [`test_step06_core_acceptance.py`](../../../tests/test_step06_core_acceptance.py) (missing candidate/source blocks even with a generic approval; an explicit disposition is stored, not scientific authorization). |
| **2** extension alone never success (HTML/login, wrong-paper, corruption, scans, incomplete, id conflict) | `Ac2ExtensionNeverSuccessTests` (HTML/wrong-paper/corrupt cannot pass even with review; PDF → `needs_review`, exact-source human review). |
| **3** duplicates reuse; versions distinct; reports linked not deleted; no fuzzy merge | `Ac3DuplicateVersionTests`; [`test_acquisition_imports.py`](../../../tests/test_acquisition_imports.py). |
| **4** imports don't bypass screening; new evidence flags, never deletes | `Ac4ImportsAndImpactTests`; [`test_acquisition_impact.py`](../../../tests/test_acquisition_impact.py). |
| **5** inaccessible supplement visible (not negative evidence); unknown integrity not clean | `Ac5SupplementIntegrityTests`; [`test_acquisition_manifest.py`](../../../tests/test_acquisition_manifest.py). |
| **6** unofficial = manual queue/import only; official allowlisted, no mirrors/bypass | `Ac6ManualAndOfficialRoutesTests`; [`test_acquisition_resolution.py`](../../../tests/test_acquisition_resolution.py), [`test_acquisition_validation.py`](../../../tests/test_acquisition_validation.py). |
| **7** storage/redistribution/hosted distinct from free-to-read/relevance | `Ac7RightsDistinctTests`. |

Also `RestartBackupChainTests` (restart + private backup preserve
occurrence/source/manifest and the gate). Retrieval job/CLI:
[`worker/acquisition.py`](../../../src/review_article_agent/worker/acquisition.py),
[`test_acquisition_worker.py`](../../../tests/test_acquisition_worker.py),
[`test_cli_acquisition_retrieval.py`](../../../tests/test_cli_acquisition_retrieval.py)
(**10 CLI tests**, one a real owned detached worker writing `manual_required` with
**zero GET**). Folder CLI: [`test_cli_acquisition_folders.py`](../../../tests/test_cli_acquisition_folders.py).

## 3. Contract behavior and honest limits

- **Official sources only.** Two fixed endpoints (Europe PMC `fullTextXML`, NCBI PMC
  `EFetch`); no mirrors, no `oa.fcgi`, no captcha/paywall bypass, no arbitrary-URL
  fetch. Unofficial acquisition is a manual queue + local import path only.
- **Global cross-artifact exact-byte guard.** An import of bytes already stored under
  a **different** artifact must not create a second source revision: static
  `cross_artifact_duplicate` with immutable `ByteMatchRef` `matching_refs`, zero
  writes, explicit canonical-target retry (rights bound to the canonical source
  revision) or a `no_full_text_acquired` occurrence. **No automatic identity merge
  or catalog** from byte equality; bounded by `MAX_BYTE_COLLISION_CANDIDATES`.
- **PDFs always exact-source human review.** The optional `pypdf==6.21.4` inspector
  is a **diagnostic** (input integrity only), **not** a scientific parser, and is
  **not** wired into the manifest acceptance; without it the status is `unavailable`.
- **Rights/terms distinct and human-reviewed.** Free-to-read, storage,
  redistribution, and hosted transfer stay separate; `unknown` storage stores
  **metadata only** (use `missing`). No real license/term was checked — every
  rights/terms value in the runs and tests is a **synthetic fixture**; the software
  checks shape/`ref` match/freshness only, never truth.
- **Durable, honest retrieval.** A may-have-dispatched mark is journaled **before**
  every GET under the registry write guard and safe metadata after; the send
  admission is linearized with pause/cancel under the **captured** lease, and no
  lock is held across network work. `run` returns a **queued admission only**
  (`job_completion=false`); `show` is a verified safe readback with an explicit
  private `--content` opt-in and issues no request. An `interrupted`/ambiguous
  outcome is never silently re-sent.
- **Gate bound fails closed.** `MAX_GATE_SNAPSHOT_ENTRIES = 10000` refuses rather
  than truncating; no 10k-scale operation was run.

## 4. Verification

- **Earlier integrated full checkpoint** `dbe613a`
  (`dbe613a4049b7744445a78e5d862be524f39fec6`; log
  `/tmp/opencode/step06-pre-worker-full.log`, `VERIFIED_START_SHA=dbe613a` …
  `VERIFIED_END_SHA=dbe613a`, `EXIT=0`): Python **3.14.4**, **Ran 3027 tests in
  389.425 s — OK, no skips**.
- **Retained-suite rollup** (coordinator-associated): **20 scripts, 79 Python
  retained spikes, 51 live-Jev mock, 7 integration mock, 58 TypeScript, 29 contract
  checks** + clean `tsc`. Locally re-verifiable: `/tmp/opencode/step06-contract-check.log`
  (29 ok, 0 failed), `/tmp/opencode/step06-retained-jev.log` (Ran 51 tests, OK),
  `/tmp/opencode/step06-retained-ts.log` (58 pass / 0 fail).
- **Pending:** the final latest-head full combined suite + rollup (coordinator).
  Targeted evidence after `dbe613a` is the 10-test retrieval CLI suite (§2).
- **Not run:** live official retrieval, paid/hosted calls, uploads, Python 3.11,
  10k-scale, and a real-network in-flight fault harness. No CI is configured.

## 5. Remaining actions

1. **Coordinator:** integrate the parallel global byte guard + narrow repair
   (`4e1e5a3`), run the final latest-head full combined suite, record the exact-head
   rollup (no CI).
2. **User:** review and merge (release actions).
3. **Deferred:** scientific parsing/screening/digestion (Step 07+) and the unrun
   items in §4.

Evidence log paths: `/tmp/opencode/step06-pre-worker-full.log` (`dbe613a`),
`step06-contract-check.log`, `step06-retained-jev.log`, `step06-retained-ts.log`.
No secrets, licensed full text, or raw provider payloads are included.
