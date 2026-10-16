from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

# Anki's bundled packages require importing Collection before other anki modules.
import anki.collection  # type: ignore[import-not-found]  # noqa: F401
from anki.collection import Collection  # type: ignore[import-not-found]

from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.adapter import AnkiAdapterError
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import ProfileStore


class NativeSnapshotContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="inkcards-snapshot-context-", dir="/private/tmp"
        )
        self.root = Path(self.tempdir.name)
        self.collection = Collection(str(self.root / "synthetic.anki2"))
        self.collection.set_config("fsrs", True)
        root_change = self.collection.decks.add_normal_deck_with_name(
            "Context hierarchy"
        )
        child_change = self.collection.decks.add_normal_deck_with_name(
            "Context hierarchy::Child"
        )
        grandchild_change = self.collection.decks.add_normal_deck_with_name(
            "Context hierarchy::Child::Grandchild"
        )
        second_deck_change = self.collection.decks.add_normal_deck_with_name(
            "Context second deck"
        )
        self.root_deck_id = int(root_change.id)
        self.child_deck_id = int(child_change.id)
        self.grandchild_deck_id = int(grandchild_change.id)
        self.second_deck_id = int(second_deck_change.id)
        second_config = self.collection.decks.add_config("Context second preset")
        second_config["maxTaken"] = 75
        second_config["desiredRetention"] = 0.85
        self.collection.decks.update_config(second_config)
        for deck_id in (self.grandchild_deck_id, self.second_deck_id):
            deck = self.collection.decks.get(deck_id)
            assert deck is not None
            self.collection.decks.set_config_id_for_deck_dict(deck, int(second_config["id"]))
            self.collection.decks.save(deck)
        self.deck_ids = [
            self.root_deck_id,
            self.child_deck_id,
            self.grandchild_deck_id,
            self.second_deck_id,
        ]
        for index, deck_id in enumerate(self.deck_ids, start=1):
            deck = self.collection.decks.get(deck_id)
            today = int(self.collection.sched.today)
            deck["newToday"] = [today, index]
            deck["revToday"] = [today, index + 10]
            deck["lrnToday"] = [today, index + 20]
            deck["timeToday"] = [today, index * 1000]
            self.collection.decks.save(deck)
        self.adapter = Anki260801Adapter(
            collection_provider=lambda: self.collection,
            profile_provider=lambda: {
                "profile_identity": "profile-native",
                "generation": 9,
                "runtime_epoch": "epoch-native",
            },
            profile_identity="profile-native",
            anki_version="26.09.3",
            anki_build="29bb700b",
        )

    def tearDown(self) -> None:
        self.collection.close()
        self.tempdir.cleanup()

    def _add_cards(self, count: int) -> list[str]:
        note_type = self.collection.models.by_name("Basic")
        card_ids: list[str] = []
        for index in range(count):
            note = self.collection.new_note(note_type)
            note.fields[0] = f"Context question {index:03d}"
            if len(note.fields) > 1:
                note.fields[1] = f"Context answer {index:03d}"
            deck_id = self.deck_ids[index % len(self.deck_ids)]
            self.collection.add_note(note, deck_id)
            card_ids.extend(str(card_id) for card_id in self.collection.card_ids_of_note(note.id))
        return card_ids

    def _page_rows(self, store: ProfileStore, snapshot_id: str):
        with closing(store.connect()) as db:
            return db.execute(
                "SELECT page_index,page_json,page_hash,page_bytes,cursor,is_last "
                "FROM snapshot_pages WHERE profile_id=? AND snapshot_id=? ORDER BY page_index",
                (store.profile_id, snapshot_id),
            ).fetchall()

    def test_production_export_publishes_bound_context_without_changing_150_card_wire(self) -> None:
        card_ids = self._add_cards(150)
        typed = self.adapter.export_snapshot_from_collection(
            self.collection,
            card_ids=card_ids,
            context=("profile-native", 9, "epoch-native"),
        )
        self.assertEqual(len(typed.cards), 150)
        self.assertEqual(len(typed.presets), 2)
        context = typed.application_context
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.profile_identity, "profile-native")
        self.assertEqual(context.collection_generation, 9)
        self.assertEqual(context.anki_version, "26.9.3")
        self.assertEqual(context.anki_build, "29bb700b")
        self.assertIs(context.fsrs_enabled, True)
        self.assertIn("1", context.preset_configs)
        self.assertIn(str(typed.decks[0].preset_id), context.preset_configs)
        self.assertEqual(context.preset_configs["1"]["fsrsParams6"], ())
        self.assertEqual(
            context.preset_parameter_identities["1"],
            "anki-native-default:26.9.3:29bb700b",
        )
        self.assertEqual(context.preset_configs["1"]["maxTaken"], 60)
        self.assertIn("Collection.decks.config_dict_for_deck_id", context.source_summary.values())
        self.assertIn("Collection.decks.parents", context.source_summary.values())
        self.assertIn("Collection._backend.get_deck().common", context.source_summary.values())
        root_id = str(self.root_deck_id)
        child_id = str(self.child_deck_id)
        grandchild_id = str(self.grandchild_deck_id)
        self.assertEqual(context.deck_ancestor_ids[root_id], ())
        self.assertEqual(context.deck_ancestor_ids[child_id], (root_id,))
        self.assertEqual(context.deck_ancestor_ids[grandchild_id], (child_id, root_id))
        self.assertIn(root_id, context.deck_contexts)
        self.assertIsNone(context.deck_contexts[root_id]["parent_id"])
        self.assertEqual(context.deck_contexts[child_id]["parent_id"], root_id)
        self.assertEqual(context.deck_contexts[grandchild_id]["parent_id"], child_id)
        self.assertNotEqual(
            context.deck_preset_ids[root_id],
            context.deck_preset_ids[grandchild_id],
        )
        second_preset = context.preset_configs[context.deck_preset_ids[grandchild_id]]
        self.assertEqual(second_preset["maxTaken"], 75)
        self.assertAlmostEqual(second_preset["desiredRetention"], 0.85)
        self.assertNotEqual(context.preset_configs["1"]["maxTaken"], second_preset["maxTaken"])
        for deck_context in context.deck_contexts.values():
            native_common = self.collection._backend.get_deck(int(deck_context["deck_id"])).common
            self.assertEqual(
                tuple(deck_context[field] for field in (
                    "last_day_studied",
                    "new_studied",
                    "review_studied",
                    "learning_studied",
                    "milliseconds_studied",
                )),
                tuple(getattr(native_common, field) for field in (
                    "last_day_studied", "new_studied", "review_studied",
                    "learning_studied", "milliseconds_studied",
                )),
            )
            self.assertGreater(deck_context["new_studied"], 0)

        with_context = ProfileStore(self.root / "with-context", profile_id="profile-native")
        wire_only = ProfileStore(self.root / "wire-only", profile_id="profile-native")
        try:
            scoped_id = "cards:150-two-presets"
            published = SnapshotService(with_context).publish(
                typed,
                scope_id=scoped_id,
                generation=9,
                applied_watermark=0,
            )
            baseline = SnapshotService(wire_only).publish(
                typed.to_dict(),
                scope_id=scoped_id,
                generation=9,
                applied_watermark=0,
            )
            record = with_context.get_snapshot(published.snapshot_id)
            self.assertIsNotNone(record.application_context)
            payload = json.loads(published.payload_bytes)
            self.assertNotIn("application_context", payload)
            self.assertEqual(published.snapshot_id, baseline.snapshot_id)
            self.assertEqual(published.checksum, baseline.checksum)
            self.assertEqual(published.payload_bytes, baseline.payload_bytes)
            self.assertEqual(len(payload["cards"]), 150)
            context_pages = self._page_rows(with_context, published.snapshot_id)
            baseline_pages = self._page_rows(wire_only, baseline.snapshot_id)
            self.assertEqual(len(context_pages), published.manifest["page_count"])
            self.assertEqual(len(context_pages), len(baseline_pages))
            self.assertEqual(
                [tuple(row) for row in context_pages],
                [tuple(row) for row in baseline_pages],
            )
            self.assertEqual(record.application_context.snapshot_checksum, published.checksum)
            self.assertEqual(record.application_context.snapshot_generation, 9)
            self.assertEqual(record.application_context.collection_generation, 9)
            print("电脑旁路 UTF-8 字节数:", len(record.application_context.to_json().encode("utf-8")))
            print("设备 payload 字节数 / 页数 / 卡数:", len(published.payload_bytes), len(context_pages), len(payload["cards"]))
        finally:
            with_context.close()
            wire_only.close()

    def test_deck_rename_during_export_refuses_context_publish(self) -> None:
        card_ids = self._add_cards(1)
        backend = self.collection._backend
        original_get_deck = backend.get_deck
        reads = 0

        def renamed_on_second_capture(deck_id):
            nonlocal reads
            reads += 1
            deck = original_get_deck(deck_id)
            if reads == 2 and int(deck_id) == self.root_deck_id:
                deck.name = "Context hierarchy renamed"
            return deck

        backend.get_deck = renamed_on_second_capture
        try:
            with self.assertRaises(AnkiAdapterError) as raised:
                self.adapter.export_snapshot_from_collection(
                    self.collection,
                    card_ids=card_ids,
                    context=("profile-native", 9, "epoch-native"),
                )
        finally:
            backend.get_deck = original_get_deck
        self.assertEqual(raised.exception.code, "snapshot_context_changed")

    def test_grandchild_only_export_retains_ancestors_without_wire_decks(self) -> None:
        self.deck_ids = [self.grandchild_deck_id]
        card_ids = self._add_cards(1)
        typed = self.adapter.export_snapshot_from_collection(
            self.collection, card_ids=card_ids, context=("profile-native", 9, "epoch-native")
        )
        context = typed.application_context
        root, child, grandchild = map(str, (self.root_deck_id, self.child_deck_id, self.grandchild_deck_id))
        self.assertEqual(set(context.deck_ancestor_ids), {grandchild})
        self.assertEqual(context.deck_ancestor_ids[grandchild], (child, root))
        self.assertEqual(set(context.deck_contexts), {root, child, grandchild})
        self.assertIsNone(context.deck_contexts[root]["parent_id"])
        self.assertEqual({deck.deck_id for deck in typed.decks}, {grandchild})

    def test_common_counter_change_during_export_is_rejected(self) -> None:
        card_ids = self._add_cards(1)
        original_get_deck = self.collection._backend.get_deck
        reads = 0

        def changed_counter(deck_id):
            nonlocal reads
            reads += 1
            deck = original_get_deck(deck_id)
            if reads == 2:
                deck.common.milliseconds_studied += 1
            return deck

        self.collection._backend.get_deck = changed_counter
        try:
            with self.assertRaises(AnkiAdapterError) as raised:
                self.adapter.export_snapshot_from_collection(
                    self.collection, card_ids=card_ids, context=("profile-native", 9, "epoch-native")
                )
        finally:
            self.collection._backend.get_deck = original_get_deck
        self.assertEqual(raised.exception.code, "snapshot_context_changed")

    def test_replaced_deck_id_during_export_refuses_context_publish(self) -> None:
        card_ids = self._add_cards(1)
        backend = self.collection._backend
        original_get_deck = backend.get_deck
        reads = 0

        def replaced_on_second_capture(deck_id):
            nonlocal reads
            reads += 1
            deck = original_get_deck(deck_id)
            if reads == 2 and int(deck_id) == self.root_deck_id:
                deck.id += 100000
            return deck

        backend.get_deck = replaced_on_second_capture
        try:
            with self.assertRaises(AnkiAdapterError) as raised:
                self.adapter.export_snapshot_from_collection(
                    self.collection,
                    card_ids=card_ids,
                    context=("profile-native", 9, "epoch-native"),
                )
        finally:
            backend.get_deck = original_get_deck
        self.assertEqual(raised.exception.code, "snapshot_context_changed")

if __name__ == "__main__":
    unittest.main()
