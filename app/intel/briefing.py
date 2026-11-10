"""Headline enrichment (classification) and the daily global brief (map-reduce summarisation)."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import desc, select, update

import config
from app.ai.base import AIRequest
from app.ai.prompts import (
    ANALYST_SYSTEM,
    BRIEF_SCHEMA,
    CATEGORIES,
    CLUSTER_SCHEMA,
    ENRICH_SCHEMA,
    clamp,
    profile_block,
)
from app.data.geo import countries as country_table
from app.data.geo import tag_countries
from app.data.markets import load_quotes
from app.db import SessionLocal
from app.models import GeoEvent, NewsArticle, NewsBatch, utcnow
from app.intel.common import load_profile, market_line, save_insight, set_state
from app.runtime import runtime

log = logging.getLogger("worldsim.brief")


async def enrich_batch(batch_id: int | None, progress=None) -> dict:
    """Let the AI tag recent headlines with countries, category, sentiment, importance, market relevance."""
    valid = set(country_table())
    async with SessionLocal() as session:
        stmt = select(NewsArticle).where(NewsArticle.category.is_(None))
        if batch_id:
            stmt = stmt.where(NewsArticle.batch_id == batch_id)
        else:
            stmt = stmt.where(NewsArticle.fetched_at >= utcnow() - timedelta(days=2))
        stmt = stmt.order_by(desc(NewsArticle.published_at).nulls_last()).limit(config.NEWS_ENRICH_MAX)
        articles = list((await session.execute(stmt)).scalars().all())
    if not articles:
        return {"enriched": 0}

    size = config.NEWS_ENRICH_BATCH
    batches = [articles[i : i + size] for i in range(0, len(articles), size)]
    enriched, providers = 0, set()
    for n, chunk in enumerate(batches, start=1):
        lines = []
        for a in chunk:
            summary = (a.summary or "")[:150]
            lines.append(f"{a.id} | {a.title}" + (f" - {summary}" if len(summary) > 20 else ""))
        prompt = (
            "Classify each headline below. For every item return:\n"
            "- id (the number before the first |)\n"
            "- countries: ISO-3166 alpha-2 codes of the countries the story is actually ABOUT (max 3; [] if "
            "truly global). Never add a country just because the outlet is based there.\n"
            f"- category: one of {', '.join(CATEGORIES)}\n"
            "- sentiment: -1 (very bad news for people/economy) .. 1 (very good)\n"
            "- importance: 0..1 global significance\n"
            "- market_relevance: 0..1 likely effect on markets or consumer demand\n\n"
            "Headlines:\n" + "\n".join(lines)
        )
        try:
            data, result = await runtime.ai.generate_json(
                AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose="news_enrich", schema=ENRICH_SCHEMA,
                          schema_name="headline_tags", depth="fast", temperature=0.1),
                validator=lambda d: isinstance(d, dict) and isinstance(d.get("items"), list),
            )
        except Exception as exc:  # noqa: BLE001 - keep going with the other batches
            log.warning("enrichment batch %s failed: %s", n, exc)
            if progress:
                await progress(n / len(batches), f"batch {n}/{len(batches)} failed: {exc}")
            continue
        providers.add(result.provider_label)
        by_id = {a.id: a for a in chunk}
        async with SessionLocal() as session:
            for item in data.get("items", []):
                try:
                    aid = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                article = by_id.get(aid)
                if article is None:
                    continue
                tagged = [c.upper() for c in item.get("countries") or [] if isinstance(c, str) and c.upper() in valid]
                # gazetteer tags (from the text itself) win; the AI adds what the text implies
                gazetteer = tag_countries(f"{article.title}. {article.summary or ''}")
                merged = list(dict.fromkeys(gazetteer + tagged))[:6] or (article.countries or [])
                category = item.get("category") if item.get("category") in CATEGORIES else "other"
                await session.execute(
                    update(NewsArticle)
                    .where(NewsArticle.id == aid)
                    .values(
                        countries=merged,
                        category=category,
                        sentiment=clamp(item.get("sentiment"), -1, 1),
                        importance=clamp(item.get("importance"), 0, 1, 0.3),
                        market_relevance=clamp(item.get("market_relevance"), 0, 1, 0.3),
                        enriched_by=result.provider_label,
                    )
                )
                enriched += 1
            await session.commit()
        if progress:
            await progress(n / len(batches), f"classified batch {n}/{len(batches)} via {result.provider_label}")
    if batch_id:
        async with SessionLocal() as session:
            await session.execute(update(NewsBatch).where(NewsBatch.id == batch_id).values(enriched=True))
            await session.commit()
    runtime.hub.publish("world_update", {"what": "news"})
    return {"enriched": enriched, "articles": len(articles), "providers": sorted(providers)}


async def _market_snapshot() -> str:
    quotes = [q.to_dict() for q in await load_quotes() if q.ok and q.kind in ("index", "volatility", "commodity", "fx")]
    lines = [f"- {market_line(q)}" for q in quotes if q.get("analytics")]
    return "\n".join(lines[:24]) or "- (no market data yet)"


async def _hazard_snapshot() -> str:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(GeoEvent)
                .where(GeoEvent.occurred_at >= utcnow() - timedelta(days=3), GeoEvent.severity >= 0.35)
                .order_by(desc(GeoEvent.severity))
                .limit(8)
            )
        ).scalars().all()
    if not rows:
        return "- no significant natural hazards in the last 3 days"
    return "\n".join(
        f"- {r.kind}: {r.title} ({r.country_name or r.place}); alert={r.alert_level or 'n/a'}; severity={r.severity:.2f}"
        for r in rows
    )


async def daily_brief(batch_id: int | None = None, progress=None) -> dict:
    async with SessionLocal() as session:
        stmt = select(NewsArticle).where(NewsArticle.fetched_at >= utcnow() - timedelta(hours=36))
        if batch_id:
            stmt = select(NewsArticle).where(NewsArticle.batch_id == batch_id)
        stmt = stmt.order_by(desc(NewsArticle.importance).nulls_last(), desc(NewsArticle.published_at).nulls_last()).limit(120)
        articles = [a.to_dict() for a in (await session.execute(stmt)).scalars().all()]
    if not articles:
        return {"skipped": "no headlines"}
    profile = await load_profile()

    # Map: summarise themed chunks of headlines.
    groups: dict[str, list[dict]] = {}
    for a in articles:
        groups.setdefault(a.get("category") or "other", []).append(a)
    chunks: list[tuple[str, list[dict]]] = []
    for cat, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        for i in range(0, len(items), 25):
            chunks.append((cat, items[i : i + 25]))
    chunks = chunks[:6]
    cluster_summaries = []
    for n, (cat, items) in enumerate(chunks, start=1):
        lines = "\n".join(
            f"- [{a['outlet']}] {a['title']} (countries: {', '.join(a.get('countries') or []) or '-'}"
            f"; tone {a['sentiment'] if a.get('sentiment') is not None else 'n/a'})"
            for a in items
        )
        try:
            data, _ = await runtime.ai.generate_json(
                AIRequest(
                    system=ANALYST_SYSTEM,
                    prompt=f"Category: {cat}. Summarise these headlines into 1-4 themes with the regions involved "
                           f"(ISO2 codes) and an overall tone (-1..1).\n{lines}",
                    purpose="brief_map",
                    schema=CLUSTER_SCHEMA,
                    schema_name="cluster_summary",
                    depth="fast",
                )
            )
            cluster_summaries.append({"category": cat, **data})
        except Exception as exc:  # noqa: BLE001
            log.warning("brief map step failed for %s: %s", cat, exc)
        if progress:
            await progress(0.1 + 0.6 * n / len(chunks), f"summarised {cat} headlines")

    if not cluster_summaries:
        raise RuntimeError("could not summarise any headline group")

    top = "\n".join(f"- [{a['outlet']}] {a['title']}" for a in articles[:15])
    prompt = (
        f"{profile_block(profile)}\n\n"
        f"THEME SUMMARIES (from today's headlines of CNN, Times of India, NYT, BBC, Guardian, Al Jazeera and others):\n"
        f"{json.dumps(cluster_summaries, ensure_ascii=False)[:9000]}\n\n"
        f"MOST IMPORTANT HEADLINES:\n{top}\n\n"
        f"MARKETS NOW:\n{await _market_snapshot()}\n\n"
        f"NATURAL HAZARDS:\n{await _hazard_snapshot()}\n\n"
        "Write today's global brief for this startup: what is going on in the world, the overall mood (-1..1), "
        "3-6 key themes with regions (ISO2) and their market impact, market implications, a watchlist, and "
        "3-6 recommendations tailored to the startup (respect its capabilities)."
    )
    data, result = await runtime.ai.generate_json(
        AIRequest(system=ANALYST_SYSTEM, prompt=prompt, purpose="daily_brief", schema=BRIEF_SCHEMA,
                  schema_name="daily_brief", depth="deep"),
        validator=lambda d: isinstance(d, dict) and d.get("headline"),
    )
    if progress:
        await progress(0.95, "saving brief")
    data["global_mood"] = clamp(data.get("global_mood"), -1, 1)
    data["sources"] = sorted({a["outlet"] for a in articles})
    data["headline_count"] = len(articles)
    insight = await save_insight(
        kind="daily_brief",
        title=data.get("headline") or "Daily global brief",
        summary=data.get("summary") or "",
        content=data,
        recommendations=data.get("recommendations"),
        region="GLOBAL",
        severity="info",
        result=result,
        dedupe_key=f"brief:{utcnow().strftime('%Y-%m-%d')}",
        profile=profile,
        notify_level="success",
    )
    await set_state("latest_brief", {"insight_id": insight["id"], "at": utcnow().isoformat()})
    return {"insight_id": insight["id"], "provider": result.provider_label}
