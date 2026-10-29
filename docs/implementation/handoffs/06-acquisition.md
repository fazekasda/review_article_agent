# Handoff — step 06: full-text acquisition, imports, and reconciliation

**Status: MVP implemented; final validation pending.** The Step 06 acquisition MVP
(AC1–AC7) is implemented with integrated source and named tests; the **final
latest-head full suite is pending at this handoff's creation**, and its results are
recorded in the PR — no final pass or count is claimed here. No CI is configured.
This is the minimal acquisition MVP only; no human approval is invented.

## Acceptance criteria → evidence pointers

| AC | Test |
| --- | --- |
| 1 frozen candidate without full text/disposition blocks the revision | `Ac1FrozenCandidateTests` in [`test_step06_core_acceptance.py`](../../../tests/test_step06_core_acceptance.py) |
| 2 extension alone never success (HTML/login, wrong-paper, corruption, scan, incomplete, id conflict) | `Ac2ExtensionNeverSuccessTests` |
| 3 duplicates reuse; versions distinct; reports linked not deleted; no fuzzy merge | `Ac3DuplicateVersionTests`; [`test_acquisition_imports.py`](../../../tests/test_acquisition_imports.py) |
| 4 imports don't bypass screening; new evidence flags, never deletes | `Ac4ImportsAndImpactTests`; [`test_acquisition_impact.py`](../../../tests/test_acquisition_impact.py) |
| 5 inaccessible supplement visible (not negative evidence); unknown integrity not clean | `Ac5SupplementIntegrityTests`; [`test_acquisition_manifest.py`](../../../tests/test_acquisition_manifest.py) |
| 6 unofficial = manual queue/import only; official allowlisted, no mirrors/bypass | `Ac6ManualAndOfficialRoutesTests`; [`test_acquisition_resolution.py`](../../../tests/test_acquisition_resolution.py) |
| 7 storage/redistribution/hosted distinct from free-to-read/relevance | `Ac7RightsDistinctTests` |

Retrieval job/CLI: [`test_acquisition_worker.py`](../../../tests/test_acquisition_worker.py),
[`test_cli_acquisition_retrieval.py`](../../../tests/test_cli_acquisition_retrieval.py)
(10 CLI tests; one a real owned detached worker writing `manual_required` with
**zero GET**). Folder CLI: [`test_cli_acquisition_folders.py`](../../../tests/test_cli_acquisition_folders.py).
Details in [`acquisition.md`](../../acquisition.md), [`cli.md`](../../cli.md) §16,
[`worker.md`](../../worker.md) §13.

## Constraints

- **Official sources only:** two fixed endpoints (Europe PMC `fullTextXML`, NCBI PMC
  `EFetch`); no mirrors, no `oa.fcgi`, no captcha/paywall bypass, no arbitrary URL.
  Unofficial acquisition is a manual queue + local import path only.
- **PDFs are always exact-source human review.** The optional `pypdf==6.21.4`
  inspector is a **diagnostic** (input integrity only), **not** a parser, and is
  **not** wired into manifest acceptance; without it the status is `unavailable`.
- **Rights/terms are distinct and human-reviewed** (acquisition / storage /
  redistribution / hosted transfer); `unknown` storage stores metadata only. Every
  rights/terms value in the runs and tests is a synthetic fixture — the software
  checks shape/`ref` match/freshness, never truth; no real license was checked.

## Verification

- Earlier checkpoint `dbe613a`: **Ran 3027 tests — OK, no skips** (Python 3.14.4);
  retained rollup 29 contract checks + 58 TypeScript pass. **That checkpoint does
  not cover the retrieval CLI, the send fence, or the global guard.**
- The **global cross-artifact exact-byte guard** (`4e1e5a3` + `d2f3d57`) is now
  integrated by the coordinator at `246df21`, so it is not accounted for by
  `dbe613a`.
- **Unrun validation checks** (not later features): live official retrieval,
  paid/hosted calls, uploads, Python 3.11, 10k-scale, a real-network fault harness.
- **Unimplemented later scientific features (Step 07+):** scientific parsing,
  screening, extraction, synthesis, drafting, review, and writer capability.
