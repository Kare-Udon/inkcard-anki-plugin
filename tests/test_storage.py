from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from inkcards_anki_bridge.domain.sync_gate import (
    LUA_SAFE_INTEGER_MAX,
    ImmutableReviewEvent,
)
from inkcards_anki_bridge.storage import (
    BusyStorageError,
    CommitUnknownError,
    ProfileStore,
    ReceiptRegressionError,
    SchemaError,
    SequenceError,
    StorageValidationError,
    UnknownApplyError,
    UnsupportedDatabaseError,
    UnsupportedSchemaError,
)
from inkcards_anki_bridge.storage.migrations import (
    _v1_statements,
    _v2_statements,
    _v3_statements,
    _v4_statements,
    _v5_statements,
)
from inkcards_anki_bridge.storage import migrations


def make_event(session_id: str, sequence: int, rating: int = 3) -> ImmutableReviewEvent:
    return ImmutableReviewEvent.create(
        f"{session_id}:{sequence}",
        sequence,
        {
            "card_id": f"card-{sequence}",
            "rating": rating,
            "answered_at_ms": 1_700_000_000_000 + sequence,
        },
    )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "profile-a"
        self.store = ProfileStore(
            self.root, profile_id="profile-a", busy_timeout_ms=120
        )
        self.store.register_device("device-a", "device-token-a")
        self.store.open_session("device-a", "session-a")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def create_v5_snapshot_database(self, root: Path) -> str:
        root.mkdir()
        payload = json.dumps(
            {"snapshot_schema": "legacy.v1", "cards": [{"card_id": "legacy-card"}]},
            separators=(",", ":"),
            sort_keys=True,
        )
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        with closing(sqlite3.connect(root / "bridge.sqlite3")) as db:
            for statements in (
                _v1_statements(),
                _v2_statements(),
                _v3_statements(),
                _v4_statements(),
                _v5_statements(),
            ):
                for statement in statements:
                    db.execute(statement)
            for version in range(1, 6):
                db.execute(
                    "INSERT INTO schema_migrations(version,applied_at_ms) VALUES(?,?)",
                    (version, version),
                )
            db.execute("PRAGMA user_version=5")
            db.execute(
                "INSERT INTO profile_meta(profile_id,created_at_ms,updated_at_ms,collection_generation) "
                "VALUES('profile-a',1,1,0)"
            )
            db.execute(
                "INSERT INTO snapshots(profile_id,snapshot_id,scope_generation,applied_watermark,payload_json,checksum,"
                "created_at_ms,scope_id,snapshot_generation,manifest_json,card_count,included_card_count,skipped_count,"
                "payload_bytes,page_count,peak_bytes_estimate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "profile-a", "legacy-snapshot", 0, 0, payload, checksum, 2,
                    "legacy-scope", 0, "{}", 1, 1, 0, len(payload.encode()), 1, 1,
                ),
            )
            db.commit()
        return payload

    def test_v5_snapshot_migrates_to_v7_without_backfilling_context(self) -> None:
        root = self.root.parent / "v5-context"
        old_payload = self.create_v5_snapshot_database(root)
        migrated = ProfileStore(root, profile_id="profile-a")
        try:
            record = migrated.get_snapshot("legacy-snapshot")
            self.assertEqual(migrated.schema_version, 9)
            self.assertEqual(record.payload_json, old_payload)
            self.assertIsNone(record.application_context)
            with closing(migrated.connect()) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)
        finally:
            migrated.close()

    def test_v5_to_v6_migration_ddl_failure_rolls_back(self) -> None:
        root = self.root.parent / "v5-context-rollback"
        old_payload = self.create_v5_snapshot_database(root)
        original = migrations._v6_statements

        def fail_after_alter():
            return tuple(original()) + ("INSERT INTO missing_context_migration_table VALUES(1)",)

        with mock.patch.object(migrations, "_v6_statements", side_effect=fail_after_alter):
            with self.assertRaises(SchemaError):
                ProfileStore(root, profile_id="profile-a")
        with closing(sqlite3.connect(root / "bridge.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            columns = {row[1] for row in db.execute("PRAGMA table_info('snapshots')")}
            self.assertNotIn("application_context_json", columns)
            self.assertEqual(db.execute("SELECT payload_json FROM snapshots").fetchone()[0], old_payload)
            self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 5)

    def test_v6_to_v7_artifact_migration_ddl_failure_rolls_back(self) -> None:
        root = self.root.parent / "v6-artifact-rollback"
        old_payload = self.create_v5_snapshot_database(root)
        with closing(sqlite3.connect(root / "bridge.sqlite3")) as db:
            db.execute("ALTER TABLE snapshots ADD COLUMN application_context_json TEXT")
            db.execute("INSERT INTO schema_migrations(version,applied_at_ms) VALUES(6,6)")
            db.execute("PRAGMA user_version=6")
            db.commit()
        original = migrations._v7_statements

        def fail_after_artifact_ddl():
            return tuple(original()) + ("INSERT INTO missing_artifact_migration_table VALUES(1)",)

        with mock.patch.object(migrations, "_v7_statements", side_effect=fail_after_artifact_ddl):
            with self.assertRaises(SchemaError):
                ProfileStore(root, profile_id="profile-a")
        with closing(sqlite3.connect(root / "bridge.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_files'"
            ).fetchone())
            self.assertEqual(db.execute("SELECT payload_json FROM snapshots").fetchone()[0], old_payload)

    def test_schema_v7_has_artifact_allowlist(self) -> None:
        expected = {
            "artifact_files", "artifact_names", "artifact_slot_members",
            "artifact_manifests", "artifact_descriptor_pages", "artifact_snapshot_files",
            "artifact_transfers", "artifact_transfer_files", "artifact_transfer_pages",
        }
        with self.store.connect() as db:
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'artifact_%'"
                )
            }
        self.assertEqual(tables, expected)

    def test_new_reopen_and_sqlite_pragmas(self) -> None:
        self.assertEqual(self.store.schema_version, 9)
        self.assertEqual(self.store.profile_id, "profile-a")
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 120)

        reopened = ProfileStore(self.root, profile_id="profile-a", busy_timeout_ms=120)
        try:
            self.assertEqual(reopened.profile_id, "profile-a")
            self.assertEqual(reopened.count_events(), 0)
            with reopened.connect() as db:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
            self.assertTrue(
                {
                    "profile_meta",
                    "devices",
                    "sessions",
                    "events",
                    "deliveries",
                    "applications",
                    "apply_intents",
                    "snapshots",
                    "permits",
                    "override_audit",
                    "snapshot_pages",
                    "pull_progress",
                }.issubset(tables)
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM pragma_table_info('snapshots') "
                    "WHERE name='application_context_json'"
                ).fetchone()
            )
        finally:
            reopened.close()

    def test_supported_v1_migrates_and_unknown_schema_fails_closed(self) -> None:
        self.store.close()
        v1_root = self.root.parent / "v1"
        v1_root.mkdir()
        with sqlite3.connect(v1_root / "bridge.sqlite3") as db:
            for statement in _v1_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version,applied_at_ms) VALUES(1,1)"
            )
            db.execute("PRAGMA user_version=1")
            db.commit()
        migrated = ProfileStore(v1_root, profile_id="profile-a", busy_timeout_ms=120)
        try:
            self.assertEqual(migrated.schema_version, 9)
            with migrated.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM pragma_table_info('events') WHERE name='normalized_payload_json'"
                    ).fetchone()
                )
        finally:
            migrated.close()

        unknown_root = self.root.parent / "unknown"
        unknown_root.mkdir()
        db_path = unknown_root / "bridge.sqlite3"
        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA user_version=99")
            db.commit()
        with self.assertRaises(UnsupportedSchemaError):
            ProfileStore(unknown_root, profile_id="profile-a")

    def test_supported_v2_migrates_to_snapshot_schema_v3(self) -> None:
        self.store.close()
        v2_root = self.root.parent / "v2"
        v2_root.mkdir()
        with sqlite3.connect(v2_root / "bridge.sqlite3") as db:
            for statement in _v1_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version,applied_at_ms) VALUES(1,1)"
            )
            for statement in _v2_statements():
                db.execute(statement)
            db.execute(
                "UPDATE events SET normalized_payload_json=raw_payload_json "
                "WHERE normalized_payload_json=''"
            )
            db.execute(
                "INSERT INTO schema_migrations(version,applied_at_ms) VALUES(2,2)"
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        migrated = ProfileStore(v2_root, profile_id="profile-a")
        try:
            self.assertEqual(migrated.schema_version, 9)
            with migrated.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM pragma_table_info('snapshots') "
                        "WHERE name='scope_id'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='pull_progress'"
                    ).fetchone()
                )
        finally:
            migrated.close()

    def test_non_sqlite_database_fails_closed(self) -> None:
        self.store.close()
        bad_root = self.root.parent / "bad"
        bad_root.mkdir()
        (bad_root / "bridge.sqlite3").write_bytes(b"not a sqlite database")
        with self.assertRaises(UnsupportedDatabaseError):
            ProfileStore(bad_root, profile_id="profile-a")

        empty_root = self.root.parent / "empty"
        empty_root.mkdir()
        (empty_root / "bridge.sqlite3").touch()
        with self.assertRaises(UnsupportedDatabaseError):
            ProfileStore(empty_root, profile_id="profile-a")

    def test_supported_v3_migrates_to_compact_inbox_v4(self) -> None:
        self.store.close()
        v3_root = self.root.parent / "v3"
        v3_root.mkdir()
        with sqlite3.connect(v3_root / "bridge.sqlite3") as db:
            for statements in (_v1_statements(), _v2_statements(), _v3_statements()):
                for statement in statements:
                    db.execute(statement)
            for version in (1, 2, 3):
                db.execute("INSERT INTO schema_migrations(version,applied_at_ms) VALUES(?,?)",
                           (version, version))
            db.execute("PRAGMA user_version=3")
            db.commit()
        migrated = ProfileStore(v3_root, profile_id="profile-a")
        try:
            self.assertEqual(migrated.schema_version, 9)
            with migrated.connect() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertIsNotNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compact_events'"
                ).fetchone())
        finally:
            migrated.close()

    def test_unknown_schema_table_fails_closed(self) -> None:
        self.store.close()
        unknown_root = self.root.parent / "unknown-table"
        unknown_root.mkdir()
        with sqlite3.connect(unknown_root / "bridge.sqlite3") as db:
            db.execute("CREATE TABLE foreign_data(value TEXT)")
            db.execute("PRAGMA user_version=2")
            db.commit()
        with self.assertRaises(SchemaError):
            ProfileStore(unknown_root, profile_id="profile-a")

    def test_receive_is_idempotent_and_conflict_preserves_immutable_event(self) -> None:
        first = self.store.receive_batch(
            "device-a", "session-a", [make_event("session-a", 1)]
        )
        duplicate = self.store.receive_batch(
            "device-a", "session-a", [make_event("session-a", 1)]
        )
        self.assertEqual(first[0].status, "received")
        self.assertEqual(duplicate[0].status, "received")
        self.assertEqual(self.store.count_events(), 1)
        original = self.store.get_event("device-a", "session-a", 1)

        conflict = self.store.receive_batch(
            "device-a", "session-a", [make_event("session-a", 1, rating=4)]
        )
        self.assertEqual(conflict[0].status, "conflict")
        self.assertEqual(self.store.count_events(), 1)
        self.assertEqual(
            self.store.get_event("device-a", "session-a", 1).payload_hash,
            original.payload_hash,
        )
        self.assertEqual(len(self.store.list_conflicts()), 1)

    def test_batch_validation_rolls_back_all_rows(self) -> None:
        with self.assertRaises(SequenceError):
            self.store.receive_batch(
                "device-a",
                "session-a",
                [make_event("session-a", 1), make_event("session-a", 3)],
            )
        self.assertEqual(self.store.count_events(), 0)
        self.store.receive_batch("device-a", "session-a", [make_event("session-a", 1)])
        self.assertEqual(self.store.count_events(), 1)

    def test_commit_failure_does_not_return_receipt(self) -> None:
        original_commit = self.store._commit

        def fail_commit(_db: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("simulated commit failure")

        self.store._commit = fail_commit
        try:
            with self.assertRaises(CommitUnknownError):
                self.store.receive_batch(
                    "device-a", "session-a", [make_event("session-a", 1)]
                )
        finally:
            self.store._commit = original_commit
        self.assertEqual(self.store.count_events(), 0)

    def test_ack_loss_is_recoverable_by_receipt_query_and_status_regression_is_rejected(
        self,
    ) -> None:
        self.store.receive_batch("device-a", "session-a", [make_event("session-a", 1)])
        queried = self.store.query_receipts("device-a", "session-a")
        self.assertEqual([receipt.event_id for receipt in queried], ["session-a:1"])
        self.store.transition_event("device-a", "session-a", 1, "pending_apply")
        self.store.transition_event("device-a", "session-a", 1, "applied")
        with self.assertRaises(ReceiptRegressionError):
            self.store.transition_event("device-a", "session-a", 1, "received")

    def test_profile_device_and_session_isolation(self) -> None:
        self.store.receive_batch("device-a", "session-a", [make_event("session-a", 1)])
        self.store.register_device("device-b", "device-token-b")
        self.store.open_session("device-b", "session-b")
        with self.assertRaises(StorageValidationError):
            self.store.receive_batch(
                "device-b", "session-b", [make_event("session-a", 1)]
            )
        other_root = self.root.parent / "profile-b"
        other = ProfileStore(other_root, profile_id="profile-b")
        try:
            other.register_device("device-a", "other-token")
            other.open_session("device-a", "session-a")
            other.receive_batch("device-a", "session-a", [make_event("session-a", 1)])
            self.assertEqual(other.count_events(), 1)
            self.assertEqual(self.store.count_events(), 1)
        finally:
            other.close()

    def test_sequence_gap_duplicate_and_safe_integer_bound(self) -> None:
        with self.assertRaises(SequenceError):
            self.store.receive_batch(
                "device-a", "session-a", [make_event("session-a", 2)]
            )
        with self.assertRaises(SequenceError):
            self.store.receive_batch(
                "device-a",
                "session-a",
                [make_event("session-a", 1), make_event("session-a", 1)],
            )
        too_large = {
            "event_id": f"session-a:{LUA_SAFE_INTEGER_MAX + 1}",
            "sequence": LUA_SAFE_INTEGER_MAX + 1,
            "payload": {"card_id": "card", "rating": 3},
        }
        with self.assertRaises(StorageValidationError):
            self.store.receive_batch("device-a", "session-a", [too_large])

    def test_event_hash_excludes_delivery_and_application_state(self) -> None:
        self.store.receive_batch("device-a", "session-a", [make_event("session-a", 1)])
        before = self.store.get_event("device-a", "session-a", 1).payload_hash
        self.store.transition_event("device-a", "session-a", 1, "pending_apply")
        self.store.transition_event("device-a", "session-a", 1, "applied")
        self.assertEqual(
            self.store.get_event("device-a", "session-a", 1).payload_hash, before
        )
        with self.assertRaises(StorageValidationError):
            self.store.receive_batch(
                "device-a",
                "session-a",
                [
                    {
                        "event_id": "session-a:2",
                        "sequence": 2,
                        "payload": {"rating": 3, "delivery_status": "received"},
                    }
                ],
            )

    def test_applying_recovers_to_unknown_and_is_not_retryable(self) -> None:
        self.store.receive_batch("device-a", "session-a", [make_event("session-a", 1)])
        intent = self.store.begin_apply("device-a", "session-a", [1])
        self.assertEqual(intent.status, "applying")
        self.store.close()
        reopened = ProfileStore(self.root, profile_id="profile-a", busy_timeout_ms=120)
        try:
            self.assertEqual(
                reopened.query_applications("device-a", "session-a")[0].status,
                "unknown",
            )
            self.assertEqual(reopened.pending_apply("device-a", "session-a"), [])
            with self.assertRaises(UnknownApplyError):
                reopened.begin_apply("device-a", "session-a", [1])
        finally:
            reopened.close()

    def test_backup_uses_sqlite_backup_and_diagnostics_are_redacted(self) -> None:
        secret = "device-token-that-must-not-leak"
        self.store.register_device("device-secret", secret)
        self.store.open_session("device-secret", "session-secret")
        self.store.receive_batch(
            "device-secret", "session-secret", [make_event("session-secret", 1)]
        )
        destination = self.root.parent / "backup.sqlite3"
        self.store.backup_to(destination)
        self.assertTrue(destination.exists())
        self.assertFalse(Path(str(destination) + "-wal").exists())
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        receipt_payload = self.store.query_receipts("device-secret", "session-secret")[
            0
        ].to_payload()
        diagnostics = json.dumps(self.store.diagnostics(), ensure_ascii=False)
        self.assertNotIn(secret, repr(self.store))
        self.assertNotIn(secret, json.dumps(receipt_payload, ensure_ascii=False))
        self.assertNotIn(secret, diagnostics)
        self.assertNotIn(secret.encode(), (self.store.db_path).read_bytes())

    def test_busy_timeout_is_bounded(self) -> None:
        lock_db = self.store.connect()
        lock_db.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with self.assertRaises(BusyStorageError):
                self.store.register_device("blocked", "blocked-token")
        finally:
            lock_db.rollback()
            lock_db.close()
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
