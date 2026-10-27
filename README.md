# review_article_agent

An AI-assisted, evidence-grounded scientific review writer.

No production application features exist yet. Implementation step 00 (the
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
versioned representation, not a new human-ratification gate. No production feature
and no scientific validation is claimed. Step 02 (CLI, project registry, artifacts,
and backups) is the next implementation step; see its
[step document](docs/implementation/02-project-foundation.md).

## Planning documents

- [Scientific and product baseline](docs/specification.md)
- [Session-by-session implementation plan](docs/implementation/README.md)
- [Step 00 handoff](docs/implementation/handoffs/00-runtime-spike.md)
- [Step 01 contracts — reading map](docs/contracts/README.md)
- [Step 01 handoff (complete)](docs/implementation/handoffs/01-contracts.md)
- [Step 01 contracts/policies decision 002 (accepted)](docs/decisions/002-step01-contracts-and-policies.md)
- [Runtime language decision 001 (accepted)](docs/decisions/001-runtime-language.md)
- [Step 01 kickoff](docs/implementation/kickoffs/01-contracts.md)
- [Implementation decision record template](docs/implementation/decision-template.md)

Next is implementation step 02 (CLI, project registry, artifacts, and backups; see
its [step document](docs/implementation/02-project-foundation.md)). Each
implementation session must follow its step document and leave a handoff report.
The step-00 prototypes and the accepted decision records do not claim any production
feature is implemented or scientifically validated: step 00 is a spike with a
bounded evidence ceiling (the model transport was a fixture; the native decision
route and per-question live failures remain unexercised), and step 01 is a
decision/contract session before substantial implementation.
