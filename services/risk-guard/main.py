"""risk-guard — persistent kill switch + hard risk caps. Fail-closed. Paper only."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-risk-guard", version="0.1.0")
import sys as _sys  # noqa: E402

_SENTINEL_ROOT = Path(__file__).resolve().parents[2]
if str(_SENTINEL_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_SENTINEL_ROOT))
from shared.security import auth_headers, cors_kwargs, require_key  # noqa: E402,F401
from shared.state_io import lock_for, write_json_atomic  # noqa: E402

# CORS allow-list (dashboard origins + file:// hub), not "*". See shared/security.py.
app.add_middleware(CORSMiddleware, **cors_kwargs())

BUILD_ROOT = Path(__file__).resolve().parents[2]


def _path(key: str, default: str) -> Path:
    raw = os.getenv(key, default)
    p = Path(raw)
    if not p.is_absolute():
        p = (BUILD_ROOT / p).resolve()
    return p


KILL_PATH = _path("KILL_SWITCH_PATH", "./data/kill_switch.json")
STATE_PATH = _path("RISK_STATE_PATH", "./data/risk_state.json")

MAX_POSITION_PCT = float(os.getenv("RISK_MAX_POSITION_PCT", "0.05"))
STOP_LOSS_PCT = float(os.getenv("RISK_STOP_LOSS_PCT", "0.02"))
DAILY_LOSS_LIMIT = float(os.getenv("RISK_DAILY_LOSS_LIMIT", "0.03"))
MAX_TRADES_PER_DAY = int(os.getenv("RISK_MAX_TRADES_PER_DAY", "10"))
MIN_RR = float(os.getenv("RISK_MIN_RR", "1.5"))
KILL_DEFAULT = os.getenv("KILL_SWITCH_DEFAULT", "true").lower() in ("1", "true", "yes")


def _today() -> str:
    return date.today().isoformat()


def _load_kill() -> bool:
    KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not KILL_PATH.exists():
        _save_kill(KILL_DEFAULT)
        return KILL_DEFAULT
    try:
        data = json.loads(KILL_PATH.read_text())
        return bool(data.get("killed", KILL_DEFAULT))
    except (json.JSONDecodeError, OSError):
        # Fail-closed on corrupt state
        return True


def _save_kill(killed: bool) -> None:
    KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "killed": killed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": "human-controlled; agent cannot bypass",
    }
    write_json_atomic(KILL_PATH, payload)


def _load_state() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        state = {"day": _today(), "trades_today": 0, "realized_pnl_today": 0.0}
        _save_state(state)
        return state
    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        state = {"day": _today(), "trades_today": 0, "realized_pnl_today": 0.0}
    if state.get("day") != _today():
        state = {"day": _today(), "trades_today": 0, "realized_pnl_today": 0.0}
        _save_state(state)
    return state


def _save_state(state: dict[str, Any]) -> None:
    write_json_atomic(STATE_PATH, state)


class KillBody(BaseModel):
    killed: bool


class OrderCheck(BaseModel):
    symbol: str
    side: str  # buy|sell
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    equity: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    daily_pnl: float | None = None
    open_notional: float | None = None
    # Start-of-day equity for the daily loss limit (falls back to equity).
    day_start_equity: float | None = None


class TradeRecord(BaseModel):
    realized_pnl: float = 0.0
    # Stop-loss / take-profit exits reduce risk and do not use the daily trade cap.
    count_trade: bool = True


@app.on_event("startup")
def startup() -> None:
    _load_kill()
    _load_state()


@app.get("/health")
def health():
    return {
        "service": "risk-guard",
        "status": "ok",
        "killed": _load_kill(),
        "limits": {
            "max_position_pct": MAX_POSITION_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "min_rr": MIN_RR,
        },
    }


@app.get("/kill-switch")
def get_kill():
    return {"killed": _load_kill(), "path": str(KILL_PATH)}


@app.post("/kill-switch", dependencies=[Depends(require_key)])
def set_kill(body: KillBody):
    # Human endpoint only — agent must not call this to clear.
    with lock_for(KILL_PATH):
        _save_kill(body.killed)
    return {"killed": body.killed, "path": str(KILL_PATH)}


@app.get("/state")
def get_state():
    return _load_state()


@app.post("/record-trade", dependencies=[Depends(require_key)])
def record_trade(body: TradeRecord):
    """Called by broker/orchestrator after a fill to update daily counters."""
    with lock_for(STATE_PATH):
        return _record_trade_locked(body)


def _record_trade_locked(body: TradeRecord) -> dict[str, Any]:
    state = _load_state()
    if body.count_trade:
        state["trades_today"] = int(state.get("trades_today", 0)) + 1
    else:
        state["exits_today"] = int(state.get("exits_today", 0)) + 1
    state["realized_pnl_today"] = float(state.get("realized_pnl_today", 0.0)) + float(
        body.realized_pnl
    )
    _save_state(state)
    return state


@app.post("/check-order")
def check_order(body: OrderCheck):
    if _load_kill():
        return {
            "allowed": False,
            "reason": "kill_switch",
            "detail": "Human kill switch is ON. Agent cannot place orders.",
            "order": body.model_dump(),
        }

    state = _load_state()
    equity = float(body.equity) if body.equity is not None else float(
        os.getenv("PAPER_STARTING_CASH", "100000")
    )
    if equity <= 0:
        return {"allowed": False, "reason": "invalid_equity", "order": body.model_dump()}

    notional = body.qty * body.price
    open_notional = float(body.open_notional or 0.0)
    position_pct = (open_notional + notional) / equity if body.side.lower() == "buy" else notional / equity
    if position_pct > MAX_POSITION_PCT + 1e-9:
        return {
            "allowed": False,
            "reason": "max_position_pct",
            "detail": f"position {position_pct:.4f} > max {MAX_POSITION_PCT}",
            "order": body.model_dump(),
        }

    if int(state.get("trades_today", 0)) >= MAX_TRADES_PER_DAY:
        return {
            "allowed": False,
            "reason": "max_trades_per_day",
            "detail": f"trades_today={state['trades_today']} >= {MAX_TRADES_PER_DAY}",
            "order": body.model_dump(),
        }

    daily_pnl = (
        float(body.daily_pnl)
        if body.daily_pnl is not None
        else float(state.get("realized_pnl_today", 0.0))
    )
    loss_base = float(body.day_start_equity) if body.day_start_equity else equity
    if daily_pnl <= -DAILY_LOSS_LIMIT * loss_base:
        return {
            "allowed": False,
            "reason": "daily_loss_limit",
            "detail": f"daily_pnl={daily_pnl:.2f} breached -{DAILY_LOSS_LIMIT * loss_base:.2f}",
            "order": body.model_dump(),
        }

    # Stop-loss / min RR when levels provided (entries only)
    side = body.side.lower()
    if side == "buy" and body.stop_loss is not None:
        risk_per_share = body.price - body.stop_loss
        if risk_per_share <= 0:
            return {
                "allowed": False,
                "reason": "invalid_stop_loss",
                "detail": "stop_loss must be below entry for buy",
                "order": body.model_dump(),
            }
        stop_pct = risk_per_share / body.price
        if stop_pct > STOP_LOSS_PCT + 1e-9:
            return {
                "allowed": False,
                "reason": "stop_loss_pct",
                "detail": f"stop {stop_pct:.4f} > max {STOP_LOSS_PCT}",
                "order": body.model_dump(),
            }
        if body.take_profit is not None:
            reward = body.take_profit - body.price
            if reward <= 0:
                return {
                    "allowed": False,
                    "reason": "invalid_take_profit",
                    "order": body.model_dump(),
                }
            rr = reward / risk_per_share
            if rr + 1e-9 < MIN_RR:
                return {
                    "allowed": False,
                    "reason": "min_rr",
                    "detail": f"rr={rr:.2f} < min {MIN_RR}",
                    "order": body.model_dump(),
                }
    elif side == "sell" and body.stop_loss is not None:
        # Short-style or exit: if stop above price treat as short risk
        risk_per_share = body.stop_loss - body.price
        if risk_per_share > 0:
            stop_pct = risk_per_share / body.price
            if stop_pct > STOP_LOSS_PCT + 1e-9:
                return {
                    "allowed": False,
                    "reason": "stop_loss_pct",
                    "detail": f"stop {stop_pct:.4f} > max {STOP_LOSS_PCT}",
                    "order": body.model_dump(),
                }
            if body.take_profit is not None:
                reward = body.price - body.take_profit
                if reward > 0:
                    rr = reward / risk_per_share
                    if rr + 1e-9 < MIN_RR:
                        return {
                            "allowed": False,
                            "reason": "min_rr",
                            "detail": f"rr={rr:.2f} < min {MIN_RR}",
                            "order": body.model_dump(),
                        }

    return {
        "allowed": True,
        "reason": "ok",
        "limits_checked": [
            "kill_switch",
            "max_position_pct",
            "max_trades_per_day",
            "daily_loss_limit",
            "stop_loss_pct",
            "min_rr",
        ],
        "order": body.model_dump(),
        "state": state,
    }
