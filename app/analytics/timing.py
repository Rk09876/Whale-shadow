"""
Market timing context for the dashboard side panel.

Standalone implementation — does not depend on AGIF. Three layers:

  1. Session phase    — pre-open / opening / mid / pre-close / post-close.
                        These map roughly to typical institutional behaviour
                        on NSE & BSE.
  2. Hora window      — Vedic 60-min planetary hours from sunrise. Optional;
                        purely informational. Provided because day-of-week +
                        hora is a common timing reference in Indian trading.
  3. Range-guard band — a placeholder field for plugging your own pre-market
                        range estimate. Default is None.

Everything here uses zoneinfo (stdlib) — no external timezone deps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SessionPhase = Literal[
    "pre_open", "opening", "mid_session", "pre_close", "post_close", "closed"
]

# NSE/BSE equity & derivatives session
PRE_OPEN_START = time(9, 0)
MARKET_OPEN = time(9, 15)
OPENING_HOUR_END = time(10, 15)
PRE_CLOSE_START = time(14, 30)
MARKET_CLOSE = time(15, 30)


@dataclass
class TimingContext:
    now_ist: str
    weekday: str
    session_phase: SessionPhase
    minutes_to_close: int
    hora_lord: str
    hora_index: int
    hora_start_ist: str
    hora_end_ist: str
    is_trading_day: bool


# Vedic weekday lords (Sunday=0 in Indian calendar convention here)
_DAY_LORDS = ["Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn"]
# Hora sequence repeats every 7 hours: starts with day-lord, then steps in
# the Chaldean order (Sat, Jup, Mars, Sun, Ven, Mer, Moon backwards).
_HORA_SEQUENCE_DAY = {
    "Sun":     ["Sun", "Venus", "Mercury", "Moon", "Saturn", "Jupiter", "Mars"],
    "Moon":    ["Moon", "Saturn", "Jupiter", "Mars", "Sun", "Venus", "Mercury"],
    "Mars":    ["Mars", "Sun", "Venus", "Mercury", "Moon", "Saturn", "Jupiter"],
    "Mercury": ["Mercury", "Moon", "Saturn", "Jupiter", "Mars", "Sun", "Venus"],
    "Jupiter": ["Jupiter", "Mars", "Sun", "Venus", "Mercury", "Moon", "Saturn"],
    "Venus":   ["Venus", "Mercury", "Moon", "Saturn", "Jupiter", "Mars", "Sun"],
    "Saturn":  ["Saturn", "Jupiter", "Mars", "Sun", "Venus", "Mercury", "Moon"],
}


def _session_phase(now: datetime) -> SessionPhase:
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return "closed"
    t = now.time()
    if t < PRE_OPEN_START:
        return "closed"
    if t < MARKET_OPEN:
        return "pre_open"
    if t < OPENING_HOUR_END:
        return "opening"
    if t < PRE_CLOSE_START:
        return "mid_session"
    if t < MARKET_CLOSE:
        return "pre_close"
    return "post_close"


def _minutes_to_close(now: datetime) -> int:
    if now.weekday() >= 5:
        return 0
    close_dt = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=IST)
    delta = (close_dt - now).total_seconds() / 60
    return max(0, int(delta))


def _approx_sunrise(now: datetime) -> datetime:
    """
    Approximate sunrise for India (averaged ~6:00 AM IST).

    A real implementation would compute sunrise from latitude/longitude
    using ephemeris. For dashboard purposes a constant 6:00 AM is close
    enough — hora windows shift by ~10-15 min across the year.
    """
    return datetime.combine(now.date(), time(6, 0), tzinfo=IST)


def _current_hora(now: datetime) -> tuple[str, int, datetime, datetime]:
    """Return (lord, hora_index_0to23, hora_start, hora_end)."""
    sunrise = _approx_sunrise(now)
    if now < sunrise:
        # Before sunrise -> belongs to previous day's night horas
        sunrise = sunrise - timedelta(days=1)

    elapsed = now - sunrise
    hora_index = int(elapsed.total_seconds() // 3600)  # 0..23
    hora_index = max(0, min(23, hora_index))

    # Day of week using ISO: Mon=0..Sun=6 -> map to our convention Sun=0..Sat=6
    iso_dow = sunrise.weekday()  # 0=Mon
    indian_dow = (iso_dow + 1) % 7  # 0=Sun
    day_lord = _DAY_LORDS[indian_dow]

    sequence = _HORA_SEQUENCE_DAY[day_lord]
    lord = sequence[hora_index % 7]

    hora_start = sunrise + timedelta(hours=hora_index)
    hora_end = hora_start + timedelta(hours=1)
    return lord, hora_index, hora_start, hora_end


def now_context() -> TimingContext:
    """Compute the current timing context for the side panel."""
    now = datetime.now(IST)
    phase = _session_phase(now)
    lord, idx, hs, he = _current_hora(now)

    return TimingContext(
        now_ist=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        weekday=now.strftime("%A"),
        session_phase=phase,
        minutes_to_close=_minutes_to_close(now),
        hora_lord=lord,
        hora_index=idx,
        hora_start_ist=hs.strftime("%H:%M"),
        hora_end_ist=he.strftime("%H:%M"),
        is_trading_day=now.weekday() < 5,
    )
