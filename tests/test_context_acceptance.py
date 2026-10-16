from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable
from unittest import mock

# Anki bundled packages require this import before any other Anki module.
import anki.collection  # type: ignore[import-not-found]  # noqa: F401
from anki.cards import FSRSMemoryState  # type: ignore[import-not-found]
from anki.collection import Collection, Config  # type: ignore[import-not-found]
from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from inkcards_anki_bridge.anki.compact_260903 import (
    AnkiCompactAdapter,
    _capture_native_card_hex,
    _capture_state,
)
from inkcards_anki_bridge.anki.operations import (
    AnkiQueryOpHost,
    OperationContext,
    QueryOpAdapter,
)
from inkcards_anki_bridge.anki.snapshot_jobs import SnapshotJobManager, SnapshotJobState
from inkcards_anki_bridge.services.compact_application import (
    CompactApplicationError,
    CompactApplicationService,
)
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import ProfileStore


class _QueuedSyntheticQueryOp:
    """在测试中保留官方 QueryOp 的 op(collection) 调用形状。"""

    def __init__(
        self,
        host: "_SyntheticQueryHost",
        *,
        parent: Any,
        op: Callable[[Any], Any],
        success: Callable[[Any], None],
    ) -> None:
        self.host = host
        self.parent = parent
        self.op = op
        self.success = success
        self.failure_callback: Callable[[BaseException], None] | None = None

    def failure(self, callback: Callable[[BaseException], None]) -> "_QueuedSyntheticQueryOp":
        self.failure_callback = callback
        return self

    def run_in_background(self) -> None:
        self.host.pending.append(self)


class _SyntheticQueryHost:
    """只向 queued QueryOp 提供本测试创建的临时 Collection。"""

    def __init__(self, collection: Collection) -> None:
        self.collection = collection
        self.pending: list[_QueuedSyntheticQueryOp] = []
        self.parent = object()

    def factory(self, *, parent: Any, op: Callable[[Any], Any], success: Callable[[Any], None]):
        return _QueuedSyntheticQueryOp(self, parent=parent, op=op, success=success)

    def run_next(self) -> None:
        query = self.pending.pop(0)
        try:
            query.success(query.op(self.collection))
        except BaseException as error:
            if query.failure_callback is None:
                raise
            query.failure_callback(error)


class ContextAcceptanceTests(unittest.TestCase):
    """真实 snapshot manager/exporter/Store；Anki 与 Store 均为临时合成实例。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="inkcards-context-acceptance-", dir="/private/tmp"
        )
        self.root = Path(self.tempdir.name)
        self.collection = Collection(str(self.root / "synthetic.anki2"))
        self.collection.set_config("fsrs", True)
        self.deck_ids = [1]
        second_deck = self.collection.decks.add_normal_deck_with_name(
            "Acceptance second deck"
        )
        self.second_deck_id = int(second_deck.id)
        second_preset = self.collection.decks.add_config("Acceptance second preset")
        second_deck_record = self.collection.decks.get(self.second_deck_id)
        assert second_deck_record is not None
        self.collection.decks.set_config_id_for_deck_dict(
            second_deck_record, int(second_preset["id"])
        )
        self.collection.decks.save(second_deck_record)
        self.deck_ids.append(self.second_deck_id)

        self.profile_id = "qa-context-profile"
        self.generation = 9
        self.runtime_epoch = "qa-context-epoch"
        self.operation_context = OperationContext(
            self.profile_id, self.generation, self.runtime_epoch
        )
        self.context_provider = lambda: self.operation_context

        def forbidden_collection_provider() -> None:
            raise AssertionError(
                "SnapshotJobManager must pass its QueryOp Collection to the exporter"
            )

        self.exporter = Anki260801Adapter(
            collection_provider=forbidden_collection_provider,
            profile_provider=self.context_provider,
            profile_identity=self.profile_id,
            anki_version="26.09.3",
            anki_build="29bb700b",
            generation_provider=lambda: self.generation,
            runtime_epoch_provider=lambda: self.runtime_epoch,
        )
        self.with_context = ProfileStore(
            self.root / "with-context", profile_id=self.profile_id
        )
        self.with_context.set_collection_generation(self.generation)
        self.snapshot_service = SnapshotService(self.with_context)
        self.wire_only = ProfileStore(
            self.root / "wire-only", profile_id=self.profile_id
        )
        self.wire_only.set_collection_generation(self.generation)
        self.wire_only_service = SnapshotService(self.wire_only)
        self.exported: list[Any] = []

        def publish(snapshot: Any, *, expected_context: Any, **kwargs: Any) -> Any:
            self.assertEqual(OperationContext.from_value(expected_context), self.context_provider())
            self.exported.append(snapshot)
            return self.snapshot_service.publish(snapshot, **kwargs)

        self.query_host = _SyntheticQueryHost(self.collection)
        anki_query_host = AnkiQueryOpHost(
            self.query_host.factory,
            self.query_host.parent,
        )
        self.manager = SnapshotJobManager(
            operation_adapter=QueryOpAdapter(
                host=anki_query_host,
                context_provider=self.context_provider,
            ),
            exporter=self.exporter,
            context_provider=self.context_provider,
            publish=publish,
            max_card_ids=200,
            export_batch_size=32,
        )

    def tearDown(self) -> None:
        self.manager.stop()
        self.with_context.close()
        self.wire_only.close()
        self.collection.close()
        self.tempdir.cleanup()

    def _add_cards(self, count: int) -> list[str]:
        note_type = self.collection.models.by_name("Basic")
        card_ids: list[str] = []
        for index in range(count):
            note = self.collection.new_note(note_type)
            note.fields[0] = f"Acceptance question {index:03d}"
            if len(note.fields) > 1:
                note.fields[1] = f"Acceptance answer {index:03d}"
            deck_id = self.deck_ids[index % len(self.deck_ids)]
            self.collection.add_note(note, deck_id)
            card_ids.extend(
                str(card_id) for card_id in self.collection.card_ids_of_note(note.id)
            )
        return card_ids

    @staticmethod
    def _page_rows(store: ProfileStore, snapshot_id: str) -> list[tuple[Any, ...]]:
        with closing(store.connect()) as db:
            rows = db.execute(
                "SELECT page_index,page_json,page_hash,page_bytes,cursor,is_last "
                "FROM snapshot_pages WHERE profile_id=? AND snapshot_id=? "
                "ORDER BY page_index",
                (store.profile_id, snapshot_id),
            ).fetchall()
        return [tuple(row) for row in rows]

    def test_queryop_producer_publishes_bound_context_without_changing_device_wire(self) -> None:
        card_ids = self._add_cards(150)
        job = self.manager.request_snapshot(card_ids=card_ids)
        self.assertEqual(job.state, SnapshotJobState.QUEUED)
        self.assertEqual(len(self.query_host.pending), 1)
        self.assertEqual(self.exported, [])

        self.query_host.run_next()

        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        self.assertEqual(len(self.exported), 1)
        self.assertEqual(len(self.exported[0].cards), 150)
        self.assertEqual(len(self.exported[0].presets), 2)
        self.assertIsNotNone(self.exported[0].application_context)
        source_context = self.exported[0].application_context
        self.assertEqual(source_context.profile_identity, self.profile_id)
        self.assertEqual(source_context.collection_generation, self.generation)
        self.assertEqual(source_context.runtime_epoch, self.runtime_epoch)
        self.assertEqual(
            source_context.to_dict()["schema"], "inkcards.application-context-source.v2"
        )
        captured_deck_ids = {str(deck_id) for deck_id in self.deck_ids}
        self.assertEqual(set(source_context.deck_ancestor_ids), captured_deck_ids)
        self.assertEqual(set(source_context.deck_contexts), captured_deck_ids)
        self.assertTrue(all(not ancestors for ancestors in source_context.deck_ancestor_ids.values()))

        publication = job.result
        self.assertIsNotNone(publication)
        stored = self.with_context.get_snapshot(publication.snapshot_id)
        bound = stored.application_context
        self.assertIsNotNone(bound)
        assert bound is not None
        self.assertEqual(bound.profile_id, self.profile_id)
        self.assertEqual(bound.snapshot_id, publication.snapshot_id)
        self.assertEqual(bound.snapshot_checksum, publication.checksum)
        self.assertEqual(bound.snapshot_generation, self.generation)
        self.assertEqual(bound.to_dict()["schema"], "inkcards.application-context.v2")
        self.assertEqual(bound.source.source_sha256, source_context.source_sha256)
        self.assertEqual(bound.source.anki_version, "26.9.3")
        self.assertEqual(bound.source.anki_build, "29bb700b")

        source_scope = {"kind": "card", "ids": sorted(set(card_ids))}
        baseline = self.wire_only_service.publish(
            self.exported[0].to_dict(),
            scope_id=job.scope_id,
            generation=self.generation,
            applied_watermark=0,
            source_scope=source_scope,
        )
        self.assertEqual(publication.snapshot_id, baseline.snapshot_id)
        self.assertEqual(publication.checksum, baseline.checksum)
        self.assertEqual(publication.payload_bytes, baseline.payload_bytes)
        self.assertEqual(
            self._page_rows(self.with_context, publication.snapshot_id),
            self._page_rows(self.wire_only, baseline.snapshot_id),
        )
        self.assertEqual(stored.included_card_count, 150)

    @contextmanager
    def _answer_call_counter(self):
        calls = [0]
        scheduler_type = type(self.collection.sched)
        original = scheduler_type.answer_card

        def observed(scheduler: Any, answer: Any) -> Any:
            # Anki exposes Scheduler.col as a weakref proxy; identity comparison is false.
            if scheduler.col == self.collection:
                calls[0] += 1
            return original(scheduler, answer)

        with mock.patch.object(scheduler_type, "answer_card", observed):
            yield calls

    def _native_score_frame(
        self, card_id: int, deck_ids: tuple[int, ...] = (1,)
    ) -> dict[str, Any]:
        return {
            "card": _capture_state(self.collection, card_id, deck_ids),
            "native_card": _capture_native_card_hex(self.collection, card_id),
            "root_deck": self.collection._backend.get_deck(1).SerializeToString(
                deterministic=True
            ).hex(),
            "native_decks": {
                str(deck_id): self.collection._backend.get_deck(deck_id).SerializeToString(
                    deterministic=True
                ).hex()
                for deck_id in deck_ids
            },
            "undo": self.collection.undo_status().SerializeToString(
                deterministic=True
            ).hex(),
            "revlog": tuple(
                self.collection.db.all(
                    "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id",
                    card_id,
                )
            ),
        }

    def _create_nested_chain(self, prefix: str) -> tuple[int, int, int]:
        parent = self.collection.decks.add_normal_deck_with_name(prefix)
        child = self.collection.decks.add_normal_deck_with_name(prefix + "::Child")
        leaf = self.collection.decks.add_normal_deck_with_name(prefix + "::Child::Leaf")
        return int(parent.id), int(child.id), int(leaf.id)

    def _prepare_review_card(self, card_id: int) -> None:
        card = self.collection.get_card(card_id)
        card.type = 2
        card.queue = 2
        card.due = int(self.collection.sched.today)
        card.ivl = 7
        card.factor = 2500
        card.reps = 1
        card.lapses = 0
        card.left = 0
        card.last_review_time = int(time.time()) - 30 * 86400
        card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
        self.collection.update_card(card)

    def _publish_queryop_snapshot(self, card_ids: list[str]) -> tuple[Any, Any]:
        job = self.manager.request_snapshot(card_ids=card_ids)
        self.assertEqual(job.state, SnapshotJobState.QUEUED)
        self.query_host.run_next()
        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        publication = job.result
        self.assertIsNotNone(publication)
        assert publication is not None
        return publication, self.with_context.get_snapshot(publication.snapshot_id)

    def _queue_single_event(
        self, device_id: str, session_id: str, snapshot_id: str, event_ms: int
    ) -> None:
        self.with_context.open_session(device_id, session_id)
        receipts, conflict = self.with_context.receive_compact_batch(
            device_id, session_id, snapshot_id, 1, [[1, 3, event_ms, 120]]
        )
        self.assertFalse(conflict)
        self.assertEqual(len(receipts), 1)

    def _legacy_source_fixture(self, *, old_wire=False):
        self.deck_ids = [1]
        self.collection.db.execute("UPDATE col SET crt=?", int(time.time()) - 90 * 86400)
        for key, value in (("rollover", 4), ("localOffset", 480), ("creationOffset", 480)):
            self.collection.set_config(key, value)
        card_id = int(self._add_cards(1)[0])
        card = self.collection.get_card(card_id)
        card.type = card.queue = 2
        card.due = int(self.collection.sched.today) - 1
        card.ivl, card.factor, card.reps = 7, 2500, 1
        event_ms = (int(self.collection.sched.day_cutoff) - 86400 - 3600) * 1000
        card.last_review_time = event_ms // 1000 - 30 * 86400
        card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
        self.collection.update_card(card)
        job = self.manager.request_snapshot(card_ids=[str(card_id)])
        self.query_host.run_next()
        snapshot = self.with_context.get_snapshot(job.result.snapshot_id)
        historical_context = snapshot.application_context
        if old_wire:
            from inkcards_anki_bridge.anki.application_context import (
                SnapshotApplicationContext,
            )
            raw = self.exported[-1].to_dict()
            for key in ("profile_identity", "profile_generation", "runtime_epoch", "anki_version", "anki_build", "timezone"):
                raw["manifest"].pop(key, None)
            publication = self.snapshot_service.publish(raw, scope_id="synthetic-old-wire", generation=9, applied_watermark=0)
            snapshot = self.with_context.get_snapshot(publication.snapshot_id)
            historical_context = SnapshotApplicationContext.bind(historical_context.source,
                profile_id=self.profile_id, snapshot_id=snapshot.snapshot_id,
                snapshot_checksum=snapshot.checksum, snapshot_generation=9)
        self.with_context.register_device("qa-context-device", "qa-context-token")
        self.with_context.open_session("qa-context-device", "legacy-session")
        self.with_context.receive_compact_batch("qa-context-device", "legacy-session", snapshot.snapshot_id,
                                                1, [[1, 3, event_ms, 120]])
        event = self.with_context.query_compact_events("qa-context-device", "legacy-session")[0]
        keys = ("profile_id", "device_id", "session_id", "sequence", "event_id", "payload_hash", "snapshot_id", "card_id")
        source = {"schema": "inkcards-legacy-source-v1", "source_id": "synthetic-historical-backup",
                  "policy_id": "inkcards-compact-legacy-restore-v1", "policy_approval": "synthetic-reviewed-ordinary-policy",
                  "application_context": historical_context.to_dict(),
                  "events": [{key: getattr(event, key) for key in keys}]}
        with closing(self.with_context.connect()) as db:
            db.execute("UPDATE snapshots SET application_context_json=NULL WHERE snapshot_id=?", (snapshot.snapshot_id,))
            db.commit()
        return CompactApplicationService(self.with_context), snapshot, source, card_id

    def test_legacy_source_missing_and_untrusted_claim_leave_native_and_intents_unchanged(self):
        service, snapshot, _source, card_id = self._legacy_source_fixture()
        before = _capture_native_card_hex(self.collection, card_id)
        undo = self.collection.undo_status()
        with self._answer_call_counter() as calls:
            for claim in (None, {"source_id": "arbitrary", "source_sha256": "a" * 64}):
                with self.assertRaises(CompactApplicationError) as caught:
                    service.authorize_next(self.collection, "qa-context-device", "legacy-session",
                                           collection_identity=self.profile_id, expected_generation=9,
                                           legacy_source=claim)
                self.assertIn(caught.exception.code, ("snapshot_application_context_missing", "legacy_source_required"))
        self.assertEqual(calls[0], 0)
        self.assertEqual(before, _capture_native_card_hex(self.collection, card_id))
        self.assertEqual(undo, self.collection.undo_status())
        application = self.with_context.query_compact_applications("qa-context-device", "legacy-session")[0]
        self.assertEqual(application.status, "not_applied")
        self.assertIsNone(application.intent_id)
        self.assertIsNone(self.with_context.get_snapshot(snapshot.snapshot_id).application_context)

    def test_legacy_reviewed_source_native_apply_receipt_duplicate_and_recovery(self):
        from inkcards_anki_bridge.anki.compact_projection import json_sha256
        service, snapshot, source, card_id = self._legacy_source_fixture()
        digest = json_sha256(source)
        import subprocess
        import sys
        source_file = self.root / "reviewed-source.json"
        source_file.write_text(json.dumps(source))
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/import_legacy_source.py"),
                   "--store-root", str(self.with_context.root), "--source", str(source_file),
                   "--expected-sha256", digest, "--historical-source-reviewed", "--ordinary-policy-approved"]
        imported = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn("legacy_source_imported", imported.stdout)
        self.assertEqual(service.import_legacy_source(source, expected_sha256=digest), digest)
        with self._answer_call_counter() as calls:
            authorization = service.authorize_next(self.collection, "qa-context-device", "legacy-session",
                                                   collection_identity=self.profile_id, expected_generation=9)
            self.assertEqual(authorization.mode, "legacy_restore")
            self.assertEqual(calls[0], 0)
            result = service.apply_pending(self.collection, "qa-context-device", "legacy-session",
                                           collection_identity=self.profile_id, expected_generation=9,
                                           authorization=authorization)
        self.assertEqual(result.status, "applied", result.to_dict())
        self.assertEqual(calls[0], 1)
        self.assertEqual(self.collection.db.scalar("SELECT count(*) FROM revlog WHERE cid=?", card_id), 1)
        app = self.with_context.query_compact_applications("qa-context-device", "legacy-session")[0]
        self.assertIsNotNone(app.receipt_hash)
        self.assertEqual(json.loads(app.policy_json)["mode"], "legacy_restore")
        self.assertTrue(self.with_context.pull_gate("qa-context-device", "legacy-session").allowed)
        self.assertIsNone(self.with_context.get_snapshot(snapshot.snapshot_id).application_context)
        self.assertEqual(self.with_context.get_snapshot(snapshot.snapshot_id).checksum, snapshot.checksum)
        with self._answer_call_counter() as calls:
            duplicate = service.recover_pending(self.collection, "qa-context-device", "legacy-session",
                                               collection_identity=self.profile_id, expected_generation=10)
        self.assertEqual(duplicate.status, "idle")
        self.assertEqual(calls[0], 0)

    def test_legacy_source_rejects_context_and_event_tampering(self):
        from inkcards_anki_bridge.anki.compact_projection import json_sha256
        service, snapshot, source, _card_id = self._legacy_source_fixture()
        for approval_field in ("policy_id", "policy_approval"):
            unapproved = json.loads(json.dumps(source))
            unapproved.pop(approval_field)
            with self.assertRaises(CompactApplicationError) as caught:
                service.import_legacy_source(unapproved, expected_sha256=json_sha256(unapproved))
            self.assertEqual(caught.exception.code, "legacy_source_invalid")
        for field in ("snapshot_checksum", "source_sha256"):
            damaged = json.loads(json.dumps(source))
            damaged["application_context"][field] = "0" * 64
            with self.assertRaises(CompactApplicationError):
                service.import_legacy_source(damaged, expected_sha256=json_sha256(damaged))
        damaged = json.loads(json.dumps(source))
        damaged["events"][0]["payload_hash"] = "0" * 64
        with self.assertRaises(CompactApplicationError) as caught:
            service.import_legacy_source(damaged, expected_sha256=json_sha256(damaged))
        self.assertEqual(caught.exception.code, "legacy_source_binding_mismatch")
        self.assertFalse(service._legacy_path(snapshot.snapshot_id).exists())

    def test_legacy_local_file_invalid_duplicate_and_authorization_change_are_no_write(self):
        from inkcards_anki_bridge.anki.compact_projection import json_sha256
        service, snapshot, source, card_id = self._legacy_source_fixture()
        before = _capture_native_card_hex(self.collection, card_id)
        duplicate = json.loads(json.dumps(source))
        duplicate["events"].append(duplicate["events"][0])
        with self.assertRaises(CompactApplicationError) as caught:
            service.import_legacy_source(duplicate, expected_sha256=json_sha256(duplicate))
        self.assertEqual(caught.exception.code, "legacy_source_binding_mismatch")
        for field in ("profile_id", "snapshot_id"):
            bad = json.loads(json.dumps(source))
            bad["application_context"][field] = "other"
            with self.assertRaises(CompactApplicationError):
                service.import_legacy_source(bad, expected_sha256=json_sha256(bad))
        service.import_legacy_source(source, expected_sha256=json_sha256(source))
        path = service._legacy_path(snapshot.snapshot_id)
        for invalid in ("null", "[]", "42", '"text"', "{"):
            path.write_text(invalid)
            with self.assertRaises(CompactApplicationError) as caught:
                service.authorize_next(self.collection, "qa-context-device", "legacy-session",
                                       collection_identity=self.profile_id, expected_generation=9)
            self.assertEqual(caught.exception.code, "legacy_source_invalid")
        path.write_text(json.dumps(source))
        authorization = service.authorize_next(self.collection, "qa-context-device", "legacy-session",
                                               collection_identity=self.profile_id, expected_generation=9)
        changed = json.loads(json.dumps(source))
        changed["source_id"] = "different-backup"
        path.write_text(json.dumps(changed))
        with self._answer_call_counter() as calls:
            result = service.apply_pending(self.collection, "qa-context-device", "legacy-session",
                                           collection_identity=self.profile_id, expected_generation=9,
                                           authorization=authorization)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "legacy_source_changed")
        self.assertEqual(calls[0], 0)
        self.assertEqual(before, _capture_native_card_hex(self.collection, card_id))
        app = self.with_context.query_compact_applications("qa-context-device", "legacy-session")[0]
        self.assertEqual(app.status, "not_applied")
        self.assertIsNone(app.intent_id)

    def _production_job(self):
        from types import SimpleNamespace

        from inkcards_anki_bridge.plugin import _attach_compact_jobs
        tasks = []
        collection = self.collection

        class CollectionOp:
            def __init__(op_self, *, parent, op):
                op_self.op = op
            def success(op_self, callback):
                op_self.on_success = callback
                return op_self
            def failure(op_self, callback):
                op_self.on_failure = callback
                return op_self
            def run_in_background(op_self, **_kwargs):
                def run():
                    try:
                        result = op_self.op(collection)
                    except Exception as error:
                        op_self.on_failure(error)
                    else:
                        op_self.on_success(result)
                tasks.append(run)

        def refresh(generation):
            self.operation_context = OperationContext(self.profile_id, generation, self.runtime_epoch)
        runtime = SimpleNamespace(profile_id=self.profile_id, store=self.with_context,
                                  snapshot_context=lambda: self.operation_context,
                                  refresh_committed_collection_generation=refresh,
                                  attach_compact_jobs=lambda manager: manager)
        window = SimpleNamespace(state="deckBrowser", col=collection,
                                 taskman=SimpleNamespace(run_on_main=tasks.append))
        manager = _attach_compact_jobs(aqt_module=SimpleNamespace(operations=SimpleNamespace(CollectionOp=CollectionOp)),
                                       main_window=window, runtime=runtime)
        job = manager.request("qa-context-device", "legacy-session")
        while tasks:
            tasks.pop(0)()
        manager.stop()
        return job

    def test_production_legacy_job_missing_context_blocked_then_reviewed_source_applied(self):
        from inkcards_anki_bridge.anki.compact_projection import json_sha256
        service, _snapshot, source, _card_id = self._legacy_source_fixture()
        undo = self.collection.undo_status()
        with self._answer_call_counter() as calls:
            rejected = self._production_job()
        self.assertEqual(rejected.state.value, "blocked")
        self.assertEqual(rejected.to_dict()["reason"], "snapshot_application_context_missing")
        self.assertEqual(calls[0], 0)
        self.assertEqual(undo, self.collection.undo_status())
        service.import_legacy_source(source, expected_sha256=json_sha256(source))
        with self._answer_call_counter() as calls:
            applied = self._production_job()
        self.assertEqual(applied.state.value, "applied", applied.to_dict())
        self.assertEqual(calls[0], 1)
        self.assertTrue(self.with_context.pull_gate("qa-context-device", "legacy-session").allowed)
        with self._answer_call_counter() as calls:
            duplicate = self._production_job()
        self.assertEqual(duplicate.state.value, "idle")
        self.assertEqual(calls[0], 0)

    def test_production_job_unexpected_authorization_exception_remains_unknown(self):
        self._legacy_source_fixture()
        with mock.patch.object(CompactApplicationService, "authorize_next", side_effect=RuntimeError("unexpected")):
            job = self._production_job()
        self.assertEqual(job.state.value, "unknown")
        self.assertEqual(job.to_dict()["reason"], "collection_operation_failed_after_start")

    def test_legacy_original_wire_without_producer_metadata_uses_only_reviewed_historical_source(self):
        from inkcards_anki_bridge.anki.compact_projection import json_sha256
        service, snapshot, source, _card_id = self._legacy_source_fixture(old_wire=True)
        self.assertNotIn("profile_identity", json.loads(snapshot.payload_json)["manifest"])
        service.import_legacy_source(source, expected_sha256=json_sha256(source))
        with self._answer_call_counter() as calls:
            job = self._production_job()
        self.assertEqual(job.state.value, "applied", job.to_dict())
        self.assertEqual(calls[0], 1)
        original = self.with_context.get_snapshot(snapshot.snapshot_id)
        self.assertEqual(original.payload_json, snapshot.payload_json)
        self.assertEqual(original.checksum, snapshot.checksum)
        self.assertIsNone(original.application_context)

    def _live_nested_scope(self, leaf_id: int) -> tuple[int, ...]:
        parents = self.collection.decks.parents(leaf_id)
        return (leaf_id, *(int(row["id"]) for row in reversed(parents)))

    def test_root_device_policy_rollover_receipt_and_duplicate_have_exact_native_write_counts(self) -> None:
        # v2 来源字段稳定；本门只验证 Default root deck，不覆盖嵌套祖先链。
        self.deck_ids = [1]
        self.collection.db.execute("UPDATE col SET crt=?", int(time.time()) - 90 * 86400)
        self.collection.set_config("rollover", 4)
        self.collection.set_config("localOffset", 480)
        self.collection.set_config("creationOffset", 480)
        self.collection.set_config("fsrs", True)
        live_today = int(self.collection.sched.today)
        live_cutoff = int(self.collection.sched.day_cutoff)

        card_ids = self._add_cards(5)
        event_ms = (live_cutoff - 2 * 86400 - 3600) * 1000
        for raw_card_id in card_ids:
            card = self.collection.get_card(int(raw_card_id))
            card.type = 2
            card.queue = 2
            card.due = live_today - 1
            card.ivl = 7
            card.factor = 2500
            card.reps = 1
            card.lapses = 0
            card.left = 0
            card.last_review_time = event_ms // 1000 - 30 * 86400
            card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
            self.collection.update_card(card)
        card_id = int(card_ids[0])

        root_deck = self.collection._backend.get_deck(1)
        root_deck.common.last_day_studied = live_today - 2
        root_deck.common.new_studied = 3
        root_deck.common.review_studied = 7
        root_deck.common.learning_studied = 2
        root_deck.common.milliseconds_studied = 21000
        self.collection._backend.update_deck(root_deck)

        self.with_context.register_device("qa-context-device", "qa-context-token")
        session_id = "qa-context-root-session"
        self.with_context.open_session("qa-context-device", session_id)
        service = CompactApplicationService(self.with_context, AnkiCompactAdapter())

        job = self.manager.request_snapshot(card_ids=card_ids)
        scheduler_type = type(self.collection.sched)
        with (
            mock.patch.object(
                scheduler_type,
                "today",
                new_callable=mock.PropertyMock,
                return_value=live_today - 1,
            ),
            mock.patch.object(
                scheduler_type,
                "day_cutoff",
                new_callable=mock.PropertyMock,
                return_value=live_cutoff - 86400,
            ),
        ):
            self.query_host.run_next()
        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        publication = job.result
        self.assertIsNotNone(publication)
        stored = self.with_context.get_snapshot(publication.snapshot_id)
        context = stored.application_context
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.snapshot_checksum, publication.checksum)
        self.assertEqual(context.source.scheduler_today, live_today - 1)
        self.assertEqual(context.source.day_cutoff_seconds, live_cutoff - 86400)
        self.assertEqual(dict(context.source.deck_ancestor_ids), {"1": ()})
        self.assertEqual(set(context.source.deck_contexts), {"1"})
        root_context = context.source.deck_contexts["1"]
        self.assertEqual(root_context["deck_id"], "1")
        self.assertIsNone(root_context["parent_id"])
        self.assertEqual(
            set(root_context),
            {
                "deck_id",
                "name",
                "parent_id",
                "last_day_studied",
                "new_studied",
                "review_studied",
                "learning_studied",
                "milliseconds_studied",
            },
        )
        self.assertEqual(len(json.loads(stored.payload_json)["cards"]), 5)
        self.assertEqual(json.loads(stored.payload_json)["cards"][0]["deck_id"], "1")

        initial_gate = self.with_context.pull_gate("qa-context-device", session_id)
        self.assertTrue(initial_gate.allowed)
        rows = [[1, 3, event_ms, 120]]
        receipts, conflict = self.with_context.receive_compact_batch(
            "qa-context-device", session_id, publication.snapshot_id, 1, rows
        )
        self.assertFalse(conflict)
        self.assertEqual(len(receipts), 1)
        before_revlog = self.collection.db.all(
            "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id", card_id
        )
        pending_gate = self.with_context.pull_gate("qa-context-device", session_id)
        self.assertFalse(pending_gate.allowed)
        self.assertEqual(pending_gate.reason, "pull_gate_compact_not_applied")

        expected_generation = self.with_context.get_collection_generation()
        with self._answer_call_counter() as calls:
            authorization = service.authorize_next(
                self.collection,
                "qa-context-device",
                session_id,
                collection_identity=self.profile_id,
                expected_generation=expected_generation,
            )
            self.assertEqual(calls[0], 0, "read-only authorization must not score")
            self.assertEqual(authorization.mode, "device_policy")
            applied = service.apply_pending(
                self.collection,
                "qa-context-device",
                session_id,
                collection_identity=self.profile_id,
                expected_generation=expected_generation,
                authorization=authorization,
            )
        self.assertEqual(applied.status, "applied", applied.to_dict())
        self.assertEqual((applied.generation_before, applied.generation_after), (9, 10))
        self.assertEqual(calls[0], 1, "positive control must hit Scheduler.answer_card once")
        self.assertTrue(list(applied.changes.ListFields()))
        after_revlog = self.collection.db.all(
            "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id", card_id
        )
        self.assertEqual(len(after_revlog), len(before_revlog) + 1)
        self.assertEqual(after_revlog[-1][1], card_id)
        application = self.with_context.query_compact_applications(
            "qa-context-device", session_id
        )[0]
        self.assertEqual(application.status, "applied")
        self.assertEqual(json.loads(application.plan_json)["policy"]["mode"], "device_policy")
        self.assertIsNotNone(application.receipt_json)
        self.assertIsNotNone(application.receipt_hash)
        self.assertEqual(application.collection_generation_after, 10)
        self.assertTrue(self.with_context.pull_gate("qa-context-device", session_id).allowed)
        applied_event_day = json.loads(application.plan_json)["projected"][
            "event_collection_day"
        ]
        self.assertEqual(applied_event_day, live_today - 2)

        # 重复投递同一序号必须保持终态收据与 revlog，并且不再调用 native answer。
        duplicate_receipts, duplicate_conflict = self.with_context.receive_compact_batch(
            "qa-context-device", session_id, publication.snapshot_id, 1, rows
        )
        self.assertFalse(duplicate_conflict)
        self.assertEqual(len(duplicate_receipts), 1)
        with self._answer_call_counter() as duplicate_calls:
            duplicate = service.apply_pending(
                self.collection,
                "qa-context-device",
                session_id,
                collection_identity=self.profile_id,
                expected_generation=self.with_context.get_collection_generation(),
                authorization=None,
            )
        self.assertEqual(duplicate.status, "idle")
        self.assertEqual(duplicate_calls[0], 0)
        self.assertEqual(
            self.collection.db.all(
                "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id", card_id
            ),
            after_revlog,
        )
        self.assertTrue(self.with_context.pull_gate("qa-context-device", session_id).allowed)

        # 配置漂移的普通 device_policy 预检必须 fail closed：无 score、无 intent、gate 关闭。
        captured_short_term = context.source.fsrs_short_term_with_steps_enabled
        self.assertIsInstance(captured_short_term, bool)
        for card_index, drift_key, expected_code in (
            (2, "short-term", "snapshot_short_term_fsrs_conflict"),
            (3, "rollover", "snapshot_clock_config_conflict"),
            (4, "localOffset", "snapshot_clock_config_conflict"),
            (5, "creationOffset", "snapshot_clock_config_conflict"),
        ):
            rejected_session = "qa-context-reject-" + drift_key
            self.with_context.open_session("qa-context-device", rejected_session)
            rejected_receipts, rejected_conflict = self.with_context.receive_compact_batch(
                "qa-context-device",
                rejected_session,
                publication.snapshot_id,
                1,
                [[card_index, 3, int(time.time() * 1000) - 5000, 120]],
            )
            self.assertFalse(rejected_conflict)
            self.assertEqual(len(rejected_receipts), 1)
            if drift_key == "short-term":
                self.collection.set_config_bool(
                    Config.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED,
                    not captured_short_term,
                )
            else:
                captured_value = context.source.clock_config[drift_key]
                self.collection.set_config(drift_key, int(captured_value) + 1)
            rejected_card_id = int(card_ids[card_index - 1])
            before_reject = self._native_score_frame(rejected_card_id)
            with self._answer_call_counter() as rejected_calls:
                with self.assertRaises(CompactApplicationError) as caught:
                    service.authorize_next(
                        self.collection,
                        "qa-context-device",
                        rejected_session,
                        collection_identity=self.profile_id,
                        expected_generation=self.with_context.get_collection_generation(),
                    )
            self.assertEqual(caught.exception.code, expected_code)
            self.assertEqual(rejected_calls[0], 0)
            self.assertEqual(self._native_score_frame(rejected_card_id), before_reject)
            rejected_application = self.with_context.query_compact_applications(
                "qa-context-device", rejected_session
            )[0]
            self.assertEqual(rejected_application.status, "not_applied")
            self.assertIsNone(rejected_application.intent_id)
            rejected_gate = self.with_context.pull_gate(
                "qa-context-device", rejected_session
            )
            self.assertFalse(rejected_gate.allowed)
            if drift_key == "short-term":
                self.collection.set_config_bool(
                    Config.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED,
                    captured_short_term,
                )
            else:
                self.collection.set_config(drift_key, captured_value)

        print(
            "QA_ROOT_ACCEPTANCE "
            + json.dumps(
                {
                    "status": "PASS",
                    "sidecar_schema": "inkcards.application-context.v2",
                    "root_ancestor_ids": list(context.source.deck_ancestor_ids["1"]),
                    "snapshot_day": context.source.scheduler_today,
                    "upload_day": live_today,
                    "prior_event_day": applied_event_day,
                    "score_mode": "device_policy",
                    "initial_answer_calls": calls[0],
                    "revlog_rows_before": len(before_revlog),
                    "revlog_rows_after": len(after_revlog),
                    "duplicate_answer_calls": duplicate_calls[0],
                    "pull_gate_after_receipt": True,
                    "rejections": [
                        "snapshot_short_term_fsrs_conflict",
                        "snapshot_clock_config_conflict:rollover",
                        "snapshot_clock_config_conflict:localOffset",
                        "snapshot_clock_config_conflict:creationOffset",
                    ],
                    "rejection_answer_calls": 0,
                    "rejection_intents": 0,
                },
                sort_keys=True,
            )
        )

    def test_nested_queryop_parent_chain_scores_once_and_advances_receipt_counters(self) -> None:
        parent_id, child_id, leaf_id = self._create_nested_chain("Acceptance nested")
        self.deck_ids = [leaf_id]
        card_ids = self._add_cards(1)
        card_id = int(card_ids[0])
        self._prepare_review_card(card_id)

        publication, stored = self._publish_queryop_snapshot(card_ids)
        context = stored.application_context
        self.assertIsNotNone(context)
        assert context is not None
        leaf_key, child_key, parent_key = map(str, (leaf_id, child_id, parent_id))
        self.assertEqual(context.source.deck_ancestor_ids[leaf_key], (child_key, parent_key))
        self.assertEqual(set(context.source.deck_contexts), {leaf_key, child_key, parent_key})
        payload = json.loads(stored.payload_json)
        self.assertEqual(payload["cards"][0]["deck_id"], leaf_key)

        device_id = "qa-context-nested-device"
        session_id = "qa-context-nested-session"
        self.with_context.register_device(device_id, "qa-context-nested-token")
        event_ms = int(time.time() * 1000)
        self._queue_single_event(device_id, session_id, publication.snapshot_id, event_ms)
        before_revlog = self.collection.db.all(
            "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id", card_id
        )
        service = CompactApplicationService(self.with_context, AnkiCompactAdapter())
        expected_generation = self.with_context.get_collection_generation()
        with self._answer_call_counter() as calls:
            authorization = service.authorize_next(
                self.collection,
                device_id,
                session_id,
                collection_identity=self.profile_id,
                expected_generation=expected_generation,
            )
            self.assertEqual(calls[0], 0)
            self.assertEqual(authorization.mode, "device_policy")
            applied = service.apply_pending(
                self.collection,
                device_id,
                session_id,
                collection_identity=self.profile_id,
                expected_generation=expected_generation,
                authorization=authorization,
            )
        self.assertEqual(applied.status, "applied", applied.to_dict())
        self.assertEqual((applied.generation_before, applied.generation_after), (9, 10))
        self.assertEqual(calls[0], 1)
        after_revlog = self.collection.db.all(
            "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id", card_id
        )
        self.assertEqual(len(after_revlog), len(before_revlog) + 1)
        self.assertEqual(after_revlog[-1][1], card_id)
        application = self.with_context.query_compact_applications(device_id, session_id)[0]
        self.assertEqual(application.status, "applied")
        self.assertEqual(json.loads(application.plan_json)["policy"]["mode"], "device_policy")
        self.assertIsNotNone(application.receipt_json)
        self.assertIsNotNone(application.receipt_hash)
        self.assertEqual(application.collection_generation_after, 10)
        self.assertTrue(self.with_context.pull_gate(device_id, session_id).allowed)
        after_state = json.loads(application.after_json)
        native_after = _capture_state(
            self.collection, card_id, (leaf_id, child_id, parent_id)
        )
        self.assertEqual(set(after_state["counters"]), {leaf_key, child_key, parent_key})
        self.assertEqual(after_state["counters"], native_after["counters"])

        duplicate_rows, duplicate_conflict = self.with_context.receive_compact_batch(
            device_id, session_id, publication.snapshot_id, 1, [[1, 3, event_ms, 120]]
        )
        self.assertFalse(duplicate_conflict)
        self.assertEqual(len(duplicate_rows), 1)
        with self._answer_call_counter() as duplicate_calls:
            duplicate = service.apply_pending(
                self.collection,
                device_id,
                session_id,
                collection_identity=self.profile_id,
                expected_generation=self.with_context.get_collection_generation(),
                authorization=None,
            )
        self.assertEqual(duplicate.status, "idle")
        self.assertEqual(duplicate_calls[0], 0)
        self.assertEqual(
            self.collection.db.all(
                "SELECT id,cid,ease,ivl,type FROM revlog WHERE cid=? ORDER BY id", card_id
            ),
            after_revlog,
        )
        self.assertTrue(self.with_context.pull_gate(device_id, session_id).allowed)
        print(
            "QA_NESTED_ACCEPTANCE "
            + json.dumps(
                {
                    "status": "PASS",
                    "source_schema": context.source.to_dict()["schema"],
                    "ancestor_ids": list(context.source.deck_ancestor_ids[leaf_key]),
                    "score_mode": "device_policy",
                    "answer_calls": calls[0],
                    "revlog_rows_before": len(before_revlog),
                    "revlog_rows_after": len(after_revlog),
                    "receipt": bool(application.receipt_hash),
                    "pull_gate_after_receipt": True,
                    "duplicate_answer_calls": duplicate_calls[0],
                    "counter_scope": [leaf_key, child_key, parent_key],
                },
                sort_keys=True,
            )
        )

    def test_nested_ancestor_rename_and_replacement_refuse_before_intent(self) -> None:
        rename_parent, rename_child, rename_leaf = self._create_nested_chain(
            "Acceptance rename parent"
        )
        replacement_parent, replacement_child, replacement_leaf = self._create_nested_chain(
            "Acceptance replacement parent"
        )
        self.deck_ids = [rename_leaf]
        rename_card_id = int(self._add_cards(1)[0])
        self.deck_ids = [replacement_leaf]
        replacement_card_id = int(self._add_cards(1)[0])
        self._prepare_review_card(rename_card_id)
        self._prepare_review_card(replacement_card_id)

        device_id = "qa-context-ancestor-device"
        self.with_context.register_device(device_id, "qa-context-ancestor-token")
        rename_snapshot, rename_record = self._publish_queryop_snapshot([str(rename_card_id)])
        replacement_snapshot, replacement_record = self._publish_queryop_snapshot(
            [str(replacement_card_id)]
        )
        rename_context = rename_record.application_context
        replacement_context = replacement_record.application_context
        self.assertIsNotNone(rename_context)
        self.assertIsNotNone(replacement_context)
        assert rename_context is not None and replacement_context is not None
        rename_leaf_key, rename_child_key, rename_parent_key = map(
            str, (rename_leaf, rename_child, rename_parent)
        )
        replacement_leaf_key, replacement_child_key, replacement_parent_key = map(
            str, (replacement_leaf, replacement_child, replacement_parent)
        )
        self.assertEqual(
            rename_context.source.deck_ancestor_ids[rename_leaf_key],
            (rename_child_key, rename_parent_key),
        )
        self.assertEqual(
            replacement_context.source.deck_ancestor_ids[replacement_leaf_key],
            (replacement_child_key, replacement_parent_key),
        )
        event_ms = int(time.time() * 1000)
        rename_session = "qa-context-ancestor-rename"
        replacement_session = "qa-context-ancestor-replacement"
        self._queue_single_event(
            device_id, rename_session, rename_snapshot.snapshot_id, event_ms
        )
        self._queue_single_event(
            device_id, replacement_session, replacement_snapshot.snapshot_id, event_ms
        )
        service = CompactApplicationService(self.with_context, AnkiCompactAdapter())

        self.collection.decks.rename(rename_parent, "Acceptance rename parent changed")
        rename_scope = self._live_nested_scope(rename_leaf)
        rename_frame_decks = tuple(dict.fromkeys((*rename_scope, rename_parent)))
        rename_before = self._native_score_frame(rename_card_id, rename_frame_decks)
        with self._answer_call_counter() as rename_calls:
            with self.assertRaises(CompactApplicationError) as renamed:
                service.authorize_next(
                    self.collection,
                    device_id,
                    rename_session,
                    collection_identity=self.profile_id,
                    expected_generation=self.with_context.get_collection_generation(),
                )
        self.assertEqual(renamed.exception.code, "snapshot_deck_name_conflict")
        self.assertEqual(rename_calls[0], 0)
        self.assertEqual(self._native_score_frame(rename_card_id, rename_frame_decks), rename_before)
        rename_application = self.with_context.query_compact_applications(
            device_id, rename_session
        )[0]
        self.assertEqual(rename_application.status, "not_applied")
        self.assertIsNone(rename_application.intent_id)
        self.assertFalse(self.with_context.pull_gate(device_id, rename_session).allowed)

        old_parent_name = "Acceptance replacement parent"
        self.collection.decks.rename(replacement_parent, old_parent_name + " retired")
        new_parent = self.collection.decks.add_normal_deck_with_name(old_parent_name)
        replacement_parent_id = int(new_parent.id)
        self.collection.decks.rename(
            replacement_child, old_parent_name + "::Child"
        )
        self.assertNotEqual(replacement_parent_id, replacement_parent)
        live_ancestors = tuple(
            str(row["id"])
            for row in reversed(self.collection.decks.parents(replacement_leaf))
        )
        self.assertEqual(live_ancestors, (replacement_child_key, str(replacement_parent_id)))
        replacement_scope = self._live_nested_scope(replacement_leaf)
        replacement_frame_decks = tuple(
            dict.fromkeys((*replacement_scope, replacement_parent, replacement_parent_id))
        )
        replacement_before = self._native_score_frame(
            replacement_card_id, replacement_frame_decks
        )
        with self._answer_call_counter() as replacement_calls:
            with self.assertRaises(CompactApplicationError) as replaced:
                service.authorize_next(
                    self.collection,
                    device_id,
                    replacement_session,
                    collection_identity=self.profile_id,
                    expected_generation=self.with_context.get_collection_generation(),
                )
        self.assertEqual(replaced.exception.code, "snapshot_parent_deck_conflict")
        self.assertEqual(replacement_calls[0], 0)
        self.assertEqual(
            self._native_score_frame(replacement_card_id, replacement_frame_decks),
            replacement_before,
        )
        replacement_application = self.with_context.query_compact_applications(
            device_id, replacement_session
        )[0]
        self.assertEqual(replacement_application.status, "not_applied")
        self.assertIsNone(replacement_application.intent_id)
        self.assertFalse(self.with_context.pull_gate(device_id, replacement_session).allowed)
        print(
            "QA_NESTED_ANCESTOR_REFUSAL "
            + json.dumps(
                {
                    "status": "PASS",
                    "rename_code": renamed.exception.code,
                    "replacement_code": replaced.exception.code,
                    "rename_answer_calls": rename_calls[0],
                    "replacement_answer_calls": replacement_calls[0],
                    "rename_intent": rename_application.intent_id,
                    "replacement_intent": replacement_application.intent_id,
                    "rename_frame_unchanged": True,
                    "replacement_frame_unchanged": True,
                    "replacement_old_parent": replacement_parent_key,
                    "replacement_new_parent": str(replacement_parent_id),
                },
                sort_keys=True,
            )
        )

    def _fallback_time_fixture(self, *, enqueue=True):
        self.deck_ids = [1]
        self.collection.db.execute("UPDATE col SET crt=?", int(time.time()) - 90 * 86400)
        for key, value in (("rollover", 4), ("localOffset", 480), ("creationOffset", 480)):
            self.collection.set_config(key, value)
        self.collection.set_config("fsrs", True)
        card_id = int(self._add_cards(1)[0])
        card = self.collection.get_card(card_id)
        card.type = card.queue = 2
        card.due = int(self.collection.sched.today) - 1
        card.ivl, card.factor, card.reps = 7, 2500, 1
        card.left, card.lapses = 0, 0
        card.last_review_time = None
        card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
        self.collection.update_card(card)
        event_ms = int(time.time() - 10) * 1000
        history_ms = event_ms - 86400000 + 123
        self.collection.db.execute("INSERT INTO revlog VALUES (?,?,0,3,7,3,2500,1000,1)", history_ms, card_id)
        _, stored = self._publish_queryop_snapshot([str(card_id)])
        self.with_context.register_device("qa-context-device", "qa-context-token")
        self.with_context.open_session("qa-context-device", "fallback-session")
        if enqueue:
            self.with_context.receive_compact_batch("qa-context-device", "fallback-session", stored.snapshot_id,
                                                    1, [[1, 3, event_ms, 120]])
        service = CompactApplicationService(self.with_context, AnkiCompactAdapter())
        return card_id, stored, service

    def test_fallback_time_native_authorize_apply_and_duplicate(self):
        card_id, stored, service = self._fallback_time_fixture()
        self.assertIsNone(self.collection.get_card(card_id).last_review_time)
        source = stored.application_context.source.card_last_review_time_sources[str(card_id)]
        self.assertIsNone(source["native_seconds"])
        with self._answer_call_counter() as calls:
            authorization = service.authorize_next(self.collection, "qa-context-device", "fallback-session",
                                                    collection_identity=self.profile_id, expected_generation=9)
            result = service.apply_pending(self.collection, "qa-context-device", "fallback-session",
                                           collection_identity=self.profile_id, expected_generation=9,
                                           authorization=authorization)
            self.assertEqual(result.status, "applied", result.to_dict())
            self.assertEqual(calls[0], 1)
            repeated = service.apply_pending(self.collection, "qa-context-device", "fallback-session",
                                              collection_identity=self.profile_id, expected_generation=10,
                                              authorization=authorization)
            self.assertEqual(calls[0], 1)
            self.assertNotEqual(repeated.status, "unknown")

    def test_fallback_time_native_history_and_optional_field_drift_reject(self):
        from inkcards_anki_bridge.anki.compact_260903 import _verify_snapshot_card
        from inkcards_anki_bridge.anki.compact_projection import CompactProjectionError
        card_id, stored, _ = self._fallback_time_fixture()
        wire_card = json.loads(stored.payload_json)["cards"][0]
        card = self.collection.get_card(card_id)
        _verify_snapshot_card(self.collection, card, wire_card, stored.application_context)
        self.collection.db.execute("UPDATE revlog SET time=time+1 WHERE cid=?", card_id)
        with self.assertRaises(CompactProjectionError) as caught:
            _verify_snapshot_card(self.collection, card, wire_card, stored.application_context)
        self.assertEqual(caught.exception.code, "snapshot_last_review_source_conflict")
        self.collection.db.execute("UPDATE revlog SET time=time-1 WHERE cid=?", card_id)
        card.last_review_time = wire_card["last_review_time_ms"] // 1000
        self.collection.update_card(card)
        with self.assertRaises(CompactProjectionError):
            _verify_snapshot_card(self.collection, self.collection.get_card(card_id), wire_card, stored.application_context)

    def test_fallback_time_native_export_device_projection_commit(self):
        import subprocess
        card_id, stored, _ = self._fallback_time_fixture(enqueue=False)
        wire = json.loads(stored.payload_json)
        context = dict(device_id="qa-context-device", session_id="fallback-session",
                       scope_id=stored.scope_id, local_generation=1)
        opened = self.snapshot_service.open_pull(**context)
        credentials = dict(permit_id=opened["permit_id"], nonce=opened["nonce"])
        page = self.snapshot_service.page(**context, **credentials, page_index=0, cursor=None)
        self.assertTrue(page["complete"])
        committed = self.snapshot_service.commit(**context, **credentials)
        status = {"device_id": "qa-context-device", "profile_id": self.profile_id,
                  "session_id": "fallback-session", "phase": "idle", "ready_scope_id": stored.scope_id,
                  "compact_delivery": {"total": 0, "counts": {"received": 0}, "ack_semantics": "received_not_applied"},
                  "compact_application": {"total": 0, "recovery_required": 0,
                    "counts": dict(not_applied=0, applying=0, applied=0, unknown=0, conflict=0),
                    "applied_watermark": "unknown"}}
        handshake = {"device_id": "qa-context-device", "profile_id": self.profile_id,
                     "protocol": "x4-2", "session_id": "fallback-session",
                     "session": {"id": "fallback-session", "high_water": 0, "next_sequence": 1}}
        fixture = {"epoch_sec": int(time.time()), "scope_id": stored.scope_id,
                   "last_review_time_ms": wire["cards"][0]["last_review_time_ms"],
                   "responses": [{"path": path, "body": body} for path, body in (
                       ("v2/handshake", handshake), ("v2/sync/status", status),
                       ("v2/pull/open", opened), ("v2/pull/page", page), ("v2/pull/commit", committed))]}
        path = self.root / "fallback-device-fixture.json"
        path.write_text(json.dumps(fixture, ensure_ascii=False))
        for engine in ("/opt/homebrew/bin/lua",
                       "diagnostics/full_scheduler_lab_20261002/device/runs/20261002T213440Z-fsrs32-medium1-9401/runtime/lua"):
            result = subprocess.run([engine, "tests/x4_last_review_time_fallback.lua", str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(self.collection.get_card(card_id).last_review_time)

    def test_fallback_time_history_drift_after_authorization_is_no_write(self):
        card_id, _, service = self._fallback_time_fixture()
        authorization = service.authorize_next(self.collection, "qa-context-device", "fallback-session",
                                                collection_identity=self.profile_id, expected_generation=9)
        self.collection.db.execute("UPDATE revlog SET factor=factor+1 WHERE cid=?", card_id)
        with self._answer_call_counter() as calls:
            result = service.apply_pending(self.collection, "qa-context-device", "fallback-session",
                                           collection_identity=self.profile_id, expected_generation=9,
                                           authorization=authorization)
            self.assertEqual(result.status, "blocked", result.to_dict())
            self.assertEqual(calls[0], 0)

    def test_fallback_time_history_drift_before_score_is_no_write(self):
        card_id, _, service = self._fallback_time_fixture()
        authorization = service.authorize_next(self.collection, "qa-context-device", "fallback-session",
                                                collection_identity=self.profile_id, expected_generation=9)
        apply = service.adapter.apply_prepared
        def drift(collection, prepared, **kwargs):
            collection.db.execute("UPDATE revlog SET time=time+1 WHERE cid=?", card_id)
            return apply(collection, prepared, **kwargs)
        with self._answer_call_counter() as calls, mock.patch.object(service.adapter, "apply_prepared", side_effect=drift):
            result = service.apply_pending(self.collection, "qa-context-device", "fallback-session",
                                           collection_identity=self.profile_id, expected_generation=9,
                                           authorization=authorization)
            self.assertNotEqual(result.status, "applied", result.to_dict())
            self.assertEqual(calls[0], 0)


if __name__ == "__main__":
    unittest.main()
