"""
Rotating 6-digit pairing code for linking secondary laptops.

The code is short-lived (5 min by default) so a peer that's about to
be paired must read the *current* one off the main laptop's banner /
UI. After pairing, peers use their long-lived per-peer secret instead
— the code is never reused.

The code is ALSO cached for one extra rotation window so a peer that
read it 4:59 ago doesn't get rejected by a fresh-1s race. After two
windows it's gone.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Optional, Tuple

ROTATE_SECONDS = 5 * 60  # 5 minutes

_lock = threading.Lock()
_current: Optional[str] = None
_current_started_at: float = 0.0
_previous: Optional[str] = None  # accepted within one extra window


def _maybe_rotate_locked(now: float) -> None:
    """Advance the window if needed. If more than one window has
    elapsed since the last call (e.g. nobody read the banner for an
    hour), the "previous" slot is wiped so a months-old code can't
    sneak in via the one-window grace — only the immediately-prior
    window stays acceptable."""
    global _current, _current_started_at, _previous
    if _current is None:
        _previous = None
        _current = f"{secrets.randbelow(10**6):06d}"
        _current_started_at = now
        return
    elapsed = now - _current_started_at
    if elapsed < ROTATE_SECONDS:
        return
    windows_passed = int(elapsed // ROTATE_SECONDS)
    if windows_passed >= 2:
        # More than one full window of silence — drop the previous
        # code entirely. Only the freshly-minted current code is
        # valid; nothing carries over.
        _previous = None
    else:
        _previous = _current
    _current = f"{secrets.randbelow(10**6):06d}"
    _current_started_at = now


def get() -> Tuple[str, int]:
    """Return ``(code, seconds_until_rotate)``. Mints the first code
    on first call so it can be shown on the startup banner without a
    background timer."""
    now = time.time()
    with _lock:
        _maybe_rotate_locked(now)
        remaining = int(ROTATE_SECONDS - (now - _current_started_at))
        return _current, max(0, remaining)


def verify(code: str) -> bool:
    """Accept the current OR the immediately-previous code. Constant
    time on the comparison itself; the structural checks (length,
    digits-only) leak nothing useful."""
    if not code or len(code) != 6 or not code.isdigit():
        return False
    # Force a rotation tick so a code that expired N minutes ago and
    # hasn't been re-checked since can't slip through. The rotate
    # logic clears the "previous" slot when more than one window has
    # elapsed, so stale codes are dropped before this comparison.
    now = time.time()
    with _lock:
        _maybe_rotate_locked(now)
        cur = _current or ""
        prev = _previous or ""
    if cur and secrets.compare_digest(cur, code):
        return True
    if prev and secrets.compare_digest(prev, code):
        return True
    return False
