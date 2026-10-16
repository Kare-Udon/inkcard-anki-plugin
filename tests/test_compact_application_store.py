from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from inkcards_anki_bridge.domain.sync_gate import ImmutableReviewEvent
from inkcards_anki_bridge.storage import BusyStorageError, CommitUnknownError, ProfileMismatchError, ProfileStore, ReceiptRegressionError, SchemaError, SequenceError, UnknownApplyError, UnsupportedSchemaError
from inkcards_anki_bridge.storage import migrations
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter
from inkcards_anki_bridge.services.inbox import InboxService


@contextmanager
def sqlite_connection(source: Path | sqlite3.Connection):
    connection = source if isinstance(source, sqlite3.Connection) else sqlite3.connect(source)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def snapshot(store: ProfileStore) -> str:
    return SnapshotService(store).publish({
        "snapshot_schema": "candidate.v1", "manifest": {}, "decks": [],
        "presets": [], "day_table": {}, "complete": True, "unsupported": [],
        "cards": [{"card_id": "card-one"}],
    }, scope_id="all", generation=0, applied_watermark=0).snapshot_id


def before(revision: int = 0) -> dict:
    return {"card_id": "card-one", "revision": revision}


def after(revision: int = 1, phase: str = "scored") -> dict:
    return {"card_id": "card-one", "revision": revision, "phase": phase}


def plan(*, due: bool = False) -> dict:
    return {
        "required_evidence": ["score_applied", "due_corrected"] if due else ["score_applied"],
        "expected_score_after": {"rev": 1, "phase": "scored"},
    }


def counters(review_count: int) -> dict:
    return scoped_counters({"1": review_count})


def scoped_counters(deck_review_counts: dict[str, int]) -> dict:
    return {
        deck_id: {
            "review_studied": review_count,
            "milliseconds_studied": review_count * 100,
            "last_day_studied": review_count,
        }
        for deck_id, review_count in deck_review_counts.items()
    }


def full_frame(card_id: str, revision: int, review_logs: list[dict], review_count: int) -> dict:
    return {
        "card": {"card_id": card_id, "revision": revision},
        "review_logs": review_logs,
        "counters": counters(review_count),
    }


def score_after_frame(card_id: str, revision: int, score_log: dict, review_log_count: int,
                      review_count: int) -> dict:
    return {
        "card": {"card_id": card_id, "revision": revision},
        "review_log_count": review_log_count,
        "score_review_log": score_log,
        "counters": counters(review_count),
    }


def full_frame_with_counters(card_id: str, revision: int, review_logs: list[dict],
                             deck_review_counts: dict[str, int], *, deck_id: str) -> dict:
    frame = full_frame(card_id, revision, review_logs, 0)
    frame["card"]["deck_id"] = deck_id
    frame["counters"] = scoped_counters(deck_review_counts)
    return frame


def score_after_frame_with_counters(card_id: str, revision: int, score_log: dict, review_log_count: int,
                                    deck_review_counts: dict[str, int], *, deck_id: str) -> dict:
    frame = score_after_frame(card_id, revision, score_log, review_log_count, 0)
    frame["card"]["deck_id"] = deck_id
    frame["counters"] = scoped_counters(deck_review_counts)
    return frame


def full_plan(expected_after: dict) -> dict:
    return {
        "required_evidence": ["score_applied"],
        "expected_score_after": expected_after,
        "expected_final_counters": expected_after["counters"],
    }


def proof(app) -> dict:
    return {
        "profile_id": app.profile_id, "device_id": app.device_id,
        "session_id": app.session_id, "sequence": app.sequence,
        "event_id": app.event_id, "attempt_id": app.attempt_id,
        "collection_identity": app.collection_identity,
        "collection_generation": app.collection_generation,
        "plan_hash": hashlib.sha256(app.plan_json.encode()).hexdigest(),
        "source": "test_application_verifier", "proof_id": "synthetic-unit-evidence",
    }


class CompactApplicationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="inkcards-store-candidate-", dir="/private/tmp")
        self.base = Path(self.tempdir.name)
        self.root = self.base / "profile"
        self.store = self.open_store()
        self.snapshot_id = snapshot(self.store)

    def tearDown(self) -> None:
        if self.store is not None:
            self.store.close()
        self.tempdir.cleanup()

    def open_store(self) -> ProfileStore:
        store = ProfileStore(self.root, profile_id="profile-a")
        with sqlite_connection(store.connect()) as db:
            has_device = db.execute(
                "SELECT 1 FROM devices WHERE profile_id='profile-a' AND device_id='device-a'"
            ).fetchone() is not None
            has_session = db.execute(
                "SELECT 1 FROM sessions WHERE profile_id='profile-a' AND device_id='device-a' AND session_id='session-a'"
            ).fetchone() is not None
        if not has_device:
            store.register_device("device-a", "device-token-a")
        if not has_session:
            store.open_session("device-a", "session-a")
        return store

    def receive(self, rows=None) -> None:
        self.store.receive_compact_batch(
            "device-a", "session-a", self.snapshot_id, 1,
            rows or [[1, 3, 1_700_000_000_000, 120]], now_ms=100,
        )

    def test_compact_retransmit_keeps_received_ack_and_application_state(self) -> None:
        self.receive()
        original_event = self.store.query_compact_events("device-a", "session-a")[0]
        receipts, conflict = self.store.receive_compact_batch(
            "device-a", "session-a", self.snapshot_id, 1,
            [[1, 3, 1_700_000_000_000, 120]], now_ms=200,
        )
        self.assertFalse(conflict)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, "received")
        self.assertEqual(receipts[0].payload_hash, original_event.payload_hash)
        self.assertEqual(len(self.store.query_compact_events("device-a", "session-a")), 1)
        self.assertEqual(self.store.query_compact_applications("device-a", "session-a")[0].status, "not_applied")
        self.assertFalse(self.store.pull_gate("device-a", "session-a").allowed)

        self.apply()
        receipts, conflict = self.store.receive_compact_batch(
            "device-a", "session-a", self.snapshot_id, 1,
            [[1, 3, 1_700_000_000_000, 120]], now_ms=300,
        )
        self.assertFalse(conflict)
        self.assertEqual(receipts[0].status, "received")
        self.assertEqual(self.store.query_compact_applications("device-a", "session-a")[0].status, "applied")
        self.assertTrue(self.store.pull_gate("device-a", "session-a").allowed)

    def begin(self, sequence=1, *, card_before=None, card_after=None, apply_plan=None,
              collection_identity="collection-a", collection_generation=None):
        card_before = card_before or before()
        card_after = card_after or after()
        apply_plan = apply_plan or plan()
        return self.store.begin_compact_apply(
            "device-a", "session-a", sequence,
            collection_identity=collection_identity,
            collection_generation=(self.store.get_collection_generation()
                                   if collection_generation is None else collection_generation),
            policy_json={"algorithm": "FSRS", "randomization": "seeded"},
            before_json=card_before, plan_json=apply_plan, expected_after_json=card_after,
            intent_id="compact-intent-%d" % sequence,
            attempt_id="compact-attempt-%d" % sequence, now_ms=10_000 + sequence,
        )

    def apply(self, sequence=1, *, card_before=None, card_after=None, apply_plan=None, generation_after=None):
        card_after = card_after or after(sequence)
        app = self.begin(sequence, card_before=card_before, card_after=card_after, apply_plan=apply_plan)
        self.store.record_compact_score_evidence(
            "device-a", "session-a", sequence, stage="score_applied",
            evidence_json={"source": "synthetic-unit-test", "sequence": sequence},
        )
        return self.store.finish_compact_apply(
            app.intent_id, after_json=card_after,
            collection_generation_after=generation_after,
        )

    def receive_same_card_chain(self) -> None:
        self.snapshot_id = SnapshotService(self.store).publish({
            "snapshot_schema": "candidate.v1", "manifest": {}, "decks": [],
            "presets": [], "day_table": {}, "complete": True, "unsupported": [],
            "cards": [{"card_id": "card-a"}, {"card_id": "card-b"}],
        }, scope_id="all", generation=0, applied_watermark=0).snapshot_id
        self.store.receive_compact_batch(
            "device-a", "session-a", self.snapshot_id, 1,
            [
                [1, 3, 1_700_000_000_000, 120],
                [2, 3, 1_700_000_001_000, 120],
                [1, 4, 1_700_000_002_000, 120],
            ],
            now_ms=200,
        )

    def apply_full_frame(self, sequence: int, before_state: dict, after_state: dict) -> None:
        app = self.store.begin_compact_apply(
            "device-a", "session-a", sequence,
            collection_identity="collection-a",
            collection_generation=self.store.get_collection_generation(),
            policy_json={"algorithm": "FSRS", "randomization": "seeded"},
            before_json=before_state,
            plan_json=full_plan(after_state),
            expected_after_json=after_state,
            intent_id="chain-intent-%d" % sequence,
            attempt_id="chain-attempt-%d" % sequence,
            now_ms=20_000 + sequence,
        )
        self.store.record_compact_score_evidence(
            "device-a", "session-a", sequence, stage="score_applied",
            evidence_json={"source": "synthetic-chain-test", "sequence": sequence},
        )
        self.store.finish_compact_apply(
            app.intent_id, after_json=after_state,
            collection_generation_after=self.store.get_collection_generation() + 1,
        )

    def same_card_chain_frames(self) -> tuple[dict, dict, dict, dict, dict, dict]:
        a_log_1 = {"id": 10_001, "card_id": "card-a", "rating": 3}
        b_log_1 = {"id": 20_001, "card_id": "card-b", "rating": 3}
        a_log_2 = {"id": 10_002, "card_id": "card-a", "rating": 4}
        a_before_1 = full_frame("card-a", 0, [], 0)
        a_after_1 = score_after_frame("card-a", 1, a_log_1, 1, 1)
        b_before = full_frame("card-b", 0, [], 1)
        b_after = score_after_frame("card-b", 1, b_log_1, 1, 2)
        a_before_2 = full_frame("card-a", 1, [a_log_1], 2)
        a_after_2 = score_after_frame("card-a", 2, a_log_2, 2, 3)
        return a_before_1, a_after_1, b_before, b_after, a_before_2, a_after_2

    def same_card_chain_with_deck_scopes(self) -> tuple[dict, dict, dict, dict, dict, dict]:
        a_log_1 = {"id": 10_101, "card_id": "card-a", "rating": 3}
        b_log_1 = {"id": 20_101, "card_id": "card-b", "rating": 3}
        a_log_2 = {"id": 10_102, "card_id": "card-a", "rating": 4}
        a_before_1 = full_frame_with_counters("card-a", 0, [], {"10": 0, "1": 0}, deck_id="10")
        a_after_1 = score_after_frame_with_counters(
            "card-a", 1, a_log_1, 1, {"10": 1, "1": 1}, deck_id="10",
        )
        b_before = full_frame_with_counters("card-b", 0, [], {"20": 0, "1": 1}, deck_id="20")
        b_after = score_after_frame_with_counters(
            "card-b", 1, b_log_1, 1, {"20": 1, "1": 2}, deck_id="20",
        )
        a_before_2 = full_frame_with_counters("card-a", 1, [a_log_1], {"10": 1, "1": 2}, deck_id="10")
        a_after_2 = score_after_frame_with_counters(
            "card-a", 2, a_log_2, 2, {"10": 2, "1": 3}, deck_id="10",
        )
        return a_before_1, a_after_1, b_before, b_after, a_before_2, a_after_2

    def test_same_card_chain_accepts_a_b_a_and_preserves_full_global_frames(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, a_after_2 = self.same_card_chain_frames()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)

        app = self.store.begin_compact_apply(
            "device-a", "session-a", 3,
            collection_identity="collection-a",
            collection_generation=self.store.get_collection_generation(),
            policy_json={"algorithm": "FSRS", "randomization": "seeded"},
            before_json=a_before_2,
            plan_json=full_plan(a_after_2),
            expected_after_json=a_after_2,
            intent_id="chain-intent-3",
            attempt_id="chain-attempt-3",
            now_ms=20_003,
        )
        self.assertEqual(json.loads(app.before_json), a_before_2)
        self.assertEqual(json.loads(app.plan_json)["expected_final_counters"], counters(3))
        self.assertEqual(json.loads(app.expected_after_json), a_after_2)

        self.store.record_compact_score_evidence(
            "device-a", "session-a", 3, stage="score_applied",
            evidence_json={"source": "synthetic-chain-test", "sequence": 3},
        )
        wrong_counters = score_after_frame(
            "card-a", 2, a_after_2["score_review_log"], a_after_2["review_log_count"], 2,
        )
        with self.assertRaises(ReceiptRegressionError) as wrong_after:
            self.store.finish_compact_apply(
                app.intent_id, after_json=wrong_counters, collection_generation_after=3,
            )
        self.assertEqual(wrong_after.exception.code, "compact_after_mismatch")
        result = self.store.finish_compact_apply(
            app.intent_id, after_json=a_after_2, collection_generation_after=3,
        )
        self.assertEqual(result.status, "applied")

    def test_same_card_chain_accepts_different_deck_scopes_with_shared_parent(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, a_after_2 = self.same_card_chain_with_deck_scopes()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)

        self.apply_full_frame(3, a_before_2, a_after_2)
        self.assertEqual(self.store.get_collection_generation(), 3)
        self.assertEqual(
            [app.status for app in self.store.query_compact_applications("device-a", "session-a")],
            ["applied", "applied", "applied"],
        )

    def test_same_card_chain_rejects_shared_deck_counter_discontinuity(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, _, _, a_before_2, _ = self.same_card_chain_with_deck_scopes()
        b_log = {"id": 20_102, "card_id": "card-b", "rating": 3}
        b_before = full_frame_with_counters("card-b", 0, [], {"20": 0, "1": 9}, deck_id="20")
        b_after = score_after_frame_with_counters(
            "card-b", 1, b_log, 1, {"20": 1, "1": 10}, deck_id="20",
        )
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)
        a_before_2["counters"] = scoped_counters({"10": 1, "1": 10})
        proposed_after = score_after_frame_with_counters(
            "card-a", 2, {"id": 10_103, "card_id": "card-a", "rating": 4}, 2,
            {"10": 2, "1": 11}, deck_id="10",
        )

        with self.assertRaises(ReceiptRegressionError) as caught:
            self.store.begin_compact_apply(
                "device-a", "session-a", 3,
                collection_identity="collection-a",
                collection_generation=self.store.get_collection_generation(),
                policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                before_json=a_before_2,
                plan_json=full_plan(proposed_after),
                expected_after_json=proposed_after,
            )
        self.assertEqual(caught.exception.code, "generation_mismatch")

    def test_same_card_chain_rejects_missing_counter_scope_key(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, _ = self.same_card_chain_with_deck_scopes()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)
        a_before_2["counters"] = scoped_counters({"1": 2})
        proposed_after = score_after_frame_with_counters(
            "card-a", 2, {"id": 10_104, "card_id": "card-a", "rating": 4}, 2,
            {"1": 3}, deck_id="10",
        )

        with self.assertRaises(ReceiptRegressionError) as caught:
            self.store.begin_compact_apply(
                "device-a", "session-a", 3,
                collection_identity="collection-a",
                collection_generation=self.store.get_collection_generation(),
                policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                before_json=a_before_2,
                plan_json=full_plan(proposed_after),
                expected_after_json=proposed_after,
            )
        self.assertEqual(caught.exception.code, "generation_mismatch")

    def test_same_card_chain_rejects_counter_scope_change_inside_receipt(self) -> None:
        self.receive_same_card_chain()
        a_before_1, _, _, _, _, _ = self.same_card_chain_with_deck_scopes()
        a_log_1 = {"id": 10_101, "card_id": "card-a", "rating": 3}
        a_after_1 = score_after_frame_with_counters(
            "card-a", 1, a_log_1, 1, {"10": 1}, deck_id="10",
        )
        b_log = {"id": 20_103, "card_id": "card-b", "rating": 3}
        b_before = full_frame_with_counters("card-b", 0, [], {"20": 0, "1": 0}, deck_id="20")
        b_after = score_after_frame_with_counters(
            "card-b", 1, b_log, 1, {"20": 1, "1": 1}, deck_id="20",
        )
        a_before_2 = full_frame_with_counters("card-a", 1, [a_log_1], {"10": 1}, deck_id="10")
        proposed_after = score_after_frame_with_counters(
            "card-a", 2, {"id": 10_105, "card_id": "card-a", "rating": 4}, 2,
            {"10": 2}, deck_id="10",
        )
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)

        with self.assertRaises(ReceiptRegressionError) as caught:
            self.store.begin_compact_apply(
                "device-a", "session-a", 3,
                collection_identity="collection-a",
                collection_generation=self.store.get_collection_generation(),
                policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                before_json=a_before_2,
                plan_json=full_plan(proposed_after),
                expected_after_json=proposed_after,
            )
        self.assertEqual(caught.exception.code, "generation_mismatch")

    def test_same_card_chain_survives_store_reopen_between_other_card_and_same_card(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, a_after_2 = self.same_card_chain_frames()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)
        self.store.close()
        self.store = self.open_store()

        self.apply_full_frame(3, a_before_2, a_after_2)
        self.assertEqual(self.store.get_collection_generation(), 3)
        self.assertEqual(
            [app.status for app in self.store.query_compact_applications("device-a", "session-a")],
            ["applied", "applied", "applied"],
        )

    def test_same_card_chain_rejects_tampered_global_bridge_receipt(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, _ = self.same_card_chain_frames()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)
        with sqlite_connection(self.store.connect()) as db:
            db.execute(
                "UPDATE compact_score_evidence SET evidence_json=? "
                "WHERE profile_id='profile-a' AND device_id='device-a' AND session_id='session-a' "
                "AND sequence=2 AND stage='score_applied'",
                ('{"tampered":true}',),
            )

        with self.assertRaises(ReceiptRegressionError) as caught:
            self.store.begin_compact_apply(
                "device-a", "session-a", 3,
                collection_identity="collection-a",
                collection_generation=self.store.get_collection_generation(),
                policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                before_json=a_before_2,
                plan_json=full_plan(score_after_frame(
                    "card-a", 2, {"id": 10_002, "card_id": "card-a", "rating": 4}, 2, 3,
                )),
                expected_after_json=score_after_frame(
                    "card-a", 2, {"id": 10_002, "card_id": "card-a", "rating": 4}, 2, 3,
                ),
            )
        self.assertEqual(caught.exception.code, "compact_receipt_invalid")

    def test_same_card_chain_rejects_unproven_external_generation_gap(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, _, _, a_before_2, _ = self.same_card_chain_frames()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.store.set_collection_generation(2)

        with self.assertRaises(ReceiptRegressionError) as caught:
            self.begin(
                3,
                card_before=a_before_2,
                card_after=score_after_frame(
                    "card-a", 2, {"id": 10_002, "card_id": "card-a", "rating": 4}, 2, 3,
                ),
                apply_plan=full_plan(score_after_frame(
                    "card-a", 2, {"id": 10_002, "card_id": "card-a", "rating": 4}, 2, 3,
                )),
                collection_generation=2,
            )
        self.assertEqual(caught.exception.code, "generation_mismatch")

    def test_same_card_chain_rejects_changed_card_projection_after_other_card(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, _ = self.same_card_chain_frames()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)
        changed_a = full_frame("card-a", 2, a_before_2["review_logs"], 2)
        changed_a_logs = full_frame("card-a", 1, [
            *a_before_2["review_logs"],
            {"id": 10_099, "card_id": "card-a", "rating": 1},
        ], 2)
        proposed_after = score_after_frame(
            "card-a", 3, {"id": 10_003, "card_id": "card-a", "rating": 4}, 2, 3,
        )

        for changed_before in (changed_a, changed_a_logs):
            with self.subTest(card=changed_before["card"], logs=changed_before["review_logs"]):
                with self.assertRaises(ReceiptRegressionError) as caught:
                    self.store.begin_compact_apply(
                        "device-a", "session-a", 3,
                        collection_identity="collection-a",
                        collection_generation=self.store.get_collection_generation(),
                        policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                        before_json=changed_before,
                        plan_json=full_plan(proposed_after),
                        expected_after_json=proposed_after,
                    )
                self.assertEqual(caught.exception.code, "compact_card_chain_mismatch")

    def test_same_card_chain_compares_unknown_full_frame_fields_strictly(self) -> None:
        self.receive_same_card_chain()
        a_before_1, a_after_1, b_before, b_after, a_before_2, _ = self.same_card_chain_frames()
        self.apply_full_frame(1, a_before_1, a_after_1)
        self.apply_full_frame(2, b_before, b_after)
        a_before_2["unrecognized_frame_field"] = {"must": "remain strict"}
        proposed_after = score_after_frame(
            "card-a", 2, {"id": 10_106, "card_id": "card-a", "rating": 4}, 2, 3,
        )

        with self.assertRaises(ReceiptRegressionError) as caught:
            self.store.begin_compact_apply(
                "device-a", "session-a", 3,
                collection_identity="collection-a",
                collection_generation=self.store.get_collection_generation(),
                policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                before_json=a_before_2,
                plan_json=full_plan(proposed_after),
                expected_after_json=proposed_after,
            )
        self.assertEqual(caught.exception.code, "compact_card_chain_mismatch")

    def create_v4_database(self, root: Path, *, rows: int = 6) -> None:
        root.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(root / "bridge.sqlite3") as db:
            db.execute("PRAGMA foreign_keys=ON")
            for version, statements in (
                (1, migrations._v1_statements()), (2, migrations._v2_statements()),
                (3, migrations._v3_statements()), (4, migrations._v4_statements()),
            ):
                for statement in statements:
                    db.execute(statement)
                if version == 2:
                    db.execute("UPDATE events SET normalized_payload_json=raw_payload_json WHERE normalized_payload_json='' ")
                db.execute("INSERT INTO schema_migrations(version,applied_at_ms) VALUES(?,?)", (version, version))
                db.execute("PRAGMA user_version=%d" % version)
            db.execute("INSERT INTO profile_meta(profile_id,created_at_ms,updated_at_ms,collection_generation) VALUES('profile-a',1,1,0)")
            db.execute("INSERT INTO devices(profile_id,device_id,token_hash,created_at_ms) VALUES('profile-a','device-a',?,1)",
                       (hashlib.sha256(b"token").hexdigest(),))
            db.execute("INSERT INTO sessions(profile_id,device_id,session_id,high_water,created_at_ms,last_seen_at_ms) VALUES('profile-a','device-a','session-a',1,1,1)")
            ordinary_event = ImmutableReviewEvent.create(
                "session-a:1", 1,
                {"card_id": "ordinary-legacy", "rating": 3, "answered_at_ms": 1_700_000_000_001},
            )
            ordinary_payload = {
                "event_id": ordinary_event.event_id,
                "sequence": ordinary_event.sequence,
                "payload": dict(ordinary_event.payload),
            }
            raw_ordinary_json = json.dumps(
                ordinary_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            normalized_ordinary_json = json.dumps(
                ordinary_payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            db.execute(
                "INSERT INTO events(profile_id,device_id,session_id,sequence,event_id,payload_hash,"
                "raw_payload_json,received_at_ms,updated_at_ms,normalized_payload_json) "
                "VALUES('profile-a','device-a','session-a',1,?,?,?,?,?,?)",
                (ordinary_event.event_id, ordinary_event.payload_hash, raw_ordinary_json,
                 101, 101, normalized_ordinary_json),
            )
            db.execute(
                "INSERT INTO deliveries(profile_id,device_id,session_id,sequence,event_id,status,reason,updated_at_ms) "
                "VALUES('profile-a','device-a','session-a',1,?,'received',NULL,101)",
                (ordinary_event.event_id,),
            )
            db.execute(
                "INSERT INTO applications(profile_id,device_id,session_id,sequence,event_id,status,"
                "intent_id,attempt_id,reason,updated_at_ms) "
                "VALUES('profile-a','device-a','session-a',1,?,'pending_apply',NULL,NULL,NULL,101)",
                (ordinary_event.event_id,),
            )
            payload = json.dumps({"cards": [{"card_id": "legacy-%d" % i} for i in range(1, 7)]}, separators=(",", ":"))
            db.execute(
                "INSERT INTO snapshots(profile_id,snapshot_id,scope_generation,applied_watermark,payload_json,checksum,created_at_ms,"
                "scope_id,snapshot_generation,manifest_json,card_count,included_card_count,skipped_count,payload_bytes,page_count,peak_bytes_estimate) "
                "VALUES('profile-a','legacy-snapshot',0,0,?,?,1,'all',0,'{}',6,6,0,0,1,0)",
                (payload, hashlib.sha256(payload.encode()).hexdigest()),
            )
            for sequence in range(1, rows + 1):
                digest = hashlib.sha256(("legacy-payload-%d" % sequence).encode()).hexdigest()
                db.execute(
                    "INSERT INTO compact_events(profile_id,device_id,session_id,sequence,event_id,snapshot_id,card_id,card_index,"
                    "rating,answered_at_ms,duration_ms,payload_hash,received_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("profile-a", "device-a", "session-a", sequence, "session-a:%d" % sequence,
                     "legacy-snapshot", "legacy-%d" % sequence, sequence, 1 + sequence % 4,
                     1_700_000_000_000 + sequence, 50 + sequence, digest, 100 + sequence),
                )
            db.commit()

    def test_actual_v4_to_v5_migration_preserves_six_rows_and_rolls_back(self) -> None:
        root = self.base / "v4"
        self.create_v4_database(root, rows=6)
        with sqlite_connection(root / "bridge.sqlite3") as db:
            before_rows = [tuple(row) for row in db.execute(
                "SELECT * FROM compact_events ORDER BY sequence"
            ).fetchall()]
        migrated = ProfileStore(root, profile_id="profile-a")
        try:
            self.assertEqual(migrated.schema_version, 6)
            self.assertEqual(len(migrated.query_compact_events("device-a", "session-a")), 6)
            self.assertEqual([row.status for row in migrated.query_compact_applications("device-a", "session-a")],
                             ["not_applied"] * 6)
            self.assertEqual(migrated.count_events(), 1)
            self.assertEqual(
                migrated.get_event("device-a", "session-a", 1).payload,
                {"card_id": "ordinary-legacy", "rating": 3, "answered_at_ms": 1_700_000_000_001},
            )
            self.assertEqual(migrated.query_receipts("device-a", "session-a")[0].status, "received")
            self.assertEqual(migrated.query_applications("device-a", "session-a")[0].status, "pending_apply")
            with sqlite_connection(migrated.connect()) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 1)
                self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM compact_events ORDER BY sequence").fetchall()], before_rows)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            migrated.close()

        rollback_root = self.base / "v4-rollback"
        self.create_v4_database(rollback_root, rows=6)
        original = migrations._v5_statements

        def fail_after_backfill():
            return tuple(original()) + ("INSERT INTO no_such_v5_table VALUES(1)",)

        with mock.patch.object(migrations, "_v5_statements", side_effect=fail_after_backfill):
            with self.assertRaises(SchemaError):
                ProfileStore(rollback_root, profile_id="profile-a")
        with sqlite_connection(rollback_root / "bridge.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM compact_events").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 1)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compact_applications'"
            ).fetchone())
        reopened = ProfileStore(rollback_root, profile_id="profile-a")
        self.assertEqual(reopened.schema_version, 6)
        reopened.close()

        unknown_root = self.base / "unknown"
        unknown_root.mkdir()
        with sqlite_connection(unknown_root / "bridge.sqlite3") as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(UnsupportedSchemaError):
            ProfileStore(unknown_root, profile_id="profile-a")

    def test_process_exit_preserves_recovery_phase_and_shared_card_claim(self) -> None:
        self.receive()
        ordinary = ImmutableReviewEvent.create(
            "session-a:1", 1, {"card_id": "card-one", "rating": 3, "answered_at_ms": 1}
        )
        self.store.receive_batch("device-a", "session-a", [ordinary])
        self.store.close()
        self.store = None
        script = "\n".join([
            "import os,sys",
            "from inkcards_anki_bridge.storage import ProfileStore",
            "s=ProfileStore(sys.argv[1],profile_id='profile-a')",
            "a=s.begin_compact_apply('device-a','session-a',1,collection_identity='collection-a',collection_generation=0,",
            "policy_json={'algorithm':'FSRS','randomization':'seeded'},before_json={'card_id':'card-one','revision':0},",
            "plan_json={'required_evidence':['score_applied'],'expected_score_after':{'rev':1,'phase':'scored'}},",
            "expected_after_json={'card_id':'card-one','revision':1,'phase':'scored'},intent_id='compact-intent-1',",
            "attempt_id='compact-attempt-1',now_ms=10001)",
            "s.record_compact_score_evidence('device-a','session-a',1,stage='score_started',",
            "evidence_json={'source':'child-test','phase':'started'},now_ms=10002)",
            "os._exit(0)",
        ])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "anki-addon") + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.run([sys.executable, "-c", script, str(self.root)], env=env, check=False)
        self.assertEqual(child.returncode, 0)

        self.store = self.open_store()
        app = self.store.query_compact_applications("device-a", "session-a")[0]
        self.assertEqual(app.status, "applying")
        self.assertTrue(app.recovery_required)
        self.assertEqual([item.stage for item in self.store.query_compact_score_evidence("device-a", "session-a", 1)],
                         ["score_started"])
        self.assertIsNone(self.store.pull_gate("device-a", "session-a").applied_watermark)
        with self.assertRaises(BusyStorageError):
            self.store.begin_apply("device-a", "session-a", [1], intent_id="ordinary-second", attempt_id="ordinary-second")

        recovered = self.store.classify_compact_recovery(
            app.intent_id, classification="not_started", verification_json=proof(app),
            observed_before_json=before(), now_ms=10003,
        )
        self.assertEqual(recovered.status, "applying")
        self.assertFalse(recovered.recovery_required)
        self.store.record_compact_score_evidence(
            "device-a", "session-a", 1, stage="score_applied",
            evidence_json={"source": "explicit-not-started-continuation"}, now_ms=10004,
        )
        done = self.store.finish_compact_apply(
            app.intent_id, after_json=after(), collection_generation_after=1, now_ms=10005,
        )
        self.assertEqual(done.status, "applied")
        self.assertEqual(self.store.query_compact_receipts("device-a", "session-a")[0].status, "received")
        ordinary_intent = self.store.begin_apply(
            "device-a", "session-a", [1], intent_id="ordinary-after", attempt_id="ordinary-after"
        )
        self.assertEqual(self.store.finish_apply(ordinary_intent.intent_id, applied=True).status, "applied")
        gate = self.store.pull_gate("device-a", "session-a")
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "pull_gate_mixed_protocol_session")

    def test_recovery_score_complete_fills_missing_due_stage_without_rescoring(self) -> None:
        self.receive()
        self.store.close()
        self.store = None
        script = "\n".join([
            "import os,sys",
            "from inkcards_anki_bridge.storage import ProfileStore",
            "s=ProfileStore(sys.argv[1],profile_id='profile-a')",
            "s.begin_compact_apply('device-a','session-a',1,collection_identity='collection-a',collection_generation=0,",
            "policy_json={'algorithm':'FSRS'},before_json={'card_id':'card-one','revision':0},",
            "plan_json={'required_evidence':['score_applied','due_corrected'],'expected_score_after':{'rev':1,'phase':'scored'}},",
            "expected_after_json={'card_id':'card-one','revision':1,'phase':'scored'},intent_id='compact-intent-1',attempt_id='compact-attempt-1')",
            "s.record_compact_score_evidence('device-a','session-a',1,stage='score_applied',evidence_json={'source':'child-test','committed':True})",
            "os._exit(0)",
        ])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "anki-addon") + os.pathsep + env.get("PYTHONPATH", "")
        self.assertEqual(subprocess.run([sys.executable, "-c", script, str(self.root)], env=env).returncode, 0)
        self.store = self.open_store()
        app = self.store.query_compact_applications("device-a", "session-a")[0]
        result = self.store.classify_compact_recovery(
            app.intent_id, classification="score_complete", verification_json=proof(app),
            observed_before_json=before(), score_after_json={"rev": 1, "phase": "scored"},
            final_after_json=after(), collection_generation_after=1,
        )
        self.assertEqual(result.status, "applied")
        stages = {item.stage for item in self.store.query_compact_score_evidence("device-a", "session-a", 1)}
        self.assertEqual(stages, {"score_applied", "due_corrected", "recovery_classification"})
        self.assertTrue(self.store.pull_gate("device-a", "session-a").allowed)

    def test_unknown_reconcile_never_replays_or_promotes_from_state_match_alone(self) -> None:
        self.receive()
        app = self.begin()
        self.store.close()
        self.store = self.open_store()
        app = self.store.query_compact_applications("device-a", "session-a")[0]
        self.assertEqual(app.status, "applying")
        self.assertTrue(app.recovery_required)

        unresolved = self.store.classify_compact_recovery(
            app.intent_id,
            classification="ambiguous",
            verification_json=proof(app),
            observed_before_json=before(),
        )
        self.assertEqual(unresolved.status, "unknown")
        self.assertFalse(unresolved.recovery_required)
        self.assertEqual(
            [item.stage for item in self.store.query_compact_score_evidence("device-a", "session-a", 1)],
            ["recovery_classification"],
        )
        with self.assertRaises(UnknownApplyError):
            self.begin()
        with self.assertRaises(UnknownApplyError):
            self.store.record_compact_score_evidence(
                "device-a", "session-a", 1, stage="score_applied", evidence_json={"would_replay": True}
            )

        reconciled = self.store.reconcile_compact_apply(app.intent_id, observed_after_json=after())
        self.assertEqual(reconciled.status, "unknown")
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason, "pull_gate_compact_unknown")
        with self.assertRaises(UnknownApplyError):
            self.store.finish_compact_apply(app.intent_id, after_json=after())

    def test_each_apply_commit_boundary_is_unknown_to_caller_and_recoverable(self) -> None:
        self.receive()
        original_commit = self.store._commit
        self.store._commit = lambda db: (_ for _ in ()).throw(sqlite3.OperationalError("simulated commit failure"))
        with self.assertRaises(CommitUnknownError):
            self.begin()
        self.store._commit = original_commit
        self.assertEqual(self.store.query_compact_applications("device-a", "session-a")[0].status, "not_applied")
        self.assertEqual(self.store.diagnostics()["card_apply_claims"], 0)

        app = self.begin()
        self.store._commit = lambda db: (_ for _ in ()).throw(sqlite3.OperationalError("simulated commit failure"))
        with self.assertRaises(CommitUnknownError):
            self.store.record_compact_score_evidence(
                "device-a", "session-a", 1, stage="score_applied", evidence_json={"score": "committed"}
            )
        self.store._commit = original_commit
        self.assertEqual(self.store.query_compact_score_evidence("device-a", "session-a", 1), [])
        self.store.record_compact_score_evidence(
            "device-a", "session-a", 1, stage="score_applied", evidence_json={"score": "committed"}
        )
        self.store._commit = lambda db: (_ for _ in ()).throw(sqlite3.OperationalError("simulated commit failure"))
        with self.assertRaises(CommitUnknownError):
            self.store.finish_compact_apply(app.intent_id, after_json=after())
        self.store._commit = original_commit
        self.store.close()
        self.store = self.open_store()
        recovered = self.store.query_compact_applications("device-a", "session-a")[0]
        self.assertEqual(recovered.status, "applying")
        self.assertTrue(recovered.recovery_required)
        result = self.store.classify_compact_recovery(
            app.intent_id, classification="score_complete", verification_json=proof(recovered),
            observed_before_json=before(), score_after_json={"rev": 1, "phase": "scored"},
            final_after_json=after(),
        )
        self.assertEqual(result.status, "applied")
        self.assertTrue(self.store.pull_gate("device-a", "session-a").allowed)

    def test_same_card_sequence_chain_and_terminal_evidence_gate(self) -> None:
        self.receive([[1, 3, 1_700_000_000_001, 120], [1, 4, 1_700_000_000_002, 130]])
        with self.assertRaises(ReceiptRegressionError):
            self.begin(2, card_before=after(), card_after=after(2))
        self.assertEqual(self.apply(1, card_before=before(), card_after=after(), generation_after=1).status, "applied")
        with self.assertRaises(ReceiptRegressionError):
            self.begin(2, card_before=before(), card_after=after(2))
        self.assertEqual(self.apply(2, card_before=after(), card_after=after(2),
                                   apply_plan={"required_evidence": ["score_applied"]},
                                   generation_after=2).status, "applied")
        self.assertTrue(self.store.pull_gate("device-a", "session-a").allowed)
        original = self.store.query_compact_score_evidence("device-a", "session-a", 1)
        with self.assertRaises(ReceiptRegressionError):
            self.store.record_compact_score_evidence(
                "device-a", "session-a", 1, stage="score_applied", evidence_json={"changed": True}
            )
        self.assertEqual(self.store.query_compact_score_evidence("device-a", "session-a", 1), original)
        with sqlite_connection(self.store.connect()) as db:
            db.execute(
                "INSERT INTO compact_score_evidence(profile_id,device_id,session_id,sequence,stage,evidence_json,evidence_hash,created_at_ms) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("profile-a", "device-a", "session-a", 1, "conflict-forged", "{}", hashlib.sha256(b"{}").hexdigest(), 20),
            )
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason,
                         "pull_gate_compact_receipt_invalid")

    def test_applied_idempotence_rejects_tampered_terminal_evidence(self) -> None:
        self.receive()
        app = self.apply()
        self.assertEqual(app.status, "applied")

        with sqlite_connection(self.store.connect()) as db:
            db.execute(
                "UPDATE compact_score_evidence SET evidence_json=? WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND sequence=? AND stage='score_applied'",
                ('{"tampered":true}', "profile-a", "device-a", "session-a", 1),
            )

        with self.assertRaises(ReceiptRegressionError) as finish_error:
            self.store.finish_compact_apply(app.intent_id, after_json=after())
        self.assertEqual(finish_error.exception.code, "compact_receipt_invalid")

        with self.assertRaises(ReceiptRegressionError) as reconcile_error:
            self.store.reconcile_compact_apply(app.intent_id, observed_after_json=after())
        self.assertEqual(reconcile_error.exception.code, "compact_receipt_invalid")

        gate = self.store.pull_gate("device-a", "session-a")
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "pull_gate_compact_receipt_invalid")
        self.assertIsNone(self.store.compact_applied_watermark("device-a", "session-a"))

    def test_payload_profile_collection_and_external_generation_mismatches_fail_closed(self) -> None:
        self.receive([[1, 3, 1_700_000_000_001, 120], [1, 4, 1_700_000_000_002, 130]])
        self.apply(1, card_before=before(), card_after=after(), generation_after=1)
        with self.assertRaises(ReceiptRegressionError) as changed_collection:
            self.begin(2, card_before=after(), card_after=after(2), collection_identity="other-collection")
        self.assertEqual(changed_collection.exception.code, "generation_mismatch")
        self.store.set_collection_generation(2)
        with self.assertRaises(ReceiptRegressionError) as changed_generation:
            self.begin(2, card_before=after(), card_after=after(2), collection_generation=2)
        self.assertEqual(changed_generation.exception.code, "generation_mismatch")

        _, payload_conflict = self.store.receive_compact_batch(
            "device-a", "session-a", self.snapshot_id, 1,
            [[1, 2, 1_700_000_000_001, 120]], now_ms=300,
        )
        self.assertTrue(payload_conflict)
        self.assertEqual(self.store.query_compact_applications("device-a", "session-a")[0].status, "applied")
        self.assertEqual(self.store.pull_gate("device-a", "session-a").reason, "pull_gate_compact_conflict")

        other = ProfileStore(self.base / "other-profile", profile_id="profile-b")
        try:
            other.register_device("device-a", "other-token")
            other.open_session("device-a", "session-a")
            self.assertEqual(other.query_compact_events("device-a", "session-a"), [])
        finally:
            other.close()
        with self.assertRaises(ProfileMismatchError):
            ProfileStore(self.root, profile_id="profile-b")

    def test_sequence_gap_and_cross_scope_same_card_block_without_clock_ordering(self) -> None:
        with self.assertRaises(SequenceError):
            self.store.receive_compact_batch(
                "device-a", "session-a", self.snapshot_id, 2, [[1, 3, 1_700_000_000_002, 120]]
            )
        self.receive()
        self.store.register_device("device-b", "device-token-b")
        self.store.open_session("device-b", "session-b")
        self.store.receive_compact_batch(
            "device-b", "session-b", self.snapshot_id, 1, [[1, 4, 1_700_000_000_010, 100]]
        )
        with self.assertRaises(ReceiptRegressionError) as caught:
            self.begin()
        self.assertEqual(caught.exception.code, "compact_cross_scope_ambiguous")

    def test_six_compact_only_rows_open_pull_and_publish_stream_scoped_snapshot(self) -> None:
        self.receive([
            [1, 1 + sequence % 4, 1_700_000_000_000 + sequence * 1_000, 100 + sequence]
            for sequence in range(1, 7)
        ])
        for sequence in range(1, 7):
            card_before = before() if sequence == 1 else after(sequence - 1)
            card_after = after(sequence)
            app = self.begin(
                sequence,
                card_before=card_before,
                card_after=card_after,
            )
            self.store.record_compact_score_evidence(
                "device-a",
                "session-a",
                sequence,
                stage="score_applied",
                evidence_json={"synthetic_score_effect": sequence},
            )
            result = self.store.finish_compact_apply(
                app.intent_id,
                after_json=card_after,
                collection_generation_after=sequence,
            )
            self.assertEqual(result.status, "applied")

        same_session_gate = self.store.pull_gate("device-a", "session-a")
        self.assertTrue(same_session_gate.allowed)
        self.assertEqual(same_session_gate.event_count, 6)
        self.assertEqual(same_session_gate.applied_watermark, 6)
        self.assertEqual(self.store.compact_applied_watermark("device-a", "session-a"), 6)

        service = SnapshotService(self.store)
        refresh_sessions = []

        def refresh_handler(**request):
            gate = self.store.pull_gate(request["device_id"], request["session_id"])
            published = service.publish(
                {
                    "snapshot_schema": "candidate.v1",
                    "manifest": {},
                    "decks": [],
                    "presets": [],
                    "day_table": {},
                    "complete": True,
                    "unsupported": [],
                    "cards": [{"card_id": "card-one", "revision": 6}],
                },
                scope_id=request["scope_id"],
                generation=self.store.get_collection_generation(),
                applied_watermark=gate.applied_watermark,
            )
            refresh_sessions.append(request["session_id"])
            return 200, published.to_payload()

        auth = DeviceAuthenticator(self.store)
        router = DeviceRouter(
            auth,
            InboxService(self.store),
            snapshot_service=service,
            refresh_handler=refresh_handler,
        )
        headers = {"Authorization": "Bearer device-token-a"}

        def call(path, payload):
            raw = json.dumps(payload, separators=(",", ":")).encode()
            response = router.dispatch("POST", path, headers, raw)
            return response.status, json.loads(response.body)

        for session_id in ("session-a", "session-b"):
            expected_watermark = 6 if session_id == "session-a" else 0
            if session_id == "session-b":
                status, handshake = call(
                    "/v2/handshake",
                    {"protocol": "x4-2", "device_id": "device-a", "session_id": session_id},
                )
                self.assertEqual(status, 200, handshake)

            status, refreshed = call(
                "/v2/pull/refresh",
                {"device_id": "device-a", "session_id": session_id, "scope_id": "all"},
            )
            self.assertEqual(status, 200, refreshed)
            self.assertEqual(refreshed["snapshot_generation"], 6)
            self.assertEqual(refreshed["applied_watermark"], {"known": expected_watermark})

            status, opened = call(
                "/v2/pull/open",
                {
                    "device_id": "device-a",
                    "session_id": session_id,
                    "scope_id": "all",
                    "local_generation": 0,
                    "applied_watermark": {"known": expected_watermark},
                },
            )
            self.assertEqual(status, 200, opened)
            self.assertEqual(opened["snapshot_generation"], 6)
            self.assertEqual(opened["applied_watermark"], {"known": expected_watermark})
            self.assertEqual(opened["snapshot_metadata"]["applied_watermark"], {"known": expected_watermark})

        self.assertEqual(refresh_sessions, ["session-a", "session-b"])
        new_session_gate = self.store.pull_gate("device-a", "session-b")
        self.assertTrue(new_session_gate.allowed)
        self.assertEqual(new_session_gate.event_count, 6)
        self.assertEqual(new_session_gate.applied_watermark, 0)
        self.assertIsNone(self.store.compact_applied_watermark("device-a", "session-b"))
        published_snapshot = self.store.latest_snapshot("all")
        self.assertEqual(published_snapshot.snapshot_generation, 6)
        self.assertEqual(published_snapshot.applied_watermark, 0)

    def test_received_ack_and_applied_watermark_require_valid_terminal_proof(self) -> None:
        root = self.base / "status-profile"
        store = ProfileStore(root, profile_id="profile-status")
        try:
            store.register_device("device-status", "device-token-status")
            store.open_session("device-status", "session-status")
            sid = snapshot(store)
            store.receive_compact_batch(
                "device-status", "session-status", sid, 1, [[1, 3, 1_700_000_000_000, 10]]
            )
            self.assertEqual(
                [receipt.status for receipt in store.query_compact_receipts("device-status", "session-status")],
                ["received"],
            )
            pending = store.query_compact_applications("device-status", "session-status")[0]
            self.assertEqual(pending.status, "not_applied")
            self.assertIsNone(store.compact_applied_watermark("device-status", "session-status"))
            pending_gate = store.pull_gate("device-status", "session-status")
            self.assertFalse(pending_gate.allowed)
            self.assertEqual(pending_gate.reason, "pull_gate_compact_not_applied")

            app = store.begin_compact_apply(
                "device-status", "session-status", 1,
                collection_identity="collection-status", collection_generation=0,
                policy_json={"algorithm": "FSRS"}, before_json={"card_id": "card-one", "revision": 0},
                plan_json={"required_evidence": ["score_applied"]},
                expected_after_json={"card_id": "card-one", "revision": 1, "phase": "scored"},
            )
            store.record_compact_score_evidence(
                "device-status", "session-status", 1, stage="score_applied", evidence_json={"verified": True}
            )
            applied = store.finish_compact_apply(
                app.intent_id,
                after_json={"card_id": "card-one", "revision": 1, "phase": "scored"},
            )
            self.assertEqual(applied.status, "applied")
            self.assertEqual(store.compact_applied_watermark("device-status", "session-status"), 1)
            self.assertTrue(store.pull_gate("device-status", "session-status").allowed)

            with sqlite_connection(store.connect()) as db:
                db.execute(
                    "UPDATE compact_score_evidence SET evidence_json=? "
                    "WHERE profile_id='profile-status' AND device_id='device-status' "
                    "AND session_id='session-status' AND sequence=1 AND stage='score_applied'",
                    (json.dumps({"tampered": True}, separators=(",", ":")),),
                )
            self.assertEqual(
                store.pull_gate("device-status", "session-status").reason,
                "pull_gate_compact_receipt_invalid",
            )
            self.assertIsNone(store.compact_applied_watermark("device-status", "session-status"))
        finally:
            store.close()

    def test_status_reports_recovery_and_received_is_not_applied_watermark(self) -> None:
        root = self.base / "status-profile"
        store = ProfileStore(root, profile_id="profile-status")
        try:
            auth = DeviceAuthenticator(store, clock=lambda: 1_000_000)
            router = DeviceRouter(auth, InboxService(store))

            def call(path, payload, headers=None):
                raw = json.dumps(payload, separators=(",", ":")).encode()
                response = router.dispatch("POST", path, headers or {}, raw)
                return response.status, json.loads(response.body)

            code = auth.issue_pairing_code()
            _, pair = call("/v2/pair", {"code": code, "device_id": "device-status"})
            headers = {"Authorization": "Bearer " + pair["token"]}
            call("/v2/handshake", {
                "protocol": "x4-2", "device_id": "device-status", "session_id": "session-status"
            }, headers)
            sid = snapshot(store)
            store.receive_compact_batch(
                "device-status", "session-status", sid, 1, [[1, 3, 1_700_000_000_000, 10]]
            )
            _, pending = call("/v2/sync/status", {
                "device_id": "device-status", "session_id": "session-status"
            }, headers)
            self.assertEqual(pending["phase"], "pending_apply")
            self.assertEqual(pending["compact_delivery"]["counts"]["received"], 1)
            self.assertEqual(pending["compact_application"]["counts"]["not_applied"], 1)
            self.assertEqual(pending["compact_application"]["applied_watermark"], "unknown")

            app = store.begin_compact_apply(
                "device-status", "session-status", 1,
                collection_identity="collection-status", collection_generation=0,
                policy_json={"algorithm": "FSRS"}, before_json={"card_id": "card-one", "revision": 0},
                plan_json={"required_evidence": ["score_applied"]},
                expected_after_json={"card_id": "card-one", "revision": 1, "phase": "scored"},
            )
            store.record_compact_score_evidence(
                "device-status", "session-status", 1, stage="score_applied", evidence_json={"verified": True}
            )
            store.finish_compact_apply(app.intent_id, after_json={"card_id": "card-one", "revision": 1, "phase": "scored"})
            _, applied = call("/v2/sync/status", {
                "device_id": "device-status", "session_id": "session-status"
            }, headers)
            self.assertEqual(applied["phase"], "applied")
            self.assertEqual(applied["compact_delivery"]["counts"]["received"], 1)
            self.assertEqual(applied["compact_application"]["applied_watermark"], {"known": 1})

            with sqlite_connection(store.connect()) as db:
                db.execute(
                    "UPDATE compact_score_evidence SET evidence_json=? "
                    "WHERE profile_id='profile-status' AND device_id='device-status' "
                    "AND session_id='session-status' AND sequence=1 AND stage='score_applied'",
                    (json.dumps({"tampered": True}, separators=(",", ":")),),
                )
            self.assertEqual(
                store.pull_gate("device-status", "session-status").reason,
                "pull_gate_compact_receipt_invalid",
            )
            _, tampered = call("/v2/sync/status", {
                "device_id": "device-status", "session_id": "session-status"
            }, headers)
            self.assertEqual(tampered["phase"], "blocked")
            self.assertEqual(tampered["compact_application"]["applied_watermark"], "unknown")
            self.assertEqual(tampered["last_error"], "compact_application_receipt_invalid")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
