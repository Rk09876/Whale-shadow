"""
NSE option chain scraper — v0.2 patch.

Changes from v0.1:
  - Three-step cookie warmup (root + /option-chain HTML + a no-op API hit).
  - Retry-on-empty-body: NSE frequently returns HTTP 200 with an empty
    payload until cookies fully propagate. We now treat that as transient
    and retry once with a fresh cookie warmup.
  - Slightly randomized delays between warmup steps (NSE's WAF is twitchy
    about robotic timing).
"""

from __future__ import annotations

import asyncio
import logging
import random
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
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Referer": f"{NSE_BASE}/option-chain",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": (
        '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
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
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            http2=True,
        )
        await self._refresh_cookies()
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _refresh_cookies(self) -> None:
        """
        Three-step warmup: root → option-chain HTML → marketStatus API.

        NSE's WAF assigns cookies progressively. Hitting the JSON API too
        early returns an empty body even with HTTP 200.
        """
        if self._client is None:
            raise RuntimeError("Client not initialized")

        async with self._lock:
            try:
                # Step 1 — homepage. May 403, but cookies still get set.
                r1 = await self._client.get(NSE_BASE)
                log.debug("Warmup step 1 (root): %s", r1.status_code)
                await asyncio.sleep(0.4 + random.random() * 0.4)

                # Step 2 — option chain HTML page.
                r2 = await self._client.get(f"{NSE_BASE}/option-chain")
                log.debug("Warmup step 2 (option-chain html): %s", r2.status_code)
                await asyncio.sleep(0.4 + random.random() * 0.4)

                # Step 3 — a cheap API ping that proves cookies work.
                r3 = await self._client.get(
                    f"{NSE_BASE}/api/marketStatus"
                )
                log.debug("Warmup step 3 (marketStatus): %s", r3.status_code)

                self._last_cookie_refresh = asyncio.get_event_loop().time()
                jar_size = len(self._client.cookies.jar)
                log.info("NSE cookies refreshed (jar=%d)", jar_size)
            except httpx.HTTPError as e:
                log.warning("NSE cookie refresh failed: %s", e)
                # Don't re-raise — partial cookies may still work.

    async def _maybe_refresh(self) -> None:
        now = asyncio.get_event_loop().time()
        if now - self._last_cookie_refresh > self._cookie_refresh_seconds:
            await self._refresh_cookies()

    async def fetch_option_chain(
        self, symbol: str = "NIFTY", *, _retry: bool = True
    ) -> dict[str, Any]:
        """
        Fetch full option chain JSON for an index symbol.

        Retries once on empty/401 response after re-warming cookies.
        """
        if self._client is None:
            raise RuntimeError("Client not initialized")

        await self._maybe_refresh()

        params = {"symbol": symbol}
        resp = await self._client.get(OPTION_CHAIN_URL, params=params)

        if resp.status_code == 401:
            log.info("Got 401, refreshing cookies and retrying")
            await self._refresh_cookies()
            resp = await self._client.get(OPTION_CHAIN_URL, params=params)

        resp.raise_for_status()

        # NSE sometimes returns 200 + empty body. Treat as transient.
        try:
            data = resp.json()
        except Exception:
            data = None

        if not data or "records" not in data or not data["records"].get("data"):
            log.warning(
                "NSE returned empty/malformed for %s (status=%s, len=%d). "
                "%s",
                symbol,
                resp.status_code,
                len(resp.content),
                "Retrying once after fresh warmup." if _retry else "Giving up.",
            )
            if _retry:
                await asyncio.sleep(1.5 + random.random())
                await self._refresh_cookies()
                return await self.fetch_option_chain(symbol, _retry=False)
            raise ValueError(f"Empty/malformed NSE response for {symbol}")

        return data


def parse_option_chain(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize NSE option chain JSON into a flat structure.
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
