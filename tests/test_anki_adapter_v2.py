from __future__ import annotations

import unittest

from inkcards_anki_bridge.adapter import AnkiAdapterError, AnkiSnapshotExporter
from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.anki.compatibility import (
    SUPPORTED_ANKI_BUILD,
    check_anki_compatibility,
)
from inkcards_anki_bridge.anki.models import DueKind, SkipReason


class Memory:
    stability = 4.0
    difficulty = 6.0


class FakeCard:
    def __init__(
        self,
        card_id: int,
        deck_id: int,
        queue: int,
        due,
        *,
        card_type: int = 2,
        question: str = "<p>题面</p>",
        answer: str = "<p>答案</p>",
        memory=Memory(),
    ) -> None:
        self.id = card_id
        self.nid = card_id + 1000
        self.did = deck_id
        self.ord = 0
        self.type = card_type
        self.queue = queue
        self.due = due
        self.ivl = 3
        self.factor = 2500
        self.reps = 2
        self.lapses = 0
        self.left = 1
        self.last_review_time = 1_700_000_000
        self.mod = 9
        self.memory_state = memory
        self._question = question
        self._answer = answer

    def question(self):
        return self._question

    def answer(self):
        return self._answer


class FakeDecks:
    def __init__(self) -> None:
        self.fsrs_enabled = True
        self.records = {
            10: {
                "id": 10,
                "name": "语言",
                "parent_id": 1,
                "preset_id": 100,
                "preset_name": "语言预设",
                "config": {
                    "id": 100,
                    "name": "语言预设",
                    "fsrs": {"params": [0.1, 0.2], "desiredRetention": 0.91},
                    "new": {"perDay": 20, "delays": [1, 10]},
                    "rev": {"perDay": 50},
                    "lapse": {"delays": [10]},
                },
                "new_today": 4,
                "review_today": 7,
                "learning_today": 2,
            },
            20: {
                "id": 20,
                "name": "语言::子牌组",
                "parent_id": 1,
                "preset_id": 200,
                "preset_name": "考试预设",
                "config": {
                    "id": 200,
                    "name": "考试预设",
                    "fsrs": {"params": [0.3, 0.4], "desiredRetention": 0.88},
                    "new": {"perDay": 10, "delays": [5]},
                    "rev": {"perDay": 30},
                    "lapse": {"delays": [20]},
                },
                "new_today": 1,
                "review_today": 3,
                "learning_today": 1,
            },
            1: {
                "id": 1,
                "name": "语言",
                "parent_id": None,
                "preset_id": 100,
                "preset_name": "语言预设",
                "config": {"id": 100, "name": "语言预设"},
                "new_today": 0,
                "review_today": 0,
                "learning_today": 0,
            },
        }

    def get(self, deck_id):
        return self.records[int(deck_id)]

    def config_dict_for_deck_id(self, deck_id):
        return self.records[int(deck_id)]["config"]

    def all_names_and_ids(self):
        return [(record["name"], record["id"]) for record in self.records.values()]

    def get_deck_configs_for_update(self, deck_id):
        return {"fsrs": self.fsrs_enabled}


class FakeScheduler:
    today = 42
    day_cutoff = 1_700_086_400


class FakeCollection:
    sched_ver = 3

    def __init__(self, cards) -> None:
        self.cards = {card.id: card for card in cards}
        self.decks = FakeDecks()
        self.sched = FakeScheduler()

    def get_card(self, card_id):
        return self.cards[int(card_id)]


class AdapterV2Tests(unittest.TestCase):
    def make_adapter(self, collection, profile_provider=None, **kwargs):
        return Anki260801Adapter(
            collection_provider=lambda: collection,
            profile_provider=profile_provider or (lambda: "profile-a"),
            profile_identity="profile-a",
            anki_version="26.08.1",
            anki_build=SUPPORTED_ANKI_BUILD,
            **kwargs,
        )

    def test_exact_version_and_build_gate_fails_closed(self) -> None:
        supported = check_anki_compatibility("26.08.1", SUPPORTED_ANKI_BUILD)
        self.assertTrue(supported.supported)
        self.assertIn("typed_due", supported.capabilities)

        current = check_anki_compatibility("26.9.3", "29bb700b")
        self.assertTrue(current.supported)
        self.assertIn("typed_due", current.capabilities)

        self.assertFalse(check_anki_compatibility("26.08.2", SUPPORTED_ANKI_BUILD).supported)
        self.assertFalse(check_anki_compatibility("26.08.1", "unknown-build").supported)
        self.assertFalse(check_anki_compatibility("26.9.3", SUPPORTED_ANKI_BUILD).supported)
        self.assertFalse(check_anki_compatibility("26.08.1", "29bb700b").supported)
        self.assertFalse(check_anki_compatibility("future", SUPPORTED_ANKI_BUILD).supported)

    def test_queue_matrix_keeps_day_learning_and_unknown_queues_typed(self) -> None:
        cards = [
            FakeCard(1, 10, 0, 7, card_type=0),
            FakeCard(2, 10, 1, 1_700_000_001, card_type=1),
            FakeCard(3, 10, 2, 42, card_type=2),
            FakeCard(4, 10, 3, 43, card_type=1),
            FakeCard(5, 10, 4, 99, card_type=2),
            FakeCard(6, 10, -2, 100, card_type=2),
            FakeCard(7, 10, 99, 100, card_type=2),
        ]
        snapshot = self.make_adapter(FakeCollection(cards)).export_snapshot(
            card_ids=[str(card.id) for card in cards]
        )
        by_id = {card.card_id: card for card in snapshot.cards}
        self.assertEqual(by_id["1"].due.kind, DueKind.NEW_POSITION)
        self.assertEqual(by_id["2"].due.kind, DueKind.LEARNING_TIMESTAMP)
        self.assertEqual(by_id["2"].due.value, 1_700_000_001_000)
        self.assertEqual(by_id["3"].due.kind, DueKind.REVIEW_DAY)
        self.assertEqual(by_id["4"].due.kind, DueKind.DAY_LEARNING_DAY)
        self.assertEqual(by_id["5"].due.kind, DueKind.UNSUPPORTED)
        self.assertEqual(by_id["6"].due.kind, DueKind.UNKNOWN)
        self.assertEqual(by_id["7"].due.kind, DueKind.UNKNOWN)
        self.assertNotEqual(by_id["6"].card_type, "new")
        self.assertTrue(any("preview" in item for item in by_id["5"].unsupported))
        self.assertTrue(any("original_queue" in item for item in by_id["6"].unsupported))

    def test_decks_presets_parent_limits_and_today_counts_are_in_manifest(self) -> None:
        cards = [FakeCard(1, 10, 2, 42), FakeCard(2, 20, 2, 42)]
        snapshot = self.make_adapter(FakeCollection(cards)).export_snapshot(card_ids=["1", "2"])
        self.assertEqual({deck.deck_id for deck in snapshot.decks}, {"10", "20"})
        self.assertEqual({preset.preset_id for preset in snapshot.presets}, {"100", "200"})
        deck = next(deck for deck in snapshot.decks if deck.deck_id == "10")
        self.assertEqual(deck.parent_id, "1")
        self.assertEqual(deck.new_limit, 20)
        self.assertEqual(deck.review_limit, 50)
        self.assertEqual(deck.new_today, 4)
        self.assertEqual(snapshot.day_table.review_day, 42)
        self.assertEqual(snapshot.manifest.baseline_counts["new"], 5)
        self.assertEqual(snapshot.manifest.baseline_counts["review"], 10)

    def make_anki_2693_collection(self, *, fsrs_enabled=True, today=42):
        collection = FakeCollection([FakeCard(1, 10, 2, today)])
        collection.decks.fsrs_enabled = fsrs_enabled
        collection.decks.records[10] = {
            "id": 10,
            "name": "脱敏牌组",
            "parent_id": None,
            "preset_id": 100,
            "preset_name": "脱敏预设",
            "config": {
                "id": 100,
                "name": "脱敏预设",
                "fsrsParams6": [],
                "desiredRetention": 0.9,
                "new": {"perDay": 20, "delays": [1, 10]},
                "rev": {"perDay": 50},
                "lapse": {"delays": [10]},
            },
            "newToday": [today, 4],
            "revToday": [today, 7],
            "lrnToday": [today, 2],
        }
        return collection

    def make_anki_2693_adapter(self, collection):
        return Anki260801Adapter(
            collection_provider=lambda: collection,
            profile_provider=lambda: "profile-a",
            profile_identity="profile-a",
            anki_version="26.9.3",
            anki_build="29bb700b",
        )

    def test_anki_2693_empty_fsrs6_params_and_today_pairs_export_without_unsupported(self) -> None:
        collection = self.make_anki_2693_collection()
        snapshot = self.make_anki_2693_adapter(collection).export_snapshot(card_ids=["1"])

        deck = snapshot.decks[0]
        preset = snapshot.presets[0]
        self.assertEqual(deck.new_today, 4)
        self.assertEqual(deck.review_today, 7)
        self.assertEqual(deck.learning_today, 2)
        self.assertIsNone(deck.learning_limit)
        self.assertNotIn("learning_limit_unavailable", deck.unsupported)
        self.assertEqual(snapshot.day_table.counts["new"], 4)
        self.assertEqual(snapshot.day_table.counts["review"], 7)
        self.assertEqual(snapshot.day_table.counts["learning"], 2)
        self.assertEqual(preset.fsrs_params["version"], 6)
        self.assertEqual(
            preset.fsrs_params["params"],
            (
                0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194,
                0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629,
                1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
            ),
        )
        self.assertEqual(snapshot.manifest.unsupported, ())

    def test_anki_2693_today_pairs_from_prior_day_export_zero_counts(self) -> None:
        collection = self.make_anki_2693_collection(today=42)
        for key in ("newToday", "revToday", "lrnToday"):
            collection.decks.records[10][key][0] = 41

        snapshot = self.make_anki_2693_adapter(collection).export_snapshot(card_ids=["1"])
        deck = snapshot.decks[0]

        self.assertEqual((deck.new_today, deck.review_today, deck.learning_today), (0, 0, 0))
        self.assertEqual(snapshot.day_table.counts["new"], 0)
        self.assertEqual(snapshot.day_table.counts["review"], 0)
        self.assertEqual(snapshot.day_table.counts["learning"], 0)
        self.assertEqual(snapshot.manifest.unsupported, ())

    def test_anki_2693_disabled_global_fsrs_does_not_export_default_fsrs_params(self) -> None:
        collection = self.make_anki_2693_collection(fsrs_enabled=False)
        snapshot = self.make_anki_2693_adapter(collection).export_snapshot(card_ids=["1"])

        preset = snapshot.presets[0]
        self.assertIn("fsrs_disabled", preset.unsupported)
        self.assertIn("fsrs_disabled", snapshot.manifest.unsupported)
        self.assertNotIn("params", preset.fsrs_params)

    def test_snapshot_context_rejects_preset_config_change_during_export(self) -> None:
        collection = self.make_anki_2693_collection()
        config = collection.decks.records[10]["config"]
        config["maxTaken"] = 60
        original_reader = collection.decks.config_dict_for_deck_id
        reads = 0

        def change_on_export_read(deck_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                config["maxTaken"] = 61
            return original_reader(deck_id)

        collection.decks.config_dict_for_deck_id = change_on_export_read
        collection.crt = 1_700_000_000
        collection.fsrs_short_term_with_steps_enabled = False
        collection.get_config = lambda key: {
            "fsrs": True,
            "rollover": 4,
            "localOffset": -480,
            "creationOffset": -480,
        }.get(key)
        marker = {"profile_identity": "profile-a", "generation": 9, "runtime_epoch": "epoch-a"}
        adapter = Anki260801Adapter(
            collection_provider=lambda: collection,
            profile_provider=lambda: marker,
            profile_identity="profile-a",
            anki_version="26.9.3",
            anki_build="29bb700b",
        )

        with self.assertRaises(AnkiAdapterError) as raised:
            adapter.export_snapshot_from_collection(
                collection,
                card_ids=["1"],
                context=("profile-a", 9, "epoch-a"),
            )
        self.assertEqual(raised.exception.code, "snapshot_context_changed")

    def test_media_and_empty_text_are_skipped_without_media_download(self) -> None:
        cards = [
            FakeCard(1, 10, 2, 42, question='<img src="front.png">', answer="答案"),
            FakeCard(2, 10, 2, 42, question="[sound:back.mp3]", answer="答案"),
            FakeCard(3, 10, 2, 42, question="<p> </p>", answer="答案"),
            FakeCard(4, 10, 2, 42, question="题面", answer="<img src='back.png'>"),
        ]
        snapshot = self.make_adapter(FakeCollection(cards)).export_snapshot(
            card_ids=["1", "2", "3", "4"]
        )
        self.assertEqual(snapshot.cards, ())
        reasons = {entry.reason for entry in snapshot.manifest.skipped}
        self.assertIn(SkipReason.MEDIA_REFERENCE, reasons)
        self.assertIn(SkipReason.EMPTY_QUESTION, reasons)
        self.assertGreaterEqual(
            snapshot.manifest.skip_counts.get(SkipReason.MEDIA_REFERENCE.value, 0)
            + snapshot.manifest.skip_counts.get(SkipReason.MEDIA_ONLY.value, 0),
            2,
        )
        self.assertTrue(snapshot.manifest.complete)

    def test_missing_memory_does_not_turn_a_learned_card_into_new(self) -> None:
        card = FakeCard(1, 10, 2, 42, card_type=2, memory=None)
        snapshot = self.make_adapter(FakeCollection([card])).export_snapshot(card_ids=["1"])
        exported = snapshot.cards[0]
        self.assertEqual(exported.queue, 2)
        self.assertEqual(exported.card_type, "review")
        self.assertIsNone(exported.memory_state)
        self.assertIn("learned_memory_missing", exported.unsupported)

    def test_profile_change_while_reading_rejects_publish(self) -> None:
        profile = ["profile-a"]
        collection = FakeCollection([FakeCard(1, 10, 2, 42)])

        def get_card(card_id):
            profile[0] = "profile-b"
            return collection.cards[int(card_id)]

        collection.get_card = get_card
        adapter = self.make_adapter(collection, profile_provider=lambda: profile[0])
        with self.assertRaises(AnkiAdapterError) as raised:
            adapter.export_snapshot(card_ids=["1"])
        self.assertEqual(raised.exception.code, "profile_changed")

    def test_deck_filter_and_unknown_template_are_explicit(self) -> None:
        unknown = FakeCard(3, 20, 2, 42)
        unknown.question = None
        unknown.answer = None
        collection = FakeCollection([FakeCard(1, 10, 2, 42), FakeCard(2, 20, 2, 42), unknown])
        snapshot = self.make_adapter(collection).export_snapshot(card_ids=["1", "2", "3"], deck_ids=["20"])
        self.assertEqual([card.card_id for card in snapshot.cards], ["2"])
        self.assertIn(SkipReason.DECK_FILTERED, {item.reason for item in snapshot.manifest.skipped})
        self.assertIn(SkipReason.UNKNOWN_TEMPLATE, {item.reason for item in snapshot.manifest.skipped})

    def test_parent_deck_scope_keeps_child_deck_cards(self) -> None:
        collection = FakeCollection([FakeCard(1, 1, 2, 42), FakeCard(2, 10, 2, 42)])
        collection.decks.name_if_exists = lambda deck_id: "语言" if deck_id == 1 else None
        collection.find_cards = lambda query, order=False: [1, 2] if query == 'deck:"语言"' else []
        snapshot = self.make_adapter(collection).export_snapshot(deck_ids=["1"])
        self.assertEqual([card.card_id for card in snapshot.cards], ["1", "2"])
        self.assertEqual(snapshot.manifest.skip_counts.get(SkipReason.DECK_FILTERED.value, 0), 0)

    def test_legacy_facade_keeps_dict_and_exposes_typed_difference(self) -> None:
        collection = FakeCollection([FakeCard(1, 10, 2, 42)])
        exporter = AnkiSnapshotExporter(
            collection_provider=lambda: collection,
            profile_provider=lambda: "profile-a",
            profile_principal="profile-a",
            anki_version="26.08.1",
            anki_build=SUPPORTED_ANKI_BUILD,
        )
        legacy = exporter.export(["1"])
        typed = exporter.export_snapshot(["1"])
        self.assertFalse(legacy["complete"])
        self.assertTrue(typed.complete)
        self.assertEqual(typed.cards[0].card_id, "1")

    def test_plain_python_import_does_not_require_aqt(self) -> None:
        import sys

        self.assertNotIn("aqt", sys.modules)


if __name__ == "__main__":
    unittest.main()
