"""SMC Stock Screener execution engine, background job manager, and single-stock SMC Explain diagnostic.

Exact parity with stock_screener/src/services/scanner.py and stock_screener/tools/explain.py.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.screener import filters, mtf, registry
from services.screener.candles import TIMEFRAMES, Timeframe, get_timeframe
from services.screener.data_provider import (
    Clock,
    Instrument,
    ScreenerApiError,
    ScreenerAuthError,
    ScreenerCancelled,
    ScreenerDataProvider,
    ScreenerNetworkError,
    create_provider,
    resolve_universe,
)
from services.screener.models import Candle, ColumnSpec, ResultRow, ScanContext
from services.screener.smc_breaker import run_full
from services.screener.timeutil import IST, from_epoch, now_ist
from utils.logging import get_logger

logger = get_logger(__name__)

BUSY_WAIT = 15.0
BUSY_TRIES = 4
NET_WAIT = 15.0
NET_TRIES = 3

JOB_TTL_SECONDS = 3600
MAX_RETAINED_JOBS = 24


@dataclass
class Progress:
    done: int
    total: int
    message: str
    phase: str = "main"  # "init" | "main" | "ltf" | "htf" | "done"


@dataclass
class ScanOutcome:
    rows: List[ResultRow] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    short: List[str] = field(default_factory=list)
    filtered: int = 0
    as_of: str = ""
    ltf_checked: int = 0
    ltf_confirmed: int = 0
    htf_checked: int = 0
    htf_hits: int = 0
    mtf_removed: int = 0
    scanned: int = 0
    universe: int = 0
    calls: int = 0
    cancelled: bool = False
    fatal: str = ""
    started: Optional[dt.datetime] = None
    finished: Optional[dt.datetime] = None

    @property
    def summary(self) -> str:
        hits = sum(1 for r in self.rows if r.valid)
        parts = ([f"AS OF {self.as_of}"] if self.as_of else []) + [
            f"{hits} match(es) in {self.scanned}/{self.universe} stocks",
            f"{self.calls} calls",
        ]
        if self.started and self.finished:
            parts.append(f"{(self.finished - self.started).total_seconds():.0f} s")
        if self.filtered:
            parts.append(f"{self.filtered} removed by price / volume / RSI filters")
        if self.ltf_checked:
            parts.append(f"LTF: {self.ltf_confirmed}/{self.ltf_checked} confirmed")
        if self.htf_checked:
            parts.append(f"other TF OB: {self.htf_hits}/{self.htf_checked}")
        if self.mtf_removed:
            parts.append(f"{self.mtf_removed} removed by multi-timeframe 'keep only'")
        if self.short:
            parts.append(f"{len(self.short)} with too little history")
        if self.errors:
            parts.append(f"{len(self.errors)} with errors")
        if self.missing:
            parts.append(f"{len(self.missing)} unknown ticker(s): {', '.join(self.missing[:10])}")
        if self.cancelled:
            parts.append("STOPPED by user")
        if self.fatal:
            parts.append(f"STOPPED: {self.fatal}")
        return " | ".join(parts)


def _resilient(
    provider: ScreenerDataProvider,
    op: Callable[[], Any],
    what: str,
    on_progress: Callable[[Progress], None],
    done: int,
    total: int,
    phase: str,
    skip_on_busy: bool,
) -> Any:
    net_tries = 0
    busy = 0
    while True:
        provider.clock.check()
        try:
            return op()
        except ScreenerAuthError:
            raise
        except ScreenerNetworkError as e:
            net_tries += 1
            if net_tries > NET_TRIES:
                raise
            logger.warning("Network issue during %s (%s); retrying in %.0fs", what, e, NET_WAIT)
            on_progress(Progress(done, total, f"Network issue - retrying in {NET_WAIT:.0f}s...", phase))
            provider.clock.sleep(NET_WAIT)
        except ScreenerApiError as e:
            if not e.retryable:
                raise
            busy += 1
            if busy > BUSY_TRIES:
                if skip_on_busy:
                    logger.error("Skipping %s after %d tries: %s", what, BUSY_TRIES, e)
                    return None
                raise
            provider.slow_down(f"server busy ({e.status})")
            on_progress(
                Progress(
                    done,
                    total,
                    f"Broker busy - retrying in {BUSY_WAIT:.0f}s ({busy}/{BUSY_TRIES})...",
                    phase,
                )
            )
            provider.clock.sleep(BUSY_WAIT)


def _ltf_lookback(tf: Timeframe, days: int) -> int:
    if tf.label == "1W":
        n = days // 7 + 2
    elif tf.label == "1D":
        n = days + 2
    elif tf.label == "1M":
        n = days // 28 + 2
    else:
        n = (days + 1) * -(-375 // tf.minutes)
    return n + mtf.LTF_WARMUP


def _reject(out: ScanOutcome, r: ResultRow, why: str, keep_rejected: bool) -> None:
    out.mtf_removed += 1
    if keep_rejected:
        r.values["status"] = f"{r.values.get('status', '')} | {why}".strip(" |")
        r.valid = False
    else:
        if r in out.rows:
            out.rows.remove(r)


def _apply_ltf(
    out: ScanOutcome,
    r: ResultRow,
    scan: Dict[str, Any],
    chart: Dict[str, Any],
    tf: Timeframe,
    cs: List[Candle],
    err: Optional[str],
    now: dt.datetime,
    keep_rejected: bool,
) -> None:
    out.ltf_checked += 1
    zone = mtf.row_zone(r.values)
    if err or not cs:
        res = mtf.LtfResult(mtf.LTF_NO_DATA)
    elif zone is None:
        res = mtf.LtfResult(mtf.LTF_NO_ZONE)
    else:
        start = now - dt.timedelta(days=mtf.ltf_days(scan))
        zs = mtf.zone_start(r.values)
        if zs is not None and zs > start:
            start = zs
        res = mtf.ltf_check(
            cs,
            zone,
            start,
            mtf.ltf_tol_pct(scan),
            mtf.confirmations(scan),
            scan.get("ltf_mode", "any"),
            chart,
            int(scan.get("ltf_pivot", 5)),
        )
    r.values.update(mtf.ltf_values(cs, res, tf.label, tf.intraday))
    if err:
        r.values["ltf_status"] = f"{mtf.LTF_NO_DATA}: {err}"
    if res.status == mtf.LTF_CONFIRMED:
        out.ltf_confirmed += 1
    elif scan.get("ltf_keep_only"):
        _reject(out, r, f"LTF: {res.status}", keep_rejected)


def _apply_htf(
    out: ScanOutcome,
    r: ResultRow,
    scan: Dict[str, Any],
    chart: Dict[str, Any],
    tf: Timeframe,
    cs: List[Candle],
    err: Optional[str],
    last: Candle,
    keep_rejected: bool,
) -> None:
    out.htf_checked += 1
    hit = False
    if err or not cs:
        r.values.update(
            {
                "htf_ob": f"no {tf.label} data" + (f": {err}" if err else ""),
                "htf_overlap": None,
                "htf_hits": None,
            }
        )
    else:
        zones = mtf.htf_zones(
            cs,
            chart,
            int(scan.get("htf_shown", 5)),
            scan.get("htf_zones") == "obs_bb",
        )
        hit, vals = mtf.htf_values(
            cs,
            last,
            mtf.row_zone(r.values),
            zones,
            float(scan.get("htf_tol_pct", 1.0)),
            tf.label,
        )
        r.values.update(vals)
    if hit:
        out.htf_hits += 1
    elif scan.get("htf_keep_only"):
        _reject(out, r, f"no {tf.label} OB under the last candle", keep_rejected)


def _mtf_phase(
    provider: ScreenerDataProvider,
    scan: Dict[str, Any],
    strat_settings: Dict[str, Any],
    out: ScanOutcome,
    last_of: Dict[str, Candle],
    inst_of: Dict[str, Instrument],
    now: dt.datetime,
    on_progress: Callable[[Progress], None],
    on_update: Callable[[List[ResultRow]], None],
    on_stats: Callable[[ScanOutcome], None] = lambda o: None,
) -> None:
    targets = [r for r in out.rows if r.valid and r.values.get("symbol") in inst_of]
    symbols = list(dict.fromkeys(r.values["symbol"] for r in targets))
    if not symbols:
        return
    keep_rejected = bool(strat_settings.get("show_invalid"))
    chart = mtf.chart_settings(strat_settings)
    mode = scan.get("forming_candle", "off")
    checks: List[Tuple[str, Timeframe, int]] = []
    if scan.get("ltf_on"):
        ltf_label = mtf.ltf_of(scan)
        if ltf_label:
            ltf = TIMEFRAMES[ltf_label]
            checks.append(("ltf", ltf, _ltf_lookback(ltf, mtf.ltf_days(scan))))
    if scan.get("htf_on"):
        htf_label = mtf.htf_of(scan)
        if htf_label:
            htf = TIMEFRAMES[htf_label]
            checks.append(("htf", htf, mtf.HTF_LOOKBACK[htf.label]))

    step = max(1, provider.batch_size)
    for kind, tf, lookback in checks:
        name = f"{tf.label} {'confirmation' if kind == 'ltf' else 'OB confluence'}"
        for b in range(0, len(symbols), step):
            provider.clock.check()
            sym_batch = symbols[b : b + step]
            inst_batch = [inst_of[s] for s in sym_batch]
            on_progress(Progress(b, len(symbols), f"{name}: {sym_batch[0]}...", kind))
            try:
                got = _resilient(
                    provider,
                    lambda ib=inst_batch: provider.fetch_batch(tf, ib, lookback, now, include_forming=mode),
                    f"{name} batch {sym_batch[0]}",
                    on_progress,
                    b,
                    len(symbols),
                    kind,
                    skip_on_busy=True,
                )
            except ScreenerApiError as e:
                if isinstance(e, (ScreenerAuthError, ScreenerNetworkError)):
                    raise
                logger.error("%s batch %s failed: %s", name, sym_batch[0], e)
                got = None
            data, errs = (
                got
                if got is not None
                else ({}, {i.code: "broker kept failing" for i in inst_batch})
            )
            for inst in inst_batch:
                cs = data.get(inst.code) or []
                for r in [r for r in targets if r.values["symbol"] == inst.symbol and r in out.rows]:
                    try:
                        if kind == "ltf":
                            _apply_ltf(
                                out, r, scan, chart, tf, cs, errs.get(inst.code), now, keep_rejected
                            )
                        else:
                            _apply_htf(
                                out,
                                r,
                                scan,
                                chart,
                                tf,
                                cs,
                                errs.get(inst.code),
                                last_of[inst.symbol],
                                keep_rejected,
                            )
                    except Exception as e:
                        logger.exception("%s failed on %s", name, inst.symbol)
                        r.values["ltf_status" if kind == "ltf" else "htf_ob"] = f"error: {e}"
            out.calls = provider.calls_count
            on_update(out.rows)
            on_stats(out)


def run_scan_with_provider(
    provider: ScreenerDataProvider,
    scan: Dict[str, Any],
    strat_settings: Dict[str, Any],
    on_progress: Callable[[Progress], None] = lambda p: None,
    on_rows: Callable[[List[ResultRow]], None] = lambda rows: None,
    now_fn: Callable[[], dt.datetime] = now_ist,
    on_update: Callable[[List[ResultRow]], None] = lambda rows: None,
    on_stats: Callable[[ScanOutcome], None] = lambda o: None,
) -> ScanOutcome:
    """Run a full SMC scan over the configured universe using any ScreenerDataProvider."""
    out = ScanOutcome(started=now_fn())
    data_now = now_fn
    as_of = scan.get("as_of_date", "").strip() if isinstance(scan.get("as_of_date"), str) else ""
    if as_of:
        cutoff = dt.datetime.strptime(as_of, "%Y-%m-%d").replace(hour=15, minute=40, tzinfo=IST)
        data_now = lambda: min(now_fn(), cutoff)  # noqa: E731
        out.as_of = as_of

    strat = registry.get(scan["strategy"])
    tf = get_timeframe(scan["timeframe"])
    try:
        on_progress(Progress(0, 0, "Loading instrument list...", "init"))
        instruments = _resilient(
            provider,
            lambda: provider.load_instruments(out.started.date()),
            "instrument list",
            on_progress,
            0,
            0,
            "init",
            skip_on_busy=False,
        )
        allow_syn = scan.get("source") == "db" and scan.get("universe_mode") != "all_nse_eq"
        universe, out.missing = resolve_universe(
            instruments,
            scan["universe_mode"],
            scan.get("custom_symbols", ""),
            scan.get("watchlist_symbols", ""),
            allow_synthetic=allow_syn,
        )
        out.universe = len(universe)
        if not universe:
            out.fatal = "No stocks to scan (check your tickers, watchlist, or Historify DB data)."
            return out

        min_c = strat.min_candles(strat_settings)
        last_of: Dict[str, Candle] = {}
        inst_of: Dict[str, Instrument] = {}
        step = max(1, provider.batch_size)
        forming = scan.get("forming_candle", "off")
        lookback = int(scan["lookback_candles"])

        for b in range(0, len(universe), step):
            provider.clock.check()
            batch = universe[b : b + step]
            on_progress(Progress(b, len(universe), f"Scanning {batch[0].symbol}...", "main"))
            now = data_now()
            try:
                got = _resilient(
                    provider,
                    lambda bt=batch, nw=now: provider.fetch_batch(
                        tf, bt, lookback, nw, include_forming=forming
                    ),
                    f"batch {batch[0].symbol}",
                    on_progress,
                    b,
                    len(universe),
                    "main",
                    skip_on_busy=True,
                )
            except ScreenerApiError as e:
                if isinstance(e, (ScreenerAuthError, ScreenerNetworkError)):
                    raise
                got = None
                logger.error("Batch %s failed: %s", batch[0].symbol, e)

            if got is None:
                data, errs = {}, {i.code: "skipped: broker kept failing for this batch" for i in batch}
            else:
                data, errs = got

            new_rows: List[ResultRow] = []
            for inst in batch:
                out.scanned += 1
                if inst.code in errs:
                    out.errors[inst.symbol] = errs[inst.code]
                    continue
                cs = data.get(inst.code) or []
                if len(cs) < min_c:
                    out.short.append(inst.symbol)
                    continue
                ctx = ScanContext(inst.symbol, inst.code, tf.label, tf.intraday, now)
                last_of[inst.symbol] = cs[-1]
                inst_of[inst.symbol] = inst
                try:
                    rows = strat.scan(cs, strat_settings, ctx)
                    rows, removed = filters.apply(
                        cs, rows, scan, bool(strat_settings.get("show_invalid")), now
                    )
                    out.filtered += removed
                    new_rows += rows
                except Exception as e:
                    logger.exception("Screener failed on %s", inst.symbol)
                    out.errors[inst.symbol] = f"screener error: {e}"
            if new_rows:
                out.rows += new_rows
                on_rows(new_rows)
            out.calls = provider.calls_count
            on_stats(out)

        if scan.get("ltf_on") or scan.get("htf_on"):
            _mtf_phase(
                provider,
                scan,
                strat_settings,
                out,
                last_of,
                inst_of,
                data_now(),
                on_progress,
                on_update,
                on_stats,
            )
        on_progress(Progress(len(universe), len(universe), "Scan complete", "done"))
    except ScreenerCancelled:
        out.cancelled = True
    except (ScreenerAuthError, ScreenerNetworkError, ScreenerApiError, ValueError, FileNotFoundError) as e:
        out.fatal = str(e)
    finally:
        out.calls = provider.calls_count
        out.finished = now_fn()
    return out


# -- Single-stock SMC Explain diagnostic (port of stock_screener/tools/explain.py) -----------
EXPLAIN_CHART_DEFAULTS = {
    "internal_obs": True,
    "swing_obs": False,
    "swing_length": 50,
    "ob_filter": "atr",
    "mitigation": "highlow",
    "max_scan": 2000,
    "internal_confluence": False,
}


def _fmt_day(cs: Sequence[Candle], idx: Optional[int], intraday: bool) -> str:
    if idx is None or idx < 0 or idx >= len(cs):
        return "-"
    return from_epoch(cs[idx].ts).strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d")


def explain_symbol(
    provider: ScreenerDataProvider,
    symbol: str,
    scan: Dict[str, Any],
    strat_settings: Dict[str, Any],
    now_fn: Callable[[], dt.datetime] = now_ist,
) -> Dict[str, Any]:
    """Run one screener on one stock with rejected rows enabled and return every active OB & +BB."""
    sym = (symbol or "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    if not sym:
        raise ValueError("Enter a stock symbol to explain.")

    now = now_fn()
    as_of = scan.get("as_of_date", "").strip() if isinstance(scan.get("as_of_date"), str) else ""
    if as_of:
        cutoff = dt.datetime.strptime(as_of, "%Y-%m-%d").replace(hour=15, minute=40, tzinfo=IST)
        now = min(now, cutoff)

    tf = get_timeframe(scan["timeframe"])
    strategy = registry.get(scan["strategy"])
    instruments = provider.load_instruments(now.date())
    by_sym = {i.symbol: i for i in instruments}
    inst = by_sym.get(sym)
    if inst is None:
        if scan.get("source") == "db":
            inst = Instrument("NSE", sym, sym, sym, "EQ")
        else:
            raise ValueError(f"{sym} is not in the NSE EQ instrument master.")

    data, errs = provider.fetch_batch(
        tf,
        [inst],
        int(scan["lookback_candles"]),
        now,
        include_forming=scan.get("forming_candle", "off"),
    )
    if inst.code in errs:
        raise ScreenerApiError(errs[inst.code])
    cs = data.get(inst.code) or []
    if not cs:
        raise ValueError(f"No candles returned for {sym} on {tf.label}.")

    last = cs[-1]
    ctx = ScanContext(inst.symbol, inst.code, tf.label, tf.intraday, now)
    min_c = strategy.min_candles(strat_settings)
    if len(cs) < min_c:
        rows: List[ResultRow] = []
    else:
        rows = strategy.scan(cs, {**strat_settings, "show_invalid": True}, ctx)
        rows, _ = filters.apply(cs, rows, scan, True, now)

    chart_cfg = {k: strat_settings.get(k, v) for k, v in EXPLAIN_CHART_DEFAULTS.items()}
    state = run_full(cs, {**chart_cfg, "breakers_from": "both"})
    chart_shown = int(strat_settings.get("chart_shown", 5))

    def ser_ob(kind: str, rank: int, ob: Any) -> Dict[str, Any]:
        lo, hi = min(ob.low, ob.high), max(ob.low, ob.high)
        return {
            "kind": kind,
            "rank": rank + 1,
            "drawn_on_chart": chart_shown == 0 or rank < chart_shown,
            "bias": "bullish" if ob.bias == 1 else "bearish",
            "low": round(lo, 2),
            "high": round(hi, 2),
            "ob_time": _fmt_day(cs, ob.index, tf.intraday),
            "formed_time": _fmt_day(cs, ob.created, tf.intraday),
            "source": ob.source,
        }

    internal_obs = [ser_ob("Internal", k, ob) for k, ob in enumerate(state.iobs[:15])]
    swing_obs = [ser_ob("Swing", k, ob) for k, ob in enumerate(state.sobs[:15])]
    bullish_breakers = [
        {
            "low": round(b.bot, 2),
            "high": round(b.top, 2),
            "ob_time": _fmt_day(cs, b.ob_index, tf.intraday),
            "formed_time": _fmt_day(cs, b.formed, tf.intraday),
            "source": b.source,
            "retested": b.retest is not None,
            "retest_time": _fmt_day(cs, b.retest, tf.intraday) if b.retest is not None else "",
        }
        for b in sorted(state.breakers, key=lambda b: b.formed, reverse=True)
        if b.bias == 1 and b.invalid_at is None
    ][:20]

    rsi = filters.last_rsi(cs, int(scan.get("rsi_period", 14)))
    avg_vol, avg_val = filters.averages(cs, int(scan.get("avg_period", 20)))

    return {
        "symbol": inst.symbol,
        "name": inst.name,
        "timeframe": tf.label,
        "strategy": strategy.id,
        "strategy_name": strategy.name,
        "candles_count": len(cs),
        "min_candles": min_c,
        "too_short": len(cs) < min_c,
        "provider": provider.provider_name,
        "judged_candle": {
            "label": filters.candle_label(last, scan, now),
            "ts": last.ts,
            "o": round(last.o, 2),
            "h": round(last.h, 2),
            "l": round(last.l, 2),
            "c": round(last.c, 2),
            "v": round(last.v),
            "rsi": round(rsi, 2) if rsi is not None else None,
            "avg_volume": round(avg_vol) if avg_vol is not None else None,
            "avg_value_cr": round(avg_val, 2) if avg_val is not None else None,
        },
        "columns": [c.to_dict() for c in filters.merge_columns(strategy.columns(), scan)],
        "rows": [{"valid": r.valid, "values": r.values} for r in rows],
        "chart_settings": {**chart_cfg, "chart_shown": chart_shown},
        "internal_obs": internal_obs,
        "swing_obs": swing_obs,
        "bullish_breakers": bullish_breakers,
    }


# -- Background Job Manager -------------------------------------------------------------------
class ScanJob:
    def __init__(
        self,
        job_id: str,
        owner_key: str,
        scan: Dict[str, Any],
        strat_settings: Dict[str, Any],
        columns: List[ColumnSpec],
        config_summary: str,
        provider_name: str,
    ):
        self.job_id = job_id
        self.owner_key = owner_key
        self.scan = scan
        self.strat_settings = strat_settings
        self.columns = columns
        self.config_summary = config_summary
        self.provider_name = provider_name

        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.state: str = "running"  # "running" | "completed" | "cancelled" | "failed"
        self.progress = Progress(0, 0, "Starting scan...", "init")
        self.rows: List[Dict[str, Any]] = []
        self.live_stats: Dict[str, int] = {
            "scanned": 0,
            "universe": 0,
            "filtered": 0,
            "short": 0,
            "errors": 0,
            "missing": 0,
            "calls": 0,
            "ltf_checked": 0,
            "ltf_confirmed": 0,
            "htf_checked": 0,
            "htf_hits": 0,
            "mtf_removed": 0,
        }
        self.outcome: Optional[ScanOutcome] = None
        self.created_at = time.monotonic()
        self.finished_at: Optional[float] = None

    def set_progress(self, p: Progress) -> None:
        with self.lock:
            self.progress = p

    def set_live_stats(self, out: ScanOutcome) -> None:
        with self.lock:
            self.live_stats = {
                "scanned": out.scanned,
                "universe": out.universe,
                "filtered": out.filtered,
                "short": len(out.short),
                "errors": len(out.errors),
                "missing": len(out.missing),
                "calls": out.calls,
                "ltf_checked": out.ltf_checked,
                "ltf_confirmed": out.ltf_confirmed,
                "htf_checked": out.htf_checked,
                "htf_hits": out.htf_hits,
                "mtf_removed": out.mtf_removed,
            }

    def append_rows(self, new_rows: Sequence[ResultRow]) -> None:
        with self.lock:
            for r in new_rows:
                self.rows.append({"valid": r.valid, "values": dict(r.values)})

    def replace_rows(self, all_rows: Sequence[ResultRow]) -> None:
        with self.lock:
            self.rows = [{"valid": r.valid, "values": dict(r.values)} for r in all_rows]

    def finish(self, outcome: ScanOutcome) -> None:
        with self.lock:
            self.outcome = outcome
            self.rows = [{"valid": r.valid, "values": dict(r.values)} for r in outcome.rows]
            if outcome.cancelled:
                self.state = "cancelled"
            elif outcome.fatal:
                self.state = "failed"
            else:
                self.state = "completed"
            self.finished_at = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            out = self.outcome
            elapsed = (
                (self.finished_at - self.created_at)
                if self.finished_at is not None
                else (time.monotonic() - self.created_at)
            )
            matches = sum(1 for r in self.rows if r["valid"])
            rejected = len(self.rows) - matches
            return {
                "job_id": self.job_id,
                "state": self.state,
                "strategy": self.scan["strategy"],
                "timeframe": self.scan["timeframe"],
                "source": self.scan["source"],
                "provider": self.provider_name,
                "config_summary": self.config_summary,
                "columns": [c.to_dict() for c in self.columns],
                "progress": {
                    "done": self.progress.done,
                    "total": self.progress.total,
                    "message": self.progress.message,
                    "phase": self.progress.phase,
                },
                "rows": list(self.rows),
                "stats": {
                    "matches": matches,
                    "rejected": rejected,
                    "scanned": out.scanned if out else self.live_stats["scanned"],
                    "universe": out.universe if out else (self.live_stats["universe"] or self.progress.total),
                    "filtered": out.filtered if out else self.live_stats["filtered"],
                    "short": len(out.short) if out else self.live_stats["short"],
                    "errors": len(out.errors) if out else self.live_stats["errors"],
                    "missing": len(out.missing) if out else self.live_stats["missing"],
                    "calls": out.calls if out else self.live_stats["calls"],
                    "ltf_checked": out.ltf_checked if out else self.live_stats["ltf_checked"],
                    "ltf_confirmed": out.ltf_confirmed if out else self.live_stats["ltf_confirmed"],
                    "htf_checked": out.htf_checked if out else self.live_stats["htf_checked"],
                    "htf_hits": out.htf_hits if out else self.live_stats["htf_hits"],
                    "mtf_removed": out.mtf_removed if out else self.live_stats["mtf_removed"],
                    "elapsed_sec": round(elapsed, 1),
                },
                "summary": out.summary if out else self.progress.message,
                "fatal": out.fatal if out else "",
                "missing_symbols": list(out.missing[:50]) if out else [],
                "error_samples": dict(list(out.errors.items())[:25]) if out else {},
            }


class ScanJobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, ScanJob] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            jid
            for jid, j in self._jobs.items()
            if j.finished_at is not None and (now - j.finished_at) > JOB_TTL_SECONDS
        ]
        for jid in expired:
            self._jobs.pop(jid, None)
        if len(self._jobs) > MAX_RETAINED_JOBS:
            finished = sorted(
                [j for j in self._jobs.values() if j.finished_at is not None],
                key=lambda j: j.finished_at or 0.0,
            )
            for j in finished[: len(self._jobs) - MAX_RETAINED_JOBS]:
                self._jobs.pop(j.job_id, None)

    def start_scan(
        self,
        owner_key: str,
        scan: Dict[str, Any],
        strat_settings: Dict[str, Any],
        *,
        broker: Optional[str] = None,
        auth_token: Optional[str] = None,
        feed_token: Optional[str] = None,
        provider: Optional[ScreenerDataProvider] = None,
    ) -> ScanJob:
        strat = registry.get(scan["strategy"])
        cols = filters.merge_columns(strat.columns(), scan)
        summary = registry.describe_run(scan, strat_settings)

        cancel_event = threading.Event()
        clock = Clock(cancel_event)
        prov = provider or create_provider(
            scan["source"],
            broker=broker,
            auth_token=auth_token,
            feed_token=feed_token,
            clock=clock,
        )
        if provider is not None:
            cancel_event = provider.clock.cancel

        job_id = uuid.uuid4().hex[:16]
        job = ScanJob(
            job_id=job_id,
            owner_key=owner_key,
            scan=scan,
            strat_settings=strat_settings,
            columns=cols,
            config_summary=summary,
            provider_name=prov.provider_name,
        )
        job.cancel_event = cancel_event

        with self._lock:
            self._prune()
            self._jobs[job_id] = job

        def _worker() -> None:
            try:
                outcome = run_scan_with_provider(
                    prov,
                    scan,
                    strat_settings,
                    on_progress=job.set_progress,
                    on_rows=job.append_rows,
                    on_update=job.replace_rows,
                    on_stats=job.set_live_stats,
                )
            except Exception as e:
                logger.exception("Unhandled error in screener job %s", job_id)
                outcome = ScanOutcome(fatal=str(e), started=now_ist(), finished=now_ist())
            job.finish(outcome)

        t = threading.Thread(target=_worker, name=f"smc-screener-{job_id}", daemon=True)
        t.start()
        return job

    def get_job(self, job_id: str, owner_key: str) -> Optional[ScanJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner_key != owner_key:
                return None
            return job

    def stop_job(self, job_id: str, owner_key: str) -> Optional[ScanJob]:
        job = self.get_job(job_id, owner_key)
        if job is not None:
            job.cancel_event.set()
        return job


job_manager = ScanJobManager()


# -- Metadata catalog for UI ------------------------------------------------------------------
def get_metadata() -> Dict[str, Any]:
    strategies_meta = []
    for s in registry.all_strategies():
        defaults = registry.strategy_defaults(s.id)
        strategies_meta.append(
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "timeframes": list(s.timeframes) if s.timeframes else list(TIMEFRAMES.keys()),
                "ui_keys": list(s.ui_keys),
                "settings_specs": [sp.to_dict() for sp in s.settings_specs()],
                "defaults": defaults,
                "columns": [c.to_dict() for c in s.columns()],
            }
        )
    return {
        "status": "success",
        "data": {
            "strategies": strategies_meta,
            "scan_specs": [sp.to_dict() for sp in registry.SCAN_SPECS],
            "scan_defaults": registry.scan_defaults(),
            "strategy_defaults": {
                s.id: registry.strategy_defaults(s.id) for s in registry.all_strategies()
            },
            "timeframes": [
                {
                    "label": tf.label,
                    "openalgo_interval": tf.openalgo_interval,
                    "intraday": tf.intraday,
                }
                for tf in TIMEFRAMES.values()
            ],
            "universe_modes": [
                {"value": k, "label": v} for k, v in registry.UNIVERSE_MODES.items()
            ],
            "data_sources": [
                {"value": k, "label": v} for k, v in registry.DATA_SOURCES.items()
            ],
            "nifty50_count": len(registry.NIFTY_50_SYMBOLS),
        },
    }
