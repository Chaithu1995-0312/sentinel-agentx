"""market-ingest — serve seed OHLCV + news for paper/backtest wiring."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="sentinel-market-ingest", version="0.1.0")
import sys as _sys  # noqa: E402

_SENTINEL_ROOT = Path(__file__).resolve().parents[2]
if str(_SENTINEL_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_SENTINEL_ROOT))
from shared.security import auth_headers, cors_kwargs, require_key  # noqa: E402,F401

# CORS allow-list (dashboard origins + file:// hub), not "*". See shared/security.py.
app.add_middleware(CORSMiddleware, **cors_kwargs())

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


# The mock seed is one synthetic instrument. Refuse other symbols rather than
# silently returning SYNTH prices under a different name.
SYMBOLS = {x.strip().upper() for x in os.getenv("MOCK_SYMBOLS", "SYNTH").split(",") if x.strip()}


def _check_symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if sym not in SYMBOLS:
        raise HTTPException(404, f"unknown symbol {symbol!r}; mock data has {sorted(SYMBOLS)}")
    return sym


@app.get("/bars")
def bars(limit: int = 90, symbol: str = "SYNTH"):
    symbol = _check_symbol(symbol)
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
    return {"symbol": symbol, "bars": out, "count": len(out), "note": "MOCK synthetic seed, not a real market"}


@app.get("/news")
def news(limit: int = 20):
    if not NEWS.exists():
        raise HTTPException(503, f"seed news missing: {NEWS}")
    items = json.loads(NEWS.read_text())
    return {"items": items[:limit], "count": len(items[:limit]), "note": "MOCK synthetic headlines, not a real news feed"}


@app.get("/last-price")
def last_price(symbol: str = "SYNTH"):
    symbol = _check_symbol(symbol)
    data = bars(limit=1, symbol=symbol)
    if not data["bars"]:
        raise HTTPException(404, "no bars")
    bar = data["bars"][-1]
    return {"symbol": symbol, "price": bar["close"], "date": bar["date"]}
