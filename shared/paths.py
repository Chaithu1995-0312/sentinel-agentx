"""Resolve seed/data paths relative to build root."""
from __future__ import annotations

import os
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]


def env_path(key: str, default: str) -> Path:
    raw = os.getenv(key, default)
    p = Path(raw)
    if not p.is_absolute():
        p = (BUILD_ROOT / p).resolve()
    return p
