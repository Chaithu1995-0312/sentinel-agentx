"""agent-orchestrator — tool loop calling sibling services (not a chatbot)."""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-agent-orchestrator", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CAP = int(os.getenv("LLM_DAILY_TOKEN_CAP", "50000"))
_tokens_used = 0

MARKET_URL = os.getenv("MARKET_INGEST_URL", "http://127.0.0.1:8001")
STRATEGY_URL = os.getenv("STRATEGY_ENGINE_URL", "http://127.0.0.1:8002")
RISK_URL = os.getenv("RISK_GUARD_URL", "http://127.0.0.1:8003")
BROKER_URL = os.getenv("PAPER_BROKER_URL", "http://127.0.0.1:8004")
JOURNAL_URL = os.getenv("JOURNAL_API_URL", "http://127.0.0.1:8006")

MAX_POS = float(os.getenv("RISK_MAX_POSITION_PCT", "0.05"))
STOP_PCT = float(os.getenv("RISK_STOP_LOSS_PCT", "0.02"))
MIN_RR = float(os.getenv("RISK_MIN_RR", "1.5"))


class RunRequest(BaseModel):
    goal: str = "paper_cycle"
    symbol: str = "SYNTH"
    dry_run: bool = False
    force_qty: float | None = None


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0)


def tool_get_market(client: httpx.Client, symbol: str) -> dict[str, Any]:
    bars = client.get(f"{MARKET_URL}/bars", params={"limit": 60, "symbol": symbol}).json()
    news = client.get(f"{MARKET_URL}/news", params={"limit": 5}).json()
    last = bars["bars"][-1] if bars.get("bars") else None
    return {"bars_count": bars.get("count"), "last": last, "news": news.get("items", [])}


def tool_run_backtest(client: httpx.Client, symbol: str) -> dict[str, Any]:
    return client.post(f"{STRATEGY_URL}/backtest", json={"symbol": symbol}).json()


def tool_signal(client: httpx.Client, symbol: str) -> dict[str, Any]:
    return client.post(f"{STRATEGY_URL}/signal", json={"symbol": symbol}).json()


def tool_kill(client: httpx.Client) -> dict[str, Any]:
    return client.get(f"{RISK_URL}/kill-switch").json()


def tool_account(client: httpx.Client) -> dict[str, Any]:
    return client.get(f"{BROKER_URL}/account").json()


def tool_check_risk(client: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
    return client.post(f"{RISK_URL}/check-order", json=payload).json()


def tool_place_order(client: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
    return client.post(f"{BROKER_URL}/orders", json=payload).json()


def tool_journal(client: httpx.Client, payload: dict[str, Any]) -> dict[str, Any]:
    return client.post(f"{JOURNAL_URL}/trade", json=payload).json()


@app.get("/health")
def health():
    return {
        "service": "agent-orchestrator",
        "status": "ok",
        "mode": "tool_loop",
        "llm_provider": os.getenv("LLM_PROVIDER", "none"),
        "tokens_used": _tokens_used,
        "token_cap": CAP,
        "urls": {
            "market": MARKET_URL,
            "strategy": STRATEGY_URL,
            "risk": RISK_URL,
            "broker": BROKER_URL,
            "journal": JOURNAL_URL,
        },
    }


@app.post("/run")
def run(body: RunRequest):
    """Deterministic tool loop: market → backtest → signal → risk → order → journal."""
    steps: list[dict[str, Any]] = []
    symbol = body.symbol

    with _client() as client:
        # 0. Kill switch early
        kill = tool_kill(client)
        steps.append({"tool": "get_kill_switch", "result": kill})
        if kill.get("killed") and body.goal in ("paper_cycle", "trade", "status_and_trade"):
            # Still gather market + backtest for transparency, but skip order
            market = tool_get_market(client, symbol)
            steps.append({"tool": "get_market", "result": {"last": market.get("last")}})
            bt = tool_run_backtest(client, symbol)
            steps.append(
                {
                    "tool": "run_backtest",
                    "result": {
                        "strategy_return": bt.get("strategy_return"),
                        "buy_hold_return": bt.get("buy_hold_return"),
                        "summary": bt.get("summary"),
                    },
                }
            )
            j = tool_journal(
                client,
                {
                    "title": "Blocked by kill switch",
                    "symbol": symbol,
                    "action": "hold",
                    "allowed": False,
                    "risk_reason": "kill_switch",
                    "reason": "Human kill switch is ON; agent refused to trade.",
                    "vs_buy_hold": bt.get("summary"),
                    "tags": ["kill_switch", "blocked"],
                },
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})
            return {
                "goal": body.goal,
                "status": "blocked_kill_switch",
                "steps": steps,
                "order": None,
            }

        # 1. Market
        market = tool_get_market(client, symbol)
        steps.append({"tool": "get_market", "result": {"last": market.get("last"), "news_n": len(market.get("news", []))}})

        # 2. Backtest vs buy-and-hold
        bt = tool_run_backtest(client, symbol)
        steps.append(
            {
                "tool": "run_backtest",
                "result": {
                    "strategy_return": bt.get("strategy_return"),
                    "buy_hold_return": bt.get("buy_hold_return"),
                    "summary": bt.get("summary"),
                },
            }
        )

        if body.goal == "backtest_only":
            tool_journal(
                client,
                {
                    "title": "Backtest report",
                    "symbol": symbol,
                    "action": "analyze",
                    "reason": bt.get("summary"),
                    "vs_buy_hold": bt.get("summary"),
                    "tags": ["backtest"],
                },
            )
            return {"goal": body.goal, "status": "ok", "backtest": bt, "steps": steps}

        # 3. Signal
        sig = tool_signal(client, symbol)
        steps.append({"tool": "get_signal", "result": sig})

        if sig.get("action") not in ("buy", "sell"):
            j = tool_journal(
                client,
                {
                    "title": "Hold — no crossover",
                    "symbol": symbol,
                    "action": "hold",
                    "price": sig.get("price"),
                    "reason": sig.get("reason", "hold"),
                    "vs_buy_hold": bt.get("summary"),
                    "tags": ["hold"],
                },
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id")}})
            return {"goal": body.goal, "status": "hold", "signal": sig, "backtest_summary": bt.get("summary"), "steps": steps}

        account = tool_account(client)
        steps.append({"tool": "get_account", "result": account})
        equity = float(account.get("equity") or account.get("cash") or 100000)
        price = float(sig["price"])
        action = sig["action"]

        # Position sizing: max MAX_POS of equity
        max_notional = equity * MAX_POS
        qty = body.force_qty if body.force_qty is not None else max(1.0, round(max_notional / price, 4))
        if action == "sell":
            pos = (account.get("positions") or {}).get(symbol) or {}
            have = float(pos.get("qty") or 0)
            if have <= 0:
                j = tool_journal(
                    client,
                    {
                        "title": "Sell signal but flat",
                        "symbol": symbol,
                        "action": "hold",
                        "reason": "Sell signal with no open position",
                        "signal_reason": sig.get("reason"),
                        "vs_buy_hold": bt.get("summary"),
                        "tags": ["hold"],
                    },
                )
                steps.append({"tool": "journal", "result": {"id": j.get("id")}})
                return {"goal": body.goal, "status": "hold_flat", "signal": sig, "steps": steps}
            qty = min(qty, have)

        stop_loss = None
        take_profit = None
        if action == "buy":
            stop_loss = round(price * (1 - STOP_PCT), 4)
            take_profit = round(price * (1 + STOP_PCT * MIN_RR), 4)

        open_notional = 0.0
        for p in (account.get("positions") or {}).values():
            open_notional += float(p.get("qty", 0)) * float(p.get("avg_price", 0))

        risk_payload = {
            "symbol": symbol,
            "side": action,
            "qty": qty,
            "price": price,
            "equity": equity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "open_notional": open_notional,
            "daily_pnl": float(account.get("realized_pnl") or 0),
        }
        risk = tool_check_risk(client, risk_payload)
        steps.append({"tool": "check_risk", "result": risk})

        if not risk.get("allowed"):
            j = tool_journal(
                client,
                {
                    "title": f"Blocked: {risk.get('reason')}",
                    "symbol": symbol,
                    "side": action,
                    "action": action,
                    "qty": qty,
                    "price": price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "allowed": False,
                    "risk_reason": risk.get("detail") or risk.get("reason"),
                    "signal_reason": sig.get("reason"),
                    "vs_buy_hold": bt.get("summary"),
                    "tags": ["blocked", str(risk.get("reason"))],
                },
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})
            return {
                "goal": body.goal,
                "status": "risk_denied",
                "risk": risk,
                "signal": sig,
                "steps": steps,
                "order": None,
            }

        if body.dry_run:
            j = tool_journal(
                client,
                {
                    "title": f"Dry-run {action}",
                    "symbol": symbol,
                    "side": action,
                    "action": action,
                    "qty": qty,
                    "price": price,
                    "allowed": True,
                    "risk_reason": "ok (dry_run)",
                    "signal_reason": sig.get("reason"),
                    "vs_buy_hold": bt.get("summary"),
                    "tags": ["dry_run"],
                },
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id")}})
            return {
                "goal": body.goal,
                "status": "dry_run",
                "would_order": risk_payload,
                "steps": steps,
            }

        order_payload = {
            "symbol": symbol,
            "side": action,
            "qty": qty,
            "price": price,
            "risk_allowed": True,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "reason": sig.get("reason"),
        }
        try:
            fill = tool_place_order(client, order_payload)
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, e.response.text) from e
        steps.append({"tool": "place_order", "result": fill})

        j = tool_journal(
            client,
            {
                "title": f"Paper {action} filled",
                "symbol": symbol,
                "side": action,
                "action": action,
                "qty": qty,
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "allowed": True,
                "risk_reason": "ok",
                "signal_reason": sig.get("reason"),
                "reason": sig.get("reason"),
                "vs_buy_hold": bt.get("summary"),
                "tags": ["fill", "paper"],
                "extra": {"fill_id": (fill.get("fill") or {}).get("fill_id")},
            },
        )
        steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})

        return {
            "goal": body.goal,
            "status": "filled",
            "signal": sig,
            "risk": risk,
            "order": fill,
            "backtest_summary": bt.get("summary"),
            "steps": steps,
            "tokens_used": _tokens_used,
            "token_cap": CAP,
        }
