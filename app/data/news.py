"""Collect global headlines from the reliable-source allow-list (runs once every 24 hours)."""
from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import feedparser
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

import config
from app.data.geo import tag_countries
from app.data.http import make_client
from app.data.sources import NEWS_SOURCES
from app.db import SessionLocal
from app.models import NewsArticle, NewsBatch, utcnow

log = logging.getLogger("worldsim.news")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CNN_LINK_RE = re.compile(
    r'<a href="(/(20\d\d)/(\d\d)/(\d\d)/[^"#?]+)"[^>]*>\s*(.*?)\s*</a>', re.DOTALL | re.IGNORECASE
)

Progress = Callable[[float, str], Awaitable[None]]


def clean_text(value: str | None, limit: int = 600) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def parse_rss(content: bytes, source: dict) -> list[dict]:
    feed = feedparser.parse(content)
    items = []
    for entry in feed.entries[: config.NEWS_MAX_PER_SOURCE * 2]:
        title = clean_text(entry.get("title"), 400)
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        items.append(
            {
                "title": title,
                "summary": clean_text(entry.get("summary") or entry.get("description"), 600),
                "url": link,
                "published_at": _entry_time(entry),
            }
        )
    return items


def parse_cnn_lite(text: str, source: dict) -> list[dict]:
    items, seen = [], set()
    for match in _CNN_LINK_RE.finditer(text):
        path, year, month, day, label = match.groups()
        title = clean_text(label, 400)
        if not title or path in seen or len(title) < 12:
            continue
        seen.add(path)
        try:
            published = datetime(int(year), int(month), int(day), 12, 0, tzinfo=timezone.utc)
        except ValueError:
            published = None
        section = path.strip("/").split("/")[3] if path.count("/") >= 4 else ""
        items.append(
            {
                "title": title,
                "summary": f"CNN {section}".strip() if section else "",
                "url": "https://www.cnn.com" + path,
                "published_at": published,
            }
        )
    return items


async def fetch_source(client, source: dict) -> tuple[list[dict], str | None]:
    try:
        resp = await client.get(source["url"])
        resp.raise_for_status()
        if source["kind"] == "cnn_lite":
            items = await asyncio.to_thread(parse_cnn_lite, resp.text, source)
        else:
            items = await asyncio.to_thread(parse_rss, resp.content, source)
        return items, None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"[:200]


def _normal_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


async def collect_headlines(progress: Progress | None = None) -> dict:
    """Fetch every allow-listed source, keep fresh unique headlines, store them in a new batch."""
    async with SessionLocal() as session:
        batch = NewsBatch()
        session.add(batch)
        await session.commit()
        batch_id = batch.id

    cutoff = utcnow() - timedelta(days=config.NEWS_MAX_AGE_DAYS)
    source_report: dict[str, dict] = {}
    rows: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    sem = asyncio.Semaphore(6)
    done = 0

    async with make_client() as client:
        async def run(source: dict) -> tuple[dict, list[dict], str | None]:
            async with sem:
                items, error = await fetch_source(client, source)
            return source, items, error

        tasks = [asyncio.create_task(run(s)) for s in NEWS_SOURCES]
        for future in asyncio.as_completed(tasks):
            source, items, error = await future
            done += 1
            fresh = [i for i in items if i["published_at"] is None or i["published_at"] >= cutoff]
            kept = 0
            for item in fresh:
                if kept >= config.NEWS_MAX_PER_SOURCE:
                    break
                key = _normal_title(item["title"])
                if item["url"] in seen_urls or key in seen_titles:
                    continue
                seen_urls.add(item["url"])
                seen_titles.add(key)
                countries = tag_countries(f"{item['title']}. {item['summary']}")
                if not countries and source.get("region"):
                    countries = [source["region"]]
                rows.append(
                    {
                        "batch_id": batch_id,
                        "source_id": source["id"],
                        "outlet": source["outlet"],
                        "feed": source["feed"],
                        "title": item["title"],
                        "summary": item["summary"],
                        "url": item["url"][:2000],
                        "published_at": item["published_at"],
                        "fetched_at": utcnow(),
                        "countries": countries,
                    }
                )
                kept += 1
            status = "failed" if error else ("stale" if items and not fresh else ("empty" if not items else "ok"))
            source_report[source["id"]] = {
                "outlet": source["outlet"],
                "feed": source["feed"],
                "status": status,
                "fetched": len(items),
                "kept": kept,
                "error": error,
            }
            if progress:
                await progress(done / len(NEWS_SOURCES) * 0.9, f"{source['outlet']} ({source['feed']}): {status}, {kept} headlines")

    new_ids: list[int] = []
    async with SessionLocal() as session:
        for start in range(0, len(rows), 200):
            chunk = rows[start : start + 200]
            stmt = (
                insert(NewsArticle)
                .values(chunk)
                .on_conflict_do_nothing(index_elements=["url"])
                .returning(NewsArticle.id)
            )
            result = await session.execute(stmt)
            new_ids.extend(result.scalars().all())
        await session.execute(
            update(NewsBatch)
            .where(NewsBatch.id == batch_id)
            .values(
                finished_at=utcnow(),
                article_count=len(rows),
                new_count=len(new_ids),
                sources=source_report,
            )
        )
        await session.commit()

    ok_sources = sum(1 for s in source_report.values() if s["status"] == "ok")
    return {
        "batch_id": batch_id,
        "headlines": len(rows),
        "new": len(new_ids),
        "new_ids": new_ids,
        "sources_ok": ok_sources,
        "sources_total": len(NEWS_SOURCES),
        "sources": source_report,
    }


async def embed_articles(article_ids: list[int], embedder) -> int:
    """Store pgvector embeddings for the given articles."""
    if not article_ids:
        return 0
    async with SessionLocal() as session:
        result = await session.execute(
            select(NewsArticle.id, NewsArticle.title, NewsArticle.summary).where(NewsArticle.id.in_(article_ids))
        )
        rows = result.all()
    count = 0
    for start in range(0, len(rows), 64):
        chunk = rows[start : start + 64]
        vectors, model = await embedder.embed([f"{r.title}. {r.summary}" for r in chunk])
        async with SessionLocal() as session:
            for row, vec in zip(chunk, vectors):
                await session.execute(
                    update(NewsArticle).where(NewsArticle.id == row.id).values(embedding=vec, embed_model=model)
                )
            await session.commit()
        count += len(chunk)
    return count
