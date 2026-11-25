"""SMC Breaker Block (LuxAlgo port) and core SMC state engine (`run_full`).

Bar-by-bar port matching LuxAlgo's Smart Money Concepts + SMC toolkit:
  1. True range, ATR(200) (Wilder; cumulative mean range before 200 bars), high-volatility swap
  2. Internal (5-bar) & Swing (`swing_length`) legs and pivots
  3. Close crossover semantics over internal / swing pivot levels
  4. Structure breaks (BOS / CHoCH) -> Order Blocks (lowest parsed low / highest parsed high)
  5. Active breaker invalidation when close breaks back through
  6. OB mitigation -> Breaker promotion (same-bar close beyond mitigated OB)
  7. Caps: 100 OBs per kind, 50 breakers (oldest dropped)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.screener import zone_touch as zt
from services.screener.models import Candle, ColumnSpec, ResultRow, ScanContext, SettingSpec, Strategy
from services.screener.timeutil import from_epoch

STATUS_FRESH = "Fresh breaker"
STATUS_RETESTED = "Retested"
STATUS_INVALID = "Invalidated"
STATUS_OLD = "Too old"
STATUS_FAR = "Too far above"
STATUS_NO_RETEST = "No retest yet"

INTERNAL_LEN = 5
ATR_LEN = 200
OB_CAP = 100
BREAKER_CAP = 50
VALID = (STATUS_FRESH, STATUS_RETESTED)


@dataclass
class OrderBlock:
    high: float
    low: float
    index: int
    bias: int  # 1 bullish OB, -1 bearish OB
    source: str  # "Internal" | "Swing"
    created: int = -1
    mitigated: Optional[int] = None


@dataclass
class Breaker:
    top: float
    bot: float
    ob_index: int
    formed: int
    bias: int  # 1 bullish breaker (+BB), -1 bearish (-BB)
    source: str
    invalid_at: Optional[int] = None
    retest: Optional[int] = None


@dataclass
class StructureBreak:
    bar: int
    level: float
    bias: int  # 1 bullish, -1 bearish
    kind: str  # "Internal" | "Swing"
    tag: str  # "CHoCH" | "BOS"
    ob: Optional[OrderBlock] = None


class _Pivot:
    __slots__ = ("level", "index", "crossed")

    def __init__(self) -> None:
        self.level: Optional[float] = None
        self.index: Optional[int] = None
        self.crossed = False


def run(cs: Sequence[Candle], s: Dict[str, Any]) -> List[Breaker]:
    return run_state(cs, s)[0]


@dataclass
class SmcState:
    breakers: List[Breaker]
    iobs: List[OrderBlock]
    sobs: List[OrderBlock]
    int_high: Optional[float]
    int_high_index: Optional[int]
    int_high_crossed: bool
    swing_high: Optional[float]
    swing_high_index: Optional[int]
    swing_high_crossed: bool
    parsed_h: List[float]
    parsed_l: List[float]
    events: List[StructureBreak] = field(default_factory=list)


def run_state(
    cs: Sequence[Candle], s: Dict[str, Any]
) -> Tuple[List[Breaker], List[OrderBlock], List[OrderBlock]]:
    st = run_full(cs, s)
    return st.breakers, st.iobs, st.sobs


def run_full(cs: Sequence[Candle], s: Dict[str, Any]) -> SmcState:
    n = len(cs)
    swing_len = int(s["swing_length"])
    max_scan = int(s["max_scan"])
    close_mitigation = s["mitigation"] == "close"
    use_atr = s["ob_filter"] == "atr"
    make_int = s["internal_obs"] and s["breakers_from"] in ("internal", "both")
    make_sw = s["swing_obs"] and s["breakers_from"] in ("swing", "both")
    confluence = s["internal_confluence"]

    parsed_h: List[float] = []
    parsed_l: List[float] = []
    tr_sum = 0.0
    atr: Optional[float] = None
    tr_first: List[float] = []

    sw_leg = 0
    int_leg = 0
    sw_hi, sw_lo, in_hi, in_lo = _Pivot(), _Pivot(), _Pivot(), _Pivot()
    prev_levels: Tuple[Optional[float], Optional[float], Optional[float], Optional[float]] = (
        None,
        None,
        None,
        None,
    )

    iobs: List[OrderBlock] = []
    sobs: List[OrderBlock] = []
    active: List[Breaker] = []
    events: List[StructureBreak] = []
    int_trend = 0
    sw_trend = 0
    done: List[Breaker] = []

    for t in range(n):
        c = cs[t]
        # 1. True range, ATR(200) / cumulative mean range, parsed high/low
        tr = c.h - c.l if t == 0 else max(c.h - c.l, abs(c.h - cs[t - 1].c), abs(c.l - cs[t - 1].c))
        tr_sum += tr
        cmr = tr_sum / t if t > 0 else None
        if t < ATR_LEN:
            tr_first.append(tr)
            if t == ATR_LEN - 1:
                atr = sum(tr_first) / ATR_LEN
        else:
            atr = (atr * (ATR_LEN - 1) + tr) / ATR_LEN  # type: ignore[operator]
        measure = (atr if atr is not None else cmr) if use_atr else cmr
        high_vol = measure is not None and (c.h - c.l) >= 2 * measure
        parsed_h.append(c.l if high_vol else c.h)
        parsed_l.append(c.h if high_vol else c.l)

        # 2. Legs -> new pivots
        prev_sw_leg, prev_int_leg = sw_leg, int_leg
        if t >= swing_len:
            p = t - swing_len
            if cs[p].h > max(x.h for x in cs[p + 1 : t + 1]):
                sw_leg = 0
            elif cs[p].l < min(x.l for x in cs[p + 1 : t + 1]):
                sw_leg = 1
        if t >= INTERNAL_LEN:
            p = t - INTERNAL_LEN
            if cs[p].h > max(x.h for x in cs[p + 1 : t + 1]):
                int_leg = 0
            elif cs[p].l < min(x.l for x in cs[p + 1 : t + 1]):
                int_leg = 1
        if sw_leg == 1 and prev_sw_leg == 0:
            sw_lo.level, sw_lo.index, sw_lo.crossed = cs[t - swing_len].l, t - swing_len, False
        elif sw_leg == 0 and prev_sw_leg == 1:
            sw_hi.level, sw_hi.index, sw_hi.crossed = cs[t - swing_len].h, t - swing_len, False
        if int_leg == 1 and prev_int_leg == 0:
            in_lo.level, in_lo.index, in_lo.crossed = cs[t - INTERNAL_LEN].l, t - INTERNAL_LEN, False
        elif int_leg == 0 and prev_int_leg == 1:
            in_hi.level, in_hi.index, in_hi.crossed = cs[t - INTERNAL_LEN].h, t - INTERNAL_LEN, False

        # 3. Crosses
        pc = cs[t - 1].c if t > 0 else None

        def cross_up(level: Optional[float], prev_level: Optional[float]) -> bool:
            return (
                level is not None
                and prev_level is not None
                and pc is not None
                and c.c > level
                and pc <= prev_level
            )

        def cross_down(level: Optional[float], prev_level: Optional[float]) -> bool:
            return (
                level is not None
                and prev_level is not None
                and pc is not None
                and c.c < level
                and pc >= prev_level
            )

        p_in_hi, p_in_lo, p_sw_hi, p_sw_lo = prev_levels
        int_bull_x = cross_up(in_hi.level, p_in_hi)
        int_bear_x = cross_down(in_lo.level, p_in_lo)
        sw_bull_x = cross_up(sw_hi.level, p_sw_hi)
        sw_bear_x = cross_down(sw_lo.level, p_sw_lo)
        bullish_bar = (c.h - max(c.c, c.o) > min(c.c, c.o - c.l)) if confluence else True
        bearish_bar = (c.h - max(c.c, c.o) < min(c.c, c.o - c.l)) if confluence else True

        # 4. Structure breaks -> order blocks
        def make_ob(pivot_index: int, bias: int, source: str) -> OrderBlock:
            dist = min(t - pivot_index, max_scan)
            best_off = dist
            vals = parsed_l if bias == 1 else parsed_h
            best = vals[t - dist]
            for off in range(dist - 1, 0, -1):  # older -> newer; strict keeps oldest
                v = vals[t - off]
                if (v < best) if bias == 1 else (v > best):
                    best, best_off = v, off
            k = t - best_off
            return OrderBlock(parsed_h[k], parsed_l[k], k, bias, source, t)

        def add(lst: List[OrderBlock], ob: OrderBlock) -> None:
            lst.insert(0, ob)
            if len(lst) > OB_CAP:
                lst.pop()

        def brk(level: float, bias: int, kind: str, trend: int, ob: Optional[OrderBlock]) -> None:
            events.append(
                StructureBreak(t, level, bias, kind, "CHoCH" if trend == -bias else "BOS", ob)
            )

        if int_bull_x and not in_hi.crossed and in_hi.level != sw_hi.level and bullish_bar:
            in_hi.crossed = True
            ob = make_ob(in_hi.index, 1, "Internal") if s["internal_obs"] else None  # type: ignore[arg-type]
            if ob:
                add(iobs, ob)
            brk(in_hi.level, 1, "Internal", int_trend, ob)  # type: ignore[arg-type]
            int_trend = 1
        if int_bear_x and not in_lo.crossed and in_lo.level != sw_lo.level and bearish_bar:
            in_lo.crossed = True
            ob = make_ob(in_lo.index, -1, "Internal") if s["internal_obs"] else None  # type: ignore[arg-type]
            if ob:
                add(iobs, ob)
            brk(in_lo.level, -1, "Internal", int_trend, ob)  # type: ignore[arg-type]
            int_trend = -1
        if sw_bull_x and not sw_hi.crossed:
            sw_hi.crossed = True
            ob = make_ob(sw_hi.index, 1, "Swing") if s["swing_obs"] else None  # type: ignore[arg-type]
            if ob:
                add(sobs, ob)
            brk(sw_hi.level, 1, "Swing", sw_trend, ob)  # type: ignore[arg-type]
            sw_trend = 1
        if sw_bear_x and not sw_lo.crossed:
            sw_lo.crossed = True
            ob = make_ob(sw_lo.index, -1, "Swing") if s["swing_obs"] else None  # type: ignore[arg-type]
            if ob:
                add(sobs, ob)
            brk(sw_lo.level, -1, "Swing", sw_trend, ob)  # type: ignore[arg-type]
            sw_trend = -1

        # 5. Breakers closed back through are removed
        for k in range(len(active) - 1, -1, -1):
            b = active[k]
            if (b.bias == 1 and c.c < b.bot) or (b.bias == -1 and c.c > b.top):
                b.invalid_at = t
                done.append(active.pop(k))

        # 6. Order-block mitigation -> new breakers
        bear_src = c.c if close_mitigation else c.h
        bull_src = c.c if close_mitigation else c.l
        for obs, allowed in ((iobs, make_int), (sobs, make_sw)):
            for k in range(len(obs) - 1, -1, -1):
                ob = obs[k]
                bear_hit = ob.bias == -1 and bear_src > ob.high
                bull_hit = ob.bias == 1 and bull_src < ob.low
                if not (bear_hit or bull_hit):
                    continue
                top, bot = max(ob.high, ob.low), min(ob.high, ob.low)
                if bear_hit and allowed and c.c > top:
                    active.append(Breaker(top, bot, ob.index, t, 1, ob.source))
                elif bull_hit and allowed and c.c < bot:
                    active.append(Breaker(top, bot, ob.index, t, -1, ob.source))
                ob.mitigated = t
                obs.pop(k)
        while len(active) > BREAKER_CAP:
            active.pop(0)

        prev_levels = (in_hi.level, in_lo.level, sw_hi.level, sw_lo.level)

    # Retests after formation and before invalidation
    for b in active + done:
        end = b.invalid_at if b.invalid_at is not None else n
        for t in range(b.formed + 1, end):
            x = cs[t]
            if (b.bias == 1 and x.l <= b.top and x.c >= b.bot) or (
                b.bias == -1 and x.h >= b.bot and x.c <= b.top
            ):
                b.retest = t
                break
    return SmcState(
        active + done,
        iobs,
        sobs,
        in_hi.level,
        in_hi.index,
        in_hi.crossed,
        sw_hi.level,
        sw_hi.index,
        sw_hi.crossed,
        parsed_h,
        parsed_l,
        events,
    )


class SmcBreakerStrategy(Strategy):
    id = "smc_breaker"
    name = "SMC Breaker Block (LuxAlgo port)"
    description = (
        "Bullish breaker blocks (+BB) matching Smart Money Concepts [LuxAlgo]: a bearish order block "
        "from a structure break that a later candle trades and closes above in the same bar."
    )

    def settings_specs(self) -> List[SettingSpec]:
        G1, G2, G3 = "Chart settings (match your SMC script)", "Screener filters", "Display"
        return (
            [
                SettingSpec(
                    "internal_obs",
                    "Internal order blocks",
                    "Internal structure uses 5-candle pivots.",
                    "bool",
                    group=G1,
                ),
                SettingSpec(
                    "swing_obs",
                    "Swing order blocks",
                    "Swing structure order blocks.",
                    "bool",
                    group=G1,
                ),
                SettingSpec(
                    "breakers_from",
                    "Breakers from",
                    "Which structure order blocks can promote to breakers.",
                    "choice",
                    choices=(("both", "Both"), ("internal", "Internal"), ("swing", "Swing")),
                    group=G1,
                ),
                SettingSpec(
                    "swing_length",
                    "Swing length",
                    "Candles each side of a swing pivot.",
                    "int",
                    10,
                    500,
                    group=G1,
                ),
                SettingSpec(
                    "ob_filter",
                    "Order block filter",
                    "Average range measure marking high-volatility bars (range >= 2x).",
                    "choice",
                    choices=(("atr", "Atr"), ("cmr", "Cumulative Mean Range")),
                    group=G1,
                ),
                SettingSpec(
                    "mitigation",
                    "Order block mitigation",
                    "High/Low or Close mitigation.",
                    "choice",
                    choices=(("highlow", "High/Low"), ("close", "Close")),
                    group=G1,
                ),
                SettingSpec(
                    "max_scan",
                    "Max bars scanned",
                    "Maximum bars scanned backward for an order block candle.",
                    "int",
                    50,
                    5000,
                    group=G1,
                ),
                SettingSpec(
                    "internal_confluence",
                    "Internal confluence filter",
                    "Require bar wick confluence on internal breaks.",
                    "bool",
                    group=G1,
                ),
                SettingSpec(
                    "max_breaker_age",
                    "Formed within (candles)",
                    "1 = last candle only, 2 = last two, 0 = no limit.",
                    "int",
                    0,
                    5000,
                    group=G2,
                ),
                SettingSpec(
                    "require_retest",
                    "Require retest",
                    "Only list breakers retested and held by a later candle.",
                    "bool",
                    group=G2,
                ),
                SettingSpec(
                    "max_distance_pct",
                    "Max distance above zone (%)",
                    "Skip stocks whose last close is farther above zone top. 0 = off.",
                    "float",
                    0,
                    100,
                    group=G2,
                ),
            ]
            + zt.specs(G2)
            + [
                SettingSpec(
                    "show_invalid",
                    "Show failed / old breakers",
                    "Also include invalidated, too old, or rejected rows.",
                    "bool",
                    group=G3,
                ),
            ]
        )

    def columns(self) -> List[ColumnSpec]:
        return [
            ColumnSpec("symbol", "Symbol"),
            ColumnSpec("status", "Status"),
            ColumnSpec("last_close", "Last close", "num"),
            ColumnSpec("ob_low", "Zone low", "num"),
            ColumnSpec("ob_high", "Zone high", "num"),
            ColumnSpec("dist_pct", "Above zone %", "pct", "Last close vs zone top"),
            zt.column(),
            zt.reaction_column(),
            ColumnSpec("source", "From", tooltip="Internal or swing structure"),
            ColumnSpec("ob_time", "OB candle", tooltip="The bearish order block candle"),
            ColumnSpec("break_time", "Breaker formed", tooltip="Candle that promoted the OB to +BB"),
            ColumnSpec("break_close", "Formed close", "num"),
            ColumnSpec("since_break", "Candles since", "int"),
            ColumnSpec("retest_time", "Retest"),
            ColumnSpec("others", "Other breakers", "int", "Other active bullish breakers"),
            ColumnSpec("timeframe", "TF"),
        ]

    def min_candles(self, settings: Dict[str, Any]) -> int:
        return int(settings["swing_length"]) + 10

    def scan(
        self, candles: Sequence[Candle], settings: Dict[str, Any], ctx: ScanContext
    ) -> List[ResultRow]:
        cs = list(candles)
        if len(cs) < self.min_candles(settings):
            return []
        n = len(cs)
        bulls = [b for b in run(cs, settings) if b.bias == 1]
        last = cs[-1].c
        rows: List[ResultRow] = []
        valid: List[Breaker] = []
        rejected: List[Breaker] = []
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        for b in bulls:
            b.reaction = zt.reaction_text(tags)  # type: ignore[attr-defined]
            age = n - 1 - b.formed
            dist = (last - b.top) / b.top * 100 if b.top else 0.0
            b.last_pos = (  # type: ignore[attr-defined]
                zt.BREAKOUT
                if b.formed == n - 1
                else zt.classify(cs[-1], b.top, b.bot, settings.get("near_pct", 1.0))
            )
            zt_verdict = zt.verdict(b.last_pos, tags, settings)  # type: ignore[attr-defined]
            if b.invalid_at is not None:
                status = STATUS_INVALID
            elif settings["max_breaker_age"] > 0 and age >= settings["max_breaker_age"]:
                status = STATUS_OLD
            elif settings["max_distance_pct"] > 0 and dist > settings["max_distance_pct"]:
                status = STATUS_FAR
            elif settings["require_retest"] and b.retest is None:
                status = STATUS_NO_RETEST
            elif zt_verdict:
                status = zt_verdict
            else:
                status = STATUS_RETESTED if b.retest is not None else STATUS_FRESH
            (valid if status in VALID else rejected).append(b)
            b.status = status  # type: ignore[attr-defined]
        valid.sort(key=lambda b: (b.formed, b.ob_index), reverse=True)
        distinct: List[Breaker] = []
        seen = set()
        for b in valid:
            if b.ob_index not in seen:
                seen.add(b.ob_index)
                distinct.append(b)
        if distinct:
            rows.append(ResultRow(self._row(cs, distinct[0], ctx, len(distinct) - 1), True))
        if settings.get("show_invalid"):
            rejected.sort(key=lambda b: b.formed, reverse=True)
            rows += [ResultRow(self._row(cs, b, ctx, 0), False) for b in rejected]
        return rows

    @staticmethod
    def _row(cs: Sequence[Candle], b: Breaker, ctx: ScanContext, others: int) -> Dict[str, Any]:
        fmt = "%Y-%m-%d %H:%M" if ctx.intraday else "%Y-%m-%d"

        def when(k: Optional[int]) -> str:
            return from_epoch(cs[k].ts).strftime(fmt) if k is not None else ""

        last = cs[-1].c
        return {
            "symbol": ctx.symbol,
            "status": getattr(b, "status", ""),
            "last_close": round(last, 2),
            "ob_low": round(b.bot, 2),
            "ob_high": round(b.top, 2),
            "dist_pct": round((last - b.top) / b.top * 100, 2) if b.top else None,
            "source": b.source,
            "ob_time": when(b.ob_index),
            "break_time": when(b.formed),
            "break_close": round(cs[b.formed].c, 2),
            "since_break": len(cs) - 1 - b.formed,
            "retest_time": when(b.retest),
            "last_pos": getattr(b, "last_pos", ""),
            "reaction": getattr(b, "reaction", ""),
            "others": others,
            "timeframe": ctx.timeframe,
        }
