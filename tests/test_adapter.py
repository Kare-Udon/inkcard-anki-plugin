from __future__ import annotations

import unittest

from inkcards_anki_bridge.adapter import (
    AnkiAdapterError,
    AnkiSnapshotExporter,
    require_supported_anki_version,
)


class Memory:
    stability = 4.5
    difficulty = 6.2


class ComputedMemory:
    state = 2
    desired_retention = 0.9
    stability = 4.5
    difficulty = 6.2
    decay = 0.5


class Card:
    id = 7
    nid = 8
    did = 9
    ord = 0
    type = 2
    queue = 2
    due = 123
    ivl = 10
    factor = 2500
    reps = 3
    lapses = 1
    left = 0
    last_review_time = 1234567890
    mod = 42
    memory_state = Memory()
    desired_retention = 0.9
    decay = 0.5


class Decks:
    def config_dict_for_deck_id(self, deck_id):
        return {"id": int(deck_id), "fsrs": {"desiredRetention": 0.9}}


class Collection:
    sched_ver = 2
    decks = Decks()

    def get_card(self, card_id):
        if card_id != 7:
            raise KeyError(card_id)
        return Card()

    def compute_memory_state(self, card_id):
        self.computed_card_id = card_id
        return ComputedMemory()

    def get_review_logs(self, card_id):
        return [{"id": 1, "rating": 3, "card_id": card_id}]

    def v3_scheduler(self):
        return True


class AdapterTests(unittest.TestCase):
    def test_version_is_exactly_locked_to_observed_candidate(self) -> None:
        self.assertEqual(require_supported_anki_version("26.08.1"), "26.8.1")
        with self.assertRaises(AnkiAdapterError) as context:
            require_supported_anki_version("26.8.2")
        self.assertEqual(context.exception.code, "unsupported_anki_version")

    def test_snapshot_is_read_only_and_preserves_known_scheduler_fields(self) -> None:
        collection = Collection()
        exporter = AnkiSnapshotExporter(
            collection_provider=lambda: collection,
            profile_provider=lambda: "Profile A",
            profile_principal="Profile A",
            anki_version="26.8.1",
        )
        snapshot = exporter.export(["7"])
        card = snapshot["cards"][0]
        self.assertEqual(card["card_id"], "7")
        self.assertEqual(card["due"], {"kind": "review_day", "value": 123})
        self.assertEqual(card["memory_state"]["stability"], 4.5)
        self.assertEqual(card["memory_state"]["computed_by"], "Collection.compute_memory_state")
        self.assertEqual(collection.computed_card_id, 7)
        self.assertFalse(snapshot["complete"])
        self.assertTrue(any("historical event application" in item for item in snapshot["limitations"]))

    def test_profile_change_is_not_read_as_the_new_profile(self) -> None:
        exporter = AnkiSnapshotExporter(
            collection_provider=lambda: Collection(),
            profile_provider=lambda: "Profile B",
            profile_principal="Profile A",
            anki_version="26.8.1",
        )
        with self.assertRaises(AnkiAdapterError) as context:
            exporter.export(["7"])
        self.assertEqual(context.exception.code, "profile_changed")


if __name__ == "__main__":
    unittest.main()
