# 05 — Reproducible discovery and corpus freeze

## Dependencies

04; contracts from 01. Implement the approved-context loop and discovery, not evidence extraction.

## Deliverables

- Brief/interview artifacts, seed search and individual approvals, framing challenge, provisional scope map, rubric/search-plan approval.
- PubMed and Europe PMC adapters plus complementary sources ratified in the project plan.
- Query runs, identity reconciliation, citation expansion, coverage report, and explicit freeze/search-again decision.
- Bounded network retries/rate handling and project cost/work reporting.

## Acceptance criteria

- Seed abstracts remain provisional; unapproved seeds cannot silently become accepted project context.
- Broad searches cannot execute unapproved scope/query plans. A substantive change produces an impact/approval request.
- Reconstruct exact queries, timestamps, contributing sources, failures, counts, and deduplication decisions from records.
- Fixed response fixtures test pagination, overlap, missing IDs, partial failures, and incremental reruns without duplicate scientific candidates.
- A known-relevant fixture set and forward/backward citation expansion expose explicit coverage gaps; reporting never claims absolute completeness.
- Human approval freezes a specific corpus revision; subsequent imports/searches do not mutate it silently.
- Public live smoke queries are small, documented, and separate from deterministic test results. No arbitrary API result cap truncation is hidden as complete retrieval.

## Handoff

List source coverage and limitations, tested API contracts, fixtures, and remaining adapters. Search strategy suggestions do not count as validated topic coverage. Record provenance for every candidate, including seeds and imports.
