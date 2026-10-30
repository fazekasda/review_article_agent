"""Build Step 13 tracing inputs from real draft / package / review state (MVP).

The Step 13 workflow consumes a plain ``claims`` list and a ``sources`` map (see
:mod:`review_article_agent.tracing.schema`).  This module derives those from the
*real* Step 11 draft and its approved drafting package instead of a synthetic
fixture:

* **one claim per drafted sentence that cites evidence.**  A sentence carries
  ``paragraph_id``/``intent_id`` (so the Step 12 section check matches it) and
  the package's verified-corpus ``citation_keys``; the claim's ``root_source_key``
  is the sentence's cited source and its atoms are the sentence's cited evidence;
* **one source per cited citation key.**  Each source dict is the exact shape
  :mod:`review_article_agent.tracing.schema` reads (``source_key``,
  ``source.sha256``, ``passages``, ``references``, ``citation_links``); its
  passages are the package's verified original passages for that citation and its
  ``sha256`` is the acquired source asset's digest;
* **``source_digests``** maps each cited source key to that acquired asset digest
  (used by the workflow for dependency-valid cache invalidation).

Read-only: it reads the store's immutable revisions to recover the acquired
asset digest and writes nothing.  It mints no authority and makes no model or
network call.  The MVP traces each drafted sentence against its first (sorted)
cited source; a sentence citing several sources is a disclosed limitation.
"""

from __future__ import annotations

from ..storage.artifacts import ArtifactRepository

__all__ = ["claims_and_sources"]


def _passages_by_citation(package: dict) -> dict:
    """Map ``citation_key -> {passage_id: text}`` from the package's evidence."""
    passages: dict = {}
    for item in package.get("evidence") or []:
        original = item.get("original_passage") or {}
        passage_id = original.get("passage_id")
        if passage_id is None:
            continue
        for key in item.get("citation_keys") or []:
            passages.setdefault(key, {}).setdefault(passage_id, original.get("text"))
    return passages


def _acquired_digest(artifacts: ArtifactRepository, record: dict):
    """The acquired asset ``content_sha256`` for one citation record, or ``None``.

    The citation record's verified ``source_refs`` name the exact acquired source
    document revision; its content digest is the acquired asset sha256.
    """
    for ref in record.get("source_refs") or []:
        if not isinstance(ref, dict):
            continue
        artifact_id = ref.get("artifact_id")
        revision_id = ref.get("revision_id")
        if not isinstance(artifact_id, str) or not isinstance(revision_id, str):
            continue
        revision = artifacts.get_revision(artifact_id, revision_id)
        if revision is not None and revision.content_sha256:
            return revision.content_sha256
    return None


def _atoms_for(paragraph_id, sentence: dict, root_key: str, package: dict) -> list:
    """One atom per cited evidence item belonging to the claim's root source."""
    atoms: list = []
    for item in package.get("evidence") or []:
        if root_key not in (item.get("citation_keys") or []):
            continue
        original = item.get("original_passage") or {}
        passage_id = original.get("passage_id")
        atoms.append(
            {
                "atom_id": f"{paragraph_id}:{sentence.get('intent_id')}:{item.get('finding_key')}",
                "text": sentence.get("text"),
                "passage_ids": [passage_id] if passage_id is not None else [],
            }
        )
    return atoms


def claims_and_sources(store, *, draft, package, review_state=None):
    """Derive ``(claims, sources, source_digests)`` from real draft/package state.

    ``draft`` is a validated Step 11 section-draft result, ``package`` its
    approved drafting package, and ``review_state`` the Step 12 review state
    (accepted for call-site symmetry; the workflow resolves the depth-0 verdict
    itself from ``review_checks``).  The returned ``claims``/``sources`` are the
    exact inputs :meth:`OriginTraceRepository.run` expects; ``source_digests``
    covers every cited source key.
    """
    artifacts = ArtifactRepository(store)
    citations = package.get("citations") or {}
    passages_by_key = _passages_by_citation(package)

    sources: dict = {}
    source_digests: dict = {}
    for key, record in citations.items():
        record = record if isinstance(record, dict) else {}
        digest = _acquired_digest(artifacts, record)
        sources[key] = {
            "source_key": key,
            "source": {"sha256": digest},
            "passages": [
                {"passage_id": passage_id, "text": text}
                for passage_id, text in sorted(passages_by_key.get(key, {}).items())
            ],
            "references": [],
            "citation_links": [],
        }
        source_digests[key] = digest

    claims: list = []
    for paragraph in draft.get("paragraphs") or []:
        paragraph_id = paragraph.get("paragraph_id")
        for sentence in paragraph.get("sentences") or []:
            cited = sorted(
                {key for key in (sentence.get("citation_keys") or []) if key in sources}
            )
            if not cited:
                continue
            root_key = cited[0]
            atoms = _atoms_for(paragraph_id, sentence, root_key, package)
            if not atoms:
                continue
            claims.append(
                {
                    "claim_id": f"claim:{paragraph_id}:{sentence.get('intent_id')}",
                    "paragraph_id": paragraph_id,
                    "intent_id": sentence.get("intent_id"),
                    "root_source_key": root_key,
                    "atoms": atoms,
                }
            )
    return claims, sources, source_digests
