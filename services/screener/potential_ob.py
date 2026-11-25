"""Potential bullish order block — BEFORE the structure break (SMC)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from services.screener import liquidity as lq
from services.screener import mtf
from services.screener import zone_touch as zt
from services.screener.models import Candle, ColumnSpec, ResultRow, ScanContext, SettingSpec, Strategy
from services.screener.smc_breaker import SmcBreakerStrategy, run_full
from services.screener.timeutil import from_epoch

STATUS_OK = "Potential OB"
STATUS_NO_LEVEL = "No break level"
STATUS_NOT_RECENT = "Low not recent"
STATUS_SHALLOW = "Pullback too small"
STATUS_NO_PRIOR = "Not near a prior low"
STATUS_FAR = "Too far from break"
STATUS_NO_OB = "Not on an older OB"
CHART_KEYS = ("swing_length", "ob_filter", "max_scan")


class PotentialObStrategy(Strategy):
    id = "potential_ob"
    name = "Potential OB - before the break (SMC)"
    description = (
        "Early setup: price pulled back to a fresh low below an unbroken swing high, "
        "optionally sitting on a prior swing low and an older active demand OB."
    )

    def settings_specs(self) -> List[SettingSpec]:
        G1, G2, G3 = "Chart settings (match your SMC script)", "Setup", "Display"
        chart = [s for s in SmcBreakerStrategy().settings_specs() if s.key in CHART_KEYS]
        return chart + [
            SettingSpec(
                "structure",
                "Break level from",
                "Internal = 5-candle pivot high; Swing = swing-length pivot high.",
                "choice",
                choices=(("internal", "Internal structure"), ("swing", "Swing structure")),
                group=G2,
            ),
            SettingSpec(
                "low_within",
                "Low made within (candles)",
                "Pullback low must be within the last N candles.",
                "int",
                1,
                50,
                group=G2,
            ),
            SettingSpec(
                "min_pullback_pct",
                "Min pullback (%)",
                "Minimum pullback below break level (%).",
                "float",
                0,
                90,
                group=G2,
            ),
            SettingSpec(
                "max_to_break_pct",
                "Max distance to break (%)",
                "Skip stocks farther than this % below break level (0 = off).",
                "float",
                0,
                200,
                group=G2,
            ),
            SettingSpec(
                "prior_low",
                "Near a prior low",
                "Require pullback low near an older swing low formed before the swing high.",
                "bool",
                group=G2,
            ),
            SettingSpec(
                "prior_low_tol_pct",
                "Prior low tolerance (%)",
                "Distance tolerance (%) to older swing low.",
                "float",
                0,
                20,
                group=G2,
            ),
            SettingSpec(
                "liq_length",
                "Prior-low pivot length",
                "Pivot length for older swing lows.",
                "int",
                2,
                100,
                group=G2,
            ),
            SettingSpec(
                "older_ob",
                "On an older OB",
                "Require pullback low to sit on an active bullish OB older than the swing high.",
                "bool",
                group=G2,
            ),
            SettingSpec(
                "older_ob_from",
                "Older OB kinds",
                "Internal, swing, or both.",
                "choice",
                choices=(
                    ("both", "Internal + swing"),
                    ("internal", "Internal"),
                    ("swing", "Swing"),
                ),
                group=G2,
            ),
            SettingSpec(
                "older_ob_zones",
                "Older zones that count",
                "Bullish OBs only, or OBs + valid bullish breakers.",
                "choice",
                choices=(("obs", "Bullish OBs"), ("obs_bb", "OBs + bullish breakers")),
                group=G2,
            ),
            SettingSpec(
                "older_ob_shown",
                "Older OB: newest per kind",
                "Newest N OBs per kind (0 = all active OBs).",
                "int",
                0,
                100,
                group=G2,
            ),
            SettingSpec(
                "older_ob_tol_pct",
                "Older OB tolerance (%)",
                "Pullback low <= OB top*(1+tol) and last close >= OB bottom*(1-tol).",
                "float",
                0,
                10,
                group=G2,
            ),
            SettingSpec(
                "reaction",
                "Bullish reaction on the last candle",
                "Require green, rejection wick, or bullish engulfing on the last candle.",
                "choice",
                choices=zt.REACTION_CHOICES,
                group=G2,
            ),
            SettingSpec(
                "wick_ratio",
                "Wick ratio",
                "Lower wick >= this x body for Rejection wick.",
                "float",
                0.5,
                10,
                group=G2,
            ),
            SettingSpec(
                "show_invalid",
                "Show rejected setups",
                "Also list rejected stocks greyed with reason.",
                "bool",
                group=G3,
            ),
        ]

    def columns(self) -> List[ColumnSpec]:
        return [
            ColumnSpec("symbol", "Symbol"),
            ColumnSpec("status", "Status"),
            ColumnSpec("last_close", "Last close", "num"),
            ColumnSpec("ob_low", "Zone low", "num", "Candidate OB = pullback-low candle"),
            ColumnSpec("ob_high", "Zone high", "num"),
            ColumnSpec("break_level", "Break level", "num", "Close above this creates the OB"),
            ColumnSpec("to_break_pct", "To break %", "pct", "Rise needed to break level"),
            ColumnSpec("pullback_pct", "Pullback %", "pct", "Low vs break level"),
            ColumnSpec("prior_low", "Prior low", "num", "Nearest older swing low"),
            ColumnSpec("prior_low_time", "Prior low from"),
            ColumnSpec("older_ob", "Older OB", tooltip="Older bullish OB / breaker the pullback sits on"),
            ColumnSpec("older_ob_hits", "Older OBs hit", "int", "Count of older bullish zones hit"),
            ColumnSpec("ob_time", "Low candle"),
            ColumnSpec("high_time", "Swing high"),
            zt.reaction_column(),
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
        port = {
            "internal_obs": True,
            "swing_obs": True,
            "breakers_from": "both",
            "mitigation": "highlow",
            "internal_confluence": False,
            **settings,
        }
        st = run_full(cs, port)
        t = len(cs) - 1
        if settings["structure"] == "swing":
            level, p_idx, crossed = st.swing_high, st.swing_high_index, st.swing_high_crossed
        else:
            level, p_idx, crossed = st.int_high, st.int_high_index, st.int_high_crossed
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        if level is None or p_idx is None or crossed or cs[t].c > level:
            if settings.get("show_invalid") and level is not None:
                return [
                    ResultRow(
                        {
                            "symbol": ctx.symbol,
                            "status": STATUS_NO_LEVEL,
                            "last_close": round(cs[t].c, 2),
                            "break_level": round(level, 2),
                            "reaction": zt.reaction_text(tags),
                            "timeframe": ctx.timeframe,
                        },
                        False,
                    )
                ]
            return []
        lo_from = max(p_idx, t - int(settings["max_scan"]))
        k = lo_from
        for i in range(lo_from + 1, t + 1):
            if st.parsed_l[i] < st.parsed_l[k]:
                k = i
        z_lo, z_hi = min(st.parsed_l[k], st.parsed_h[k]), max(st.parsed_l[k], st.parsed_h[k])
        pullback = (level - z_lo) / level * 100 if level else 0.0
        to_break = (level - cs[t].c) / cs[t].c * 100 if cs[t].c else 0.0
        prior = None
        if settings["prior_low"]:
            pivots = lq.run(cs, int(settings["liq_length"]), 0.0).pivots
            prior = lq.nearest_prior_low(pivots, z_lo, p_idx)
        older, older_hits = None, 0
        if settings.get("older_ob", False):
            kinds = {
                "both": ("Internal", "Swing"),
                "internal": ("Internal",),
                "swing": ("Swing",),
            }[settings.get("older_ob_from", "both")]
            tol = float(settings.get("older_ob_tol_pct", 1.0)) / 100
            zones = [
                z
                for z in mtf.zones_from_state(
                    st,
                    int(settings.get("older_ob_shown", 5)),
                    settings.get("older_ob_zones", "obs_bb") == "obs_bb",
                    kinds,
                )
                if z.since < p_idx and cs[k].l <= z.top * (1 + tol) and cs[t].c >= z.bot * (1 - tol)
            ]
            older, older_hits = (zones[0] if zones else None), len(zones)
        if t - k >= int(settings["low_within"]):
            status = STATUS_NOT_RECENT
        elif pullback < settings["min_pullback_pct"]:
            status = STATUS_SHALLOW
        elif settings["max_to_break_pct"] > 0 and to_break > settings["max_to_break_pct"]:
            status = STATUS_FAR
        elif settings["prior_low"] and (prior is None or prior[1] > settings["prior_low_tol_pct"]):
            status = STATUS_NO_PRIOR
        elif settings.get("older_ob", False) and older is None:
            status = STATUS_NO_OB
        elif (
            settings["reaction"] != "off"
            and not set(tags) & zt.REACTION_ACCEPT[settings["reaction"]]
        ):
            status = zt.STATUS_NO_REACTION
        else:
            status = STATUS_OK
        if status != STATUS_OK and not settings.get("show_invalid"):
            return []
        fmt = "%Y-%m-%d %H:%M" if ctx.intraday else "%Y-%m-%d"

        def when(i: Optional[int]) -> str:
            return from_epoch(cs[i].ts).strftime(fmt) if i is not None else ""

        return [
            ResultRow(
                {
                    "symbol": ctx.symbol,
                    "status": status,
                    "last_close": round(cs[t].c, 2),
                    "ob_low": round(z_lo, 2),
                    "ob_high": round(z_hi, 2),
                    "break_level": round(level, 2),
                    "to_break_pct": round(to_break, 2),
                    "pullback_pct": round(-pullback, 2),
                    "prior_low": round(prior[0].price, 2) if prior else None,
                    "prior_low_time": when(prior[0].index) if prior else "",
                    "older_ob": (
                        f"{older.kind} {older.bot:,.2f} - {older.top:,.2f}, from {when(older.since)}"
                        if older
                        else ""
                    ),
                    "older_ob_hits": older_hits,
                    "ob_time": when(k),
                    "high_time": when(p_idx),
                    "reaction": zt.reaction_text(tags),
                    "timeframe": ctx.timeframe,
                },
                status == STATUS_OK,
            )
        ]
