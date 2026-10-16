"""生产四态能力的最小 RED；只使用合成原生库。"""

import tempfile
import json
from pathlib import Path
import unittest

from test_compact_service import SyntheticFixture, count_active_answers
from inkcards_anki_bridge.anki.compact_projection import map_rating


class CompactFourStateTests(unittest.TestCase):
    def test_store_rejects_caller_forged_frozen_baseline_before_intent(self):
        import copy
        from inkcards_anki_bridge.storage import ReceiptRegressionError

        for tamper in ("snapshot_generation", "application_context_sha256", "card"):
            with (
                self.subTest(tamper=tamper),
                tempfile.TemporaryDirectory(
                    prefix="inkcards-native-service-", dir="/private/tmp"
                ) as name,
            ):
                f = SyntheticFixture(Path(name))
                try:
                    snapshot = f.publish_production_snapshot()
                    f.receive(snapshot, [[1, 3, f.times()[0], 120]])
                    original = f.store.begin_compact_apply

                    def forged(*args, **kwargs):
                        kwargs = copy.deepcopy(kwargs)
                        plan = kwargs["plan_json"]
                        if tamper == "card":
                            plan["before"]["card"]["reps"] += 1
                        else:
                            plan[tamper] = (
                                99 if tamper == "snapshot_generation" else "f" * 64
                            )
                        return original(*args, **kwargs)

                    f.store.begin_compact_apply = forged
                    before = f.native_frame()
                    with count_active_answers(f, forbid=True) as calls:
                        with self.assertRaises(ReceiptRegressionError) as caught:
                            f.apply_one()
                    self.assertEqual(
                        caught.exception.code, "compact_snapshot_baseline_invalid"
                    )
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(f.native_frame(), before)
                    self.assertIsNone(
                        f.store.query_compact_applications(
                            "synthetic-device", "synthetic-session"
                        )[0].intent_id
                    )
                finally:
                    f.close()

    def test_external_baseline_rejects_stale_snapshot_or_bad_historical_receipt(self):
        from inkcards_anki_bridge.services.compact_application import (
            CompactApplicationError,
        )

        for tamper in ("old_snapshot", "receipt_hash", "foreign_marker"):
            with (
                self.subTest(tamper=tamper),
                tempfile.TemporaryDirectory(
                    prefix="inkcards-native-service-", dir="/private/tmp"
                ) as name,
            ):
                f = SyntheticFixture(Path(name))
                try:
                    old = f.publish_production_snapshot()
                    f.receive(old, [[1, 3, f.times()[0], 120]])
                    self.assertEqual(f.apply_one().status, "applied")
                    f.collection.undo()
                    f.store.set_collection_generation(2)
                    if tamper == "foreign_marker":
                        card = f.collection.get_card(f.meta["card_ids"][0])
                        card.custom_data = json.dumps({"inkcards": "f" * 64})
                        f.collection.update_card(card)
                    fresh = f.publish_production_snapshot()
                    if tamper == "receipt_hash":
                        db = f.store.connect()
                        try:
                            db.execute(
                                "UPDATE compact_applications SET receipt_hash=? WHERE session_id=?",
                                ("f" * 64, "synthetic-session"),
                            )
                            db.commit()
                        finally:
                            db.close()
                    f.store.open_session("synthetic-device", "blocked-external")
                    f.receive(
                        old if tamper == "old_snapshot" else fresh,
                        [[1, 3, f.times()[2], 120]],
                        session="blocked-external",
                    )
                    before = f.native_frame()
                    with count_active_answers(f, forbid=True) as calls:
                        with self.assertRaises(CompactApplicationError):
                            f.apply_one(session="blocked-external")
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(f.native_frame(), before)
                    self.assertIsNone(
                        f.store.query_compact_applications(
                            "synthetic-device", "blocked-external"
                        )[0].intent_id
                    )
                    self.assertEqual(
                        f.store.query_compact_applications(
                            "synthetic-device", "synthetic-session"
                        )[0].status,
                        "applied",
                    )
                finally:
                    f.close()

    def test_fresh_snapshot_after_external_undo_or_score_is_a_new_baseline(self):
        from anki.scheduler_pb2 import CardAnswer

        for operation in ("undo", "score"):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory(
                    prefix="inkcards-native-service-", dir="/private/tmp"
                ) as name,
            ):
                f = SyntheticFixture(Path(name))
                try:
                    snapshot = f.publish_production_snapshot()
                    f.receive(snapshot, [[1, 3, f.times()[0], 120]])
                    self.assertEqual(f.apply_one().status, "applied")
                    previous = f.store.query_compact_applications(
                        "synthetic-device", "synthetic-session"
                    )[0]
                    if operation == "undo":
                        f.collection.undo()
                    else:
                        cid = f.meta["card_ids"][0]
                        states = f.collection._backend.get_scheduling_states(cid)
                        f.collection.sched.answer_card(
                            CardAnswer(
                                card_id=cid,
                                current_state=states.current,
                                new_state=states.good,
                                rating=2,
                                answered_at_millis=f.times()[1],
                                milliseconds_taken=120,
                            )
                        )
                    f.store.set_collection_generation(2)
                    fresh = f.publish_production_snapshot()
                    f.store.open_session("synthetic-device", "after-external")
                    f.receive(
                        fresh, [[1, 3, f.times()[2], 120]], session="after-external"
                    )
                    with count_active_answers(f) as calls:
                        result = f.apply_one(session="after-external")
                    self.assertEqual(result.status, "applied", result.to_dict())
                    self.assertEqual(calls[0], 1)
                    self.assertEqual(f.store.get_collection_generation(), 3)
                    self.assertEqual(
                        f.store.query_compact_applications(
                            "synthetic-device", "synthetic-session"
                        )[0],
                        previous,
                    )
                finally:
                    f.close()

    def test_product_marker_capacity_rejects_before_native_answer_and_preserves_undo(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-", dir="/private/tmp"
        ) as name:
            f = SyntheticFixture(Path(name))
            try:
                card = f.collection.get_card(f.meta["card_ids"][0])
                card.custom_data = json.dumps({"other": "x" * 50})
                f.collection.update_card(card)
                snapshot = f.publish_production_snapshot()
                f.receive(snapshot, [[1, 3, f.times()[0], 120]])
                before = f.native_frame()
                self.assertTrue(before["undo"])
                with count_active_answers(f, forbid=True) as calls:
                    result = f.apply_one()
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.reason, "custom_data_too_large")
                self.assertEqual(calls[0], 0)
                self.assertEqual(f.native_frame(), before)
                self.assertIsNone(
                    f.store.query_compact_applications(
                        "synthetic-device", "synthetic-session"
                    )[0].intent_id
                )
            finally:
                f.close()

    def test_device_unsupported_short_term_and_empty_steps_are_explicit(self):
        from anki.collection import Config

        for mode, expected in (
            ("short_term", "fsrs_short_term_with_steps_device_unsupported"),
            ("empty_learning", "fsrs_empty_learning_steps_device_unsupported"),
            ("empty_relearning", "fsrs_empty_relearning_steps_device_unsupported"),
        ):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory(
                    prefix="inkcards-native-service-", dir="/private/tmp"
                ) as name,
            ):
                f = SyntheticFixture(Path(name))
                try:
                    f.collection.set_config_bool(
                        Config.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED,
                        mode == "short_term",
                    )
                    config = f.collection.decks.config_dict_for_deck_id(
                        f.meta["deck_ids"][0]
                    )
                    if mode == "empty_learning":
                        config["new"]["delays"] = []
                    if mode == "empty_relearning":
                        config["lapse"]["delays"] = []
                    f.collection.decks.save(config)
                    typed = f.export_snapshot_from_query_context()
                    preset = next(
                        p for p in typed.presets if p.preset_id == str(config["id"])
                    )
                    self.assertIn(expected, preset.unsupported)
                    self.assertIn(expected, typed.manifest.unsupported)
                    from inkcards_anki_bridge.anki.adapter_260801 import (
                        Anki260801Adapter,
                    )
                    from inkcards_anki_bridge.anki.catalog_jobs import (
                        build_deck_preflight,
                        CatalogJobError,
                    )

                    adapter = Anki260801Adapter(
                        collection_provider=lambda: f.collection,
                        profile_provider=lambda: "synthetic-native-profile",
                        profile_identity="synthetic-native-profile",
                    )
                    before = f.native_frame()
                    with self.assertRaises(CatalogJobError) as caught:
                        build_deck_preflight(
                            f.collection, adapter, deck_ids=[str(f.meta["deck_ids"][0])]
                        )
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(f.native_frame(), before)
                finally:
                    f.close()

    def test_actual_export_preserves_nondefault_maximum_interval(self):
        with tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-", dir="/private/tmp"
        ) as name:
            f = SyntheticFixture(Path(name))
            try:
                config = f.collection.decks.config_dict_for_deck_id(
                    f.meta["deck_ids"][0]
                )
                config["rev"]["maxIvl"] = 7
                f.collection.decks.save(config)
                snapshot = f.publish_production_snapshot()
                preset_id = str(config["id"])
                presets = json.loads(f.store.get_snapshot(snapshot).payload_json)[
                    "presets"
                ]
                actual = next(p for p in presets if p["preset_id"] == preset_id)
                self.assertEqual(actual.get("max_interval_days"), 7)
                self.assertEqual(actual.get("learning_steps_unit"), "minutes")
                self.assertEqual(actual["learning_steps"], [1.0, 10.0])
            finally:
                f.close()

    def test_again_is_a_native_rating(self):
        self.assertEqual(map_rating(1), (0, "again"))

    def test_new_again_goes_through_real_snapshot_store_and_native(self):
        with tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-", dir="/private/tmp"
        ) as name:
            f = SyntheticFixture(Path(name))
            try:
                card = f.collection.get_card(f.meta["card_ids"][0])
                card.type = 0
                card.queue = 0
                card.due = 1
                card.ivl = 0
                card.reps = 0
                card.left = 0
                card.last_review_time = 0
                card.memory_state = None
                f.collection.update_card(card)
                snapshot = f.publish_production_snapshot()
                f.receive(snapshot, [[1, 1, f.times()[0], 120]])
                with count_active_answers(f) as calls:
                    result = f.apply_one()
                self.assertEqual(result.status, "applied", result.to_dict())
                self.assertEqual(calls[0], 1)
                self.assertEqual(
                    f.store.pull_gate(
                        "synthetic-device", "synthetic-session"
                    ).applied_watermark,
                    1,
                )
                after = f.collection.get_card(card.id)
                self.assertEqual((after.type, after.queue), (1, 1))
                self.assertEqual(after.reps, 1)
                event = f.store.query_compact_events(
                    "synthetic-device", "synthetic-session"
                )[0]
                application = f.store.query_compact_applications(
                    "synthetic-device", "synthetic-session"
                )[0]
                restored = f.adapter.restore_prepared_event(
                    f.collection, event, application
                )
                self.assertEqual(restored.plan, json.loads(application.plan_json))
            finally:
                f.close()

    def test_unproven_learning_cutoff_is_blocked_before_intent(self):
        with tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-", dir="/private/tmp"
        ) as name:
            f = SyntheticFixture(Path(name))
            try:
                card = f.collection.get_card(f.meta["card_ids"][0])
                card.type = 0
                card.queue = 0
                card.due = 1
                card.ivl = 0
                card.reps = 0
                card.left = 0
                card.last_review_time = 0
                card.memory_state = None
                f.collection.update_card(card)
                snapshot = f.publish_production_snapshot()
                event = (int(f.collection.sched.day_cutoff) - 86400 - 5) * 1000
                f.receive(snapshot, [[1, 1, event, 120]])
                before = f.native_frame()
                with count_active_answers(f, forbid=True) as calls:
                    result = f.apply_one()
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.reason, "event_cutoff_transfer_unsupported")
                self.assertEqual(calls[0], 0)
                self.assertEqual(f.native_frame(), before)
                self.assertIsNone(
                    f.store.query_compact_applications(
                        "synthetic-device", "synthetic-session"
                    )[0].intent_id
                )
            finally:
                f.close()


if __name__ == "__main__":
    unittest.main()
