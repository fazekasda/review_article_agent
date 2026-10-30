#!/usr/bin/env python3
"""Synthetic 1,000- and 10,000-candidate operational scale tests (Step 15).

Authority
---------

* ``docs/implementation/15-release.md`` deliverable 2 ("Synthetic 1,000- and
  10,000-candidate operational tests without implying 10,000 paid model calls or
  copyrighted downloads") and acceptance criterion 5 ("Scale tests report
  measured elapsed time, memory/storage, queue behavior and failures against
  budgets agreed in 01; no invented universal latency or cost promises").
* ``docs/specification.md`` line 17 ("Target approximately 1,000-10,000
  candidates per project. Do not create an autonomous session for every
  deterministic paper operation.").

Budget note (honesty)
---------------------

Step 01 (``docs/implementation/01-contracts.md``) did **not** agree numeric
scale budgets. There is therefore **no numeric budget agreed in 01** against
which the measured numbers below can be scored. Every number this script prints
is a raw measurement taken on the machine that ran it; none of it is a universal
latency, throughput, memory, or cost promise. Model calls and network calls are
**zero** by construction: the harness only feeds deterministic synthetic metadata
through local, already-existing code paths.

What is measured (real, existing, deterministic paths only)
-----------------------------------------------------------

1. ``discovery_ledger`` — synthetic candidate metadata records are written
   through the real discovery observation store
   (:class:`~review_article_agent.storage.discovery.DiscoveryRepository`) and the
   real cumulative candidate ledger
   (:class:`~review_article_agent.discovery.candidates.CandidateRepository`),
   then read back through the real ``current()`` snapshot read and a cheap
   freeze/roster-shaped read
   (:func:`~review_article_agent.discovery.freeze.corpus_has_authentic_freeze`).
2. ``queue_behaviour`` — synthetic logical jobs are admitted through the real
   durable worker job store
   (:class:`~review_article_agent.runtime.jobs.JobRepository`), including
   duplicate request ids (both idempotent replays and divergent-payload
   rejections). Admission time, resulting queue depth, duplicate counts, and
   failures are measured.

Each stage reports measured wall time (``time.perf_counter``), peak Python
allocations (``tracemalloc``), process-lifetime peak RSS
(``resource.getrusage``), on-disk store size, and failure/error counts. A
per-stage time budget bounds work; if it is exceeded the stage is **capped**, and
the cap and the amount actually processed are recorded rather than hidden.

This module is **not** production code: it adds no product behavior. It only
drives existing deterministic paths from synthetic inputs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

# Allow direct execution (``PYTHONPATH=src python3 scripts/scale_test.py``) and
# import as a module from the test suite.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from review_article_agent.discovery.candidates import (  # noqa: E402
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
)
from review_article_agent.discovery.freeze import (  # noqa: E402
    corpus_has_authentic_freeze,
)
from review_article_agent.discovery.records import (  # noqa: E402
    SOURCE_PUBMED,
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from review_article_agent.runtime.jobs import (  # noqa: E402
    JobConflictError,
    JobRepository,
)
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.storage.store import ProjectStore  # noqa: E402

#: The operations a synthetic rights record permits (mirrors the fixture used by
#: ``tests/test_discovery_candidates.py``; no new production behavior).
_RIGHTS_OPERATIONS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: How many synthetic observations one recorded run carries. A run is one
#: provider-shaped page of metadata; splitting into several runs exercises the
#: cumulative multi-run ledger path without changing the candidate count.
_RUN_CHUNK = 500

#: How many recorded runs are added per cumulative ledger revision. Each ingest
#: adds at most ``_RUN_CHUNK * _INGEST_RUNS`` newly-minted candidates to one
#: snapshot revision, which keeps this harness on the real, documented
#: incremental accumulation path (``CandidateRepository.ingest`` adds runs to the
#: prior cumulative snapshot). It also stays clear of an observed limitation:
#: a *single* revision minting >= 10,000 candidates overflows the 4-digit
#: ``dedup-NNNN`` decision-id shape and then fails read-back revalidation (see the
#: report ``notes``; fixing that is out of this task's scope).
_INGEST_RUNS = 4

#: Default per-stage wall-clock budget in seconds. If a stage exceeds it, the
#: stage stops and records ``capped`` with the amount actually processed.
_DEFAULT_STAGE_BUDGET_SECONDS = 600.0


# --------------------------------------------------------------------------- #
# Synthetic inputs (no network, no model, no copyrighted bytes)
# --------------------------------------------------------------------------- #
def generate_records(n: int) -> list[MetadataRecord]:
    """Return ``n`` distinct synthetic metadata records with unique identifiers.

    Titles and ids are fabricated for scale testing only. They carry no
    scientific meaning and are never claimed to be real publications.
    """
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("n must be a non-negative integer")
    records: list[MetadataRecord] = []
    for i in range(n):
        pmid = str(10_000_000 + i)
        records.append(
            MetadataRecord(
                source=SOURCE_PUBMED,
                source_id=pmid,
                pmid=pmid,
                pmcid=f"PMC{20_000_000 + i}",
                doi=f"10.9999/scale.{i}",
                title=f"Synthetic candidate {i} (scale test; not a real study)",
                publication_year=2000 + (i % 25),
            )
        )
    return records


def _search_result(records: list[MetadataRecord], *, run_index: int) -> SearchResult:
    """Wrap one chunk of records as a complete PubMed-shaped search result."""
    query = f"scale synthetic query {run_index}"
    page = SearchPage(
        source=SOURCE_PUBMED,
        query=query,
        requested_url=(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=scale"
        ),
        retrieved_at="2026-10-30T00:00:00+00:00",
        page_index=0,
        records=tuple(records),
        page_size=max(1, len(records)),
        total_count=len(records),
        complete=True,
        stop_reason="completed",
    )
    return SearchResult.from_pages(
        source=SOURCE_PUBMED,
        query=query,
        pages=(page,),
        started_at="2026-10-30T00:00:00+00:00",
        completed_at="2026-10-30T00:00:01+00:00",
        stop_reason="completed",
        total_count=len(records),
    )


def _rights_record(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """One synthetic public-domain rights record for a synthetic snapshot.

    This mirrors the synthetic rights fixture already used by the discovery
    candidate tests; it grants local storage of fabricated data only.
    """
    return {
        "rights_id": f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _RIGHTS_OPERATIONS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _RIGHTS_OPERATIONS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {
            "personal_data": False,
            "confidentiality": "public",
            "denial": False,
        },
        "recorded_by": "user",
        "recorded_at": "2026-10-30T00:00:00Z",
        "derived_status": "valid",
    }


def _dir_bytes(root: str) -> int:
    """Total on-disk size in bytes of every regular file under ``root``."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield start, items[start : start + size]


# --------------------------------------------------------------------------- #
# Stage 1: discovery candidate ledger
# --------------------------------------------------------------------------- #
def run_discovery_stage(
    n: int, workdir: Path, *, stage_budget: float = _DEFAULT_STAGE_BUDGET_SECONDS
) -> dict:
    """Push ``n`` synthetic records through the real candidate ledger paths."""
    app_dir = paths.app_dir_for(str(workdir / "project"))
    project_id = paths.new_project_id()
    records = generate_records(n)

    store = ProjectStore.create_at(app_dir, project_id)
    failures = 0
    errors: list[str] = []
    run_ids: list[str] = []
    processed = 0
    capped = False
    candidates_ingested = 0
    snapshot_present = False
    frozen = False
    revision_count = 0

    tracemalloc.start()
    started = time.perf_counter()
    try:
        try:
            discovery = DiscoveryRepository(store)
            corpus = CandidateRepository(store)

            # -- record synthetic observations through the real store --------- #
            for run_index, (offset, chunk) in enumerate(_chunks(records, _RUN_CHUNK)):
                try:
                    stored = discovery.record_search(
                        request_id=f"scale-run-{run_index:06d}",
                        result=_search_result(chunk, run_index=run_index),
                    )
                    run_ids.append(stored.run_id)
                    processed += len(chunk)
                except Exception as exc:  # noqa: BLE001 - measured, not hidden
                    failures += 1
                    errors.append(f"record_search:{type(exc).__name__}")
                if time.perf_counter() - started > stage_budget:
                    capped = True
                    break

            # -- cumulative candidate ledger ingestion + reads ---------------- #
            if run_ids:
                # Ingest recorded runs incrementally: each revision is a
                # cumulative snapshot adding the next batch of runs. This is the
                # real documented accumulation path (repeated searches feed one
                # cumulative ledger), and it keeps each revision's minted-candidate
                # count within the observed 4-digit decision-id ceiling.
                for batch_no, (_off, run_batch) in enumerate(
                    _chunks(run_ids, _INGEST_RUNS)
                ):
                    revision_id = CandidateRepository.new_revision_id()
                    rights = _rights_record(
                        project_id, DEFAULT_CORPUS_ARTIFACT_ID, revision_id
                    )
                    try:
                        corpus.ingest(
                            run_batch,
                            request_id=f"scale-corpus-{batch_no:06d}",
                            rights=rights,
                            revision_id=revision_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        errors.append(f"ingest:{type(exc).__name__}")
                    if time.perf_counter() - started > stage_budget:
                        capped = True
                        break

                if not capped or failures == 0:
                    try:
                        snapshot = corpus.current()
                        snapshot_present = snapshot is not None
                        if snapshot is not None:
                            candidates_ingested = len(snapshot["candidates"])
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        errors.append(f"current:{type(exc).__name__}")

                # Cheap freeze/roster-shaped read on the written corpus.
                try:
                    frozen = bool(
                        corpus_has_authentic_freeze(store, DEFAULT_CORPUS_ARTIFACT_ID)
                    )
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    errors.append(f"freeze_read:{type(exc).__name__}")

                try:
                    revision_count = int(
                        store.connection.execute(
                            "SELECT COUNT(*) AS n FROM revision WHERE project_id = ? "
                            "AND artifact_id = ?",
                            (project_id, DEFAULT_CORPUS_ARTIFACT_ID),
                        ).fetchone()["n"]
                    )
                except Exception:  # noqa: BLE001
                    revision_count = 0
        finally:
            elapsed = time.perf_counter() - started
            traced_current, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            max_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            store_bytes = _dir_bytes(store.app_dir)
            store.close()
    except Exception as exc:  # noqa: BLE001 - top-level guard
        elapsed = time.perf_counter() - started
        traced_peak = 0  # type: ignore[assignment]
        max_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        store_bytes = 0
        failures += 1
        errors.append(f"discovery_stage:{type(exc).__name__}")

    return {
        "records_requested": n,
        "records_processed": processed,
        "runs_recorded": len(run_ids),
        "ingestion_mode": "incremental_cumulative",
        "ingest_batch_runs": _INGEST_RUNS,
        "candidates_ingested": candidates_ingested,
        "corpus_snapshot_present": snapshot_present,
        "corpus_revision_count": revision_count,
        "corpus_frozen": frozen,
        "elapsed_seconds": round(elapsed, 4),
        "peak_python_traced_bytes": int(traced_peak),
        "max_rss_kib": int(max_rss_kib),
        "store_bytes": int(store_bytes),
        "failures": failures,
        "errors": errors,
        "capped": capped,
        "stage_time_budget_seconds": stage_budget,
    }


# --------------------------------------------------------------------------- #
# Stage 2: durable worker job queue behaviour
# --------------------------------------------------------------------------- #
def run_queue_stage(
    k: int, workdir: Path, *, stage_budget: float = _DEFAULT_STAGE_BUDGET_SECONDS
) -> dict:
    """Admit ``k`` synthetic jobs (with duplicates) through the real job store."""
    app_dir = paths.app_dir_for(str(workdir / "queue"))
    project_id = paths.new_project_id()
    store = ProjectStore.create_at(app_dir, project_id)

    failures = 0
    errors: list[str] = []
    admitted = 0
    duplicates_idempotent = 0
    duplicates_rejected = 0
    capped = False
    queue_depth = 0

    tracemalloc.start()
    started = time.perf_counter()
    try:
        jobs = JobRepository(store)
        # -- admit k distinct jobs -------------------------------------------- #
        for i in range(k):
            try:
                jobs.submit(
                    f"scale-job-{i:08d}",
                    task_kind="synthetic_probe",
                    parameters={"n": i},
                )
                admitted += 1
            except Exception as exc:  # noqa: BLE001 - measured, not hidden
                failures += 1
                errors.append(f"submit:{type(exc).__name__}")
            if time.perf_counter() - started > stage_budget:
                capped = True
                break

        # -- idempotent duplicate request ids (same payload) ------------------ #
        for i in range(0, admitted, 2):
            try:
                jobs.submit(
                    f"scale-job-{i:08d}",
                    task_kind="synthetic_probe",
                    parameters={"n": i},
                )
                duplicates_idempotent += 1
            except Exception as exc:  # noqa: BLE001
                failures += 1
                errors.append(f"dup_idempotent:{type(exc).__name__}")
            if time.perf_counter() - started > stage_budget:
                capped = True
                break

        # -- divergent duplicate request ids (must be refused) --------------- #
        for i in range(0, admitted, 10):
            try:
                jobs.submit(
                    f"scale-job-{i:08d}",
                    task_kind="synthetic_probe",
                    parameters={"n": i, "divergent": True},
                )
                # A divergent payload for an existing key is expected to refuse.
                failures += 1
                errors.append("dup_divergent:unexpected_accept")
            except JobConflictError:
                duplicates_rejected += 1
            except Exception as exc:  # noqa: BLE001
                failures += 1
                errors.append(f"dup_divergent:{type(exc).__name__}")
            if time.perf_counter() - started > stage_budget:
                capped = True
                break

        queue_depth = len(jobs.list(state="queued"))
    except Exception as exc:  # noqa: BLE001 - top-level guard
        failures += 1
        errors.append(f"queue_stage:{type(exc).__name__}")
    finally:
        elapsed = time.perf_counter() - started
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        max_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        store_bytes = _dir_bytes(store.app_dir)
        store.close()

    return {
        "jobs_requested": k,
        "jobs_admitted": admitted,
        "queue_depth_queued": queue_depth,
        "duplicates_idempotent": duplicates_idempotent,
        "duplicates_rejected": duplicates_rejected,
        "elapsed_seconds": round(elapsed, 4),
        "peak_python_traced_bytes": int(traced_peak),
        "max_rss_kib": int(max_rss_kib),
        "store_bytes": int(store_bytes),
        "failures": failures,
        "errors": errors,
        "capped": capped,
        "stage_time_budget_seconds": stage_budget,
    }


# --------------------------------------------------------------------------- #
# Orchestration + report
# --------------------------------------------------------------------------- #
def run_size(
    n: int, workdir: Path, *, stage_budget: float = _DEFAULT_STAGE_BUDGET_SECONDS
) -> dict:
    """Run both stages for one candidate size in its own subdirectory."""
    started = time.perf_counter()
    size_dir = Path(workdir) / f"size-{n}"
    size_dir.mkdir(parents=True, exist_ok=True)
    discovery = run_discovery_stage(n, size_dir, stage_budget=stage_budget)
    queue = run_queue_stage(n, size_dir, stage_budget=stage_budget)
    total = time.perf_counter() - started
    return {
        "size": n,
        "discovery_ledger": discovery,
        "queue_behaviour": queue,
        "total_elapsed_seconds": round(total, 4),
        "failures": discovery["failures"] + queue["failures"],
        "capped": discovery["capped"] or queue["capped"],
    }


def _environment() -> dict:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "model_calls": 0,
        "network_calls": 0,
        "notes": [
            "Observed limitation (not fixed here; out of this task's scope): a "
            "single candidate-ledger revision that mints >= 10,000 candidates "
            "fails read-back revalidation. The snapshot's dedup decision ids are "
            "formatted as dedup-NNNN (exactly four digits), so the 10,000th "
            "decision id 'dedup-10000' is rejected as corrupt membership. This "
            "harness therefore ingests incrementally (each revision adds at most "
            "ingest_batch_runs * run_chunk candidates), which is the real "
            "documented accumulation path and stays within the ceiling.",
        ],
        "budget_note": (
            "No numeric scale budget was agreed in Step 01 "
            "(docs/implementation/01-contracts.md); docs/specification.md line 17 "
            "sets only a target of ~1,000-10,000 candidates per project. All "
            "numbers here are measured once on this machine and are not universal "
            "latency, throughput, memory, storage, or cost promises. Model calls "
            "and network calls are zero: synthetic inputs only."
        ),
    }


def run_sizes(
    sizes: list[int],
    *,
    workdir: Path | None = None,
    stage_budget: float = _DEFAULT_STAGE_BUDGET_SECONDS,
) -> dict:
    """Run every requested size and return the JSON-serializable report."""
    owns_tmp = workdir is None
    tmp = tempfile.TemporaryDirectory(prefix="raa-scale-") if owns_tmp else None
    root = Path(tmp.name) if owns_tmp else Path(workdir)
    try:
        report = {
            "schema_version": 1,
            "kind": "step15_scale_report",
            **_environment(),
            "stage_time_budget_seconds": stage_budget,
            "run_chunk": _RUN_CHUNK,
            "ingest_batch_runs": _INGEST_RUNS,
            "sizes": [run_size(n, root, stage_budget=stage_budget) for n in sizes],
        }
    finally:
        if tmp is not None:
            tmp.cleanup()
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthetic scale/operational tests for review_article_agent."
    )
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[1000, 10000],
        help="candidate sizes to test (default: 1000 10000)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="optional path to also write the JSON report to",
    )
    parser.add_argument(
        "--stage-time-budget",
        type=float,
        default=_DEFAULT_STAGE_BUDGET_SECONDS,
        help="per-stage wall-clock budget in seconds before a stage is capped",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_sizes(
        list(args.sizes), workdir=None, stage_budget=args.stage_time_budget
    )
    text = json.dumps(report, indent=2, sort_keys=False)
    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
