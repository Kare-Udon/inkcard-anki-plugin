from __future__ import annotations

import json
import copy
import tempfile
import time
import unittest
from pathlib import Path

# Anki's bundled packages require importing Collection before other anki modules.
import anki.collection  # type: ignore[import-not-found]  # noqa: F401
from anki.cards import FSRSMemoryState  # type: ignore[import-not-found]
from anki.collection import Collection  # type: ignore[import-not-found]
from anki.collection_pb2 import OpChanges  # type: ignore[import-not-found]
from anki.errors import InvalidInput  # type: ignore[import-not-found]
from anki.scheduler_pb2 import CardAnswer  # type: ignore[import-not-found]

from inkcards_anki_bridge.anki.compact_260903 import (
    AnkiCompactAdapter,
    _capture_card,
    _capture_state,
    _anki_storage_memory,
    _score_stage_readback_matches,
    _state_matches_before,
)
from inkcards_anki_bridge.anki.compact_projection import (
    MAX_CUSTOM_DATA_BYTES,
    CompactProjectionError,
    build_marker_custom_data,
    canonical_json,
)


class CompactNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="inkcards-native-", dir="/private/tmp")
        self.root = Path(self.tempdir.name)
        self.collection = Collection(str(self.root / "synthetic.anki2"))
        self.collection.set_config("fsrs", True)
        deck_config = self.collection.decks.config_dict_for_deck_id(1)
        deck_config["desiredRetention"] = 0.9
        self.collection.decks.save(deck_config)
        self.card_id = self._new_review_card()

    def tearDown(self) -> None:
        self.collection.close()
        self.tempdir.cleanup()

    def _new_review_card(self) -> int:
        note_type = self.collection.models.by_name("Basic")
        note = self.collection.new_note(note_type)
        note.fields[0] = "synthetic compact front"
        if len(note.fields) > 1:
            note.fields[1] = "synthetic compact back"
        self.collection.add_note(note, 1)
        card_id = int(self.collection.card_ids_of_note(note.id)[0])
        card = self.collection.get_card(card_id)
        card.type = 2
        card.queue = 2
        card.due = int(self.collection.sched.today) - 1
        card.ivl = 7
        card.factor = 2500
        card.reps = 1
        card.lapses = 0
        card.left = 0
        card.last_review_time = int(time.time()) - 86_400
        card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
        changes = self.collection.update_card(card)
        self.assertIsInstance(changes, OpChanges)
        return card_id

    def test_marker_replacement_requires_proven_predecessor(self) -> None:
        old_marker = "a" * 64
        new_marker = "b" * 64
        existing = canonical_json({"other": "preserved", "inkcards": old_marker})
        replaced = json.loads(
            build_marker_custom_data(
                existing,
                new_marker,
                owned_marker_digest=old_marker,
            )
        )
        self.assertEqual(replaced, {"other": "preserved", "inkcards": new_marker})

        for owner in (None, "c" * 64):
            with self.subTest(owner=owner):
                with self.assertRaises(CompactProjectionError) as caught:
                    build_marker_custom_data(existing, new_marker, owned_marker_digest=owner)
                self.assertEqual(caught.exception.code, "compact_marker_unowned")

    def test_addon_99_byte_policy_accepts_99_and_rejects_100(self) -> None:
        marker = "d" * 64
        extra_length = MAX_CUSTOM_DATA_BYTES - len(build_marker_custom_data({"other": ""}, marker).encode("utf-8"))
        exact_payload = {"other": "x" * extra_length}
        exact = build_marker_custom_data(exact_payload, marker)
        self.assertEqual(len(exact.encode("utf-8")), 99)

        card = self.collection.get_card(self.card_id)
        card.custom_data = exact
        changes = self.collection.update_card(card)
        self.assertIsInstance(changes, OpChanges)
        observed = str(self.collection.get_card(self.card_id).custom_data)
        self.assertEqual(len(observed.encode("utf-8")), 99)
        self.assertEqual(json.loads(observed)["other"], exact_payload["other"])
        self.assertEqual(json.loads(observed)["inkcards"], marker)

        with self.assertRaises(CompactProjectionError) as too_large:
            build_marker_custom_data({"other": "x" * (extra_length + 1)}, marker)
        self.assertEqual(too_large.exception.code, "custom_data_too_large")

    def test_native_source_inclusive_100_byte_boundary_and_101_rollback(self) -> None:
        # 固定 sourceSHA aacbb588... 的实际条件是 json_str.len() <= 100。
        marker = "e" * 64
        empty = canonical_json({"other": "", "inkcards": marker})
        extra_length = 100 - len(empty.encode("utf-8"))
        exactly_100 = canonical_json({"other": "x" * extra_length, "inkcards": marker})
        self.assertEqual(len(exactly_100.encode("utf-8")), 100)
        card = self.collection.get_card(self.card_id)
        card.custom_data = exactly_100
        self.assertIsInstance(self.collection.update_card(card), OpChanges)
        self.assertEqual(self.collection.get_card(self.card_id).custom_data, exactly_100)
        before = _capture_state(self.collection, self.card_id, (1,))
        card = self.collection.get_card(self.card_id)
        card.custom_data = canonical_json({"other": "x" * (extra_length + 1), "inkcards": marker})
        self.assertEqual(len(card.custom_data.encode("utf-8")), 101)
        with self.assertRaises(InvalidInput):
            self.collection.update_card(card)
        self.assertEqual(_capture_state(self.collection, self.card_id, (1,)), before)

    @unittest.expectedFailure
    def test_native_answer_invalid_marker_rolls_back_card_revlog_memory_and_counters(self) -> None:
        # 固定 Anki 26.09.3 vendor 已知限制：InvalidInput 保持持久表，却清空进程内 Undo。
        # 保留原断言作为直接 API 探针；插件容量预检另有真实产品回归，不能 monkeypatch vendor。
        card = self.collection.get_card(self.card_id)
        card.custom_data = canonical_json({"other": "保留"})
        self.collection.update_card(card)
        states = self.collection._backend.get_scheduling_states(self.card_id)
        answer = CardAnswer(
            card_id=self.card_id, current_state=states.current, new_state=states.good,
            rating=CardAnswer.GOOD, answered_at_millis=int(time.time() * 1000), milliseconds_taken=120,
        )
        answer.new_state.custom_data = canonical_json({"other": "x" * 101})
        before = _capture_state(self.collection, self.card_id, (1,))
        undo = self.collection.undo_status().SerializeToString(deterministic=True)
        with self.assertRaises(InvalidInput):
            self.collection.sched.answer_card(answer)
        self.assertEqual(_capture_state(self.collection, self.card_id, (1,)), before)
        self.assertEqual(self.collection.undo_status().SerializeToString(deterministic=True), undo)

    def test_f32_ties_away_storage_matches_real_backend_exactly(self) -> None:
        card = self.collection.get_card(self.card_id)
        card.memory_state = FSRSMemoryState(stability=1.23445, difficulty=5.0005)
        self.collection.update_card(card)
        actual = self.collection.get_card(self.card_id).memory_state
        self.assertEqual(float(actual.stability), _anki_storage_memory(1.23445, 4))
        self.assertEqual(float(actual.difficulty), _anki_storage_memory(5.0005, 3))
        self.assertNotEqual(float(actual.difficulty), 5.0)

    def test_utf8_key_bytes_and_third_party_value_preservation(self) -> None:
        result = build_marker_custom_data({"日本": 1}, "f" * 64)
        self.assertEqual(json.loads(result)["日本"], 1)
        with self.assertRaises(CompactProjectionError) as caught:
            build_marker_custom_data({"日本語": 1}, "f" * 64)
        self.assertEqual(caught.exception.code, "custom_data_key_too_long")

    def test_score_stage_readback_requires_full_card_logs_and_counters(self) -> None:
        plan = {
            "before": {
                "card": {"card_id": str(self.card_id), "schedule_revision": "volatile"},
                "review_logs": [],
                "counters": {"1": {"review_studied": 0}},
            },
            "expected_score_after": {
                "card": {"card_id": str(self.card_id), "custom_data": "{\"inkcards\":\"x\"}"},
                "score_review_log": {"id": 10, "card_id": self.card_id, "ease": 3},
                "review_log_count": 1,
                "counters": {"1": {"review_studied": 1}},
            },
            "expected_final_card": {
                "card_id": str(self.card_id),
                "custom_data": "{\"inkcards\":\"x\"}",
            },
            "expected_final_counters": {"1": {"review_studied": 0}},
        }
        expected_score_card = plan["expected_score_after"]["card"]
        valid_score = {
            "card": expected_score_card,
            "review_logs": [{"id": 10, "card_id": self.card_id, "ease": 3}],
            "counters": {"1": {"review_studied": 1}},
        }
        self.assertTrue(_score_stage_readback_matches(valid_score, plan))
        missing_old_log = {**valid_score, "review_logs": []}
        self.assertFalse(_score_stage_readback_matches(missing_old_log, plan))
        wrong_counter = {**valid_score, "counters": {"1": {"review_studied": 2}}}
        self.assertFalse(_score_stage_readback_matches(wrong_counter, plan))

    def test_raw_prestate_rejects_mod_and_third_party_memory_change(self) -> None:
        before = _capture_state(self.collection, self.card_id, (1,))

        changed_mod = copy.deepcopy(before)
        changed_mod["card"]["schedule_revision"] = str(int(before["card"]["schedule_revision"]) + 1)
        # Store 帧为链连续性省略 mod；原生前态仍独立检查完整原始 revision。
        self.assertEqual(
            AnkiCompactAdapter.state_for_store(changed_mod),
            AnkiCompactAdapter.state_for_store(before),
        )
        self.assertFalse(_state_matches_before(changed_mod, before))

        live_before_card = _capture_card(self.collection.get_card(self.card_id))
        card = self.collection.get_card(self.card_id)
        card.memory_state = FSRSMemoryState(
            stability=float(live_before_card["memory_state"]["stability"]),
            difficulty=float(live_before_card["memory_state"]["difficulty"]) + 0.001,
        )
        self.collection.update_card(card)
        third_party_after = _capture_state(self.collection, self.card_id, (1,))
        self.assertNotEqual(
            live_before_card["memory_state"]["difficulty"],
            third_party_after["card"]["memory_state"]["difficulty"],
        )
        self.assertFalse(_state_matches_before(third_party_after, before))

    def test_no_write_changes_value_is_a_real_empty_opchanges(self) -> None:
        empty = AnkiCompactAdapter.empty_changes()
        self.assertIsInstance(empty, OpChanges)
        self.assertEqual(list(empty.ListFields()), [])


if __name__ == "__main__":
    unittest.main()
