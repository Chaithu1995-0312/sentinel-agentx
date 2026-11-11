"""journal-api — structured + plain-English trade journal (templates; optional LLM later)."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-journal-api", version="0.1.0")
import sys as _sys  # noqa: E402

_SENTINEL_ROOT = Path(__file__).resolve().parents[2]
if str(_SENTINEL_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_SENTINEL_ROOT))
from shared.security import auth_headers, cors_kwargs, require_key  # noqa: E402,F401
from shared.state_io import append_line, lock_for, write_text_atomic  # noqa: E402

# CORS allow-list (dashboard origins + file:// hub), not "*". See shared/security.py.
app.add_middleware(CORSMiddleware, **cors_kwargs())

BUILD_ROOT = Path(__file__).resolve().parents[2]
TOKEN_CAP = int(os.getenv("LLM_DAILY_TOKEN_CAP", "50000"))
_tokens_used = 0


def _journal_path() -> Path:
    raw = os.getenv("JOURNAL_PATH", "./data/journal.jsonl")
    p = Path(raw)
    if not p.is_absolute():
        p = (BUILD_ROOT / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_entries() -> list[dict]:
    path = _journal_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append(row: dict) -> None:
    # Append-only, one JSON object per line, under a lock, fsynced.
    append_line(_journal_path(), json.dumps(row))


def _sentence(text: str) -> str:
    text = str(text).strip()
    if not text:
        return text
    return text if text[-1] in ".!?" else text + "."


def _template_body(payload: dict) -> str:
    action = payload.get("action") or payload.get("side") or "decision"
    symbol = payload.get("symbol", "SYNTH")
    qty = payload.get("qty")
    price = payload.get("price")
    reason = payload.get("reason") or payload.get("signal_reason") or "n/a"
    risk = payload.get("risk_reason") or "n/a"
    allowed = payload.get("allowed")
    parts = [
        f"We looked at {symbol} and decided to {action}.",
    ]
    if qty is not None and price is not None:
        parts.append(f"Size {qty} at about {price}.")
    parts.append(f"Why: {_sentence(reason)}")
    if allowed is False:
        parts.append(f"Risk guard blocked it: {_sentence(risk)}")
    elif allowed is True:
        parts.append(f"Risk guard allowed it ({risk}).")
    if payload.get("stop_loss") is not None:
        parts.append(f"Stop loss at {payload['stop_loss']}.")
    if payload.get("take_profit") is not None:
        parts.append(f"Take profit at {payload['take_profit']}.")
    if payload.get("strategy"):
        parts.append(f"Strategy in use: {payload['strategy']}.")
    selection = payload.get("selection_reason")
    if selection and selection != reason:
        parts.append(f"Strategy pick: {_sentence(selection)}")
    if payload.get("news_score") is not None or payload.get("news_reason"):
        if payload.get("news_score") is None:
            parts.append(_sentence(f"News: {payload.get('news_reason')}"))
        else:
            news_line = f"News score {float(payload['news_score']):.2f}."
            if payload.get("news_reason"):
                news_line = f"{news_line} {payload['news_reason']}"
            parts.append(_sentence(news_line))
    if payload.get("vs_buy_hold"):
        parts.append(f"Backtest note: {_sentence(payload['vs_buy_hold'])}")
    return " ".join(parts)


class Entry(BaseModel):
    title: str
    body: str | None = None
    tags: list[str] = Field(default_factory=list)
    structured: dict = Field(default_factory=dict)


class TradeJournal(BaseModel):
    """Convenience endpoint: build plain-English from structured trade fields."""
    title: str | None = None
    symbol: str = "SYNTH"
    side: str | None = None
    action: str | None = None
    qty: float | None = None
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str | None = None
    signal_reason: str | None = None
    risk_reason: str | None = None
    allowed: bool | None = None
    vs_buy_hold: str | None = None
    strategy: str | None = None
    selection_reason: str | None = None
    news_score: float | None = None
    news_reason: str | None = None
    tags: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


@app.get("/health")
def health():
    return {
        "service": "journal-api",
        "status": "ok",
        "count": len(_load_entries()),
        "llm_tokens_used": _tokens_used,
        "llm_token_cap": TOKEN_CAP,
        "llm_mode": "template",
    }


@app.get("/entries")
def list_entries(limit: int = 50):
    rows = _load_entries()
    return {"entries": rows[-limit:], "count": len(rows)}


@app.post("/entries", dependencies=[Depends(require_key)])
def add_entry(body: Entry):
    text = body.body or _template_body(body.structured)
    row = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "title": body.title,
        "body": text,
        "tags": body.tags,
        "structured": body.structured,
    }
    _append(row)
    return row


@app.post("/trade", dependencies=[Depends(require_key)])
def add_trade(body: TradeJournal):
    structured = {
        "symbol": body.symbol,
        "side": body.side,
        "action": body.action or body.side,
        "qty": body.qty,
        "price": body.price,
        "stop_loss": body.stop_loss,
        "take_profit": body.take_profit,
        "reason": body.reason,
        "signal_reason": body.signal_reason,
        "risk_reason": body.risk_reason,
        "allowed": body.allowed,
        "vs_buy_hold": body.vs_buy_hold,
        "strategy": body.strategy,
        "selection_reason": body.selection_reason,
        "news_score": body.news_score,
        "news_reason": body.news_reason,
        **body.extra,
    }
    title = body.title or f"{structured['action']} {body.symbol}".strip()
    text = _template_body(structured)
    # Prefer templates; optional LLM hook left unused without key (cap ready).
    row = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "title": title,
        "body": text,
        "tags": body.tags or ["trade"],
        "structured": structured,
    }
    _append(row)
    return row


@app.delete("/entries", dependencies=[Depends(require_key)])
def clear_entries():
    """Demo reset only (used by demo_check); the agent never calls it."""
    path = _journal_path()
    with lock_for(path):
        if path.exists():
            write_text_atomic(path, "")
    return {"status": "cleared"}
