"""Step 13 acquisition port backed by the project's **already acquired** sources.

The pure Step 13 rules (:mod:`review_article_agent.tracing.schema`) call an
injected ``acquire(request)`` port for every claim-relevant upstream candidate.
This module supplies the real MVP port, bound to Step 06 acquisition and Step 07
parsing:

* it resolves a requested upstream reference (``identifiers`` = ``doi``/``pmid``)
  against the project's **already acquired** sources only -- the current
  acquisition manifest (:class:`~review_article_agent.acquisition.manifest.AcquisitionRepository`)
  and the parsed reports of those sources
  (:class:`~review_article_agent.parsing.repository.ParsingRepository`);
* ``{"status": "acquired", "source": <source dict>}`` when the reference maps to
  an acquired candidate whose source document has a valid parsed report; the
  ``source`` dict is the exact shape :mod:`review_article_agent.tracing.schema`
  reads (``source_key``, ``source.sha256``, ``passages``, ``references``,
  ``citation_links``);
* ``{"status": "pending_disposition"}`` when the reference is known to the
  project (a manifest entry exists) but is not acquired and parsed yet -- the
  user must acquire/import it through the existing Step 06 gate;
* ``{"status": "unavailable"}`` when the reference is unknown to the project.

Invariants (``docs/implementation/13-origin-tracing.md``): **no automatic
unofficial acquisition** and no network.  The port only *reads* durable state;
it never fetches, never admits a source to the corpus, and never mints
authority.  ``primary_verified`` is never emitted here.  Documents are untrusted
input; only reports that pass the parsing repository's full revalidation are
used.
"""

from __future__ import annotations

from ..acquisition.manifest import AcquisitionRepository, ManifestError
from ..parsing.repository import ParsingRepository, ParsingRepositoryError
from ..storage.artifacts import ArtifactRepository

#: Identifier namespaces a requested upstream reference is matched on.
REFERENCE_NAMESPACES = ("doi", "pmid")

#: The three port responses (see :func:`review_article_agent.tracing.schema.trace_atom`).
ACQUIRED = "acquired"
PENDING_DISPOSITION = "pending_disposition"
UNAVAILABLE = "unavailable"

__all__ = [
    "REFERENCE_NAMESPACES",
    "ACQUIRED",
    "PENDING_DISPOSITION",
    "UNAVAILABLE",
    "make_acquire_port",
]


def _request_identifiers(request) -> dict:
    """The ``doi``/``pmid`` identifiers carried by one acquire request."""
    identifiers = request.get("identifiers") if isinstance(request, dict) else None
    if not isinstance(identifiers, dict):
        return {}
    return {
        name: identifiers[name]
        for name in REFERENCE_NAMESPACES
        if isinstance(identifiers.get(name), str) and identifiers[name].strip()
    }


def _entry_identifiers(entry) -> dict:
    """The verified ``doi``/``pmid`` identifiers of one manifest entry."""
    expected = entry.get("expected_identifiers") if isinstance(entry, dict) else None
    if not isinstance(expected, dict):
        return {}
    return {
        name: expected[name]
        for name in REFERENCE_NAMESPACES
        if isinstance(expected.get(name), str) and expected[name].strip()
    }


def _same_reference(left: dict, right: dict) -> bool:
    """True when the two identifier maps name the same reference.

    A DOI is compared case-insensitively (DOIs are case-insensitive); a PMID is
    compared exactly.  A namespace present on only one side cannot match.
    """
    for name in REFERENCE_NAMESPACES:
        a, b = left.get(name), right.get(name)
        if a is None or b is None:
            continue
        if name == "doi":
            if a.lower() == b.lower():
                return True
        elif a == b:
            return True
    return False


def _tracing_source(source_key: str, payload: dict) -> dict:
    """Project a validated parsed-report payload into a tracing source dict.

    Only the fields :mod:`review_article_agent.tracing.schema` reads are kept:
    passage id/text, reference id/text (with empty identifiers -- the parser does
    not extract reference identifiers, so a further hop stops at
    ``missing_identifier``), and the tagged citation links.  The report's source
    digest is the acquired asset's sha256.
    """
    passages = [
        {"passage_id": p.get("passage_id"), "text": p.get("text")}
        for p in payload.get("passages") or []
        if isinstance(p, dict)
    ]
    references = [
        {"ref_id": r.get("ref_id"), "text": r.get("text"), "identifiers": {}}
        for r in payload.get("references") or []
        if isinstance(r, dict)
    ]
    citation_links = [
        {
            "passage_id": link.get("passage_id"),
            "marker": link.get("marker"),
            "state": link.get("state"),
            "resolved_ref_ids": list(link.get("resolved_ref_ids") or []),
        }
        for link in payload.get("citation_links") or []
        if isinstance(link, dict)
    ]
    source = payload.get("source")
    sha = source.get("sha256") if isinstance(source, dict) else None
    return {
        "source_key": source_key,
        "source": {"sha256": sha},
        "passages": passages,
        "references": references,
        "citation_links": citation_links,
    }


def _known_entries(acquisition: AcquisitionRepository) -> list:
    """Every entry of the current acquisition manifest (any approval state)."""
    try:
        document = acquisition.current()
    except ManifestError:
        return []
    if not isinstance(document, dict):
        return []
    entries = (document.get("payload") or {}).get("entries")
    return entries if isinstance(entries, list) else []


def _acquired_candidate_ids(acquisition: AcquisitionRepository) -> set:
    """The candidate ids of the currently-complete acquisition manifest."""
    try:
        document = acquisition.require_acquired()
    except ManifestError:
        return set()
    if not isinstance(document, dict):
        return set()
    return set(document.get("acquired_candidate_ids") or [])


def _find_parsed_report(store, source_ref: dict):
    """The validated parsed report derived from ``source_ref``, or ``None``.

    Candidate reports are discovered through the exact ``derived_from`` document
    dependency that :meth:`ParsingRepository.parse_stored_document` records; no
    artifact naming convention is assumed and nothing is rebound to a newer
    revision.  A candidate only counts once
    :meth:`ParsingRepository.read_report` fully revalidates it -- other report
    artifacts that merely depend on the same source document (for example the
    acquisition manifest) are rejected by that revalidation.
    """
    artifacts = ArtifactRepository(store)
    parsing = ParsingRepository(store)
    target_artifact = source_ref.get("artifact_id")
    target_revision = source_ref.get("revision_id")
    if not isinstance(target_artifact, str) or not isinstance(target_revision, str):
        return None
    for artifact in artifacts.list_artifacts():
        if artifact.kind != "report":
            continue
        for revision in artifacts.list_revisions(artifact.artifact_id):
            if not any(
                dep.role == "derived_from"
                and dep.artifact_id == target_artifact
                and dep.revision_id == target_revision
                for dep in artifacts.list_dependencies(revision.artifact_id, revision.revision_id)
            ):
                continue
            try:
                return parsing.read_report(
                    {
                        "project_id": store.project_id,
                        "artifact_id": revision.artifact_id,
                        "revision_id": revision.revision_id,
                    }
                )
            except ParsingRepositoryError:
                continue
    return None


def make_acquire_port(store):
    """Return an ``acquire(request)`` port over the project's acquired sources.

    ``store`` is an already-verified :class:`ProjectStore`.  The returned
    callable is read-only and makes no network call; it resolves each request
    against the current acquisition manifest and the parsed reports of its
    acquired source documents.
    """
    acquisition = AcquisitionRepository(store)

    def acquire(request):
        requested = _request_identifiers(request)
        if not requested:
            return {"status": UNAVAILABLE}

        match = None
        for entry in _known_entries(acquisition):
            if _same_reference(_entry_identifiers(entry), requested):
                match = entry
                break
        if match is None:
            return {"status": UNAVAILABLE}

        candidate_id = match.get("candidate_id")
        source_ref = match.get("source_ref")
        if candidate_id not in _acquired_candidate_ids(acquisition):
            return {"status": PENDING_DISPOSITION}
        if not isinstance(source_ref, dict):
            return {"status": PENDING_DISPOSITION}

        report = _find_parsed_report(store, source_ref)
        if report is None:
            return {"status": PENDING_DISPOSITION}

        source = _tracing_source(source_ref["artifact_id"], report["payload"])
        return {"status": ACQUIRED, "source": source}

    return acquire
