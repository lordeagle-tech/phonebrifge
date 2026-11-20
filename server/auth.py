import secrets
import threading
import time
from typing import Optional

_pin: str = ""
# token -> (created_at, last_activity)
_sessions: dict[str, tuple[float, float]] = {}
_session_lock = threading.Lock()
_tickets: dict[str, tuple[str, str, float]] = {}
_ticket_lock = threading.Lock()
CONNECTION_TICKET_TTL = 30.0

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
    with _session_lock:
        _sessions[token] = (now, now)
    return token


def _valid_entry_locked(token: str, now: float) -> Optional[tuple[float, float]]:
    entry = _sessions.get(token)
    if entry is None:
        return None
    created, last = entry
    if now - created > SESSION_TTL or (
            _inactivity_timeout and now - last > _inactivity_timeout):
        _sessions.pop(token, None)
        return None
    return entry


def is_token_valid(token: Optional[str]) -> bool:
    """Validate a session without counting background checks as user activity."""
    if not token:
        return False
    now = time.time()
    with _session_lock:
        return _valid_entry_locked(token, now) is not None


def verify_token(token: Optional[str]) -> bool:
    """Validate a session and renew its inactivity timer for real requests."""
    if not token:
        return False
    now = time.time()
    with _session_lock:
        entry = _valid_entry_locked(token, now)
        if entry is None:
            return False
        created, _last = entry
        _sessions[token] = (created, now)
        return True


def invalidate_session(token: str) -> None:
    with _session_lock:
        _sessions.pop(token, None)


def create_connection_ticket(token: str, path: str) -> Optional[str]:
    """Mint a 30-second, one-use ticket scoped to one URL path."""
    if not verify_token(token):
        return None
    if (not isinstance(path, str) or not path.startswith("/")
            or "?" in path or "#" in path or len(path) > 512):
        return None
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    with _ticket_lock:
        expired = [key for key, (_, _, expiry) in _tickets.items() if expiry <= now]
        for key in expired:
            _tickets.pop(key, None)
        _tickets[ticket] = (token, path, now + CONNECTION_TICKET_TTL)
    return ticket


def consume_connection_ticket(ticket: Optional[str], path: str) -> Optional[str]:
    """Consume a ticket once and return its still-active parent session token."""
    if not ticket:
        return None
    with _ticket_lock:
        entry = _tickets.pop(ticket, None)
    if entry is None:
        return None
    session_token, ticket_path, expiry = entry
    if expiry <= time.monotonic() or ticket_path != path:
        return None
    return session_token if verify_token(session_token) else None


def active_session_count() -> int:
    with _session_lock:
        return len(_sessions)
