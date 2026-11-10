"""Background job handlers (registered on the task queue at startup)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import desc, select, update

import config
from app.data import hazards, markets, news, worldbank
from app.data.geo import country_name
from app.data.sources import MARKETS
from app.db import SessionLocal
from app.intel import briefing, geo_impact, outlook, personas, risk, simreport
from app.intel.common import get_state, load_profile, recent_insight, set_state
from app.models import AIInsight, Calibration, MarketQuote, NewsArticle, PersonaMemory, SimulationRun, utcnow
from app.runtime import runtime
from app.sim.calibration import calibrate
from app.sim.engine import SimCancelled, compute_uplift, run_simulation
from app.sim.scenarios import baseline_events
from app.sim.world import build_world
from app.tasks.queue import TaskCancelled, TaskContext, TaskQueue

log = logging.getLogger("worldsim.jobs")
AUTO_OUTLOOKS = 3  # automatic "market is down" analyses per market refresh


# --------------------------------------------------------------------------- data
async def job_news_fetch(ctx: TaskContext, payload: dict) -> dict:
    await ctx.progress(0.02, "Collecting global headlines from reliable sources")
    res = await news.collect_headlines(progress=lambda p, note: ctx.progress(0.03 + p * 0.72, note))
    await ctx.progress(0.8, "Embedding headlines for semantic search (pgvector)")
    await news.embed_articles(res["new_ids"], runtime.embedder)
    first = await get_state("first_run")
    first_time = not first.get("news_done")
    await set_state("first_run", {**first, "news_done": utcnow().isoformat()})
    await set_state("latest_news_batch", {"batch_id": res["batch_id"], "at": utcnow().isoformat(),
                                          "headlines": res["headlines"], "new": res["new"]})
    runtime.hub.publish("world_update", {"what": "news"})
    failed = [s["outlet"] + " " + s["feed"] for s in res["sources"].values() if s["status"] != "ok"]
    await runtime.hub.notify(
        "success",
        f"Collected {res['new']} new headlines from {res['sources_ok']}/{res['sources_total']} reliable sources",
        ("Unavailable or stale: " + ", ".join(failed)) if failed else "All sources responded.",
        link="#news",
    )
    await ctx.enqueue("news.enrich", {"batch_id": res["batch_id"]}, dedupe_key=f"enrich:{res['batch_id']}", priority=2)
    if first_time:
        for kind, prio in (("hazards.refresh", 2), ("markets.refresh", 3), ("worldbank.refresh", 6)):
            await ctx.enqueue(kind, {}, dedupe_key=f"sched:{kind}", priority=prio)
            await set_state(f"schedule:{kind}", {"last": utcnow().isoformat()})
    return {k: v for k, v in res.items() if k not in ("new_ids",)}


async def job_news_enrich(ctx: TaskContext, payload: dict) -> dict:
    res = await briefing.enrich_batch(payload.get("batch_id"), progress=lambda p, n: ctx.progress(p * 0.95, n))
    await ctx.enqueue("brief.daily", {"batch_id": payload.get("batch_id")},
                      dedupe_key=f"brief:{payload.get('batch_id')}", priority=2)
    return res


async def job_brief_daily(ctx: TaskContext, payload: dict) -> dict:
    res = await briefing.daily_brief(payload.get("batch_id"), progress=ctx.progress)
    await ctx.enqueue("personas.pulse", {}, dedupe_key="sched:personas.pulse", priority=5)
    return res


async def job_markets_refresh(ctx: TaskContext, payload: dict) -> dict:
    res = await markets.refresh_markets(progress=lambda p, n: ctx.progress(p * 0.9, n))
    runtime.hub.publish("world_update", {"what": "markets"})
    async with SessionLocal() as session:
        calibrated = (await session.execute(select(Calibration.id).limit(1))).scalar_one_or_none()
    if calibrated is None and res["ok"]:
        # first market data is in: fit the simulation's market models to it right away
        await ctx.enqueue("calibrate.markets", {}, dedupe_key="calibrate:all", priority=6)
        await set_state("schedule:calibrate.markets", {"last": utcnow().isoformat()})
    # Spam protection: at most AUTO_OUTLOOKS deep analyses per refresh, most relevant first
    # (HQ and target markets, then the steepest falls). The rest only notify.
    profile = await load_profile() or {}
    focus = {(profile.get("hq_country") or "").upper(), *[t.upper() for t in profile.get("target_markets") or []]}

    def rank(alert: dict) -> tuple:
        a = alert.get("analytics") or {}
        return (alert.get("country") not in focus, min(a.get("chg_1d") or 0, (a.get("chg_5d") or 0) / 2))

    auto = 0
    for alert in sorted(res["alerts"], key=rank):
        iso = alert.get("country")
        if not iso:
            continue
        key = f"market:{iso}"
        if await recent_insight(key, config.ANALYSIS_COOLDOWN_SECONDS * 4):
            continue
        analyse = auto < AUTO_OUTLOOKS
        await runtime.hub.notify(
            "warning", f"{alert['name']} is {alert['reason']}",
            f"{country_name(iso)}'s market is falling - "
            + ("the AI is analysing what is going on and what may happen next." if analyse
               else "open the country on the globe and ask the AI for an outlook."),
            link=f"#country/{iso}", meta={"country": iso},
        )
        if analyse:
            auto += 1
            await ctx.enqueue("market.outlook", {"country": iso, "reason": f"{alert['name']} {alert['reason']}"},
                              dedupe_key=f"market.outlook:{iso}", priority=3)
    return {k: v for k, v in res.items() if k != "alerts"} | {"alerts": [a["name"] for a in res["alerts"]]}


async def job_hazards_refresh(ctx: TaskContext, payload: dict) -> dict:
    profile = await load_profile()
    hq = None
    if profile and profile.get("hq_lat") is not None:
        hq = (profile["hq_lat"], profile["hq_lon"])
    res = await hazards.refresh_hazards(hq)
    runtime.hub.publish("world_update", {"what": "hazards"})
    for event_id in res["near_hq_ids"]:
        await ctx.enqueue("risk.event_alert", {"event_id": event_id}, dedupe_key=f"riskev:{event_id}", priority=1)
    for event_id in res["significant_ids"]:
        await ctx.enqueue("geo.impact", {"event_id": event_id}, dedupe_key=f"geo:{event_id}", priority=3)
    return res


async def job_worldbank(ctx: TaskContext, payload: dict) -> dict:
    return await worldbank.refresh_country_stats()


# --------------------------------------------------------------------------- AI analyses
async def job_market_outlook(ctx: TaskContext, payload: dict) -> dict:
    return await outlook.market_outlook(payload["country"], payload.get("reason"), progress=ctx.progress)


async def job_region_analysis(ctx: TaskContext, payload: dict) -> dict:
    return await outlook.region_analysis(payload["country"], progress=ctx.progress)


async def job_geo_impact(ctx: TaskContext, payload: dict) -> dict:
    return await geo_impact.analyze_event(int(payload["event_id"]), progress=ctx.progress)


async def job_risk_scan(ctx: TaskContext, payload: dict) -> dict:
    return await risk.location_scan(force=bool(payload.get("force")), progress=ctx.progress)


async def job_risk_event(ctx: TaskContext, payload: dict) -> dict:
    return await risk.event_alert(int(payload["event_id"]), progress=ctx.progress)


async def job_personas(ctx: TaskContext, payload: dict) -> dict:
    return await personas.pulse(force=bool(payload.get("force")), progress=ctx.progress)


async def job_sim_report(ctx: TaskContext, payload: dict) -> dict:
    return await simreport.report(int(payload["run_id"]), progress=ctx.progress)


# --------------------------------------------------------------------------- simulation
async def job_calibrate_markets(ctx: TaskContext, payload: dict) -> dict:
    wanted = payload.get("symbol")
    symbols = [m["symbol"] for m in MARKETS if m.get("primary")]
    if wanted:
        symbols = [wanted]
    done = {}
    for i, sym in enumerate(symbols, start=1):
        async with SessionLocal() as session:
            quote = await session.get(MarketQuote, sym)
        closes = [p[1] for p in (quote.history if quote else [])]
        if len(closes) < 120:
            continue
        res = await asyncio.to_thread(calibrate, closes)
        async with SessionLocal() as session:
            session.add(Calibration(symbol=sym, params=res["params"], target_moments=res["target"],
                                    sim_moments=res["sim"], loss=res["loss"], observations=res["observations"]))
            await session.commit()
        done[sym] = res["loss"]
        await ctx.progress(i / len(symbols), f"calibrated {sym} (loss {res['loss']:.3f})")
    return {"calibrated": done}


def _downsample(results: dict, max_points: int = 400) -> dict:
    """Keep stored series small enough for the browser."""
    series = results.get("series") or {}
    n = len(series.get("t") or [])
    if n <= max_points:
        return results
    step = -(-n // max_points)

    def thin(value):
        if isinstance(value, list):
            return value[::step]
        if isinstance(value, dict):
            return {k: thin(v) for k, v in value.items()}
        return value

    results["series"] = thin(series)
    results["meta"]["downsampled_every"] = step
    return results


async def job_sim_run(ctx: TaskContext, payload: dict) -> dict:
    run_id = int(payload["run_id"])
    async with SessionLocal() as session:
        run = await session.get(SimulationRun, run_id)
        if run is None:
            return {"skipped": "run not found"}
        run.status = "running"
        run.task_id = ctx.task_id
        await session.commit()
        scenario = dict(run.scenario or {})
        params = dict(run.params or {})
        name = run.name
    profile = await load_profile()
    events = scenario.get("events") or []
    loop = asyncio.get_running_loop()

    def bridge(offset: float, span: float):
        def report(fraction: float, note: str) -> None:
            asyncio.run_coroutine_threadsafe(ctx.progress(offset + fraction * span, note), loop)
        return report

    async def build(evts: list[dict], label: str):
        return await build_world(profile, events=evts, consumers=params["consumers"], ticks=params["ticks"],
                                 seed=params["seed"], shards=params.get("shards", 0), name=f"{name} ({label})")

    try:
        cfg = await build(events, "scenario")
        compare = bool(scenario.get("compare_baseline"))
        span = 0.45 if compare else 0.9
        results = await asyncio.to_thread(run_simulation, cfg, bridge(0.02, span), ctx.cancelled)
        uplift = {}
        if compare:
            base_cfg = await build(baseline_events(events), "baseline")
            base = await asyncio.to_thread(run_simulation, base_cfg, bridge(0.5, 0.45), ctx.cancelled)
            uplift = compute_uplift(results, base)
            results["baseline_final"] = base["final"]
            results["baseline_series"] = {
                "global_market": base["series"]["global_market"],
                "share": {k: v for k, v in base["series"]["share"].items()},
                "global_conf": base["series"]["global_conf"],
            }
    except SimCancelled:
        await _set_run(run_id, status="cancelled", error="cancelled")
        raise TaskCancelled() from None
    except Exception as exc:
        await _set_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        raise
    results["regions_detail"] = cfg.regions
    results = _downsample(results)
    await _set_run(
        run_id, status="done", results=results, patterns=results.get("patterns", []), uplift=uplift,
        backend=results["meta"]["backend"], shards=results["meta"]["shards"], consumers=results["meta"]["agents"],
        duration_s=results["meta"]["duration_s"], finished_at=utcnow(),
    )
    runtime.hub.publish("sim", {"run_id": run_id, "status": "done"})
    await runtime.hub.notify(
        "success", f"Simulation finished: {name}",
        f"{results['meta']['agents']:,} agents, {len(results.get('patterns', []))} emergent patterns detected.",
        link=f"#sim/{run_id}",
    )
    await ctx.enqueue("sim.report", {"run_id": run_id}, dedupe_key=f"simreport:{run_id}", priority=4)
    return {"run_id": run_id, "patterns": len(results.get("patterns", [])), "duration_s": results["meta"]["duration_s"]}


async def _set_run(run_id: int, **values) -> None:
    async with SessionLocal() as session:
        await session.execute(update(SimulationRun).where(SimulationRun.id == run_id).values(**values))
        await session.commit()
    runtime.hub.publish("sim", {"run_id": run_id, "status": values.get("status")})


# --------------------------------------------------------------------------- maintenance
async def job_reembed(ctx: TaskContext, payload: dict) -> dict:
    """Re-embed rows whose vectors came from a different model (e.g. after Docker Gemma comes back)."""
    await runtime.embedder.probe()
    model = runtime.embedder.model_name
    updated = 0
    since = utcnow() - timedelta(days=7)
    targets = [
        (NewsArticle, lambda r: f"{r.title}. {r.summary or ''}", NewsArticle.fetched_at >= since),
        (AIInsight, lambda r: f"{r.title}. {r.summary or ''}", AIInsight.created_at >= since),
        (PersonaMemory, lambda r: r.content, PersonaMemory.created_at >= since),
    ]
    for model_cls, text_of, recent in targets:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(model_cls).where(recent, (model_cls.embed_model != model) | model_cls.embed_model.is_(None))
                    .order_by(desc(model_cls.id)).limit(1000)
                )
            ).scalars().all()
            for start in range(0, len(rows), 64):
                chunk = rows[start : start + 64]
                vectors, used = await runtime.embedder.embed([text_of(r) for r in chunk])
                for row, vec in zip(chunk, vectors):
                    row.embedding = vec
                    row.embed_model = used
                updated += len(chunk)
            await session.commit()
    return {"model": model, "updated": updated}


def register_jobs(queue: TaskQueue) -> None:
    queue.register("news.fetch", "io", job_news_fetch, timeout=900)
    queue.register("markets.refresh", "io", job_markets_refresh, timeout=600)
    queue.register("hazards.refresh", "io", job_hazards_refresh, timeout=600)
    queue.register("worldbank.refresh", "io", job_worldbank, timeout=600)
    queue.register("embeddings.reembed", "io", job_reembed, timeout=1800)
    # AI jobs: generous timeouts (CPU-only Gemma is slow) and a few retries for transient AI failures
    queue.register("news.enrich", "ai", job_news_enrich, timeout=4 * 3600, max_attempts=3)
    queue.register("brief.daily", "ai", job_brief_daily, timeout=2 * 3600, max_attempts=3)
    queue.register("market.outlook", "ai", job_market_outlook, timeout=3600, max_attempts=3)
    queue.register("region.analysis", "ai", job_region_analysis, timeout=3600, max_attempts=3)
    queue.register("geo.impact", "ai", job_geo_impact, timeout=3600, max_attempts=3)
    queue.register("risk.scan", "ai", job_risk_scan, timeout=1800, max_attempts=3)
    queue.register("risk.event_alert", "ai", job_risk_event, timeout=1800, max_attempts=3)
    queue.register("personas.pulse", "ai", job_personas, timeout=3600, max_attempts=3)
    queue.register("sim.report", "ai", job_sim_report, timeout=3600, max_attempts=3)
    queue.register("sim.run", "sim", job_sim_run, timeout=3 * 3600, max_attempts=1)
    queue.register("calibrate.markets", "sim", job_calibrate_markets, timeout=3600)
