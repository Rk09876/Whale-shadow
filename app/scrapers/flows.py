"""
Free institutional flow scrapers — the "whale wake" data:

  - FII/DII daily cash-market flows (NSE, EOD)
  - FII derivatives statistics (NSE, EOD)
  - Block & bulk deals (NSE/BSE, same-day)
  - Participant-wise OI (NSE, EOD) — futures category positioning

These are the slowest and most reliable institutional signals. They move on
a daily timescale, which is exactly what a retail trader on a phone can act on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"

# Public, EOD-published endpoints
FII_DII_URL = f"{NSE_BASE}/api/fiidiiTradeReact"
BLOCK_DEALS_URL = f"{NSE_BASE}/api/block-deal"
BULK_DEALS_URL = f"{NSE_BASE}/api/historical/bulk-deals"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{NSE_BASE}/",
}


async def _seeded_client() -> httpx.AsyncClient:
    """Return a client with NSE cookies seeded."""
    client = httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(15.0, connect=10.0),
        follow_redirects=True,
        http2=True,
    )
    try:
        await client.get(NSE_BASE)
    except httpx.HTTPError as e:
        log.warning("Cookie seed failed: %s", e)
    return client


async def fetch_fii_dii_flows() -> list[dict[str, Any]]:
    """
    Fetch FII & DII cash-market flows for the latest published date.

    Returns:
        [
          {"category": "FII/FPI", "date": str,
           "buy_value": float, "sell_value": float, "net_value": float},
          {"category": "DII", ...},
        ]
    """
    client = await _seeded_client()
    try:
        resp = await client.get(FII_DII_URL)
        resp.raise_for_status()
        data = resp.json()
    finally:
        await client.aclose()

    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for entry in data:
            out.append(
                {
                    "category": entry.get("category", ""),
                    "date": entry.get("date", ""),
                    "buy_value": _to_float(entry.get("buyValue")),
                    "sell_value": _to_float(entry.get("sellValue")),
                    "net_value": _to_float(entry.get("netValue")),
                    "fetched_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
    return out


async def fetch_block_deals() -> list[dict[str, Any]]:
    """
    Fetch today's block deals (large pre-arranged trades).

    These are explicit institutional whale moves — same-day published.
    """
    client = await _seeded_client()
    try:
        resp = await client.get(BLOCK_DEALS_URL)
        resp.raise_for_status()
        data = resp.json()
    finally:
        await client.aclose()

    deals: list[dict[str, Any]] = []
    raw_list = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return deals

    for entry in raw_list:
        deals.append(
            {
                "date": entry.get("BD_DT_DATE") or entry.get("date", ""),
                "symbol": entry.get("BD_SYMBOL") or entry.get("symbol", ""),
                "client_name": (
                    entry.get("BD_CLIENT_NAME") or entry.get("clientName", "")
                ),
                "deal_type": entry.get("BD_BUY_SELL") or entry.get("dealType", ""),
                "quantity": int(_to_float(
                    entry.get("BD_QTY_TRD") or entry.get("quantity")
                )),
                "trade_price": _to_float(
                    entry.get("BD_TP_WATP") or entry.get("tradePrice")
                ),
                "fetched_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
    return deals


def _to_float(v: Any) -> float:
    if v is None or v == "" or v == "-":
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("(", "-").replace(")", ""))
    except (TypeError, ValueError):
        return 0.0
