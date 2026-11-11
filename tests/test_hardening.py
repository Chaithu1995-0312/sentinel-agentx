"""Hardening fixes: broker re-verify, locking, CORS, API key, trailing-window pick,
paper costs, intraday exits, strict backtest, plus unit checks that were missing."""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DEAD_URL = "http://127.0.0.1:9"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def risk(tmp_path, monkeypatch):
    mod = _load("h_risk", "services/risk-guard/main.py")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "risk_state.json")
    monkeypatch.setattr(mod, "KILL_PATH", tmp_path / "kill_switch.json")
    TestClient(mod.app).post("/kill-switch", json={"killed": False})
    return mod


@pytest.fixture()
def broker(tmp_path, monkeypatch, risk):
    mod = _load("h_broker", "services/paper-broker/main.py")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "broker_state.json")
    monkeypatch.setattr(mod, "RISK_URL", DEAD_URL)
    rc = TestClient(risk.app)
    # Route the broker's risk calls to the in-process risk-guard.
    monkeypatch.setattr(mod, "_risk_check", lambda payload: rc.post("/check-order", json=payload).json())
    monkeypatch.setattr(
        mod, "_record_trade",
        lambda realized, count_trade: rc.post("/record-trade", json={"realized_pnl": realized, "count_trade": count_trade}),
    )
    return mod


def _buy(qty=10.0, price=100.0, **kw):
    body = {"symbol": "SYNTH", "side": "buy", "qty": qty, "price": price, "risk_allowed": True,
            "stop_loss": round(price * 0.98, 4), "take_profit": round(price * 1.03, 4)}
    body.update(kw)
    return body


# 1. Broker no longer trusts risk_allowed=true --------------------------------
def test_broker_reverifies_and_refuses_oversized_order(broker):
    c = TestClient(broker.app)
    r = c.post("/orders", json=_buy(qty=300.0))  # 30% of equity, risk_allowed=true
    assert r.status_code == 403 and "max_position_pct" in r.json()["detail"]
    assert c.get("/account").json()["positions"] == {}


def test_broker_refuses_under_kill_switch_even_with_flag(broker, risk):
    TestClient(risk.app).post("/kill-switch", json={"killed": True})
    r = TestClient(broker.app).post("/orders", json=_buy())
    assert r.status_code == 403 and "kill_switch" in r.json()["detail"]


def test_broker_fails_closed_when_risk_guard_unreachable(tmp_path, monkeypatch):
    mod = _load("h_broker_dead", "services/paper-broker/main.py")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "b.json")
    monkeypatch.setattr(mod, "RISK_URL", DEAD_URL)
    r = TestClient(mod.app).post("/orders", json=_buy())
    assert r.status_code == 503


def test_small_order_fills_with_costs_and_recheck(broker):
    c = TestClient(broker.app)
    r = c.post("/orders", json=_buy(qty=10.0, price=100.0)).json()
    f = r["fill"]
    assert r["status"] == "filled" and f["risk_recheck"] == "ok"
    assert f["price"] == pytest.approx(100.05)  # 5 bps slippage against us
    assert f["fee"] == pytest.approx(10 * 100.05 * 0.001, abs=1e-4)  # 10 bps fee
    assert c.get("/account").json()["cash"] == pytest.approx(100000 - 1000.5 - f["fee"], abs=1e-3)


# 2. Concurrency ----------------------------------------------------------------
def test_concurrent_orders_cannot_breach_position_cap(broker):
    c = TestClient(broker.app)
    codes: list[int] = []

    def go():
        codes.append(c.post("/orders", json=_buy(qty=40.0, price=100.0)).status_code)  # 4% each

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == 1 and codes.count(403) == 7
    assert c.get("/account").json()["positions"]["SYNTH"]["qty"] == 40.0


def test_concurrent_record_trade_loses_no_update(risk):
    rc = TestClient(risk.app)
    threads = [threading.Thread(target=lambda: rc.post("/record-trade", json={"realized_pnl": -1.0})) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    s = rc.get("/state").json()
    assert s["trades_today"] == 40 and s["realized_pnl_today"] == pytest.approx(-40.0)
    assert not list(Path(risk.STATE_PATH).parent.glob(".*.tmp"))  # atomic writes leave no temp files


def test_run_is_serialised(monkeypatch):
    orch = _load("h_orch_lock", "services/agent-orchestrator/main.py")
    monkeypatch.setattr(orch, "RUN_LOCK_WAIT", 0.05)
    orch.RUN_LOCK.acquire()
    try:
        with pytest.raises(HTTPException) as exc:
            orch.run(orch.RunRequest())
        assert exc.value.status_code == 409
    finally:
        orch.RUN_LOCK.release()


# 3. CORS -------------------------------------------------------------------------
@pytest.mark.parametrize("origin,allowed", [
    ("http://127.0.0.1:8010", True), ("http://localhost:8010", True), ("null", True),
    ("https://evil.example", False),
])
def test_cors_allow_list(risk, origin, allowed):
    r = TestClient(risk.app).options(
        "/kill-switch",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type,x-sentinel-key"},
    )
    assert (r.headers.get("access-control-allow-origin") == origin) is allowed


# 4. Optional API key ----------------------------------------------------------
def test_api_key_required_on_mutating_routes_when_set(risk, broker, monkeypatch):
    monkeypatch.setenv("SENTINEL_API_KEY", "s3cret")
    rc, bc = TestClient(risk.app), TestClient(broker.app)
    assert rc.post("/kill-switch", json={"killed": True}).status_code == 401
    assert rc.post("/kill-switch", json={"killed": True}, headers={"X-Sentinel-Key": "wrong"}).status_code == 401
    assert bc.post("/reset").status_code == 401
    assert rc.get("/kill-switch").status_code == 200  # reads stay open
    ok = rc.post("/kill-switch", json={"killed": False}, headers={"X-Sentinel-Key": "s3cret"})
    assert ok.status_code == 200


def test_api_key_unset_keeps_local_demo_open(risk, monkeypatch):
    monkeypatch.delenv("SENTINEL_API_KEY", raising=False)
    assert TestClient(risk.app).post("/kill-switch", json={"killed": False}).status_code == 200


def test_service_calls_forward_the_key(monkeypatch):
    sec = _load("h_sec", "shared/security.py")
    monkeypatch.setenv("SENTINEL_API_KEY", "abc")
    assert sec.auth_headers() == {"X-Sentinel-Key": "abc"}
    monkeypatch.delenv("SENTINEL_API_KEY")
    assert sec.auth_headers() == {}


# 5. Live pick uses the trailing window, not generator labels -----------------
@pytest.fixture()
def engine(monkeypatch):
    mod = _load("h_engine", "services/strategy-engine/main.py")
    monkeypatch.setattr(mod, "MARKET_URL", DEAD_URL)  # read the seed files directly
    return mod


def test_backtest_live_pick_is_trailing_window(engine):
    bt = engine.backtest(engine.BacktestRequest())
    lw = bt["live_window"]
    assert lw["bars"] == engine.LIVE_WINDOW_BARS and lw["end"] == bt["equity_curve"][-1]["date"]
    assert bt["regime_strategy"] == lw["selected_strategy"]


def test_orchestrator_ignores_regime_labels(tmp_path, monkeypatch):
    orch = _load("h_orch_pick", "services/agent-orchestrator/main.py")
    monkeypatch.setattr(orch, "CHOICE_PATH", tmp_path / "c.json")
    row = lambda n, sc: {"name": n, "score": sc, "return": sc, "max_drawdown": 0.0, "trades": 2}
    bt = {
        "selected_strategy": "bollinger_breakout",
        "current_regime": "recovery",
        "regimes": [{"name": "recovery", "strategies": [row("rsi_mean_reversion", 0.9)]}],  # label says RSI
        "live_window": {"bars": 60, "start": "a", "end": "b", "strategies": [row("sma_crossover", 0.2), row("rsi_mean_reversion", 0.0)]},
    }
    choice, note = orch.pick_live_strategy(bt)
    assert choice == "sma_crossover" and "Trailing 60 bars" in note


def test_strict_backtest_fills_next_open_and_hits_stop(engine):
    bars = [
        {"date": "d1", "open": 100, "high": 100, "low": 100, "close": 100},
        {"date": "d2", "open": 101, "high": 101, "low": 101, "close": 101},
        {"date": "d3", "open": 100, "high": 100, "low": 97, "close": 98},  # low breaches 2% stop
    ]
    sim = engine._simulate_strict(bars, ["buy", "hold", "hold"], 1000.0, 0.0, 0.02, 0.03)
    assert sim["fills"] == 2 and sim["stop_exits"] == 1
    assert sim["final_equity"] == pytest.approx(1000.0 * (101 * 0.98) / 101)  # bought at d2 open, out at stop


def test_backtest_reports_strict_check(engine):
    bt = engine.backtest(engine.BacktestRequest())
    names = {r["name"] for r in bt["strict_check"]["strategies"]}
    assert names == set(engine.STRATEGIES)


# 8. Intraday exits on later bars, gaps, entry-bar guard ------------------------
def _seed_long(broker, entry_bar="2025-11-03"):
    st = broker._empty_state()
    st["cash"] = 90000.0
    st["positions"] = {"SYNTH": {"qty": 100.0, "avg_price": 100.0, "stop_loss": 98.0, "take_profit": 103.0, "entry_bar": entry_bar}}
    broker._save(st)


def test_intraday_low_touch_triggers_stop_at_level(broker, monkeypatch):
    monkeypatch.setattr(broker, "SLIPPAGE_BPS", 0.0)
    monkeypatch.setattr(broker, "FEE_BPS", 0.0)
    _seed_long(broker)
    r = TestClient(broker.app).post("/evaluate-exits", json={
        "symbol": "SYNTH", "price": 99.5, "bar_date": "2025-11-04", "open": 99.8, "high": 100.2, "low": 97.9}).json()
    assert r["exits"][0]["reason"] == "stop_loss" and r["exits"][0]["price"] == pytest.approx(98.0)


def test_gap_below_stop_fills_at_open(broker, monkeypatch):
    monkeypatch.setattr(broker, "SLIPPAGE_BPS", 0.0)
    monkeypatch.setattr(broker, "FEE_BPS", 0.0)
    _seed_long(broker)
    r = TestClient(broker.app).post("/evaluate-exits", json={
        "symbol": "SYNTH", "price": 95.5, "bar_date": "2025-11-04", "open": 95.0, "high": 96.0, "low": 94.0}).json()
    assert r["exits"][0]["price"] == pytest.approx(95.0)


def test_entry_bar_range_does_not_trigger(broker):
    _seed_long(broker, entry_bar="2025-11-04")
    r = TestClient(broker.app).post("/evaluate-exits", json={
        "symbol": "SYNTH", "price": 100.0, "bar_date": "2025-11-04", "open": 99.0, "high": 104.0, "low": 96.0}).json()
    assert r["exits"] == []


def test_account_can_mark_to_market(broker):
    _seed_long(broker)
    c = TestClient(broker.app)
    assert c.get("/account").json()["equity"] == pytest.approx(100000.0)
    m = c.get("/account", params={"symbol": "SYNTH", "price": 90.0}).json()
    assert m["equity"] == pytest.approx(99000.0) and m["equity_basis"] == "marked"


def test_market_rejects_unknown_symbol():
    mi = _load("h_market", "services/market-ingest/main.py")
    c = TestClient(mi.app)
    assert c.get("/bars", params={"symbol": "AAPL"}).status_code == 404
    assert c.get("/bars", params={"symbol": "SYNTH", "limit": 2}).json()["count"] == 2


def test_journal_outage_does_not_crash_cycle():
    orch = _load("h_orch_j", "services/agent-orchestrator/main.py")
    with httpx.Client(timeout=0.5) as client:
        orig = orch.JOURNAL_URL
        orch.JOURNAL_URL = DEAD_URL
        try:
            out = orch.tool_journal(client, {"title": "x"})
        finally:
            orch.JOURNAL_URL = orig
    assert out["id"] is None and "journal unavailable" in out["error"]


def test_dashboard_has_no_validation_link():
    html = (ROOT / "services" / "dashboard-ui" / "index.html").read_text()
    assert 'href="/validation"' not in html


def test_requirements_list_pytest():
    assert any(l.startswith("pytest") for l in (ROOT / "requirements.txt").read_text().splitlines())


# Unit checks that were missing ----------------------------------------------
def test_indicators(engine):
    assert engine._sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    mid, up, lo = engine._bollinger([1.0, 3.0], 2, 2)
    assert mid[1] == 2.0 and up[1] == pytest.approx(4.0) and lo[1] == pytest.approx(0.0)  # population sd = 1


def test_sma_cross_rule(engine):
    closes = [10, 10, 10, 10, 10, 12, 14, 16]
    acts = engine._actions("sma_crossover", closes, 2, 4)
    assert "buy" in acts and acts.index("buy") == 5


def test_headline_scoring(engine):
    assert engine.score_headline("Stocks gain as rally firm")["score"] > 0
    assert engine.score_headline("Sell-off deepens")["score"] < 0


def test_risk_order_size_checked_before_trade_count(risk):
    rc = TestClient(risk.app)
    for _ in range(risk.MAX_TRADES_PER_DAY):
        rc.post("/record-trade", json={"realized_pnl": 0})
    big = rc.post("/check-order", json={"symbol": "SYNTH", "side": "buy", "qty": 300, "price": 100}).json()
    small = rc.post("/check-order", json={"symbol": "SYNTH", "side": "buy", "qty": 1, "price": 100}).json()
    assert big["reason"] == "max_position_pct" and small["reason"] == "max_trades_per_day"


def test_journal_template_plain_english():
    j = _load("h_journal", "services/journal-api/main.py")
    text = j._template_body({"symbol": "SYNTH", "action": "buy", "qty": 1, "price": 10, "reason": "SMA crossed", "allowed": False, "risk_reason": "max_position_pct"})
    assert text.startswith("We looked at SYNTH and decided to buy.") and "Risk guard blocked it" in text


def test_full_size_order_stays_inside_cap():
    orch = _load("h_orch_size", "services/agent-orchestrator/main.py")
    for price in (126.3, 99.99, 101.37, 250.01):
        qty = orch._sized_qty(100000.0, price, 1.0, None)
        assert qty * price <= 100000.0 * orch.MAX_POS + 1e-9
