"""Optional Grok (xAI) headline scorer for strategy-engine.

Off by default. Enabled only when SENTINEL_LLM_NEWS is true AND GROK_API_KEY is set.
One chat-completions call per signal, strict JSON validation, a hard daily token
cap kept in a small local JSON file, and a word-list fallback on any failure.

The API key is read from the environment at call time and is never logged,
returned, or written to disk.
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from shared.paths import env_path

XAI_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TIMEOUT_S = 8.0
DEFAULT_DAILY_TOKEN_CAP = 50000
MAX_OUTPUT_TOKENS = 300
MAX_REASON_CHARS = 400
REQUIRED_KEYS = {"sentiment_score", "confidence", "headline_ids", "reason"}

_LEDGER_LOCK = threading.Lock()

SYSTEM_PROMPT = (
    "You are a news sentiment classifier for a paper-trading demo. "
    "Use only the headlines provided; do not use outside knowledge. "
    "Return only one JSON object, no prose and no code fences, with exactly these keys: "
    '"sentiment_score" (number from -1 to 1; negative means bearish for the symbol), '
    '"confidence" (number from 0 to 1; your confidence in this classification), '
    '"headline_ids" (non-empty array of ids, taken only from the input, of the headlines that drove the score), '
    '"reason" (one short sentence grounded in those headlines).'
)


class LLMNewsError(Exception):
    """Carries a short, key-free fallback reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def enabled() -> bool:
    return os.getenv("SENTINEL_LLM_NEWS", "false").strip().lower() in ("1", "true", "yes", "on")


def config() -> dict[str, Any]:
    """Non-secret settings. Never includes the key."""
    def _num(key: str, default: float) -> float:
        raw = os.getenv(key)
        if raw is None or raw.strip() == "":
            return default
        return float(raw)

    return {
        "model": (os.getenv("SENTINEL_LLM_MODEL") or DEFAULT_MODEL).strip(),
        "temperature": _num("SENTINEL_LLM_TEMPERATURE", DEFAULT_TEMPERATURE),
        "timeout_s": _num("SENTINEL_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S),
        "daily_token_cap": int(_num("SENTINEL_LLM_DAILY_TOKEN_CAP", DEFAULT_DAILY_TOKEN_CAP)),
    }


def usage_path() -> Path:
    return env_path("SENTINEL_LLM_USAGE_PATH", "./data/llm_news_usage.json")


def _today() -> str:
    return date.today().isoformat()


def tokens_used_today(path: Path | None = None) -> int:
    p = path or usage_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict) or data.get("date") != _today():
        return 0
    try:
        return int(data.get("tokens", 0))
    except (TypeError, ValueError):
        return 0


def _add_tokens(n: int, path: Path | None = None) -> int:
    p = path or usage_path()
    with _LEDGER_LOCK:
        total = tokens_used_today(p) + max(0, int(n))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps({"date": _today(), "tokens": total}))
        os.replace(tmp, p)
        return total


def build_messages(symbol: str, bar_date: str, headlines: list[dict]) -> list[dict]:
    payload = {"symbol": symbol, "bar_date": bar_date, "headlines": headlines}
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def validate(obj: Any, allowed_ids: set[str]) -> dict:
    """Strict check. Anything off raises; nothing is clamped or repaired."""
    if not isinstance(obj, dict):
        raise LLMNewsError("invalid_response:not_an_object")
    keys = set(obj)
    if keys != REQUIRED_KEYS:
        missing = sorted(REQUIRED_KEYS - keys)
        extra = sorted(keys - REQUIRED_KEYS)
        raise LLMNewsError(f"invalid_response:keys(missing={missing},extra={extra})")
    s = obj["sentiment_score"]
    if not _is_number(s) or not -1.0 <= float(s) <= 1.0:
        raise LLMNewsError("invalid_response:sentiment_score")
    c = obj["confidence"]
    if not _is_number(c) or not 0.0 <= float(c) <= 1.0:
        raise LLMNewsError("invalid_response:confidence")
    ids = obj["headline_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
        or not set(ids) <= allowed_ids
    ):
        raise LLMNewsError("invalid_response:headline_ids")
    reason = obj["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise LLMNewsError("invalid_response:reason")
    return {
        "sentiment_score": float(s),
        "confidence": float(c),
        "headline_ids": list(ids),
        "reason": reason.strip(),
    }


def score(symbol: str, bar_date: str, headlines: list[dict], *, transport: httpx.BaseTransport | None = None) -> dict:
    """One Grok call. Returns validated fields plus metadata, or raises LLMNewsError.

    `headlines` items must carry "id", "date", "source", "headline".
    `transport` exists only so tests can mock HTTP; production never passes it.
    """
    key = os.getenv("GROK_API_KEY", "").strip()
    if not key:
        raise LLMNewsError("missing_api_key")
    try:
        cfg = config()
    except ValueError:
        raise LLMNewsError("invalid_config")
    messages = build_messages(symbol, bar_date, headlines)
    estimate = sum(len(m["content"]) for m in messages) // 4 + MAX_OUTPUT_TOKENS
    used = tokens_used_today()
    if used + estimate > cfg["daily_token_cap"]:
        raise LLMNewsError("daily_token_cap")
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"],
        "max_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=cfg["timeout_s"], transport=transport) as client:
            r = client.post(XAI_URL, json=body, headers=headers)
    except httpx.TimeoutException:
        _add_tokens(estimate)  # may still be billed; count it conservatively
        raise LLMNewsError("timeout")
    except httpx.HTTPError:
        raise LLMNewsError("transport_error")
    if r.status_code != 200:
        if r.status_code not in (401, 403):
            _add_tokens(estimate)
        raise LLMNewsError(f"http_error:{r.status_code}")
    try:
        data = r.json()
    except ValueError:
        _add_tokens(estimate)
        raise LLMNewsError("invalid_response:body_not_json")
    tokens = estimate
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
        tokens = usage["total_tokens"]
    _add_tokens(tokens)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMNewsError("invalid_response:no_content")
    if not isinstance(content, str):
        raise LLMNewsError("invalid_response:no_content")
    try:
        parsed = json.loads(content)
    except ValueError:
        raise LLMNewsError("invalid_response:content_not_json")
    result = validate(parsed, {str(h["id"]) for h in headlines})
    result.update(
        {
            "model": cfg["model"],
            "temperature": cfg["temperature"],
            "tokens": tokens,
        }
    )
    return result
