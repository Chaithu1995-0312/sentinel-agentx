"""agent-orchestrator — tool loop calling sibling services (not a chatbot)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="sentinel-agent-orchestrator", version="0.2.0")
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
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from shared.selection import apply_hysteresis  # noqa: E402

SWITCH_MARGIN = float(os.getenv("SWITCH_MARGIN", "0.02"))
CHOICE_PATH = _ROOT / "data" / "strategy_choice.json"

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
    r = client.post(f"{STRATEGY_URL}/backtest", json={"symbol": symbol})
    r.raise_for_status()
    return r.json()


def tool_signal(client: httpx.Client, symbol: str, strategy: str) -> dict[str, Any]:
    r = client.post(
        f"{STRATEGY_URL}/signal",
        json={"symbol": symbol, "strategy": strategy},
    )
    r.raise_for_status()
    return r.json()


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


def tool_evaluate_exits(client: httpx.Client, symbol: str, price: float, dry_run: bool) -> dict[str, Any]:
    r = client.post(
        f"{BROKER_URL}/evaluate-exits",
        json={"symbol": symbol, "price": price, "dry_run": dry_run},
    )
    r.raise_for_status()
    return r.json()


def tool_daily_pnl(client: httpx.Client, symbol: str, price: float) -> dict[str, Any]:
    r = client.get(f"{BROKER_URL}/daily-pnl", params={"symbol": symbol, "price": price})
    r.raise_for_status()
    return r.json()


TRADE_GOALS = ("paper_cycle", "trade", "status_and_trade")


def _exit_journal(fill: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    trigger = fill.get("reason")
    price = fill.get("price")
    entry = fill.get("entry_price")
    pnl = float(fill.get("realized_pnl") or 0.0)
    if trigger == "stop_loss":
        title = "Stop-loss hit - position closed"
        why = (
            f"Price {price} fell to or below the stop-loss {fill.get('stop_loss')} "
            f"set when we bought at about {entry}"
        )
    else:
        title = "Take-profit hit - position closed"
        why = (
            f"Price {price} rose to or above the take-profit {fill.get('take_profit')} "
            f"set when we bought at about {entry}"
        )
    verb = "would close" if dry_run else "closed"
    if dry_run:
        title = f"Dry-run: {title}"
    return {
        "title": title,
        "symbol": fill.get("symbol"),
        "side": "sell",
        "action": "close the position",
        "qty": fill.get("qty"),
        "price": price,
        "stop_loss": fill.get("stop_loss"),
        "take_profit": fill.get("take_profit"),
        "reason": (
            f"{why}, so the agent {verb} the whole position on paper. "
            f"Paper P&L on this position {pnl:+.2f}. Exits only reduce risk, so they run "
            "even when the kill switch is ON and do not count toward the 10 trades a day"
        ),
        "tags": ["exit", str(trigger), "paper"] + (["dry_run"] if dry_run else []),
        "extra": {"fill_id": fill.get("fill_id"), "trigger": trigger},
    }


def _process_exits(client: httpx.Client, symbol: str, dry_run: bool, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Before any new signal: close open positions whose stop or take-profit was reached."""
    market = tool_get_market(client, symbol)
    last = market.get("last") or {}
    price = last.get("close")
    if not price:
        steps.append({"tool": "evaluate_exits", "result": {"skipped": "no price"}})
        return []
    res = tool_evaluate_exits(client, symbol, float(price), dry_run)
    exits = list(res.get("exits") or [])
    steps.append({"tool": "evaluate_exits", "result": {"price": float(price), "exits": exits}})
    for fill in exits:
        j = tool_journal(client, _exit_journal(fill, dry_run))
        steps.append({"tool": "journal_exit", "result": {"id": j.get("id"), "body": j.get("body")}})
    return exits


def _trade_phrase(n: int) -> str:
    return f"{n} trade" if int(n) == 1 else f"{n} trades"


def _load_choice() -> str | None:
    try:
        return str(json.loads(CHOICE_PATH.read_text()).get("strategy") or "") or None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_choice(name: str) -> None:
    CHOICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHOICE_PATH.write_text(json.dumps({"strategy": name}))


def _net_score(row: dict[str, Any], penalty: float) -> float:
    if row.get("net_score") is not None:
        return float(row["net_score"])
    if row.get("score") is not None:
        return float(row["score"])
    return float(row["return"]) - penalty * float(row["max_drawdown"])


def pick_strategy(bt: dict[str, Any]) -> tuple[str, str]:
    """Pick the trading strategy with the best return-minus-drawdown score.

    score = return - drawdown_penalty * max_drawdown. Buy-and-hold is the
    benchmark in the report and is not eligible.
    """
    penalty = float(bt.get("drawdown_penalty", 0.5))
    rows = list(bt.get("strategies") or [])
    if not rows:
        raise HTTPException(502, "backtest returned no strategies")

    def sort_key(row: dict[str, Any]) -> tuple:
        score = _net_score(row, penalty)
        return (-score, -float(row["return"]), int(row["trades"]), str(row["name"]))

    ranked = sorted(rows, key=sort_key)
    best = ranked[0]
    if bt.get("selected_strategy") == "cash":
        return "cash", str(bt.get("selection_reason") or "Net score is negative, so the book stays in cash.")
    if best["name"] == bt.get("selected_strategy") and bt.get("selection_reason"):
        return str(best["name"]), str(bt["selection_reason"])

    def _score(row: dict[str, Any]) -> float:
        return round(float(row["return"]) - penalty * float(row["max_drawdown"]), 6)

    parts = [
        (
            f"Selected {best['name']} because its score {_score(best):.4f} "
            f"is the best trading strategy. "
            f"Score = return - {penalty:g} * max drawdown."
        ),
        (
            f"{best['name']} return {float(best['return']) * 100:.2f}%, "
            f"max drawdown {float(best['max_drawdown']) * 100:.2f}%, "
            f"{_trade_phrase(best['trades'])}."
        ),
    ]
    for other in ranked[1:]:
        parts.append(
            f"{other['name']} score {_score(other):.4f} "
            f"(return {float(other['return']) * 100:.2f}%, "
            f"max drawdown {float(other['max_drawdown']) * 100:.2f}%, "
            f"{_trade_phrase(other['trades'])})."
        )
    bh = bt.get("buy_hold") or {}
    if bh:
        parts.append(
            f"Buy-and-hold return {float(bh['return']) * 100:.2f}%, "
            f"max drawdown {float(bh['max_drawdown']) * 100:.2f}%, "
            f"{_trade_phrase(bh['trades'])}. "
            "Buy-and-hold is the benchmark, not a candidate the agent can switch into."
        )
    return str(best["name"]), " ".join(parts)


def pick_live_strategy(bt: dict[str, Any]) -> tuple[str, str]:
    """Re-select inside the current mock regime with the same score. Falls back to the full-sample winner."""
    penalty = float(bt.get("drawdown_penalty", 0.5))
    current = bt.get("current_regime")
    match = next((reg for reg in bt.get("regimes") or [] if reg.get("name") == current), None)
    rows = list((match or {}).get("strategies") or [])
    fallback = str(bt.get("selected_strategy") or bt.get("regime_strategy") or "")
    if not match or not rows:
        return fallback, ""

    def sort_key(row: dict[str, Any]) -> tuple:
        score = _net_score(row, penalty)
        return (-score, -float(row["return"]), int(row["trades"]), str(row["name"]))

    ranked = sorted(rows, key=sort_key)
    best = ranked[0]
    score = _net_score(best, penalty)
    bh = match.get("buy_hold") or {}
    raw_name = "cash" if score < 0 else str(best["name"])
    choice, hyst = apply_hysteresis(ranked, _load_choice(), float(bt.get("switch_margin") or SWITCH_MARGIN))
    _save_choice(choice)
    note = (
        f"Current mock regime {current} ({match.get('start')} to {match.get('end')}) "
        f"ranks {raw_name} first on net score {score:.4f} "
        f"(net return {float(best['return']) * 100:.2f}%, "
        f"gross {float(best.get('gross_return', best['return'])) * 100:.2f}%, "
        f"net max drawdown {float(best['max_drawdown']) * 100:.2f}%, "
        f"{_trade_phrase(best['trades'])}). "
        f"{hyst} "
        f"Buy-and-hold in this regime net return {float(bh.get('return') or 0) * 100:.2f}% "
        f"with net max drawdown {float(bh.get('max_drawdown') or 0) * 100:.2f}%."
    )
    return choice, note


def _bt_brief(bt: dict[str, Any], chosen: str) -> dict[str, Any]:
    return {
        "selected_strategy": chosen,
        "strategy_return": bt.get("strategy_return"),
        "buy_hold_return": bt.get("buy_hold_return"),
        "summary": bt.get("summary"),
        "strategies": [
            {
                "name": row.get("name"),
                "return": row.get("return"),
                "max_drawdown": row.get("max_drawdown"),
                "trades": row.get("trades"),
                "score": row.get("score"),
            }
            for row in bt.get("strategies") or []
        ],
        "buy_hold": {
            "return": (bt.get("buy_hold") or {}).get("return", bt.get("buy_hold_return")),
            "max_drawdown": (bt.get("buy_hold") or {}).get(
                "max_drawdown", bt.get("buy_hold_max_drawdown")
            ),
            "trades": (bt.get("buy_hold") or {}).get("trades", bt.get("buy_hold_trades")),
        },
        "current_regime": bt.get("current_regime"),
        "regime_strategy": bt.get("regime_strategy"),
        "regimes": [
            {
                "name": reg.get("name"),
                "start": reg.get("start"),
                "end": reg.get("end"),
                "selected_strategy": reg.get("selected_strategy"),
                "strategies": [
                    {
                        "name": row.get("name"),
                        "return": row.get("return"),
                        "max_drawdown": row.get("max_drawdown"),
                        "trades": row.get("trades"),
                        "score": row.get("score"),
                    }
                    for row in reg.get("strategies") or []
                ],
                "buy_hold": {
                    "return": (reg.get("buy_hold") or {}).get("return"),
                    "max_drawdown": (reg.get("buy_hold") or {}).get("max_drawdown"),
                    "trades": (reg.get("buy_hold") or {}).get("trades"),
                },
            }
            for reg in bt.get("regimes") or []
        ],
    }


def _journal_payload(
    payload: dict[str, Any],
    sig: dict[str, Any] | None,
    chosen: str,
    reason: str,
) -> dict[str, Any]:
    body = dict(payload)
    body.setdefault("strategy", chosen)
    body.setdefault("selection_reason", reason)
    if sig is not None:
        if sig.get("news_score") is not None:
            body.setdefault("news_score", sig.get("news_score"))
        if sig.get("news_reason"):
            body.setdefault("news_reason", sig.get("news_reason"))
        body.setdefault("signal_reason", sig.get("reason"))
    return body


def _sized_qty(equity: float, price: float, mult: float, force_qty: float | None) -> float:
    if force_qty is not None:
        return force_qty
    raw = (equity * MAX_POS * max(mult, 0.0)) / price if price else 0.0
    if mult < 1:
        return round(max(raw, 0.0001), 4)
    return max(1.0, round(raw, 4))


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
    """Deterministic tool loop: exits -> market -> backtest -> select -> signal -> risk -> order -> journal."""
    result = _run_cycle(body)
    exits: list[dict[str, Any]] = []
    for step in result.get("steps") or []:
        if step.get("tool") == "evaluate_exits":
            exits.extend((step.get("result") or {}).get("exits") or [])
    result["exits"] = exits
    return result


def _run_cycle(body: RunRequest) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    symbol = body.symbol

    with _client() as client:
        kill = tool_kill(client)
        steps.append({"tool": "get_kill_switch", "result": kill})
        if body.goal in TRADE_GOALS:
            # Risk-reducing exits run first, even with the kill switch ON.
            _process_exits(client, symbol, body.dry_run, steps)
        if kill.get("killed") and body.goal in TRADE_GOALS:
            market = tool_get_market(client, symbol)
            steps.append({"tool": "get_market", "result": {"last": market.get("last")}})
            bt = tool_run_backtest(client, symbol)
            chosen, selection_reason = pick_strategy(bt)
            live_strategy, regime_note = pick_live_strategy(bt)
            journal_reason = f"{selection_reason} {regime_note}".strip()
            steps.append({"tool": "run_backtest", "result": _bt_brief(bt, chosen)})
            steps.append(
                {
                    "tool": "select_strategy",
                    "result": {
                        "strategy": chosen,
                        "reason": selection_reason,
                        "regime_strategy": live_strategy,
                        "current_regime": bt.get("current_regime"),
                    },
                }
            )
            j = tool_journal(
                client,
                _journal_payload(
                    {
                        "title": "Blocked by kill switch",
                        "symbol": symbol,
                        "action": "hold",
                        "allowed": False,
                        "risk_reason": "kill_switch",
                        "reason": "Human kill switch is ON; agent refused to trade.",
                        "vs_buy_hold": bt.get("summary"),
                        "tags": ["kill_switch", "blocked", chosen],
                    },
                    None,
                    chosen,
                    journal_reason,
                ),
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})
            return {
                "goal": body.goal,
                "status": "blocked_kill_switch",
                "selected_strategy": chosen,
                "selection_reason": selection_reason,
                "regime_strategy": live_strategy,
                "current_regime": bt.get("current_regime"),
                "steps": steps,
                "order": None,
            }

        market = tool_get_market(client, symbol)
        steps.append(
            {
                "tool": "get_market",
                "result": {"last": market.get("last"), "news_n": len(market.get("news", []))},
            }
        )

        bt = tool_run_backtest(client, symbol)
        chosen, selection_reason = pick_strategy(bt)
        live_strategy, regime_note = pick_live_strategy(bt)
        journal_reason = f"{selection_reason} {regime_note}".strip()
        steps.append({"tool": "run_backtest", "result": _bt_brief(bt, chosen)})
        steps.append(
            {
                "tool": "select_strategy",
                "result": {
                    "strategy": chosen,
                    "reason": selection_reason,
                    "regime_strategy": live_strategy,
                    "current_regime": bt.get("current_regime"),
                },
            }
        )
        sel = tool_journal(
            client,
            _journal_payload(
                {
                    "title": f"Selected {chosen}",
                    "symbol": symbol,
                    "action": "compare strategies",
                    "reason": journal_reason,
                    "vs_buy_hold": bt.get("summary"),
                    "tags": ["strategy_selection", chosen, live_strategy],
                },
                None,
                chosen,
                journal_reason,
            ),
        )
        steps.append({"tool": "journal_selection", "result": {"id": sel.get("id")}})

        if body.goal == "backtest_only":
            return {
                "goal": body.goal,
                "status": "ok",
                "backtest": bt,
                "selected_strategy": chosen,
                "selection_reason": selection_reason,
                "regime_strategy": live_strategy,
                "current_regime": bt.get("current_regime"),
                "steps": steps,
            }

        sig = tool_signal(client, symbol, live_strategy)
        steps.append({"tool": "get_signal", "result": sig})

        if sig.get("action") not in ("buy", "sell"):
            title = (
                "Hold - news vetoed the buy"
                if sig.get("news_changed_decision") and sig.get("technical_action") == "buy"
                else "Hold - no trade signal"
            )
            j = tool_journal(
                client,
                _journal_payload(
                    {
                        "title": title,
                        "symbol": symbol,
                        "action": "hold",
                        "price": sig.get("price"),
                        "reason": sig.get("reason", "hold"),
                        "vs_buy_hold": bt.get("summary"),
                        "tags": ["hold", chosen],
                    },
                    sig,
                    live_strategy,
                    journal_reason,
                ),
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})
            return {
                "goal": body.goal,
                "status": "hold",
                "signal": sig,
                "selected_strategy": chosen,
                "selection_reason": selection_reason,
                "regime_strategy": live_strategy,
                "current_regime": bt.get("current_regime"),
                "backtest_summary": bt.get("summary"),
                "steps": steps,
            }

        account = tool_account(client)
        steps.append({"tool": "get_account", "result": account})
        equity = float(account.get("equity") or account.get("cash") or 100000)
        price = float(sig["price"])
        action = sig["action"]
        mult = 1.0 if sig.get("size_multiplier") is None else float(sig["size_multiplier"])

        qty = _sized_qty(equity, price, mult, body.force_qty)
        if action == "sell":
            pos = (account.get("positions") or {}).get(symbol) or {}
            have = float(pos.get("qty") or 0)
            if have <= 0:
                j = tool_journal(
                    client,
                    _journal_payload(
                        {
                            "title": "Sell signal but flat",
                            "symbol": symbol,
                            "action": "hold",
                            "reason": "Sell signal with no open position",
                            "signal_reason": sig.get("reason"),
                            "vs_buy_hold": bt.get("summary"),
                            "tags": ["hold", chosen],
                        },
                        sig,
                        live_strategy,
                        journal_reason,
                    ),
                )
                steps.append({"tool": "journal", "result": {"id": j.get("id")}})
                return {
                    "goal": body.goal,
                    "status": "hold_flat",
                    "signal": sig,
                    "selected_strategy": chosen,
                    "selection_reason": selection_reason,
                    "regime_strategy": live_strategy,
                    "current_regime": bt.get("current_regime"),
                    "steps": steps,
                }
            qty = min(qty, have)

        stop_loss = None
        take_profit = None
        if action == "buy":
            stop_loss = round(price * (1 - STOP_PCT), 4)
            take_profit = round(price * (1 + STOP_PCT * MIN_RR), 4)

        open_notional = 0.0
        for p in (account.get("positions") or {}).values():
            open_notional += float(p.get("qty", 0)) * float(p.get("avg_price", 0))

        # True daily loss: today's realized P&L + unrealized on open positions,
        # against start-of-day equity. If the broker can't answer, risk-guard
        # falls back to its own date-reset realized P&L for today.
        daily_pnl = None
        day_start_equity = None
        try:
            daily = tool_daily_pnl(client, symbol, price)
            steps.append({"tool": "get_daily_pnl", "result": daily})
            daily_pnl = float(daily["daily_pnl"])
            day_start_equity = float(daily["day_start_equity"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            steps.append({"tool": "get_daily_pnl", "result": {"error": "unavailable"}})

        risk_payload = {
            "symbol": symbol,
            "side": action,
            "qty": qty,
            "price": price,
            "equity": equity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "open_notional": open_notional,
            "daily_pnl": daily_pnl,
            "day_start_equity": day_start_equity,
        }
        risk = tool_check_risk(client, risk_payload)
        steps.append({"tool": "check_risk", "result": risk})

        if not risk.get("allowed"):
            j = tool_journal(
                client,
                _journal_payload(
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
                        "tags": ["blocked", str(risk.get("reason")), chosen],
                    },
                    sig,
                    live_strategy,
                    journal_reason,
                ),
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})
            return {
                "goal": body.goal,
                "status": "risk_denied",
                "risk": risk,
                "signal": sig,
                "selected_strategy": chosen,
                "selection_reason": selection_reason,
                "regime_strategy": live_strategy,
                "current_regime": bt.get("current_regime"),
                "steps": steps,
                "order": None,
            }

        if body.dry_run:
            j = tool_journal(
                client,
                _journal_payload(
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
                        "tags": ["dry_run", chosen],
                    },
                    sig,
                    live_strategy,
                    journal_reason,
                ),
            )
            steps.append({"tool": "journal", "result": {"id": j.get("id")}})
            return {
                "goal": body.goal,
                "status": "dry_run",
                "would_order": risk_payload,
                "signal": sig,
                "selected_strategy": chosen,
                "selection_reason": selection_reason,
                "regime_strategy": live_strategy,
                "current_regime": bt.get("current_regime"),
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
            _journal_payload(
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
                    "tags": ["fill", "paper", chosen],
                    "extra": {"fill_id": (fill.get("fill") or {}).get("fill_id")},
                },
                sig,
                live_strategy,
                journal_reason,
            ),
        )
        steps.append({"tool": "journal", "result": {"id": j.get("id"), "body": j.get("body")}})

        return {
            "goal": body.goal,
            "status": "filled",
            "signal": sig,
            "risk": risk,
            "order": fill,
            "selected_strategy": chosen,
            "selection_reason": selection_reason,
            "regime_strategy": live_strategy,
            "current_regime": bt.get("current_regime"),
            "backtest_summary": bt.get("summary"),
            "steps": steps,
            "tokens_used": _tokens_used,
            "token_cap": CAP,
        }
