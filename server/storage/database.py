"""Async SQLite database with WAL mode and connection pooling."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite


class Database:
    """Async SQLite connection pool with WAL mode."""

    def __init__(self, db_path: str | Path, pool_size: int = 5):
        self._db_path = Path(db_path)
        self._pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=pool_size)
        self._initialized = False
        self._all_conns: list[aiosqlite.Connection] = []

    async def initialize(self) -> None:
        """Create connection pool and configure WAL mode."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        for _ in range(self._pool_size):
            conn = await aiosqlite.connect(str(self._db_path))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA wal_autocheckpoint=1000")
            await conn.execute("PRAGMA foreign_keys=ON")
            # PROV-1: a concurrent write loser WAITs briefly instead of raising
            # SQLITE_BUSY immediately, so the losing pairing-redeem surfaces a
            # clean generic 400 rather than an OperationalError.
            await conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = aiosqlite.Row
            self._all_conns.append(conn)
            await self._pool.put(conn)

        # FS-1: lock down DB file/dir perms on shared hosts. SQLite creates
        # the DB world-readable by default. Skip for in-memory / non-file DBs.
        if str(self._db_path) != ":memory:" and self._db_path.is_file():
            try:
                os.chmod(self._db_path, 0o600)
                os.chmod(self._db_path.parent, 0o700)
            except OSError:
                # Best-effort; don't block startup if the FS rejects it.
                pass

        self._initialized = True

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        """Acquire a connection from the pool."""
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            await self._pool.put(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Acquire a connection and wrap operations in a transaction.

        Commits on success, rolls back on exception.
        """
        async with self.acquire() as conn:
            await conn.execute("BEGIN")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute a single statement using a pooled connection."""
        async with self.acquire() as conn:
            cursor = await conn.execute(sql, params)
            await conn.commit()
            return cursor

    async def executescript(self, sql: str) -> None:
        """Execute a multi-statement script."""
        async with self.acquire() as conn:
            await conn.executescript(sql)

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        """Fetch a single row."""
        async with self.acquire() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        """Fetch all rows."""
        async with self.acquire() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()

    async def close(self) -> None:
        """Close all connections in the pool."""
        for conn in self._all_conns:
            await conn.close()
        self._all_conns.clear()
        while not self._pool.empty():
            self._pool.get_nowait()
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized
