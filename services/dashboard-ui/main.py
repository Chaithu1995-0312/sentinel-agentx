"""dashboard-ui — paper control panel plus a local real-data validation page."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VALIDATION_DIR = ROOT / "data" / "validation"
REALDATA_DIR = ROOT / "data" / "realdata"
RESULTS_PATH = VALIDATION_DIR / "validation_results.json"
LOG_PATH = VALIDATION_DIR / "run.log"
PYTHON = os.getenv("SENTINEL_PYTHON", sys.executable)

app = FastAPI(title="sentinel-dashboard-ui", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_lock = threading.Lock()
_job: dict = {"running": False, "exit_code": None, "error": None, "filename": None}


def _local(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "validation controls are localhost only")


def _safe_csv(filename: str) -> Path:
    name = Path(filename).name
    if name != filename or not name.lower().endswith(".csv"):
        raise HTTPException(400, "filename must be a CSV in data/realdata")
    path = (REALDATA_DIR / name).resolve()
    if path.parent != REALDATA_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "csv not found in data/realdata")
    return path


def _annotate(item: dict) -> dict:
    out = dict(item)
    png = str(item.get("png") or "")
    name = Path(png).name if png else f"equity_{item.get('symbol') or ''}.png"
    if name and (VALIDATION_DIR / name).is_file():
        out["chart_url"] = f"/api/validation/charts/{name}"
    return out


def _load_results() -> dict:
    if not RESULTS_PATH.is_file():
        return {
            "present": False,
            "instruments": [],
            "how_to": (
                "Put a CSV in data/realdata/ and run "
                "python scripts/validate_real.py --csv data/realdata/FILE.csv --symbol SYMBOL --bars H1. "
                "That folder and data/validation/ are gitignored. Paper only. No broker or MT5 connection."
            ),
        }
    raw = json.loads(RESULTS_PATH.read_text())
    caveats = []
    if isinstance(raw, dict):
        instruments = raw.get("instruments") or raw.get("results") or []
        caveats = list(raw.get("caveats") or [])
    elif isinstance(raw, list):
        instruments = raw
    else:
        instruments = []
    return {
        "present": True,
        "instruments": [_annotate(item) for item in instruments if isinstance(item, dict)],
        "caveats": caveats,
        "banner": "Private validation on real historical data - not part of hackathon demo data",
    }


class RunBody(BaseModel):
    filename: str
    symbol: str = Field(default="", max_length=20)
    bars: str = "H1"


def _run_job(csv_path: Path, symbol: str, bars: str) -> None:
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        PYTHON,
        str(ROOT / "scripts" / "validate_real.py"),
        "--csv",
        str(csv_path),
        "--symbol",
        symbol,
        "--bars",
        bars,
    ]
    try:
        with LOG_PATH.open("w") as log:
            proc = subprocess.run(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, check=False)
        code = proc.returncode
        err = None if code == 0 else f"validate_real.py exited {code}"
    except OSError as exc:
        code = 1
        err = str(exc)
    with _lock:
        _job["running"] = False
        _job["exit_code"] = code
        _job["error"] = err


@app.get("/health")
def health():
    return {"service": "dashboard-ui", "status": "ok", "port": int(os.getenv("DASHBOARD_UI_PORT", "8010"))}


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/validation")
def validation_page():
    return FileResponse(HERE / "validation.html")


@app.get("/api/validation")
def validation_results():
    return _load_results()


@app.get("/api/validation/csvs")
def validation_csvs():
    REALDATA_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in REALDATA_DIR.glob("*.csv") if p.is_file())
    return {"csvs": names}


@app.get("/api/validation/charts/{name}")
def validation_chart(name: str):
    safe = Path(name).name
    if safe != name or Path(safe).suffix.lower() != ".png":
        raise HTTPException(404, "chart not found")
    path = (VALIDATION_DIR / safe).resolve()
    if path.parent != VALIDATION_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "chart not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/validation/status")
def validation_status():
    tail = ""
    if LOG_PATH.is_file():
        lines = LOG_PATH.read_text(errors="replace").splitlines()
        tail = "\n".join(lines[-40:])
    with _lock:
        return {
            "running": _job["running"],
            "exit_code": _job["exit_code"],
            "error": _job["error"],
            "filename": _job["filename"],
            "log": tail,
        }


@app.post("/api/validation/run")
def validation_run(body: RunBody, request: Request):
    _local(request)
    bars = body.bars.upper()
    if bars not in {"M15", "H1", "H4"}:
        raise HTTPException(400, "bars must be M15, H1, or H4")
    symbol = body.symbol.strip() or Path(body.filename).stem
    if not symbol.replace("_", "").isalnum():
        raise HTTPException(400, "symbol must be letters, numbers, or underscore")
    csv_path = _safe_csv(body.filename)
    with _lock:
        if _job["running"]:
            raise HTTPException(409, "a validation run is already in progress")
        _job["running"] = True
        _job["exit_code"] = None
        _job["error"] = None
        _job["filename"] = csv_path.name
    thread = threading.Thread(target=_run_job, args=(csv_path, symbol, bars), daemon=True)
    thread.start()
    return {"status": "started", "filename": csv_path.name, "symbol": symbol, "bars": bars}


app.mount("/static", StaticFiles(directory=str(HERE)), name="static")
