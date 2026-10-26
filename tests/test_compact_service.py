"""真实 Anki 26.09.3 + ProfileStore 的有界生产链路与独立进程中断验证。

所有 Collection 均由本测试在 /private/tmp 新建。正常设备策略快照经有界 QueryOp
context producer 导出并原样发布；旧无 context 兼容入口只用于失败关闭检查。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

import anki.collection  # 必须先加载安装版 Collection，再加载其余 Anki 模块。
from anki import buildinfo
from anki.cards import FSRSMemoryState
from anki.collection import Collection
from anki.collection_pb2 import OpChanges
from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.anki.application_context import SnapshotApplicationContext
from inkcards_anki_bridge.anki.compact_260903 import AnkiCompactAdapter, _capture_state
from inkcards_anki_bridge.anki.compact_projection import canonical_json, json_sha256
from inkcards_anki_bridge.anki.operations import OperationContext
from inkcards_anki_bridge.services.compact_application import (
    CompactApplicationError,
    CompactApplicationService,
)
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import ProfileStore

PROFILE = "synthetic-native-profile"
DEVICE = "synthetic-device"
SESSION = "synthetic-session"
EVIDENCE: list[dict] = []
WEIGHTS = [
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194,
    0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629,
    1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
]
CRASH_PHASES = (
    "intent", "score_native", "score_committed", "score_evidence", "due_native", "due_evidence",
    "counter_partial", "counter_complete", "verified_evidence", "receipt",
)


class SyntheticFixture:
    """只创建或重开本测试生成的临时文件。"""

    def __init__(self, root: Path, *, create: bool = True, nested: bool = True) -> None:
        self.root = root.resolve()
        if not str(self.root).startswith("/private/tmp/inkcards-native-service-"):
            raise RuntimeError("拒绝打开非测试所有的 Collection")
        self.root.mkdir(parents=True, exist_ok=True)
        self.closed = False
        self.collection = Collection(str(self.root / "synthetic.anki2"))
        self.store = ProfileStore(self.root / "bridge", profile_id=PROFILE)
        if create:
            self.store.register_device(DEVICE, "synthetic-token")
            self.store.open_session(DEVICE, SESSION)
            col = self.collection
            # 仅此新建合成集合使用 SQL 设置旧创建时间，验证 scratch 时钟对齐。
            col.db.execute("UPDATE col SET crt=?", int(time.time()) - 90 * 86400)
            col.set_config("rollover", 4)
            col.set_config("localOffset", 480)
            col.set_config("creationOffset", 480)
            col.set_config("fsrs", True)
            parent = int(col.decks.id("Native"))
            deck_a = int(col.decks.id("Native::A" if nested else "Native A"))
            deck_b = int(col.decks.id("Native::B" if nested else "Native B"))
            preset_ids = []
            for name, deck_id, retention, weights in (
                ("Native preset A", deck_a, 0.9, []),
                ("Native preset B", deck_b, 0.82, WEIGHTS),
            ):
                config = col.decks.add_config(name)
                config["desiredRetention"] = retention
                config["fsrsParams6"] = list(weights)
                config["rev"]["maxIvl"] = 36500
                config["maxTaken"] = 60
                col.decks.save(config)
                deck = col.decks.get(deck_id)
                deck["conf"] = int(config["id"])
                col.decks.update_dict(deck)
                preset_ids.append(int(config["id"]))
            card_ids = []
            for index, deck_id in enumerate((deck_a, deck_b)):
                note = col.new_note(col.models.by_name("Basic"))
                note.fields[0] = f"合成问题 {index}"
                note.fields[1] = f"合成答案 {index}"
                col.add_note(note, deck_id)
                card_id = int(col.card_ids_of_note(note.id)[0])
                card = col.get_card(card_id)
                card.type = 2
                card.queue = 2
                card.due = int(col.sched.today) - 1
                card.ivl = 7
                card.factor = 2500
                card.reps = 1
                card.lapses = 0
                card.left = 0
                card.last_review_time = int(time.time()) - 7 * 86400
                card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
                card.custom_data = canonical_json({"other": "ok"})
                col.update_card(card)
                card_ids.append(card_id)
            self.meta = {"card_ids": card_ids, "deck_ids": [deck_a, deck_b], "parent": parent, "preset_ids": preset_ids}
            (self.root / "meta.json").write_text(canonical_json(self.meta), encoding="utf-8")
            self.set_counters(old_day=False)
            col.decks.select(parent)
        else:
            self.meta = json.loads((self.root / "meta.json").read_text(encoding="utf-8"))
        self.adapter = AnkiCompactAdapter()
        self.service = CompactApplicationService(self.store, self.adapter)

    def close(self) -> None:
        if self.closed:
            return
        self.collection.close()
        self.store.close()
        self.closed = True

    def set_counters(self, *, old_day: bool) -> None:
        for index, deck_id in enumerate((*self.meta["deck_ids"], self.meta["parent"])):
            deck = self.collection._backend.get_deck(deck_id)
            deck.common.last_day_studied = int(self.collection.sched.today) - (2 if old_day else 0)
            deck.common.new_studied = 3 + index
            deck.common.review_studied = 7 + index
            deck.common.learning_studied = 2 + index
            deck.common.milliseconds_studied = 21000 + index * 1000
            self.collection._backend.update_deck(deck)

    def times(self, *, historical: bool = False) -> list[int]:
        start = int(self.collection.sched.day_cutoff) - 86400
        if historical:
            return [(start - 3600) * 1000 + index * 1100 for index in range(3)]
        elapsed = int(time.time()) - start
        if elapsed < 12:
            raise RuntimeError("日界后不足 12 秒，当前日交错评分夹具暂不可用")
        return [(start + elapsed * fraction // 4) * 1000 + index for index, fraction in enumerate((1, 2, 3))]

    def export_snapshot_from_query_context(self):
        generation = int(self.store.get_collection_generation())
        runtime_epoch = "synthetic-runtime-epoch"
        context = OperationContext(PROFILE, generation, runtime_epoch)
        exporter = Anki260801Adapter(
            collection_provider=lambda: self.collection,
            profile_provider=lambda: PROFILE,
            profile_identity=PROFILE,
            anki_version=buildinfo.version,
            anki_build=buildinfo.buildhash,
            generation_provider=self.store.get_collection_generation,
            runtime_epoch_provider=lambda: runtime_epoch,
        )
        return exporter.export_snapshot_from_collection(
            self.collection,
            card_ids=[str(card_id) for card_id in self.meta["card_ids"]],
            context=context,
        )

    def publish_production_snapshot(self) -> str:
        typed = self.export_snapshot_from_query_context()
        return SnapshotService(self.store).publish(
            typed,
            scope_id="all",
            generation=int(typed.manifest.profile_generation),
            applied_watermark=0,
        ).snapshot_id

    def receive(self, snapshot_id: str, rows: list[list[int]], *, session: str = SESSION) -> None:
        receipts, conflict = self.store.receive_compact_batch(DEVICE, session, snapshot_id, 1, rows)
        if conflict or len(receipts) != len(rows):
            raise RuntimeError("合成收件失败")

    def apply_one(self, *, session: str = SESSION):
        generation = self.store.get_collection_generation()
        authorization = self.service.authorize_next(
            self.collection, DEVICE, session, collection_identity=PROFILE, expected_generation=generation,
        )
        return self.service.apply_pending(
            self.collection, DEVICE, session, collection_identity=PROFILE,
            expected_generation=generation, authorization=authorization,
        )

    def native_frame(self) -> dict:
        col = self.collection
        return {
            "cards": [_capture_state(col, cid, (*self.meta["deck_ids"], self.meta["parent"])) for cid in self.meta["card_ids"]],
            "undo": col.undo_status().SerializeToString(deterministic=True).hex(),
            "config": col.all_config(),
            "decks": [col._backend.get_deck(did).SerializeToString(deterministic=True).hex() for did in (*self.meta["deck_ids"], self.meta["parent"])],
            "load_balancer": col.load_balancer_enabled,
            "queue": col.sched.get_queued_cards(fetch_limit=50).SerializeToString(deterministic=True).hex(),
        }


@contextmanager
def count_active_answers(fixture: SyntheticFixture, *, forbid: bool = False):
    calls = [0]
    scheduler_type = type(fixture.collection.sched)
    original = scheduler_type.answer_card

    def observed(scheduler, answer):
        # Anki 的 scheduler.col 是 Collection 的弱引用代理；同一对象的身份比较也会为假。
        if scheduler.col == fixture.collection:
            calls[0] += 1
            if forbid:
                raise AssertionError("已提交评分的恢复禁止再次调用 answer_card")
        return original(scheduler, answer)

    with mock.patch.object(scheduler_type, "answer_card", observed):
        yield calls


def checkpoint(phase: str) -> None:
    print("INKCARDS_CHECKPOINT " + canonical_json({"phase": phase, "pid": os.getpid()}), flush=True)
    # 父进程只对该测试所有的子进程发送真实 SIGKILL；不正常关闭数据库。
    sys.stdin.read()
    raise RuntimeError("checkpoint 不应通过 stdin 继续")


def crash_worker(root: Path, phase: str) -> None:
    fixture = SyntheticFixture(root, create=False)
    col, store = fixture.collection, fixture.store
    original_record = store.record_compact_score_evidence

    def record(*args, **kwargs):
        value = original_record(*args, **kwargs)
        selected = {"score_started": "intent", "score_applied": "score_evidence", "due_corrected": "due_evidence", "score_verified": "verified_evidence"}
        if selected.get(kwargs["stage"]) == phase:
            checkpoint(phase)
        return value

    store.record_compact_score_evidence = record
    original_answer = type(col.sched).answer_card

    def answer(scheduler, value):
        changes = original_answer(scheduler, value)
        # 与计数器相同：Anki scheduler.col 是 Collection 的弱引用代理。
        if scheduler.col == col and phase == "score_native":
            checkpoint(phase)
        return changes

    original_update = col.update_card

    def update(card, *args, **kwargs):
        changes = original_update(card, *args, **kwargs)
        if phase == "due_native" and "inkcards" in json.loads(card.custom_data or "{}"):
            checkpoint(phase)
        return changes

    col.update_card = update
    original_deck_update = type(col._backend).update_deck
    original_commit = type(col._backend).db_commit

    def commit(backend):
        value = original_commit(backend)
        if backend is col._backend and phase == "score_committed":
            checkpoint(phase)
        return value
    repaired_decks = [0]

    def update_deck(backend, deck):
        changes = original_deck_update(backend, deck)
        if backend is col._backend:
            repaired_decks[0] += 1
            if (phase == "counter_partial" and repaired_decks[0] == 1) or (phase == "counter_complete" and repaired_decks[0] == 2):
                checkpoint(phase)
        return changes

    original_finish = store.finish_compact_apply

    def finish(*args, **kwargs):
        value = original_finish(*args, **kwargs)
        if phase == "receipt":
            checkpoint(phase)
        return value

    store.finish_compact_apply = finish
    with mock.patch.object(type(col.sched), "answer_card", answer), mock.patch.object(type(col._backend), "update_deck", update_deck), mock.patch.object(type(col._backend), "db_commit", commit):
        result = fixture.apply_one()
        print("INKCARDS_NO_CHECKPOINT " + canonical_json(result.to_dict()), flush=True)
    fixture.close()
    raise RuntimeError("目标中断点未达到")


def run_sigkill(root: Path, phase: str) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--crash-worker", str(root), phase],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    deadline = time.monotonic() + 30
    lines = []
    reached = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                line = process.stdout.readline()
                lines.append(line)
                if line.startswith("INKCARDS_CHECKPOINT "):
                    reached = json.loads(line.split(" ", 1)[1])
                    break
            if process.poll() is not None:
                break
        if reached is None:
            process.kill()
            out, err = process.communicate(timeout=5)
            raise AssertionError("中断点未达到: " + "".join(lines) + out + err)
        if reached["phase"] != phase or reached["pid"] != process.pid:
            raise AssertionError("checkpoint 身份不符")
        os.kill(process.pid, signal.SIGKILL)
        out, err = process.communicate(timeout=5)
        if process.returncode != -signal.SIGKILL:
            raise AssertionError("未由真实 SIGKILL 终止")
        return {"phase": phase, "pid": process.pid, "signal": "SIGKILL", "returncode": process.returncode, "stdout": "".join(lines) + out, "stderr": err}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


class CompactServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="inkcards-native-service-", dir="/private/tmp")
        self.root = Path(self.tempdir.name)
        self.fixture = SyntheticFixture(self.root / "main")

    def tearDown(self) -> None:
        if self.fixture is not None:
            self.fixture.close()
        self.tempdir.cleanup()

    def assert_empty_changes(self, result) -> None:
        self.assertIsInstance(result.changes, OpChanges)
        self.assertEqual(list(result.changes.ListFields()), [])

    def test_production_snapshot_previous_day_upload_and_prior_day_event(self) -> None:
        self.fixture.close()
        self.fixture = fixture = SyntheticFixture(self.root / "root-clock", nested=False)
        scheduler_type = type(fixture.collection.sched)
        today, cutoff = int(fixture.collection.sched.today), int(fixture.collection.sched.day_cutoff)
        # 仅模拟导出时的 D 日读时钟；producer、Collection、发布与评分均是真实实现。
        with mock.patch.object(scheduler_type, "today", new_callable=mock.PropertyMock, return_value=today - 1), mock.patch.object(scheduler_type, "day_cutoff", new_callable=mock.PropertyMock, return_value=cutoff - 86400):
            snapshot_id = fixture.publish_production_snapshot()
        stored = fixture.store.get_snapshot(snapshot_id)
        context = stored.application_context
        self.assertIsNotNone(context)
        self.assertEqual(context.source.scheduler_today, today - 1)
        self.assertEqual(context.source.anki_version, "26.9.3")
        self.assertNotIn("application_context", json.loads(stored.payload_json))
        self.assertEqual(hashlib.sha256(stored.payload_json.encode()).hexdigest(), context.snapshot_checksum)
        prior_ms = (cutoff - 2 * 86400 - 3600) * 1000
        current_ms = fixture.times()[1]
        rows = [[1, 3, prior_ms, 120], [1, 4, current_ms, 130]]
        fixture.receive(snapshot_id, rows)
        fixture.receive(snapshot_id, rows)
        self.assertEqual(len(fixture.store.query_compact_events(DEVICE, SESSION)), 2)
        with count_active_answers(fixture) as calls:
            results = [fixture.apply_one()]
            self.assertEqual(calls[0], 1)
            results.append(fixture.apply_one())
            self.assertEqual(calls[0], 2)
        self.assertEqual([r.status for r in results], ["applied", "applied"], [r.to_dict() for r in results])
        self.assertEqual(calls[0], 2)
        apps = fixture.store.query_compact_applications(DEVICE, SESSION)
        self.assertEqual([app.collection_generation_after for app in apps], [1, 2])
        self.assertEqual(context.source.collection_generation, 0)
        plans = [json.loads(app.plan_json) for app in apps]
        self.assertEqual([plan["projected"]["event_collection_day"] for plan in plans], [today - 2, today])
        self.assertTrue(all(plan["policy"]["mode"] == "device_policy" for plan in plans))
        self.assertTrue(all(plan["preset"]["max_interval_days"] == 36500 for plan in plans))
        self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
        EVIDENCE.append({"case": "producer_D_upload_D_plus_1", "status": "PASS", "captured_day": today - 1, "upload_day": today, "event_days": [today - 2, today], "answer_calls": calls[0], "source_generation": 0, "current_generation": 2, "clock_simulation": "Scheduler today/cutoff properties only during real producer export"})

    def test_production_sidecar_binding_and_context_config_changes_are_no_write(self) -> None:
        for tamper in ("binding", "short_term", "clock", "unsupported", "max_interval"):
            with self.subTest(tamper=tamper):
                fixture = SyntheticFixture(self.root / ("context-" + tamper), nested=False)
                try:
                    snapshot_id = fixture.publish_production_snapshot()
                    fixture.receive(snapshot_id, [[1, 3, fixture.times()[0], 120]])
                    record = fixture.store.get_snapshot(snapshot_id)
                    context = record.application_context
                    altered = record
                    if tamper == "binding":
                        altered = replace(record, application_context=SnapshotApplicationContext.bind(context.source, profile_id=context.profile_id, snapshot_id="other-snapshot", snapshot_checksum=context.snapshot_checksum, snapshot_generation=context.snapshot_generation))
                    elif tamper == "unsupported":
                        source = replace(context.source, unsupported=("preset_config_conflict",))
                        altered = replace(record, application_context=SnapshotApplicationContext.bind(source, profile_id=context.profile_id, snapshot_id=context.snapshot_id, snapshot_checksum=context.snapshot_checksum, snapshot_generation=context.snapshot_generation))
                    elif tamper == "short_term":
                        from anki.collection import Config
                        fixture.collection.set_config_bool(Config.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED, not context.source.fsrs_short_term_with_steps_enabled)
                    elif tamper == "max_interval":
                        config = fixture.collection.decks.config_dict_for_deck_id(fixture.meta["deck_ids"][0])
                        config["rev"]["maxIvl"] = 1234
                        fixture.collection.decks.save(config)
                    else:
                        fixture.collection.set_config("rollover", 5)
                    before = fixture.native_frame()
                    with mock.patch.object(fixture.store, "get_snapshot", return_value=altered), count_active_answers(fixture, forbid=True) as calls:
                        with self.assertRaises(CompactApplicationError) as caught:
                            fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(fixture.native_frame(), before)
                    self.assertIsNone(fixture.store.query_compact_applications(DEVICE, SESSION)[0].intent_id)
                    EVIDENCE.append({"case": "context_no_write_" + tamper, "status": "PASS", "reason": caught.exception.code, "answer_calls": 0})
                finally:
                    fixture.close()

    def test_production_review_scope_and_captured_nondefault_interval_limit(self) -> None:
        # 四态已支持；仅非法 type/queue 组合仍须在 intent 前拒绝。
        for kind, card_type, queue, rating in (("invalid_new_review", 0, 2, 3), ("invalid_review_learning", 2, 1, 3)):
            with self.subTest(kind=kind):
                fixture = SyntheticFixture(self.root / ("scope-" + kind), nested=False)
                try:
                    card = fixture.collection.get_card(fixture.meta["card_ids"][0])
                    card.type, card.queue = card_type, queue
                    if queue == 0:
                        card.due = 1
                    elif queue == 1:
                        card.due = int(time.time()) - 60
                    fixture.collection.update_card(card)
                    snapshot_id = fixture.publish_production_snapshot()
                    fixture.receive(snapshot_id, [[1, rating, fixture.times()[0], 120]])
                    before = fixture.native_frame()
                    with count_active_answers(fixture, forbid=True) as calls:
                        with self.assertRaises(CompactApplicationError) as caught:
                            fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
                    self.assertEqual(caught.exception.code, "scheduler_state_unsupported")
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(fixture.native_frame(), before)
                    self.assertIsNone(fixture.store.query_compact_applications(DEVICE, SESSION)[0].intent_id)
                    EVIDENCE.append({"case": "production_scope_" + kind, "status": "PASS", "reason": caught.exception.code, "answer_calls": 0})
                finally:
                    fixture.close()
        fixture = SyntheticFixture(self.root / "max-interval-seven", nested=False)
        try:
            config = fixture.collection.decks.config_dict_for_deck_id(fixture.meta["deck_ids"][0])
            config["rev"]["maxIvl"] = 7
            fixture.collection.decks.save(config)
            snapshot_id = fixture.publish_production_snapshot()
            fixture.receive(snapshot_id, [[1, 4, fixture.times()[0], 120]])
            with count_active_answers(fixture) as calls:
                result = fixture.apply_one()
            self.assertEqual(result.status, "applied", result.to_dict())
            self.assertEqual(calls[0], 1)
            app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
            self.assertEqual(json.loads(app.plan_json)["preset"]["max_interval_days"], 7)
            self.assertLessEqual(fixture.collection.get_card(fixture.meta["card_ids"][0]).ivl, 7)
            EVIDENCE.append({"case": "captured_maxIvl_seven", "status": "PASS", "answer_calls": 1, "max_interval_days": 7})
        finally:
            fixture.close()

    def test_v2_frozen_parent_identity_and_counter_baseline_are_no_write(self) -> None:
        for tamper in ("parent_name", "parent_counter", "child_counter"):
            with self.subTest(tamper=tamper):
                fixture = SyntheticFixture(self.root / ("v2-" + tamper))
                try:
                    snapshot_id = fixture.publish_production_snapshot()
                    fixture.receive(snapshot_id, [[1, 3, fixture.times()[0], 120]])
                    did = fixture.meta["deck_ids"][0] if tamper == "child_counter" else fixture.meta["parent"]
                    deck = fixture.collection._backend.get_deck(did)
                    if tamper == "parent_name":
                        deck.name = "Changed Native"
                    else:
                        deck.common.review_studied += 17
                    fixture.collection._backend.update_deck(deck)
                    before = fixture.native_frame()
                    with count_active_answers(fixture) as calls:
                        with self.assertRaises(CompactApplicationError) as caught:
                            fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
                    self.assertIn(caught.exception.code, {"snapshot_parent_deck_conflict", "snapshot_deck_name_conflict", "snapshot_counter_baseline_conflict"})
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(fixture.native_frame(), before)
                    self.assertIsNone(fixture.store.query_compact_applications(DEVICE, SESSION)[0].intent_id)
                    self.assertFalse(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    EVIDENCE.append({"case": "v2_" + tamper, "status": "PASS", "reason": caught.exception.code, "answer_calls": calls[0], "native_no_write": True})
                finally:
                    fixture.close()

    def test_v2_receipt_overlay_conflicts_reject_new_card_before_intent(self) -> None:
        # 版本间隙现在由 test_generation_compatibility 验证：有效证明允许，
        # 缺失/伪造证明拒绝；原 Store 无证明跳代断言仍保留。
        for tamper in ("parent_counter", "child_counter", "receipt_hash"):
            with self.subTest(tamper=tamper):
                fixture = SyntheticFixture(self.root / ("overlay-" + tamper))
                try:
                    snapshot_id = fixture.publish_production_snapshot()
                    times = fixture.times()
                    fixture.receive(snapshot_id, [[1, 3, times[0], 120], [2, 4, times[1], 130]])
                    with count_active_answers(fixture) as first_calls:
                        first = fixture.apply_one()
                    self.assertEqual((first.status, first_calls[0]), ("applied", 1), first.to_dict())
                    if tamper in ("parent_counter", "child_counter"):
                        did = fixture.meta["parent"] if tamper == "parent_counter" else fixture.meta["deck_ids"][1]
                        deck = fixture.collection._backend.get_deck(did)
                        deck.common.review_studied += 17
                        fixture.collection._backend.update_deck(deck)
                    else:
                        # 仅篡改测试所有 SQLite 行，不触及真实用户卡库或生产 schema。
                        db = fixture.store.connect()
                        try:
                            db.execute("UPDATE compact_applications SET receipt_hash=? WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=1", ("f" * 64, PROFILE, DEVICE, SESSION))
                            db.commit()
                        finally:
                            db.close()
                    before = fixture.native_frame()
                    with count_active_answers(fixture, forbid=True) as calls:
                        with self.assertRaises(CompactApplicationError) as caught:
                            fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=fixture.store.get_collection_generation())
                    self.assertIn(caught.exception.code, {"snapshot_counter_baseline_conflict", "snapshot_counter_receipt_path_invalid", "compact_predecessor_receipt_invalid"})
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(fixture.native_frame(), before)
                    second = fixture.store.query_compact_applications(DEVICE, SESSION)[1]
                    self.assertIsNone(second.intent_id)
                    self.assertFalse(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    EVIDENCE.append({"case": "v2_overlay_" + tamper, "status": "PASS", "reason": caught.exception.code, "answer_calls": calls[0], "native_no_write": True, "intent_created": False})
                finally:
                    fixture.close()

    def test_sigkill_intent_recovery_uses_clamped_frozen_duration(self) -> None:
        root = self.root / "clamped-intent"
        fixture = SyntheticFixture(root)
        fixture.set_counters(old_day=True)
        snapshot_id = fixture.publish_production_snapshot()
        fixture.receive(snapshot_id, [[1, 3, fixture.times(historical=True)[0], 90000]])
        fixture.close()
        killed = run_sigkill(root, "intent")
        fixture = SyntheticFixture(root, create=False)
        try:
            with count_active_answers(fixture) as calls:
                result = fixture.service.recover_pending(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
            self.assertEqual(result.status, "applied", result.to_dict())
            self.assertEqual(calls[0], 1)
            log = fixture.collection.db.all("SELECT time FROM revlog WHERE cid=?", fixture.meta["card_ids"][0])
            self.assertEqual([int(row[0]) for row in log], [60000])
            self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
            EVIDENCE.append({"case": "sigkill_intent_clamped_duration", "status": "PASS", "process": killed, "answer_calls": 1, "review_time": 60000})
        finally:
            fixture.close()

    def test_readonly_preflight_default_lb_old_clock_and_two_effective_presets(self) -> None:
        fixture = self.fixture
        snapshot_id = fixture.publish_production_snapshot()
        times = fixture.times()
        fixture.receive(snapshot_id, [[1, 3, times[0], 120], [2, 4, times[1], 130]])
        self.assertTrue(fixture.collection.load_balancer_enabled)
        before = fixture.native_frame()
        auth = fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
        event = fixture.store.query_compact_events(DEVICE, SESSION)[0]
        prepared = fixture.adapter.prepare_event(
            fixture.collection, event, fixture.store.get_snapshot(snapshot_id), auth,
            collection_identity=PROFILE, collection_generation=0, intent_id="readonly-intent", attempt_id="readonly-attempt",
        )
        self.assertEqual(fixture.native_frame(), before)
        self.assertEqual(fixture.store.query_compact_applications(DEVICE, SESSION)[0].status, "not_applied")
        self.assertEqual(fixture.store.query_compact_score_evidence(DEVICE, SESSION, 1), [])
        self.assertEqual(prepared.plan["policy"]["policy_id"], "fixed-ordinary-fuzz-v1")
        self.assertEqual(prepared.plan["preset"]["weights"], [])
        first = fixture.apply_one()
        self.assertEqual(first.status, "applied", first.to_dict())
        second_auth = fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=1)
        second = fixture.adapter.prepare_event(
            fixture.collection, fixture.store.query_compact_events(DEVICE, SESSION)[1], fixture.store.get_snapshot(snapshot_id), second_auth,
            collection_identity=PROFILE, collection_generation=1, intent_id="readonly-intent-b", attempt_id="readonly-attempt-b",
            expected_counters=fixture.service._snapshot_counter_baseline(fixture.store.get_snapshot(snapshot_id), PROFILE, 1),
        )
        self.assertNotEqual(prepared.plan["preset"]["preset_id"], second.plan["preset"]["preset_id"])
        self.assertEqual(len(second.plan["preset"]["weights"]), 21)
        self.assertAlmostEqual(prepared.plan["preset"]["desired_retention"], 0.9)
        self.assertAlmostEqual(second.plan["preset"]["desired_retention"], 0.82)
        EVIDENCE.append({"case": "readonly_old_clock_default_lb_two_presets", "status": "PASS", "today": int(fixture.collection.sched.today), "presets": [prepared.plan["preset"], second.plan["preset"]]})

    def test_aba_mixed_presets_reopen_and_actual_stage_readbacks(self) -> None:
        fixture = self.fixture
        snapshot_id = fixture.publish_production_snapshot()
        times = fixture.times()
        fixture.receive(snapshot_id, [[1, 2, times[0], 120], [2, 3, times[1], 130], [1, 4, times[2], 140]])
        actual_stages = []
        original_record = fixture.store.record_compact_score_evidence

        def record(*args, **kwargs):
            if kwargs["stage"] in {"score_applied", "due_corrected", "score_verified"}:
                value = kwargs["evidence_json"]
                event = fixture.store.query_compact_events(DEVICE, SESSION)[int(args[2]) - 1]
                current = fixture.collection.get_card(int(event.card_id))
                self.assertEqual(value["native_observed"]["card"]["schedule_revision"], str(current.mod))
                self.assertEqual(value["native_observed"]["native_card_hex"], fixture.collection._backend.get_card(int(event.card_id)).SerializeToString(deterministic=True).hex())
                logs = fixture.collection.db.all("SELECT id,ease,time FROM revlog WHERE cid=? ORDER BY id", int(event.card_id))
                self.assertEqual([(row["id"], row["ease"], row["review_time"]) for row in value["observed"]["review_logs"]], [tuple(row) for row in logs])
                self.assertEqual(value["observed"]["card"]["memory_state"], {"stability": float(current.memory_state.stability), "difficulty": float(current.memory_state.difficulty)})
                actual_stages.append({"sequence": int(args[2]), "stage": kwargs["stage"], "evidence": copy.deepcopy(value)})
            return original_record(*args, **kwargs)

        fixture.store.record_compact_score_evidence = record
        with count_active_answers(fixture) as calls:
            first = fixture.apply_one()
            self.assertEqual(calls[0], 1)
            second = fixture.apply_one()
            self.assertEqual(calls[0], 2)
            self.assertEqual((first.status, second.status), ("applied", "applied"), (first.to_dict(), second.to_dict()))
            self.assertEqual(calls[0], 2)
        fixture.close()
        self.fixture = fixture = SyntheticFixture(self.root / "main", create=False)
        original_record = fixture.store.record_compact_score_evidence
        fixture.store.record_compact_score_evidence = record
        with count_active_answers(fixture) as calls:
            third = fixture.apply_one()
            self.assertEqual(third.status, "applied", third.to_dict())
            self.assertEqual(calls[0], 1)
        apps = fixture.store.query_compact_applications(DEVICE, SESSION)
        self.assertEqual([app.collection_generation_after for app in apps], [1, 2, 3])
        self.assertEqual(fixture.store.get_collection_generation(), 3)
        self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
        for app in apps:
            self.assertIsNotNone(app.receipt_hash)
            plan = json.loads(app.plan_json)
            for stage in fixture.store.query_compact_score_evidence(DEVICE, SESSION, app.sequence):
                if stage.stage == "score_started":
                    continue
                evidence = json.loads(stage.evidence_json)
                self.assertEqual(evidence["binding"]["plan_hash"], hashlib.sha256(app.plan_json.encode()).hexdigest())
                self.assertNotEqual(evidence["binding"]["plan_hash"], plan["plan_sha256"])
                self.assertEqual(evidence["binding"]["marker_binding"], plan["marker_binding"])
                self.assertEqual(hashlib.sha256(stage.evidence_json.encode()).hexdigest(), stage.evidence_hash)
            after = json.loads(app.after_json)
            self.assertEqual(json.loads(after["card"]["custom_data"])["other"], "ok")
            self.assertEqual(after, json.loads(app.expected_after_json))
        for result in (first, second, third):
            self.assertIsInstance(result.changes, OpChanges)
            self.assertTrue(result.changes.card)
            self.assertTrue(result.refresh_context)
        self.assertEqual(len(actual_stages), 9)
        last_auth = fixture.service._predecessor_after({"card_id": str(fixture.meta["card_ids"][0]), "sequence": 4, "snapshot_id": snapshot_id}, DEVICE, SESSION)
        self.assertIn("native_card_hex", last_auth)
        idle = fixture.service.recover_pending(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=3)
        self.assertEqual(idle.status, "idle")
        self.assert_empty_changes(idle)
        EVIDENCE.append({"case": "aba_mixed_presets_reopen", "status": "PASS", "generations": [0, 1, 2, 3], "actual_stages": actual_stages, "results": [result.to_dict() for result in (first, second, third)]})

    def test_same_card_across_sessions_requires_and_uses_proven_native_receipt(self) -> None:
        fixture = self.fixture
        snapshot_id = fixture.publish_production_snapshot()
        times = fixture.times()
        fixture.receive(snapshot_id, [[1, 3, times[0], 120]])
        with count_active_answers(fixture) as first_calls:
            first = fixture.apply_one()
        self.assertEqual(first_calls[0], 1)
        self.assertEqual(first.status, "applied", first.to_dict())
        fixture.close()
        self.fixture = fixture = SyntheticFixture(self.root / "main", create=False)
        fixture.store.open_session(DEVICE, "session-two")
        snapshot_two = fixture.publish_production_snapshot()
        fixture.receive(snapshot_two, [[1, 4, times[2], 150]], session="session-two")
        with count_active_answers(fixture) as calls:
            second = fixture.apply_one(session="session-two")
            self.assertEqual(second.status, "applied", second.to_dict())
            self.assertEqual(calls[0], 1)
        self.assertEqual(fixture.store.get_collection_generation(), 2)
        self.assertTrue(fixture.store.pull_gate(DEVICE, "session-two").allowed)
        self.assertEqual(len(fixture.collection.db.all("SELECT id FROM revlog WHERE cid=?", fixture.meta["card_ids"][0])), 2)
        EVIDENCE.append({"case": "same_card_cross_session_reopen", "status": "PASS", "results": [first.to_dict(), second.to_dict()]})

    def test_historical_due_and_all_old_day_parent_child_counters_restored(self) -> None:
        fixture = self.fixture
        fixture.set_counters(old_day=True)
        snapshot_id = fixture.publish_production_snapshot()
        times = fixture.times(historical=True)
        fixture.receive(snapshot_id, [[1, 3, times[0], 90000]])
        before = _capture_state(fixture.collection, fixture.meta["card_ids"][0], (fixture.meta["deck_ids"][0], fixture.meta["parent"]))
        result = fixture.apply_one()
        self.assertEqual(result.status, "applied", result.to_dict())
        app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
        plan = json.loads(app.plan_json)
        after = _capture_state(fixture.collection, fixture.meta["card_ids"][0], (fixture.meta["deck_ids"][0], fixture.meta["parent"]))
        self.assertEqual(after["counters"], before["counters"])
        self.assertEqual(after["card"]["due"], plan["projected"]["historical_due"])
        self.assertEqual(plan["expected_score_after"]["score_review_log"]["review_time"], 60000)
        self.assertEqual(after["review_logs"][-1]["review_time"], 60000)
        self.assertTrue(result.changes.deck)
        EVIDENCE.append({"case": "historical_due_old_day_all_counters", "status": "PASS", "before": before, "after": after, "plan": plan})

    def test_compact_score_returns_real_changes_and_undo_restores_native_state(self) -> None:
        fixture = self.fixture
        snapshot_id = fixture.publish_production_snapshot()
        fixture.receive(snapshot_id, [[1, 3, fixture.times()[0], 120]])
        card_id = fixture.meta["card_ids"][0]
        deck_ids = (*fixture.meta["deck_ids"], fixture.meta["parent"])
        before = _capture_state(fixture.collection, card_id, deck_ids)
        undo_before = fixture.collection.undo_status().SerializeToString(deterministic=True)

        with count_active_answers(fixture) as calls:
            result = fixture.apply_one()
        self.assertEqual(result.status, "applied", result.to_dict())
        self.assertEqual(calls[0], 1)
        self.assertIsInstance(result.changes, OpChanges)
        self.assertTrue(result.changes.card)
        self.assertNotEqual(list(result.changes.ListFields()), [])
        self.assertNotEqual(
            fixture.collection.undo_status().SerializeToString(deterministic=True),
            undo_before,
        )

        scored = _capture_state(fixture.collection, card_id, deck_ids)
        self.assertEqual(len(scored["review_logs"]), len(before["review_logs"]) + 1)
        undo_output = fixture.collection.undo()
        undo_changes = undo_output.changes
        self.assertIsInstance(undo_changes, OpChanges)
        self.assertTrue(undo_changes.card)
        restored = _capture_state(fixture.collection, card_id, deck_ids)
        for field in (
            "type", "queue", "due", "ivl", "factor", "reps", "lapses", "left",
            "last_review_time", "memory_state", "custom_data",
        ):
            self.assertEqual(restored["card"].get(field), before["card"].get(field), field)
        self.assertEqual(restored["review_logs"], before["review_logs"])
        self.assertEqual(restored["counters"], before["counters"])
        EVIDENCE.append({
            "case": "compact_score_real_opchanges_undo",
            "status": "PASS",
            "answer_calls": calls[0],
            "score_changes": result.changes.SerializeToString(deterministic=True).hex(),
            "undo_changes": undo_changes.SerializeToString(deterministic=True).hex(),
            "review_logs_after_undo": len(restored["review_logs"]),
        })

    def test_unexpected_scheduler_failure_returns_only_observed_native_changes(self) -> None:
        fixture = self.fixture
        snapshot_id = fixture.publish_production_snapshot()
        fixture.receive(snapshot_id, [[1, 3, fixture.times()[0], 120]])
        observed_changes: list[OpChanges] = []
        answer_calls = [0]
        original_update = fixture.collection.update_card
        original_answer = type(fixture.collection.sched).answer_card
        before = _capture_state(fixture.collection, fixture.meta['card_ids'][0], (fixture.meta['deck_ids'][0], fixture.meta['parent']))

        def capture_update(card, *args, **kwargs):
            changes = original_update(card, *args, **kwargs)
            observed_changes.append(changes)
            return changes

        def fail_answer(_scheduler, _answer):
            if _scheduler.col != fixture.collection:
                return original_answer(_scheduler, _answer)
            answer_calls[0] += 1
            raise RuntimeError("injected backend failure before answer commit")

        fixture.collection.update_card = capture_update
        try:
            with mock.patch.object(type(fixture.collection.sched), "answer_card", fail_answer):
                result = fixture.apply_one()
        finally:
            fixture.collection.update_card = original_update

        self.assertEqual(result.status, "unknown", result.to_dict())
        self.assertEqual(result.reason, "native_transaction_rolled_back")
        self.assertEqual(answer_calls[0], 1)
        self.assertEqual(len(observed_changes), 1)
        self.assertIsInstance(observed_changes[0], OpChanges)
        self.assertTrue(observed_changes[0].card)
        self.assert_empty_changes(result)
        self.assertFalse(result.refresh_context)
        self.assertEqual(_capture_state(fixture.collection, fixture.meta['card_ids'][0], (fixture.meta['deck_ids'][0], fixture.meta['parent'])), before)
        application = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
        self.assertEqual(application.status, "applying")
        EVIDENCE.append({
            "case": "unexpected_scheduler_failure_real_changes",
            "status": "PASS",
            "answer_calls": answer_calls[0],
            "observed_native_changes": observed_changes[0].SerializeToString(deterministic=True).hex(),
            "returned_changes": result.changes.SerializeToString(deterministic=True).hex(),
            "result": result.to_dict(),
        })

    def test_stale_authorization_foreign_marker_memory_mod_flags_and_capacity_have_no_native_writes(self) -> None:
        for tamper in ("foreign_marker", "memory_001", "mod_only", "flags", "capacity"):
            with self.subTest(tamper=tamper):
                fixture = SyntheticFixture(self.root / tamper)
                try:
                    if tamper == "capacity":
                        card = fixture.collection.get_card(fixture.meta["card_ids"][0])
                        card.custom_data = canonical_json({"other": "x" * 20})
                        fixture.collection.update_card(card)
                    snapshot_id = fixture.publish_production_snapshot()
                    fixture.receive(snapshot_id, [[1, 3, fixture.times()[0], 120]])
                    authorization = fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
                    card = fixture.collection.get_card(fixture.meta["card_ids"][0])
                    if tamper == "foreign_marker":
                        card.custom_data = canonical_json({"other": "ok", "inkcards": "f" * 64})
                    elif tamper == "capacity":
                        pass
                    elif tamper == "memory_001":
                        card.memory_state = FSRSMemoryState(stability=float(card.memory_state.stability), difficulty=float(card.memory_state.difficulty) + 0.001)
                    elif tamper == "flags":
                        card.flags = 1
                    else:
                        time.sleep(max(0.01, int(card.mod) + 1.05 - time.time()))
                    if tamper != "capacity":
                        fixture.collection.update_card(card)
                    before = fixture.native_frame()
                    undo_before = fixture.collection.undo_status().SerializeToString(deterministic=True)
                    with count_active_answers(fixture) as calls:
                        result = fixture.service.apply_pending(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0, authorization=authorization)
                    self.assertEqual(result.status, "blocked", result.to_dict())
                    self.assert_empty_changes(result)
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(fixture.native_frame(), before)
                    self.assertEqual(
                        fixture.collection.undo_status().SerializeToString(deterministic=True),
                        undo_before,
                        tamper,
                    )
                    app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
                    self.assertEqual(app.status, "not_applied")
                    self.assertIsNone(app.intent_id)
                    self.assertFalse(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    EVIDENCE.append({"case": "preflight_" + tamper, "status": "PASS", "reason": result.reason, "answer_calls": calls[0], "native_no_write": True, "active_load_balancer_enabled": fixture.collection.load_balancer_enabled})
                finally:
                    fixture.close()

    def test_again_is_applied_and_real_native_changes_are_empty_on_idle(self) -> None:
        fixture = self.fixture
        snapshot_id = fixture.publish_production_snapshot()
        fixture.receive(snapshot_id, [[1, 1, fixture.times()[0], 120]])
        with count_active_answers(fixture) as calls:
            result = fixture.apply_one()
        self.assertEqual(result.status, "applied", result.to_dict())
        self.assertEqual(calls[0], 1)
        self.assertEqual(fixture.collection.get_card(fixture.meta['card_ids'][0]).type, 3)
        fixture.store.open_session(DEVICE, "empty-session")
        idle = fixture.service.recover_pending(fixture.collection, DEVICE, "empty-session", collection_identity=PROFILE, expected_generation=1)
        self.assertEqual(idle.status, "idle")
        self.assert_empty_changes(idle)
        EVIDENCE.append({"case": "again_applied_idle_opchanges", "status": "PASS", "answer_calls": 1})

    def test_actual_production_snapshot_record_is_unmodified_and_missing_evidence_fails_closed(self) -> None:
        fixture = self.fixture
        exporter = Anki260801Adapter(
            collection_provider=lambda: fixture.collection, profile_provider=lambda: PROFILE,
            profile_identity=PROFILE, anki_version=buildinfo.version, anki_build=buildinfo.buildhash,
            generation_provider=fixture.store.get_collection_generation, runtime_epoch_provider=lambda: 1,
        )
        typed = exporter.export_snapshot(card_ids=[str(cid) for cid in fixture.meta["card_ids"]])
        self.assertEqual(len(typed.cards), 2)
        published = SnapshotService(fixture.store).publish(typed, scope_id="all", generation=0, applied_watermark=0)
        stored = fixture.store.get_snapshot(published.snapshot_id)
        payload = json.loads(stored.payload_json)
        fixture.receive(published.snapshot_id, [[1, 3, fixture.times()[0], 120]])
        before = fixture.native_frame()
        with count_active_answers(fixture) as calls:
            with self.assertRaises(CompactApplicationError) as caught:
                fixture.service.authorize_next(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=0)
            # 构造器拒绝后没有合法授权，应用入口必须返回真实空 OpChanges。
            blocked = fixture.service.apply_pending(
                fixture.collection, DEVICE, SESSION, collection_identity=PROFILE,
                expected_generation=0, authorization=None,
            )
        self.assertEqual(calls[0], 0)
        self.assertEqual(fixture.native_frame(), before)
        self.assertIsNone(fixture.store.query_compact_applications(DEVICE, SESSION)[0].intent_id)
        self.assertEqual(caught.exception.code, "snapshot_application_context_missing")
        self.assertEqual((blocked.status, blocked.reason), ("blocked", "compact_authorization_required"))
        self.assert_empty_changes(blocked)
        self.assertTrue(all(preset['max_interval_days'] == 36500 and "effective_config" not in preset for preset in payload["presets"]))
        EVIDENCE.append({
            "case": "current_production_snapshot_record", "safety_gate": "PASS", "production_application_gate": "FAIL",
            "reason": caught.exception.code, "snapshot": payload, "snapshot_id": published.snapshot_id,
            "apply_result": blocked.to_dict(), "native_answer_calls": calls[0], "intent_created": False,
            "source": "Anki260801Adapter.export_snapshot -> SnapshotService.publish -> ProfileStore.get_snapshot",
            "missing": ["frozen application context", "timezone", "max_answer_seconds/maxTaken", "explicit fsrs_enabled/native-default parameter identity"],
        })

    def test_sigkill_legacy_source_preserves_original_attempt_and_never_duplicates_committed_score(self) -> None:
        self._run_sigkill_boundaries(legacy=True)

    def test_sigkill_atomic_commit_boundaries_recover_same_attempt_without_duplicate_scoring(self) -> None:
        self._run_sigkill_boundaries(legacy=False)

    def _run_sigkill_boundaries(self, *, legacy: bool) -> None:
        uncommitted = {"intent", "score_native", "due_native", "counter_partial", "counter_complete"}
        for phase in CRASH_PHASES:
            with self.subTest(phase=phase):
                root = self.root / ("crash-" + phase)
                fixture = SyntheticFixture(root)
                fixture.set_counters(old_day=True)
                snapshot_id = fixture.publish_production_snapshot()
                fixture.receive(snapshot_id, [[1, 3, fixture.times(historical=True)[0], 120]])
                if legacy:
                    snapshot = fixture.store.get_snapshot(snapshot_id)
                    event = fixture.store.query_compact_events(DEVICE, SESSION)[0]
                    keys = ("profile_id", "device_id", "session_id", "sequence", "event_id", "payload_hash", "snapshot_id", "card_id")
                    source = {"schema": "inkcards-legacy-source-v1", "source_id": "synthetic-historical-backup",
                  "policy_id": "inkcards-compact-legacy-restore-v1", "policy_approval": "synthetic-reviewed-ordinary-policy",
                              "application_context": snapshot.application_context.to_dict(),
                              "events": [{key: getattr(event, key) for key in keys}]}
                    with closing(fixture.store.connect()) as db:
                        db.execute("UPDATE snapshots SET application_context_json=NULL WHERE snapshot_id=?", (snapshot_id,))
                        db.commit()
                    fixture.service.import_legacy_source(source, expected_sha256=json_sha256(source))
                fixture.close()
                killed = run_sigkill(root, phase)
                fixture = SyntheticFixture(root, create=False)
                try:
                    prior_app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
                    intent_id, attempt_id = prior_app.intent_id, prior_app.attempt_id
                    prior_revlogs = len(fixture.collection.db.all("SELECT id FROM revlog WHERE cid=?", fixture.meta["card_ids"][0]))
                    prior_state = _capture_state(fixture.collection, fixture.meta["card_ids"][0], (fixture.meta["deck_ids"][0], fixture.meta["parent"]))
                    with count_active_answers(fixture, forbid=phase not in uncommitted) as calls:
                        result = fixture.service.recover_pending(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=fixture.store.get_collection_generation())
                    self.assertEqual(result.status, "idle" if phase == "receipt" else "applied", result.to_dict())
                    self.assertIsInstance(result.changes, OpChanges)
                    self.assertEqual(calls[0], 1 if phase in uncommitted else 0)
                    self.assertEqual(prior_revlogs, 0 if phase in uncommitted else 1)
                    app = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
                    self.assertEqual((app.intent_id, app.attempt_id), (intent_id, attempt_id))
                    self.assertEqual(app.status, "applied")
                    self.assertEqual(fixture.store.get_collection_generation(), 1)
                    self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    final = _capture_state(fixture.collection, fixture.meta["card_ids"][0], (fixture.meta["deck_ids"][0], fixture.meta["parent"]))
                    self.assertEqual(len(final["review_logs"]), 1)
                    self.assertEqual(fixture.adapter.state_for_store(final), json.loads(app.after_json))
                    self.assertEqual(json.loads(final["card"]["custom_data"])["other"], "ok")
                    if phase not in uncommitted and phase != "receipt":
                        proof = json.loads(next(row.evidence_json for row in fixture.store.query_compact_score_evidence(DEVICE, SESSION, 1) if row.stage == "recovery_classification"))
                        self.assertEqual(proof["classification"], "score_complete")
                        self.assertEqual(proof["verification"]["observed_recovery_state"], prior_state)
                        self.assertEqual(proof["verification"]["final_native_readback"], final)
                        self.assertEqual(proof["verification"]["plan_hash"], hashlib.sha256(app.plan_json.encode()).hexdigest())
                    EVIDENCE.append({"case": "sigkill_" + phase, "status": "PASS", "process": killed, "recovery_answer_calls": calls[0], "prior_revlog_count": prior_revlogs, "final_revlog_count": len(final["review_logs"]), "generation_after": 1, "pull_allowed": True})
                finally:
                    fixture.close()
                fixture = SyntheticFixture(root, create=False)
                try:
                    with count_active_answers(fixture, forbid=True) as calls:
                        idle = fixture.service.recover_pending(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=1)
                    self.assertEqual(idle.status, "idle")
                    self.assert_empty_changes(idle)
                    self.assertEqual(calls[0], 0)
                    self.assertTrue(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    fixture.store.open_session(DEVICE, "after-recovery")
                    if legacy:
                        # 显式历史来源只授权原事件；后续新事件必须使用新冻结快照。
                        snapshot_id = fixture.publish_production_snapshot()
                    fixture.receive(snapshot_id, [[2, 4, fixture.times(historical=True)[1], 130]], session="after-recovery")
                    with count_active_answers(fixture) as next_calls:
                        following = fixture.apply_one(session="after-recovery")
                    self.assertEqual(following.status, "applied", following.to_dict())
                    self.assertEqual(next_calls[0], 1)
                    self.assertEqual(fixture.store.get_collection_generation(), 2)
                    self.assertTrue(fixture.store.pull_gate(DEVICE, "after-recovery").allowed)
                    EVIDENCE.append({"case": "sigkill_" + phase + "_receipt_overlay_next_deck", "status": "PASS", "following_answer_calls": next_calls[0], "snapshot_generation": 0, "generation_after": 2, "pull_allowed": True})
                finally:
                    fixture.close()

    def test_sigkill_score_gap_foreign_missing_marker_memory_counters_preset_generation_are_no_write(self) -> None:
        for tamper in ("foreign_marker", "missing_marker", "memory_001", "counters", "preset", "generation", "parent_name"):
            with self.subTest(tamper=tamper):
                root = self.root / ("negative-" + tamper)
                fixture = SyntheticFixture(root)
                snapshot_id = fixture.publish_production_snapshot()
                fixture.receive(snapshot_id, [[1, 3, fixture.times(historical=True)[0], 120]])
                fixture.close()
                killed = run_sigkill(root, "score_committed")
                fixture = SyntheticFixture(root, create=False)
                try:
                    card = fixture.collection.get_card(fixture.meta["card_ids"][0])
                    if tamper == "foreign_marker":
                        card.custom_data = canonical_json({"other": "ok", "inkcards": "f" * 64})
                        fixture.collection.update_card(card)
                    elif tamper == "missing_marker":
                        card.custom_data = canonical_json({"other": "ok"})
                        fixture.collection.update_card(card)
                    elif tamper == "memory_001":
                        prior_difficulty = float(card.memory_state.difficulty)
                        card.memory_state = FSRSMemoryState(stability=float(card.memory_state.stability), difficulty=prior_difficulty + 0.001)
                        fixture.collection.update_card(card)
                        self.assertNotEqual(float(fixture.collection.get_card(card.id).memory_state.difficulty), prior_difficulty)
                    elif tamper == "counters":
                        deck = fixture.collection._backend.get_deck(fixture.meta["deck_ids"][0])
                        deck.common.review_studied += 17
                        fixture.collection._backend.update_deck(deck)
                    elif tamper == "preset":
                        config = fixture.collection.decks.config_dict_for_deck_id(card.did)
                        config["desiredRetention"] = 0.95
                        fixture.collection.decks.save(config)
                    elif tamper == "parent_name":
                        deck = fixture.collection._backend.get_deck(fixture.meta["parent"])
                        deck.name = "Changed Native"
                        fixture.collection._backend.update_deck(deck)
                    else:
                        fixture.store.set_collection_generation(1)
                    before = fixture.native_frame()
                    with count_active_answers(fixture, forbid=True) as calls:
                        result = fixture.service.recover_pending(fixture.collection, DEVICE, SESSION, collection_identity=PROFILE, expected_generation=fixture.store.get_collection_generation())
                    self.assertIn(result.status, {"unknown", "conflict"}, result.to_dict())
                    self.assert_empty_changes(result)
                    self.assertEqual(calls[0], 0)
                    self.assertEqual(fixture.native_frame(), before)
                    self.assertFalse(fixture.store.pull_gate(DEVICE, SESSION).allowed)
                    EVIDENCE.append({"case": "sigkill_negative_" + tamper, "status": "PASS", "process": killed, "result": result.to_dict(), "recovery_answer_calls": calls[0], "native_no_write": True, "pull_allowed": False})
                finally:
                    fixture.close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--crash-worker":
        crash_worker(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
