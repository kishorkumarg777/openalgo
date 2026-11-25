"""Technical indicators shared by all SMC screeners. Pure functions; no look-ahead."""
from __future__ import annotations

from typing import List, Optional, Sequence

from services.screener.models import Candle


def rsi_wilder(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI (TradingView `ta.rsi` semantics). out[t] is None for t < period."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if period < 1 or n <= period:
        return out
    gains, losses = [], []
    for t in range(1, n):
        d = closes[t] - closes[t - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out[period] = _rsi(avg_g, avg_l)
    for t in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[t - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[t - 1]) / period
        out[t] = _rsi(avg_g, avg_l)
    return out


def _rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr_wilder(cs: Sequence[Candle], period: int) -> List[Optional[float]]:
    """Wilder ATR. atr[t] is None until `period` candles exist; uses candles <= t only."""
    n = len(cs)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    tr = []
    for t, c in enumerate(cs):
        if t == 0:
            tr.append(c.h - c.l)
        else:
            pc = cs[t - 1].c
            tr.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))
    out[period - 1] = sum(tr[:period]) / period
    for t in range(period, n):
        out[t] = (out[t - 1] * (period - 1) + tr[t]) / period  # type: ignore[operator]
    return out
