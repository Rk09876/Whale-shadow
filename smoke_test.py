"""
Smoke test — run this locally after `pip install -r requirements.txt` to
validate that the NSE/BSE scrapers reach the public endpoints from your
network. No data is persisted; it just prints what it finds.

Usage:
    python smoke_test.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smoke")


async def test_nifty():
    from app.scrapers.nse import snapshot_nifty
    from app.analytics.walls import detect_walls

    log.info("Fetching NIFTY option chain…")
    parsed = await snapshot_nifty()
    log.info(
        "  underlying=%.2f  rows=%d  expiries=%d",
        parsed["underlying"],
        len(parsed["rows"]),
        len(parsed["expiries"]),
    )
    walls = detect_walls(parsed["rows"])
    log.info("  walls detected: %d (whale=%d large=%d)",
             len(walls["walls"]),
             sum(1 for w in walls["walls"] if w["grade"] == "whale"),
             sum(1 for w in walls["walls"] if w["grade"] == "large"))
    if walls["walls"]:
        log.info("  top wall: %s", walls["walls"][0])
    log.info("  max pain: %s", walls["stats"]["max_pain"])
    return True


async def test_sensex():
    from app.scrapers.bse import snapshot_sensex
    from app.analytics.walls import detect_walls

    log.info("Fetching SENSEX option chain…")
    try:
        parsed = await snapshot_sensex()
    except Exception as e:
        log.warning("  SENSEX fetch failed (BSE schema may have shifted): %s", e)
        return False

    log.info(
        "  underlying=%.2f  rows=%d  expiries=%d",
        parsed["underlying"],
        len(parsed["rows"]),
        len(parsed["expiries"]),
    )
    walls = detect_walls(parsed["rows"])
    log.info("  walls detected: %d", len(walls["walls"]))
    return True


async def test_flows():
    from app.scrapers.flows import fetch_fii_dii_flows, fetch_block_deals

    log.info("Fetching FII/DII flows…")
    try:
        flows = await fetch_fii_dii_flows()
        log.info("  got %d flow rows", len(flows))
        for f in flows[:3]:
            log.info("    %s", f)
    except Exception as e:
        log.warning("  flows failed: %s", e)

    log.info("Fetching block deals…")
    try:
        deals = await fetch_block_deals()
        log.info("  got %d block deals", len(deals))
        for d in deals[:3]:
            log.info("    %s", d)
    except Exception as e:
        log.warning("  block deals failed: %s", e)


async def main():
    ok_n = await test_nifty()
    print()
    ok_s = await test_sensex()
    print()
    await test_flows()
    print()
    if ok_n and ok_s:
        log.info("✓ all scrapers reached their endpoints")
        return 0
    log.warning("✗ some scrapers failed — see above")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
