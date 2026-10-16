"""标准库 SQLite schema 与显式迁移。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Set

from .errors import SchemaError, UnsupportedSchemaError

SCHEMA_VERSION = 6
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6)

_V1_TABLES = {
    "profile_meta",
    "devices",
    "sessions",
    "events",
    "deliveries",
    "applications",
    "apply_intents",
    "apply_intent_events",
    "snapshots",
    "permits",
    "override_audit",
    "schema_migrations",
}
_V2_TABLES = _V1_TABLES | {"event_conflicts"}
_V3_TABLES = _V2_TABLES | {"snapshot_pages", "pull_progress"}
_V4_TABLES = _V3_TABLES | {"compact_events", "compact_conflicts"}
_V5_TABLES = _V4_TABLES | {
    "compact_applications",
    "compact_score_evidence",
    "card_apply_locks",
}


def _v1_statements() -> Iterable[str]:
    """返回第一版 schema 的有序 DDL。"""

    return (
        """
        CREATE TABLE profile_meta (
            profile_id TEXT PRIMARY KEY CHECK(length(profile_id) BETWEEN 1 AND 128),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            collection_generation INTEGER NOT NULL DEFAULT 0 CHECK(collection_generation >= 0)
        )
        """,
        """
        CREATE TABLE devices (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL CHECK(length(device_id) BETWEEN 1 AND 128),
            token_hash TEXT NOT NULL CHECK(length(token_hash) = 64),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            last_seen_at_ms INTEGER,
            revoked_at_ms INTEGER,
            PRIMARY KEY(profile_id, device_id),
            UNIQUE(profile_id, token_hash),
            FOREIGN KEY(profile_id) REFERENCES profile_meta(profile_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE sessions (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL CHECK(length(session_id) BETWEEN 1 AND 128),
            high_water INTEGER NOT NULL DEFAULT 0
                CHECK(high_water >= 0 AND high_water <= 9007199254740991),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            last_seen_at_ms INTEGER NOT NULL CHECK(last_seen_at_ms >= 0),
            closed_at_ms INTEGER,
            PRIMARY KEY(profile_id, device_id, session_id),
            FOREIGN KEY(profile_id, device_id)
                REFERENCES devices(profile_id, device_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE events (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1 AND sequence <= 9007199254740991),
            event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 256),
            payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
            raw_payload_json TEXT NOT NULL,
            received_at_ms INTEGER NOT NULL CHECK(received_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= received_at_ms),
            PRIMARY KEY(profile_id, device_id, session_id, sequence),
            UNIQUE(profile_id, device_id, session_id, event_id),
            FOREIGN KEY(profile_id, device_id, session_id)
                REFERENCES sessions(profile_id, device_id, session_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE deliveries (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('received','pending_apply','applying','applied','unknown','conflict','abandoned')),
            reason TEXT,
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
            PRIMARY KEY(profile_id, device_id, session_id, sequence),
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES events(profile_id, device_id, session_id, sequence) ON DELETE CASCADE,
            UNIQUE(profile_id, device_id, session_id, event_id)
        )
        """,
        """
        CREATE TABLE applications (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('pending_apply','applying','applied','unknown','conflict','abandoned')),
            intent_id TEXT,
            attempt_id TEXT,
            reason TEXT,
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
            PRIMARY KEY(profile_id, device_id, session_id, sequence),
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES events(profile_id, device_id, session_id, sequence) ON DELETE CASCADE,
            UNIQUE(profile_id, device_id, session_id, event_id)
        )
        """,
        """
        CREATE TABLE apply_intents (
            profile_id TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('applying','applied','unknown','abandoned')),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            reason TEXT,
            PRIMARY KEY(profile_id, intent_id),
            UNIQUE(profile_id, attempt_id),
            FOREIGN KEY(profile_id) REFERENCES profile_meta(profile_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE apply_intent_events (
            profile_id TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY(profile_id, intent_id, device_id, session_id, sequence),
            FOREIGN KEY(profile_id, intent_id)
                REFERENCES apply_intents(profile_id, intent_id) ON DELETE CASCADE,
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES events(profile_id, device_id, session_id, sequence) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE snapshots (
            profile_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            scope_generation INTEGER NOT NULL CHECK(scope_generation >= 0),
            applied_watermark INTEGER,
            payload_json TEXT NOT NULL,
            checksum TEXT NOT NULL CHECK(length(checksum) = 64),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            PRIMARY KEY(profile_id, snapshot_id),
            FOREIGN KEY(profile_id) REFERENCES profile_meta(profile_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE permits (
            profile_id TEXT NOT NULL,
            permit_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            nonce_hash TEXT NOT NULL CHECK(length(nonce_hash) = 64),
            status TEXT NOT NULL CHECK(status IN ('issued','used','expired','revoked')),
            expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            used_at_ms INTEGER,
            PRIMARY KEY(profile_id, permit_id),
            FOREIGN KEY(profile_id, snapshot_id)
                REFERENCES snapshots(profile_id, snapshot_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE override_audit (
            profile_id TEXT NOT NULL,
            audit_id TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            context_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            PRIMARY KEY(profile_id, audit_id),
            FOREIGN KEY(profile_id) REFERENCES profile_meta(profile_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at_ms INTEGER NOT NULL CHECK(applied_at_ms >= 0)
        )
        """,
        "CREATE INDEX deliveries_status_idx ON deliveries(profile_id, status)",
        "CREATE INDEX applications_status_idx ON applications(profile_id, status)",
        "CREATE INDEX events_session_idx ON events(profile_id, device_id, session_id, sequence)",
    )


def _v2_statements() -> Iterable[str]:
    """返回第二版 schema 的增量 DDL。"""

    return (
        "ALTER TABLE events ADD COLUMN normalized_payload_json TEXT NOT NULL DEFAULT ''",
        """
        CREATE TABLE event_conflicts (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            observed_payload_hash TEXT NOT NULL CHECK(length(observed_payload_hash) = 64),
            observed_payload_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            PRIMARY KEY(profile_id, device_id, session_id, sequence, observed_payload_hash),
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES events(profile_id, device_id, session_id, sequence) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX event_conflicts_lookup_idx ON event_conflicts(profile_id, device_id, session_id, sequence)",
    )


def _v3_statements() -> Iterable[str]:
    """返回第三版 snapshot/permit/progress 增量 DDL。"""

    return (
        "ALTER TABLE snapshots ADD COLUMN scope_id TEXT NOT NULL DEFAULT '__legacy__'",
        "ALTER TABLE snapshots ADD COLUMN snapshot_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE snapshots ADD COLUMN card_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN included_card_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN payload_bytes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN page_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN peak_bytes_estimate INTEGER NOT NULL DEFAULT 0",
        "UPDATE snapshots SET snapshot_generation=scope_generation WHERE snapshot_generation=0",
        "ALTER TABLE permits ADD COLUMN device_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE permits ADD COLUMN session_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE permits ADD COLUMN scope_id TEXT NOT NULL DEFAULT '__legacy__'",
        "ALTER TABLE permits ADD COLUMN local_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE permits ADD COLUMN snapshot_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE permits ADD COLUMN applied_watermark INTEGER NOT NULL DEFAULT 0",
        "UPDATE permits SET scope_id=(SELECT scope_id FROM snapshots WHERE snapshots.profile_id=permits.profile_id AND snapshots.snapshot_id=permits.snapshot_id),"
        " snapshot_generation=(SELECT snapshot_generation FROM snapshots WHERE snapshots.profile_id=permits.profile_id AND snapshots.snapshot_id=permits.snapshot_id)"
        " WHERE EXISTS (SELECT 1 FROM snapshots WHERE snapshots.profile_id=permits.profile_id AND snapshots.snapshot_id=permits.snapshot_id)",
        """
        CREATE TABLE snapshot_pages (
            profile_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            page_index INTEGER NOT NULL CHECK(page_index >= 0 AND page_index <= 9007199254740991),
            page_json TEXT NOT NULL,
            page_hash TEXT NOT NULL CHECK(length(page_hash) = 64),
            page_bytes INTEGER NOT NULL CHECK(page_bytes > 0),
            cursor TEXT,
            is_last INTEGER NOT NULL CHECK(is_last IN (0,1)),
            PRIMARY KEY(profile_id, snapshot_id, page_index),
            FOREIGN KEY(profile_id, snapshot_id)
                REFERENCES snapshots(profile_id, snapshot_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE pull_progress (
            profile_id TEXT NOT NULL,
            permit_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            local_generation INTEGER NOT NULL CHECK(local_generation >= 0 AND local_generation <= 9007199254740991),
            snapshot_id TEXT NOT NULL,
            snapshot_generation INTEGER NOT NULL CHECK(snapshot_generation >= 0 AND snapshot_generation <= 9007199254740991),
            applied_watermark INTEGER NOT NULL CHECK(applied_watermark >= 0 AND applied_watermark <= 9007199254740991),
            next_page_index INTEGER NOT NULL CHECK(next_page_index >= 0 AND next_page_index <= 9007199254740991),
            received_pages INTEGER NOT NULL CHECK(received_pages >= 0 AND received_pages <= 9007199254740991),
            received_bytes INTEGER NOT NULL CHECK(received_bytes >= 0 AND received_bytes <= 9007199254740991),
            complete INTEGER NOT NULL CHECK(complete IN (0,1)),
            last_page_index INTEGER NOT NULL DEFAULT -1 CHECK(last_page_index >= -1 AND last_page_index <= 9007199254740991),
            last_cursor TEXT,
            last_page_json TEXT,
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            PRIMARY KEY(profile_id, permit_id),
            FOREIGN KEY(profile_id, permit_id)
                REFERENCES permits(profile_id, permit_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX snapshots_scope_idx ON snapshots(profile_id, scope_id, snapshot_generation, created_at_ms)",
    )


def _v4_statements() -> Iterable[str]:
    """隔离 x4-3 短日志；不接入旧 Anki application 表。"""

    return (
        """
        CREATE TABLE compact_events (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1 AND sequence <= 9007199254740991),
            event_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            card_index INTEGER NOT NULL CHECK(card_index >= 1),
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 4),
            answered_at_ms INTEGER NOT NULL CHECK(answered_at_ms >= 0),
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
            received_at_ms INTEGER NOT NULL CHECK(received_at_ms >= 0),
            PRIMARY KEY(profile_id, device_id, session_id, sequence),
            UNIQUE(profile_id, device_id, session_id, event_id),
            FOREIGN KEY(profile_id, device_id, session_id)
                REFERENCES sessions(profile_id, device_id, session_id) ON DELETE CASCADE,
            FOREIGN KEY(profile_id, snapshot_id)
                REFERENCES snapshots(profile_id, snapshot_id)
        )
        """,
        """
        CREATE TABLE compact_conflicts (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            observed_payload_hash TEXT NOT NULL CHECK(length(observed_payload_hash) = 64),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            PRIMARY KEY(profile_id, device_id, session_id, sequence, observed_payload_hash),
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES compact_events(profile_id, device_id, session_id, sequence) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX compact_events_lookup_idx ON compact_events(profile_id, device_id, session_id, sequence)",
    )


def _v5_statements() -> Iterable[str]:
    """为 x4 compact 行增加独立应用状态、评分证据和共享卡锁。"""

    return (
        """
        CREATE TABLE compact_applications (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
            status TEXT NOT NULL CHECK(status IN
                ('not_applied','applying','applied','unknown','conflict')),
            recovery_required INTEGER NOT NULL DEFAULT 0 CHECK(recovery_required IN (0,1)),
            collection_identity TEXT,
            collection_generation INTEGER,
            collection_generation_after INTEGER,
            policy_json TEXT,
            before_json TEXT,
            before_hash TEXT,
            plan_json TEXT,
            expected_after_json TEXT,
            after_json TEXT,
            after_hash TEXT,
            intent_id TEXT,
            attempt_id TEXT,
            reason TEXT,
            receipt_json TEXT,
            receipt_hash TEXT,
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
            PRIMARY KEY(profile_id, device_id, session_id, sequence),
            UNIQUE(profile_id, device_id, session_id, event_id),
            UNIQUE(profile_id, intent_id),
            UNIQUE(profile_id, attempt_id),
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES compact_events(profile_id, device_id, session_id, sequence) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE compact_score_evidence (
            profile_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(length(stage) BETWEEN 1 AND 64),
            evidence_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL CHECK(length(evidence_hash) = 64),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
            PRIMARY KEY(profile_id, device_id, session_id, sequence, stage),
            FOREIGN KEY(profile_id, device_id, session_id, sequence)
                REFERENCES compact_applications(profile_id, device_id, session_id, sequence) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE card_apply_locks (
            profile_id TEXT NOT NULL,
            card_id TEXT NOT NULL CHECK(length(card_id) BETWEEN 1 AND 128),
            source_kind TEXT NOT NULL CHECK(source_kind IN ('compact','ordinary')),
            device_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            intent_id TEXT NOT NULL,
            acquired_at_ms INTEGER NOT NULL CHECK(acquired_at_ms >= 0),
            PRIMARY KEY(profile_id, card_id),
            FOREIGN KEY(profile_id) REFERENCES profile_meta(profile_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX compact_applications_status_idx ON compact_applications(profile_id,status)",
        "CREATE INDEX compact_applications_card_idx ON compact_applications(profile_id,status,collection_identity,collection_generation)",
    )


def _v6_statements() -> Iterable[str]:
    """在 snapshot 同一行旁路保存不可变评分上下文。"""

    return ("ALTER TABLE snapshots ADD COLUMN application_context_json TEXT",)


def _table_names(db: sqlite3.Connection) -> Set[str]:
    """读取 SQLite 用户表名。"""

    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(db: sqlite3.Connection, table: str) -> Set[str]:
    """读取指定表的列名。"""

    return {row[1] for row in db.execute('PRAGMA table_info("%s")' % table)}


def validate_schema(db: sqlite3.Connection, version: int) -> None:
    """验证 schema 只包含当前实现明确认识的表和关键列。"""

    if version == 1:
        expected_tables = _V1_TABLES
    elif version == 2:
        expected_tables = _V2_TABLES
    elif version == 3:
        expected_tables = _V3_TABLES
    elif version == 4:
        expected_tables = _V4_TABLES
    else:
        expected_tables = _V5_TABLES
    actual_tables = _table_names(db)
    if actual_tables != expected_tables:
        raise SchemaError(
            "database table allowlist mismatch", "schema_table_allowlist_mismatch"
        )
    expected_columns = {
        "profile_meta": {
            "profile_id",
            "created_at_ms",
            "updated_at_ms",
            "collection_generation",
        },
        "devices": {
            "profile_id",
            "device_id",
            "token_hash",
            "created_at_ms",
            "last_seen_at_ms",
            "revoked_at_ms",
        },
        "sessions": {
            "profile_id",
            "device_id",
            "session_id",
            "high_water",
            "created_at_ms",
            "last_seen_at_ms",
            "closed_at_ms",
        },
        "events": {
            "profile_id",
            "device_id",
            "session_id",
            "sequence",
            "event_id",
            "payload_hash",
            "raw_payload_json",
            "received_at_ms",
            "updated_at_ms",
        },
        "deliveries": {
            "profile_id",
            "device_id",
            "session_id",
            "sequence",
            "event_id",
            "status",
            "reason",
            "updated_at_ms",
        },
        "applications": {
            "profile_id",
            "device_id",
            "session_id",
            "sequence",
            "event_id",
            "status",
            "intent_id",
            "attempt_id",
            "reason",
            "updated_at_ms",
        },
        "apply_intents": {
            "profile_id",
            "intent_id",
            "attempt_id",
            "status",
            "created_at_ms",
            "updated_at_ms",
            "reason",
        },
        "apply_intent_events": {
            "profile_id",
            "intent_id",
            "device_id",
            "session_id",
            "sequence",
            "event_id",
        },
        "snapshots": {
            "profile_id",
            "snapshot_id",
            "scope_generation",
            "applied_watermark",
            "payload_json",
            "checksum",
            "created_at_ms",
        },
        "permits": {
            "profile_id",
            "permit_id",
            "snapshot_id",
            "nonce_hash",
            "status",
            "expires_at_ms",
            "created_at_ms",
            "used_at_ms",
        },
        "override_audit": {
            "profile_id",
            "audit_id",
            "action",
            "reason",
            "context_json",
            "created_at_ms",
        },
        "schema_migrations": {"version", "applied_at_ms"},
    }
    if version == 2:
        expected_columns["events"].add("normalized_payload_json")
        expected_columns["event_conflicts"] = {
            "profile_id",
            "device_id",
            "session_id",
            "sequence",
            "event_id",
            "observed_payload_hash",
            "observed_payload_json",
            "reason",
            "created_at_ms",
        }
    if version >= 3:
        expected_columns["events"].add("normalized_payload_json")
        expected_columns["event_conflicts"] = {
            "profile_id",
            "device_id",
            "session_id",
            "sequence",
            "event_id",
            "observed_payload_hash",
            "observed_payload_json",
            "reason",
            "created_at_ms",
        }
        expected_columns["snapshots"].update(
            {
                "scope_id",
                "snapshot_generation",
                "manifest_json",
                "card_count",
                "included_card_count",
                "skipped_count",
                "payload_bytes",
                "page_count",
                "peak_bytes_estimate",
            }
        )
        expected_columns["permits"].update(
            {
                "device_id",
                "session_id",
                "scope_id",
                "local_generation",
                "snapshot_generation",
                "applied_watermark",
            }
        )
        expected_columns["snapshot_pages"] = {
            "profile_id",
            "snapshot_id",
            "page_index",
            "page_json",
            "page_hash",
            "page_bytes",
            "cursor",
            "is_last",
        }
        expected_columns["pull_progress"] = {
            "profile_id",
            "permit_id",
            "device_id",
            "session_id",
            "scope_id",
            "local_generation",
            "snapshot_id",
            "snapshot_generation",
            "applied_watermark",
            "next_page_index",
            "received_pages",
            "received_bytes",
            "complete",
            "last_page_index",
            "last_cursor",
            "last_page_json",
            "created_at_ms",
            "updated_at_ms",
        }
    if version >= 4:
        expected_columns["compact_events"] = {
            "profile_id", "device_id", "session_id", "sequence", "event_id",
            "snapshot_id", "card_id", "card_index", "rating", "answered_at_ms",
            "duration_ms", "payload_hash", "received_at_ms",
        }
        expected_columns["compact_conflicts"] = {
            "profile_id", "device_id", "session_id", "sequence",
            "observed_payload_hash", "created_at_ms",
        }
    if version >= 5:
        expected_columns["compact_applications"] = {
            "profile_id", "device_id", "session_id", "sequence", "event_id",
            "payload_hash", "status", "recovery_required", "collection_identity", "collection_generation",
            "collection_generation_after",
            "policy_json", "before_json", "before_hash", "plan_json",
            "expected_after_json", "after_json", "after_hash", "intent_id",
            "attempt_id", "reason", "receipt_json", "receipt_hash",
            "created_at_ms", "updated_at_ms",
        }
        expected_columns["compact_score_evidence"] = {
            "profile_id", "device_id", "session_id", "sequence", "stage",
            "evidence_json", "evidence_hash", "created_at_ms",
        }
        expected_columns["card_apply_locks"] = {
            "profile_id", "card_id", "source_kind", "device_id", "session_id",
            "sequence", "intent_id", "acquired_at_ms",
        }
    if version >= 6:
        expected_columns["snapshots"].add("application_context_json")
    for table, columns in expected_columns.items():
        if _columns(db, table) != columns:
            raise SchemaError(
                "table columns do not match allowlist", "schema_columns_mismatch"
            )


def apply_migrations(db: sqlite3.Connection, now_ms: int) -> None:
    """在一个有界事务中创建或升级受支持的 SQLite schema。"""

    row = db.execute("PRAGMA user_version").fetchone()
    version = int(row[0])
    if version not in (0,) + SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaError(
            "unsupported database schema version", "unsupported_schema_version"
        )

    if version == 0:
        if _table_names(db):
            raise UnsupportedSchemaError(
                "unversioned non-empty database", "unversioned_database"
            )
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _v1_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(?, ?)",
                (1, now_ms),
            )
            db.execute("PRAGMA user_version=1")
            db.commit()
        except Exception:
            db.rollback()
            raise
        version = 1
    else:
        validate_schema(db, version)

    if version == 1:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _v2_statements():
                db.execute(statement)
            db.execute(
                "UPDATE events SET normalized_payload_json=raw_payload_json "
                "WHERE normalized_payload_json=''"
            )
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(?, ?)",
                (2, now_ms),
            )
            db.execute("PRAGMA user_version=2")
            db.commit()
        except Exception:
            db.rollback()
            raise
        version = 2

    if version == 2:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _v3_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(?, ?)",
                (3, now_ms),
            )
            db.execute("PRAGMA user_version=3")
            db.commit()
        except Exception:
            db.rollback()
            raise
        version = 3

    if version == 3:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _v4_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(?, ?)",
                (4, now_ms),
            )
            db.execute("PRAGMA user_version=4")
            db.commit()
        except Exception:
            db.rollback()
            raise
        version = 4

    if version == 4:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _v5_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO compact_applications(profile_id,device_id,session_id,sequence,event_id,"
                "payload_hash,status,created_at_ms,updated_at_ms) "
                "SELECT profile_id,device_id,session_id,sequence,event_id,payload_hash,'not_applied',"
                "received_at_ms,received_at_ms FROM compact_events"
            )
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(?, ?)",
                (5, now_ms),
            )
            db.execute("PRAGMA user_version=5")
            db.commit()
        except Exception:
            db.rollback()
            raise
        version = 5

    if version == 5:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _v6_statements():
                db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at_ms) VALUES(?, ?)",
                (6, now_ms),
            )
            db.execute("PRAGMA user_version=6")
            db.commit()
        except Exception:
            db.rollback()
            raise
        version = 6

    validate_schema(db, version)
