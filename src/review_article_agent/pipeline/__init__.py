"""Read-only pipeline orchestration helpers (kickoff 16 §6).

The package currently exposes only the read-only stage resolver used by the
``pipeline status`` CLI command (see :mod:`review_article_agent.pipeline.status`).
The companion ``pipeline run`` orchestrator reuses the same resolver.
"""

from __future__ import annotations

from .status import STAGES, Stage, resolve

__all__ = ["STAGES", "Stage", "resolve"]
