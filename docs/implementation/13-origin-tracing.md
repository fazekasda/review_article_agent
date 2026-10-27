# 13 — Configurable citation-origin tracing

## Dependencies

07 reference structure, 12 claim/source checks, and tracing policy from 01.

## Deliverables

- Claim–citation attribution analysis with evidence: own result/analysis, conceptual contribution, secondary, mixed, or unclear.
- Claim-relevant reference resolution and upstream acquisition requests, bounded by hop/source/retry limits and cycle detection.
- Persistent trace graph, per-edge provenance, stopping reasons, warning policy, and dependency-valid caching.
- Section-check integration plus a read-only final origin-coverage operation for 14.

## Acceptance criteria

- Depth 0 is the currently cited source; trace direct support, not earliest discovery. Do not recurse every bibliography indiscriminately.
- Test review→primary, multi-hop chains, cycles, multiple candidate references, missing identifiers/full text, compound claims, method citations, original framework reviews, and reused-data analyses.
- Missing citation markers do not prove originality; a nearby citation is not automatically attribution.
- Acquiring a primary paper does not establish claim support. Detect a primary that contradicts/qualifies the secondary and propose scientific correction.
- At budget/unavailable-origin limits, allow only a supported, explicitly secondary account with visible warning and final human approval. Never replace its citation with an unread original or clear unsupported wording.
- New upstream candidates follow existing acquisition/disposition rules. Trace expansion cannot bypass approved scientific gates or budgets.
- Final audit never edits; repairs route back to authorized steps. Cached results invalidate on claim/source/policy changes.

## Handoff

Provide source-chain fixtures, traces, gate/budget evidence, secondary-warning outputs, and known parsing/attribution limits. No exactly correct or exhaustive historical-origin claim is permitted.
