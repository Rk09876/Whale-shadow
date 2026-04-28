"""
SQLite storage for option chain snapshots and flows.

Why SQLite instead of Postgres:
  - Zero ops, file-based, persists on Render's disk volume.
  - Plenty fast for one user reading + scheduler writing every 30s.
  - Easy to download the .db file and analyse offline in DuckDB / pandas.

Schema:
  snapshots(id, symbol, fetched_utc, underlying, raw_json)
  walls    (snapshot_id, expiry, strike, side, oi, chg_oi, grade)
  flows    (id, fetched_utc, category, date_str, buy, sell, net)
  block_deals(id, fetched_utc, date_str, symbol, client, side, qty, price)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path("data/whale_shadow.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    fetched_utc  TEXT NOT NULL,
    underlying   REAL,
    raw_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_symbol_time
    ON snapshots(symbol, fetched_utc);

CREATE TABLE IF NOT EXISTS walls (
    snapshot_id  INTEGER NOT NULL,
    expiry       TEXT,
    strike       REAL NOT NULL,
    side         TEXT NOT NULL,
    oi           INTEGER NOT NULL,
    chg_oi       INTEGER NOT NULL,
    grade        TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_walls_snapshot
    ON walls(snapshot_id);

CREATE TABLE IF NOT EXISTS flows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_utc  TEXT NOT NULL,
    category     TEXT NOT NULL,
    date_str     TEXT NOT NULL,
    buy          REAL,
    sell         REAL,
    net          REAL
);
CREATE INDEX IF NOT EXISTS ix_flows_date ON flows(date_str, category);

CREATE TABLE IF NOT EXISTS block_deals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_utc  TEXT NOT NULL,
    date_str     TEXT NOT NULL,
    symbol       TEXT,
    client       TEXT,
    side         TEXT,
    qty          INTEGER,
    price        REAL
);
CREATE INDEX IF NOT EXISTS ix_block_deals_date ON block_deals(date_str);
"""


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def _connect(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def save_snapshot(
    symbol: str,
    parsed: dict[str, Any],
    walls_payload: dict[str, Any],
    path: Path = DB_PATH,
) -> int:
    """Persist a parsed option chain + its detected walls. Returns snapshot id."""
    fetched_utc = parsed.get("fetched_utc") or datetime.now(timezone.utc).isoformat()
    underlying = parsed.get("underlying", 0.0)
    raw_json = json.dumps(parsed, default=str)

    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO snapshots (symbol, fetched_utc, underlying, raw_json) "
            "VALUES (?, ?, ?, ?);",
            (symbol, fetched_utc, underlying, raw_json),
        )
        snapshot_id = cur.lastrowid

        rows = [
            (
                snapshot_id,
                w.get("expiry"),
                w["strike"],
                w["side"],
                w["oi"],
                w.get("chg_oi", 0),
                w["grade"],
            )
            for w in walls_payload.get("walls", [])
        ]
        if rows:
            conn.executemany(
                "INSERT INTO walls "
                "(snapshot_id, expiry, strike, side, oi, chg_oi, grade) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                rows,
            )
    return snapshot_id


def latest_snapshots(
    symbol: str, limit: int = 60, path: Path = DB_PATH
) -> list[dict[str, Any]]:
    """Return the most recent N snapshots for a symbol, oldest-first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, symbol, fetched_utc, underlying, raw_json "
            "FROM snapshots WHERE symbol=? "
            "ORDER BY id DESC LIMIT ?;",
            (symbol, limit),
        ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    for o in out:
        o["parsed"] = json.loads(o.pop("raw_json"))
    return out


def latest_walls(
    symbol: str, path: Path = DB_PATH
) -> list[dict[str, Any]]:
    """Walls from the most recent snapshot of `symbol`."""
    with _connect(path) as conn:
        snap = conn.execute(
            "SELECT id FROM snapshots WHERE symbol=? "
            "ORDER BY id DESC LIMIT 1;",
            (symbol,),
        ).fetchone()
        if not snap:
            return []
        rows = conn.execute(
            "SELECT expiry, strike, side, oi, chg_oi, grade FROM walls "
            "WHERE snapshot_id=? ORDER BY oi DESC;",
            (snap["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def save_flows(flows: list[dict[str, Any]], path: Path = DB_PATH) -> None:
    if not flows:
        return
    rows = [
        (
            f.get("fetched_utc") or datetime.now(timezone.utc).isoformat(),
            f.get("category", ""),
            f.get("date", ""),
            f.get("buy_value", 0.0),
            f.get("sell_value", 0.0),
            f.get("net_value", 0.0),
        )
        for f in flows
    ]
    with _connect(path) as conn:
        conn.executemany(
            "INSERT INTO flows (fetched_utc, category, date_str, buy, sell, net) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            rows,
        )


def save_block_deals(deals: list[dict[str, Any]], path: Path = DB_PATH) -> None:
    if not deals:
        return
    rows = [
        (
            d.get("fetched_utc") or datetime.now(timezone.utc).isoformat(),
            d.get("date", ""),
            d.get("symbol", ""),
            d.get("client_name", ""),
            d.get("deal_type", ""),
            int(d.get("quantity", 0) or 0),
            float(d.get("trade_price", 0.0) or 0.0),
        )
        for d in deals
    ]
    with _connect(path) as conn:
        conn.executemany(
            "INSERT INTO block_deals "
            "(fetched_utc, date_str, symbol, client, side, qty, price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?);",
            rows,
        )


def latest_flows(limit: int = 10, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM flows ORDER BY id DESC LIMIT ?;",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_block_deals(limit: int = 30, path: Path = DB_PATH) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM block_deals ORDER BY id DESC LIMIT ?;",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_old_snapshots(keep_per_symbol: int = 500, path: Path = DB_PATH) -> int:
    """Keep only the most recent `keep_per_symbol` snapshots per symbol."""
    deleted = 0
    with _connect(path) as conn:
        symbols = [
            r["symbol"]
            for r in conn.execute(
                "SELECT DISTINCT symbol FROM snapshots;"
            ).fetchall()
        ]
        for sym in symbols:
            cur = conn.execute(
                """
                DELETE FROM snapshots
                WHERE symbol=? AND id NOT IN (
                    SELECT id FROM snapshots
                    WHERE symbol=?
                    ORDER BY id DESC
                    LIMIT ?
                );
                """,
                (sym, sym, keep_per_symbol),
            )
            deleted += cur.rowcount
    return deleted
