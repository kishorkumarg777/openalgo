"""Strategy registry, default configurations (exact match with stock_screener/config/config.json),
setting specs, validation, and run summaries.
"""
from __future__ import annotations

import copy
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from services.screener.candles import FORMING_MODES, TIMEFRAMES, forming_mode
from services.screener.filters import FILTER_SPECS, validate_filters
from services.screener.models import SettingSpec, Strategy, compact, validate
from services.screener.mtf import MTF_DEFAULTS, MTF_SPECS
from services.screener.mtf import validate as validate_mtf
from services.screener.ob_place import ObAboveObStrategy, ObAtOpenStrategy, ObInBreakerStrategy
from services.screener.ob_stack import ObBelowBearishStrategy, ObSweepStrategy
from services.screener.potential_ob import PotentialObStrategy
from services.screener.smc_breaker import SmcBreakerStrategy
from services.screener.smc_ob import SmcOrderBlockStrategy
from services.screener.timeutil import now_ist

UNIVERSE_MODES: Dict[str, str] = {
    "all_nse_eq": "All NSE stocks (EQ series)",
    "nifty50": "NIFTY 50 (Quick scan)",
    "custom": "Custom ticker list",
    "watchlist": "Watchlist upload (.txt / .csv)",
}

DATA_SOURCES: Dict[str, str] = {
    "api": "Broker API (Live)",
    "db": "Historify DB (Local)",
}

NIFTY_50_SYMBOLS: Tuple[str, ...] = (
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
)

_STRATEGIES: List[Strategy] = [
    SmcBreakerStrategy(),
    SmcOrderBlockStrategy(),
    PotentialObStrategy(),
    ObBelowBearishStrategy(),
    ObSweepStrategy(),
    ObAboveObStrategy(),
    ObAtOpenStrategy(),
    ObInBreakerStrategy(),
]

_BY_ID: Dict[str, Strategy] = {s.id: s for s in _STRATEGIES}


def all_strategies() -> List[Strategy]:
    return list(_STRATEGIES)


def get(strategy_id: str) -> Strategy:
    if strategy_id not in _BY_ID:
        raise KeyError(f"Unknown screener {strategy_id!r}")
    return _BY_ID[strategy_id]


SCAN_DEFAULTS: Dict[str, Any] = {
    "strategy": "smc_breaker",
    "timeframe": "1D",
    "source": "api",
    "lookback_candles": 250,
    "as_of_date": "",
    "forming_candle": "final",
    "universe_mode": "all_nse_eq",
    "custom_symbols": "",
    "watchlist_symbols": "",
    "min_price": 50,
    "rsi_period": 14,
    "rsi_min": 0,
    "rsi_max": 65,
    "avg_period": 20,
    "min_avg_volume": 50000,
    "min_avg_value_cr": 0,
    **MTF_DEFAULTS,
}

STRATEGY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "smc_breaker": {
        "internal_obs": True,
        "swing_obs": False,
        "breakers_from": "both",
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "max_breaker_age": 1,
        "require_retest": False,
        "max_distance_pct": 0,
        "last_at_zone": "off",
        "near_pct": 1.0,
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
    "smc_ob": {
        "internal_obs": True,
        "swing_obs": False,
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "chart_shown": 5,
        "last_at_zone": "touch",
        "near_pct": 1.0,
        "reaction": "off",
        "wick_ratio": 2.0,
        "max_ob_age": 0,
        "show_invalid": False,
    },
    "potential_ob": {
        "swing_length": 50,
        "ob_filter": "atr",
        "max_scan": 2000,
        "structure": "internal",
        "low_within": 2,
        "min_pullback_pct": 3.0,
        "max_to_break_pct": 0,
        "prior_low": True,
        "prior_low_tol_pct": 3.0,
        "liq_length": 10,
        "older_ob": True,
        "older_ob_from": "both",
        "older_ob_zones": "obs_bb",
        "older_ob_shown": 5,
        "older_ob_tol_pct": 1.0,
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
    "ob_below_bearish": {
        "internal_obs": True,
        "swing_obs": True,
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "chart_shown": 5,
        "max_gap_pct": 1.0,
        "bear_first": True,
        "last_candle": "any",
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
    "ob_sweep": {
        "internal_obs": True,
        "swing_obs": True,
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "chart_shown": 5,
        "liq_length": 10,
        "merge_atr": 0.25,
        "in_ob": "touch",
        "max_below_pct": 0,
        "level_in_ob": False,
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
    "ob_above_ob": {
        "internal_obs": True,
        "swing_obs": True,
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "chart_shown": 5,
        "upper_bias": "any",
        "lower_bias": "any",
        "max_gap_pct": 1.0,
        "max_overlap_pct": 0,
        "last_candle": "any",
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
    "ob_at_open": {
        "internal_obs": True,
        "swing_obs": True,
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "chart_shown": 5,
        "candle_time": "09:15",
        "bias": "bullish",
        "only_latest": True,
        "max_age_days": 0,
        "last_at_ob": False,
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
    "ob_in_breaker": {
        "internal_obs": True,
        "swing_obs": True,
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
        "chart_shown": 5,
        "inside_tol_pct": 0,
        "min_inside_share_pct": 60,
        "ob_after_bb": True,
        "in_ob": "touch",
        "reaction": "off",
        "wick_ratio": 2.0,
        "show_invalid": False,
    },
}

SCAN_GROUP = "Scan & Universe"

SCAN_SPECS: List[SettingSpec] = [
    SettingSpec(
        "strategy",
        "Screener",
        "Which SMC screener to run.",
        "choice",
        choices=tuple((s.id, s.name) for s in _STRATEGIES),
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "timeframe",
        "Timeframe",
        "Candle size. 1D = daily; intraday sizes come from the same broker/DB source.",
        "choice",
        choices=tuple((k, k) for k in TIMEFRAMES),
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "source",
        "Data source",
        "Broker API (live historical bars; uses INDstocks 5-stock batching on INDMoney) or local Historify DB.",
        "choice",
        choices=tuple(DATA_SOURCES.items()),
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "lookback_candles",
        "Lookback (candles)",
        "How many completed candles to download per stock. More = older order blocks found, but slower.",
        "int",
        50,
        2000,
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "as_of_date",
        "Scan as of date",
        "Run the scan with data only up to the close of this past day (YYYY-MM-DD). Blank = latest data.",
        "text",
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "forming_candle",
        "Forming candle",
        "Which candle counts as the last one. Completed only: drop open bar. Final stretch: include open bar "
        "near its close (1W Fri, 1M month-end, 1D >= 15:00 IST, intraday last 25%). Always: include open bar anytime.",
        "choice",
        choices=tuple(FORMING_MODES.items()),
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "universe_mode",
        "Stocks to scan",
        "Which stocks to screen.",
        "choice",
        choices=tuple(UNIVERSE_MODES.items()),
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "custom_symbols",
        "Custom list",
        "NSE tickers separated by commas, spaces or new lines.",
        "text",
        group=SCAN_GROUP,
    ),
    SettingSpec(
        "watchlist_symbols",
        "Watchlist symbols",
        "Tickers loaded from an uploaded .txt or .csv watchlist (one per line or first CSV column).",
        "text",
        group=SCAN_GROUP,
    ),
] + FILTER_SPECS + MTF_SPECS


def scan_defaults() -> Dict[str, Any]:
    return copy.deepcopy(SCAN_DEFAULTS)


def strategy_defaults(strategy_id: str) -> Dict[str, Any]:
    if strategy_id not in STRATEGY_DEFAULTS:
        raise KeyError(f"Unknown screener {strategy_id!r}")
    return copy.deepcopy(STRATEGY_DEFAULTS[strategy_id])


def migrate_scan(values: Dict[str, Any]) -> Dict[str, Any]:
    """Support stock_screener legacy keys (use_forming_candle, watchlist_path)."""
    out = dict(values or {})
    if "use_forming_candle" in out:
        old = out.pop("use_forming_candle")
        if out.get("forming_candle") in (None, ""):
            out["forming_candle"] = forming_mode(old)
    return out


def parse_as_of(text: str) -> Optional[dt.date]:
    text = (text or "").strip()
    if not text:
        return None
    return dt.datetime.strptime(text, "%Y-%m-%d").date()


def validate_as_of(text: str, today: Optional[dt.date] = None) -> List[str]:
    try:
        d = parse_as_of(text)
    except ValueError:
        return ["Scan as of date: use YYYY-MM-DD, e.g. 2026-10-19 (or leave blank)."]
    if d is None:
        return []
    ref = today or now_ist().date()
    if d > ref:
        return ["Scan as of date: must not be in the future."]
    if d.year < 2000:
        return ["Scan as of date: must be 2000 or later."]
    return []


def validate_run_settings(
    strategy_id: str,
    timeframe: str,
    scan_input: Optional[Dict[str, Any]] = None,
    strategy_input: Optional[Dict[str, Any]] = None,
    *,
    require_universe_symbols: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str], List[str]]:
    """Validate and normalize scan + strategy settings against defaults.
    Returns (clean_scan, clean_strategy, errors, warnings).
    """
    defaults = scan_defaults()
    raw_scan = migrate_scan({**defaults, **(scan_input or {})})
    raw_scan["strategy"] = strategy_id
    raw_scan["timeframe"] = timeframe

    known_scan = {s.key for s in SCAN_SPECS}
    warnings = [
        f"scan: unknown key '{k}' ignored"
        for k in (scan_input or {})
        if k not in known_scan and not str(k).startswith("_") and k not in ("use_forming_candle", "watchlist_path")
    ]

    clean_scan, errors = validate(SCAN_SPECS, raw_scan, defaults)
    if not errors:
        if require_universe_symbols:
            if clean_scan["universe_mode"] == "custom" and not clean_scan["custom_symbols"].strip():
                errors.append("Custom list: enter at least one ticker.")
            if clean_scan["universe_mode"] == "watchlist" and not clean_scan["watchlist_symbols"].strip():
                errors.append("Watchlist: upload a .txt/.csv watchlist or paste symbols.")
        errors += validate_filters(clean_scan)
        errors += validate_as_of(clean_scan.get("as_of_date", ""))
        errors += validate_mtf(clean_scan)

    try:
        strat_obj = get(strategy_id)
    except KeyError:
        return clean_scan, {}, errors + [f"Unknown screener '{strategy_id}'."], warnings

    if strat_obj.timeframes and timeframe not in strat_obj.timeframes:
        errors.append(
            f"{strat_obj.name} works on {' / '.join(strat_obj.timeframes)} only - "
            f"choose one of those timeframes (not {timeframe})."
        )

    strat_specs = strat_obj.settings_specs()
    strat_def = strategy_defaults(strategy_id)
    raw_strat = {**strat_def, **(strategy_input or {})}
    known_strat = {s.key for s in strat_specs}
    warnings += [
        f"strategies.{strategy_id}: unknown key '{k}' ignored"
        for k in (strategy_input or {})
        if k not in known_strat and not str(k).startswith("_")
    ]

    clean_strat, strat_errs = validate(strat_specs, raw_strat, strat_def)
    errors += strat_errs
    if not strat_errs:
        errors += strat_obj.check(clean_strat)

    return clean_scan, clean_strat, errors, warnings


def describe_run(scan: Dict[str, Any], strat: Dict[str, Any]) -> str:
    """One-line human-readable summary of the active scan configuration."""
    uni = {
        "all_nse_eq": "All NSE EQ",
        "nifty50": "NIFTY 50",
        "custom": "custom list",
        "watchlist": "watchlist",
    }.get(scan.get("universe_mode"), str(scan.get("universe_mode")))
    if scan.get("universe_mode") == "custom":
        syms = [x for x in str(scan.get("custom_symbols", "")).replace(",", " ").split() if x]
        uni = f"{len(syms)} custom: {', '.join(syms[:4])}{' ...' if len(syms) > 4 else ''}"
    elif scan.get("universe_mode") == "watchlist":
        syms = [x for x in str(scan.get("watchlist_symbols", "")).replace(",", " ").split() if x]
        uni = f"{len(syms)} watchlist"

    src = "Historify DB" if scan.get("source") == "db" else "Broker API"
    parts = [
        src,
        uni,
        f"lookback {scan.get('lookback_candles')}",
        f"forming: {scan.get('forming_candle')}",
    ]
    if scan.get("as_of_date"):
        parts.insert(0, f"AS OF {scan['as_of_date']}")
    f: List[str] = []
    if scan.get("min_price"):
        f.append(f"price >= {scan['min_price']:g}")
    if scan.get("rsi_min", 0) > 0 or scan.get("rsi_max", 100) < 100:
        f.append(f"RSI {scan.get('rsi_min', 0):g}-{scan.get('rsi_max', 100):g}")
    if scan.get("min_avg_volume"):
        f.append(f"avg vol >= {compact(scan['min_avg_volume'])}")
    if scan.get("min_avg_value_cr"):
        f.append(f"avg value >= {scan['min_avg_value_cr']:g} Cr")
    parts.append("filters: " + (", ".join(f) if f else "off"))
    if scan.get("ltf_on"):
        parts.append("LTF check on" + (" (keep only)" if scan.get("ltf_keep_only") else ""))
    if scan.get("htf_on"):
        parts.append("other-TF OB on" + (" (keep only)" if scan.get("htf_keep_only") else ""))
    if strat.get("show_invalid"):
        parts.append("showing rejected")
    try:
        for k in get(scan.get("strategy", "")).ui_keys:
            parts.append(f"{k}: {strat.get(k)}")
    except KeyError:
        pass
    return " | ".join(parts)
