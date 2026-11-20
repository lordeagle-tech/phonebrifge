import asyncio
import threading
import time
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, File, UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from server import auth, peer_pair_code, security
from server.request_limits import RequestBodyLimitMiddleware
from server.security_origin import websocket_origin_is_allowed
from server.routes import files as file_routes
from server.routes.peer_proxy import _build_upstream_url, _peer_ssl_context
import peer_agent


TEST_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDFzCCAf+gAwIBAgIUb/ndLIvlRVH6ciy/vXpUmk0KLAwwDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQcGhvbmVicmlkZ2UtdGVzdDAeFw0yNjExMjAwOTQzNDJa
Fw0yNjExMjIwOTQzNDJaMBsxGTAXBgNVBAMMEHBob25lYnJpZGdlLXRlc3QwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDBTlKi7UB4AMAGeZF+edXhJXnD
4XfZq/hkACO+yeMhaeGNq8lMbMH/N2ri3bp2bVv4mchO8GPBKV0WPB5ab7up1E6O
YFDhYcQNktVBPFqiIPnfiTAncizOAFS8yCwC8Ek5DMP9G2teV2r+k20rMkMGzWYs
W9yPWO9w+s9hGfSj5GCY6az1lfkah1nd2RkHZsqfOrfQsG49na+QrU2aug38P2jO
sVwcj2AbRIsrYqbu4ec4ZDosloeMScIiu2RB8EO+nXS+z4Y2DxQEQW2o3AZyHOqw
B7A1pHByXySGtAOrm40OC+wkRETlU8cHYgDDH3CHKRprjdNgLTWv4sU5ZWDRAgMB
AAGjUzBRMB0GA1UdDgQWBBSEhDhRlGgygGBdzRXHXHpzJDG4TjAfBgNVHSMEGDAW
gBSEhDhRlGgygGBdzRXHXHpzJDG4TjAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3
DQEBCwUAA4IBAQB/n3UlwFqu78pO336fT6cNsF7saeqOOyF06m+PL9+ivPrtrU7h
jjRaJx8iiedKnsew27cQ73ZIWIiO8QmL4cHJ7b+p5iLaG1DCajuWxYwFK4JczENr
0EoNYZfQEDf0H2TwLla0qTj952kaQUxLgTFt5a/uJePqGH1yPIjlMXSvMBsd29AV
4qUXOZ+gFqzknELvUwIrApkY0T1MKSaRE9L7eks3nxP42pGTa6tQcJ0OjJHI3+Zy
mhR2XgaHOIYWR0ghZDQVoO4Q3sxhdENG3nRcxPRNOo1Q5Tz6LdxzjIxVG23qZ7q3
2QKClUHNXGQGcxWT5hg7NFWKgNgcztm9jgUt
-----END CERTIFICATE-----
"""


class UploadAndArchiveSecurityTests(unittest.TestCase):
    def test_upload_destination_rejects_path_components_and_existing_targets(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = home / "uploads"
            target.mkdir()
            with patch.object(file_routes, "HOME", home):
                for bad_name in (
                    "../escape", "sub/file", r"sub\\file", ".", "..", ".. ",
                    "C:escape", "file.txt:stream", "",
                ):
                    with self.subTest(name=bad_name):
                        with self.assertRaises(HTTPException):
                            file_routes._safe_upload_destination(target, bad_name)
                existing = target / "already.txt"
                existing.write_text("preserve", encoding="utf-8")
                with self.assertRaises(HTTPException) as exc:
                    file_routes._safe_upload_destination(target, existing.name)
                self.assertEqual(exc.exception.status_code, 409)
                self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")

    def test_zip_excludes_symlinks_and_out_of_home_descendants(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            selected = home / "selected"
            outside = Path(td) / "outside-secret.txt"
            selected.mkdir(parents=True)
            outside.write_text("secret", encoding="utf-8")
            (selected / "safe.txt").write_text("safe", encoding="utf-8")
            (selected / r"..\..\outside-secret.txt").write_text(
                "misleading name", encoding="utf-8")
            (selected / "file.txt:alternate-stream").write_text(
                "alternate stream name", encoding="utf-8")
            symlinks_available = True
            try:
                (selected / "external-link").symlink_to(outside)
                (selected / "external-dir").symlink_to(
                    outside.parent, target_is_directory=True)
            except (OSError, NotImplementedError):
                symlinks_available = False
            archive = Path(td) / "result.zip"
            with patch.object(file_routes, "HOME", home):
                file_routes._build_zip_sync(
                    [selected], archive, threading.Event())
            with zipfile.ZipFile(archive) as zf:
                names = set(zf.namelist())
                self.assertIn("selected/safe.txt", names)
                self.assertFalse(any("\\" in name for name in names))
                self.assertFalse(any(":" in name for name in names))
                if symlinks_available:
                    self.assertFalse(any("external-link" in name for name in names))
                    self.assertFalse(any("external-dir" in name for name in names))
                self.assertNotIn("outside-secret.txt", names)


class RequestBodyLimitTests(unittest.TestCase):
    def _invoke(self, *, headers, chunks, limit=5, path="/api/files/upload",
                limited_path="/api/files/upload"):
        result = {}
        app_called = False

        async def app(scope, receive, send):
            nonlocal app_called
            app_called = True
            try:
                while True:
                    message = await receive()
                    if not message.get("more_body", False):
                        break
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail},
                                   status_code=exc.status_code)(scope, receive, send)

        queue = list(chunks)

        async def receive():
            return queue.pop(0)

        async def send(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]

        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
        }
        middleware = RequestBodyLimitMiddleware(
            app, path=limited_path, max_bytes=limit,
            include_peer_proxy=path.startswith("/api/peers/"))
        asyncio.run(middleware(scope, receive, send))
        return result.get("status"), app_called

    def test_content_length_rejected_before_application(self):
        status, app_called = self._invoke(
            headers=[(b"content-length", b"6")], chunks=[])
        self.assertEqual(status, 413)
        self.assertFalse(app_called)

    def test_peer_pairing_body_cap_rejects_oversized_json(self):
        status, app_called = self._invoke(
            headers=[(b"content-length", b"16385")], chunks=[],
            limit=16 * 1024, path="/api/peers/pair",
            limited_path="/api/peers/pair")
        self.assertEqual(status, 413)
        self.assertFalse(app_called)

    def test_chunked_body_rejected_at_stream_limit(self):
        chunks = [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": True},
        ]
        status, app_called = self._invoke(headers=[], chunks=chunks)
        self.assertEqual(status, 413)
        self.assertTrue(app_called)

    def test_multipart_parser_returns_413_for_oversized_chunked_body(self):
        app = FastAPI()

        @app.post("/api/files/upload")
        async def upload(file: UploadFile = File(...)):
            return {"ok": True}

        boundary = b"test-boundary"
        body = (
            b"--" + boundary
            + b'\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
            + b"1234567890\r\n--" + boundary + b"--\r\n"
        )
        chunks = [body[:10], body[10:]]
        messages = []

        async def receive():
            if chunks:
                data = chunks.pop(0)
                return {"type": "http.request", "body": data,
                        "more_body": bool(chunks)}
            return {"type": "http.request", "body": b"",
                    "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/files/upload", "raw_path": b"/api/files/upload",
            "query_string": b"", "root_path": "",
            "headers": [(b"content-type",
                         b"multipart/form-data; boundary=test-boundary")],
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
        }
        middleware = RequestBodyLimitMiddleware(
            app.build_middleware_stack(), path="/api/files/upload", max_bytes=5)
        asyncio.run(middleware(scope, receive, send))
        status = next(message["status"] for message in messages
                      if message["type"] == "http.response.start")
        self.assertEqual(status, 413)

    def test_buffered_peer_proxy_body_is_also_bounded(self):
        chunks = [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": True},
        ]
        status, app_called = self._invoke(
            headers=[], chunks=chunks,
            path="/api/peers/0123456789ab/files/upload")
        self.assertEqual(status, 413)
        self.assertTrue(app_called)


class WebSocketOriginTests(unittest.TestCase):
    def _scope(self, scheme, host, origin=None):
        headers = [(b"host", host.encode("ascii"))]
        if origin is not None:
            headers.append((b"origin", origin.encode("ascii")))
        return {"type": "websocket", "scheme": scheme, "headers": headers}

    def test_same_origin_wss_through_proxy(self):
        self.assertTrue(websocket_origin_is_allowed(
            self._scope("wss", "bridge.example", "https://bridge.example")))

    def test_cross_scheme_and_cross_port_origins_are_rejected(self):
        self.assertFalse(websocket_origin_is_allowed(
            self._scope("wss", "bridge.example", "http://bridge.example")))
        self.assertFalse(websocket_origin_is_allowed(
            self._scope("wss", "bridge.example", "https://bridge.example:8443")))

    def test_native_client_without_origin_remains_supported(self):
        self.assertTrue(websocket_origin_is_allowed(
            self._scope("ws", "127.0.0.1:8765")))


class ConnectionTicketTests(unittest.TestCase):
    def setUp(self):
        self.token = auth.create_session()

    def tearDown(self):
        auth.invalidate_session(self.token)

    def test_ticket_is_single_use_and_path_scoped(self):
        ticket = auth.create_connection_ticket(self.token, "/ws/example")
        self.assertIsNotNone(ticket)
        self.assertEqual(
            auth.consume_connection_ticket(ticket, "/ws/example"), self.token)
        self.assertIsNone(auth.consume_connection_ticket(ticket, "/ws/example"))

        wrong_path_ticket = auth.create_connection_ticket(self.token, "/ws/one")
        self.assertIsNone(
            auth.consume_connection_ticket(wrong_path_ticket, "/ws/two"))
        self.assertIsNone(
            auth.consume_connection_ticket(wrong_path_ticket, "/ws/one"))

    def test_ticket_expires(self):
        ticket = auth.create_connection_ticket(self.token, "/api/files/download")
        now = time.monotonic()
        with patch("server.auth.time.monotonic", return_value=now + auth.CONNECTION_TICKET_TTL + 1):
            self.assertIsNone(
                auth.consume_connection_ticket(ticket, "/api/files/download"))

    def test_pair_code_shape_and_pair_attempt_limit(self):
        code, _ = peer_pair_code.get()
        self.assertEqual(len(code), peer_pair_code.CODE_DIGITS)
        self.assertTrue(code.isdigit())
        self.assertTrue(peer_pair_code.verify(code))
        self.assertEqual(security.LIMITS["pair"], (60, 3))
        self.assertEqual(security.LIMITS["ticket"], (60, 120))

    def test_pair_code_is_consumed_and_cannot_be_replayed(self):
        code, _ = peer_pair_code.get()
        self.assertTrue(peer_pair_code.consume(code))
        self.assertFalse(peer_pair_code.consume(code))
        self.assertFalse(peer_pair_code.verify(code))

    def test_passive_validation_does_not_renew_inactivity_timestamp(self):
        old_timeout = auth.get_inactivity_timeout()
        try:
            auth.set_inactivity_timeout(300)
            before = auth._sessions[self.token]
            with patch("server.auth.time.time", return_value=before[1] + 100):
                self.assertTrue(auth.is_token_valid(self.token))
            self.assertEqual(auth._sessions[self.token], before)
        finally:
            auth.set_inactivity_timeout(old_timeout)


class PeerTlsTrustTests(unittest.TestCase):
    def test_peer_proxy_trusts_only_advertised_certificate(self):
        context = _peer_ssl_context({"cert_pem": TEST_CERT_PEM})
        self.assertEqual(context.cert_store_stats()["x509_ca"], 1)
        self.assertFalse(context.check_hostname)

    def test_peer_agent_pin_context_trusts_only_pinned_certificate(self):
        context = peer_agent._ssl_ctx_for_main(
            "https://bridge.example", TEST_CERT_PEM)
        self.assertEqual(context.cert_store_stats()["x509_ca"], 1)
        self.assertFalse(context.check_hostname)

    def test_peer_upstream_url_never_contains_credentials(self):
        request = Request({
            "type": "http", "method": "GET", "scheme": "https",
            "server": ("bridge.example", 443),
            "client": ("127.0.0.1", 12345), "headers": [],
            "query_string": b"fps=5&secret=browser-value&token=browser-token&ticket=browser-ticket",
        })
        upstream = _build_upstream_url(
            {"ip": "192.0.2.10", "port": 9443, "secret": "peer-secret"},
            "screen.mjpeg", request)
        self.assertEqual(upstream, "https://192.0.2.10:9443/screen.mjpeg?fps=5")
        for credential in ("peer-secret", "browser-value", "browser-token", "browser-ticket"):
            self.assertNotIn(credential, upstream)


if __name__ == "__main__":
    unittest.main()
from starlette.requests import Request
