"""
Market outlook ("the Indian market is down - what happens next?") and on-demand region analysis.

Quantitative grounding passed to the AI:
  * Monte Carlo (filtered historical simulation) on the real index
  * an agent-based forecast: several runs of the calibrated world model
  * headlines retrieved with pgvector + persona agents' reactions
The AI writes the narrative and bear/base/bull scenarios consistent with those numbers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import numpy as np
from sqlalchemy import desc, select

from app.ai.base import AIRequest
from app.ai.prompts import ANALYST_SYSTEM, OUTLOOK_SCHEMA, REGION_SCHEMA, clamp, profile_block
from app.data.geo import country_name
from app.data.markets import monte_carlo
from app.data.sources import FX_BY_COUNTRY, PRIMARY_INDEX
from app.db import SessionLocal
from app.intel.common import load_profile, market_line, news_lines, retrieve_news, save_insight
from app.models import CountryStat, GeoEvent, MarketQuote, PersonaMemory, utcnow
from app.runtime import runtime
from app.sim.engine import run_simulation
from app.sim.world import build_world

log = logging.getLogger("worldsim.outlook")


async def _quote(symbol: str | None) -> dict | None:
    if not symbol:
        return None
    async with SessionLocal() as session:
        row = await session.get(MarketQuote, symbol)
        return row.to_dict(with_history=True) if row and row.ok else None


async def persona_reactions(region: str, limit: int = 3) -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PersonaMemory)
                .where(PersonaMemory.region == region, PersonaMemory.kind == "reaction",
                       PersonaMemory.created_at >= utcnow() - timedelta(days=2))
                .order_by(desc(PersonaMemory.created_at))
                .limit(limit)
            )
        ).scalars().all()
    return [r.to_dict() for r in rows]


def _persona_text(reactions: list[dict]) -> str:
    if not reactions:
        return "- (no persona reactions recorded yet)"
    return "\n".join(
        f"- {r['data'].get('persona', r['persona_key'])}: mood {r['mood']:+.2f}; \"{r['content'][:220]}\""
        for r in reactions
        if r.get("mood") is not None
    )


async def abm_forecast(profile: dict | None, iso2: str, horizon: int = 20, runs: int = 6) -> dict | None:
    """Run the calibrated agent-based world several times and read off this market's distribution."""
    cfg = await build_world(profile, events=[], consumers=30_000, ticks=horizon, seed=1, shards=1,
                            extra_regions=[iso2], traders_per_market=8000, name=f"forecast {iso2}")
    cfg.use_processes = False
    region = next((r for r in cfg.regions if r["iso2"] == iso2), None)
    if not region or not region.get("market"):
        return None
    name = region["name"]

    def run_all() -> list[tuple]:
        out = []
        for s in range(runs):
            cfg.seed = 100 + s
            res = run_simulation(cfg)
            mk = res["final"]["markets"].get(name)
            out.append((
                mk["change_pct"] if mk else None,
                mk["max_drawdown_pct"] if mk else None,
                max(res["series"]["panic"][name]),
                res["series"]["conf"][name][-1],
            ))
        return out

    outs = await asyncio.to_thread(run_all)
    changes = np.array([o[0] for o in outs if o[0] is not None], dtype=float)
    if changes.size == 0:
        return None
    return {
        "runs": runs,
        "horizon_days": horizon,
        "median_change_pct": round(float(np.median(changes)), 2),
        "p10_change_pct": round(float(np.percentile(changes, 10)), 2),
        "p90_change_pct": round(float(np.percentile(changes, 90)), 2),
        "worst_drawdown_pct": round(float(min(o[1] for o in outs if o[1] is not None)), 2),
        "consumer_panic_peak": round(float(np.mean([o[2] for o in outs])), 3),
        "consumer_confidence_end": round(float(np.mean([o[3] for o in outs])), 3),
        "calibrated": bool(region["market"].get("calibrated")),
        "baseline_confidence": round(region["baseline_conf"], 3),
    }


def _mc_text(mc: dict | None) -> str:
    if not mc:
        return "not enough price history"
    return (
        f"{mc['horizon_days']} trading days, {mc['paths']} bootstrap paths scaled to current volatility "
        f"({mc['daily_vol_pct']}%/day): median {mc['median_pct']:+.2f}%, P(fall >5%)={mc['prob_down_5']:.0%}, "
        f"P(fall >10%)={mc['prob_down_10']:.0%}, P(rise >5%)={mc['prob_up_5']:.0%}, "
        f"5th-95th percentile level {mc['p5'][-1]:.2f} .. {mc['p95'][-1]:.2f}"
    )


def _abm_text(abm: dict | None) -> str:
    if not abm:
        return "not available (no market model for this region)"
    return (
        f"{abm['runs']} runs over {abm['horizon_days']} days (calibrated to the real index: {abm['calibrated']}): "
        f"median {abm['median_change_pct']:+.2f}%, p10..p90 {abm['p10_change_pct']:+.2f}%..{abm['p90_change_pct']:+.2f}%, "
        f"worst drawdown {abm['worst_drawdown_pct']:.1f}%, consumer panic peak {abm['consumer_panic_peak']:.0%}, "
        f"consumer confidence ends at {abm['consumer_confidence_end']:+.2f} (starts {abm['baseline_confidence']:+.2f})"
    )


def _normalise_scenarios(data: dict) -> None:
    scenarios = [s for s in data.get("scenarios") or [] if isinstance(s, dict)]
    total = sum(clamp(s.get("probability"), 0, 1) for s in scenarios)
    for s in scenarios:
        p = clamp(s.get("probability"), 0, 1)
        s["probability"] = round(p / total, 3) if total > 0 else round(1 / max(1, len(scenarios)), 3)
        s["expected_move_pct"] = clamp(s.get("expected_move_pct"), -60, 60)
    data["scenarios"] = scenarios
    data["confidence"] = clamp(data.get("confidence"), 0, 1, 0.5)


async def market_outlook(iso2: str, reason: str | None = None, progress=None) -> dict:
    iso2 = iso2.upper()
    profile = await load_profile()
    name = country_name(iso2)
    index = await _quote(PRIMARY_INDEX.get(iso2))
    fx = await _quote(FX_BY_COUNTRY.get(iso2))
    vol = await _quote({"IN": "^INDIAVIX", "US": "^VIX"}.get(iso2))
    if progress:
        await progress(0.1, "Monte Carlo on the real index")
    mc = monte_carlo(index["history"]) if index else None
    if progress:
        await progress(0.25, "agent-based forecast (calibrated world model)")
    try:
        abm = await abm_forecast(profile, iso2)
    except Exception as exc:  # noqa: BLE001
        log.warning("ABM forecast failed: %s", exc)
        abm = None
    if progress:
        await progress(0.5, "retrieving relevant headlines")
    index_name = index["name"] if index else "stock market"
    articles = await retrieve_news(f"{name} {index_name} stock market economy investors outlook", countries=[iso2], limit=18)
    reactions = await persona_reactions(iso2)
    prompt = (
        f"{profile_block(profile)}\n\n"
        f"MARKET: {name}\n- index: {market_line(index)}\n- currency: {market_line(fx)}\n- volatility gauge: {market_line(vol)}\n"
        f"TRIGGER: {reason or 'requested analysis'}\n\n"
        f"MONTE CARLO: {_mc_text(mc)}\n"
        f"AGENT-BASED SIMULATION: {_abm_text(abm)}\n\n"
        f"HEADLINES (reliable outlets, last 72h, most relevant first):\n{news_lines(articles)}\n\n"
        f"HOW PEOPLE ARE REACTING (persona agents):\n{_persona_text(reactions)}\n\n"
        f"Explain what is going on in {name}'s market and why (use the headlines), how consumers and investors are "
        "reacting, and what is likely over the next ~20 trading days as bear/base/bull scenarios whose probabilities "
        "sum to 1 and stay consistent with the two quantitative models. Then the impact on the startup and 3-5 "
        "recommendations it can act on."
    )
    if progress:
        await progress(0.6, "AI analysis")
    data, result = await runtime.ai.generate_json(
        AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose="market_outlook", schema=OUTLOOK_SCHEMA,
                  schema_name="market_outlook", depth="deep"),
        validator=lambda d: isinstance(d, dict) and d.get("headline"),
    )
    _normalise_scenarios(data)
    a = (index or {}).get("analytics") or {}
    severity = "info"
    if (a.get("chg_1d") or 0) <= -3 or (a.get("drawdown_pct") or 0) <= -20:
        severity = "critical"
    elif reason or (a.get("chg_1d") or 0) <= -1.5 or (a.get("drawdown_pct") or 0) <= -10:
        severity = "warning"
    data["quant"] = {"monte_carlo": mc, "abm": abm, "index": {k: index.get(k) for k in ("symbol", "name", "analytics")} if index else None,
                     "fx": {k: fx.get(k) for k in ("symbol", "name", "analytics")} if fx else None}
    data["sources"] = [{"title": x["title"], "outlet": x["outlet"], "url": x["url"]} for x in articles[:10]]
    data["trigger"] = reason
    insight = await save_insight(
        kind="market_outlook",
        title=data.get("headline") or f"{name} market outlook",
        summary=data.get("situation") or "",
        content=data,
        recommendations=data.get("recommendations"),
        region=iso2,
        severity=severity,
        result=result,
        dedupe_key=f"market:{iso2}",
        profile=profile,
    )
    return {"insight_id": insight["id"], "provider": result.provider_label}


async def region_analysis(iso2: str, progress=None) -> dict:
    iso2 = iso2.upper()
    profile = await load_profile()
    name = country_name(iso2)
    index = await _quote(PRIMARY_INDEX.get(iso2))
    fx = await _quote(FX_BY_COUNTRY.get(iso2))
    mc = monte_carlo(index["history"]) if index else None
    abm = None
    if index:
        if progress:
            await progress(0.2, "agent-based forecast")
        try:
            abm = await abm_forecast(profile, iso2)
        except Exception as exc:  # noqa: BLE001
            log.warning("ABM forecast failed: %s", exc)
    async with SessionLocal() as session:
        stat = await session.get(CountryStat, iso2)
        hazards = (
            await session.execute(
                select(GeoEvent)
                .where(GeoEvent.country == iso2, GeoEvent.occurred_at >= utcnow() - timedelta(days=10))
                .order_by(desc(GeoEvent.severity))
                .limit(6)
            )
        ).scalars().all()
    if progress:
        await progress(0.45, "retrieving headlines")
    articles = await retrieve_news(f"{name} economy politics business consumers latest", countries=[iso2], limit=20)
    reactions = await persona_reactions(iso2)
    stats_text = (
        f"population {stat.population:,.0f}, GDP per capita ${stat.gdp_per_capita:,.0f}, inflation {stat.inflation:.1f}% ({stat.year})"
        if stat and stat.population and stat.gdp_per_capita and stat.inflation is not None
        else "World Bank data not loaded yet"
    )
    hazard_text = "\n".join(
        f"- {h.kind}: {h.title}; alert={h.alert_level or 'n/a'}; severity={h.severity:.2f}" for h in hazards
    ) or "- none in the last 10 days"
    prompt = (
        f"{profile_block(profile)}\n\n"
        f"REGION: {name} ({iso2}) - {stats_text}\n"
        f"- index: {market_line(index)}\n- currency: {market_line(fx)}\n"
        f"MONTE CARLO: {_mc_text(mc)}\nAGENT-BASED SIMULATION: {_abm_text(abm)}\n\n"
        f"NATURAL HAZARDS:\n{hazard_text}\n\n"
        f"HEADLINES (last 72h):\n{news_lines(articles)}\n\n"
        f"HOW PEOPLE ARE REACTING (persona agents):\n{_persona_text(reactions)}\n\n"
        f"Give a concise situation report on {name}: key stories, drivers, consumer and investor reaction, "
        "bear/base/bull outlook (probabilities sum to 1, consistent with the models), risks, opportunities, "
        "the impact on the startup and 3-5 recommendations it can act on."
    )
    if progress:
        await progress(0.6, "AI analysis")
    data, result = await runtime.ai.generate_json(
        AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose="region_analysis", schema=REGION_SCHEMA,
                  schema_name="region_analysis", depth="deep"),
        validator=lambda d: isinstance(d, dict) and d.get("headline"),
    )
    _normalise_scenarios(data)
    data["quant"] = {"monte_carlo": mc, "abm": abm}
    data["sources"] = [{"title": x["title"], "outlet": x["outlet"], "url": x["url"]} for x in articles[:10]]
    insight = await save_insight(
        kind="region_analysis",
        title=data.get("headline") or f"{name} situation report",
        summary=data.get("situation") or "",
        content=data,
        recommendations=data.get("recommendations"),
        region=iso2,
        severity="info",
        result=result,
        dedupe_key=f"region:{iso2}",
        profile=profile,
    )
    return {"insight_id": insight["id"], "provider": result.provider_label}


__all__ = ["abm_forecast", "market_outlook", "persona_reactions", "region_analysis"]
