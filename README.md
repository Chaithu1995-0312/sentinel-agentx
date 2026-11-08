# Sentinel (AgentX 2026 PS-02)

Paper-trading agentic assistant for team **Chaithu1705**. **No live money.**

## Quick start (Python only — no Docker)

```bash
cd /workspace/agentx/build   # or your clone root
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env          # optional; defaults work for localhost
python scripts/run_all.py
```

Health checks:

| Service | URL |
|---------|-----|
| market-ingest | http://127.0.0.1:8001/health |
| strategy-engine | http://127.0.0.1:8002/health |
| risk-guard | http://127.0.0.1:8003/health |
| paper-broker | http://127.0.0.1:8004/health |
| agent-orchestrator | http://127.0.0.1:8005/health |
| journal-api | http://127.0.0.1:8006/health |
| dashboard-ui | http://127.0.0.1:8010/ |

Demo (second terminal, services already up):

```bash
source .venv/bin/activate
python scripts/demo_run.py
```

## What it does (PS-02)

- **Paper trading only** — simulated fills in `paper-broker`
- **Hard risk** — position %, stop-loss %, daily loss, max trades/day, min RR (`risk-guard`)
- **Human kill switch** — persisted under `data/kill_switch.json`; agent cannot bypass; fail-closed
- **Mock year of prices** — `python scripts/gen_mock_ohlcv.py` (fixed seed) writes about 12 months of weekday bars with a rally, a sharp crash, chop, and a recovery. It is labeled mock data. The old 80-day uptrend is `seeds/ohlcv_short_sample.csv`.
- **Backtest vs buy-and-hold** — SMA crossover, RSI(2) mean reversion, and Bollinger breakout, overall and inside each regime. Fills pay 10 bps fee plus 5 bps slippage per side. The orchestrator ranks by net score, `net return - 0.5 * net max drawdown`, and will not hold a strategy with a negative net score (it stays in cash). A live switch needs a 2 percentage point lead. Buy-and-hold is the benchmark only.
- **News-aware signals** — lexicon sentiment in `strategy-engine` (no LLM). Strongly negative crash headlines veto a buy; positive headlines raise confidence by 0.10. The signal carries `news_score` and `news_reason`, and the journal says them in plain English.
- **Plain-language journal** — template text per trade (`journal-api`)
- **Agent = tool loop** — orchestrator calls APIs (not a chatbot)

## Docs

- [PREBUILD_RUNBOOK.md](./PREBUILD_RUNBOOK.md)
- [ARCHITECTURE.md](./ARCHITECTURE.md)
- [GITHUB_SETUP.md](./GITHUB_SETUP.md)

Docker compose files may still exist for optional use; **primary path is uvicorn via `scripts/run_all.py`.**

## Private validation (not demo data)

The Validation page at http://127.0.0.1:8010/validation reads `data/validation/validation_results.json` on this machine. Real CSVs stay in `data/realdata/`. Both folders are gitignored.

```bash
python scripts/validate_real.py --csv data/realdata/YOUR.csv --symbol XAUUSD --bars H1
```

The script resamples local bars, backtests the three strategies against buy-and-hold, checks the result with pandas, and runs paper risk tests against the local services. It does not contact a broker or MT5. The page can re-run any CSV already in `data/realdata/`.
