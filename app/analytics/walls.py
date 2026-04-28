"""
Wall detection — the analytic core.

Given a sequence of option chain snapshots over time, identify:
  1. Static walls    — strikes with abnormally high absolute OI (institutions
                       are committed there).
  2. Building walls  — strikes seeing rapid OI accumulation in the last N
                       snapshots (real-time whale entry).
  3. Dissolving walls — strikes seeing OI exit (retreat / position unwind).
  4. Whale grade     — magnitude tier (white < orange < magenta) used by the
                       heatmap to colour each cell.

Design choices:
  - Tiers are computed from the cross-strike distribution itself (z-scores),
    not absolute thresholds. This adapts automatically to NIFTY vs SENSEX
    vs different expiries with different OI magnitudes.
  - All functions are pure and operate on plain dicts/lists, so they're
    trivial to unit-test and re-run on stored snapshots.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Literal

WhaleGrade = Literal["normal", "large", "whale"]


def classify_grade(value: float, mean: float, std: float) -> WhaleGrade:
    """
    Tier a single OI / OI-delta value relative to the cross-strike distribution.

      z < 1.5  -> normal  (white)
      z < 3.0  -> large   (orange)
      z >= 3.0 -> whale   (magenta)
    """
    if std <= 0:
        return "normal"
    z = (abs(value) - mean) / std
    if z >= 3.0:
        return "whale"
    if z >= 1.5:
        return "large"
    return "normal"


def detect_walls(
    rows: list[dict[str, Any]],
    *,
    expiry_filter: str | None = None,
    side: Literal["both", "ce", "pe"] = "both",
) -> dict[str, Any]:
    """
    Detect OI walls in a single snapshot.

    Args:
        rows:           parsed option chain rows (from scrapers.nse / .bse).
        expiry_filter:  if set, restrict to one expiry.
        side:           which option side(s) to consider.

    Returns:
        {
          "walls": [
            {"strike": float, "side": "CE"|"PE", "oi": int,
             "chg_oi": int, "grade": "large"|"whale"},
            ...
          ],
          "stats": {
            "ce_oi_mean": float, "ce_oi_std": float,
            "pe_oi_mean": float, "pe_oi_std": float,
            "max_pain": float | None,
          }
        }
    """
    filtered = [r for r in rows if expiry_filter is None or r.get("expiry") == expiry_filter]

    ce_ois = [r["ce_oi"] for r in filtered if r.get("ce_oi", 0) > 0]
    pe_ois = [r["pe_oi"] for r in filtered if r.get("pe_oi", 0) > 0]

    ce_mean = statistics.fmean(ce_ois) if ce_ois else 0.0
    ce_std = statistics.pstdev(ce_ois) if len(ce_ois) > 1 else 0.0
    pe_mean = statistics.fmean(pe_ois) if pe_ois else 0.0
    pe_std = statistics.pstdev(pe_ois) if len(pe_ois) > 1 else 0.0

    walls: list[dict[str, Any]] = []
    for r in filtered:
        if side in ("both", "ce"):
            grade = classify_grade(r.get("ce_oi", 0), ce_mean, ce_std)
            if grade != "normal":
                walls.append(
                    {
                        "strike": r["strike"],
                        "side": "CE",
                        "oi": r["ce_oi"],
                        "chg_oi": r.get("ce_chg_oi", 0),
                        "grade": grade,
                    }
                )
        if side in ("both", "pe"):
            grade = classify_grade(r.get("pe_oi", 0), pe_mean, pe_std)
            if grade != "normal":
                walls.append(
                    {
                        "strike": r["strike"],
                        "side": "PE",
                        "oi": r["pe_oi"],
                        "chg_oi": r.get("pe_chg_oi", 0),
                        "grade": grade,
                    }
                )

    walls.sort(key=lambda w: (-w["oi"], w["strike"]))

    return {
        "walls": walls,
        "stats": {
            "ce_oi_mean": ce_mean,
            "ce_oi_std": ce_std,
            "pe_oi_mean": pe_mean,
            "pe_oi_std": pe_std,
            "max_pain": compute_max_pain(filtered),
        },
    }


def compute_max_pain(rows: list[dict[str, Any]]) -> float | None:
    """
    Max pain = strike at which the total OI value (writers' loss) is minimized.

    This is the price level option writers (typically institutions) would
    most prefer at expiry. It's a soft magnetic level rather than a hard wall,
    but it complements the wall map nicely.
    """
    if not rows:
        return None

    strikes = sorted({r["strike"] for r in rows if r.get("strike") is not None})
    if not strikes:
        return None

    pain_at: dict[float, float] = {}
    for s in strikes:
        total = 0.0
        for r in rows:
            k = r.get("strike")
            if k is None:
                continue
            ce_oi = r.get("ce_oi", 0)
            pe_oi = r.get("pe_oi", 0)
            if s > k:
                total += (s - k) * ce_oi
            elif s < k:
                total += (k - s) * pe_oi
        pain_at[s] = total

    return min(pain_at, key=pain_at.get)


def detect_oi_changes(
    prev_rows: list[dict[str, Any]],
    curr_rows: list[dict[str, Any]],
    *,
    expiry_filter: str | None = None,
) -> list[dict[str, Any]]:
    """
    Compare two snapshots and return per-strike OI deltas, graded.

    This is what powers the time-series heatmap: each (strike, time) cell
    gets coloured by its delta-grade against the cross-strike distribution
    at that timestamp.
    """
    def index(rows: list[dict[str, Any]]) -> dict[tuple[str, float], dict[str, Any]]:
        out: dict[tuple[str, float], dict[str, Any]] = {}
        for r in rows:
            if expiry_filter and r.get("expiry") != expiry_filter:
                continue
            key = (r.get("expiry", ""), r.get("strike", math.nan))
            if not math.isnan(key[1]):
                out[key] = r
        return out

    prev = index(prev_rows)
    curr = index(curr_rows)

    ce_deltas: list[int] = []
    pe_deltas: list[int] = []
    deltas_by_key: dict[tuple[str, float], dict[str, int]] = {}

    for key, c in curr.items():
        p = prev.get(key)
        if p is None:
            continue
        d_ce = c.get("ce_oi", 0) - p.get("ce_oi", 0)
        d_pe = c.get("pe_oi", 0) - p.get("pe_oi", 0)
        ce_deltas.append(abs(d_ce))
        pe_deltas.append(abs(d_pe))
        deltas_by_key[key] = {"d_ce": d_ce, "d_pe": d_pe}

    ce_mean = statistics.fmean(ce_deltas) if ce_deltas else 0.0
    ce_std = statistics.pstdev(ce_deltas) if len(ce_deltas) > 1 else 0.0
    pe_mean = statistics.fmean(pe_deltas) if pe_deltas else 0.0
    pe_std = statistics.pstdev(pe_deltas) if len(pe_deltas) > 1 else 0.0

    out: list[dict[str, Any]] = []
    for (expiry, strike), d in deltas_by_key.items():
        out.append(
            {
                "expiry": expiry,
                "strike": strike,
                "d_ce": d["d_ce"],
                "d_pe": d["d_pe"],
                "ce_grade": classify_grade(d["d_ce"], ce_mean, ce_std),
                "pe_grade": classify_grade(d["d_pe"], pe_mean, pe_std),
            }
        )
    out.sort(key=lambda x: x["strike"])
    return out
