"""review_article_agent — durable local project core.

This package holds the non-scientific application foundation for Step 02 of
``docs/implementation/02-project-foundation.md``: a bounded ``domain`` and
``storage`` layer implementing a per-project SQLite store plus a minimal,
atomic application-level registry. Agent execution, artifacts, approvals,
backups, and the CLI are intentionally *not* here yet (later workers own them).

Design constraints honoured here (see ``docs/contracts/03-policy-decisions.md``
``ARCH-STORE``/``ARCH-MIGRATE``/``ARCH-ACTIVE-ENF`` and
``docs/implementation/02-project-foundation.md``):

* Standard-library only, Python ``>=3.11``. No framework, no third-party runtime
  dependency.
* One app-owned, project-scoped SQLite store per project, plus a minimal
  application-level registry that maps stable application project IDs to store
  locations and arbitrates the single active project.
* Stable project IDs are generated independently of filesystem paths or Git.
* Versioned, transactional schema migrations; a newer on-disk schema is refused
  rather than silently downgraded.
* Owner-private application directories and database files.
* Exactly one *selected* project at a time. Selection is **not** execution: no
  worker runs in Step 02.
"""

from __future__ import annotations

__all__ = ["__version__"]

# Kept in step with ``pyproject.toml``; the app version is recorded in every
# store's metadata so future migrations and backups can reason about it.
__version__ = "0.0.0"
