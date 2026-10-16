"""采用明确批准规则的恢复链；仅操作新建合成 Collection。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import anki.collection  # noqa: F401
from anki import buildinfo
from inkcards_anki_bridge.anki.application_context import capture_collection_context
from inkcards_anki_bridge.anki.compact_260903 import _capture_state
from inkcards_anki_bridge.anki.compact_projection import canonical_json, json_sha256
from inkcards_anki_bridge.services.compact_application import CompactApplicationError
from inkcards_anki_bridge.services.snapshots import SnapshotService
from test_compact_service import (
    DEVICE,
    PROFILE,
    SESSION,
    SyntheticFixture,
    count_active_answers,
    run_sigkill,
)

POLICY = {
    "fuzz": "fixed_ordinary",
    "load_balancing": "disabled",
    "counters": "preserve_current",
    "event_inputs": "original",
}
BINDING_KEYS = (
    "profile_id",
    "device_id",
    "session_id",
    "sequence",
    "event_id",
    "payload_hash",
    "snapshot_id",
    "card_id",
)


def adopted_fixture(fixture, *, missing_persisted_lrt=False, session=SESSION):
    typed = fixture.export_snapshot_from_query_context()
    wire = typed.to_dict()
    # 模拟旧 wire 明确缺字段；只改变测试所有的初始快照，不改真实用户来源。
    for card in wire["cards"]:
        if missing_persisted_lrt:
            current = fixture.collection.get_card(int(card["card_id"]))
            current.last_review_time = None
            fixture.collection.update_card(current)
            card["schedule_revision"] = str(
                fixture.collection.get_card(int(card["card_id"])).mod
            )
        card["last_review_time_ms"] = None
    snapshot_id = (
        SnapshotService(fixture.store)
        .publish(
            wire, scope_id="synthetic-old-" + session, generation=0, applied_watermark=0
        )
        .snapshot_id
    )
    fixture.receive(
        snapshot_id, [[1, 3, fixture.times(historical=True)[0], 120]], session=session
    )
    snapshot = fixture.store.get_snapshot(snapshot_id)
    context = capture_collection_context(
        fixture.collection,
        profile_identity=PROFILE,
        collection_generation=0,
        runtime_epoch="adopted-review-current-capture",
        anki_version="26.9.3",
        anki_build=buildinfo.buildhash,
        deck_ids=[str(d) for d in fixture.meta["deck_ids"]],
    )
    event = fixture.store.query_compact_events(DEVICE, session)[0]
    frame = _capture_state(
        fixture.collection,
        int(event.card_id),
        tuple(int(d) for d in context.deck_contexts),
    )
    return {
        "schema": "inkcards-adopted-source-v1",
        "source_id": "synthetic-reviewed-adopted",
        "policy_id": "inkcards-compact-adopted-ordinary-v1",
        "policy": POLICY,
        "approval": {
            "authority": "direct_user",
            "record_id": "synthetic-explicit-approval",
            "decision": "approve",
            "policy": POLICY,
        },
        "snapshot": {
            "profile_id": PROFILE,
            "snapshot_id": snapshot_id,
            "checksum": snapshot.checksum,
            "snapshot_generation": 0,
        },
        "adopted_context": context.to_dict(),
        "basis": [
            {
                "source_id": "synthetic-reviewed-backup",
                "sha256": "a" * 64,
                "role": "historical_backup",
            }
        ],
        "events": [{k: getattr(event, k) for k in BINDING_KEYS}],
        "cards": {
            event.card_id: {
                k: frame[k] for k in ("card", "native_card_hex", "native_revlogs")
            }
        },
    }


class AdoptedRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-adopted-", dir="/private/tmp"
        )
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_adopted_source_native_receipt_duplicate_and_original_unchanged(
        self,
    ):
        fixture = SyntheticFixture(self.root / "success")
        try:
            source = adopted_fixture(fixture, missing_persisted_lrt=True)
            before = fixture.store.get_snapshot(source["snapshot"]["snapshot_id"])
            with self.assertRaises(CompactApplicationError):
                fixture.service.import_legacy_source(
                    source, expected_sha256=json_sha256(source)
                )
            fixture.service.import_adopted_source(
                source, expected_sha256=json_sha256(source)
            )
            auth = fixture.service.authorize_next(
                fixture.collection,
                DEVICE,
                SESSION,
                collection_identity=PROFILE,
                expected_generation=0,
            )
            self.assertEqual(auth.mode, "adopted_restore")
            with count_active_answers(fixture) as calls:
                result = fixture.service.apply_pending(
                    fixture.collection,
                    DEVICE,
                    SESSION,
                    collection_identity=PROFILE,
                    expected_generation=0,
                    authorization=auth,
                )
            self.assertEqual(
                (result.status, calls[0]), ("applied", 1), result.to_dict()
            )
            app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
            self.assertEqual(json.loads(app.policy_json)["mode"], "adopted_restore")
            plan = json.loads(app.plan_json)
            self.assertEqual(plan["context_kind"], "adopted")
            self.assertEqual(plan["adopted_approval"], source["approval"])
            self.assertEqual(fixture.store.get_snapshot(before.snapshot_id), before)
            self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
            with count_active_answers(fixture, forbid=True) as calls:
                idle = fixture.service.recover_pending(
                    fixture.collection,
                    DEVICE,
                    SESSION,
                    collection_identity=PROFILE,
                    expected_generation=1,
                )
            self.assertEqual((idle.status, calls[0]), ("idle", 0))
        finally:
            fixture.close()

    def test_startup_generation_gap_adopted_preserves_current_not_capture_counters(
        self,
    ):
        fixture = SyntheticFixture(self.root / "startup-gap")
        try:
            source = adopted_fixture(fixture)
            fixture.service.import_adopted_source(
                source, expected_sha256=json_sha256(source)
            )
            # 模拟真实宿主启动的外部 operation +1，没有任何评分收据边。
            fixture.store.set_collection_generation(1)
            cid = int(source["events"][0]["card_id"])
            scope = tuple(int(d) for d in source["adopted_context"]["deck_contexts"])
            for did in scope:
                deck = fixture.collection._backend.get_deck(did)
                deck.common.review_studied += 13
                fixture.collection._backend.update_deck(deck)
            current = _capture_state(fixture.collection, cid, scope)["counters"]
            with count_active_answers(fixture) as calls:
                result = fixture.apply_one()
            self.assertEqual(
                (result.status, calls[0]), ("applied", 1), result.to_dict()
            )
            self.assertEqual(
                _capture_state(fixture.collection, cid, scope)["counters"], current
            )
            self.assertEqual(fixture.store.get_collection_generation(), 2)
            self.assertEqual(
                fixture.store.get_snapshot(
                    source["snapshot"]["snapshot_id"]
                ).application_context,
                None,
            )
        finally:
            fixture.close()

    def test_bad_source_and_unapproved_policy_reject_before_intent(self):
        for bad in ("approval", "snapshot", "event", "cards", "policy", "context"):
            with self.subTest(bad=bad):
                fixture = SyntheticFixture(self.root / bad)
                try:
                    source = adopted_fixture(fixture)
                    broken = copy.deepcopy(source)
                    if bad == "approval":
                        broken["approval"]["decision"] = "pending"
                    elif bad == "snapshot":
                        broken["snapshot"]["checksum"] = "f" * 64
                    elif bad == "event":
                        broken["events"][0]["payload_hash"] = "f" * 64
                    elif bad == "cards":
                        broken["cards"] = {}
                    elif bad == "policy":
                        broken["policy"]["load_balancing"] = "enabled"
                    else:
                        broken["adopted_context"]["profile_identity"] = "other"
                    frame = fixture.native_frame()
                    with self.assertRaises(CompactApplicationError):
                        fixture.service.import_adopted_source(
                            broken, expected_sha256=json_sha256(broken)
                        )
                    self.assertEqual(frame, fixture.native_frame())
                    self.assertIsNone(
                        fixture.store.query_compact_applications(DEVICE, SESSION)[
                            0
                        ].intent_id
                    )
                finally:
                    fixture.close()

    def test_prestate_or_revlog_drift_and_source_change_refuse_without_score(self):
        for drift in ("card", "revlog", "counter", "source"):
            with self.subTest(drift=drift):
                fixture = SyntheticFixture(self.root / drift)
                try:
                    source = adopted_fixture(fixture)
                    fixture.service.import_adopted_source(
                        source, expected_sha256=json_sha256(source)
                    )
                    auth = fixture.service.authorize_next(
                        fixture.collection,
                        DEVICE,
                        SESSION,
                        collection_identity=PROFILE,
                        expected_generation=0,
                    )
                    cid = int(source["events"][0]["card_id"])
                    if drift == "card":
                        card = fixture.collection.get_card(cid)
                        card.ivl += 1
                        fixture.collection.update_card(card)
                    elif drift == "revlog":
                        fixture.collection.db.execute(
                            "INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?,?)",
                            12345,
                            cid,
                            -1,
                            0,
                            7,
                            7,
                            2500,
                            0,
                            5,
                        )
                    elif drift == "counter":
                        did = int(source["cards"][str(cid)]["card"]["deck_id"])
                        deck = fixture.collection._backend.get_deck(did)
                        deck.common.review_studied += 1
                        fixture.collection._backend.update_deck(deck)
                    else:
                        source["approval"]["record_id"] = "changed-after-authorization"
                        fixture.service._legacy_path(
                            source["snapshot"]["snapshot_id"]
                        ).write_text(canonical_json(source))
                    before = fixture.native_frame()
                    with count_active_answers(fixture, forbid=True) as calls:
                        result = fixture.service.apply_pending(
                            fixture.collection,
                            DEVICE,
                            SESSION,
                            collection_identity=PROFILE,
                            expected_generation=0,
                            authorization=auth,
                        )
                    self.assertEqual(
                        (result.status, calls[0]), ("blocked", 0), result.to_dict()
                    )
                    self.assertEqual(before, fixture.native_frame())
                    self.assertIsNone(
                        fixture.store.query_compact_applications(DEVICE, SESSION)[
                            0
                        ].intent_id
                    )
                finally:
                    fixture.close()

    def test_sigkill_before_and_after_commit_preserves_attempt_and_never_repeats_commit(
        self,
    ):
        for phase, expected_calls in (("score_native", 1), ("score_committed", 0)):
            with self.subTest(phase=phase):
                root = self.root / phase
                fixture = SyntheticFixture(root)
                source = adopted_fixture(fixture)
                fixture.service.import_adopted_source(
                    source, expected_sha256=json_sha256(source)
                )
                fixture.close()
                run_sigkill(root, phase)
                fixture = SyntheticFixture(root, create=False)
                try:
                    app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
                    with count_active_answers(
                        fixture, forbid=expected_calls == 0
                    ) as calls:
                        result = fixture.service.recover_pending(
                            fixture.collection,
                            DEVICE,
                            SESSION,
                            collection_identity=PROFILE,
                            expected_generation=0,
                        )
                    self.assertEqual(
                        (result.status, calls[0]),
                        ("applied", expected_calls),
                        result.to_dict(),
                    )
                    final = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
                    self.assertEqual(
                        (app.intent_id, app.attempt_id),
                        (final.intent_id, final.attempt_id),
                    )
                    self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    self.assertEqual(
                        len(
                            fixture.collection.db.all(
                                "SELECT * FROM revlog WHERE cid=?",
                                int(source["events"][0]["card_id"]),
                            )
                        ),
                        1,
                    )
                finally:
                    fixture.close()
