from __future__ import annotations

from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@lru_cache(maxsize=16)
def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    _ENGINE_REGISTRY[database_url] = engine
    return engine


@lru_cache(maxsize=16)
def create_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(create_engine(database_url), expire_on_commit=False)


async def dispose_engines() -> None:
    """Dispose all cached pools during application/worker shutdown."""
    engines = _cached_engines()
    for engine in engines:
        await engine.dispose()
    _ENGINE_REGISTRY.clear()
    create_session_factory.cache_clear()
    create_engine.cache_clear()


def _cached_engines() -> list[AsyncEngine]:
    # functools' cache does not expose values; this helper is replaced by the
    # registry populated below and keeps shutdown explicit and testable.
    return list(_ENGINE_REGISTRY.values())


_ENGINE_REGISTRY: dict[str, AsyncEngine] = {}


async def check_database(database_url: str) -> bool:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
