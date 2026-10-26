"""版本间隙的原生评分回归；只打开本测试创建的临时合成库。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_compact_service import (
    DEVICE,
    PROFILE,
    SESSION,
    SyntheticFixture,
    count_active_answers,
)
from inkcards_anki_bridge.services.compact_application import CompactApplicationError
from inkcards_anki_bridge.storage import ReceiptRegressionError


class GenerationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-", dir="/private/tmp"
        )
        self.f = SyntheticFixture(Path(self.tmp.name))

    def tearDown(self):
        self.f.close()
        self.tmp.cleanup()

    def pending(self, rows=None):
        snapshot = self.f.publish_production_snapshot()
        self.f.receive(snapshot, rows or [[1, 3, self.f.times()[0], 120]])
        self.f.store.set_collection_generation(1)
        return snapshot

    def test_unchanged_gap_applies_once_and_keeps_event_identity(self):
        self.pending()
        event = self.f.store.query_compact_events(DEVICE, SESSION)[0]
        with count_active_answers(self.f) as calls:
            result = self.f.apply_one()
            self.assertEqual(result.status, "applied", result.to_dict())
            again = self.f.service.recover_pending(
                self.f.collection,
                DEVICE,
                SESSION,
                collection_identity=PROFILE,
                expected_generation=2,
            )
        self.assertEqual((calls[0], again.status), (1, "idle"))
        self.assertEqual(self.f.store.query_compact_events(DEVICE, SESSION)[0], event)
        app = self.f.store.query_compact_applications(DEVICE, SESSION)[0]
        proof = json.loads(
            json.loads(app.policy_json)["authorization"]["baseline_proof_json"]
        )
        self.assertEqual(proof["gaps"], [[0, 1]])
        self.assertEqual(
            (app.collection_generation, app.collection_generation_after), (1, 2)
        )

    def test_same_card_receipts_and_later_gap_survive_reopen(self):
        times = self.f.times()
        snapshot = self.f.publish_production_snapshot()
        self.f.receive(snapshot, [[1, 3, times[0], 120], [1, 4, times[1], 130]])
        self.assertEqual(self.f.apply_one().status, "applied")
        self.f.store.set_collection_generation(2)
        with count_active_answers(self.f) as calls:
            result = self.f.apply_one()
        self.assertEqual(result.status, "applied", result.to_dict())
        self.assertEqual(calls[0], 1)
        self.f.close()
        self.f = SyntheticFixture(Path(self.tmp.name), create=False)
        with count_active_answers(self.f, forbid=True):
            result = self.f.service.recover_pending(
                self.f.collection,
                DEVICE,
                SESSION,
                collection_identity=PROFILE,
                expected_generation=3,
            )
        self.assertEqual(result.status, "idle")
        self.assertTrue(self.f.store.pull_gate(DEVICE, SESSION).allowed)

    def test_changed_state_still_rejects_before_intent(self):
        self.pending()
        card = self.f.collection.get_card(self.f.meta["card_ids"][0])
        card.ivl += 1
        self.f.collection.update_card(card)
        before = self.f.native_frame()
        with count_active_answers(self.f, forbid=True):
            with self.assertRaises(CompactApplicationError):
                self.f.apply_one()
        self.assertEqual(self.f.native_frame(), before)
        self.assertIsNone(
            self.f.store.query_compact_applications(DEVICE, SESSION)[0].intent_id
        )

    def test_store_rejects_forged_or_missing_gap_proof(self):
        self.pending()
        original = self.f.store.begin_compact_apply
        for tamper in ("missing", "generation", "counters", "receipts"):
            with self.subTest(tamper=tamper):

                def forged(*args, **kwargs):
                    kwargs = copy.deepcopy(kwargs)
                    policy = kwargs["plan_json"]["policy"]
                    proof = json.loads(policy["baseline_proof_json"])
                    if tamper == "missing":
                        policy.pop("baseline_proof_json")
                    else:
                        if tamper == "generation":
                            proof["to_generation"] += 1
                        elif tamper == "counters":
                            proof["counters_sha256"] = "f" * 64
                        else:
                            proof["receipts"] = ["f" * 64]
                        policy["baseline_proof_json"] = json.dumps(proof)
                    kwargs["policy_json"]["authorization"] = policy
                    return original(*args, **kwargs)

                with patch.object(
                    self.f.store, "begin_compact_apply", side_effect=forged
                ):
                    with count_active_answers(self.f, forbid=True):
                        with self.assertRaises(ReceiptRegressionError):
                            self.f.apply_one()
                self.assertIsNone(
                    self.f.store.query_compact_applications(DEVICE, SESSION)[
                        0
                    ].intent_id
                )

    def test_gap_authorization_is_invalidated_by_later_native_change(self):
        self.pending()
        auth = self.f.service.authorize_next(
            self.f.collection,
            DEVICE,
            SESSION,
            collection_identity=PROFILE,
            expected_generation=1,
        )
        card = self.f.collection.get_card(self.f.meta["card_ids"][0])
        card.ivl += 1
        self.f.collection.update_card(card)
        before = self.f.native_frame()
        with count_active_answers(self.f, forbid=True):
            result = self.f.service.apply_pending(
                self.f.collection,
                DEVICE,
                SESSION,
                collection_identity=PROFILE,
                expected_generation=1,
                authorization=auth,
            )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.f.native_frame(), before)
        self.assertIsNone(
            self.f.store.query_compact_applications(DEVICE, SESSION)[0].intent_id
        )

    def test_two_devices_and_interleaved_receipts_keep_shared_counters(self):
        snapshot = self.pending()
        self.f.store.register_device("device-b", "token-b")
        self.f.store.open_session("device-b", "session-b")
        self.f.store.receive_compact_batch(
            "device-b", "session-b", snapshot, 1, [[2, 3, self.f.times()[1], 120]]
        )
        auth = self.f.service.authorize_next(
            self.f.collection,
            "device-b",
            "session-b",
            collection_identity=PROFILE,
            expected_generation=1,
        )
        result = self.f.service.apply_pending(
            self.f.collection,
            "device-b",
            "session-b",
            collection_identity=PROFILE,
            expected_generation=1,
            authorization=auth,
        )
        self.assertEqual(result.status, "applied", result.to_dict())
        with count_active_answers(self.f) as calls:
            result = self.f.apply_one()
        self.assertEqual((result.status, calls[0]), ("applied", 1), result.to_dict())
        self.assertEqual(self.f.store.get_collection_generation(), 3)
        self.assertTrue(self.f.store.pull_gate(DEVICE, SESSION).allowed)

    def test_committed_native_score_without_receipt_recovers_without_rescoring(self):
        self.pending()
        with patch.object(
            self.f.store,
            "finish_compact_apply",
            side_effect=OSError("test interruption"),
        ):
            with count_active_answers(self.f) as calls:
                result = self.f.apply_one()
        self.assertEqual((result.status, calls[0]), ("unknown", 1))
        self.f.close()
        self.f = SyntheticFixture(Path(self.tmp.name), create=False)
        with count_active_answers(self.f, forbid=True):
            result = self.f.service.recover_pending(
                self.f.collection,
                DEVICE,
                SESSION,
                collection_identity=PROFILE,
                expected_generation=1,
            )
        self.assertEqual(result.status, "applied", result.to_dict())
        self.assertEqual(self.f.store.get_collection_generation(), 2)

    def test_gap_never_hides_real_counter_or_preset_changes(self):
        self.pending()
        for change in ("counter", "preset"):
            with self.subTest(change=change):
                if change == "counter":
                    deck = self.f.collection._backend.get_deck(self.f.meta["parent"])
                    deck.common.review_studied += 1
                    self.f.collection._backend.update_deck(deck)
                else:
                    config = self.f.collection.decks.config_dict_for_deck_id(
                        self.f.meta["deck_ids"][0]
                    )
                    config["rev"]["maxIvl"] = 1234
                    self.f.collection.decks.save(config)
                before = self.f.native_frame()
                with count_active_answers(self.f, forbid=True):
                    with self.assertRaises(CompactApplicationError):
                        self.f.apply_one()
                self.assertEqual(self.f.native_frame(), before)

    def test_cached_last_review_time_does_not_hide_history_only_change(self):
        self.pending()
        cid = self.f.meta["card_ids"][0]
        self.f.collection.db.execute(
            "INSERT INTO revlog(id,cid,usn,ease,ivl,lastIvl,factor,time,type) VALUES(?,?,?,?,?,?,?,?,?)",
            self.f.times()[0] - 1000,
            cid,
            -1,
            3,
            7,
            7,
            2500,
            100,
            1,
        )
        before = self.f.native_frame()
        with count_active_answers(self.f, forbid=True):
            with self.assertRaises(CompactApplicationError):
                self.f.apply_one()
        self.assertEqual(self.f.native_frame(), before)
