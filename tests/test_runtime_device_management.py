from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inkcards_anki_bridge.runtime import AddonRuntime


class DeviceManagementRuntimeForwardingTests(unittest.TestCase):
    def test_device_management_methods_forward_only_to_runtime_authenticator(
        self,
    ) -> None:
        class FakeDeviceAdmin:
            def __init__(self) -> None:
                self.calls = []

            def add_device(self, name):
                self.calls.append(("add_device", name))
                return {
                    "slot_id": "slot-a",
                    "name": name,
                    "status": "pending",
                    "code": "123456789012",
                }

            def list_devices(self, *, include_revoked=False):
                self.calls.append(("list_devices", include_revoked))
                return [{"slot_id": "slot-a", "name": "reader", "status": "pending"}]

            def allow_rebind(self, slot_id):
                self.calls.append(("allow_rebind", slot_id))

            def reset_pairing_code(self, slot_id):
                self.calls.append(("reset_pairing_code", slot_id))
                return {"slot_id": slot_id, "code": "987654321098"}

            def remove_device(self, slot_id):
                self.calls.append(("remove_device", slot_id))

            def rename_device(self, slot_id, name):
                self.calls.append(("rename_device", slot_id, name))

        with tempfile.TemporaryDirectory() as temp:
            runtime = AddonRuntime(profile_id="profile-test", root=Path(temp))
            runtime._state = "running"
            runtime._server = object()
            admin = FakeDeviceAdmin()
            runtime._auth = admin

            created = runtime.add_device("reader")
            listed = runtime.list_devices()
            runtime.allow_rebind("slot-a")
            reset = runtime.reset_pairing_code("slot-a")
            runtime.remove_device("slot-a")
            runtime.rename_device("slot-a", "living room")

        self.assertEqual(created["code"], "123456789012")
        self.assertNotIn("code", listed[0])
        self.assertEqual(reset["code"], "987654321098")
        self.assertEqual(
            admin.calls,
            [
                ("add_device", "reader"),
                ("list_devices", False),
                ("allow_rebind", "slot-a"),
                ("reset_pairing_code", "slot-a"),
                ("remove_device", "slot-a"),
                ("rename_device", "slot-a", "living room"),
            ],
        )


class AddonRuntimeDeviceManagementIntegrationTests(unittest.TestCase):
    def test_real_runtime_auth_device_management_round_trip_without_socket_bind(
        self,
    ) -> None:
        class NoSocketServer:
            def __init__(self, _router, *, host, port, **_kwargs):
                self.server_address = (host, port)

            def start(self):
                pass

            def stop(self, *, timeout=None):
                pass

        with tempfile.TemporaryDirectory() as temp:
            runtime = AddonRuntime(
                profile_id="profile-device-management",
                root=Path(temp) / "profile-root",
                host="127.0.0.1",
                port=0,
            )
            with patch("inkcards_anki_bridge.runtime.DeviceServer", NoSocketServer):
                runtime.start()
            try:
                created = runtime.add_device("客厅阅读器")
                slot_id = created["slot_id"]
                first_code = created["code"]
                self.assertRegex(first_code, r"^[0-9]{12}$")
                self.assertNotIn(first_code, repr(created))

                listed = runtime.list_devices()
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["status"], "pending")
                self.assertNotIn("code", listed[0])
                self.assertNotIn("token", listed[0])

                credential = runtime.auth.pair(first_code, "reader-a")
                self.assertNotIn(credential.token, repr(credential))
                runtime.allow_rebind(slot_id)
                self.assertEqual(runtime.list_devices()[0]["status"], "rebind_ready")

                reset = runtime.reset_pairing_code(slot_id)
                self.assertRegex(reset["code"], r"^[0-9]{12}$")
                self.assertNotEqual(reset["code"], first_code)
                self.assertNotIn(reset["code"], repr(reset))
                runtime.rename_device(slot_id, "书房阅读器")
                self.assertEqual(runtime.list_devices()[0]["name"], "书房阅读器")

                runtime.remove_device(slot_id)
                self.assertEqual(runtime.list_devices(), [])
                removed = runtime.list_devices(include_revoked=True)
                self.assertEqual(removed[0]["status"], "revoked")
                self.assertEqual(removed[0]["name"], "书房阅读器")
                self.assertNotIn("code", removed[0])
                self.assertNotIn("token", removed[0])
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
