"""Database wiring: declarative base, async engine, session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def make_engine(url: str | None = None) -> AsyncEngine:
    """Create an async engine, defaulting to the configured DATABASE_URL."""
    return create_async_engine(url or get_settings().DATABASE_URL, future=True)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first use."""
    global _engine, _session_factory
    if _session_factory is None:
        _engine = make_engine()
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session; intended as a FastAPI dependency once routes exist."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
