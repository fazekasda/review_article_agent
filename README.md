# review_article_agent

An AI-assisted, evidence-grounded scientific review writer.

No production application features exist yet. Implementation step 00 (the
OpenCode integration and language spike) is **complete**: throwaway prototypes
under `spikes/runtime/` are verified against a real disposable app-owned OpenCode
v2.2.0 service, and on 2026-10-27 the user accepted the runtime-language decision
(Python core with a supported, explicitly authenticated OpenCode V2 HTTP
integration). Step 01 (contracts and technical decisions) is the next step; see
its [kickoff](docs/implementation/kickoffs/01-contracts.md).

## Planning documents

- [Scientific and product baseline](docs/specification.md)
- [Session-by-session implementation plan](docs/implementation/README.md)
- [Step 00 handoff](docs/implementation/handoffs/00-runtime-spike.md)
- [Step 01 contracts — proposal bundle (reading map)](docs/contracts/README.md)
- [Step 01 handoff (partial, awaiting user ratification)](docs/implementation/handoffs/01-contracts.md)
- [Runtime language decision 001 (accepted)](docs/decisions/001-runtime-language.md)
- [Step 01 kickoff](docs/implementation/kickoffs/01-contracts.md)
- [Implementation decision record template](docs/implementation/decision-template.md)

Start with implementation step 01. Each implementation session must follow its step document and leave a handoff report. The step-00 prototypes and the accepted decision record do not claim any production feature is implemented or scientifically validated: step 00 is a spike with a bounded evidence ceiling (the model transport was a fixture; the native decision route and per-question live failures remain unexercised), and step 01 is a decision/contract session before substantial implementation.
