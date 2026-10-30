# 14 — Manuscript integration, author notes, freshness, and delivery

## Dependencies

13; all section acceptance conditions must be integrated, not stubbed.

## Deliverables

- Whole-manuscript coherence/scientificness/coverage review and maximum 2 repair rounds.
- Explicit freshness/top-up/cutoff decision; later imports reopen appropriate revisions.
- Human notes action plan, once-per-batch approved edit, affected-content verification, and return to user.
- Final read-only support/origin/approval report and exact-revision final human approval.
- Markdown, BibTeX, RIS and backup/export package; reporting/disclosure/venue material as ratified in 01.

## Acceptance criteria

- Global review cannot start while required section checks remain incomplete. Inspect consequential omitted contrary/qualifying evidence, not just claims already cited.
- Freshness never silently adds papers or runs an endless update loop. User can explicitly accept the existing cutoff.
- Scope/source-changing notes require impact approval rather than a forced local edit. Ambiguous scientific notes require clarification.
- Post-review factual/citation edits trigger support/origin revalidation; no final edit bypasses checks.
- Final secondary warnings are visible and explicitly accepted; unsupported major claims cannot be waived into verified status.
- Manuscript citation keys, bibliography identities/versions, and RIS exports are consistent. No duplicate-as-independent evidence or model-invented reference is introduced during export.
- Validate artifact revisions/hashes immediately before final export; any subsequent change invalidates the applicable approval.
- Export public methods/limitations/AI declarations only under the ratified contract and applicable venue policy; never assert independent validation or submission compliance without basis.

## Handoff

Provide a complete example delivery and failure examples, actual loop counters, final approval manifest, export checks, and backup/restore evidence.

Revalidation after a post-review factual/citation edit **recomputes** the Step 12
support check and the Step 13 origin coverage rather than trusting caller-supplied
results: `DeliveryRepository.revalidate(delivery_id, paragraph_ids, *,
package, origin_state=None)` requires (a) every cited key to exist in the approved
package citations, (b) the deterministic Step 12 fact check to return `supported`
for the paragraph text, and (c) origin coverage to be complete when an origin
state is supplied.  A paragraph passes only when all checks pass; otherwise it
stays `revalidation_required` with recorded reasons, and final approval remains
refused.
