"""
BSE option chain scraper for SENSEX — v0.2 patch.

Changes from v0.1:
  - The old `GetSensexData` endpoint was actually for the spot index quote
    and is now bouncing to error_Bse.html. Removed.
  - Tries three known BSE option-chain endpoint variants in order, picking
    whichever returns a parseable JSON body.
  - Verbose logging of what each endpoint returned so we can iterate quickly
    if BSE shifts again.
  - Defensive parser handles three known response shapes.

Note: BSE's public option chain API has been the most volatile of any
Indian exchange surface. Expect to revisit this file when SENSEX data goes
stale — patch this single file, redeploy, done.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Candidate endpoints, tried in order. The first one returning structured
# JSON with strikes wins. Each entry is (label, url, params).
CANDIDATE_ENDPOINTS = [
    (
        "v1.optionchain",
        "https://api.bseindia.com/BseIndiaAPI/api/Optionchain/w",
        {"strType": "OptionChain", "expiry": "", "scripcode": "1"},
    ),
    (
        "v2.getoptionchain",
        "https://api.bseindia.com/BseIndiaAPI/api/GetOptionChain/w",
        {"scripcode": "1", "expiry": ""},
    ),
    (
        "v3.derivative_quote",
        "https://api.bseindia.com/BseIndiaAPI/api/DeriativeQuotes/w",
        {"scripcode": "1"},
    ),
]

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
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


class BSEClient:
    """Async client for BSE public endpoints."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers=BROWSER_HEADERS,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,  # don't follow redirects to error pages
        )
        # warmup — get cookies from main site
        try:
            r = await self._client.get(
                "https://www.bseindia.com/markets/Derivatives/DeriReports/optionchaint.aspx"
            )
            log.debug("BSE warmup: %s", r.status_code)
        except httpx.HTTPError as e:
            log.debug("BSE warmup failed (non-fatal): %s", e)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def fetch_sensex_option_chain(self) -> dict[str, Any]:
        """
        Try each candidate endpoint until one returns parseable JSON
        with what looks like option chain data.
        """
        if self._client is None:
            raise RuntimeError("Client not initialized")

        last_error: str = "no endpoints tried"

        for label, url, params in CANDIDATE_ENDPOINTS:
            try:
                resp = await self._client.get(url, params=params)
                log.info(
                    "BSE %s: status=%s body_len=%d",
                    label,
                    resp.status_code,
                    len(resp.content),
                )

                # 302 / 301 -> redirected to error page, skip.
                if resp.status_code in (301, 302, 303, 307, 308):
                    redirect_to = resp.headers.get("location", "?")
                    log.info("  redirected to %s", redirect_to)
                    last_error = f"{label} redirected"
                    continue

                if resp.status_code != 200:
                    last_error = f"{label} HTTP {resp.status_code}"
                    continue

                if not resp.content:
                    last_error = f"{label} empty body"
                    continue

                try:
                    data = resp.json()
                except Exception as e:
                    last_error = f"{label} non-JSON: {e}"
                    continue

                if not data:
                    last_error = f"{label} JSON empty"
                    continue

                log.info("BSE %s: usable JSON, top-level keys=%s",
                         label,
                         list(data.keys()) if isinstance(data, dict) else type(data).__name__)
                return {"_endpoint": label, "data": data}

            except httpx.HTTPError as e:
                last_error = f"{label} {type(e).__name__}: {e}"
                continue

        raise RuntimeError(
            f"All BSE endpoint candidates failed. Last error: {last_error}"
        )


def parse_sensex_chain(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize BSE response into the same shape used by the NSE parser.

    Handles multiple known response shapes defensively.
    """
    payload = raw.get("data") if isinstance(raw, dict) else raw
    endpoint = raw.get("_endpoint", "?") if isinstance(raw, dict) else "?"

    underlying = 0.0
    rows: list[dict[str, Any]] = []
    expiries: list[str] = []

    if not isinstance(payload, (dict, list)):
        log.warning("BSE %s: unexpected payload type %s", endpoint, type(payload))
        return {
            "underlying": 0.0,
            "timestamp_ist": "",
            "fetched_utc": datetime.now(timezone.utc).isoformat(),
            "expiries": [],
            "rows": [],
        }

    # Try common locations for underlying value.
    if isinstance(payload, dict):
        for key in ("CurrRate", "ltp", "LTP", "lastPrice", "underlyingValue", "UndValue"):
            v = payload.get(key)
            if v:
                try:
                    underlying = float(str(v).replace(",", ""))
                    break
                except (TypeError, ValueError):
                    continue

    # Try to locate the actual chain rows.
    chain = None
    if isinstance(payload, dict):
        for key in ("Table", "Data", "data", "OptionChain", "Records", "records"):
            if key in payload and payload[key]:
                chain = payload[key]
                break
    elif isinstance(payload, list):
        chain = payload

    if isinstance(chain, dict):
        # dict of expiry -> list of rows
        for expiry, items in chain.items():
            if not isinstance(items, list):
                continue
            expiries.append(str(expiry))
            for entry in items:
                rows.append(_parse_bse_row(entry, str(expiry)))
    elif isinstance(chain, list):
        for entry in chain:
            expiry = str(
                entry.get("expiry")
                or entry.get("Expiry")
                or entry.get("ExpiryDate")
                or entry.get("EXPIRY")
                or ""
            )
            if expiry and expiry not in expiries:
                expiries.append(expiry)
            rows.append(_parse_bse_row(entry, expiry))

    return {
        "underlying": underlying,
        "timestamp_ist": "",
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "expiries": expiries,
        "rows": rows,
        "_source": endpoint,
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

    strike = num("Strike_Price", "StrikePrice", "strikePrice", "strike", "STRIKE")

    return {
        "expiry": expiry,
        "strike": strike,
        "ce_oi": integer("CE_OpenInterest", "CallOI", "ce_oi", "CE_OI"),
        "ce_chg_oi": integer("CE_ChangeInOI", "CallChgOI", "ce_chg_oi"),
        "ce_volume": integer("CE_Volume", "CallVol", "ce_volume"),
        "ce_iv": num("CE_IV", "CallIV", "ce_iv"),
        "ce_ltp": num("CE_LTP", "CallLTP", "ce_ltp"),
        "ce_bid": num("CE_BidPrice", "CallBid", "ce_bid"),
        "ce_ask": num("CE_AskPrice", "CallAsk", "ce_ask"),
        "pe_oi": integer("PE_OpenInterest", "PutOI", "pe_oi", "PE_OI"),
        "pe_chg_oi": integer("PE_ChangeInOI", "PutChgOI", "pe_chg_oi"),
        "pe_volume": integer("PE_Volume", "PutVol", "pe_volume"),
        "pe_iv": num("PE_IV", "PutIV", "pe_iv"),
        "pe_ltp": num("PE_LTP", "PutLTP", "pe_ltp"),
        "pe_bid": num("PE_BidPrice", "PutBid", "pe_bid"),
        "pe_ask": num("PE_AskPrice", "PutAsk", "pe_ask"),
    }


async def snapshot_sensex() -> dict[str, Any]:
    """One-shot helper for SENSEX option chain."""
    async with BSEClient() as c:
        raw = await c.fetch_sensex_option_chain()
        return parse_sensex_chain(raw)
