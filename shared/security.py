"""Optional shared API key and the CORS allow-list used by every service.

SENTINEL_API_KEY unset (default): local demo mode, mutating routes stay open on
127.0.0.1. Set it and every mutating route requires header X-Sentinel-Key with
the same value; services pass it to each other automatically.

SENTINEL_CORS_ORIGINS: comma list. Default is the dashboard origins plus "null",
which is the Origin a browser sends for the offline file:// demo hub. Set it to
just the dashboard origins to drop file:// access.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

KEY_HEADER = "X-Sentinel-Key"
DEFAULT_ORIGINS = "http://127.0.0.1:8010,http://localhost:8010,null"


def api_key() -> str:
    return os.getenv("SENTINEL_API_KEY", "").strip()


def cors_origins() -> list[str]:
    raw = os.getenv("SENTINEL_CORS_ORIGINS", DEFAULT_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


def cors_kwargs() -> dict:
    return {
        "allow_origins": cors_origins(),
        "allow_methods": ["GET", "POST", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", KEY_HEADER],
    }


def require_key(x_sentinel_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency for mutating routes."""
    expected = api_key()
    if not expected:
        return
    if not x_sentinel_key or not hmac.compare_digest(x_sentinel_key, expected):
        raise HTTPException(401, f"missing or wrong {KEY_HEADER} header")


def auth_headers() -> dict[str, str]:
    """Headers for service-to-service calls."""
    key = api_key()
    return {KEY_HEADER: key} if key else {}
