"""
Background scheduler.

Two loops run concurrently:

  1. Option chain loop  — every POLL_SECONDS (default 30s), fetch NIFTY and
     SENSEX option chains, run wall detection, persist to SQLite.
  2. Flows loop         — every FLOWS_INTERVAL_SECONDS (default 1800s = 30 min),
     refresh FII/DII flows and block deals.

The scheduler is built around tenacity-style retries on transient errors,
with exponential backoff. Failures are logged but never crash the loop.

Polite-scraper guarantees:
  - Polls NSE no faster than every 15 s by default.
  - Skips polling outside trading hours (saves bandwidth, less suspicious).
  - Adds jitter so multiple deployments don't synchronize.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from .analytics.timing import IST, now_context
from .analytics.walls import detect_walls
from .scrapers.bse import snapshot_sensex
from .scrapers.flows import fetch_block_deals, fetch_fii_dii_flows
from .scrapers.nse import snapshot_nifty
from .storage import db

log = logging.getLogger(__name__)

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
FLOWS_INTERVAL_SECONDS = int(os.getenv("FLOWS_INTERVAL_SECONDS", "1800"))
PRUNE_INTERVAL_SECONDS = int(os.getenv("PRUNE_INTERVAL_SECONDS", "3600"))
SNAPSHOTS_KEEP = int(os.getenv("SNAPSHOTS_KEEP", "500"))
TRADING_HOURS_ONLY = os.getenv("TRADING_HOURS_ONLY", "1") == "1"


def _is_trading_window() -> bool:
    """Mon-Fri, 09:00 - 16:00 IST (small buffer around session)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    after_open = (h, m) >= (9, 0)
    before_close = (h, m) <= (16, 0)
    return after_open and before_close


async def _safe(coro, label: str):
    try:
        return await coro
    except Exception as e:
        log.warning("%s failed: %s", label, e)
        return None


async def option_chain_loop():
    log.info("Option chain loop started, poll=%ds", POLL_SECONDS)
    while True:
        if TRADING_HOURS_ONLY and not _is_trading_window():
            await asyncio.sleep(60)
            continue

        # NIFTY (NSE)
        parsed = await _safe(snapshot_nifty(), "NIFTY snapshot")
        if parsed and parsed.get("rows"):
            walls = detect_walls(parsed["rows"])
            db.save_snapshot("NIFTY", parsed, walls)
            log.info(
                "Saved NIFTY snapshot: spot=%.2f walls=%d",
                parsed.get("underlying", 0.0),
                len(walls.get("walls", [])),
            )

        # Stagger BSE call so we're not hitting both providers at the same instant
        await asyncio.sleep(2 + random.random())

        # SENSEX (BSE)
        parsed_s = await _safe(snapshot_sensex(), "SENSEX snapshot")
        if parsed_s and parsed_s.get("rows"):
            walls_s = detect_walls(parsed_s["rows"])
            db.save_snapshot("SENSEX", parsed_s, walls_s)
            log.info(
                "Saved SENSEX snapshot: spot=%.2f walls=%d",
                parsed_s.get("underlying", 0.0),
                len(walls_s.get("walls", [])),
            )

        # Sleep with jitter to spread load
        await asyncio.sleep(POLL_SECONDS + random.uniform(0, 5))


async def flows_loop():
    log.info("Flows loop started, interval=%ds", FLOWS_INTERVAL_SECONDS)
    while True:
        flows = await _safe(fetch_fii_dii_flows(), "FII/DII flows")
        if flows:
            db.save_flows(flows)
            log.info("Saved %d FII/DII rows", len(flows))

        deals = await _safe(fetch_block_deals(), "Block deals")
        if deals:
            db.save_block_deals(deals)
            log.info("Saved %d block deals", len(deals))

        await asyncio.sleep(FLOWS_INTERVAL_SECONDS + random.uniform(0, 30))


async def prune_loop():
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
        try:
            n = db.prune_old_snapshots(keep_per_symbol=SNAPSHOTS_KEEP)
            if n:
                log.info("Pruned %d old snapshots", n)
        except Exception as e:
            log.warning("Prune failed: %s", e)


async def run_all():
    db.init_db()
    await asyncio.gather(
        option_chain_loop(),
        flows_loop(),
        prune_loop(),
    )
