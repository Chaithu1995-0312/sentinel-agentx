"""dashboard-ui — thin static control panel for kill switch / trades / backtest."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
app = FastAPI(title="sentinel-dashboard-ui", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"service": "dashboard-ui", "status": "ok", "port": int(os.getenv("DASHBOARD_UI_PORT", "8010"))}


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


app.mount("/static", StaticFiles(directory=str(HERE)), name="static")
