# review_article_agent

An AI-assisted, evidence-grounded scientific review writer.

A durable **local project core** exists; scientific features do not. Step 00 (the
OpenCode integration and language spike) is **complete**: throwaway prototypes
under `spikes/runtime/` are verified against a real disposable app-owned OpenCode
v2.2.0 service, and on 2026-10-27 the user accepted the runtime-language decision
(Python core with a supported, explicitly authenticated OpenCode V2 HTTP
integration). Step 01 (contracts and technical decisions) is **complete as a
decision/contract documentation step**: on 2026-10-27 the user dispositioned every
P1–P6 and operational/technical item in the recorded Step 01 interview, binding to
the reviewed revision `ee7f7ea`, and the versioned implementation contracts
(C01 v1.4, C02 v3.2, C03 v1) carry the accepted behaviors. The behavioral
directions are approved; the exact field/schema spelling remains a reviewable
versioned representation, not a new human-ratification gate. Step 02 (CLI, project
registry, artifacts, and backups) is **complete as a local foundation** (integrated
at `5efa4f2`): a stdlib-only CLI over a path-independent project registry,
immutable artifact revisions, non-authorizing local fixture approvals, and a
consistent private backup/verify/restore. It implements **no** agent execution, no
OpenCode session, and no scientific feature or scientific approval — every
revision is a local `proposal`, and `export` is a **private local storage
snapshot, not a manuscript export**. Step 03 (background worker, durable jobs, and
recovery) is **complete as a synthetic slice** (integrated at `2530955`): a
stdlib-only, owner-authenticated **detached worker** with durable job/attempt/result/
budget state, an owner-only Unix-domain-socket control transport, explicit
recovery/reconciliation, exactly one execution-active project globally, and a
real-process crash/concurrency matrix. Every task is a bounded **synthetic**
numeric/waiting-human task and the external provider is a **synthetic fixture** with
no egress; reserves are **virtual** (actual external spend USD 0) and **no**
exactly-once external execution is claimed. Step 04 (restricted agent roles, model
adapters, and approvals) is now **integrated** at `8626cee` as a **restricted
runtime**: an app-owned, version-pinned OpenCode **v2.2.0** service, a pure
fail-closed role/proposal boundary, a thin bounded session
`submit_session_turn`/`reconcile_session_turn` **library** path, a direct-Jev typed
adapter with durable versioned records and an atomic commit, and app-owned exact
named-gate human approvals through the `gate` CLI. **Only the model transport is a
loopback fixture**: there is **no** paid provider call, provider credential, or
network egress (actual spend **USD 0**), **no** production paid generation or hosted
transfer is authorized, and this is **still not** a working review writer — **no**
scientific feature, production model run, or **scientific validation is claimed**.
Step 04 is **complete as a restricted runtime**: its full local verification passed
at `c1f927b` (**2,105 tests, no skips**; see the
[Step 04 handoff](docs/implementation/handoffs/04-agent-runtime.md)).
Step 05 (reproducible discovery and corpus freeze) now has its **core library
implemented plus a personal discovery CLI**: a bounded, stdlib-only public-metadata
transport and provider adapters; a lossless codec and schema-11 observation
repository; a typed context read-and-gate layer; an approved-plan/seed/metadata
import execution library; a durable cumulative candidate ledger; bounded
bidirectional citation expansion; a deterministic coverage/work/monetary report;
an exact human-approved freeze/search-again record; and a live-capable,
separately-authorized direct lead-proposal runner (implemented but **unrun/not
authorized** here; the existing app-owned OpenCode production block is unchanged).
The `discovery` CLI group submits and inspects these as **detached background
jobs**, including the integrated `report create|show` and `freeze
propose|status|show|history|require` readbacks. Step 05 **implementation is
integrated**; **final acceptance and PR are pending** (the E1B/E2 acceptance
cases, a final frozen-HEAD run, and the PR) — see the
[Step 05 handoff](docs/implementation/handoffs/05-discovery.md) and the
[discovery API/limits](docs/discovery.md).

## Getting started (local core only)

No install is required; the core is standard-library only. Use the module entry
point with `PYTHONPATH=src`:

```console
$ PYTHONPATH=src python3 -m review_article_agent --help
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state create ./project --name Demo
$ PYTHONPATH=src python3 -m review_article_agent --data-dir ./state list
```

The same `main()` is also reachable as `python -m review_article_agent.cli …` and
as an installed `review-article-agent …` console script. See the
[CLI contract](docs/cli.md) and [storage contract](docs/storage.md) for the exact
commands, output/exit behavior, and the limits of the private snapshot.

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

## Planning documents

- [Scientific and product baseline](docs/specification.md)
- [Session-by-session implementation plan](docs/implementation/README.md)
- [Step 00 handoff](docs/implementation/handoffs/00-runtime-spike.md)
- [Step 01 contracts — reading map](docs/contracts/README.md)
- [Step 01 handoff (complete)](docs/implementation/handoffs/01-contracts.md)
- [Step 02 handoff (complete, local foundation)](docs/implementation/handoffs/02-project-foundation.md)
- [Step 03 handoff (complete, synthetic detached worker)](docs/implementation/handoffs/03-durable-worker.md)
- [Step 04 handoff (complete, restricted runtime)](docs/implementation/handoffs/04-agent-runtime.md)
- [Step 05 handoff (integrated — final acceptance/PR pending)](docs/implementation/handoffs/05-discovery.md)
- [Discovery core, adapters, codec, CLI, and limits (Step 05)](docs/discovery.md)
- [Step 02 CLI contract](docs/cli.md)
- [Step 02 storage contract](docs/storage.md)
- [Step 03 worker runtime safety contract](docs/worker.md)
- [Step 01 contracts/policies decision 002 (accepted)](docs/decisions/002-step01-contracts-and-policies.md)
- [Runtime language decision 001 (accepted)](docs/decisions/001-runtime-language.md)
- [Step 01 kickoff](docs/implementation/kickoffs/01-contracts.md)
- [Implementation decision record template](docs/implementation/decision-template.md)

Implementation step 04 (restricted agent roles, model adapters, and approvals; see
its [step document](docs/implementation/04-agent-runtime.md)) is now **complete as a
restricted runtime** (integrated at `8626cee`; final full-repository verification
passed at `c1f927b` — see the
[Step 04 handoff](docs/implementation/handoffs/04-agent-runtime.md)). Each
implementation session must follow its step document and
leave a handoff report. The step-00 prototypes and the accepted decision records do
not claim any production feature is implemented or scientifically validated: step 00
is a spike with a bounded evidence ceiling (the model transport was a fixture; the
native decision route and per-question live failures remain unexercised), step 01 is
a decision/contract session, step 02 is a durable **local** core with no agent
execution and no scientific approval, step 03 is a synthetic detached worker with no
model, provider, or scientific approval, and step 04 is a **restricted** runtime
whose only model transport is a loopback fixture — no scientific autonomous
workflow, production paid generation, hosted transfer, scientific feature, or
scientific validation is implemented or claimed. Step 05 has its **core library
and personal discovery CLI implemented** — a bounded public-metadata transport,
provider adapters, a lossless schema-11 observation repository, a typed context
read-and-gate layer, an approved-plan/seed/import execution library, a cumulative
candidate ledger, bounded citation expansion, a coverage/work/monetary report, an
exact human-approved freeze/search-again record, and a live-capable hosted
lead-proposal runner, surfaced through the `discovery` CLI (see the
[Step 05 handoff](docs/implementation/handoffs/05-discovery.md)); its
**implementation is integrated** and **final acceptance and PR are pending** (the
E1B/E2 acceptance cases, a final frozen-HEAD run, and the PR — no CI is
configured). No scientific feature, production paid
generation, upload, or scientific validation is implemented or claimed.
