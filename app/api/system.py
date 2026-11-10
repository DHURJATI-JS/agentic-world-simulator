"""System status: AI failover chain, guard statistics, schedule, first-run state, sources."""
from __future__ import annotations

from fastapi import APIRouter, Depends

import config
from app.data.sources import NEWS_SOURCES
from app.intel.common import get_state, load_profile
from app.runtime import runtime
from app.security import api_limiter, require_user, user_ai_limiter

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status")
async def status(user: str = Depends(require_user)) -> dict:
    first = await get_state("first_run")
    profile = await load_profile()
    latest_news = await get_state("latest_news_batch")
    latest_brief = await get_state("latest_brief")
    ai = runtime.ai.status()
    ai["guard"]["user_rate_rejections"] = user_ai_limiter.rejected
    ai["guard"]["api_rate_rejections"] = api_limiter.rejected
    return {
        "app": {
            "name": config.APP_NAME,
            "version": config.APP_VERSION,
            "user": user,
            "started_at": runtime.started_at.isoformat() if runtime.started_at else None,
            "first_run": not first.get("news_done"),
            "news_started": first.get("news_started"),
            "news_done": first.get("news_done"),
            "onboarding_complete": bool(profile and profile.get("onboarding_complete")),
            "latest_news": latest_news,
            "latest_brief": latest_brief,
        },
        "ai": ai,
        "risk": await get_state("risk_profile"),
        "embeddings": {"model": runtime.embedder.model_name, "dimensions": config.EMBEDDING_DIM},
        "tasks": await runtime.tasks.counts(),
        "schedule": await runtime.scheduler.describe(),
        "live_clients": runtime.hub.subscriber_count,
    }


@router.post("/ai/recheck")
async def recheck_ai(user: str = Depends(require_user)) -> dict:
    """Probe the failover chain now (rate limited like any AI action)."""
    wait = user_ai_limiter.check(f"user:{user}")
    if wait > 0:
        return {"ok": False, "retry_after": int(wait) + 1, "ai": runtime.ai.status()}
    await runtime.ai.probe_all()
    return {"ok": True, "ai": runtime.ai.status()}


@router.get("/sources")
async def sources(user: str = Depends(require_user)) -> dict:
    latest = await get_state("latest_news_batch")
    from app.db import SessionLocal
    from app.models import NewsBatch

    report = {}
    if latest.get("batch_id"):
        async with SessionLocal() as session:
            batch = await session.get(NewsBatch, latest["batch_id"])
            report = (batch.sources or {}) if batch else {}
    return {
        "news": [{**s, "last": report.get(s["id"])} for s in NEWS_SOURCES],
        "refresh_every_hours": config.NEWS_REFRESH_SECONDS // 3600,
        "other": [
            {"name": "Yahoo Finance chart API", "use": "indices, FX, commodities"},
            {"name": "USGS Earthquake Hazards Program", "use": "earthquakes, seismic baselines"},
            {"name": "GDACS (UN/EC)", "use": "cyclones, floods, volcanoes, droughts, wildfires"},
            {"name": "Open-Meteo", "use": "geocoding and 7-day weather extremes"},
            {"name": "World Bank Open Data", "use": "GDP per capita, population, inflation"},
        ],
    }
