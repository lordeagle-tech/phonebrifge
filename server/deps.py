from typing import Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import consume_connection_ticket, verify_token
from . import cli_token as _cli_token

_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ticket: Optional[str] = Query(None),
) -> str:
    """FastAPI dependency that enforces PIN-session authentication on any route."""
    t = credentials.credentials if credentials else None
    if t is None:
        t = consume_connection_ticket(ticket, request.url.path)
    if not verify_token(t):
        raise HTTPException(status_code=401, detail="Unauthorized")
    # Keep the on-disk CLI token "fresh" while a terminal session is
    # actively making calls. No-op for browser PIN sessions.
    _cli_token.touch_if_current(t)
    return t
