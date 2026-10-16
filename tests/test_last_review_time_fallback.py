"""只读时间来源与冻结前态的回归。"""

import sqlite3
import unittest

import test_anki_adapter_v2 as fixtures
from inkcards_anki_bridge.anki.application_context import CapturedApplicationContext
from inkcards_anki_bridge.anki.compact_projection import verify_card_snapshot


class LastReviewFallbackTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE revlog(id,cid,usn,ease,ivl,lastIvl,factor,time,type)"
        )
        self.card = fixtures.FakeCard(1, 10, 2, 42)
        self.card.last_review_time = None
        self.collection = fixtures.FakeCollection([self.card])
        database = self.db

        class Reader:
            def all(self, query, *args):
                return database.execute(query, args).fetchall()

        self.collection.db = Reader()
        self.adapter = fixtures.AdapterV2Tests().make_adapter(self.collection)

    def tearDown(self):
        self.db.close()

    def add_log(self, timestamp=1_700_000_000_123, ease=3, kind=1, factor=2500):
        self.db.execute(
            "INSERT INTO revlog VALUES (?,1,0,?,7,3,?,1000,?)",
            (timestamp, ease, factor, kind),
        )

    def test_missing_native_time_uses_official_second_precision(self):
        self.add_log()
        snapshot = self.adapter.export_snapshot(card_ids=["1"])
        self.assertEqual(snapshot.cards[0].last_review_time_ms, 1_700_000_000_000)
        self.assertIsNone(self.card.last_review_time)

    def test_new_card_never_uses_old_history(self):
        self.add_log()
        self.card.type, self.card.queue, self.card.due = 0, 0, 1
        snapshot = self.adapter.export_snapshot(card_ids=["1"])
        self.assertIsNone(snapshot.cards[0].last_review_time_ms)
        self.assertNotIn(
            "scheduler_last_review_time_missing", snapshot.cards[0].unsupported
        )

    def test_missing_history_is_explicitly_unsupported(self):
        snapshot = self.adapter.export_snapshot(card_ids=["1"])
        self.assertIn(
            "scheduler_last_review_time_missing", snapshot.cards[0].unsupported
        )
        from inkcards_anki_bridge.services.snapshots import (
            SnapshotError,
            SnapshotService,
        )

        with self.assertRaises(SnapshotError) as caught:
            SnapshotService._snapshot_dict(snapshot)
        self.assertEqual(caught.exception.code, "scheduler_last_review_time_missing")

    def test_only_scheduling_ratings_supply_time(self):
        self.add_log()
        for index, (ease, kind, factor) in enumerate(
            ((0, 4, 2500), (0, 5, 2500), (3, 3, 0)), 1
        ):
            self.add_log(1_700_000_000_123 + index * 1000, ease, kind, factor)
        self.assertEqual(
            self.adapter.export_snapshot(card_ids=["1"]).cards[0].last_review_time_ms,
            1_700_000_000_000,
        )
        self.add_log(1_700_000_010_123, 3, 3, 2500)
        self.assertEqual(
            self.adapter.export_snapshot(card_ids=["1"]).cards[0].last_review_time_ms,
            1_700_000_010_000,
        )

    def test_frozen_source_verifies_native_none_and_history(self):
        from inkcards_anki_bridge.anki.application_context import (
            capture_last_review_time_source,
        )

        self.add_log()
        source = capture_last_review_time_source(self.collection, self.card)
        wire = self.adapter.export_snapshot(card_ids=["1"]).cards[0].to_dict()
        verify_card_snapshot(self.card, wire, last_review_time_source=source)
        self.card.last_review_time = source["resolved_seconds"]
        with self.assertRaises(ValueError):
            verify_card_snapshot(self.card, wire, last_review_time_source=source)

    def test_old_context_hash_is_unchanged_when_source_map_empty(self):
        context = CapturedApplicationContext("profile-a", 0, None, "26.9.3", "29bb700b")
        self.assertNotIn("card_last_review_time_sources", context.to_dict())
        self.assertEqual(
            CapturedApplicationContext.from_dict(context.to_dict()).source_sha256,
            context.source_sha256,
        )
