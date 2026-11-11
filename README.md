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
- **Stop-loss / take-profit exits** — every buy stores its stop (entry −2%) and take-profit (entry +3%) on the paper position. At the start of each `/run` cycle, before any new signal, the orchestrator sends the latest mock close to `paper-broker` `POST /evaluate-exits`. Price at or below the stop closes the whole position (reason `stop_loss`); at or above the take-profit closes it (reason `take_profit`). On later bars the bar low/high is used and a gap fills at the open (see Security & design choices). Exits only reduce risk, so they run even with the kill switch ON and do not use the 10-trades-a-day cap (risk-guard counts them as `exits_today`). Each exit gets a plain-English journal entry. `dry_run` reports the exit without closing.
- **True daily loss limit** — `paper-broker` `GET /daily-pnl` returns today's realized P&L (fills on this machine's local date) plus current unrealized P&L on open positions at the latest close. Risk-guard refuses new orders when that is at or below −3% of start-of-day equity (cash + open positions at cost, snapshotted on the first broker call of each local date). The window resets by date; a paper `/reset` keeps today's realized loss.
- **Human kill switch** — persisted under `data/kill_switch.json`; agent cannot bypass; fail-closed
- **Mock year of prices** — `python scripts/gen_mock_ohlcv.py` (fixed seed) writes about 12 months of weekday bars with a rally, a sharp crash, chop, and a recovery. It is labeled mock data. The old 80-day uptrend is `seeds/ohlcv_short_sample.csv`.
- **Backtest vs buy-and-hold** — SMA crossover, RSI(2) mean reversion, and Bollinger breakout, overall and inside each regime. Fills pay 10 bps fee plus 5 bps slippage per side. The orchestrator ranks by net score, `net return - 0.5 * net max drawdown`, and will not hold a strategy with a negative net score (it stays in cash). The live pick re-ranks on the trailing 60 bars and switches only with a 2 percentage point lead. Buy-and-hold is the benchmark only.
- **News-aware signals** — lexicon sentiment in `strategy-engine` (no LLM). Strongly negative crash headlines veto a buy; positive headlines raise confidence by 0.10. The signal carries `news_score` and `news_reason`, and the journal says them in plain English.
- **Plain-language journal** — template text per trade (`journal-api`)
- **Agent = tool loop** — orchestrator calls APIs (not a chatbot)

## Security & design choices

Paper trading and mock data only. Services bind to 127.0.0.1.

- **Broker re-verifies every order.** `paper-broker /orders` calls risk-guard `/check-order` itself, using its own book (equity marked at the order price, open exposure, today's P&L, start-of-day equity). A caller's `risk_allowed=true` is required but never enough. If risk-guard is unreachable, the broker refuses (fail-closed).
- **No double fills.** State files are written atomically (temp file + `os.replace`) under a per-process lock (`shared/state_io.py`); the broker checks risk and fills inside one lock; the orchestrator serialises `/run` (a second concurrent call waits, then gets 409). Each state file is owned by one service process (single uvicorn worker).
- **CORS allow-list, not `*`.** Default: `http://127.0.0.1:8010`, `http://localhost:8010` and `null` (the Origin browsers send for the offline `file://` demo hub). Override with `SENTINEL_CORS_ORIGINS`; drop `null` if you don't use the offline hub.
- **Optional API key.** Set `SENTINEL_API_KEY` and every mutating route (`/run`, `/orders`, `/reset`, `/evaluate-exits`, `/kill-switch` POST, `/record-trade`, journal writes and clear) requires header `X-Sentinel-Key`. Services forward it to each other. The dashboard and judge view send it when opened once with `?key=...` (stored in the browser). **Unset (default) = local demo mode, routes are open on localhost.**
- **Live strategy pick uses a trailing window.** The live pick ranks strategies on the last `LIVE_WINDOW_BARS` (default 60) bars of the continuous backtest, then applies the 0.02 switch margin. The mock regime labels from the data generator are shown for reporting only and never drive decisions.
- **Paper fills pay costs.** Same model as the backtest: 5 bps slippage against us plus 10 bps fee (`PAPER_SLIPPAGE_BPS`, `PAPER_FEE_BPS`). Fees are in the cost basis, so realized P&L is after costs.
- **Exits use the bar's range.** On a bar after entry, the stop triggers on the bar low and the take-profit on the high; a gap through the level fills at the open; if both are touched, the stop is assumed first. On the entry bar only the close is used.
- **Strict backtest check.** `/backtest` also reports `strict_check`: next-bar-open fills plus the 2% stop / 3% take-profit on bar low/high. It is shown for comparison, not used for selection. The backtest still ignores news.
- **Sizing rounds down** to 4 decimals so a full-size order never lands a hair over the 5% cap.
- **Optional autopilot.** `SENTINEL_AUTORUN_SECONDS=N` makes the orchestrator call its own cycle every N seconds (default 0 = off; status at `GET :8005/autopilot`).
- **No LLM in the loop, by choice.** Decisions are deterministic and reproducible; the journal is template text.
- Not production-ready: no database, single symbol (`SYNTH`; other symbols get 404), no real broker adapter. `docker-compose.yml` is kept for optional use and is not tested with this build.

Tests: `python -m pytest -q tests` (pytest is in `requirements.txt`; CI workflow in `.github/workflows/tests.yml`).

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
