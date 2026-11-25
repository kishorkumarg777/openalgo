"""IST helpers. All screener session boundaries are in IST (Asia/Kolkata, UTC+05:30)."""
from __future__ import annotations

import datetime as dt
import time
from typing import Callable

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)


def now_ist(clock: Callable[[], float] = time.time) -> dt.datetime:
    return dt.datetime.fromtimestamp(clock(), IST)


def from_epoch(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, IST)


def to_epoch(d: dt.datetime) -> float:
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    return d.timestamp()


def at(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, IST)
