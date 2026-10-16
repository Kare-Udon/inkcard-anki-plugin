from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inkcards_anki_bridge.domain.sync_gate import ImmutableReviewEvent
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.storage import ProfileStore


def event(session_id: str, sequence: int) -> ImmutableReviewEvent:
    return ImmutableReviewEvent.create(
        f"{session_id}:{sequence}", sequence, {"card_id": "card", "rating": 3}
    )


class InboxServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.store.register_device("device-a", "secret-a")
        self.store.open_session("device-a", "session-a")
        self.inbox = InboxService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_receive_returns_durable_receipt_and_query_domains_are_separate(
        self,
    ) -> None:
        batch = self.inbox.receive_batch(
            "device-a", "session-a", [event("session-a", 1)]
        )
        self.assertTrue(batch.durable)
        self.assertEqual(batch.receipts[0].status, "received")
        self.assertEqual(
            self.inbox.query_receipts("device-a", "session-a")[0].status, "received"
        )
        applications = self.inbox.query_applications("device-a", "session-a")
        self.assertEqual(applications[0].status, "pending_apply")
        self.assertNotIn("secret-a", repr(batch))

    def test_apply_intent_is_explicit_and_unknown_is_not_replayed(self) -> None:
        self.inbox.receive_batch("device-a", "session-a", [event("session-a", 1)])
        intent = self.inbox.begin_apply("device-a", "session-a", [1])
        self.assertTrue(intent.intent_id)
        self.assertTrue(intent.attempt_id)
        self.assertEqual(
            self.inbox.query_applications("device-a", "session-a")[0].status, "applying"
        )
        self.inbox.finish_apply(
            intent.intent_id, applied=False, reason="process_crashed"
        )
        self.assertEqual(
            self.inbox.query_applications("device-a", "session-a")[0].status, "unknown"
        )
        self.assertEqual(self.inbox.retryable("device-a", "session-a"), [])

    def test_successful_apply_changes_receipt_and_application_separately(self) -> None:
        self.inbox.receive_batch("device-a", "session-a", [event("session-a", 1)])
        intent = self.inbox.begin_apply("device-a", "session-a", [1])
        self.inbox.finish_apply(intent.intent_id, applied=True)
        self.assertEqual(
            self.inbox.query_receipts("device-a", "session-a")[0].status, "applied"
        )
        self.assertEqual(
            self.inbox.query_applications("device-a", "session-a")[0].status, "applied"
        )


if __name__ == "__main__":
    unittest.main()
