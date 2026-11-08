#!/usr/bin/env python3
"""Launch all Sentinel services with uvicorn (no Docker)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# Ensure localhost URLs for local Python runs
os.environ.setdefault("MARKET_INGEST_URL", "http://127.0.0.1:8001")
os.environ.setdefault("STRATEGY_ENGINE_URL", "http://127.0.0.1:8002")
os.environ.setdefault("RISK_GUARD_URL", "http://127.0.0.1:8003")
os.environ.setdefault("PAPER_BROKER_URL", "http://127.0.0.1:8004")
os.environ.setdefault("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8005")
os.environ.setdefault("JOURNAL_API_URL", "http://127.0.0.1:8006")
os.environ.setdefault("SEED_OHLCV_PATH", str(ROOT / "seeds" / "ohlcv_sample.csv"))
os.environ.setdefault("SEED_REGIME_PATH", str(ROOT / "seeds" / "ohlcv_regimes.json"))
os.environ.setdefault("SEED_NEWS_PATH", str(ROOT / "seeds" / "news_sample.json"))
os.environ.setdefault("KILL_SWITCH_PATH", str(ROOT / "data" / "kill_switch.json"))
os.environ.setdefault("JOURNAL_PATH", str(ROOT / "data" / "journal.jsonl"))
os.environ.setdefault("BROKER_STATE_PATH", str(ROOT / "data" / "broker_state.json"))
os.environ.setdefault("RISK_STATE_PATH", str(ROOT / "data" / "risk_state.json"))

(ROOT / "data").mkdir(parents=True, exist_ok=True)

SERVICES = [
    ("market-ingest", "services.market-ingest.main:app", int(os.getenv("MARKET_INGEST_PORT", "8001"))),
    ("strategy-engine", "services.strategy-engine.main:app", int(os.getenv("STRATEGY_ENGINE_PORT", "8002"))),
    ("risk-guard", "services.risk-guard.main:app", int(os.getenv("RISK_GUARD_PORT", "8003"))),
    ("paper-broker", "services.paper-broker.main:app", int(os.getenv("PAPER_BROKER_PORT", "8004"))),
    ("agent-orchestrator", "services.agent-orchestrator.main:app", int(os.getenv("AGENT_ORCHESTRATOR_PORT", "8005"))),
    ("journal-api", "services.journal-api.main:app", int(os.getenv("JOURNAL_API_PORT", "8006"))),
    ("dashboard-ui", "services.dashboard-ui.main:app", int(os.getenv("DASHBOARD_UI_PORT", "8010"))),
]

# Module paths use hyphens in folder names - uvicorn needs import path with underscores or file path.
# Use --app-dir per service folder instead.


def _procs() -> list[subprocess.Popen]:
    procs: list[subprocess.Popen] = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for name, _mod, port in SERVICES:
        app_dir = ROOT / "services" / name
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app-dir",
            str(app_dir),
        ]
        print(f"[run_all] starting {name} on :{port}")
        procs.append(subprocess.Popen(cmd, cwd=str(ROOT), env=env))
    return procs


def main() -> int:
    procs = _procs()

    def _stop(*_args):
        print("\n[run_all] shutting down...")
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Wait briefly then print health hints
    time.sleep(1.5)
    print("[run_all] health:")
    for name, _m, port in SERVICES:
        print(f"  http://127.0.0.1:{port}/health  ({name})")
    print(f"[run_all] dashboard: http://127.0.0.1:{os.getenv('DASHBOARD_UI_PORT', '8010')}/")
    print("[run_all] Ctrl+C to stop")

    while True:
        for p in procs:
            rc = p.poll()
            if rc is not None:
                print(f"[run_all] a process exited with {rc}; shutting down others")
                _stop()
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
