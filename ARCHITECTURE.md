# Sentinel — Architecture

Paper-trading agentic system. **No live money paths.**

## Service map

| Service | Port | Role |
|---------|------|------|
| `market-ingest` | 8001 | Seed OHLCV + news |
| `strategy-engine` | 8002 | SMA, RSI mean-reversion, Bollinger breakout; lexicon news score; backtest vs buy-and-hold |
| `risk-guard` | 8003 | Hard caps + **persisted kill switch** (fail-closed) |
| `paper-broker` | 8004 | Simulated fills; cash/positions/PnL |
| `agent-orchestrator` | 8005 | Tool loop (calls other services) |
| `journal-api` | 8006 | Structured + plain-English journal |
| `dashboard-ui` | 8010 | Thin HTML control panel |

## Who calls whom

```
agent-orchestrator
  ├─→ market-ingest     GET /bars, /news, /last-price
  ├─→ strategy-engine   POST /signal, POST /backtest
  ├─→ risk-guard        POST /check-order, GET /kill-switch
  ├─→ paper-broker      POST /orders   (only if risk allows)
  └─→ journal-api       POST /trade

dashboard-ui
  └─→ risk-guard, paper-broker, journal-api, strategy-engine, agent-orchestrator
```

Orders never skip `risk-guard`. Broker re-checks kill switch (fail-closed if risk-guard down).

## Strategy selection

`POST /backtest` runs three long-only strategies on the seed bars and buy-and-hold:

- `sma_crossover` — SMA(5) / SMA(15)
- `rsi_mean_reversion` — Wilder RSI(2), buy on a cross below 30, sell on a cross above 70
- `bollinger_breakout` — 20-bar, 2 standard-deviation bands; buy on a cross above the upper band, sell on a cross below the midline

The orchestrator ranks trading strategies by `score = return - 0.5 * max_drawdown` (higher wins; ties break on return, then fewer trades, then name). It journals that comparison and calls `POST /signal` with the winner. Buy-and-hold is reported beside them and is not a candidate. Backtests are price-only.

## News adjustment

Headlines in the 14 days up to the signal bar are scored with a fixed lexicon: `clamp(sum(weights) / 2, -1, 1)`. `news_score` is the lowest headline score. At or below -0.60, a buy becomes a hold and size goes to 0. A milder negative score cuts buy confidence to 60% and size in half. At or above 0.15, buy confidence rises by 0.10 and size stays put. The journal repeats `news_score` and `news_reason`. No model API is called.

## Kill-switch flow

1. Start: `KILL_SWITCH_DEFAULT=true` → file `data/kill_switch.json`
2. Human: `POST /kill-switch` `{ "killed": false }` to clear
3. Agent: any order while killed → denied; orchestrator journals the block
4. Caps: position %, stop-loss %, daily loss, max trades/day, min RR

## Run

`python scripts/run_all.py` (no Docker required)
