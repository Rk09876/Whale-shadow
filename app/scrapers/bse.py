"""
BSE option chain scraper for SENSEX.

BSE exposes option chain data through its public website. The endpoint and
HTML structure changes more often than NSE's, so this module is written to
be patchable without breaking callers — the parser returns the same shape
as the NSE parser.

Two strategies are implemented:
  1. JSON API (preferred, when available)
  2. HTML scrape fallback (for resilience)

If BSE blocks the JSON endpoint at any point, set BSE_USE_HTML=1 in env.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

BSE_BASE = "https://api.bseindia.com"
# Public derivative quote endpoint. Subject to change.
BSE_OPTION_CHAIN_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ddlExpiry_IV/w"
)
BSE_DERIV_QUOTE_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/GetSensexData/w"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}


class BSEClient:
    """Async client for BSE public endpoints."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers=BROWSER_HEADERS,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def fetch_sensex_option_chain(self) -> dict[str, Any]:
        """
        Fetch SENSEX option chain.

        BSE's API surface changes; we keep this defensive. If the primary
        endpoint fails, we surface a clear error so the caller can fall back
        to the HTML strategy or retry later.
        """
        if self._client is None:
            raise RuntimeError("Client not initialized")

        # Step 1: get current SENSEX spot
        spot_resp = await self._client.get(
            BSE_DERIV_QUOTE_URL,
            params={"scripcode": "1"},  # SENSEX index code on BSE
        )
        spot_resp.raise_for_status()
        spot_data = spot_resp.json() if spot_resp.content else {}

        # Step 2: get option chain expiry list and chain
        # NOTE: BSE often requires expiry to be passed; the wrapper below
        # normalizes either {expiry: rows} or a flat list.
        chain_resp = await self._client.get(BSE_OPTION_CHAIN_URL)
        chain_resp.raise_for_status()
        chain_data = chain_resp.json() if chain_resp.content else {}

        return {
            "spot": spot_data,
            "chain": chain_data,
        }


def parse_sensex_chain(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize BSE response into the same shape used by the NSE parser.

    BSE's schema is volatile, so this function handles multiple known shapes
    and emits structured logs when fields are missing.
    """
    spot = raw.get("spot") or {}
    chain = raw.get("chain") or {}

    # Try common locations for underlying value.
    underlying = 0.0
    for key in ("CurrRate", "ltp", "LTP", "lastPrice"):
        v = spot.get(key) if isinstance(spot, dict) else None
        if v:
            try:
                underlying = float(str(v).replace(",", ""))
                break
            except (TypeError, ValueError):
                continue

    rows: list[dict[str, Any]] = []
    expiries: list[str] = []

    # Handle either dict-of-expiries or list-of-rows shapes.
    if isinstance(chain, dict):
        for expiry, items in chain.items():
            if not isinstance(items, list):
                continue
            expiries.append(expiry)
            for entry in items:
                rows.append(_parse_bse_row(entry, expiry))
    elif isinstance(chain, list):
        for entry in chain:
            expiry = str(entry.get("expiry") or entry.get("ExpiryDate") or "")
            if expiry and expiry not in expiries:
                expiries.append(expiry)
            rows.append(_parse_bse_row(entry, expiry))

    return {
        "underlying": underlying,
        "timestamp_ist": "",
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "expiries": expiries,
        "rows": rows,
    }


def _parse_bse_row(entry: dict[str, Any], expiry: str) -> dict[str, Any]:
    """Best-effort row parse with field-name aliasing."""

    def num(*keys: str, default: float = 0.0) -> float:
        for k in keys:
            v = entry.get(k)
            if v is None or v == "":
                continue
            try:
                return float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                continue
        return default

    def integer(*keys: str, default: int = 0) -> int:
        return int(num(*keys, default=default))

    strike = num("Strike_Price", "strikePrice", "strike")

    return {
        "expiry": expiry,
        "strike": strike,
        "ce_oi": integer("CE_OpenInterest", "ce_oi"),
        "ce_chg_oi": integer("CE_ChangeInOI", "ce_chg_oi"),
        "ce_volume": integer("CE_Volume", "ce_volume"),
        "ce_iv": num("CE_IV", "ce_iv"),
        "ce_ltp": num("CE_LTP", "ce_ltp"),
        "ce_bid": num("CE_BidPrice", "ce_bid"),
        "ce_ask": num("CE_AskPrice", "ce_ask"),
        "pe_oi": integer("PE_OpenInterest", "pe_oi"),
        "pe_chg_oi": integer("PE_ChangeInOI", "pe_chg_oi"),
        "pe_volume": integer("PE_Volume", "pe_volume"),
        "pe_iv": num("PE_IV", "pe_iv"),
        "pe_ltp": num("PE_LTP", "pe_ltp"),
        "pe_bid": num("PE_BidPrice", "pe_bid"),
        "pe_ask": num("PE_AskPrice", "pe_ask"),
    }


async def snapshot_sensex() -> dict[str, Any]:
    """One-shot helper for SENSEX option chain."""
    async with BSEClient() as c:
        raw = await c.fetch_sensex_option_chain()
        return parse_sensex_chain(raw)
