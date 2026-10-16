from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter, RouteLimits
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import CommitUnknownError, ProfileStore


class CompactRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = ProfileStore(self.root, profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: 1_000_000)
        self.router = DeviceRouter(self.auth, InboxService(self.store))
        code = self.auth.issue_pairing_code()
        _, pair = self.call("/v2/pair", {"code": code, "device_id": "device-a"})
        self.headers = {"Authorization": "Bearer " + pair["token"]}
        self.call("/v2/handshake", {
            "protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"
        })
        self.snapshot_id = SnapshotService(self.store).publish(
            {
                "snapshot_schema": "test.v1", "manifest": {}, "decks": [],
                "presets": [], "day_table": {}, "complete": True, "unsupported": [],
                "cards": [{"card_id": "card-one"}, {"card_id": "card-two"}],
            }, scope_id="all", generation=0, applied_watermark=0,
        ).snapshot_id

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def call(self, path: str, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        response = self.router.dispatch("POST", path, getattr(self, "headers", {}), raw)
        return response.status, json.loads(response.body)

    def batch(self, sequence: int, rows: list[list[int]]) -> dict:
        return {
            "protocol": "x4-3", "device_id": "device-a", "session_id": "session-a",
            "snapshot_id": self.snapshot_id, "base_sequence": sequence, "events": rows,
        }

    def test_durable_receipt_is_idempotent_and_not_an_anki_application(self) -> None:
        payload = self.batch(1, [[2, 3, 1_700_000_000_000, 1200]])
        first_status, first = self.call("/v3/reviews/compact", payload)
        second_status, second = self.call("/v3/reviews/compact", payload)
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first["receipts"], second["receipts"])
        self.assertEqual(first["ack_semantics"], "durably_received_not_applied")
        self.assertEqual(self.store.count_events(), 0)
        self.assertEqual(self.store.query_applications("device-a", "session-a"), [])
        with self.store.connect() as db:
            row = db.execute("SELECT card_id,card_index FROM compact_events").fetchone()
            self.assertEqual((row["card_id"], row["card_index"]), ("card-two", 2))
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason,
                         "pull_gate_compact_not_applied")

    def test_durable_ack_queues_host_job_and_exposes_only_cached_job_fields(self) -> None:
        scheduled: list[tuple[str, str]] = []
        latest: dict[str, object] = {}

        def request_job(device_id: str, session_id: str) -> dict[str, object]:
            scheduled.append((device_id, session_id))
            latest.update({
                "job_id": "compact-1",
                "state": "queued",
                "profile_identity": "private-profile-value",
                "runtime_epoch": "private-epoch-value",
            })
            return latest

        self.router = DeviceRouter(
            self.auth,
            InboxService(self.store),
            compact_job_requester=request_job,
            compact_job_status_reader=lambda _device, _session: latest or None,
        )
        status, ack = self.call("/v3/reviews/compact", self.batch(1, [[1, 3, 1000, 20]]))

        self.assertEqual(status, 200)
        self.assertTrue(ack["durable"])
        self.assertEqual(ack["ack_semantics"], "durably_received_not_applied")
        self.assertEqual(ack["apply_job"]["state"], "queued")
        self.assertNotIn("profile_identity", ack["apply_job"])
        self.assertNotIn("runtime_epoch", ack["apply_job"])
        self.assertEqual(scheduled, [("device-a", "session-a")])
        self.assertEqual(len(self.store.query_compact_receipts("device-a", "session-a")), 1)

        status, state = self.call("/v2/sync/status", {
            "device_id": "device-a", "session_id": "session-a",
        })
        self.assertEqual(status, 200)
        self.assertEqual(state["compact_job"]["job_id"], "compact-1")
        self.assertNotIn("profile_identity", state["compact_job"])

    def test_status_requeues_persisted_pending_work_only_when_no_job_is_cached(self) -> None:
        self.call("/v3/reviews/compact", self.batch(1, [[1, 3, 1000, 20]]))
        scheduled: list[tuple[str, str]] = []
        latest: dict[str, object] = {}

        def request_job(device_id: str, session_id: str) -> dict[str, object]:
            scheduled.append((device_id, session_id))
            latest.update({"job_id": "compact-recovered", "state": "queued"})
            return latest

        self.router = DeviceRouter(
            self.auth,
            InboxService(self.store),
            compact_job_requester=request_job,
            compact_job_status_reader=lambda _device, _session: latest or None,
        )
        _, first = self.call("/v2/sync/status", {
            "device_id": "device-a", "session_id": "session-a",
        })
        _, second = self.call("/v2/sync/status", {
            "device_id": "device-a", "session_id": "session-a",
        })

        self.assertEqual(first["compact_job"]["job_id"], "compact-recovered")
        self.assertEqual(second["compact_job"]["job_id"], "compact-recovered")
        self.assertEqual(scheduled, [("device-a", "session-a")])

    def test_job_queue_failure_does_not_retract_durable_ack(self) -> None:
        class QueueFull(Exception):
            code = "job_capacity"

        self.router = DeviceRouter(
            self.auth,
            InboxService(self.store),
            compact_job_requester=lambda _device, _session: (_ for _ in ()).throw(QueueFull()),
        )
        status, ack = self.call("/v3/reviews/compact", self.batch(1, [[1, 3, 1000, 20]]))

        self.assertEqual(status, 200)
        self.assertTrue(ack["durable"])
        self.assertEqual(ack["apply_job"], {"state": "not_scheduled", "reason": "job_capacity"})
        self.assertEqual(len(self.store.query_compact_receipts("device-a", "session-a")), 1)

    def test_lost_ack_can_be_queried_after_store_reopen(self) -> None:
        self.call("/v3/reviews/compact", self.batch(1, [[1, 2, 1_700_000_000_000, 20]]))
        reopened = ProfileStore(self.root, profile_id="profile-a")
        try:
            receipts = reopened.query_compact_receipts("device-a", "session-a")
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].status, "received")
        finally:
            reopened.close()
        status, body = self.call("/v3/receipts/compact", {
            "protocol": "x4-3", "device_id": "device-a", "session_id": "session-a",
            "after_sequence": 0,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["application"], "not_applied")
        self.assertEqual(body["receipts"][0]["sequence"], 1)

    def test_receipt_query_is_paged_by_sequence(self) -> None:
        self.call("/v3/reviews/compact", self.batch(1, [
            [1, 2, 1000, 20], [2, 3, 2000, 30],
        ]))
        paged = DeviceRouter(self.auth, InboxService(self.store), limits=RouteLimits(max_event_ids=1))
        original = self.router
        self.router = paged
        try:
            status, first = self.call("/v3/receipts/compact", {
                "protocol": "x4-3", "device_id": "device-a", "session_id": "session-a",
                "after_sequence": 0,
            })
            self.assertEqual(status, 200)
            self.assertTrue(first["has_more"])
            self.assertEqual([r["sequence"] for r in first["receipts"]], [1])
            _, second = self.call("/v3/receipts/compact", {
                "protocol": "x4-3", "device_id": "device-a", "session_id": "session-a",
                "after_sequence": 1,
            })
            self.assertFalse(second["has_more"])
            self.assertEqual([r["sequence"] for r in second["receipts"]], [2])
        finally:
            self.router = original

    def test_conflict_commits_evidence_without_partially_acking_new_rows(self) -> None:
        self.call("/v3/reviews/compact", self.batch(1, [[1, 3, 1000, 20]]))
        status, body = self.call("/v3/reviews/compact", self.batch(1, [
            [1, 4, 1000, 20], [2, 3, 2000, 30],
        ]))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "receipt_conflict")
        self.assertNotIn("receipts", body)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM compact_events").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM compact_conflicts").fetchone()[0], 1)
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason,
                         "pull_gate_compact_conflict")

    def test_index_and_sequence_are_validated_before_ack(self) -> None:
        status, body = self.call("/v3/reviews/compact", self.batch(1, [[3, 3, 1000, 20]]))
        self.assertEqual((status, body["error"]["code"]), (422, "card_index_invalid"))
        status, body = self.call("/v3/reviews/compact", self.batch(2, [[1, 3, 1000, 20]]))
        self.assertEqual((status, body["error"]["code"]), (422, "sequence_gap"))
        self.assertEqual(self.store.query_compact_receipts("device-a", "session-a"), [])

    def test_new_session_cannot_bypass_old_compact_delivery(self) -> None:
        self.call("/v3/reviews/compact", self.batch(1, [[1, 3, 1000, 20]]))
        self.store.open_session("device-a", "session-b")
        self.assertEqual(self.store.pull_gate("device-a", "session-b").reason,
                         "pull_gate_compact_not_applied")

    def test_compact_status_shows_recovery_and_received_is_not_applied_watermark(self) -> None:
        status, received = self.call(
            "/v3/reviews/compact", self.batch(1, [[1, 3, 1000, 20]])
        )
        self.assertEqual(status, 200)
        self.assertEqual(received["receipts"][0]["status"], "received")

        status, pending = self.call("/v2/sync/status", {
            "device_id": "device-a", "session_id": "session-a",
        })
        self.assertEqual(status, 200)
        self.assertEqual(pending["phase"], "pending_apply")
        self.assertEqual(pending["compact_delivery"]["counts"]["received"], 1)
        self.assertEqual(pending["compact_delivery"]["ack_semantics"], "received_not_applied")
        self.assertEqual(pending["compact_application"]["counts"]["not_applied"], 1)
        self.assertEqual(pending["compact_application"]["applied_watermark"], "unknown")
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason,
                         "pull_gate_compact_not_applied")

        self.store.begin_compact_apply(
            "device-a",
            "session-a",
            1,
            collection_identity="collection-a",
            collection_generation=self.store.get_collection_generation(),
            policy_json={"policy_id": "policy-a"},
            before_json={"card_id": "card-one", "revision": 0},
            plan_json={"required_evidence": ["score_applied"]},
            expected_after_json={"card_id": "card-one", "revision": 1},
            intent_id="intent-a",
            attempt_id="attempt-a",
        )

        self.store.close()
        self.store = ProfileStore(self.root, profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: 1_000_000)
        self.router = DeviceRouter(self.auth, InboxService(self.store))
        status, recovering = self.call("/v2/sync/status", {
            "device_id": "device-a", "session_id": "session-a",
        })
        self.assertEqual(status, 200)
        self.assertEqual(recovering["phase"], "recover_application")
        self.assertEqual(recovering["compact_application"]["recovery_required"], 1)
        self.assertEqual(recovering["compact_application"]["applied_watermark"], "unknown")
        self.assertEqual(recovering["last_error"], "recovery_classification_required")
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason,
                         "pull_gate_compact_applying")

    def test_failed_commit_returns_no_durable_ack(self) -> None:
        original_commit = self.store._commit
        try:
            self.store._commit = lambda db: (_ for _ in ()).throw(RuntimeError("fault"))
            with self.assertRaises(CommitUnknownError):
                self.store.receive_compact_batch(
                    "device-a", "session-a", self.snapshot_id, 1, [[1, 3, 1000, 20]]
                )
        finally:
            self.store._commit = original_commit
        self.assertEqual(self.store.query_compact_receipts("device-a", "session-a"), [])


if __name__ == "__main__":
    unittest.main()
