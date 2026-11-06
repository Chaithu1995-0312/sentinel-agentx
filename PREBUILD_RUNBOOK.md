# Sentinel — Runbook (AgentX 2026 PS-02)

**Team:** Chaithu1705 (Chaithanya lead, Naveena co-builder)  
**Build window:** 11 Nov 2026 10:00 → 12 Nov 2026 10:00 Asia/Calcutta (IST)

---

## Local start (Python only — no Docker)

```bash
cd /path/to/sentinel-agentx   # this build/ tree
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp -n env.example .env
python scripts/run_all.py
```

Dashboard: http://127.0.0.1:8010/  
Smoke demo: `python scripts/demo_run.py`

Alternate: `./scripts/start_services.sh`

---

## Day-minus checklist

### Accounts & access
- [ ] GitHub public repo `sentinel-agentx` (see `GITHUB_SETUP.md`)
- [ ] Naveena invited as collaborator
- [ ] Optional free LLM keys in local `.env` only (templates work without keys)
- [ ] hack4hack.com team confirmed

### Local machine
- [ ] Python 3.11+ 
- [ ] `pip install -r requirements.txt` in `.venv`
- [ ] `python scripts/run_all.py` then all `/health` return ok
- [ ] `python scripts/demo_run.py` prints PASSED
- [ ] CRT dashboard optional for video polish only (do not fork CRT into this repo)

### Fail-closed rules
- Paper trading only — **no** live broker keys
- Kill switch default **ON** until a human clears it
- Free/local only; hard `LLM_DAILY_TOKEN_CAP` if LLM used
- Freeze new features at H16

---

## Env vars

See `env.example`. Service URLs default to `http://127.0.0.1:800x` for local uvicorn.

| Var | Purpose |
|-----|---------|
| `KILL_SWITCH_PATH` | persisted kill flag (fail-closed) |
| `RISK_MAX_POSITION_PCT` | e.g. 0.05 |
| `RISK_STOP_LOSS_PCT` | e.g. 0.02 |
| `RISK_DAILY_LOSS_LIMIT` | e.g. 0.03 of equity |
| `RISK_MAX_TRADES_PER_DAY` | e.g. 10 |
| `RISK_MIN_RR` | e.g. 1.5 |
| `PAPER_STARTING_CASH` | e.g. 100000 |
| `LLM_DAILY_TOKEN_CAP` | hard cap if LLM enabled |

---

## Role split (suggested)

| Who | Owns |
|-----|------|
| **Chaithanya** | risk-guard, paper-broker, agent-orchestrator, kill switch, submission |
| **Naveena** | market-ingest, strategy-engine, journal-api, backtest charts, deck/video |

---

## Submission pack (before 12 Nov 10:00 IST)

- [ ] Public GitHub repo + README how-to-run
- [ ] Demo video ≤ 3 min
- [ ] Deck ≤ 6 slides
- [ ] Show: paper trade, risk reject, kill switch halt, backtest vs BH, journal entry
- [ ] No secrets in git

---

## Quick pointers

- Architecture: `ARCHITECTURE.md`
- Seeds: `seeds/` (synthetic)
- Data/runtime: `data/` (gitignored state)
