"""Process-wide singletons, populated during application startup (see app.main.lifespan)."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from app.ai.embeddings import Embedder
    from app.ai.router import AIRouter
    from app.notify import NotificationHub
    from app.tasks.queue import TaskQueue
    from app.tasks.scheduler import Scheduler


class Runtime:
    ai: "AIRouter"
    hub: "NotificationHub"
    tasks: "TaskQueue"
    scheduler: "Scheduler"
    embedder: "Embedder"
    started_at: datetime | None = None
    extras: dict[str, Any]

    def __init__(self) -> None:
        self.extras = {}


runtime = Runtime()
