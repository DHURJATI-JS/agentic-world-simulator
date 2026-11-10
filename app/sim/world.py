"""Build simulation worlds from real data stored in Postgres (calibration against reality).

- agent counts per region      <- World Bank population
- income / price sensitivity   <- World Bank GDP per capita
- starting consumer confidence <- AI-scored news sentiment + LLM persona moods
- stock markets                <- live index level + parameters calibrated to that index
- the startup                  <- onboarding profile (what it sells, where it can ship)
"""
from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import desc, func, select

import config
from app.data.geo import countries, country_name
from app.data.sources import PRIMARY_INDEX
from app.db import SessionLocal
from app.models import Calibration, CountryStat, MarketQuote, NewsArticle, PersonaMemory, utcnow
from app.sim.engine import SimConfig

DEFAULT_REGIONS = ["US", "IN", "JP", "CN", "GB", "DE", "BR"]
ESSENTIAL_WORDS = (
    "food", "grocery", "health", "medical", "medicine", "pharma", "water", "hygiene", "energy",
    "utility", "power", "logistics", "agri", "farm", "insurance", "telecom", "shelter",
)


async def region_signals(isos: list[str]) -> dict[str, dict]:
    since = utcnow() - timedelta(hours=72)
    out: dict[str, dict] = {iso: {"news_sentiment": None, "news_count": 0, "persona_mood": None} for iso in isos}
    async with SessionLocal() as session:
        for iso in isos:
            row = (
                await session.execute(
                    select(func.avg(NewsArticle.sentiment), func.count(NewsArticle.id)).where(
                        NewsArticle.fetched_at >= since, NewsArticle.countries.contains([iso])
                    )
                )
            ).one()
            out[iso]["news_sentiment"] = float(row[0]) if row[0] is not None else None
            out[iso]["news_count"] = int(row[1] or 0)
            mood = (
                await session.execute(
                    select(func.avg(PersonaMemory.mood)).where(
                        PersonaMemory.region == iso,
                        PersonaMemory.kind == "reaction",
                        PersonaMemory.created_at >= utcnow() - timedelta(hours=48),
                    )
                )
            ).scalar()
            out[iso]["persona_mood"] = float(mood) if mood is not None else None
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def pick_regions(profile: dict | None, extra: list[str]) -> list[str]:
    table = countries()
    wanted: list[str] = []
    if profile:
        wanted.append((profile.get("hq_country") or "").upper())
        wanted += [t.upper() for t in profile.get("target_markets") or []]
    wanted += [e.upper() for e in extra or []]
    wanted += DEFAULT_REGIONS
    out: list[str] = []
    for iso in wanted:
        if iso and iso in table and iso not in out:
            out.append(iso)
        if len(out) >= config.SIM_MAX_REGIONS:
            break
    return out


async def build_regions(isos: list[str]) -> list[dict]:
    table = countries()
    signals = await region_signals(isos)
    async with SessionLocal() as session:
        stats = {s.iso2: s for s in (await session.execute(select(CountryStat).where(CountryStat.iso2.in_(isos)))).scalars()}
        symbols = [PRIMARY_INDEX[i] for i in isos if i in PRIMARY_INDEX]
        quotes = {q.symbol: q for q in (await session.execute(select(MarketQuote).where(MarketQuote.symbol.in_(symbols)))).scalars()}
        calibrations: dict[str, Calibration] = {}
        for sym in symbols:
            cal = (
                await session.execute(
                    select(Calibration).where(Calibration.symbol == sym).order_by(desc(Calibration.created_at)).limit(1)
                )
            ).scalar_one_or_none()
            if cal:
                calibrations[sym] = cal

    regions = []
    for iso in isos:
        st = stats.get(iso)
        pop = (st.population if st and st.population else None) or (table[iso].pop_est if iso in table else 0) or 1e7
        gdp = (st.gdp_per_capita if st and st.gdp_per_capita else None) or 10000.0
        sig = signals.get(iso, {})
        news = sig.get("news_sentiment")
        mood = sig.get("persona_mood")
        baseline = 0.0
        if news is not None:
            baseline += 0.5 * news
        if mood is not None:
            baseline += 0.3 * mood
        sym = PRIMARY_INDEX.get(iso)
        quote = quotes.get(sym) if sym else None
        market = None
        if quote and quote.ok and quote.price:
            cal = calibrations.get(sym)
            market = {
                "symbol": sym,
                "name": quote.name,
                "p0": float(quote.price),
                "params": dict(cal.params) if cal else {},
                "calibrated": bool(cal),
            }
        regions.append(
            {
                "iso2": iso,
                "name": country_name(iso),
                "weight": float(pop) ** 0.6,
                "population": float(pop),
                "gdp_per_capita": float(gdp),
                "income_level": float(gdp) / 12000.0,
                "equity_level": _clamp(0.1 + 0.25 * math.log10(max(gdp, 300.0) / 1000.0), 0.05, 0.8),
                "baseline_conf": _clamp(baseline, -0.5, 0.5),
                "news_sentiment": news,
                "news_count": sig.get("news_count", 0),
                "persona_mood": mood,
                "market": market,
            }
        )
    return regions


def company_specs(profile: dict | None, regions: list[dict], consumers: int) -> tuple[list[dict], bool]:
    isos = [r["iso2"] for r in regions]
    R = len(isos)
    p = profile or {}
    hq = (p.get("hq_country") or "").upper()
    targets = {t.upper() for t in p.get("target_markets") or []}
    ships_to = {s.upper() for s in p.get("ships_to") or []}
    physical_only = bool(p.get("sells_physical_products")) and not (p.get("digital_only") or p.get("offers_services"))

    if physical_only:
        available = []
        for iso in isos:
            domestic = iso == hq  # sells at home (in store or by local delivery)
            international = bool(p.get("can_ship_international")) and (
                not ships_to or "WORLDWIDE" in ships_to or iso in ships_to
            )
            available.append(bool(domestic or international))
        if not any(available) and hq in isos:
            available[isos.index(hq)] = True
    else:
        available = [True] * R

    alloc = [
        (3.0 if iso in targets else 2.0 if iso == hq else 0.5) if available[i] else 0.0
        for i, iso in enumerate(isos)
    ]
    init_aw = [0.08 if iso == hq else 0.04 if iso in targets else 0.01 for iso in isos]
    init_share = [
        (0.02 if iso == hq else 0.006 if iso in targets else 0.001) if available[i] else 0.0
        for i, iso in enumerate(isos)
    ]
    capacity = consumers / 7.0 * 0.45 * 0.12 if p.get("sells_physical_products") else None
    text = " ".join([p.get("sector", ""), p.get("description", ""), " ".join(p.get("offerings") or [])]).lower()
    essential = any(word in text for word in ESSENTIAL_WORDS)

    startup = {
        "name": p.get("name") or "Your startup",
        "is_startup": True,
        "price": 1.0,
        "quality": 0.62,
        "unit_cost": 0.55 if p.get("sells_physical_products") else 0.35,
        "marketing": 0.8,
        "alloc": alloc,
        "available": available,
        "capacity": capacity,
        "supply_regions": [isos.index(hq)] if (hq in isos and p.get("sells_physical_products")) else [],
        "adaptive": False,
        "init_awareness": init_aw,
        "init_share": init_share,
    }
    competitors = [
        {"name": "Incumbent", "price": 1.15, "quality": 0.65, "marketing": 1.5, "unit_cost": 0.6,
         "init_awareness": [0.55] * R, "init_share": [0.35] * R, "adaptive": False},
        {"name": "Challenger", "price": 0.95, "quality": 0.55, "marketing": 1.0, "unit_cost": 0.55,
         "init_awareness": [0.30] * R, "init_share": [0.15] * R, "adaptive": True},
        {"name": "Budget", "price": 0.75, "quality": 0.40, "marketing": 0.6, "unit_cost": 0.45,
         "init_awareness": [0.25] * R, "init_share": [0.12] * R, "adaptive": True},
    ]
    if startup["name"] in {c["name"] for c in competitors}:
        startup["name"] += " (you)"
    return [startup] + competitors, essential


async def build_world(
    profile: dict | None,
    *,
    events: list[dict],
    consumers: int,
    ticks: int,
    seed: int,
    shards: int = 0,
    extra_regions: list[str] | None = None,
    name: str = "simulation",
    traders_per_market: int | None = None,
) -> SimConfig:
    event_regions: list[str] = []
    for e in events:
        reg = e.get("region")
        if isinstance(reg, list):
            event_regions += [str(r) for r in reg]
        elif reg and str(reg).upper() not in ("ALL", "GLOBAL"):
            event_regions.append(str(reg))
    isos = pick_regions(profile, (extra_regions or []) + event_regions)
    regions = await build_regions(isos)
    companies, essential = company_specs(profile, regions, consumers)
    return SimConfig(
        regions=regions,
        companies=companies,
        events=events,
        consumers=int(consumers),
        ticks=int(ticks),
        shards=int(shards or config.SIM_SHARDS),
        seed=int(seed),
        essential=essential,
        use_processes=config.SIM_USE_PROCESSES,
        traders_per_market=int(traders_per_market or config.SIM_TRADERS_PER_MARKET),
        name=name,
    )


__all__ = ["build_world", "build_regions", "company_specs", "pick_regions", "region_signals"]
