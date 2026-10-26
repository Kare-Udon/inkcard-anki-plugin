from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple

from inkcards_anki_bridge.anki.models import (
    CardSnapshot,
    DayTable,
    DueKind,
    SchedulerDue,
    SnapshotManifest,
    TypedSnapshot,
)
from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter, RouteLimits
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import ProfileStore


def make_snapshot(answer: str = "答案") -> TypedSnapshot:
    return TypedSnapshot(
        manifest=SnapshotManifest(
            schema="inkcards.typed-snapshot.v1",
            profile_identity="profile-a",
            profile_generation=0,
            runtime_epoch=None,
            anki_version="26.8.1",
            anki_build="39e4b0b4",
            scope_card_ids=("1",),
            scope_deck_ids=("20",),
            card_count=1,
            included_card_count=1,
        ),
        decks=(),
        presets=(),
        day_table=DayTable(review_day=42),
        cards=(
            CardSnapshot(
                card_id="1",
                note_id="10",
                deck_id="20",
                ordinal=0,
                card_type="review",
                queue=2,
                due=SchedulerDue(DueKind.REVIEW_DAY, 42, "day", 42),
                question="题面",
                answer=answer,
            ),
        ),
    )


class SnapshotRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: 1_000_000)
        self.inbox = InboxService(self.store)
        self.snapshot_now = [1_000_000]
        self.snapshots = SnapshotService(
            self.store, page_bytes=1400, clock=lambda: self.snapshot_now[0]
        )
        self.router = DeviceRouter(
            self.auth,
            self.inbox,
            snapshot_service=self.snapshots,
            limits=RouteLimits(max_request_bytes=16_384, max_response_bytes=16_384),
        )
        code = self.auth.issue_pairing_code()
        self.token = self.auth.pair(code, "device-a").token
        self.headers = {"Authorization": "Bearer " + self.token}
        status, _ = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.published = self.snapshots.publish(
            make_snapshot(), scope_id="deck:language", generation=0, applied_watermark=0
        )

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

    def test_handshake_pull_capability_and_three_routes(self) -> None:
        status, handshake = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(handshake["capabilities"]["pull"])
        self.assertFalse(handshake["capabilities"]["pull_refresh"])

        status, opened = self.call(
            "POST",
            "/v2/pull/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
            },
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(opened["snapshot_metadata"]),
            {
                "schema",
                "snapshot_schema",
                "manifest",
                "decks",
                "presets",
                "day_table",
                "complete",
                "unsupported",
                "profile_id",
                "scope_id",
                "snapshot_generation",
                "applied_watermark",
            },
        )
        self.assertNotIn("cards", opened["snapshot_metadata"])
        self.assertIn("snapshot_metadata_hash", opened)
        page_request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 0,
            "permit_id": opened["permit_id"],
            "nonce": opened["nonce"],
            "page_index": 0,
            "cursor": None,
        }
        status, page = self.call("POST", "/v2/pull/page", page_request, self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(page["page_index"], 0)
        self.assertIsNone(page["cursor"])
        self.assertEqual(len(page["cards"]), 1)
        status, committed = self.call(
            "POST",
            "/v2/pull/commit",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "permit_id": opened["permit_id"],
                "nonce": opened["nonce"],
                "snapshot_id": self.published.snapshot_id,
            },
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertTrue(committed["committed"])

    def test_refresh_route_auth_gate_and_pending_response(self) -> None:
        called = []
        def refresh(**kwargs):
            called.append(kwargs)
            return 202, {"protocol": "x4-2", "profile_id": "profile-a", "scope_id": "deck:language",
                         "refresh_token": "r_abc", "state": "pending"}
        router = DeviceRouter(self.auth, self.inbox, snapshot_service=self.snapshots,
                              refresh_handler=refresh)
        request = {"device_id": "device-a", "session_id": "session-a",
                   "scope_id": "deck:language"}
        status, body = self.call_with_router(router, "POST", "/v2/pull/refresh", request, self.headers)
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], "pending")
        self.assertEqual(called[0]["scope_id"], "deck:language")
        self.store.receive_batch("device-a", "session-a",
                                 [{"event_id": "session-a:1", "sequence": 1,
                                   "payload": {"rating": 3}}])
        status, body = self.call_with_router(router, "POST", "/v2/pull/refresh", request, self.headers)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "pull_gate_pending_apply")
        self.assertEqual(len(called), 1)

    def test_pull_open_budget_fails_before_issuing_permit(self) -> None:
        small_router = DeviceRouter(
            self.auth,
            self.inbox,
            snapshot_service=self.snapshots,
            limits=RouteLimits(max_request_bytes=16_384, max_response_bytes=256),
        )
        status, body = self.call_with_router(
            small_router,
            "POST",
            "/v2/pull/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
            },
            self.headers,
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "response_too_large")
        connection = self.store.connect()
        try:
            active = connection.execute(
                "SELECT COUNT(*) FROM permits WHERE profile_id=? AND status='issued'",
                (self.store.profile_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(active, 0)

    def test_lost_pull_open_response_recovers_same_permit_idempotently(self) -> None:
        request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
        lost = self.router.dispatch("POST", "/v2/pull/open", self.headers, raw)
        self.assertEqual(lost.status, 200)
        # 丢弃响应内容，只保留服务端已提交的记录来模拟响应丢失。
        connection = self.store.connect()
        try:
            rows = connection.execute(
                "SELECT permit_id,nonce_hash,expires_at_ms FROM permits "
                "WHERE profile_id=? AND device_id=? AND session_id=? AND scope_id=? "
                "AND local_generation=? AND status='issued'",
                ("profile-a", "device-a", "session-a", "deck:language", 1),
            ).fetchall()
            progress_count = connection.execute(
                "SELECT COUNT(*) FROM pull_progress WHERE profile_id=?",
                ("profile-a",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(progress_count, 1)
        original_id, original_nonce_hash, original_expiry = tuple(rows[0])

        status, recovered = self.call(
            "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(recovered["permit_id"], original_id)
        self.assertEqual(recovered["expires_at_ms"], original_expiry)
        self.assertNotIn(self.token, json.dumps(recovered))
        self.assertEqual(
            hashlib.sha256(recovered["nonce"].encode("utf-8")).hexdigest(),
            original_nonce_hash,
        )

        status, repeated = self.call(
            "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated["permit_id"], recovered["permit_id"])
        self.assertEqual(repeated["nonce"], recovered["nonce"])
        self.assertEqual(repeated["expires_at_ms"], recovered["expires_at_ms"])
        self.assertEqual(self.store.get_pull_progress(original_id).received_pages, 0)

        page_status, page = self.call(
            "POST",
            "/v2/pull/page",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 1,
                "permit_id": recovered["permit_id"],
                "nonce": recovered["nonce"],
                "page_index": 0,
                "cursor": None,
            },
            self.headers,
        )
        self.assertEqual(page_status, 200)
        self.assertEqual(page["page_index"], 0)
        status, after_page = self.call(
            "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(after_page["permit_id"], recovered["permit_id"])
        self.assertEqual(after_page["nonce"], recovered["nonce"])
        self.assertEqual(after_page["next_page_index"], 1)
        self.assertEqual(after_page["received_pages"], 1)
        self.assertTrue(after_page["complete"])

        connection = self.store.connect()
        try:
            permit_count = connection.execute(
                "SELECT COUNT(*) FROM permits WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND scope_id=? AND local_generation=?",
                ("profile-a", "device-a", "session-a", "deck:language", 1),
            ).fetchone()[0]
            final_progress_count = connection.execute(
                "SELECT COUNT(*) FROM pull_progress WHERE profile_id=?",
                ("profile-a",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(permit_count, 1)
        self.assertEqual(final_progress_count, 1)
        self.assertEqual(self.store.get_pull_progress(original_id).received_pages, 1)

    def test_open_retry_recovers_preexisting_random_nonce_permit(self) -> None:
        request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        old = self.snapshots.open_pull(**request)
        old_nonce_hash = self.store.get_permit(old["permit_id"]).nonce_hash

        status, recovered = self.call(
            "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(recovered["permit_id"], old["permit_id"])
        self.assertEqual(recovered["expires_at_ms"], old["expires_at_ms"])
        self.assertNotEqual(
            hashlib.sha256(recovered["nonce"].encode("utf-8")).hexdigest(),
            old_nonce_hash,
        )

        status, repeated = self.call(
            "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated["permit_id"], recovered["permit_id"])
        self.assertEqual(repeated["nonce"], recovered["nonce"])
        rejected, _ = self.call(
            "POST",
            "/v2/pull/page",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 1,
                "permit_id": old["permit_id"],
                "nonce": old["nonce"],
                "page_index": 0,
                "cursor": None,
            },
            self.headers,
        )
        self.assertEqual(rejected, 422)
        accepted, _ = self.call(
            "POST",
            "/v2/pull/page",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 1,
                "permit_id": recovered["permit_id"],
                "nonce": recovered["nonce"],
                "page_index": 0,
                "cursor": None,
            },
            self.headers,
        )
        self.assertEqual(accepted, 200)

    def test_parallel_duplicate_open_requests_share_one_permit_and_nonce(self) -> None:
        request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        original_find = self.store.find_recoverable_permit
        first_reads = 0
        reads_lock = threading.Lock()
        both_reads = threading.Barrier(2)

        def synchronized_find(**kwargs):
            nonlocal first_reads
            result = original_find(**kwargs)
            should_wait = False
            with reads_lock:
                if result is None and first_reads < 2:
                    first_reads += 1
                    should_wait = True
            if should_wait:
                both_reads.wait(timeout=5)
            return result

        self.store.find_recoverable_permit = synchronized_find
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: self.call(
                            "POST", "/v2/pull/open", request, self.headers
                        ),
                        range(2),
                    )
                )
        finally:
            self.store.find_recoverable_permit = original_find

        self.assertEqual([status for status, _ in results], [200, 200])
        opened = [body for _, body in results]
        self.assertEqual(opened[0]["permit_id"], opened[1]["permit_id"])
        self.assertEqual(opened[0]["nonce"], opened[1]["nonce"])
        connection = self.store.connect()
        try:
            permit_count = connection.execute(
                "SELECT COUNT(*) FROM permits WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND scope_id=? AND local_generation=? "
                "AND status='issued'",
                ("profile-a", "device-a", "session-a", "deck:language", 1),
            ).fetchone()[0]
            progress_count = connection.execute(
                "SELECT COUNT(*) FROM pull_progress WHERE profile_id=?",
                ("profile-a",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(permit_count, 1)
        self.assertEqual(progress_count, 1)

    def test_recovery_is_bound_to_device_session_scope_and_local_generation(self) -> None:
        original_request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        status, original = self.call(
            "POST", "/v2/pull/open", original_request, self.headers
        )
        self.assertEqual(status, 200)

        pairing_code = self.auth.issue_pairing_code()
        credential_b = self.auth.pair(pairing_code, "device-b")
        headers_b = {"Authorization": "Bearer " + credential_b.token}
        status, _ = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-b", "session_id": "session-a"},
            headers_b,
        )
        self.assertEqual(status, 200)
        status, device_b = self.call(
            "POST",
            "/v2/pull/open",
            dict(original_request, device_id="device-b"),
            headers_b,
        )
        self.assertEqual(status, 200)
        self.assertEqual(device_b["device_id"], "device-b")
        self.assertNotEqual(device_b["permit_id"], original["permit_id"])
        self.assertNotEqual(device_b["nonce"], original["nonce"])

        status, _ = self.call(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-b"},
            self.headers,
        )
        self.assertEqual(status, 200)
        status, session_b = self.call(
            "POST",
            "/v2/pull/open",
            dict(original_request, session_id="session-b"),
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(session_b["permit_id"], original["permit_id"])

        status, local_generation_two = self.call(
            "POST",
            "/v2/pull/open",
            dict(original_request, local_generation=2),
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(
            local_generation_two["permit_id"], original["permit_id"]
        )

        self.snapshots.publish(
            make_snapshot(), scope_id="deck:other", generation=0, applied_watermark=0
        )
        status, scope_other = self.call(
            "POST",
            "/v2/pull/open",
            dict(original_request, scope_id="deck:other"),
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(scope_other["permit_id"], original["permit_id"])

        page_status, _ = self.call(
            "POST",
            "/v2/pull/page",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 1,
                "permit_id": original["permit_id"],
                "nonce": original["nonce"],
                "page_index": 0,
                "cursor": None,
            },
            self.headers,
        )
        self.assertEqual(page_status, 200)

    def test_recovery_uses_latest_snapshot_generation_and_does_not_revive_old_permit(self) -> None:
        request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        status, old = self.call("POST", "/v2/pull/open", request, self.headers)
        self.assertEqual(status, 200)
        self.snapshots.publish(
            make_snapshot(answer="更新后的答案"),
            scope_id="deck:language",
            generation=1,
            applied_watermark=0,
        )

        status, current = self.call("POST", "/v2/pull/open", request, self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(current["snapshot_generation"], 1)
        self.assertNotEqual(current["permit_id"], old["permit_id"])
        self.assertEqual(self.store.get_permit(old["permit_id"]).status, "revoked")

    def test_expired_permit_is_not_recovered_or_extended(self) -> None:
        request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        status, old = self.call("POST", "/v2/pull/open", request, self.headers)
        self.assertEqual(status, 200)
        self.snapshot_now[0] = old["expires_at_ms"]

        status, current = self.call("POST", "/v2/pull/open", request, self.headers)
        self.assertEqual(status, 200)
        self.assertNotEqual(current["permit_id"], old["permit_id"])
        self.assertGreater(current["expires_at_ms"], old["expires_at_ms"])
        self.assertEqual(self.store.get_permit(old["permit_id"]).status, "expired")

    def test_recovery_response_budget_fails_before_nonce_rebind(self) -> None:
        request = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "deck:language",
            "local_generation": 1,
        }
        status, opened = self.call(
            "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 200)
        old_nonce_hash = self.store.get_permit(opened["permit_id"]).nonce_hash
        small_router = DeviceRouter(
            self.auth,
            self.inbox,
            snapshot_service=self.snapshots,
            limits=RouteLimits(max_request_bytes=16_384, max_response_bytes=256),
        )

        status, body = self.call_with_router(
            small_router, "POST", "/v2/pull/open", request, self.headers
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "response_too_large")
        self.assertEqual(
            self.store.get_permit(opened["permit_id"]).nonce_hash, old_nonce_hash
        )
        page_status, _ = self.call(
            "POST",
            "/v2/pull/page",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 1,
                "permit_id": opened["permit_id"],
                "nonce": opened["nonce"],
                "page_index": 0,
                "cursor": None,
            },
            self.headers,
        )
        self.assertEqual(page_status, 200)

    def test_gate_rejects_p3_pending_and_nonce_body_does_not_leak(self) -> None:
        self.store.receive_batch(
            "device-a",
            "session-a",
            [{"event_id": "session-a:1", "sequence": 1, "payload": {"rating": 3}}],
        )
        status, body = self.call(
            "POST",
            "/v2/pull/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
            },
            self.headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "pull_gate_pending_apply")
        self.assertNotIn(self.token, json.dumps(body))

    def test_strict_allowlist_and_old_router_compatibility(self) -> None:
        status, body = self.call(
            "POST",
            "/v2/pull/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
                "unexpected": True,
            },
            self.headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unknown_field")

        old_router = DeviceRouter(self.auth, self.inbox)
        status, body = self.call_with_router(
            old_router, "POST", "/v2/pull/open", {}, self.headers
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "capability_unavailable")

    def call_with_router(
        self,
        router: DeviceRouter,
        method: str,
        path: str,
        payload: Any,
        headers: Dict[str, str] | None = None,
    ) -> Tuple[int, Dict[str, Any]]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = router.dispatch(method, path, headers or {}, raw)
        return response.status, json.loads(response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
