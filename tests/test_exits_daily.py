"""Stop-loss / take-profit exits and the true daily loss limit (paper only, no network)."""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DEAD_URL = "http://127.0.0.1:9"  # nothing listens; best-effort calls fail fast


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    mod = _load("t_broker", "services/paper-broker/main.py")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "broker_state.json")
    monkeypatch.setattr(mod, "RISK_URL", DEAD_URL)
    return mod


@pytest.fixture()
def risk(tmp_path, monkeypatch):
    mod = _load("t_risk", "services/risk-guard/main.py")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "risk_state.json")
    monkeypatch.setattr(mod, "KILL_PATH", tmp_path / "kill_switch.json")
    return mod


def _seed(broker, *, qty=100.0, avg=100.0, stop=98.0, tp=103.0, fills=None, cash=90000.0):
    state = broker._empty_state()
    state["cash"] = cash
    state["day_start_equity"] = 100000.0
    state["positions"] = {"SYNTH": {"qty": qty, "avg_price": avg, "stop_loss": stop, "take_profit": tp}}
    state["fills"] = fills or []
    broker._save(state)


def test_stop_loss_closes_position(broker):
    _seed(broker)
    c = TestClient(broker.app)
    r = c.post("/evaluate-exits", json={"symbol": "SYNTH", "price": 97.5}).json()
    assert len(r["exits"]) == 1
    fill = r["exits"][0]
    assert fill["reason"] == "stop_loss" and fill["side"] == "sell" and fill["qty"] == 100.0
    assert fill["realized_pnl"] == pytest.approx(-250.0)
    acct = c.get("/account").json()
    assert acct["positions"] == {}
    assert acct["cash"] == pytest.approx(90000.0 + 9750.0)


def test_take_profit_closes_position(broker):
    _seed(broker)
    c = TestClient(broker.app)
    r = c.post("/evaluate-exits", json={"symbol": "SYNTH", "price": 103.0}).json()
    assert [f["reason"] for f in r["exits"]] == ["take_profit"]
    assert r["exits"][0]["realized_pnl"] == pytest.approx(300.0)
    assert c.get("/account").json()["positions"] == {}


def test_no_trigger_between_stop_and_take_profit(broker):
    _seed(broker)
    c = TestClient(broker.app)
    for px in (98.01, 100.0, 102.99):
        r = c.post("/evaluate-exits", json={"symbol": "SYNTH", "price": px}).json()
        assert r["exits"] == []
    assert c.get("/account").json()["positions"]["SYNTH"]["qty"] == 100.0


def test_dry_run_reports_but_does_not_close(broker):
    _seed(broker)
    c = TestClient(broker.app)
    r = c.post("/evaluate-exits", json={"symbol": "SYNTH", "price": 90.0, "dry_run": True}).json()
    assert r["exits"][0]["reason"] == "stop_loss"
    assert "SYNTH" in c.get("/account").json()["positions"]


def test_exit_runs_under_kill_switch_and_skips_trade_cap(broker, risk, tmp_path, monkeypatch):
    orch = _load("t_orch", "services/agent-orchestrator/main.py")
    monkeypatch.setattr(orch, "CHOICE_PATH", tmp_path / "choice.json")
    _seed(broker)
    bc = TestClient(broker.app)
    rc = TestClient(risk.app)
    rc.post("/kill-switch", json={"killed": True})
    journal: list[dict] = []

    monkeypatch.setattr(orch, "tool_kill", lambda client: rc.get("/kill-switch").json())
    monkeypatch.setattr(
        orch, "tool_get_market", lambda client, s: {"last": {"close": 97.0}, "news": [], "bars_count": 1}
    )
    monkeypatch.setattr(
        orch,
        "tool_evaluate_exits",
        lambda client, s, p, d: bc.post("/evaluate-exits", json={"symbol": s, "price": p, "dry_run": d}).json(),
    )
    bt = {
        "strategies": [{"name": "sma_crossover", "return": 0.1, "max_drawdown": 0.05, "trades": 2}],
        "selected_strategy": "sma_crossover",
        "selection_reason": "test",
    }
    monkeypatch.setattr(orch, "tool_run_backtest", lambda client, s: bt)
    monkeypatch.setattr(orch, "tool_journal", lambda client, p: journal.append(p) or {"id": len(journal)})
    # Broker tells risk-guard about the exit; route that to the in-process risk app.
    monkeypatch.setattr(
        broker,
        "_record_trade",
        lambda realized, count_trade: rc.post(
            "/record-trade", json={"realized_pnl": realized, "count_trade": count_trade}
        ),
    )
    before = rc.get("/state").json()["trades_today"]

    out = orch.run(orch.RunRequest())

    assert out["status"] == "blocked_kill_switch"
    assert [e["reason"] for e in out["exits"]] == ["stop_loss"]
    assert bc.get("/account").json()["positions"] == {}
    assert journal[0]["title"].startswith("Stop-loss hit")
    assert "kill switch is ON" in journal[0]["reason"]
    state = rc.get("/state").json()
    assert state["trades_today"] == before  # exit not counted against 10/day
    assert state["exits_today"] == 1
    # New entries are still refused while the kill switch is ON.
    chk = rc.post("/check-order", json={"symbol": "SYNTH", "side": "buy", "qty": 1, "price": 97}).json()
    assert chk["reason"] == "kill_switch"


def _ts_days_ago(days: int) -> float:
    return time.time() - days * 86400


def test_daily_limit_counts_only_today_and_includes_unrealized(broker, risk):
    fills = [
        {"realized_pnl": -5000.0, "ts": _ts_days_ago(2)},  # old loss: must not count
        {"realized_pnl": -2000.0, "ts": time.time()},  # today's realized loss
    ]
    _seed(broker, qty=100.0, avg=100.0, stop=None, tp=None, fills=fills)
    bc = TestClient(broker.app)
    d = bc.get("/daily-pnl", params={"symbol": "SYNTH", "price": 85.0}).json()
    assert d["realized_today"] == pytest.approx(-2000.0)
    assert d["unrealized"] == pytest.approx(-1500.0)
    assert d["daily_pnl"] == pytest.approx(-3500.0)

    rc = TestClient(risk.app)
    rc.post("/kill-switch", json={"killed": False})
    order = {"symbol": "SYNTH", "side": "buy", "qty": 1, "price": 85.0, "equity": 98500.0}
    # Realized-only view (-2000) stays inside 3% of 100k ...
    ok = rc.post("/check-order", json={**order, "daily_pnl": d["realized_today"], "day_start_equity": 100000}).json()
    assert ok["allowed"] is True
    # ... but realized + unrealized (-3500) breaches it.
    bad = rc.post("/check-order", json={**order, "daily_pnl": d["daily_pnl"], "day_start_equity": d["day_start_equity"]}).json()
    assert bad["allowed"] is False and bad["reason"] == "daily_loss_limit"


def test_day_rollover_resets_daily_window(broker, monkeypatch):
    _seed(broker, qty=10.0, avg=100.0, stop=None, tp=None, fills=[{"realized_pnl": -2500.0, "ts": time.time()}], cash=97500.0)
    bc = TestClient(broker.app)
    assert bc.get("/daily-pnl").json()["realized_today"] == pytest.approx(-2500.0)

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(broker, "_today", lambda: tomorrow)
    d = bc.get("/daily-pnl").json()
    assert d["day"] == tomorrow
    assert d["realized_today"] == 0.0
    # New start-of-day equity snapshot = cash + position at cost.
    assert d["day_start_equity"] == pytest.approx(97500.0 + 1000.0)
    saved = json.loads(broker.STATE_PATH.read_text())
    assert saved["day"] == tomorrow


def test_paper_reset_does_not_clear_todays_loss(broker):
    _seed(broker, fills=[{"realized_pnl": -1200.0, "ts": time.time()}])
    bc = TestClient(broker.app)
    bc.post("/reset")
    assert bc.get("/daily-pnl").json()["realized_today"] == pytest.approx(-1200.0)


def test_risk_guard_day_rollover_resets_trade_count(risk, monkeypatch):
    rc = TestClient(risk.app)
    rc.post("/record-trade", json={"realized_pnl": -10.0})
    assert rc.get("/state").json()["trades_today"] == 1
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(risk, "_today", lambda: tomorrow)
    s = rc.get("/state").json()
    assert s["day"] == tomorrow and s["trades_today"] == 0 and s["realized_pnl_today"] == 0.0
