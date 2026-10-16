from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.anki.compatibility import SUPPORTED_ANKI_BUILD
from inkcards_anki_bridge.anki.catalog_jobs import (
    CatalogJobError,
    CatalogJobManager,
    CatalogJobState,
    DeckCatalog,
    DeckPreflight,
    build_deck_catalog,
    build_deck_preflight,
)
from inkcards_anki_bridge.anki.operations import OperationContext, QueryOpAdapter


class FakeDeckName:
    def __init__(self, deck_id: int, name: str) -> None:
        self.id = deck_id
        self.name = name


class FakeDecks:
    def __init__(self) -> None:
        self.entries = [
            FakeDeckName(20, "中文::子牌组"),
            FakeDeckName(10, "中文"),
            FakeDeckName(30, "数学"),
            FakeDeckName(30, "数学"),
        ]
        self.catalog_calls: list[tuple[bool, bool]] = []
        self.count_calls: list[tuple[tuple[int, ...], bool]] = []
        self.count = 2
        self.block_count = False
        self.entered_count = threading.Event()
        self.release_count = threading.Event()
        self.fail_catalog = False

    def all_names_and_ids(self, *, skip_empty_default: bool, include_filtered: bool):
        self.catalog_calls.append((skip_empty_default, include_filtered))
        if self.fail_catalog:
            raise RuntimeError("catalog failed")
        return list(self.entries)

    def card_count(self, deck_ids, include_subdecks: bool):
        self.count_calls.append((tuple(deck_ids), include_subdecks))
        self.entered_count.set()
        if self.block_count:
            self.release_count.wait(timeout=2.0)
        return self.count


class FakeCollection:
    def __init__(self) -> None:
        self.decks = FakeDecks()
        self.db_accessed = False

    @property
    def db(self):
        self.db_accessed = True
        raise AssertionError("catalog/preflight must not access collection.db")


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


class CatalogJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ContextSource()
        self.host = FakeHost()
        self.collection = FakeCollection()
        self.provider_called = False

        def forbidden_collection_provider():
            self.provider_called = True
            raise AssertionError("collection provider must not be used by catalog jobs")

        self.adapter = Anki260801Adapter(
            collection_provider=forbidden_collection_provider,
            profile_provider=self.source,
            profile_identity="profile-a",
            anki_version="26.08.1",
            anki_build=SUPPORTED_ANKI_BUILD,
        )
        self.manager = CatalogJobManager(
            operation_adapter=QueryOpAdapter(self.host, self.source),
            adapter=self.adapter,
            context_provider=self.source,
            snapshot_limit=4,
        )

    def run_pending(self, index: int = 0) -> None:
        operation = self.host.pending.pop(index)
        operation(self.collection)

    def test_context_provider_failure_on_request_is_classified(self):
        with patch.object(self.manager, "_context_provider", side_effect=RuntimeError("profile closed")):
            with self.assertRaises(CatalogJobError) as raised:
                self.manager.request_catalog()
        self.assertEqual(raised.exception.code, "context_unavailable")
        self.assertEqual(self.host.pending, [])

    def test_context_provider_failure_at_completion_releases_scope(self):
        job = self.manager.request_catalog()
        original = self.manager._on_operation_result

        def complete(job, result):
            with patch.object(self.manager, "_context_provider", side_effect=RuntimeError("profile closed")):
                original(job, result)

        with patch.object(self.manager, "_on_operation_result", side_effect=complete):
            self.run_pending()
        self.assertEqual(job.state, CatalogJobState.STALE_RESULT_DROPPED)
        self.assertIsNone(job.operation_task)
        self.assertIsNone(self.manager.last_catalog)
        self.manager.request_catalog().cancel()

    def test_job_capacity_and_history_are_bounded_without_dropping_active_jobs(self):
        self.manager = CatalogJobManager(
            operation_adapter=QueryOpAdapter(self.host, self.source),
            adapter=self.adapter,
            context_provider=self.source,
            max_active_jobs=2,
            max_retained_jobs=3,
        )
        active = self.manager.request_catalog()
        second = self.manager.request_preflight(deck_ids=["10"])
        with self.assertRaises(CatalogJobError) as raised:
            self.manager.request_preflight(deck_ids=["20"])
        self.assertEqual(raised.exception.code, "job_capacity")
        self.assertEqual(len(self.host.pending), 2)
        second.cancel()
        oldest = second
        for _ in range(5):
            latest = self.manager.request_preflight(deck_ids=["10"])
            latest.cancel()
        self.assertIs(self.manager.get(active.job_id), active)
        self.assertIsNone(self.manager.get(oldest.job_id))
        self.assertIs(self.manager.get(latest.job_id), latest)
        self.assertEqual(oldest.state, CatalogJobState.CANCELED)

    def test_catalog_is_immutable_sorted_deduplicated_and_derives_parent(self) -> None:
        catalog = build_deck_catalog(self.collection, self.adapter)

        self.assertIsInstance(catalog, DeckCatalog)
        self.assertEqual([entry.deck_id for entry in catalog.entries], ["10", "20", "30"])
        child = catalog.by_id("20")
        self.assertIsNotNone(child)
        assert child is not None
        self.assertEqual(child.parent_name, "中文")
        self.assertEqual(child.parent_id, "10")
        self.assertEqual(child.path, ("中文", "子牌组"))
        self.assertEqual(child.depth, 1)
        with self.assertRaises((AttributeError, TypeError)):
            child.name = "changed"  # type: ignore[misc]
        self.assertEqual(self.collection.decks.catalog_calls, [(False, True)])
        self.assertFalse(self.collection.db_accessed)

    def test_catalog_name_anomaly_fails_closed(self) -> None:
        self.collection.decks.entries = [FakeDeckName(10, "中文::")]
        with self.assertRaises(CatalogJobError) as raised:
            build_deck_catalog(self.collection, self.adapter)
        self.assertEqual(raised.exception.code, "deck_name_invalid")

    def test_preflight_uses_fixed_parent_includes_subdecks_semantics(self) -> None:
        result = build_deck_preflight(
            self.collection,
            self.adapter,
            deck_ids=["20", "10", "20"],
            snapshot_limit=4,
        )

        self.assertIsInstance(result, DeckPreflight)
        self.assertEqual(result.deck_ids, ("10", "20"))
        self.assertEqual(result.card_count, 2)
        self.assertEqual(result.snapshot_limit, 4)
        self.assertTrue(result.within_limit)
        self.assertEqual(self.collection.decks.count_calls, [((10, 20), True)])
        self.assertFalse(self.collection.db_accessed)

    def test_preflight_reports_count_over_snapshot_limit(self) -> None:
        self.collection.decks.count = 5
        result = build_deck_preflight(
            self.collection,
            self.adapter,
            deck_ids=["10"],
            snapshot_limit=4,
        )

        self.assertFalse(result.within_limit)
        self.assertEqual(result.error_code, "snapshot_limit_exceeded")
        self.assertEqual(result.card_count, 5)
        self.assertIn("snapshot", result.error_message or "")

    def test_scope_is_explicit_non_empty_and_bounded_at_64(self) -> None:
        with self.assertRaises(CatalogJobError) as empty:
            self.manager.request_preflight(deck_ids=[])
        self.assertEqual(empty.exception.code, "scope_required")
        with self.assertRaises(CatalogJobError) as too_large:
            self.manager.request_preflight(deck_ids=[str(value) for value in range(1, 66)])
        self.assertEqual(too_large.exception.code, "scope_too_large")
        self.assertEqual(self.host.pending, [])

    def test_async_catalog_reads_only_inside_queryop_and_publishes_last_success(self) -> None:
        job = self.manager.request_catalog()
        self.assertEqual(job.state, CatalogJobState.QUEUED)
        self.assertFalse(self.provider_called)
        self.assertIsNone(self.manager.last_catalog)

        self.run_pending()

        self.assertEqual(job.state, CatalogJobState.PUBLISHED)
        self.assertIsInstance(job.result, DeckCatalog)
        self.assertIs(self.manager.last_catalog, job.result)
        self.assertFalse(self.provider_called)

    def test_duplicate_requests_are_rejected(self) -> None:
        self.manager.request_catalog()
        with self.assertRaises(CatalogJobError) as catalog_duplicate:
            self.manager.request_catalog()
        self.assertEqual(catalog_duplicate.exception.code, "duplicate_catalog")

        self.run_pending()
        self.manager.request_preflight(deck_ids=["10"])
        with self.assertRaises(CatalogJobError) as preflight_duplicate:
            self.manager.request_preflight(deck_ids=[10])
        self.assertEqual(preflight_duplicate.exception.code, "duplicate_preflight")

    def test_cancel_before_and_after_start_never_publishes(self) -> None:
        queued = self.manager.request_catalog()
        self.assertTrue(queued.cancel())
        self.run_pending()
        self.assertEqual(queued.state, CatalogJobState.CANCELED)
        self.assertIsNone(self.manager.last_catalog)

        self.collection.decks.block_count = True
        started = self.manager.request_preflight(deck_ids=["10"])
        worker = threading.Thread(target=self.run_pending)
        worker.start()
        self.assertTrue(self.collection.decks.entered_count.wait(timeout=1.0))
        self.assertTrue(started.cancel())
        self.collection.decks.release_count.set()
        worker.join(timeout=2.0)
        self.assertEqual(started.state, CatalogJobState.CANCELED)
        self.assertIsNone(self.manager.last_preflight)

    def test_late_result_cannot_replace_newer_preflight(self) -> None:
        first = self.manager.request_preflight(deck_ids=["10"])
        second = self.manager.request_preflight(deck_ids=["20"])
        self.collection.decks.count = 3
        self.run_pending(index=1)
        self.assertEqual(second.state, CatalogJobState.PUBLISHED)
        self.assertIs(self.manager.last_preflight, second.result)

        self.collection.decks.count = 1
        self.run_pending(index=0)
        self.assertEqual(first.state, CatalogJobState.STALE_RESULT_DROPPED)
        self.assertIs(self.manager.last_preflight, second.result)

    def test_failure_keeps_previous_success_and_stop_drops_late_callback(self) -> None:
        self.manager.request_catalog()
        self.run_pending()
        old = self.manager.last_catalog
        self.assertIsNotNone(old)

        self.collection.decks.fail_catalog = True
        failed = self.manager.request_catalog()
        self.run_pending()
        self.assertEqual(failed.state, CatalogJobState.FAILED)
        self.assertIs(self.manager.last_catalog, old)

        queued = self.manager.request_catalog()
        self.manager.stop()
        self.run_pending()
        self.assertEqual(queued.state, CatalogJobState.CANCELED)
        self.assertIs(self.manager.last_catalog, old)
        with self.assertRaises(CatalogJobError) as stopped:
            self.manager.request_catalog()
        self.assertEqual(stopped.exception.code, "runtime_stopped")

    def test_context_change_drops_result(self) -> None:
        job = self.manager.request_catalog()
        self.source.current = OperationContext("profile-a", 8, "epoch-a")
        self.run_pending()
        self.assertEqual(job.state, CatalogJobState.STALE_RESULT_DROPPED)
        self.assertIsNone(self.manager.last_catalog)


if __name__ == "__main__":
    unittest.main()
