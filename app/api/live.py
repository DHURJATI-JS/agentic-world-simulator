"""Tasks, notifications and the live Server-Sent-Events stream."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select, update

from app.db import SessionLocal
from app.models import Notification, TaskRecord
from app.notify import sse_format
from app.runtime import runtime
from app.security import require_user

router = APIRouter(prefix="/api", tags=["live"])


@router.get("/tasks")
async def list_tasks(status: str | None = None, limit: int = Query(60, ge=1, le=200),
                     user: str = Depends(require_user)) -> dict:
    stmt = select(TaskRecord).order_by(desc(TaskRecord.id)).limit(limit)
    if status:
        stmt = stmt.where(TaskRecord.status == status)
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return {"items": [r.to_dict() for r in rows]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, user: str = Depends(require_user)) -> dict:
    ok = await runtime.tasks.cancel(task_id)
    if not ok:
        raise HTTPException(409, "task is not queued or running")
    return {"ok": True}


@router.get("/notifications")
async def list_notifications(unread_only: bool = False, limit: int = Query(50, ge=1, le=200),
                             user: str = Depends(require_user)) -> dict:
    stmt = select(Notification).order_by(desc(Notification.id)).limit(limit)
    if unread_only:
        stmt = stmt.where(Notification.read.is_(False))
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
        unread = (await session.execute(select(func.count(Notification.id)).where(Notification.read.is_(False)))).scalar()
    return {"items": [r.to_dict() for r in rows], "unread": unread or 0}


class ReadBody(BaseModel):
    ids: list[int] | None = None
    all: bool = False


@router.post("/notifications/read")
async def mark_read(body: ReadBody, user: str = Depends(require_user)) -> dict:
    stmt = update(Notification).values(read=True)
    if not body.all:
        if not body.ids:
            return {"ok": True}
        stmt = stmt.where(Notification.id.in_(body.ids[:500]))
    async with SessionLocal() as session:
        await session.execute(stmt)
        await session.commit()
    return {"ok": True}


@router.get("/stream")
async def stream(request: Request, user: str = Depends(require_user)) -> StreamingResponse:
    queue = runtime.hub.subscribe()

    async def events():
        try:
            yield "retry: 3000\n\n"
            yield sse_format("ai_status", runtime.ai.status())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield sse_format(event, data)
        finally:
            runtime.hub.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
