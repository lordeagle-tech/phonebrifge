"""
Loopback-only CLI token.

The bridge writes a fresh, random token to ``~/.phone-bridge/cli_token``
(mode 600, parent dir 700) every time the server starts and registers it
in the regular session store. The companion ``run.py mirror`` subcommand
reads it so terminal sessions can call the local API without re-typing
the PIN. The file is loopback-only by design (file-system perms gate
access); it is intentionally NOT exposed over the network.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import auth

STATE_DIR = Path.home() / ".phone-bridge"
TOKEN_FILE = STATE_DIR / "cli_token"

# Track the active CLI token so the auth layer can refresh the file
# mtime only for the right session — we don't want every browser PIN
# session to bump a file that doesn't belong to it.
_active_token: Optional[str] = None


def install() -> Optional[str]:
    """Mint a new CLI token, register it, and persist to disk.

    Returns the token, or ``None`` if the file could not be written
    (e.g. read-only home dir). Failures are non-fatal: the rest of the
    server still works, the user just can't use the CLI shortcut.
    """
    global _active_token
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return None
    token = auth.create_session()
    try:
        # Write atomically with restrictive perms so other users on
        # the box can't read it. os.open lets us set the mode at
        # creation time rather than racing with chmod.
        fd = os.open(
            str(TOKEN_FILE),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        # Defensive chmod in case the file pre-existed with looser perms.
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        auth.invalidate_session(token)
        return None
    _active_token = token
    return token


def uninstall(token: Optional[str]) -> None:
    """Remove the on-disk file and invalidate the session."""
    global _active_token
    if token:
        auth.invalidate_session(token)
    if _active_token == token:
        _active_token = None
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def touch_if_current(token: Optional[str]) -> None:
    """Refresh the token file's mtime if ``token`` matches the active
    CLI token. Called from the auth dependency on every successful
    authenticated request so a busy CLI session keeps the file "fresh"
    on disk (useful for log rotation / cleanup tooling that ages files
    by mtime). No-op for browser PIN sessions and silent on any I/O
    error — refreshing is a hint, never load-bearing."""
    if not token or _active_token is None or token != _active_token:
        return
    try:
        # os.utime with times=None updates atime+mtime to "now".
        os.utime(TOKEN_FILE, None)
    except OSError:
        pass


def read() -> Optional[str]:
    """Read the token from disk for use by the CLI client."""
    try:
        return TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None
