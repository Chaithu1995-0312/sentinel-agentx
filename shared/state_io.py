"""Atomic JSON state files with a per-file lock.

Each state file is owned by exactly one service process (broker, risk-guard,
journal), so an in-process re-entrant lock serialises read-modify-write. Writes
go to a temp file and are swapped in with os.replace, so a crash or a reader
never sees a half-written file. Windows can briefly refuse os.replace while
another program has the file open, so we retry a few times.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.RLock()
        return _LOCKS[key]


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=2))


def append_line(path: Path, line: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_for(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
            f.flush()
            os.fsync(f.fileno())
