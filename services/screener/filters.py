"""Common post-screener filters: Price, Wilder RSI, Avg Volume, and Avg Traded Value (Cr).

Exact port of stock_screener/src/filters.py.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.screener import mtf
from services.screener.candles import candle_end, get_timeframe, in_final_stretch
from services.screener.indicators import rsi_wilder
from services.screener.models import Candle, ColumnSpec, ResultRow, SettingSpec
from services.screener.timeutil import from_epoch

GROUP = "Filters (all screeners)"

FILTER_SPECS: List[SettingSpec] = [
    SettingSpec(
        "min_price",
        "Min price (Rs)",
        "Keep only stocks whose last close is at least this. 0 = off.",
        "float",
        0,
        1000000,
        group=GROUP,
    ),
    SettingSpec(
        "rsi_period",
        "RSI period",
        "Candles used for RSI (Wilder, same as TradingView). Calculated on the last candle of the chosen timeframe.",
        "int",
        2,
        100,
        group=GROUP,
    ),
    SettingSpec(
        "rsi_min",
        "RSI min",
        "Keep only stocks whose RSI is at least this. 0 = no lower limit.",
        "float",
        0,
        100,
        group=GROUP,
    ),
    SettingSpec(
        "rsi_max",
        "RSI max",
        "Keep only stocks whose RSI is at most this. 100 = no upper limit.",
        "float",
        0,
        100,
        group=GROUP,
    ),
    SettingSpec(
        "avg_period",
        "Avg volume period",
        "Candles averaged for volume and traded value (the last N candles of the chosen timeframe, e.g. 20 daily candles).",
        "int",
        1,
        500,
        group=GROUP,
    ),
    SettingSpec(
        "min_avg_volume",
        "Min avg volume",
        "Keep only stocks whose average volume per candle (shares) is at least this. 0 = off.",
        "float",
        0,
        1e10,
        group=GROUP,
    ),
    SettingSpec(
        "min_avg_value_cr",
        "Min avg traded value (Cr)",
        "Keep only stocks whose average traded value per candle (close x volume, in Rs crore) is at least this. 0 = off.",
        "float",
        0,
        100000,
        group=GROUP,
    ),
]
FILTER_KEYS = [s.key for s in FILTER_SPECS]


def validate_filters(values: Dict[str, Any]) -> List[str]:
    if values.get("rsi_min", 0) > values.get("rsi_max", 100):
        return ["RSI min must not be above RSI max."]
    return []


def rsi_active(settings: Dict[str, Any]) -> bool:
    return settings.get("rsi_min", 0) > 0 or settings.get("rsi_max", 100) < 100


def price_verdict(price: Optional[float], settings: Dict[str, Any]) -> Tuple[bool, str]:
    mp = settings.get("min_price", 0)
    if mp <= 0 or price is None:
        return True, ""
    if price < mp:
        return False, f"Price {price:.2f} below {mp:g}"
    return True, ""


def liquidity_active(settings: Dict[str, Any]) -> bool:
    return settings.get("min_avg_volume", 0) > 0 or settings.get("min_avg_value_cr", 0) > 0


def columns(settings: Dict[str, Any]) -> List[ColumnSpec]:
    p = int(settings.get("rsi_period", 14))
    n = int(settings.get("avg_period", 20))
    return [
        ColumnSpec(
            "last_date",
            "Candle judged",
            tooltip="The candle every 'last candle' value refers to (Last close, Last candle, Reaction, RSI). "
            "Its opening date: for 1W the week's first trading day, for 1M the month's. '(forming)' = not complete "
            "yet; '(forming, final stretch)' = e.g. this week's candle on Friday.",
        ),
        ColumnSpec("rsi", f"RSI({p})", "num", f"RSI({p}) of the last candle (Wilder)"),
        ColumnSpec("avg_volume", f"Avg vol ({n})", "int", f"Average volume of the last {n} candles"),
        ColumnSpec(
            "avg_value_cr",
            f"Avg value Cr ({n})",
            "num",
            f"Average close x volume of the last {n} candles, Rs crore",
        ),
    ]


def averages(candles: Sequence[Candle], period: int) -> Tuple[Optional[float], Optional[float]]:
    """(average volume, average traded value in Rs crore) of the last `period` candles;
    (None, None) when fewer candles exist."""
    if period < 1 or len(candles) < period:
        return None, None
    last = candles[-period:]
    vol = sum(c.v for c in last) / period
    value = sum(c.c * c.v for c in last) / period / 1e7
    return vol, value


def liquidity_verdict(
    vol: Optional[float], value: Optional[float], settings: Dict[str, Any]
) -> Tuple[bool, str]:
    if not liquidity_active(settings):
        return True, ""
    if vol is None:
        return False, "Avg volume n/a (too little history)"
    mv, mval = settings.get("min_avg_volume", 0), settings.get("min_avg_value_cr", 0)
    if mv > 0 and vol < mv:
        return False, f"Avg vol {vol:,.0f} below {mv:,.0f}"
    if mval > 0 and value is not None and value < mval:
        return False, f"Avg value {value:.2f} Cr below {mval:g} Cr"
    return True, ""


def merge_columns(strategy_columns: Sequence[ColumnSpec], settings: Dict[str, Any]) -> List[ColumnSpec]:
    """Strategy columns with the common filter columns inserted after 'Last close' (or at the end),
    plus any active MTF columns."""
    cols = list(strategy_columns)
    keys = [c.key for c in cols]
    at = keys.index("last_close") + 1 if "last_close" in keys else len(cols)
    return cols[:at] + columns(settings) + cols[at:] + mtf.columns(settings)


def last_rsi(candles: Sequence[Candle], period: int) -> Optional[float]:
    vals = rsi_wilder([c.c for c in candles], period)
    return vals[-1] if vals else None


def rsi_verdict(rsi: Optional[float], settings: Dict[str, Any]) -> Tuple[bool, str]:
    """(passes, reason)."""
    if not rsi_active(settings):
        return True, ""
    if rsi is None:
        return False, "RSI n/a (too little history)"
    lo, hi = settings.get("rsi_min", 0), settings.get("rsi_max", 100)
    if rsi < lo:
        return False, f"RSI {rsi:.1f} below {lo:g}"
    if rsi > hi:
        return False, f"RSI {rsi:.1f} above {hi:g}"
    return True, ""


def candle_label(
    candle: Candle, settings: Dict[str, Any], now: Optional[dt.datetime] = None
) -> str:
    """'2026-11-02' (D/W/M) or '2026-11-06 14:15' (intraday); ' (forming)' when not complete at `now`."""
    try:
        tf = get_timeframe(settings.get("timeframe", "1D"))
    except ValueError:
        return ""
    text = from_epoch(candle.ts).strftime("%Y-%m-%d %H:%M" if tf.intraday else "%Y-%m-%d")
    if now is not None and candle_end(tf, candle.ts) > now:
        text += " (forming, final stretch)" if in_final_stretch(tf, candle.ts, now) else " (forming)"
    return text


def apply(
    candles: Sequence[Candle],
    rows: List[ResultRow],
    settings: Dict[str, Any],
    keep_rejected: bool,
    now: Optional[dt.datetime] = None,
) -> Tuple[List[ResultRow], int]:
    """Adds the RSI / average volume / traded value columns to every row and applies the filters.
    Returns (rows to show, number of matches the filters removed)."""
    if not rows:
        return rows, 0
    rsi = last_rsi(candles, int(settings.get("rsi_period", 14)))
    vol, value = averages(candles, int(settings.get("avg_period", 20)))
    ok_r, why_r = rsi_verdict(rsi, settings)
    ok_l, why_l = liquidity_verdict(vol, value, settings)
    ok_p, why_p = price_verdict(candles[-1].c if candles else None, settings)
    ok = ok_r and ok_l and ok_p
    why = " | ".join(w for w in (why_p, why_l, why_r) if w)
    out: List[ResultRow] = []
    removed = 0
    label = candle_label(candles[-1], settings, now) if candles else ""
    for r in rows:
        r.values["last_date"] = label
        r.values["rsi"] = round(rsi, 2) if rsi is not None else None
        r.values["avg_volume"] = round(vol) if vol is not None else None
        r.values["avg_value_cr"] = round(value, 2) if value is not None else None
        if ok or not r.valid:
            out.append(r)
            continue
        removed += 1
        if keep_rejected:
            r.values["status"] = f"{r.values.get('status', '')} | {why}".strip(" |")
            r.valid = False
            out.append(r)
    return out, removed
