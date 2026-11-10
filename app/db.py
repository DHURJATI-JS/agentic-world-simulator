"""Async database engine/session factory and schema bootstrap (PostgreSQL + pgvector)."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import config

engine = create_async_engine(
    config.DATABASE_URL,
    pool_size=config.DB_POOL_SIZE,
    max_overflow=10,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Create the pgvector extension and all tables (idempotent)."""
    from app.models import Base

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session
