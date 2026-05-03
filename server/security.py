"""
Per-IP PIN-attempt lockout and lightweight rate limiting for the bridge.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

# ── PIN lockout ─────────────────────────────────────────────────────────
MAX_PIN_FAILS = 5
LOCKOUT_SECONDS = 15 * 60
# After this many consecutive fails, slow down further attempts. The delay
# doubles each additional failure (1s, 2s, 4s, 8s) up to MAX_DELAY_SECONDS.
DELAY_AFTER_FAILS = 3
MAX_DELAY_SECONDS = 8

_lock = threading.Lock()
_fails: Dict[str, Deque[float]] = defaultdict(deque)
_locked_until: Dict[str, float] = {}


def is_locked(ip: str) -> Tuple[bool, int]:
    """Return (locked, seconds_remaining)."""
    with _lock:
        until = _locked_until.get(ip, 0)
        now = time.time()
        if until > now:
            return True, int(until - now)
        if until and until <= now:
            _locked_until.pop(ip, None)
            _fails.pop(ip, None)
        return False, 0


def record_failure(ip: str) -> Tuple[bool, int]:
    """Record a PIN failure. Returns (now_locked, attempts_remaining)."""
    with _lock:
        now = time.time()
        dq = _fails[ip]
        # drop stale failures (> lockout window old)
        while dq and now - dq[0] > LOCKOUT_SECONDS:
            dq.popleft()
        dq.append(now)
        if len(dq) >= MAX_PIN_FAILS:
            _locked_until[ip] = now + LOCKOUT_SECONDS
            return True, 0
        return False, MAX_PIN_FAILS - len(dq)


def record_success(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)
        _locked_until.pop(ip, None)


def next_failure_delay(ip: str) -> float:
    """Seconds to delay the *next* PIN attempt from this IP, based on the
    number of recent failures already on record. Returns 0 if under the
    DELAY_AFTER_FAILS threshold."""
    with _lock:
        now = time.time()
        dq = _fails.get(ip)
        if not dq:
            return 0.0
        # mirror the stale-failure pruning done in record_failure
        while dq and now - dq[0] > LOCKOUT_SECONDS:
            dq.popleft()
        n = len(dq)
        if n < DELAY_AFTER_FAILS:
            return 0.0
        # n=3 -> 1s, 4 -> 2s, 5 -> 4s ... capped
        delay = float(1 << (n - DELAY_AFTER_FAILS))
        return min(delay, float(MAX_DELAY_SECONDS))


# ── Rate limiting ───────────────────────────────────────────────────────
# Sliding-window per (ip, bucket). Buckets group endpoints so a flood of
# clipboard reads can't starve the auth endpoint.

_rate_lock = threading.Lock()
_buckets: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)

# (window_seconds, max_requests) per bucket
LIMITS: Dict[str, Tuple[int, int]] = {
    "auth": (60, 10),
    "files": (60, 60),
    "clipboard": (60, 60),
    "default": (60, 120),
}


def check_rate(ip: str, bucket: str) -> bool:
    """Return True if allowed, False if over the limit."""
    window, max_req = LIMITS.get(bucket, LIMITS["default"])
    now = time.time()
    key = (ip, bucket)
    with _rate_lock:
        dq = _buckets[key]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= max_req:
            return False
        dq.append(now)
        return True


# ── WebSocket session watchdog ───────────────────────────────────────────
import asyncio  # noqa: E402

from . import audit, auth  # noqa: E402


def ws_client_ip(websocket) -> str:
    try:
        return websocket.client.host if websocket.client else "-"
    except Exception:
        return "-"


async def ws_session_watchdog(websocket, token: str, category: str,
                              action: str, ip: str, interval: float = 30.0) -> None:
    """
    Periodically re-validate a WebSocket session. Closes the socket and logs
    a 'session_expired' audit event if the token becomes invalid (idle timeout
    or explicit logout).

    Run this concurrently with the WS's send/receive coroutines via
    `asyncio.gather(..., ws_session_watchdog(...))`. When this task closes the
    socket, the other coroutines will raise WebSocketDisconnect and exit.
    """
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        if not auth.verify_token(token):
            audit.log(category, "session_expired", ip=ip, outcome="closed",
                      detail={"action": action})
            try:
                await websocket.close(code=4401)
            except Exception:
                pass
            return
