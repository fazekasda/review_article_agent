"""Module entry point for ``python -m review_article_agent.cli``."""

from __future__ import annotations

from .main import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
