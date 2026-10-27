"""Command-line interface for ``review_article_agent`` (Step 02).

This package is the **stdlib-only** command surface over the durable local
project foundation. It contains no scientific processing, no agent execution,
and no model/provider configuration:

* :mod:`review_article_agent.cli.config` — the optional, strict configuration
  file loader and data-directory resolution.
* :mod:`review_article_agent.cli.main` — the ``argparse`` tree, command
  handlers, stable JSON output, and stable non-success exit categories.

Entry points (defined in ``pyproject.toml`` and by module execution):

* the console script ``review-article-agent``;
* ``python -m review_article_agent``;
* ``python -m review_article_agent.cli``.

The public CLI contract is documented in ``docs/cli.md``.
"""

from __future__ import annotations

from .config import ConfigError
from .main import main

__all__ = ["ConfigError", "main"]
