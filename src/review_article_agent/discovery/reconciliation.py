"""Deterministic, conservative publication-overlap **review report** (Step 05).

Authority
---------

``docs/implementation/05-discovery.md`` deliverable 3 (query runs / identity
reconciliation) and acceptance criteria 3 ("reconstruct exact queries,
timestamps, contributing sources, failures, counts, and deduplication decisions
from records") and 4 (fixed fixtures for pagination, overlap, missing IDs, and
partial failures/incremental reruns without duplicate scientific candidates);
``docs/specification.md`` workflow steps 2-5 and the untrusted-data invariant
("treat documents/model outputs as untrusted data"); and
``docs/contracts/01-identities.md`` §3.3 (a content hash is not scientific
identity) and §6 (duplicate bytes, alternate versions, and linked reports of one
study are distinct relations, and distinct ids alone never establish
independence).

What this module is
-------------------

A **pure, deterministic** function :func:`reconcile_searches` over immutable
per-run inputs (:class:`SearchRun`: an explicit ``run_id`` plus a
:class:`~review_article_agent.discovery.records.SearchResult`). It reconstructs
the *identifier overlap* between observed provider occurrences and returns an
immutable :class:`ReconciliationReport`:

* every occurrence is uniquely pinned by ``(run_id, record_index)`` and retains
  its exact :class:`~review_article_agent.discovery.records.MetadataRecord`,
  including identical duplicate metadata within one run;
* occurrences are linked by **exact** asserted identifier tokens only --
  provider-native ``(source, source_id)`` and namespaced ``pmid`` / ``pmcid`` /
  ``doi`` -- and the connected components of that token graph become **review
  groups**;
* a group with more than one distinct value in any publication namespace (a
  ``pmid``/``pmcid``/``doi`` namespace or one provider's native namespace) is
  flagged :data:`CLASS_IDENTIFIER_CONFLICT` and is **never** labelled a resolved
  duplicate;
* a group is otherwise :data:`CLASS_IDENTIFIER_OVERLAP` (more than one
  occurrence sharing an exact token), :data:`CLASS_UNMATCHED` (one identified
  occurrence), or :data:`CLASS_MISSING_IDENTIFIERS` (one occurrence with no
  identifier at all).

What this module is **not**
---------------------------

* **Not science, and not an approval.** A group is a *review* group, never an
  accepted identity, a deduplicated scientific candidate, a study, or an
  authorization. Nothing here merges, ingests, ranks, expands citations, or
  freezes a corpus. Group ids are report-local labels only.
* **Not a completeness claim.** A retrieval that ran to a natural end is not a
  complete literature; a report never asserts completeness. Every report is
  explicitly non-authorizing (``ReconciliationReport.authorizing`` is ``False``).
* **Not fuzzy matching.** Only exact, already-normalized identifier tokens are
  compared. Case, identifier prefixes, DOI/URL spellings, titles, and years are
  never normalized, guessed, or fuzzed here -- the provider adapters own
  normalization, and publication identity is never treated as study identity.
* **Not a store.** It defines no path, schema, or durability policy.

Determinism, replay, and bounds
-------------------------------

Run ids are sorted and record indexes preserved, so the report is identical for
any permutation of the input run order. The same ``run_id`` supplied twice with
an identical result is an **input replay** and collapses to one run; the same
``run_id`` with a divergent result is a **static conflict** and is refused. The
total occurrence count is bounded by :data:`MAX_TOTAL_OCCURRENCES`; crossing it
is an explicit technical refusal, never a silent truncation. Token linking uses
a token index plus union-find (near-linear), not an all-pairs comparison.

Every failure raises :class:`DiscoveryReconciliationError` with a static,
value-free message: no query, URL, title, identifier, or run id is ever echoed.
Each accepted run result is additionally validated through the existing codec
(``encode_search_result`` -> ``decode_search_result``) so a forged or mutated
dataclass cannot smuggle an inconsistent aggregate into the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..domain.errors import ReviewAgentError
from .records import MetadataRecord, SearchResult
from .serialization import (
    DiscoverySerializationError,
    decode_search_result,
    encode_search_result,
)

__all__ = [
    "NAMESPACE_PMID",
    "NAMESPACE_PMCID",
    "NAMESPACE_DOI",
    "CLASS_IDENTIFIER_OVERLAP",
    "CLASS_IDENTIFIER_CONFLICT",
    "CLASS_UNMATCHED",
    "CLASS_MISSING_IDENTIFIERS",
    "CLASSIFICATIONS",
    "MAX_TOTAL_OCCURRENCES",
    "RECONCILIATION_NOTICE",
    "DiscoveryReconciliationError",
    "IdentifierToken",
    "Occurrence",
    "SearchRun",
    "ReconciliationGroup",
    "ReconciliationReport",
    "reconcile_searches",
]

#: Namespaced publication identifier namespaces compared here (exact value only).
NAMESPACE_PMID = "pmid"
NAMESPACE_PMCID = "pmcid"
NAMESPACE_DOI = "doi"

#: A component sharing an exact token across more than one occurrence.
CLASS_IDENTIFIER_OVERLAP = "identifier_overlap"

#: A component with >1 distinct value in some publication namespace.
CLASS_IDENTIFIER_CONFLICT = "identifier_conflict"

#: A single identified occurrence whose tokens match nothing else.
CLASS_UNMATCHED = "unmatched"

#: A single occurrence that asserts no identifier at all (explicit, not hidden).
CLASS_MISSING_IDENTIFIERS = "missing_identifiers"

#: Every classification, in a fixed order (stable counts and iteration).
CLASSIFICATIONS = (
    CLASS_IDENTIFIER_OVERLAP,
    CLASS_IDENTIFIER_CONFLICT,
    CLASS_UNMATCHED,
    CLASS_MISSING_IDENTIFIERS,
)

#: Technical upper bound on the total occurrences admitted in one call. The
#: documented per-query retrieval envelope is 20,000 PubMed records
#: (``discovery.pubmed.MAX_TOTAL_RECORDS``); this bound admits several bounded
#: runs while keeping the reconstruction finite. Crossing it is an explicit
#: refusal, never a truncation.
MAX_TOTAL_OCCURRENCES = 100_000

#: Static, value-free notice attached to every report.
RECONCILIATION_NOTICE = (
    "Non-authorizing review only: these are identifier-overlap groups, not "
    "accepted identities or deduplicated scientific candidates; publication "
    "identity is not study identity, and retrieval completeness is not "
    "literature completeness."
)


class DiscoveryReconciliationError(ReviewAgentError, ValueError):
    """A reconciliation input or overlap decision failed validation.

    Messages are static and value-free: they never echo a run id, query, URL,
    title, or identifier, so a failure is safe to log next to private discovery
    records.
    """


# --------------------------------------------------------------------------- #
# Validation helpers (static, value-free messages only)
# --------------------------------------------------------------------------- #
def _require_run_id(value: object) -> str:
    """Validate one caller-supplied run id without echoing it."""
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        raise DiscoveryReconciliationError("search run id must be a non-empty string")
    for char in value:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise DiscoveryReconciliationError(
                "search run id must not contain control characters"
            )
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise DiscoveryReconciliationError(
                "search run id must not contain an invalid Unicode surrogate"
            )
    return value


def _require_optional_token_value(value: object) -> str | None:
    """A token value is retained verbatim; only its shape is validated."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DiscoveryReconciliationError(
            "identifier token value must be a non-empty string or None"
        )
    return value


# --------------------------------------------------------------------------- #
# Token / occurrence values
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IdentifierToken:
    """One exact asserted identifier token: a namespace and its exact value.

    The namespace is ``pmid``/``pmcid``/``doi`` or ``native:<source>`` for a
    provider's own ``(source, source_id)``. Two tokens match **iff** both the
    namespace and the value are exactly equal; no normalization is applied.
    """

    namespace: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not self.namespace:
            raise DiscoveryReconciliationError(
                "identifier token namespace must be a non-empty string"
            )
        _require_optional_token_value(self.value)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.namespace}:{self.value}"


@dataclass(frozen=True)
class Occurrence:
    """One observed metadata record, uniquely pinned by ``(run_id, index)``.

    ``record`` is the exact :class:`MetadataRecord` retained for review; the
    occurrence adds no scientific interpretation of it.
    """

    run_id: str
    record_index: int
    record: MetadataRecord
    tokens: tuple[IdentifierToken, ...]

    @property
    def key(self) -> tuple[str, int]:
        """The deterministic ``(run_id, record_index)`` pin."""
        return (self.run_id, self.record_index)


@dataclass(frozen=True)
class SearchRun:
    """One immutable per-run reconciliation input: an explicit id and result.

    A ``run_id`` supplied more than once with an identical ``result`` is a replay
    and collapses; a divergent ``result`` for the same ``run_id`` is refused.
    """

    run_id: str
    result: SearchResult

    def __post_init__(self) -> None:
        _require_run_id(self.run_id)
        if not isinstance(self.result, SearchResult):
            raise DiscoveryReconciliationError(
                "search run result must be a SearchResult"
            )


@dataclass(frozen=True)
class ReconciliationGroup:
    """One token-connected component of occurrences (a *review* group only).

    ``classification`` is one of :data:`CLASSIFICATIONS`. ``shared_tokens`` are
    the exact tokens asserted by more than one occurrence in the group.
    ``conflicts`` lists the conflicting publication namespaces with their
    distinct values (present only for :data:`CLASS_IDENTIFIER_CONFLICT`).
    """

    group_id: str
    classification: str
    occurrences: tuple[Occurrence, ...]
    shared_tokens: tuple[IdentifierToken, ...]
    conflicts: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def tokens(self) -> tuple[IdentifierToken, ...]:
        """Every distinct token asserted anywhere in the group, sorted."""
        seen: set[tuple[str, str]] = set()
        for occurrence in self.occurrences:
            for token in occurrence.tokens:
                seen.add((token.namespace, token.value))
        return tuple(IdentifierToken(namespace, value) for namespace, value in sorted(seen))

    @property
    def is_conflict(self) -> bool:
        """True when the group carries an explicit identifier conflict."""
        return self.classification == CLASS_IDENTIFIER_CONFLICT

    @property
    def conflicting_namespaces(self) -> tuple[str, ...]:
        """The publication namespaces with more than one distinct value."""
        return tuple(namespace for namespace, _values in self.conflicts)


@dataclass(frozen=True)
class ReconciliationReport:
    """The immutable, non-authorizing overlap report.

    ``runs`` are the canonical inputs (unique, sorted by ``run_id``, original
    :class:`SearchResult` preserved) and ``groups`` are the deterministic review
    groups. Aggregate counts are derived, so the report can never be internally
    inconsistent.
    """

    runs: tuple[SearchRun, ...]
    groups: tuple[ReconciliationGroup, ...]

    # -- aggregate counts --------------------------------------------------- #
    @property
    def total_runs(self) -> int:
        return len(self.runs)

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def total_occurrences(self) -> int:
        return sum(len(group.occurrences) for group in self.groups)

    @property
    def sources(self) -> tuple[str, ...]:
        """Every contributing provider source, sorted and de-duplicated."""
        return tuple(sorted({run.result.source for run in self.runs}))

    def count(self, classification: str) -> int:
        """Number of groups with ``classification`` (validated)."""
        if classification not in CLASSIFICATIONS:
            raise DiscoveryReconciliationError(
                "unknown reconciliation classification"
            )
        return sum(1 for group in self.groups if group.classification == classification)

    @property
    def classification_counts(self) -> tuple[tuple[str, int], ...]:
        """``(classification, count)`` for every classification, fixed order."""
        return tuple((name, self.count(name)) for name in CLASSIFICATIONS)

    def groups_of(self, classification: str) -> tuple[ReconciliationGroup, ...]:
        """The groups with ``classification`` in deterministic order."""
        if classification not in CLASSIFICATIONS:
            raise DiscoveryReconciliationError(
                "unknown reconciliation classification"
            )
        return tuple(
            group for group in self.groups if group.classification == classification
        )

    # -- explicit non-authorization / no completeness claim ----------------- #
    @property
    def authorizing(self) -> bool:
        """Always ``False``: a report never approves or accepts anything."""
        return False

    @property
    def completeness_claimed(self) -> bool:
        """Always ``False``: retrieval completeness is not literature completeness."""
        return False

    @property
    def notice(self) -> str:
        """The static, value-free non-authorization notice."""
        return RECONCILIATION_NOTICE


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #
def _materialize_runs(runs: object) -> list[SearchRun]:
    if isinstance(runs, (str, bytes, bytearray)):
        raise DiscoveryReconciliationError(
            "search runs must be an iterable of SearchRun values"
        )
    try:
        items = list(runs)  # type: ignore[arg-type]
    except TypeError as exc:
        raise DiscoveryReconciliationError(
            "search runs must be an iterable of SearchRun values"
        ) from exc
    for item in items:
        if not isinstance(item, SearchRun):
            raise DiscoveryReconciliationError(
                "search runs must contain SearchRun values"
            )
    return items


def _collapse_runs(runs: list[SearchRun]) -> dict[str, SearchRun]:
    """Collapse identical replays; refuse a divergent duplicate ``run_id``."""
    collapsed: dict[str, SearchRun] = {}
    for run in runs:
        existing = collapsed.get(run.run_id)
        if existing is None:
            collapsed[run.run_id] = run
        elif existing.result != run.result:
            raise DiscoveryReconciliationError(
                "search runs contain a divergent duplicate run id"
            )
    return collapsed


def _record_count(run: SearchRun) -> int:
    records = run.result.records
    if type(records) is not tuple:
        raise DiscoveryReconciliationError(
            "search run result records must be an immutable tuple"
        )
    return len(records)


def _validate_run_result(run: SearchRun) -> None:
    """Refuse a forged or mutated dataclass via the existing codec roundtrip."""
    try:
        decoded = decode_search_result(encode_search_result(run.result))
    except DiscoverySerializationError as exc:
        raise DiscoveryReconciliationError(
            "search run result is not a valid immutable search result"
        ) from exc
    if decoded != run.result:
        raise DiscoveryReconciliationError(
            "search run result failed immutable round-trip validation"
        )


# --------------------------------------------------------------------------- #
# Occurrences and tokens
# --------------------------------------------------------------------------- #
def _occurrence_tokens(record: MetadataRecord) -> tuple[IdentifierToken, ...]:
    """Exact asserted identifier tokens of one record, sorted and de-duplicated.

    Comparison is exact: the provider-native token is namespaced by ``source``
    (so two providers with the same ``source_id`` never match), and the
    namespaced ``pmid``/``pmcid``/``doi`` tokens are compared verbatim.
    """
    raw: list[tuple[str, str]] = []
    if record.source_id is not None:
        raw.append((f"native:{record.source}", record.source_id))
    if record.pmid is not None:
        raw.append((NAMESPACE_PMID, record.pmid))
    if record.pmcid is not None:
        raw.append((NAMESPACE_PMCID, record.pmcid))
    if record.doi is not None:
        raw.append((NAMESPACE_DOI, record.doi))
    return tuple(IdentifierToken(namespace, value) for namespace, value in sorted(set(raw)))


def _occurrences_for_run(run: SearchRun) -> list[Occurrence]:
    return [
        Occurrence(
            run_id=run.run_id,
            record_index=index,
            record=record,
            tokens=_occurrence_tokens(record),
        )
        for index, record in enumerate(run.result.records)
    ]


# --------------------------------------------------------------------------- #
# Grouping (token index + union-find; never all-pairs)
# --------------------------------------------------------------------------- #
def _build_group(
    number: int, members: list[int], occurrences: list[Occurrence]
) -> ReconciliationGroup:
    member_occurrences = tuple(occurrences[index] for index in members)

    value_sets: dict[str, set[str]] = {}
    token_uses: dict[tuple[str, str], int] = {}
    for occurrence in member_occurrences:
        for token in occurrence.tokens:
            value_sets.setdefault(token.namespace, set()).add(token.value)
            key = (token.namespace, token.value)
            token_uses[key] = token_uses.get(key, 0) + 1

    conflicts = tuple(
        (namespace, tuple(sorted(values)))
        for namespace, values in sorted(value_sets.items())
        if len(values) > 1
    )
    shared_tokens = tuple(
        IdentifierToken(namespace, value)
        for (namespace, value), uses in sorted(token_uses.items())
        if uses > 1
    )

    if conflicts:
        classification = CLASS_IDENTIFIER_CONFLICT
    elif len(members) > 1:
        classification = CLASS_IDENTIFIER_OVERLAP
    elif member_occurrences[0].tokens:
        classification = CLASS_UNMATCHED
    else:
        classification = CLASS_MISSING_IDENTIFIERS

    return ReconciliationGroup(
        group_id=f"group-{number:04d}",
        classification=classification,
        occurrences=member_occurrences,
        shared_tokens=shared_tokens,
        conflicts=conflicts,
    )


def _build_groups(occurrences: list[Occurrence]) -> tuple[ReconciliationGroup, ...]:
    count = len(occurrences)
    parent = list(range(count))

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    token_index: dict[tuple[str, str], list[int]] = {}
    for index, occurrence in enumerate(occurrences):
        for token in occurrence.tokens:
            token_index.setdefault((token.namespace, token.value), []).append(index)

    for members in token_index.values():
        anchor = members[0]
        for other in members[1:]:
            union(anchor, other)

    components: dict[int, list[int]] = {}
    for index in range(count):
        components.setdefault(find(index), []).append(index)

    # ``occurrences`` is already in deterministic (run_id, record_index) order,
    # so ordering components by their smallest index is order-independent.
    ordered = sorted(components.values(), key=lambda members: members[0])
    return tuple(
        _build_group(number, members, occurrences)
        for number, members in enumerate(ordered, start=1)
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def reconcile_searches(runs: Iterable[SearchRun]) -> ReconciliationReport:
    """Propose deterministic identifier-overlap **review** groups.

    ``runs`` is an iterable of :class:`SearchRun`. The returned
    :class:`ReconciliationReport` is immutable and non-authorizing: it proposes
    review groups only and never accepts an identity, merges candidates, or
    claims completeness.

    Raises :class:`DiscoveryReconciliationError` (static, value-free message) for
    a non-iterable input, a non-:class:`SearchRun` element, an invalid run id, a
    divergent duplicate run id, an input past :data:`MAX_TOTAL_OCCURRENCES`, or a
    run result that is not a valid immutable value (codec round-trip guard).
    """
    items = _materialize_runs(runs)
    collapsed = _collapse_runs(items)
    ordered_runs = tuple(sorted(collapsed.values(), key=lambda run: run.run_id))

    total = 0
    for run in ordered_runs:
        total += _record_count(run)
        if total > MAX_TOTAL_OCCURRENCES:
            raise DiscoveryReconciliationError(
                "reconciliation input exceeds the supported occurrence bound"
            )

    occurrences: list[Occurrence] = []
    for run in ordered_runs:
        _validate_run_result(run)
        occurrences.extend(_occurrences_for_run(run))

    return ReconciliationReport(runs=ordered_runs, groups=_build_groups(occurrences))
