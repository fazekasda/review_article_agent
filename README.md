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
exactly-once external execution is claimed. This is **still not** a working review
writer: **no** model, provider, network, agent execution, or scientific feature or
approval is implemented, and **no scientific validation is claimed**.

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

## Planning documents

- [Scientific and product baseline](docs/specification.md)
- [Session-by-session implementation plan](docs/implementation/README.md)
- [Step 00 handoff](docs/implementation/handoffs/00-runtime-spike.md)
- [Step 01 contracts — reading map](docs/contracts/README.md)
- [Step 01 handoff (complete)](docs/implementation/handoffs/01-contracts.md)
- [Step 02 handoff (complete, local foundation)](docs/implementation/handoffs/02-project-foundation.md)
- [Step 03 handoff (complete, synthetic detached worker)](docs/implementation/handoffs/03-durable-worker.md)
- [Step 02 CLI contract](docs/cli.md)
- [Step 02 storage contract](docs/storage.md)
- [Step 03 worker runtime safety contract](docs/worker.md)
- [Step 01 contracts/policies decision 002 (accepted)](docs/decisions/002-step01-contracts-and-policies.md)
- [Runtime language decision 001 (accepted)](docs/decisions/001-runtime-language.md)
- [Step 01 kickoff](docs/implementation/kickoffs/01-contracts.md)
- [Implementation decision record template](docs/implementation/decision-template.md)

Next is implementation step 04 (restricted agent roles, model adapters, and
approvals; see its [step document](docs/implementation/04-agent-runtime.md)), which
is **not** implemented. Each implementation session must follow its step document
and leave a handoff report. The step-00 prototypes and the accepted decision
records do not claim any production feature is implemented or scientifically
validated: step 00 is a spike with a bounded evidence ceiling (the model transport
was a fixture; the native decision route and per-question live failures remain
unexercised), step 01 is a decision/contract session, step 02 is a durable **local**
core with no agent execution and no scientific approval, and step 03 is a synthetic
detached worker with no model, provider, or scientific approval.
