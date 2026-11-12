#!/usr/bin/env python3
"""Write a deterministic MOCK weekday OHLCV seed. Not a real market.

Fixed seed. About 12 months of sessions in four labeled regimes:
rally, sharp crash (close-to-close -25%), sideways chop, recovery.

Rewrites:
  seeds/ohlcv_sample.csv
  seeds/ohlcv_regimes.json
  seeds/news_sample.json

The previous 80-day uptrend is kept as seeds/ohlcv_short_sample.csv.
"""
from __future__ import annotations

import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 20251107
START = date(2024, 11, 18)  # Monday
# 80 + 24 + 74 + 74 = 252 weekdays, about twelve months.
REGIME_LENGTHS = (
    ("rally", 80),
    ("crash", 24),
    ("chop", 74),
    ("recovery", 74),
)
MOCK_NOTE = (
    "MOCK DATA. Synthetic weekday prices and headlines for paper trading only. "
    "Not a real market and not a forecast."
)


def _weekdays(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _ohlc(rng: random.Random, prev_close: float, close: float) -> tuple[float, float, float, float, int]:
    open_ = prev_close * (1.0 + rng.uniform(-0.0025, 0.0025))
    wick = abs(rng.gauss(0.0, 0.0035)) + 0.0015
    high = max(open_, close) * (1.0 + wick)
    low = min(open_, close) * (1.0 - wick)
    # Keep the candle ordered after rounding.
    open_r = round(open_, 2)
    high_r = round(max(high, open_, close), 2)
    low_r = round(min(low, open_, close), 2)
    close_r = round(close, 2)
    if low_r > min(open_r, close_r):
        low_r = min(open_r, close_r)
    if high_r < max(open_r, close_r):
        high_r = max(open_r, close_r)
    volume = int(8_000_000 + rng.random() * 10_000_000)
    return open_r, high_r, low_r, close_r, volume


def _prices(rng: random.Random, n_rally: int, n_crash: int, n_chop: int, n_recovery: int) -> list[float]:
    """Close path. Crash ends exactly 25% under the rally close."""
    price = 100.0
    closes: list[float] = []

    for _ in range(n_rally):
        price *= math.exp(0.0032 + rng.gauss(0.0, 0.006))
        closes.append(price)

    crash_start = closes[-1]
    for i in range(n_crash):
        frac = 0.75 ** ((i + 1) / n_crash)
        # Small noise around the glide path, biased down, then the last print is pinned.
        wobble = 1.0 + rng.uniform(-0.012, 0.006)
        closes.append(crash_start * frac * wobble)
    closes[-1] = crash_start * 0.75

    base = closes[-1]
    for i in range(n_chop):
        wave = math.sin(2.0 * math.pi * i / 18.0) * 0.04
        closes.append(base * (1.0 + wave + rng.gauss(0.0, 0.004)))

    price = closes[-1]
    for _ in range(n_recovery):
        price *= math.exp(0.0042 + rng.gauss(0.0, 0.007))
        closes.append(price)
    return closes


def _headlines(regime_days: dict[str, list[date]]) -> list[dict]:
    """Mock headlines. Crash copy uses the strong-negative lexicon; rally and recovery stay positive."""
    catalog = {
        "rally": [
            "Index futures steady as buyers add to the rally",
            "Banking sector leads morning gains",
            "Growth names surge on upbeat earnings",
            "Risk appetite improves as the rally broadens",
            "Energy names firm and refining margins improve",
            "Breadth rises with another day of gains",
        ],
        "crash": [
            "Markets plunge on surprise default fears and heavy selloff",
            "Stocks crash as recession worries spark a selloff",
            "Benchmarks tumble in a crisis selloff",
            "Prices plunge again as default fears return",
            "A fresh selloff hits as crash headlines spread",
        ],
        "chop": [
            "Index holds a tight range with no fresh catalyst",
            "Market breadth narrows in a mixed session",
            "Traders describe a mixed and narrow tape",
            "Volumes stay light in another mixed session",
        ],
        "recovery": [
            "Stocks rebound as risk appetite returns",
            "Rally resumes and market breadth improves",
            "Flows turn positive and gains broaden",
            "Recovery firms as earnings growth improves",
            "Risk appetite builds on a steady rebound",
        ],
    }
    rows: list[dict] = []
    for name, days in regime_days.items():
        lines = catalog[name]
        if name == "crash":
            picks = list(range(len(days)))
        else:
            picks = list(range(0, len(days), 10))
        for n, idx in enumerate(picks):
            rows.append(
                {
                    "date": days[idx].isoformat(),
                    "headline": lines[n % len(lines)],
                    "source": "MockWire",
                    "regime": name,
                    "note": "MOCK headline, not a real news report",
                }
            )
    rows.sort(key=lambda row: row["date"])
    return rows


def main() -> int:
    lengths = dict(REGIME_LENGTHS)
    total = sum(lengths.values())
    days = _weekdays(START, total)
    rng = random.Random(SEED)
    closes = _prices(rng, lengths["rally"], lengths["crash"], lengths["chop"], lengths["recovery"])
    if len(closes) != len(days):
        raise SystemExit(f"price count {len(closes)} != days {len(days)}")

    # A second stream for candle noise so the close path above stays independent of wick draws.
    wick_rng = random.Random(SEED + 1)
    prev = closes[0]
    bars = []
    cursor = 0
    regime_days: dict[str, list[date]] = {}
    regimes = []
    for name, count in REGIME_LENGTHS:
        chunk_days = days[cursor : cursor + count]
        regime_days[name] = chunk_days
        regimes.append(
            {
                "name": name,
                "start": chunk_days[0].isoformat(),
                "end": chunk_days[-1].isoformat(),
                "bars": count,
            }
        )
        for day, close in zip(chunk_days, closes[cursor : cursor + count]):
            open_, high, low, close_r, volume = _ohlc(wick_rng, prev, close)
            bars.append(
                {
                    "date": day.isoformat(),
                    "open": f"{open_:.2f}",
                    "high": f"{high:.2f}",
                    "low": f"{low:.2f}",
                    "close": f"{close_r:.2f}",
                    "volume": str(volume),
                }
            )
            prev = close_r
        cursor += count

    ohlcv_path = ROOT / "seeds" / "ohlcv_sample.csv"
    with ohlcv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(bars)

    crash = next(item for item in regimes if item["name"] == "crash")
    crash_start_px = float(bars[lengths["rally"] - 1]["close"])
    crash_end_px = float(bars[lengths["rally"] + lengths["crash"] - 1]["close"])
    meta = {
        "label": MOCK_NOTE,
        "seed": SEED,
        "symbol": "SYNTH",
        "bars": total,
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "crash_close_to_close": round(crash_end_px / crash_start_px - 1.0, 6),
        "regimes": regimes,
    }
    (ROOT / "seeds" / "ohlcv_regimes.json").write_text(json.dumps(meta, indent=2) + "\n")

    news = _headlines(regime_days)
    (ROOT / "seeds" / "news_sample.json").write_text(json.dumps(news, indent=2) + "\n")

    print("MOCK DATA ONLY. Not a real market.")
    print(f"seed={SEED} bars={total} {days[0].isoformat()} .. {days[-1].isoformat()}")
    print(f"crash close-to-close {meta['crash_close_to_close']:.2%} ({crash['start']} .. {crash['end']})")
    for item in regimes:
        print(f"  {item['name']:9} {item['start']} .. {item['end']} ({item['bars']} bars)")
    print(f"wrote {ohlcv_path}")
    print(f"wrote {ROOT / 'seeds' / 'ohlcv_regimes.json'}")
    print(f"wrote {ROOT / 'seeds' / 'news_sample.json'} ({len(news)} headlines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
