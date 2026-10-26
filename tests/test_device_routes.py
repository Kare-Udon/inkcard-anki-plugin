from __future__ import annotations

import json
import tempfile
import unittest
import uuid
import secrets
from pathlib import Path
from typing import Any, Dict, Tuple

from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter, RouteLimits
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.storage import ProfileStore


class DeviceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: 1_000_000)
        self.inbox = InboxService(self.store)
        self.router = DeviceRouter(
            self.auth,
            self.inbox,
            limits=RouteLimits(max_request_bytes=16_384, max_response_bytes=16_384, max_review_events=3),
        )
        code = self.auth.issue_pairing_code()
        self.token = secrets.token_hex(32)
        pairing_id = str(uuid.uuid4())
        response = self.call("POST", "/v2/pair", {"code": code, "device_id": "device-a", "pairing_id": pairing_id, "token": self.token})
        self.assertEqual(response[0], 200)
        self.assertNotIn("token", response[1])
        self.assertEqual(self.call("POST", "/v2/pair/confirm", {"device_id": "device-a", "pairing_id": pairing_id}, {"Authorization": "Bearer " + self.token})[0], 200)
        self.headers = {"Authorization": "Bearer " + self.token}
        status, body = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], "x4-2")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def call(
        self,
        method: str,
        path: str,
        payload: Any,
        headers: Dict[str, str] | None = None,
    ) -> Tuple[int, Dict[str, Any]]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self.router.dispatch(method, path, headers or {}, raw)
        return response.status, json.loads(response.body.decode("utf-8"))

    def event(self, sequence: int, rating: int = 3) -> Dict[str, Any]:
        return {
            "event_id": "session-a:%d" % sequence,
            "sequence": sequence,
            "payload": {"card_id": "card-%d" % sequence, "rating": rating},
        }

    def test_handshake_negotiates_limits_and_does_not_return_card_library(self) -> None:
        status, body = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-b"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], "x4-2")
        self.assertIn("limits", body)
        self.assertIn("session", body)
        self.assertNotIn("cards", body)
        self.assertFalse(body["capabilities"]["pull"])

    def test_handshake_can_allocate_a_new_session_when_id_is_omitted(self) -> None:
        status, body = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["session_id"].startswith("s_"))
        self.assertEqual(body["session"]["next_sequence"], 1)

    def test_auth_token_and_device_binding_are_enforced(self) -> None:
        status, body = self.call(
            "POST",
            "/v2/sync/status",
            {"device_id": "device-b", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_token")
        self.assertNotIn(self.token, json.dumps(body))
        status, body = self.call(
            "POST",
            "/v2/sync/status",
            {"device_id": "device-a", "session_id": "session-a"},
            {"Authorization": "Bearer wrong"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_token")

    def test_reviews_are_idempotent_conflicts_have_no_success_ack_and_receipts_recover(self) -> None:
        payload = {"device_id": "device-a", "session_id": "session-a", "events": [self.event(1)]}
        status, body = self.call("POST", "/v2/reviews", payload, self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["receipts"][0]["status"], "received")
        status, body = self.call("POST", "/v2/reviews", payload, self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["receipts"][0]["status"], "received")

        conflict = {"device_id": "device-a", "session_id": "session-a", "events": [self.event(1, 4)]}
        status, body = self.call("POST", "/v2/reviews", conflict, self.headers)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "receipt_conflict")
        self.assertNotIn("receipts", body)

        status, body = self.call(
            "POST",
            "/v2/receipts",
            {"device_id": "device-a", "session_id": "session-a", "event_ids": ["session-a:1"]},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["receipts"][0]["status"], "received")
        self.assertEqual(body["applications"][0]["status"], "pending_apply")

    def test_failed_batch_commits_no_event(self) -> None:
        invalid = {
            "device_id": "device-a",
            "session_id": "session-a",
            "events": [self.event(1), {"event_id": "session-a:2", "sequence": 2, "payload": {"rating": 9}}],
        }
        status, body = self.call("POST", "/v2/reviews", invalid, self.headers)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_event")
        self.assertEqual(self.store.count_events(), 0)

    def test_status_reads_current_sqlite_and_separates_delivery_from_application_counts(self) -> None:
        self.call(
            "POST",
            "/v2/reviews",
            {"device_id": "device-a", "session_id": "session-a", "events": [self.event(1)]},
            self.headers,
        )
        status, body = self.call(
            "POST",
            "/v2/sync/status",
            {"device_id": "device-a", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["delivery"]["counts"]["received"], 1)
        self.assertEqual(body["application"]["counts"]["pending_apply"], 1)
        self.store.transition_event("device-a", "session-a", 1, "pending_apply")
        self.store.transition_event("device-a", "session-a", 1, "applied")
        status, body = self.call(
            "POST",
            "/v2/sync/status",
            {"device_id": "device-a", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["delivery"]["counts"]["applied"], 1)
        self.assertEqual(body["application"]["counts"]["applied"], 1)

    def test_method_unknown_and_deferred_capabilities_are_explicit(self) -> None:
        status, body = self.call("GET", "/v2/sync/status", {})
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        status, body = self.call("POST", "/v2/pull/open", {}, self.headers)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "capability_unavailable")
        status, body = self.call("POST", "/v2/no-such-route", {}, self.headers)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_route_response_budget_is_honored(self) -> None:
        small = DeviceRouter(
            self.auth,
            self.inbox,
            limits=RouteLimits(max_request_bytes=16_384, max_response_bytes=64),
        )
        response = small.dispatch(
            "POST",
            "/v2/handshake",
            self.headers,
            json.dumps(
                {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-c"},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        self.assertLessEqual(len(response.body), 64)
        self.assertIn(response.status, (200, 413, 503))

    def test_pair_http_requires_client_transaction_and_confirm_bearer(self):
        slot = self.auth.add_device("新阅读器")
        token = secrets.token_hex(32)
        pairing_id = str(uuid.uuid4())
        legacy = {"code": slot["code"], "device_id": "new-reader"}
        self.assertEqual(self.call("POST", "/v2/pair", legacy)[0], 400)
        request = dict(legacy, token=token, pairing_id=pairing_id)
        status, body = self.call("POST", "/v2/pair", request)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")
        self.assertTrue(body["capabilities"]["device_management_v1"])
        self.assertNotIn("token", body)
        self.assertNotIn("code", body)
        confirm = {"device_id": "new-reader", "pairing_id": pairing_id}
        self.assertEqual(self.call("POST", "/v2/pair/confirm", confirm)[0], 401)
        bearer = {"Authorization": "Bearer " + token}
        self.assertEqual(self.call("POST", "/v2/handshake", {"protocol": "x4-2", "device_id": "new-reader"}, bearer)[0], 401)
        status, active = self.call("POST", "/v2/pair/confirm", confirm, bearer)
        self.assertEqual(status, 200)
        self.assertEqual(active["status"], "active")
        self.assertEqual(self.call("POST", "/v2/pair/confirm", confirm, bearer)[1], active)
        self.assertEqual(self.call("POST", "/v2/pair", request)[1]["status"], "active")
        self.assertEqual(self.call("POST", "/v2/pair", dict(request, code="123456"))[1]["error"]["code"], "pairing_code_upgrade_required")

    def test_pair_storage_failures_keep_safe_types_and_off_stays_silent(self):
        import io
        from unittest.mock import patch
        from inkcards_anki_bridge.logging_control import AddonLogger
        from inkcards_anki_bridge.storage import BusyStorageError
        for level in ("debug", "off"):
            output = io.StringIO()
            logger = AddonLogger(self.store.root, policy={"level": level, "console": True}, console_stream=output)
            self.router.logger = logger
            request = {"code": "123456789012", "device_id": "private-reader", "pairing_id": str(uuid.uuid4()), "token": "c" * 64}
            with patch.object(self.store, "prepare_device_pairing", side_effect=BusyStorageError("PRIVATE-ERROR", "sqlite_busy_timeout")):
                status, body = self.call("POST", "/v2/pair", request)
            self.assertEqual((status, body["error"]["code"]), (503, "storage_busy"))
            log = output.getvalue()
            if level == "off":
                self.assertEqual(log, "")
            else:
                self.assertIn("BusyStorageError", log)
                self.assertIn("sqlite_busy_timeout", log)
            for private in ["123456789012", "PRIVATE-ERROR", "private-reader", "c" * 64]:
                self.assertNotIn(private, log)
            logger.close()


if __name__ == "__main__":
    unittest.main()
