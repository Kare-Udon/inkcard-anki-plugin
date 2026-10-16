from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from inkcards_anki_bridge.anki.models import DayTable, SnapshotManifest, TypedSnapshot
from inkcards_anki_bridge.anki.snapshot_jobs import (
    SnapshotJobError, SnapshotJobManager, SnapshotJobState, make_scope,
)
from inkcards_anki_bridge.runtime import AddonRuntime
from inkcards_anki_bridge.services.snapshots import SnapshotError


class FakeServer:
    def __init__(self, _router, *, host, port, limits=None):
        self.server_address = (host, 39123)

    def start(self):
        pass

    def stop(self, timeout=None):
        pass


class FakeJob:
    def __init__(self, scope_id):
        self.scope_id = scope_id
        self.state = SnapshotJobState.QUEUED
        self.result = None


class FakeManager(SnapshotJobManager):
    def __init__(self):
        self.calls = []
        self.job = None

    def request_snapshot(self, *, card_ids=None, deck_ids=None, applied_watermark=None):
        scope = make_scope(card_ids=card_ids, deck_ids=deck_ids)
        self.calls.append((card_ids, deck_ids))
        self.job = FakeJob(scope.scope_id)
        return self.job

    def stop(self):
        pass


def snapshot(day: int) -> TypedSnapshot:
    return TypedSnapshot(
        manifest=SnapshotManifest(
            schema="inkcards.typed-snapshot.v1", profile_identity="profile-a",
            profile_generation=0, runtime_epoch=None, anki_version="26.8.1",
            anki_build="39e4b0b4", scope_card_ids=(), scope_deck_ids=("20",),
            card_count=0, included_card_count=0,
        ),
        decks=(), presets=(), day_table=DayTable(review_day=day), cards=(),
    )


class PullRefreshRuntimeTests(unittest.TestCase):
    def test_full_snapshot_job_queue_returns_retryable_busy(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "inkcards_anki_bridge.runtime.DeviceServer", FakeServer
        ):
            runtime = AddonRuntime(profile_id="profile-a", root=Path(temp), port=0).start()
            try:
                runtime.store.register_device("device-a", "device-token-a")
                runtime.store.open_session("device-a", "session-a")
                scope_id = make_scope(card_ids=None, deck_ids=["20"]).scope_id
                runtime.snapshots.publish(
                    snapshot(42), scope_id=scope_id,
                    source_scope={"kind": "deck", "ids": ["20"]},
                )
                manager = FakeManager()
                runtime.attach_snapshot_jobs(manager)
                with patch.object(manager, "request_snapshot", side_effect=SnapshotJobError(
                    "job_capacity", "snapshot job queue is full", status=503
                )):
                    with self.assertRaises(SnapshotError) as raised:
                        runtime.refresh_pull(device_id="device-a", session_id="session-a", scope_id=scope_id)
                self.assertEqual(raised.exception.code, "snapshot_refresh_busy")
                self.assertEqual(raised.exception.status, 503)
                self.assertTrue(raised.exception.retryable)
            finally:
                runtime.stop()

    def test_pending_poll_ready_date_only_and_identity_binding(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "inkcards_anki_bridge.runtime.DeviceServer", FakeServer
        ):
            runtime = AddonRuntime(profile_id="profile-a", root=Path(temp), port=0).start()
            try:
                runtime.store.register_device("device-a", "device-token-a")
                runtime.store.open_session("device-a", "session-a")
                scope_id = make_scope(card_ids=None, deck_ids=["20"]).scope_id
                old = runtime.snapshots.publish(
                    snapshot(42), scope_id=scope_id,
                    source_scope={"kind": "deck", "ids": ["20"]},
                )
                manager = FakeManager()
                runtime.attach_snapshot_jobs(manager)
                status, pending = runtime.refresh_pull(
                    device_id="device-a", session_id="session-a", scope_id=scope_id,
                    base_snapshot_id=old.snapshot_id,
                )
                self.assertEqual(status, 202)
                self.assertEqual(manager.calls, [(None, ["20"])])
                self.assertEqual(pending["scope_id"], scope_id)
                token = pending["refresh_token"]
                with self.assertRaises(SnapshotError) as raised:
                    runtime.refresh_pull(device_id="device-a", session_id="session-a",
                                         scope_id=scope_id, base_snapshot_id=None,
                                         refresh_token=token)
                self.assertEqual(raised.exception.code, "snapshot_refresh_token_invalid")
                new = runtime.snapshots.publish(
                    snapshot(43), scope_id=scope_id,
                    source_scope={"kind": "deck", "ids": ["20"]},
                )
                manager.job.state = SnapshotJobState.PUBLISHED
                manager.job.result = new
                status, ready = runtime.refresh_pull(
                    device_id="device-a", session_id="session-a", scope_id=scope_id,
                    base_snapshot_id=old.snapshot_id, refresh_token=token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(ready["mode"], "date_only")
                self.assertEqual(ready["snapshot_id"], new.snapshot_id)
                self.assertEqual(ready["day_table"]["review_day"], 43)
                runtime.store.receive_batch("device-a", "session-a", [
                    {"event_id": "session-a:1", "sequence": 1,
                     "payload": {"rating": 3}},
                ])
                with self.assertRaises(SnapshotError) as raised:
                    runtime.refresh_pull(device_id="device-a", session_id="session-a",
                                         scope_id=scope_id, base_snapshot_id=old.snapshot_id,
                                         refresh_token=token)
                self.assertEqual(raised.exception.code, "pull_gate_pending_apply")
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
