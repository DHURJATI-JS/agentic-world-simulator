"""SQLAlchemy 2.0 ORM models. Every AI result is persisted with a timezone-aware timestamp."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config

DIM = config.EMBEDDING_DIM


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class Base(DeclarativeBase):
    pass


class AppState(Base):
    """Small key/value store: scheduler timestamps, first-run flags, cached scans."""

    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StartupProfile(Base):
    __tablename__ = "startup_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    sector: Mapped[str] = mapped_column(String(200), default="")
    business_model: Mapped[str] = mapped_column(String(200), default="")
    offerings: Mapped[list] = mapped_column(JSONB, default=list)
    sells_physical_products: Mapped[bool] = mapped_column(Boolean, default=False)
    offers_services: Mapped[bool] = mapped_column(Boolean, default=False)
    digital_only: Mapped[bool] = mapped_column(Boolean, default=False)
    can_ship_domestic: Mapped[bool] = mapped_column(Boolean, default=False)
    can_ship_international: Mapped[bool] = mapped_column(Boolean, default=False)
    ships_to: Mapped[list] = mapped_column(JSONB, default=list)
    target_markets: Mapped[list] = mapped_column(JSONB, default=list)
    hq_city: Mapped[str] = mapped_column(String(200), default="")
    hq_country: Mapped[str] = mapped_column(String(2), default="")
    hq_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    hq_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[str] = mapped_column(String(100), default="")
    team_size: Mapped[str] = mapped_column(String(100), default="")
    insured_for: Mapped[list] = mapped_column(JSONB, default=list)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "sector": self.sector,
            "business_model": self.business_model,
            "offerings": self.offerings or [],
            "sells_physical_products": self.sells_physical_products,
            "offers_services": self.offers_services,
            "digital_only": self.digital_only,
            "can_ship_domestic": self.can_ship_domestic,
            "can_ship_international": self.can_ship_international,
            "ships_to": self.ships_to or [],
            "target_markets": self.target_markets or [],
            "hq_city": self.hq_city,
            "hq_country": self.hq_country,
            "hq_lat": self.hq_lat,
            "hq_lon": self.hq_lon,
            "stage": self.stage,
            "team_size": self.team_size,
            "insured_for": self.insured_for or [],
            "onboarding_complete": self.onboarding_complete,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class OnboardingMessage(Base):
    __tablename__ = "onboarding_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "meta": self.meta or {},
            "created_at": _iso(self.created_at),
        }


class NewsBatch(Base):
    """One 24-hour headline collection run."""

    __tablename__ = "news_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[dict] = mapped_column(JSONB, default=dict)
    enriched: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "article_count": self.article_count,
            "new_count": self.new_count,
            "sources": self.sources or {},
            "enriched": self.enriched,
        }


class NewsArticle(Base):
    __tablename__ = "news_articles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("news_batches.id"), nullable=True)
    source_id: Mapped[str] = mapped_column(String(60))
    outlet: Mapped[str] = mapped_column(String(120))
    feed: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, unique=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    countries: Mapped[list] = mapped_column(JSONB, default=list)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    importance: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    enriched_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    embedding = mapped_column(Vector(DIM), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        Index("ix_news_published", "published_at"),
        Index("ix_news_countries", "countries", postgresql_using="gin"),
        Index(
            "ix_news_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "outlet": self.outlet,
            "feed": self.feed,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "published_at": _iso(self.published_at),
            "fetched_at": _iso(self.fetched_at),
            "countries": self.countries or [],
            "category": self.category,
            "sentiment": self.sentiment,
            "importance": self.importance,
            "market_relevance": self.market_relevance,
        }


class MarketQuote(Base):
    """Latest snapshot per instrument, with ~1 year of daily closes for analytics."""

    __tablename__ = "market_quotes"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    analytics: Mapped[dict] = mapped_column(JSONB, default=dict)
    history: Mapped[list] = mapped_column(JSONB, default=list)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self, with_history: bool = False) -> dict[str, Any]:
        data = {
            "symbol": self.symbol,
            "name": self.name,
            "kind": self.kind,
            "country": self.country,
            "currency": self.currency,
            "price": self.price,
            "change_1d": self.change_1d,
            "analytics": self.analytics or {},
            "ok": self.ok,
            "error": self.error,
            "fetched_at": _iso(self.fetched_at),
            "spark": [p[1] for p in (self.history or [])[-30:]],
        }
        if with_history:
            data["history"] = self.history or []
        return data


class GeoEvent(Base):
    """Natural hazards from USGS and GDACS."""

    __tablename__ = "geo_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), unique=True)
    source: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(Text)
    place: Mapped[str] = mapped_column(Text, default="")
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country_name: Mapped[str] = mapped_column(String(120), default="")
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    depth_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    magnitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    alert_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tsunami: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[float] = mapped_column(Float, default=0.0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    url: Mapped[str] = mapped_column(Text, default="")
    near_hq_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    analyzed: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_geo_events_time", "occurred_at"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "external_id": self.external_id,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "place": self.place,
            "country": self.country,
            "country_name": self.country_name,
            "lat": self.lat,
            "lon": self.lon,
            "depth_km": self.depth_km,
            "magnitude": self.magnitude,
            "alert_level": self.alert_level,
            "tsunami": self.tsunami,
            "severity": self.severity,
            "occurred_at": _iso(self.occurred_at),
            "updated_at": _iso(self.updated_at),
            "url": self.url,
            "near_hq_km": self.near_hq_km,
            "analyzed": self.analyzed,
        }


class AIInsight(Base):
    """Every AI summary / recommendation, stored with its date and time stamp."""

    __tablename__ = "ai_insights"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    recommendations: Mapped[list] = mapped_column(JSONB, default=list)
    hidden_recommendations: Mapped[list] = mapped_column(JSONB, default=list)
    region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    related_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("geo_events.id", ondelete="SET NULL"), nullable=True
    )
    related_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("simulation_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    embedding = mapped_column(Vector(DIM), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        Index("ix_insights_kind_time", "kind", "created_at"),
        Index("ix_insights_dedupe", "dedupe_key"),
    )

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "region": self.region,
            "severity": self.severity,
            "provider": self.provider,
            "model": self.model,
            "related_event_id": self.related_event_id,
            "related_run_id": self.related_run_id,
            "created_at": _iso(self.created_at),
        }
        if full:
            data["content"] = self.content or {}
            data["recommendations"] = self.recommendations or []
            data["hidden_recommendations"] = self.hidden_recommendations or []
        return data


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(20), default="info")
    title: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(200), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "link": self.link,
            "meta": self.meta or {},
            "read": self.read,
            "created_at": _iso(self.created_at),
        }


class TaskRecord(Base):
    """Durable background task queue (claimed with SELECT ... FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(60))
    lane: Mapped[str] = mapped_column(String(20), default="io")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(60), default="system")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    progress_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_tasks_claim", "lane", "status", "priority", "id"),
        Index("ix_tasks_dedupe", "dedupe_key", "status"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "lane": self.lane,
            "status": self.status,
            "priority": self.priority,
            "payload": self.payload or {},
            "result": self.result or {},
            "error": self.error,
            "requested_by": self.requested_by,
            "attempts": self.attempts,
            "progress": self.progress,
            "progress_note": self.progress_note,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    scenario: Mapped[dict] = mapped_column(JSONB, default=dict)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    consumers: Mapped[int] = mapped_column(Integer, default=0)
    ticks: Mapped[int] = mapped_column(Integer, default=0)
    shards: Mapped[int] = mapped_column(Integer, default=1)
    backend: Mapped[str] = mapped_column(String(20), default="")
    results: Mapped[dict] = mapped_column(JSONB, default=dict)
    patterns: Mapped[list] = mapped_column(JSONB, default=list)
    uplift: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_insight_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self, full: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "scenario": self.scenario or {},
            "consumers": self.consumers,
            "ticks": self.ticks,
            "shards": self.shards,
            "backend": self.backend,
            "error": self.error,
            "task_id": self.task_id,
            "report_insight_id": self.report_insight_id,
            "duration_s": self.duration_s,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "pattern_count": len(self.patterns or []),
        }
        if full:
            data["params"] = self.params or {}
            data["results"] = self.results or {}
            data["patterns"] = self.patterns or []
            data["uplift"] = self.uplift or {}
        return data


class Calibration(Base):
    __tablename__ = "calibrations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    target_moments: Mapped[dict] = mapped_column(JSONB, default=dict)
    sim_moments: Mapped[dict] = mapped_column(JSONB, default=dict)
    loss: Mapped[float] = mapped_column(Float, default=0.0)
    observations: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_calibrations_symbol_time", "symbol", "created_at"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "params": self.params or {},
            "target_moments": self.target_moments or {},
            "sim_moments": self.sim_moments or {},
            "loss": self.loss,
            "observations": self.observations,
            "created_at": _iso(self.created_at),
        }


class CountryStat(Base):
    """World Bank indicators used to calibrate the synthetic population."""

    __tablename__ = "country_stats"
    iso2: Mapped[str] = mapped_column(String(2), primary_key=True)
    iso3: Mapped[str] = mapped_column(String(3), default="")
    name: Mapped[str] = mapped_column(String(120), default="")
    gdp_per_capita: Mapped[float | None] = mapped_column(Float, nullable=True)
    population: Mapped[float | None] = mapped_column(Float, nullable=True)
    inflation: Mapped[float | None] = mapped_column(Float, nullable=True)
    year: Mapped[str | None] = mapped_column(String(10), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iso2": self.iso2,
            "iso3": self.iso3,
            "name": self.name,
            "gdp_per_capita": self.gdp_per_capita,
            "population": self.population,
            "inflation": self.inflation,
            "year": self.year,
            "updated_at": _iso(self.updated_at),
        }


class PersonaMemory(Base):
    """Memory stream of LLM persona agents (retrieved by recency x importance x similarity)."""

    __tablename__ = "persona_memories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    persona_key: Mapped[str] = mapped_column(String(80))
    region: Mapped[str] = mapped_column(String(2))
    kind: Mapped[str] = mapped_column(String(20), default="observation")
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    mood: Mapped[float | None] = mapped_column(Float, nullable=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    embedding = mapped_column(Vector(DIM), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (Index("ix_persona_mem_key_time", "persona_key", "created_at"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "persona_key": self.persona_key,
            "region": self.region,
            "kind": self.kind,
            "content": self.content,
            "importance": self.importance,
            "mood": self.mood,
            "data": self.data or {},
            "created_at": _iso(self.created_at),
        }


class AIUsage(Base):
    """One row per LLM call attempt (which tier answered, latency, tokens) - powers the guard."""

    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120), default="")
    purpose: Mapped[str] = mapped_column(String(80), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_ai_usage_time", "created_at"),)


__all__ = [
    "AIInsight",
    "AIUsage",
    "AppState",
    "Base",
    "Calibration",
    "CountryStat",
    "GeoEvent",
    "MarketQuote",
    "NewsArticle",
    "NewsBatch",
    "Notification",
    "OnboardingMessage",
    "PersonaMemory",
    "SimulationRun",
    "StartupProfile",
    "TaskRecord",
    "func",
    "utcnow",
]
