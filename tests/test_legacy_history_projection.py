"""原生正控制：持久 lrt 缺失时，scratch 必须保留原始复习历史。"""
from __future__ import annotations

import unittest

import anki.collection  # noqa: F401
from anki.scheduler_pb2 import CardAnswer
from test_compact_service import SyntheticFixture

from inkcards_anki_bridge.anki.compact_260903 import CompactNativeError, _capture_card
from inkcards_anki_bridge.anki.compact_projection import normalize_preset_config
from inkcards_anki_bridge.anki.compact_transfer import project_native_answer


class LegacyHistoryProjectionTests(unittest.TestCase):
    def test_missing_persisted_lrt_matches_native_source_with_real_prior_revlog(self):
        import tempfile
        import time
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix="inkcards-native-service-legacy-lrt-", dir="/private/tmp") as directory:
            fixture = SyntheticFixture(Path(directory) / "fixture")
            try:
                col = fixture.collection
                cid = fixture.meta["card_ids"][0]
                col._backend.set_load_balancer_enabled(False)
                states = col._backend.get_scheduling_states(cid)
                col.sched.answer_card(CardAnswer(card_id=cid, current_state=states.current,
                    new_state=states.good, rating=3, answered_at_millis=(int(time.time())-20*86400)*1000,
                    milliseconds_taken=120))
                card = col.get_card(cid)
                card.last_review_time = None
                col.update_card(card)
                self.assertEqual(col._backend.get_card(cid).last_review_time_secs, 0)
                self.assertEqual(col.db.scalar("SELECT count(*) FROM revlog WHERE cid=?", cid), 1)
                before = _capture_card(col.get_card(cid))
                config = normalize_preset_config({**col.decks.config_dict_for_deck_id(int(card.did)), "fsrs_enabled": True})
                native = col._backend.get_scheduling_states(cid)
                event = {"card_id": str(cid), "rating": 3, "answered_at_ms": int(time.time())*1000, "duration_ms": 120}
                projected = project_native_answer(col, before, config, event,
                    today=int(col.sched.today), cutoff=int(col.sched.day_cutoff), event_day=int(col.sched.today))
                self.assertEqual(projected["states"].good.normal.review.scheduled_days,
                                 native.good.normal.review.scheduled_days)
                self.assertEqual(projected["states"].good.normal.review.memory_state,
                                 native.good.normal.review.memory_state)
            finally:
                fixture.close()

    def test_real_review_after_event_rejects_before_any_native_answer(self):
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="inkcards-native-service-legacy-future-", dir="/private/tmp") as directory:
            fixture = SyntheticFixture(Path(directory) / "fixture")
            try:
                col = fixture.collection
                cid = fixture.meta["card_ids"][0]
                states = col._backend.get_scheduling_states(cid)
                col.sched.answer_card(CardAnswer(card_id=cid, current_state=states.current,
                    new_state=states.good, rating=3, answered_at_millis=(int(time.time())-2*86400)*1000,
                    milliseconds_taken=120))
                card = col.get_card(cid)
                card.last_review_time = None
                col.update_card(card)
                before = _capture_card(col.get_card(cid))
                logs = col.db.all("SELECT * FROM revlog WHERE cid=?", cid)
                config = normalize_preset_config({**col.decks.config_dict_for_deck_id(int(card.did)), "fsrs_enabled": True})
                event = {"card_id": str(cid), "rating": 3, "answered_at_ms": (int(time.time())-3*86400)*1000, "duration_ms": 120}
                with patch.object(type(col.sched), "answer_card", side_effect=AssertionError("不能评分")) as answer:
                    with self.assertRaises(CompactNativeError) as caught:
                        project_native_answer(col, before, config, event,
                            today=int(col.sched.today), cutoff=int(col.sched.day_cutoff), event_day=int(col.sched.today)-3)
                self.assertEqual(caught.exception.code, "event_precedes_last_review")
                self.assertEqual(answer.call_count, 0)
                self.assertEqual(before, _capture_card(col.get_card(cid)))
                self.assertEqual(logs, col.db.all("SELECT * FROM revlog WHERE cid=?", cid))
            finally:
                fixture.close()
