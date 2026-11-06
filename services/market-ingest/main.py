"""market-ingest — serve seed OHLCV + news for paper/backtest wiring."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="sentinel-market-ingest", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BUILD_ROOT = Path(__file__).resolve().parents[2]


def _seed(key: str, default: str) -> Path:
    raw = os.getenv(key, default)
    p = Path(raw)
    if not p.is_absolute():
        p = (BUILD_ROOT / p).resolve()
    return p


OHLCV = _seed("SEED_OHLCV_PATH", "./seeds/ohlcv_sample.csv")
NEWS = _seed("SEED_NEWS_PATH", "./seeds/news_sample.json")


@app.get("/health")
def health():
    return {
        "service": "market-ingest",
        "status": "ok",
        "ohlcv_exists": OHLCV.exists(),
        "news_exists": NEWS.exists(),
    }


@app.get("/bars")
def bars(limit: int = 90, symbol: str = "SYNTH"):
    if not OHLCV.exists():
        raise HTTPException(503, f"seed ohlcv missing: {OHLCV}")
    with OHLCV.open() as f:
        rows = list(csv.DictReader(f))
    # coerce numerics
    out = []
    for r in rows[-limit:]:
        out.append(
            {
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
        )
    return {"symbol": symbol, "bars": out, "count": len(out), "note": "synthetic seed"}


@app.get("/news")
def news(limit: int = 20):
    if not NEWS.exists():
        raise HTTPException(503, f"seed news missing: {NEWS}")
    items = json.loads(NEWS.read_text())
    return {"items": items[:limit], "count": len(items[:limit]), "note": "synthetic seed"}


@app.get("/last-price")
def last_price(symbol: str = "SYNTH"):
    data = bars(limit=1, symbol=symbol)
    if not data["bars"]:
        raise HTTPException(404, "no bars")
    bar = data["bars"][-1]
    return {"symbol": symbol, "price": bar["close"], "date": bar["date"]}
