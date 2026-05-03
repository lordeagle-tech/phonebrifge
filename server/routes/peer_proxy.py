"""
Browser-facing proxy routes for controlling a peer laptop.

The browser is PIN-authed against the main bridge as usual; the main
bridge then forwards screen + input traffic to the chosen peer using
the peer's per-pair secret. The peer's local HTTP server is never
exposed to the browser directly — every byte flows through the main
bridge.

Endpoints (all PIN-auth; peers themselves are auth'd by the secret
the main bridge already holds in the registry):
  GET /api/peers/{id}/screen.mjpeg?token=...&fps=&quality=
      Streams MJPEG from the peer's screen.
  WS  /ws/peers/{id}/input?token=...
      Bidirectional JSON proxy to the peer's input websocket. The
      browser sends mouse/keyboard messages; the peer can echo
      pings (used by the browser to compute round-trip latency).

Plus generic catch-alls used by the phone "machine picker":
  ANY /api/peers/{id}/{rest:path}        -> http://peer/api/{rest}
  WS  /ws/peers/{id}/{rest:path}         -> ws://peer/ws/{rest}
These let the phone re-target any tab (Files, Stats, Clipboard,
Terminal, …) at a peer laptop without per-feature plumbing.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlencode

import websockets
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from .. import audit, peer_registry, security
from ..auth import verify_token
from ..deps import require_auth

http_router = APIRouter()
ws_router = APIRouter()

PEER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
# Cap MJPEG read size to avoid letting a hostile peer blow up memory
# with an infinite chunk. 1 MiB is comfortably more than the largest
# JPEG frame we'd ever capture (full HD JPEG at q=95 is well under that).
MJPEG_CHUNK = 64 * 1024
MJPEG_TIMEOUT = 30.0
INPUT_RECV_TIMEOUT = 60.0
PROXY_CHUNK = 64 * 1024
PROXY_TIMEOUT = 30.0
# Hop-by-hop request headers we MUST NOT forward verbatim — they
# describe the browser↔main-bridge connection, not the main↔peer one.
_DROP_REQ_HEADERS = {
    "host", "authorization", "cookie", "connection",
    "content-length", "transfer-encoding", "accept-encoding",
    "x-forwarded-for", "x-real-ip", "x-forwarded-proto",
}
_DROP_RESP_HEADERS = {
    "content-encoding", "transfer-encoding", "connection",
    "keep-alive", "content-length",
}


def _peer_endpoint_or_404(pid: str) -> dict:
    if not PEER_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="Invalid peer id")
    ep = peer_registry.get_endpoint(pid)
    if ep is None:
        # Either unknown peer, or a peer that paired before we added
        # the screen+input feature (no peer_port). Either way the
        # browser gets a clean "not controllable" signal.
        raise HTTPException(status_code=503,
                            detail="Peer is not reachable for control")
    if not peer_registry.online(pid):
        raise HTTPException(status_code=503, detail="Peer is offline")
    return ep


@http_router.get("/peers/{peer_id}/screen.mjpeg")
async def proxy_screen_mjpeg(
    peer_id: str,
    request: Request,
    fps: int = Query(10, ge=1, le=30),
    quality: int = Query(60, ge=20, le=95),
    token: Optional[str] = Query(None),
):
    """Proxy the peer's MJPEG stream to the browser. We hop to a
    background thread to do the blocking ``urlopen`` + chunked read,
    pushing bytes into an asyncio queue that StreamingResponse
    drains. This is simpler than wiring an async HTTP client and lets
    us keep the dep list lean (urllib + threading only)."""
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    ep = _peer_endpoint_or_404(peer_id)

    qs = urlencode({"secret": ep["secret"], "fps": fps, "quality": quality})
    upstream = f"http://{ep['ip']}:{ep['port']}/screen.mjpeg?{qs}"

    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _pull() -> None:
        # Plain HTTP — peer server is LAN-only and gated by the per-
        # peer secret which the browser never sees. We use stdlib so
        # no extra deps; the cost is a thread per active viewer,
        # which is fine for the handful of peers a user actually has.
        from urllib.request import urlopen
        try:
            with urlopen(upstream, timeout=10.0) as resp:
                while not stop.is_set():
                    chunk = resp.read(MJPEG_CHUNK)
                    if not chunk:
                        break
                    fut = asyncio.run_coroutine_threadsafe(
                        queue.put(chunk), loop)
                    try:
                        fut.result(timeout=MJPEG_TIMEOUT)
                    except Exception:  # noqa: BLE001
                        break
        except Exception:  # noqa: BLE001
            pass
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    asyncio.get_event_loop().run_in_executor(None, _pull)
    audit.log("peer", "screen_open",
              ip=request.client.host if request.client else "-",
              outcome="ok",
              detail={"peer_id": peer_id, "fps": fps, "quality": quality})

    async def stream():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(),
                                                  timeout=MJPEG_TIMEOUT)
                except asyncio.TimeoutError:
                    break
                if item is None:
                    break
                yield item
        finally:
            stop.set()

    return StreamingResponse(
        stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@ws_router.websocket("/ws/peers/{peer_id}/input")
async def proxy_input_ws(websocket: WebSocket, peer_id: str,
                         token: Optional[str] = Query(None)):
    """Bidirectional text-frame proxy to the peer's /input WS.

    Browser <-> main bridge <-> peer agent. We pump messages in both
    directions; either side closing tears the whole pipe down so the
    browser sees the disconnect immediately."""
    ip = security.ws_client_ip(websocket)
    if not verify_token(token):
        await websocket.close(code=4001)
        return
    if not PEER_ID_RE.match(peer_id):
        await websocket.close(code=4000)
        return
    ep = peer_registry.get_endpoint(peer_id)
    if ep is None or not peer_registry.online(peer_id):
        await websocket.close(code=4004, reason="peer offline")
        return
    if not security.check_rate(ip, "default"):
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("peer", "input_open", ip=ip, outcome="ok",
              detail={"peer_id": peer_id})

    qs = urlencode({"secret": ep["secret"]})
    upstream_url = f"ws://{ep['ip']}:{ep['port']}/input?{qs}"

    try:
        async with websockets.connect(upstream_url,
                                      ping_interval=None,
                                      open_timeout=5.0) as up:
            async def browser_to_peer() -> None:
                try:
                    while True:
                        msg = await asyncio.wait_for(
                            websocket.receive_text(),
                            timeout=INPUT_RECV_TIMEOUT)
                        await up.send(msg)
                except (WebSocketDisconnect, asyncio.TimeoutError,
                        websockets.exceptions.WebSocketException):
                    pass

            async def peer_to_browser() -> None:
                try:
                    while True:
                        msg = await up.recv()
                        if isinstance(msg, bytes):
                            msg = msg.decode("utf-8", errors="replace")
                        await websocket.send_text(msg)
                except (WebSocketDisconnect,
                        websockets.exceptions.WebSocketException):
                    pass

            done, pending = await asyncio.wait(
                {asyncio.create_task(browser_to_peer()),
                 asyncio.create_task(peer_to_browser())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (OSError, websockets.exceptions.WebSocketException) as e:
        # Couldn't reach the peer agent at all — let the browser know.
        try:
            await websocket.close(code=1011, reason=f"peer unreachable: {e}")
        except Exception:  # noqa: BLE001
            pass
        return
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        audit.log("peer", "input_close", ip=ip, outcome="ok",
                  detail={"peer_id": peer_id})


# ---------------------------------------------------------------------------
# Generic catch-alls for the phone "machine picker"
#
# These let the phone UI keep using its existing API surface
# (/api/files, /ws/terminal, …) and just rewrite the URL to
# /api/peers/{id}/files or /ws/peers/{id}/terminal when the user
# targets a different laptop. The phone stays totally unaware of
# the per-peer secret — only the main bridge knows it.
# ---------------------------------------------------------------------------


def _filtered_request_headers(request: Request) -> dict:
    """Drop hop-by-hop + auth headers before forwarding to the peer.
    The peer authenticates via ``?secret=`` only — leaking the
    main-bridge bearer token would expose a different trust
    boundary entirely."""
    out = {}
    for k, v in request.headers.items():
        if k.lower() in _DROP_REQ_HEADERS:
            continue
        out[k] = v
    return out


def _filtered_response_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in _DROP_RESP_HEADERS:
            continue
        out[k] = v
    return out


def _build_upstream_url(ep: dict, rest: str, request: Request,
                        scheme: str = "http") -> str:
    """Compose the peer URL: keep the original query string, drop any
    incoming PIN token, and append the per-peer secret."""
    # Strip incoming `token=…` from the query string so the peer
    # doesn't see (or have to ignore) a PIN bearer it can't validate.
    items = [(k, v) for k, v in request.query_params.multi_items()
             if k != "token"]
    items.append(("secret", ep["secret"]))
    qs = urlencode(items)
    base = "/" + rest.lstrip("/")
    sep = "?" if qs else ""
    return f"{scheme}://{ep['ip']}:{ep['port']}{base}{sep}{qs}"


@http_router.api_route(
    "/peers/{peer_id}/{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
)
async def proxy_http(peer_id: str, rest: str, request: Request,
                     _token: str = Depends(require_auth)):
    """Generic HTTP proxy: ``/api/peers/{id}/<x>`` → ``http://peer/api/<x>``.

    Body is buffered (acceptable for the phone-bridge's typical
    payloads — a few-MB upload at most). Response is streamed so
    large file downloads don't have to fit in memory.

    The peer's secret is appended to the query string; the browser's
    PIN bearer is stripped so it never leaves the main bridge."""
    if not rest:
        # Without a tail, this would shadow ``GET /api/peers/{id}``
        # if registration order ever changed. Be explicit.
        raise HTTPException(status_code=404, detail="Not found")
    ep = _peer_endpoint_or_404(peer_id)

    # The phone hits /api/files etc., so we re-prepend /api when
    # forwarding to the peer (which mounts the same routers under
    # the same /api prefix).
    upstream = _build_upstream_url(ep, f"api/{rest}", request)
    headers = _filtered_request_headers(request)
    body = await request.body()

    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    meta: dict = {}

    def _pull() -> None:
        from urllib.request import Request as URequest, urlopen
        from urllib.error import HTTPError, URLError
        req = URequest(upstream, data=body if body else None,
                       method=request.method, headers=headers)
        try:
            resp = urlopen(req, timeout=PROXY_TIMEOUT)
        except HTTPError as e:
            # Forward the peer's status + body so the browser sees
            # the same 404/403/etc. it would have gotten locally.
            try:
                payload = e.read() or b""
            except Exception:  # noqa: BLE001
                payload = b""
            meta["status"] = e.code
            meta["headers"] = dict(e.headers or {})
            asyncio.run_coroutine_threadsafe(queue.put(payload), loop)
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            return
        except (URLError, OSError) as e:
            meta["status"] = 502
            meta["headers"] = {"content-type": "application/json"}
            err = (b'{"detail":"Peer unreachable: '
                   + str(e).encode("utf-8", "replace")
                   + b'"}')
            asyncio.run_coroutine_threadsafe(queue.put(err), loop)
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            return

        meta["status"] = resp.status
        meta["headers"] = dict(resp.headers)
        try:
            while not stop.is_set():
                chunk = resp.read(PROXY_CHUNK)
                if not chunk:
                    break
                fut = asyncio.run_coroutine_threadsafe(
                    queue.put(chunk), loop)
                try:
                    fut.result(timeout=PROXY_TIMEOUT)
                except Exception:  # noqa: BLE001
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    pull_fut = loop.run_in_executor(None, _pull)

    # Wait for the worker to set the response status/headers (or
    # finish if the request errored before connecting). The first
    # queued item is either the start of the body or the entire
    # error payload.
    while "status" not in meta:
        if pull_fut.done():
            break
        await asyncio.sleep(0.01)

    status = meta.get("status", 502)
    resp_headers = _filtered_response_headers(meta.get("headers") or {})

    async def stream():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=PROXY_TIMEOUT)
                except asyncio.TimeoutError:
                    break
                if item is None:
                    break
                yield item
        finally:
            stop.set()

    return StreamingResponse(stream(), status_code=status,
                             headers=resp_headers)


@ws_router.websocket("/ws/peers/{peer_id}/{rest:path}")
async def proxy_ws(websocket: WebSocket, peer_id: str, rest: str,
                   token: Optional[str] = Query(None)):
    """Generic WebSocket proxy: ``/ws/peers/{id}/<x>`` →
    ``ws://peer/ws/<x>``. Forwards both text and binary frames."""
    ip = security.ws_client_ip(websocket)
    if not verify_token(token):
        await websocket.close(code=4001)
        return
    if not PEER_ID_RE.match(peer_id):
        await websocket.close(code=4000)
        return
    if rest in ("", "input"):
        # /input has its own dedicated handler above; let it win.
        # Empty rest would dead-end on the peer.
        await websocket.close(code=4004)
        return
    ep = peer_registry.get_endpoint(peer_id)
    if ep is None or not peer_registry.online(peer_id):
        await websocket.close(code=4004, reason="peer offline")
        return
    if not security.check_rate(ip, "default"):
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("peer", "ws_open", ip=ip, outcome="ok",
              detail={"peer_id": peer_id, "kind": rest})

    upstream_url = _build_upstream_url(ep, f"ws/{rest}", websocket,
                                       scheme="ws")  # type: ignore[arg-type]

    try:
        async with websockets.connect(upstream_url,
                                      ping_interval=None,
                                      open_timeout=5.0,
                                      max_size=None) as up:
            async def browser_to_peer() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if (text := msg.get("text")) is not None:
                            await up.send(text)
                        elif (data := msg.get("bytes")) is not None:
                            await up.send(data)
                except (WebSocketDisconnect,
                        websockets.exceptions.WebSocketException):
                    pass

            async def peer_to_browser() -> None:
                try:
                    while True:
                        msg = await up.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except (WebSocketDisconnect,
                        websockets.exceptions.WebSocketException):
                    pass

            # The watchdog closes ``websocket`` when the PIN session
            # expires or the user logs out, which causes
            # ``browser_to_peer`` to exit; we then cancel both relay
            # tasks and close the upstream peer socket. Without this
            # a logged-out user could keep a live shell on the peer
            # indefinitely (the dedicated /ws/terminal handler enforces
            # the same rule, so the peer-targeted version must too).
            tasks = {
                asyncio.create_task(browser_to_peer()),
                asyncio.create_task(peer_to_browser()),
                asyncio.create_task(security.ws_session_watchdog(
                    websocket, token, "peer", f"ws_{rest}", ip)),
            }
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (OSError, websockets.exceptions.WebSocketException) as e:
        try:
            await websocket.close(code=1011, reason=f"peer unreachable: {e}")
        except Exception:  # noqa: BLE001
            pass
        return
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        audit.log("peer", "ws_close", ip=ip, outcome="ok",
                  detail={"peer_id": peer_id, "kind": rest})
