"""Screeners 6, 7 & 8:
- ob_above_ob: OB just above another OB (SMC)
- ob_at_open: OB on chosen candle time(s) (intraday)
- ob_in_breaker: Last candle in an OB inside a breaker (SMC)

Exact port of stock_screener/src/strategies/ob_place.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from services.screener import zone_touch as zt
from services.screener.models import Candle, ColumnSpec, ResultRow, ScanContext, SettingSpec, Strategy
from services.screener.ob_stack import G2, G3, Zone, _react_ok, _when, chart_specs, drawn_zones
from services.screener.smc_breaker import run_full
from services.screener.timeutil import from_epoch

INTRADAY = ("4H", "2H", "1H", "30m", "15m")
BIAS_CHOICES = (
    ("bullish", "Bullish OB (demand)"),
    ("bearish", "Bearish OB (supply)"),
    ("any", "Either"),
)


def _bias_text(z: Zone) -> str:
    return "Bullish" if z.ob.bias == 1 else "Bearish"


def _pick(bull: List[Zone], bear: List[Zone], bias: str) -> List[Zone]:
    out = bull if bias == "bullish" else bear if bias == "bearish" else bull + bear
    return sorted(out, key=lambda z: (z.ob.created, z.ob.index), reverse=True)


def _position(c: Candle, z: Zone) -> str:
    if z.ob.bias == 1:
        return zt.classify(c, z.top, z.bot, 1.0)
    if c.c > z.top:
        return "Above"
    if c.c >= z.bot:
        return "Inside"
    return "Wick into it" if c.h >= z.bot else "Below"


def _touch(c: Candle, z: Zone) -> bool:
    """Last candle traded into the box (low <= top and high >= bottom)."""
    return c.l <= z.top and c.h >= z.bot


# -- OB on chosen candle time(s) (intraday) ---------------------------------------------------
STATUS_OPEN_OB = "OB on the chosen candle"
STATUS_OLD = "Too old"
STATUS_NOT_LATEST = "Not the latest candle at that time"
STATUS_NOT_AT = "Last candle not at the OB"


class ObAtOpenStrategy(Strategy):
    id = "ob_at_open"
    name = "OB on chosen candle time(s) (intraday)"
    description = (
        "An active order block whose OB candle opens at one of the chosen times (candle_time, "
        'e.g. "09:15, 13:15, 15:15") - use on 1H or other intraday timeframes. Same boxes as the SMC chart script.'
    )
    timeframes = INTRADAY
    ui_keys = ("candle_time",)

    def settings_specs(self) -> List[SettingSpec]:
        return chart_specs() + [
            SettingSpec(
                "candle_time",
                "OB candle time(s) (HH:MM IST)",
                'Opening time(s) of the candle the OB must sit on, separated by commas: "9:15" or '
                '"9:15, 13:15, 15:15". Must be candle starts of the chosen timeframe (1H: 9:15, 10:15 ... 15:15).',
                "text",
                group=G2,
            ),
            SettingSpec(
                "bias",
                "OB kind",
                "Bullish (demand), bearish (supply) or either.",
                "choice",
                choices=BIAS_CHOICES,
                group=G2,
            ),
            SettingSpec(
                "only_latest",
                "Only the latest candle at each time",
                "On (default): the OB candle must be the MOST RECENT candle at one of the chosen times "
                "(e.g. the last 13:15 candle in the data). Older candles at that time do not count. "
                "Off: any day (see max_age_days).",
                "bool",
                group=G2,
            ),
            SettingSpec(
                "max_age_days",
                "OB candle within (days)",
                "Only OBs whose OB candle is within the last N trading days of the data (1 = today's / "
                "the last session's). 0 = no limit.",
                "int",
                0,
                365,
                group=G2,
            ),
            SettingSpec(
                "last_at_ob",
                "Last candle at the OB",
                "Off = only the OB matters. On = the last candle traded into the box.",
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
                "Also list OBs on the chosen candle time(s) that fail a rule, muted with the reason.",
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
            ColumnSpec("bias", "Kind"),
            ColumnSpec(
                "ob_time",
                "OB candle",
                tooltip="The candle the box starts on (date and one of the chosen times)",
            ),
            ColumnSpec("created_time", "Structure break", tooltip="Break that created the OB"),
            ColumnSpec("last_pos", "Last candle", tooltip="Last candle vs the box"),
            zt.reaction_column(),
            ColumnSpec("source", "From", tooltip="Internal or swing structure"),
            ColumnSpec("others", "Other OBs", "int", "Other active OBs on one of the chosen candle times"),
            ColumnSpec("timeframe", "TF"),
        ]

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return int(settings["swing_length"]) + 10

    def check(self, settings: Dict[str, Any]) -> List[str]:
        try:
            self.parse_times(settings.get("candle_time", ""))
            return []
        except (ValueError, TypeError) as e:
            return [
                f"candle_time (OB candle time): {e}. Use HH:MM, comma separated, between 9:15 and 15:29 - "
                f'e.g. "9:15, 13:15, 15:15" (got {settings.get("candle_time")!r})'
            ]

    @staticmethod
    def parse_time(text: str) -> tuple:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"'{text}' is not a time")
        return h, m

    @classmethod
    def parse_times(cls, value: Any) -> List[tuple]:
        """'9:15, 13:15 15:15' (or a JSON list ['9:15', '13:15']) -> [(9, 15), (13, 15), (15, 15)]."""
        items = (
            value
            if isinstance(value, (list, tuple))
            else str(value)
            .replace("[", " ")
            .replace("]", " ")
            .replace("'", " ")
            .replace('"', " ")
            .replace(";", ",")
            .replace(",", " ")
            .split()
        )
        out: List[tuple] = []
        for it in items:
            h, m = cls.parse_time(str(it).strip())
            if not ((9, 15) <= (h, m) <= (15, 29)):
                raise ValueError(f"'{it}' is outside the NSE session 9:15 - 15:29")
            if (h, m) not in out:
                out.append((h, m))
        if not out:
            raise ValueError("no time given")
        return out

    def scan(self, candles: Sequence[Candle], settings: Dict[str, Any], ctx: ScanContext) -> List[ResultRow]:
        cs = list(candles)
        if not ctx.intraday or len(cs) < self.min_candles(settings):
            return []
        wanted = set(self.parse_times(settings["candle_time"]))
        bull, bear = drawn_zones(cs, settings)
        days = sorted({from_epoch(c.ts).date() for c in cs})
        lim = int(settings["max_age_days"])
        first_day = days[-lim] if 0 < lim <= len(days) else None
        latest = {}  # (h, m) -> index of the most recent such candle
        for i, c in enumerate(cs):
            t = from_epoch(c.ts)
            if (t.hour, t.minute) in wanted:
                latest[(t.hour, t.minute)] = i
        latest_idx = set(latest.values())
        last = cs[-1]
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        hits, rejected = [], []
        for z in _pick(bull, bear, settings["bias"]):
            t = from_epoch(cs[z.ob.index].ts)
            if (t.hour, t.minute) not in wanted:
                continue
            if settings.get("only_latest", True) and z.ob.index not in latest_idx:
                status = STATUS_NOT_LATEST
            elif first_day is not None and t.date() < first_day:
                status = STATUS_OLD
            elif settings.get("last_at_ob") and not _touch(last, z):
                status = STATUS_NOT_AT
            elif not _react_ok(tags, settings.get("reaction", "off")):
                status = zt.STATUS_NO_REACTION
            else:
                status = STATUS_OPEN_OB
            (hits if status == STATUS_OPEN_OB else rejected).append((z, status))
        rows: List[ResultRow] = []
        if hits:
            rows.append(ResultRow(self._row(cs, hits[0], tags, ctx, len(hits) - 1), True))
        elif settings.get("show_invalid") and rejected:
            rows.append(ResultRow(self._row(cs, rejected[0], tags, ctx, 0), False))
        return rows

    @staticmethod
    def _row(cs, item, tags, ctx: ScanContext, others: int) -> Dict[str, Any]:
        z, status = item
        return {
            "symbol": ctx.symbol,
            "status": status,
            "last_close": round(cs[-1].c, 2),
            "ob_low": round(z.bot, 2),
            "ob_high": round(z.top, 2),
            "bias": _bias_text(z),
            "ob_time": _when(cs, z.ob.index, True),
            "created_time": _when(cs, z.ob.created, True),
            "last_pos": _position(cs[-1], z),
            "reaction": zt.reaction_text(tags),
            "source": z.ob.source,
            "others": others,
            "timeframe": ctx.timeframe,
        }


# -- OB just above another OB -----------------------------------------------------------------
STATUS_ABOVE = "OB just above another OB"
STATUS_GAP = "Gap above the limit"
STATUS_OVERLAP = "Boxes overlap too much"
STATUS_NOT_IN = "Last candle not in place"
LAST_CHOICES = (
    ("any", "Anywhere"),
    ("upper", "At the upper OB"),
    ("lower", "At the lower OB"),
    ("either", "At either OB"),
)


class ObAboveObStrategy(Strategy):
    id = "ob_above_ob"
    name = "OB just above another OB (SMC)"
    description = (
        "Two active order blocks stacked: the upper box starts at most 'Max gap %' above the lower "
        "box's top. Kinds of both boxes are configurable."
    )

    def settings_specs(self) -> List[SettingSpec]:
        return chart_specs() + [
            SettingSpec("upper_bias", "Upper OB kind", "Kind of the upper box.", "choice", choices=BIAS_CHOICES, group=G2),
            SettingSpec("lower_bias", "Lower OB kind", "Kind of the lower box.", "choice", choices=BIAS_CHOICES, group=G2),
            SettingSpec(
                "max_gap_pct",
                "Max gap (%)",
                "Upper box bottom at most this far above the lower box top, in % of the lower top. "
                "1 = less than 1 % apart.",
                "float",
                0,
                20,
                group=G2,
            ),
            SettingSpec(
                "max_overlap_pct",
                "Max overlap (%)",
                "How far the upper box may reach down INTO the lower box, in % of the lower top. "
                "0 = no overlap (touching is fine). The upper box must still reach higher than the lower one.",
                "float",
                0,
                20,
                group=G2,
            ),
            SettingSpec(
                "last_candle",
                "Last candle",
                "Anywhere, or the last candle must trade into the upper / lower / either box.",
                "choice",
                choices=LAST_CHOICES,
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
                "Also list the nearest pair that fails a rule, muted with the reason.",
                "bool",
                group=G3,
            ),
        ]

    def columns(self) -> List[ColumnSpec]:
        return [
            ColumnSpec("symbol", "Symbol"),
            ColumnSpec("status", "Status"),
            ColumnSpec("last_close", "Last close", "num"),
            ColumnSpec("ob_low", "Lower OB low", "num"),
            ColumnSpec("ob_high", "Lower OB high", "num"),
            ColumnSpec("upper_low", "Upper OB low", "num"),
            ColumnSpec("upper_high", "Upper OB high", "num"),
            ColumnSpec("gap_pct", "Gap %", "pct", "Upper bottom vs lower top (below 0 = overlap)"),
            ColumnSpec("kinds", "Kinds", tooltip="Upper / lower box kind"),
            ColumnSpec("upper_pos", "Vs upper OB"),
            ColumnSpec("lower_pos", "Vs lower OB"),
            zt.reaction_column(),
            ColumnSpec("upper_time", "Upper OB candle"),
            ColumnSpec("ob_time", "Lower OB candle"),
            ColumnSpec("others", "Other pairs", "int"),
            ColumnSpec("timeframe", "TF"),
        ]

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return int(settings["swing_length"]) + 10

    def scan(self, candles: Sequence[Candle], settings: Dict[str, Any], ctx: ScanContext) -> List[ResultRow]:
        cs = list(candles)
        if len(cs) < self.min_candles(settings):
            return []
        bull, bear = drawn_zones(cs, settings)
        uppers = _pick(bull, bear, settings["upper_bias"])
        lowers = _pick(bull, bear, settings["lower_bias"])
        last = cs[-1]
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        hits, rejected = [], []
        for lo in lowers:
            above = [u for u in uppers if u is not lo and u.top > lo.top and u.bot > lo.bot]
            if not above or not lo.top:
                continue
            up = min(above, key=lambda u: abs(u.bot - lo.top))  # the nearest box above
            gap = (up.bot - lo.top) / lo.top * 100
            mode = settings.get("last_candle", "any")
            in_place = {
                "any": True,
                "upper": _touch(last, up),
                "lower": _touch(last, lo),
                "either": _touch(last, up) or _touch(last, lo),
            }[mode]
            if gap > settings["max_gap_pct"]:
                status = STATUS_GAP
            elif gap < -settings["max_overlap_pct"]:
                status = STATUS_OVERLAP
            elif not in_place:
                status = STATUS_NOT_IN
            elif not _react_ok(tags, settings.get("reaction", "off")):
                status = zt.STATUS_NO_REACTION
            else:
                status = STATUS_ABOVE
            (hits if status == STATUS_ABOVE else rejected).append((lo, up, gap, status))
        rows: List[ResultRow] = []
        if hits:
            rows.append(ResultRow(self._row(cs, hits[0], tags, ctx, len(hits) - 1), True))
        elif settings.get("show_invalid") and rejected:
            rows.append(ResultRow(self._row(cs, min(rejected, key=lambda r: abs(r[2])), tags, ctx, 0), False))
        return rows

    @staticmethod
    def _row(cs, item, tags, ctx: ScanContext, others: int) -> Dict[str, Any]:
        lo, up, gap, status = item
        last = cs[-1]
        return {
            "symbol": ctx.symbol,
            "status": status,
            "last_close": round(last.c, 2),
            "ob_low": round(lo.bot, 2),
            "ob_high": round(lo.top, 2),
            "upper_low": round(up.bot, 2),
            "upper_high": round(up.top, 2),
            "gap_pct": round(gap, 2),
            "kinds": f"{_bias_text(up)} / {_bias_text(lo)}",
            "upper_pos": _position(last, up),
            "lower_pos": _position(last, lo),
            "reaction": zt.reaction_text(tags),
            "upper_time": _when(cs, up.ob.index, ctx.intraday),
            "ob_time": _when(cs, lo.ob.index, ctx.intraday),
            "others": others,
            "timeframe": ctx.timeframe,
        }


# -- Last candle in an OB that sits inside a bullish breaker block ----------------------------
STATUS_OB_IN_BB = "In OB inside breaker"
STATUS_OB_BEFORE_BB = "OB formed before the breaker"
STATUS_LAST_OUT = "Last candle not in the OB"
STATUS_STICKS_OUT = "OB sticks out of the breaker"


def _sticks_out(z: Zone, b: Any) -> float:
    """% by which the OB reaches above the breaker top or below its bottom (0 = fully inside)."""
    up = (z.top - b.top) / b.top * 100 if b.top else 0.0
    down = (b.bot - z.bot) / b.bot * 100 if b.bot else 0.0
    return max(0.0, up, down)


def _inside_share(z: Zone, b: Any) -> float:
    """% of the OB's height that lies inside the breaker (100 = fully inside)."""
    h = z.top - z.bot
    inter = max(0.0, min(z.top, b.top) - max(z.bot, b.bot))
    if h <= 0:
        return 100.0 if b.bot <= z.bot <= b.top else 0.0
    return inter / h * 100


class ObInBreakerStrategy(Strategy):
    id = "ob_in_breaker"
    name = "Last candle in an OB inside a breaker (SMC)"
    description = (
        "The last candle is on an active bullish OB (demand box) that lies inside a still-valid "
        "bullish breaker block (+BB). Same boxes as the SMC chart script."
    )

    def settings_specs(self) -> List[SettingSpec]:
        return chart_specs() + [
            SettingSpec(
                "inside_tol_pct",
                "OB may stick out of the breaker by (%)",
                "0 = the OB must lie fully inside the breaker (bottom >= breaker bottom, top <= breaker top). "
                "Above 0 the OB may reach that many % (of the breaker edge) beyond it. Used only when "
                "min_inside_share_pct is 0.",
                "float",
                0,
                10,
                group=G2,
            ),
            SettingSpec(
                "min_inside_share_pct",
                "Min share of the OB inside the breaker (%)",
                "At least this % of the OB's height must lie inside the breaker (60 = 60 %; 100 = fully inside). "
                "0 = off, then inside_tol_pct decides.",
                "float",
                0,
                100,
                group=G2,
            ),
            SettingSpec(
                "ob_after_bb",
                "OB formed after the breaker",
                "On: the OB's structure break comes after the breaker was formed (price came back to the "
                "breaker and built an OB there). Off: any order.",
                "bool",
                group=G2,
            ),
            SettingSpec(
                "in_ob",
                "Last candle in the OB",
                "Touch = closed inside the OB, or wicked into it and closed above. Inside = closed inside the OB.",
                "choice",
                choices=(("touch", "Inside or wick touch"), ("inside", "Inside only")),
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
                "Also list the OB-inside-breaker found when another rule fails, muted with the reason.",
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
            ColumnSpec("bb_low", "Breaker low", "num"),
            ColumnSpec("bb_high", "Breaker high", "num"),
            ColumnSpec(
                "out_pct",
                "Sticks out %",
                "pct",
                "How far the OB reaches beyond the breaker's top or bottom (0 = fully inside). "
                "Must be <= inside_tol_pct (when min_inside_share_pct is 0).",
            ),
            ColumnSpec(
                "share_pct",
                "Inside %",
                "num",
                "Share of the OB's height that lies inside the breaker. "
                "Must be >= min_inside_share_pct (when that is above 0).",
            ),
            zt.column(),
            zt.reaction_column(),
            ColumnSpec("ob_time", "OB candle"),
            ColumnSpec("ob_formed", "OB formed", tooltip="Structure break that created the OB"),
            ColumnSpec("bb_formed", "Breaker formed", tooltip="Candle that turned the bearish OB into the breaker"),
            ColumnSpec("source", "From", tooltip="Internal or swing structure (OB / breaker)"),
            ColumnSpec("others", "Other OBs", "int", "Other OBs inside a breaker that the last candle is on"),
            ColumnSpec("timeframe", "TF"),
        ]

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return int(settings["swing_length"]) + 10

    def scan(self, candles: Sequence[Candle], settings: Dict[str, Any], ctx: ScanContext) -> List[ResultRow]:
        cs = list(candles)
        if len(cs) < self.min_candles(settings):
            return []
        st = run_full(cs, {**settings, "breakers_from": "both"})
        limit = int(settings.get("chart_shown", 5))
        obs: List[Zone] = []
        for lst in (st.iobs, st.sobs):  # the boxes the chart draws, newest first
            for k, ob in enumerate(lst):
                if limit > 0 and k >= limit:
                    break
                if ob.bias == 1:
                    obs.append(Zone(max(ob.high, ob.low), min(ob.high, ob.low), ob))
        seen, bbs = set(), []
        for b in sorted(st.breakers, key=lambda b: (b.formed, b.ob_index), reverse=True):
            if b.bias == 1 and b.invalid_at is None and b.ob_index not in seen:
                seen.add(b.ob_index)
                bbs.append(b)
        tol = float(settings["inside_tol_pct"]) / 100
        min_share = float(settings.get("min_inside_share_pct", 0))

        def fits(z: Zone, b: Any) -> bool:
            if min_share > 0:
                return _inside_share(z, b) >= min_share
            return z.bot >= b.bot * (1 - tol) and z.top <= b.top * (1 + tol)

        last = cs[-1]
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        hits, rejected = [], []
        for z in sorted(obs, key=lambda z: (z.ob.created, z.ob.index), reverse=True):
            pos = zt.classify(last, z.top, z.bot, 0.0)
            in_ob = pos == zt.INSIDE if settings["in_ob"] == "inside" else pos in (zt.INSIDE, zt.WICK)
            inside = [b for b in bbs if fits(z, b)]
            if not inside:
                over = [b for b in bbs if z.bot < b.top and z.top > b.bot]
                if over:
                    b = max(over, key=lambda b: _inside_share(z, b))
                    why = (
                        f"only {_inside_share(z, b):.1f} % of the OB inside the breaker"
                        if min_share > 0
                        else f"{STATUS_STICKS_OUT} by {_sticks_out(z, b):.2f} %"
                    )
                    rejected.append((z, b, pos, why + ("" if in_ob else " (last candle not in the OB)")))
                continue
            after = [b for b in inside if z.ob.created > b.formed]
            b = (after or inside)[0]
            if settings.get("ob_after_bb") and not after:
                status = STATUS_OB_BEFORE_BB
            elif not in_ob:
                status = STATUS_LAST_OUT
            elif not _react_ok(tags, settings.get("reaction", "off")):
                status = zt.STATUS_NO_REACTION
            else:
                status = STATUS_OB_IN_BB
            (hits if status == STATUS_OB_IN_BB else rejected).append((z, b, pos, status))
        rows: List[ResultRow] = []
        if hits:
            rows.append(ResultRow(self._row(cs, hits[0], tags, ctx, len(hits) - 1), True))
        elif settings.get("show_invalid") and rejected:
            rows += [ResultRow(self._row(cs, r, tags, ctx, 0), False) for r in rejected[:3]]
        return rows

    @staticmethod
    def _row(cs, item, tags, ctx: ScanContext, others: int) -> Dict[str, Any]:
        z, b, pos, status = item
        return {
            "symbol": ctx.symbol,
            "status": status,
            "last_close": round(cs[-1].c, 2),
            "ob_low": round(z.bot, 2),
            "ob_high": round(z.top, 2),
            "bb_low": round(b.bot, 2),
            "bb_high": round(b.top, 2),
            "last_pos": pos,
            "reaction": zt.reaction_text(tags),
            "ob_time": _when(cs, z.ob.index, ctx.intraday),
            "ob_formed": _when(cs, z.ob.created, ctx.intraday),
            "bb_formed": _when(cs, b.formed, ctx.intraday),
            "source": f"{z.ob.source} / {b.source}",
            "out_pct": round(_sticks_out(z, b), 2),
            "share_pct": round(_inside_share(z, b), 1),
            "others": others,
            "timeframe": ctx.timeframe,
        }
