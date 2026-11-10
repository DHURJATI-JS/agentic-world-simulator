"""Shared helpers for the AI pipelines: profile, capability filter, insight storage, semantic retrieval."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, or_, select

from app.ai.base import AIResult
from app.ai.prompts import REQUIRES
from app.data.geo import country_name
from app.db import SessionLocal
from app.models import AIInsight, AppState, NewsArticle, StartupProfile, utcnow
from app.runtime import runtime

log = logging.getLogger("worldsim.intel")

SEVERITY_TO_LEVEL = {"info": "info", "watch": "info", "warning": "warning", "critical": "critical", "success": "success"}


async def load_profile() -> dict | None:
    async with SessionLocal() as session:
        row = (await session.execute(select(StartupProfile).order_by(StartupProfile.id).limit(1))).scalar_one_or_none()
        return row.to_dict() if row else None


async def get_state(key: str) -> dict:
    async with SessionLocal() as session:
        row = await session.get(AppState, key)
        return dict(row.value or {}) if row else {}


async def set_state(key: str, value: dict) -> None:
    async with SessionLocal() as session:
        row = await session.get(AppState, key)
        if row is None:
            session.add(AppState(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()
        await session.commit()


# ---------------------------------------------------------------------------
# Capability filter: a startup that does not sell products can't ship products.
# ---------------------------------------------------------------------------
def capabilities(profile: dict | None) -> set[str]:
    caps = {"none"}
    if not profile:
        return caps
    physical = bool(profile.get("sells_physical_products"))
    if physical:
        caps.add("physical_products")
        if profile.get("can_ship_domestic"):
            caps.add("shipping_domestic")
        if profile.get("can_ship_international"):
            caps.add("shipping_international")
    if profile.get("offers_services") or profile.get("digital_only"):
        caps.add("services")
    return caps


def filter_recommendations(recs: Any, profile: dict | None, region: str | None = None) -> tuple[list[dict], list[dict]]:
    caps = capabilities(profile)
    hq = ((profile or {}).get("hq_country") or "").upper()
    ships_to = {s.upper() for s in (profile or {}).get("ships_to") or []}
    kept, hidden = [], []
    for rec in recs or []:
        if not isinstance(rec, dict) or not (rec.get("title") or rec.get("detail")):
            continue
        requires = rec.get("requires") if rec.get("requires") in REQUIRES else "none"
        clean = {
            "title": str(rec.get("title", ""))[:200],
            "detail": str(rec.get("detail", ""))[:1200],
            "priority": rec.get("priority") if rec.get("priority") in ("high", "medium", "low") else "medium",
            "requires": requires,
        }
        reason = None
        if requires not in caps:
            reason = {
                "physical_products": "Your startup does not sell physical products, so this does not apply.",
                "shipping_domestic": "Your startup cannot ship products, so this does not apply.",
                "shipping_international": "Your startup cannot ship products internationally.",
                "services": "Your startup does not offer services.",
            }.get(requires, "Not within your startup's capabilities.")
        elif region and requires.startswith("shipping"):
            reg = region.upper()
            if requires == "shipping_domestic" and hq and reg != hq and "shipping_international" not in caps:
                reason = f"Shipping to {country_name(reg)} would be international, which your startup cannot do."
            if requires == "shipping_international" and ships_to and "WORLDWIDE" not in ships_to and reg not in ships_to and reg != hq:
                reason = f"Your startup does not ship to {country_name(reg)}."
        if reason:
            clean["hidden_reason"] = reason
            hidden.append(clean)
        else:
            kept.append(clean)
    order = {"high": 0, "medium": 1, "low": 2}
    kept.sort(key=lambda r: order.get(r["priority"], 1))
    return kept, hidden


# ---------------------------------------------------------------------------
# Insight storage (every AI result is stored with a timestamp)
# ---------------------------------------------------------------------------
async def recent_insight(dedupe_key: str, within_seconds: int) -> dict | None:
    since = utcnow() - timedelta(seconds=within_seconds)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(AIInsight)
                .where(AIInsight.dedupe_key == dedupe_key, AIInsight.created_at >= since)
                .order_by(desc(AIInsight.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        return row.to_dict(full=False) if row else None


async def save_insight(
    *,
    kind: str,
    title: str,
    summary: str,
    content: dict,
    recommendations: list | None = None,
    region: str | None = None,
    severity: str = "info",
    result: AIResult | None = None,
    dedupe_key: str | None = None,
    related_event_id: int | None = None,
    related_run_id: int | None = None,
    profile: dict | None = None,
    notify: bool = True,
    notify_level: str | None = None,
) -> dict:
    if profile is None:
        profile = await load_profile()
    kept, hidden = filter_recommendations(recommendations or [], profile, region)
    try:
        vector, model = await runtime.embedder.embed_one(f"{title}. {summary}")
    except Exception:  # noqa: BLE001
        vector, model = None, None
    async with SessionLocal() as session:
        row = AIInsight(
            kind=kind,
            title=(title or kind)[:500],
            summary=summary or "",
            content=content or {},
            recommendations=kept,
            hidden_recommendations=hidden,
            region=region,
            severity=severity,
            dedupe_key=dedupe_key,
            provider=result.provider_label if result else "",
            model=result.model if result else "",
            related_event_id=related_event_id,
            related_run_id=related_run_id,
            embedding=vector,
            embed_model=model,
        )
        session.add(row)
        await session.commit()
        data = row.to_dict()
    runtime.hub.publish("insight", {k: data[k] for k in ("id", "kind", "title", "summary", "region", "severity", "created_at", "provider")})
    if notify:
        level = notify_level or SEVERITY_TO_LEVEL.get(severity, "info")
        await runtime.hub.notify(level, data["title"], (summary or "")[:280], link=f"#insight/{data['id']}",
                                 meta={"insight_id": data["id"], "kind": kind, "region": region})
    return data


# ---------------------------------------------------------------------------
# Semantic retrieval over headlines (pgvector cosine distance)
# ---------------------------------------------------------------------------
async def retrieve_news(query: str, *, countries: list[str] | None = None, hours: int = 72, limit: int = 20) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    vector, model = await runtime.embedder.embed_one(query)
    country_filter = or_(*[NewsArticle.countries.contains([c]) for c in countries]) if countries else None
    async with SessionLocal() as session:
        stmt = select(NewsArticle).where(
            NewsArticle.fetched_at >= since,
            NewsArticle.embedding.is_not(None),
            NewsArticle.embed_model == model,
        )
        if country_filter is not None:
            stmt = stmt.where(country_filter)
        stmt = stmt.order_by(NewsArticle.embedding.cosine_distance(vector)).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) < max(3, limit // 2):
            extra = select(NewsArticle).where(NewsArticle.fetched_at >= since)
            if country_filter is not None:
                extra = extra.where(country_filter)
            extra = extra.order_by(desc(NewsArticle.importance).nulls_last(), desc(NewsArticle.published_at).nulls_last()).limit(limit)
            seen = {r.id for r in rows}
            for row in (await session.execute(extra)).scalars():
                if row.id not in seen:
                    rows.append(row)
                    seen.add(row.id)
                if len(rows) >= limit:
                    break
    return [r.to_dict() for r in rows]


def news_lines(articles: list[dict], limit_chars: int = 5000) -> str:
    lines, used = [], 0
    for a in articles:
        sent = a.get("sentiment")
        tone = f" (tone {sent:+.1f})" if isinstance(sent, (int, float)) else ""
        date = (a.get("published_at") or a.get("fetched_at") or "")[:10]
        line = f"- [{a.get('outlet')}, {date}] {a.get('title')}{tone}"
        if a.get("summary") and len(a["summary"]) > 30:
            line += f" - {a['summary'][:160]}"
        if used + len(line) > limit_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if lines else "- (no country-specific headlines in the last 72 hours; rely on the market data)"


def market_line(quote: dict | None) -> str:
    if not quote:
        return "no market data"
    a = quote.get("analytics") or {}
    parts = [f"{quote.get('name')} {a.get('last')}"]
    for key, label in (("chg_1d", "1D"), ("chg_5d", "5D"), ("chg_1m", "1M"), ("chg_3m", "3M")):
        if a.get(key) is not None:
            parts.append(f"{label} {a[key]:+.2f}%")
    if a.get("drawdown_pct") is not None:
        parts.append(f"{a['drawdown_pct']:+.1f}% vs 52w high")
    if a.get("vol_20d_ann") is not None:
        parts.append(f"vol {a['vol_20d_ann']:.1f}%")
    if a.get("regime"):
        parts.append(a["regime"])
    return ", ".join(parts)
