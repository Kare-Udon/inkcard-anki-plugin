"""只读导出复习时间调查；断言现状，不实现或伪造 revlog fallback。"""

import sqlite3
import unittest

import test_anki_adapter_v2 as fixtures


class LastReviewTimeInvestigationTests(unittest.TestCase):
    def export(self, last_review_time, **attributes):
        card = fixtures.FakeCard(1, 10, 2, 42, card_type=2)
        card.last_review_time = last_review_time
        for name, value in attributes.items():
            setattr(card, name, value)
        collection = fixtures.FakeCollection([card])
        calls = []

        def get_review_logs(card_id):
            calls.append(card_id)
            return [{"id": 1_700_000_000_123, "ease": 3, "type": 1, "factor": 2500}]

        collection.get_review_logs = get_review_logs
        adapter = fixtures.AdapterV2Tests().make_adapter(collection)
        snapshot = adapter.export_snapshot(card_ids=["1"])
        return snapshot.cards[0], calls

    def test_missing_native_time_is_exported_even_with_review_history(self):
        card, calls = self.export(None)
        self.assertIsNone(card.last_review_time_ms)
        self.assertFalse(card.supported)
        self.assertIn("scheduler_last_review_time_missing", card.unsupported)
        self.assertIsNone(card.to_dict()["last_review_time_ms"])
        self.assertEqual(calls, [])

    def test_explicit_zero_is_preserved_not_treated_as_missing(self):
        card, calls = self.export(0)
        self.assertEqual(card.last_review_time_ms, 0)
        self.assertEqual(calls, [])

    def test_positive_seconds_are_converted_to_milliseconds(self):
        card, calls = self.export(1_700_000_000)
        self.assertEqual(card.last_review_time_ms, 1_700_000_000_000)
        self.assertEqual(calls, [])

    def test_explicit_millisecond_field_has_priority(self):
        card, calls = self.export(1_700_000_000, last_review_time_ms=1_700_000_000_123)
        self.assertEqual(card.last_review_time_ms, 1_700_000_000_123)
        self.assertEqual(calls, [])

    def test_official_runtime_fallback_sql_on_synthetic_history(self):
        # 固定 Anki 26.09.3 time_of_last_review.sql，独立内存库复现选取规则。
        query = """SELECT id / 1000 FROM revlog WHERE cid = ?
            AND ease BETWEEN 1 AND 4 AND (type != 3 OR factor != 0)
            ORDER BY id DESC LIMIT 1"""
        cases = (
            ([(1000123, 3, 1, 2500), (2000123, 0, 4, 2500)], 1000),
            ([(1000123, 3, 1, 2500), (2000123, 0, 5, 2500)], 1000),
            ([(1000123, 3, 1, 2500), (2000123, 3, 3, 0)], 1000),
            ([(1000123, 3, 1, 2500), (2000123, 3, 3, 2500)], 2000),
            ([(2000123, 0, 4, 0)], None),
            ([], None),
        )
        for rows, expected_seconds in cases:
            with (
                self.subTest(expected_seconds=expected_seconds, rows=len(rows)),
                sqlite3.connect(":memory:") as db,
            ):
                db.execute(
                    "CREATE TABLE revlog(id INTEGER, cid INTEGER, ease INTEGER, type INTEGER, factor INTEGER)"
                )
                db.executemany("INSERT INTO revlog VALUES (?, 1, ?, ?, ?)", rows)
                result = db.execute(query, (1,)).fetchone()
                self.assertEqual(result[0] if result else None, expected_seconds)
