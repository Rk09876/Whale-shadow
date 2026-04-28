"""
FastAPI entrypoint for Whale Shadow Tracker.

Endpoints:
  GET  /                     — serves the dashboard HTML
  GET  /api/health           — liveness check
  GET  /api/timing           — current timing context (session phase, hora)
  GET  /api/snapshots/{sym}  — recent N snapshots for NIFTY / SENSEX
  GET  /api/walls/{sym}      — latest walls for a symbol
  GET  /api/heatmap/{sym}    — time-series matrix for the heatmap
  GET  /api/flows            — latest FII/DII rows
  GET  /api/block-deals      — latest block deals

Static UI served from /app/static.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .analytics.timing import now_context
from .analytics.walls import detect_oi_changes, detect_walls
from .scheduler import run_all
from .storage import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("whale_shadow")

STATIC_DIR = Path(__file__).parent / "static"
SUPPORTED_SYMBOLS = ("NIFTY", "SENSEX")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("Starting background scheduler")
    task = asyncio.create_task(run_all())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="Whale Shadow Tracker",
    description="Free, legal institutional-flow heatmap for NIFTY & SENSEX.",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/timing")
async def timing():
    ctx = now_context()
    return {
        "now_ist": ctx.now_ist,
        "weekday": ctx.weekday,
        "session_phase": ctx.session_phase,
        "minutes_to_close": ctx.minutes_to_close,
        "hora": {
            "lord": ctx.hora_lord,
            "index": ctx.hora_index,
            "start_ist": ctx.hora_start_ist,
            "end_ist": ctx.hora_end_ist,
        },
        "is_trading_day": ctx.is_trading_day,
    }


def _validate_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s not in SUPPORTED_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported symbol '{symbol}'. Use one of {SUPPORTED_SYMBOLS}.",
        )
    return s


@app.get("/api/snapshots/{symbol}")
async def snapshots(symbol: str, limit: int = 60):
    sym = _validate_symbol(symbol)
    rows = db.latest_snapshots(sym, limit=limit)
    return {
        "symbol": sym,
        "count": len(rows),
        "snapshots": [
            {
                "id": r["id"],
                "fetched_utc": r["fetched_utc"],
                "underlying": r["underlying"],
                "expiries": r["parsed"].get("expiries", []),
            }
            for r in rows
        ],
    }


@app.get("/api/walls/{symbol}")
async def walls(symbol: str, expiry: str | None = None):
    sym = _validate_symbol(symbol)
    snaps = db.latest_snapshots(sym, limit=1)
    if not snaps:
        return {"symbol": sym, "underlying": None, "walls": [], "stats": {}}

    parsed = snaps[0]["parsed"]
    payload = detect_walls(parsed["rows"], expiry_filter=expiry)
    return {
        "symbol": sym,
        "underlying": parsed.get("underlying"),
        "fetched_utc": parsed.get("fetched_utc"),
        "expiries": parsed.get("expiries", []),
        "selected_expiry": expiry,
        **payload,
    }


@app.get("/api/heatmap/{symbol}")
async def heatmap(
    symbol: str,
    expiry: str | None = None,
    snapshots: int = 60,
    side: str = "both",
):
    """
    Build the time-series heatmap matrix.

    Returns:
        {
          "symbol": str,
          "expiry": str | None,
          "timestamps": [str, ...],
          "strikes":    [float, ...],
          "ce_oi":      [[int, ...], ...],   # rows=timestamps, cols=strikes
          "pe_oi":      [[int, ...], ...],
          "ce_grade":   [["normal"|"large"|"whale", ...], ...],
          "pe_grade":   [...],
          "underlying": [float, ...],         # one per timestamp
          "max_pain":   [float, ...],
        }
    """
    sym = _validate_symbol(symbol)
    if side not in ("both", "ce", "pe"):
        raise HTTPException(400, "side must be both|ce|pe")

    snaps = db.latest_snapshots(sym, limit=snapshots)
    if not snaps:
        return {
            "symbol": sym,
            "expiry": expiry,
            "timestamps": [],
            "strikes": [],
            "ce_oi": [],
            "pe_oi": [],
            "ce_grade": [],
            "pe_grade": [],
            "underlying": [],
            "max_pain": [],
        }

    # Determine expiry: nearest if not specified.
    if expiry is None:
        expiries = snaps[-1]["parsed"].get("expiries", [])
        expiry = expiries[0] if expiries else None

    # Collect the universe of strikes across all snapshots for this expiry.
    strike_set: set[float] = set()
    for s in snaps:
        for r in s["parsed"]["rows"]:
            if expiry and r.get("expiry") != expiry:
                continue
            if r.get("strike") is not None:
                strike_set.add(float(r["strike"]))
    strikes = sorted(strike_set)

    ce_matrix: list[list[int]] = []
    pe_matrix: list[list[int]] = []
    ce_grade_matrix: list[list[str]] = []
    pe_grade_matrix: list[list[str]] = []
    timestamps: list[str] = []
    underlyings: list[float] = []
    max_pains: list[float | None] = []

    for s in snaps:
        rows_at_t = [
            r for r in s["parsed"]["rows"]
            if not expiry or r.get("expiry") == expiry
        ]
        by_strike = {r["strike"]: r for r in rows_at_t if r.get("strike") is not None}

        ce_row = [int(by_strike.get(k, {}).get("ce_oi", 0)) for k in strikes]
        pe_row = [int(by_strike.get(k, {}).get("pe_oi", 0)) for k in strikes]

        # Grade per-strike using the cross-strike distribution at this timestamp.
        wall_payload = detect_walls(rows_at_t, expiry_filter=expiry, side=side)
        grade_map: dict[tuple[float, str], str] = {
            (w["strike"], w["side"]): w["grade"] for w in wall_payload["walls"]
        }
        ce_grade_row = [grade_map.get((k, "CE"), "normal") for k in strikes]
        pe_grade_row = [grade_map.get((k, "PE"), "normal") for k in strikes]

        ce_matrix.append(ce_row)
        pe_matrix.append(pe_row)
        ce_grade_matrix.append(ce_grade_row)
        pe_grade_matrix.append(pe_grade_row)
        timestamps.append(s["fetched_utc"])
        underlyings.append(s["parsed"].get("underlying") or 0.0)
        max_pains.append(wall_payload["stats"].get("max_pain"))

    return {
        "symbol": sym,
        "expiry": expiry,
        "timestamps": timestamps,
        "strikes": strikes,
        "ce_oi": ce_matrix,
        "pe_oi": pe_matrix,
        "ce_grade": ce_grade_matrix,
        "pe_grade": pe_grade_matrix,
        "underlying": underlyings,
        "max_pain": max_pains,
    }


@app.get("/api/flows")
async def flows(limit: int = 10):
    return {"flows": db.latest_flows(limit=limit)}


@app.get("/api/block-deals")
async def block_deals(limit: int = 30):
    return {"block_deals": db.latest_block_deals(limit=limit)}
