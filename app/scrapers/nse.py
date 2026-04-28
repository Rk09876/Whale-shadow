"""
NSE option chain scraper.

NSE blocks naive scrapers. The trick is:
1. Hit homepage first to seed cookies (required for the JSON endpoint).
2. Refresh the cookie session every ~5 minutes.
3. Use realistic browser headers.
4. Respect a 3-5s polite delay between requests.

This module is read-only against public endpoints only. No credentials.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
OPTION_CHAIN_URL = f"{NSE_BASE}/api/option-chain-indices"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": f"{NSE_BASE}/option-chain",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}


class NSEClient:
    """Async client that maintains a cookie jar for NSE."""

    def __init__(self, cookie_refresh_seconds: int = 300):
        self._client: httpx.AsyncClient | None = None
        self._cookie_refresh_seconds = cookie_refresh_seconds
        self._last_cookie_refresh: float = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers=BROWSER_HEADERS,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
            http2=True,
        )
        await self._refresh_cookies()
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _refresh_cookies(self) -> None:
        """Hit homepage + option-chain page to seed cookies."""
        if self._client is None:
            raise RuntimeError("Client not initialized")

        async with self._lock:
            try:
                # Two-step warmup: homepage, then option chain page.
                await self._client.get(NSE_BASE)
                await asyncio.sleep(0.5)
                await self._client.get(f"{NSE_BASE}/option-chain")
                self._last_cookie_refresh = asyncio.get_event_loop().time()
                log.info("NSE cookies refreshed (jar size=%d)", len(self._client.cookies.jar))
            except httpx.HTTPError as e:
                log.warning("NSE cookie refresh failed: %s", e)
                raise

    async def _maybe_refresh(self) -> None:
        now = asyncio.get_event_loop().time()
        if now - self._last_cookie_refresh > self._cookie_refresh_seconds:
            await self._refresh_cookies()

    async def fetch_option_chain(self, symbol: str = "NIFTY") -> dict[str, Any]:
        """
        Fetch full option chain JSON for an index symbol.

        Args:
            symbol: NIFTY | BANKNIFTY | FINNIFTY | MIDCPNIFTY

        Returns:
            Raw JSON dict from NSE.

        Raises:
            httpx.HTTPError on network failure.
            ValueError on empty/malformed response.
        """
        if self._client is None:
            raise RuntimeError("Client not initialized")

        await self._maybe_refresh()

        params = {"symbol": symbol}
        resp = await self._client.get(OPTION_CHAIN_URL, params=params)

        # NSE sometimes returns 401 if cookies expired mid-flight.
        if resp.status_code == 401:
            log.info("Got 401, refreshing cookies and retrying")
            await self._refresh_cookies()
            resp = await self._client.get(OPTION_CHAIN_URL, params=params)

        resp.raise_for_status()
        data = resp.json()

        if not data or "records" not in data:
            raise ValueError(f"Empty/malformed NSE response for {symbol}")

        return data


def parse_option_chain(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize NSE option chain JSON into a flat structure.

    Returns:
        {
          "underlying": float,
          "timestamp_ist": str,
          "expiries": [str, ...],
          "rows": [
            {
              "expiry": str, "strike": float,
              "ce_oi": int, "ce_chg_oi": int, "ce_volume": int,
              "ce_iv": float, "ce_ltp": float, "ce_bid": float, "ce_ask": float,
              "pe_oi": int, "pe_chg_oi": int, "pe_volume": int,
              "pe_iv": float, "pe_ltp": float, "pe_bid": float, "pe_ask": float,
            },
            ...
          ]
        }
    """
    records = raw.get("records", {})
    underlying = records.get("underlyingValue", 0.0)
    timestamp_ist = records.get("timestamp", "")
    expiries = records.get("expiryDates", [])

    rows: list[dict[str, Any]] = []
    for entry in records.get("data", []):
        strike = entry.get("strikePrice")
        expiry = entry.get("expiryDate")
        ce = entry.get("CE") or {}
        pe = entry.get("PE") or {}

        rows.append(
            {
                "expiry": expiry,
                "strike": float(strike) if strike is not None else None,
                "ce_oi": int(ce.get("openInterest") or 0),
                "ce_chg_oi": int(ce.get("changeinOpenInterest") or 0),
                "ce_volume": int(ce.get("totalTradedVolume") or 0),
                "ce_iv": float(ce.get("impliedVolatility") or 0.0),
                "ce_ltp": float(ce.get("lastPrice") or 0.0),
                "ce_bid": float(ce.get("bidprice") or 0.0),
                "ce_ask": float(ce.get("askPrice") or 0.0),
                "pe_oi": int(pe.get("openInterest") or 0),
                "pe_chg_oi": int(pe.get("changeinOpenInterest") or 0),
                "pe_volume": int(pe.get("totalTradedVolume") or 0),
                "pe_iv": float(pe.get("impliedVolatility") or 0.0),
                "pe_ltp": float(pe.get("lastPrice") or 0.0),
                "pe_bid": float(pe.get("bidprice") or 0.0),
                "pe_ask": float(pe.get("askPrice") or 0.0),
            }
        )

    return {
        "underlying": float(underlying) if underlying else 0.0,
        "timestamp_ist": timestamp_ist,
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "expiries": expiries,
        "rows": rows,
    }


async def snapshot_nifty() -> dict[str, Any]:
    """One-shot helper: fetch + parse NIFTY option chain."""
    async with NSEClient() as c:
        raw = await c.fetch_option_chain("NIFTY")
        return parse_option_chain(raw)
