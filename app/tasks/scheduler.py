"""
Periodic scheduler. On the very first run only the global-headline fetch is scheduled;
everything else waits until those headlines are in. Afterwards headlines refresh once
every 24 hours, markets hourly, hazards every 15 minutes, HQ risk every 6 hours, etc.
Last-run times are stored in Postgres so restarts do not trigger extra fetches.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
from app.intel.common import get_state, load_profile, set_state
from app.models import utcnow
from app.runtime import runtime

log = logging.getLogger("worldsim.scheduler")


@dataclass
class ScheduledJob:
    kind: str
    every: int
    priority: int
    label: str
    requires_profile: bool = False
    after_first_news: bool = True
    requires_brief: bool = False


JOBS = [
    ScheduledJob("news.fetch", config.NEWS_REFRESH_SECONDS, 1, "Global headlines (reliable sources)", after_first_news=False),
    ScheduledJob("hazards.refresh", config.HAZARDS_REFRESH_SECONDS, 2, "Earthquakes & disasters (USGS, GDACS)"),
    ScheduledJob("markets.refresh", config.MARKETS_REFRESH_SECONDS, 3, "Markets, FX & commodities"),
    ScheduledJob("risk.scan", config.RISK_SCAN_SECONDS, 4, "HQ location risk scan", requires_profile=True),
    ScheduledJob("personas.pulse", config.PERSONA_PULSE_SECONDS, 6, "Persona agents: how people react",
                 requires_brief=True),
    ScheduledJob("worldbank.refresh", config.WORLDBANK_REFRESH_SECONDS, 7, "World Bank calibration data"),
    ScheduledJob("calibrate.markets", config.CALIBRATION_REFRESH_SECONDS, 8, "Calibrate market models to real indices"),
    ScheduledJob("embeddings.reembed", config.REEMBED_SECONDS, 9, "Re-embed vectors with the current model"),
]


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(2)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(30)

    async def tick(self) -> None:
        if not runtime.ai.available:
            return
        first = await get_state("first_run")
        profile = await load_profile()
        has_profile = bool(profile and profile.get("onboarding_complete"))
        has_brief = bool((await get_state("latest_brief")).get("insight_id"))
        now = utcnow()
        for job in JOBS:
            if job.after_first_news and not first.get("news_done"):
                continue
            if job.requires_profile and not has_profile:
                continue
            if job.requires_brief and not has_brief:
                continue
            state = await get_state(f"schedule:{job.kind}")
            last = datetime.fromisoformat(state["last"]) if state.get("last") else None
            if last is not None and now - last < timedelta(seconds=job.every):
                continue
            await runtime.tasks.enqueue(job.kind, {}, dedupe_key=f"sched:{job.kind}", priority=job.priority,
                                        requested_by="scheduler")
            await set_state(f"schedule:{job.kind}", {"last": now.isoformat()})
            if job.kind == "news.fetch" and not first.get("news_started"):
                await set_state("first_run", {**first, "news_started": now.isoformat()})
                await runtime.hub.notify(
                    "info",
                    "First run: collecting global headlines",
                    "Fetching today's headlines from CNN, The Times of India, The New York Times, BBC and other "
                    "reliable outlets before anything else.",
                    link="#tasks",
                )

    async def describe(self) -> list[dict]:
        out = []
        for job in JOBS:
            state = await get_state(f"schedule:{job.kind}")
            last = state.get("last")
            next_at = None
            if last:
                next_at = (datetime.fromisoformat(last) + timedelta(seconds=job.every)).isoformat()
            out.append({"kind": job.kind, "label": job.label, "every_s": job.every, "last": last, "next": next_at})
        return out
