"""SMC Order Block — price at active bullish order block (LuxAlgo port)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from services.screener import zone_touch as zt
from services.screener.models import Candle, ColumnSpec, ResultRow, ScanContext, SettingSpec, Strategy
from services.screener.smc_breaker import SmcBreakerStrategy, run_state
from services.screener.timeutil import from_epoch

STATUS_AT_OB = "At OB"
STATUS_ACTIVE = "Active OB"
STATUS_OLD = "Too old"
STATUS_HIDDEN = "Not drawn on chart"
CHART_KEYS = (
    "internal_obs",
    "swing_obs",
    "swing_length",
    "ob_filter",
    "mitigation",
    "max_scan",
    "internal_confluence",
)


class SmcOrderBlockStrategy(Strategy):
    id = "smc_ob"
    name = "SMC Order Block - price at OB (LuxAlgo port)"
    description = (
        "Stocks whose last candle landed on an active bullish order block (demand zone) "
        "matching Smart Money Concepts [LuxAlgo]."
    )

    def settings_specs(self) -> List[SettingSpec]:
        G1, G2, G3 = "Chart settings (match your SMC script)", "Screener filters", "Display"
        chart = [s for s in SmcBreakerStrategy().settings_specs() if s.key in CHART_KEYS]
        return (
            chart
            + [
                SettingSpec(
                    "chart_shown",
                    "Chart shows (newest OBs per kind)",
                    "Newest N OBs drawn per kind (chart default 5). 0 = all active OBs.",
                    "int",
                    0,
                    100,
                    group=G1,
                ),
            ]
            + zt.specs(G2)
            + [
                SettingSpec(
                    "max_ob_age",
                    "Formed within (candles)",
                    "1 = last candle only, 2 = last two, 0 = no limit.",
                    "int",
                    0,
                    5000,
                    group=G2,
                ),
                SettingSpec(
                    "show_invalid",
                    "Show OBs not at zone / old",
                    "Also include active bullish OBs not reached, too old, or hidden on chart.",
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
            zt.column(),
            zt.reaction_column(),
            ColumnSpec("dist_pct", "Above zone %", "pct", "Last close vs zone top"),
            ColumnSpec("source", "From", tooltip="Internal or swing structure"),
            ColumnSpec("ob_time", "OB candle", tooltip="Order block candle"),
            ColumnSpec("created_time", "Structure break", tooltip="Bullish break that created the OB"),
            ColumnSpec("since_created", "Candles since", "int"),
            ColumnSpec("others", "Other OBs hit", "int", "Other bullish OBs the last candle landed on"),
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
        _, iobs, sobs = run_state(cs, {**settings, "breakers_from": "both"})
        limit = int(settings["chart_shown"])
        mode = settings.get("last_at_zone", "touch")
        tags = zt.reaction(cs, settings.get("wick_ratio", 2.0))
        react = zt.reaction_text(tags)
        valid, rejected = [], []
        for lst in (iobs, sobs):
            for k, ob in enumerate(lst):
                if ob.bias != 1:
                    continue
                top, bot = max(ob.high, ob.low), min(ob.high, ob.low)
                pos = zt.classify(cs[-1], top, bot, settings.get("near_pct", 1.0))
                age = n - 1 - ob.created
                zt_v = zt.verdict(pos, tags, {**settings, "last_at_zone": mode})
                if limit > 0 and k >= limit:
                    status = STATUS_HIDDEN
                elif settings["max_ob_age"] > 0 and age >= settings["max_ob_age"]:
                    status = STATUS_OLD
                elif zt_v:
                    status = zt_v
                else:
                    status = STATUS_ACTIVE if mode == "off" else STATUS_AT_OB
                item = (ob, top, bot, pos, status, react)
                (valid if status in (STATUS_AT_OB, STATUS_ACTIVE) else rejected).append(item)
        valid.sort(key=lambda x: (x[0].created, x[0].index), reverse=True)
        rows: List[ResultRow] = []
        if valid:
            rows.append(ResultRow(self._row(cs, valid[0], ctx, len(valid) - 1), True))
        if settings.get("show_invalid"):
            rejected.sort(key=lambda x: (x[0].created, x[0].index), reverse=True)
            rows += [ResultRow(self._row(cs, it, ctx, 0), False) for it in rejected]
        return rows

    @staticmethod
    def _row(cs: Sequence[Candle], item: Any, ctx: ScanContext, others: int) -> Dict[str, Any]:
        ob, top, bot, pos, status, react = item
        fmt = "%Y-%m-%d %H:%M" if ctx.intraday else "%Y-%m-%d"

        def when(k: Optional[int]) -> str:
            return from_epoch(cs[k].ts).strftime(fmt) if k is not None and k >= 0 else ""

        last = cs[-1].c
        return {
            "symbol": ctx.symbol,
            "status": status,
            "last_close": round(last, 2),
            "ob_low": round(bot, 2),
            "ob_high": round(top, 2),
            "last_pos": pos,
            "reaction": react,
            "dist_pct": round((last - top) / top * 100, 2) if top else None,
            "source": ob.source,
            "ob_time": when(ob.index),
            "created_time": when(ob.created),
            "since_created": len(cs) - 1 - ob.created,
            "others": others,
            "timeframe": ctx.timeframe,
        }
