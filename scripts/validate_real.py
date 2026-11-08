#!/usr/bin/env python3
"""Paper-only validation of a local OHLCV CSV. Never calls a broker or MT5.

Resamples timestamp,open,high,low,close,volume bars, runs the three Sentinel
strategies against buy-and-hold, checks that result with pandas, then exercises
the running risk-guard / paper-broker / agent on localhost. Writes
data/validation/validation_results.json (gitignored).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from shared.selection import apply_hysteresis  # noqa: E402

OUT_DIR = ROOT / "data" / "validation"
OUT_PATH = OUT_DIR / "validation_results.json"
BARS = {"M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}
CAVEATS = [
    "The window is only as long as the CSV you passed. A winner here is not a claim about any other period.",
    "Price backtests ignore news. If the running services have no headlines, news_score stays 0.",
    "Default costs are 10 bps fee plus 5 bps slippage on every entry and every exit. Gross figures ignore those costs. Selection uses the net score.",
    "Resample is left-closed and left-labeled. Hours with no source bars are omitted. Partial bins stay in the sample.",
    "Fixed mock regimes (rally, crash, chop, recovery) are not applied to these bars. The score is the full sample.",
    "Risk tests hit the local paper services only. The forced fill, oversized deny, and daily-loss deny use those services. They do not place a live order.",
    "Kill switch, position cap, and daily loss limit are the service's own checks.",
]


def _engine():
    path = ROOT / "services" / "strategy-engine" / "main.py"
    spec = importlib.util.spec_from_file_location("sentinel_strategy_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in frame.columns}
    need = ["timestamp", "open", "high", "low", "close"]
    missing = [name for name in need if name not in cols]
    if missing:
        raise SystemExit(f"csv missing columns: {missing}")
    out = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(frame[cols["timestamp"]], utc=False),
            "open": frame[cols["open"]].astype(float),
            "high": frame[cols["high"]].astype(float),
            "low": frame[cols["low"]].astype(float),
            "close": frame[cols["close"]].astype(float),
            "volume": frame[cols["volume"]].astype(float) if "volume" in cols else 0.0,
        }
    )
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    out = out.sort_values("timestamp")
    return out


def _resample(frame: pd.DataFrame, bars: str) -> pd.DataFrame:
    rule = BARS[bars]
    indexed = frame.set_index("timestamp")
    grouped = indexed.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    grouped = grouped.dropna(subset=["open", "close"])
    grouped = grouped.reset_index()
    return grouped


def _curve(dates: list[str], equity: list[float]) -> list[dict]:
    return [{"date": d, "equity": round(float(e), 2)} for d, e in zip(dates, equity)]


def _dates(bars: pd.DataFrame) -> list[str]:
    return [ts.strftime("%Y-%m-%d %H:%M:%S") for ts in bars["timestamp"]]


def _one(engine, closes: list[float], dates: list[str], name: str, rate: float, bb_period: int | None = None, bb_k: float | None = None):
    cash = 100000.0
    if name == "buy_and_hold":
        actions = ["buy"] + ["hold"] * (len(closes) - 1)
    else:
        actions = engine._actions(name, closes, engine.FAST, engine.SLOW, bb_period, bb_k)
    gross = engine._simulate(closes, dates, actions, cash, 0.0)
    net = engine._simulate(closes, dates, actions, cash, rate)
    return engine._pack_costs(name, gross, net), net, gross


def _backtest(engine, bars: pd.DataFrame, fee_bps: float | None = None, slippage_bps: float | None = None) -> dict:
    dates = _dates(bars)
    closes = [float(v) for v in bars["close"]]
    fee = engine.FEE_BPS if fee_bps is None else fee_bps
    slip = engine.SLIPPAGE_BPS if slippage_bps is None else slippage_bps
    rate = engine._cost_rate(fee, slip)
    sims = {}
    rows = []
    for name in engine.STRATEGIES:
        packed, net, _gross = _one(engine, closes, dates, name, rate)
        sims[name] = net
        rows.append(packed)
    bh, bh_sim, _bh_gross = _one(engine, closes, dates, "buy_and_hold", rate)
    ranked = engine._rank(rows)
    best = ranked[0]
    return {
        "dates": dates,
        "closes": closes,
        "sims": sims,
        "rows": rows,
        "bh": bh,
        "bh_sim": bh_sim,
        "best": best,
        "selected": engine._select_name(ranked),
        "reason": engine._selection_reason(ranked, bh),
        "penalty": engine.DD_PENALTY,
        "fee_bps": fee,
        "slippage_bps": slip,
        "cost_rate": rate,
    }


def _max_dd(equity: pd.Series) -> float:
    peak = equity.cummax()
    drop = (peak - equity) / peak
    return float(drop.max()) if len(drop) else 0.0


def _sma_events(dates: list[str], closes: list[float]) -> list[dict]:
    series = pd.Series(closes, dtype=float)
    fast = series.rolling(5).mean()
    slow = series.rolling(15).mean()
    prev_fast = fast.shift(1)
    prev_slow = slow.shift(1)
    position = 0
    events = []
    for i in range(len(closes)):
        if pd.isna(prev_fast.iloc[i]) or pd.isna(prev_slow.iloc[i]) or pd.isna(fast.iloc[i]) or pd.isna(slow.iloc[i]):
            continue
        buy = position == 0 and prev_fast.iloc[i] <= prev_slow.iloc[i] and fast.iloc[i] > slow.iloc[i]
        sell = position == 1 and prev_fast.iloc[i] >= prev_slow.iloc[i] and fast.iloc[i] < slow.iloc[i]
        if buy:
            position = 1
            events.append({"date": dates[i], "side": "buy"})
        elif sell:
            position = 0
            events.append({"date": dates[i], "side": "sell"})
    return events


def _no_lookahead(engine, closes: list[float]) -> dict:
    by_strategy = {}
    ok = True
    for name in engine.STRATEGIES:
        full = engine._actions(name, closes, engine.FAST, engine.SLOW)
        failure = None
        step = 1 if len(closes) <= 2500 else max(1, len(closes) // 400)
        indexes = list(range(0, len(closes), step))
        if indexes[-1] != len(closes) - 1:
            indexes.append(len(closes) - 1)
        for i in indexes:
            prefix = engine._actions(name, closes[: i + 1], engine.FAST, engine.SLOW)
            if prefix[-1] != full[i]:
                failure = f"prefix mismatch at {i}"
                break
        if failure is None and closes:
            shocked = closes[:-1] + [closes[-1] * 1.25 if closes[-1] else 1.0]
            shocked_actions = engine._actions(name, shocked, engine.FAST, engine.SLOW)
            if shocked_actions[:-1] != full[:-1]:
                failure = "earlier signals changed when the last close changed"
        by_strategy[name] = {
            "prefix_and_shock": failure is None,
            "failure": failure,
            "prefixes_checked": len(indexes),
        }
        ok = ok and failure is None
    return {"pass": ok, "by_strategy": by_strategy}


def _checks(engine, result: dict) -> dict:
    dates = result["dates"]
    closes = result["closes"]
    cash = 100000.0
    close = pd.Series(closes, dtype=float)
    equity = cash * close / close.iloc[0]
    pandas_ret = float(equity.iloc[-1] / cash - 1.0)
    pandas_dd = _max_dd(equity)
    engine_bh = result["bh"]
    gross_ret = engine_bh.get("gross_return", engine_bh["return"])
    gross_dd = engine_bh.get("gross_max_drawdown", engine_bh["max_drawdown"])
    bh_pass = (
        round(pandas_ret, 6) == gross_ret
        and round(pandas_dd, 6) == gross_dd
        and engine_bh["trades"] == 1
    )
    pandas_sma = _sma_events(dates, closes)
    engine_sma = result["sims"]["sma_crossover"].get("trade_events") or []
    sma_pass = pandas_sma == engine_sma
    look = _no_lookahead(engine, closes)
    ranked = sorted(
        result["rows"],
        key=lambda row: (-float(row["score"]), -float(row["return"]), int(row["trades"]), str(row["name"])),
    )
    best_name = result["best"]["name"]
    return {
        "buy_and_hold": {
            "pass": bh_pass,
            "pandas_return_rounded": round(pandas_ret, 6),
            "engine_return": gross_ret,
            "engine_net_return": engine_bh["return"],
            "pandas_max_drawdown_rounded": round(pandas_dd, 6),
            "engine_max_drawdown": gross_dd,
            "engine_net_max_drawdown": engine_bh["max_drawdown"],
            "engine_trades": engine_bh["trades"],
        },
        "sma_trade_dates": {
            "pass": sma_pass,
            "pandas_trades": len(pandas_sma),
            "engine_trades": len(engine_sma),
            "mismatch_count": 0 if sma_pass else sum(1 for a, b in zip(pandas_sma, engine_sma) if a != b) + abs(len(pandas_sma) - len(engine_sma)),
        },
        "no_lookahead": look,
        "score_selection": {
            "pass": ranked[0]["name"] == best_name,
            "ranked_first": ranked[0]["name"],
            "engine_selected": best_name,
            "score": result["best"]["score"],
        },
    }


def _urls() -> dict[str, str]:
    return {
        "risk": os.getenv("RISK_GUARD_URL", "http://127.0.0.1:8003"),
        "broker": os.getenv("PAPER_BROKER_URL", "http://127.0.0.1:8004"),
        "agent": os.getenv("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8005"),
        "market": os.getenv("MARKET_INGEST_URL", "http://127.0.0.1:8001"),
    }


def _state_files() -> list[Path]:
    names = ["kill_switch.json", "risk_state.json", "broker_state.json"]
    paths = []
    for name in names:
        env_key = {
            "kill_switch.json": "KILL_SWITCH_PATH",
            "risk_state.json": "RISK_STATE_PATH",
            "broker_state.json": "BROKER_STATE_PATH",
        }[name]
        raw = os.getenv(env_key, str(ROOT / "data" / name))
        path = Path(raw)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        paths.append(path)
    return paths


def _snapshot(paths: list[Path]) -> dict[Path, str | None]:
    saved = {}
    for path in paths:
        saved[path] = path.read_text() if path.is_file() else None
    return saved


def _restore(saved: dict[Path, str | None]) -> None:
    for path, text in saved.items():
        if text is None:
            if path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _risk_tests(symbol: str) -> dict:
    urls = _urls()
    saved = _snapshot(_state_files())
    out: dict = {}
    try:
        with httpx.Client(timeout=30.0) as client:
            client.post(f"{urls['broker']}/reset")
            client.post(f"{urls['risk']}/kill-switch", json={"killed": True})
            blocked = client.post(f"{urls['agent']}/run", json={"goal": "paper_cycle", "symbol": symbol}).json()
            out["kill_switch"] = {
                "status": blocked.get("status"),
                "pass": blocked.get("status") == "blocked_kill_switch",
            }
            client.post(f"{urls['risk']}/kill-switch", json={"killed": False})
            # Zero the daily counter so this test does not inherit an old breach.
            risk_path = _state_files()[1]
            risk_path.parent.mkdir(parents=True, exist_ok=True)
            risk_path.write_text(json.dumps({"day": "2099-01-01", "trades_today": 0, "realized_pnl_today": 0.0}))
            price = float(client.get(f"{urls['market']}/last-price").json()["price"])
            equity = float(client.get(f"{urls['broker']}/account").json()["equity"])
            stop = round(price * (1 - 0.02), 4)
            take = round(price * (1 + 0.02 * 1.5), 4)
            qty = max(0.0001, round((equity * 0.04) / price, 4))
            allowed = client.post(
                f"{urls['risk']}/check-order",
                json={
                    "symbol": symbol,
                    "side": "buy",
                    "qty": qty,
                    "price": price,
                    "equity": equity,
                    "stop_loss": stop,
                    "take_profit": take,
                    "open_notional": 0,
                    "daily_pnl": 0,
                },
            ).json()
            fill = None
            if allowed.get("allowed"):
                fill = client.post(
                    f"{urls['broker']}/orders",
                    json={
                        "symbol": symbol,
                        "side": "buy",
                        "qty": qty,
                        "price": price,
                        "risk_allowed": True,
                        "stop_loss": stop,
                        "take_profit": take,
                        "reason": "validation paper fill",
                    },
                ).json()
            actual_stop = (fill or {}).get("fill", {}).get("stop_loss")
            actual_tp = (fill or {}).get("fill", {}).get("take_profit")
            out["force_fill"] = {
                "status": (fill or {}).get("status"),
                "pass": (fill or {}).get("status") == "filled" and actual_stop == stop and actual_tp == take,
                "stop_math": {
                    "price": price,
                    "expected_stop": stop,
                    "expected_take_profit": take,
                    "actual_stop": actual_stop,
                    "actual_take_profit": actual_tp,
                    "matches_engine_rounding": actual_stop == stop and actual_tp == take,
                },
                "order": fill,
                "risk": allowed,
            }
            huge = (equity * 0.20) / price
            oversized = client.post(
                f"{urls['risk']}/check-order",
                json={
                    "symbol": symbol,
                    "side": "buy",
                    "qty": huge,
                    "price": price,
                    "equity": equity,
                    "stop_loss": round(price * 0.99, 4),
                    "take_profit": round(price * 1.05, 4),
                },
            ).json()
            out["oversized"] = {
                "status": "risk_denied" if oversized.get("allowed") is False else "allowed",
                "pass": oversized.get("reason") == "max_position_pct",
                "risk": oversized,
            }
            sell_price = round(price * 0.1, 4)
            if fill and fill.get("status") == "filled":
                client.post(
                    f"{urls['broker']}/orders",
                    json={
                        "symbol": symbol,
                        "side": "sell",
                        "qty": qty,
                        "price": sell_price,
                        "risk_allowed": True,
                        "reason": "validation paper loss",
                    },
                )
            account = client.get(f"{urls['broker']}/account").json()
            denied = client.post(
                f"{urls['risk']}/check-order",
                json={
                    "symbol": symbol,
                    "side": "buy",
                    "qty": max(0.0001, round((float(account["equity"]) * 0.02) / price, 4)),
                    "price": price,
                    "equity": account["equity"],
                    "stop_loss": stop,
                    "take_profit": take,
                    "open_notional": 0,
                },
            ).json()
            out["daily_loss"] = {
                "status": "risk_denied" if denied.get("allowed") is False else "allowed",
                "pass": denied.get("reason") == "daily_loss_limit",
                "risk": denied,
            }
    except httpx.HTTPError as exc:
        out["error"] = str(exc)
        out.setdefault("kill_switch", {"pass": False, "status": "error"})
    finally:
        _restore(saved)
    return out


def _merge(record: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if OUT_PATH.is_file():
        raw = json.loads(OUT_PATH.read_text())
        if isinstance(raw, list):
            existing = raw
        elif isinstance(raw, dict):
            existing = list(raw.get("instruments") or [])
    kept = [item for item in existing if item.get("symbol") != record["symbol"]]
    kept.append(record)
    kept.sort(key=lambda item: str(item.get("symbol")))
    payload = {
        "banner": "Private validation on real historical data - not part of hackathon demo data",
        "caveats": CAVEATS,
        "instruments": kept,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def _stitch(bucket: list[float], equity: list[float], starting: float = 100000.0) -> None:
    """Append a window's mark-to-market path, including entry costs versus starting cash."""
    if not equity or not starting:
        return
    scale = bucket[-1] / starting
    bucket.extend(point * scale for point in equity)


def _path_stats(curve: list[float]) -> dict:
    if len(curve) < 2 or not curve[0]:
        return {"return": 0.0, "max_drawdown": 0.0}
    ret = curve[-1] / curve[0] - 1.0
    peak = curve[0]
    worst = 0.0
    for point in curve:
        peak = max(peak, point)
        if peak:
            worst = max(worst, (peak - point) / peak)
    return {"return": round(ret, 6), "max_drawdown": round(worst, 6)}


def _score(stats: dict, penalty: float) -> float:
    return round(float(stats["return"]) - penalty * float(stats["max_drawdown"]), 6)


def walk_forward(engine, bars: pd.DataFrame, train_days: int, test_days: int, rate: float, margin: float) -> dict:
    """Select on each train window. Score that choice only on the next unseen test window."""
    stamps = [pd.Timestamp(value) for value in bars["timestamp"]]
    closes = [float(v) for v in bars["close"]]
    cursor = pd.Timestamp(stamps[0])
    last = pd.Timestamp(stamps[-1])
    current = None
    names = list(engine.STRATEGIES) + ["buy_and_hold", "selected"]
    stitched = {
        name: {"gross": [100000.0], "net": [100000.0]}
        for name in names
    }
    windows = []
    while cursor < last:
        train_end = cursor + pd.Timedelta(days=int(train_days))
        test_end = train_end + pd.Timedelta(days=int(test_days))
        train_idx = [i for i, stamp in enumerate(stamps) if cursor <= stamp < train_end]
        test_idx = [i for i, stamp in enumerate(stamps) if train_end <= stamp < test_end]
        cursor = cursor + pd.Timedelta(days=int(test_days))
        if train_end >= last or len(train_idx) < 30 or len(test_idx) < 5:
            continue
        train_closes = [closes[i] for i in train_idx]
        train_dates = [stamps[i].strftime("%Y-%m-%d %H:%M:%S") for i in train_idx]
        test_closes = [closes[i] for i in test_idx]
        test_dates = [stamps[i].strftime("%Y-%m-%d %H:%M:%S") for i in test_idx]
        candidates = []
        for name in engine.STRATEGIES:
            packed, _net, _gross = _one(engine, train_closes, train_dates, name, rate)
            candidates.append(packed)
        choice, reason = apply_hysteresis(candidates, current, margin)
        current = choice
        window_rows = {}
        for name in list(engine.STRATEGIES) + ["buy_and_hold"]:
            packed, net, gross = _one(engine, test_closes, test_dates, name, rate)
            window_rows[name] = {
                "gross_return": packed["gross_return"],
                "net_return": packed["net_return"],
                "gross_max_drawdown": packed["gross_max_drawdown"],
                "net_max_drawdown": packed["net_max_drawdown"],
                "trades": packed["trades"],
                "net_score": packed["net_score"],
            }
            _stitch(stitched[name]["gross"], gross["equity"])
            _stitch(stitched[name]["net"], net["equity"])
        if choice == "cash":
            stitched["selected"]["gross"].append(stitched["selected"]["gross"][-1])
            stitched["selected"]["net"].append(stitched["selected"]["net"][-1])
            chosen_stats = {
                "gross_return": 0.0,
                "net_return": 0.0,
                "gross_max_drawdown": 0.0,
                "net_max_drawdown": 0.0,
                "trades": 0,
                "net_score": 0.0,
            }
        else:
            packed, net, gross = _one(engine, test_closes, test_dates, choice, rate)
            chosen_stats = window_rows[choice]
            _stitch(stitched["selected"]["gross"], gross["equity"])
            _stitch(stitched["selected"]["net"], net["equity"])
        windows.append(
            {
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "test_start": test_dates[0],
                "test_end": test_dates[-1],
                "choice": choice,
                "reason": reason,
                "chosen": chosen_stats,
            }
        )
    penalty = engine.DD_PENALTY
    aggregate = {}
    for name, curves in stitched.items():
        gross = _path_stats(curves["gross"])
        net = _path_stats(curves["net"])
        aggregate[name] = {
            "gross_return": gross["return"],
            "gross_max_drawdown": gross["max_drawdown"],
            "gross_score": _score(gross, penalty),
            "net_return": net["return"],
            "net_max_drawdown": net["max_drawdown"],
            "net_score": _score(net, penalty),
        }
    counts: dict[str, int] = {}
    for window in windows:
        counts[window["choice"]] = counts.get(window["choice"], 0) + 1
    return {
        "train_days": train_days,
        "test_days": test_days,
        "windows": len(windows),
        "choice_counts": counts,
        "aggregate": aggregate,
        "sample_windows": windows[:3] + windows[-3:] if len(windows) > 6 else windows,
    }


def bollinger_grid(engine, bars: pd.DataFrame, rate: float) -> dict:
    dates = _dates(bars)
    closes = [float(v) for v in bars["close"]]
    bh, _net, _gross = _one(engine, closes, dates, "buy_and_hold", rate)
    rows = []
    for window in (10, 15, 20, 30, 40):
        for width in (1.5, 2.0, 2.5):
            packed, _net, _gross = _one(engine, closes, dates, "bollinger_breakout", rate, window, width)
            rows.append(
                {
                    "window": window,
                    "std": width,
                    "gross_return": packed["gross_return"],
                    "net_return": packed["net_return"],
                    "gross_max_drawdown": packed["gross_max_drawdown"],
                    "net_max_drawdown": packed["net_max_drawdown"],
                    "trades": packed["trades"],
                    "gross_score": packed["gross_score"],
                    "net_score": packed["net_score"],
                    "beats_buy_hold_net": packed["net_score"] > bh["net_score"],
                }
            )
    return {
        "buy_hold_net_score": bh["net_score"],
        "buy_hold_gross_score": bh["gross_score"],
        "tested": len(rows),
        "beat_buy_hold_net": sum(1 for row in rows if row["beats_buy_hold_net"]),
        "rows": rows,
    }


def per_year_bollinger(engine, bars: pd.DataFrame, rate: float) -> list[dict]:
    out = []
    years = bars["timestamp"].dt.year
    for year, group in bars.groupby(years):
        if len(group) < 40:
            continue
        grid = bollinger_grid(engine, group, rate)
        default = next(row for row in grid["rows"] if row["window"] == 20 and row["std"] == 2.0)
        out.append(
            {
                "year": int(year),
                "bars": int(len(group)),
                "default_net_return": default["net_return"],
                "default_net_max_drawdown": default["net_max_drawdown"],
                "default_net_score": default["net_score"],
                "buy_hold_net_score": grid["buy_hold_net_score"],
                "default_beats_buy_hold": default["beats_buy_hold_net"],
                "grid_beat_buy_hold_net": grid["beat_buy_hold_net"],
                "grid_tested": grid["tested"],
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-only local CSV validation. No broker or MT5.")
    parser.add_argument("--csv", required=True, help="Local OHLCV csv (timestamp,open,high,low,close,volume)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--bars", default="H1", choices=sorted(BARS))
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--bollinger-grid", action="store_true")
    parser.add_argument("--skip-risk", action="store_true")
    parser.add_argument("--fee-bps", type=float, default=None)
    parser.add_argument("--slippage-bps", type=float, default=None)
    args = parser.parse_args()
    csv_path = Path(args.csv).resolve()
    if not csv_path.is_file():
        raise SystemExit(f"csv not found: {csv_path}")
    print(f"[validate] reading {csv_path.name} as {args.bars} for {args.symbol}", flush=True)
    print("[validate] paper only. no broker, MT5, or external market API", flush=True)
    frame = _read_csv(csv_path)
    bars = _resample(frame, args.bars)
    if len(bars) < 22:
        raise SystemExit(f"only {len(bars)} {args.bars} bars after resample; need at least 22")
    print(f"[validate] {len(frame)} source rows -> {len(bars)} {args.bars} bars", flush=True)
    engine = _engine()
    result = _backtest(engine, bars, args.fee_bps, args.slippage_bps)
    checks = _checks(engine, result)
    print("[validate] backtest and pandas checks done", flush=True)
    research: dict = {}
    if args.walk_forward:
        print(
            f"[validate] walk-forward train {args.train_days}d test {args.test_days}d",
            flush=True,
        )
        research["walk_forward"] = walk_forward(
            engine,
            bars,
            args.train_days,
            args.test_days,
            result["cost_rate"],
            engine.SWITCH_MARGIN,
        )
        print(
            f"[validate] walk-forward windows {research['walk_forward']['windows']}",
            flush=True,
        )
    if args.bollinger_grid:
        print("[validate] bollinger grid", flush=True)
        research["bollinger_grid"] = bollinger_grid(engine, bars, result["cost_rate"])
        research["bollinger_by_year"] = per_year_bollinger(engine, bars, result["cost_rate"])
    if args.skip_risk:
        risk = {"skipped": True}
        print("[validate] skipped live risk tests", flush=True)
    else:
        print("[validate] risk tests against localhost services", flush=True)
        risk = _risk_tests(args.symbol)
    best = result["best"]
    selected = result["selected"]
    record = {
        "symbol": args.symbol,
        "label": f"{args.symbol} (local csv)",
        "source_file": str(csv_path),
        "bars_note": "Local historical file. Paper and backtest only. Not a live market feed.",
        "market_health": {
            "timeframe": args.bars,
            "source_rows": int(len(frame)),
            "bars": int(len(bars)),
            "first": result["dates"][0],
            "last": result["dates"][-1],
            "real_seed": False,
        },
        "backtest": {
            "bars": int(len(bars)),
            "starting_cash": 100000.0,
            "selected_strategy": selected,
            "unconstrained_strategy": best["name"] if selected != "cash" else best["name"],
            "selection_reason": result["reason"] if selected != "cash" else (
                f"Best net score {best['score']:.4f} for {best['name']} is negative, so the book stays in cash. "
                + result["reason"]
            ),
            "strategies": result["rows"],
            "buy_hold": result["bh"],
            "drawdown_penalty": result["penalty"],
            "fee_bps": result["fee_bps"],
            "slippage_bps": result["slippage_bps"],
            "news_in_backtest": False,
        },
        "research": research,
        "equity_curve": _curve(result["dates"], result["sims"][best["name"]]["equity"]) if len(bars) <= 4000 else [],
        "buy_hold_curve": _curve(result["dates"], result["bh_sim"]["equity"]) if len(bars) <= 4000 else [],
        "trade_markers": result["sims"][best["name"]].get("trade_events") or [],
        "checks": checks,
        "agent": risk,
        "caveats": CAVEATS,
    }
    _merge(record)
    failed = [name for name, item in checks.items() if not item.get("pass")]
    risk_failed = [
        name
        for name, item in risk.items()
        if isinstance(item, dict) and item.get("pass") is False and not args.skip_risk
    ]
    print(f"[validate] wrote {OUT_PATH}", flush=True)
    print(
        f"[validate] selected {selected} unconstrained {best['name']} "
        f"net score {best['score']} gross score {best.get('gross_score')}",
        flush=True,
    )
    if failed or risk_failed or risk.get("error"):
        print(f"[validate] FAILED checks={failed} risk={risk_failed} error={risk.get('error')}", flush=True)
        return 1
    print("[validate] PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
