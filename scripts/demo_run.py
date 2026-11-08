#!/usr/bin/env python3
"""One-shot happy path + kill-switch denial demo. Requires services up via run_all.py."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

RISK = os.getenv("RISK_GUARD_URL", "http://127.0.0.1:8003")
BROKER = os.getenv("PAPER_BROKER_URL", "http://127.0.0.1:8004")
AGENT = os.getenv("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8005")
JOURNAL = os.getenv("JOURNAL_API_URL", "http://127.0.0.1:8006")
MARKET = os.getenv("MARKET_INGEST_URL", "http://127.0.0.1:8001")
STRATEGY = os.getenv("STRATEGY_ENGINE_URL", "http://127.0.0.1:8002")

PORTS = [MARKET, STRATEGY, RISK, BROKER, AGENT, JOURNAL]


def wait_ready(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    with httpx.Client(timeout=2.0) as c:
        while time.time() < deadline:
            ok = True
            for base in PORTS:
                try:
                    r = c.get(f"{base}/health")
                    if r.status_code != 200:
                        ok = False
                        break
                except httpx.HTTPError:
                    ok = False
                    break
            if ok:
                print("[demo] all services healthy")
                return
            time.sleep(0.5)
    raise SystemExit("[demo] services not ready - start with: python scripts/run_all.py")


def pp(label: str, data) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(data, indent=2)[:2000])


def main() -> int:
    wait_ready()
    failures = []

    with httpx.Client(timeout=30.0) as c:
        # Reset paper + journal for clean demo
        c.post(f"{BROKER}/reset")
        try:
            c.request("DELETE", f"{JOURNAL}/entries")
        except httpx.HTTPError:
            pass

        # --- Part A: kill ON -> deny ---
        print("\n[demo] PART A - kill switch ON must deny")
        c.post(f"{RISK}/kill-switch", json={"killed": True})
        ks = c.get(f"{RISK}/kill-switch").json()
        assert ks.get("killed") is True, ks
        blocked = c.post(f"{AGENT}/run", json={"goal": "paper_cycle", "symbol": "SYNTH"}).json()
        pp("agent while killed", {"status": blocked.get("status"), "steps": [s["tool"] for s in blocked.get("steps", [])]})
        if blocked.get("status") != "blocked_kill_switch":
            failures.append(f"expected blocked_kill_switch, got {blocked.get('status')}")
        else:
            print("[demo] OK kill switch blocked agent")

        # Direct risk check also denies
        chk = c.post(
            f"{RISK}/check-order",
            json={"symbol": "SYNTH", "side": "buy", "qty": 1, "price": 100, "equity": 100000},
        ).json()
        if chk.get("allowed") is not False or chk.get("reason") != "kill_switch":
            failures.append(f"check-order kill fail: {chk}")
        else:
            print("[demo] OK check-order reason=kill_switch")

        # --- Part B: clear kill, force a buy via orchestrator with force_qty ---
        print("\n[demo] PART B - clear kill, run happy path")
        c.post(f"{RISK}/kill-switch", json={"killed": False})
        ks = c.get(f"{RISK}/kill-switch").json()
        assert ks.get("killed") is False

        # Seed a deterministic buy: use risk+broker directly if signal is hold,
        # but still exercise full agent loop first.
        run = c.post(
            f"{AGENT}/run",
            json={"goal": "paper_cycle", "symbol": "SYNTH", "force_qty": 10},
        ).json()
        pp("agent paper_cycle", {"status": run.get("status"), "backtest_summary": run.get("backtest_summary")})

        # If signal was hold, place a controlled paper buy through risk path to prove fills
        if run.get("status") in ("hold", "hold_flat"):
            print("[demo] signal was hold - placing controlled paper buy via risk->broker")
            price = c.get(f"{MARKET}/last-price").json()["price"]
            stop = round(price * 0.98, 4)
            tp = round(price * (1 + 0.02 * 1.5), 4)
            risk = c.post(
                f"{RISK}/check-order",
                json={
                    "symbol": "SYNTH",
                    "side": "buy",
                    "qty": 10,
                    "price": price,
                    "equity": 100000,
                    "stop_loss": stop,
                    "take_profit": tp,
                    "open_notional": 0,
                },
            ).json()
            pp("risk check", risk)
            if not risk.get("allowed"):
                failures.append(f"expected allow after clear kill: {risk}")
            else:
                fill = c.post(
                    f"{BROKER}/orders",
                    json={
                        "symbol": "SYNTH",
                        "side": "buy",
                        "qty": 10,
                        "price": price,
                        "risk_allowed": True,
                        "stop_loss": stop,
                        "take_profit": tp,
                        "reason": "demo controlled buy",
                    },
                ).json()
                pp("fill", fill)
                c.post(
                    f"{JOURNAL}/trade",
                    json={
                        "title": "Demo paper buy",
                        "symbol": "SYNTH",
                        "side": "buy",
                        "action": "buy",
                        "qty": 10,
                        "price": price,
                        "stop_loss": stop,
                        "take_profit": tp,
                        "allowed": True,
                        "risk_reason": "ok",
                        "reason": "demo controlled buy after SMA hold",
                        "tags": ["demo", "fill"],
                    },
                )
                print("[demo] OK controlled fill")
        elif run.get("status") == "filled":
            print("[demo] OK agent filled paper order")
        elif run.get("status") == "risk_denied":
            # Still ok if limits blocked - prove kill cleared path worked earlier
            print("[demo] WARN agent risk_denied (limits) - kill path already proven")
        else:
            failures.append(f"unexpected agent status {run.get('status')}")

        # Backtest shape: every strategy plus buy-and-hold, and the selection metric.
        bt = c.post(f"{STRATEGY}/backtest", json={"symbol": "SYNTH"}).json()
        pp("backtest", {
            "selected_strategy": bt.get("selected_strategy"),
            "strategy_return": bt.get("strategy_return"),
            "buy_hold_return": bt.get("buy_hold_return"),
            "summary": bt.get("summary"),
            "strategies": bt.get("strategies"),
            "buy_hold": bt.get("buy_hold"),
        })
        if bt.get("strategy_return") is None or bt.get("buy_hold_return") is None:
            failures.append("backtest missing returns")
        else:
            print("[demo] OK backtest vs buy-and-hold")

        print("\n[demo] PART D - news score and strategy selection")
        names = {row.get("name") for row in bt.get("strategies") or []}
        required = {"sma_crossover", "rsi_mean_reversion", "bollinger_breakout"}
        if not required <= names:
            failures.append(f"backtest strategies missing {sorted(required - names)}")
        rows = list(bt.get("strategies") or [])
        bh = bt.get("buy_hold") or {}
        if bh.get("name") != "buy_and_hold":
            failures.append(f"buy_hold missing or misnamed: {bh}")
        else:
            rows_for_fields = rows + [bh]
            bad_rows = [
                row.get("name")
                for row in rows_for_fields
                if row.get("return") is None or row.get("max_drawdown") is None or row.get("trades") is None
            ]
            if bad_rows:
                failures.append(f"comparison rows missing return, max drawdown, or trades: {bad_rows}")
        penalty = float(bt.get("drawdown_penalty", 0.5))

        def _score(row: dict) -> float:
            return float(row["return"]) - penalty * float(row["max_drawdown"])

        if rows and all(row.get("return") is not None and row.get("max_drawdown") is not None for row in rows):
            best = sorted(
                rows,
                key=lambda row: (-_score(row), -float(row["return"]), int(row["trades"]), row["name"]),
            )[0]
            if bt.get("selected_strategy") != best["name"]:
                failures.append(
                    f"selected {bt.get('selected_strategy')} but metric picks {best['name']}"
                )
            elif run.get("selected_strategy") != bt.get("selected_strategy"):
                failures.append(
                    f"orchestrator picked {run.get('selected_strategy')}, "
                    f"backtest selected {bt.get('selected_strategy')}"
                )
            else:
                print(f"[demo] OK selected {best['name']} by return - {penalty:g} * max drawdown")
        else:
            failures.append("cannot score strategies")

        regime_names = [reg.get("name") for reg in bt.get("regimes") or []]
        missing_regimes = [name for name in ("rally", "crash", "chop", "recovery") if name not in regime_names]
        if missing_regimes:
            failures.append(f"backtest regimes missing {missing_regimes}")
        else:
            bad_regime = False
            for reg in bt.get("regimes") or []:
                regime_rows = list(reg.get("strategies") or []) + [reg.get("buy_hold") or {}]
                if any(row.get("return") is None or row.get("max_drawdown") is None or row.get("trades") is None for row in regime_rows):
                    failures.append(f"regime {reg.get('name')} missing return, max drawdown, or trades")
                    bad_regime = True
            crash = next(reg for reg in bt["regimes"] if reg["name"] == "crash")
            crash_bh = crash.get("buy_hold") or {}
            crash_sel = next((row for row in crash.get("strategies") or [] if row.get("name") == bt.get("selected_strategy")), None)
            if float(crash_bh.get("max_drawdown") or 0) < 0.20:
                failures.append(f"crash buy-and-hold drawdown is not sharp: {crash_bh.get('max_drawdown')}")
                bad_regime = True
            elif crash_sel is None:
                failures.append("selected strategy missing from crash regime")
                bad_regime = True
            elif float(crash_sel["max_drawdown"]) >= float(crash_bh["max_drawdown"]):
                failures.append(
                    f"selected {bt.get('selected_strategy')} crash drawdown {crash_sel['max_drawdown']} "
                    f"did not beat buy-and-hold {crash_bh['max_drawdown']}"
                )
                bad_regime = True
            if bt.get("regime_strategy") not in required:
                failures.append(f"regime_strategy missing: {bt.get('regime_strategy')}")
                bad_regime = True
            elif run.get("regime_strategy") != bt.get("regime_strategy"):
                failures.append(
                    f"orchestrator regime strategy {run.get('regime_strategy')} != {bt.get('regime_strategy')}"
                )
                bad_regime = True
            if not bad_regime:
                print(
                    f"[demo] OK regimes; current {bt.get('current_regime')} "
                    f"uses {bt.get('regime_strategy')}; "
                    f"crash drawdown {crash_sel['max_drawdown']} < buy-and-hold {crash_bh['max_drawdown']}"
                )

        example = bt.get("news_decision_example") or {}
        if not example.get("date") or not example.get("strategy"):
            failures.append(f"news_decision_example missing date or strategy: {example}")
            veto = {}
        else:
            veto = c.post(
                f"{STRATEGY}/signal",
                json={
                    "symbol": "SYNTH",
                    "strategy": example["strategy"],
                    "as_of_date": example["date"],
                },
            ).json()
        pp("news veto", {
            "date": veto.get("date"),
            "technical_action": veto.get("technical_action"),
            "action": veto.get("action"),
            "news_score": veto.get("news_score"),
            "size_multiplier": veto.get("size_multiplier"),
            "news_reason": veto.get("news_reason"),
        })
        if veto.get("technical_action") != "buy" or veto.get("action") != "hold":
            failures.append(
                f"expected news to change buy into hold, got {veto.get('technical_action')} -> {veto.get('action')}"
            )
        elif veto.get("news_score") is None or float(veto["news_score"]) > -0.6:
            failures.append(f"veto news_score not strongly negative: {veto.get('news_score')}")
        elif "veto" not in str(veto.get("news_reason") or "").lower():
            failures.append("news_reason does not explain the veto")
        elif veto.get("size_multiplier") is None or float(veto["size_multiplier"]) != 0.0:
            failures.append(f"veto size_multiplier should be 0, got {veto.get('size_multiplier')}")
        else:
            print(f"[demo] OK strongly negative news vetoed the {example.get('strategy')} buy")

        boost_example = bt.get("news_boost_example") or {}
        if not boost_example.get("date") or not boost_example.get("strategy"):
            failures.append(f"news_boost_example missing date or strategy: {boost_example}")
            boosted = {}
        else:
            boosted = c.post(
                f"{STRATEGY}/signal",
                json={
                    "symbol": "SYNTH",
                    "strategy": boost_example["strategy"],
                    "as_of_date": boost_example["date"],
                },
            ).json()
        if not boosted:
            pass
        elif boosted.get("technical_action") != "buy" or boosted.get("action") != "buy":
            failures.append(
                f"expected positive-news buy to stay a buy, got "
                f"{boosted.get('technical_action')} -> {boosted.get('action')}"
            )
        elif boosted.get("news_score") is None or float(boosted["news_score"]) < 0.15:
            failures.append(f"positive news_score too low: {boosted.get('news_score')}")
        elif float(boosted.get("confidence") or 0) + 1e-9 < float(boosted.get("technical_confidence") or 0):
            failures.append("positive news lowered buy confidence")
        elif round(float(boosted.get("confidence") or 0) - float(boosted.get("technical_confidence") or 0), 3) != 0.10:
            failures.append(
                f"expected confidence +0.10, got {boosted.get('technical_confidence')} -> {boosted.get('confidence')}"
            )
        elif boosted.get("size_multiplier") is None or float(boosted["size_multiplier"]) != 1.0:
            failures.append(f"positive news should keep size_multiplier 1, got {boosted.get('size_multiplier')}")
        else:
            print("[demo] OK positive news raised buy confidence by 0.10")

        live = c.post(
            f"{STRATEGY}/signal",
            json={"symbol": "SYNTH", "strategy": bt.get("selected_strategy") or "sma_crossover"},
        ).json()
        if live.get("news_score") is None or not live.get("news_reason"):
            failures.append(f"live signal missing news_score or news_reason: {live}")
        elif live.get("strategy") != bt.get("selected_strategy"):
            failures.append(
                f"live signal strategy {live.get('strategy')} != selected {bt.get('selected_strategy')}"
            )
        else:
            print("[demo] OK live signal includes news_score and news_reason")

        example = bt.get("news_decision_example") or {}
        if example.get("technical_action") != "buy" or example.get("action") == "buy" or not example.get("news_reason"):
            failures.append(f"news_decision_example did not change a buy: {example}")
        else:
            print("[demo] OK backtest reports a news decision change")

        # Cap violation demo: oversized position
        print("\n[demo] PART C - max position reject")
        price = c.get(f"{MARKET}/last-price").json()["price"]
        huge_qty = (100000 * 0.20) / price  # 20% > 5% cap
        bad = c.post(
            f"{RISK}/check-order",
            json={
                "symbol": "SYNTH",
                "side": "buy",
                "qty": huge_qty,
                "price": price,
                "equity": 100000,
                "stop_loss": price * 0.99,
                "take_profit": price * 1.05,
            },
        ).json()
        pp("oversized", bad)
        if bad.get("reason") != "max_position_pct":
            failures.append(f"expected max_position_pct, got {bad}")
        else:
            print("[demo] OK max_position_pct deny")

        # Broker refuses without risk_allowed
        try:
            r = c.post(
                f"{BROKER}/orders",
                json={"symbol": "SYNTH", "side": "buy", "qty": 1, "price": price, "risk_allowed": False},
            )
            if r.status_code != 403:
                failures.append(f"broker should 403 without risk_allowed, got {r.status_code}")
            else:
                print("[demo] OK broker 403 without risk_allowed")
        except httpx.HTTPError as e:
            failures.append(str(e))

        ents = c.get(f"{JOURNAL}/entries").json()
        pp("journal sample", {"count": ents.get("count"), "last": (ents.get("entries") or [None])[-1]})
        bodies = "\n".join((entry.get("body") or "") for entry in (ents.get("entries") or []))
        if not ents.get("entries"):
            failures.append("journal empty")
        elif "News score" not in bodies:
            failures.append("journal does not mention the news score")
        elif bt.get("selected_strategy") and bt["selected_strategy"] not in bodies:
            failures.append("journal does not name the selected strategy")
        elif "max drawdown" not in bodies.lower():
            failures.append("journal does not explain the max-drawdown comparison")
        elif "regime" not in bodies.lower():
            failures.append("journal does not mention the regime re-selection")
        else:
            print("[demo] OK journal has entries, news score, strategy pick, and regime")

        acct = c.get(f"{BROKER}/account").json()
        pp("account", acct)

    print("\n========== DEMO RESULT ==========")
    if failures:
        print("FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASSED - happy path + kill denial + risk caps + journal + backtest + news + strategy selection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
