"""Bound request bodies before multipart parsing or proxy buffering."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class RequestBodyLimitMiddleware:
    """Bound selected request bodies before application code reads them."""

    def __init__(self, app, *, path: str, max_bytes: int,
                 include_peer_proxy: bool = False):
        self.app = app
        self.path = path
        self.max_bytes = max_bytes
        self.include_peer_proxy = include_peer_proxy

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        method = scope.get("method", "")
        parts = path.split("/")
        is_peer_proxy = (
            len(parts) >= 5
            and parts[1:3] == ["api", "peers"]
            and len(parts[3]) == 12
            and all(char in "0123456789abcdef" for char in parts[3])
        )
        is_limited_path = path == self.path or (
            self.include_peer_proxy and is_peer_proxy
        )
        body_method = method in ("POST", "PUT", "PATCH") or (
            self.include_peer_proxy and is_peer_proxy
        )
        if (scope.get("type") != "http" or not body_method
                or not is_limited_path):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    await JSONResponse(
                        {"detail": "Request body is too large"},
                        status_code=413,
                    )(scope, receive, send)
                    return
            except ValueError:
                # Ignore malformed lengths; the streaming counter below is
                # authoritative and still enforces the cap.
                pass

        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="Request body is too large",
                    )
            return message

        await self.app(scope, limited_receive, send)
