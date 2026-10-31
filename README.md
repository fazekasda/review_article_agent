# review_article_agent

An AI-assisted, evidence-grounded scientific review writer.

`review_article_agent` is a personal, local Python CLI that carries a review project
from brief to a final approved export, with a human approval gate between every
stage. It is **standard-library only** (`dependencies = []`; one optional extra,
`pypdf`, for PDF text), `requires-python >= 3.11`, stores everything in SQLite under
a path-independent project registry, and runs long work through a detached,
owner-authenticated background worker. At most one project is execution-active at a
time, and no stage becomes authoritative without its exact named human gate.

## What the tool does today (end to end)

The machine stages are implemented and locally verified end to end:

- **Discovery and freeze** (Step 05): bounded public-metadata search (PubMed, Europe
  PMC), a cumulative candidate ledger, citation expansion, a coverage report, and an
  exact human-approved corpus freeze.
- **Acquisition** (Step 06): open-access JATS XML retrieval and folder import,
  deduplication, and rights/reconciliation bookkeeping.
- **Parsing** (Step 07): XML/JATS/TEI structural parsing with stable passage
  locators, plus bounded per-page **PDF text extraction** through the optional
  `pypdf` extra (page-locator passages; OCR, figures, and tables escalate to human
  review).
- **Screening** (Step 08): an iterative pilot, Jev screening, and exclusion audits.
- **Extraction** (Step 09): grounded extraction runs with output audits, available as
  a detached `extraction_run` worker job.
- **Synthesis, outline, and drafting** (Steps 10–11): framework, outline, section
  blueprints, and evidence-grounded drafts.
- **Review and tracing** (Steps 12–13): bounded critique/fact-check and
  citation-origin tracing, surfaced through the `review`/`tracing` CLI groups.
- **Delivery** (Step 14): global review, freshness, author notes, an exact-revision
  final approval, and a real Markdown/BibTeX/RIS export whose bibliography carries
  the title/authors/year/journal recorded in the frozen corpus.
- **Model-backed roles** (kickoff 16 §5): every generative role runs through one
  small adapter on the direct OpenRouter route — see
  [Generative roles](docs/generation.md).
- **Orchestrator** (kickoff 16 §6): `pipeline status` reports the current stage and
  the exact next action; `pipeline run` submits the current stage's machine jobs and
  stops at the next human gate, budget limit, pause, or failure.

## What is still manual (human work the tool will not do)

- The **brief, plan, and corpus freeze** (seed/plan/freeze approvals).
- **Screening pilot design** and **extraction pilot design**: the pilot configs and
  thresholds are human choices, and `pipeline run` deliberately submits nothing for
  the screening stage.
- **Every human gate**: the owner-authenticated named approvals recorded out of band
  on the controlling terminal through the `gate` CLI.
- **Hosted-use approvals**: a per-role `generation_hosted_use` document plus its
  named gate before any paid model call.
- **Final approval** before export: the owner-authenticated exact-revision
  `delivery.final_approval` gate; `delivery export` refuses without it.

**No live model or provider call** is made by the tests or this documentation, and
**no scientific validation is claimed**: every model output in the suite is a
recorded or injected fake, and live model validation is **unrun** (no credential was
available to the coordinator).

## Quickstart

No install is required; the core is standard-library only. Use the module entry
point with `PYTHONPATH=src`:

```console
$ PYTHONPATH=src python3 -m review_article_agent --help
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state create ./project --name Demo
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state list
```

Once a project exists, `pipeline status` reports the current stage and the exact
next action (read-only), and `pipeline run` drives the current stage's machine jobs
until the next human gate:

```console
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state pipeline status --project prj_…
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state pipeline run --config pipeline.json --project prj_…
```

The same `main()` is also reachable as `python -m review_article_agent.cli …` and
as an installed `review-article-agent …` console script. See the
[CLI contract](docs/cli.md) (§19 `pipeline status`, §24 `pipeline run`, §23
`generation run`) and [storage contract](docs/storage.md) for the exact commands,
output/exit behavior, and the limits of the private snapshot. The model port and the
per-role hosted-use approval are documented in
[Generative roles](docs/generation.md); the test tiers and CI jobs are in
[Continuous integration](docs/ci.md) and the [Running tests](#running-tests) section
below. When the pipeline stops at a human gate, the
[Operator guide — approving the human gates](docs/operator-guide.md) walks you
through each gate in order with the exact commands.

### Synthetic worker and job lifecycle (Step 03)

The Step 03 worker runs **bounded synthetic tasks only** (no model, provider,
network, or scientific work) over owner-only local control. `job run` ensures a
detached worker, submits one job, and returns **admission, never completion**;
observe the result later from a new CLI with `job status`/`job attach`. A stable
`--request-id` is the idempotency key: an exact-key replay yields the same job.

```console
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state create ./project --name Demo
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state job run --project prj_… --request-id demo-1 --steps 5
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state job status JOB_ID --project prj_…
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state worker stop
```

The `worker start|status|stop` lifecycle is documented in
[CLI contract §9](docs/cli.md) and the `job run|status|list|pause|resume|cancel|switch|drain|reconcile|attach`
surface in [CLI contract §10](docs/cli.md); the runtime safety contract is in
[Worker runtime safety contract](docs/worker.md), with the completion evidence in
the [Step 03 handoff](docs/implementation/handoffs/03-durable-worker.md).

### Discovery metadata adapters and inert observations (Step 05)

A bounded, stdlib-only **library** foundation for public literature metadata now
exists under `src/review_article_agent/discovery/`: a fixed-allowlist HTTPS GET
transport (no credentials, redirects/proxies disabled, bounded retries, size cap,
per-instance rate spacing) and provider adapters — PubMed ESearch+EFetch,
Europe PMC cursor-paginated search, and the Europe PMC citation family for
bounded bidirectional expansion. Constructing a transport and the adapters
performs **no** network I/O; a request happens only when `search()` is called. The
calls below are **placeholders** and are **not** executed by the docs or the tests:

```python
from review_article_agent.discovery.transport import DiscoveryTransport
from review_article_agent.discovery.pubmed import PubMedClient
from review_article_agent.discovery.europepmc import EuropePMCClient

transport = DiscoveryTransport()            # constructed; no I/O until search()
pubmed = PubMedClient(transport=transport)  # shares the one transport instance
europepmc = EuropePMCClient(transport)      # positional transport argument

# Placeholders only — never executed by docs or tests:
# pubmed_result = pubmed.search("asthma AND review[pt]")
# epmc_result = europepmc.search("EXT_ID:31452104 AND SRC:MED")
```

An added slice stores exact observations in the additive schema-11
`discovery_search_run` table through
`review_article_agent.storage.discovery.DiscoveryRepository`. Given a trusted
caller's `request_id` and an already-returned `SearchResult`,
`record_search(*, request_id=..., result=...)` writes one immutable row;
`get_search(run_id)` and `list_searches()` reconstruct it exactly, and an identical
request-key replay returns the historical row and timestamp. A divergent result is
a static conflict; different request keys stay separate observations even for an
identical payload (this is **not** scientific deduplication). The repository
performs **no** network call, so `search()` itself still repeats provider
observations and is **not** idempotent candidate ingestion.

Beyond the observation repository, the Step 05 **core library** and a personal
**discovery CLI** are implemented: a typed context read-and-gate layer
(brief/interview/seed/framing/scope/rubric/plan with exact named gates), an
approved-plan/seed/metadata-import execution library, a durable cumulative
candidate ledger (conservative exact publication matching — never a
study-independence claim, missing IDs and identifier conflicts stay explicit and
unresolved), bounded bidirectional citation expansion, a deterministic
coverage/work/monetary report, an exact human-approved `freeze`/`search_again`
record, and a live-capable revision-scoped hosted lead-proposal runner (implemented
but **unrun/not authorized** here). The `discovery` CLI
group (`context import|show`, `corpus ingest|show`, `lead status|content`, plus
`search run|seed|import`, `expand run|show`, `lead configure|run`,
`report create|show`, and `freeze propose|status|show|history|require`) submits,
proposes, and inspects these as **detached background jobs** or local readbacks; a
submission returns a queued
admission only. **No** scientific feature, production paid generation, upload,
scientific validation, or literature-coverage claim is implemented or made, and
metadata is an **untrusted observation** — no accepted context, eligibility, or
scientific identity. Default outputs are private, safe summaries and `--content`
is the explicit opt-in for private content. Its private bytes may contain exact
queries, source URLs, titles, and identifiers; hashes detect corruption, not
authenticity, and it is not a secret scanner. The exact API, defaults, and maximum
limits are in the [discovery API/limits](docs/discovery.md), and the
criterion-by-criterion status is in the
[Step 05 handoff](docs/implementation/handoffs/05-discovery.md).

## Running tests

The suite is standard-library only and needs no install. `scripts/run_tests.py`
runs one subprocess per test module in parallel and prints the slowest modules.

Fast tier (default; excludes the slow tier; < 1 min locally on 4 CPUs):

```console
$ python3 scripts/run_tests.py
```

Full tier, including the slow tier (`--slow` sets `RAA_SLOW_TESTS=1`):

```console
$ python3 scripts/run_tests.py --slow
```

Run a single module directly (add `RAA_SLOW_TESTS=1` to include its slow tests):

```console
$ PYTHONPATH=src:tests python3 -m unittest tests.test_cli
$ RAA_SLOW_TESTS=1 PYTHONPATH=src:tests python3 -m unittest tests.test_cli
```

By default the runner sets `RAA_TEST_SQLITE_SYNCHRONOUS=OFF`, a test-only hook
that lowers SQLite durability for throwaway temp stores; pass `--durable-sqlite`
to keep the production `PRAGMA synchronous = FULL`. The **slow tier** holds tests
that spawn real subprocess CLI runs, a real detached worker, the mock OpenCode
service, SIGKILL/recovery matrices, scale runs, and otherwise expensive or
off-MVP-path tests; the **fast tier** excludes them. See
[docs/ci.md](docs/ci.md) for the tiers, the CI jobs, and the local equivalents.

## Planning documents

- [Scientific and product baseline](docs/specification.md)
- [Session-by-session implementation plan](docs/implementation/README.md)
- [Step 00 handoff](docs/implementation/handoffs/00-runtime-spike.md)
- [Step 01 contracts — reading map](docs/contracts/README.md)
- [Step 01 handoff (complete)](docs/implementation/handoffs/01-contracts.md)
- [Step 02 handoff (complete, local foundation)](docs/implementation/handoffs/02-project-foundation.md)
- [Step 03 handoff (complete, synthetic detached worker)](docs/implementation/handoffs/03-durable-worker.md)
- [Step 04 handoff (complete, restricted runtime)](docs/implementation/handoffs/04-agent-runtime.md)
- [Step 05 handoff (complete — scoped discovery workflow)](docs/implementation/handoffs/05-discovery.md)
- [Step 15 release guide](docs/release.md) and [Step 15 handoff (locally verified MVP; awaiting user review)](docs/implementation/handoffs/15-release.md)
- [Discovery core, adapters, codec, CLI, and limits (Step 05)](docs/discovery.md)
- [Step 02 CLI contract](docs/cli.md)
- [Step 02 storage contract](docs/storage.md)
- [Step 03 worker runtime safety contract](docs/worker.md)
- [Continuous integration (GitHub Actions) and how to run the checks locally](docs/ci.md)
- [Step 01 contracts/policies decision 002 (accepted)](docs/decisions/002-step01-contracts-and-policies.md)
- [Runtime language decision 001 (accepted)](docs/decisions/001-runtime-language.md)
- [Step 01 kickoff](docs/implementation/kickoffs/01-contracts.md)
- [Generative roles on the direct OpenRouter route (operator/developer reference)](docs/generation.md)
- [Operator guide — approving the human gates](docs/operator-guide.md)
- [Generative roles direct-route decision 005 (accepted)](docs/decisions/005-generative-roles-direct-route.md)
- [Implementation decision record template](docs/implementation/decision-template.md)

Each implementation session must follow its step document and leave a handoff
report. The step-00 spike and the accepted decision records do not claim any
production feature is implemented or scientifically validated, and the tests make
no live model or provider call. What is implemented today, what remains manual, and
the honest limits of each capability are stated in
[docs/release.md](docs/release.md) (feature/support matrix and scientific
limitations) and the per-step handoffs under
[docs/implementation/handoffs/](docs/implementation/handoffs/).
