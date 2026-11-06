#!/usr/bin/env bash
# Python-only launcher (no Docker).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python scripts/run_all.py
