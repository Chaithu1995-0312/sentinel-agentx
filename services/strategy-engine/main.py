"""strategy-engine — SMA, RSI mean-reversion, Bollinger breakout, lexicon news."""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="sentinel-strategy-engine", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BUILD_ROOT = Path(__file__).resolve().parents[2]
if str(BUILD_ROOT) not in sys.path:
    sys.path.insert(0, str(BUILD_ROOT))
from shared.selection import apply_hysteresis  # noqa: E402
MARKET_URL = os.getenv("MARKET_INGEST_URL", "http://127.0.0.1:8001")
FAST = int(os.getenv("SMA_FAST", "5"))
SLOW = int(os.getenv("SMA_SLOW", "15"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "2"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "70"))
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_K = float(os.getenv("BB_K", "2"))
# score = return - penalty * max_drawdown. Higher wins. Buy-and-hold is not a candidate.
DD_PENALTY = float(os.getenv("STRATEGY_DD_PENALTY", "0.5"))
FEE_BPS = float(os.getenv("FEE_BPS", "10"))
SLIPPAGE_BPS = float(os.getenv("SLIPPAGE_BPS", "5"))
SWITCH_MARGIN = float(os.getenv("SWITCH_MARGIN", "0.02"))
NEWS_LOOKBACK_DAYS = int(os.getenv("NEWS_LOOKBACK_DAYS", "14"))
# Worst headline at or below this vetoes a buy. One strong word (-2) / 2 = -1.0.
NEWS_VETO_SCORE = -0.60
NEWS_MILD_SCORE = -0.05
NEWS_POSITIVE_SCORE = 0.15

STRATEGIES = ("sma_crossover", "rsi_mean_reversion", "bollinger_breakout")

# Hand lexicon. Score = clamp(sum(weights) / 2, -1, 1). Phrases are removed first.
_PHRASES: tuple[tuple[str, int], ...] = (
    ("risk appetite", 2),
    ("sell-off", -2),
    ("sell off", -2),
)
_LEXICON: dict[str, int] = {
    "gain": 1,
    "gains": 1,
    "positive": 1,
    "improve": 1,
    "improves": 1,
    "improved": 1,
    "firm": 1,
    "steady": 1,
    "rally": 1,
    "leads": 1,
    "lead": 1,
    "appetite": 1,
    "ease": 1,
    "eases": 1,
    "easing": 1,
    "soft": 1,
    "surge": 2,
    "surges": 2,
    "rise": 1,
    "rises": 1,
    "rising": 1,
    "upbeat": 1,
    "optimism": 1,
    "optimistic": 1,
    "strong": 1,
    "strength": 1,
    "beat": 1,
    "beats": 1,
    "rebound": 1,
    "rebounds": 1,
    "growth": 1,
    "plunge": -2,
    "plunges": -2,
    "plunged": -2,
    "crash": -2,
    "crashes": -2,
    "selloff": -2,
    "default": -2,
    "defaults": -2,
    "fear": -1,
    "fears": -1,
    "lag": -1,
    "lags": -1,
    "lagged": -1,
    "narrow": -1,
    "narrows": -1,
    "mixed": -1,
    "weak": -1,
    "weakness": -1,
    "loss": -1,
    "losses": -1,
    "slump": -2,
    "tumble": -2,
    "tumbles": -2,
    "fall": -1,
    "falls": -1,
    "drop": -1,
    "drops": -1,
    "decline": -1,
    "declines": -1,
    "crisis": -2,
    "downgrade": -2,
    "bearish": -2,
    "recession": -2,
    "fraud": -2,
    "warning": -1,
    "bankruptcy": -2,
    "miss": -1,
    "misses": -1,
    "worries": -1,
    "worry": -1,
}


def _seed_path(key: str, default: str) -> Path:
    raw = os.getenv(key, default)
    p = Path(raw)
    if not p.is_absolute():
        p = (BUILD_ROOT / p).resolve()
    return p


def _seed_bars() -> list[dict]:
    p = _seed_path("SEED_OHLCV_PATH", "./seeds/ohlcv_sample.csv")
    if not p.exists():
        raise HTTPException(503, f"seed missing: {p}")
    with p.open() as f:
        rows = list(csv.DictReader(f))
    return [_bar(r) for r in rows]


def _bar(r: dict) -> dict:
    return {
        "date": r["date"],
        "close": float(r["close"]),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "volume": float(r["volume"]),
    }


def _seed_news() -> list[dict]:
    p = _seed_path("SEED_NEWS_PATH", "./seeds/news_sample.json")
    if not p.exists():
        return []
    return json.loads(p.read_text())


def _fetch_bars(limit: int = 500) -> list[dict]:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{MARKET_URL}/bars", params={"limit": limit})
            r.raise_for_status()
            return r.json()["bars"]
    except httpx.HTTPError:
        return _seed_bars()[-limit:]


def _fetch_news() -> list[dict]:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{MARKET_URL}/news", params={"limit": 100})
            r.raise_for_status()
            return list(r.json().get("items") or [])
    except httpx.HTTPError:
        return _seed_news()


def _sma(values: list[float], window: int) -> list[float | None]:
    """Simple moving average. Each point is the mean of its own window so it matches pandas."""
    out: list[float | None] = [None] * len(values)
    if window <= 0:
        return out
    for i in range(window - 1, len(values)):
        out[i] = sum(values[i - window + 1 : i + 1]) / window
    return out


def _rsi(values: list[float], period: int) -> list[float | None]:
    """Wilder RSI. Period 2 is short-horizon mean reversion on this 80-bar seed."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period

    def _value(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _value(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(delta, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _value(avg_g, avg_l)
    return out


def _bollinger(values: list[float], period: int, k: float):
    mid = _sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for i, m in enumerate(mid):
        if m is None:
            continue
        window = values[i - period + 1 : i + 1]
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return mid, upper, lower


def _max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            worst = max(worst, (peak - e) / peak)
    return worst


def _cost_rate(fee_bps: float, slippage_bps: float) -> float:
    return (float(fee_bps) + float(slippage_bps)) / 10000.0


def _actions(
    strategy: str,
    closes: list[float],
    fast: int,
    slow: int,
    bb_period: int | None = None,
    bb_k: float | None = None,
) -> list[str]:
    n = len(closes)
    actions = ["hold"] * n
    if strategy == "sma_crossover":
        f = _sma(closes, fast)
        s = _sma(closes, slow)
        for i in range(1, n):
            if None in (f[i], s[i], f[i - 1], s[i - 1]):
                continue
            if f[i - 1] <= s[i - 1] and f[i] > s[i]:
                actions[i] = "buy"
            elif f[i - 1] >= s[i - 1] and f[i] < s[i]:
                actions[i] = "sell"
        return actions
    if strategy == "rsi_mean_reversion":
        rv = _rsi(closes, RSI_PERIOD)
        for i in range(1, n):
            if rv[i] is None or rv[i - 1] is None:
                continue
            if rv[i - 1] >= RSI_OVERSOLD and rv[i] < RSI_OVERSOLD:
                actions[i] = "buy"
            elif rv[i - 1] <= RSI_OVERBOUGHT and rv[i] > RSI_OVERBOUGHT:
                actions[i] = "sell"
        return actions
    if strategy == "bollinger_breakout":
        period = BB_PERIOD if bb_period is None else int(bb_period)
        width = BB_K if bb_k is None else float(bb_k)
        mid, upper, _lower = _bollinger(closes, period, width)
        for i in range(1, n):
            if None in (mid[i], mid[i - 1], upper[i], upper[i - 1]):
                continue
            if closes[i - 1] <= upper[i - 1] and closes[i] > upper[i]:
                actions[i] = "buy"
            elif closes[i - 1] >= mid[i - 1] and closes[i] < mid[i]:
                actions[i] = "sell"
        return actions
    raise HTTPException(400, f"unknown strategy {strategy}")


def _simulate(
    closes: list[float],
    dates: list[str],
    actions: list[str],
    starting_cash: float,
    cost_rate: float = 0.0,
) -> dict:
    """Fill at the close. cost_rate is charged on each entry and each exit (fee + slippage)."""
    cash = float(starting_cash)
    qty = 0.0
    position = 0
    trades = 0
    equity: list[float] = []
    trade_dates: list[str] = []
    trade_events: list[dict] = []
    rate = max(0.0, float(cost_rate))
    for i, px in enumerate(closes):
        act = actions[i]
        if position == 0 and act == "buy":
            fill = px * (1.0 + rate)
            qty = cash / fill if fill else 0.0
            cash = 0.0
            position = 1
            trades += 1
            trade_dates.append(dates[i])
            trade_events.append({"date": dates[i], "side": "buy"})
        elif position == 1 and act == "sell":
            cash = qty * px * (1.0 - rate)
            qty = 0.0
            position = 0
            trades += 1
            trade_dates.append(dates[i])
            trade_events.append({"date": dates[i], "side": "sell"})
        equity.append(cash + qty * px)
    final = equity[-1] if equity else starting_cash
    ret = (final / starting_cash) - 1.0 if starting_cash else 0.0
    return {
        "return": ret,
        "max_drawdown": _max_drawdown(equity),
        "trades": trades,
        "final_equity": final,
        "equity": equity,
        "trade_dates": trade_dates,
        "trade_events": trade_events,
    }


def _pack(name: str, sim: dict) -> dict:
    ret = round(float(sim["return"]), 6)
    dd = round(float(sim["max_drawdown"]), 6)
    score = round(ret - DD_PENALTY * dd, 6)
    return {
        "name": name,
        "return": ret,
        "max_drawdown": dd,
        "trades": int(sim["trades"]),
        "score": score,
        "final_equity": round(float(sim["final_equity"]), 2),
    }


def _pack_costs(name: str, gross: dict, net: dict) -> dict:
    """Selection fields (return, max_drawdown, score) are net of fee and slippage."""
    packed = _pack(name, net)
    gross_packed = _pack(name, gross)
    packed.update(
        {
            "gross_return": gross_packed["return"],
            "gross_max_drawdown": gross_packed["max_drawdown"],
            "gross_score": gross_packed["score"],
            "gross_final_equity": gross_packed["final_equity"],
            "net_return": packed["return"],
            "net_max_drawdown": packed["max_drawdown"],
            "net_score": packed["score"],
            "net_final_equity": packed["final_equity"],
        }
    )
    return packed


def _select_name(ranked: list[dict]) -> str:
    if not ranked or float(ranked[0]["score"]) < 0:
        return "cash"
    return str(ranked[0]["name"])


def _trade_phrase(n: int) -> str:
    return f"{n} trade" if n == 1 else f"{n} trades"


def _selection_reason(ranked: list[dict], bh: dict) -> str:
    best = ranked[0]
    parts = [
        (
            f"Selected {best['name']} because its net score {best['score']:.4f} "
            f"is the best trading strategy. "
            f"Net score = net return - {DD_PENALTY:g} * net max drawdown, "
            f"after {FEE_BPS:g} bps fee and {SLIPPAGE_BPS:g} bps slippage per side."
        ),
        (
            f"{best['name']} net return {best['return'] * 100:.2f}% "
            f"(gross {best.get('gross_return', best['return']) * 100:.2f}%), "
            f"net max drawdown {best['max_drawdown'] * 100:.2f}%, {_trade_phrase(best['trades'])}."
        ),
    ]
    for other in ranked[1:]:
        parts.append(
            f"{other['name']} net score {other['score']:.4f} "
            f"(net return {other['return'] * 100:.2f}%, gross {other.get('gross_return', other['return']) * 100:.2f}%, "
            f"net max drawdown {other['max_drawdown'] * 100:.2f}%, "
            f"{_trade_phrase(other['trades'])})."
        )
    parts.append(
        f"Buy-and-hold net return {bh['return'] * 100:.2f}% "
        f"(gross {bh.get('gross_return', bh['return']) * 100:.2f}%), "
        f"net max drawdown {bh['max_drawdown'] * 100:.2f}%, {_trade_phrase(bh['trades'])}. "
        "Buy-and-hold is the benchmark, not a candidate the agent can switch into."
    )
    return " ".join(parts)


def _rank(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-r["score"], -r["return"], r["trades"], r["name"]))


def score_headline(text: str) -> dict:
    scratch = (text or "").lower()
    hits: list[dict] = []
    for phrase, weight in _PHRASES:
        if phrase in scratch:
            hits.append({"token": phrase, "weight": weight})
            scratch = scratch.replace(phrase, " ")
    for token in re.findall(r"[a-z]+", scratch):
        if token in _LEXICON:
            hits.append({"token": token, "weight": _LEXICON[token]})
    raw = sum(int(h["weight"]) for h in hits)
    score = max(-1.0, min(1.0, raw / 2.0))
    return {"score": round(score, 4), "hits": hits}


def _news_window(items: list[dict], as_of: str) -> list[dict]:
    if not as_of:
        return []
    try:
        as_of_dt = datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError:
        return []
    kept: list[dict] = []
    for it in items:
        try:
            nd = datetime.strptime(str(it.get("date")), "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        delta = (as_of_dt - nd).days
        if 0 <= delta <= NEWS_LOOKBACK_DAYS:
            kept.append(it)
    kept.sort(key=lambda x: str(x.get("date")))
    return kept


def _apply_news(tech: dict, items: list[dict], as_of: str) -> dict:
    window = _news_window(items, as_of)
    scored: list[dict] = []
    for it in window:
        scored.append(
            {
                "date": it.get("date"),
                "headline": it.get("headline") or "",
                "source": it.get("source"),
                **score_headline(it.get("headline") or ""),
            }
        )
    out = dict(tech)
    out["technical_action"] = tech["action"]
    out["technical_confidence"] = tech["confidence"]
    out["technical_reason"] = tech["reason"]
    out["news_items"] = [
        {
            "date": s["date"],
            "headline": s["headline"],
            "score": s["score"],
            "hits": s["hits"],
        }
        for s in scored
    ]
    if not scored:
        out["news_score"] = 0.0
        out["news_reason"] = (
            f"No headlines in the {NEWS_LOOKBACK_DAYS} days up to {as_of or 'the bar date'}. "
            "Technical signal stands."
        )
        out["size_multiplier"] = 1.0
        out["news_changed_decision"] = False
        return out

    worst = min(scored, key=lambda s: (s["score"], str(s["date"])))
    score = float(worst["score"])
    hits = ", ".join(f"{h['token']} ({int(h['weight']):+d})" for h in worst["hits"]) or "no lexicon words"
    noun = "headline" if len(scored) == 1 else "headlines"
    base = (
        f"Looked at {len(scored)} {noun} in the last {NEWS_LOOKBACK_DAYS} days. "
        f"The lowest-scoring was \"{worst['headline']}\" ({worst['date']}). "
        f"Lexicon hits: {hits}. "
        "Score is the weight sum divided by 2 and clamped to [-1, 1]."
    )
    action = tech["action"]
    conf = float(tech["confidence"])
    size = 1.0
    note: str | None = None
    if action == "buy" and score <= NEWS_VETO_SCORE:
        action = "hold"
        conf = round(conf * 0.6, 3)
        size = 0.0
        note = "Strongly negative news vetoed the buy (score at or below -0.60)."
    elif action == "buy" and score <= NEWS_MILD_SCORE:
        conf = round(conf * 0.6, 3)
        size = 0.5
        note = "Negative news lowered confidence to 60% and cut size in half."
    elif action == "buy" and score >= NEWS_POSITIVE_SCORE:
        conf = round(min(1.0, conf + 0.10), 3)
        note = "Positive news raised confidence by 0.10."
    elif action == "sell" and score <= NEWS_VETO_SCORE:
        conf = round(min(1.0, conf + 0.10), 3)
        note = "Strongly negative news raised sell confidence by 0.10."
    elif action == "sell" and score >= NEWS_POSITIVE_SCORE:
        conf = round(max(0.0, conf * 0.85), 3)
        note = "Positive news lowered sell confidence slightly (multiplied by 0.85)."
    elif action == "hold" and score <= NEWS_VETO_SCORE:
        conf = round(conf * 0.6, 3)
        note = "Strongly negative news would veto a buy. Confidence was reduced."
    elif action == "hold" and score <= NEWS_MILD_SCORE:
        conf = round(conf * 0.6, 3)
        note = "Negative news lowered confidence. It does not create a trade by itself."
    elif action == "hold" and score >= NEWS_POSITIVE_SCORE:
        conf = round(min(1.0, conf + 0.10), 3)
        note = "Positive news raised confidence by 0.10. It does not create a buy by itself."
    if note is None:
        if score <= NEWS_MILD_SCORE:
            note = "News is negative but not strong enough to change this signal."
        elif score >= NEWS_POSITIVE_SCORE:
            note = "News is positive but does not change this signal."
        else:
            note = "News is neutral, so the technical signal stands."
    out["action"] = action
    out["confidence"] = round(conf, 3)
    out["size_multiplier"] = size
    out["news_score"] = round(score, 4)
    out["news_reason"] = f"{base} {note}"
    out["news_changed_decision"] = action != tech["action"]
    if action != tech["action"]:
        out["reason"] = f"{tech['reason']}. {note}"
    else:
        out["reason"] = tech["reason"]
    return out


def _technical(strategy: str, bars: list[dict], fast: int, slow: int, symbol: str) -> dict:
    closes = [float(b["close"]) for b in bars]
    dates = [b["date"] for b in bars]
    base = {
        "symbol": symbol,
        "strategy": strategy,
        "action": "hold",
        "confidence": 0.0,
        "reason": "insufficient_bars",
        "price": closes[-1] if closes else None,
        "date": dates[-1] if dates else None,
    }
    if strategy == "sma_crossover":
        if len(closes) < slow + 1:
            return base
        f = _sma(closes, fast)
        s = _sma(closes, slow)
        i = len(closes) - 1
        j = i - 1
        if None in (f[i], s[i], f[j], s[j]):
            base["reason"] = "warmup"
            return base
        action = "hold"
        reason = f"SMA{fast} did not cross SMA{slow}"
        if f[j] <= s[j] and f[i] > s[i]:
            action = "buy"
            reason = f"SMA{fast} crossed above SMA{slow}"
        elif f[j] >= s[j] and f[i] < s[i]:
            action = "sell"
            reason = f"SMA{fast} crossed below SMA{slow}"
        spread = abs(f[i] - s[i]) / closes[i] if closes[i] else 0.0
        base.update(
            {
                "action": action,
                "confidence": round(min(1.0, spread * 50), 3),
                "reason": reason,
                "sma_fast": round(f[i], 4),
                "sma_slow": round(s[i], 4),
            }
        )
        return base
    if strategy == "rsi_mean_reversion":
        if len(closes) < RSI_PERIOD + 2:
            return base
        rv = _rsi(closes, RSI_PERIOD)
        i = len(closes) - 1
        if rv[i] is None or rv[i - 1] is None:
            base["reason"] = "warmup"
            return base
        action = "hold"
        reason = (
            f"RSI({RSI_PERIOD}) {rv[i]:.1f} has not crossed {RSI_OVERSOLD:g} or {RSI_OVERBOUGHT:g}"
        )
        if rv[i - 1] >= RSI_OVERSOLD and rv[i] < RSI_OVERSOLD:
            action = "buy"
            reason = f"RSI({RSI_PERIOD}) crossed below {RSI_OVERSOLD:g} (mean-reversion buy)"
        elif rv[i - 1] <= RSI_OVERBOUGHT and rv[i] > RSI_OVERBOUGHT:
            action = "sell"
            reason = f"RSI({RSI_PERIOD}) crossed above {RSI_OVERBOUGHT:g} (mean-reversion exit)"
        depth = abs(rv[i] - (RSI_OVERSOLD if action == "buy" else RSI_OVERBOUGHT)) / 100.0
        base.update(
            {
                "action": action,
                "confidence": round(min(1.0, 0.35 + depth), 3),
                "reason": reason,
                "rsi": round(rv[i], 4),
                "rsi_prev": round(rv[i - 1], 4),
            }
        )
        return base
    if strategy == "bollinger_breakout":
        if len(closes) < BB_PERIOD + 1:
            return base
        mid, upper, lower = _bollinger(closes, BB_PERIOD, BB_K)
        i = len(closes) - 1
        if None in (mid[i], mid[i - 1], upper[i], upper[i - 1]):
            base["reason"] = "warmup"
            return base
        action = "hold"
        reason = (
            f"close {closes[i]:.2f} inside Bollinger bands "
            f"(upper {upper[i]:.2f}, mid {mid[i]:.2f}); no breakout"
        )
        if closes[i - 1] <= upper[i - 1] and closes[i] > upper[i]:
            action = "buy"
            reason = f"close crossed above the upper Bollinger band ({BB_PERIOD}, {BB_K:g} sd)"
        elif closes[i - 1] >= mid[i - 1] and closes[i] < mid[i]:
            action = "sell"
            reason = f"close crossed below the Bollinger midline ({BB_PERIOD})"
        stretch = abs(closes[i] - upper[i]) / closes[i] if closes[i] else 0.0
        base.update(
            {
                "action": action,
                "confidence": round(min(1.0, max(stretch * 50, 0.2 if action != "hold" else stretch * 50)), 3),
                "reason": reason,
                "bb_upper": round(upper[i], 4),
                "bb_mid": round(mid[i], 4),
                "bb_lower": round(lower[i], 4) if lower[i] is not None else None,
            }
        )
        return base
    raise HTTPException(400, f"unknown strategy {strategy}")


def _check_strategy(name: str, fast: int, slow: int) -> None:
    if name not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy {name}; choose one of {', '.join(STRATEGIES)}")
    if name == "sma_crossover" and fast >= slow:
        raise HTTPException(400, "fast must be < slow")


def _bars_as_of(as_of: str | None) -> list[dict]:
    bars = _fetch_bars(limit=2000)
    if not as_of:
        return bars
    try:
        datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, "as_of_date must be YYYY-MM-DD") from exc
    sliced = [b for b in bars if b["date"] <= as_of]
    if not sliced:
        raise HTTPException(400, "as_of_date is before the first bar")
    return sliced


def _news_veto_example(bars: list[dict], news: list[dict], fast: int, slow: int, symbol: str) -> dict | None:
    """First historical buy whose news rule changes the action. Price backtests ignore this."""
    for strategy in STRATEGIES:
        actions = _actions(strategy, [float(b["close"]) for b in bars], fast, slow)
        for i, act in enumerate(actions):
            if act != "buy":
                continue
            tech = _technical(strategy, bars[: i + 1], fast, slow, symbol)
            if tech["action"] != "buy":
                continue
            adjusted = _apply_news(tech, news, bars[i]["date"])
            if not adjusted["news_changed_decision"]:
                continue
            return {
                "date": bars[i]["date"],
                "strategy": strategy,
                "price": tech.get("price"),
                "technical_action": "buy",
                "technical_reason": tech["reason"],
                "action": adjusted["action"],
                "news_score": adjusted["news_score"],
                "news_reason": adjusted["news_reason"],
                "technical_confidence": adjusted["technical_confidence"],
                "confidence": adjusted["confidence"],
                "size_multiplier": adjusted["size_multiplier"],
            }
    return None


def _news_boost_example(bars: list[dict], news: list[dict], fast: int, slow: int, symbol: str) -> dict | None:
    """First buy whose positive headlines raise confidence by 0.10 and leave the buy in place."""
    for strategy in STRATEGIES:
        actions = _actions(strategy, [float(b["close"]) for b in bars], fast, slow)
        for i, act in enumerate(actions):
            if act != "buy":
                continue
            tech = _technical(strategy, bars[: i + 1], fast, slow, symbol)
            if tech["action"] != "buy":
                continue
            adjusted = _apply_news(tech, news, bars[i]["date"])
            if adjusted["action"] != "buy":
                continue
            delta = round(float(adjusted["confidence"]) - float(adjusted["technical_confidence"]), 3)
            if float(adjusted["news_score"]) < NEWS_POSITIVE_SCORE or delta != 0.10:
                continue
            if float(adjusted["size_multiplier"]) != 1.0:
                continue
            return {
                "date": bars[i]["date"],
                "strategy": strategy,
                "price": tech.get("price"),
                "technical_action": "buy",
                "action": "buy",
                "news_score": adjusted["news_score"],
                "news_reason": adjusted["news_reason"],
                "technical_confidence": adjusted["technical_confidence"],
                "confidence": adjusted["confidence"],
                "size_multiplier": adjusted["size_multiplier"],
            }
    return None


def _load_regimes() -> dict:
    path = _seed_path("SEED_REGIME_PATH", "./seeds/ohlcv_regimes.json")
    if not path.exists():
        return {"label": "", "regimes": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"label": "", "regimes": []}
    data.setdefault("regimes", [])
    return data


def _window_metrics(name: str, sim: dict, dates: list[str], start: str, end: str) -> dict:
    """Continuous-backtest result inside one regime. The prior close is the anchor."""
    anchor = None
    last = None
    for day, equity in zip(dates, sim["equity"]):
        if day < start:
            anchor = equity
        elif day <= end:
            last = equity
    series: list[float] = []
    if anchor is not None:
        series.append(float(anchor))
    for day, equity in zip(dates, sim["equity"]):
        if start <= day <= end:
            series.append(float(equity))
    if len(series) < 2 or last is None:
        ret = 0.0
        dd = 0.0
        final = float(series[-1]) if series else 0.0
    else:
        ret = (series[-1] / series[0]) - 1.0 if series[0] else 0.0
        dd = _max_drawdown(series)
        final = series[-1]
    trades = sum(1 for day in sim.get("trade_dates") or [] if start <= day <= end)
    packed = _pack(
        name,
        {"return": ret, "max_drawdown": dd, "trades": trades, "final_equity": final},
    )
    return packed


def _with_gross(net_row: dict, gross_row: dict) -> dict:
    net_row = dict(net_row)
    net_row.update(
        {
            "gross_return": gross_row["return"],
            "gross_max_drawdown": gross_row["max_drawdown"],
            "gross_score": gross_row["score"],
            "gross_final_equity": gross_row["final_equity"],
            "net_return": net_row["return"],
            "net_max_drawdown": net_row["max_drawdown"],
            "net_score": net_row["score"],
            "net_final_equity": net_row["final_equity"],
        }
    )
    return net_row


def _regime_reports(
    net_sims: dict[str, dict],
    gross_sims: dict[str, dict],
    net_bh: dict,
    gross_bh: dict,
    dates: list[str],
    meta: dict,
) -> list[dict]:
    reports = []
    for reg in meta.get("regimes") or []:
        start = str(reg.get("start") or "")
        end = str(reg.get("end") or "")
        if not start or not end:
            continue
        rows = [
            _with_gross(
                _window_metrics(name, net_sims[name], dates, start, end),
                _window_metrics(name, gross_sims[name], dates, start, end),
            )
            for name in STRATEGIES
        ]
        bh = _with_gross(
            _window_metrics("buy_and_hold", net_bh, dates, start, end),
            _window_metrics("buy_and_hold", gross_bh, dates, start, end),
        )
        ranked = _rank(rows)
        reports.append(
            {
                "name": reg.get("name"),
                "start": start,
                "end": end,
                "bars": reg.get("bars"),
                "label": (
                    "MOCK regime. Net return and net max drawdown are measured on the continuous "
                    "backtest inside this window, including a position carried in. "
                    "Costs are fee plus slippage on each fill."
                ),
                "selected_strategy": _select_name(ranked),
                "strategies": rows,
                "buy_hold": bh,
            }
        )
    return reports


class SignalRequest(BaseModel):
    symbol: str = "SYNTH"
    fast: int = Field(default=FAST, ge=2)
    slow: int = Field(default=SLOW, ge=3)
    strategy: str = "sma_crossover"
    as_of_date: str | None = None


class BacktestRequest(BaseModel):
    symbol: str = "SYNTH"
    fast: int = Field(default=FAST, ge=2)
    slow: int = Field(default=SLOW, ge=3)
    starting_cash: float = 100000.0
    fee_bps: float | None = None
    slippage_bps: float | None = None
    current_strategy: str | None = None
    switch_margin: float | None = None


def _stop_defaults() -> dict:
    return {
        "suggested_stop_pct": float(os.getenv("RISK_STOP_LOSS_PCT", "0.02")),
        "suggested_rr": float(os.getenv("RISK_MIN_RR", "1.5")),
    }


@app.get("/health")
def health():
    return {
        "service": "strategy-engine",
        "status": "ok",
        "strategies": list(STRATEGIES) + ["cash"],
        "metric": "net_return_minus_drawdown_penalty",
        "drawdown_penalty": DD_PENALTY,
        "fee_bps": FEE_BPS,
        "slippage_bps": SLIPPAGE_BPS,
        "switch_margin": SWITCH_MARGIN,
        "sma_fast": FAST,
        "sma_slow": SLOW,
        "rsi_period": RSI_PERIOD,
        "bb_period": BB_PERIOD,
    }


@app.post("/signal")
def signal(body: SignalRequest):
    if body.strategy == "cash":
        bars = _bars_as_of(body.as_of_date)
        last = bars[-1] if bars else {}
        return {
            "symbol": body.symbol,
            "strategy": "cash",
            "action": "hold",
            "technical_action": "hold",
            "confidence": 0.0,
            "technical_confidence": 0.0,
            "reason": "Net score is negative, so the book stays in cash.",
            "technical_reason": "cash",
            "price": last.get("close"),
            "date": last.get("date"),
            "news_score": 0.0,
            "news_reason": "Cash has no technical signal.",
            "news_changed_decision": False,
            "size_multiplier": 0.0,
            **_stop_defaults(),
        }
    _check_strategy(body.strategy, body.fast, body.slow)
    bars = _bars_as_of(body.as_of_date)
    tech = _technical(body.strategy, bars, body.fast, body.slow, body.symbol)
    out = _apply_news(tech, _fetch_news(), tech.get("date") or "")
    out.update(_stop_defaults())
    return out


@app.post("/backtest")
def backtest(body: BacktestRequest):
    if body.fast >= body.slow:
        raise HTTPException(400, "fast must be < slow")
    bars = _fetch_bars(limit=2000)
    closes = [float(b["close"]) for b in bars]
    dates = [b["date"] for b in bars]
    need = max(body.slow + 2, BB_PERIOD + 2, RSI_PERIOD + 2)
    if len(closes) < need:
        raise HTTPException(400, "not enough bars")

    fee = FEE_BPS if body.fee_bps is None else float(body.fee_bps)
    slip = SLIPPAGE_BPS if body.slippage_bps is None else float(body.slippage_bps)
    margin = SWITCH_MARGIN if body.switch_margin is None else float(body.switch_margin)
    rate = _cost_rate(fee, slip)
    rows = []
    curves: dict[str, list] = {}
    net_sims: dict[str, dict] = {}
    gross_sims: dict[str, dict] = {}
    for name in STRATEGIES:
        actions = _actions(name, closes, body.fast, body.slow)
        gross = _simulate(closes, dates, actions, body.starting_cash, 0.0)
        net = _simulate(closes, dates, actions, body.starting_cash, rate)
        gross_sims[name] = gross
        net_sims[name] = net
        rows.append(_pack_costs(name, gross, net))
        curves[name] = [
            {"date": d, "equity": round(e, 2)} for d, e in zip(dates, net["equity"])
        ]
    bh_actions = ["buy"] + ["hold"] * (len(closes) - 1)
    bh_gross = _simulate(closes, dates, bh_actions, body.starting_cash, 0.0)
    bh_net = _simulate(closes, dates, bh_actions, body.starting_cash, rate)
    bh = _pack_costs("buy_and_hold", bh_gross, bh_net)
    ranked = _rank(rows)
    best = ranked[0]
    chosen = _select_name(ranked)
    reason = _selection_reason(ranked, bh)
    if chosen == "cash":
        reason = (
            f"Best net score is {best['score']:.4f} for {best['name']}, which is negative, "
            "so the book stays in cash. " + reason
        )
    elif body.current_strategy:
        chosen, hyst = apply_hysteresis(rows, body.current_strategy, margin)
        reason = hyst + " " + reason
    news = _fetch_news()
    example = _news_veto_example(bars, news, body.fast, body.slow, body.symbol)
    boost = _news_boost_example(bars, news, body.fast, body.slow, body.symbol)
    meta = _load_regimes()
    regimes = _regime_reports(net_sims, gross_sims, bh_net, bh_gross, dates, meta)
    last_day = dates[-1]
    current = next((reg for reg in regimes if reg["start"] <= last_day <= reg["end"]), None)
    regime_strategy = (current or {}).get("selected_strategy") or chosen
    veto_reason = (example or {}).get("news_reason") or ""
    scored_news = []
    for item in news:
        headline = item.get("headline") or ""
        scored = score_headline(headline)
        scored_news.append(
            {
                "date": item.get("date"),
                "headline": headline,
                "source": item.get("source"),
                "regime": item.get("regime"),
                "score": scored["score"],
                "veto": bool(headline)
                and headline in veto_reason
                and str(item.get("date") or "") in veto_reason
                and scored["score"] <= NEWS_VETO_SCORE,
            }
        )
    shown = best if chosen == "cash" else next(row for row in rows if row["name"] == chosen)
    summary = (
        f"Selected {chosen} net score {shown['score']:.4f} "
        f"(net return {shown['return'] * 100:.2f}%, gross {shown['gross_return'] * 100:.2f}%, "
        f"net max drawdown {shown['max_drawdown'] * 100:.2f}%, {_trade_phrase(shown['trades'])}). "
        f"Buy-and-hold net return {bh['return'] * 100:.2f}%, gross {bh['gross_return'] * 100:.2f}%, "
        f"net max drawdown {bh['max_drawdown'] * 100:.2f}%, {_trade_phrase(bh['trades'])}. "
        f"Costs are {fee:g} bps fee plus {slip:g} bps slippage per side."
    )
    curve_name = best["name"] if chosen == "cash" else chosen
    return {
        "symbol": body.symbol,
        "metric": "net_return_minus_drawdown_penalty",
        "drawdown_penalty": DD_PENALTY,
        "fee_bps": fee,
        "slippage_bps": slip,
        "switch_margin": margin,
        "metric_note": "net score = net return - drawdown_penalty * net max drawdown; costs are fee_bps + slippage_bps per side; higher wins; a negative net score selects cash; buy-and-hold is the benchmark only",
        "selected_strategy": chosen,
        "unconstrained_strategy": _select_name(ranked),
        "selection_reason": reason,
        "strategy": chosen,
        "strategy_return": shown["return"],
        "gross_return": shown["gross_return"],
        "net_return": shown["net_return"],
        "max_drawdown": shown["max_drawdown"],
        "gross_max_drawdown": shown["gross_max_drawdown"],
        "trades": shown["trades"],
        "final_equity": shown["final_equity"],
        "buy_hold_return": bh["return"],
        "buy_hold_gross_return": bh["gross_return"],
        "buy_hold_max_drawdown": bh["max_drawdown"],
        "buy_hold_gross_max_drawdown": bh["gross_max_drawdown"],
        "buy_hold_trades": bh["trades"],
        "buy_hold_final": bh["final_equity"],
        "outperformance": round(shown["return"] - bh["return"], 6),
        "starting_cash": body.starting_cash,
        "bars": len(closes),
        "news_in_backtest": False,
        "strategies": rows,
        "buy_hold": bh,
        "equity_curve": curves[curve_name] if chosen != "cash" else [{"date": d, "equity": body.starting_cash} for d in dates],
        "trade_markers": [] if chosen == "cash" else net_sims[curve_name].get("trade_events") or [],
        "scored_news": scored_news,
        "buy_hold_curve": [
            {"date": d, "equity": round(e, 2)} for d, e in zip(dates, bh_net["equity"])
        ],
        "summary": summary,
        "mock_label": meta.get("label") or "",
        "current_regime": (current or {}).get("name"),
        "regime_strategy": regime_strategy,
        "regimes": regimes,
        "news_decision_example": example,
        "news_boost_example": boost,
    }
