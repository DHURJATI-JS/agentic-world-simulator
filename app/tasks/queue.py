"""
Durable background task queue on PostgreSQL.

- tasks live in the `tasks` table (survive restarts; crashed 'running' tasks are re-queued)
- workers claim work with SELECT ... FOR UPDATE SKIP LOCKED, one pool per lane (io / ai / sim)
- dedupe keys stop the same job being queued twice (spam protection)
- progress is pushed live to the browser over SSE
- while the AI is not present, workers pause and tasks wait
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update

import config
from app.ai.guard import AIOverloaded
from app.ai.router import AIRequestFailed, AIUnavailable
from app.db import SessionLocal
from app.models import TaskRecord, utcnow
from app.runtime import runtime

log = logging.getLogger("worldsim.tasks")

Handler = Callable[["TaskContext", dict], Awaitable[dict | None]]


@dataclass
class JobSpec:
    kind: str
    lane: str
    handler: Handler
    max_attempts: int = 2
    timeout: float = 1800
    needs_ai: bool = True


class TaskContext:
    def __init__(self, queue: "TaskQueue", task: TaskRecord) -> None:
        self.queue = queue
        self.task_id = task.id
        self.kind = task.kind
        self.requested_by = task.requested_by
        self._last_push = 0.0

    async def progress(self, fraction: float, note: str = "") -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        now = time.monotonic()
        self.queue.hub_publish(self.task_id, self.kind, "running", fraction, note)
        if now - self._last_push < 1.0 and fraction < 1.0:
            return
        self._last_push = now
        async with SessionLocal() as session:
            await session.execute(
                update(TaskRecord).where(TaskRecord.id == self.task_id).values(progress=fraction, progress_note=note[:500])
            )
            await session.commit()

    def cancelled(self) -> bool:
        return self.task_id in self.queue.cancel_requests

    async def enqueue(self, kind: str, payload: dict | None = None, **kwargs) -> dict:
        task, _ = await self.queue.enqueue(kind, payload, requested_by=kwargs.pop("requested_by", self.requested_by), **kwargs)
        return task


class TaskQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, JobSpec] = {}
        self._wake = {lane: asyncio.Event() for lane in config.TASK_LANES}
        self._workers: list[asyncio.Task] = []
        self.cancel_requests: set[int] = set()

    def register(self, kind: str, lane: str, handler: Handler, **kwargs) -> None:
        self.jobs[kind] = JobSpec(kind=kind, lane=lane, handler=handler, **kwargs)

    def hub_publish(self, task_id: int, kind: str, status: str, progress: float, note: str = "",
                    error: str | None = None, result: dict | None = None) -> None:
        runtime.hub.publish("task", {"id": task_id, "kind": kind, "status": status, "progress": progress,
                                     "note": note, "error": error, "result": result})

    async def enqueue(
        self,
        kind: str,
        payload: dict | None = None,
        *,
        dedupe_key: str | None = None,
        priority: int = 5,
        requested_by: str = "system",
        delay: float = 0,
    ) -> tuple[dict, bool]:
        spec = self.jobs.get(kind)
        if spec is None:
            raise ValueError(f"unknown task kind: {kind}")
        async with SessionLocal() as session:
            if dedupe_key:
                existing = (
                    await session.execute(
                        select(TaskRecord)
                        .where(TaskRecord.dedupe_key == dedupe_key, TaskRecord.status.in_(("queued", "running")))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing:
                    return existing.to_dict(), False
            task = TaskRecord(
                kind=kind,
                lane=spec.lane,
                payload=payload or {},
                dedupe_key=dedupe_key,
                priority=priority,
                requested_by=requested_by,
                max_attempts=spec.max_attempts,
                run_after=utcnow() + timedelta(seconds=delay),
            )
            session.add(task)
            await session.commit()
            data = task.to_dict()
        self.hub_publish(data["id"], kind, "queued", 0.0, "queued")
        self._wake[spec.lane].set()
        return data, True

    async def start(self) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(TaskRecord)
                .where(TaskRecord.status == "running")
                .values(status="queued", progress_note="re-queued after restart")
            )
            await session.commit()
        for lane, count in config.TASK_LANES.items():
            for n in range(count):
                self._workers.append(asyncio.create_task(self._worker(lane, n), name=f"worker-{lane}-{n}"))

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def cancel(self, task_id: int) -> bool:
        async with SessionLocal() as session:
            task = await session.get(TaskRecord, task_id)
            if task is None or task.status not in ("queued", "running"):
                return False
            if task.status == "queued":
                task.status = "cancelled"
                task.finished_at = utcnow()
                await session.commit()
                self.hub_publish(task_id, task.kind, "cancelled", task.progress, "cancelled")
                return True
        self.cancel_requests.add(task_id)  # running: cooperative cancellation
        return True

    async def counts(self) -> dict:
        async with SessionLocal() as session:
            rows = (
                await session.execute(select(TaskRecord.status).where(TaskRecord.status.in_(("queued", "running"))))
            ).scalars().all()
        return {"queued": sum(1 for r in rows if r == "queued"), "running": sum(1 for r in rows if r == "running")}

    async def _claim(self, lane: str) -> TaskRecord | None:
        async with SessionLocal() as session:
            task = (
                await session.execute(
                    select(TaskRecord)
                    .where(TaskRecord.lane == lane, TaskRecord.status == "queued", TaskRecord.run_after <= utcnow())
                    .order_by(TaskRecord.priority, TaskRecord.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if task is None:
                return None
            task.status = "running"
            task.started_at = utcnow()
            task.attempts += 1
            task.progress = 0.0
            task.progress_note = "started"
            await session.commit()
            return task

    async def _worker(self, lane: str, n: int) -> None:
        await asyncio.sleep(0.5 * n)
        while True:
            try:
                if not runtime.ai.available:
                    await asyncio.sleep(5)  # AI is not present: the app does not run work
                    continue
                task = await self._claim(lane)
                if task is None:
                    self._wake[lane].clear()
                    try:
                        await asyncio.wait_for(self._wake[lane].wait(), timeout=5)
                    except TimeoutError:
                        pass
                    continue
                await self._run(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker %s-%s crashed; continuing", lane, n)
                await asyncio.sleep(2)

    async def _finish(self, task: TaskRecord, status: str, *, result: dict | None = None, error: str | None = None,
                      retry_in: float | None = None, keep_attempt: bool = True) -> None:
        values: dict = {"status": status, "error": error}
        if status == "queued":
            values["run_after"] = utcnow() + timedelta(seconds=retry_in or 30)
            values["progress_note"] = f"retrying: {error}"[:500] if error else "re-queued"
            if not keep_attempt:
                values["attempts"] = max(0, task.attempts - 1)
        else:
            values["finished_at"] = utcnow()
            values["progress"] = 1.0 if status == "done" else task.progress
            if status == "done":
                values["progress_note"] = "done"
            if result is not None:
                values["result"] = result
        async with SessionLocal() as session:
            await session.execute(update(TaskRecord).where(TaskRecord.id == task.id).values(**values))
            await session.commit()
        self.cancel_requests.discard(task.id)
        self.hub_publish(task.id, task.kind, status, values.get("progress", 0.0), values.get("progress_note", ""), error, result)

    async def _run(self, task: TaskRecord) -> None:
        spec = self.jobs.get(task.kind)
        if spec is None:
            await self._finish(task, "failed", error=f"no handler for {task.kind}")
            return
        ctx = TaskContext(self, task)
        self.hub_publish(task.id, task.kind, "running", 0.0, "started")
        try:
            result = await asyncio.wait_for(spec.handler(ctx, dict(task.payload or {})), timeout=spec.timeout)
        except asyncio.CancelledError:
            await self._finish(task, "queued", error="worker stopped", retry_in=5, keep_attempt=False)
            raise
        except AIRequestFailed as exc:
            # The AI is present but could not answer this request (slow model, refusal...): retry with backoff.
            message = f"AI could not answer: {exc}"[:1000]
            if task.attempts < (task.max_attempts or spec.max_attempts):
                await self._finish(task, "queued", error=message, retry_in=120 * task.attempts)
            else:
                await self._finish(task, "failed", error=message)
                await runtime.hub.notify("warning", f"AI task failed: {task.kind}", message[:300], link="#tasks",
                                         meta={"task_id": task.id})
        except AIUnavailable as exc:
            # The AI chain is down: keep the task, it runs when the AI is back.
            await self._finish(task, "queued", error=f"AI is not present: {exc}"[:400], retry_in=60, keep_attempt=False)
        except AIOverloaded as exc:
            await self._finish(task, "queued", error=str(exc), retry_in=exc.retry_after or 30, keep_attempt=False)
        except TaskCancelled:
            await self._finish(task, "cancelled", error="cancelled by user")
        except Exception as exc:  # noqa: BLE001
            log.exception("task %s (%s) failed", task.id, task.kind)
            message = f"{type(exc).__name__}: {exc}"[:1000]
            if task.attempts < (task.max_attempts or spec.max_attempts):
                await self._finish(task, "queued", error=message, retry_in=30 * task.attempts)
            else:
                await self._finish(task, "failed", error=message)
                await runtime.hub.notify("warning", f"Background task failed: {task.kind}", message[:300],
                                         link="#tasks", meta={"task_id": task.id})
        else:
            await self._finish(task, "done", result=_compact(result or {}))


class TaskCancelled(Exception):
    pass


def _compact(result: dict) -> dict:
    """Keep task results small (big payloads belong in their own tables)."""
    out = {}
    for key, value in result.items():
        if isinstance(value, (list, dict)) and len(str(value)) > 4000:
            out[key] = f"<{type(value).__name__} with {len(value)} items>"
        else:
            out[key] = value
    return out
