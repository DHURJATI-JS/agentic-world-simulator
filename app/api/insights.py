"""Stored AI insights (with timestamps), semantic search, and on-demand analyses (rate limited)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

import config
from app.db import SessionLocal
from app.intel.common import recent_insight
from app.models import AIInsight, GeoEvent
from app.runtime import runtime
from app.security import ai_rate_limit, require_user

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("")
async def list_insights(
    kind: str | None = None,
    region: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: str = Depends(require_user),
) -> dict:
    stmt = select(AIInsight).order_by(desc(AIInsight.created_at))
    if kind:
        stmt = stmt.where(AIInsight.kind == kind)
    if region:
        stmt = stmt.where(AIInsight.region == region.upper())
    async with SessionLocal() as session:
        rows = (await session.execute(stmt.offset(offset).limit(limit))).scalars().all()
    return {"items": [r.to_dict(full=False) for r in rows]}


@router.get("/latest")
async def latest(kind: str, region: str | None = None, user: str = Depends(require_user)) -> dict:
    stmt = select(AIInsight).where(AIInsight.kind == kind)
    if region:
        stmt = stmt.where(AIInsight.region == region.upper())
    async with SessionLocal() as session:
        row = (await session.execute(stmt.order_by(desc(AIInsight.created_at)).limit(1))).scalar_one_or_none()
    return {"item": row.to_dict() if row else None}


@router.get("/search")
async def search(q: str = Query(..., min_length=2, max_length=300), limit: int = Query(15, ge=1, le=50),
                 user: str = Depends(require_user)) -> dict:
    vector, model = await runtime.embedder.embed_one(q)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(AIInsight)
                .where(AIInsight.embed_model == model, AIInsight.embedding.is_not(None))
                .order_by(AIInsight.embedding.cosine_distance(vector))
                .limit(limit)
            )
        ).scalars().all()
    return {"items": [r.to_dict(full=False) for r in rows], "model": model}


@router.get("/{insight_id}")
async def get_insight(insight_id: int, user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        row = await session.get(AIInsight, insight_id)
    if row is None:
        raise HTTPException(404, "insight not found")
    return row.to_dict()


class AnalyzeBody(BaseModel):
    kind: Literal["market_outlook", "region_analysis", "geo_impact", "risk_scan", "persona_pulse", "daily_brief"]
    region: str | None = Field(None, max_length=2)
    event_id: int | None = None
    force: bool = False


@router.post("/analyze")
async def analyze(body: AnalyzeBody, user: str = Depends(ai_rate_limit)) -> dict:
    """Queue an AI analysis. Identical recent analyses are reused instead of re-running the AI."""
    region = body.region.upper() if body.region else None
    if body.kind in ("market_outlook", "region_analysis") and not region:
        raise HTTPException(422, "region is required")
    if body.kind == "geo_impact":
        if not body.event_id:
            raise HTTPException(422, "event_id is required")
        async with SessionLocal() as session:
            if await session.get(GeoEvent, body.event_id) is None:
                raise HTTPException(404, "event not found")

    dedupe = {
        "market_outlook": f"market:{region}",
        "region_analysis": f"region:{region}",
        "geo_impact": f"geo:{body.event_id}",
        "risk_scan": "risk:anomaly",
        "persona_pulse": "persona",
        "daily_brief": "brief",
    }[body.kind]
    cooldown = config.ANALYSIS_COOLDOWN_SECONDS if not body.force else 120
    if body.kind in ("market_outlook", "region_analysis", "geo_impact"):
        existing = await recent_insight(dedupe, cooldown)
        if existing:
            return {"reused": True, "insight": existing,
                    "message": "A fresh analysis already exists - reusing it (spam protection)."}

    kind, payload = {
        "market_outlook": ("market.outlook", {"country": region, "reason": "requested by admin"}),
        "region_analysis": ("region.analysis", {"country": region}),
        "geo_impact": ("geo.impact", {"event_id": body.event_id}),
        "risk_scan": ("risk.scan", {"force": body.force}),
        "persona_pulse": ("personas.pulse", {"force": True}),
        "daily_brief": ("brief.daily", {}),
    }[body.kind]
    task, created = await runtime.tasks.enqueue(kind, payload, dedupe_key=f"user:{dedupe}", priority=2, requested_by=user)
    return {"reused": False, "task": task, "created": created,
            "message": "Queued." if created else "Already queued - not queued twice."}
