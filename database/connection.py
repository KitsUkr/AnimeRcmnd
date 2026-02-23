import psycopg2
import os
import threading
from contextlib import contextmanager
from typing import Generator, Any
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError("Вкажи DATABASE_URL у .env (DATABASE_URL=postgresql://...)")


class ConnectionWrapper:
    """Wrapper that provides SQLite-like execute() method for psycopg2 connection."""
    
    def __init__(self, conn: psycopg2.extensions.connection):
        self._conn = conn
        self._cursor = None
    
    def execute(self, query: str, params: tuple = None):
        """Execute a query and return cursor (like SQLite connection.execute())."""
        if self._cursor is None or self._cursor.closed:
            self._cursor = self._conn.cursor()
        self._cursor.execute(query, params or ())
        return self._cursor
    
    def executemany(self, query: str, params_list):
        """Execute a query with multiple parameter sets."""
        if self._cursor is None or self._cursor.closed:
            self._cursor = self._conn.cursor()
        self._cursor.executemany(query, params_list)
        return self._cursor
    
    def commit(self):
        self._conn.commit()
    
    def rollback(self):
        self._conn.rollback()
    
    def close(self):
        if self._cursor and not self._cursor.closed:
            self._cursor.close()
        self._conn.close()
    
    @property
    def closed(self):
        return self._conn.closed


class DBHelper:
    _instance = None
    _lock = threading.Lock()
    _conn = None
    _wrapper = None
    _transaction_depth = 0

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(DBHelper, cls).__new__(cls)
        return cls._instance

    def get_connection(self) -> ConnectionWrapper:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(DATABASE_URL)
            self._conn.autocommit = False
            self._wrapper = ConnectionWrapper(self._conn)
        return self._wrapper

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """Context manager for database transactions with nested call support.
        
        Usage:
            with db.transaction():
                # multiple write operations
                mark_seen(...)
                set_feedback(...)
        """
        wrapper = self.get_connection()
        is_outer = (self._transaction_depth == 0)
        
        self._transaction_depth += 1
        
        try:
            yield
            if is_outer:
                wrapper.commit()
        except Exception:
            if is_outer:
                wrapper.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def close(self):
        if self._wrapper and not self._wrapper.closed:
            self._wrapper.close()
            self._conn = None
            self._wrapper = None
            self._transaction_depth = 0


_db_helper = DBHelper()


def db() -> ConnectionWrapper:
    """Returns the persistent database connection wrapper with execute() method."""
    return _db_helper.get_connection()


def transaction():
    """Context manager for database transactions.
    
    Usage:
        with transaction():
            mark_seen(...)
            set_feedback(...)
    """
    return _db_helper.transaction()
