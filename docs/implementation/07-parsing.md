# 07 — Parsing, evidence assets, and reference structure

## Dependencies

06. Choose parsers using representative authorized examples, not package popularity.

## Deliverables

- Structured XML-first ingestion and selected PDF parser adapters with explicit versions and quality states.
- Section/paragraph/table/figure/supplement references, stable passage locators, normalization rules, and resolvability checks.
- Bibliography and local citation-marker relationships for later origin tracing.
- Human-review path for necessary material the parser cannot reliably interpret.

## Acceptance criteria

- Resolve a stored passage against its exact source/parsed revision after restart; detect changed parser/text/source dependencies.
- Test multi-column order, ligatures/hyphenation, negation, numbers/units, captions, OCR failures, and necessary non-text assets.
- JATS/TEI citation markers link to matching reference entries when tagged; absent/malformed links are explicit, not fabricated.
- Unrelated malformed bibliography entries do not automatically invalidate every finding; relevant unresolved links remain a check failure.
- Parser success and locator hashes are never labelled semantic correctness, originality, or complete evidence extraction.
- Failed parsing cannot yield an empty-paper exclusion. Required unavailable evidence produces a human action.
- Raw assets remain immutable; normalized text is a separate revision with documented transformations.

## Handoff

Record parser benchmark cases, failures, selected licenses/model-weight terms, resource requirements, and which visuals require human interpretation. No scientific classifier/extractor accuracy is claimed in this step.
