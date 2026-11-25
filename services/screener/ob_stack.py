"""Screeners 4 & 5:
- ob_below_bearish: Bullish OB just below a bearish OB (SMC)
- ob_sweep: Last candle in bullish OB + SSL sweep (SMC)

Exact port of stock_screener/src/strategies/ob_stack.py on the LuxAlgo SMC state engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.screener import liquidity as lq
from services.screener import zone_touch as zt
from services.screener.models import Candle, ColumnSpec, ResultRow, ScanContext, SettingSpec, Strategy
from services.screener.smc_breaker import OrderBlock, SmcBreakerStrategy, run_full
from services.screener.timeutil import from_epoch

CHART_KEYS = (
    "internal_obs",
    "swing_obs",
    "swing_length",
    "ob_filter",
    "mitigation",
    "max_scan",
    "internal_confluence",
)
G1, G2, G3 = "Chart settings (match your SMC script)", "Screener filters", "Display"


@dataclass
class Zone:
    top: float
    bot: float
    ob: OrderBlock


def chart_specs() -> List[SettingSpec]:
    specs = [s for s in SmcBreakerStrategy().settings_specs() if s.key in CHART_KEYS]
    return specs + [
        SettingSpec(
            "chart_shown",
            "Chart shows (newest OBs per kind)",
            "Chart inputs 'Internal order blocks shown' / 'Swing order blocks shown' (5). "
            "Only those boxes count; 0 = every active OB.",
            "int",
            0,
            100,
            group=G1,
        )
    ]


def drawn_zones(cs: Sequence[Candle], s: Dict[str, Any]) -> Tuple[List[Zone], List[Zone]]:
    """(bullish, bearish) active OBs the chart draws after the last of `cs`, newest first."""
    st = run_full(cs, {**s, "breakers_from": "both"})
    bull: List[Zone] = []
    bear: List[Zone] = []
    limit = int(s.get("chart_shown", 5))
    for lst in (st.iobs, st.sobs):  # each newest first, bullish and bearish mixed
        for k, ob in enumerate(lst):
            if limit > 0 and k >= limit:
                break
            z = Zone(max(ob.high, ob.low), min(ob.high, ob.low), ob)
            (bull if ob.bias == 1 else bear).append(z)
    key = lambda z: (z.ob.created, z.ob.index)  # noqa: E731
    return sorted(bull, key=key, reverse=True), sorted(bear, key=key, reverse=True)


def _when(cs: Sequence[Candle], k: Optional[int], intraday: bool) -> str:
    if k is None or k < 0:
        return ""
    return from_epoch(cs[k].ts).strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d")


def _react_ok(tags: Sequence[str], mode: str) -> bool:
    return mode == "off" or bool(set(tags) & zt.REACTION_ACCEPT[mode])


# -- Bullish OB just below a bearish OB -------------------------------------------------------
STATUS_STACK = "OB under bearish OB"
STATUS_NO_PAIR = "No bearish OB above"
STATUS_GAP = "Gap to bearish OB above the limit"
STATUS_ORDER = "Bullish OB formed before the bearish OB"
STATUS_POS = "Last candle not in place"
POS_CHOICES = (
    ("any", "Anywhere (both boxes are active)"),
    ("bull", "On the bullish OB (inside / wick)"),
    ("stack", "Between bullish bottom and bearish top"),
)


def bear_position(c: Candle, top: float, bot: float) -> str:
    if c.c > top:
        return "Above"
    if c.c >= bot:
        return "Inside"
    if c.h >= bot:
        return "Wick into it"
    return "Below"


class ObBelowBearishStrategy(Strategy):
    id = "ob_below_bearish"
    name = "Bullish OB just below a bearish OB (SMC)"
    description = (
        "First a bearish OB (supply box) forms, then a bullish OB (demand box) forms just below it "
        "(distance <= Max distance %). Both still active. Same boxes as the SMC chart script."
    )

    def settings_specs(self) -> List[SettingSpec]:
        return chart_specs() + [
            SettingSpec(
                "max_gap_pct",
                "Max distance between the OBs (%)",
                "Distance between the bearish OB's bottom and the bullish OB's top, in % of the bullish top, "
                "either way: 1 = up to 1 % gap or up to 1 % overlap. 0 = the boxes must touch exactly.",
                "float",
                0,
                20,
                group=G2,
            ),
            SettingSpec(
                "bear_first",
                "Bearish OB formed first",
                "On: the bearish OB's structure break must come BEFORE the bullish OB's (supply first, then "
                "demand forms just below it). Off: any order.",
                "bool",
                group=G2,
            ),
            SettingSpec(
                "last_candle",
                "Last candle",
                "Anywhere = only the two boxes matter (price cannot be far: an active bullish OB is not broken "
                "below, an active bearish OB not above). On the bullish OB = inside or a wick into it. Between = "
                "close between the bullish bottom and the bearish top.",
                "choice",
                choices=POS_CHOICES,
                group=G2,
            ),
            SettingSpec(
                "reaction",
                "Bullish reaction on the last candle",
                "Also require the last candle to be green, a rejection wick or a bullish engulfing.",
                "choice",
                choices=zt.REACTION_CHOICES,
                group=G2,
            ),
            SettingSpec(
                "wick_ratio",
                "Wick ratio",
                "For 'Rejection wick': lower wick >= this x body.",
                "float",
                0.5,
                10,
                group=G2,
            ),
            SettingSpec(
                "show_invalid",
                "Show rejected",
                "Also list stocks with an active bullish OB that fail a rule, muted with the reason.",
                "bool",
                group=G3,
            ),
        ]

    def columns(self) -> List[ColumnSpec]:
        return [
            ColumnSpec("symbol", "Symbol"),
            ColumnSpec("status", "Status"),
            ColumnSpec("last_close", "Last close", "num"),
            ColumnSpec("ob_low", "Bull OB low", "num", "Bullish OB (demand) bottom"),
            ColumnSpec("ob_high", "Bull OB high", "num", "Bullish OB (demand) top"),
            ColumnSpec("bear_low", "Bear OB low", "num", "Bearish OB (supply) bottom"),
            ColumnSpec("bear_high", "Bear OB high", "num", "Bearish OB (supply) top"),
            ColumnSpec(
                "gap_pct",
                "Gap %",
                "pct",
                "Bearish bottom vs bullish top (below 0 = the boxes overlap)",
            ),
            ColumnSpec("bear_formed", "Bear OB formed", tooltip="Structure break that created the bearish OB"),
            ColumnSpec(
                "bull_formed",
                "Bull OB formed",
                tooltip="Structure break that created the bullish OB (after the bearish one when 'bear_first' is on)",
            ),
            ColumnSpec(
                "last_pos",
                "Vs bull OB",
                tooltip="Last candle vs the bullish OB: Inside, Wick touch, Near, Above, Below",
            ),
            ColumnSpec(
                "bear_pos",
                "Vs bear OB",
                tooltip="Last candle close vs the bearish OB: Below, Wick into it, Inside, Above",
            ),
            zt.reaction_column(),
            ColumnSpec("ob_time", "Bull OB candle"),
            ColumnSpec("bear_time", "Bear OB candle"),
            ColumnSpec("source", "From", tooltip="Internal / Swing structure of the bullish and the bearish OB"),
            ColumnSpec("others", "Other pairs", "int", "Other bullish-under-bearish pairs on this stock"),
            ColumnSpec("timeframe", "TF"),
        ]

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return int(settings["swing_length"]) + 10

    def scan(self, candles: Sequence[Candle], settings: Dict[str, Any], ctx: ScanContext) -> List[ResultRow]:
        cs = list(candles)
        if len(cs) < self.min_candles(settings):
            return []
        bull, bear = drawn_zones(cs, settings)
        last = cs[-1]
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        pairs, rejected = [], []
        for b in bull:
            above = [x for x in bear if x.top > b.top and x.bot > b.bot]
            if not above:
                rejected.append((b, None, None, STATUS_NO_PAIR))
                continue
            gap_of = lambda z: (z.bot - b.top) / b.top * 100 if b.top else 0.0  # noqa: E731
            first = [x for x in above if x.ob.created < b.ob.created]
            pool = first if settings.get("bear_first", True) and first else above
            x = min(pool, key=lambda z: abs(gap_of(z)))  # the nearest box above
            gap = gap_of(x)
            pos = zt.classify(last, b.top, b.bot, 1.0)
            mode = settings.get("last_candle", "any")
            if settings.get("bear_first", True) and not x.ob.created < b.ob.created:
                status = STATUS_ORDER
            elif abs(gap) > settings["max_gap_pct"]:
                status = STATUS_GAP
            elif mode == "bull" and pos not in (zt.INSIDE, zt.WICK):
                status = STATUS_POS
            elif mode == "stack" and not (b.bot <= last.c <= x.top):
                status = STATUS_POS
            elif not _react_ok(tags, settings.get("reaction", "off")):
                status = zt.STATUS_NO_REACTION
            else:
                status = STATUS_STACK
            (pairs if status == STATUS_STACK else rejected).append((b, x, gap, status))
        rows: List[ResultRow] = []
        if pairs:
            rows.append(ResultRow(self._row(cs, pairs[0], tags, ctx, len(pairs) - 1), True))
        if settings.get("show_invalid") and not pairs:
            rows += [ResultRow(self._row(cs, it, tags, ctx, 0), False) for it in rejected[:3]]
        return rows

    @staticmethod
    def _row(cs, item, tags, ctx: ScanContext, others: int) -> Dict[str, Any]:
        b, x, gap, status = item
        last = cs[-1]
        return {
            "symbol": ctx.symbol,
            "status": status,
            "last_close": round(last.c, 2),
            "ob_low": round(b.bot, 2),
            "ob_high": round(b.top, 2),
            "bear_low": round(x.bot, 2) if x else None,
            "bear_high": round(x.top, 2) if x else None,
            "gap_pct": round(gap, 2) if gap is not None else None,
            "last_pos": zt.classify(last, b.top, b.bot, 1.0),
            "bear_pos": bear_position(last, x.top, x.bot) if x else "",
            "reaction": zt.reaction_text(tags),
            "bear_formed": _when(cs, x.ob.created, ctx.intraday) if x else "",
            "bull_formed": _when(cs, b.ob.created, ctx.intraday),
            "ob_time": _when(cs, b.ob.index, ctx.intraday),
            "bear_time": _when(cs, x.ob.index, ctx.intraday) if x else "",
            "source": f"{b.ob.source} / {x.ob.source}" if x else b.ob.source,
            "others": others,
            "timeframe": ctx.timeframe,
        }


# -- Last candle in a bullish OB + SSL sweep --------------------------------------------------
STATUS_OB_SWEEP = "Sweep inside OB"
STATUS_NOT_IN_OB = "Not in a bullish OB"
STATUS_NO_SWEEP = "No SSL sweep"
STATUS_PIERCED = "Low below the OB"
STATUS_LEVEL_OUT = "Swept level outside the OB"


class ObSweepStrategy(Strategy):
    id = "ob_sweep"
    name = "Last candle in bullish OB + SSL sweep (SMC)"
    description = (
        "The last candle is on an active bullish OB (demand box) and swept sell-side liquidity: its "
        "low went below a prior swing low and it closed back above it."
    )

    def settings_specs(self) -> List[SettingSpec]:
        GL = "Liquidity levels (match your SMC script)"
        lq_specs = [
            SettingSpec(
                "liq_length",
                "Liquidity pivot length",
                "Chart input 'Liquidity pivot length': a swing low needs this many higher lows on each side "
                "(confirmed this many candles later).",
                "int",
                2,
                100,
                group=GL,
            ),
            SettingSpec(
                "merge_atr",
                "Merge levels within (ATR)",
                "Chart input 'Merge liquidity levels within, in ATR': lows closer than this x ATR(14) form "
                "one level (at the lower price).",
                "float",
                0,
                3,
                group=GL,
            ),
        ]
        return chart_specs() + lq_specs + [
            SettingSpec(
                "in_ob",
                "Last candle in the OB",
                "Touch = closed inside the OB or wicked into it and closed above. Inside = closed inside the OB.",
                "choice",
                choices=(("touch", "Inside or wick touch"), ("inside", "Inside only")),
                group=G2,
            ),
            SettingSpec(
                "max_below_pct",
                "Low may pierce the OB by (%)",
                "0 = the sweep low must stay inside the OB (with High/Low mitigation a low below it removes "
                "the box on the chart). Above 0: the low may go this far below the OB bottom, in % of it, if "
                "the candle closes back inside / above.",
                "float",
                0,
                10,
                group=G2,
            ),
            SettingSpec(
                "level_in_ob",
                "Swept level inside the OB",
                "Also require the swept swing low itself to lie within the OB (bottom .. top).",
                "bool",
                group=G2,
            ),
            SettingSpec(
                "reaction",
                "Bullish reaction on the last candle",
                "Also require the last candle to be green, a rejection wick or a bullish engulfing.",
                "choice",
                choices=zt.REACTION_CHOICES,
                group=G2,
            ),
            SettingSpec(
                "wick_ratio",
                "Wick ratio",
                "For 'Rejection wick': lower wick >= this x body.",
                "float",
                0.5,
                10,
                group=G2,
            ),
            SettingSpec(
                "show_invalid",
                "Show rejected",
                "Also list stocks whose last candle is on an OB or made a sweep but fail a rule, muted with the reason.",
                "bool",
                group=G3,
            ),
        ]

    def columns(self) -> List[ColumnSpec]:
        return [
            ColumnSpec("symbol", "Symbol"),
            ColumnSpec("status", "Status"),
            ColumnSpec("last_close", "Last close", "num"),
            ColumnSpec("ob_low", "OB low", "num"),
            ColumnSpec("ob_high", "OB high", "num"),
            zt.column(),
            ColumnSpec("level", "Swept level", "num", "The prior swing low (SSL) the last candle swept"),
            ColumnSpec("level_time", "Level from"),
            ColumnSpec("sweep_low", "Sweep low", "num", "Low of the last candle"),
            ColumnSpec("depth_pct", "Depth %", "pct", "How far the wick went below the swept level"),
            zt.reaction_column(),
            ColumnSpec("ob_time", "OB candle"),
            ColumnSpec("source", "From", tooltip="Internal or swing structure"),
            ColumnSpec("others", "Other OBs hit", "int"),
            ColumnSpec("timeframe", "TF"),
        ]

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return max(int(settings["swing_length"]) + 10, 2 * int(settings["liq_length"]) + 15)

    def scan(self, candles: Sequence[Candle], settings: Dict[str, Any], ctx: ScanContext) -> List[ResultRow]:
        cs = list(candles)
        if len(cs) < self.min_candles(settings):
            return []
        n = len(cs)
        last = cs[-1]
        bull, _ = drawn_zones(cs[:-1], settings)  # boxes on the chart BEFORE the last candle
        sweeps = [
            s
            for s in lq.run(cs, int(settings["liq_length"]), float(settings["merge_atr"])).sweeps
            if s.bar == n - 1
        ]
        sweep = min(sweeps, key=lambda s: s.level_index) if sweeps else None  # the oldest level taken
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        pierce = float(settings.get("max_below_pct", 0)) / 100
        hits, rejected = [], []
        for z in bull:
            pos = zt.classify(last, z.top, z.bot, 0.0)
            in_ob = pos == zt.INSIDE if settings["in_ob"] == "inside" else pos in (zt.INSIDE, zt.WICK)
            if not in_ob:
                continue
            if last.l < z.bot * (1 - pierce):
                status = STATUS_PIERCED
            elif sweep is None:
                status = STATUS_NO_SWEEP
            elif settings.get("level_in_ob") and not (z.bot <= sweep.level <= z.top):
                status = STATUS_LEVEL_OUT
            elif not _react_ok(tags, settings.get("reaction", "off")):
                status = zt.STATUS_NO_REACTION
            else:
                status = STATUS_OB_SWEEP
            (hits if status == STATUS_OB_SWEEP else rejected).append((z, pos, status))
        rows: List[ResultRow] = []
        if hits:
            rows.append(ResultRow(self._row(cs, hits[0], sweep, tags, ctx, len(hits) - 1), True))
        elif settings.get("show_invalid"):
            if rejected:
                rows.append(ResultRow(self._row(cs, rejected[0], sweep, tags, ctx, 0), False))
            elif sweep is not None:
                rows.append(ResultRow(self._row(cs, None, sweep, tags, ctx, 0), False))
        return rows

    @staticmethod
    def _row(cs, item, sweep: Optional[lq.Sweep], tags, ctx: ScanContext, others: int) -> Dict[str, Any]:
        z, pos, status = item if item else (None, "", STATUS_NOT_IN_OB)
        return {
            "symbol": ctx.symbol,
            "status": status,
            "last_close": round(cs[-1].c, 2),
            "ob_low": round(z.bot, 2) if z else None,
            "ob_high": round(z.top, 2) if z else None,
            "last_pos": pos,
            "level": round(sweep.level, 2) if sweep else None,
            "level_time": _when(cs, sweep.level_index, ctx.intraday) if sweep else "",
            "sweep_low": round(cs[-1].l, 2),
            "depth_pct": round(-(sweep.level - sweep.low) / sweep.level * 100, 2)
            if sweep and sweep.level
            else None,
            "reaction": zt.reaction_text(tags),
            "ob_time": _when(cs, z.ob.index, ctx.intraday) if z else "",
            "source": z.ob.source if z else "",
            "others": others,
            "timeframe": ctx.timeframe,
        }
