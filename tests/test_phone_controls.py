import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from server import phone_input


class PhoneDeviceActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.rotation_file_patch = patch.object(
            phone_input, "_ROTATION_STATE_FILE",
            Path(self.temp_dir.name) / "rotation-restore.json",
        )
        self.rotation_file_patch.start()
        self.addCleanup(self.rotation_file_patch.stop)

    async def test_navigation_actions_map_to_fixed_android_keycodes(self):
        with patch.object(
            phone_input, "keyevent", new=AsyncMock(return_value={"ok": True}),
        ) as keyevent:
            for action, code in (("home", "3"), ("back", "4"), ("recents", "187")):
                with self.subTest(action=action):
                    result = await phone_input.device_action("test-serial", action)
                    self.assertTrue(result["ok"])
                    keyevent.assert_awaited_with("test-serial", code)

    async def test_rotate_locks_and_alternates_portrait_landscape(self):
        run_adb = AsyncMock(side_effect=[
            {"ok": True, "stdout": "0"},
            {"ok": True, "stdout": "1"},
            {"ok": True, "stdout": ""},
            {"ok": True, "stdout": ""},
        ])
        with patch.object(phone_input, "_run_adb", new=run_adb):
            result = await phone_input.device_action("test-serial", "rotate")

        self.assertTrue(result["ok"])
        self.assertEqual(result["orientation"], "landscape")
        self.assertEqual(result["rotation"], 1)
        self.assertEqual(run_adb.await_args_list, [
            call("test-serial", "settings", "get", "system", "user_rotation"),
            call("test-serial", "settings", "get", "system", "accelerometer_rotation"),
            call("test-serial", "settings", "put", "system", "accelerometer_rotation", "0"),
            call("test-serial", "settings", "put", "system", "user_rotation", "1"),
        ])
        self.assertTrue(result["restore_available"])
        self.assertEqual(phone_input._get_rotation_baseline("test-serial"), {
            "accelerometer_rotation": 1,
            "user_rotation": 0,
        })

    async def test_rotate_reuses_persisted_baseline_without_overwriting_it(self):
        baseline = {"accelerometer_rotation": 0, "user_rotation": 3}
        self.assertTrue(phone_input._set_rotation_baseline("test-serial", baseline))
        run_adb = AsyncMock(side_effect=[
            {"ok": True, "stdout": "2"},
            {"ok": True, "stdout": ""},
            {"ok": True, "stdout": ""},
        ])
        with patch.object(phone_input, "_run_adb", new=run_adb):
            result = await phone_input.device_action("test-serial", "rotate")

        self.assertTrue(result["ok"])
        self.assertEqual(phone_input._get_rotation_baseline("test-serial"), baseline)
        self.assertEqual(run_adb.await_count, 3)

    async def test_restore_rotation_restores_user_rotation_then_auto_rotate_and_clears_state(self):
        baseline = {"accelerometer_rotation": 1, "user_rotation": 3}
        self.assertTrue(phone_input._set_rotation_baseline("test-serial", baseline))
        run_adb = AsyncMock(return_value={"ok": True, "stdout": ""})
        with patch.object(phone_input, "_run_adb", new=run_adb):
            result = await phone_input.device_action("test-serial", "rotate_restore")

        self.assertEqual(run_adb.await_args_list, [
            call("test-serial", "settings", "put", "system", "user_rotation", "3"),
            call("test-serial", "settings", "put", "system", "accelerometer_rotation", "1"),
        ])
        self.assertEqual(result, {
            "ok": True, "restored": True, "rotation": 3, "auto_rotate": True,
        })
        self.assertIsNone(phone_input._get_rotation_baseline("test-serial"))
        self.assertFalse(phone_input._ROTATION_STATE_FILE.exists())

    async def test_failed_restore_keeps_baseline_for_retry(self):
        baseline = {"accelerometer_rotation": 0, "user_rotation": 2}
        self.assertTrue(phone_input._set_rotation_baseline("test-serial", baseline))
        failure = {"ok": False, "error": "device offline"}
        run_adb = AsyncMock(return_value=failure)
        with patch.object(phone_input, "_run_adb", new=run_adb):
            result = await phone_input.device_action("test-serial", "rotate_restore")

        self.assertEqual(result, failure)
        self.assertEqual(phone_input._get_rotation_baseline("test-serial"), baseline)
        run_adb.assert_awaited_once_with(
            "test-serial", "settings", "put", "system", "user_rotation", "2",
        )

    async def test_failed_auto_rotate_restore_keeps_baseline_for_retry(self):
        baseline = {"accelerometer_rotation": 1, "user_rotation": 2}
        self.assertTrue(phone_input._set_rotation_baseline("test-serial", baseline))
        run_adb = AsyncMock(side_effect=[
            {"ok": True, "stdout": ""},
            {"ok": False, "error": "permission denied"},
        ])
        with patch.object(phone_input, "_run_adb", new=run_adb):
            result = await phone_input.device_action("test-serial", "rotate_restore")

        self.assertFalse(result["ok"])
        self.assertEqual(phone_input._get_rotation_baseline("test-serial"), baseline)
        self.assertEqual(run_adb.await_count, 2)

    async def test_clearing_one_device_preserves_other_saved_baselines(self):
        first = {"accelerometer_rotation": 0, "user_rotation": 1}
        second = {"accelerometer_rotation": 1, "user_rotation": 2}
        self.assertTrue(phone_input._set_rotation_baseline("first", first))
        self.assertTrue(phone_input._set_rotation_baseline("second", second))
        self.assertTrue(phone_input._clear_rotation_baseline("first"))

        self.assertIsNone(phone_input._get_rotation_baseline("first"))
        self.assertEqual(phone_input._get_rotation_baseline("second"), second)

    async def test_keep_awake_only_changes_power_connected_behavior(self):
        run_adb = AsyncMock(return_value={"ok": True})
        with patch.object(phone_input, "_run_adb", new=run_adb):
            await phone_input.device_action("test-serial", "awake_on")
            await phone_input.device_action("test-serial", "awake_off")

        self.assertEqual(run_adb.await_args_list, [
            call("test-serial", "svc", "power", "stayon", "true"),
            call("test-serial", "svc", "power", "stayon", "false"),
        ])

    async def test_unsupported_action_is_rejected_without_adb(self):
        with patch.object(phone_input, "_run_adb", new=AsyncMock()) as run_adb:
            result = await phone_input.device_action("test-serial", "run arbitrary shell")

        self.assertFalse(result["ok"])
        run_adb.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
