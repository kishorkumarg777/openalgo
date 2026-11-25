"""Multi-timeframe checks run after the main scan on matched rows only:
1. Lower-timeframe (LTF) confirmation: BOS/CHoCH, LTF OB, or LTF SSL sweep inside the zone.
2. Other-timeframe (HTF) OB confluence: last candle sitting on an active bullish OB or +BB.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.screener import liquidity as lq
from services.screener.models import Candle, ColumnSpec, SettingSpec
from services.screener.smc_breaker import OrderBlock, StructureBreak, run_full
from services.screener.timeutil import IST, from_epoch

GROUP = "Multi-timeframe checks"

ORDER = ["1M", "1W", "1D", "4H", "2H", "1H", "30m", "15m"]
LTF_AUTO = {
    "1M": "1W",
    "1W": "1D",
    "1D": "1H",
    "4H": "15m",
    "2H": "15m",
    "1H": "15m",
    "30m": "15m",
}
LTF_DAYS_AUTO = {"1W": 90, "1D": 20, "4H": 10, "2H": 7, "1H": 5, "30m": 3, "15m": 3}
LTF_WARMUP = 60
HTF_AUTO = {
    "1W": "1M",
    "1D": "1W",
    "4H": "1D",
    "2H": "1D",
    "1H": "1D",
    "30m": "1H",
    "15m": "1H",
}
HTF_LOOKBACK = {
    "1M": 120,
    "1W": 250,
    "1D": 400,
    "4H": 200,
    "2H": 200,
    "1H": 200,
    "30m": 200,
    "15m": 200,
}
TOL_CHOICES = (
    ("auto", "Auto (0.5 %, 1.0 % when scanning 1W / 1M)"),
    ("0", "0 %"),
    ("0.25", "0.25 %"),
    ("0.5", "0.5 %"),
    ("1", "1 %"),
    ("1.5", "1.5 %"),
    ("2", "2 %"),
    ("3", "3 %"),
    ("5", "5 %"),
)

CHART_DEFAULTS = {
    "internal_obs": True,
    "swing_obs": False,
    "breakers_from": "both",
    "swing_length": 50,
    "ob_filter": "atr",
    "mitigation": "highlow",
    "max_scan": 2000,
    "internal_confluence": False,
}

LTF_CONFIRMED = "Confirmed"
LTF_WAITING = "Waiting (in zone)"
LTF_NOT_IN_ZONE = "Not in zone"
LTF_BELOW = "Closed below zone"
LTF_NO_DATA = "No LTF data"
LTF_NO_ZONE = "No zone"

MTF_SPECS: List[SettingSpec] = [
    SettingSpec(
        "ltf_on",
        "Lower-timeframe confirmation",
        "Check each matched stock on a lower timeframe for bullish BOS/CHoCH, OB, or SSL sweep inside the zone.",
        "bool",
        group=GROUP,
    ),
    SettingSpec(
        "ltf_timeframe",
        "LTF timeframe",
        "Auto: 1M -> 1W, 1W -> 1D, 1D -> 1H, 4H/2H/1H/30m -> 15m.",
        "choice",
        choices=(("auto", "Auto"),) + tuple((k, k) for k in ORDER[1:]),
        group=GROUP,
    ),
    SettingSpec(
        "ltf_days",
        "LTF look-back (days)",
        "Calendar days of LTF candles (0 = Auto).",
        "int",
        0,
        365,
        group=GROUP,
    ),
    SettingSpec(
        "ltf_mode",
        "Confirmations needed",
        "Any of = one ticked confirmation is enough; All of = every ticked one required.",
        "choice",
        choices=(("any", "Any of"), ("all", "All of")),
        group=GROUP,
    ),
    SettingSpec(
        "ltf_use_break",
        "Break up (BOS / CHoCH)",
        "LTF internal (5-candle) bullish structure break after price reached the zone.",
        "bool",
        group=GROUP,
    ),
    SettingSpec(
        "ltf_use_ob",
        "LTF OB in zone",
        "The bullish OB created by that break has its low inside the zone.",
        "bool",
        group=GROUP,
    ),
    SettingSpec(
        "ltf_use_sweep",
        "LTF sweep in zone",
        "An LTF SSL sweep whose low is inside the zone.",
        "bool",
        group=GROUP,
    ),
    SettingSpec(
        "ltf_tol",
        "LTF zone tolerance",
        "Widen the zone on both sides by this percentage.",
        "choice",
        choices=TOL_CHOICES,
        group=GROUP,
    ),
    SettingSpec(
        "ltf_pivot",
        "LTF pivot length (sweeps)",
        "Swing low pivot length for LTF SSL sweeps.",
        "int",
        2,
        20,
        group=GROUP,
    ),
    SettingSpec(
        "ltf_keep_only",
        "Keep only LTF-confirmed rows",
        "Remove rows that are not LTF Confirmed.",
        "bool",
        group=GROUP,
    ),
    SettingSpec(
        "htf_on",
        "Other-timeframe OB confluence",
        "Check whether each matched stock's last candle sits on an active bullish OB/breaker of another timeframe.",
        "bool",
        group=GROUP,
    ),
    SettingSpec(
        "htf_timeframe",
        "Other timeframe",
        "Auto (higher): 1D -> 1W, 1W -> 1M, 4H/2H/1H -> 1D, 30m/15m -> 1H.",
        "choice",
        choices=(("auto", "Auto (higher)"),) + tuple((k, k) for k in ORDER),
        group=GROUP,
    ),
    SettingSpec(
        "htf_zones",
        "Zones that count",
        "Bullish OBs only, or bullish OBs + valid bullish breakers (+BB).",
        "choice",
        choices=(("obs", "Bullish OBs"), ("obs_bb", "OBs + bullish breakers")),
        group=GROUP,
    ),
    SettingSpec(
        "htf_shown",
        "Other TF: newest OBs per kind",
        "Newest N OBs per kind (0 = all active OBs).",
        "int",
        0,
        100,
        group=GROUP,
    ),
    SettingSpec(
        "htf_tol_pct",
        "Other TF touch tolerance (%)",
        "Last candle low <= top*(1+tol) and close >= bottom*(1-tol).",
        "float",
        0,
        5,
        group=GROUP,
    ),
    SettingSpec(
        "htf_keep_only",
        "Keep only rows with confluence",
        "Remove rows without an other-timeframe zone hit.",
        "bool",
        group=GROUP,
    ),
]
MTF_KEYS = [s.key for s in MTF_SPECS]
MTF_DEFAULTS: Dict[str, Any] = {
    "ltf_on": False,
    "ltf_timeframe": "auto",
    "ltf_days": 0,
    "ltf_mode": "any",
    "ltf_use_break": True,
    "ltf_use_ob": True,
    "ltf_use_sweep": False,
    "ltf_tol": "auto",
    "ltf_pivot": 5,
    "ltf_keep_only": False,
    "htf_on": False,
    "htf_timeframe": "auto",
    "htf_zones": "obs_bb",
    "htf_shown": 5,
    "htf_tol_pct": 1.0,
    "htf_keep_only": False,
}


def ltf_of(scan: Dict[str, Any]) -> Optional[str]:
    v = scan.get("ltf_timeframe", "auto")
    return LTF_AUTO.get(scan["timeframe"]) if v == "auto" else v


def htf_of(scan: Dict[str, Any]) -> Optional[str]:
    v = scan.get("htf_timeframe", "auto")
    return HTF_AUTO.get(scan["timeframe"]) if v == "auto" else v


def ltf_days(scan: Dict[str, Any]) -> int:
    d = int(scan.get("ltf_days", 0) or 0)
    return d if d > 0 else LTF_DAYS_AUTO.get(ltf_of(scan) or "", 5)


def ltf_tol_pct(scan: Dict[str, Any]) -> float:
    v = scan.get("ltf_tol", "auto")
    if v == "auto":
        return 1.0 if scan["timeframe"] in ("1W", "1M") else 0.5
    return float(v)


def confirmations(scan: Dict[str, Any]) -> List[str]:
    return [
        k
        for k, key in (
            ("break", "ltf_use_break"),
            ("ob", "ltf_use_ob"),
            ("sweep", "ltf_use_sweep"),
        )
        if scan.get(key)
    ]


def validate(scan: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    tf = scan.get("timeframe")
    if tf not in ORDER:
        return errors
    if scan.get("ltf_on"):
        ltf = ltf_of(scan)
        if ltf is None:
            errors.append(f"LTF confirmation: there is no lower timeframe than {tf}.")
        elif ORDER.index(ltf) <= ORDER.index(tf):
            errors.append(f"LTF timeframe: {ltf} is not lower than the scan timeframe {tf}.")
        if not confirmations(scan):
            errors.append(
                "LTF confirmation: tick at least one of Break up / LTF OB in zone / LTF sweep."
            )
    if scan.get("htf_on"):
        htf = htf_of(scan)
        if htf is None:
            errors.append(f"Other-timeframe OB: there is no higher timeframe than {tf}; choose one.")
        elif htf == tf:
            errors.append("Other timeframe: choose a timeframe different from the scan timeframe.")
    return errors


def columns(scan: Dict[str, Any]) -> List[ColumnSpec]:
    cols: List[ColumnSpec] = []
    if scan.get("ltf_on"):
        cols += [
            ColumnSpec("ltf", "LTF", tooltip="Lower timeframe checked"),
            ColumnSpec(
                "ltf_status",
                "LTF status",
                tooltip="Confirmed / Waiting (in zone) / Not in zone / Closed below zone",
            ),
            ColumnSpec(
                "ltf_break",
                "LTF break",
                tooltip="Newest LTF bullish break after price reached the zone",
            ),
            ColumnSpec("ltf_ob", "LTF OB", tooltip="LTF bullish OB inside the zone"),
            ColumnSpec("ltf_sweep", "LTF sweep", tooltip="LTF sell-side sweep inside the zone"),
        ]
    if scan.get("htf_on"):
        cols += [
            ColumnSpec(
                "htf_ob",
                "Other TF OB",
                tooltip="Newest other-timeframe bullish zone the last candle is on",
            ),
            ColumnSpec(
                "htf_overlap",
                "Overlap %",
                "pct",
                "Share of this row's zone inside the other-timeframe zone",
            ),
            ColumnSpec(
                "htf_hits",
                "Other TF zones hit",
                "int",
                "Number of other-timeframe zones the last candle is on",
            ),
        ]
    return cols


def chart_settings(strat_settings: Dict[str, Any]) -> Dict[str, Any]:
    return {k: strat_settings.get(k, v) for k, v in CHART_DEFAULTS.items()}


def row_zone(values: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    lo, hi = values.get("ob_low"), values.get("ob_high")
    if lo is None or hi is None:
        lo, hi = values.get("sweep_low"), values.get("level")
    if lo is None or hi is None:
        return None
    return (min(lo, hi), max(lo, hi))


def zone_start(values: Dict[str, Any]) -> Optional[dt.datetime]:
    text = values.get("ob_time") or values.get("sweep_time") or ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except (TypeError, ValueError):
            continue
    return None


@dataclass
class LtfResult:
    status: str
    brk: Optional[StructureBreak] = None
    ob: Optional[OrderBlock] = None
    sweep: Optional[lq.Sweep] = None
    entry: Optional[int] = None


def ltf_check(
    cs: Sequence[Candle],
    zone: Tuple[float, float],
    start: dt.datetime,
    tol_pct: float,
    need: Sequence[str],
    mode: str,
    chart: Dict[str, Any],
    pivot_len: int = 5,
) -> LtfResult:
    if not cs:
        return LtfResult(LTF_NO_DATA)
    z_lo, z_hi = zone[0] * (1 - tol_pct / 100), zone[1] * (1 + tol_pct / 100)
    start_ts = start.timestamp()
    w0 = next((i for i, c in enumerate(cs) if c.ts >= start_ts), None)
    if w0 is None:
        return LtfResult(LTF_NOT_IN_ZONE)
    e = next((i for i in range(w0, len(cs)) if cs[i].l <= z_hi), None)
    if e is None:
        return LtfResult(LTF_NOT_IN_ZONE)
    if any(cs[i].c < z_lo for i in range(e, len(cs))):
        return LtfResult(LTF_BELOW, entry=e)
    st = run_full(cs, {**chart, "internal_obs": True, "swing_obs": False, "breakers_from": "both"})
    breaks = [b for b in st.events if b.bias == 1 and b.kind == "Internal" and b.bar >= e]
    brk = breaks[-1] if breaks else None
    obs = [b.ob for b in breaks if b.ob is not None and z_lo <= min(b.ob.low, b.ob.high) <= z_hi]
    ob = obs[-1] if obs else None
    sweep = None
    if "sweep" in need:
        sws = [s for s in lq.run(cs, pivot_len, 0.25).sweeps if s.bar >= e and z_lo <= s.low <= z_hi]
        sweep = sws[-1] if sws else None
    got = {"break": brk is not None, "ob": ob is not None, "sweep": sweep is not None}
    met = [got[k] for k in need]
    ok = bool(met) and (all(met) if mode == "all" else any(met))
    return LtfResult(LTF_CONFIRMED if ok else LTF_WAITING, brk, ob, sweep, e)


def ltf_values(
    cs: Sequence[Candle], res: LtfResult, ltf: str, intraday: bool
) -> Dict[str, Any]:
    fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"

    def when(i: int) -> str:
        return from_epoch(cs[i].ts).strftime(fmt)

    out: Dict[str, Any] = {
        "ltf": ltf,
        "ltf_status": res.status,
        "ltf_break": "",
        "ltf_ob": "",
        "ltf_sweep": "",
    }
    if res.brk:
        out["ltf_break"] = f"{when(res.brk.bar)} {res.brk.tag} above {res.brk.level:,.2f}"
    if res.ob:
        lo, hi = min(res.ob.low, res.ob.high), max(res.ob.low, res.ob.high)
        out["ltf_ob"] = (
            f"{lo:,.2f} - {hi:,.2f} ({'mitigated' if res.ob.mitigated is not None else 'active'})"
        )
    if res.sweep:
        out["ltf_sweep"] = f"{when(res.sweep.bar)} low {res.sweep.low:,.2f}"
    return out


@dataclass
class HtfZone:
    top: float
    bot: float
    kind: str  # "OB" | "+BB"
    since: int
    order: Tuple[int, int]


def htf_zones(
    cs: Sequence[Candle], chart: Dict[str, Any], shown: int, with_breakers: bool
) -> List[HtfZone]:
    return zones_from_state(run_full(cs, {**chart, "breakers_from": "both"}), shown, with_breakers)


def zones_from_state(
    st: Any,
    shown: int,
    with_breakers: bool,
    kinds: Sequence[str] = ("Internal", "Swing"),
) -> List[HtfZone]:
    out: List[HtfZone] = []
    for kind, lst in (("Internal", st.iobs), ("Swing", st.sobs)):
        if kind not in kinds:
            continue
        for k, ob in enumerate(lst):
            if shown > 0 and k >= shown:
                break
            if ob.bias == 1:
                out.append(
                    HtfZone(
                        max(ob.high, ob.low),
                        min(ob.high, ob.low),
                        "OB",
                        ob.index,
                        (ob.created, ob.index),
                    )
                )
    if with_breakers:
        seen = set()
        for b in sorted(st.breakers, key=lambda b: (b.formed, b.ob_index), reverse=True):
            if b.bias == 1 and b.invalid_at is None and b.ob_index not in seen and b.source in kinds:
                seen.add(b.ob_index)
                out.append(HtfZone(b.top, b.bot, "+BB", b.ob_index, (b.formed, b.ob_index)))
    out.sort(key=lambda z: z.order, reverse=True)
    return out


def on_zone(last: Candle, z: HtfZone, tol_pct: float) -> bool:
    return last.l <= z.top * (1 + tol_pct / 100) and last.c >= z.bot * (1 - tol_pct / 100)


def overlap_pct(zone: Optional[Tuple[float, float]], z: HtfZone) -> Optional[float]:
    if zone is None:
        return None
    lo, hi = zone
    inter = max(0.0, min(hi, z.top) - max(lo, z.bot))
    if hi - lo <= 0:
        return 100.0 if z.bot <= lo <= z.top else 0.0
    return round(inter / (hi - lo) * 100, 1)


def htf_values(
    htf_cs: Sequence[Candle],
    last: Candle,
    zone: Optional[Tuple[float, float]],
    zones: List[HtfZone],
    tol_pct: float,
    htf: str,
) -> Tuple[bool, Dict[str, Any]]:
    hits = [z for z in zones if on_zone(last, z, tol_pct)]
    if not hits:
        return False, {"htf_ob": f"none on {htf}", "htf_overlap": None, "htf_hits": 0}
    z = hits[0]
    fmt = "%Y-%m-%d %H:%M" if htf not in ("1D", "1W", "1M") else "%Y-%m-%d"
    since = from_epoch(htf_cs[z.since].ts).strftime(fmt)
    return True, {
        "htf_ob": f"{htf} {z.kind} {z.bot:,.2f} - {z.top:,.2f}, from {since}",
        "htf_overlap": overlap_pct(zone, z),
        "htf_hits": len(hits),
    }
