"""Minimal reconstructible approved evidence/citation drafting package (Step 11, S11-PACKAGE).

One bounded outcome: assemble a deterministic, JSON-portable **drafting package**
for one approved outline section from the exact *current* approved global chain
(accepted synthesis -> framework -> outline), the accepted extraction batch cases,
and **verified frozen corpus metadata**.  The package reconstructs entirely from
durable store artifacts -- no raw session history, no live execution, no model
call, and no storage migration.

Contract (``build(outline_ref, section_id)``)::

    {
      "section": <the exact outline section dict>,
      "framework": <the approved framework result>,
      "synthesis": <the approved synthesis result>,
      "evidence": [
        {"finding_key": "<case_id>:<finding_id>",
         "finding": <full accepted finding record>,
         "summary": <full accepted readable case summary>,
         "extraction_ref": <exact extraction ref>,
         "source_refs": [<the finding's own exact source document ref>],
         "citation_keys": [<stable citation key strings>],
         "original_passage": {<verified receipt passage: text, locator,
                               source_ref, report_ref, section_path, assets>}}, ...
      ],
      "citations": {<stable key>: <verified corpus bibliographic record:
                                   candidate_id, version, identity {doi/pmid/pmcid},
                                   and the observed title/authors/year/journal
                                   where present>},
      "methods": {..., "original_sources": [<verified passages/assets per used source>]},
      ... exact global refs, methods/context, and inert authority flags ...
    }

Every retained accepted finding is included (MVP: no costly semantic
relevance inference).  Each finding is bound to its **own** verified receipt
passage (``passage_id`` + ``source_key``) and to that passage's exact
``source_ref``; a case with several sources yields each finding's own citation,
never a blanket of every case source.  Original passages/locators, context,
numerical details, attribution, and limitations stay on the finding; the readable
extraction summary and the source's methods/context (``methods.original_sources``)
remain available for interpretation.

A citation is **only** a verified corpus record: the finding's exact source
document is traced through the current approved acquisition manifest entries to
the frozen candidate metadata of the accepted freeze.  Each citation record
carries the verified frozen-corpus metadata available for that candidate -- its
identity identifiers, its frozen-corpus revision ``version``, and the observed
``title``/``authors``/``year``/``journal`` where the corpus recorded them -- so
the delivery export emits a real bibliography.  A missing, foreign, unresolved,
or ambiguous linkage is
rejected rather than invented; no model/caller bibliography is accepted, and a
field the corpus does not carry is omitted, never invented.  The stable citation
key derives deterministically from the project + candidate identity (never a
title/author guess).  A legacy synthetic receipt whose passages lack a
per-passage source ref is bound to the case's unique single source only, and the
fallback is disclosed in ``limitations`` and ``source_ref_fallback``.

Authority is only the existing named owner gates consumed by
:meth:`~review_article_agent.synthesis.workflow.SynthesisWorkflowRepository.require_for_blueprints`
and
:meth:`~review_article_agent.acquisition.manifest.AcquisitionRepository.require_acquired`;
this module mints no authority.  Not in scope: workflow/gates, role sessions, CLI,
schema validator, or any schema change.
"""

from __future__ import annotations

import hashlib

from ..acquisition.manifest import AcquisitionRepository, ManifestError
from ..discovery.candidates import CandidateError, CandidateRepository
from ..domain.errors import ReviewAgentError
from ..domain.records import RecordValidationError, canonical_json, validate_ref
from ..extraction.workflow import ExtractionWorkflowError, ExtractionWorkflowRepository
from ..storage import opmemo
from ..storage.discovery import DiscoveryRepository
from ..synthesis.workflow import SynthesisWorkflowError, SynthesisWorkflowRepository

#: Package envelope schema version.
#:
#: Version 2 carries the verified frozen-corpus bibliographic metadata (``title``/
#: ``year`` from the recorded observations, plus ``identity`` and ``version``) on
#: each citation record so export emits a real bibliography. The optional
#: ``authors``/``journal`` observations are carried the same additive way when
#: the recorded provider metadata has them (absent fields are simply omitted).
SCHEMA_VERSION = 2

#: Generic-document ``document_type`` of one drafting package.
DOCUMENT_TYPE = "drafting_package"

#: Dispositions that *retain* an accepted finding for the package.  ``not_used``
#: and ``out_of_scope`` findings are deliberately excluded (they were accepted as
#: evidence but not carried into the synthesis argument).
RETAINED_DISPOSITIONS = (
    "incorporated",
    "genuine_disagreement",
    "species_or_outcome_difference",
    "null_evidence",
    "insufficient_data",
)

#: The bibliographic identifier namespaces carried by a frozen candidate.
_IDENTIFIER_NAMESPACES = ("pmid", "pmcid", "doi")

NOTICE = (
    "Approved, reconstructible drafting package over the exact current accepted "
    "synthesis/framework/outline, accepted extraction batch cases, and verified "
    "frozen corpus metadata. Citations are traced source document -> approved "
    "acquisition manifest entry -> frozen candidate metadata; a missing, "
    "unresolved, or ambiguous linkage is refused, never invented. The stable "
    "citation key derives from the project + candidate identity, not a model "
    "title/author guess. Workflow approval is not scientific validation and this "
    "package is provisional: source checks and origin tracing are not complete."
)


class DraftingPackageError(ReviewAgentError):
    """A drafting-package input, dependency, or metadata linkage is invalid."""


def citation_key(project_id, candidate_id):
    """Return the deterministic, stable citation key for one frozen candidate.

    The key derives from the *project + candidate identity* only -- never a
    title, author, journal, year, or other model/ caller-supplied guess -- so it
    is stable across revisions, sessions, and re-openings, and distinct across
    projects.
    """
    material = canonical_json(["drafting.citation", project_id, candidate_id])
    return "cit_" + hashlib.sha256(material).hexdigest()[:32]


def _memo_key(*parts):
    """Return a stable tuple key, or ``None`` when an argument is not keyable.

    See :func:`review_article_agent.synthesis.workflow._memo_key`; an unkeyable
    argument makes the caller run the original body uncached, so fail-closed
    behaviour is unchanged.
    """
    try:
        return tuple(
            part if isinstance(part, str) else canonical_json(part).decode()
            for part in parts
        )
    except (RecordValidationError, TypeError, ValueError):
        return None


def _memoize(conn, key, compute):
    """Run ``compute`` under the operation-scoped memo, or directly if unkeyable."""
    if key is None:
        return compute()
    return opmemo.memoize(conn, key, compute)


def _ambiguity(tokens):
    """Return ``True`` when a candidate carries two values for one namespace.

    A candidate whose identifiers are internally ambiguous can never be a
    verified bibliographic record; the linkage fails closed.
    """
    seen = {}
    for token in tokens:
        if not isinstance(token, list) or len(token) != 2:
            return True
        namespace, value = token[0], token[1]
        if not isinstance(namespace, str) or not isinstance(value, str):
            return True
        if namespace in seen and seen[namespace] != value:
            return True
        seen[namespace] = value
    return False


def _observed_bibliographic_fields(candidate, discovery):
    """Return the verified bibliographic fields recorded for one frozen candidate.

    The title, publication year, authors, and journal are read from the
    candidate's *own* recorded provider observations (the frozen corpus
    re-derives the candidate from exactly these observations).  A field is
    returned only when every occurrence that carries it agrees on a single
    value; a field that is absent or whose occurrences disagree is omitted -- it
    is never invented or guessed.  Authors are emitted as a JSON-portable list
    (the provider's own order), never re-ordered or synthesised.
    """
    titles: set = set()
    years: set = set()
    authors: set = set()
    journals: set = set()
    for pin in candidate.get("occurrences") or []:
        if not isinstance(pin, list) or len(pin) != 2:
            continue
        run_id, index = pin[0], pin[1]
        if not isinstance(run_id, str) or isinstance(index, bool) or not isinstance(index, int):
            continue
        record = discovery.get_search(run_id)
        if record is None:
            continue
        records = record.result.records
        if index < 0 or index >= len(records):
            continue
        observed = records[index]
        title = observed.title
        if isinstance(title, str) and title.strip():
            titles.add(title)
        year = observed.publication_year
        if isinstance(year, int) and not isinstance(year, bool):
            years.add(year)
        observed_authors = observed.authors
        if isinstance(observed_authors, tuple) and observed_authors:
            authors.add(observed_authors)
        journal = observed.journal
        if isinstance(journal, str) and journal.strip():
            journals.add(journal)
    fields: dict = {}
    if len(titles) == 1:
        fields["title"] = next(iter(titles))
    if len(years) == 1:
        fields["year"] = next(iter(years))
    if len(authors) == 1:
        fields["authors"] = list(next(iter(authors)))
    if len(journals) == 1:
        fields["journal"] = next(iter(journals))
    return fields


def _bibliographic_record(
    project_id, candidate, corpus_ref, freeze_ref, source_refs, observed=None
):
    """Build one verified bibliographic record from a frozen candidate doc.

    The record carries the verified corpus metadata available for the candidate:
    the identity identifiers from its exact tokens, its frozen-corpus revision
    ``version``, and any observed ``title``/``authors``/``year``/``journal``
    (``observed``).  A field that the frozen corpus does not carry is omitted,
    never invented.  The stable citation key derivation is unchanged.
    """
    tokens = candidate.get("tokens")
    if not isinstance(tokens, list):
        raise DraftingPackageError("a frozen candidate has no readable identifier tokens")
    identifiers = {}
    providers = {}
    for token in tokens:
        namespace, value = token[0], token[1]
        if namespace in _IDENTIFIER_NAMESPACES:
            identifiers.setdefault(namespace, value)
        else:
            providers.setdefault(namespace, value)
    if not identifiers and not providers:
        # A resolved candidate always carries at least its provider-native token;
        # an empty token set means the metadata cannot be verified.
        raise DraftingPackageError("a frozen candidate carries no bibliographic metadata")
    identity = {
        namespace: identifiers[namespace]
        for namespace in sorted(identifiers)
    }
    record = {
        "citation_key": citation_key(project_id, candidate["candidate_id"]),
        "candidate_id": candidate["candidate_id"],
        "project_id": project_id,
        "resolved": bool(candidate.get("resolved")),
        # The exact frozen-corpus revision the metadata was verified against; it
        # is the bibliography version the export consistency check compares.
        "version": corpus_ref.get("revision_id"),
        "identity": identity,
        "bibliographic_identifiers": dict(sorted(identifiers.items())),
        "provider_identifiers": dict(sorted(providers.items())),
        "tokens": [list(token) for token in tokens],
        "source_refs": [dict(ref) for ref in source_refs],
        "corpus_ref": dict(corpus_ref),
        "freeze_ref": dict(freeze_ref),
    }
    for field, value in (observed or {}).items():
        record[field] = value
    return record


class DraftingPackageRepository:
    """Assemble one approved, reconstructible drafting package per section.

    The repository is read-only: it consumes the current accepted
    synthesis/framework/outline via the Step 10 ``require_for_blueprints``
    authority, the accepted extraction batch via ``require_for_synthesis`` /
    ``require_batch``, and the currently-complete acquisition manifest via
    ``require_acquired``, then resolves verified frozen corpus metadata.  It
    writes nothing and mints no authority.
    """

    def __init__(self, store):
        self.store = store

    @property
    def project_id(self):
        return self.store.project_id

    # -- metadata linkage ---------------------------------------------------- #
    def _source_index(self, entries):
        """Map every canonical acquired source ref to its frozen candidate id.

        A source document that maps to two different frozen candidates is an
        ambiguous linkage and is refused rather than silently resolved to the
        first-seen candidate.
        """
        index = {}
        for entry in entries:
            candidate_id = entry.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise DraftingPackageError("a manifest entry has no candidate id")
            refs = [entry.get("source_ref")]
            for asset in entry.get("required_assets") or []:
                refs.append(asset.get("source_ref"))
            for ref in refs:
                if ref is None:
                    continue
                key = canonical_json(validate_ref(ref, self.project_id))
                existing = index.get(key)
                if existing is not None and existing != candidate_id:
                    raise DraftingPackageError(
                        "a source document maps to two different frozen candidates"
                    )
                index[key] = candidate_id
        return index

    def _receipt_passages(self, extraction, case):
        """Return the **verified** receipt passages for one accepted case.

        The passages come through the existing verified extraction seam
        (``ExtractionWorkflowRepository._receipt`` -> the runner's
        ``require_run``), never a generic unchecked JSON read.
        """
        receipt, _config_ref = extraction._receipt(case["extraction_ref"], "extraction")
        passages = receipt["passages"]
        if not isinstance(passages, list) or not passages:
            raise DraftingPackageError("a case has no verified original passages")
        return passages

    def _finding_passage(self, finding, passages):
        """Bind one finding to its exact verified passage by id + source key."""
        passage_id = finding.get("passage_id")
        source_key = finding.get("source_key")
        matches = [
            passage
            for passage in passages
            if passage.get("passage_id") == passage_id
            and passage.get("source_key") == source_key
        ]
        if len(matches) != 1:
            raise DraftingPackageError(
                "a finding does not bind to exactly one verified original passage"
            )
        return matches[0]

    def _passage_source_ref(self, passage, case_source_refs):
        """Return ``(source_ref, used_legacy_fallback)`` for one verified passage.

        A verified receipt passage carries its own exact ``source_ref``.  A legacy
        synthetic fixture whose receipt passages lack ``source_ref`` may only fall
        back to the case's *unique* single source; a multi-source case without a
        per-passage source ref is refused rather than guessed.
        """
        ref = passage.get("source_ref")
        if ref is not None:
            return validate_ref(ref, self.project_id), False
        unique = {}
        for candidate_ref in case_source_refs or []:
            unique[canonical_json(validate_ref(candidate_ref, self.project_id))] = (
                validate_ref(candidate_ref, self.project_id)
            )
        if len(unique) != 1:
            raise DraftingPackageError(
                "a verified passage has no source ref and its case is not single-source"
            )
        return next(iter(unique.values())), True

    def _candidate_for_source(self, source_ref, source_index, candidates_by_id):
        """Resolve one exact source ref to a verified, resolved frozen candidate."""
        key = canonical_json(source_ref)
        candidate_id = source_index.get(key)
        if candidate_id is None:
            raise DraftingPackageError(
                "a finding source is not a source of any approved acquisition entry"
            )
        candidate = candidates_by_id.get(candidate_id)
        if candidate is None:
            raise DraftingPackageError(
                "a manifest candidate is missing from the frozen corpus"
            )
        if not candidate.get("resolved"):
            raise DraftingPackageError(
                "a finding source resolves to an unresolved frozen candidate"
            )
        if _ambiguity(candidate.get("tokens")):
            raise DraftingPackageError(
                "a finding source candidate has ambiguous bibliographic metadata"
            )
        return candidate_id, candidate

    @staticmethod
    def _original_passage(passage, source_ref, fallback):
        """Project one verified receipt passage into the package's exact shape."""
        return {
            "passage_id": passage.get("passage_id"),
            "source_key": passage.get("source_key"),
            "text": passage.get("text"),
            "raw_text": passage.get("raw_text"),
            "section_path": list(passage.get("section_path") or []),
            "asset_ids": list(passage.get("asset_ids") or []),
            "asset_kind": passage.get("asset_kind"),
            "report_ref": passage.get("report_ref"),
            "source_ref": dict(source_ref),
            "locator": passage.get("locator"),
            "source_ref_fallback": bool(fallback),
        }

    @staticmethod
    def _original_source_record(source, passages):
        """Build one used-source context record (passages, sections, assets)."""
        assets = [p for p in passages if p.get("asset_ids")]
        methods_ids = [
            p["passage_id"]
            for p in passages
            if any("method" in str(part).lower() for part in p.get("section_path") or [])
        ]
        return {
            "source_ref": dict(source["ref"]),
            "candidate_id": source["candidate_id"],
            "citation_key": source["citation_key"],
            "passages": passages,
            "assets": [
                {
                    "passage_id": p["passage_id"],
                    "asset_ids": p["asset_ids"],
                    "asset_kind": p.get("asset_kind"),
                    "text": p.get("text"),
                    "locator": p.get("locator"),
                }
                for p in assets
            ],
            "section_paths": [
                list(path)
                for path in sorted({tuple(p.get("section_path") or []) for p in passages})
            ],
            "methods_section_passage_ids": sorted(set(methods_ids)),
            "methods_context_available": bool(methods_ids),
            "source_ref_fallback": bool(source["fallback"]),
        }

    # -- public API ---------------------------------------------------------- #
    def build(self, outline_ref, section_id):
        """Return the approved drafting package for one outline section.

        Fails closed (static :class:`DraftingPackageError`) when the exact current
        approved chain is not consumable, when the section is not part of the
        approved outline, or when any accepted case source cannot be traced to
        verified frozen corpus metadata.  Every upstream workflow/metadata error
        is normalized to :class:`DraftingPackageError`.
        """
        try:
            return self._build(outline_ref, section_id)
        except DraftingPackageError:
            raise
        except (
            SynthesisWorkflowError,
            ExtractionWorkflowError,
            ManifestError,
            CandidateError,
            RecordValidationError,
        ) as exc:
            raise DraftingPackageError(
                "the approved drafting chain or frozen corpus metadata is not consumable"
            ) from exc

    def _build(self, outline_ref, section_id):
        with self.store.transaction():
            key = _memo_key(
                "drafting.package._build",
                self.project_id,
                validate_ref(outline_ref, self.project_id),
                section_id,
            )
            return _memoize(
                self.store.connection,
                key,
                lambda: self._build_uncached(outline_ref, section_id),
            )

    def _build_uncached(self, outline_ref, section_id):
        context = SynthesisWorkflowRepository(self.store).require_for_blueprints(
            outline_ref
        )

        outline = context["outline"]
        section = next(
            (s for s in outline["sections"] if s["section_id"] == section_id), None
        )
        if section is None:
            raise DraftingPackageError(
                "the requested section is not part of the approved outline"
            )

        synthesis = context["synthesis"]
        framework = context["framework"]
        audit_ref = context["audit_ref"]

        extraction = ExtractionWorkflowRepository(self.store)
        discovery = DiscoveryRepository(self.store)
        try:
            accepted = extraction.require_for_synthesis(audit_ref)
            batch = extraction.require_batch(accepted["batch_ref"])
        except ExtractionWorkflowError as exc:
            raise DraftingPackageError(
                "the accepted extraction evidence is not consumable"
            ) from exc
        acquisition_ref = accepted["acquisition_ref"]

        try:
            manifest = AcquisitionRepository(self.store).require_acquired(
                at_ref=acquisition_ref
            )
            freeze = manifest["payload"]["freeze"]
            freeze_ref = validate_ref(freeze["ref"], self.project_id)
            corpus_ref = validate_ref(freeze["corpus_ref"], self.project_id)
            snapshot = CandidateRepository(self.store).get(corpus_ref)
        except (ManifestError, CandidateError, RecordValidationError) as exc:
            raise DraftingPackageError(
                "the approved acquisition manifest or frozen corpus metadata is not intact"
            ) from exc
        candidates_by_id = {
            doc["candidate_id"]: doc for doc in snapshot["candidates"]
        }
        source_index = self._source_index(manifest["payload"]["entries"])

        cases_by_id = {case["id"]: case for case in batch["cases"]}
        dispositions = {
            entry["finding"]: entry["disposition"]
            for entry in synthesis["dispositions"]
        }

        # Verified receipt passages per accepted case (existing verified seam).
        passages_by_case = {
            case["id"]: self._receipt_passages(extraction, case)
            for case in batch["cases"]
        }

        citations = {}
        evidence = []
        limitations = []
        #: Used source ref key -> {ref, candidate_id, citation_key, fallback}
        used_sources = {}
        used_cases = set()
        for record in synthesis["findings"]:
            finding_key = record["key"]
            if dispositions.get(finding_key) not in RETAINED_DISPOSITIONS:
                continue
            case_id = record["case_id"]
            case = cases_by_id.get(case_id)
            if case is None:
                raise DraftingPackageError(
                    "a synthesized finding names a case absent from the accepted batch"
                )
            finding = record["record"]
            # Bind the finding to its exact verified passage, then to the
            # passage's own source ref -- never blanket every case source.
            passage = self._finding_passage(finding, passages_by_case[case_id])
            source_ref, fallback = self._passage_source_ref(passage, case["source_refs"])
            candidate_id, candidate = self._candidate_for_source(
                source_ref, source_index, candidates_by_id
            )
            key = citation_key(self.project_id, candidate_id)
            if key not in citations:
                observed = _observed_bibliographic_fields(candidate, discovery)
                citations[key] = _bibliographic_record(
                    self.project_id,
                    candidate,
                    corpus_ref,
                    freeze_ref,
                    [source_ref],
                    observed,
                )
            if fallback:
                limitations.append(
                    "legacy receipt passages without a per-passage source ref were "
                    "bound to the case's unique single source"
                )
            evidence.append(
                {
                    "finding_key": finding_key,
                    "finding": finding,
                    "summary": case["extraction"]["summary"],
                    "extraction_ref": dict(record["extraction_ref"]),
                    "source_refs": [dict(source_ref)],
                    "citation_keys": [key],
                    "original_passage": self._original_passage(
                        passage, source_ref, fallback
                    ),
                }
            )
            used_cases.add(case_id)
            source_key = canonical_json(source_ref)
            used_sources.setdefault(
                source_key,
                {
                    "ref": source_ref,
                    "candidate_id": candidate_id,
                    "citation_key": key,
                    "fallback": False,
                },
            )
            used_sources[source_key]["fallback"] = (
                used_sources[source_key]["fallback"] or fallback
            )
        evidence.sort(key=lambda item: item["finding_key"])

        # Original context: every verified receipt passage of every *used*
        # source (MVP -- no semantic relevance inference), plus assets.
        passages_by_used_source = {key: [] for key in used_sources}
        for case_id in sorted(used_cases):
            case = cases_by_id[case_id]
            for passage in passages_by_case[case_id]:
                source_ref, fallback = self._passage_source_ref(
                    passage, case["source_refs"]
                )
                source_key = canonical_json(source_ref)
                if source_key not in passages_by_used_source:
                    continue
                passages_by_used_source[source_key].append(
                    self._original_passage(passage, source_ref, fallback)
                )
        original_sources = [
            self._original_source_record(
                used_sources[key], passages_by_used_source[key]
            )
            for key in sorted(used_sources)
        ]

        return {
            "schema_version": SCHEMA_VERSION,
            "document_type": DOCUMENT_TYPE,
            "outline_ref": dict(context["outline_ref"]),
            "framework_ref": dict(context["framework_ref"]),
            "synthesis_ref": dict(context["synthesis_ref"]),
            "audit_ref": dict(audit_ref),
            "acquisition_ref": dict(acquisition_ref),
            "freeze_ref": dict(freeze_ref),
            "corpus_ref": dict(corpus_ref),
            "section_id": section["section_id"],
            "section": dict(section),
            "framework": framework,
            "synthesis": synthesis,
            "evidence": evidence,
            "citations": {key: citations[key] for key in sorted(citations)},
            "methods": {
                "framework_criteria": framework["criteria"],
                "framework_contribution": framework["contribution"],
                "framework_limitations": framework.get("limitations"),
                "synthesis_limitations": synthesis.get("limitations"),
                "synthesis_independence": synthesis["independence"],
                "correlation_groups": synthesis["correlation_groups"],
                # Verified original-paper context for every used source:
                # passages (text/locator/section), assets, and methods-section
                # availability (MVP -- all verified receipt passages).
                "original_sources": original_sources,
            },
            "limitations": sorted(set(limitations)),
            "approved": True,
            "authorizes_execution": False,
            "authorizes_export": False,
            "scientific_validation": False,
            "authority": "named_gate_only",
            "notice": NOTICE,
        }


#: Convenience alias used by callers that prefer the shorter name.
DraftingRepository = DraftingPackageRepository

__all__ = [
    "DOCUMENT_TYPE",
    "NOTICE",
    "RETAINED_DISPOSITIONS",
    "SCHEMA_VERSION",
    "DraftingPackageError",
    "DraftingPackageRepository",
    "DraftingRepository",
    "citation_key",
]
