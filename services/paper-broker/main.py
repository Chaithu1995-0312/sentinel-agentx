"""paper-broker — simulated fills only. Requires risk_allowed. Tracks cash/positions/PnL."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-paper-broker", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BUILD_ROOT = Path(__file__).resolve().parents[2]
STARTING_CASH = float(os.getenv("PAPER_STARTING_CASH", "100000"))
RISK_URL = os.getenv("RISK_GUARD_URL", "http://127.0.0.1:8003")
STATE_PATH = Path(os.getenv("BROKER_STATE_PATH", str(BUILD_ROOT / "data" / "broker_state.json")))
if not STATE_PATH.is_absolute():
    STATE_PATH = (BUILD_ROOT / STATE_PATH).resolve()


def _today() -> str:
    """Local calendar date of this machine. The daily loss window resets on it."""
    return date.today().isoformat()


def _fill_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).date().isoformat()


def _empty_state() -> dict[str, Any]:
    return {
        "cash": STARTING_CASH,
        "positions": {},  # symbol -> {qty, avg_price, stop_loss?, take_profit?}
        "fills": [],
        "realized_pnl": 0.0,
        "day": _today(),
        "day_start_equity": STARTING_CASH,
    }


def _roll_day(state: dict[str, Any]) -> bool:
    """On the first touch of a new local date, snapshot start-of-day equity.

    Start-of-day equity = cash + open positions at cost (the broker has no
    market feed of its own). Returns True if the state changed.
    """
    if state.get("day") == _today() and state.get("day_start_equity") is not None:
        return False
    state["day"] = _today()
    state["day_start_equity"] = round(_equity(state), 4)
    return True


def _load() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        state = _empty_state()
        _save(state)
        return state
    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        state = _empty_state()
        _save(state)
        return state
    if _roll_day(state):
        _save(state)
    return state


def _save(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _equity(state: dict[str, Any], marks: dict[str, float] | None = None) -> float:
    eq = float(state["cash"])
    for sym, pos in state.get("positions", {}).items():
        qty = float(pos["qty"])
        px = (marks or {}).get(sym, float(pos["avg_price"]))
        eq += qty * px
    return eq


class OrderRequest(BaseModel):
    symbol: str
    side: str
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    risk_allowed: bool = False
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str | None = None


class ExitCheck(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    dry_run: bool = False


def _record_trade(realized: float, count_trade: bool) -> None:
    """Tell risk-guard about a fill (best-effort)."""
    try:
        with httpx.Client(timeout=3.0) as client:
            client.post(
                f"{RISK_URL}/record-trade",
                json={"realized_pnl": realized, "count_trade": count_trade},
            )
    except httpx.HTTPError:
        pass


def _daily(state: dict[str, Any], marks: dict[str, float]) -> dict[str, Any]:
    """Today's (local date) realized P&L + current unrealized P&L on open positions."""
    today = _today()
    realized_today = sum(
        float(f.get("realized_pnl") or 0.0)
        for f in state.get("fills", [])
        if f.get("ts") is not None and _fill_day(f["ts"]) == today
    )
    carry = state.get("reset_carry") or {}
    if carry.get("day") == today:
        realized_today += float(carry.get("realized") or 0.0)
    unrealized = 0.0
    for sym, pos in state.get("positions", {}).items():
        px = marks.get(sym)
        if px is None:
            continue  # no mark for this symbol: counted at cost (0 unrealized)
        unrealized += (float(px) - float(pos["avg_price"])) * float(pos["qty"])
    return {
        "day": today,
        "day_start_equity": float(state.get("day_start_equity") or STARTING_CASH),
        "realized_today": round(realized_today, 4),
        "unrealized": round(unrealized, 4),
        "daily_pnl": round(realized_today + unrealized, 4),
    }


@app.on_event("startup")
def startup() -> None:
    _load()


@app.get("/health")
def health():
    state = _load()
    return {
        "service": "paper-broker",
        "status": "ok",
        "cash": state["cash"],
        "fills": len(state["fills"]),
        "realized_pnl": state["realized_pnl"],
        "equity": _equity(state),
    }


@app.get("/account")
def account():
    state = _load()
    return {
        "cash": state["cash"],
        "positions": state["positions"],
        "realized_pnl": state["realized_pnl"],
        "equity": _equity(state),
        "fills_count": len(state["fills"]),
    }


@app.get("/fills")
def fills(limit: int = 50):
    state = _load()
    return {"fills": state["fills"][-limit:]}


@app.get("/daily-pnl")
def daily_pnl(symbol: str | None = None, price: float | None = None):
    """Daily loss input for risk-guard. Optional mark: ?symbol=SYNTH&price=126.3."""
    state = _load()
    marks = {symbol: float(price)} if symbol and price else {}
    return _daily(state, marks)


@app.post("/evaluate-exits")
def evaluate_exits(body: ExitCheck):
    """Close an open long when the price reaches its stop-loss or take-profit.

    Exits only reduce risk, so they are allowed while the kill switch is ON and
    are not counted against the daily trade cap. Paper fills at the given price.
    """
    state = _load()
    pos = state.get("positions", {}).get(body.symbol)
    if not pos:
        return {"symbol": body.symbol, "price": body.price, "exits": [], "position": None}
    stop = pos.get("stop_loss")
    tp = pos.get("take_profit")
    trigger = None
    if stop is not None and body.price <= float(stop):
        trigger = "stop_loss"
    elif tp is not None and body.price >= float(tp):
        trigger = "take_profit"
    if trigger is None:
        return {"symbol": body.symbol, "price": body.price, "exits": [], "position": pos}

    qty = float(pos["qty"])
    avg = float(pos["avg_price"])
    level = float(stop if trigger == "stop_loss" else tp)
    fill = {
        "fill_id": str(uuid.uuid4()),
        "symbol": body.symbol,
        "side": "sell",
        "qty": qty,
        "price": body.price,
        "notional": round(qty * body.price, 2),
        "realized_pnl": round((body.price - avg) * qty, 4),
        "stop_loss": stop,
        "take_profit": tp,
        "reason": trigger,
        "trigger_level": level,
        "entry_price": avg,
        "ts": time.time(),
        "venue": "paper",
    }
    if body.dry_run:
        return {"symbol": body.symbol, "price": body.price, "exits": [fill], "dry_run": True, "position": pos}

    state["cash"] = round(state["cash"] + qty * body.price, 4)
    state["realized_pnl"] = round(float(state["realized_pnl"]) + fill["realized_pnl"], 4)
    state["positions"].pop(body.symbol, None)
    state["fills"].append(fill)
    _save(state)
    _record_trade(fill["realized_pnl"], count_trade=False)
    return {"symbol": body.symbol, "price": body.price, "exits": [fill], "position": None}


@app.post("/reset")
def reset():
    # A paper reset must not erase today's realized loss from the daily limit.
    old = _load()
    carry = _daily(old, {})["realized_today"]
    state = _empty_state()
    if carry:
        state["reset_carry"] = {"day": _today(), "realized": carry}
    _save(state)
    return {"status": "reset", "cash": state["cash"]}


@app.post("/orders")
def place_order(body: OrderRequest):
    if not body.risk_allowed:
        raise HTTPException(403, "risk_allowed=false — refuse order (paper)")

    # Defense in depth: re-check kill switch at broker
    try:
        with httpx.Client(timeout=3.0) as client:
            ks = client.get(f"{RISK_URL}/kill-switch").json()
            if ks.get("killed"):
                raise HTTPException(403, "kill_switch ON — broker refuse")
    except httpx.HTTPError:
        # Fail-closed if risk-guard unreachable
        raise HTTPException(503, "risk-guard unreachable — fail-closed")

    state = _load()
    side = body.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy|sell")

    notional = body.qty * body.price
    realized = 0.0
    positions = state.setdefault("positions", {})

    if side == "buy":
        if state["cash"] < notional:
            raise HTTPException(400, "insufficient_cash")
        state["cash"] = round(state["cash"] - notional, 4)
        pos = positions.get(body.symbol, {"qty": 0.0, "avg_price": 0.0})
        new_qty = float(pos["qty"]) + body.qty
        if new_qty <= 0:
            positions.pop(body.symbol, None)
        else:
            avg = (
                (float(pos["qty"]) * float(pos["avg_price"]) + notional) / new_qty
                if new_qty
                else 0.0
            )
            new_pos: dict[str, Any] = {"qty": new_qty, "avg_price": round(avg, 6)}
            # Latest entry's protective levels apply to the whole position.
            sl = body.stop_loss if body.stop_loss is not None else pos.get("stop_loss")
            tp = body.take_profit if body.take_profit is not None else pos.get("take_profit")
            if sl is not None:
                new_pos["stop_loss"] = sl
            if tp is not None:
                new_pos["take_profit"] = tp
            positions[body.symbol] = new_pos
    else:
        pos = positions.get(body.symbol)
        if not pos or float(pos["qty"]) < body.qty - 1e-9:
            raise HTTPException(400, "insufficient_position")
        avg = float(pos["avg_price"])
        realized = round((body.price - avg) * body.qty, 4)
        state["cash"] = round(state["cash"] + notional, 4)
        state["realized_pnl"] = round(float(state["realized_pnl"]) + realized, 4)
        rem = float(pos["qty"]) - body.qty
        if rem <= 1e-9:
            positions.pop(body.symbol, None)
        else:
            positions[body.symbol] = {**pos, "qty": rem, "avg_price": avg}

    fill = {
        "fill_id": str(uuid.uuid4()),
        "symbol": body.symbol,
        "side": side,
        "qty": body.qty,
        "price": body.price,
        "notional": round(notional, 2),
        "realized_pnl": realized,
        "stop_loss": body.stop_loss,
        "take_profit": body.take_profit,
        "reason": body.reason,
        "ts": time.time(),
        "venue": "paper",
    }
    state["fills"].append(fill)
    _save(state)

    # Inform risk-guard of trade count / pnl (best-effort)
    _record_trade(realized, count_trade=True)

    return {
        "status": "filled",
        "fill": fill,
        "account": {
            "cash": state["cash"],
            "equity": _equity(state, {body.symbol: body.price}),
            "realized_pnl": state["realized_pnl"],
            "positions": state["positions"],
        },
    }
