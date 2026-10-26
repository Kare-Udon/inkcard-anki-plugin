"""InkCards 插件自有 SQLite 存储。

本模块只保存设备交付、应用证据和恢复状态，不读取 Anki collection，也不执行
Anki 写操作。每个 profile 使用一个独立 root；所有可见 ACK 都在事务提交后生成。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from ..domain.sync_gate import LUA_SAFE_INTEGER_MAX, ImmutableReviewEvent
from .errors import (
    BusyStorageError,
    CommitUnknownError,
    ProfileMismatchError,
    ReceiptRegressionError,
    SchemaError,
    SequenceError,
    StorageError,
    StorageValidationError,
    UnknownApplyError,
    UnsupportedDatabaseError,
)
from .migrations import SCHEMA_VERSION, apply_migrations
from .device_management import DeviceManagementStore
from .models import (
    ApplicationRecord,
    ApplyIntent,
    ConflictRecord,
    CompactApplicationRecord,
    CompactEventRecord,
    CompactScoreEvidenceRecord,
    DeviceRecord,
    PermitRecord,
    PullGate,
    PullProgressRecord,
    Receipt,
    SessionRecord,
    SnapshotRecord,
    StoredEvent,
)

_DELIVERY_STATUSES = frozenset(
    {
        "received",
        "pending_apply",
        "applying",
        "applied",
        "unknown",
        "conflict",
        "abandoned",
    }
)
_APPLICATION_STATUSES = frozenset(
    {"pending_apply", "applying", "applied", "unknown", "conflict", "abandoned"}
)
_COMPACT_APPLICATION_STATUSES = frozenset(
    {"not_applied", "applying", "applied", "unknown", "conflict"}
)
_ALLOWED_TRANSITIONS = {
    "received": frozenset({"pending_apply", "conflict", "abandoned"}),
    "pending_apply": frozenset({"applying", "applied", "conflict", "abandoned"}),
    "applying": frozenset({"applied", "unknown", "conflict"}),
    "applied": frozenset(),
    "unknown": frozenset(),
    "conflict": frozenset(),
    "abandoned": frozenset(),
}


@dataclass(frozen=True)
class _PreparedEvent:
    """事务开始前已经通过 domain 和 wire 形状校验的事件。"""

    event_id: str
    sequence: int
    payload_hash: str
    payload: Dict[str, Any]
    raw_payload_json: str
    normalized_payload_json: str


def _now_ms() -> int:
    """返回当前 Unix 毫秒；该函数不参与事件 hash。"""

    return int(time.time() * 1000)


def _translate_sqlite_error(error: sqlite3.Error) -> StorageError:
    """把 SQLite 的有界锁错误转换成稳定的 storage 错误。"""

    message = str(error)
    lowered = message.lower()
    if "locked" in lowered or "busy" in lowered:
        return BusyStorageError("sqlite busy timeout exceeded", "sqlite_busy_timeout")
    return StorageError("sqlite operation failed", "sqlite_error")


def _validate_text(value: Any, name: str, maximum: int) -> str:
    """校验标识符文本，不把输入值写入错误消息。"""

    if not isinstance(value, str) or not value or len(value) > maximum:
        raise StorageValidationError("invalid identifier", "invalid_identifier")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise StorageValidationError(
            "identifier contains a control character", "invalid_identifier"
        )
    return value


def _validate_sequence(value: Any) -> int:
    """校验 Lua/JSON 安全整数范围内的正序号。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageValidationError("sequence must be an integer", "invalid_sequence")
    if not 1 <= value <= LUA_SAFE_INTEGER_MAX:
        raise StorageValidationError(
            "sequence is outside the safe integer range", "invalid_sequence"
        )
    return value


def _validate_nonnegative_safe(value: Any, name: str) -> int:
    """校验协议中允许 0 的安全整数。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageValidationError("%s must be an integer" % name, "invalid_integer")
    if not 0 <= value <= LUA_SAFE_INTEGER_MAX:
        raise StorageValidationError(
            "%s is outside the safe integer range" % name, "invalid_integer"
        )
    return value


def _hash_secret(secret: Union[str, bytes, bytearray]) -> str:
    """只计算设备 secret 摘要，绝不返回或持久化原文。"""

    if isinstance(secret, str):
        raw = secret.encode("utf-8")
    elif isinstance(secret, (bytes, bytearray)):
        raw = bytes(secret)
    else:
        raise StorageValidationError("secret must be text or bytes", "invalid_secret")
    if not raw:
        raise StorageValidationError("secret must not be empty", "invalid_secret")
    return hashlib.sha256(raw).hexdigest()


def _canonical_event_json(
    event_id: str, sequence: int, payload: Mapping[str, Any]
) -> str:
    """生成只覆盖不可变事件内容的规范 JSON。"""

    try:
        return json.dumps(
            {"event_id": event_id, "sequence": sequence, "payload": dict(payload)},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise StorageValidationError(
            "event payload is not strict JSON", "invalid_event_payload"
        ) from None


def _canonical_object_json(value: Any, name: str) -> str:
    """校验并规范化要求为 JSON 对象的存储证据。"""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise StorageValidationError("invalid JSON evidence", "compact_evidence_invalid") from None
    if not isinstance(value, Mapping):
        raise StorageValidationError("evidence must be a JSON object", "compact_evidence_invalid")
    try:
        return json.dumps(
            dict(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raise StorageValidationError("invalid JSON evidence", "compact_evidence_invalid") from None


def _json_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_compact_stages(plan_json: str) -> Tuple[str, ...]:
    """从已规范化计划读取必须先持久化的评分阶段。"""

    try:
        plan = json.loads(plan_json)
        stages = plan.get("required_evidence", ["score_applied"])
    except (TypeError, ValueError, AttributeError):
        raise StorageValidationError("compact plan is invalid", "compact_plan_invalid") from None
    allowed = {"score_applied", "due_corrected", "score_verified"}
    if (
        not isinstance(stages, list)
        or not stages
        or any(not isinstance(stage, str) or stage not in allowed for stage in stages)
        or len(set(stages)) != len(stages)
    ):
        raise StorageValidationError("compact plan evidence stages are invalid", "compact_plan_invalid")
    return tuple(stages)


def _event_input(
    event: Any,
) -> Tuple[str, int, Mapping[str, Any], Dict[str, Any], Optional[str]]:
    """提取 domain 事件或严格事件对象的字段。"""

    if isinstance(event, ImmutableReviewEvent):
        try:
            event.verify_hash()
        except Exception:
            raise StorageValidationError(
                "event hash does not match its content", "event_hash_mismatch"
            ) from None
        raw = {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "payload": dict(event.payload),
        }
        return event.event_id, event.sequence, event.payload, raw, event.payload_hash
    if not isinstance(event, Mapping):
        raise StorageValidationError(
            "event must be an immutable domain event or object", "invalid_event"
        )
    allowed = {"event_id", "sequence", "payload", "payload_hash"}
    if set(event) - allowed:
        raise StorageValidationError("event contains an unknown field", "invalid_event")
    if "event_id" not in event or "sequence" not in event or "payload" not in event:
        raise StorageValidationError(
            "event is missing a required field", "invalid_event"
        )
    raw = dict(event)
    if "payload_hash" in raw and not isinstance(raw["payload_hash"], str):
        raise StorageValidationError("event hash must be text", "event_hash_mismatch")
    supplied_hash = raw.get("payload_hash")
    return (
        event["event_id"],
        event["sequence"],
        event["payload"],
        raw,
        supplied_hash if isinstance(supplied_hash, str) else None,
    )


def _prepare_event(event: Any, session_id: str) -> _PreparedEvent:
    """把输入变成不可变事件行，并排除交付/应用字段的影响。"""

    try:
        event_id, sequence_value, payload, raw, supplied_hash = _event_input(event)
        event_id = _validate_text(event_id, "event_id", 256)
        sequence = _validate_sequence(sequence_value)
        if not isinstance(payload, Mapping):
            raise StorageValidationError(
                "event payload must be an object", "invalid_event_payload"
            )
        if event_id != "%s:%s" % (session_id, sequence):
            raise StorageValidationError(
                "event id does not belong to this session", "event_session_mismatch"
            )
        immutable = ImmutableReviewEvent.create(event_id, sequence, dict(payload))
        normalized = _canonical_event_json(event_id, sequence, immutable.payload)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if supplied_hash is not None and supplied_hash != digest:
            raise StorageValidationError(
                "event hash does not match its content", "event_hash_mismatch"
            )
        raw_json = json.dumps(
            raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except StorageError:
        raise
    except (TypeError, ValueError) as error:
        raise StorageValidationError(
            "event is not valid strict JSON", "invalid_event"
        ) from error
    except Exception as error:
        raise StorageValidationError(
            "event failed domain validation", "invalid_event"
        ) from error
    return _PreparedEvent(
        event_id=event_id,
        sequence=sequence,
        payload_hash=digest,
        payload=dict(immutable.payload),
        raw_payload_json=raw_json,
        normalized_payload_json=normalized,
    )


class ProfileStore(DeviceManagementStore):
    """一个 Anki profile 的独立插件 SQLite 存储。"""

    def __init__(
        self,
        root: Union[str, os.PathLike[str]],
        profile_id: Optional[str] = None,
        *,
        busy_timeout_ms: int = 500,
    ) -> None:
        """打开或创建 profile 数据库，并执行受支持的显式迁移。"""

        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise StorageValidationError(
                "busy timeout must be a positive integer", "invalid_busy_timeout"
            )
        self.root = Path(root)
        self.db_path = self.root / "bridge.sqlite3"
        self.busy_timeout_ms = busy_timeout_ms
        self._closed = False
        self._requested_profile_id = (
            _validate_text(profile_id, "profile_id", 128)
            if profile_id is not None
            else None
        )
        self._profile_id = ""
        self._initialize()

    @property
    def schema_version(self) -> int:
        """返回当前支持的 schema 版本。"""

        return SCHEMA_VERSION

    @property
    def profile_id(self) -> str:
        """返回此数据库绑定的 profile UUID/身份字符串。"""

        return self._profile_id

    def __repr__(self) -> str:
        """返回不包含 token、secret 或 payload 的安全调试表示。"""

        return "ProfileStore(root=%r, profile_id=%r, schema_version=%d)" % (
            str(self.root),
            self._profile_id,
            SCHEMA_VERSION,
        )

    def close(self) -> None:
        """关闭此 store 的后续操作入口；每个操作自己的 SQLite 连接会及时关闭。"""

        self._closed = True

    def connect(self) -> sqlite3.Connection:
        """打开一个已配置 foreign_keys、WAL 和有界 busy timeout 的连接。"""

        if self._closed:
            raise StorageError("storage is closed", "storage_closed")
        return self._connect()

    def _connect(self) -> sqlite3.Connection:
        """创建内部连接并配置持久化相关 PRAGMA。"""

        # 公共读写均经此入口；仅在 connect() 检查会漏过 _read/_transaction。
        # 已开始的事务仍自行完成，关闭后不再接受新的旧 profile 操作。
        if self._closed:
            raise StorageError("storage is closed", "storage_closed")
        try:
            db = sqlite3.connect(
                str(self.db_path),
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=False,
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=%d" % self.busy_timeout_ms)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.DatabaseError as error:
            try:
                db.close()  # type: ignore[name-defined]
            except Exception:
                pass
            raise _translate_sqlite_error(error) from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """执行一个 BEGIN IMMEDIATE 事务，并把提交异常变成无 ACK 结果。"""

        db = self._connect()
        try:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise _translate_sqlite_error(error) from error
            try:
                yield db
            except Exception as error:
                if db.in_transaction:
                    db.rollback()
                if isinstance(error, StorageError):
                    raise
                if isinstance(error, sqlite3.Error):
                    raise _translate_sqlite_error(error) from error
                raise
            try:
                self._commit(db)
            except Exception as error:
                try:
                    if db.in_transaction:
                        db.rollback()
                except Exception:
                    pass
                raise CommitUnknownError(
                    "sqlite commit result is unknown; durable receipt was not returned",
                    "commit_unknown",
                ) from error
        finally:
            db.close()

    def _commit(self, db: sqlite3.Connection) -> None:
        """提交内部事务；测试可替换此窄点以模拟提交失败。"""

        db.commit()

    def _initialize(self) -> None:
        """执行文件格式检查、迁移、profile 绑定和 applying 恢复。"""

        if self.root.exists() and not self.root.is_dir():
            raise StorageError(
                "profile root is not a directory", "invalid_profile_root"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            try:
                with self.db_path.open("rb") as handle:
                    header = handle.read(16)
            except OSError as error:
                raise UnsupportedDatabaseError(
                    "database file cannot be read as SQLite", "unsupported_database_format"
                ) from error
            if header != b"SQLite format 3\x00":
                raise UnsupportedDatabaseError(
                    "database file is not SQLite", "unsupported_database_format"
                )
        elif any(
            Path(str(self.db_path) + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise UnsupportedDatabaseError(
                "SQLite sidecar exists without its database", "orphaned_database_sidecar"
            )

        db = self._connect()
        try:
            try:
                apply_migrations(db, _now_ms())
            except StorageError:
                raise
            except sqlite3.DatabaseError as error:
                raise SchemaError(
                    "database schema could not be migrated", "schema_migration_failed"
                ) from error
        finally:
            db.close()
        self._bind_profile()
        self._recover_applying()

    def _bind_profile(self) -> None:
        """读取或首次写入 profile 身份，拒绝同一 root 的身份漂移。"""

        requested = self._requested_profile_id
        with self._transaction() as db:
            row = db.execute("SELECT profile_id FROM profile_meta LIMIT 1").fetchone()
            if row is None:
                actual = (
                    requested
                    or "profile-%s"
                    % hashlib.sha256(
                        str(self.root.resolve()).encode("utf-8")
                    ).hexdigest()[:24]
                )
                now = _now_ms()
                db.execute(
                    "INSERT INTO profile_meta(profile_id,created_at_ms,updated_at_ms,collection_generation) "
                    "VALUES(?,?,?,0)",
                    (actual, now, now),
                )
            else:
                actual = str(row["profile_id"])
                if requested is not None and requested != actual:
                    raise ProfileMismatchError(
                        "database is bound to another profile", "profile_mismatch"
                    )
        self._profile_id = actual

    def _recover_applying(self) -> None:
        """普通应用转 unknown；compact 保持 applying 并要求应用层先分类恢复。"""

        now = _now_ms()
        with self._transaction() as db:
            ordinary_rows = db.execute(
                "SELECT a.device_id,a.session_id,a.sequence,a.intent_id,e.normalized_payload_json "
                "FROM applications a JOIN events e ON e.profile_id=a.profile_id "
                "AND e.device_id=a.device_id AND e.session_id=a.session_id AND e.sequence=a.sequence "
                "WHERE a.profile_id=? AND a.status='applying'",
                (self._profile_id,),
            ).fetchall()
            for row in ordinary_rows:
                try:
                    card_id = json.loads(str(row["normalized_payload_json"]))["payload"].get("card_id")
                except (TypeError, ValueError, KeyError, AttributeError):
                    card_id = None
                if isinstance(card_id, str) and card_id:
                    db.execute(
                        "INSERT OR IGNORE INTO card_apply_locks(profile_id,card_id,source_kind,device_id,"
                        "session_id,sequence,intent_id,acquired_at_ms) VALUES(?,?,'ordinary',?,?,?,?,?)",
                        (self._profile_id, card_id, row["device_id"], row["session_id"],
                         row["sequence"], row["intent_id"] or "recovered-ordinary", now),
                    )
            compact_rows = db.execute(
                "SELECT a.device_id,a.session_id,a.sequence,a.intent_id,e.card_id "
                "FROM compact_applications a JOIN compact_events e ON e.profile_id=a.profile_id "
                "AND e.device_id=a.device_id AND e.session_id=a.session_id AND e.sequence=a.sequence "
                "WHERE a.profile_id=? AND a.status='applying'",
                (self._profile_id,),
            ).fetchall()
            for row in compact_rows:
                db.execute(
                    "INSERT OR IGNORE INTO card_apply_locks(profile_id,card_id,source_kind,device_id,"
                    "session_id,sequence,intent_id,acquired_at_ms) VALUES(?,?,'compact',?,?,?,?,?)",
                    (self._profile_id, row["card_id"], row["device_id"], row["session_id"],
                     row["sequence"], row["intent_id"] or "recovered-compact", now),
                )
            db.execute(
                "UPDATE applications SET status='unknown', reason='recovered_after_restart', "
                "updated_at_ms=? WHERE profile_id=? AND status='applying'",
                (now, self._profile_id),
            )
            db.execute(
                "UPDATE compact_applications SET recovery_required=1,reason='recovery_classification_required', "
                "updated_at_ms=? WHERE profile_id=? AND status='applying'",
                (now, self._profile_id),
            )
            db.execute(
                "UPDATE deliveries SET status='unknown', reason='recovered_after_restart', "
                "updated_at_ms=? WHERE profile_id=? AND status='applying'",
                (now, self._profile_id),
            )
            db.execute(
                "UPDATE apply_intents SET status='unknown', reason='recovered_after_restart', "
                "updated_at_ms=? WHERE profile_id=? AND status='applying'",
                (now, self._profile_id),
            )

    def _read(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        """在只读连接中运行一个查询并保证连接关闭。"""

        db = self._connect()
        try:
            try:
                return operation(db)
            except StorageError:
                raise
            except sqlite3.Error as error:
                raise _translate_sqlite_error(error) from error
        finally:
            db.close()

    def _require_session(
        self, db: sqlite3.Connection, device_id: str, session_id: str
    ) -> sqlite3.Row:
        """在当前事务中检查 device/session/profile 关系。"""

        row = db.execute(
            "SELECT s.* FROM sessions s JOIN devices d ON d.profile_id=s.profile_id "
            "AND d.device_id=s.device_id WHERE s.profile_id=? AND s.device_id=? AND s.session_id=? "
            "AND d.revoked_at_ms IS NULL",
            (self._profile_id, device_id, session_id),
        ).fetchone()
        if row is None:
            raise StorageValidationError(
                "device or session is not owned by this profile", "identity_mismatch"
            )
        return row

    def get_collection_generation(self) -> int:
        """读取当前 profile 的持久 collection generation。"""

        return int(
            self._read(
                lambda db: db.execute(
                    "SELECT collection_generation FROM profile_meta WHERE profile_id=?",
                    (self._profile_id,),
                ).fetchone()[0]
            )
        )

    def set_collection_generation(
        self, generation: int, *, now_ms: Optional[int] = None
    ) -> int:
        """更新 collection generation，并使旧的 issued permit 失效。"""

        generation = _validate_nonnegative_safe(generation, "collection_generation")
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            db.execute(
                "UPDATE profile_meta SET collection_generation=?,updated_at_ms=? "
                "WHERE profile_id=?",
                (generation, now, self._profile_id),
            )
            db.execute(
                "UPDATE permits SET status='revoked' WHERE profile_id=? AND status='issued'",
                (self._profile_id,),
            )
        return generation

    def pull_gate(self, device_id: str, session_id: str) -> PullGate:
        """从当前 SQLite 查询 receipt/application，计算 fail-closed pull gate。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)

        def read(db: sqlite3.Connection) -> PullGate:
            session = self._require_session(db, device_id, session_id)
            high_water = int(session["high_water"])
            compact_conflict = db.execute(
                "SELECT 1 FROM compact_conflicts WHERE profile_id=? LIMIT 1",
                (self._profile_id,),
            ).fetchone()
            if compact_conflict is not None:
                return PullGate(False, "pull_gate_compact_conflict", "blocked", None, 0, high_water)
            compact_rows = db.execute(
                "SELECT e.device_id AS event_device_id,e.session_id AS event_session_id,e.sequence AS event_sequence,"
                "e.event_id AS event_event_id,e.payload_hash AS event_hash,a.* "
                "FROM compact_events e LEFT JOIN compact_applications a ON a.profile_id=e.profile_id "
                "AND a.device_id=e.device_id AND a.session_id=e.session_id AND a.sequence=e.sequence "
                "WHERE e.profile_id=? ORDER BY e.received_at_ms,e.device_id,e.session_id,e.sequence",
                (self._profile_id,),
            ).fetchall()
            target_compact_sequences = [
                int(row["event_sequence"]) for row in compact_rows
                if str(row["event_device_id"]) == device_id and str(row["event_session_id"]) == session_id
            ]
            if target_compact_sequences and target_compact_sequences != list(
                range(1, max(target_compact_sequences) + 1)
            ):
                return PullGate(False, "pull_gate_compact_sequence_gap", "blocked", None,
                                len(compact_rows), high_water)
            for row in compact_rows:
                status = row["status"]
                if status is None:
                    return PullGate(False, "pull_gate_compact_application_missing", "blocked", None,
                                    len(compact_rows), high_water)
                if str(row["event_hash"]) != str(row["payload_hash"]) or str(row["event_event_id"]) != str(row["event_id"]):
                    return PullGate(False, "pull_gate_compact_hash_mismatch", "blocked", None,
                                    len(compact_rows), high_water)
                if str(status) != "applied":
                    reasons = {
                        "not_applied": "pull_gate_compact_not_applied",
                        "applying": "pull_gate_compact_applying",
                        "unknown": "pull_gate_compact_unknown",
                        "conflict": "pull_gate_compact_conflict",
                    }
                    return PullGate(False, reasons.get(str(status), "pull_gate_compact_invalid"),
                                    str(status), None, len(compact_rows), high_water)
                if not self._compact_terminal_receipt_valid(
                    db, row, event_id=str(row["event_event_id"]), event_hash=str(row["event_hash"])
                ):
                    return PullGate(False, "pull_gate_compact_receipt_invalid", "blocked", None,
                                    len(compact_rows), high_water)
            conflict = db.execute(
                "SELECT 1 FROM event_conflicts WHERE profile_id=? AND device_id=? AND session_id=? LIMIT 1",
                (self._profile_id, device_id, session_id),
            ).fetchone()
            if conflict is not None:
                return PullGate(
                    False,
                    "pull_gate_conflict",
                    "blocked",
                    None,
                    0,
                    high_water,
                )
            rows = db.execute(
                "SELECT e.sequence,d.status AS delivery_status,a.status AS application_status "
                "FROM events e LEFT JOIN deliveries d ON d.profile_id=e.profile_id "
                "AND d.device_id=e.device_id AND d.session_id=e.session_id AND d.sequence=e.sequence "
                "LEFT JOIN applications a ON a.profile_id=e.profile_id AND a.device_id=e.device_id "
                "AND a.session_id=e.session_id AND a.sequence=e.sequence "
                "WHERE e.profile_id=? AND e.device_id=? AND e.session_id=? ORDER BY e.sequence",
                (self._profile_id, device_id, session_id),
            ).fetchall()
            sequences = [int(row["sequence"]) for row in rows]
            if rows and target_compact_sequences:
                return PullGate(False, "pull_gate_mixed_protocol_session", "blocked", None,
                                len(rows) + len(compact_rows), high_water)
            if not rows and high_water == 0 and not compact_rows:
                return PullGate(True, None, "not_required", 0, 0, high_water)
            contiguous = len(sequences) == high_water and all(
                sequence == index
                for index, sequence in enumerate(sequences, start=1)
            )
            if not contiguous:
                return PullGate(
                    False,
                    "pull_gate_sequence_gap",
                    "blocked",
                    None,
                    len(rows),
                    high_water,
                )
            for row in rows:
                delivery_status = row["delivery_status"]
                application_status = row["application_status"]
                if delivery_status is None or application_status is None:
                    return PullGate(
                        False,
                        "pull_gate_application_missing",
                        "blocked",
                        None,
                        len(rows),
                        high_water,
                    )
                if str(delivery_status) != "applied":
                    if str(delivery_status) in {"received", "pending_apply"}:
                        reason = "pull_gate_pending_apply"
                    elif str(delivery_status) == "applying":
                        reason = "pull_gate_applying"
                    else:
                        reason = "pull_gate_%s" % str(delivery_status)
                    return PullGate(
                        False,
                        reason,
                        str(application_status),
                        None,
                        len(rows),
                        high_water,
                    )
                if str(application_status) != "applied":
                    if str(application_status) in {"pending_apply", "applying"}:
                        reason = "pull_gate_%s" % str(application_status)
                    else:
                        reason = "pull_gate_%s" % str(application_status)
                    return PullGate(
                        False,
                        reason,
                        str(application_status),
                        None,
                        len(rows),
                        high_water,
                    )
            return PullGate(
                True,
                None,
                "applied",
                max(target_compact_sequences) if target_compact_sequences else high_water,
                len(rows) + len(compact_rows),
                high_water,
            )

        return self._read(read)

    @staticmethod
    def _snapshot_record(row: sqlite3.Row) -> SnapshotRecord:
        raw_context = (
            row["application_context_json"]
            if "application_context_json" in row.keys()
            else None
        )
        application_context = None
        if raw_context is not None:
            from ..anki.application_context import SnapshotApplicationContext

            try:
                application_context = SnapshotApplicationContext.from_json(str(raw_context))
            except ValueError as error:
                if "incomplete pre-ancestry application context" in str(error):
                    raise SchemaError(
                        "snapshot application context lacks required ancestry data; re-export snapshot",
                        "snapshot_context_incomplete",
                    ) from None
                raise SchemaError("snapshot application context is corrupt", "snapshot_context_corrupt") from None
            except (KeyError, TypeError):
                raise SchemaError("snapshot application context is corrupt", "snapshot_context_corrupt") from None
            if (
                application_context.profile_id != str(row["profile_id"])
                or application_context.snapshot_id != str(row["snapshot_id"])
                or application_context.snapshot_checksum != str(row["checksum"])
                or application_context.snapshot_generation != int(row["snapshot_generation"])
            ):
                raise SchemaError("snapshot application context binding mismatch", "snapshot_context_corrupt")
        return SnapshotRecord(
            profile_id=str(row["profile_id"]),
            snapshot_id=str(row["snapshot_id"]),
            scope_id=str(row["scope_id"]),
            scope_generation=int(row["scope_generation"]),
            snapshot_generation=int(row["snapshot_generation"]),
            applied_watermark=int(row["applied_watermark"] or 0),
            payload_json=str(row["payload_json"]),
            checksum=str(row["checksum"]),
            manifest_json=str(row["manifest_json"]),
            card_count=int(row["card_count"]),
            included_card_count=int(row["included_card_count"]),
            skipped_count=int(row["skipped_count"]),
            payload_bytes=int(row["payload_bytes"]),
            page_count=int(row["page_count"]),
            peak_bytes_estimate=int(row["peak_bytes_estimate"]),
            created_at_ms=int(row["created_at_ms"]),
            application_context=application_context,
        )

    @staticmethod
    def _permit_record(row: sqlite3.Row) -> PermitRecord:
        return PermitRecord(
            profile_id=str(row["profile_id"]),
            permit_id=str(row["permit_id"]),
            device_id=str(row["device_id"]),
            session_id=str(row["session_id"]),
            scope_id=str(row["scope_id"]),
            local_generation=int(row["local_generation"]),
            snapshot_id=str(row["snapshot_id"]),
            snapshot_generation=int(row["snapshot_generation"]),
            applied_watermark=int(row["applied_watermark"]),
            nonce_hash=str(row["nonce_hash"]),
            status=str(row["status"]),
            expires_at_ms=int(row["expires_at_ms"]),
            created_at_ms=int(row["created_at_ms"]),
            used_at_ms=(
                int(row["used_at_ms"]) if row["used_at_ms"] is not None else None
            ),
        )

    def save_snapshot(
        self,
        *,
        snapshot_id: str,
        scope_id: str,
        generation: int,
        applied_watermark: int,
        payload_json: str,
        checksum: str,
        manifest_json: str,
        card_count: int,
        included_card_count: int,
        skipped_count: int,
        payload_bytes: int,
        page_count: int,
        peak_bytes_estimate: int,
        pages: Sequence[Tuple[int, str, str, int, Optional[str], bool]],
        application_context_json: Optional[str] = None,
        now_ms: Optional[int] = None,
    ) -> SnapshotRecord:
        """在单事务中发布不可变 payload、manifest 和预编码页。"""

        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        generation = _validate_nonnegative_safe(generation, "snapshot_generation")
        applied_watermark = _validate_nonnegative_safe(
            applied_watermark, "applied_watermark"
        )
        if not isinstance(payload_json, str) or not payload_json:
            raise StorageValidationError("invalid snapshot payload", "snapshot_invalid")
        if not isinstance(manifest_json, str) or not manifest_json:
            raise StorageValidationError("invalid snapshot manifest", "snapshot_invalid")
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != checksum
        ):
            raise StorageValidationError("snapshot checksum mismatch", "snapshot_invalid")
        if application_context_json is not None:
            from ..anki.application_context import SnapshotApplicationContext

            try:
                bound_context = SnapshotApplicationContext.from_json(application_context_json)
                if bound_context.to_json() != application_context_json:
                    raise ValueError("application context must use canonical JSON")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise StorageValidationError(
                    "snapshot application context is invalid", "snapshot_context_invalid"
                ) from None
            if (
                bound_context.profile_id != self._profile_id
                or bound_context.snapshot_id != snapshot_id
                or bound_context.snapshot_checksum != checksum
                or bound_context.snapshot_generation != generation
            ):
                raise StorageValidationError(
                    "snapshot application context binding mismatch", "snapshot_context_binding_mismatch"
                )
        for name, value in (
            ("card_count", card_count),
            ("included_card_count", included_card_count),
            ("skipped_count", skipped_count),
            ("payload_bytes", payload_bytes),
            ("page_count", page_count),
            ("peak_bytes_estimate", peak_bytes_estimate),
        ):
            _validate_nonnegative_safe(value, name)
        if page_count <= 0 or len(pages) != page_count:
            raise StorageValidationError("snapshot page count mismatch", "snapshot_invalid")
        try:
            if not isinstance(json.loads(payload_json), dict):
                raise ValueError
            if not isinstance(json.loads(manifest_json), dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise StorageValidationError("snapshot JSON is invalid", "snapshot_invalid") from None
        normalized_pages: List[Tuple[int, str, str, int, Optional[str], int]] = []
        for expected_index, page in enumerate(pages):
            if not isinstance(page, (tuple, list)) or len(page) != 6:
                raise StorageValidationError("snapshot page is invalid", "snapshot_invalid")
            page_index, page_json, page_hash, page_bytes, cursor, is_last = page
            if page_index != expected_index or not isinstance(page_json, str) or not page_json:
                raise StorageValidationError("snapshot page is invalid", "snapshot_invalid")
            try:
                page_object = json.loads(page_json)
                page_content = {
                    "page_index": page_index,
                    "cards": page_object["cards"],
                }
                expected_page_hash = hashlib.sha256(
                    json.dumps(
                        page_content,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                expected_page_hash = ""
            if (
                not isinstance(page_hash, str)
                or len(page_hash) != 64
                or any(char not in "0123456789abcdef" for char in page_hash)
                or expected_page_hash != page_hash
            ):
                raise StorageValidationError("snapshot page checksum mismatch", "snapshot_invalid")
            page_bytes = _validate_nonnegative_safe(page_bytes, "page_bytes")
            if page_bytes <= 0 or page_bytes != len(page_json.encode("utf-8")):
                raise StorageValidationError("snapshot page byte mismatch", "snapshot_invalid")
            if cursor is not None:
                cursor = _validate_text(cursor, "cursor", 256)
            if not isinstance(is_last, bool):
                raise StorageValidationError("snapshot page is invalid", "snapshot_invalid")
            if bool(is_last) != (expected_index == page_count - 1):
                raise StorageValidationError("snapshot page terminal mismatch", "snapshot_invalid")
            normalized_pages.append(
                (page_index, page_json, page_hash, page_bytes, cursor, int(is_last))
            )
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                (self._profile_id, snapshot_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["payload_json"]) != payload_json
                    or str(existing["checksum"]) != checksum
                    or str(existing["scope_id"]) != scope_id
                    or int(existing["snapshot_generation"]) != generation
                ):
                    raise StorageValidationError(
                        "snapshot id already contains different content",
                        "snapshot_immutable_conflict",
                    )
                if existing["application_context_json"] != application_context_json:
                    raise StorageValidationError(
                        "snapshot id already contains different application context",
                        "snapshot_context_conflict",
                    )
                return self._snapshot_record(existing)
            # A new scope/generation snapshot invalidates every in-flight permit for
            # that scope. The immutable snapshot rows and their pages remain auditable.
            db.execute(
                "UPDATE permits SET status='revoked' WHERE profile_id=? AND scope_id=? AND status='issued'",
                (self._profile_id, scope_id),
            )
            db.execute(
                "INSERT INTO snapshots(profile_id,snapshot_id,scope_generation,applied_watermark,"
                "payload_json,checksum,created_at_ms,scope_id,snapshot_generation,manifest_json,"
                "card_count,included_card_count,skipped_count,payload_bytes,page_count,peak_bytes_estimate,"
                "application_context_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._profile_id,
                    snapshot_id,
                    generation,
                    applied_watermark,
                    payload_json,
                    checksum,
                    now,
                    scope_id,
                    generation,
                    manifest_json,
                    card_count,
                    included_card_count,
                    skipped_count,
                    payload_bytes,
                    page_count,
                    peak_bytes_estimate,
                    application_context_json,
                ),
            )
            for page_index, page_json, page_hash, page_bytes, cursor, is_last in normalized_pages:
                db.execute(
                    "INSERT INTO snapshot_pages(profile_id,snapshot_id,page_index,page_json,page_hash,"
                    "page_bytes,cursor,is_last) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self._profile_id,
                        snapshot_id,
                        page_index,
                        page_json,
                        page_hash,
                        page_bytes,
                        cursor,
                        is_last,
                    ),
                )
            row = db.execute(
                "SELECT * FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                (self._profile_id, snapshot_id),
            ).fetchone()
            if row is None:
                raise SchemaError("snapshot row disappeared after insert", "snapshot_corrupt")
            return self._snapshot_record(row)

    def get_snapshot(
        self, snapshot_id: str, *, scope_id: Optional[str] = None
    ) -> SnapshotRecord:
        """读取已提交快照摘要，不能读取活动 Anki collection。"""

        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        if scope_id is not None:
            scope_id = _validate_text(scope_id, "scope_id", 128)

        def read(db: sqlite3.Connection) -> SnapshotRecord:
            if scope_id is None:
                row = db.execute(
                    "SELECT * FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                    (self._profile_id, snapshot_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM snapshots WHERE profile_id=? AND snapshot_id=? AND scope_id=?",
                    (self._profile_id, snapshot_id, scope_id),
                ).fetchone()
            if row is None:
                raise StorageValidationError("snapshot does not exist", "snapshot_not_found")
            return self._snapshot_record(row)

        return self._read(read)

    def latest_snapshot(self, scope_id: str) -> SnapshotRecord:
        """返回 scope 最新发布的不可变快照。"""

        scope_id = _validate_text(scope_id, "scope_id", 128)

        def read(db: sqlite3.Connection) -> SnapshotRecord:
            row = db.execute(
                "SELECT * FROM snapshots WHERE profile_id=? AND scope_id=? "
                "ORDER BY snapshot_generation DESC,created_at_ms DESC,snapshot_id DESC LIMIT 1",
                (self._profile_id, scope_id),
            ).fetchone()
            if row is None:
                raise StorageValidationError("snapshot does not exist", "snapshot_not_found")
            return self._snapshot_record(row)

        return self._read(read)

    def latest_current_scope_id(self) -> Optional[str]:
        """返回当前 collection generation 最近发布的快照范围。"""

        def read(db: sqlite3.Connection) -> Optional[str]:
            row = db.execute(
                "SELECT s.scope_id FROM snapshots s JOIN profile_meta p ON p.profile_id=s.profile_id "
                "WHERE s.profile_id=? AND s.snapshot_generation=p.collection_generation "
                "ORDER BY s.created_at_ms DESC,s.snapshot_id DESC LIMIT 1",
                (self._profile_id,),
            ).fetchone()
            return str(row["scope_id"]) if row is not None else None

        return self._read(read)

    def issue_permit(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        snapshot_id: str,
        snapshot_generation: int,
        applied_watermark: int,
        permit_id: str,
        nonce_hash: str,
        expires_at_ms: int,
        now_ms: Optional[int] = None,
        resume_permit_id: Optional[str] = None,
    ) -> PermitRecord:
        """持久签发或恢复一个绑定完整 context 的一次性 permit。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _validate_nonnegative_safe(
            local_generation, "local_generation"
        )
        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        snapshot_generation = _validate_nonnegative_safe(
            snapshot_generation, "snapshot_generation"
        )
        applied_watermark = _validate_nonnegative_safe(
            applied_watermark, "applied_watermark"
        )
        permit_id = _validate_text(permit_id, "permit_id", 256)
        if (
            not isinstance(nonce_hash, str)
            or len(nonce_hash) != 64
            or any(char not in "0123456789abcdef" for char in nonce_hash)
        ):
            raise StorageValidationError("invalid permit nonce hash", "permit_invalid")
        if (
            isinstance(expires_at_ms, bool)
            or not isinstance(expires_at_ms, int)
            or expires_at_ms < 0
        ):
            raise StorageValidationError("invalid permit expiry", "permit_invalid")
        if resume_permit_id is not None:
            resume_permit_id = _validate_text(resume_permit_id, "permit_id", 256)
            if resume_permit_id != permit_id:
                raise StorageValidationError(
                    "permit identity mismatch", "permit_invalid"
                )
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            snapshot = db.execute(
                "SELECT * FROM snapshots WHERE profile_id=? AND snapshot_id=? AND scope_id=?",
                (self._profile_id, snapshot_id, scope_id),
            ).fetchone()
            if snapshot is None:
                raise StorageValidationError("snapshot does not exist", "snapshot_not_found")
            if int(snapshot["snapshot_generation"]) != snapshot_generation:
                raise StorageValidationError(
                    "snapshot generation mismatch", "generation_mismatch"
                )
            if int(snapshot["applied_watermark"] or 0) < applied_watermark:
                raise StorageValidationError(
                    "snapshot watermark is behind", "snapshot_watermark_behind"
                )
            existing_valid = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND device_id=? AND session_id=? "
                "AND scope_id=? AND local_generation=? AND snapshot_id=? "
                "AND snapshot_generation=? AND applied_watermark=? AND status='issued' "
                "AND expires_at_ms>?",
                (
                    self._profile_id,
                    device_id,
                    session_id,
                    scope_id,
                    local_generation,
                    snapshot_id,
                    snapshot_generation,
                    applied_watermark,
                    now,
                ),
            ).fetchall()
            for existing in existing_valid:
                if resume_permit_id is None:
                    raise StorageValidationError(
                        "a valid permit already exists", "permit_already_issued"
                    )
                if str(existing["permit_id"]) != resume_permit_id:
                    raise StorageValidationError(
                        "a different valid permit already exists",
                        "permit_already_issued",
                    )
                if (
                    str(existing["device_id"]) != device_id
                    or str(existing["session_id"]) != session_id
                    or str(existing["scope_id"]) != scope_id
                    or int(existing["local_generation"]) != local_generation
                    or str(existing["snapshot_id"]) != snapshot_id
                    or int(existing["snapshot_generation"]) != snapshot_generation
                    or int(existing["applied_watermark"]) != applied_watermark
                    or not hmac.compare_digest(str(existing["nonce_hash"]), nonce_hash)
                ):
                    raise StorageValidationError(
                        "permit context or nonce mismatch", "permit_invalid"
                    )
                return self._permit_record(existing)
            if resume_permit_id is not None:
                previous = db.execute(
                    "SELECT status FROM permits WHERE profile_id=? AND permit_id=?",
                    (self._profile_id, resume_permit_id),
                ).fetchone()
                if previous is not None and str(previous["status"]) == "used":
                    raise StorageValidationError(
                        "permit has already been consumed", "permit_already_used"
                    )
                if previous is not None:
                    raise StorageValidationError(
                        "permit is no longer valid", "permit_invalid"
                    )
            db.execute(
                "UPDATE permits SET status='expired' WHERE profile_id=? AND scope_id=? "
                "AND status='issued' AND expires_at_ms<=?",
                (self._profile_id, scope_id, now),
            )
            try:
                db.execute(
                    "INSERT INTO permits(profile_id,permit_id,snapshot_id,nonce_hash,status,"
                    "expires_at_ms,created_at_ms,used_at_ms,device_id,session_id,scope_id,"
                    "local_generation,snapshot_generation,applied_watermark) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._profile_id,
                        permit_id,
                        snapshot_id,
                        nonce_hash,
                        "issued",
                        expires_at_ms,
                        now,
                        None,
                        device_id,
                        session_id,
                        scope_id,
                        local_generation,
                        snapshot_generation,
                        applied_watermark,
                    ),
                )
                db.execute(
                    "INSERT INTO pull_progress(profile_id,permit_id,device_id,session_id,scope_id,"
                    "local_generation,snapshot_id,snapshot_generation,applied_watermark,next_page_index,"
                    "received_pages,received_bytes,complete,last_page_index,last_cursor,last_page_json,"
                    "created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._profile_id,
                        permit_id,
                        device_id,
                        session_id,
                        scope_id,
                        local_generation,
                        snapshot_id,
                        snapshot_generation,
                        applied_watermark,
                        0,
                        0,
                        0,
                        0,
                        -1,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StorageValidationError(
                    "permit identity conflict", "permit_invalid"
                ) from error
            row = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if row is None:
                raise SchemaError("permit row disappeared after insert", "permit_corrupt")
            return self._permit_record(row)

    def get_permit(self, permit_id: str) -> PermitRecord:
        """读取 permit 的非秘密持久字段。"""

        permit_id = _validate_text(permit_id, "permit_id", 256)

        def read(db: sqlite3.Connection) -> PermitRecord:
            row = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if row is None:
                raise StorageValidationError("permit does not exist", "permit_invalid")
            return self._permit_record(row)

        return self._read(read)

    def find_recoverable_permit(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        snapshot_id: str,
        snapshot_generation: int,
        applied_watermark: int,
        now_ms: Optional[int] = None,
    ) -> Optional[Tuple[PermitRecord, PullProgressRecord]]:
        """按完整拉取上下文查找唯一未过期的已签发 permit。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _validate_nonnegative_safe(
            local_generation, "local_generation"
        )
        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        snapshot_generation = _validate_nonnegative_safe(
            snapshot_generation, "snapshot_generation"
        )
        applied_watermark = _validate_nonnegative_safe(
            applied_watermark, "applied_watermark"
        )
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")

        def read(db: sqlite3.Connection) -> Optional[Tuple[PermitRecord, PullProgressRecord]]:
            self._require_session(db, device_id, session_id)
            rows = db.execute(
                "SELECT p.*,pp.next_page_index AS progress_next_page_index,"
                "pp.received_pages AS progress_received_pages,"
                "pp.received_bytes AS progress_received_bytes,"
                "pp.complete AS progress_complete,"
                "pp.last_page_index AS progress_last_page_index,"
                "pp.last_cursor AS progress_last_cursor,"
                "pp.last_page_json AS progress_last_page_json "
                "FROM permits p JOIN pull_progress pp ON pp.profile_id=p.profile_id "
                "AND pp.permit_id=p.permit_id AND pp.device_id=p.device_id "
                "AND pp.session_id=p.session_id AND pp.scope_id=p.scope_id "
                "AND pp.local_generation=p.local_generation "
                "AND pp.snapshot_id=p.snapshot_id "
                "AND pp.snapshot_generation=p.snapshot_generation "
                "AND pp.applied_watermark=p.applied_watermark "
                "WHERE p.profile_id=? AND p.device_id=? AND p.session_id=? "
                "AND p.scope_id=? AND p.local_generation=? AND p.snapshot_id=? "
                "AND p.snapshot_generation=? AND p.applied_watermark=? "
                "AND p.status='issued' AND p.expires_at_ms>? "
                "ORDER BY p.created_at_ms DESC LIMIT 2",
                (
                    self._profile_id,
                    device_id,
                    session_id,
                    scope_id,
                    local_generation,
                    snapshot_id,
                    snapshot_generation,
                    applied_watermark,
                    now,
                ),
            ).fetchall()
            if len(rows) > 1:
                raise StorageValidationError(
                    "multiple active permits match the pull context",
                    "permit_recovery_ambiguous",
                )
            if not rows:
                return None
            row = rows[0]
            progress = PullProgressRecord(
                permit_id=str(row["permit_id"]),
                next_page_index=int(row["progress_next_page_index"]),
                received_pages=int(row["progress_received_pages"]),
                received_bytes=int(row["progress_received_bytes"]),
                complete=bool(row["progress_complete"]),
                last_page_index=int(row["progress_last_page_index"]),
                last_cursor=row["progress_last_cursor"],
                last_page_json=row["progress_last_page_json"],
            )
            return self._permit_record(row), progress

        return self._read(read)

    def recover_permit_nonce(
        self,
        *,
        expected_permit: PermitRecord,
        expected_progress: PullProgressRecord,
        nonce_hash: str,
        now_ms: int,
    ) -> Tuple[PermitRecord, PullProgressRecord]:
        """在事务中将精确匹配的有效 permit 绑定到幂等恢复 nonce。"""

        if not isinstance(expected_permit, PermitRecord) or not isinstance(
            expected_progress, PullProgressRecord
        ):
            raise TypeError("expected permit and progress records are required")
        if (
            expected_permit.profile_id != self._profile_id
            or expected_progress.permit_id != expected_permit.permit_id
        ):
            raise StorageValidationError(
                "permit recovery context mismatch", "permit_recovery_conflict"
            )
        if (
            not isinstance(nonce_hash, str)
            or len(nonce_hash) != 64
            or any(char not in "0123456789abcdef" for char in nonce_hash)
        ):
            raise StorageValidationError("invalid permit nonce hash", "permit_invalid")
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")

        with self._transaction() as db:
            self._require_session(
                db, expected_permit.device_id, expected_permit.session_id
            )
            row = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                (self._profile_id, expected_permit.permit_id),
            ).fetchone()
            if row is None:
                raise StorageValidationError("permit does not exist", "permit_invalid")
            current = self._permit_record(row)
            expected_context = (
                expected_permit.device_id,
                expected_permit.session_id,
                expected_permit.scope_id,
                expected_permit.local_generation,
                expected_permit.snapshot_id,
                expected_permit.snapshot_generation,
                expected_permit.applied_watermark,
                expected_permit.expires_at_ms,
            )
            current_context = (
                current.device_id,
                current.session_id,
                current.scope_id,
                current.local_generation,
                current.snapshot_id,
                current.snapshot_generation,
                current.applied_watermark,
                current.expires_at_ms,
            )
            if current_context != expected_context:
                raise StorageValidationError(
                    "permit recovery context changed", "permit_recovery_conflict"
                )
            if current.status != "issued" or current.expires_at_ms <= now_ms:
                raise StorageValidationError("permit is no longer active", "permit_expired")
            progress_row = db.execute(
                "SELECT * FROM pull_progress WHERE profile_id=? AND permit_id=? "
                "AND device_id=? AND session_id=? AND scope_id=? AND local_generation=? "
                "AND snapshot_id=? AND snapshot_generation=? AND applied_watermark=?",
                (
                    self._profile_id,
                    expected_permit.permit_id,
                    expected_permit.device_id,
                    expected_permit.session_id,
                    expected_permit.scope_id,
                    expected_permit.local_generation,
                    expected_permit.snapshot_id,
                    expected_permit.snapshot_generation,
                    expected_permit.applied_watermark,
                ),
            ).fetchone()
            if progress_row is None:
                raise StorageValidationError(
                    "pull progress does not match permit", "pull_progress_corrupt"
                )
            progress = PullProgressRecord(
                permit_id=str(progress_row["permit_id"]),
                next_page_index=int(progress_row["next_page_index"]),
                received_pages=int(progress_row["received_pages"]),
                received_bytes=int(progress_row["received_bytes"]),
                complete=bool(progress_row["complete"]),
                last_page_index=int(progress_row["last_page_index"]),
                last_cursor=progress_row["last_cursor"],
                last_page_json=progress_row["last_page_json"],
            )
            if progress != expected_progress:
                raise StorageValidationError(
                    "pull progress changed during permit recovery",
                    "permit_recovery_conflict",
                )
            if current.nonce_hash != nonce_hash:
                if current.nonce_hash != expected_permit.nonce_hash:
                    raise StorageValidationError(
                        "permit nonce changed during recovery",
                        "permit_recovery_conflict",
                    )
                db.execute(
                    "UPDATE permits SET nonce_hash=? WHERE profile_id=? AND permit_id=? "
                    "AND status='issued' AND expires_at_ms>? AND nonce_hash=?",
                    (
                        nonce_hash,
                        self._profile_id,
                        expected_permit.permit_id,
                        now_ms,
                        expected_permit.nonce_hash,
                    ),
                )
                if db.execute("SELECT changes()").fetchone()[0] != 1:
                    raise StorageValidationError(
                        "permit changed during nonce recovery",
                        "permit_recovery_conflict",
                    )
                row = db.execute(
                    "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                    (self._profile_id, expected_permit.permit_id),
                ).fetchone()
                if row is None:
                    raise SchemaError("permit disappeared during recovery", "permit_corrupt")
                current = self._permit_record(row)
            return current, progress

    def get_pull_progress(self, permit_id: str) -> PullProgressRecord:
        """读取可跨进程恢复的分页 checkpoint。"""

        permit_id = _validate_text(permit_id, "permit_id", 256)

        def read(db: sqlite3.Connection) -> PullProgressRecord:
            row = db.execute(
                "SELECT * FROM pull_progress WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if row is None:
                raise StorageValidationError(
                    "pull progress does not exist", "pull_progress_corrupt"
                )
            return PullProgressRecord(
                permit_id,
                int(row["next_page_index"]),
                int(row["received_pages"]),
                int(row["received_bytes"]),
                bool(row["complete"]),
                int(row["last_page_index"]),
                row["last_cursor"],
                row["last_page_json"],
            )

        return self._read(read)

    def record_page(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        permit_id: str,
        nonce_hash: str,
        snapshot_id: str,
        snapshot_generation: int,
        page_index: int,
        cursor: Optional[str],
        now_ms: Optional[int] = None,
    ) -> Tuple[str, str, int, Optional[str], bool, PullProgressRecord]:
        """在 SQLite 事务中校验并记录连续页；重复同页返回已提交字节。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _validate_nonnegative_safe(
            local_generation, "local_generation"
        )
        permit_id = _validate_text(permit_id, "permit_id", 256)
        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        snapshot_generation = _validate_nonnegative_safe(
            snapshot_generation, "snapshot_generation"
        )
        page_index = _validate_nonnegative_safe(page_index, "page_index")
        if (
            not isinstance(nonce_hash, str)
            or len(nonce_hash) != 64
            or any(char not in "0123456789abcdef" for char in nonce_hash)
        ):
            raise StorageValidationError("invalid permit nonce hash", "permit_invalid")
        if cursor is not None:
            cursor = _validate_text(cursor, "cursor", 256)
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            permit_row = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if permit_row is None:
                raise StorageValidationError("permit does not exist", "permit_invalid")
            status = str(permit_row["status"])
            if status == "used":
                raise StorageValidationError(
                    "permit has already been consumed", "permit_already_used"
                )
            if status != "issued":
                raise StorageValidationError("permit is not active", "permit_invalid")
            if int(permit_row["expires_at_ms"]) <= now:
                db.execute(
                    "UPDATE permits SET status='expired' WHERE profile_id=? AND permit_id=?",
                    (self._profile_id, permit_id),
                )
                raise StorageValidationError("permit has expired", "permit_expired")
            if not hmac.compare_digest(str(permit_row["nonce_hash"]), nonce_hash):
                raise StorageValidationError("permit nonce mismatch", "permit_invalid")
            expected = (
                str(permit_row["device_id"]) == device_id
                and str(permit_row["session_id"]) == session_id
                and str(permit_row["scope_id"]) == scope_id
                and int(permit_row["local_generation"]) == local_generation
                and str(permit_row["snapshot_id"]) == snapshot_id
                and int(permit_row["snapshot_generation"]) == snapshot_generation
            )
            if not expected:
                raise StorageValidationError(
                    "permit context mismatch", "permit_invalid"
                )
            progress = db.execute(
                "SELECT * FROM pull_progress WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if progress is None:
                raise SchemaError("pull progress is missing", "pull_progress_corrupt")
            last_page_index = int(progress["last_page_index"])
            if page_index == last_page_index and progress["last_page_json"] is not None:
                if progress["last_cursor"] != cursor:
                    raise StorageValidationError(
                        "repeated page cursor mismatch", "page_out_of_order"
                    )
                page_row = db.execute(
                    "SELECT page_hash,page_bytes,cursor,is_last FROM snapshot_pages "
                    "WHERE profile_id=? AND snapshot_id=? AND page_index=?",
                    (self._profile_id, snapshot_id, page_index),
                ).fetchone()
                if page_row is None:
                    raise SchemaError("snapshot page is missing", "snapshot_corrupt")
                current_progress = PullProgressRecord(
                    permit_id,
                    int(progress["next_page_index"]),
                    int(progress["received_pages"]),
                    int(progress["received_bytes"]),
                    bool(progress["complete"]),
                    last_page_index,
                    progress["last_cursor"],
                    str(progress["last_page_json"]),
                )
                return (
                    str(progress["last_page_json"]),
                    str(page_row["page_hash"]),
                    int(page_row["page_bytes"]),
                    page_row["cursor"],
                    bool(page_row["is_last"]),
                    current_progress,
                )
            next_page_index = int(progress["next_page_index"])
            if page_index < next_page_index:
                # 设备重启后没有先前卡页的运行时副本；已确认页可从不可变快照只读重放。
                previous_cursor = None
                if page_index > 0:
                    previous = db.execute(
                        "SELECT cursor FROM snapshot_pages WHERE profile_id=? AND snapshot_id=? AND page_index=?",
                        (self._profile_id, snapshot_id, page_index - 1),
                    ).fetchone()
                    if previous is None:
                        raise SchemaError("snapshot page is missing", "snapshot_corrupt")
                    previous_cursor = previous["cursor"]
                if cursor != previous_cursor:
                    raise StorageValidationError(
                        "replayed page cursor mismatch", "page_out_of_order"
                    )
                page_row = db.execute(
                    "SELECT page_json,page_hash,page_bytes,cursor,is_last FROM snapshot_pages "
                    "WHERE profile_id=? AND snapshot_id=? AND page_index=?",
                    (self._profile_id, snapshot_id, page_index),
                ).fetchone()
                if page_row is None:
                    raise SchemaError("snapshot page is missing", "snapshot_corrupt")
                current_progress = PullProgressRecord(
                    permit_id,
                    next_page_index,
                    int(progress["received_pages"]),
                    int(progress["received_bytes"]),
                    bool(progress["complete"]),
                    last_page_index,
                    progress["last_cursor"],
                    progress["last_page_json"],
                )
                return (
                    str(page_row["page_json"]),
                    str(page_row["page_hash"]),
                    int(page_row["page_bytes"]),
                    page_row["cursor"],
                    bool(page_row["is_last"]),
                    current_progress,
                )
            if page_index != next_page_index:
                raise StorageValidationError(
                    "page index is not continuous", "page_out_of_order"
                )
            if page_index == 0:
                if cursor is not None:
                    raise StorageValidationError(
                        "first page cursor must be null", "page_out_of_order"
                    )
            else:
                previous = db.execute(
                    "SELECT cursor FROM snapshot_pages WHERE profile_id=? AND snapshot_id=? AND page_index=?",
                    (self._profile_id, snapshot_id, page_index - 1),
                ).fetchone()
                if previous is None or previous["cursor"] != cursor:
                    raise StorageValidationError(
                        "page cursor is not continuous", "page_out_of_order"
                    )
            page_row = db.execute(
                "SELECT page_json,page_hash,page_bytes,cursor,is_last FROM snapshot_pages "
                "WHERE profile_id=? AND snapshot_id=? AND page_index=?",
                (self._profile_id, snapshot_id, page_index),
            ).fetchone()
            if page_row is None:
                raise StorageValidationError("page index does not exist", "page_out_of_order")
            received_pages = int(progress["received_pages"]) + 1
            received_bytes = int(progress["received_bytes"]) + int(page_row["page_bytes"])
            complete = bool(page_row["is_last"])
            db.execute(
                "UPDATE pull_progress SET next_page_index=?,received_pages=?,received_bytes=?,"
                "complete=?,last_page_index=?,last_cursor=?,last_page_json=?,updated_at_ms=? "
                "WHERE profile_id=? AND permit_id=?",
                (
                    page_index + 1,
                    received_pages,
                    received_bytes,
                    int(complete),
                    page_index,
                    cursor,
                    str(page_row["page_json"]),
                    now,
                    self._profile_id,
                    permit_id,
                ),
            )
            progress = db.execute(
                "SELECT * FROM pull_progress WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if progress is None:
                raise SchemaError("pull progress disappeared", "pull_progress_corrupt")
            current_progress = PullProgressRecord(
                permit_id,
                int(progress["next_page_index"]),
                int(progress["received_pages"]),
                int(progress["received_bytes"]),
                bool(progress["complete"]),
                int(progress["last_page_index"]),
                progress["last_cursor"],
                progress["last_page_json"],
            )
            return (
                str(page_row["page_json"]),
                str(page_row["page_hash"]),
                int(page_row["page_bytes"]),
                page_row["cursor"],
                bool(page_row["is_last"]),
                current_progress,
            )

    def consume_permit(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        permit_id: str,
        nonce_hash: str,
        snapshot_id: str,
        snapshot_generation: int,
        snapshot_checksum: str,
        now_ms: Optional[int] = None,
    ) -> PermitRecord:
        """只有完整且至少一页已持久记录时才消费 permit。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _validate_nonnegative_safe(
            local_generation, "local_generation"
        )
        permit_id = _validate_text(permit_id, "permit_id", 256)
        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        snapshot_generation = _validate_nonnegative_safe(
            snapshot_generation, "snapshot_generation"
        )
        if (
            not isinstance(nonce_hash, str)
            or len(nonce_hash) != 64
            or any(char not in "0123456789abcdef" for char in nonce_hash)
        ):
            raise StorageValidationError("invalid permit nonce hash", "permit_invalid")
        if not isinstance(snapshot_checksum, str) or len(snapshot_checksum) != 64:
            raise StorageValidationError("invalid snapshot checksum", "snapshot_mismatch")
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            row = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if row is None:
                raise StorageValidationError("permit does not exist", "permit_invalid")
            status = str(row["status"])
            if status == "used":
                raise StorageValidationError(
                    "permit has already been consumed", "permit_already_used"
                )
            if status != "issued":
                raise StorageValidationError("permit is not active", "permit_invalid")
            if int(row["expires_at_ms"]) <= now:
                db.execute(
                    "UPDATE permits SET status='expired' WHERE profile_id=? AND permit_id=?",
                    (self._profile_id, permit_id),
                )
                raise StorageValidationError("permit has expired", "permit_expired")
            if not hmac.compare_digest(str(row["nonce_hash"]), nonce_hash):
                raise StorageValidationError("permit nonce mismatch", "permit_invalid")
            if (
                str(row["device_id"]) != device_id
                or str(row["session_id"]) != session_id
                or str(row["scope_id"]) != scope_id
                or int(row["local_generation"]) != local_generation
                or str(row["snapshot_id"]) != snapshot_id
                or int(row["snapshot_generation"]) != snapshot_generation
            ):
                raise StorageValidationError("permit context mismatch", "permit_invalid")
            snapshot = db.execute(
                "SELECT checksum,page_count FROM snapshots WHERE profile_id=? AND snapshot_id=? AND scope_id=?",
                (self._profile_id, snapshot_id, scope_id),
            ).fetchone()
            if snapshot is None or str(snapshot["checksum"]) != snapshot_checksum:
                raise StorageValidationError(
                    "snapshot checksum mismatch", "snapshot_mismatch"
                )
            progress = db.execute(
                "SELECT * FROM pull_progress WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if (
                progress is None
                or not bool(progress["complete"])
                or int(progress["received_pages"]) <= 0
                or int(progress["received_pages"]) != int(snapshot["page_count"])
                or int(progress["next_page_index"]) != int(snapshot["page_count"])
                or int(progress["last_page_index"]) < 0
            ):
                raise StorageValidationError(
                    "pull progress is incomplete", "pull_incomplete"
                )
            used_at = now
            db.execute(
                "UPDATE permits SET status='used',used_at_ms=? WHERE profile_id=? AND permit_id=?",
                (used_at, self._profile_id, permit_id),
            )
            row = db.execute(
                "SELECT * FROM permits WHERE profile_id=? AND permit_id=?",
                (self._profile_id, permit_id),
            ).fetchone()
            if row is None:
                raise SchemaError("permit disappeared after commit", "permit_corrupt")
            return self._permit_record(row)

    def register_device(
        self,
        device_id: str,
        token: Union[str, bytes, bytearray],
        *,
        now_ms: Optional[int] = None,
    ) -> DeviceRecord:
        """注册设备并只保存 token 摘要，不在返回值或日志中暴露 token。"""

        device_id = _validate_text(device_id, "device_id", 128)
        token_hash = _hash_secret(token)
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            row = db.execute(
                "SELECT token_hash,created_at_ms,last_seen_at_ms,revoked_at_ms FROM devices "
                "WHERE profile_id=? AND device_id=?",
                (self._profile_id, device_id),
            ).fetchone()
            if row is not None:
                if row["token_hash"] != token_hash:
                    raise StorageValidationError(
                        "device identity conflict", "device_conflict"
                    )
                db.execute(
                    "UPDATE devices SET revoked_at_ms=NULL,last_seen_at_ms=? WHERE profile_id=? AND device_id=?",
                    (now, self._profile_id, device_id),
                )
                created = int(row["created_at_ms"])
                last_seen = now
            else:
                try:
                    db.execute(
                        "INSERT INTO devices(profile_id,device_id,token_hash,created_at_ms,last_seen_at_ms,revoked_at_ms) "
                        "VALUES(?,?,?,?,?,NULL)",
                        (self._profile_id, device_id, token_hash, now, now),
                    )
                except sqlite3.IntegrityError as error:
                    raise StorageValidationError(
                        "device identity conflict", "device_conflict"
                    ) from error
                created = now
                last_seen = now
        return DeviceRecord(self._profile_id, device_id, created, last_seen, None)

    def verify_device_token(
        self, device_id: str, token: Union[str, bytes, bytearray]
    ) -> bool:
        """验证设备 token 摘要并更新最近访问时间；错误时只返回 False。"""

        device_id = _validate_text(device_id, "device_id", 128)
        token_hash = _hash_secret(token)
        now = _now_ms()
        with self._transaction() as db:
            row = db.execute(
                "SELECT token_hash,revoked_at_ms FROM devices WHERE profile_id=? AND device_id=?",
                (self._profile_id, device_id),
            ).fetchone()
            if (
                row is None
                or row["revoked_at_ms"] is not None
                or row["token_hash"] != token_hash
            ):
                return False
            db.execute(
                "UPDATE devices SET last_seen_at_ms=? WHERE profile_id=? AND device_id=?",
                (now, self._profile_id, device_id),
            )
            return True

    def revoke_device(self, device_id: str, *, now_ms: Optional[int] = None) -> None:
        """撤销设备并关闭其后续 session 身份。"""

        device_id = _validate_text(device_id, "device_id", 128)
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            db.execute(
                "UPDATE devices SET revoked_at_ms=? WHERE profile_id=? AND device_id=?",
                (now, self._profile_id, device_id),
            )
            db.execute(
                "UPDATE sessions SET closed_at_ms=? WHERE profile_id=? AND device_id=? AND closed_at_ms IS NULL",
                (now, self._profile_id, device_id),
            )

    def open_session(
        self, device_id: str, session_id: str, *, now_ms: Optional[int] = None
    ) -> SessionRecord:
        """在已注册设备下创建或恢复一个 session 序号水位。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            existing_device = db.execute(
                "SELECT 1 FROM devices WHERE profile_id=? AND device_id=? AND revoked_at_ms IS NULL",
                (self._profile_id, device_id),
            ).fetchone()
            if existing_device is None:
                raise StorageValidationError(
                    "device is not registered", "unknown_device"
                )
            row = db.execute(
                "SELECT * FROM sessions WHERE profile_id=? AND device_id=? AND session_id=?",
                (self._profile_id, device_id, session_id),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO sessions(profile_id,device_id,session_id,high_water,created_at_ms,last_seen_at_ms,closed_at_ms) "
                    "VALUES(?,?,?,?,?,?,NULL)",
                    (self._profile_id, device_id, session_id, 0, now, now),
                )
                high_water = 0
                created = now
                closed = None
            else:
                db.execute(
                    "UPDATE sessions SET last_seen_at_ms=?,closed_at_ms=NULL WHERE profile_id=? AND device_id=? AND session_id=?",
                    (now, self._profile_id, device_id, session_id),
                )
                high_water = int(row["high_water"])
                created = int(row["created_at_ms"])
                closed = None
        return SessionRecord(
            self._profile_id, device_id, session_id, high_water, created, now, closed
        )

    def close_session(
        self, device_id: str, session_id: str, *, now_ms: Optional[int] = None
    ) -> None:
        """显式关闭 session；已持久事件仍保留用于回执查询。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            db.execute(
                "UPDATE sessions SET closed_at_ms=?,last_seen_at_ms=? "
                "WHERE profile_id=? AND device_id=? AND session_id=?",
                (now, now, self._profile_id, device_id, session_id),
            )

    def receive_compact_batch(
        self,
        device_id: str,
        session_id: str,
        snapshot_id: str,
        base_sequence: int,
        events: Sequence[Sequence[int]],
        *,
        now_ms: Optional[int] = None,
    ) -> Tuple[Tuple[Receipt, ...], bool]:
        """收下 x4-3 短日志；整批冲突时只提交冲突证据，不提交新事件。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        snapshot_id = _validate_text(snapshot_id, "snapshot_id", 256)
        if isinstance(base_sequence, bool) or not isinstance(base_sequence, int) or base_sequence < 1:
            raise StorageValidationError("invalid base sequence", "invalid_base_sequence")
        if not isinstance(events, (list, tuple)) or not events:
            raise StorageValidationError("invalid compact events", "invalid_compact_events")
        if base_sequence + len(events) - 1 > LUA_SAFE_INTEGER_MAX:
            raise StorageValidationError("sequence overflow", "invalid_base_sequence")
        normalized: List[Tuple[int, int, int, int]] = []
        for event in events:
            if not isinstance(event, (list, tuple)) or len(event) != 4:
                raise StorageValidationError("invalid compact event", "invalid_compact_event")
            index, rating, answered, duration = event
            values = (index, rating, answered, duration)
            if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
                raise StorageValidationError("invalid compact event", "invalid_compact_event")
            if index < 1 or rating not in (1, 2, 3, 4) or answered < 0 or duration < 0:
                raise StorageValidationError("invalid compact event", "invalid_compact_event")
            if answered > LUA_SAFE_INTEGER_MAX or duration > LUA_SAFE_INTEGER_MAX:
                raise StorageValidationError("invalid compact event", "invalid_compact_event")
            normalized.append((index, rating, answered, duration))
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")

        receipts: List[Receipt] = []
        conflict = False
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            snapshot = db.execute(
                "SELECT payload_json FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                (self._profile_id, snapshot_id),
            ).fetchone()
            if snapshot is None:
                raise StorageValidationError("snapshot not found", "snapshot_not_found")
            try:
                cards = json.loads(str(snapshot["payload_json"]))["cards"]
                if not isinstance(cards, list):
                    raise ValueError
                card_ids = [card["card_id"] for card in cards]
                if not all(isinstance(card_id, str) and card_id for card_id in card_ids):
                    raise ValueError
            except (TypeError, ValueError, KeyError, IndexError):
                raise StorageValidationError("snapshot card map invalid", "snapshot_invalid") from None
            if any(index > len(card_ids) for index, _, _, _ in normalized):
                raise StorageValidationError("card index outside snapshot", "card_index_invalid")
            high = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM compact_events "
                "WHERE profile_id=? AND device_id=? AND session_id=?",
                (self._profile_id, device_id, session_id),
            ).fetchone()[0]
            prepared = []
            next_new = int(high) + 1
            for offset, (index, rating, answered, duration) in enumerate(normalized):
                sequence = base_sequence + offset
                event_id = "%s:%d" % (session_id, sequence)
                card_id = card_ids[index - 1]
                payload = [snapshot_id, card_id, index, rating, answered, duration]
                encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                existing = db.execute(
                    "SELECT payload_hash FROM compact_events WHERE profile_id=? AND device_id=? "
                    "AND session_id=? AND sequence=?",
                    (self._profile_id, device_id, session_id, sequence),
                ).fetchone()
                if existing is not None:
                    if str(existing["payload_hash"]) != digest:
                        conflict = True
                        db.execute(
                            "INSERT OR IGNORE INTO compact_conflicts(profile_id,device_id,session_id,sequence,"
                            "observed_payload_hash,created_at_ms) VALUES(?,?,?,?,?,?)",
                            (self._profile_id, device_id, session_id, sequence, digest, now),
                        )
                        db.execute(
                            "UPDATE compact_applications SET status='conflict',reason='payload_conflict',updated_at_ms=? "
                            "WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=? "
                            "AND status!='applied'",
                            (now, self._profile_id, device_id, session_id, sequence),
                        )
                    else:
                        receipts.append(Receipt(event_id, sequence, "received", digest))
                    continue
                if sequence != next_new:
                    raise SequenceError("compact event sequence has a gap", "sequence_gap")
                next_new += 1
                prepared.append((sequence, event_id, card_id, index, rating, answered, duration, digest))
            if not conflict:
                for sequence, event_id, card_id, index, rating, answered, duration, digest in prepared:
                    db.execute(
                        "INSERT INTO compact_events(profile_id,device_id,session_id,sequence,event_id,"
                        "snapshot_id,card_id,card_index,rating,answered_at_ms,duration_ms,payload_hash,received_at_ms) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self._profile_id, device_id, session_id, sequence, event_id, snapshot_id,
                         card_id, index, rating, answered, duration, digest, now),
                    )
                    db.execute(
                        "INSERT INTO compact_applications(profile_id,device_id,session_id,sequence,event_id,"
                        "payload_hash,status,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,'not_applied',?,?)",
                        (self._profile_id, device_id, session_id, sequence, event_id, digest, now, now),
                    )
                    receipts.append(Receipt(event_id, sequence, "received", digest))
                receipts.sort(key=lambda receipt: receipt.sequence)
            if conflict or prepared:
                db.execute(
                    "UPDATE permits SET status='revoked' WHERE profile_id=? AND device_id=? "
                    "AND status='issued'",
                    (self._profile_id, device_id),
                )
        return tuple(receipts), conflict

    def query_compact_receipts(
        self, device_id: str, session_id: str, *, after_sequence: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Receipt]:
        """查询 x4-3 durable 收据；没有 Anki application 状态。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        if after_sequence is not None and (
            isinstance(after_sequence, bool) or not isinstance(after_sequence, int)
            or after_sequence < 0 or after_sequence > LUA_SAFE_INTEGER_MAX
        ):
            raise StorageValidationError("invalid after sequence", "invalid_after_sequence")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000
        ):
            raise StorageValidationError("invalid receipt limit", "invalid_receipt_limit")
        def read(db: sqlite3.Connection) -> List[Receipt]:
            self._require_session(db, device_id, session_id)
            rows = db.execute(
                "SELECT event_id,sequence,payload_hash FROM compact_events "
                "WHERE profile_id=? AND device_id=? AND session_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (self._profile_id, device_id, session_id, after_sequence or 0,
                 limit if limit is not None else LUA_SAFE_INTEGER_MAX),
            ).fetchall()
            return [Receipt(str(row["event_id"]), int(row["sequence"]), "received", str(row["payload_hash"]))
                    for row in rows]
        return self._read(read)

    @staticmethod
    def _compact_event_record(row: sqlite3.Row) -> CompactEventRecord:
        return CompactEventRecord(
            profile_id=str(row["profile_id"]), device_id=str(row["device_id"]),
            session_id=str(row["session_id"]), sequence=int(row["sequence"]),
            event_id=str(row["event_id"]), snapshot_id=str(row["snapshot_id"]),
            card_id=str(row["card_id"]), card_index=int(row["card_index"]),
            rating=int(row["rating"]), answered_at_ms=int(row["answered_at_ms"]),
            duration_ms=int(row["duration_ms"]), payload_hash=str(row["payload_hash"]),
            received_at_ms=int(row["received_at_ms"]),
        )

    @staticmethod
    def _compact_application_record(row: sqlite3.Row) -> CompactApplicationRecord:
        return CompactApplicationRecord(
            profile_id=str(row["profile_id"]), device_id=str(row["device_id"]),
            session_id=str(row["session_id"]), sequence=int(row["sequence"]),
            event_id=str(row["event_id"]), payload_hash=str(row["payload_hash"]),
            status=str(row["status"]), recovery_required=bool(row["recovery_required"]),
            collection_identity=row["collection_identity"],
            collection_generation=(int(row["collection_generation"])
                                  if row["collection_generation"] is not None else None),
            collection_generation_after=(int(row["collection_generation_after"])
                                         if row["collection_generation_after"] is not None else None),
            policy_json=row["policy_json"], before_json=row["before_json"],
            before_hash=row["before_hash"], plan_json=row["plan_json"],
            expected_after_json=row["expected_after_json"], after_json=row["after_json"],
            after_hash=row["after_hash"], intent_id=row["intent_id"],
            attempt_id=row["attempt_id"], reason=row["reason"],
            receipt_json=row["receipt_json"], receipt_hash=row["receipt_hash"],
            created_at_ms=int(row["created_at_ms"]), updated_at_ms=int(row["updated_at_ms"]),
        )

    def query_compact_events(self, device_id: str, session_id: str) -> List[CompactEventRecord]:
        """读取指定设备/session 的 compact 事件，不与普通事件序列混读。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)

        def read(db: sqlite3.Connection) -> List[CompactEventRecord]:
            self._require_session(db, device_id, session_id)
            rows = db.execute(
                "SELECT * FROM compact_events WHERE profile_id=? AND device_id=? AND session_id=? "
                "ORDER BY sequence",
                (self._profile_id, device_id, session_id),
            ).fetchall()
            return [self._compact_event_record(row) for row in rows]

        return self._read(read)

    def query_compact_applications(
        self, device_id: str, session_id: str
    ) -> List[CompactApplicationRecord]:
        """读取 compact 应用证据；收件回执仍由 query_compact_receipts 返回 received。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)

        def read(db: sqlite3.Connection) -> List[CompactApplicationRecord]:
            self._require_session(db, device_id, session_id)
            rows = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND device_id=? AND session_id=? "
                "ORDER BY sequence",
                (self._profile_id, device_id, session_id),
            ).fetchall()
            return [self._compact_application_record(row) for row in rows]

        return self._read(read)

    def query_compact_score_evidence(
        self, device_id: str, session_id: str, sequence: int
    ) -> List[CompactScoreEvidenceRecord]:
        """读取一个 compact 应用尝试的全部阶段证据。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        sequence = _validate_sequence(sequence)

        def read(db: sqlite3.Connection) -> List[CompactScoreEvidenceRecord]:
            self._require_session(db, device_id, session_id)
            rows = db.execute(
                "SELECT * FROM compact_score_evidence WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND sequence=? ORDER BY created_at_ms,stage",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchall()
            return [CompactScoreEvidenceRecord(
                profile_id=str(row["profile_id"]), device_id=str(row["device_id"]),
                session_id=str(row["session_id"]), sequence=int(row["sequence"]),
                stage=str(row["stage"]), evidence_json=str(row["evidence_json"]),
                evidence_hash=str(row["evidence_hash"]), created_at_ms=int(row["created_at_ms"]),
            ) for row in rows]

        return self._read(read)

    def begin_compact_apply(
        self,
        device_id: str,
        session_id: str,
        sequence: int,
        *,
        collection_identity: str,
        collection_generation: int,
        policy_json: Any,
        before_json: Any,
        plan_json: Any,
        expected_after_json: Any,
        intent_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        now_ms: Optional[int] = None,
    ) -> CompactApplicationRecord:
        """先持久化卡级 claim、身份、policy 和计划；调用者只能在返回后评分。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        sequence = _validate_sequence(sequence)
        collection_identity = _validate_text(collection_identity, "collection_identity", 256)
        collection_generation = _validate_nonnegative_safe(collection_generation, "collection_generation")
        policy = _canonical_object_json(policy_json, "policy_json")
        before = _canonical_object_json(before_json, "before_json")
        plan = _canonical_object_json(plan_json, "plan_json")
        expected_after = _canonical_object_json(expected_after_json, "expected_after_json")
        required_stages = _required_compact_stages(plan)
        intent = _validate_text(intent_id or uuid.uuid4().hex, "intent_id", 128)
        attempt = _validate_text(attempt_id or uuid.uuid4().hex, "attempt_id", 128)
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        before_hash = _json_hash(before)
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            event = db.execute(
                "SELECT * FROM compact_events WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone()
            app = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone()
            if event is None or app is None:
                raise StorageValidationError("compact event does not exist", "compact_event_not_found")
            if str(app["payload_hash"]) != str(event["payload_hash"]):
                raise SchemaError("compact payload hash mismatch", "compact_application_corrupt")
            if db.execute(
                "SELECT 1 FROM compact_conflicts WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone() is not None:
                raise ReceiptRegressionError("compact event has a payload conflict", "compact_conflict")
            if str(app["status"]) == "unknown":
                raise UnknownApplyError("unknown compact application is not retryable", "unknown_compact_apply")
            if str(app["status"]) != "not_applied":
                raise ReceiptRegressionError("compact application is not pending", "compact_apply_not_pending")
            generation_row = db.execute(
                "SELECT collection_generation FROM profile_meta WHERE profile_id=?",
                (self._profile_id,),
            ).fetchone()
            if generation_row is None or int(generation_row["collection_generation"]) != collection_generation:
                raise ProfileMismatchError("collection generation changed", "generation_mismatch")

            session_sequences = [int(row[0]) for row in db.execute(
                "SELECT sequence FROM compact_events WHERE profile_id=? AND device_id=? AND session_id=? "
                "AND sequence<? ORDER BY sequence",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchall()]
            if session_sequences != list(range(1, sequence)):
                raise SequenceError("compact session has a sequence gap before this event", "compact_sequence_gap")
            same_session_prior = db.execute(
                "SELECT a.status FROM compact_events e JOIN compact_applications a ON a.profile_id=e.profile_id "
                "AND a.device_id=e.device_id AND a.session_id=e.session_id AND a.sequence=e.sequence "
                "WHERE e.profile_id=? AND e.device_id=? AND e.session_id=? AND e.card_id=? AND e.sequence<? "
                "ORDER BY e.sequence",
                (self._profile_id, device_id, session_id, event["card_id"], sequence),
            ).fetchall()
            if any(str(row["status"]) != "applied" for row in same_session_prior):
                raise ReceiptRegressionError("an earlier same-session same-card event is unresolved", "compact_card_order")
            cross_scope_pending = db.execute(
                "SELECT 1 FROM compact_events e JOIN compact_applications a ON a.profile_id=e.profile_id "
                "AND a.device_id=e.device_id AND a.session_id=e.session_id AND a.sequence=e.sequence "
                "WHERE e.profile_id=? AND e.card_id=? AND NOT(e.device_id=? AND e.session_id=?) "
                "AND a.status!='applied' LIMIT 1",
                (self._profile_id, event["card_id"], device_id, session_id),
            ).fetchone()
            if cross_scope_pending is not None:
                raise ReceiptRegressionError("cross-session same-card order is unresolved", "compact_cross_scope_ambiguous")

            remaining = db.execute(
                "SELECT a.* "
                "FROM compact_events e JOIN compact_applications a ON a.profile_id=e.profile_id "
                "AND a.device_id=e.device_id AND a.session_id=e.session_id AND a.sequence=e.sequence "
                "WHERE e.profile_id=? AND e.card_id=? AND a.status='applied' "
                "AND NOT(e.device_id=? AND e.session_id=? AND e.sequence=?)",
                (self._profile_id, event["card_id"], device_id, session_id, sequence),
            ).fetchall()
            remaining = list(remaining)
            cursor_json = before
            cursor_hash = before_hash
            chain_rows: List[sqlite3.Row] = []
            used_card_projection = False
            frozen_baseline = None
            compatibility_verified = False
            for row in remaining:
                self._require_valid_compact_terminal_receipt(db, row)
            snapshot_row = db.execute(
                "SELECT * FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                (self._profile_id, event['snapshot_id']),
            ).fetchone()
            if snapshot_row is not None:
                snapshot_record = self._snapshot_record(snapshot_row)
                context = snapshot_record.application_context
                if context is not None:
                    native_plan = json.loads(plan)
                    if (context.source.profile_identity != collection_identity
                        or context.source.collection_generation > collection_generation
                        or native_plan.get('snapshot_generation') != context.snapshot_generation
                        or native_plan.get('application_context_sha256') != context.source_sha256
                        or _json_hash(snapshot_record.payload_json) != snapshot_record.checksum):
                        raise ReceiptRegressionError('fresh snapshot baseline is not bound to the native plan', 'compact_snapshot_baseline_invalid')
                    # 所有旧收据已完整核验。冻结快照之前的历史不覆盖此新基线；
                    # 只要求该基线之后的同卡事件形成唯一链，不删改历史收据。
                    remaining = [row for row in remaining
                                 if int(row['collection_generation_after']) > context.source.collection_generation]
                    frozen_baseline = (snapshot_record, context, native_plan)
            while remaining:
                card_id = str(event["card_id"])
                cursor_projection = self._compact_full_frame_card_logs_projection(cursor_json, card_id)
                predecessors: List[Tuple[sqlite3.Row, bool]] = []
                for row in remaining:
                    if row["after_json"] is None or row["after_hash"] is None:
                        continue
                    if cursor_projection is not None:
                        after_projection = self._compact_applied_after_card_logs_projection(row, card_id)
                        if after_projection is not None and after_projection == cursor_projection:
                            predecessors.append((row, True))
                            continue
                    if str(row["after_json"]) == cursor_json and str(row["after_hash"]) == cursor_hash:
                        predecessors.append((row, False))
                if len(predecessors) != 1:
                    code = "compact_card_chain_ambiguous" if len(predecessors) > 1 else "compact_card_chain_mismatch"
                    raise ReceiptRegressionError("same-card applied history is not a unique after chain", code)
                previous, projected = predecessors[0]
                used_card_projection = used_card_projection or projected
                if previous["before_json"] is None or previous["before_hash"] is None:
                    raise ReceiptRegressionError("same-card applied history is incomplete", "compact_receipt_invalid")
                chain_rows.append(previous)
                cursor_json = str(previous["before_json"])
                cursor_hash = str(previous["before_hash"])
                remaining.remove(previous)

            if frozen_baseline is not None:
                snapshot_record, context, native_plan = frozen_baseline
                # 有后续同卡收据时从最早 before 核验冻结卡态；否则核验本次 raw before。
                anchor = json.loads(chain_rows[-1]['plan_json']) if chain_rows else native_plan
                from ..anki.compact_projection import resolve_snapshot_card, verify_card_snapshot, CompactProjectionError
                try:
                    frozen_card, _, _ = resolve_snapshot_card(dict(event), json.loads(snapshot_record.payload_json))
                    anchor_card = dict(anchor['before']['card'])
                    source = context.source.card_last_review_time_sources.get(str(event['card_id']))
                    if source is not None:
                        from ..anki.application_context import last_review_time_source_from_rows
                        # Compact 旧帧以 0 表示缺失；真实 optional 状态另由 native_card_hex 核验。
                        if anchor_card.get('last_review_time') == 0:
                            anchor_card['last_review_time'] = None
                        actual_source = last_review_time_source_from_rows(
                            anchor_card.get('last_review_time'), anchor['before']['native_revlogs'])
                        if actual_source != dict(source):
                            raise ValueError('frozen last review source changed')
                    if anchor_card.get('type') == 0 and anchor_card.get('last_review_time') == 0:
                        anchor_card['last_review_time'] = None
                    verify_card_snapshot(anchor_card, frozen_card, last_review_time_source=source)
                except (CompactProjectionError, KeyError, TypeError, ValueError) as exc:
                    raise ReceiptRegressionError('native before is not the frozen snapshot card', 'compact_snapshot_baseline_invalid') from exc
                proof_json = native_plan.get("policy", {}).get("baseline_proof_json")
                if proof_json is not None and json.loads(policy).get("authorization") != native_plan.get("policy"):
                    raise ReceiptRegressionError("compatibility policy differs from plan", "compact_snapshot_baseline_invalid")
                if proof_json is not None:
                    from ..anki.compact_projection import json_sha256
                    history_hash = context.source.card_review_log_hashes.get(str(event["card_id"]))
                    if history_hash is None:
                        history_hash = context.source.card_last_review_time_sources.get(str(event["card_id"]), {}).get("revlog_sha256")
                    if history_hash is None or json_sha256(anchor["before"].get("native_revlogs")) != history_hash:
                        raise ReceiptRegressionError("frozen review history is not proven", "compact_snapshot_baseline_invalid")
                    if chain_rows:
                        latest = chain_rows[0]
                        evidence = {row["stage"]: json.loads(row["evidence_json"]) for row in db.execute(
                            "SELECT stage,evidence_json FROM compact_score_evidence WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                            (self._profile_id, latest["device_id"], latest["session_id"], latest["sequence"]),
                        ).fetchall()}
                        final = evidence.get("score_verified", {}).get("native_observed")
                        if final is None:
                            final = evidence.get("recovery_classification", {}).get("verification", {}).get("final_native_readback")
                        if not isinstance(final, Mapping) or not isinstance(final.get("native_revlogs"), list) or final["native_revlogs"] != native_plan["before"].get("native_revlogs"):
                            raise ReceiptRegressionError("latest receipt history changed", "compact_snapshot_baseline_invalid")
                compatibility_verified = self._require_snapshot_counter_path(
                    db, context, collection_generation, collection_identity, before, proof_json=proof_json,
                )

            if chain_rows and not compatibility_verified:
                if used_card_projection:
                    first_generation = min(int(row["collection_generation"]) for row in chain_rows)
                    self._require_compact_generation_receipt_path(
                        db,
                        first_generation=first_generation,
                        target_generation=collection_generation,
                        collection_identity=collection_identity,
                        latest_same_card_after_json=str(chain_rows[0]["after_json"]),
                        target_before_json=before,
                    )
                else:
                    expected_generation = collection_generation
                    for previous in chain_rows:
                        if (
                            previous["collection_generation_after"] is None
                            or int(previous["collection_generation_after"]) != expected_generation
                            or str(previous["collection_identity"]) != collection_identity
                        ):
                            raise ReceiptRegressionError(
                                "same-card generation/identity chain changed", "generation_mismatch"
                            )
                        expected_generation = int(previous["collection_generation"])

            lock = db.execute(
                "SELECT source_kind,intent_id FROM card_apply_locks WHERE profile_id=? AND card_id=?",
                (self._profile_id, event["card_id"]),
            ).fetchone()
            if lock is not None:
                raise BusyStorageError("card already has an active apply claim", "card_apply_busy")
            db.execute(
                "INSERT INTO card_apply_locks(profile_id,card_id,source_kind,device_id,session_id,sequence,intent_id,acquired_at_ms) "
                "VALUES(?,?,'compact',?,?,?,?,?)",
                (self._profile_id, event["card_id"], device_id, session_id, sequence, intent, now),
            )
            db.execute(
                "UPDATE compact_applications SET status='applying',collection_identity=?,collection_generation=?,"
                "policy_json=?,before_json=?,before_hash=?,plan_json=?,expected_after_json=?,intent_id=?,attempt_id=?,"
                "reason=NULL,updated_at_ms=? WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (collection_identity, collection_generation, policy, before, before_hash, plan, expected_after,
                 intent, attempt, now, self._profile_id, device_id, session_id, sequence),
            )
            row = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone()
            if row is None:
                raise SchemaError("compact application disappeared", "compact_application_corrupt")
            # Re-parse here to reject a malformed/unsupported required-evidence list before scoring.
            if required_stages != _required_compact_stages(str(row["plan_json"])):
                raise SchemaError("compact plan changed during claim", "compact_plan_corrupt")
            return self._compact_application_record(row)

    def record_compact_score_evidence(
        self,
        device_id: str,
        session_id: str,
        sequence: int,
        *,
        stage: str,
        evidence_json: Any,
        now_ms: Optional[int] = None,
    ) -> CompactScoreEvidenceRecord:
        """持久化评分/日期修正阶段；重复同文幂等，异文会锁定为 conflict。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        sequence = _validate_sequence(sequence)
        stage = _validate_text(stage, "stage", 64)
        if stage not in {"score_started", "score_applied", "due_corrected", "score_verified"}:
            raise StorageValidationError("unsupported compact evidence stage", "compact_stage_invalid")
        evidence = _canonical_object_json(evidence_json, "evidence_json")
        digest = _json_hash(evidence)
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        conflict = False
        result: Optional[CompactScoreEvidenceRecord] = None
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            app = db.execute(
                "SELECT status FROM compact_applications WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone()
            if app is None:
                raise StorageValidationError("compact application does not exist", "compact_application_not_found")
            current = str(app["status"])
            old = db.execute(
                "SELECT * FROM compact_score_evidence WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=? AND stage=?",
                (self._profile_id, device_id, session_id, sequence, stage),
            ).fetchone()
            if old is not None:
                if str(old["evidence_hash"]) == digest and str(old["evidence_json"]) == evidence:
                    result = CompactScoreEvidenceRecord(
                        self._profile_id, device_id, session_id, sequence, stage,
                        evidence, digest, int(old["created_at_ms"]),
                    )
                else:
                    if current == "applied":
                        raise ReceiptRegressionError(
                            "terminal compact evidence is immutable", "compact_evidence_after_terminal"
                        )
                    conflict = True
                    conflict_stage = ("conflict-%s-%s" % (stage[:30], digest[:16]))[:64]
                    db.execute(
                        "INSERT OR IGNORE INTO compact_score_evidence(profile_id,device_id,session_id,sequence,stage,"
                        "evidence_json,evidence_hash,created_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                        (self._profile_id, device_id, session_id, sequence, conflict_stage, evidence, digest, now),
                    )
                    db.execute(
                        "UPDATE compact_applications SET status='conflict',reason='score_evidence_conflict',updated_at_ms=? "
                        "WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=? AND status!='applied'",
                        (now, self._profile_id, device_id, session_id, sequence),
                    )
            else:
                if current != "applying":
                    if current == "unknown":
                        raise UnknownApplyError("unknown compact application is not retryable", "unknown_compact_apply")
                    raise ReceiptRegressionError("compact application is not active", "compact_apply_not_active")
                if bool(db.execute(
                    "SELECT recovery_required FROM compact_applications WHERE profile_id=? AND device_id=? "
                    "AND session_id=? AND sequence=?",
                    (self._profile_id, device_id, session_id, sequence),
                ).fetchone()[0]):
                    recovery = db.execute(
                        "SELECT evidence_json FROM compact_score_evidence WHERE profile_id=? AND device_id=? "
                        "AND session_id=? AND sequence=? AND stage='recovery_classification'",
                        (self._profile_id, device_id, session_id, sequence),
                    ).fetchone()
                    try:
                        classification = json.loads(str(recovery["evidence_json"])).get("classification") if recovery else None
                    except (TypeError, ValueError, AttributeError):
                        classification = None
                    if classification != "not_started":
                        raise ReceiptRegressionError(
                            "compact recovery must be classified before writing score evidence",
                            "compact_recovery_classification_required",
                        )
                db.execute(
                    "INSERT INTO compact_score_evidence(profile_id,device_id,session_id,sequence,stage,evidence_json,evidence_hash,created_at_ms) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (self._profile_id, device_id, session_id, sequence, stage, evidence, digest, now),
                )
                result = CompactScoreEvidenceRecord(
                    self._profile_id, device_id, session_id, sequence, stage, evidence, digest, now
                )
        if conflict:
            raise ReceiptRegressionError("compact evidence changed for an existing stage", "compact_evidence_conflict")
        if result is None:
            raise SchemaError("compact evidence disappeared", "compact_evidence_corrupt")
        return result

    def _required_compact_evidence_present(
        self, db: sqlite3.Connection, app: sqlite3.Row
    ) -> bool:
        required = _required_compact_stages(str(app["plan_json"]))
        rows = db.execute(
            "SELECT stage,evidence_json,evidence_hash FROM compact_score_evidence WHERE profile_id=? AND device_id=? "
            "AND session_id=? AND sequence=?",
            (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
        ).fetchall()
        evidence = {str(row["stage"]): row for row in rows}
        if any(str(row["stage"]).startswith("conflict-") for row in rows):
            return False
        allowed = {"score_started", "score_applied", "due_corrected", "score_verified", "recovery_classification"}
        if any(str(row["stage"]) not in allowed for row in rows):
            return False
        if any(_json_hash(str(row["evidence_json"])) != str(row["evidence_hash"]) for row in rows):
            return False
        return all(
            stage in evidence
            and _json_hash(str(evidence[stage]["evidence_json"])) == str(evidence[stage]["evidence_hash"])
            for stage in required
        )

    @staticmethod
    def _compact_card_logs_projection(
        card: Any, review_logs: Any, card_id: str
    ) -> Tuple[str, Tuple[str, ...]]:
        """生成只含目标卡和该卡 review log 的规范投影。"""

        if not isinstance(card, Mapping) or str(card.get("card_id", card.get("id", ""))) != card_id:
            raise ReceiptRegressionError("compact card frame names another card", "compact_card_chain_mismatch")
        if not isinstance(review_logs, list):
            raise ReceiptRegressionError("compact card frame has no review log list", "compact_card_chain_mismatch")
        log_json: List[str] = []
        for review_log in review_logs:
            if not isinstance(review_log, Mapping) or str(review_log.get("card_id", "")) != card_id:
                raise ReceiptRegressionError("compact review log names another card", "compact_card_chain_mismatch")
            log_json.append(_canonical_object_json(review_log, "review_log"))
        return _canonical_object_json(card, "card"), tuple(sorted(log_json))

    @classmethod
    def _compact_full_frame_card_logs_projection(
        cls, state_json: str, card_id: str
    ) -> Optional[Tuple[str, Tuple[str, ...]]]:
        """只把精确的 native before 帧识别为可忽略 counters 的完整卡帧。"""

        try:
            state = json.loads(state_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(state, Mapping) or set(state) != {"card", "review_logs", "counters"}:
            return None
        if cls._compact_frame_counters(state_json) is None:
            return None
        return cls._compact_card_logs_projection(state.get("card"), state.get("review_logs"), card_id)

    @classmethod
    def _compact_applied_after_card_logs_projection(
        cls, app: sqlite3.Row, card_id: str
    ) -> Optional[Tuple[str, Tuple[str, ...]]]:
        """识别完整 after 帧，或从已知 native after 摘要和 before 日志重建投影。"""

        after_json = app["after_json"]
        if after_json is None:
            return None
        after_raw = str(after_json)
        complete = cls._compact_full_frame_card_logs_projection(after_raw, card_id)
        if complete is not None:
            return complete
        try:
            after = json.loads(after_raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(after, Mapping) or set(after) != {
            "card", "review_log_count", "score_review_log", "counters"
        }:
            return None
        if cls._compact_frame_counters(after_raw) is None:
            return None
        before_json = app["before_json"]
        if before_json is None:
            return None
        before_raw = str(before_json)
        before_projection = cls._compact_full_frame_card_logs_projection(before_raw, card_id)
        if before_projection is None:
            return None
        try:
            before = json.loads(before_raw)
        except (TypeError, ValueError):
            return None
        before_logs = before.get("review_logs")
        score_log = after.get("score_review_log")
        log_count = after.get("review_log_count")
        if (
            not isinstance(before_logs, list)
            or isinstance(log_count, bool)
            or not isinstance(log_count, int)
            or log_count != len(before_logs) + 1
            or not isinstance(score_log, Mapping)
            or str(score_log.get("card_id", "")) != card_id
        ):
            raise ReceiptRegressionError("compact native after log summary is invalid", "compact_card_chain_mismatch")
        card_json = _canonical_object_json(after.get("card"), "card") if isinstance(after.get("card"), Mapping) else None
        if card_json is None or str(after["card"].get("card_id", after["card"].get("id", ""))) != card_id:
            raise ReceiptRegressionError("compact native after names another card", "compact_card_chain_mismatch")
        score_log_json = _canonical_object_json(score_log, "review_log")
        if score_log_json in before_projection[1]:
            raise ReceiptRegressionError("compact native after duplicates its score log", "compact_card_chain_mismatch")
        return card_json, tuple(sorted((*before_projection[1], score_log_json)))

    @staticmethod
    def _compact_frame_counters(state_json: Any) -> Optional[Dict[str, str]]:
        """读取精确的 deck-id → counter-object map，不把分支键集压成一个整体 hash。"""

        if state_json is None:
            return None
        try:
            state = json.loads(str(state_json))
        except (TypeError, ValueError):
            return None
        counters = state.get("counters") if isinstance(state, Mapping) else None
        if not isinstance(counters, Mapping) or not counters:
            return None
        result: Dict[str, str] = {}
        for deck_id, value in counters.items():
            if (
                not isinstance(deck_id, str)
                or not deck_id.isascii()
                or not deck_id.isdecimal()
                or deck_id.startswith("0")
                or not isinstance(value, Mapping)
                or not value
            ):
                return None
            result[deck_id] = _canonical_object_json(value, "deck_counters")
        return result

    def _require_snapshot_counter_path(
        self,
        db: sqlite3.Connection,
        context: Any,
        generation: int,
        identity: str,
        before_json: str,
        *,
        proof_json: Optional[str] = None,
    ) -> bool:
        """独立重算冻结帧、完整终态收据和间隙依据，再比对实际评分前态。"""
        from ..anki.compact_projection import (
            resolve_counter_baseline,
            CompactProjectionError,
        )

        rows = db.execute(
            "SELECT * FROM compact_applications WHERE profile_id=? AND status='applied' "
            "AND collection_generation>=? AND collection_generation<?",
            (self._profile_id, context.source.collection_generation, generation),
        ).fetchall()
        edges = []
        for row in rows:
            self._require_valid_compact_terminal_receipt(db, row)
            edges.append(
                {
                    "generation": row["collection_generation"],
                    "after_generation": row["collection_generation_after"],
                    "identity": row["collection_identity"],
                    "before": json.loads(row["before_json"]).get("counters"),
                    "after": json.loads(row["after_json"]).get("counters"),
                    "receipt_hash": row["receipt_hash"],
                }
            )
        try:
            counters, expected_proof = resolve_counter_baseline(
                context, identity, generation, edges, allow_gaps=proof_json is not None
            )
            if proof_json is not None and (
                expected_proof is None
                or json.loads(proof_json) != json.loads(expected_proof)
            ):
                raise ValueError("compatibility proof differs")
        except (CompactProjectionError, ValueError, TypeError) as error:
            raise ReceiptRegressionError(
                "snapshot baseline proof is invalid",
                "compact_snapshot_baseline_invalid",
            ) from error
        current = self._compact_frame_counters(before_json)
        expected = {
            did: _canonical_object_json(value, "snapshot_counters")
            for did, value in counters.items()
        }
        if not current or any(
            did not in expected or expected[did] != value
            for did, value in current.items()
        ):
            raise ReceiptRegressionError(
                "native counters do not match frozen snapshot path",
                "compact_snapshot_baseline_invalid",
            )
        return expected_proof is not None

    def _require_compact_generation_receipt_path(
        self,
        db: sqlite3.Connection,
        *,
        first_generation: int,
        target_generation: int,
        collection_identity: str,
        latest_same_card_after_json: str,
        target_before_json: str,
    ) -> None:
        """验证每个全局 generation 边都有唯一、连续且 counters 连续的有效终态收据。"""

        if target_generation <= first_generation:
            raise ReceiptRegressionError("compact generation path is not forward", "generation_mismatch")
        rows = db.execute(
            "SELECT a.* FROM compact_events e JOIN compact_applications a ON a.profile_id=e.profile_id "
            "AND a.device_id=e.device_id AND a.session_id=e.session_id AND a.sequence=e.sequence "
            "WHERE e.profile_id=? AND a.status='applied' AND a.collection_generation>=? "
            "AND a.collection_generation<? ORDER BY a.collection_generation,a.device_id,a.session_id,a.sequence",
            (self._profile_id, first_generation, target_generation),
        ).fetchall()
        cursor = first_generation
        known_counters: Dict[str, str] = {}
        while cursor < target_generation:
            candidates = [row for row in rows if int(row["collection_generation"]) == cursor]
            if len(candidates) != 1:
                raise ReceiptRegressionError("compact generation path has a gap or fork", "generation_mismatch")
            app = candidates[0]
            if (
                str(app["collection_identity"]) != collection_identity
                or app["collection_generation_after"] is None
                or int(app["collection_generation_after"]) != cursor + 1
            ):
                raise ReceiptRegressionError("compact generation edge is discontinuous", "generation_mismatch")
            self._require_valid_compact_terminal_receipt(db, app)
            before_counters = self._compact_frame_counters(app["before_json"])
            after_counters = self._compact_frame_counters(app["after_json"])
            if (
                before_counters is None
                or after_counters is None
                or set(before_counters) != set(after_counters)
            ):
                raise ReceiptRegressionError("compact generation edge has no counter proof", "generation_mismatch")
            for deck_id, before_value in before_counters.items():
                if deck_id in known_counters and known_counters[deck_id] != before_value:
                    raise ReceiptRegressionError(
                        "compact generation counters are discontinuous", "generation_mismatch"
                    )
                if deck_id not in known_counters:
                    known_counters[deck_id] = before_value
            known_counters.update(after_counters)
            cursor += 1
        target_counters = self._compact_frame_counters(target_before_json)
        if target_counters is None:
            raise ReceiptRegressionError("compact current counters are incomplete", "generation_mismatch")
        latest_same_card_after = self._compact_frame_counters(latest_same_card_after_json)
        if latest_same_card_after is None or set(target_counters) != set(latest_same_card_after):
            raise ReceiptRegressionError("compact current counter scope changed", "generation_mismatch")
        if any(
            deck_id not in known_counters or known_counters[deck_id] != value
            for deck_id, value in target_counters.items()
        ):
            raise ReceiptRegressionError("compact current counters do not match the applied receipt path", "generation_mismatch")

    def _compact_terminal_receipt_valid(
        self,
        db: sqlite3.Connection,
        app: sqlite3.Row,
        *,
        event_id: str,
        event_hash: str,
    ) -> bool:
        """只认可绑定事件 hash、after、阶段证据及 receipt hash 的终态行。"""

        if (
            str(app["status"]) != "applied"
            or str(app["event_id"]) != event_id
            or str(app["payload_hash"]) != event_hash
            or app["collection_identity"] is None
            or app["collection_generation"] is None
            or app["collection_generation_after"] is None
            or app["policy_json"] is None
            or app["before_json"] is None
            or app["before_hash"] is None
            or app["after_json"] is None
            or app["after_hash"] is None
            or app["receipt_json"] is None
            or app["receipt_hash"] is None
            or not self._required_compact_evidence_present(db, app)
        ):
            return False
        try:
            evidence_rows = db.execute(
                "SELECT stage,evidence_hash FROM compact_score_evidence WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND sequence=? ORDER BY stage",
                (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
            ).fetchall()
            evidence_hashes = {str(row["stage"]): str(row["evidence_hash"]) for row in evidence_rows}
            expected = {
                "event_id": event_id,
                "payload_hash": event_hash,
                "collection_identity": str(app["collection_identity"]),
                "collection_generation": int(app["collection_generation"]),
                "collection_generation_after": int(app["collection_generation_after"]),
                "policy_hash": _json_hash(str(app["policy_json"])),
                "before_hash": str(app["before_hash"]),
                "after_hash": str(app["after_hash"]),
                "evidence_hashes": evidence_hashes,
            }
            expected_json = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return (
                str(app["receipt_json"]) == expected_json
                and str(app["receipt_hash"]) == _json_hash(expected_json)
                and str(app["before_hash"]) == _json_hash(str(app["before_json"]))
                and str(app["after_hash"]) == _json_hash(str(app["after_json"]))
            )
        except (TypeError, ValueError, OverflowError, sqlite3.Error):
            return False

    def _require_valid_compact_terminal_receipt(
        self, db: sqlite3.Connection, app: sqlite3.Row
    ) -> None:
        """终态幂等返回前，重新核对收据、事件和所有评分阶段证据。"""

        event = db.execute(
            "SELECT event_id,payload_hash FROM compact_events WHERE profile_id=? AND device_id=? "
            "AND session_id=? AND sequence=?",
            (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
        ).fetchone()
        if event is None or not self._compact_terminal_receipt_valid(
            db,
            app,
            event_id=str(event["event_id"]),
            event_hash=str(event["payload_hash"]),
        ):
            raise ReceiptRegressionError(
                "terminal compact receipt or evidence is invalid", "compact_receipt_invalid"
            )

    def compact_applied_watermark(self, device_id: str, session_id: str) -> Optional[int]:
        """只计算本 compact 流中连续且已核验终态收据的 watermark。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)

        def read(db: sqlite3.Connection) -> Optional[int]:
            self._require_session(db, device_id, session_id)
            if db.execute(
                "SELECT 1 FROM compact_conflicts WHERE profile_id=? AND device_id=? AND session_id=? LIMIT 1",
                (self._profile_id, device_id, session_id),
            ).fetchone() is not None:
                return None
            rows = db.execute(
                "SELECT e.sequence,e.event_id AS event_event_id,e.payload_hash AS event_hash,a.* "
                "FROM compact_events e LEFT JOIN compact_applications a ON a.profile_id=e.profile_id "
                "AND a.device_id=e.device_id AND a.session_id=e.session_id AND a.sequence=e.sequence "
                "WHERE e.profile_id=? AND e.device_id=? AND e.session_id=? ORDER BY e.sequence",
                (self._profile_id, device_id, session_id),
            ).fetchall()
            if not rows:
                return None
            sequences = [int(row["sequence"]) for row in rows]
            if sequences != list(range(1, sequences[-1] + 1)):
                return None
            for row in rows:
                if row["status"] is None or not self._compact_terminal_receipt_valid(
                    db, row, event_id=str(row["event_event_id"]), event_hash=str(row["event_hash"])
                ):
                    return None
            return sequences[-1]

        return self._read(read)

    def classify_compact_recovery(
        self,
        intent_id: str,
        *,
        classification: str,
        verification_json: Any,
        observed_before_json: Optional[Any] = None,
        score_after_json: Optional[Any] = None,
        final_after_json: Optional[Any] = None,
        collection_generation_after: Optional[int] = None,
        now_ms: Optional[int] = None,
    ) -> CompactApplicationRecord:
        """持久化应用层恢复分类；不调用 Anki，也不自动重评分。"""

        intent_id = _validate_text(intent_id, "intent_id", 128)
        if classification not in {"not_started", "score_complete", "ambiguous", "conflict"}:
            raise StorageValidationError("invalid compact recovery classification", "compact_recovery_invalid")
        verification = _canonical_object_json(verification_json, "verification_json")
        before = _canonical_object_json(observed_before_json, "observed_before_json") if observed_before_json is not None else None
        score_after = _canonical_object_json(score_after_json, "score_after_json") if score_after_json is not None else None
        final_after = _canonical_object_json(final_after_json, "final_after_json") if final_after_json is not None else None
        if collection_generation_after is not None:
            collection_generation_after = _validate_nonnegative_safe(
                collection_generation_after, "collection_generation_after"
            )
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")

        with self._transaction() as db:
            app = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                (self._profile_id, intent_id),
            ).fetchone()
            if app is None:
                raise StorageValidationError("compact intent does not exist", "compact_intent_not_found")
            current = str(app["status"])
            if current == "applied":
                raise ReceiptRegressionError("applied compact intent is immutable", "compact_apply_regression")
            if current == "conflict":
                return self._compact_application_record(app)
            if current not in {"applying", "unknown"}:
                raise ReceiptRegressionError("compact intent is not recoverable", "compact_recovery_invalid")
            if current == "applying" and not bool(app["recovery_required"]):
                raise ReceiptRegressionError("compact intent has no pending recovery classification", "compact_recovery_invalid")
            claim = db.execute(
                "SELECT 1 FROM card_apply_locks WHERE profile_id=? AND source_kind='compact' AND intent_id=? "
                "AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, intent_id, app["device_id"], app["session_id"], app["sequence"]),
            ).fetchone()
            if claim is None:
                raise SchemaError("recoverable compact claim is missing", "compact_claim_missing")
            generation = db.execute(
                "SELECT collection_generation FROM profile_meta WHERE profile_id=?",
                (self._profile_id,),
            ).fetchone()
            if generation is None or int(generation["collection_generation"]) != int(app["collection_generation"]):
                db.execute(
                    "UPDATE compact_applications SET status='conflict',recovery_required=0,"
                    "reason='external_generation_change',updated_at_ms=? WHERE profile_id=? AND intent_id=?",
                    (now, self._profile_id, intent_id),
                )
                result = db.execute(
                    "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                    (self._profile_id, intent_id),
                ).fetchone()
                if result is None:
                    raise SchemaError("compact application disappeared", "compact_application_corrupt")
                return self._compact_application_record(result)

            try:
                verification_object = json.loads(verification)
                verification_context = {
                    "profile_id": self._profile_id,
                    "device_id": str(app["device_id"]),
                    "session_id": str(app["session_id"]),
                    "sequence": int(app["sequence"]),
                    "event_id": str(app["event_id"]),
                    "attempt_id": str(app["attempt_id"]),
                    "collection_identity": str(app["collection_identity"]),
                    "collection_generation": int(app["collection_generation"]),
                    "plan_hash": _json_hash(str(app["plan_json"])),
                }
                context_matches = (
                    isinstance(verification_object, dict)
                    and all(verification_object.get(key) == value for key, value in verification_context.items())
                    and isinstance(verification_object.get("source"), str)
                    and bool(verification_object["source"])
                    and any(verification_object.get(key) for key in ("revlog_id", "marker_id", "proof_id"))
                )
            except (TypeError, ValueError, KeyError):
                verification_object = {}
                context_matches = False
            if not context_matches:
                classification = "conflict"

            if classification in {"not_started", "score_complete"} and before != str(app["before_json"]):
                classification = "conflict"
            if classification == "score_complete":
                if score_after is None or final_after is None or final_after != str(app["expected_after_json"]):
                    classification = "conflict"
                else:
                    try:
                        expected_score = json.loads(str(app["plan_json"])).get("expected_score_after")
                    except (TypeError, ValueError, AttributeError):
                        expected_score = None
                    if expected_score is not None and _canonical_object_json(expected_score, "expected_score_after") != score_after:
                        classification = "conflict"

            classification_payload = {
                "classification": classification,
                "verification": verification_object,
                "verification_context_matches": context_matches,
                "observed_before_hash": _json_hash(before) if before is not None else None,
                "score_after_hash": _json_hash(score_after) if score_after is not None else None,
                "final_after_hash": _json_hash(final_after) if final_after is not None else None,
            }
            classification_json = _canonical_object_json(classification_payload, "recovery_classification")
            classification_hash = _json_hash(classification_json)
            old = db.execute(
                "SELECT evidence_json,evidence_hash FROM compact_score_evidence WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND sequence=? AND stage='recovery_classification'",
                (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
            ).fetchone()
            if old is not None and (
                str(old["evidence_json"]) != classification_json
                or str(old["evidence_hash"]) != classification_hash
            ):
                db.execute(
                    "UPDATE compact_applications SET status='conflict',recovery_required=0,"
                    "reason='recovery_evidence_conflict',updated_at_ms=? WHERE profile_id=? AND intent_id=?",
                    (now, self._profile_id, intent_id),
                )
                result = db.execute(
                    "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                    (self._profile_id, intent_id),
                ).fetchone()
                if result is None:
                    raise SchemaError("compact application disappeared", "compact_application_corrupt")
                return self._compact_application_record(result)
            if old is None:
                db.execute(
                    "INSERT INTO compact_score_evidence(profile_id,device_id,session_id,sequence,stage,evidence_json,evidence_hash,created_at_ms) "
                    "VALUES(?,?,?,?,'recovery_classification',?,?,?)",
                    (self._profile_id, app["device_id"], app["session_id"], app["sequence"], classification_json, classification_hash, now),
                )

            if classification == "ambiguous":
                db.execute(
                    "UPDATE compact_applications SET status='unknown',recovery_required=0,"
                    "reason='recovery_ambiguous',updated_at_ms=? WHERE profile_id=? AND intent_id=?",
                    (now, self._profile_id, intent_id),
                )
            elif classification == "conflict":
                db.execute(
                    "UPDATE compact_applications SET status='conflict',recovery_required=0,"
                    "reason='recovery_conflict',updated_at_ms=? WHERE profile_id=? AND intent_id=?",
                    (now, self._profile_id, intent_id),
                )
            elif classification == "not_started":
                next_status = "applying" if current == "applying" else "unknown"
                db.execute(
                    "UPDATE compact_applications SET status=?,recovery_required=0,reason=?,updated_at_ms=? "
                    "WHERE profile_id=? AND intent_id=?",
                    (next_status, "recovery_proved_not_started" if next_status == "applying" else "recovery_unknown_not_replayed",
                     now, self._profile_id, intent_id),
                )
            else:  # score_complete: fill only missing evidence, then write terminal receipt.
                required = _required_compact_stages(str(app["plan_json"]))
                derived = {
                    "score_applied": {"recovery_classification_hash": classification_hash,
                                      "score_after_hash": _json_hash(str(score_after))},
                    "due_corrected": {"recovery_classification_hash": classification_hash,
                                      "final_after_hash": _json_hash(str(final_after))},
                    "score_verified": {"recovery_classification_hash": classification_hash,
                                       "final_after_hash": _json_hash(str(final_after))},
                }
                for stage in required:
                    existing = db.execute(
                        "SELECT evidence_json,evidence_hash FROM compact_score_evidence WHERE profile_id=? AND device_id=? "
                        "AND session_id=? AND sequence=? AND stage=?",
                        (self._profile_id, app["device_id"], app["session_id"], app["sequence"], stage),
                    ).fetchone()
                    if existing is not None:
                        if _json_hash(str(existing["evidence_json"])) != str(existing["evidence_hash"]):
                            db.execute(
                                "UPDATE compact_applications SET status='conflict',recovery_required=0,"
                                "reason='score_evidence_corrupt',updated_at_ms=? WHERE profile_id=? AND intent_id=?",
                                (now, self._profile_id, intent_id),
                            )
                            result = db.execute(
                                "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                                (self._profile_id, intent_id),
                            ).fetchone()
                            if result is None:
                                raise SchemaError("compact application disappeared", "compact_application_corrupt")
                            return self._compact_application_record(result)
                        continue
                    evidence_json = _canonical_object_json(derived[stage], "recovery_score_evidence")
                    db.execute(
                        "INSERT INTO compact_score_evidence(profile_id,device_id,session_id,sequence,stage,evidence_json,evidence_hash,created_at_ms) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (self._profile_id, app["device_id"], app["session_id"], app["sequence"],
                         stage, evidence_json, _json_hash(evidence_json), now),
                    )
                db.execute(
                    "UPDATE compact_applications SET status='applying',recovery_required=0,reason=NULL,updated_at_ms=? "
                    "WHERE profile_id=? AND intent_id=?",
                    (now, self._profile_id, intent_id),
                )
                refreshed = db.execute(
                    "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                    (self._profile_id, intent_id),
                ).fetchone()
                event = db.execute(
                    "SELECT * FROM compact_events WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                    (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
                ).fetchone()
                if refreshed is None or event is None or str(event["payload_hash"]) != str(app["payload_hash"]):
                    raise SchemaError("compact event identity changed", "compact_application_corrupt")
                generation_after = (
                    int(app["collection_generation"])
                    if collection_generation_after is None else collection_generation_after
                )
                if generation_after < int(app["collection_generation"]):
                    raise StorageValidationError("collection generation cannot move backwards", "generation_mismatch")
                self._write_compact_terminal(db, refreshed, event, str(final_after), generation_after, now)

            result = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                (self._profile_id, intent_id),
            ).fetchone()
            if result is None:
                raise SchemaError("compact application disappeared", "compact_application_corrupt")
            return self._compact_application_record(result)

    def _write_compact_terminal(
        self, db: sqlite3.Connection, app: sqlite3.Row, event: sqlite3.Row,
        after: str, generation_after: int, now: int,
    ) -> sqlite3.Row:
        """在当前事务中写经过比对的终态收据并释放同一张卡的 claim。"""

        after_hash = _json_hash(after)
        evidence_rows = db.execute(
            "SELECT stage,evidence_hash,evidence_json FROM compact_score_evidence WHERE profile_id=? "
            "AND device_id=? AND session_id=? AND sequence=? ORDER BY stage",
            (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
        ).fetchall()
        evidence_hashes: Dict[str, str] = {}
        for evidence in evidence_rows:
            stage = str(evidence["stage"])
            evidence_hash = str(evidence["evidence_hash"])
            if stage.startswith("conflict-") or _json_hash(str(evidence["evidence_json"])) != evidence_hash:
                raise ReceiptRegressionError("compact evidence is conflicted or corrupt", "compact_evidence_conflict")
            evidence_hashes[stage] = evidence_hash
        receipt = json.dumps(
            {
                "event_id": str(event["event_id"]),
                "payload_hash": str(event["payload_hash"]),
                "collection_identity": str(app["collection_identity"]),
                "collection_generation": int(app["collection_generation"]),
                "collection_generation_after": generation_after,
                "policy_hash": _json_hash(str(app["policy_json"])),
                "before_hash": str(app["before_hash"]),
                "after_hash": after_hash,
                "evidence_hashes": evidence_hashes,
            },
            ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        )
        receipt_hash = _json_hash(receipt)
        db.execute(
            "UPDATE compact_applications SET status='applied',collection_generation_after=?,after_json=?,after_hash=?,receipt_json=?,receipt_hash=?,"
            "reason=NULL,updated_at_ms=? WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
            (generation_after, after, after_hash, receipt, receipt_hash, now, self._profile_id,
             app["device_id"], app["session_id"], app["sequence"]),
        )
        if generation_after != int(app["collection_generation"]):
            db.execute(
                "UPDATE profile_meta SET collection_generation=?,updated_at_ms=MAX(updated_at_ms,?) WHERE profile_id=? "
                "AND collection_generation=?",
                (generation_after, now, self._profile_id, app["collection_generation"]),
            )
            db.execute(
                "UPDATE permits SET status='revoked' WHERE profile_id=? AND status='issued'",
                (self._profile_id,),
            )
        lock = db.execute(
            "SELECT 1 FROM card_apply_locks WHERE profile_id=? AND source_kind='compact' AND intent_id=? "
            "AND device_id=? AND session_id=? AND sequence=?",
            (self._profile_id, app["intent_id"], app["device_id"], app["session_id"], app["sequence"]),
        ).fetchone()
        if lock is None:
            raise SchemaError("compact card claim is missing", "compact_claim_missing")
        db.execute(
            "DELETE FROM card_apply_locks WHERE profile_id=? AND source_kind='compact' AND intent_id=? "
            "AND device_id=? AND session_id=? AND sequence=?",
            (self._profile_id, app["intent_id"], app["device_id"], app["session_id"], app["sequence"]),
        )
        result = db.execute(
            "SELECT * FROM compact_applications WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
            (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
        ).fetchone()
        if result is None:
            raise SchemaError("compact application disappeared", "compact_application_corrupt")
        return result

    def finish_compact_apply(
        self, intent_id: str, *, after_json: Any,
        collection_generation_after: Optional[int] = None,
        now_ms: Optional[int] = None,
    ) -> CompactApplicationRecord:
        """核对计划 after 与阶段证据，在一个 bridge DB 事务内写 applied 收据。"""

        intent_id = _validate_text(intent_id, "intent_id", 128)
        after = _canonical_object_json(after_json, "after_json")
        if collection_generation_after is not None:
            collection_generation_after = _validate_nonnegative_safe(
                collection_generation_after, "collection_generation_after"
            )
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            app = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                (self._profile_id, intent_id),
            ).fetchone()
            if app is None:
                raise StorageValidationError("compact intent does not exist", "compact_intent_not_found")
            if str(app["status"]) == "applied":
                self._require_valid_compact_terminal_receipt(db, app)
                if str(app["after_json"]) != after:
                    raise ReceiptRegressionError("terminal compact after is immutable", "compact_after_conflict")
                if (collection_generation_after is not None
                        and int(app["collection_generation_after"]) != collection_generation_after):
                    raise ReceiptRegressionError("terminal compact generation is immutable", "compact_after_conflict")
                return self._compact_application_record(app)
            if str(app["status"]) == "unknown":
                raise UnknownApplyError("unknown compact application requires reconciliation", "unknown_compact_apply")
            if str(app["status"]) != "applying":
                raise ReceiptRegressionError("compact intent is not active", "compact_apply_not_active")
            if bool(app["recovery_required"]):
                raise ReceiptRegressionError(
                    "compact recovery must be classified before finish", "compact_recovery_classification_required"
                )
            event = db.execute(
                "SELECT * FROM compact_events WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
            ).fetchone()
            if event is None or str(event["event_id"]) != str(app["event_id"]) or str(event["payload_hash"]) != str(app["payload_hash"]):
                raise SchemaError("compact event identity changed", "compact_application_corrupt")
            if after != str(app["expected_after_json"]):
                raise ReceiptRegressionError("observed compact after differs from the persisted plan", "compact_after_mismatch")
            if not self._required_compact_evidence_present(db, app):
                raise ReceiptRegressionError("required compact score evidence is missing", "compact_evidence_missing")
            generation_row = db.execute(
                "SELECT collection_generation FROM profile_meta WHERE profile_id=?",
                (self._profile_id,),
            ).fetchone()
            if generation_row is None or int(generation_row["collection_generation"]) != int(app["collection_generation"]):
                raise ProfileMismatchError("collection generation changed during apply", "generation_mismatch")
            generation_after = (
                int(app["collection_generation"])
                if collection_generation_after is None else collection_generation_after
            )
            if generation_after < int(app["collection_generation"]):
                raise StorageValidationError("collection generation cannot move backwards", "generation_mismatch")
            result = self._write_compact_terminal(db, app, event, after, generation_after, now)
            return self._compact_application_record(result)

    def reconcile_compact_apply(
        self, intent_id: str, *, observed_after_json: Any, now_ms: Optional[int] = None
    ) -> CompactApplicationRecord:
        """只核验现有 native 状态，不重放 UNKNOWN；无法唯一证明时保留阻塞。"""

        intent_id = _validate_text(intent_id, "intent_id", 128)
        observed = _canonical_object_json(observed_after_json, "observed_after_json")
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        with self._transaction() as db:
            app = db.execute(
                "SELECT * FROM compact_applications WHERE profile_id=? AND intent_id=?",
                (self._profile_id, intent_id),
            ).fetchone()
            if app is None:
                raise StorageValidationError("compact intent does not exist", "compact_intent_not_found")
            status = str(app["status"])
            if status == "applied":
                self._require_valid_compact_terminal_receipt(db, app)
                if observed != str(app["after_json"]):
                    raise ReceiptRegressionError("verified compact after changed", "compact_after_conflict")
                return self._compact_application_record(app)
            if status not in {"unknown", "conflict"}:
                raise ReceiptRegressionError("only terminal uncertainty can be reconciled", "compact_reconcile_invalid")
            if status == "conflict":
                return self._compact_application_record(app)
            if observed == str(app["before_json"]):
                # Exact before proves no state change, but policy forbids replaying UNKNOWN.
                return self._compact_application_record(app)
            if observed != str(app["expected_after_json"]):
                db.execute(
                    "UPDATE compact_applications SET status='conflict',reason='reconcile_mismatch',updated_at_ms=? "
                    "WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                    (now, self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
                )
                row = db.execute(
                    "SELECT * FROM compact_applications WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                    (self._profile_id, app["device_id"], app["session_id"], app["sequence"]),
                ).fetchone()
                if row is None:
                    raise SchemaError("compact application disappeared", "compact_application_corrupt")
                return self._compact_application_record(row)
            # Matching state alone is not source evidence; only explicit recovery classification
            # from the application layer may finish an UNKNOWN attempt.
            return self._compact_application_record(app)

    def receive_batch(
        self,
        device_id: str,
        session_id: str,
        events: Sequence[Any],
        *,
        now_ms: Optional[int] = None,
    ) -> Tuple[Receipt, ...]:
        """以单事务接收有序事件；只有提交成功才返回 durable receipts。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        if not isinstance(events, Sequence) or isinstance(
            events, (str, bytes, bytearray)
        ):
            raise StorageValidationError(
                "events must be a sequence", "invalid_event_batch"
            )
        if not events:
            raise StorageValidationError(
                "event batch must not be empty", "empty_event_batch"
            )
        prepared = [_prepare_event(event, session_id) for event in events]
        sequences = [event.sequence for event in prepared]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise SequenceError("event batch is not strictly ordered", "sequence_order")
        now = _now_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise StorageValidationError("invalid timestamp", "invalid_timestamp")
        receipts: List[Receipt] = []
        with self._transaction() as db:
            session = self._require_session(db, device_id, session_id)
            high_water = int(session["high_water"])
            next_new = high_water + 1
            highest_new = high_water
            existing_rows: Dict[int, sqlite3.Row] = {}
            invalidates_pull = False
            for item in prepared:
                row = db.execute(
                    "SELECT e.*,d.status,d.reason AS delivery_reason FROM events e "
                    "JOIN deliveries d ON d.profile_id=e.profile_id AND d.device_id=e.device_id "
                    "AND d.session_id=e.session_id AND d.sequence=e.sequence "
                    "WHERE e.profile_id=? AND e.device_id=? AND e.session_id=? AND e.sequence=?",
                    (self._profile_id, device_id, session_id, item.sequence),
                ).fetchone()
                if row is not None:
                    existing_rows[item.sequence] = row
                    if row["event_id"] != item.event_id:
                        raise StorageValidationError(
                            "stored sequence has another event id", "sequence_conflict"
                        )
                    continue
                if item.sequence <= high_water:
                    raise SequenceError(
                        "sequence is missing below the session waterline",
                        "sequence_gap",
                    )
                if item.sequence != next_new:
                    raise SequenceError("event sequence has a gap", "sequence_gap")
                next_new += 1
                highest_new = item.sequence

            for item in prepared:
                row = existing_rows.get(item.sequence)
                if row is not None:
                    if row["payload_hash"] == item.payload_hash:
                        receipts.append(
                            Receipt(
                                item.event_id,
                                item.sequence,
                                str(row["status"]),
                                str(row["payload_hash"]),
                                row["delivery_reason"],
                            )
                        )
                        continue
                    self._record_conflict(db, device_id, session_id, item, now)
                    invalidates_pull = True
                    receipts.append(
                        Receipt(
                            item.event_id,
                            item.sequence,
                            "conflict",
                            item.payload_hash,
                            "same_sequence_different_payload",
                        )
                    )
                    continue
                db.execute(
                    "INSERT INTO events(profile_id,device_id,session_id,sequence,event_id,payload_hash,"
                    "raw_payload_json,received_at_ms,updated_at_ms,normalized_payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._profile_id,
                        device_id,
                        session_id,
                        item.sequence,
                        item.event_id,
                        item.payload_hash,
                        item.raw_payload_json,
                        now,
                        now,
                        item.normalized_payload_json,
                    ),
                )
                db.execute(
                    "INSERT INTO deliveries(profile_id,device_id,session_id,sequence,event_id,status,reason,updated_at_ms) "
                    "VALUES(?,?,?,?,?,'received',NULL,?)",
                    (
                        self._profile_id,
                        device_id,
                        session_id,
                        item.sequence,
                        item.event_id,
                        now,
                    ),
                )
                db.execute(
                    "INSERT INTO applications(profile_id,device_id,session_id,sequence,event_id,status,"
                    "intent_id,attempt_id,reason,updated_at_ms) VALUES(?,?,?,?,?,'pending_apply',NULL,NULL,NULL,?)",
                    (
                        self._profile_id,
                        device_id,
                        session_id,
                        item.sequence,
                        item.event_id,
                        now,
                    ),
                )
                invalidates_pull = True
                receipts.append(
                    Receipt(
                        item.event_id,
                        item.sequence,
                        "received",
                        item.payload_hash,
                        None,
                    )
                )

            if highest_new > high_water:
                db.execute(
                    "UPDATE sessions SET high_water=?,last_seen_at_ms=? WHERE profile_id=? AND device_id=? AND session_id=?",
                    (highest_new, now, self._profile_id, device_id, session_id),
                )
            if invalidates_pull:
                db.execute(
                    "UPDATE permits SET status='revoked' WHERE profile_id=? AND device_id=? "
                    "AND session_id=? AND status='issued'",
                    (self._profile_id, device_id, session_id),
                )
        return tuple(receipts)

    def _record_conflict(
        self,
        db: sqlite3.Connection,
        device_id: str,
        session_id: str,
        item: _PreparedEvent,
        now: int,
    ) -> None:
        """把异文证据写入独立 conflict 表，不改写原始 events 行。"""

        db.execute(
            "INSERT OR IGNORE INTO event_conflicts(profile_id,device_id,session_id,sequence,event_id,"
            "observed_payload_hash,observed_payload_json,reason,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                self._profile_id,
                device_id,
                session_id,
                item.sequence,
                item.event_id,
                item.payload_hash,
                item.raw_payload_json,
                "same_sequence_different_payload",
                now,
            ),
        )

    def count_events(self) -> int:
        """返回当前 profile 的不可变事件行数。"""

        return int(
            self._read(
                lambda db: db.execute(
                    "SELECT count(*) FROM events WHERE profile_id=?",
                    (self._profile_id,),
                ).fetchone()[0]
            )
        )

    def get_event(self, device_id: str, session_id: str, sequence: int) -> StoredEvent:
        """读取一个事件的持久原文和规范 hash；状态不混入 hash。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        sequence = _validate_sequence(sequence)

        def read(db: sqlite3.Connection) -> StoredEvent:
            row = db.execute(
                "SELECT * FROM events WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone()
            if row is None:
                raise StorageValidationError("event does not exist", "event_not_found")
            normalized = str(row["normalized_payload_json"])
            decoded = json.loads(normalized)
            return StoredEvent(
                self._profile_id,
                device_id,
                session_id,
                sequence,
                str(row["event_id"]),
                str(row["payload_hash"]),
                dict(decoded["payload"]),
                str(row["raw_payload_json"]),
                normalized,
            )

        return self._read(read)

    def query_receipts(
        self,
        device_id: str,
        session_id: str,
        *,
        event_ids: Optional[Sequence[str]] = None,
        after_sequence: Optional[int] = None,
    ) -> List[Receipt]:
        """查询 durable delivery receipt，与应用状态查询保持独立。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        if after_sequence is not None:
            after_sequence = _validate_sequence(after_sequence)
        ids = None
        if event_ids is not None:
            if isinstance(event_ids, (str, bytes, bytearray)):
                raise StorageValidationError(
                    "event_ids must be a sequence", "invalid_event_ids"
                )
            ids = [_validate_text(event_id, "event_id", 256) for event_id in event_ids]

        def read(db: sqlite3.Connection) -> List[Receipt]:
            self._require_session(db, device_id, session_id)
            clauses = [
                "d.profile_id=?",
                "d.device_id=?",
                "d.session_id=?",
            ]
            params: List[Any] = [self._profile_id, device_id, session_id]
            if after_sequence is not None:
                clauses.append("d.sequence>?")
                params.append(after_sequence)
            if ids:
                placeholders = ",".join("?" for _ in ids)
                clauses.append("d.event_id IN (%s)" % placeholders)
                params.extend(ids)
            rows = db.execute(
                "SELECT d.event_id,d.sequence,d.status,d.reason,e.payload_hash FROM deliveries d "
                "JOIN events e ON e.profile_id=d.profile_id AND e.device_id=d.device_id "
                "AND e.session_id=d.session_id AND e.sequence=d.sequence WHERE %s ORDER BY d.sequence"
                % " AND ".join(clauses),
                params,
            ).fetchall()
            return [
                Receipt(
                    str(row["event_id"]),
                    int(row["sequence"]),
                    str(row["status"]),
                    str(row["payload_hash"]),
                    row["reason"],
                )
                for row in rows
            ]

        return self._read(read)

    def query_applications(
        self, device_id: str, session_id: str
    ) -> List[ApplicationRecord]:
        """查询 Anki 应用证据，绝不把 received 当作 applied。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)

        def read(db: sqlite3.Connection) -> List[ApplicationRecord]:
            self._require_session(db, device_id, session_id)
            rows = db.execute(
                "SELECT event_id,sequence,status,intent_id,attempt_id,reason FROM applications "
                "WHERE profile_id=? AND device_id=? AND session_id=? ORDER BY sequence",
                (self._profile_id, device_id, session_id),
            ).fetchall()
            return [
                ApplicationRecord(
                    str(row["event_id"]),
                    int(row["sequence"]),
                    str(row["status"]),
                    row["intent_id"],
                    row["attempt_id"],
                    row["reason"],
                )
                for row in rows
            ]

        return self._read(read)

    def pending_apply(self, device_id: str, session_id: str) -> List[ApplicationRecord]:
        """返回唯一可进入新 apply intent 的 pending_apply 事件，不包含 unknown。"""

        return [
            record
            for record in self.query_applications(device_id, session_id)
            if record.status == "pending_apply"
        ]

    def transition_event(
        self,
        device_id: str,
        session_id: str,
        sequence: int,
        status: str,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """按单向状态图推进 receipt/application，拒绝状态回退。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        sequence = _validate_sequence(sequence)
        if status not in _DELIVERY_STATUSES:
            raise StorageValidationError(
                "unknown delivery status", "invalid_delivery_status"
            )
        if reason is not None:
            reason = _validate_text(reason, "reason", 256)
        now = _now_ms()
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            row = db.execute(
                "SELECT status FROM deliveries WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                (self._profile_id, device_id, session_id, sequence),
            ).fetchone()
            if row is None:
                raise StorageValidationError("event does not exist", "event_not_found")
            current = str(row["status"])
            if current == status:
                return
            if status not in _ALLOWED_TRANSITIONS[current]:
                raise ReceiptRegressionError(
                    "delivery status transition is not monotonic", "receipt_regression"
                )
            db.execute(
                "UPDATE deliveries SET status=?,reason=?,updated_at_ms=? WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND sequence=?",
                (
                    status,
                    reason,
                    now,
                    self._profile_id,
                    device_id,
                    session_id,
                    sequence,
                ),
            )
            app_status = "pending_apply" if status == "pending_apply" else status
            if app_status not in _APPLICATION_STATUSES:
                app_status = "conflict" if status == "conflict" else "abandoned"
            db.execute(
                "UPDATE applications SET status=?,reason=?,updated_at_ms=? WHERE profile_id=? AND device_id=? "
                "AND session_id=? AND sequence=?",
                (
                    app_status,
                    reason,
                    now,
                    self._profile_id,
                    device_id,
                    session_id,
                    sequence,
                ),
            )

    def begin_apply(
        self,
        device_id: str,
        session_id: str,
        sequences: Sequence[int],
        *,
        intent_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> ApplyIntent:
        """创建显式 apply intent 并把选定 pending 事件标成 applying。"""

        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        if isinstance(sequences, (str, bytes, bytearray)) or not sequences:
            raise StorageValidationError(
                "sequences must be a non-empty sequence", "invalid_apply_range"
            )
        normalized_sequences = [_validate_sequence(value) for value in sequences]
        if normalized_sequences != sorted(normalized_sequences) or len(
            set(normalized_sequences)
        ) != len(normalized_sequences):
            raise SequenceError(
                "apply range is not strictly ordered", "apply_range_order"
            )
        intent = _validate_text(intent_id or uuid.uuid4().hex, "intent_id", 128)
        attempt = _validate_text(attempt_id or uuid.uuid4().hex, "attempt_id", 128)
        now = _now_ms()
        event_ids: List[str] = []
        card_claims: Dict[str, int] = {}
        with self._transaction() as db:
            self._require_session(db, device_id, session_id)
            rows = []
            for sequence in normalized_sequences:
                row = db.execute(
                    "SELECT a.event_id,a.status,e.normalized_payload_json FROM applications a "
                    "JOIN events e ON e.profile_id=a.profile_id AND e.device_id=a.device_id "
                    "AND e.session_id=a.session_id AND e.sequence=a.sequence "
                    "WHERE a.profile_id=? AND a.device_id=? AND a.session_id=? AND a.sequence=?",
                    (self._profile_id, device_id, session_id, sequence),
                ).fetchone()
                if row is None:
                    raise StorageValidationError(
                        "event does not exist", "event_not_found"
                    )
                if str(row["status"]) == "unknown":
                    raise UnknownApplyError(
                        "unknown application is not retryable", "unknown_apply"
                    )
                if str(row["status"]) != "pending_apply":
                    raise ReceiptRegressionError(
                        "event is not pending apply", "apply_not_pending"
                    )
                rows.append(row)
                event_ids.append(str(row["event_id"]))
                try:
                    payload = json.loads(str(row["normalized_payload_json"])).get("payload", {})
                    card_id = payload.get("card_id") if isinstance(payload, Mapping) else None
                except (TypeError, ValueError, AttributeError):
                    card_id = None
                if isinstance(card_id, int) and not isinstance(card_id, bool):
                    card_id = str(card_id)
                if isinstance(card_id, str) and card_id:
                    card_claims.setdefault(_validate_text(card_id, "card_id", 128), sequence)
            for card_id in card_claims:
                if db.execute(
                    "SELECT 1 FROM card_apply_locks WHERE profile_id=? AND card_id=?",
                    (self._profile_id, card_id),
                ).fetchone() is not None:
                    raise BusyStorageError("card already has an active apply claim", "card_apply_busy")
            db.execute(
                "INSERT INTO apply_intents(profile_id,intent_id,attempt_id,status,created_at_ms,updated_at_ms,reason) "
                "VALUES(?,?,?,'applying',?,?,NULL)",
                (self._profile_id, intent, attempt, now, now),
            )
            for sequence, row in zip(normalized_sequences, rows):
                db.execute(
                    "INSERT INTO apply_intent_events(profile_id,intent_id,device_id,session_id,sequence,event_id) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        self._profile_id,
                        intent,
                        device_id,
                        session_id,
                        sequence,
                        row["event_id"],
                    ),
                )
                db.execute(
                    "UPDATE applications SET status='applying',intent_id=?,attempt_id=?,reason=NULL,updated_at_ms=? "
                    "WHERE profile_id=? AND device_id=? AND session_id=? AND sequence=?",
                    (
                        intent,
                        attempt,
                        now,
                        self._profile_id,
                        device_id,
                        session_id,
                        sequence,
                    ),
                )
                db.execute(
                    "UPDATE deliveries SET status='applying',reason=NULL,updated_at_ms=? WHERE profile_id=? "
                    "AND device_id=? AND session_id=? AND sequence=?",
                    (now, self._profile_id, device_id, session_id, sequence),
                )
            for card_id, sequence in card_claims.items():
                db.execute(
                    "INSERT INTO card_apply_locks(profile_id,card_id,source_kind,device_id,session_id,sequence,intent_id,acquired_at_ms) "
                    "VALUES(?,?,'ordinary',?,?,?,?,?)",
                    (self._profile_id, card_id, device_id, session_id, sequence, intent, now),
                )
        return ApplyIntent(intent, attempt, "applying", tuple(event_ids), now, None)

    def finish_apply(
        self, intent_id: str, *, applied: bool, reason: Optional[str] = None
    ) -> ApplyIntent:
        """记录一次 apply 的明确成功或未知结果，不调用 Anki。"""

        intent_id = _validate_text(intent_id, "intent_id", 128)
        if not isinstance(applied, bool):
            raise StorageValidationError(
                "applied must be boolean", "invalid_apply_result"
            )
        if reason is not None:
            reason = _validate_text(reason, "reason", 256)
        now = _now_ms()
        status = "applied" if applied else "unknown"
        event_ids: List[str] = []
        attempt = ""
        created = now
        with self._transaction() as db:
            row = db.execute(
                "SELECT attempt_id,created_at_ms,status FROM apply_intents WHERE profile_id=? AND intent_id=?",
                (self._profile_id, intent_id),
            ).fetchone()
            if row is None:
                raise StorageValidationError(
                    "apply intent does not exist", "intent_not_found"
                )
            current = str(row["status"])
            if current != "applying":
                if current == "unknown":
                    raise UnknownApplyError(
                        "unknown application is not retryable", "unknown_apply"
                    )
                raise ReceiptRegressionError(
                    "apply intent is already terminal", "apply_regression"
                )
            attempt = str(row["attempt_id"])
            created = int(row["created_at_ms"])
            linked = db.execute(
                "SELECT device_id,session_id,sequence,event_id FROM apply_intent_events "
                "WHERE profile_id=? AND intent_id=? ORDER BY sequence",
                (self._profile_id, intent_id),
            ).fetchall()
            if not linked:
                raise SchemaError("apply intent has no linked events", "intent_corrupt")
            for link in linked:
                event_ids.append(str(link["event_id"]))
                db.execute(
                    "UPDATE applications SET status=?,reason=?,updated_at_ms=? WHERE profile_id=? AND device_id=? "
                    "AND session_id=? AND sequence=?",
                    (
                        status,
                        reason,
                        now,
                        self._profile_id,
                        link["device_id"],
                        link["session_id"],
                        link["sequence"],
                    ),
                )
                db.execute(
                    "UPDATE deliveries SET status=?,reason=?,updated_at_ms=? WHERE profile_id=? AND device_id=? "
                    "AND session_id=? AND sequence=?",
                    (
                        status,
                        reason,
                        now,
                        self._profile_id,
                        link["device_id"],
                        link["session_id"],
                        link["sequence"],
                    ),
                )
            db.execute(
                "UPDATE apply_intents SET status=?,reason=?,updated_at_ms=? WHERE profile_id=? AND intent_id=?",
                (status, reason, now, self._profile_id, intent_id),
            )
            if applied:
                db.execute(
                    "DELETE FROM card_apply_locks WHERE profile_id=? AND source_kind='ordinary' AND intent_id=?",
                    (self._profile_id, intent_id),
                )
        return ApplyIntent(
            intent_id, attempt, status, tuple(event_ids), created, reason
        )

    def list_conflicts(self) -> List[ConflictRecord]:
        """返回同键异文证据，不返回任何设备 token。"""

        def read(db: sqlite3.Connection) -> List[ConflictRecord]:
            rows = db.execute(
                "SELECT device_id,session_id,sequence,event_id,observed_payload_hash,reason,created_at_ms,"
                "observed_payload_json FROM event_conflicts WHERE profile_id=? "
                "ORDER BY device_id,session_id,sequence,created_at_ms",
                (self._profile_id,),
            ).fetchall()
            return [
                ConflictRecord(
                    str(row["device_id"]),
                    str(row["session_id"]),
                    int(row["sequence"]),
                    str(row["event_id"]),
                    str(row["observed_payload_hash"]),
                    str(row["reason"]),
                    int(row["created_at_ms"]),
                    str(row["observed_payload_json"]),
                )
                for row in rows
            ]

        return self._read(read)

    def backup_to(self, destination: Union[str, os.PathLike[str]]) -> Path:
        """使用 SQLite backup API 生成一致备份，覆盖 WAL 中的已提交内容。"""

        destination_path = Path(destination)
        if destination_path.resolve() == self.db_path.resolve():
            raise StorageValidationError(
                "backup destination must differ from source",
                "invalid_backup_destination",
            )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            if Path(str(destination_path) + suffix).exists():
                raise StorageError(
                    "backup destination has a SQLite sidecar", "backup_sidecar_exists"
                )
        fd, temporary_name = tempfile.mkstemp(
            prefix=".inkcards-backup-", dir=str(destination_path.parent)
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        source = self._connect()
        target: Optional[sqlite3.Connection] = None
        try:
            target = sqlite3.connect(str(temporary_path), isolation_level=None)
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("PRAGMA journal_mode=DELETE")
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise StorageError(
                    "backup integrity check failed", "backup_integrity_failed"
                )
            target.commit()
            target.close()
            target = None
            os.replace(str(temporary_path), str(destination_path))
            return destination_path
        except sqlite3.Error as error:
            raise _translate_sqlite_error(error) from error
        finally:
            source.close()
            if target is not None:
                target.close()
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def diagnostics(self) -> Dict[str, Any]:
        """导出脱敏诊断计数，不包含 token、secret、raw payload 或 payload hash。"""

        def read(db: sqlite3.Connection) -> Dict[str, Any]:
            def count(table: str) -> int:
                return int(
                    db.execute(
                        "SELECT count(*) FROM %s WHERE profile_id=?" % table,
                        (self._profile_id,),
                    ).fetchone()[0]
                )

            status_rows = db.execute(
                "SELECT status,count(*) AS n FROM deliveries WHERE profile_id=? GROUP BY status",
                (self._profile_id,),
            ).fetchall()
            application_rows = db.execute(
                "SELECT status,count(*) AS n FROM applications WHERE profile_id=? GROUP BY status",
                (self._profile_id,),
            ).fetchall()
            compact_application_rows = db.execute(
                "SELECT status,count(*) AS n FROM compact_applications WHERE profile_id=? GROUP BY status",
                (self._profile_id,),
            ).fetchall()
            return {
                "schema_version": SCHEMA_VERSION,
                "profile_id": self._profile_id,
                "root": str(self.root),
                "database": str(self.db_path),
                "devices": count("devices"),
                "sessions": count("sessions"),
                "events": count("events"),
                "conflicts": count("event_conflicts"),
                "delivery_status": {
                    str(row["status"]): int(row["n"]) for row in status_rows
                },
                "application_status": {
                    str(row["status"]): int(row["n"]) for row in application_rows
                },
                "compact_events": count("compact_events"),
                "compact_conflicts": count("compact_conflicts"),
                "compact_application_status": {
                    str(row["status"]): int(row["n"]) for row in compact_application_rows
                },
                "card_apply_claims": count("card_apply_locks"),
            }

        return self._read(read)
