"""Module entry point for ``python -m review_article_agent``."""

from __future__ import annotations

from .cli.main import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
