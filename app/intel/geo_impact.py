"""
Geographic impact of a natural hazard (e.g. "Japan was hit by an earthquake"):
market & currency reaction, what people need, supply-chain risks, and whether / how
the startup can help - with a simulated estimate of the attention and reputation
effect (helping genuinely vs. promotionally vs. not helping, same random seed).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update

from app.ai.base import AIRequest
from app.ai.prompts import ANALYST_SYSTEM, GEO_SCHEMA, profile_block
from app.data.geo import country_name
from app.data.sources import FX_BY_COUNTRY, PRIMARY_INDEX
from app.db import SessionLocal
from app.intel.common import load_profile, market_line, news_lines, retrieve_news, save_insight
from app.intel.outlook import _quote
from app.models import CountryStat, GeoEvent
from app.runtime import runtime
from app.sim.engine import compute_uplift, run_simulation
from app.sim.scenarios import OFFERING_BY_FIT, relief_fit
from app.sim.world import build_world

log = logging.getLogger("worldsim.geo_impact")

FIT_TEXT = {
    "ship_supplies": "CAN ship physical relief supplies to this country",
    "offer_services": "cannot ship goods there, but CAN offer its services (e.g. free access, pro-bono help)",
    "insights_only": "cannot ship products: give it market insights only; helping means donating or partnering with NGOs",
}


def move_since(quote: dict | None, when: datetime) -> float | None:
    if not quote or not quote.get("history"):
        return None
    ts = int(when.timestamp())
    before = [p for p in quote["history"] if p[0] < ts]
    after = [p for p in quote["history"] if p[0] >= ts]
    if not before or not after:
        return None
    return round((after[-1][1] / before[-1][1] - 1) * 100, 2)


async def simulate_help(profile: dict | None, iso2: str, severity: float, kind: str, fit: str) -> dict | None:
    disaster = {"type": "disaster", "region": iso2, "severity": max(0.2, min(1.0, severity)), "start": 5,
                "duration": 35, "kind": kind}
    campaign = {"type": "csr_campaign", "company": "startup", "region": iso2, "offering": OFFERING_BY_FIT[fit],
                "budget": 1.0, "start": 8, "duration": 20, "treatment": True}
    variants = {
        "baseline": [disaster],
        "genuine": [disaster, {**campaign, "style": "genuine"}],
        "promotional": [disaster, {**campaign, "style": "promotional"}],
    }
    configs = {}
    for label, events in variants.items():
        cfg = await build_world(profile, events=events, consumers=40_000, ticks=60, seed=21, shards=1,
                                extra_regions=[iso2], traders_per_market=8000, name=f"help {iso2} {label}")
        cfg.use_processes = False
        configs[label] = cfg

    def run_all() -> dict:
        return {label: run_simulation(cfg) for label, cfg in configs.items()}

    results = await asyncio.to_thread(run_all)
    base = results["baseline"]
    region_name = country_name(iso2)
    out = {}
    for label in ("genuine", "promotional"):
        up = compute_uplift(results[label], base)
        local = up.get("by_region", {}).get(region_name, {})
        out[label] = {
            "awareness_pp_region": local.get("awareness_pp"),
            "share_pp_region": local.get("share_pp"),
            "reputation_delta_region": local.get("reputation_delta"),
            "awareness_pp_global": up.get("awareness_pp"),
            "revenue_pct": up.get("revenue_pct"),
            "roi": up.get("roi"),
            "campaign_spend": up.get("campaign_spend"),
        }
    panic = base["series"]["panic"].get(region_name) or [0]
    out["consumer_panic_peak"] = round(max(panic), 3)
    market = base["final"]["markets"].get(region_name)
    out["simulated_market_drawdown_pct"] = round(market["max_drawdown_pct"], 2) if market else None
    out["offering"] = OFFERING_BY_FIT[fit]
    return out


def _sim_text(sim: dict | None) -> str:
    if not sim:
        return "simulation unavailable"
    g, p = sim["genuine"], sim["promotional"]
    return (
        f"Helping via '{sim['offering']}' (budget = 1 month of marketing, 20 days), 40k agents, same seed as not helping:\n"
        f"- genuine help: awareness in region {g['awareness_pp_region']:+.1f}pp, customer share there "
        f"{g['share_pp_region']:+.2f}pp, reputation {g['reputation_delta_region']:+.3f}, global awareness "
        f"{g['awareness_pp_global']:+.2f}pp, revenue {g['revenue_pct']:+.1f}%, ROI {g.get('roi')}\n"
        f"- promotional/branded help: awareness {p['awareness_pp_region']:+.1f}pp, reputation "
        f"{p['reputation_delta_region']:+.3f}, revenue {p['revenue_pct']:+.1f}%, ROI {p.get('roi')}\n"
        f"- simulated local consumer panic peak {sim['consumer_panic_peak']:.0%}, simulated market drawdown "
        f"{sim['simulated_market_drawdown_pct']}%"
    )


async def analyze_event(event_id: int, progress=None) -> dict:
    async with SessionLocal() as session:
        event = await session.get(GeoEvent, event_id)
        if event is None:
            return {"skipped": "event not found"}
        ev = event.to_dict()
        stat = await session.get(CountryStat, event.country) if event.country else None
    iso2 = ev["country"]
    if not iso2:
        await _mark(event_id)
        return {"skipped": "event is not near any country"}
    profile = await load_profile()
    name = country_name(iso2)
    fit = relief_fit(profile, iso2)
    occurred = datetime.fromisoformat(ev["occurred_at"]).astimezone(timezone.utc)
    index = await _quote(PRIMARY_INDEX.get(iso2))
    fx = await _quote(FX_BY_COUNTRY.get(iso2))
    idx_move, fx_move = move_since(index, occurred), move_since(fx, occurred)

    if progress:
        await progress(0.15, "simulating the effect of helping vs not helping")
    try:
        sim = await simulate_help(profile, iso2, ev["severity"], ev["kind"], fit)
    except Exception as exc:  # noqa: BLE001
        log.warning("help simulation failed: %s", exc)
        sim = None
    if progress:
        await progress(0.5, "retrieving headlines about the event")
    articles = await retrieve_news(f"{ev['title']} {name} {ev['kind']} damage casualties relief aid", countries=[iso2],
                                   hours=96, limit=15)
    stats = (
        f"population {stat.population:,.0f}, GDP per capita ${stat.gdp_per_capita:,.0f}"
        if stat and stat.population and stat.gdp_per_capita else "no statistics"
    )
    details = (
        f"{ev['kind']} - {ev['title']}; place: {ev['place'] or name}; magnitude: {ev['magnitude']}; depth: {ev['depth_km']} km; "
        f"alert level: {ev['alert_level'] or 'n/a'}; tsunami flag: {ev['tsunami']}; severity score: {ev['severity']:.2f}; "
        f"time (UTC): {ev['occurred_at']}; source: {ev['source']}"
    )
    distance = f"{ev['near_hq_km']:.0f} km from the startup HQ" if ev.get("near_hq_km") is not None else "distance to HQ unknown"
    prompt = (
        f"{profile_block(profile)}\n\n"
        f"EVENT: {details}\n{distance}\n"
        f"COUNTRY: {name} ({iso2}) - {stats}\n"
        f"LOCAL MARKETS: index {market_line(index)}; move since event {idx_move if idx_move is not None else 'n/a'}%\n"
        f"currency {market_line(fx)}; move since event {fx_move if fx_move is not None else 'n/a'}%\n\n"
        f"HEADLINES:\n{news_lines(articles)}\n\n"
        f"WHAT THIS STARTUP CAN DO: it {FIT_TEXT[fit]}.\n"
        f"SIMULATED EFFECT OF HELPING (agent-based model):\n{_sim_text(sim)}\n\n"
        "Assess: what happened and the human impact; the products and services people there need most now; "
        "the likely effect on local markets, currency and sectors (and for how long); supply-chain risks; "
        "whether and how this startup can genuinely help and how much attention/reputation that may bring "
        "(use the simulation; say plainly if it does not pay back), an ethics note about not exploiting a "
        "disaster, and 3-5 recommendations within the startup's capabilities."
    )
    if progress:
        await progress(0.6, "AI analysis")
    data, result = await runtime.ai.generate_json(
        AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose="geo_impact", schema=GEO_SCHEMA,
                  schema_name="geo_impact", depth="deep"),
        validator=lambda d: isinstance(d, dict) and d.get("headline"),
    )
    data["event"] = ev
    data["fit"] = fit
    data["simulation"] = sim
    data["market_moves"] = {"index": idx_move, "fx": fx_move,
                            "index_name": index["name"] if index else None, "fx_name": fx["name"] if fx else None}
    data["sources"] = [{"title": x["title"], "outlet": x["outlet"], "url": x["url"]} for x in articles[:10]]
    severity = "critical" if ev["severity"] >= 0.8 or ev["alert_level"] == "red" else "warning"
    insight = await save_insight(
        kind="geo_impact",
        title=data.get("headline") or f"{ev['kind'].title()} in {name}",
        summary=data.get("summary") or "",
        content=data,
        recommendations=data.get("recommendations"),
        region=iso2,
        severity=severity,
        result=result,
        dedupe_key=f"geo:{event_id}",
        related_event_id=event_id,
        profile=profile,
    )
    await _mark(event_id)
    return {"insight_id": insight["id"], "provider": result.provider_label, "fit": fit}


async def _mark(event_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(update(GeoEvent).where(GeoEvent.id == event_id).values(analyzed=True))
        await session.commit()
