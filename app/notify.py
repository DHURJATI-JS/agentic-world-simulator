"""Live notifications: persisted to Postgres and pushed to browsers over Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.db import SessionLocal
from app.models import Notification

log = logging.getLogger("worldsim.notify")


class NotificationHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """Fan an event out to every connected browser (never blocks)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                pass  # slow client: drop rather than stall the app

    async def notify(
        self,
        level: str,
        title: str,
        message: str = "",
        *,
        link: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a notification and push it live. level: info | success | warning | critical."""
        try:
            async with SessionLocal() as session:
                row = Notification(
                    level=level, title=title[:500], message=message[:4000], link=link, meta=meta or {}
                )
                session.add(row)
                await session.commit()
                data = row.to_dict()
        except Exception:  # DB hiccup must not break the caller
            log.exception("could not persist notification")
            data = {"id": None, "level": level, "title": title, "message": message, "link": link,
                    "meta": meta or {}, "read": False, "created_at": None}
        self.publish("notification", data)
        return data


def sse_format(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
