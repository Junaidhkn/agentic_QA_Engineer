"""SQLite storage shared across threads (one connection, serialized by a lock)."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.RLock()
_conn = sqlite3.connect(os.environ.get("SHOP_DB", ":memory:"), check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                                  password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL NOT NULL,
                                     stock INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                                   quantity INTEGER NOT NULL, status TEXT NOT NULL);
"""


def init() -> None:
    with _lock:
        _conn.executescript(SCHEMA)
        if not _conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            _conn.executemany(
                "INSERT INTO products (name, price, stock) VALUES (?, ?, ?)",
                [("Coffee beans", 12.5, 100000), ("Tea leaves", 8.0, 100000), ("Limited mug", 25.0, 50)],
            )


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    with _lock:
        yield _conn


def query(sql: str, *args: object) -> list[sqlite3.Row]:
    with cursor() as c:
        return c.execute(sql, args).fetchall()


def execute(sql: str, *args: object) -> int:
    with cursor() as c:
        cur = c.execute(sql, args)
        return int(cur.lastrowid or 0)
