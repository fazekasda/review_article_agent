# 02 — CLI, projects, artifacts, and backups

## Dependencies

00–01. Implement durable local domain state, not agent execution.

## Deliverables

- CLI entry point, configuration validation, project create/list/select/status, and stable application project IDs independent of paths or Git.
- Versioned storage migrations, immutable artifact revisions, validated user edits, approvals/dependencies, and per-project directories.
- Consistent backup/export and restore with manifest/schema versions; secrets excluded.
- Fixture projects and interface tests. Exact CLI spellings become documented contracts.

## Acceptance criteria

- Create two non-Git projects, restart the CLI, and recover their independent state/artifacts. Renaming/moving a registered project is explicit and does not create a new scientific identity.
- Invalid edits cannot overwrite approved revisions. An approved artifact edited afterward becomes a proposal and invalidates affected checks.
- A project ID/path mismatch or traversal cannot read/write another project's assets.
- Duplicate create/import requests are safe; failed transactions do not leave half-created projects or dangling approvals.
- Restore a consistent snapshot and verify artifact hashes and approvals, including while writes were occurring during backup. Do not copy only the live database file and ignore WAL state.
- Unsupported newer schemas fail clearly; migrations are tested on fixtures and preserve prior artifacts.
- CLI errors have actionable messages and stable non-success exit behavior; no credentials appear in status/export.

## Handoff and exclusions

Document storage authority, edit/import behavior, migration/backup commands, and test evidence. No background execution, OpenCode session, or scientific approval is yet operational. Domain fixtures must not masquerade as actual reviewed evidence.
