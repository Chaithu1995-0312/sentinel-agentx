# Sentinel — Architecture

Paper-trading agentic system. **No live money paths.**

## Service map

| Service | Port | Role |
|---------|------|------|
| `market-ingest` | 8001 | Seed OHLCV + news |
| `strategy-engine` | 8002 | SMA crossover signal + backtest vs buy-and-hold |
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

## Kill-switch flow

1. Start: `KILL_SWITCH_DEFAULT=true` → file `data/kill_switch.json`
2. Human: `POST /kill-switch` `{ "killed": false }` to clear
3. Agent: any order while killed → denied; orchestrator journals the block
4. Caps: position %, stop-loss %, daily loss, max trades/day, min RR

## Run

`python scripts/run_all.py` (no Docker required)
