"""Simulation lab: presets, runs (queued in the background), calibrations."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

import config
from app.db import SessionLocal
from app.intel.common import load_profile
from app.models import Calibration, GeoEvent, SimulationRun, TaskRecord, utcnow
from app.runtime import runtime
from app.sim.scenarios import EVENT_TYPES, presets
from app.security import ai_rate_limit, require_user

router = APIRouter(prefix="/api/sim", tags=["simulation"])


async def _disaster_region() -> str:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(GeoEvent)
                .where(GeoEvent.occurred_at >= utcnow() - timedelta(days=7), GeoEvent.country.is_not(None))
                .order_by(desc(GeoEvent.severity))
                .limit(1)
            )
        ).scalar_one_or_none()
    return row.country if row and row.severity >= 0.4 else "JP"


@router.get("/presets")
async def get_presets(user: str = Depends(require_user)) -> dict:
    profile = await load_profile()
    return {
        "presets": presets(profile, await _disaster_region()),
        "event_types": EVENT_TYPES,
        "limits": {"max_consumers": config.SIM_MAX_CONSUMERS, "max_ticks": config.SIM_MAX_TICKS,
                   "default_consumers": config.SIM_DEFAULT_CONSUMERS, "default_ticks": config.SIM_DEFAULT_TICKS},
    }


class RunBody(BaseModel):
    preset_id: str | None = None
    name: str | None = Field(None, max_length=200)
    events: list[dict] | None = Field(None, max_length=40)
    consumers: int = Field(config.SIM_DEFAULT_CONSUMERS, ge=5_000, le=config.SIM_MAX_CONSUMERS)
    ticks: int = Field(config.SIM_DEFAULT_TICKS, ge=20, le=config.SIM_MAX_TICKS)
    shards: int = Field(0, ge=0, le=32)
    seed: int = Field(42, ge=0, le=10_000_000)
    compare_baseline: bool | None = None


@router.post("/run")
async def start_run(body: RunBody, user: str = Depends(ai_rate_limit)) -> dict:
    async with SessionLocal() as session:
        pending = (
            await session.execute(
                select(func.count(TaskRecord.id)).where(TaskRecord.kind == "sim.run",
                                                        TaskRecord.status.in_(("queued", "running")))
            )
        ).scalar() or 0
    if pending >= 2:
        raise HTTPException(429, "Two simulations are already queued or running - wait for one to finish.")
    profile = await load_profile()
    chosen = None
    if body.preset_id:
        chosen = next((p for p in presets(profile, await _disaster_region()) if p["id"] == body.preset_id), None)
        if chosen is None:
            raise HTTPException(404, "unknown preset")
    events = body.events if body.events is not None else (chosen["events"] if chosen else [])
    clean_events = []
    for e in events:
        if not isinstance(e, dict) or e.get("type") not in EVENT_TYPES:
            raise HTTPException(422, f"invalid event: {e}")
        e = dict(e)
        e["start"] = max(0, min(int(e.get("start", 0)), body.ticks - 1))
        clean_events.append(e)
    compare = body.compare_baseline if body.compare_baseline is not None else bool(chosen and chosen.get("compare_baseline"))
    name = body.name or (chosen["name"] if chosen else "Custom scenario")
    async with SessionLocal() as session:
        run = SimulationRun(
            name=name,
            status="queued",
            scenario={"preset_id": body.preset_id, "events": clean_events, "compare_baseline": compare,
                      "description": chosen["description"] if chosen else ""},
            params={"consumers": body.consumers, "ticks": body.ticks, "shards": body.shards, "seed": body.seed},
            consumers=body.consumers,
            ticks=body.ticks,
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    task, _ = await runtime.tasks.enqueue("sim.run", {"run_id": run_id}, dedupe_key=f"sim:{run_id}", priority=3,
                                          requested_by=user)
    async with SessionLocal() as session:
        row = await session.get(SimulationRun, run_id)
        row.task_id = task["id"]
        await session.commit()
        data = row.to_dict()
    return {"run": data, "task": task}


@router.get("/runs")
async def list_runs(limit: int = Query(30, ge=1, le=100), user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        rows = (await session.execute(select(SimulationRun).order_by(desc(SimulationRun.id)).limit(limit))).scalars().all()
    return {"items": [r.to_dict() for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: int, user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        row = await session.get(SimulationRun, run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    return row.to_dict(full=True)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        row = await session.get(SimulationRun, run_id)
    if row is None or not row.task_id:
        raise HTTPException(404, "run not found")
    ok = await runtime.tasks.cancel(row.task_id)
    return {"ok": ok}


class CalibrateBody(BaseModel):
    symbol: str | None = Field(None, max_length=40)


@router.post("/calibrate")
async def start_calibration(body: CalibrateBody, user: str = Depends(ai_rate_limit)) -> dict:
    key = f"calibrate:{body.symbol or 'all'}"
    task, created = await runtime.tasks.enqueue("calibrate.markets", {"symbol": body.symbol}, dedupe_key=key,
                                                priority=4, requested_by=user)
    return {"task": task, "created": created}


@router.get("/calibrations")
async def calibrations(user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        rows = (await session.execute(select(Calibration).order_by(desc(Calibration.created_at)).limit(200))).scalars().all()
    latest: dict[str, dict] = {}
    for row in rows:
        latest.setdefault(row.symbol, row.to_dict())
    return {"items": list(latest.values())}
