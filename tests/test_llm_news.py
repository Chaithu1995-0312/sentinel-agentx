"""Optional Grok news scorer: flag off is unchanged, LLM scores feed the same rules,
and every failure falls back to the word list. HTTP is mocked; no real calls."""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "xai-test-key-never-real"
CRASH = {"date": "2025-08-27", "headline": "Markets plunge on surprise default fears and heavy selloff", "source": "Reuters"}
UPBEAT = {"date": "2024-12-16", "headline": "Growth names surge on upbeat earnings", "source": "MockWire"}


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    for k in ("SENTINEL_LLM_NEWS", "SENTINEL_LLM_MODEL", "SENTINEL_LLM_TEMPERATURE",
              "SENTINEL_LLM_TIMEOUT_S", "SENTINEL_LLM_DAILY_TOKEN_CAP", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENTINEL_LLM_USAGE_PATH", str(tmp_path / "llm_news_usage.json"))
    return _load("llm_engine", "services/strategy-engine/main.py")


def _tech(action: str = "buy", conf: float = 0.5, d: str = "2025-08-28") -> dict:
    return {"symbol": "SYNTH", "strategy": "sma_crossover", "action": action, "confidence": conf,
            "reason": "SMA5 crossed above SMA15", "price": 100.0, "date": d}


def _reply(obj, total_tokens: int = 120, status: int = 200, raw: str | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        content = raw if raw is not None else json.dumps(obj)
        return httpx.Response(status, json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": total_tokens},
        })

    return httpx.MockTransport(handler), calls


def _never():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("LLM must not be called")
    return httpx.MockTransport(handler)


def _on(monkeypatch, key: bool = True):
    monkeypatch.setenv("SENTINEL_LLM_NEWS", "true")
    if key:
        monkeypatch.setenv("GROK_API_KEY", FAKE_KEY)


# --- flag off ---------------------------------------------------------------
def test_flag_off_signal_is_unchanged(engine, monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", FAKE_KEY)  # a key alone must not turn it on
    monkeypatch.setattr(engine, "MARKET_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(engine.llm_news, "score", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert engine.llm_news.enabled() is False
    out = TestClient(engine.app).post("/signal", json={"symbol": "SYNTH", "strategy": "sma_crossover"}).json()
    assert "news_scorer" not in out
    bars = engine._bars_as_of(None)
    tech = engine._technical("sma_crossover", bars, engine.FAST, engine.SLOW, "SYNTH")
    expected = engine._apply_news(tech, engine._fetch_news(), tech.get("date") or "")
    expected.update(engine._stop_defaults())
    assert out == json.loads(json.dumps(expected))


def test_flag_off_rules_match_wordlist(engine):
    out = engine._apply_news(_tech(), [CRASH], "2025-08-28")
    assert out["action"] == "hold" and out["news_score"] == -1.0 and "news_scorer" not in out


# --- LLM path ---------------------------------------------------------------
def test_crash_headline_llm_vetoes_buy(engine, monkeypatch):
    _on(monkeypatch)
    transport, calls = _reply({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": ["h1"],
                               "reason": "Markets plunge on default fears and a heavy selloff."})
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=transport)
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    assert sent["temperature"] == 0.0 and sent["model"] == engine.llm_news.DEFAULT_MODEL
    assert sent["response_format"] == {"type": "json_object"}
    user = json.loads(sent["messages"][1]["content"])
    assert user["symbol"] == "SYNTH" and user["bar_date"] == "2025-08-28"
    assert user["headlines"][0]["id"] == "h1"
    assert calls[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert out["action"] == "hold" and out["technical_action"] == "buy"
    assert out["news_changed_decision"] is True and out["size_multiplier"] == 0.0
    assert out["news_score"] == -0.8
    assert out["confidence"] == 0.3 and out["technical_confidence"] == 0.5  # 0.5 * 0.6, tech kept separate
    meta = out["news_scorer"]
    assert meta["scorer"] == "llm" and meta["fallback_reason"] is None
    assert meta["llm_confidence"] == 0.9 and meta["temperature"] == 0.0 and meta["headline_ids"] == ["h1"]
    assert FAKE_KEY not in json.dumps(out)


def test_upbeat_headline_llm_keeps_buy(engine, monkeypatch):
    _on(monkeypatch)
    transport, _ = _reply({"sentiment_score": 0.7, "confidence": 0.8, "headline_ids": ["h1"],
                           "reason": "Growth names surge on upbeat earnings."})
    out = engine._apply_news_llm(_tech(d="2024-12-17"), [UPBEAT], "2024-12-17", transport=transport)
    assert out["action"] == "buy" and out["news_changed_decision"] is False
    assert out["confidence"] == 0.6 and out["size_multiplier"] == 1.0
    assert out["news_scorer"]["scorer"] == "llm"


def test_success_counts_tokens(engine, monkeypatch):
    _on(monkeypatch)
    transport, _ = _reply({"sentiment_score": 0.0, "confidence": 0.5, "headline_ids": ["h1"], "reason": "Neutral."},
                          total_tokens=321)
    engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=transport)
    assert engine.llm_news.tokens_used_today() == 321


# --- fallbacks --------------------------------------------------------------
def _assert_fallback(engine, out, reason, items=(CRASH,), as_of="2025-08-28"):
    wl = engine._apply_news(_tech(d=as_of), list(items), as_of)
    assert out["news_scorer"]["scorer"] == "wordlist"
    assert out["news_scorer"]["fallback_reason"] == reason
    assert out["news_scorer"]["llm_confidence"] is None
    for k in ("action", "confidence", "size_multiplier", "news_score", "news_changed_decision", "reason"):
        assert out[k] == wl[k]
    assert FAKE_KEY not in json.dumps(out)


def test_missing_key_falls_back(engine, monkeypatch):
    _on(monkeypatch, key=False)
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=_never())
    _assert_fallback(engine, out, "missing_api_key")


def test_invalid_json_falls_back(engine, monkeypatch):
    _on(monkeypatch)
    transport, _ = _reply(None, raw="Sentiment looks bearish, maybe -0.8")
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=transport)
    _assert_fallback(engine, out, "invalid_response:content_not_json")


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"sentiment_score": -1.5, "confidence": 0.9, "headline_ids": ["h1"], "reason": "x"}, "invalid_response:sentiment_score"),
        ({"sentiment_score": -0.8, "confidence": 1.2, "headline_ids": ["h1"], "reason": "x"}, "invalid_response:confidence"),
        ({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": ["h9"], "reason": "x"}, "invalid_response:headline_ids"),
        ({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": [], "reason": "x"}, "invalid_response:headline_ids"),
        ({"sentiment_score": "-0.8", "confidence": 0.9, "headline_ids": ["h1"], "reason": "x"}, "invalid_response:sentiment_score"),
        ({"sentiment_score": True, "confidence": 0.9, "headline_ids": ["h1"], "reason": "x"}, "invalid_response:sentiment_score"),
        ({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": ["h1"], "reason": "  "}, "invalid_response:reason"),
        ({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": ["h1"]}, "invalid_response:keys(missing=['reason'],extra=[])"),
        ({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": ["h1"], "reason": "x", "temperature": 0},
         "invalid_response:keys(missing=[],extra=['temperature'])"),
        ([1, 2], "invalid_response:not_an_object"),
    ],
)
def test_invalid_fields_fall_back_without_clamping(engine, monkeypatch, payload, reason):
    _on(monkeypatch)
    transport, _ = _reply(payload)
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=transport)
    _assert_fallback(engine, out, reason)


def test_timeout_falls_back(engine, monkeypatch):
    _on(monkeypatch)

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=httpx.MockTransport(handler))
    _assert_fallback(engine, out, "timeout")


def test_http_error_falls_back(engine, monkeypatch):
    _on(monkeypatch)
    transport, _ = _reply({}, status=500)
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=transport)
    _assert_fallback(engine, out, "http_error:500")


def test_daily_cap_exceeded_falls_back(engine, monkeypatch, tmp_path):
    _on(monkeypatch)
    monkeypatch.setenv("SENTINEL_LLM_DAILY_TOKEN_CAP", "1000")
    (tmp_path / "llm_news_usage.json").write_text(json.dumps({"date": date.today().isoformat(), "tokens": 1000}))
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=_never())
    _assert_fallback(engine, out, "daily_token_cap")


def test_cap_resets_on_a_new_day(engine, monkeypatch, tmp_path):
    _on(monkeypatch)
    monkeypatch.setenv("SENTINEL_LLM_DAILY_TOKEN_CAP", "1000")
    (tmp_path / "llm_news_usage.json").write_text(json.dumps({"date": "2000-01-01", "tokens": 999999}))
    transport, calls = _reply({"sentiment_score": -0.8, "confidence": 0.9, "headline_ids": ["h1"], "reason": "Plunge."})
    out = engine._apply_news_llm(_tech(), [CRASH], "2025-08-28", transport=transport)
    assert len(calls) == 1 and out["news_scorer"]["scorer"] == "llm"


def test_no_headlines_skips_call(engine, monkeypatch):
    _on(monkeypatch)
    out = engine._apply_news_llm(_tech(), [CRASH], "2026-01-01", transport=_never())
    assert out["news_scorer"]["fallback_reason"] == "no_headlines_in_window"
    assert out["action"] == "buy"


def test_journal_payload_carries_scorer_only_when_present():
    orch = _load("llm_orch", "services/agent-orchestrator/main.py")
    sig = {"news_score": -0.8, "news_reason": "r", "reason": "x"}
    assert "extra" not in orch._journal_payload({"title": "t"}, sig, "sma_crossover", "why")
    meta = {"scorer": "llm", "model": "m", "temperature": 0.0, "llm_confidence": 0.9}
    body = orch._journal_payload({"title": "t", "extra": {"fill_id": "f1"}}, {**sig, "news_scorer": meta}, "sma_crossover", "why")
    assert body["extra"] == {"fill_id": "f1", "news_scorer": meta}
