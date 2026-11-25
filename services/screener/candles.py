"""Timeframes, date-range paging windows, candle parsing, and completed-candle filter."""
from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.screener.models import Candle
from services.screener.timeutil import SESSION_CLOSE, at, from_epoch

SESSION_MINUTES = 375  # 09:15 - 15:30 IST
DAILY_SETTLE = dt.timedelta(minutes=10)  # D/W/M candle final at 15:40 IST
HISTORY_START_SLACK_DAYS = {"1D": 15, "1W": 21, "1M": 62}


@dataclass(frozen=True)
class Timeframe:
    label: str  # UI label (1D, 1W, 1M, 4H, 2H, 1H, 30m, 15m)
    indstocks_interval: str  # INDstocks /market/historical/{interval}
    openalgo_interval: str  # OpenAlgo history_service interval
    max_days: int  # max window per INDstocks call
    minutes: int = 0  # intraday size; 0 for D/W/M

    @property
    def intraday(self) -> bool:
        return self.minutes > 0


TIMEFRAMES: Dict[str, Timeframe] = {
    tf.label: tf
    for tf in (
        Timeframe("1D", "1day", "D", 365),
        Timeframe("1W", "1week", "W", 365),
        Timeframe("1M", "1month", "M", 365),
        Timeframe("4H", "240minute", "4h", 15, 240),
        Timeframe("2H", "120minute", "2h", 15, 120),
        Timeframe("1H", "60minute", "1h", 15, 60),
        Timeframe("30m", "30minute", "30m", 7, 30),
        Timeframe("15m", "15minute", "15m", 7, 15),
    )
}


def get_timeframe(label: str) -> Timeframe:
    try:
        return TIMEFRAMES[label]
    except KeyError:
        raise ValueError(f"Unknown timeframe {label!r}. Choose one of {', '.join(TIMEFRAMES)}")


def calendar_days_needed(tf: Timeframe, candles: int) -> int:
    """Calendar days that comfortably contain `candles` candles (weekends + holidays)."""
    if tf.label == "1D":
        return int(candles * 1.45) + 10
    if tf.label == "1W":
        return candles * 7 + 14
    if tf.label == "1M":
        return candles * 31 + 62
    per_day = math.ceil(SESSION_MINUTES / tf.minutes)
    return int(math.ceil(candles / per_day) * 1.45) + 4


def windows(
    tf: Timeframe,
    candles: int,
    end: dt.datetime,
    max_windows: int = 40,
) -> List[Tuple[int, int]]:
    """Newest-first list of (start_ms, end_ms) windows, each <= interval's maximum."""
    days = calendar_days_needed(tf, candles)
    n = min(max_windows, max(1, math.ceil(days / tf.max_days)))
    out: List[Tuple[int, int]] = []
    hi = end
    for _ in range(n):
        lo = hi - dt.timedelta(days=tf.max_days)
        out.append((int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)))
        hi = lo
    return out


def candle_end(tf: Timeframe, ts: int) -> dt.datetime:
    """When a candle that opened at `ts` is final in IST."""
    start = from_epoch(ts)
    day = start.date()
    close = at(day, SESSION_CLOSE)
    if tf.intraday:
        return min(start + dt.timedelta(minutes=tf.minutes), close)
    if tf.label == "1D":
        return close + DAILY_SETTLE
    if tf.label == "1W":
        # Weekly candle stamped on a weekend (e.g. Sunday Muhurat) finishes on the following Friday.
        wd = day.weekday()
        friday = day + dt.timedelta(days=(4 - wd) if wd <= 4 else (11 - wd))
        return at(friday, SESSION_CLOSE) + DAILY_SETTLE
    last = calendar.monthrange(day.year, day.month)[1]
    return at(dt.date(day.year, day.month, last), SESSION_CLOSE) + DAILY_SETTLE


FORMING_MODES = {
    "off": "Completed candles only",
    "final": "Forming candle in its final stretch",
    "always": "Always include the forming candle",
}
FINAL_DAILY_FROM = dt.time(15, 0)  # 1D: last 30 minutes of session
FINAL_INTRADAY_SHARE = 0.25  # intraday: last quarter of candle


def last_weekday(d: dt.date) -> dt.date:
    while d.weekday() > 4:
        d -= dt.timedelta(days=1)
    return d


def in_final_stretch(tf: Timeframe, ts: int, now: dt.datetime) -> bool:
    """True when candle at `ts` is complete or in the final stretch of its session."""
    end = candle_end(tf, ts)
    if now >= end:
        return True
    start = from_epoch(ts)
    if tf.intraday:
        return now >= end - (end - start) * FINAL_INTRADAY_SHARE
    if tf.label == "1D":
        return now.date() == start.date() and now.time() >= FINAL_DAILY_FROM
    if tf.label == "1W":
        return now.date() == end.date()
    return now.date() >= last_weekday(end.date())


def forming_mode(value: Any) -> str:
    if value is True:
        return "always"
    if value is False or value is None:
        return "off"
    return value if value in FORMING_MODES else "off"


def completed(
    tf: Timeframe,
    candles: Sequence[Candle],
    now: dt.datetime,
    mode: str = "off",
) -> List[Candle]:
    """Drop trailing candle still forming at `now`, unless `mode` keeps it."""
    out = list(candles)
    if out and candle_end(tf, out[-1].ts) > now:
        keep = mode == "always" or (mode == "final" and in_final_stretch(tf, out[-1].ts, now))
        if not keep:
            out.pop()
    while len(out) > 1 and candle_end(tf, out[-2].ts) > now:
        out.pop(-2)
    return out


def _num(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_candles(raw: Any) -> List[Candle]:
    """Parse INDstocks ({ts,o,h,l,c,v} or [ts,o,h,l,c,v]) or OpenAlgo ({timestamp,open,...}) rows."""
    by_ts: Dict[int, Candle] = {}
    for item in raw or []:
        if isinstance(item, dict):
            if "ts" in item:
                vals = [item.get(k) for k in ("ts", "o", "h", "l", "c", "v")]
            else:
                vals = [item.get(k) for k in ("timestamp", "open", "high", "low", "close", "volume")]
        elif isinstance(item, (list, tuple)):
            vals = list(item)[:6] + [None] * max(0, 6 - len(item))
        else:
            continue
        ts, o, h, l, c, v = (_num(x) for x in vals)
        if ts is None or c is None:
            continue
        # Normalize millisecond epoch timestamps to seconds when encountered
        if ts > 1e11:
            ts = ts / 1000.0
        o = c if o is None else o
        h = max(x for x in (h, o, c) if x is not None)
        l = min(x for x in (l, o, c) if x is not None)
        by_ts[int(ts)] = Candle(int(ts), o, h, l, c, v or 0.0)
    return [by_ts[k] for k in sorted(by_ts)]
