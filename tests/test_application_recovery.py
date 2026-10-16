from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

from inkcards_anki_bridge.anki.application_260801 import (
    ApplicationResult,
    CapabilityReport,
    classify_recovery,
    stable_recovery_fingerprint,
)
from inkcards_anki_bridge.services.application import ApplicationService
from inkcards_anki_bridge.storage import ProfileStore
from inkcards_anki_bridge.domain.sync_gate import ImmutableReviewEvent


def _payload(card_id: str = "101", sequence: int = 1) -> dict:
    return {
        "card_id": card_id,
        "rating": 3,
        "answered_at_ms": 1_704_067_200_000 + sequence * 1_000,
        "duration_ms": 1_500,
        "scheduler_version": "x4-2",
        "fsrs_version": "FSRS-6",
        "config_revision": "cfg-1",
        "before": {
            "card_id": card_id,
            "schedule_revision": "r-%d" % sequence,
            "state": "review",
            "step": 0,
            "memory": {"stability": 4.0, "difficulty": 6.0},
            "interval_days": 5,
            "due_kind": "review_days",
            "due_value": 20,
            "due_collection_day": 30,
            "short_interval": False,
        },
        "after": {
            "card_id": card_id,
            "schedule_revision": "r-%d-next" % sequence,
            "state": "review",
            "step": 0,
            "memory": {"stability": 5.0, "difficulty": 5.8},
            "interval_days": 7,
            "due_kind": "review_days",
            "due_value": 27,
            "due_collection_day": 30,
            "short_interval": False,
        },
        "day_context": {"collection_day": 30, "timezone": "UTC"},
    }


def _event(session: str, sequence: int, card_id: str = "101") -> ImmutableReviewEvent:
    return ImmutableReviewEvent.create(
        "%s:%d" % (session, sequence), sequence, _payload(card_id, sequence)
    )


class FakeApplication:
    def __init__(self, *, enabled: bool = True, fail: Optional[str] = None) -> None:
        self.report = CapabilityReport(
            anki_version="26.08.1",
            anki_build="39e4b0b4",
            exact=True,
            write_enabled=enabled,
            disabled_reasons=() if enabled else ("historical_scheduler",),
        )
        self.fail = fail
        self.prepare_calls = 0
        self.apply_calls = 0
        self.order: List[str] = []

    def capability_report(self) -> CapabilityReport:
        return self.report

    def prepare_event(self, stored_event: object) -> object:
        self.prepare_calls += 1
        event_id = getattr(stored_event, "event_id")
        self.order.append(event_id)
        if self.fail == "conflict":
            from inkcards_anki_bridge.anki.application_260801 import ApplicationConflictError

            raise ApplicationConflictError("before_revision_mismatch", "before changed")
        return stored_event

    def apply_prepared(self, prepared: object) -> ApplicationResult:
        self.apply_calls += 1
        if self.fail == "unknown":
            raise RuntimeError("simulated_ack_loss")
        event_id = getattr(prepared, "event_id")
        return ApplicationResult(
            event_id=event_id,
            card_id="101",
            status="applied",
            before_fingerprint="before-%s" % event_id,
            after_fingerprint="after-%s" % event_id,
            stable_recovery_fingerprint="stable-%s" % event_id,
        )


class ApplicationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.store.register_device("device-a", "secret-a")
        self.store.open_session("device-a", "session-a")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _receive(self, *events: ImmutableReviewEvent) -> None:
        self.store.receive_batch("device-a", "session-a", list(events))

    def test_disabled_capability_makes_no_anki_mutation_and_keeps_pending(self) -> None:
        self._receive(_event("session-a", 1))
        fake = FakeApplication(enabled=False)
        result = ApplicationService(self.store, fake).apply_pending("device-a", "session-a")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(fake.prepare_calls, 0)
        self.assertEqual(fake.apply_calls, 0)
        self.assertEqual(self.store.query_applications("device-a", "session-a")[0].status, "pending_apply")

    def test_applied_events_are_not_applied_twice(self) -> None:
        self._receive(_event("session-a", 1))
        fake = FakeApplication()
        service = ApplicationService(self.store, fake)
        first = service.apply_pending("device-a", "session-a")
        second = service.apply_pending("device-a", "session-a")
        self.assertEqual(first.status, "applied")
        self.assertEqual(second.results, ())
        self.assertEqual(fake.apply_calls, 1)
        self.assertEqual(self.store.query_applications("device-a", "session-a")[0].status, "applied")

    def test_unknown_is_terminal_and_is_not_replayed(self) -> None:
        self._receive(_event("session-a", 1))
        fake = FakeApplication(fail="unknown")
        service = ApplicationService(self.store, fake)
        first = service.apply_pending("device-a", "session-a")
        second = service.apply_pending("device-a", "session-a")
        self.assertEqual(first.status, "unknown")
        self.assertEqual(second.results, ())
        self.assertEqual(fake.apply_calls, 1)
        self.assertEqual(self.store.query_applications("device-a", "session-a")[0].status, "unknown")

    def test_before_conflict_is_terminal_without_answer(self) -> None:
        self._receive(_event("session-a", 1))
        fake = FakeApplication(fail="conflict")
        result = ApplicationService(self.store, fake).apply_pending("device-a", "session-a")
        self.assertEqual(result.status, "conflict")
        self.assertEqual(fake.apply_calls, 0)
        self.assertEqual(self.store.query_applications("device-a", "session-a")[0].status, "conflict")

    def test_same_card_events_are_applied_in_sequence_order(self) -> None:
        self._receive(_event("session-a", 1), _event("session-a", 2))
        fake = FakeApplication()
        result = ApplicationService(self.store, fake).apply_pending("device-a", "session-a")
        self.assertEqual(result.status, "applied")
        self.assertEqual(fake.order, ["session-a:1", "session-a:2"])
        self.assertEqual(fake.apply_calls, 2)

    def test_restart_recovers_applying_to_unknown_without_replay(self) -> None:
        self._receive(_event("session-a", 1))
        intent = self.store.begin_apply("device-a", "session-a", [1])
        self.store.close()
        reopened = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        try:
            self.assertEqual(reopened.query_applications("device-a", "session-a")[0].status, "unknown")
            result = ApplicationService(reopened, FakeApplication()).recover("device-a", "session-a")
            self.assertEqual(result.results[0].status, "unknown")
            self.assertEqual(intent.status, "applying")
        finally:
            reopened.close()

    def test_recovery_fingerprint_can_ignore_clock_but_not_business_due(self) -> None:
        before = {"profile_principal": "p", "collection_identity": "c", "card_id": "101", "card": {"reps": 1, "due": 10}, "review_logs": []}
        after = {"profile_principal": "p", "collection_identity": "c", "card_id": "101", "card": {"reps": 2, "due": 20}, "review_logs": [{"time": 1, "button": 3}], "diagnostic": {"collection_mod": 1, "scheduler_current_state": {"elapsed_secs": 1}}}
        current = {"profile_principal": "p", "collection_identity": "c", "card_id": "101", "card": {"reps": 2, "due": 21}, "review_logs": [{"time": 1, "button": 3}], "diagnostic": {"collection_mod": 2, "scheduler_current_state": {"elapsed_secs": 2}}}
        intent = {
            "before_fingerprint": stable_recovery_fingerprint(before),
            "before_snapshot": before,
            "after_fingerprint": stable_recovery_fingerprint(after),
            "after_snapshot": after,
        }
        self.assertEqual(stable_recovery_fingerprint(after), stable_recovery_fingerprint(current))
        decision = classify_recovery(intent, current)
        self.assertEqual(decision.status, "conflict")
        self.assertFalse(decision.replay_allowed)


if __name__ == "__main__":
    unittest.main()
