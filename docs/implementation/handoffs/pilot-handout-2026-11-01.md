# Review-article agent pilot: status and improvement handout

**Snapshot:** 1 November 2026, after the explicitly authorized single extraction call.<br>
**Project:** `prj_a8128474930448fca0de7d8ba18f5fca`<br>
**Status:** screening complete; extraction pilot blocked; no accepted extraction, synthesis, manuscript or export.<br>
**Authorization:** no further paid calls authorized at this stopping point. This document proposes future work; it does not approve execution or merge a pull request.

> **Provenance:** This is a historical snapshot dated 2026-11-01, moved into this repository at the owner's request. The original execution data and raw pilot material remain outside the repository. Later states of the pilot may differ from this snapshot.

## 1. Executive summary

The live pilot has exercised framing, literature discovery, open-access acquisition, parsing, screening, human adjudication and exclusion approval. It exposed and drove repairs to several integration and recovery defects that synthetic tests had not caught.

The corpus contains 23 acquired and parsed papers. Screening produced 12 retained papers, 4 uncertain papers that remain in consideration, and 7 exclusions approved after random and risk-based original-source checks. There are therefore **16 retained/uncertain papers**, not 16 confirmed eligible papers and not a final review selection.

Extraction has not yet produced an accepted result. The latest call successfully returned, preserved its cost, and exposed a concrete schema mismatch: all 29 known numeric values were strings rather than JSON numbers. For example, the model returned `"15.35"` where the schema requires `15.35`. The validator correctly rejected the output. Its 27 proposed findings are **unvalidated**, not an evidence base for writing.

The extraction infrastructure fixes are in **[PR #37](https://github.com/fazekasda/review_article_agent/pull/37)**, currently open and unmerged. Its exact head is `c2ee583310bf112e44cc4b42f2f3112ce5b3fed3`. All 12 applicable CI checks passed; the release job is intentionally conditional and skipped for this PR. The coordinator's complete local slow suite passed 3,742 tests, with zero failures/errors and 56 existing skips.

**Recommended next move:** review the PR and perform offline prompt/schema alignment against the retained rejected output. Agree on the next bounded live experiment only afterward. Do not simply repeat the same call or weaken validation to obtain a green pilot.

## 2. Agreed pilot scope and operating rules

### Scientific objective

Explain how insulin/IGF-1 signalling—DAF-2 → AGE-1 → DAF-16/FOXO—regulates lifespan in *Caenorhabditis elegans*, including effect magnitudes, experimental conditions, DAF-16 dependence, contradictions and limitations.

- Intended output: a two-section narrative review, ultimately based on roughly 3–5 included papers.
- Initial discovery target: approximately 30 candidates.
- Boundaries: English, primary experimental work, publication years 2015–2026, freshness cutoff **2026-10-31**, authorized open-access full text only.
- Eligibility: C. elegans experiments; qualifying IIS-pathway manipulation; measured lifespan. Compound studies must explicitly test pathway components or dependence, rather than merely claim pathway association.
- Exclusions include reviews, editorials, protocols, preprints, other-organism-only work and healthspan-only studies without lifespan.
- Figures, tables and supplements are not treated as read. Figure-only values must remain missing; source inspection escalates when those materials are needed for a decision.
- Samples: screening pilot of 10; extraction pilot of 2; subsequent audits use one random and one highest-risk case. Sampled checks do not establish population accuracy or complete coverage.

### Models and providers

| Role | Agreed model | Route/provider |
|---|---|---|
| Lead and writing | `anthropic/claude-sonnet-5.5` | Anthropic via OpenRouter |
| Critic and global reviewer | `openai/gpt-6.1-terra` | OpenAI via OpenRouter |
| Screening | `typesafe/jev-1.14` | Pinned typed-decision route via OpenRouter |
| Extraction | `deepseek/deepseek-v4.1-flash` | `google-vertex/global` via OpenRouter |
| Quality checks and notes planning | `anthropic/claude-haiku-5.5` | Anthropic for the prepared extraction config |

No model substitution is authorized by this handout. No sensitive data are intended; provider data collection was acceptable to the owner, although adapters may apply stricter settings.

### Approval, budget and isolation rules

- The owner approves each gate in a real terminal using `~/review-pilot/ops/approve.sh`. Chat/form adjudications do not replace the terminal gate.
- Material config, instructions, model or parser changes reopen the relevant approvals and pilot checks.
- Application caps remain **$25/project**, **$10/stage**, and at most **2 concurrent calls**; actual pilot scheduling has used a single serial remote slot.
- Agreed per-role guards are $25 for strong roles and $5 for cheaper roles. Their enforcement should be checked consistently across runners rather than assumed from documentation alone.
- Direct-call network/unknown outcomes use bounded retries under new identities, conservative accounting and approximately 30% stage retry overhead. This policy does not authorize unlimited retries of scientific/schema failures.
- Raw pilot material stays outside the repository in `~/review-pilot/`. Only proportionate implementation documentation and an eventual cost/friction summary belong in the repository.

## 3. Completed work

### Repository and operations

- Removed 111 old worktrees; retained the two special-purpose Step 04 worktrees containing dirty/unique work.
- Deleted 180 merged local and 28 merged remote branches; enabled automatic branch deletion after merge.
- Added the operator guide and approval helper workflow.
- Pruned tests pragmatically rather than forcing the unsupported target of roughly 1,000 tests. PR #32 merged at 3,638 tests; subsequent repairs increased the suite.
- Main's required checks include `ci-gate`, `fast (py3.11)` and `fast (py3.14)`.

### Discovery, acquisition and parsing

- Completed the framing call and obtained protocol approval.
- PubMed/Europe PMC searches yielded 40 occurrences and 28 corpus candidates.
- Citation expansion found 102 edges across 100 frontier papers. **Those frontier papers were not admitted to the corpus.**
- Recorded coverage, froze 23 includes and 5 excludes, and acquired official OA JATS full text for all 23 includes.
- Recorded licences, rights and the text-only limitations in the acquisition manifest; no required non-text assets were declared.
- Parsed all 23 papers as `rep-<pmcid>/rep-r2` using `step07-jats-tei/2`.

### Screening and human review

- Configured three eligibility predicates: primary C. elegans research, qualifying IIS manipulation, and measured lifespan.
- Thresholds: probability below 0.2 proposes exclusion; at least 0.8 retains; intermediate results remain uncertain.
- The seeded 10-case pilot was screened and adjudicated by the owner: 6 retain, 2 uncertain, 2 exclude, no important failure recorded.
- After terminal pilot approval, screened the remaining 13 papers and reused the pilot results.
- All 48 successful screening-window requests completed with known cost and no retries.
- Final machine batch: **12 retain / 4 uncertain / 7 proposed exclusions**.
- Random audit source: **PMC13296142**, the Ficus carica polysaccharide study.
- Highest-risk audit source: **PMC13373874**, the long-lived-mutant gene-expression tool report.
- The owner confirmed both sampled exclusions and approved the seven-exclusion gate.

Approved exclusion IDs:

```text
pmc13296142  pmc13373874  pmc13535778  pmc13603120
pmc13609055  pmc13678977  pmc13714644
```

### Extraction preparation

- Approved `extraction-config/config-r1` and exact-paper hosted-use documents for two pilot sources:
  1. **PMC13714240:** USP-48 enables insulin-mediated longevity—genetic study.
  2. **PMC13450824:** isoliquiritigenin, DAF-16 and aquaporin-2—compound study.
- Configured original-passage grounding, context, numerical details, missingness, negation/null preservation, attribution, coverage/omissions and narrative limitations. No formal appraisal tool or universal quality score is configured.
- Approved revised `extraction-config/config-r2` and both hosted-use `hu-r2` revisions after a diagnostic call demonstrated output truncation.
- The **only config change** from r1 to r2 was extractor `max_output_tokens`: **16,384 → 65,536**. Extractor context remains 131,072 tokens; quality output ceiling remains 8,192.
- No Haiku quality call has been made. Its hosted-use approval must bind the actual upstream extraction receipt, which is not yet usable.

## 4. Product repairs and their release status

| PR | Work | Status |
|---|---|---|
| [#31](https://github.com/fazekasda/review_article_agent/pull/31) | Operator guide | Merged |
| [#32](https://github.com/fazekasda/review_article_agent/pull/32) | Test pruning | Merged |
| [#33](https://github.com/fazekasda/review_article_agent/pull/33) | Gate staleness uses a revision's own inputs; ancestors are lineage | Merged; C02 v3.3 clarification |
| [#34](https://github.com/fazekasda/review_article_agent/pull/34) | Cleanly parsed XML with figures no longer forces human action; limitation retained | Merged; parser version `/2` |
| [#35](https://github.com/fazekasda/review_article_agent/pull/35) | Whole-text bounded screening windows; pre-send refusal handling | Merged |
| [#36](https://github.com/fazekasda/review_article_agent/pull/36) | Direct-call retry policy and Jev `transport=None` fix | Merged; ADR 006 / C02 v3.4 |
| [#37](https://github.com/fazekasda/review_article_agent/pull/37) | Extraction diagnostics, numeric serialization and result-commit safety | **Open, green, unmerged** |

### Screening-window behaviour

Jev requests are limited to 32 KB. The revised implementation splits the complete article into deterministic bounded windows, reserves space for headers and non-text limitations, and drops no article text. Each question takes the maximum probability across windows. Missing/refused answers retain uncertainty; exclusions require all windows to fall below the relevant excluding threshold. Each completed window has a separately verified result reference.

### Retry behaviour

- Direct calls only; OpenCode session turns are unchanged.
- Deterministic request chain: `X`, `X-r1`, `X-r2`; at most three attempts, subject to the overhead cap.
- Backoff: 2 seconds, then 8 seconds, supplied by production runners.
- An unknown predecessor is terminally resolved with its full reserve booked as an estimated cost before a successor is considered.
- The stage policy uses `superseded_total + retry_reserve <= max(0.30 × base_total, retry_reserve)`; a fresh stage can receive one retry from the floor, but this is not a general allowance for two retries.
- The cap is rechecked across restarts/reruns. Exhaustion/refusal frees the slot without claiming the request was free.

### PR #37 contents

1. Static response diagnostics survive adapter → runner → worker → CLI, without retaining raw provider text in diagnostic metadata.
2. Definitive pre-billing rejections transition to `failed` before settling `not_incurred`, avoiding a database constraint violation.
3. Decimal-valued model content is serialized only if it round-trips to a finite JSON-native number without numeric loss. Overflow, underflow and precision loss produce a clean diagnostic. Cost arithmetic remains exact Decimal.
4. Unexpected extraction exceptions log only their class name.
5. Expected proposal-guard rejection of oversized/forbidden-key/invalid-shape content discards the rejected raw payload and records a tiny static diagnostic with reported cost through the same guard. Size/security guards are not weakened. Storage, fencing and non-allowlisted errors still fail closed.

These defects were reproduced offline at real adapter/runner/worker seams. The exact subtype of the earlier lost live responses is **not provable**, because those responses were rolled back/discarded. Tests demonstrate the repaired patterns, not retrospective recovery of missing evidence.

## 5. Current blocker: what the last call established

The owner authorized exactly one further call on the smaller paper using the reviewed PR branch. It made one POST, with retries disabled for that experiment.

| Item | Observed result |
|---|---|
| Request | `extract-pilot02-one-1` |
| Runtime head | `c2ee583310bf112e44cc4b42f2f3112ce5b3fed3` |
| Receipt | `extract-pilot02-one/run-r1` |
| Model/provider | DeepSeek V4.1 Flash / Google |
| HTTP status | 200 |
| Tokens | 9,799 prompt; 27,002 completion; 36,801 total |
| Cost | **$0.0353421**, known |
| Application outcome | `escalation`, reason `extraction_schema_invalid` |
| Validator error | `findings[0].numeric_details[0].value.value must be a finite number when known` |
| Returned output | 27 proposed findings; all 29 known numeric values were strings |

Example of the rejected representation:

```json
{"name":"mean lifespan extension","value":{"state":"known","value":"15.35"},"unit":{"state":"known","value":"%"},"denominator":{"state":"missing","value":null}}
```

For a plain numeric value, the `value.value` field must be a JSON number, not a string. This identifies a model-output contract mismatch, not evidence that the underlying scientific value is correct. A quoted substring, an accepted HTTP response, a completed job, or a well-formed receipt alone never establishes a valid scientific extraction.

**Do not silently cast every numeric string.** Ranges, inequalities, units, uncertainty and significant figures need an explicit representation policy. Do not turn rejected output into accepted evidence by editing stored artifacts or bypassing the validator.

## 6. Costs and financial uncertainty

Amounts below are the application dispatch ledger, not coding-assistant/subagent charges or a provider invoice reconciliation.

| Stage | Known reported charges | Conservative estimated charges | Accounted total |
|---|---:|---:|---:|
| Framing/discovery model call | $0.024966000 | $0 | $0.024966000 |
| Screening | $0.012341322 | $0.001375962 | $0.013717284 |
| Extraction attempts | $0.104796540 | $0.235929600 | $0.340726140 |
| **Application ledger total** | **$0.142103862** | **$0.237305562** | **$0.379409424** |

- Earlier external smoke checks add $0.000014 for Jev and $0.0000315 for an adapter smoke test, tracked separately from this application ledger. Including them gives approximately **$0.379455** accounted exposure.
- Extraction comprises six dispatches: four with known reported charges and two conservatively estimated at their full $0.1179648 reserves.
- The two estimates are **not confirmed charges** and are not zero. They reflect results/charges lost during earlier commit failures.
- At this snapshot there is **no outstanding unknown reserve**, no remote dispatch occupancy, and the dispatch integrity check is clean.
- The extraction worker is stopped. Registry execution activation remains active for this project; that is not evidence of a running provider call.

## 7. Approval and execution state

Completed stages reported by `pipeline status`:

```text
discovery.plan → discovery.coverage → discovery.freeze
→ acquisition.manifest → parsing.reports
→ screening.pilot → screening.exclusions → extraction.config
```

The pipeline reports **`extraction.pilot`** as next. This is a stage label, not proof that usable pilot receipts exist. They do not.

Current exact approval targets:

- Screening pilot: `screening-pilot/pilot-r1`, gate `pilot / screening.pilot`.
- Screening exclusions: `screening-audit/audit-r1`, gate `exclusion_set / screening.exclusions`.
- Extraction config: `extraction-config/config-r2`, gate `batch / extraction.config`.
- Pilot extraction hosted use: `hu-extraction-pilot01/hu-r2` and `hu-extraction-pilot02/hu-r2`, gate `batch / extraction.hosted_use.extraction`.

Earlier extraction r1 documents remain history, not the config to resume. The latest single-call authorization has been consumed. Larger-paper extraction, Haiku checks, bulk extraction and synthesis remain paused.

## 8. Prioritized improvement plan

All items below are proposals, not authorized implementation or paid work. Work should be split into bounded tasks with explicit evidence; avoid combining every finding into one large refactor.

### P0 — unblock extraction without compromising evidence

**P0.1 Review and disposition PR #37.** Reconfirm the current head and all required checks before any merge/release decision. The PR is not merged by this handout.

Acceptance: current-head CI green; focused security/financial regressions reviewed; no schema/size guard relaxation; runtime checkout and worker provenance recorded explicitly.

**P0.2 Perform offline prompt/schema alignment.** Use the retained last response as a rejection fixture. Identify every mismatch, not merely the first numeric-string error. Tighten examples/instructions for number types, missingness, units, denominators and qualified results. Investigate structured-output constraints if the approved route supports them; do not assume availability equals enforcement.

Acceptance: unchanged rejected response still fails; valid fixtures contain true JSON numbers where required; strings/ranges/inequalities/units are not silently coerced; exact snippets and qualifiers are preserved; a fully populated example passes the real runner and workflow validators without network access.

**P0.3 Align output budgets and storage limits.** The extractor permits 65,536 output tokens and a 2 MiB response, while proposal content is bounded at 262,144 bytes. Full-text coverage plus long snippets can exceed the proposal limit even when the provider response fits.

Acceptance: boundary tests cover response size, proposal size, context and output ceilings; oversized results become charged diagnostics, not crashes or silent truncation. Prefer compact, bounded output or an explicitly designed chunk/merge strategy over blindly raising limits. Any extraction chunking must preserve context, passage coverage, omissions and cross-window qualifications.

**P0.4 Agree on one next live experiment.** Decide whether prompt correction, output-format controls or another approach is warranted. Material changes require new config/hosted-use revisions and relevant gate approvals. Keep the experiment to one paper initially, with a declared call count, reserve and stop condition.

Acceptance: one structurally valid, source-bound extraction receipt; honest cost/diagnostic reporting; no unapproved retry or spillover into the other paper/quality calls. A schema pass is only the entry point to scientific checking.

### P1 — finish the pilot and original requested review

1. Complete both extraction examples under one exact approved config. Cover a genetic study and a compound study, with missing/negated/null/quantitative/context/attribution and non-text failure modes addressed explicitly.
2. Prepare and approve Haiku hosted-use documents bound to the actual upstream extraction receipts, then run quality checks against original text.
3. Have the owner inspect original sources for omitted findings, softened qualifications and field correctness. Important/major failures block bulk work; material changes trigger re-pilot.
4. Propose and obtain extraction-pilot approval. Determine the final 3–5-paper review subset explicitly; screening's 16 retained/uncertain papers are not that final selection. Resolve uncertain eligibility before treating a paper as included.
5. Run only the approved bulk extraction scope; inspect one random and one highest-risk output, including omissions, and approve accepted outputs before synthesis.
6. Create and approve the cross-paper evidence map/synthesis memo, scientific framework and two-section outline. Do not equate paper counts, confidence or model votes with strength of evidence.
7. Approve section sources/blueprints; draft from extracted summaries **and** linked original evidence. Run the distinct section critic and factual checks with the existing bounded repair limits.
8. Run global review and the freshness decision; process any owner notes once per approved batch; complete reference-origin tracing after factual edits.
9. Obtain exact-revision final approval and export. Write the repository's compact pilot cost/friction summary without copying raw pilot material into it.

Acceptance: each gate is bound to exact current artifacts; no synthesis or writing bypasses extraction audits; all final factual claims have appropriate original support or explicit limitations; no claim of comprehensive literature coverage.

### P1 — reliability and operating safety

- **Audit latent sibling defects:** generation/lead paths may share the settle-before-fail ordering and Decimal-content parsing patterns. Verify with failing real-seam fixtures before changing them; do not assume all siblings are affected identically.
- **Complete failure diagnostics:** persist safe schema error codes/field paths as well as adapter failures. The latest schema escalation still has `diagnostic_code: null`; its cause required a local validator replay. Never log raw secrets/provider/source text in the safe summary.
- **Capture financial evidence before fragile result handling:** ensure expected parse/schema/proposal failures cannot discard valid reported usage/cost. Genuine storage/fencing/corruption failures must remain explicit, not be presented as free calls.
- **Finish operator recovery controls:** supply a supported CLI for quiescing/recovering legacy dispatches and job/dispatch reconciliation. The pilot required operator library calls; a completed job can still coexist with an unresolved dispatch in older paths.
- **Clarify lifecycle semantics:** distinguish job completed, adapter completed, schema-valid extraction, human-audited evidence and authorization to proceed. Surface precise stage blockers instead of generic `discovery_indeterminate` labels for extraction.
- **Add cost reporting:** known charges, estimates, reserves, stage/project/role caps, retry exposure and provider reconciliation should be visible without SQL/scripts. Never label an estimate as an actual charge.
- **Clarify retry metadata:** `no_automatic_retry: true` remains a per-adapter-call claim but is misleading at runner level. Record logical request and attempt chain explicitly; retain restart-safe cap checks and explain the first-retry floor.
- **Ensure one-call controls are supported:** the explicitly authorized experiment used an operator wrapper to cap POST count and disable retries. Make bounded diagnostic mode an explicit, tested interface rather than ad hoc orchestration.

Acceptance: production-default transports, rejection/recovery/cost branches, crash windows, malformed model content, and replay are exercised at adapter→runner→worker→CLI seams. No same-identity duplicate POST, silent cap increase or ambiguity reset.

### P2 — scientific coverage and data ingestion

- **Search recency bias:** first-page default ordering yielded a recency-heavy sample. Expose ordering/pagination/coverage evidence and assess foundational IIS work rather than treating the sample as the complete literature.
- **Citation-expansion admission:** provide a deduplicated, provenance-preserving path from the 100-paper frontier into corpus revision, with the approved admission cap and impact review. The 102 edges are not 102 newly included papers.
- **DOI normalization:** repair case-variant conflicts such as `eLife` versus `elife`, with positive/negative identity fixtures and no merging of genuinely different reports/studies.
- **OA rights tooling:** derive source/occurrence/report rights and terms records from validated licence information; preserve CC-NC/ND restrictions and require explicit hosted authorization where necessary. Avoid the current per-paper manual boilerplate.
- **Preflight rights earlier:** lead/config setup should expose redistribution/hosted-transfer authorization defects before gates and queued jobs fail. Never infer rights from accessibility alone.
- **Rights correction workflow:** design an auditable amendment path or clearer guidance when fixing rights requires new content revisions and reopened gates.
- **Integrity metadata:** make correction/retraction/non-replication checks reproducible, with source evidence, uncertainty and freshness rather than unsupported completeness claims.
- **Non-text evidence:** distinguish captions, machine-readable tables, figures and supplements. Identify material omissions explicitly; add targeted acquisition/human inspection when needed rather than pretending text-only extraction read them.

Acceptance: corpus changes retain lineage and trigger appropriate—not blanket—reapproval; identity/rights fixes do not silently change historical evidence; coverage reports state source and sampling limitations.

### P2 — configuration, interface and test maintenance

- Replace screening's required-but-ignored chat-model config and fake positive output price with truthful typed Jev configuration; permit legitimate zero prices where appropriate.
- Improve field-specific CLI validation, including `pricing_ref` whitespace errors, without echoing credentials or private content.
- Make the approval helper reject accidental duplicated rationales with a clear positional-argument error. Keep real-terminal confirmation; do not automate owner approval.
- Review provider-specific prices and tier overrides when configuring models. The extraction snapshot uses Vertex-specific prices, not the cheapest advertised provider; Haiku's reserve uses conservative tier prices.
- Align real-service test expectations with the installed OpenCode version (the earlier environment had v2.2.5 while tests pinned v2.2.0). Do not misreport skips as exercised integration evidence.
- Investigate the intermittent worker concurrent-start IPC `BrokenPipeError` independently. It was observed under parallel load and passed standalone/re-run; do not hide it with retries or skips.
- Keep tests proportional and focused on invariants. The failed attempt to reach roughly 1,000 tests is not a reason to remove needed coverage.

## 9. Resume checklist and decision points

Before any new paid attempt:

- [ ] Review/disposition PR #37; record the chosen runtime commit. Do not assume the pilot checkout follows the coordinator's branch.
- [ ] Inspect the retained response and complete the offline contract-mismatch inventory.
- [ ] Decide on corrective instructions/format strategy; do not silently repair stored rejected scientific output into accepted evidence.
- [ ] Reapprove affected exact revisions if settings/instructions/provider behaviour materially change.
- [ ] Obtain explicit owner authorization for the next experiment, including one-call/retry limits and cost bound. The previous single-call authorization is spent.
- [ ] Verify worker/code provenance, current source/config/hosted gates, stage caps and zero unintended pending work.
- [ ] Afterward report accepted versus rejected outcome separately from HTTP/job success, plus actual/estimated cost. Stop on the agreed condition.

Outstanding owner decisions: PR merge/review; prompt/schema strategy; whether to fund another bounded experiment; final review-paper subset; subsequent quality, pilot, extraction-output and manuscript approvals. This handout does not settle them.

## 10. Operational locations and read-only checks

### Checkouts

- Main working checkout: `/home/fazekasda/projects/review_article_agent`, branch `docs/session-implementation-plan` (not switched or overwritten during this work).
- Coordinator/integration checkout: `/home/fazekasda/projects/review_article_agent-worktrees/s19-int-extractdiag`, branch `fix/extraction-protocol-diagnostics`, head `c2ee583…`.
- Pilot application checkout: `~/review-pilot/app`, detached at `dd52c4b5d2a529b79113032dfe3a6d45fe331eb0` (merged PR #36). **It does not yet contain PR #37.**
- Disposable worker checkouts for these repairs have been removed after clean integration; the current integration checkout remains available for the open PR.

### Data and evidence

- Project store: `~/review-pilot/project/.review_article_agent/store.sqlite3`.
- Application data/registry: `~/review-pilot/data/`.
- Operator inputs and results: `~/review-pilot/ops/`.
- Screening: `ops/screen/pilot-input.json`, `pilot-adjudications.json`, `batch-input.json`, `batch-preview.json`, `audit-input.json`.
- Extraction config: `ops/extract/config-r2.json`; previous `config.json` is r1.
- Latest result: `ops/extract/pilot02-one-result.json`; local replay: `pilot02-one-local-validation.json`.
- Latest receipt: `extract-pilot02-one/run-r1`; retained proposal: `prop_c018f1391f7cd42420c960cfbf2834a5`.
- Latest request: `extract-pilot02-one-1`; dispatch: `disp_6f9de6eee227d4f75ae66d70e0c27180`.
- Cost, decisions, tasks, repairs and gate ledger:
  `~/.local/state/opencode-coordinator/6803b8c89a0b0dd2de643e7cb58db51e763c4278393e2fb549f2b30cd4ebb0c9/s19-pilot-20261031/ledger.json`.

Use the reviewed code for diagnostic-aware reads; these commands make no model call:

```bash
CODE=/home/fazekasda/projects/review_article_agent-worktrees/s19-int-extractdiag
P=prj_a8128474930448fca0de7d8ba18f5fca
R=(env PYTHONPATH="$CODE/src" python3 -m review_article_agent --data-dir "$HOME/review-pilot/data")
"${R[@]}" pipeline status --project "$P"
"${R[@]}" worker status
gh pr view 37 --json headRefOid,state,statusCheckRollup
```

The latest one-call experiment used the guarded library runner directly, not a new worker job. Its receipt/proposal and saved result are authoritative records; do not assume a worker-job status lookup alone will locate it. No credential values should be copied into this handout, logs or future prompts.

## 11. Confidence and limitations

- This is a pilot execution and engineering handout, not a scientific review or claim of scientific validity.
- Owner adjudications are recorded as supplied; their short rationale was “Approved.” Do not invent more detailed inspection evidence retrospectively.
- Known costs come from accepted provider-reported usage; estimates cover lost results and need independent provider reconciliation if exact billing matters.
- The final observed blocker is known from retained output. The exact failures inside earlier discarded responses remain unknowable from current records.
- The latest code is CI-verified but unmerged; infrastructure regression tests do not establish that future model output will satisfy the schema or be scientifically correct.
- Future improvement items are prioritized proposals. They have not been implemented, approved or funded merely by appearing here.
