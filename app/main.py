"""FastAPI application: lifespan (AI gate, DB, workers, scheduler), middleware, routes."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

import config
from app.ai.embeddings import Embedder
from app.ai.guard import AIOverloaded
from app.ai.router import AIBadOutput, AIRequestFailed, AIUnavailable, build_router, render_unavailable_banner
from app.api import auth, insights, live, onboarding, simulation, system, world
from app.db import SessionLocal, engine, init_db
from app.models import AIUsage
from app.notify import NotificationHub
from app.runtime import runtime
from app.security import api_limiter
from app.tasks.jobs import register_jobs
from app.tasks.queue import TaskQueue
from app.tasks.scheduler import Scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("worldsim")

STATIC = Path(__file__).resolve().parent.parent / "static"
AI_LOCK_EXEMPT = ("/api/system/status", "/api/system/ai/recheck", "/api/auth/", "/api/stream", "/api/notifications")


async def _record_usage(row: dict) -> None:
    async with SessionLocal() as session:
        session.add(AIUsage(**row))
        await session.commit()


async def _on_ai_status(ok: bool, status: dict) -> None:
    runtime.hub.publish("ai_status", status)
    if runtime.started_at is None:
        return
    if ok:
        await runtime.hub.notify("success", f"AI is available again ({status.get('active_label')})",
                                 "Background work has resumed.")
    else:
        await runtime.hub.notify("critical", "AI is not present",
                                 (status.get("detail") or "")[:400] + " - the app is paused until a tier answers.")


async def _seed_claude_budget() -> None:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as session:
        calls, tokens = (
            await session.execute(
                select(func.count(AIUsage.id), func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0))
                .where(AIUsage.provider == "anthropic", AIUsage.ok.is_(True), AIUsage.created_at >= today)
            )
        ).one()
    runtime.ai.guard.seed_claude_usage(int(calls or 0), int(tokens or 0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime.hub = NotificationHub()
    runtime.ai = build_router()
    runtime.embedder = Embedder()
    runtime.ai.status_listener = _on_ai_status
    runtime.ai.usage_recorder = _record_usage

    await init_db()
    try:
        statuses = await runtime.ai.startup_check()
        log.info("AI chain ready - active tier: %s", runtime.ai.status().get("active_label"))
        for item in statuses:
            log.info("  %s. %-20s %-15s %s", item["order"], item["label"], item["state"], item.get("last_error") or item["model"])
    except AIUnavailable as exc:
        log.critical(render_unavailable_banner(exc.statuses))
        if config.AI_STARTUP_POLICY == "exit":
            raise RuntimeError("AI is not present - refusing to start") from None
        log.warning("AI_STARTUP_POLICY=lock: starting with the UI locked until an AI tier answers")
    await _seed_claude_budget()
    await runtime.embedder.probe()
    log.info("embeddings: %s", runtime.embedder.model_name)

    runtime.tasks = TaskQueue()
    register_jobs(runtime.tasks)
    await runtime.tasks.start()
    runtime.scheduler = Scheduler()
    runtime.scheduler.start()
    health = asyncio.create_task(runtime.ai.health_loop(), name="ai-health")
    runtime.started_at = datetime.now(timezone.utc)
    log.info("Agentic World Simulator running on http://%s:%s", config.APP_HOST, config.APP_PORT)
    try:
        yield
    finally:
        health.cancel()
        await runtime.scheduler.stop()
        await runtime.tasks.stop()
        await engine.dispose()


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan, docs_url="/api/docs",
              redoc_url=None, openapi_url="/api/openapi.json")


class GuardMiddleware:
    """Per-IP API rate limit, CSRF header check and the 'AI is not present' lock (pure ASGI)."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.inner(scope, receive, send)
        path, method = scope["path"], scope["method"]
        client = scope.get("client")
        wait = api_limiter.check(f"ip:{client[0] if client else 'unknown'}")
        if wait > 0:
            response = JSONResponse({"detail": "Too many requests - slow down."}, status_code=429,
                                    headers={"Retry-After": str(int(wait) + 1)})
            return await response(scope, receive, send)
        if method not in ("GET", "HEAD", "OPTIONS"):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"x-worldsim") != b"1":
                return await JSONResponse({"detail": "missing X-WorldSim header"}, status_code=403)(scope, receive, send)
        ai = getattr(runtime, "ai", None)
        if ai is not None and not ai.available and not path.startswith(AI_LOCK_EXEMPT):
            response = JSONResponse({"detail": "AI is not present", "ai": ai.status()}, status_code=503)
            return await response(scope, receive, send)
        return await self.inner(scope, receive, send)


app.add_middleware(GuardMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="worldsim_session",
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,
)


@app.exception_handler(AIUnavailable)
async def _ai_unavailable(request: Request, exc: AIUnavailable):
    return JSONResponse({"detail": "AI is not present", "reason": str(exc)[:600]}, status_code=503)


@app.exception_handler(AIRequestFailed)
async def _ai_request_failed(request: Request, exc: AIRequestFailed):
    return JSONResponse({"detail": f"The AI could not answer right now - please retry. ({str(exc)[:400]})"},
                        status_code=503, headers={"Retry-After": "60"})


@app.exception_handler(AIOverloaded)
async def _ai_overloaded(request: Request, exc: AIOverloaded):
    retry = int(exc.retry_after or 30) + 1
    return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": str(retry)})


@app.exception_handler(AIBadOutput)
async def _ai_bad_output(request: Request, exc: AIBadOutput):
    return JSONResponse({"detail": f"The AI returned an unusable answer: {exc}"}, status_code=502)


for module in (auth, system, world, insights, onboarding, simulation, live):
    app.include_router(module.router)


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
