"""Sell-side liquidity (SSL) levels and sweeps — LuxAlgo SMC port (bullish side)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from services.screener.indicators import atr_wilder
from services.screener.models import Candle

LEVEL_CAP = 50


@dataclass
class Level:
    price: float
    index: int  # bar of the (oldest merged) pivot low


@dataclass
class Sweep:
    bar: int  # the candle that swept
    level: float
    level_index: int
    low: float
    close: float


@dataclass
class LiquidityResult:
    sweeps: List[Sweep]  # all bullish SSL sweeps, oldest first
    taken: List[Tuple[int, Level]]  # (bar, level) for every level taken
    active: List[Level]  # untouched levels after the last candle, oldest first
    pivots: List[Level]  # every confirmed pivot low (before merging), oldest first


def pivot_low_at(cs: Sequence[Candle], p: int, length: int) -> bool:
    if p - length < 0 or p + length >= len(cs):
        return False
    lo = cs[p].l
    return all(lo < cs[k].l for k in range(p - length, p)) and all(
        lo <= cs[k].l for k in range(p + 1, p + length + 1)
    )


def run(cs: Sequence[Candle], length: int = 10, merge_atr: float = 0.25) -> LiquidityResult:
    atr14 = atr_wilder(cs, 14)
    levels: List[Level] = []
    sweeps: List[Sweep] = []
    taken: List[Tuple[int, Level]] = []
    pivots: List[Level] = []
    for t, c in enumerate(cs):
        # 1. Take / sweep existing levels (newest first)
        for k in range(len(levels) - 1, -1, -1):
            lv = levels[k]
            if c.l < lv.price:
                if c.c > lv.price:
                    sweeps.append(Sweep(t, lv.price, lv.index, c.l, c.c))
                taken.append((t, lv))
                levels.pop(k)
        # 2. New pivot low confirmed on this bar (pivot at t - length)
        p = t - length
        if pivot_low_at(cs, p, length):
            pivots.append(Level(cs[p].l, p))
            new = Level(cs[p].l, p)
            tol = merge_atr * (atr14[t] or 0.0)
            for k in range(len(levels) - 1, -1, -1):
                if abs(levels[k].price - new.price) <= tol:
                    new = Level(min(new.price, levels[k].price), min(new.index, levels[k].index))
                    levels.pop(k)
            levels.append(new)
            if len(levels) > LEVEL_CAP:
                levels.pop(0)
    return LiquidityResult(sweeps, taken, levels, pivots)


def nearest_prior_low(
    pivots: Sequence[Level], price: float, before_index: int
) -> Optional[Tuple[Level, float]]:
    """Pivot low formed before `before_index` closest to `price`, and distance in % of that low."""
    best = None
    for lv in pivots:
        if lv.index >= before_index or lv.price <= 0:
            continue
        d = abs(price - lv.price) / lv.price * 100
        if best is None or d < best[1]:
            best = (lv, d)
    return best
