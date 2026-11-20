"""Same-origin checks for browser WebSocket handshakes."""

from urllib.parse import urlsplit


def websocket_origin_is_allowed(scope) -> bool:
    """Return whether an optional Origin matches the effective connection.

    ASGI may report WebSockets as ``ws``/``wss`` or their HTTP-equivalent
    schemes. Proxy deployments should provide the effective scheme and Host
    (for example through Uvicorn's trusted proxy-header handling).
    """
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    origin_raw = headers.get(b"origin")
    if not origin_raw:
        # Native clients commonly omit Origin; authentication is still required.
        return True

    host_raw = headers.get(b"host")
    if not host_raw:
        return False

    try:
        origin = urlsplit(origin_raw.decode("latin-1"))
        host = urlsplit("//" + host_raw.decode("latin-1"))
        scheme = scope.get("scheme", "").lower()
        if scheme == "ws":
            scheme = "http"
        elif scheme == "wss":
            scheme = "https"

        if (origin.scheme not in ("http", "https")
                or origin.scheme != scheme
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path not in ("", "/")
                or origin.query
                or origin.fragment
                or not host.hostname
                or host.username is not None
                or host.password is not None):
            return False

        if scheme not in ("http", "https"):
            return False
        default_port = 443 if scheme == "https" else 80
        origin_port = default_port if origin.port is None else origin.port
        host_port = default_port if host.port is None else host.port
        return (
            origin.hostname.lower() == host.hostname.lower()
            and origin_port == host_port
        )
    except (UnicodeError, ValueError):
        return False
