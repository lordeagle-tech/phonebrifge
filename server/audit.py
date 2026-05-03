"""
Audit logger for the Laptop-Phone Bridge.

Writes structured JSON-line events to ~/.phone-bridge/audit.log (with simple
size-based rotation) and keeps the most recent events in an in-memory ring
buffer for the /api/activity endpoint.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

LOG_DIR = Path.home() / ".phone-bridge"
LOG_FILE = LOG_DIR / "audit.log"
ROTATE_BYTES = 1_000_000  # 1 MB
KEEP_ROTATIONS = 3
MAX_RING = 500  # in-memory ring for /api/activity

_lock = threading.Lock()
_ring: Deque[Dict[str, Any]] = deque(maxlen=MAX_RING)


def _ensure_dir() -> None:
    try:
        LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass


def _rotate_if_needed() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > ROTATE_BYTES:
            for i in range(KEEP_ROTATIONS, 0, -1):
                src = LOG_FILE.with_suffix(f".log.{i}") if i > 1 else LOG_FILE
                dst = LOG_FILE.with_suffix(f".log.{i + 1 if i > 1 else 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
    except OSError:
        pass


def log(category: str, action: str, ip: str = "-", outcome: str = "ok",
        detail: Optional[Dict[str, Any]] = None) -> None:
    """Append an audit event. category: auth|files|clipboard|control|screen|system."""
    event = {
        "ts": time.time(),
        "category": category,
        "action": action,
        "ip": ip,
        "outcome": outcome,
        "detail": detail or {},
    }
    with _lock:
        _ring.append(event)
        _ensure_dir()
        _rotate_if_needed()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            pass


def recent(limit: int = 50, category: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_ring)
    if category and category != "all":
        items = [e for e in items if e["category"] == category]
    return list(reversed(items[-limit:]))
