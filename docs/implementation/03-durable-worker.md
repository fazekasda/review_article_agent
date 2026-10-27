# 03 — Background worker, durable jobs, and recovery

## Dependencies

02. Use synthetic deterministic tasks; real agent integration follows in 04.

## Deliverables

- Local worker independent of CLI lifetime; supported start/status/attach/pause/resume/stop behavior and authenticated local control.
- Durable job/attempt states, authorization/budget checks, bounded retries, checkpoints, and recovery reconciliation.
- Exactly one active project globally; safe switching/draining. Waiting gates release execution resources.
- Explicit paused, waiting-human, waiting-external, failed, interrupted, and reconciliation-required outcomes.

## Acceptance criteria

- Start a task, close the CLI, and confirm completion/checkpoint from a new CLI.
- Kill/restart the test worker before dispatch, after external admission, and before local commit. Recover one accepted local result without claiming exactly-once external execution.
- Never blindly resend ambiguous external work with a new identity; demonstrate reconciliation or human escalation.
- Concurrent activation attempts cannot execute two projects. A switch cannot leave old-project jobs committing into the new project.
- Stale attempts cannot overwrite newer authorized results. Explain that fencing protects accepted writes, not arbitrary external side effects.
- Paused/budget-exhausted work does not schedule new calls. No busy loop while waiting for approval.
- Authentication rejects non-owner control; no root or public unauthenticated listener is required.

## Handoff and exclusions

Record restart/failure matrix, task identities, retention and retry policy. Automatic logout/reboot startup is deferred. Do not change system services/lingering or deploy a distributed engine without explicit authorization. All tests use owned disposable processes.
