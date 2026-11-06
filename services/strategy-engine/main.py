"""strategy-engine — SMA crossover signal + backtest vs buy-and-hold."""
from __future__ import annotations

import csv
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-strategy-engine", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BUILD_ROOT = Path(__file__).resolve().parents[2]
MARKET_URL = os.getenv("MARKET_INGEST_URL", "http://127.0.0.1:8001")
FAST = int(os.getenv("SMA_FAST", "5"))
SLOW = int(os.getenv("SMA_SLOW", "15"))


def _seed_bars() -> list[dict]:
    raw = os.getenv("SEED_OHLCV_PATH", "./seeds/ohlcv_sample.csv")
    p = Path(raw)
    if not p.is_absolute():
        p = (BUILD_ROOT / p).resolve()
    if not p.exists():
        raise HTTPException(503, f"seed missing: {p}")
    with p.open() as f:
        rows = list(csv.DictReader(f))
    return [
        {
            "date": r["date"],
            "close": float(r["close"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "volume": float(r["volume"]),
        }
        for r in rows
    ]


def _fetch_bars(limit: int = 90) -> list[dict]:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{MARKET_URL}/bars", params={"limit": limit})
            r.raise_for_status()
            return r.json()["bars"]
    except httpx.HTTPError:
        return _seed_bars()[-limit:]


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if window <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= window:
            s -= values[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out


class SignalRequest(BaseModel):
    symbol: str = "SYNTH"
    fast: int = Field(default=FAST, ge=2)
    slow: int = Field(default=SLOW, ge=3)


class BacktestRequest(BaseModel):
    symbol: str = "SYNTH"
    fast: int = Field(default=FAST, ge=2)
    slow: int = Field(default=SLOW, ge=3)
    starting_cash: float = 100000.0


@app.get("/health")
def health():
    return {"service": "strategy-engine", "status": "ok", "sma_fast": FAST, "sma_slow": SLOW}


@app.post("/signal")
def signal(body: SignalRequest):
    if body.fast >= body.slow:
        raise HTTPException(400, "fast must be < slow")
    bars = _fetch_bars(limit=max(body.slow + 5, 60))
    closes = [float(b["close"]) for b in bars]
    if len(closes) < body.slow + 1:
        return {
            "symbol": body.symbol,
            "action": "hold",
            "confidence": 0.0,
            "reason": "insufficient_bars",
        }
    fast = _sma(closes, body.fast)
    slow = _sma(closes, body.slow)
    i = len(closes) - 1
    j = i - 1
    if fast[i] is None or slow[i] is None or fast[j] is None or slow[j] is None:
        return {"symbol": body.symbol, "action": "hold", "confidence": 0.0, "reason": "warmup"}

    action = "hold"
    reason = "no_crossover"
    if fast[j] <= slow[j] and fast[i] > slow[i]:
        action = "buy"
        reason = f"SMA{body.fast} crossed above SMA{body.slow}"
    elif fast[j] >= slow[j] and fast[i] < slow[i]:
        action = "sell"
        reason = f"SMA{body.fast} crossed below SMA{body.slow}"

    spread = abs(fast[i] - slow[i]) / closes[i]
    return {
        "symbol": body.symbol,
        "action": action,
        "confidence": round(min(1.0, spread * 50), 3),
        "price": closes[i],
        "date": bars[i]["date"],
        "sma_fast": round(fast[i], 4),
        "sma_slow": round(slow[i], 4),
        "reason": reason,
        "suggested_stop_pct": float(os.getenv("RISK_STOP_LOSS_PCT", "0.02")),
        "suggested_rr": float(os.getenv("RISK_MIN_RR", "1.5")),
    }


@app.post("/backtest")
def backtest(body: BacktestRequest):
    if body.fast >= body.slow:
        raise HTTPException(400, "fast must be < slow")
    bars = _fetch_bars(limit=500)
    closes = [float(b["close"]) for b in bars]
    dates = [b["date"] for b in bars]
    if len(closes) < body.slow + 2:
        raise HTTPException(400, "not enough bars")

    fast = _sma(closes, body.fast)
    slow = _sma(closes, body.slow)

    cash = body.starting_cash
    qty = 0.0
    entry = 0.0
    trades = 0
    equity_curve = []
    position = 0  # 0 flat, 1 long

    for i in range(len(closes)):
        if fast[i] is None or slow[i] is None:
            equity_curve.append({"date": dates[i], "equity": cash + qty * closes[i]})
            continue
        if i == 0 or fast[i - 1] is None or slow[i - 1] is None:
            equity_curve.append({"date": dates[i], "equity": cash + qty * closes[i]})
            continue

        # Golden cross -> buy
        if position == 0 and fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            qty = cash / closes[i]
            entry = closes[i]
            cash = 0.0
            position = 1
            trades += 1
        # Death cross -> sell
        elif position == 1 and fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            cash = qty * closes[i]
            qty = 0.0
            position = 0
            trades += 1

        equity_curve.append({"date": dates[i], "equity": round(cash + qty * closes[i], 2)})

    final_eq = cash + qty * closes[-1]
    strategy_return = (final_eq / body.starting_cash) - 1.0

    # Buy-and-hold: buy first bar after warmup, hold to end
    start_i = body.slow - 1
    bh_qty = body.starting_cash / closes[start_i]
    bh_final = bh_qty * closes[-1]
    buy_hold_return = (bh_final / body.starting_cash) - 1.0

    bh_curve = []
    for i in range(len(closes)):
        if i < start_i:
            bh_curve.append({"date": dates[i], "equity": body.starting_cash})
        else:
            bh_curve.append(
                {"date": dates[i], "equity": round(bh_qty * closes[i], 2)}
            )

    return {
        "symbol": body.symbol,
        "strategy": f"SMA_crossover_{body.fast}_{body.slow}",
        "starting_cash": body.starting_cash,
        "final_equity": round(final_eq, 2),
        "strategy_return": round(strategy_return, 6),
        "buy_hold_return": round(buy_hold_return, 6),
        "buy_hold_final": round(bh_final, 2),
        "outperformance": round(strategy_return - buy_hold_return, 6),
        "trades": trades,
        "bars": len(closes),
        "equity_curve": equity_curve,
        "buy_hold_curve": bh_curve,
        "summary": (
            f"SMA({body.fast}/{body.slow}) return {strategy_return*100:.2f}% vs "
            f"buy-and-hold {buy_hold_return*100:.2f}% "
            f"({'beats' if strategy_return > buy_hold_return else 'lags'} BH)"
        ),
    }
