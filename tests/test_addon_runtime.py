from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from inkcards_anki_bridge.device.server import ServerLimits
from inkcards_anki_bridge.profile_identity import (
    ProfileIdentityError,
    load_or_create_profile_identity,
)
from inkcards_anki_bridge.runtime import AddonRuntime, BridgeRuntime


class ProfileIdentityTests(unittest.TestCase):
    def test_first_open_reopen_and_profile_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "user_files"
            profile_a = Path(temp) / "Anki" / "User 1"
            profile_b = Path(temp) / "Anki" / "User 2"

            first = load_or_create_profile_identity(root, profile_a)
            reopened = load_or_create_profile_identity(root, profile_a)
            other = load_or_create_profile_identity(root, profile_b)

            self.assertEqual(first.profile_id, reopened.profile_id)
            self.assertEqual(first.namespace, reopened.namespace)
            self.assertNotEqual(first.profile_id, other.profile_id)
            self.assertNotEqual(first.namespace, other.namespace)
            self.assertEqual(stat.S_IMODE(first.identity_path.stat().st_mode), 0o600)
            self.assertTrue(first.identity_path.read_bytes().startswith(b"{"))
            self.assertEqual(first.root.parent, root / "profiles")

    def test_corrupt_or_insecure_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "user_files"
            identity = load_or_create_profile_identity(root, Path(temp) / "profile")
            identity.identity_path.write_text("{not-json", encoding="utf-8")
            os.chmod(identity.identity_path, 0o600)
            with self.assertRaises(ProfileIdentityError):
                load_or_create_profile_identity(root, Path(temp) / "profile")

            identity.identity_path.write_text(
                json.dumps({"profile_id": identity.profile_id, "schema": 1}),
                encoding="utf-8",
            )
            os.chmod(identity.identity_path, 0o644)
            with self.assertRaises(ProfileIdentityError):
                load_or_create_profile_identity(root, Path(temp) / "profile")


class AddonRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.identity = load_or_create_profile_identity(
            Path(self.tempdir.name) / "user_files",
            Path(self.tempdir.name) / "anki-profile",
        )
        self.runtime = AddonRuntime(
            profile_id=self.identity.profile_id,
            root=self.identity.root,
            host="127.0.0.1",
            port=0,
            server_limits=ServerLimits(
                max_workers=1,
                work_queue_size=1,
                max_connections=2,
                read_timeout=0.5,
                write_timeout=0.5,
                shutdown_timeout=1.0,
            ),
        )
        self.runtime.start()

    def tearDown(self) -> None:
        self.runtime.stop()
        self.tempdir.cleanup()

    def request(self, path: str, payload: object, token: str | None = None):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.runtime.port, timeout=2)
        try:
            connection.request("POST", path, raw, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def pair(self) -> tuple[str, str]:
        code = self.runtime.issue_pairing_code()
        status, body = self.request("/v2/pair", {"code": code, "device_id": "device-a"})
        self.assertEqual(status, 200)
        return body["device_id"], body["token"]

    def test_start_status_pair_revoke_and_stop_are_bounded(self) -> None:
        first_epoch = self.runtime.runtime_epoch
        self.assertTrue(self.runtime.running)
        self.assertIs(self.runtime.start(), self.runtime)
        self.assertEqual(self.runtime.runtime_epoch, first_epoch)

        status = self.runtime.status()
        self.assertEqual(status["profile_id"], self.identity.profile_id)
        self.assertEqual(status["bind"]["host"], "127.0.0.1")
        self.assertEqual(status["bind"]["port"], self.runtime.port)
        self.assertFalse(status["write_enabled"])
        self.assertIn("P3", status["write_disabled_reason"])
        self.assertFalse(status["snapshot"]["available"])
        self.assertNotIn(str(self.identity.root), json.dumps(status))

        device_id, token = self.pair()
        status_code, handshake = self.request(
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": device_id, "session_id": "session-a"},
            token,
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(handshake["session_id"], "session-a")

        self.runtime.revoke_device(device_id)
        status_code, body = self.request(
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": device_id, "session_id": "session-b"},
            token,
        )
        self.assertEqual(status_code, 401)
        self.assertEqual(body["error"]["code"], "invalid_token")

        port = self.runtime.port
        self.runtime.stop()
        self.runtime.stop()
        self.assertFalse(self.runtime.running)
        with self.assertRaises((ConnectionRefusedError, OSError, socket.timeout)):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
            try:
                connection.request("POST", "/v2/pair", b"{}")
                connection.getresponse()
            finally:
                connection.close()

    def test_pending_review_keeps_pull_gate_closed_and_no_snapshot_is_fabricated(self) -> None:
        self.assertIsNone(self.runtime.last_snapshot)
        self.runtime.publish_snapshot(
            {"cards": [], "manifest": {"card_count": 0, "included_card_count": 0}},
            scope_id="deck:empty",
            applied_watermark=0,
        )
        _, token = self.pair()
        status_code, handshake = self.request(
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"},
            token,
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(handshake["capabilities"]["pull"])
        status_code, _ = self.request(
            "/v2/reviews",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "events": [
                    {
                        "event_id": "session-a:1",
                        "sequence": 1,
                        "payload": {"card_id": "card-1", "rating": 3},
                    }
                ],
            },
            token,
        )
        self.assertEqual(status_code, 200)
        status_code, body = self.request(
            "/v2/pull/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:empty",
                "local_generation": 0,
            },
            token,
        )
        self.assertEqual(status_code, 409)
        self.assertEqual(body["error"]["code"], "pull_gate_pending_apply")
        self.assertFalse(self.runtime.status()["write_enabled"])

    def test_expected_snapshot_context_rejects_publish_and_preserves_old_snapshot(self) -> None:
        old_context = self.runtime.snapshot_context()
        self.assertIsNotNone(old_context)
        assert old_context is not None
        old = self.runtime.publish_snapshot(
            {"cards": [], "manifest": {"card_count": 0, "included_card_count": 0}},
            scope_id="deck:atomic-context",
            generation=old_context.generation,
            applied_watermark=0,
            expected_context=old_context,
        )
        store = self.runtime.store
        self.assertIsNotNone(store)
        assert store is not None
        before = store.latest_snapshot(old.scope_id)

        self.runtime.update_collection_generation(old_context.generation + 1)
        with self.assertRaisesRegex(RuntimeError, "snapshot context changed"):
            self.runtime.publish_snapshot(
                {"cards": [], "manifest": {"card_count": 0, "included_card_count": 0}},
                scope_id=old.scope_id,
                generation=old_context.generation,
                applied_watermark=0,
                expected_context=old_context,
            )

        self.assertIs(self.runtime.last_snapshot, old)
        after = store.latest_snapshot(old.scope_id)
        self.assertEqual(after.snapshot_id, before.snapshot_id)
        self.assertEqual(after.checksum, before.checksum)

    def test_bind_failure_rolls_back_store_and_server(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            runtime = AddonRuntime(
                profile_id=self.identity.profile_id,
                root=Path(self.tempdir.name) / "occupied",
                host="127.0.0.1",
                port=port,
            )
            with self.assertRaises(OSError):
                runtime.start()
            self.assertFalse(runtime.running)
            self.assertIsNone(runtime.store)
            self.assertIsNone(runtime.server)
            runtime.stop()
        finally:
            listener.close()

    def test_bridge_name_is_only_a_compatibility_alias_for_new_runtime(self) -> None:
        self.assertIs(BridgeRuntime, AddonRuntime)


if __name__ == "__main__":
    unittest.main()
