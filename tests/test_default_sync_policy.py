from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from inkcards_anki_bridge.runtime import AddonRuntime
from inkcards_anki_bridge.anki.snapshot_jobs import make_scope
from tests.test_pull_refresh_runtime import FakeServer, FakeManager, FakeJob
from inkcards_anki_bridge.anki.adapter_260801 import Anki260801Adapter
from tests.test_snapshot_jobs import FakeCollection, FakeCard


class DefaultSyncPolicyTests(unittest.TestCase):
    def test_default_save_restart_and_failed_save_keeps_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = AddonRuntime(profile_id="p", root=temp)
            self.assertEqual(runtime.get_sync_policy(), {"excluded_deck_ids": [], "revision": 0})
            self.assertEqual(runtime.set_excluded_decks(["20", "10", "20"]),
                             {"excluded_deck_ids": ["10", "20"], "revision": 1})
            other = AddonRuntime(profile_id="p", root=temp)
            self.assertEqual(other.get_sync_policy(), runtime.get_sync_policy())
            with patch("inkcards_anki_bridge.sync_policy.os.replace", side_effect=OSError("disk")):
                with self.assertRaises(Exception) as caught:
                    runtime.set_excluded_decks([])
            self.assertEqual(caught.exception.code, "sync_policy_save_failed")
            self.assertEqual(other.get_sync_policy()["excluded_deck_ids"], ["10", "20"])

    def test_corrupt_read_blocks_instead_of_default(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = AddonRuntime(profile_id="p", root=temp)
            runtime.set_excluded_decks(["10"])
            (Path(temp) / "sync-policy.json").write_text("{bad")
            with self.assertRaises(Exception) as caught:
                runtime.get_sync_policy()
            self.assertEqual(caught.exception.code, "sync_policy_corrupt")

    def test_empty_exclusion_scope_is_stable(self):
        scope = make_scope(card_ids=None, deck_ids=None, excluded_deck_ids=[])
        self.assertEqual(scope.kind, "exclude")
        self.assertEqual(scope.ids, ())
        self.assertEqual(scope.scope_id, make_scope(card_ids=None, deck_ids=None, excluded_deck_ids=[]).scope_id)
        self.assertNotEqual(scope.scope_id, make_scope(card_ids=None, deck_ids=None, excluded_deck_ids=["10"]).scope_id)

    def test_parent_exclusion_exact_card_filter_and_new_decks(self):
        collection = FakeCollection([FakeCard(1, 10), FakeCard(2, 20), FakeCard(3, 30)])
        names = {10: "Root", 20: "Root::Child", 30: "Other"}
        collection.decks.all_names_and_ids = lambda **kw: [SimpleNamespace(id=k, name=v) for k, v in names.items()]
        collection.find_cards = lambda query, order=False: list(collection.cards)
        adapter = Anki260801Adapter(collection_provider=lambda: collection,
            profile_provider=lambda: {"profile_identity": "p", "generation": 0, "runtime_epoch": "e"},
            profile_identity="p", anki_version="26.09.3", anki_build="29bb700b")
        self.assertEqual(adapter.resolve_excluded_card_ids(collection, ["10"]), ["3"])
        names[10], names[20] = "Renamed", "Renamed::Child"
        self.assertEqual(adapter.resolve_excluded_card_ids(collection, ["10"]), ["3"])
        names[40] = "Renamed::New"
        collection.cards[4] = FakeCard(4, 40)
        self.assertEqual(adapter.resolve_excluded_card_ids(collection, ["10"]), ["3"])
        self.assertEqual(adapter.resolve_excluded_card_ids(collection, ["10", "30"]), [])

    def test_capacity_above_legacy_limits(self):
        self.assertGreaterEqual(len(make_scope(card_ids=None, deck_ids=list(range(1, 100))).ids), 99)
        collection = FakeCollection([FakeCard(i, 10) for i in range(1, 4100)])
        collection.decks.all_names_and_ids = lambda **kw: [SimpleNamespace(id=10, name="All")]
        collection.find_cards = lambda query, order=False: list(collection.cards)
        adapter = Anki260801Adapter(collection_provider=lambda: collection,
            profile_provider=lambda: {"profile_identity": "p"}, profile_identity="p",
            anki_version="26.09.3", anki_build="29bb700b")
        self.assertEqual(len(adapter.resolve_excluded_card_ids(collection, [])), 4099)

    def test_first_request_without_snapshot_and_policy_frozen_by_token(self):
        class Manager(FakeManager):
            def request_snapshot(self, *, excluded_deck_ids=None, **kw):
                self.calls.append(tuple(excluded_deck_ids))
                self.job = FakeJob(make_scope(card_ids=None, deck_ids=None, excluded_deck_ids=excluded_deck_ids).scope_id)
                return self.job
        with tempfile.TemporaryDirectory() as temp, patch("inkcards_anki_bridge.runtime.DeviceServer", FakeServer):
            runtime = AddonRuntime(profile_id="p", root=temp).start()
            try:
                runtime.store.register_device("d", "t")
                runtime.store.open_session("d", "s")
                manager = Manager()
                runtime.attach_snapshot_jobs(manager)
                scope = runtime.default_sync_scope_id()
                status, response = runtime.refresh_pull(device_id="d", session_id="s", scope_id=scope)
                self.assertEqual(status, 202)
                runtime.set_excluded_decks(["10"])
                runtime.refresh_pull(device_id="d", session_id="s", scope_id=scope,
                                     refresh_token=response["refresh_token"])
                self.assertEqual(manager.calls, [()])
                self.assertNotEqual(scope, runtime.default_sync_scope_id())
                self.assertEqual(runtime.get_status()["sync"]["stage"], "preparing")
            finally:
                runtime.stop()


class NativeDefaultSyncTests(unittest.TestCase):
    def setUp(self):
        from tests.test_snapshot_context_native import NativeSnapshotContextTests
        NativeSnapshotContextTests.setUp(self)

    def tearDown(self):
        from tests.test_snapshot_context_native import NativeSnapshotContextTests
        NativeSnapshotContextTests.tearDown(self)

    def _add_cards(self, count):
        from tests.test_snapshot_context_native import NativeSnapshotContextTests
        return NativeSnapshotContextTests._add_cards(self, count)

    def test_native_empty_and_all_excluded_artifacts_do_not_retain_old_cards(self):
        from inkcards_anki_bridge.services.snapshots import SnapshotService
        from inkcards_anki_bridge.services.snapshot_files import SnapshotFilesService
        from inkcards_anki_bridge.storage import ProfileStore
        self._add_cards(4)
        store = ProfileStore(self.root / "store", profile_id="profile-native")
        try:
            store.set_collection_generation(9)
            service = SnapshotService(store)
            files = SnapshotFilesService(store)
            for ids in ([], list(map(str, self.deck_ids))):
                scope = make_scope(card_ids=None, deck_ids=None, excluded_deck_ids=ids)
                typed = self.adapter.export_snapshot_from_collection(self.collection,
                    excluded_deck_ids=ids, context=("profile-native", 9, "epoch-native"))
                pub = service.publish(typed, scope_id=scope.scope_id, generation=9,
                                      source_scope={"kind": "exclude", "ids": ids})
                if ids:
                    self.assertEqual(len(typed.cards), 0)
                    root = files._prepare_snapshot(store.get_snapshot(pub.snapshot_id))
                    self.assertEqual(root["card_count"], 0)
                    self.assertEqual(root["metadata"]["catalog"], [])
                    self.assertEqual(root["descriptor_pages"], [])
                    self.assertEqual(service.refresh_scope(scope.scope_id), {"kind": "exclude", "ids": sorted(ids, key=int)})
            self.collection.remove_notes(list(self.collection.find_notes("")))
            typed = self.adapter.export_snapshot_from_collection(self.collection,
                excluded_deck_ids=[], context=("profile-native", 9, "epoch-native"))
            self.assertEqual(len(typed.cards), 0)
        finally:
            store.close()

    def test_native_exact_filter_rename_new_child_and_catalog_counts(self):
        from inkcards_anki_bridge.anki.catalog_jobs import build_deck_catalog
        self._add_cards(4)
        excluded = [str(self.child_deck_id)]
        selected = self.adapter.resolve_excluded_card_ids(self.collection, excluded)
        self.assertEqual(len(selected), 2)
        self.collection.decks.rename(self.child_deck_id, "Context hierarchy::Renamed")
        self.assertEqual(self.adapter.resolve_excluded_card_ids(self.collection, excluded), selected)
        new_id = self.collection.decks.add_normal_deck_with_name("Context hierarchy::Renamed::New").id
        note = self.collection.new_note(self.collection.models.by_name("Basic"))
        note.fields = ["new", "answer"]
        self.collection.add_note(note, new_id)
        self.assertEqual(self.adapter.resolve_excluded_card_ids(self.collection, excluded), selected)
        catalog = build_deck_catalog(self.collection, self.adapter)
        self.assertEqual(catalog.by_id(str(self.child_deck_id)).card_count, 3)

    def test_missing_image_keeps_card_and_publishes_placeholder(self):
        from inkcards_anki_bridge.anki.snapshot_jobs import SnapshotJobManager, SnapshotJobState
        from inkcards_anki_bridge.anki.operations import OperationContext, QueryOpAdapter
        from tests.test_snapshot_jobs import FakeHost
        self._add_cards(1)
        note = self.collection.get_note(self.collection.find_notes("")[0])
        note.fields[0] = '前文<img src="missing.png">后文'
        self.collection.update_note(note)
        host = FakeHost()
        context = lambda: OperationContext("profile-native", 9, "epoch-native")
        publications = []
        manager = SnapshotJobManager(operation_adapter=QueryOpAdapter(host, context), exporter=self.adapter,
            context_provider=context, publish=lambda snapshot, **kw: publications.append(snapshot))
        job = manager.request_snapshot(excluded_deck_ids=[])
        host.pending.pop(0)(self.collection)
        self.assertEqual(job.state, SnapshotJobState.PUBLISHED)
        snapshot = publications[0]
        self.assertEqual(snapshot.manifest.included_card_count, 1)
        self.assertFalse(snapshot.manifest.skipped)
        blocks = snapshot.cards[0].question_blocks
        self.assertEqual(blocks[0]['text'], '前文')
        self.assertIn('图片不可用', blocks[1]['text'])
        self.assertEqual(blocks[2]['text'], '后文')

    def test_auto_job_unsupported_blocks_with_card_reason_and_keeps_previous(self):
        from inkcards_anki_bridge.anki.snapshot_jobs import SnapshotJobManager, SnapshotJobState
        from inkcards_anki_bridge.anki.operations import OperationContext, QueryOpAdapter
        from tests.test_snapshot_jobs import FakeHost
        self._add_cards(1)
        note = self.collection.get_note(self.collection.find_notes("")[0])
        note.fields[0] = '<video src="missing.mp4"></video>'
        self.collection.update_note(note)
        host = FakeHost()
        context = lambda: OperationContext("profile-native", 9, "epoch-native")
        publications = ["previous"]
        manager = SnapshotJobManager(operation_adapter=QueryOpAdapter(host, context), exporter=self.adapter,
            context_provider=context, publish=lambda snapshot, **kw: publications.append(snapshot))
        job = manager.request_snapshot(excluded_deck_ids=[])
        host.pending.pop(0)(self.collection)
        self.assertEqual(job.state, SnapshotJobState.FAILED)
        self.assertEqual(job.error.code, "snapshot_content_unsupported")
        self.assertIn("media", str(job.error))
        self.assertIn("Context hierarchy", str(job.error))
        self.assertIn(str(self.root_deck_id), str(job.error))
        self.assertIn(str(self.collection.find_cards("")[0]), str(job.error))
        self.assertEqual(publications, ["previous"])


class ArtifactCapacityTests(unittest.TestCase):
    def test_more_than_64_decks_and_4096_cards_prepare_batched_files(self):
        from dataclasses import replace
        from tests.test_snapshot_files import make_snapshot
        from inkcards_anki_bridge.storage import ProfileStore
        from inkcards_anki_bridge.services.snapshots import SnapshotService
        from inkcards_anki_bridge.services.snapshot_files import SnapshotFilesService
        rows = [dict(card_id=str(1700000000000 + i), note_id=str(1800000000000 + i),
                     ordinal=i, question="Q", answer="A") for i in range(4100)]
        typed = make_snapshot(rows)
        typed = replace(typed,
            decks=tuple(replace(typed.decks[0], deck_id=str(1700000000000+i), name="Deck " + str(i)) for i in range(1, 66)),
            cards=tuple(replace(card, deck_id=str(1700000000000+i % 65 + 1)) for i, card in enumerate(typed.cards)),
            manifest=replace(typed.manifest, scope_card_ids=(), scope_deck_ids=tuple(str(1700000000000+i) for i in range(1, 66))))
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(Path(temp), profile_id="profile-a")
            try:
                scope = make_scope(card_ids=None, deck_ids=None, excluded_deck_ids=[])
                pub = SnapshotService(store).publish(typed, scope_id=scope.scope_id,
                        source_scope={"kind": "exclude", "ids": []})
                root = SnapshotFilesService(store)._prepare_snapshot(store.get_snapshot(pub.snapshot_id))
                self.assertEqual(root["card_count"], 4100)
                self.assertEqual(len(root["metadata"]["catalog"]), 65)
                self.assertGreater(len(root["descriptor_pages"]), 0)
            finally:
                store.close()


class SyncStatusTests(unittest.TestCase):
    def test_snapshot_publication_is_prepared_and_only_commit_is_completed(self):
        from inkcards_anki_bridge.services.snapshot_files import ArtifactError
        with tempfile.TemporaryDirectory() as temp, patch("inkcards_anki_bridge.runtime.DeviceServer", FakeServer):
            runtime = AddonRuntime(profile_id="p", root=temp).start()
            try:
                runtime.store.register_device("d", "token-a")
                runtime.store.open_session("d", "s")
                runtime._observe_device_sync("prepared", device_id="d", session_id="s")
                self.assertEqual(runtime.status()["sync"]["state"], "active")
                request = json.dumps({"device_id": "d", "session_id": "s", "transfer_id": "transfer",
                                      "commit_key": "key"}).encode()
                service = runtime.router.snapshot_files_service
                with patch.object(service, "commit", side_effect=ArtifactError("artifact_files_incomplete", status=409)):
                    response = runtime.router.dispatch("POST", "/v2/artifacts/commit",
                        {"Authorization": "Bearer token-a"}, request)
                self.assertEqual(response.status, 409)
                self.assertEqual(runtime.status()["sync"]["state"], "failed")
                self.assertEqual(runtime.status()["sync"]["error"]["code"], "artifact_files_incomplete")
                with patch.object(service, "commit", return_value={"status": "committed", "snapshot_id": "snapshot"}):
                    response = runtime.router.dispatch("POST", "/v2/artifacts/commit",
                        {"Authorization": "Bearer token-a"}, request)
                self.assertEqual(response.status, 200)
                self.assertEqual(runtime.status()["sync"]["state"], "completed")
                self.assertEqual(runtime.status()["sync"]["stage"], "committed")
            finally:
                runtime.stop()


class SourceCapacityTests(unittest.TestCase):
    def _collection(self):
        collection = FakeCollection([FakeCard(i, 10 if i <= 32000 else 20) for i in range(1, 33001)])
        collection.decks.all_names_and_ids = lambda **kw: [SimpleNamespace(id=10, name="Large"),
                                                          SimpleNamespace(id=20, name="Kept")]
        collection.find_cards = lambda query, order=False: list(collection.cards)
        adapter = Anki260801Adapter(collection_provider=lambda: collection,
            profile_provider=lambda: {"profile_identity": "p"}, profile_identity="p",
            anki_version="26.09.3", anki_build="29bb700b")
        return collection, adapter

    def test_excluded_source_over_limit_can_sync_small_effective_range(self):
        collection, adapter = self._collection()
        self.assertEqual(len(adapter.resolve_excluded_card_ids(collection, ["10"])), 1000)
        with self.assertRaises(Exception) as caught:
            adapter.resolve_excluded_card_ids(collection, [])
        self.assertEqual(caught.exception.code, "scope_too_large")

    def test_source_scan_budget_and_cancel_remain_bounded(self):
        collection, adapter = self._collection()
        collection.find_cards = lambda query, order=False: range(1, 262146)
        with self.assertRaises(Exception) as caught:
            adapter.resolve_excluded_card_ids(collection, ["10"])
        self.assertEqual(caught.exception.code, "source_scan_too_large")
        collection.find_cards = lambda query, order=False: list(collection.cards)
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            adapter.resolve_excluded_card_ids(collection, ["10"],
                cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("cancel")))


class CommittedTimestampTests(unittest.TestCase):
    def test_committed_timestamp_matches_persistent_receipt_before_and_after_restart(self):
        from tests.test_snapshot_files import make_snapshot
        with tempfile.TemporaryDirectory() as temp, patch("inkcards_anki_bridge.runtime.DeviceServer", FakeServer):
            runtime = AddonRuntime(profile_id="profile-a", root=temp).start()
            try:
                runtime.store.register_device("d", "token-a")
                runtime.store.open_session("d", "s")
                pub = runtime.publish_snapshot(make_snapshot([]), scope_id="empty")
                service = runtime.router.snapshot_files_service
                service._clock = lambda: 1700000000123
                transfer = service.open_transfer(device_id="d", session_id="s",
                    snapshot_id=pub.snapshot_id, scope_id="empty")
                request = json.dumps({"device_id": "d", "session_id": "s",
                    "transfer_id": transfer["transfer_id"],
                    "commit_key": "12345678-1234-1234-1234-123456789012", "present_file_ids": []}).encode()
                response = runtime.router.dispatch("POST", "/v2/artifacts/commit",
                    {"Authorization": "Bearer token-a"}, request)
                self.assertEqual(response.status, 200, response.body)
                current = runtime.status()["sync"]
                self.assertEqual(current["updated_at_ms"], 1700000000123)
                self.assertEqual(current["completed_at_ms"], 1700000000123)
            finally:
                runtime.stop()
            runtime = AddonRuntime(profile_id="profile-a", root=temp).start()
            try:
                recent = runtime.status()["sync"]
                self.assertEqual(recent["state"], "completed")
                self.assertEqual(recent["updated_at_ms"], current["updated_at_ms"])
                self.assertEqual(recent["completed_at_ms"], current["completed_at_ms"])
            finally:
                runtime.stop()
