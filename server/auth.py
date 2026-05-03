import secrets
import time
from typing import Optional

_pin: str = ""
# token -> (created_at, last_activity)
_sessions: dict[str, tuple[float, float]] = {}

# Absolute session lifetime (24h) and inactivity timeout (configurable).
SESSION_TTL = 86400
_inactivity_timeout: float = 30 * 60  # default 30 minutes


def set_pin(pin: str) -> None:
    global _pin
    _pin = pin


def get_pin() -> str:
    return _pin


def verify_pin(pin: str) -> bool:
    return pin == _pin


def set_inactivity_timeout(seconds: float) -> None:
    """Configure the session inactivity timeout in seconds (0 disables)."""
    global _inactivity_timeout
    _inactivity_timeout = max(0.0, float(seconds))


def get_inactivity_timeout() -> float:
    return _inactivity_timeout


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    _sessions[token] = (now, now)
    return token


def verify_token(token: Optional[str]) -> bool:
    if not token:
        return False
    entry = _sessions.get(token)
    if entry is None:
        return False
    created, last = entry
    now = time.time()
    if now - created > SESSION_TTL:
        _sessions.pop(token, None)
        return False
    if _inactivity_timeout and now - last > _inactivity_timeout:
        _sessions.pop(token, None)
        return False
    # touch
    _sessions[token] = (created, now)
    return True


def invalidate_session(token: str) -> None:
    _sessions.pop(token, None)


def active_session_count() -> int:
    return len(_sessions)
