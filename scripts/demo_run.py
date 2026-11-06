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
    raise SystemExit("[demo] services not ready — start with: python scripts/run_all.py")


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

        # --- Part A: kill ON → deny ---
        print("\n[demo] PART A — kill switch ON must deny")
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
        print("\n[demo] PART B — clear kill, run happy path")
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
            print("[demo] signal was hold — placing controlled paper buy via risk→broker")
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
            # Still ok if limits blocked — prove kill cleared path worked earlier
            print("[demo] WARN agent risk_denied (limits) — kill path already proven")
        else:
            failures.append(f"unexpected agent status {run.get('status')}")

        # Backtest shape
        bt = c.post(f"{STRATEGY}/backtest", json={"symbol": "SYNTH"}).json()
        pp("backtest", {
            "strategy_return": bt.get("strategy_return"),
            "buy_hold_return": bt.get("buy_hold_return"),
            "summary": bt.get("summary"),
        })
        if bt.get("strategy_return") is None or bt.get("buy_hold_return") is None:
            failures.append("backtest missing returns")
        else:
            print("[demo] OK backtest vs buy-and-hold")

        # Cap violation demo: oversized position
        print("\n[demo] PART C — max position reject")
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
        if not ents.get("entries"):
            failures.append("journal empty")
        else:
            print("[demo] OK journal has entries")

        acct = c.get(f"{BROKER}/account").json()
        pp("account", acct)

    print("\n========== DEMO RESULT ==========")
    if failures:
        print("FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASSED — happy path + kill denial + risk caps + journal + backtest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
