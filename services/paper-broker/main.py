"""paper-broker — simulated fills only. Paper venue, mock prices.

Every order is re-verified with risk-guard /check-order using the broker's own
book (equity, open exposure, today's P&L); the caller's risk_allowed flag is
necessary but never sufficient. State is written atomically under a lock, so
concurrent requests cannot double-fill or lose an update.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-paper-broker", version="0.3.0")
import sys as _sys  # noqa: E402

_SENTINEL_ROOT = Path(__file__).resolve().parents[2]
if str(_SENTINEL_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_SENTINEL_ROOT))
from shared.security import auth_headers, cors_kwargs, require_key  # noqa: E402,F401
from shared.state_io import write_json_atomic  # noqa: E402

# CORS allow-list (dashboard origins + file:// hub), not "*". See shared/security.py.
app.add_middleware(CORSMiddleware, **cors_kwargs())

BUILD_ROOT = Path(__file__).resolve().parents[2]
STARTING_CASH = float(os.getenv("PAPER_STARTING_CASH", "100000"))
RISK_URL = os.getenv("RISK_GUARD_URL", "http://127.0.0.1:8003")
# Same cost model as the backtest: fee on notional + slippage against us.
FEE_BPS = float(os.getenv("PAPER_FEE_BPS", os.getenv("FEE_BPS", "10")))
SLIPPAGE_BPS = float(os.getenv("PAPER_SLIPPAGE_BPS", os.getenv("SLIPPAGE_BPS", "5")))
STATE_PATH = Path(os.getenv("BROKER_STATE_PATH", str(BUILD_ROOT / "data" / "broker_state.json")))
if not STATE_PATH.is_absolute():
    STATE_PATH = (BUILD_ROOT / STATE_PATH).resolve()

_LOCK = threading.RLock()  # one book per process; serialises read-modify-write


def _today() -> str:
    """Local calendar date of this machine. The daily loss window resets on it."""
    return date.today().isoformat()


def _fill_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).date().isoformat()


def _empty_state() -> dict[str, Any]:
    return {
        "cash": STARTING_CASH,
        "positions": {},  # symbol -> {qty, avg_price, stop_loss?, take_profit?, entry_bar?}
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
    with _LOCK:
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
    write_json_atomic(STATE_PATH, state)


def _equity(state: dict[str, Any], marks: dict[str, float] | None = None) -> float:
    eq = float(state["cash"])
    for sym, pos in state.get("positions", {}).items():
        qty = float(pos["qty"])
        px = (marks or {}).get(sym, float(pos["avg_price"]))
        eq += qty * px
    return eq


def _costs(side: str, price: float, qty: float) -> tuple[float, float]:
    """Executed price after slippage, and fee on executed notional."""
    slip = SLIPPAGE_BPS / 10000.0
    px = price * (1 + slip) if side == "buy" else price * (1 - slip)
    fee = abs(qty * px) * FEE_BPS / 10000.0
    return round(px, 6), round(fee, 4)


class OrderRequest(BaseModel):
    symbol: str
    side: str
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    risk_allowed: bool = False
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str | None = None
    bar_date: str | None = None  # date of the bar the price came from


class ExitCheck(BaseModel):
    symbol: str
    price: float = Field(gt=0)  # latest close
    dry_run: bool = False
    bar_date: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None


def _record_trade(realized: float, count_trade: bool) -> None:
    """Tell risk-guard about a fill (best-effort)."""
    try:
        with httpx.Client(timeout=3.0, headers=auth_headers()) as client:
            client.post(
                f"{RISK_URL}/record-trade",
                json={"realized_pnl": realized, "count_trade": count_trade},
            )
    except httpx.HTTPError:
        pass


def _risk_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask risk-guard. Fail closed if it is unreachable or answers badly."""
    try:
        with httpx.Client(timeout=3.0, headers=auth_headers()) as client:
            r = client.post(f"{RISK_URL}/check-order", json=payload)
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "risk-guard unreachable — fail-closed")


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
        "fee_bps": FEE_BPS,
        "slippage_bps": SLIPPAGE_BPS,
    }


@app.get("/account")
def account(symbol: str | None = None, price: float | None = None):
    """Equity at cost, or marked to market when ?symbol=&price= is given."""
    state = _load()
    marks = {symbol: float(price)} if symbol and price else {}
    return {
        "cash": state["cash"],
        "positions": state["positions"],
        "realized_pnl": state["realized_pnl"],
        "equity": _equity(state, marks),
        "equity_basis": "marked" if marks else "cost",
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


def _exit_trigger(pos: dict[str, Any], body: ExitCheck) -> tuple[str | None, float]:
    """Return (trigger, fill price before slippage).

    On a bar after entry we use its low/high, so an intraday touch counts. If the
    bar opened beyond the level (a gap) we fill at the open, not the level. If a
    bar touched both, we assume the stop came first (conservative). On the entry
    bar itself only the close is used, because its range happened before entry.
    """
    stop = pos.get("stop_loss")
    tp = pos.get("take_profit")
    after_entry = bool(body.bar_date and pos.get("entry_bar") and body.bar_date > pos["entry_bar"])
    if after_entry and body.low is not None and body.high is not None:
        if stop is not None and body.low <= float(stop):
            gap = body.open is not None and body.open <= float(stop)
            return "stop_loss", float(body.open) if gap else float(stop)
        if tp is not None and body.high >= float(tp):
            gap = body.open is not None and body.open >= float(tp)
            return "take_profit", float(body.open) if gap else float(tp)
        return None, body.price
    if stop is not None and body.price <= float(stop):
        return "stop_loss", body.price
    if tp is not None and body.price >= float(tp):
        return "take_profit", body.price
    return None, body.price


@app.post("/evaluate-exits", dependencies=[Depends(require_key)])
def evaluate_exits(body: ExitCheck):
    """Close an open long when the price reaches its stop-loss or take-profit.

    Exits only reduce risk, so they are allowed while the kill switch is ON and
    are not counted against the daily trade cap.
    """
    with _LOCK:
        state = _load()
        pos = state.get("positions", {}).get(body.symbol)
        if not pos:
            return {"symbol": body.symbol, "price": body.price, "exits": [], "position": None}
        trigger, raw_px = _exit_trigger(pos, body)
        if trigger is None:
            return {"symbol": body.symbol, "price": body.price, "exits": [], "position": pos}

        qty = float(pos["qty"])
        avg = float(pos["avg_price"])
        px, fee = _costs("sell", raw_px, qty)
        stop, tp = pos.get("stop_loss"), pos.get("take_profit")
        fill = {
            "fill_id": str(uuid.uuid4()),
            "symbol": body.symbol,
            "side": "sell",
            "qty": qty,
            "price": px,
            "quoted_price": raw_px,
            "fee": fee,
            "notional": round(qty * px, 2),
            "realized_pnl": round((px - avg) * qty - fee, 4),
            "stop_loss": stop,
            "take_profit": tp,
            "reason": trigger,
            "trigger_level": float(stop if trigger == "stop_loss" else tp),
            "entry_price": avg,
            "bar_date": body.bar_date,
            "ts": time.time(),
            "venue": "paper",
        }
        if body.dry_run:
            return {"symbol": body.symbol, "price": body.price, "exits": [fill], "dry_run": True, "position": pos}

        state["cash"] = round(state["cash"] + qty * px - fee, 4)
        state["realized_pnl"] = round(float(state["realized_pnl"]) + fill["realized_pnl"], 4)
        state["positions"].pop(body.symbol, None)
        state["fills"].append(fill)
        _save(state)
        _record_trade(fill["realized_pnl"], count_trade=False)
    return {"symbol": body.symbol, "price": body.price, "exits": [fill], "position": None}


@app.post("/reset", dependencies=[Depends(require_key)])
def reset():
    # A paper reset must not erase today's realized loss from the daily limit.
    with _LOCK:
        old = _load()
        carry = _daily(old, {})["realized_today"]
        state = _empty_state()
        if carry:
            state["reset_carry"] = {"day": _today(), "realized": carry}
        _save(state)
    return {"status": "reset", "cash": state["cash"]}


@app.post("/orders", dependencies=[Depends(require_key)])
def place_order(body: OrderRequest):
    if not body.risk_allowed:
        raise HTTPException(403, "risk_allowed=false — refuse order (paper)")
    side = body.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy|sell")

    with _LOCK:  # check and fill as one step: no double fill between them
        state = _load()
        positions = state.setdefault("positions", {})
        marks = {body.symbol: body.price}
        open_notional = sum(
            float(p.get("qty", 0)) * float(p.get("avg_price", 0)) for p in positions.values()
        )
        daily = _daily(state, marks)
        # Re-verify with the broker's own book. risk_allowed alone is not trusted.
        risk = _risk_check(
            {
                "symbol": body.symbol,
                "side": side,
                "qty": body.qty,
                "price": body.price,
                "equity": _equity(state, marks),
                "stop_loss": body.stop_loss,
                "take_profit": body.take_profit,
                "open_notional": open_notional,
                "daily_pnl": daily["daily_pnl"],
                "day_start_equity": daily["day_start_equity"],
            }
        )
        if not risk.get("allowed"):
            reason = risk.get("reason")
            if reason == "kill_switch":
                raise HTTPException(403, "kill_switch ON — broker refuse")
            raise HTTPException(403, f"risk-guard denied at broker: {reason} {risk.get('detail') or ''}".strip())

        px, fee = _costs(side, body.price, body.qty)
        notional = body.qty * px
        realized = 0.0
        if side == "buy":
            if state["cash"] < notional + fee:
                raise HTTPException(400, "insufficient_cash")
            state["cash"] = round(state["cash"] - notional - fee, 4)
            pos = positions.get(body.symbol, {"qty": 0.0, "avg_price": 0.0})
            new_qty = float(pos["qty"]) + body.qty
            # Fee is folded into the cost basis, so realized P&L is after costs.
            avg = (float(pos["qty"]) * float(pos["avg_price"]) + notional + fee) / new_qty
            new_pos: dict[str, Any] = {"qty": new_qty, "avg_price": round(avg, 6)}
            # Latest entry's protective levels apply to the whole position.
            sl = body.stop_loss if body.stop_loss is not None else pos.get("stop_loss")
            tp = body.take_profit if body.take_profit is not None else pos.get("take_profit")
            if sl is not None:
                new_pos["stop_loss"] = sl
            if tp is not None:
                new_pos["take_profit"] = tp
            if body.bar_date:
                new_pos["entry_bar"] = body.bar_date
            positions[body.symbol] = new_pos
        else:
            pos = positions.get(body.symbol)
            if not pos or float(pos["qty"]) < body.qty - 1e-9:
                raise HTTPException(400, "insufficient_position")
            avg = float(pos["avg_price"])
            realized = round((px - avg) * body.qty - fee, 4)
            state["cash"] = round(state["cash"] + notional - fee, 4)
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
            "price": px,
            "quoted_price": body.price,
            "fee": fee,
            "notional": round(notional, 2),
            "realized_pnl": realized,
            "stop_loss": body.stop_loss,
            "take_profit": body.take_profit,
            "reason": body.reason,
            "bar_date": body.bar_date,
            "risk_recheck": risk.get("reason"),
            "ts": time.time(),
            "venue": "paper",
        }
        state["fills"].append(fill)
        _save(state)
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
