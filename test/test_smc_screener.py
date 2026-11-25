"""Comprehensive unit, integration, and differential-parity tests for OpenAlgo's SMC Stock Screener."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

# Ensure openalgo root is importable
OPENALGO_ROOT = Path(__file__).resolve().parents[1]
if str(OPENALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(OPENALGO_ROOT))

from services.screener import candles as sc_candles
from services.screener import data_provider as sc_dp
from services.screener import engine as sc_engine
from services.screener import filters as sc_filters
from services.screener import indicators as sc_ind
from services.screener import liquidity as sc_liq
from services.screener import mtf as sc_mtf
from services.screener import registry as sc_reg
from services.screener import smc_breaker as sc_smc
from services.screener.models import Candle, ScanContext
from services.screener.timeutil import IST

START_DATE = dt.date(2025, 11, 3)


def day_ts(k: int) -> int:
    d = START_DATE
    n = 0
    while n < k:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return int(dt.datetime.combine(d, dt.time(9, 15), IST).timestamp())


def mk_daily(rows: Sequence[Tuple[float, float, float, float]], vol: float = 100_000.0) -> List[Candle]:
    return [Candle(day_ts(k), o, h, l, c, vol) for k, (o, h, l, c) in enumerate(rows)]


def mk_hourly(rows: Sequence[Tuple[float, float, float, float]], vol: float = 100_000.0) -> List[Candle]:
    return [
        Candle(day_ts(k // 7) + (k % 7) * 3600, o, h, l, c, vol)
        for k, (o, h, l, c) in enumerate(rows)
    ]


def smc_breaker_daily_candles() -> List[Candle]:
    """Deterministic waveform producing a bearish internal OB flipped into a valid bullish breaker."""
    closes = [
        110, 109, 108, 107, 106, 105, 104, 105, 106, 107, 108, 109, 110,
        109, 108, 107, 106, 105, 104, 103, 102, 101, 102, 103, 104, 105,
        111, 111.5, 112, 112.5,
    ]
    rows: List[Tuple[float, float, float, float]] = [(110.5, 110.7, 110.3, 110.5)] * 40
    prev = 110.5
    for c in closes:
        rows.append((prev, max(prev, c) + 0.2, min(prev, c) - 0.2, c))
        prev = c
    return mk_daily(rows)


def synthetic_wave_candles(
    n: int = 160,
    base: float = 200.0,
    seed: int = 1,
    hourly: bool = False,
) -> List[Candle]:
    """Rich multi-swing waveform exercising bullish/bearish OBs, breakers, sweeps, and pullbacks."""
    rows: List[Tuple[float, float, float, float]] = []
    p = base
    for i in range(n):
        wave = (
            14.0 * math.sin((i + seed * 7) / 8.5)
            + 7.5 * math.cos((i + seed * 3) / 3.7)
            + 0.12 * i * (1 if seed % 2 == 0 else -0.4)
        )
        nxt = max(20.0, base + wave)
        o = p
        c = nxt
        wick_up = 1.2 + ((i * 13 + seed) % 5) * 0.45
        wick_dn = 1.4 + ((i * 17 + seed) % 6) * 0.50
        h = max(o, c) + wick_up
        l = max(1.0, min(o, c) - wick_dn)
        rows.append((round(o, 2), round(h, 2), round(l, 2), round(c, 2)))
        p = c
    return mk_hourly(rows, vol=250_000.0) if hourly else mk_daily(rows, vol=250_000.0)


class FakeProvider(sc_dp.ScreenerDataProvider):
    batch_size = 5
    provider_name = "fake"

    def __init__(
        self,
        instruments: List[sc_dp.Instrument],
        candles_by_tf: Dict[str, Dict[str, List[Candle]]],
        errors: Optional[Dict[str, str]] = None,
        clock: Optional[sc_dp.Clock] = None,
    ):
        super().__init__(clock)
        self._instruments = instruments
        self._candles_by_tf = candles_by_tf
        self._errors = errors or {}

    def load_instruments(self, today: dt.date) -> List[sc_dp.Instrument]:
        return list(self._instruments)

    def fetch_batch(
        self,
        tf: sc_candles.Timeframe,
        batch: Sequence[sc_dp.Instrument],
        lookback: int,
        now: dt.datetime,
        include_forming: str = "off",
    ) -> Tuple[Dict[str, List[Candle]], Dict[str, str]]:
        self.clock.check()
        self.calls_count += 1
        tf_map = self._candles_by_tf.get(tf.label, {})
        out: Dict[str, List[Candle]] = {}
        errs: Dict[str, str] = {}
        for inst in batch:
            if inst.code in self._errors:
                errs[inst.code] = self._errors[inst.code]
            else:
                cs = sc_candles.completed(tf, tf_map.get(inst.code, []), now, sc_candles.forming_mode(include_forming))
                out[inst.code] = cs[-lookback:]
        return out, errs


# -- 1. Indicators & Forming-Candle Tests -----------------------------------------------------
def test_wilder_rsi_and_atr():
    closes = [100 + i * 1.5 for i in range(30)]
    rsi = sc_ind.rsi_wilder(closes, 14)
    assert rsi[13] is None
    assert rsi[-1] == 100.0

    flat = [100.0] * 30
    assert sc_ind.rsi_wilder(flat, 14)[-1] == 50.0

    cs = smc_breaker_daily_candles()
    atr = sc_ind.atr_wilder(cs, 14)
    assert atr[12] is None
    assert atr[13] is not None and atr[13] > 0


def test_forming_candle_modes():
    tf_d = sc_candles.get_timeframe("1D")
    # Monday 2026-11-02 09:15 IST daily candle
    c0 = Candle(int(dt.datetime(2026, 10, 30, 9, 15, tzinfo=IST).timestamp()), 100, 102, 99, 101, 1000)
    c1 = Candle(int(dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp()), 101, 104, 100, 103, 1000)
    midday = dt.datetime(2026, 11, 2, 12, 30, tzinfo=IST)
    final_stretch = dt.datetime(2026, 11, 2, 15, 10, tzinfo=IST)
    after_close = dt.datetime(2026, 11, 2, 15, 45, tzinfo=IST)

    assert len(sc_candles.completed(tf_d, [c0, c1], midday, "off")) == 1
    assert len(sc_candles.completed(tf_d, [c0, c1], midday, "final")) == 1
    assert len(sc_candles.completed(tf_d, [c0, c1], midday, "always")) == 2
    assert len(sc_candles.completed(tf_d, [c0, c1], final_stretch, "final")) == 2
    assert len(sc_candles.completed(tf_d, [c0, c1], after_close, "off")) == 2


# -- 2. Registry & Settings Validation --------------------------------------------------------
def test_registry_has_all_8_strategies_and_validates():
    ids = [s.id for s in sc_reg.all_strategies()]
    assert ids == [
        "smc_breaker",
        "smc_ob",
        "potential_ob",
        "ob_below_bearish",
        "ob_sweep",
        "ob_above_ob",
        "ob_at_open",
        "ob_in_breaker",
    ]

    # ob_at_open rejects daily timeframe
    _, _, errs, _ = sc_reg.validate_run_settings("ob_at_open", "1D")
    assert any("only" in e for e in errs)

    # ob_at_open accepts 1H and validates candle_time
    _, strat_ok, errs_ok, _ = sc_reg.validate_run_settings(
        "ob_at_open", "1H", strategy_input={"candle_time": "9:15, 13:15"}
    )
    assert not errs_ok
    assert strat_ok["candle_time"] == "9:15, 13:15"

    _, _, errs_bad_time, _ = sc_reg.validate_run_settings(
        "ob_at_open", "1H", strategy_input={"candle_time": "22:00"}
    )
    assert errs_bad_time

    # RSI min > max rejected
    _, _, errs_rsi, _ = sc_reg.validate_run_settings(
        "smc_breaker", "1D", scan_input={"rsi_min": 70, "rsi_max": 40}
    )
    assert any("RSI" in e for e in errs_rsi)


# -- 3. Core SMC Breaker + Filters + Explain + Job Manager ------------------------------------
def test_smc_breaker_match_and_filters():
    cs = smc_breaker_daily_candles()
    strat = sc_reg.get("smc_breaker")
    strat_cfg = {**sc_reg.strategy_defaults("smc_breaker"), "max_breaker_age": 0}
    ctx = ScanContext("TEST", "NSE_1001", "1D", False, dt.datetime(2026, 3, 2, 16, 0, tzinfo=IST))
    rows = strat.scan(cs, strat_cfg, ctx)
    assert len(rows) == 1
    assert rows[0].valid is True
    assert rows[0].values["status"] == "Fresh breaker"

    # Applying strict price filter rejects the match
    scan_cfg = {**sc_reg.scan_defaults(), "min_price": 500, "rsi_max": 100, "min_avg_volume": 0}
    out_drop, removed = sc_filters.apply(cs, list(rows), scan_cfg, keep_rejected=False, now=ctx.now)
    assert removed == 1
    assert out_drop == []


def test_explain_symbol_and_job_manager():
    cs = smc_breaker_daily_candles()
    inst = sc_dp.Instrument("NSE", "1001", "AAA", "AAA LTD", "EQ", "ES")
    prov = FakeProvider([inst], {"1D": {"NSE_1001": cs}})

    scan_cfg, strat_cfg, errs, _ = sc_reg.validate_run_settings(
        "smc_breaker",
        "1D",
        scan_input={
            "universe_mode": "custom",
            "custom_symbols": "AAA",
            "min_price": 0,
            "rsi_max": 100,
            "min_avg_volume": 0,
        },
        strategy_input={"max_breaker_age": 0},
    )
    assert not errs

    # Explain diagnostic
    ex = sc_engine.explain_symbol(
        prov,
        "AAA",
        scan_cfg,
        strat_cfg,
        now_fn=lambda: dt.datetime(2026, 3, 2, 16, 0, tzinfo=IST),
    )
    assert ex["symbol"] == "AAA"
    assert ex["candles_count"] == len(cs)
    assert len(ex["rows"]) == 1
    assert ex["rows"][0]["valid"] is True
    assert len(ex["bullish_breakers"]) >= 1

    # Background Job Manager
    mgr = sc_engine.ScanJobManager()
    job = mgr.start_scan("user-1", scan_cfg, strat_cfg, provider=prov)
    deadline = time.monotonic() + 5.0
    snap = job.snapshot()
    while snap["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        snap = job.snapshot()
    assert snap["state"] == "completed"
    assert snap["stats"]["matches"] == 1
    assert snap["rows"][0]["values"]["symbol"] == "AAA"
    # Owner isolation
    assert mgr.get_job(job.job_id, "user-2") is None
    assert mgr.get_job(job.job_id, "user-1") is not None


# -- 4. IndMoneyBatchProvider HTTP behavior (batching, 400 bisection, 429 retry, CSV) ---------
class _FakeClock(sc_dp.Clock):
    def __init__(self):
        super().__init__(threading.Event())
        self.t = 1_790_000_000.0
        self.slept: List[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.check()
        if seconds > 0:
            self.slept.append(seconds)
            self.t += seconds


class _FakeResp:
    def __init__(self, status: int, body: Any = None, text: Optional[str] = None, headers: Optional[Dict[str, str]] = None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.headers = headers or {}

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        path = url.replace(sc_dp.INDSTOCKS_BASE_URL, "")
        self.calls.append((method, path, dict(params or {})))
        return self.handler(path, params or {}, headers or {})


def test_indmoney_csv_parsing_and_400_bisection():
    csv_text = (
        "EXCH,SEGMENT,SECURITY_ID,INSTRUMENT_NAME,TRADING_SYMBOL,CUSTOM_SYMBOL,SEM_EXCH_INSTRUMENT_TYPE,SERIES,SYMBOL_NAME\n"
        "NSE,E,1001,EQUITY,TCS,TCS,ES,EQ,TCS LTD\n"
        "NSE,E,1002,EQUITY,BAD-BE,BAD,ES,BE,BAD LTD\n"
        "NSE,E,1003,EQUITY,NIFTYBEES,NIFTYBEES,ETF,EQ,NIFTY ETF\n"
        "BSE,E,5001,EQUITY,TCS,TCS,ES,A,TCS LTD\n"
    )
    parsed = sc_dp.parse_indstocks_instruments(csv_text)
    assert [i.symbol for i in parsed] == ["TCS", "NIFTYBEES"]
    all_eq, _ = sc_dp.resolve_universe(parsed, "all_nse_eq")
    assert [i.symbol for i in all_eq] == ["TCS"]

    # Test 400 batch bisection + 429 retry slowdown
    hit_429 = {"done": False}
    cs_sample = smc_breaker_daily_candles()[-5:]

    def handler(path, params, headers):
        codes = (params.get("scrip-codes") or "").split(",")
        if not hit_429["done"]:
            hit_429["done"] = True
            return _FakeResp(429, {"message": "rate limited"}, headers={"Retry-After": "1"})
        if "NSE_BAD" in codes:
            return _FakeResp(400, {"message": "bad scrip code"})
        return _FakeResp(
            200,
            {
                "success": True,
                "data": {c: {"candles": [x.__dict__ for x in cs_sample]} for c in codes},
            },
        )

    clock = _FakeClock()
    prov = sc_dp.IndMoneyBatchProvider(lambda: "tok-123", session=_FakeSession(handler), clock=clock)
    data, errs = prov.historical_resilient("1day", ["NSE_1001", "NSE_BAD"], 1_700_000_000_000, 1_760_000_000_000)
    assert "NSE_1001" in data and len(data["NSE_1001"]) == 5
    assert "NSE_BAD" in errs
    assert prov.slowdowns >= 1


# -- 5. DIRECT DIFFERENTIAL PARITY TEST AGAINST ORIGINAL stock_screener -----------------------
REF_ROOT = Path(
    r"C:\Users\kishor.gunasekaran\Documents\My_Folder\Personal\Investment\INDMoney-Algo\stock_screener"
)


@pytest.mark.skipif(not REF_ROOT.exists(), reason="Reference stock_screener repo not found on disk")
def test_exact_differential_parity_with_stock_screener():
    """Run OpenAlgo's screener AND the original stock_screener side-by-side across all 8 strategies,
    filters, LTF, HTF, and SMC state on multiple waveforms, asserting 100% identical results."""
    ref_str = str(REF_ROOT)
    if ref_str not in sys.path:
        sys.path.insert(0, ref_str)

    ref_reg = importlib.import_module("src.strategies.registry")
    ref_filters = importlib.import_module("src.filters")
    ref_mtf = importlib.import_module("src.mtf")
    ref_smc = importlib.import_module("src.strategies.smc_breaker")
    ref_base = importlib.import_module("src.strategies.base")
    ref_api = importlib.import_module("src.services.api_client")
    ref_cfg = json.loads((REF_ROOT / "config" / "config.json").read_text(encoding="utf-8"))

    # Verify defaults parity for all 8 strategies
    for sid, ref_defaults in ref_cfg["strategies"].items():
        oa_defaults = sc_reg.strategy_defaults(sid)
        assert oa_defaults == ref_defaults, f"Defaults mismatch for {sid}"

    now_dt = dt.datetime(2026, 6, 15, 16, 0, tzinfo=IST)
    daily_sets = [
        smc_breaker_daily_candles(),
        synthetic_wave_candles(180, base=150.0, seed=1, hourly=False),
        synthetic_wave_candles(180, base=320.0, seed=2, hourly=False),
        synthetic_wave_candles(180, base=85.0, seed=3, hourly=False),
        synthetic_wave_candles(220, base=500.0, seed=4, hourly=False),
    ]
    hourly_sets = [
        synthetic_wave_candles(210, base=240.0, seed=5, hourly=True),
        synthetic_wave_candles(210, base=410.0, seed=6, hourly=True),
    ]

    for strat in sc_reg.all_strategies():
        sid = strat.id
        ref_strat = ref_reg.get(sid)
        is_intraday = sid == "ob_at_open"
        tf_label = "1H" if is_intraday else "1D"
        datasets = hourly_sets if is_intraday else daily_sets

        strat_cfg = {**ref_cfg["strategies"][sid], "show_invalid": True}
        if sid == "smc_breaker":
            strat_cfg["max_breaker_age"] = 0
        if sid == "ob_at_open":
            strat_cfg["only_latest"] = False

        scan_cfg = {
            **ref_cfg["scan"],
            "strategy": sid,
            "timeframe": tf_label,
            "min_price": 50,
            "rsi_min": 10,
            "rsi_max": 90,
            "min_avg_volume": 10_000,
        }

        for idx, oa_cs in enumerate(datasets):
            ref_cs = [ref_api.Candle(c.ts, c.o, c.h, c.l, c.c, c.v) for c in oa_cs]
            sym = f"SYM{idx}"
            oa_ctx = ScanContext(sym, f"NSE_{idx}", tf_label, is_intraday, now_dt)
            ref_ctx = ref_base.ScanContext(sym, f"NSE_{idx}", tf_label, is_intraday, now_dt)

            # 1) Strategy scan parity
            oa_rows = strat.scan(oa_cs, strat_cfg, oa_ctx)
            ref_rows = ref_strat.scan(ref_cs, strat_cfg, ref_ctx)

            # 2) Post-filter parity
            oa_rows, oa_rem = sc_filters.apply(oa_cs, oa_rows, scan_cfg, True, now_dt)
            ref_rows, ref_rem = ref_filters.apply(ref_cs, ref_rows, scan_cfg, True, now_dt)

            assert oa_rem == ref_rem, f"{sid} dataset {idx}: filtered count mismatch"
            assert [(r.valid, r.values) for r in oa_rows] == [
                (r.valid, r.values) for r in ref_rows
            ], f"{sid} dataset {idx}: row parity mismatch!"

    # 3) Deep SMC state parity (internal OBs, swing OBs, breakers) + MTF parity
    chart_cfg = {
        "internal_obs": True,
        "swing_obs": True,
        "breakers_from": "both",
        "swing_length": 50,
        "ob_filter": "atr",
        "mitigation": "highlow",
        "max_scan": 2000,
        "internal_confluence": False,
    }
    for oa_cs in daily_sets:
        ref_cs = [ref_api.Candle(c.ts, c.o, c.h, c.l, c.c, c.v) for c in oa_cs]
        oa_st = sc_smc.run_full(oa_cs, chart_cfg)
        ref_st = ref_smc.run_full(ref_cs, chart_cfg)

        assert [(o.index, o.created, o.bias, o.high, o.low, o.source) for o in oa_st.iobs] == [
            (o.index, o.created, o.bias, o.high, o.low, o.source) for o in ref_st.iobs
        ]
        assert [(o.index, o.created, o.bias, o.high, o.low, o.source) for o in oa_st.sobs] == [
            (o.index, o.created, o.bias, o.high, o.low, o.source) for o in ref_st.sobs
        ]
        assert [
            (b.ob_index, b.formed, b.bias, b.top, b.bot, b.invalid_at, b.retest, b.source)
            for b in oa_st.breakers
        ] == [
            (b.ob_index, b.formed, b.bias, b.top, b.bot, b.invalid_at, b.retest, b.source)
            for b in ref_st.breakers
        ]

        # HTF zones & values parity
        oa_hz = sc_mtf.htf_zones(oa_cs, chart_cfg, 5, True)
        ref_hz = ref_mtf.htf_zones(ref_cs, chart_cfg, 5, True)
        zone = (oa_cs[-1].l * 0.99, oa_cs[-1].h * 1.01)
        assert sc_mtf.htf_values(oa_cs, oa_cs[-1], zone, oa_hz, 1.0, "1W") == ref_mtf.htf_values(
            ref_cs, ref_cs[-1], zone, ref_hz, 1.0, "1W"
        )

    # LTF confirmation parity
    oa_ltf = hourly_sets[0]
    ref_ltf = [ref_api.Candle(c.ts, c.o, c.h, c.l, c.c, c.v) for c in oa_ltf]
    z = (min(c.l for c in oa_ltf[-30:]) * 1.005, max(c.h for c in oa_ltf[-30:]) * 0.995)
    start_dt = dt.datetime.fromtimestamp(oa_ltf[-40].ts, tz=IST)
    oa_res = sc_mtf.ltf_check(oa_ltf, z, start_dt, 0.5, ["break", "ob", "sweep"], "any", chart_cfg, 5)
    ref_res = ref_mtf.ltf_check(ref_ltf, z, start_dt, 0.5, ["break", "ob", "sweep"], "any", chart_cfg, 5)
    assert sc_mtf.ltf_values(oa_ltf, oa_res, "1H", True) == ref_mtf.ltf_values(ref_ltf, ref_res, "1H", True)


def test_end_to_end_mtf_phase_and_cancellation():
    daily_cs = smc_breaker_daily_candles()
    hourly_cs = synthetic_wave_candles(210, base=110.0, seed=7, hourly=True)
    weekly_cs = synthetic_wave_candles(120, base=110.0, seed=8, hourly=False)
    inst = sc_dp.Instrument("NSE", "1001", "MTF1", "MTF ONE", "EQ", "ES")
    prov = FakeProvider(
        [inst],
        {
            "1D": {"NSE_1001": daily_cs},
            "1H": {"NSE_1001": hourly_cs},
            "1W": {"NSE_1001": weekly_cs},
        },
    )
    scan_cfg, strat_cfg, errs, _ = sc_reg.validate_run_settings(
        "smc_breaker",
        "1D",
        scan_input={
            "universe_mode": "custom",
            "custom_symbols": "MTF1",
            "min_price": 0,
            "rsi_max": 100,
            "min_avg_volume": 0,
            "ltf_on": True,
            "htf_on": True,
        },
        strategy_input={"max_breaker_age": 0},
    )
    assert not errs
    out = sc_engine.run_scan_with_provider(
        prov,
        scan_cfg,
        strat_cfg,
        now_fn=lambda: dt.datetime(2026, 3, 2, 16, 0, tzinfo=IST),
    )
    assert out.fatal == ""
    assert out.ltf_checked == 1
    assert out.htf_checked == 1
    assert len(out.rows) == 1
    assert "ltf_status" in out.rows[0].values
    assert "htf_ob" in out.rows[0].values

    # Cancellation before start returns cancelled outcome cleanly
    cancel_ev = threading.Event()
    cancel_ev.set()
    prov_cancel = FakeProvider([inst], {"1D": {"NSE_1001": daily_cs}}, clock=sc_dp.Clock(cancel_ev))
    out_c = sc_engine.run_scan_with_provider(prov_cancel, scan_cfg, strat_cfg)
    assert out_c.cancelled is True


def test_restx_screener_api_endpoints(monkeypatch):
    from flask import Flask
    from flask_restx import Api
    from restx_api import screener as screener_api

    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(screener_api.api, path="/api/v1/screener")
    client = app.test_client()

    # 1. /meta returns all 8 strategies
    r_meta = client.get("/api/v1/screener/meta")
    assert r_meta.status_code == 200
    meta = r_meta.get_json()
    assert meta["status"] == "success"
    assert len(meta["data"]["strategies"]) == 8

    # 2. Invalid apikey -> 403
    monkeypatch.setattr(screener_api, "verify_api_key", lambda k: "u1" if k == "valid-key" else None)
    r_bad = client.post(
        "/api/v1/screener/start",
        json={"apikey": "wrong", "strategy": "smc_breaker", "timeframe": "1D"},
    )
    assert r_bad.status_code == 403

    # 3. Invalid timeframe for ob_at_open -> 400
    r_tf = client.post(
        "/api/v1/screener/start",
        json={"apikey": "valid-key", "strategy": "ob_at_open", "timeframe": "1D"},
    )
    assert r_tf.status_code == 400

    # 4. Valid start + status + stop + explain using FakeProvider
    cs = smc_breaker_daily_candles()
    inst = sc_dp.Instrument("NSE", "1001", "AAA", "AAA LTD", "EQ", "ES")
    fake_prov = FakeProvider([inst], {"1D": {"NSE_1001": cs}})
    monkeypatch.setattr(screener_api, "create_provider", lambda *a, **kw: fake_prov)
    monkeypatch.setattr(sc_engine, "create_provider", lambda *a, **kw: fake_prov)

    r_start = client.post(
        "/api/v1/screener/start",
        json={
            "apikey": "valid-key",
            "strategy": "smc_breaker",
            "timeframe": "1D",
            "scan": {
                "source": "db",
                "universe_mode": "custom",
                "custom_symbols": "AAA",
                "min_price": 0,
                "rsi_max": 100,
                "min_avg_volume": 0,
            },
            "strategy_settings": {"max_breaker_age": 0},
        },
    )
    assert r_start.status_code == 200
    job_id = r_start.get_json()["data"]["job_id"]

    # Poll until finished
    deadline = time.monotonic() + 5.0
    snap = {}
    while time.monotonic() < deadline:
        r_st = client.post(
            "/api/v1/screener/status",
            json={"apikey": "valid-key", "job_id": job_id},
        )
        assert r_st.status_code == 200
        snap = r_st.get_json()["data"]
        if snap["state"] != "running":
            break
        time.sleep(0.02)
    assert snap["state"] == "completed"
    assert snap["stats"]["matches"] == 1

    # Explain endpoint
    r_exp = client.post(
        "/api/v1/screener/explain",
        json={
            "apikey": "valid-key",
            "symbol": "AAA",
            "strategy": "smc_breaker",
            "timeframe": "1D",
            "scan": {"source": "db", "min_price": 0, "rsi_max": 100, "min_avg_volume": 0},
            "strategy_settings": {"max_breaker_age": 0},
        },
    )
    assert r_exp.status_code == 200
    exp_data = r_exp.get_json()["data"]
    assert exp_data["symbol"] == "AAA"
    assert len(exp_data["bullish_breakers"]) >= 1

