# Step 13 — citation-origin tracing
Status: **locally verified MVP.** Two small, stdlib-only modules plus focused
tests. No live acquisition/model call, no CLI, no storage migration, no new
dependency. **No exactly-correct or exhaustive historical-origin claim is made.**
Full suite: see PR.

## Scope / modules
- `tracing/schema.py` (pure, JSON-portable): `validate_policy`,
  `classify_attribution`, `assess_support`, `trace_atom`, `trace_claim`,
  `trace_fingerprint`; `ATTRIBUTIONS`, `POLICY_FIELDS`.
- `tracing/workflow.py`: `OriginTraceRepository.run/read` — durable, resumable
  tracing that persists the full state (claim entries, trace graphs, project
  budget, `acquire_calls`, `invalidated`, `section_checks`, `status`) as a new
  artifact revision after every claim and resumes from the latest revision.
  `origin_coverage(state)` is the read-only final coverage operation.
  `acquire(request)` is the injected Step 06 acquisition port (no real
  acquisition is made here); `rights(artifact_id, revision_id)` authorises each
  persisted revision.

## Acceptance criteria (each pointer is an actual test method)
- AC1 (depth 0 = the currently cited source; direct support, not earliest
  discovery; no indiscriminate bibliography recursion):
  `TraceAtomTests.test_review_to_primary`, `test_multi_hop_chain`,
  `test_own_result_origin_at_depth_zero`,
  `test_conceptual_framework_origin_at_depth_zero`,
  `test_method_citation_makes_no_acquire_call`,
  `ClassifyAttributionTests.test_nearby_citation_in_other_passage_ignored`.
- AC2 (review→primary, multi-hop, cycles, multiple candidates, missing
  identifier/full text, compound claims, method citations, original-framework
  reviews, reused-data analyses): `TraceAtomTests.test_review_to_primary`,
  `test_multi_hop_chain`, `test_cycle_detected`,
  `test_multiple_candidates_one_supports`, `test_missing_identifier_no_acquire`,
  `test_full_text_unavailable_after_retries`,
  `test_method_citation_makes_no_acquire_call`,
  `test_conceptual_framework_origin_at_depth_zero`,
  `ClassifyAttributionTests.test_reused_data_note`,
  `TraceClaimTests.test_compound_claim_mixed_outcomes`.
- AC3 (missing markers do not prove originality; a nearby citation is not
  attribution): `ClassifyAttributionTests.test_unclear_missing_markers_not_proof`,
  `test_nearby_citation_in_other_passage_ignored`.
- AC4 (acquiring a primary does not establish support; a contradicting/qualifying
  primary proposes correction): `TraceAtomTests.test_primary_contradicts_proposes_correction`,
  `test_primary_qualifies_proposes_correction`,
  `CoverageReadOnlyTests.test_correction_proposed_routes_to_step11_and_12`.
- AC5 (budget/unavailable-origin limits allow only a supported, explicitly
  secondary account with visible warning and final human approval; never an
  unread original or cleared wording):
  `TraceAtomTests.test_budget_limit_secondary_warning`,
  `test_project_budget_limit`, `test_max_hops_stop`,
  `test_depth0_unsupported_blocker`,
  `test_depth0_unsupported_overrides_own_result_origin`;
  `BudgetTests.test_budget_limit_surfaces_warning_and_requires_approval`.
- AC6 (new upstream candidates follow the existing acquisition/disposition rules;
  expansion cannot bypass gates or budgets):
  `TraceAtomTests.test_missing_identifier_no_acquire`,
  `test_pending_disposition`, `test_full_text_unavailable_after_retries`,
  `test_method_citation_makes_no_acquire_call`,
  `test_unset_policy_configuration_required_no_acquire`;
  `PolicyGateTests.test_unset_policy_requires_configuration_and_no_acquire`.
- AC7 (final audit never edits; repairs route back to authorized steps; cached
  results invalidate on claim/source/policy changes):
  `CoverageReadOnlyTests.test_origin_coverage_does_not_mutate_or_persist`,
  `test_correction_proposed_routes_to_step11_and_12`;
  `CacheTests.test_changed_claim_text_retraces`,
  `test_changed_depth0_sha_retraces`, `test_changed_policy_retraces`,
  `test_changed_step12_verdict_retraces`,
  `test_changed_upstream_source_digest_retraces`,
  `test_rerun_cache_hit_makes_zero_acquire_calls`;
  `FingerprintTests.test_fingerprint_changes`.

## Handoff items
- **Source-chain fixtures + traces:** `TraceAtomTests.test_review_to_primary`,
  `test_multi_hop_chain`, `test_cycle_detected`;
  `PersistedTraceTests.test_persisted_trace_graph_is_readable` (persisted graph
  nodes carry depths `{0, 1}`).
- **Gate/budget evidence:** `PolicyGateTests.test_unset_policy_requires_configuration_and_no_acquire`
  and `TraceAtomTests.test_unset_policy_configuration_required_no_acquire`
  (`configuration_required` with **zero** acquire calls);
  `TraceAtomTests.test_budget_limit_secondary_warning`,
  `test_project_budget_limit`, `TraceClaimTests.test_claim_budget_shared_across_atoms`
  (claim/project budgets). Acquisition `requests` are routed to
  `route="step06_acquisition"` with `requires_disposition=True`, and upstream
  nodes are tagged `corpus_admission="not_admitted"` (never admitted).
- **Secondary-warning outputs:**
  `BudgetTests.test_budget_limit_surfaces_warning_and_requires_approval`
  (`origin_outcome="secondary_warning"`, `requires_final_human_approval=True`,
  visible `origin_warning`; coverage lists the `cite_source_key` actually read).
- **Section-check integration:**
  `SectionCheckTests.test_section_checks_keep_verdict_and_never_primary_verified`;
  `OutputHygieneTests.test_json_never_contains_primary_verified`.
- **Known parsing/attribution limits:** attribution cues and support are
  **lexical heuristics** (case-insensitive cue regex lists plus content-word
  overlap) over synthetic fixtures — deterministic and fixturable, not semantics
  of arbitrary prose; only Step 07 tagged citation links are read; cycle
  detection is by `source_key`/`sha` **after** acquisition (so a cycle still
  consumes an acquisition attempt); the `acquire` port is **injected** (a real
  Step 06/07-backed port now exists — see below — but the engine still accepts
  any callable); **upstream cache invalidation depends on the caller supplying
  current `source_digests`** (the engine cannot observe upstream changes on its
  own).
- **Step 14:** `origin_coverage(state)` is the read-only final coverage
  operation — `edits_applied: False`, `export_ready` (plus `complete`), and
  `blockers` each carrying a `route_to` step. No exactly-correct or exhaustive
  historical-origin claim is made.

## Wired acquisition port and real inputs (MVP, kickoff 16 §8)
- `tracing/acquisition_port.py` (`make_acquire_port(store)`) supplies the real
  `acquire(request)` port, backed by **already acquired** sources only:
  - `{"status": "acquired", "source": <source dict>}` when the request's
    `identifiers` (`doi`/`pmid`) match the `expected_identifiers` of an acquired
    candidate (the currently-complete acquisition manifest via
    `AcquisitionRepository.require_acquired`) whose source document has a
    **fully revalidated** parsed report (`ParsingRepository.read_report`). The
    `source` dict is the exact shape `tracing.schema` reads
    (`source_key`, `source.sha256` = the acquired asset digest, `passages`,
    `references`, `citation_links`); reference identifiers are empty (the Step 07
    parser does not extract them), so a further hop stops at `missing_identifier`.
  - `{"status": "pending_disposition"}` when the reference is **known** to the
    project (a current-manifest entry exists) but is not acquired and parsed yet
    — the user must acquire/import it through the existing Step 06 gate.
  - `{"status": "unavailable"}` when the reference is unknown to the project.
  - The port **reads only**: no fetch, no network, no corpus admission, no
    authority; `primary_verified` is never emitted. Reports are found through the
    exact `derived_from` document dependency and only count once `read_report`
    revalidates them (the acquisition manifest also depends on the same source
    and is rejected by that revalidation).
- `tracing/inputs.py` (`claims_and_sources(store, *, draft, package,
  review_state)`) derives the real tracing inputs from the Step 11 draft and its
  approved package: one claim per drafted sentence that cites evidence (atoms
  from the sentence's cited evidence), one tracing source per cited citation key
  (passages = the package's verified original passages; `source.sha256` = the
  acquired asset digest), and a `source_digests` map from those digests.
  Read-only.
- The Step 15 manuscript e2e now uses these real builders + port instead of the
  synthetic `tracing_claims_sources` bridge. With the synthetic frozen-corpus
  source the depth-0 tagged passage does not attribute the claim, so the trace
  ends at **depth 0** with the explicit **secondary-attribution warning**
  (`secondary_warning`, `requires_final_human_approval=True`, listed by
  `origin_coverage` for final human approval) — the spec-acceptable MVP outcome.

## Disclosures
- The `acquire` port and depth-0 sources were **injected fixtures** that count
  calls; the real Step 06/07-backed port (`make_acquire_port`) now also exists
  and reads durable state only. No live model, network, provider, or OpenCode
  session call. No CLI is added and no upstream source is admitted to the corpus.
- The MVP traces each drafted sentence against its first (sorted) cited source;
  a sentence citing several sources is traced against one of them only.
- Cached reuse is **not** a fresh read of the upstream source; a fingerprint
  match only proves the recorded inputs are unchanged.

## Verification
- `PYTHONPATH=src python3 -m unittest tests.test_tracing_schema tests.test_tracing_workflow tests.test_review_schema tests.test_review_workflow`
  — **87 tests OK** (39 tracing schema + 12 tracing workflow + 26 review schema + 10 review workflow).
- `python3 scripts/check_contract_docs.py` — **15 checks ok, 0 failed**.
- `git diff --check` — clean. Full suite: see PR.
