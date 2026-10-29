# 004 — Bounded direct extraction and quality checks

Accepted by the user on 2026-10-29 in session `ses_e77d76164ffeVqzjPHrRKrCB2N`,
against baseline `7d0d485b10ea195a654e361b23abfaa7041a043b`.

The user chose **“Real model execution”** and **“Extend direct route”**: extend
the [direct lead/Jev exception](003-step05-discovery-and-direct-lead.md) to
bounded, tool-free OpenRouter extraction and quality checking. This reuses the
existing rights, provider-term, scoped human-approval, activation, and cost-ledger
safeguards rather than opening the still-disabled OpenCode production route.
Models and project-specific pilot/audit rules remain explicit configuration;
this decision supplies neither scientific defaults nor project approvals.

The alternative was a larger OpenCode production-runtime extension. No global
execution-boundary restriction is relaxed. Real source transfer still requires
the exact source rights and hosted-use approval; sampled checks cannot establish
complete correctness. The user separately authorized a small live smoke test
using `OPENROUTER_API_KEY` from the environment; that is not authorization to
upload third-party papers or approve a real project's pilot.
