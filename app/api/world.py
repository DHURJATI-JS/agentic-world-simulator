"""World data for the globe: country overview, news, markets, hazards, country drill-down."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select, text

import config
from app.data.geo import countries, country_name
from app.data.markets import monte_carlo
from app.data.sources import PRIMARY_INDEX
from app.db import SessionLocal
from app.intel.common import get_state, load_profile
from app.intel.outlook import persona_reactions
from app.models import (
    AIInsight,
    Calibration,
    CountryStat,
    GeoEvent,
    MarketQuote,
    NewsArticle,
    SimulationRun,
    utcnow,
)
from app.runtime import runtime
from app.security import ai_rate_limit, require_user

router = APIRouter(prefix="/api/world", tags=["world"])


@router.get("/overview")
async def overview(user: str = Depends(require_user)) -> dict:
    table = countries()
    out: dict[str, dict] = {}

    def entry(iso: str) -> dict:
        if iso not in out:
            c = table.get(iso)
            out[iso] = {"name": c.name if c else iso, "lat": c.lat if c else None, "lon": c.lon if c else None}
        return out[iso]

    async with SessionLocal() as session:
        quotes = (await session.execute(select(MarketQuote).where(MarketQuote.ok.is_(True)))).scalars().all()
        for q in quotes:
            if q.country and q.kind == "index" and PRIMARY_INDEX.get(q.country) == q.symbol:
                a = q.analytics or {}
                entry(q.country)["market"] = {
                    "symbol": q.symbol, "name": q.name, "price": q.price, "chg_1d": a.get("chg_1d"),
                    "chg_1m": a.get("chg_1m"), "drawdown_pct": a.get("drawdown_pct"), "regime": a.get("regime"),
                }
        rows = (
            await session.execute(
                text(
                    "SELECT c AS iso, count(*) AS n, avg(sentiment) AS s FROM news_articles, "
                    "jsonb_array_elements_text(countries) AS c WHERE fetched_at >= :since GROUP BY c"
                ),
                {"since": utcnow() - timedelta(hours=48)},
            )
        ).all()
        for iso, n, s in rows:
            if iso in table:
                e = entry(iso)
                e["news_count"] = int(n)
                e["news_sentiment"] = round(float(s), 3) if s is not None else None
        hz = (
            await session.execute(
                select(GeoEvent.country, func.count(GeoEvent.id), func.max(GeoEvent.severity))
                .where(GeoEvent.occurred_at >= utcnow() - timedelta(days=7), GeoEvent.country.is_not(None))
                .group_by(GeoEvent.country)
            )
        ).all()
        for iso, n, sev in hz:
            e = entry(iso)
            e["hazards"] = int(n)
            e["max_severity"] = float(sev or 0)
        ins = (
            await session.execute(
                select(AIInsight.region, func.count(AIInsight.id), func.max(AIInsight.created_at))
                .where(AIInsight.region.is_not(None), AIInsight.created_at >= utcnow() - timedelta(days=7))
                .group_by(AIInsight.region)
            )
        ).all()
        for iso, n, last in ins:
            if iso in table:
                e = entry(iso)
                e["insights"] = int(n)
                e["last_insight_at"] = last.isoformat() if last else None
        run = (
            await session.execute(
                select(SimulationRun).where(SimulationRun.status == "done").order_by(desc(SimulationRun.id)).limit(1)
            )
        ).scalar_one_or_none()
        geo_insights = (
            await session.execute(
                select(AIInsight)
                .where(AIInsight.kind == "geo_impact", AIInsight.created_at >= utcnow() - timedelta(days=5))
                .order_by(desc(AIInsight.created_at))
                .limit(12)
            )
        ).scalars().all()

    sim = None
    if run and run.results:
        series = run.results.get("series", {})
        regions = run.results.get("meta", {}).get("regions", [])
        conf = {r["iso2"]: (series.get("conf", {}).get(r["name"]) or [None])[-1] for r in regions}
        for iso, value in conf.items():
            if value is not None and iso in table:
                entry(iso)["sim_conf"] = value
        sim = {"run_id": run.id, "name": run.name, "finished_at": run.finished_at.isoformat() if run.finished_at else None}

    profile = await load_profile()
    hq = None
    if profile and profile.get("hq_lat") is not None:
        hq = {"lat": profile["hq_lat"], "lon": profile["hq_lon"], "city": profile.get("hq_city"),
              "country": profile.get("hq_country"), "name": profile.get("name")}
    opportunities = []
    for g in geo_insights:
        fit = (g.content or {}).get("fit")
        ev = (g.content or {}).get("event") or {}
        if fit in ("ship_supplies", "offer_services") and ev.get("lat") is not None:
            opportunities.append({"insight_id": g.id, "lat": ev["lat"], "lon": ev["lon"], "fit": fit,
                                  "title": g.title, "region": g.region})
    return {
        "countries": out,
        "hq": hq,
        "opportunities": opportunities,
        "simulation": sim,
        "latest_news": await get_state("latest_news_batch"),
        "latest_brief": await get_state("latest_brief"),
    }


@router.get("/news")
async def list_news(
    country: str | None = None,
    category: str | None = None,
    q: str | None = Query(None, max_length=300),
    hours: int = Query(72, ge=1, le=24 * 30),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: str = Depends(require_user),
) -> dict:
    since = utcnow() - timedelta(hours=hours)
    stmt = select(NewsArticle).where(NewsArticle.fetched_at >= since)
    if country:
        stmt = stmt.where(NewsArticle.countries.contains([country.upper()]))
    if category:
        stmt = stmt.where(NewsArticle.category == category)
    mode = "recent"
    if q:
        vector, model = await runtime.embedder.embed_one(q)
        stmt = stmt.where(NewsArticle.embed_model == model, NewsArticle.embedding.is_not(None)).order_by(
            NewsArticle.embedding.cosine_distance(vector)
        )
        mode = f"semantic ({model})"
    else:
        stmt = stmt.order_by(desc(NewsArticle.published_at).nulls_last(), desc(NewsArticle.id))
    async with SessionLocal() as session:
        rows = (await session.execute(stmt.offset(offset).limit(limit))).scalars().all()
    return {"items": [r.to_dict() for r in rows], "mode": mode}


@router.get("/markets")
async def list_markets(user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        rows = (await session.execute(select(MarketQuote).order_by(MarketQuote.kind, MarketQuote.symbol))).scalars().all()
    return {"items": [r.to_dict() for r in rows]}


@router.get("/markets/{symbol}")
async def market_detail(symbol: str, user: str = Depends(require_user)) -> dict:
    async with SessionLocal() as session:
        quote = await session.get(MarketQuote, symbol)
        if quote is None:
            raise HTTPException(404, "unknown symbol")
        cal = (
            await session.execute(
                select(Calibration).where(Calibration.symbol == symbol).order_by(desc(Calibration.created_at)).limit(1)
            )
        ).scalar_one_or_none()
        outlook = None
        if quote.country:
            outlook = (
                await session.execute(
                    select(AIInsight)
                    .where(AIInsight.kind == "market_outlook", AIInsight.region == quote.country)
                    .order_by(desc(AIInsight.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
    data = quote.to_dict(with_history=True)
    data["monte_carlo"] = monte_carlo(quote.history or []) if quote.ok else None
    data["calibration"] = cal.to_dict() if cal else None
    data["outlook"] = outlook.to_dict(full=False) if outlook else None
    return data


@router.get("/hazards")
async def list_hazards(
    days: int = Query(7, ge=1, le=60),
    min_severity: float = Query(0.0, ge=0, le=1),
    user: str = Depends(require_user),
) -> dict:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(GeoEvent)
                .where(GeoEvent.occurred_at >= utcnow() - timedelta(days=days), GeoEvent.severity >= min_severity)
                .order_by(desc(GeoEvent.occurred_at))
                .limit(400)
            )
        ).scalars().all()
        insight_rows = (
            await session.execute(
                select(AIInsight.related_event_id, AIInsight.id).where(AIInsight.related_event_id.is_not(None))
            )
        ).all()
    insight_map: dict[int, int] = {}
    for event_id, insight_id in insight_rows:
        insight_map[event_id] = max(insight_id, insight_map.get(event_id, 0))
    return {"items": [r.to_dict() | {"insight_id": insight_map.get(r.id)} for r in rows]}


@router.get("/country/{iso2}")
async def country_detail(iso2: str, user: str = Depends(require_user)) -> dict:
    iso2 = iso2.upper()
    c = countries().get(iso2)
    if c is None:
        raise HTTPException(404, "unknown country")
    async with SessionLocal() as session:
        stat = await session.get(CountryStat, iso2)
        quotes = (await session.execute(select(MarketQuote).where(MarketQuote.country == iso2))).scalars().all()
        news_rows = (
            await session.execute(
                select(NewsArticle)
                .where(NewsArticle.countries.contains([iso2]), NewsArticle.fetched_at >= utcnow() - timedelta(days=3))
                .order_by(desc(NewsArticle.importance).nulls_last(), desc(NewsArticle.published_at).nulls_last())
                .limit(15)
            )
        ).scalars().all()
        events = (
            await session.execute(
                select(GeoEvent)
                .where(GeoEvent.country == iso2, GeoEvent.occurred_at >= utcnow() - timedelta(days=14))
                .order_by(desc(GeoEvent.severity))
                .limit(10)
            )
        ).scalars().all()
        insights = (
            await session.execute(
                select(AIInsight).where(AIInsight.region == iso2).order_by(desc(AIInsight.created_at)).limit(10)
            )
        ).scalars().all()
    sentiments = [n.sentiment for n in news_rows if n.sentiment is not None]
    return {
        "iso2": iso2,
        "name": c.name,
        "continent": c.continent,
        "subregion": c.subregion,
        "stats": stat.to_dict() if stat else None,
        "markets": [q.to_dict() for q in quotes],
        "news": [n.to_dict() for n in news_rows],
        "news_sentiment": round(sum(sentiments) / len(sentiments), 3) if sentiments else None,
        "hazards": [e.to_dict() for e in events],
        "insights": [i.to_dict(full=False) for i in insights],
        "personas": await persona_reactions(iso2, limit=4),
    }


@router.post("/refresh/{what}")
async def refresh(what: str, user: str = Depends(ai_rate_limit)) -> dict:
    if what == "news":
        state = await get_state("schedule:news.fetch")
        if state.get("last"):
            last = datetime.fromisoformat(state["last"])
            next_at = last + timedelta(seconds=config.NEWS_REFRESH_SECONDS)
            if utcnow() < next_at:
                raise HTTPException(
                    409, f"Headlines are collected once every 24 hours. Next collection: {next_at.isoformat()}"
                )
    kinds = {"news": "news.fetch", "markets": "markets.refresh", "hazards": "hazards.refresh"}
    kind = kinds.get(what)
    if kind is None:
        raise HTTPException(404, "unknown refresh target")
    task, created = await runtime.tasks.enqueue(kind, {}, dedupe_key=f"sched:{kind}", priority=2, requested_by=user)
    return {"task": task, "created": created}
