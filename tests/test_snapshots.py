from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from inkcards_anki_bridge.anki.models import (
    CardSnapshot,
    DayTable,
    DueKind,
    MemoryState,
    PresetSnapshot,
    SchedulerDue,
    SnapshotManifest,
    TypedSnapshot,
)
from inkcards_anki_bridge.anki.application_context import (
    CapturedApplicationContext,
    SnapshotApplicationContext,
)
from inkcards_anki_bridge.anki.snapshot_jobs import make_scope
from inkcards_anki_bridge.services.snapshots import (
    SnapshotError,
    SnapshotService,
)
from inkcards_anki_bridge.storage import ProfileStore
from inkcards_anki_bridge.storage import CommitUnknownError, SchemaError, StorageValidationError


def make_snapshot(question: str = "题面", answer: str = "答案") -> TypedSnapshot:
    card = CardSnapshot(
        card_id="1",
        note_id="10",
        deck_id="20",
        ordinal=0,
        card_type="review",
        queue=2,
        due=SchedulerDue(DueKind.REVIEW_DAY, 42, "day", 42),
        question=question,
        answer=answer,
        memory_state=MemoryState(state="review", stability=4.0),
    )
    manifest = SnapshotManifest(
        schema="inkcards.typed-snapshot.v1",
        profile_identity="profile-a",
        profile_generation=0,
        runtime_epoch=None,
        anki_version="26.8.1",
        anki_build="39e4b0b4",
        scope_card_ids=("1",),
        scope_deck_ids=("20",),
        card_count=1,
        included_card_count=1,
        baseline_counts={"new": 0, "review": 1},
    )
    return TypedSnapshot(
        manifest=manifest,
        decks=(),
        presets=(PresetSnapshot("preset-a", name="默认"),),
        day_table=DayTable(review_day=42),
        cards=(card,),
    )


class SnapshotServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.store.register_device("device-a", "device-token-a")
        self.store.open_session("device-a", "session-a")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_refresh_source_scope_is_persisted_and_verified(self) -> None:
        service = SnapshotService(self.store)
        scope = make_scope(card_ids=None, deck_ids=["20"])
        service.publish(make_snapshot(), scope_id=scope.scope_id,
                        source_scope={"kind": "deck", "ids": ["20"]})
        self.assertEqual(service.refresh_scope(scope.scope_id), {"kind": "deck", "ids": ["20"]})
        opened = service.open_pull(device_id="device-a", session_id="session-a",
                                   scope_id=scope.scope_id, local_generation=0)
        self.assertNotIn("source_scope", opened["preflight"])
        with self.assertRaises(SnapshotError) as raised:
            service.publish(make_snapshot(), scope_id=scope.scope_id,
                            source_scope={"kind": "deck", "ids": ["21"]})
        self.assertEqual(raised.exception.code, "snapshot_refresh_scope_invalid")

    def test_legacy_refresh_scope_never_guesses_parent_deck(self) -> None:
        service = SnapshotService(self.store)
        service.publish(make_snapshot(), scope_id="decks:sha256:unknown")
        with self.assertRaises(SnapshotError) as raised:
            service.refresh_scope("decks:sha256:unknown")
        self.assertEqual(raised.exception.code, "snapshot_refresh_scope_unavailable")

    def test_refresh_result_date_only_requires_every_other_field_equal(self) -> None:
        service = SnapshotService(self.store)
        scope = make_scope(card_ids=None, deck_ids=["20"])
        old = service.publish(make_snapshot(), scope_id=scope.scope_id,
                              source_scope={"kind": "deck", "ids": ["20"]})
        updated = replace(make_snapshot(), day_table=DayTable(review_day=43))
        new = service.publish(updated, scope_id=scope.scope_id,
                              source_scope={"kind": "deck", "ids": ["20"]})
        result = service.refresh_result(scope.scope_id, old.snapshot_id, new.snapshot_id)
        self.assertEqual(result["mode"], "date_only")
        self.assertEqual(result["day_table"]["review_day"], 43)
        changed = service.publish(make_snapshot(answer="异文"), scope_id=scope.scope_id,
                                  source_scope={"kind": "deck", "ids": ["20"]})
        result = service.refresh_result(scope.scope_id, new.snapshot_id, changed.snapshot_id)
        self.assertEqual(result["mode"], "full")
        self.assertNotIn("day_table", result)
        connection = self.store.connect()
        try:
            connection.execute("UPDATE snapshots SET payload_json=payload_json || ' ' WHERE snapshot_id=?",
                               (old.snapshot_id,))
            connection.commit()
        finally:
            connection.close()
        result = service.refresh_result(scope.scope_id, old.snapshot_id, new.snapshot_id)
        self.assertEqual(result["mode"], "full")

    def test_numeric_anki_revision_is_text_in_published_snapshot(self) -> None:
        snapshot = make_snapshot()
        card = replace(snapshot.cards[0], schedule_revision=42)
        snapshot = replace(snapshot, cards=(card,))
        published = SnapshotService(self.store).publish(
            snapshot, scope_id="deck:revision", generation=0, applied_watermark=0
        )
        payload = json.loads(published.payload_bytes)
        self.assertEqual(payload["cards"][0]["schedule_revision"], "42")
        self.assertEqual(
            replace(card, schedule_revision="rev-42").to_dict()["schedule_revision"],
            "rev-42",
        )

    def test_publish_is_canonical_immutable_and_has_preflight(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        published = service.publish(
            make_snapshot(), scope_id="deck:language", generation=7, applied_watermark=0
        )
        self.assertTrue(published.snapshot_id.startswith("sha256:"))
        self.assertEqual(published.manifest["card_count"], 1)
        self.assertEqual(published.manifest["included_card_count"], 1)
        self.assertGreater(published.manifest["payload_bytes"], 0)
        self.assertGreaterEqual(published.manifest["page_count"], 1)
        self.assertGreaterEqual(published.manifest["peak_bytes_estimate"], published.manifest["payload_bytes"])
        self.assertEqual(
            hashlib.sha256(published.payload_bytes).hexdigest(), published.checksum
        )
        self.assertEqual(
            published.payload_bytes,
            json.dumps(
                json.loads(published.payload_bytes.decode("utf-8")),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        with self.assertRaises(SnapshotError) as raised:
            service.publish(
                make_snapshot(answer="异文"),
                scope_id="deck:language",
                generation=7,
                applied_watermark=0,
                snapshot_id=published.snapshot_id,
            )
        self.assertEqual(raised.exception.code, "snapshot_immutable_conflict")

    def _context_snapshot(self, *, max_taken: int = 60, generation: int = 7) -> TypedSnapshot:
        snapshot = make_snapshot()
        manifest = replace(
            snapshot.manifest,
            profile_generation=generation,
            runtime_epoch="epoch-a",
            anki_version="26.09.3",
            anki_build="29bb700b",
        )
        context = CapturedApplicationContext(
            profile_identity="profile-a",
            collection_generation=generation,
            runtime_epoch="epoch-a",
            anki_version="26.09.3",
            anki_build="29bb700b",
            timezone="Asia/Shanghai",
            timezone_source="/etc/localtime -> Asia/Shanghai",
            scheduler_today=42,
            day_cutoff_seconds=1_700_086_400,
            collection_created_at_seconds=1_700_000_000,
            fsrs_enabled=True,
            fsrs_short_term_with_steps_enabled=False,
            deck_preset_ids={"20": "100"},
            preset_configs={
                "100": {
                    "id": 100,
                    "fsrsParams6": [],
                    "desiredRetention": 0.9,
                    "new": {"delays": [1, 10]},
                    "lapse": {"delays": [10]},
                    "rev": {"maxIvl": 36500},
                    "maxTaken": max_taken,
                }
            },
            preset_parameter_identities={
                "100": "anki-native-default:26.09.3:29bb700b"
            },
            deck_ancestor_ids={"20": ()},
            deck_contexts={
                "20": {
                    "deck_id": "20",
                    "name": "Context deck",
                    "parent_id": None,
                    "last_day_studied": 0,
                    "new_studied": 0,
                    "review_studied": 0,
                    "learning_studied": 0,
                    "milliseconds_studied": 0,
                }
            },
            clock_config={"rollover": 4, "localOffset": -480, "creationOffset": -480},
        )
        return replace(snapshot, manifest=manifest, application_context=context)

    def test_snapshot_context_is_bound_immutable_and_duplicate_context_is_exact(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        snapshot = self._context_snapshot()
        first = service.publish(
            snapshot,
            scope_id="deck:context",
            generation=7,
            applied_watermark=0,
            snapshot_id="stable-context-snapshot",
        )
        record = self.store.get_snapshot(first.snapshot_id)
        context = record.application_context
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.profile_id, "profile-a")
        self.assertEqual(context.snapshot_id, first.snapshot_id)
        self.assertEqual(context.snapshot_checksum, first.checksum)
        self.assertEqual(context.snapshot_generation, 7)
        self.assertEqual(context.collection_generation, 7)
        self.assertEqual(context.source.preset_configs["100"]["fsrsParams6"], ())
        self.assertEqual(context.source.deck_ancestor_ids["20"], ())
        self.assertEqual(context.source.deck_contexts["20"]["parent_id"], None)
        self.assertEqual(
            SnapshotApplicationContext.from_json(context.to_json()).source_sha256,
            context.source_sha256,
        )
        self.assertEqual(
            context.source.preset_parameter_identities["100"],
            "anki-native-default:26.09.3:29bb700b",
        )
        with self.assertRaises(TypeError):
            context.source.preset_configs["100"]["maxTaken"] = 90
        with self.assertRaises(TypeError):
            context.source.deck_contexts["20"]["review_studied"] = 90

        repeated = service.publish(
            snapshot,
            scope_id="deck:context",
            generation=7,
            applied_watermark=0,
            snapshot_id="stable-context-snapshot",
        )
        self.assertEqual(repeated.checksum, first.checksum)
        self.assertEqual(
            self.store.get_snapshot(first.snapshot_id).application_context.source_sha256,
            context.source_sha256,
        )

        with self.assertRaises(SnapshotError) as no_context:
            service.publish(
                snapshot.to_dict(),
                scope_id="deck:context",
                generation=7,
                applied_watermark=0,
                snapshot_id="stable-context-snapshot",
            )
        self.assertEqual(no_context.exception.code, "snapshot_context_conflict")

        changed = self._context_snapshot(max_taken=90)
        with self.assertRaises(SnapshotError) as raised:
            service.publish(
                changed,
                scope_id="deck:context",
                generation=7,
                applied_watermark=0,
                snapshot_id="stable-context-snapshot",
            )
        self.assertEqual(raised.exception.code, "snapshot_context_conflict")
        self.assertEqual(
            self.store.get_snapshot(first.snapshot_id).application_context.source.preset_configs["100"]["maxTaken"],
            60,
        )

    def test_pre_ancestry_context_row_is_rejected_without_backfill(self) -> None:
        published = SnapshotService(self.store).publish(
            self._context_snapshot(),
            scope_id="deck:context-pre-ancestry",
            generation=7,
            applied_watermark=0,
            snapshot_id="pre-ancestry-context-snapshot",
        )
        with closing(self.store.connect()) as db:
            row = db.execute(
                "SELECT application_context_json FROM snapshots "
                "WHERE profile_id=? AND snapshot_id=?",
                (self.store.profile_id, published.snapshot_id),
            ).fetchone()
            legacy = json.loads(row[0])
            legacy["schema"] = "inkcards.application-context.v1"
            legacy["source"]["schema"] = "inkcards.application-context-source.v1"
            legacy["source"].pop("deck_ancestor_ids")
            legacy["source"].pop("deck_contexts")
            db.execute(
                "UPDATE snapshots SET application_context_json=? "
                "WHERE profile_id=? AND snapshot_id=?",
                (
                    json.dumps(legacy, ensure_ascii=False, separators=(",", ":")),
                    self.store.profile_id,
                    published.snapshot_id,
                ),
            )
        with self.assertRaises(SchemaError) as raised:
            self.store.get_snapshot(published.snapshot_id)
        self.assertEqual(raised.exception.code, "snapshot_context_incomplete")
        with closing(self.store.connect()) as db:
            raw_context = db.execute(
                "SELECT application_context_json FROM snapshots "
                "WHERE profile_id=? AND snapshot_id=?",
                (self.store.profile_id, published.snapshot_id),
            ).fetchone()[0]
        self.assertIsNotNone(raw_context)
        self.assertIn("application-context.v1", raw_context)

    def test_context_v2_missing_lineage_or_counter_is_rejected(self) -> None:
        source = self._context_snapshot().application_context
        for required in ("deck_ancestor_ids", "deck_contexts"):
            value = source.to_dict()
            value.pop(required)
            with self.subTest(required=required), self.assertRaises(ValueError):
                CapturedApplicationContext.from_dict(value)
        value = source.to_dict()
        value["deck_ancestor_ids"] = {}
        value["deck_contexts"] = {}
        with self.assertRaises(ValueError):
            CapturedApplicationContext.from_dict(value)
        value = source.to_dict()
        value["deck_contexts"]["20"].pop("milliseconds_studied")
        with self.assertRaises(ValueError):
            CapturedApplicationContext.from_dict(value)
        value = source.to_dict()
        value["deck_contexts"]["20"]["name"] = "Parent::Child"
        with self.assertRaises(ValueError):
            CapturedApplicationContext.from_dict(value)

    def test_context_hash_covers_deck_identity_and_all_common_counters(self) -> None:
        source = self._context_snapshot().application_context
        for field in ("name", "last_day_studied", "new_studied", "review_studied",
                      "learning_studied", "milliseconds_studied"):
            value = source.to_dict()
            value["deck_contexts"]["20"][field] = "Changed deck" if field == "name" else 1
            with self.subTest(field=field):
                changed = CapturedApplicationContext.from_dict(value)
                self.assertNotEqual(source.source_sha256, changed.source_sha256)
        value = source.to_dict()
        value["deck_contexts"]["20"]["deck_id"] = "21"
        with self.assertRaises(ValueError):
            CapturedApplicationContext.from_dict(value)

    def test_snapshot_context_generation_mismatch_is_rejected_before_publish(self) -> None:
        service = SnapshotService(self.store)
        with self.assertRaises(SnapshotError) as raised:
            service.publish(
                self._context_snapshot(generation=8),
                scope_id="deck:context-generation",
                generation=7,
                applied_watermark=0,
            )
        self.assertEqual(raised.exception.code, "snapshot_context_generation_mismatch")
        with self.assertRaises(StorageValidationError):
            self.store.get_snapshot("sha256:missing")

    def test_snapshot_and_context_roll_back_together_on_commit_failure(self) -> None:
        service = SnapshotService(self.store)
        original_commit = self.store._commit

        def fail_commit(_db) -> None:
            raise sqlite3.OperationalError("simulated snapshot commit failure")

        self.store._commit = fail_commit
        try:
            with self.assertRaises(CommitUnknownError):
                service.publish(
                    self._context_snapshot(),
                    scope_id="deck:context-commit",
                    generation=7,
                    applied_watermark=0,
                )
        finally:
            self.store._commit = original_commit

        with self.assertRaises(StorageValidationError):
            self.store.get_snapshot("sha256:missing")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshot_pages").fetchone()[0], 0)
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE application_context_json IS NOT NULL"
                ).fetchone()[0],
                0,
            )

    def test_open_pull_returns_exact_canonical_metadata_without_cards(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        published = service.publish(
            make_snapshot(), scope_id="deck:language", generation=7, applied_watermark=0
        )
        opened = service.open_pull(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            snapshot_id=published.snapshot_id,
            max_response_bytes=16 * 1024,
        )

        metadata = opened["snapshot_metadata"]
        self.assertEqual(
            set(metadata),
            {
                "schema",
                "snapshot_schema",
                "manifest",
                "decks",
                "presets",
                "day_table",
                "complete",
                "unsupported",
                "profile_id",
                "scope_id",
                "snapshot_generation",
                "applied_watermark",
            },
        )
        self.assertNotIn("cards", metadata)
        payload = json.loads(
            self.store.get_snapshot(published.snapshot_id).payload_json
        )
        expected = {
            "schema": "inkcards.snapshot-metadata.v1",
            **{key: payload[key] for key in set(payload) - {"cards"}},
        }
        self.assertEqual(metadata, expected)
        metadata_wire = json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            opened["snapshot_metadata_hash"],
            hashlib.sha256(metadata_wire).hexdigest(),
        )
        self.assertEqual(opened["snapshot_hash"], published.checksum)

    def test_open_pull_metadata_is_stable_when_resuming_permit(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        published = service.publish(
            make_snapshot(), scope_id="deck:language", generation=0, applied_watermark=0
        )
        opened = service.open_pull(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            snapshot_id=published.snapshot_id,
        )
        resumed = service.open_pull(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            snapshot_id=published.snapshot_id,
            permit_id=opened["permit_id"],
            nonce=opened["nonce"],
        )
        self.assertEqual(resumed["permit_id"], opened["permit_id"])
        self.assertEqual(resumed["nonce"], opened["nonce"])
        self.assertEqual(
            resumed["snapshot_metadata"], opened["snapshot_metadata"]
        )
        self.assertEqual(
            resumed["snapshot_metadata_hash"], opened["snapshot_metadata_hash"]
        )
        self.assertEqual(resumed["snapshot_hash"], opened["snapshot_hash"])

    def test_payload_or_identity_tamper_fails_closed_before_permit(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        published = service.publish(
            make_snapshot(), scope_id="deck:language", generation=0, applied_watermark=0
        )
        connection = self.store.connect()
        try:
            payload = json.loads(
                self.store.get_snapshot(published.snapshot_id).payload_json
            )
            payload["cards"][0]["answer"] = "篡改"
            tampered = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                "UPDATE snapshots SET payload_json=? WHERE profile_id=? AND snapshot_id=?",
                (tampered, self.store.profile_id, published.snapshot_id),
            )
        finally:
            connection.close()

        with self.assertRaises(SnapshotError) as raised:
            service.open_pull(
                device_id="device-a",
                session_id="session-a",
                scope_id="deck:language",
                local_generation=0,
                snapshot_id=published.snapshot_id,
            )
        self.assertEqual(raised.exception.code, "snapshot_corrupt")

        connection = self.store.connect()
        try:
            payload["profile_id"] = "other-profile"
            identity_tampered = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                "UPDATE snapshots SET payload_json=?,checksum=?,payload_bytes=? "
                "WHERE profile_id=? AND snapshot_id=?",
                (
                    identity_tampered,
                    hashlib.sha256(identity_tampered.encode("utf-8")).hexdigest(),
                    len(identity_tampered.encode("utf-8")),
                    self.store.profile_id,
                    published.snapshot_id,
                ),
            )
        finally:
            connection.close()

        with self.assertRaises(SnapshotError) as raised:
            service.open_pull(
                device_id="device-a",
                session_id="session-a",
                scope_id="deck:language",
                local_generation=0,
                snapshot_id=published.snapshot_id,
            )
        self.assertEqual(raised.exception.code, "snapshot_corrupt")

    def test_pending_or_conflict_events_block_pull_but_zero_events_allow(self) -> None:
        service = SnapshotService(self.store)
        published = service.publish(
            make_snapshot(), scope_id="deck:language", generation=0, applied_watermark=0
        )
        opened = service.open_pull(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            snapshot_id=published.snapshot_id,
        )
        self.assertEqual(opened["preflight"]["page_count"], 1)

        self.store.receive_batch(
            "device-a",
            "session-a",
            [
                {
                    "event_id": "session-a:1",
                    "sequence": 1,
                    "payload": {"card_id": "1", "rating": 3},
                }
            ],
        )
        with self.assertRaises(SnapshotError) as raised:
            service.open_pull(
                device_id="device-a",
                session_id="session-a",
                scope_id="deck:language",
                local_generation=0,
                snapshot_id=published.snapshot_id,
            )
        self.assertEqual(raised.exception.code, "pull_gate_pending_apply")

    def test_wire_budget_single_card_is_explicitly_unpageable(self) -> None:
        service = SnapshotService(self.store, page_bytes=256)
        with self.assertRaises(SnapshotError) as raised:
            service.publish(
                make_snapshot(question="超长" * 500),
                scope_id="deck:language",
                generation=0,
                applied_watermark=0,
            )
        self.assertEqual(raised.exception.code, "snapshot_unpageable")

    def test_nonce_page_idempotency_commit_and_restart(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        published = service.publish(
            make_snapshot(), scope_id="deck:language", generation=0, applied_watermark=0
        )
        opened = service.open_pull(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            snapshot_id=published.snapshot_id,
        )
        self.assertGreaterEqual(len(opened["nonce"]), 43)
        page = service.page(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            permit_id=opened["permit_id"],
            nonce=opened["nonce"],
            page_index=0,
            cursor=None,
        )
        repeat = service.page(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            permit_id=opened["permit_id"],
            nonce=opened["nonce"],
            page_index=0,
            cursor=None,
        )
        self.assertEqual(page, repeat)
        service.close()  # progress/permit must be SQLite-owned, not service-memory-owned
        reopened = SnapshotService(self.store, page_bytes=1200)
        committed = reopened.commit(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            permit_id=opened["permit_id"],
            nonce=opened["nonce"],
            snapshot_id=published.snapshot_id,
        )
        self.assertTrue(committed["committed"])
        with self.assertRaises(SnapshotError) as raised:
            reopened.commit(
                device_id="device-a",
                session_id="session-a",
                scope_id="deck:language",
                local_generation=0,
                permit_id=opened["permit_id"],
                nonce=opened["nonce"],
                snapshot_id=published.snapshot_id,
            )
        self.assertEqual(raised.exception.code, "permit_already_used")

    def test_resumed_pull_replays_all_confirmed_pages_without_advancing_progress(self) -> None:
        original = make_snapshot()
        cards = tuple(
            replace(original.cards[0], card_id=str(index), note_id=str(index + 10))
            for index in range(1, 4)
        )
        snapshot = replace(
            original,
            cards=cards,
            manifest=replace(
                original.manifest,
                scope_card_ids=tuple(card.card_id for card in cards),
                card_count=len(cards),
                included_card_count=len(cards),
                baseline_counts={"new": 0, "review": len(cards)},
            ),
        )
        service = SnapshotService(self.store, page_bytes=1200, max_cards_per_page=1)
        published = service.publish(
            snapshot, scope_id="deck:language", generation=0, applied_watermark=0
        )
        opened = service.open_pull(
            device_id="device-a", session_id="session-a", scope_id="deck:language",
            local_generation=0, snapshot_id=published.snapshot_id,
        )
        self.assertEqual(opened["preflight"]["page_count"], 3)
        request = {
            "device_id": "device-a", "session_id": "session-a",
            "scope_id": "deck:language", "local_generation": 0,
            "permit_id": opened["permit_id"], "nonce": opened["nonce"],
        }
        pages = []
        cursor = None
        for index in range(2):
            page = service.page(**request, page_index=index, cursor=cursor)
            pages.append(page)
            cursor = page["cursor"]
        progress = self.store.get_pull_progress(opened["permit_id"])
        self.assertEqual((progress.next_page_index, progress.received_pages), (2, 2))
        self.assertFalse(progress.complete)
        service.close()

        reopened = SnapshotService(self.store, page_bytes=1200, max_cards_per_page=1)
        resumed = reopened.open_pull(
            **request, snapshot_id=published.snapshot_id,
        )
        self.assertEqual((resumed["next_page_index"], resumed["received_pages"]), (2, 2))
        cursor = None
        replayed = []
        for index in range(2):
            page = reopened.page(**request, page_index=index, cursor=cursor)
            replayed.append(page)
            cursor = page["cursor"]
            self.assertEqual(self.store.get_pull_progress(opened["permit_id"]), progress)
        self.assertEqual(replayed, pages)
        final_page = reopened.page(**request, page_index=2, cursor=cursor)
        self.assertTrue(final_page["complete"])
        self.assertEqual(
            self.store.get_pull_progress(opened["permit_id"]).received_pages, 3
        )

        for changes, expected_code in (
            ({"cursor": "wrong"}, "page_out_of_order"),
            ({"nonce": "wrong"}, "permit_invalid"),
            ({"device_id": "other-device"}, "identity_mismatch"),
        ):
            invalid = dict(request, page_index=0, cursor=None)
            invalid.update(changes)
            with self.assertRaises(SnapshotError) as raised:
                reopened.page(**invalid)
            self.assertEqual(raised.exception.code, expected_code)
            self.assertEqual(
                self.store.get_pull_progress(opened["permit_id"]).received_pages, 3
            )

        committed = reopened.commit(
            **request, snapshot_id=published.snapshot_id,
        )
        self.assertTrue(committed["committed"])

    def test_new_scope_generation_revokes_old_permit(self) -> None:
        service = SnapshotService(self.store, page_bytes=1200)
        first = service.publish(
            make_snapshot(), scope_id="deck:language", generation=0, applied_watermark=0
        )
        opened = service.open_pull(
            device_id="device-a",
            session_id="session-a",
            scope_id="deck:language",
            local_generation=0,
            snapshot_id=first.snapshot_id,
        )
        service.publish(
            make_snapshot(answer="新答案"),
            scope_id="deck:language",
            generation=1,
            applied_watermark=0,
        )
        with self.assertRaises(SnapshotError) as raised:
            service.page(
                device_id="device-a",
                session_id="session-a",
                scope_id="deck:language",
                local_generation=0,
                permit_id=opened["permit_id"],
                nonce=opened["nonce"],
                page_index=0,
                cursor=None,
                snapshot_id=first.snapshot_id,
            )
        self.assertEqual(raised.exception.code, "permit_invalid")


if __name__ == "__main__":
    unittest.main()
