from __future__ import annotations

import threading
import gc
import weakref
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.anki.compatibility import SUPPORTED_ANKI_BUILD
from inkcards_anki_bridge.anki.models import TypedSnapshot
from inkcards_anki_bridge.anki.operations import OperationContext, QueryOpAdapter
from inkcards_anki_bridge.anki.snapshot_jobs import (
    SnapshotJobError,
    SnapshotJobManager,
    SnapshotJobState,
)


class FakeCard:
    def __init__(self, card_id: int, deck_id: int = 10) -> None:
        self.id = card_id
        self.nid = card_id + 1000
        self.did = deck_id
        self.ord = 0
        self.type = 0
        self.queue = 0
        self.due = card_id
        self.ivl = 0
        self.factor = 0
        self.reps = 0
        self.lapses = 0
        self.left = 0
        self.last_review_time = 0
        self.mod = card_id
        self.memory_state = None

    def question(self) -> str:
        return f"题面 {self.id}"

    def answer(self) -> str:
        return f"答案 {self.id}"


class FakeDecks:
    def __init__(self) -> None:
        self.records = {
            10: {
                "id": 10,
                "name": "语言",
                "preset_id": 100,
                "config": {
                    "id": 100,
                    "name": "语言预设",
                    "fsrs": {"params": [0.1], "desiredRetention": 0.9},
                    "new": {"perDay": 20, "delays": [1]},
                    "rev": {"perDay": 20},
                    "learn": {"perDay": 20},
                    "lapse": {"delays": [1]},
                },
                "new_today": 0,
                "review_today": 0,
                "learning_today": 0,
            },
            20: {
                "id": 20,
                "name": "数学",
                "preset_id": 200,
                "config": {
                    "id": 200,
                    "name": "数学预设",
                    "fsrs": {"params": [0.2], "desiredRetention": 0.91},
                    "new": {"perDay": 20, "delays": [1]},
                    "rev": {"perDay": 20},
                    "learn": {"perDay": 20},
                    "lapse": {"delays": [1]},
                },
                "new_today": 0,
                "review_today": 0,
                "learning_today": 0,
            },
        }

    def name(self, deck_id: int, default: bool = False) -> str | None:
        record = self.records.get(int(deck_id))
        return record["name"] if record is not None else None

    def get(self, deck_id: int, default: bool = True):
        record = self.records.get(int(deck_id))
        if record is None and default:
            return self.records[10]
        return record

    def config_dict_for_deck_id(self, deck_id: int):
        return self.records[int(deck_id)]["config"]


class FakeCollection:
    def __init__(self, cards: list[FakeCard]) -> None:
        self.cards = {card.id: card for card in cards}
        self.decks = FakeDecks()
        self.sched = SimpleNamespace(today=42, day_cutoff=1_700_000_000)
        self.search_calls: list[tuple[str, bool]] = []
        self.read_count = 0
        self.entered_first_read = threading.Event()
        self.release_first_read = threading.Event()
        self.block_first_read = False

    def find_cards(self, query: str, order: bool = False):
        self.search_calls.append((query, order))
        if 'deck:"语言"' in query:
            return [2, 1]
        if 'deck:"数学"' in query:
            return [3, 2]
        return []

    def get_card(self, card_id: int) -> FakeCard:
        self.read_count += 1
        if self.read_count == 1:
            self.entered_first_read.set()
            if self.block_first_read:
                self.release_first_read.wait(timeout=2.0)
        return self.cards[int(card_id)]


class FakeHost:
    def __init__(self) -> None:
        self.pending = []

    def submit_query(self, operation):
        self.pending.append(operation)


class ContextSource:
    def __init__(self) -> None:
        self.current = OperationContext("profile-a", 7, "epoch-a")

    def __call__(self):
        return self.current


class SnapshotJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ContextSource()
        self.host = FakeHost()
        self.collection = FakeCollection([FakeCard(1), FakeCard(2), FakeCard(3, 20)])
        self.published = []
        self.provider_called = False

        def forbidden_collection_provider():
            self.provider_called = True
            raise AssertionError("collection provider must not be used by QueryOp job")

        self.exporter = Anki260801Adapter(
            collection_provider=forbidden_collection_provider,
            profile_provider=self.source,
            profile_identity="profile-a",
            anki_version="26.08.1",
            anki_build=SUPPORTED_ANKI_BUILD,
        )
        self.manager = SnapshotJobManager(
            operation_adapter=QueryOpAdapter(self.host, self.source),
            exporter=self.exporter,
            context_provider=self.source,
            publish=self.publish,
            export_batch_size=2,
            max_card_ids=8,
        )
        self.source_scopes = []
        self.watermarks = []

    def publish(
        self,
        snapshot: TypedSnapshot,
        *,
        scope_id: str,
        generation: int,
        expected_context=None,
        source_scope=None,
        applied_watermark=None,
    ):
        self.assertEqual(expected_context, self.source.current)
        self.source_scopes.append(source_scope)
        self.watermarks.append(applied_watermark)
        self.published.append((snapshot, scope_id, generation))
        return {"scope_id": scope_id, "generation": generation}

    def run_pending(self, collection: FakeCollection | None = None) -> None:
        operation = self.host.pending.pop(0)
        operation(collection or self.collection)

    def test_completed_snapshot_does_not_retain_export_payload(self):
        job = self.manager.request_snapshot(card_ids=["1"])
        self.run_pending()
        payload = weakref.ref(self.published[0][0])
        self.published.clear()
        gc.collect()
        self.assertIsNone(payload())
        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        self.assertIsNotNone(job.result)

    def test_failed_publication_does_not_retain_snapshot_through_exception_chain(self):
        payloads = []

        def publish(snapshot, **_kwargs):
            payloads.append(weakref.ref(snapshot))
            try:
                raise RuntimeError("publication cause")
            except RuntimeError as error:
                raise SnapshotJobError("publication_failed", "发布失败", status=503) from error

        self.manager._publish = publish
        job = self.manager.request_snapshot(card_ids=["1"])
        self.run_pending()
        gc.collect()
        self.assertEqual(job.state, SnapshotJobState.FAILED)
        self.assertEqual(job.error.code, "publication_failed")
        self.assertEqual(job.error.status, 503)
        self.assertIsNone(job.error.__traceback__)
        self.assertIsNone(job.error.__cause__)
        self.assertIsNone(job.error.__context__)
        self.assertIsNone(payloads[0]())

    def test_context_provider_failure_on_request_is_classified(self):
        with patch.object(self.manager, "_context_provider", side_effect=RuntimeError("profile closed")):
            with self.assertRaises(SnapshotJobError) as raised:
                self.manager.request_snapshot(card_ids=["1"])
        self.assertEqual(raised.exception.code, "context_unavailable")
        self.assertEqual(self.host.pending, [])

    def test_context_provider_failure_at_completion_releases_scope(self):
        job = self.manager.request_snapshot(card_ids=["1"])
        original = self.manager._on_operation_result

        def complete(job, result):
            with patch.object(self.manager, "_context_provider", side_effect=RuntimeError("profile closed")):
                original(job, result)

        with patch.object(self.manager, "_on_operation_result", side_effect=complete):
            self.run_pending()
        self.assertEqual(job.state, SnapshotJobState.STALE_RESULT_DROPPED)
        self.assertIsNone(job.operation_task)
        self.assertEqual(self.published, [])
        self.manager.request_snapshot(card_ids=["1"]).cancel()

    def test_job_capacity_and_history_are_bounded_without_dropping_active_jobs(self):
        self.manager = SnapshotJobManager(
            operation_adapter=QueryOpAdapter(self.host, self.source),
            exporter=self.exporter,
            context_provider=self.source,
            publish=self.publish,
            max_active_jobs=2,
            max_retained_jobs=3,
        )
        active = self.manager.request_snapshot(card_ids=["1"])
        second = self.manager.request_snapshot(card_ids=["2"])
        with self.assertRaises(SnapshotJobError) as raised:
            self.manager.request_snapshot(card_ids=["3"])
        self.assertEqual(raised.exception.code, "job_capacity")
        self.assertEqual(len(self.host.pending), 2)
        second.cancel()
        oldest = second
        for _ in range(5):
            latest = self.manager.request_snapshot(card_ids=["2"])
            latest.cancel()
        self.assertIs(self.manager.get(active.job_id), active)
        self.assertIsNone(self.manager.get(oldest.job_id))
        self.assertIs(self.manager.get(latest.job_id), latest)
        self.assertEqual(oldest.state, SnapshotJobState.CANCELED)

    def test_refresh_watermark_is_bound_to_its_job_publication(self) -> None:
        for value in (6, 2, 0):
            job = self.manager.request_snapshot(card_ids=["1"], applied_watermark=value)
            self.run_pending()
            self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
            self.assertEqual(self.watermarks[-1], value)
        for value in (-1, True, "6", 9007199254740992):
            with self.assertRaises(SnapshotJobError):
                self.manager.request_snapshot(card_ids=["1"], applied_watermark=value)

    def test_success_publishes_only_after_queryop_reads_collection(self) -> None:
        job = self.manager.request_snapshot(card_ids=["2", "1", "2"])
        self.assertEqual(job.state, SnapshotJobState.QUEUED)
        self.assertEqual(self.collection.read_count, 0)
        self.assertFalse(self.provider_called)

        self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.source_scopes, [{"kind": "card", "ids": ["1", "2"]}])
        snapshot, scope_id, generation = self.published[0]
        self.assertEqual([card.card_id for card in snapshot.cards], ["1", "2"])
        self.assertEqual(scope_id, job.scope_id)
        self.assertEqual(generation, 7)
        self.assertFalse(self.provider_called)

    def test_deck_scope_uses_fixed_find_cards_and_is_sorted_and_deduplicated(self) -> None:
        job = self.manager.request_snapshot(deck_ids=["20", "10", "20"])
        self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        self.assertEqual(
            self.collection.search_calls,
            [('deck:"语言"', False), ('deck:"数学"', False)],
        )
        snapshot = self.published[0][0]
        self.assertEqual(snapshot.manifest.scope_deck_ids, ("10", "20"))
        self.assertEqual([card.card_id for card in snapshot.cards], ["1", "2", "3"])

    def test_same_scope_is_rejected_until_previous_job_finishes(self) -> None:
        self.manager.request_snapshot(card_ids=["1", "2"])
        with self.assertRaises(SnapshotJobError) as raised:
            self.manager.request_snapshot(card_ids=["2", "1"])
        self.assertEqual(raised.exception.code, "duplicate_scope")
        self.run_pending()
        second = self.manager.request_snapshot(card_ids=["2", "1"])
        self.assertNotEqual(second.job_id, "snapshot-1")

    def test_cancel_before_start_never_reads_or_publishes(self) -> None:
        job = self.manager.request_snapshot(card_ids=["1"])
        self.assertTrue(job.cancel())
        self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.CANCELED)
        self.assertEqual(self.collection.read_count, 0)
        self.assertEqual(self.published, [])

    def test_cancel_after_start_discards_result_without_partial_publish(self) -> None:
        self.collection.block_first_read = True
        job = self.manager.request_snapshot(card_ids=["1", "2"])
        worker = threading.Thread(target=self.run_pending)
        worker.start()
        self.assertTrue(self.collection.entered_first_read.wait(timeout=1.0))
        self.assertTrue(job.cancel())
        self.collection.release_first_read.set()
        worker.join(timeout=2.0)

        self.assertEqual(job.state, SnapshotJobState.CANCELED)
        self.assertEqual(self.published, [])

    def test_cancel_after_publication_does_not_retract_snapshot(self) -> None:
        job = self.manager.request_snapshot(card_ids=["1"])
        self.run_pending()
        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        self.assertFalse(job.cancel())
        self.assertEqual(len(self.published), 1)

    def test_generation_change_drops_late_result(self) -> None:
        job = self.manager.request_snapshot(card_ids=["1"])

        def change_generation(card_id: int):
            self.source.current = OperationContext("profile-a", 8, "epoch-a")
            return self.collection.cards[int(card_id)]

        self.collection.get_card = change_generation
        self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.STALE_RESULT_DROPPED)
        self.assertEqual(self.published, [])

    def test_profile_or_epoch_change_drops_late_result(self) -> None:
        job = self.manager.request_snapshot(card_ids=["1"])

        def change_identity(card_id: int):
            self.source.current = OperationContext("profile-b", 7, "epoch-b")
            return self.collection.cards[int(card_id)]

        self.collection.get_card = change_identity
        self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.STALE_RESULT_DROPPED)
        self.assertEqual(self.published, [])

    def test_empty_or_ambiguous_scope_fails_closed_before_queryop(self) -> None:
        with self.assertRaises(SnapshotJobError) as empty:
            self.manager.request_snapshot(card_ids=[])
        self.assertEqual(empty.exception.code, "scope_required")
        with self.assertRaises(SnapshotJobError) as ambiguous:
            self.manager.request_snapshot(card_ids=["1"], deck_ids=["10"])
        self.assertEqual(ambiguous.exception.code, "scope_invalid")
        self.assertEqual(self.host.pending, [])

    def test_export_failure_keeps_previous_publication(self) -> None:
        old = {"scope_id": "old", "generation": 6}
        self.published.append(("old-snapshot", "old", 6))
        job = self.manager.request_snapshot(card_ids=["1"])
        with patch.object(
            self.exporter,
            "export_snapshot_from_collection",
            side_effect=RuntimeError("export failed"),
        ):
            self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.FAILED)
        self.assertEqual(self.published, [("old-snapshot", "old", 6)])
        self.assertEqual(old, {"scope_id": "old", "generation": 6})

    def test_stop_drops_queued_result(self) -> None:
        job = self.manager.request_snapshot(card_ids=["1"])
        self.manager.stop()
        self.run_pending()

        self.assertEqual(job.state, SnapshotJobState.CANCELED)
        self.assertEqual(self.published, [])
        with self.assertRaises(SnapshotJobError) as raised:
            self.manager.request_snapshot(card_ids=["1"])
        self.assertEqual(raised.exception.code, "runtime_stopped")


if __name__ == "__main__":
    unittest.main()
