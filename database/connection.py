import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError("Вкажи DATABASE_URL у .env (DATABASE_URL=postgresql://...)")

_pool: AsyncConnectionPool | None = None
_active_connection: ContextVar["ConnectionWrapper | None"] = ContextVar(
    "active_db_connection", default=None
)


class QueryResult:
    def __init__(self, rows: list[Any], rowcount: int):
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Query:
    def __init__(self, executor: Callable[[], Awaitable[QueryResult]]):
        self._executor = executor

    def __await__(self):
        return self._executor().__await__()

    async def fetchone(self):
        return (await self._executor()).fetchone()

    async def fetchall(self):
        return (await self._executor()).fetchall()


class ConnectionWrapper:
    def __init__(self, conn: AsyncConnection):
        self._conn = conn

    async def _run(self, query: str, params: Iterable | None = None, many: bool = False) -> QueryResult:
        async with self._conn.cursor() as cursor:
            if many:
                await cursor.executemany(query, params or ())
            else:
                await cursor.execute(query, params or ())
            rows = await cursor.fetchall() if cursor.description else []
            return QueryResult(rows, cursor.rowcount)

    def execute(self, query: str, params: Iterable | None = None) -> Query:
        return Query(lambda: self._run(query, params))

    def executemany(self, query: str, params_list: Iterable) -> Query:
        return Query(lambda: self._run(query, params_list, many=True))


class Database:
    async def _run_once(self, query: str, params: Iterable | None = None, many: bool = False) -> QueryResult:
        active = _active_connection.get()
        if active is not None:
            return await active._run(query, params, many)

        pool = _get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                return await ConnectionWrapper(conn)._run(query, params, many)

    def execute(self, query: str, params: Iterable | None = None) -> Query:
        return Query(lambda: self._run_once(query, params))

    def executemany(self, query: str, params_list: Iterable) -> Query:
        return Query(lambda: self._run_once(query, params_list, many=True))


def _get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Пул БД не ініціалізовано. Виконай init_db_pool() при старті.")
    return _pool


async def init_db_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = AsyncConnectionPool(
        DATABASE_URL,
        min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
        max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
        timeout=10,
        max_idle=300,
        open=False,
    )
    await _pool.open()


async def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def db() -> Database:
    return Database()


@asynccontextmanager
async def transaction() -> AsyncIterator[None]:
    active = _active_connection.get()
    if active is not None:
        async with active._conn.transaction():
            yield
        return

    pool = _get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            token = _active_connection.set(ConnectionWrapper(conn))
            try:
                yield
            finally:
                _active_connection.reset(token)
