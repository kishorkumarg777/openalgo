"""
Local SQLite tick recorder for institutional-grade Order Flow / Footprint charts.

Captures live market ticks flowing through the OpenAlgo WebSocket proxy
(Quote and Depth feeds across all brokers), extracts traded quantity from
cumulative day-volume deltas or last-traded quantity, classifies aggressive
buy ('ask') vs sell ('bid') side using top-of-book bid/ask quotes (falling
back to the tick rule inside the spread or when quotes are absent), and
persists classified prints to a WAL-mode SQLite database on a non-blocking
background flusher thread.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_DB_PATH = os.path.join("db", "orderflow_ticks.db")
_FLUSH_INTERVAL_SEC = 0.25
_FLUSH_BATCH_SIZE = 250
_MAX_QUEUE_SIZE = 100_000
_PRUNE_INTERVAL_SEC = 3600.0
_MAX_QUERY_LIMIT = 150_000


def _env_enabled() -> bool:
    raw = os.getenv("ORDERFLOW_RECORDING_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _env_retention_days() -> int:
    try:
        days = int(os.getenv("ORDERFLOW_RETENTION_DAYS", "7"))
        return max(1, min(90, days))
    except (TypeError, ValueError):
        return 7


def _positive_float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _normalize_ts_ms(raw: Any, now_sec: float | None = None) -> int:
    """Normalize epoch s / ms / us timestamps to integer UTC milliseconds."""
    now = time.time() if now_sec is None else now_sec
    fallback = int(now * 1000)
    if raw is None or raw == "":
        return fallback
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(val) or val <= 0:
        return fallback
    if val > 1e14:
        ts_ms = int(val / 1000.0)
    elif val > 1e11:
        ts_ms = int(val)
    else:
        ts_ms = int(val * 1000.0)
    # Guard against broker zero/epoch-offset clocks (more than 30 days away from now).
    if abs(ts_ms / 1000.0 - now) > 86400 * 30:
        return fallback
    return ts_ms


def _extract_quote_book(data: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract top-of-book (bid, ask) from Quote or Depth payloads."""
    bid = _positive_float(data.get("bid_price") or data.get("bid"))
    ask = _positive_float(data.get("ask_price") or data.get("ask"))
    if bid is None or ask is None:
        depth = data.get("depth")
        if isinstance(depth, dict):
            buys = depth.get("buy") or depth.get("bids")
            sells = depth.get("sell") or depth.get("asks")
            if bid is None and isinstance(buys, list) and buys and isinstance(buys[0], dict):
                bid = _positive_float(buys[0].get("price"))
            if ask is None and isinstance(sells, list) and sells and isinstance(sells[0], dict):
                ask = _positive_float(sells[0].get("price"))
    return bid, ask


@dataclass
class _SymbolState:
    prev_volume: float | None = None
    prev_price: float | None = None
    prev_side: str = "ask"
    bid: float | None = None
    ask: float | None = None
    last_ts_ms: int = 0


class OrderFlowTickRecorder:
    """Non-blocking, WAL-mode SQLite recorder for live classified order-flow prints."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        enabled: bool | None = None,
        retention_days: int | None = None,
        flush_interval_sec: float = _FLUSH_INTERVAL_SEC,
        start_thread: bool = True,
    ) -> None:
        self.db_path = db_path or os.getenv("ORDERFLOW_DB_PATH", _DEFAULT_DB_PATH)
        self.enabled = _env_enabled() if enabled is None else enabled
        self.retention_days = _env_retention_days() if retention_days is None else max(1, retention_days)
        self.flush_interval_sec = max(0.05, float(flush_interval_sec))

        self._state_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._states: dict[tuple[str, str], _SymbolState] = {}
        self._buffer: deque[tuple[str, str, int, float, int, str, float | None, float | None]] = deque(
            maxlen=_MAX_QUEUE_SIZE
        )
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_prune = 0.0
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None

        self._ensure_schema()
        if start_thread and self.enabled:
            self._thread = threading.Thread(
                target=self._flush_loop,
                name="orderflow-tick-flusher",
                daemon=True,
            )
            self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        with self._db_lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orderflow_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    bid REAL,
                    ask REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orderflow_sym_ts "
                "ON orderflow_ticks(exchange, symbol, ts_ms)"
            )
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def record_market_data(
        self,
        symbol: str,
        exchange: str,
        mode: int,
        data: dict[str, Any],
        *,
        now_sec: float | None = None,
    ) -> bool:
        """
        Inspect one live broker tick and enqueue a classified trade print when
        real traded quantity occurred. Returns True if a trade was enqueued.
        """
        if not self.enabled or not symbol or not exchange or not isinstance(data, dict):
            return False

        ltp = _positive_float(data.get("ltp") or data.get("last_price"))
        if ltp is None:
            return False

        sym_u = symbol.strip().upper()
        exch_u = exchange.strip().upper()
        key = (exch_u, sym_u)

        bid, ask = _extract_quote_book(data)
        volume = _positive_float(data.get("volume") or data.get("total_volume"))
        ltq = _positive_float(data.get("ltq") or data.get("last_trade_quantity") or data.get("last_quantity"))
        ts_ms = _normalize_ts_ms(data.get("ltt") or data.get("timestamp"), now_sec=now_sec)

        with self._state_lock:
            st = self._states.get(key)
            if st is None:
                st = _SymbolState()
                self._states[key] = st

            if bid is not None:
                st.bid = bid
            if ask is not None:
                st.ask = ask

            # Compute traded quantity: cumulative day-volume delta wins when available
            # because it captures all trades coalesced between broker WS snapshots.
            qty_raw = 0.0
            if volume is not None:
                prev_vol = st.prev_volume
                st.prev_volume = volume
                if prev_vol is None:
                    # First snapshot of the session sets the baseline; never record
                    # whole-day cumulative volume as a single print.
                    st.prev_price = ltp
                    return False
                if volume < prev_vol:
                    # New trading day reset.
                    qty_raw = ltq or 0.0
                else:
                    qty_raw = volume - prev_vol
            elif ltq is not None:
                qty_raw = ltq

            qty = int(round(qty_raw))
            if qty <= 0:
                # Quote/price refresh without a trade: keep price/book context updated.
                st.prev_price = ltp
                return False

            # Quote-rule classification when top-of-book is valid (0 < bid < ask),
            # falling back to tick rule inside the spread or when book is absent.
            eff_bid = bid if bid is not None else st.bid
            eff_ask = ask if ask is not None else st.ask
            if eff_bid is not None and eff_ask is not None and 0 < eff_bid < eff_ask:
                if ltp >= eff_ask:
                    side = "ask"
                elif ltp <= eff_bid:
                    side = "bid"
                else:
                    side = self._tick_rule(ltp, st.prev_price, st.prev_side)
            else:
                side = self._tick_rule(ltp, st.prev_price, st.prev_side)

            st.prev_price = ltp
            st.prev_side = side
            if ts_ms < st.last_ts_ms:
                ts_ms = st.last_ts_ms
            st.last_ts_ms = ts_ms

            self._buffer.append((exch_u, sym_u, ts_ms, ltp, qty, side, eff_bid, eff_ask))
            should_wake = len(self._buffer) >= _FLUSH_BATCH_SIZE

        if should_wake:
            self._wake.set()
        return True

    @staticmethod
    def _tick_rule(price: float, prev_price: float | None, prev_side: str) -> str:
        if prev_price is None:
            return "ask"
        if price > prev_price:
            return "ask"
        if price < prev_price:
            return "bid"
        return prev_side

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.flush_interval_sec)
            self._wake.clear()
            try:
                self.flush()
            except Exception as exc:
                logger.debug(f"[OrderFlowRecorder] Background flush error: {exc}")

    def flush(self) -> int:
        """Write all buffered ticks to SQLite in a single transaction."""
        batch: list[tuple[str, str, int, float, int, str, float | None, float | None]] = []
        with self._state_lock:
            while self._buffer:
                batch.append(self._buffer.popleft())

        if not batch:
            self._maybe_prune()
            return 0

        with self._db_lock:
            conn = self._get_conn()
            conn.executemany(
                "INSERT INTO orderflow_ticks "
                "(exchange, symbol, ts_ms, price, qty, side, bid, ask) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            conn.commit()

        self._maybe_prune()
        return len(batch)

    def _maybe_prune(self, *, force: bool = False, now_sec: float | None = None) -> int:
        now = time.time() if now_sec is None else now_sec
        if not force and (now - self._last_prune) < _PRUNE_INTERVAL_SEC:
            return 0
        self._last_prune = now
        cutoff_ms = int((now - self.retention_days * 86400) * 1000)
        try:
            with self._db_lock:
                conn = self._get_conn()
                cur = conn.execute("DELETE FROM orderflow_ticks WHERE ts_ms < ?", (cutoff_ms,))
                deleted = cur.rowcount or 0
                if deleted > 0:
                    conn.commit()
                return deleted
        except Exception as exc:
            logger.debug(f"[OrderFlowRecorder] Prune error: {exc}")
            return 0

    def query_ticks(
        self,
        symbol: str,
        exchange: str,
        *,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 100_000,
    ) -> list[list[Any]]:
        """
        Return recorded classified ticks as compact chronological tuples:
        [time_sec, price, qty, side_code] where side_code is 1 (ask/buy) or 0 (bid/sell).
        """
        self.flush()
        sym_u = symbol.strip().upper()
        exch_u = exchange.strip().upper()
        capped_limit = max(1, min(_MAX_QUERY_LIMIT, int(limit)))

        clauses = ["exchange = ?", "symbol = ?"]
        params: list[Any] = [exch_u, sym_u]
        if start_time is not None and math.isfinite(start_time):
            clauses.append("ts_ms >= ?")
            params.append(int(start_time * 1000))
        if end_time is not None and math.isfinite(end_time):
            clauses.append("ts_ms <= ?")
            params.append(int(end_time * 1000))

        # Fetch the newest `capped_limit` ticks in range, then return oldest-first.
        sql = (
            "SELECT ts_ms, price, qty, side FROM orderflow_ticks "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ts_ms DESC, id DESC LIMIT ?"
        )
        params.append(capped_limit)

        with self._db_lock:
            conn = self._get_conn()
            rows = conn.execute(sql, params).fetchall()

        rows.reverse()
        return [
            [round(ts_ms / 1000.0, 3), float(price), int(qty), 1 if side == "ask" else 0]
            for ts_ms, price, qty, side in rows
        ]

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self.flush()
        except Exception:
            pass
        with self._db_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


_recorder_singleton: OrderFlowTickRecorder | None = None
_singleton_lock = threading.Lock()


def get_orderflow_recorder() -> OrderFlowTickRecorder:
    """Return the process-wide OrderFlowTickRecorder singleton."""
    global _recorder_singleton
    if _recorder_singleton is None:
        with _singleton_lock:
            if _recorder_singleton is None:
                _recorder_singleton = OrderFlowTickRecorder()
    return _recorder_singleton
