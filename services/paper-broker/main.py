"""paper-broker — simulated fills only. Requires risk_allowed. Tracks cash/positions/PnL."""
from __future__ import annotations

import json
import os
import time
import uuid
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


def _empty_state() -> dict[str, Any]:
    return {
        "cash": STARTING_CASH,
        "positions": {},  # symbol -> {qty, avg_price}
        "fills": [],
        "realized_pnl": 0.0,
    }


def _load() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        state = _empty_state()
        _save(state)
        return state
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        state = _empty_state()
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


@app.post("/reset")
def reset():
    state = _empty_state()
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
            positions[body.symbol] = {"qty": new_qty, "avg_price": round(avg, 6)}
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
            positions[body.symbol] = {"qty": rem, "avg_price": avg}

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
    try:
        with httpx.Client(timeout=3.0) as client:
            client.post(f"{RISK_URL}/record-trade", json={"realized_pnl": realized})
    except httpx.HTTPError:
        pass

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
