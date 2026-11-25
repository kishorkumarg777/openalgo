"""Unit tests for the local Order Flow SQLite tick recorder and query service."""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database.orderflow_tick_db import OrderFlowTickRecorder
from services.orderflow_service import get_orderflow_ticks


def _make_recorder(tmp_path, **kwargs):
    return OrderFlowTickRecorder(
        db_path=str(tmp_path / "orderflow_test.db"),
        start_thread=False,
        **kwargs,
    )


def test_quote_rule_classification_and_volume_deltas(tmp_path):
    rec = _make_recorder(tmp_path)
    base_ts = int(time.time()) - 120
    try:
        # First snapshot sets baseline volume (no trade emitted).
        assert (
            rec.record_market_data(
                "HBLENGINE",
                "NSE",
                2,
                {"ltp": 650.0, "volume": 10_000, "bid_price": 649.95, "ask_price": 650.05, "ltt": base_ts},
                now_sec=base_ts,
            )
            is False
        )
        # Trade at ask -> aggressive buy (side=1).
        assert (
            rec.record_market_data(
                "HBLENGINE",
                "NSE",
                2,
                {"ltp": 650.05, "volume": 10_120, "bid_price": 649.95, "ask_price": 650.05, "ltt": base_ts + 1},
                now_sec=base_ts + 1,
            )
            is True
        )
        # Duplicate volume refresh -> ignored (no double counting).
        assert (
            rec.record_market_data(
                "HBLENGINE",
                "NSE",
                2,
                {"ltp": 650.05, "volume": 10_120, "bid_price": 649.95, "ask_price": 650.05, "ltt": base_ts + 2},
                now_sec=base_ts + 2,
            )
            is False
        )
        # Trade at bid -> aggressive sell (side=0).
        assert (
            rec.record_market_data(
                "HBLENGINE",
                "NSE",
                2,
                {"ltp": 649.95, "volume": 10_200, "bid_price": 649.95, "ask_price": 650.05, "ltt": base_ts + 3},
                now_sec=base_ts + 3,
            )
            is True
        )
        # Trade inside spread (650.00 > prev 649.95) -> falls back to tick rule -> buy (side=1).
        assert (
            rec.record_market_data(
                "HBLENGINE",
                "NSE",
                2,
                {"ltp": 650.00, "volume": 10_250, "bid_price": 649.95, "ask_price": 650.05, "ltt": base_ts + 4},
                now_sec=base_ts + 4,
            )
            is True
        )

        ticks = rec.query_ticks("hblengine", "nse")
        assert len(ticks) == 3
        assert ticks[0] == [float(base_ts + 1), 650.05, 120, 1]
        assert ticks[1] == [float(base_ts + 3), 649.95, 80, 0]
        assert ticks[2] == [float(base_ts + 4), 650.00, 50, 1]
    finally:
        rec.close()


def test_depth_payload_book_extraction_and_ltq_fallback(tmp_path):
    rec = _make_recorder(tmp_path)
    base_ts = int(time.time()) - 60
    try:
        # Depth payload with no cumulative volume uses ltq + top-of-book depth prices.
        assert (
            rec.record_market_data(
                "CUPID",
                "NSE",
                3,
                {
                    "ltp": 360.50,
                    "ltq": 45,
                    "depth": {"buy": [{"price": 360.50, "quantity": 100}], "sell": [{"price": 360.60, "quantity": 200}]},
                    "timestamp": (base_ts + 1) * 1000,
                },
                now_sec=base_ts + 1,
            )
            is True
        )
        ticks = rec.query_ticks("CUPID", "NSE")
        assert len(ticks) == 1
        assert ticks[0][1:] == [360.50, 45, 0]
    finally:
        rec.close()


def test_query_window_limit_and_retention_pruning(tmp_path):
    rec = _make_recorder(tmp_path, retention_days=2)
    now = int(time.time())
    try:
        # Inject one stale tick (5 days old) and 5 recent ticks.
        old_ms = (now - 5 * 86400) * 1000
        with rec._db_lock:
            conn = rec._get_conn()
            conn.execute(
                "INSERT INTO orderflow_ticks (exchange, symbol, ts_ms, price, qty, side, bid, ask) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("NSE", "HBLENGINE", old_ms, 600.0, 10, "ask", 599.95, 600.05),
            )
            conn.commit()

        for i in range(5):
            rec.record_market_data(
                "HBLENGINE",
                "NSE",
                2,
                {"ltp": 650.0 + i * 0.05, "ltq": 10 + i, "ltt": now - 10 + i},
                now_sec=now - 10 + i,
            )
        rec.flush()
        # Flush auto-pruned the stale 5-day-old row; only the 5 recent ticks remain.
        all_ticks = rec.query_ticks("HBLENGINE", "NSE")
        assert len(all_ticks) == 5
        assert all(t[0] >= now - 10 for t in all_ticks)

        # Explicit forced prune removes a freshly injected stale row.
        with rec._db_lock:
            conn = rec._get_conn()
            conn.execute(
                "INSERT INTO orderflow_ticks (exchange, symbol, ts_ms, price, qty, side, bid, ask) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("NSE", "HBLENGINE", old_ms, 600.0, 10, "ask", 599.95, 600.05),
            )
            conn.commit()
        assert rec._maybe_prune(force=True, now_sec=now) == 1

        # Limit takes the newest N in chronological order.
        newest_two = rec.query_ticks("HBLENGINE", "NSE", limit=2)
        assert len(newest_two) == 2
        assert newest_two[0][2] == 13
        assert newest_two[1][2] == 14

        # Time window filter works inclusively.
        windowed = rec.query_ticks("HBLENGINE", "NSE", start_time=now - 8, end_time=now - 7)
        assert [t[2] for t in windowed] == [12, 13]
    finally:
        rec.close()


def test_orderflow_service_auth_and_response_shape(tmp_path):
    rec = _make_recorder(tmp_path)
    now = int(time.time()) - 20
    try:
        rec.record_market_data("HBLENGINE", "NSE", 2, {"ltp": 650.0, "ltq": 25, "ltt": now}, now_sec=now)
        rec.flush()

        ok, body, code = get_orderflow_ticks("", "HBLENGINE", "NSE")
        assert (ok, code) == (False, 401)

        with patch("services.orderflow_service.verify_api_key", return_value=None):
            ok, body, code = get_orderflow_ticks("bad-key", "HBLENGINE", "NSE")
            assert (ok, code) == (False, 403)

        with (
            patch("services.orderflow_service.verify_api_key", return_value="user-1"),
            patch("services.orderflow_service.get_orderflow_recorder", return_value=rec),
        ):
            ok, body, code = get_orderflow_ticks("valid-key", "hblengine", "nse")
            assert ok is True
            assert code == 200
            assert body["status"] == "success"
            assert body["data"]["symbol"] == "HBLENGINE"
            assert body["data"]["count"] == 1
            assert body["data"]["recording"] is True
            assert body["data"]["ticks"][0][1:] == [650.0, 25, 1]
    finally:
        rec.close()


def test_indmoney_quote_mode_resolves_ltp_from_close_and_drops_zero_ltp(tmp_path):
    """INDstocks WebSocket quote packets carry live price in `close` (no `ltp` key).
    Adapter must populate `ltp` from `close`, never publish 0.00 LTP, and tick recorder
    must capture real volume deltas cleanly."""
    import websocket_proxy  # noqa: F401  (avoid circular import order)
    from broker.indmoney.streaming.indmoney_adapter import IndmoneyWebSocketAdapter

    adapter = IndmoneyWebSocketAdapter()
    published = []
    adapter.publish_market_data = lambda topic, data: published.append((topic, data))
    adapter.subscriptions = {
        "ANTELOPUS_NSE_2": {
            "symbol": "ANTELOPUS",
            "exchange": "NSE",
            "token": "13598",
            "instrument_token": "NSE:13598",
            "mode": 2,
        }
    }

    # 1. Realistic INDstocks quote frame (live price in `close`, no `ltp` key)
    adapter._on_data(
        None,
        {
            "mode": "quote",
            "instrument": "13598",
            "timestamp": 1795586846420,
            "data": {"open": 815, "high": 826, "low": 778.3, "close": 781.25, "volume": 68564},
        },
    )
    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "NSE_ANTELOPUS_QUOTE"
    assert payload["ltp"] == 781.25
    assert payload["volume"] == 68564

    # 2. Second quote tick with updated live price & incremented volume
    adapter._on_data(
        None,
        {
            "mode": "quote",
            "instrument": "13598",
            "timestamp": 1795586847821,
            "data": {"open": 815, "high": 826, "low": 778.3, "close": 781.50, "volume": 68814},
        },
    )
    assert len(published) == 2
    assert published[1][1]["ltp"] == 781.50

    # 3. Recorder captures exact 250-share aggressive buy print from the two frames
    rec = _make_recorder(tmp_path)
    now = int(time.time()) - 10
    try:
        assert rec.record_market_data("ANTELOPUS", "NSE", 2, published[0][1], now_sec=now) is False
        assert rec.record_market_data("ANTELOPUS", "NSE", 2, published[1][1], now_sec=now + 1) is True
        ticks = rec.query_ticks("ANTELOPUS", "NSE")
        assert len(ticks) == 1
        assert ticks[0][1:] == [781.50, 250, 1]
    finally:
        rec.close()

    # 4. Fresh symbol with zero/empty quote must be dropped (never publish ltp=0)
    adapter.subscriptions["ZERO_NSE_2"] = {
        "symbol": "ZERO",
        "exchange": "NSE",
        "token": "99999",
        "instrument_token": "NSE:99999",
        "mode": 2,
    }
    before = len(published)
    adapter._on_data(
        None,
        {
            "mode": "quote",
            "instrument": "99999",
            "timestamp": 1795586849000,
            "data": {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0},
        },
    )
    assert len(published) == before


def test_indmoney_split_date_range_includes_same_day_and_exact_boundaries():
    """Same-day intraday history refreshes (start == end) and multi-chunk ranges
    must anchor backward from end_date so the newest chunk never collapses into a
    1-day tail sliver on today."""
    from broker.indmoney.api.data import BrokerData

    bd = BrokerData("dummy-token")
    assert bd._split_date_range("2026-11-25", "2026-11-25", 7) == [("2026-11-25", "2026-11-25")]
    assert bd._split_date_range("2026-11-01", "2026-11-06", 5) == [
        ("2026-11-01", "2026-11-01"),
        ("2026-11-02", "2026-11-06"),
    ]
    assert bd._split_date_range("2026-11-24", "2026-11-25", 30) == [("2026-11-24", "2026-11-25")]
    # 3-year daily lookback fits cleanly in 3 full chunks ending today (max_days=366)
    chunks = bd._split_date_range("2023-11-26", "2026-11-25", 366)
    assert len(chunks) == 3
    assert chunks[0][0] == "2023-11-26"
    assert chunks[-1] == ("2025-11-25", "2026-11-25")


def test_indmoney_short_daily_window_pads_fetch_and_filters_to_requested_range():
    """Short 1day windows (1-3 days) pad backward to >=10 days when querying
    INDstocks so today's daily candle is not dropped, then filter strictly back
    to the caller's [start_date, end_date]."""
    from datetime import datetime
    from broker.indmoney.api.data import BrokerData

    bd = BrokerData("dummy-token")
    captured = []

    def fake_api(endpoint, auth, method="GET", params=None):
        captured.append((endpoint, dict(params or {})))
        # Simulate INDstocks returning candles when window >= 10 days
        ts_nov20 = int(datetime(2026, 11, 20, 5, 30).timestamp())
        ts_nov23 = int(datetime(2026, 11, 23, 5, 30).timestamp())
        ts_nov25 = int(datetime(2026, 11, 25, 5, 30).timestamp())
        return {
            "data": {
                "NSE_6994": {
                    "candles": [
                        {"ts": ts_nov20, "o": 295.5, "h": 311.8, "l": 293.8, "c": 303.15, "v": 1000},
                        {"ts": ts_nov23, "o": 307.75, "h": 319.25, "l": 302.1, "c": 304.7, "v": 2000},
                        {"ts": ts_nov25, "o": 309.75, "h": 311.1, "l": 296.6, "c": 299.25, "v": 3000},
                    ]
                }
            }
        }

    with (
        patch.object(bd, "_get_scrip_code", return_value="NSE_6994"),
        patch("broker.indmoney.api.data.get_api_response", side_effect=fake_api),
    ):
        df = bd.get_history("BSOFT", "NSE", "D", "2026-11-23", "2026-11-25")

    assert len(captured) == 1
    assert captured[0][0] == "/market/historical/1day"
    # Padded start_time should reach back to 2026-11-16 (10-day span ending Nov 25)
    expected_start = str(bd._date_to_timestamp_ms("2026-11-16"))
    assert captured[0][1]["start_time"] == expected_start
    # Returned DataFrame is filtered strictly to Nov 23..Nov 25 (excludes Nov 20)
    assert len(df) == 2
    assert list(df["close"]) == [304.7, 299.25]


