import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from server.routes import scrcpy


def fake_request():
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/scrcpy/stop",
        "raw_path": b"/api/scrcpy/stop",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    })


class ScrcpyStopRotationRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_mirror_stop_restores_saved_phone_rotation(self):
        stopped = {"running": False, "serial": None}
        restored = {"ok": True, "restored": True, "rotation": 0, "auto_rotate": True}
        with (
            patch.object(scrcpy.scrcpy_state, "status", return_value={
                "running": True, "serial": "phone-serial",
            }),
            patch.object(scrcpy.scrcpy_state, "stop", return_value=stopped),
            patch.object(scrcpy.phone_input, "device_action", new=AsyncMock(
                return_value=restored,
            )) as device_action,
            patch.object(scrcpy.audit, "log"),
        ):
            result = await scrcpy.stop(fake_request(), _token="test")

        device_action.assert_awaited_once_with("phone-serial", "rotate_restore")
        self.assertEqual(result["rotation_restore"], restored)

    async def test_stop_without_active_mirror_does_not_restore_an_unrelated_phone(self):
        with (
            patch.object(scrcpy.scrcpy_state, "status", return_value={
                "running": False, "serial": None,
            }),
            patch.object(scrcpy.scrcpy_state, "stop", return_value={"running": False}),
            patch.object(scrcpy.phone_input, "device_action", new=AsyncMock()) as device_action,
            patch.object(scrcpy.audit, "log"),
        ):
            result = await scrcpy.stop(fake_request(), _token="test")

        device_action.assert_not_awaited()
        self.assertNotIn("rotation_restore", result)


if __name__ == "__main__":
    unittest.main()
