"""Standalone synthetic worker package (Step 03).

Exposes the explicit worker engine and the process entry point
(``python -m review_article_agent.worker --data-dir <ABS>``).
"""

from __future__ import annotations

from .engine import (
    APP_NAME,
    OPERATIONS,
    PROTOCOL_VERSION,
    WorkerEngine,
)

__all__ = ["APP_NAME", "OPERATIONS", "PROTOCOL_VERSION", "WorkerEngine"]
