"""Thin Postgres helpers (psycopg 3)."""
from __future__ import annotations

import json
from contextlib import contextmanager

from . import settings


@contextmanager
def conn():
    import psycopg
    from psycopg.rows import dict_row

    settings.require("DATABASE_URL")
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row, autocommit=True) as c:
        yield c


def q(sql: str, params: tuple | dict = ()) -> list[dict]:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def one(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def kv_get(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM clip_kv WHERE key = %s", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    q("INSERT INTO clip_kv(key, value) VALUES (%s, %s) "
      "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def as_json(v) -> str:
    return json.dumps(v)
