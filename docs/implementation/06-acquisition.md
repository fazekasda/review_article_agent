# 06 — Full-text acquisition, imports, and reconciliation

## Dependencies

05. Acquisition validation may inspect files; scientific digestion still waits for the project gate.

## Deliverables

- Official/authorized resolution waterfall, source/version provenance, missing-paper queue, and explicit human dispositions.
- Folder import, identity validation, immutable asset storage, duplicate/version/supplement relationships.
- Required visual/supplement dependency queue and incremental corpus revisions.
- Integrity-status handling if ratified under P3; unknown status is not asserted clean.

## Acceptance criteria

- A frozen candidate without full text or explicit disposition prevents scientific processing of that revision.
- PDF extension alone never establishes success: test HTML/login responses, wrong-paper PDFs, corruption, scans, incomplete text, and identifier conflicts.
- Identical imports reuse matching results; alternate versions remain distinct; several reports of one study are linked, not deleted as duplicates. Fuzzy titles alone cannot authorize a merge.
- Imported papers do not bypass screening or seed approval. New evidence flags affected artifacts without deleting unaffected earlier work.
- An inaccessible necessary supplement/figure is visible and cannot be interpreted as negative evidence.
- Unofficial acquisition is a manual queue/import path only; no mirror discovery or captcha/paywall bypass code.
- Storage, redistribution, and hosted-transfer constraints remain distinct from free-to-read status and scientific relevance.

## Handoff

Document source licenses/terms actually checked, retrieval failures, version policy, disposition commands, and test assets. User-owned imports are not automatically proof of unrestricted processing rights.

---

## Implementation status (appended; requirements above unchanged)

**MVP implemented; final validation pending.** AC1–AC7 have integrated source and named tests
(checkpoint `dbe613a`; the global guard `4e1e5a3`+`d2f3d57` is integrated at `246df21`). Final
delivery verification is recorded in the PR. Scientific parsing/screening/digestion are Step 07+.
See the [handoff](handoffs/06-acquisition.md).
