"""Data providers and symbol universe management for the SMC Stock Screener.

Three strictly READ-ONLY providers share one interface:
  1. IndMoneyBatchProvider: native INDstocks 5-stock batching (/market/historical/{interval} +
     daily-cached /market/instruments?source=equity SERIES=='EQ'), matching stock_screener byte-for-byte.
  2. OpenAlgoBrokerProvider: universal fallback using services.history_service.get_history_with_auth
     for any other OpenAlgo broker (Zerodha, Dhan, Angel, Upstox, Fyers, etc.).
  3. HistorifyDbProvider: offline local scans from OpenAlgo's Historify DuckDB database.
"""
from __future__ import annotations

import abc
import csv
import datetime as dt
import io
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from services.screener.candles import (
    HISTORY_START_SLACK_DAYS,
    Timeframe,
    calendar_days_needed,
    completed,
    forming_mode,
    parse_candles,
    windows,
)
from services.screener.models import Candle
from services.screener.registry import NIFTY_50_SYMBOLS
from utils.logging import get_logger

logger = get_logger(__name__)

INDSTOCKS_BASE_URL = "https://api.indstocks.com"
HIST_MAX_CODES = 5
DAILY_LIMIT = 100_000


# -- Error hierarchy --------------------------------------------------------------------------
class ScreenerApiError(Exception):
    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        error_type: str = "",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.retryable = retryable


class ScreenerAuthError(ScreenerApiError):
    """Token missing, invalid, expired or revoked."""


class ScreenerNetworkError(ScreenerApiError):
    """Could not reach the API after retries."""


class ScreenerCancelled(Exception):
    """Scan cancelled by user."""


def describe_error(status: int, body: Any, text: str) -> Tuple[str, str]:
    etype, msg = "", ""
    if isinstance(body, dict):
        etype = str(body.get("error_type") or body.get("error_code") or "")
        msg = str(body.get("debug_info") or body.get("message") or body.get("error") or "")
        if body.get("debug_info") and body.get("message"):
            msg = f"{body['message']}: {body['debug_info']}"
    if not msg:
        msg = (text or "").strip()[:200] or f"HTTP {status}"
    return etype, msg


# -- Clock & RateLimiter ----------------------------------------------------------------------
class Clock:
    """Time source with cancellable sleep."""

    def __init__(self, cancel: Optional[threading.Event] = None):
        self.cancel = cancel or threading.Event()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.cancel.wait(seconds):
            raise ScreenerCancelled()

    def check(self) -> None:
        if self.cancel.is_set():
            raise ScreenerCancelled()


class RateLimiter:
    """Thread-safe pacing limiter with daily call counter."""

    def __init__(self, per_second: float, clock: Clock, daily_cap: int = DAILY_LIMIT):
        self.per_second = per_second
        self.min_gap = 1.0 / per_second
        self.clock = clock
        self.daily_cap = daily_cap
        self.next_ok = 0.0
        self.count = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            if self.count >= self.daily_cap * 0.98:
                raise ScreenerApiError(
                    f"Daily request budget ({self.daily_cap:,}) almost used up - stopping."
                )
            now = self.clock.time()
            wait = self.next_ok - now
            self.next_ok = max(now, self.next_ok) + self.min_gap
            self.count += 1
        if wait > 0:
            self.clock.sleep(wait)

    def set_rate(self, per_second: float) -> None:
        with self._lock:
            self.per_second = per_second
            self.min_gap = 1.0 / per_second


# -- Instruments & Universe -------------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    exch: str
    token: str
    symbol: str
    name: str
    series: str
    itype: str = ""

    @property
    def code(self) -> str:
        return f"{self.exch}_{self.token}"


def _clean_ticker(raw: str, series: str) -> str:
    t = (raw or "").strip().upper()
    if series and t.endswith("-" + series.upper()):
        t = t[: -(len(series) + 1)]
    return t


def parse_indstocks_instruments(
    csv_text: str, series: Iterable[str] = ("EQ",)
) -> List[Instrument]:
    """NSE cash-market equities of the given series from INDstocks's CSV master."""
    wanted = {s.upper() for s in series}
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if reader.fieldnames:
        reader.fieldnames = [f.strip().upper() for f in reader.fieldnames]
    out: List[Instrument] = []
    seen = set()
    for r in reader:
        if (r.get("EXCH") or "").strip().upper() != "NSE":
            continue
        if (r.get("INSTRUMENT_NAME") or "EQUITY").strip().upper() not in ("EQUITY", ""):
            continue
        ser = (r.get("SERIES") or "").strip().upper()
        if ser not in wanted:
            continue
        token = (r.get("SECURITY_ID") or "").strip()
        sym = _clean_ticker(r.get("TRADING_SYMBOL") or "", ser) or _clean_ticker(
            r.get("CUSTOM_SYMBOL") or "", ser
        )
        if not token or not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(
            Instrument(
                "NSE",
                token,
                sym,
                (r.get("SYMBOL_NAME") or "").strip(),
                ser,
                (r.get("SEM_EXCH_INSTRUMENT_TYPE") or "").strip().upper(),
            )
        )
    return out


def _cache_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    d = root / "db" / "screener_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(day: dt.date) -> Path:
    return _cache_dir() / f"instruments_equity_{day:%Y%m%d}.csv"


def load_cached_master(day: dt.date) -> Optional[str]:
    p = _cache_path(day)
    if p.exists() and p.stat().st_size > 1000:
        return p.read_text(encoding="utf-8")
    return None


def save_master(day: dt.date, text: str) -> None:
    cache_d = _cache_dir()
    keep = _cache_path(day).name
    for old in cache_d.glob("instruments_equity_*.csv"):
        if old.name != keep:
            try:
                old.unlink()
            except OSError:
                pass
    _cache_path(day).write_text(text, encoding="utf-8")


def split_symbols(text: str) -> List[str]:
    """'TCS, INFY\\nRELIANCE.NS' -> ['TCS', 'INFY', 'RELIANCE'] (order preserved, deduplicated)."""
    out: List[str] = []
    seen = set()
    for tok in re.split(r"[\s,;]+", text or ""):
        t = tok.strip().upper()
        if t.endswith(".NS"):
            t = t[:-3]
        if t.endswith("-EQ"):
            t = t[:-3]
        if t and t not in ("SYMBOL", "TICKER") and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_watchlist_text(text: str) -> List[str]:
    """Parse pasted or uploaded .txt/.csv watchlist text (first CSV column, ignoring # comments)."""
    syms: List[str] = []
    for line in (text or "").lstrip("\ufeff").splitlines():
        first = line.split(",")[0].strip()
        if first and not first.startswith("#"):
            syms.append(first)
    return split_symbols("\n".join(syms))


_NON_EQ_RE = re.compile(r"^[0-9]|NSETEST|-(BE|BZ|SM|ST|BL|IL|W1|N\d+)$", re.IGNORECASE)
_ETF_NAME_RE = re.compile(r"\b(ETF|BEES|LIQUIDCASE|GOLDCASE|NIFTYBEES)\b", re.IGNORECASE)


def load_symtoken_nse_eq() -> List[Instrument]:
    """Load NSE EQ stocks from OpenAlgo's SymToken SQLite table (filtering out test/bond rows)."""
    from database.symbol import SymToken, db_session

    try:
        rows = (
            db_session.query(SymToken)
            .filter(SymToken.exchange == "NSE", SymToken.instrumenttype == "EQ")
            .all()
        )
    finally:
        db_session.remove()

    out: List[Instrument] = []
    seen = set()
    for r in rows:
        sym = (r.symbol or "").strip().upper()
        tok = (r.token or "").strip()
        name = (r.name or "").strip()
        if not sym or not tok or sym in seen:
            continue
        if _NON_EQ_RE.search(sym):
            continue
        seen.add(sym)
        itype = "ETF" if _ETF_NAME_RE.search(f"{sym} {name}") else "ES"
        out.append(Instrument("NSE", tok, sym, name, "EQ", itype))
    out.sort(key=lambda i: i.symbol)
    return out


def resolve_universe(
    instruments: Sequence[Instrument],
    mode: str,
    custom_text: str = "",
    watchlist_text: str = "",
    *,
    allow_synthetic: bool = False,
) -> Tuple[List[Instrument], List[str]]:
    """Return (instruments to scan, unknown symbols)."""
    if mode == "all_nse_eq":
        return [i for i in instruments if i.itype != "ETF"], []
    if mode == "nifty50":
        wanted = list(NIFTY_50_SYMBOLS)
    elif mode == "custom":
        wanted = split_symbols(custom_text)
    elif mode == "watchlist":
        wanted = parse_watchlist_text(watchlist_text)
    else:
        raise ValueError(f"Unknown universe mode {mode!r}")

    by_sym = {i.symbol: i for i in instruments}
    picked: List[Instrument] = []
    missing: List[str] = []
    for s in wanted:
        inst = by_sym.get(s)
        if inst is not None:
            picked.append(inst)
        elif allow_synthetic:
            picked.append(Instrument("NSE", s, s, s, "EQ"))
        else:
            missing.append(s)
    return picked, missing


# -- Abstract Provider ------------------------------------------------------------------------
class ScreenerDataProvider(abc.ABC):
    batch_size: int = 1
    provider_name: str = "generic"

    def __init__(self, clock: Optional[Clock] = None):
        self.clock = clock or Clock()
        self.calls_count: int = 0

    def slow_down(self, why: str) -> None:
        return None

    @abc.abstractmethod
    def load_instruments(self, today: dt.date) -> List[Instrument]:
        """Return the NSE EQ instrument catalog for this provider."""

    @abc.abstractmethod
    def fetch_batch(
        self,
        tf: Timeframe,
        batch: Sequence[Instrument],
        lookback: int,
        now: dt.datetime,
        include_forming: str = "off",
    ) -> Tuple[Dict[str, List[Candle]], Dict[str, str]]:
        """Fetch up to `lookback` candles per instrument in `batch` (oldest first).
        Returns ({inst.code: [Candle, ...]}, {inst.code: error_message}).
        """


# -- 1. INDstocks Native 5-Stock Batch Provider -----------------------------------------------
def _num(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


class IndMoneyBatchProvider(ScreenerDataProvider):
    """Direct read-only INDstocks client with 5-stock batching, adaptive rate limiting,
    false-auth recheck via /user/profile, and HTTP 400 per-code bisection.
    Matches stock_screener/src/services/api_client.py + data/candles.py exactly.
    """

    RETRY_STATUS = {429, 500, 502, 503, 504}
    batch_size = HIST_MAX_CODES
    provider_name = "indmoney_batch"

    def __init__(
        self,
        token_provider: Callable[[], Optional[str]],
        session: Optional[requests.Session] = None,
        clock: Optional[Clock] = None,
        data_rps: float = 4.0,
        max_retries: int = 4,
        timeout: Tuple[float, float] = (5.0, 30.0),
        recheck_wait: float = 10.0,
    ):
        super().__init__(clock)
        self.token_provider = token_provider
        self.session = session or requests.Session()
        self.limiters = {
            "data": RateLimiter(min(data_rps, 5.0), self.clock),
            "other": RateLimiter(10.0, self.clock),
        }
        self.max_retries = max_retries
        self.timeout = timeout
        self.recheck_wait = recheck_wait
        self.max_false_auth = 3
        self.false_auth = 0
        self.min_rps = 2.0
        self.slowdowns = 0
        self._rechecking = False

    @property
    def calls_today(self) -> int:
        return sum(lim.count for lim in self.limiters.values())

    def _sync_calls(self) -> None:
        self.calls_count = self.calls_today

    def _token(self) -> str:
        tok = self.token_provider()
        if not tok:
            raise ScreenerAuthError("INDMoney broker session is not active. Please log in again.")
        return tok

    def slow_down(self, why: str) -> None:
        lim = self.limiters["data"]
        new = max(self.min_rps, lim.per_second - 0.5)
        if new < lim.per_second:
            lim.set_rate(new)
            self.slowdowns += 1
            logger.warning("Screener slowing INDstocks requests to %.1f/s (%s)", new, why)

    def _token_recheck(self, path: str, msg: str, in_a_row: int) -> bool:
        if self._rechecking or in_a_row >= self.max_false_auth:
            return False
        self._rechecking = True
        try:
            logger.warning("%s rejected token (%s); re-checking via /user/profile", path, msg)
            self.clock.sleep(self.recheck_wait)
            try:
                self.profile()
            except ScreenerApiError:
                return False
            self.false_auth += 1
            self.slow_down("false token rejection")
            return True
        finally:
            self._rechecking = False

    def _backoff(self, attempt: int, why: str, retry_after: Optional[float]) -> None:
        delay = min(20.0, 0.5 * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
        if retry_after:
            delay = max(delay, min(retry_after, 60.0))
        logger.warning("Screener retrying in %.1fs (%s, attempt %d)", delay, why, attempt)
        self.clock.sleep(delay)

    def request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        category: str = "data",
        want: str = "json",
        timeout: Optional[Tuple[float, float]] = None,
    ) -> Any:
        attempt = 0
        false_here = 0
        while True:
            self.clock.check()
            attempt += 1
            token = self._token()
            self.limiters[category].acquire()
            self._sync_calls()
            try:
                resp = self.session.request(
                    "GET",
                    INDSTOCKS_BASE_URL + path,
                    params=params,
                    headers={"Authorization": token, "Accept": "application/json"},
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as e:
                if attempt > self.max_retries:
                    raise ScreenerNetworkError(
                        f"Network problem reaching INDstocks ({type(e).__name__}).",
                        retryable=True,
                    )
                self._backoff(attempt, f"network error {type(e).__name__}", None)
                continue

            status = resp.status_code
            body: Any = None
            if want == "json" or status != 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = None

            if status == 200:
                if want == "text":
                    return resp.text
                if body is None:
                    raise ScreenerApiError(f"{path}: response was not valid JSON")
                if isinstance(body, dict) and (
                    body.get("success") is False
                    or str(body.get("status", "")).lower() == "error"
                ):
                    etype, msg = describe_error(status, body, "")
                    if etype == "TokenException":
                        if self._token_recheck(path, msg, false_here):
                            false_here += 1
                            continue
                        raise ScreenerAuthError(f"Token rejected: {msg}", status, etype)
                    raise ScreenerApiError(f"{path}: {msg}", status, etype)
                return body

            etype, msg = describe_error(status, body, getattr(resp, "text", ""))
            if status in (401, 403) or etype == "TokenException":
                if self._token_recheck(path, msg, false_here):
                    false_here += 1
                    continue
                raise ScreenerAuthError(
                    f"Broker token rejected ({msg}). Re-authenticate with INDMoney in OpenAlgo.",
                    status,
                    etype,
                )
            if status == 429:
                self.slow_down("HTTP 429")
            if status in self.RETRY_STATUS and attempt <= self.max_retries:
                ra = _num(resp.headers.get("Retry-After")) if getattr(resp, "headers", None) else None
                self._backoff(attempt, f"HTTP {status}", ra)
                continue
            if status == 429:
                raise ScreenerApiError(
                    "Rate limit hit repeatedly (HTTP 429).",
                    status,
                    etype,
                    retryable=True,
                )
            raise ScreenerApiError(
                f"{path}: HTTP {status} {msg}".strip(),
                status,
                etype,
                retryable=status in self.RETRY_STATUS,
            )

    def profile(self) -> Dict[str, Any]:
        body = self.request("/user/profile", category="other")
        return body.get("data", body) if isinstance(body, dict) else {}

    def instruments_csv(self, source: str = "equity") -> str:
        return self.request(
            "/market/instruments",
            {"source": source},
            "data",
            want="text",
            timeout=(5.0, 120.0),
        )

    def historical(
        self, interval: str, codes: Sequence[str], start_ms: int, end_ms: int
    ) -> Dict[str, List[Candle]]:
        if not 0 < len(codes) <= HIST_MAX_CODES:
            raise ValueError("historical takes 1..5 scrip codes")
        body = self.request(
            f"/market/historical/{interval}",
            {
                "scrip-codes": ",".join(codes),
                "start_time": int(start_ms),
                "end_time": int(end_ms),
            },
        )
        data = body.get("data") or {}
        if not isinstance(data, dict):
            return {}
        return {
            code: parse_candles((payload or {}).get("candles"))
            for code, payload in data.items()
        }

    def historical_resilient(
        self, interval: str, codes: Sequence[str], start_ms: int, end_ms: int
    ) -> Tuple[Dict[str, List[Candle]], Dict[str, str]]:
        try:
            return self.historical(interval, codes, start_ms, end_ms), {}
        except (ScreenerAuthError, ScreenerCancelled):
            raise
        except ScreenerApiError as e:
            if e.status != 400 or len(codes) == 1:
                if len(codes) == 1:
                    return {}, {codes[0]: str(e)}
                raise
        out: Dict[str, List[Candle]] = {}
        errs: Dict[str, str] = {}
        for code in codes:
            try:
                out.update(self.historical(interval, [code], start_ms, end_ms))
            except (ScreenerAuthError, ScreenerCancelled):
                raise
            except ScreenerApiError as e2:
                errs[code] = str(e2)
        return out, errs

    def load_instruments(self, today: dt.date) -> List[Instrument]:
        text = load_cached_master(today)
        if text is None:
            text = self.instruments_csv("equity")
            save_master(today, text)
        inst = parse_indstocks_instruments(text)
        if not inst:
            raise ScreenerApiError("The instrument list from INDstocks was empty or unreadable.")
        return inst

    def fetch_batch(
        self,
        tf: Timeframe,
        batch: Sequence[Instrument],
        lookback: int,
        now: dt.datetime,
        include_forming: str = "off",
    ) -> Tuple[Dict[str, List[Candle]], Dict[str, str]]:
        mode = forming_mode(include_forming)
        codes = [i.code for i in batch]
        merged: Dict[str, Dict[int, Candle]] = {c: {} for c in codes}
        errors: Dict[str, str] = {}
        exhausted: set = set()
        need = lookback + 1
        slack_s = HISTORY_START_SLACK_DAYS.get(tf.label, 4) * 86_400

        for start_ms, end_ms in windows(tf, lookback, now):
            pending = [
                c
                for c in codes
                if len(merged[c]) < need and c not in errors and c not in exhausted
            ]
            if not pending:
                break
            data, errs = self.historical_resilient(
                tf.indstocks_interval, pending, start_ms, end_ms
            )
            errors.update(errs)
            for code in pending:
                cs = data.get(code) or []
                for c in cs:
                    merged[code][c.ts] = c
                if not cs or cs[0].ts > start_ms / 1000 + slack_s:
                    exhausted.add(code)

        out: Dict[str, List[Candle]] = {}
        for code in codes:
            cs = [merged[code][k] for k in sorted(merged[code])]
            cs = completed(tf, cs, now, mode)
            out[code] = cs[-lookback:]
        self._sync_calls()
        return out, errors


# -- 2. Universal OpenAlgo Broker Provider ----------------------------------------------------
class OpenAlgoBrokerProvider(ScreenerDataProvider):
    """Uses OpenAlgo's broker-agnostic services.history_service.get_history_with_auth."""

    batch_size = 1
    provider_name = "openalgo_broker"

    def __init__(
        self,
        auth_token: str,
        feed_token: Optional[str],
        broker: str,
        clock: Optional[Clock] = None,
    ):
        super().__init__(clock)
        self.auth_token = auth_token
        self.feed_token = feed_token
        self.broker = broker

    def load_instruments(self, today: dt.date) -> List[Instrument]:
        inst = load_symtoken_nse_eq()
        if not inst:
            raise ScreenerApiError(
                "No NSE EQ symbols found in OpenAlgo master contract database. "
                "Log in to your broker first so master contracts download."
            )
        return inst

    def fetch_batch(
        self,
        tf: Timeframe,
        batch: Sequence[Instrument],
        lookback: int,
        now: dt.datetime,
        include_forming: str = "off",
    ) -> Tuple[Dict[str, List[Candle]], Dict[str, str]]:
        from services.history_service import _enforce_rate_limit, get_history_with_auth

        mode = forming_mode(include_forming)
        days = calendar_days_needed(tf, lookback)
        start_date = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        cutoff_ts = int(now.timestamp())

        out: Dict[str, List[Candle]] = {}
        errs: Dict[str, str] = {}
        for inst in batch:
            self.clock.check()
            _enforce_rate_limit(background=True)
            self.calls_count += 1
            ok, payload, status_code = get_history_with_auth(
                auth_token=self.auth_token,
                feed_token=self.feed_token,
                broker=self.broker,
                symbol=inst.symbol,
                exchange=inst.exch,
                interval=tf.openalgo_interval,
                start_date=start_date,
                end_date=end_date,
            )
            if not ok:
                msg = str(payload.get("message") or f"HTTP {status_code}")
                if status_code in (401, 403):
                    raise ScreenerAuthError(msg, status_code)
                errs[inst.code] = msg
                continue
            cs = parse_candles(payload.get("data"))
            cs = [c for c in cs if c.ts <= cutoff_ts]
            cs = completed(tf, cs, now, mode)
            out[inst.code] = cs[-lookback:]
        return out, errs


# -- 3. Historify Local DuckDB Provider -------------------------------------------------------
class HistorifyDbProvider(ScreenerDataProvider):
    """Offline local scans against OpenAlgo's Historify DuckDB database."""

    batch_size = 5
    provider_name = "historify_db"

    def load_instruments(self, today: dt.date) -> List[Instrument]:
        from database.historify_db import get_available_symbols

        avail = get_available_symbols()
        nse_syms = sorted(
            {
                str(r["symbol"]).strip().upper()
                for r in avail
                if str(r.get("exchange", "")).strip().upper() == "NSE"
                and r.get("symbol")
            }
        )
        if nse_syms:
            return [Instrument("NSE", s, s, s, "EQ") for s in nse_syms]
        # Fall back to SymToken so custom/nifty50 lookups can report clear messages
        try:
            return load_symtoken_nse_eq()
        except Exception:
            return []

    def fetch_batch(
        self,
        tf: Timeframe,
        batch: Sequence[Instrument],
        lookback: int,
        now: dt.datetime,
        include_forming: str = "off",
    ) -> Tuple[Dict[str, List[Candle]], Dict[str, str]]:
        from services.history_service import get_history_from_db

        mode = forming_mode(include_forming)
        days = calendar_days_needed(tf, lookback)
        start_date = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        cutoff_ts = int(now.timestamp())

        out: Dict[str, List[Candle]] = {}
        errs: Dict[str, str] = {}
        for inst in batch:
            self.clock.check()
            self.calls_count += 1
            ok, payload, status_code = get_history_from_db(
                symbol=inst.symbol,
                exchange=inst.exch,
                interval=tf.openalgo_interval,
                start_date=start_date,
                end_date=end_date,
            )
            if not ok:
                errs[inst.code] = str(payload.get("message") or f"No local data ({status_code})")
                continue
            cs = parse_candles(payload.get("data"))
            cs = [c for c in cs if c.ts <= cutoff_ts]
            cs = completed(tf, cs, now, mode)
            out[inst.code] = cs[-lookback:]
        return out, errs


def create_provider(
    source: str,
    *,
    broker: Optional[str] = None,
    auth_token: Optional[str] = None,
    feed_token: Optional[str] = None,
    clock: Optional[Clock] = None,
) -> ScreenerDataProvider:
    if source == "db":
        return HistorifyDbProvider(clock=clock)
    if not auth_token or not broker:
        raise ScreenerAuthError(
            "Broker API requires an active broker login. Log in to your broker or switch source to Historify DB."
        )
    if str(broker).strip().lower() == "indmoney":
        return IndMoneyBatchProvider(lambda: auth_token, clock=clock)
    return OpenAlgoBrokerProvider(auth_token, feed_token, broker, clock=clock)
