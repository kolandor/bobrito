"""Async database session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bobrito.persistence.models import Base


class DatabaseManager:
    def __init__(self, url: str) -> None:
        self._url = url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def init(self) -> None:
        """Create engine, ensure data directory exists, create tables."""
        if "sqlite" in self._url:
            db_path = self._url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_async_engine(
            self._url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False} if "sqlite" in self._url else {},
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()

    def session(self) -> AsyncSession:
        if self._session_factory is None:
            raise RuntimeError("DatabaseManager not initialised. Call await init() first.")
        return self._session_factory()

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self.session() as s:
            yield s


_db_manager: DatabaseManager | None = None


def init_db_manager(url: str) -> DatabaseManager:
    global _db_manager
    _db_manager = DatabaseManager(url)
    return _db_manager


def get_db_manager() -> DatabaseManager:
    if _db_manager is None:
        raise RuntimeError("DatabaseManager not created. Call init_db_manager() first.")
    return _db_manager


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_db_manager().session() as session:
        yield session
