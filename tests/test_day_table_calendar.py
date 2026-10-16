"""按宿主本地日历导出 Anki 学习日边界。"""

from __future__ import annotations

import calendar
import os
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter


@contextmanager
def local_timezone(name: str):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def utc_ms(year: int, month: int, day: int, hour: int) -> int:
    return calendar.timegm((year, month, day, hour, 0, 0)) * 1000


class DayTableCalendarTests(unittest.TestCase):
    def export(self, cutoff_ms: int):
        adapter = Anki260801Adapter(
            collection_provider=lambda: None,
            profile_provider=lambda: "test",
            profile_identity="test",
            anki_version="26.08.1",
            anki_build="39e4b0b4",
        )
        collection = SimpleNamespace(sched=SimpleNamespace(today=650, day_cutoff=cutoff_ms // 1000))
        return adapter._day_table(collection, {"new": 1})

    def test_spring_dst_day_is_23_hours_without_losing_calendar_day(self):
        with local_timezone("America/New_York"):
            table = self.export(utc_ms(2026, 3, 7, 9))  # 3 月 7 日当地 04:00
            self.assertEqual(table.first_day_start_ms, utc_ms(2026, 3, 6, 9))
            self.assertEqual(len(table.future_cutoffs_ms), 90)
            self.assertEqual(table.future_cutoffs_ms[0], table.day_cutoff_ms)
            self.assertEqual(table.future_cutoffs_ms[1], utc_ms(2026, 3, 8, 8))
            self.assertEqual(table.future_cutoffs_ms[2], utc_ms(2026, 3, 9, 8))
            self.assertTrue(all(a < b for a, b in zip(table.future_cutoffs_ms, table.future_cutoffs_ms[1:])))

    def test_fall_dst_day_is_25_hours(self):
        with local_timezone("America/New_York"):
            table = self.export(utc_ms(2026, 10, 31, 8))  # 10 月 31 日当地 04:00
            self.assertEqual(table.future_cutoffs_ms[0], utc_ms(2026, 10, 31, 8))
            self.assertEqual(table.future_cutoffs_ms[1], utc_ms(2026, 11, 1, 9))
            self.assertEqual(table.future_cutoffs_ms[2], utc_ms(2026, 11, 2, 9))

    def test_fixed_zone_keeps_existing_day_fields_and_90_cutoffs(self):
        with local_timezone("Asia/Shanghai"):
            table = self.export(utc_ms(2026, 9, 29, 20))  # 9 月 30 日当地 04:00
            self.assertEqual(table.review_day, 650)
            self.assertEqual(table.day_learning_day, 650)
            self.assertEqual(table.counts["new"], 1)
            self.assertEqual(table.first_day_start_ms, utc_ms(2026, 9, 28, 20))
            self.assertEqual(table.future_cutoffs_ms[0], utc_ms(2026, 9, 29, 20))
            self.assertEqual(table.future_cutoffs_ms[-1], utc_ms(2026, 12, 27, 20))
            self.assertEqual(table.to_dict()["future_cutoffs_ms"], list(table.future_cutoffs_ms))

    def test_nonexistent_dst_rollover_does_not_publish_guessed_boundaries(self):
        with local_timezone("America/New_York"):
            table = self.export(utc_ms(2026, 3, 7, 7) + 30 * 60_000)  # 当地 02:30，次日不存在
            self.assertIsNone(table.first_day_start_ms)
            self.assertEqual(table.future_cutoffs_ms, ())
            self.assertIn("future_day_table_unavailable", table.unsupported)

    def test_ambiguous_dst_rollover_does_not_choose_a_fold(self):
        with local_timezone("America/New_York"):
            table = self.export(utc_ms(2026, 10, 31, 5) + 30 * 60_000)  # 当地 01:30
            self.assertIsNone(table.first_day_start_ms)
            self.assertEqual(table.future_cutoffs_ms, ())
            self.assertIn("future_day_table_unavailable", table.unsupported)


if __name__ == "__main__":
    unittest.main()
