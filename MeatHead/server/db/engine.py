"""
================================================================================
GiLLBoT — Database Engine
================================================================================
File:     server/db/engine.py

PURPOSE
-------
Async SQLAlchemy engine and session factory. Reads DATABASE_URL from .env.
Uses PostgreSQL + asyncpg in all environments.

USAGE
-----
    from server.db.engine import get_db, create_tables

    # In lifespan:
    await create_tables()

    # In route handlers:
    async with get_db() as db:
        user = await db.get(User, user_id)
================================================================================
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from server.db.models import Base

load_dotenv()

logger = logging.getLogger("gillbot.db")

# ---------------------------------------------------------------------------
# Engine configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost:5432/gillbot",
)

# Railway provides PostgreSQL URLs as "postgresql://..." but asyncpg needs
# "postgresql+asyncpg://..." — auto-fix here so it works out of the box.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "").lower() == "true",
    pool_size=10,
    max_overflow=20,
    # Railway managed Postgres requires SSL. Local dev (no "railway" in URL)
    # connects without SSL as before.
    connect_args={"ssl": "require"} if "railway" in DATABASE_URL else {},
)

# Session factory — produces async sessions
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SCHEMA_UPGRADES = [
    "ALTER TABLE campaign_leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
    "ALTER TABLE content_drafts ADD COLUMN IF NOT EXISTS product_context TEXT",
    "ALTER TABLE content_drafts ADD COLUMN IF NOT EXISTS topic TEXT",
    "ALTER TABLE content_drafts ADD COLUMN IF NOT EXISTS tone VARCHAR(64)",
    "ALTER TABLE sequence_executions ADD COLUMN IF NOT EXISTS error TEXT",
    "ALTER TABLE sequence_executions ALTER COLUMN channel DROP NOT NULL",
    "ALTER TABLE sequence_executions ALTER COLUMN generated_body DROP NOT NULL",
]


async def create_tables() -> None:
    """Create all tables if they don't exist. Called once at startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if engine.dialect.name == "postgresql":
        from sqlalchemy import text
        for stmt in _SCHEMA_UPGRADES:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(stmt))
            except Exception as e:
                logger.info(f"Schema upgrade skipped ({stmt[:60]}): {e}")
    logger.info(f"Database tables created/verified (url={DATABASE_URL.split('://')[0]})")


async def close_db() -> None:
    """Dispose of the engine connection pool. Call on shutdown."""
    await engine.dispose()
    logger.info("Database connection pool closed.")


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for database sessions.

    Usage:
        async with get_db() as db:
            result = await db.execute(select(User))
            users = result.scalars().all()
    """
    session = async_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db_dependency() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency version of get_db (for use with Depends()).

    Usage:
        async def my_route(db: AsyncSession = Depends(get_db_dependency)):
            ...
    """
    async with get_db() as db:
        yield db
