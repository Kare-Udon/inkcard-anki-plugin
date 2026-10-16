"""x4-2 上传优先同步门槛的纯 Python domain。

本模块只描述可持久化状态、严格输入、状态转移和下一动作，不执行网络、文件、
SQLite、Anki 或设备 I/O。它是对已通过 Rust `sync_v2` 专项测试的行为迁移，
不是旧 RPC envelope、旧 Companion 或未来存储/HTTP/Anki 接线的替代品。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)


SYNC_V2_SCHEMA_VERSION = 1
LUA_SAFE_INTEGER_MAX = 9_007_199_254_740_991
MAX_PAGE_INDEX = 4_294_967_295


class _ValueEnum(str, Enum):
    """以协议中的 snake_case 值表示的枚举基类。"""

    def __str__(self) -> str:
        return self.value


class ChecksumStatus(_ValueEnum):
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    INVALID = "invalid"


class UploadOutcome(_ValueEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReceiptStatus(_ValueEnum):
    DURABLE_RECEIVED = "durable_received"
    APPLIED = "applied"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    ABANDONED = "abandoned"


class ApplicationState(_ValueEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPLIED = "applied"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class SyncPhase(_ValueEnum):
    IDLE = "idle"
    UPLOAD_REQUIRED = "upload_required"
    RECONCILE_RECEIPTS = "reconcile_receipts"
    PENDING_APPLY = "pending_apply"
    RECONCILE_APPLICATION = "reconcile_application"
    AWAITING_SNAPSHOT = "awaiting_snapshot"
    PULL_READY = "pull_ready"
    PULLING = "pulling"
    PULL_COMPLETE = "pull_complete"
    COMMITTED = "committed"
    OVERRIDE_PENDING_EXECUTION = "override_pending_execution"
    BLOCKED = "blocked"


class ApplyFenceStatus(_ValueEnum):
    QUIESCED = "quiesced"
    APPLYING = "applying"
    UNKNOWN = "unknown"


class OverrideTokenStatus(_ValueEnum):
    PREPARED = "prepared"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


class PullAbortReason(_ValueEnum):
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CONNECTION_LOST = "connection_lost"


class ErrorCode(_ValueEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_STATE = "invalid_state"
    INVALID_TRANSITION = "invalid_transition"
    DEVICE_MISMATCH = "device_mismatch"
    PROFILE_MISMATCH = "profile_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    GENERATION_NOT_ADVANCED = "generation_not_advanced"
    SEQUENCE_GAP = "sequence_gap"
    RECEIPT_MISSING = "receipt_missing"
    RECEIPT_UNKNOWN = "receipt_unknown"
    RECEIPT_CONFLICT = "receipt_conflict"
    RECEIPT_REGRESSION = "receipt_regression"
    UPLOAD_FAILED = "upload_failed"
    UPLOAD_PARTIAL = "upload_partial"
    UPLOAD_UNKNOWN = "upload_unknown"
    UPLOAD_CHECKSUM_INVALID = "upload_checksum_invalid"
    UPLOAD_CHECKSUM_UNKNOWN = "upload_checksum_unknown"
    APPLY_UNKNOWN = "apply_unknown"
    PARTIAL_APPLY = "partial_apply"
    APPLY_CONFLICT = "apply_conflict"
    APPLIED_WATERMARK_MISSING = "applied_watermark_missing"
    APPLIED_WATERMARK_REGRESSION = "applied_watermark_regression"
    APPLIED_RECEIPT_COVERAGE_INCOMPLETE = "applied_receipt_coverage_incomplete"
    SNAPSHOT_INVALID = "snapshot_invalid"
    SNAPSHOT_WATERMARK_BEHIND = "snapshot_watermark_behind"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    PERMIT_INVALID = "permit_invalid"
    PERMIT_ALREADY_USED = "permit_already_used"
    PERMIT_ALREADY_ISSUED = "permit_already_issued"
    PULL_INCOMPLETE = "pull_incomplete"
    PAGE_OUT_OF_ORDER = "page_out_of_order"
    PAGE_CHECKSUM_INVALID = "page_checksum_invalid"
    PULL_ABORTED = "pull_aborted"
    OVERRIDE_TOKEN_INVALID = "override_token_invalid"
    OVERRIDE_TOKEN_EXPIRED = "override_token_expired"
    OVERRIDE_TOKEN_UNCONFIRMED = "override_token_unconfirmed"
    OVERRIDE_ALREADY_USED = "override_already_used"
    OVERRIDE_BASELINE_CHANGED = "override_baseline_changed"
    CONCURRENT_APPLY_UNKNOWN = "concurrent_apply_unknown"
    OVERRIDE_IN_PROGRESS = "override_in_progress"
    OVERRIDE_FENCE_INVALID = "override_fence_invalid"
    OVERRIDE_FENCE_NOT_QUIESCED = "override_fence_not_quiesced"
    OVERRIDE_STATE_NOT_ELIGIBLE = "override_state_not_eligible"


class SyncV2Error(Exception):
    """可安全跨 domain 边界传播的 x4-2 错误，包含稳定错误码。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


def _error(code: ErrorCode, message: str) -> SyncV2Error:
    return SyncV2Error(code, message)


def _invalid(message: str) -> SyncV2Error:
    return _error(ErrorCode.INVALID_INPUT, message)


def _transition(message: str) -> SyncV2Error:
    return _error(ErrorCode.INVALID_TRANSITION, message)


def _require_object(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{name}_must_be_object")
    return value


def _strict_object(value: Any, allowed: Iterable[str], name: str) -> Dict[str, Any]:
    result = _require_object(value, name)
    allowed_set = set(allowed)
    unknown = sorted(set(result) - allowed_set)
    if unknown:
        raise _invalid(f"{name}_unknown_field:{unknown[0]}")
    return result


def _required(result: Mapping[str, Any], key: str, name: str) -> Any:
    if key not in result:
        raise _invalid(f"{name}_missing_field:{key}")
    return result[key]


def _string(value: Any, name: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{name}_must_be_string")
    if non_empty and not value.strip():
        raise _invalid(f"{name}_empty")
    return value


def _safe_int(
    value: Any,
    name: str,
    *,
    minimum: Optional[int] = None,
    maximum: int = LUA_SAFE_INTEGER_MAX,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name}_must_be_integer")
    if value > maximum:
        raise _invalid(f"{name}_exceeds_lua_safe_integer")
    if minimum is not None and value < minimum:
        raise _invalid(f"{name}_below_minimum")
    return value


E = TypeVar("E", bound=Enum)


def _enum_value(enum_type: Type[E], value: Any, name: str) -> E:
    if not isinstance(value, str):
        raise _invalid(f"{name}_must_be_string")
    try:
        return enum_type(value)
    except ValueError:
        raise _invalid(f"{name}_unknown_value:{value}") from None


def _strict_json_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """JSON object hook；重复键不能静默覆盖持久化证据。"""

    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid(f"duplicate_json_key:{key}")
        result[key] = value
    return result


@dataclass
class Watermark:
    """明确表示应用水位未知或已知；不把未知编码成 0。"""

    value: Optional[int] = None

    def __post_init__(self) -> None:
        if self.value is not None:
            _safe_int(self.value, "watermark", minimum=0)

    @classmethod
    def unknown(cls) -> "Watermark":
        return cls(None)

    @classmethod
    def known(cls, value: int) -> "Watermark":
        return cls(_safe_int(value, "watermark", minimum=0))

    @property
    def is_known(self) -> bool:
        return self.value is not None

    def validate(self) -> None:
        if self.value is not None:
            _safe_int(self.value, "watermark", minimum=0)

    def to_dict(self) -> Union[str, Dict[str, int]]:
        self.validate()
        return "unknown" if self.value is None else {"known": self.value}

    @classmethod
    def from_dict(cls, value: Any) -> "Watermark":
        if value == "unknown":
            return cls.unknown()
        result = _strict_object(value, {"known"}, "watermark")
        return cls.known(
            _safe_int(_required(result, "known", "watermark"), "watermark", minimum=0)
        )


@dataclass
class SyncScope:
    """设备同步范围；范围 ID 是字符串，不由 Lua number 承载。"""

    scope_id: str

    def validate(self) -> None:
        _string(self.scope_id, "scope_id", non_empty=True)

    def to_dict(self) -> Dict[str, str]:
        self.validate()
        return {"scope_id": self.scope_id}

    @classmethod
    def from_dict(cls, value: Any) -> "SyncScope":
        result = _strict_object(value, {"scope_id"}, "sync_scope")
        instance = cls(_string(_required(result, "scope_id", "sync_scope"), "scope_id"))
        instance.validate()
        return instance


@dataclass
class SyncContext:
    """所有报告、许可和分页请求共享的身份边界。"""

    device_id: str
    profile_id: str
    scope: SyncScope
    session_id: str
    local_generation: int

    def validate(self) -> None:
        _string(self.device_id, "device_id", non_empty=True)
        _string(self.profile_id, "profile_id", non_empty=True)
        _string(self.session_id, "session_id", non_empty=True)
        if not isinstance(self.scope, SyncScope):
            raise _invalid("scope_must_be_sync_scope")
        self.scope.validate()
        _safe_int(self.local_generation, "local_generation", minimum=0)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "scope": self.scope.to_dict(),
            "session_id": self.session_id,
            "local_generation": self.local_generation,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SyncContext":
        result = _strict_object(
            value,
            {"device_id", "profile_id", "scope", "session_id", "local_generation"},
            "sync_context",
        )
        instance = cls(
            device_id=_string(
                _required(result, "device_id", "sync_context"), "device_id"
            ),
            profile_id=_string(
                _required(result, "profile_id", "sync_context"), "profile_id"
            ),
            scope=SyncScope.from_dict(_required(result, "scope", "sync_context")),
            session_id=_string(
                _required(result, "session_id", "sync_context"), "session_id"
            ),
            local_generation=_safe_int(
                _required(result, "local_generation", "sync_context"),
                "local_generation",
                minimum=0,
            ),
        )
        instance.validate()
        return instance


@dataclass
class EventRange:
    """冻结的连续事件区间；序号为正且受 Lua 安全整数上限约束。"""

    first_sequence: int
    last_sequence: int

    def validate(self) -> None:
        _safe_int(self.first_sequence, "first_sequence", minimum=1)
        _safe_int(self.last_sequence, "last_sequence", minimum=1)
        if self.first_sequence > self.last_sequence:
            raise _invalid("event_range_reversed")

    def contains(self, sequence: int) -> bool:
        return self.first_sequence <= sequence <= self.last_sequence

    def length(self) -> int:
        self.validate()
        return self.last_sequence - self.first_sequence + 1

    def to_dict(self) -> Dict[str, int]:
        self.validate()
        return {
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "EventRange":
        result = _strict_object(
            value,
            {"first_sequence", "last_sequence"},
            "event_range",
        )
        instance = cls(
            first_sequence=_safe_int(
                _required(result, "first_sequence", "event_range"),
                "first_sequence",
                minimum=1,
            ),
            last_sequence=_safe_int(
                _required(result, "last_sequence", "event_range"),
                "last_sequence",
                minimum=1,
            ),
        )
        instance.validate()
        return instance


@dataclass
class DurableReceipt:
    """单事件持久收据；durable received 不等于 Anki 已应用。"""

    event_id: str
    sequence: int
    status: ReceiptStatus

    def validate_shape(self) -> None:
        _string(self.event_id, "receipt_event_id", non_empty=True)
        _safe_int(self.sequence, "receipt_sequence", minimum=1)
        if not isinstance(self.status, ReceiptStatus):
            raise _invalid("receipt_status_unknown")

    def to_dict(self) -> Dict[str, Any]:
        self.validate_shape()
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "DurableReceipt":
        result = _strict_object(
            value,
            {"event_id", "sequence", "status"},
            "durable_receipt",
        )
        instance = cls(
            event_id=_string(
                _required(result, "event_id", "durable_receipt"),
                "receipt_event_id",
            ),
            sequence=_safe_int(
                _required(result, "sequence", "durable_receipt"),
                "receipt_sequence",
                minimum=1,
            ),
            status=_enum_value(
                ReceiptStatus,
                _required(result, "status", "durable_receipt"),
                "receipt_status",
            ),
        )
        instance.validate_shape()
        return instance


@dataclass
class BeginSync:
    """开始一次绑定到 session/generation 的同步。"""

    context: SyncContext
    local_events: Optional[EventRange]
    applied_watermark: Watermark


@dataclass
class UploadReport:
    """上传结果和逐事件收据；网络响应未知时必须走查收据。"""

    context: SyncContext
    range: EventRange
    outcome: UploadOutcome
    checksum: ChecksumStatus
    receipts: List[DurableReceipt]


@dataclass
class ReceiptQueryReport:
    """有界收据查询结果。"""

    context: SyncContext
    range: EventRange
    outcome: UploadOutcome
    receipts: List[DurableReceipt]


@dataclass
class AnkiApplicationEvidence:
    """由未来 Anki adapter 提供的应用状态和 watermark 证据。"""

    context: SyncContext
    state: ApplicationState
    applied_watermark: Watermark


@dataclass
class SnapshotEvidence:
    """不可变、已校验且包含应用水位的快照证据。"""

    context: SyncContext
    snapshot_id: str
    applied_watermark: int
    snapshot_generation: int
    immutable: bool
    checksum: ChecksumStatus

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context.to_dict(),
            "snapshot_id": self.snapshot_id,
            "applied_watermark": self.applied_watermark,
            "snapshot_generation": self.snapshot_generation,
            "immutable": self.immutable,
            "checksum": self.checksum.value,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SnapshotEvidence":
        result = _strict_object(
            value,
            {
                "context",
                "snapshot_id",
                "applied_watermark",
                "snapshot_generation",
                "immutable",
                "checksum",
            },
            "snapshot",
        )
        immutable = _required(result, "immutable", "snapshot")
        if not isinstance(immutable, bool):
            raise _invalid("snapshot_immutable_must_be_boolean")
        return cls(
            context=SyncContext.from_dict(_required(result, "context", "snapshot")),
            snapshot_id=_string(
                _required(result, "snapshot_id", "snapshot"),
                "snapshot_id",
            ),
            applied_watermark=_safe_int(
                _required(result, "applied_watermark", "snapshot"),
                "applied_watermark",
                minimum=0,
            ),
            snapshot_generation=_safe_int(
                _required(result, "snapshot_generation", "snapshot"),
                "snapshot_generation",
                minimum=0,
            ),
            immutable=immutable,
            checksum=_enum_value(
                ChecksumStatus,
                _required(result, "checksum", "snapshot"),
                "snapshot_checksum",
            ),
        )


@dataclass
class PermitRequest:
    """请求一次绑定 nonce 的下载许可。"""

    permit_id: str
    nonce: str


@dataclass
class DownloadPermit:
    """一次性绑定的 snapshot 下载许可。"""

    permit_id: str
    nonce: str
    context: SyncContext
    event_range: Optional[EventRange]
    applied_watermark: int
    snapshot_id: str
    snapshot_generation: int
    consumed: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permit_id": self.permit_id,
            "nonce": self.nonce,
            "context": self.context.to_dict(),
            "event_range": None
            if self.event_range is None
            else self.event_range.to_dict(),
            "applied_watermark": self.applied_watermark,
            "snapshot_id": self.snapshot_id,
            "snapshot_generation": self.snapshot_generation,
            "consumed": self.consumed,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "DownloadPermit":
        result = _strict_object(
            value,
            {
                "permit_id",
                "nonce",
                "context",
                "event_range",
                "applied_watermark",
                "snapshot_id",
                "snapshot_generation",
                "consumed",
            },
            "download_permit",
        )
        consumed = _required(result, "consumed", "download_permit")
        if not isinstance(consumed, bool):
            raise _invalid("permit_consumed_must_be_boolean")
        event_range_value = result["event_range"]
        return cls(
            permit_id=_string(
                _required(result, "permit_id", "download_permit"), "permit_id"
            ),
            nonce=_string(
                _required(result, "nonce", "download_permit"), "permit_nonce"
            ),
            context=SyncContext.from_dict(
                _required(result, "context", "download_permit")
            ),
            event_range=(
                None
                if event_range_value is None
                else EventRange.from_dict(event_range_value)
            ),
            applied_watermark=_safe_int(
                _required(result, "applied_watermark", "download_permit"),
                "permit_applied_watermark",
                minimum=0,
            ),
            snapshot_id=_string(
                _required(result, "snapshot_id", "download_permit"),
                "permit_snapshot_id",
            ),
            snapshot_generation=_safe_int(
                _required(result, "snapshot_generation", "download_permit"),
                "permit_snapshot_generation",
                minimum=0,
            ),
            consumed=consumed,
        )


@dataclass
class OpenPullRequest:
    """打开一个已发放 permit 的分页下载。"""

    context: SyncContext
    permit_id: str
    nonce: str


@dataclass
class PullPage:
    """单页分页证据；页号、cursor 和 complete 必须相互一致。"""

    context: SyncContext
    permit_id: str
    nonce: str
    snapshot_id: str
    snapshot_generation: int
    page_index: int
    page_bytes: int
    checksum: ChecksumStatus
    cursor: Optional[str]
    complete: bool


@dataclass
class PullProgress:
    """可恢复的分页进度；received_pages 必须等于 next_page_index。"""

    permit_id: str
    nonce: str
    snapshot_id: str
    snapshot_generation: int
    next_page_index: int
    received_pages: int
    received_bytes: int
    complete: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permit_id": self.permit_id,
            "nonce": self.nonce,
            "snapshot_id": self.snapshot_id,
            "snapshot_generation": self.snapshot_generation,
            "next_page_index": self.next_page_index,
            "received_pages": self.received_pages,
            "received_bytes": self.received_bytes,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "PullProgress":
        result = _strict_object(
            value,
            {
                "permit_id",
                "nonce",
                "snapshot_id",
                "snapshot_generation",
                "next_page_index",
                "received_pages",
                "received_bytes",
                "complete",
            },
            "pull_progress",
        )
        complete = _required(result, "complete", "pull_progress")
        if not isinstance(complete, bool):
            raise _invalid("pull_complete_must_be_boolean")
        return cls(
            permit_id=_string(
                _required(result, "permit_id", "pull_progress"), "pull_permit_id"
            ),
            nonce=_string(_required(result, "nonce", "pull_progress"), "pull_nonce"),
            snapshot_id=_string(
                _required(result, "snapshot_id", "pull_progress"),
                "pull_snapshot_id",
            ),
            snapshot_generation=_safe_int(
                _required(result, "snapshot_generation", "pull_progress"),
                "pull_snapshot_generation",
                minimum=0,
            ),
            next_page_index=_safe_int(
                _required(result, "next_page_index", "pull_progress"),
                "pull_next_page_index",
                minimum=0,
            ),
            received_pages=_safe_int(
                _required(result, "received_pages", "pull_progress"),
                "pull_received_pages",
                minimum=0,
            ),
            received_bytes=_safe_int(
                _required(result, "received_bytes", "pull_progress"),
                "pull_received_bytes",
                minimum=0,
            ),
            complete=complete,
        )


@dataclass
class CommitRequest:
    """提交已完整接收并校验的候选快照。"""

    context: SyncContext
    permit_id: str
    nonce: str
    snapshot_id: str
    snapshot_generation: int


@dataclass
class PullAbortRequest:
    """取消、超时或断线时放弃当前半页进度但保留未消费 permit。"""

    context: SyncContext
    permit_id: str
    nonce: str
    reason: PullAbortReason


@dataclass
class OverrideBaseline:
    """覆盖授权要绑定的实时同步基线和审计 snapshot ID。"""

    scope: SyncScope
    local_generation: int
    local_events: Optional[EventRange]
    receipts: List[DurableReceipt]
    applied_watermark: Watermark
    snapshot_id: Optional[str]
    application_state: ApplicationState


@dataclass
class ApplyCoordinationFence:
    """外层应用协调器提供的 quiesced fence；纯 domain 不执行冻结。"""

    fence_id: str
    status: ApplyFenceStatus
    context: SyncContext
    local_events: Optional[EventRange]
    receipts: List[DurableReceipt]
    applied_watermark: Watermark
    application_state: ApplicationState


@dataclass
class OverridePrepareRequest:
    """预检一次性覆盖授权，不能隐式删除事件。"""

    context: SyncContext
    scope: SyncScope
    abandoned_events: Optional[EventRange]
    baseline: OverrideBaseline
    fence: ApplyCoordinationFence
    token_id: str
    nonce: str
    expires_at_ms: int


@dataclass
class OverrideToken:
    """覆盖授权的持久审计记录。"""

    token_id: str
    nonce: str
    context: SyncContext
    scope: SyncScope
    abandoned_events: Optional[EventRange]
    baseline: OverrideBaseline
    fence: ApplyCoordinationFence
    expires_at_ms: int
    status: OverrideTokenStatus

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_id": self.token_id,
            "nonce": self.nonce,
            "context": self.context.to_dict(),
            "scope": self.scope.to_dict(),
            "abandoned_events": (
                None
                if self.abandoned_events is None
                else self.abandoned_events.to_dict()
            ),
            "baseline": _baseline_to_dict(self.baseline),
            "fence": _fence_to_dict(self.fence),
            "expires_at_ms": self.expires_at_ms,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "OverrideToken":
        result = _strict_object(
            value,
            {
                "token_id",
                "nonce",
                "context",
                "scope",
                "abandoned_events",
                "baseline",
                "fence",
                "expires_at_ms",
                "status",
            },
            "override_token",
        )
        abandoned = result["abandoned_events"]
        return cls(
            token_id=_string(
                _required(result, "token_id", "override_token"), "token_id"
            ),
            nonce=_string(
                _required(result, "nonce", "override_token"), "override_nonce"
            ),
            context=SyncContext.from_dict(
                _required(result, "context", "override_token")
            ),
            scope=SyncScope.from_dict(_required(result, "scope", "override_token")),
            abandoned_events=None
            if abandoned is None
            else EventRange.from_dict(abandoned),
            baseline=_baseline_from_dict(
                _required(result, "baseline", "override_token")
            ),
            fence=_fence_from_dict(_required(result, "fence", "override_token")),
            expires_at_ms=_safe_int(
                _required(result, "expires_at_ms", "override_token"),
                "override_expires_at_ms",
                minimum=1,
            ),
            status=_enum_value(
                OverrideTokenStatus,
                _required(result, "status", "override_token"),
                "override_status",
            ),
        )


@dataclass
class OverrideConfirmRequest:
    """确认已预检的单次覆盖授权。"""

    context: SyncContext
    scope: SyncScope
    token_id: str
    nonce: str
    fence: ApplyCoordinationFence


@dataclass
class OverrideAuthorization:
    """确认结果；交给未来外层事务执行放弃和完整快照替换。"""

    token_id: str
    nonce: str
    context: SyncContext
    scope: SyncScope
    abandoned_events: Optional[EventRange]
    baseline: OverrideBaseline
    fence: ApplyCoordinationFence


@dataclass(frozen=True)
class SyncAction:
    """状态机建议的下一步；动作本身不执行 I/O。"""

    kind: str
    range: Optional[EventRange] = None
    applied_watermark: Optional[int] = None
    permit_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    page_index: Optional[int] = None

    @classmethod
    def none(cls) -> "SyncAction":
        return cls("none")

    @classmethod
    def upload(cls, selected_range: EventRange) -> "SyncAction":
        return cls("upload", range=selected_range)

    @classmethod
    def query_receipts(cls, selected_range: EventRange) -> "SyncAction":
        return cls("query_receipts", range=selected_range)

    @classmethod
    def wait_for_anki_apply(cls, selected_range: Optional[EventRange]) -> "SyncAction":
        return cls("wait_for_anki_apply", range=selected_range)

    @classmethod
    def query_anki_watermark(cls) -> "SyncAction":
        return cls("query_anki_watermark")

    @classmethod
    def request_snapshot(cls, applied_watermark: int) -> "SyncAction":
        return cls("request_snapshot", applied_watermark=applied_watermark)

    @classmethod
    def open_pull(cls, permit_id: str, snapshot_id: str) -> "SyncAction":
        return cls("open_pull", permit_id=permit_id, snapshot_id=snapshot_id)

    @classmethod
    def pull_page(
        cls, permit_id: str, snapshot_id: str, page_index: int
    ) -> "SyncAction":
        return cls(
            "pull_page",
            permit_id=permit_id,
            snapshot_id=snapshot_id,
            page_index=page_index,
        )

    @classmethod
    def commit(cls, permit_id: str, snapshot_id: str) -> "SyncAction":
        return cls("commit", permit_id=permit_id, snapshot_id=snapshot_id)


@dataclass
class LocalEvent:
    """通知 domain 有一个更高 generation 的连续本地事件。"""

    context: SyncContext
    sequence: int


@dataclass
class ImmutableReviewEvent:
    """不含交付/应用状态的不可变事件摘要。

    `payload_hash` 只覆盖 event_id、sequence 和 payload。delivery/application 状态
    由 receipt/application 表单离线保存，不能通过重传 ACK 改写事件 hash。
    """

    event_id: str
    sequence: int
    payload: Dict[str, Any]
    payload_hash: str

    FORBIDDEN_PAYLOAD_FIELDS = frozenset(
        {"delivery_status", "application_status", "receipt", "applied"}
    )

    @classmethod
    def create(
        cls,
        event_id: str,
        sequence: int,
        payload: Mapping[str, Any],
    ) -> "ImmutableReviewEvent":
        event_id = _string(event_id, "event_id", non_empty=True)
        sequence = _safe_int(sequence, "sequence", minimum=1)
        if not isinstance(payload, Mapping):
            raise _invalid("event_payload_must_be_object")
        payload_dict = dict(payload)

        def forbidden_keys(value: Any) -> List[str]:
            found: List[str] = []
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    if key in cls.FORBIDDEN_PAYLOAD_FIELDS:
                        found.append(str(key))
                    found.extend(forbidden_keys(nested))
            elif isinstance(value, list):
                for nested in value:
                    found.extend(forbidden_keys(nested))
            return found

        forbidden = sorted(set(forbidden_keys(payload_dict)))
        if forbidden:
            raise _invalid(f"event_payload_contains_delivery_state:{forbidden[0]}")
        if "rating" in payload_dict:
            rating = payload_dict["rating"]
            if (
                isinstance(rating, bool)
                or not isinstance(rating, int)
                or not 1 <= rating <= 4
            ):
                raise _invalid("event_rating_out_of_range")
        try:
            canonical = json.dumps(
                {"event_id": event_id, "sequence": sequence, "payload": payload_dict},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise _invalid("event_payload_not_json") from None
        return cls(
            event_id, sequence, payload_dict, hashlib.sha256(canonical).hexdigest()
        )

    def verify_hash(self) -> None:
        expected = self.create(self.event_id, self.sequence, self.payload).payload_hash
        if expected != self.payload_hash:
            raise _invalid("event_payload_hash_mismatch")


@dataclass
class PersistedState:
    """唯一需要由未来 storage 持久化的状态形状，schema 和字段均严格。"""

    schema_version: int = SYNC_V2_SCHEMA_VERSION
    phase: SyncPhase = SyncPhase.IDLE
    context: Optional[SyncContext] = None
    local_events: Optional[EventRange] = None
    upload_checksum: ChecksumStatus = ChecksumStatus.UNKNOWN
    receipts: List[DurableReceipt] = field(default_factory=list)
    applied_watermark: Watermark = field(default_factory=Watermark.unknown)
    application_state: ApplicationState = ApplicationState.UNKNOWN
    snapshot: Optional[SnapshotEvidence] = None
    permit: Optional[DownloadPermit] = None
    pull: Optional[PullProgress] = None
    last_error: Optional[ErrorCode] = None
    override_tokens: List[OverrideToken] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "phase": self.phase.value,
            "context": None if self.context is None else self.context.to_dict(),
            "local_events": (
                None if self.local_events is None else self.local_events.to_dict()
            ),
            "upload_checksum": self.upload_checksum.value,
            "receipts": [receipt.to_dict() for receipt in self.receipts],
            "applied_watermark": self.applied_watermark.to_dict(),
            "application_state": self.application_state.value,
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
            "permit": None if self.permit is None else self.permit.to_dict(),
            "pull": None if self.pull is None else self.pull.to_dict(),
            "last_error": None if self.last_error is None else self.last_error.value,
            "override_tokens": [token.to_dict() for token in self.override_tokens],
        }

    def to_json(self) -> str:
        """输出确定性的严格 JSON；不引入交付或应用字段到事件 hash。"""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "PersistedState":
        """严格恢复 state；未知字段/未知枚举在进入状态机前即拒绝。"""

        result = _strict_object(
            value,
            {
                "schema_version",
                "phase",
                "context",
                "local_events",
                "upload_checksum",
                "receipts",
                "applied_watermark",
                "application_state",
                "snapshot",
                "permit",
                "pull",
                "last_error",
                "override_tokens",
            },
            "persisted_state",
        )
        context_value = result["context"]
        local_events_value = result["local_events"]
        snapshot_value = result["snapshot"]
        permit_value = result["permit"]
        pull_value = result["pull"]
        last_error_value = result["last_error"]
        receipts_value = _required(result, "receipts", "persisted_state")
        override_value = _required(result, "override_tokens", "persisted_state")
        if not isinstance(receipts_value, list):
            raise _invalid("persisted_receipts_must_be_array")
        if not isinstance(override_value, list):
            raise _invalid("persisted_override_tokens_must_be_array")
        if last_error_value is not None and not isinstance(last_error_value, str):
            raise _invalid("persisted_last_error_must_be_string_or_null")
        return cls(
            schema_version=_safe_int(
                _required(result, "schema_version", "persisted_state"),
                "schema_version",
                minimum=0,
                maximum=65_535,
            ),
            phase=_enum_value(
                SyncPhase,
                _required(result, "phase", "persisted_state"),
                "persisted_phase",
            ),
            context=None
            if context_value is None
            else SyncContext.from_dict(context_value),
            local_events=(
                None
                if local_events_value is None
                else EventRange.from_dict(local_events_value)
            ),
            upload_checksum=_enum_value(
                ChecksumStatus,
                _required(result, "upload_checksum", "persisted_state"),
                "persisted_upload_checksum",
            ),
            receipts=[DurableReceipt.from_dict(item) for item in receipts_value],
            applied_watermark=Watermark.from_dict(
                _required(result, "applied_watermark", "persisted_state")
            ),
            application_state=_enum_value(
                ApplicationState,
                _required(result, "application_state", "persisted_state"),
                "persisted_application_state",
            ),
            snapshot=None
            if snapshot_value is None
            else SnapshotEvidence.from_dict(snapshot_value),
            permit=None
            if permit_value is None
            else DownloadPermit.from_dict(permit_value),
            pull=None if pull_value is None else PullProgress.from_dict(pull_value),
            last_error=(
                None
                if last_error_value is None
                else _enum_value(ErrorCode, last_error_value, "persisted_last_error")
            ),
            override_tokens=[OverrideToken.from_dict(item) for item in override_value],
        )

    @classmethod
    def from_json(cls, value: str) -> "PersistedState":
        try:
            decoded = json.loads(
                value,
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
            )
        except SyncV2Error:
            raise
        except (TypeError, ValueError, json.JSONDecodeError):
            raise _invalid("persisted_state_malformed_json") from None
        return cls.from_dict(decoded)

    def validate(self) -> None:
        """验证恢复后的完整状态组合；失败时不能降级成可下载状态。"""

        if self.schema_version != SYNC_V2_SCHEMA_VERSION:
            raise _invalid("sync_v2_schema_version")
        if not isinstance(self.phase, SyncPhase):
            raise _invalid("phase_unknown")
        if not isinstance(self.upload_checksum, ChecksumStatus):
            raise _invalid("upload_checksum_unknown")
        if not isinstance(self.application_state, ApplicationState):
            raise _invalid("application_state_unknown")
        if self.last_error is not None and not isinstance(self.last_error, ErrorCode):
            raise _invalid("last_error_unknown")
        if not isinstance(self.applied_watermark, Watermark):
            raise _invalid("applied_watermark_invalid")
        self.applied_watermark.validate()
        if self.context is not None:
            self.context.validate()
        if self.local_events is not None:
            self.local_events.validate()
        if self.context is None and self.phase != SyncPhase.IDLE:
            raise _error(ErrorCode.INVALID_STATE, "non_idle_state_without_context")
        if self.local_events is None and self.receipts:
            raise _error(ErrorCode.INVALID_STATE, "receipts_without_event_range")
        if self.local_events is not None and self.context is not None:
            _validate_receipts(self.context, self.local_events, self.receipts, False)
            if any(
                left.sequence >= right.sequence
                for left, right in zip(self.receipts, self.receipts[1:])
            ):
                raise _error(ErrorCode.INVALID_STATE, "receipts_not_sorted")

        if self.snapshot is not None:
            _validate_snapshot(self.snapshot)
            if self.context is None:
                raise _error(ErrorCode.INVALID_STATE, "snapshot_without_context")
            _ensure_exact_context(self.context, self.snapshot.context)

        if self.permit is not None:
            _validate_permit(self.permit)
            if self.context is None:
                raise _error(ErrorCode.INVALID_STATE, "permit_without_context")
            _ensure_exact_context(self.context, self.permit.context)
            if self.permit.event_range != self.local_events:
                raise _error(ErrorCode.INVALID_STATE, "permit_event_range_mismatch")
            if self.snapshot is None:
                raise _error(ErrorCode.INVALID_STATE, "permit_without_snapshot")
            if (
                self.permit.snapshot_id != self.snapshot.snapshot_id
                or self.permit.snapshot_generation != self.snapshot.snapshot_generation
            ):
                raise _error(ErrorCode.INVALID_STATE, "permit_snapshot_mismatch")
            if not self.applied_watermark.is_known:
                raise _error(
                    ErrorCode.INVALID_STATE, "permit_without_applied_watermark"
                )
            if (
                self.permit.applied_watermark != self.applied_watermark.value
                or self.snapshot.applied_watermark < self.applied_watermark.value
            ):
                raise _error(ErrorCode.INVALID_STATE, "permit_watermark_mismatch")

        if self.pull is not None:
            _validate_pull_progress(self.pull)
            if self.permit is None:
                raise _error(ErrorCode.INVALID_STATE, "pull_without_permit")
            if (
                self.pull.permit_id != self.permit.permit_id
                or self.pull.nonce != self.permit.nonce
                or self.pull.snapshot_id != self.permit.snapshot_id
                or self.pull.snapshot_generation != self.permit.snapshot_generation
            ):
                raise _error(ErrorCode.INVALID_STATE, "pull_permit_mismatch")

        prepared_count = 0
        for index, token in enumerate(self.override_tokens):
            _validate_override_token(token)
            if token.status == OverrideTokenStatus.PREPARED:
                prepared_count += 1
                if prepared_count > 1:
                    raise _error(
                        ErrorCode.INVALID_STATE, "multiple_prepared_override_tokens"
                    )
            if any(
                other.token_id == token.token_id or other.nonce == token.nonce
                for other in self.override_tokens[index + 1 :]
            ):
                raise _error(
                    ErrorCode.INVALID_STATE, "duplicate_override_token_identity"
                )
            if self.context is None:
                raise _error(ErrorCode.INVALID_STATE, "override_token_without_context")
            if token.status == OverrideTokenStatus.PREPARED or (
                self.phase == SyncPhase.OVERRIDE_PENDING_EXECUTION
                and token.status == OverrideTokenStatus.CONFIRMED
            ):
                _ensure_exact_context(self.context, token.context)
            if token.baseline.scope != token.scope:
                raise _error(ErrorCode.INVALID_STATE, "override_scope_mismatch")

        if self.phase == SyncPhase.IDLE:
            if (
                self.context is not None
                or self.local_events is not None
                or self.receipts
                or self.snapshot is not None
                or self.permit is not None
                or self.pull is not None
                or self.last_error is not None
            ):
                raise _error(ErrorCode.INVALID_STATE, "idle_state_has_sync_data")
        elif self.phase in {
            SyncPhase.UPLOAD_REQUIRED,
            SyncPhase.RECONCILE_RECEIPTS,
            SyncPhase.PENDING_APPLY,
            SyncPhase.RECONCILE_APPLICATION,
            SyncPhase.AWAITING_SNAPSHOT,
            SyncPhase.BLOCKED,
        }:
            if (
                self.snapshot is not None
                or self.permit is not None
                or self.pull is not None
            ):
                raise _error(ErrorCode.INVALID_STATE, "blocked_phase_has_download_gate")
        elif self.phase == SyncPhase.OVERRIDE_PENDING_EXECUTION:
            if (
                self.snapshot is not None
                or self.permit is not None
                or self.pull is not None
            ):
                raise _error(
                    ErrorCode.INVALID_STATE, "override_pending_has_download_gate"
                )
            confirmed = [
                token
                for token in self.override_tokens
                if token.status == OverrideTokenStatus.CONFIRMED
            ]
            if len(confirmed) != 1:
                raise _error(
                    ErrorCode.INVALID_STATE,
                    "override_pending_requires_one_confirmed_token",
                )
            if any(
                token.status == OverrideTokenStatus.PREPARED
                for token in self.override_tokens
            ):
                raise _error(
                    ErrorCode.INVALID_STATE, "override_pending_has_prepared_token"
                )
            confirmed_token = confirmed[0]
            current_baseline = _current_override_baseline(self)
            current_baseline.snapshot_id = confirmed_token.baseline.snapshot_id
            if confirmed_token.baseline != current_baseline:
                raise _error(
                    ErrorCode.INVALID_STATE, "override_pending_baseline_mismatch"
                )
            current_fence = _current_apply_fence(
                self,
                confirmed_token.fence.fence_id,
                ApplyFenceStatus.QUIESCED,
            )
            if confirmed_token.fence != current_fence:
                raise _error(ErrorCode.INVALID_STATE, "override_pending_fence_mismatch")
            if confirmed_token.abandoned_events != self.local_events:
                raise _error(
                    ErrorCode.INVALID_STATE, "override_pending_event_range_mismatch"
                )
        elif self.phase == SyncPhase.PULL_READY:
            if self.permit is None:
                raise _error(ErrorCode.INVALID_STATE, "pull_ready_without_permit")
            if self.permit.consumed or self.snapshot is None or self.pull is not None:
                raise _error(ErrorCode.INVALID_STATE, "pull_ready_inconsistent")
        elif self.phase == SyncPhase.PULLING:
            if self.permit is None:
                raise _error(ErrorCode.INVALID_STATE, "pulling_without_permit")
            if self.pull is None:
                raise _error(ErrorCode.INVALID_STATE, "pulling_without_progress")
            if self.permit.consumed or self.pull.complete or self.snapshot is None:
                raise _error(ErrorCode.INVALID_STATE, "pulling_inconsistent")
        elif self.phase == SyncPhase.PULL_COMPLETE:
            if self.permit is None:
                raise _error(ErrorCode.INVALID_STATE, "pull_complete_without_permit")
            if self.pull is None:
                raise _error(ErrorCode.INVALID_STATE, "pull_complete_without_progress")
            if self.permit.consumed or not self.pull.complete or self.snapshot is None:
                raise _error(ErrorCode.INVALID_STATE, "pull_complete_inconsistent")
        elif self.phase == SyncPhase.COMMITTED:
            if self.permit is None:
                raise _error(ErrorCode.INVALID_STATE, "committed_without_permit")
            if (
                not self.permit.consumed
                or self.snapshot is None
                or self.pull is not None
            ):
                raise _error(ErrorCode.INVALID_STATE, "committed_inconsistent")

        if self.phase in {
            SyncPhase.PULL_READY,
            SyncPhase.PULLING,
            SyncPhase.PULL_COMPLETE,
            SyncPhase.COMMITTED,
        }:
            if not self.applied_watermark.is_known:
                raise _error(
                    ErrorCode.INVALID_STATE, "download_phase_without_applied_watermark"
                )
            applied_watermark = self.applied_watermark.value
            if self.local_events is not None:
                if (
                    not _coverage_complete(self.local_events, self.receipts)
                    or not _all_receipts_applied(self.receipts)
                    or self.application_state != ApplicationState.APPLIED
                    or applied_watermark < self.local_events.last_sequence
                ):
                    raise _error(
                        ErrorCode.INVALID_STATE,
                        "download_phase_before_all_events_applied",
                    )
            elif self.application_state not in {
                ApplicationState.APPLIED,
                ApplicationState.NOT_REQUIRED,
            }:
                raise _error(
                    ErrorCode.INVALID_STATE,
                    "download_phase_without_application_evidence",
                )


def _baseline_to_dict(baseline: OverrideBaseline) -> Dict[str, Any]:
    return {
        "scope": baseline.scope.to_dict(),
        "local_generation": baseline.local_generation,
        "local_events": None
        if baseline.local_events is None
        else baseline.local_events.to_dict(),
        "receipts": [receipt.to_dict() for receipt in baseline.receipts],
        "applied_watermark": baseline.applied_watermark.to_dict(),
        "snapshot_id": baseline.snapshot_id,
        "application_state": baseline.application_state.value,
    }


def _baseline_from_dict(value: Any) -> OverrideBaseline:
    result = _strict_object(
        value,
        {
            "scope",
            "local_generation",
            "local_events",
            "receipts",
            "applied_watermark",
            "snapshot_id",
            "application_state",
        },
        "override_baseline",
    )
    local_events_value = result["local_events"]
    receipts_value = _required(result, "receipts", "override_baseline")
    if not isinstance(receipts_value, list):
        raise _invalid("override_baseline_receipts_must_be_array")
    snapshot_id = result["snapshot_id"]
    if snapshot_id is not None:
        snapshot_id = _string(snapshot_id, "override_snapshot_id")
    return OverrideBaseline(
        scope=SyncScope.from_dict(_required(result, "scope", "override_baseline")),
        local_generation=_safe_int(
            _required(result, "local_generation", "override_baseline"),
            "override_local_generation",
            minimum=0,
        ),
        local_events=None
        if local_events_value is None
        else EventRange.from_dict(local_events_value),
        receipts=[DurableReceipt.from_dict(item) for item in receipts_value],
        applied_watermark=Watermark.from_dict(
            _required(result, "applied_watermark", "override_baseline")
        ),
        snapshot_id=snapshot_id,
        application_state=_enum_value(
            ApplicationState,
            _required(result, "application_state", "override_baseline"),
            "override_application_state",
        ),
    )


def _fence_to_dict(fence: ApplyCoordinationFence) -> Dict[str, Any]:
    return {
        "fence_id": fence.fence_id,
        "status": fence.status.value,
        "context": fence.context.to_dict(),
        "local_events": None
        if fence.local_events is None
        else fence.local_events.to_dict(),
        "receipts": [receipt.to_dict() for receipt in fence.receipts],
        "applied_watermark": fence.applied_watermark.to_dict(),
        "application_state": fence.application_state.value,
    }


def _fence_from_dict(value: Any) -> ApplyCoordinationFence:
    result = _strict_object(
        value,
        {
            "fence_id",
            "status",
            "context",
            "local_events",
            "receipts",
            "applied_watermark",
            "application_state",
        },
        "apply_fence",
    )
    local_events_value = result["local_events"]
    receipts_value = _required(result, "receipts", "apply_fence")
    if not isinstance(receipts_value, list):
        raise _invalid("apply_fence_receipts_must_be_array")
    return ApplyCoordinationFence(
        fence_id=_string(_required(result, "fence_id", "apply_fence"), "fence_id"),
        status=_enum_value(
            ApplyFenceStatus,
            _required(result, "status", "apply_fence"),
            "fence_status",
        ),
        context=SyncContext.from_dict(_required(result, "context", "apply_fence")),
        local_events=None
        if local_events_value is None
        else EventRange.from_dict(local_events_value),
        receipts=[DurableReceipt.from_dict(item) for item in receipts_value],
        applied_watermark=Watermark.from_dict(
            _required(result, "applied_watermark", "apply_fence")
        ),
        application_state=_enum_value(
            ApplicationState,
            _required(result, "application_state", "apply_fence"),
            "fence_application_state",
        ),
    )


class SyncMachine:
    """x4-2 状态机；每个公开操作都以验证后原子提交结束。"""

    def __init__(self, state: Optional[PersistedState] = None) -> None:
        self._state = state if state is not None else PersistedState()

    @classmethod
    def restore(cls, state: Union[PersistedState, Mapping[str, Any]]) -> "SyncMachine":
        """严格恢复状态，失败时绝不把坏 state 降级为可下载状态。"""

        if isinstance(state, Mapping):
            state = PersistedState.from_dict(dict(state))
        if not isinstance(state, PersistedState):
            raise _invalid("sync_machine_restore_requires_state")
        state.validate()
        return cls(state)

    def state(self) -> PersistedState:
        """返回当前 state；外层应把它作为只读快照使用。"""

        return self._state

    def into_state(self) -> PersistedState:
        """取出当前 state，供持久化适配器保存。"""

        return self._state

    def next_action(self) -> SyncAction:
        """只报告动作，不执行上传、查询、下载或写回。"""

        phase = self._state.phase
        if phase in {
            SyncPhase.IDLE,
            SyncPhase.COMMITTED,
            SyncPhase.OVERRIDE_PENDING_EXECUTION,
            SyncPhase.BLOCKED,
        }:
            return SyncAction.none()
        if phase == SyncPhase.UPLOAD_REQUIRED:
            return (
                SyncAction.none()
                if self._state.local_events is None
                else SyncAction.upload(self._state.local_events)
            )
        if phase == SyncPhase.RECONCILE_RECEIPTS:
            return (
                SyncAction.none()
                if self._state.local_events is None
                else SyncAction.query_receipts(self._state.local_events)
            )
        if phase == SyncPhase.PENDING_APPLY:
            return SyncAction.wait_for_anki_apply(self._state.local_events)
        if phase == SyncPhase.RECONCILE_APPLICATION:
            return SyncAction.query_anki_watermark()
        if phase == SyncPhase.AWAITING_SNAPSHOT:
            return (
                SyncAction.request_snapshot(self._state.applied_watermark.value)
                if self._state.applied_watermark.is_known
                else SyncAction.query_anki_watermark()
            )
        if phase == SyncPhase.PULL_READY:
            if self._state.permit is None or self._state.snapshot is None:
                return SyncAction.none()
            return SyncAction.open_pull(
                self._state.permit.permit_id,
                self._state.snapshot.snapshot_id,
            )
        if phase == SyncPhase.PULLING:
            if self._state.permit is None or self._state.pull is None:
                return SyncAction.none()
            return SyncAction.pull_page(
                self._state.permit.permit_id,
                self._state.pull.snapshot_id,
                self._state.pull.next_page_index,
            )
        if phase == SyncPhase.PULL_COMPLETE:
            if self._state.permit is None or self._state.snapshot is None:
                return SyncAction.none()
            return SyncAction.commit(
                self._state.permit.permit_id,
                self._state.snapshot.snapshot_id,
            )
        return SyncAction.none()

    def begin(self, input_value: BeginSync) -> SyncPhase:
        """开始同步并冻结本次 local event range。"""

        def operation(state: PersistedState) -> SyncPhase:
            input_value.context.validate()
            if input_value.local_events is not None:
                input_value.local_events.validate()
            if state.phase not in {SyncPhase.IDLE, SyncPhase.COMMITTED}:
                raise _transition("sync_already_in_progress")
            if state.context is not None:
                _ensure_principal(state.context, input_value.context)
                if (
                    input_value.context.local_generation
                    < state.context.local_generation
                ):
                    raise _error(
                        ErrorCode.GENERATION_MISMATCH, "begin_generation_regressed"
                    )
            state.context = copy.deepcopy(input_value.context)
            _invalidate_prepared_overrides(state)
            state.local_events = copy.deepcopy(input_value.local_events)
            state.upload_checksum = (
                ChecksumStatus.VERIFIED
                if input_value.local_events is None
                else ChecksumStatus.UNKNOWN
            )
            state.receipts.clear()
            state.applied_watermark = copy.deepcopy(input_value.applied_watermark)
            state.application_state = (
                ApplicationState.PENDING
                if input_value.local_events is not None
                else (
                    ApplicationState.APPLIED
                    if input_value.applied_watermark.is_known
                    else ApplicationState.UNKNOWN
                )
            )
            _clear_download_gate(state)
            state.last_error = None
            state.phase = (
                SyncPhase.UPLOAD_REQUIRED
                if input_value.local_events is not None
                else (
                    SyncPhase.AWAITING_SNAPSHOT
                    if input_value.applied_watermark.is_known
                    else SyncPhase.RECONCILE_APPLICATION
                )
            )
            return state.phase

        return self._atomic(operation)

    def record_upload(self, report: UploadReport) -> SyncPhase:
        """记录上传结果；失败、未知或缺 receipt 均不开放 pull。"""

        def operation(state: PersistedState) -> SyncPhase:
            _ensure_override_state_mutable(state)
            _ensure_report_context(state, report.context)
            expected_range = state.local_events
            if expected_range is None:
                raise _transition("upload_without_local_events")
            if report.range != expected_range:
                raise _error(ErrorCode.SEQUENCE_GAP, "upload_range_mismatch")
            report.range.validate()
            _validate_receipts(report.context, report.range, report.receipts, False)
            _invalidate_prepared_overrides(state)
            _clear_download_gate(state)
            state.upload_checksum = report.checksum
            if report.receipts:
                _merge_receipts(state, report.receipts)
            if report.checksum == ChecksumStatus.INVALID:
                state.phase = SyncPhase.UPLOAD_REQUIRED
                state.application_state = ApplicationState.PENDING
                state.last_error = ErrorCode.UPLOAD_CHECKSUM_INVALID
            elif report.outcome == UploadOutcome.FAILED:
                state.phase = SyncPhase.UPLOAD_REQUIRED
                state.application_state = ApplicationState.PENDING
                state.last_error = ErrorCode.UPLOAD_FAILED
            elif report.outcome == UploadOutcome.PARTIAL:
                state.phase = SyncPhase.UPLOAD_REQUIRED
                state.application_state = ApplicationState.PENDING
                state.last_error = ErrorCode.UPLOAD_PARTIAL
            elif report.outcome == UploadOutcome.UNKNOWN:
                state.phase = SyncPhase.RECONCILE_RECEIPTS
                state.application_state = ApplicationState.PENDING
                state.last_error = ErrorCode.UPLOAD_UNKNOWN
            elif report.checksum != ChecksumStatus.VERIFIED:
                state.phase = SyncPhase.UPLOAD_REQUIRED
                state.application_state = ApplicationState.PENDING
                state.last_error = ErrorCode.UPLOAD_CHECKSUM_UNKNOWN
            elif not _coverage_complete(expected_range, state.receipts):
                state.phase = SyncPhase.RECONCILE_RECEIPTS
                state.application_state = ApplicationState.PENDING
                state.last_error = ErrorCode.RECEIPT_MISSING
            else:
                state.application_state = ApplicationState.PENDING
                _recompute_after_receipts(state)
            return state.phase

        return self._atomic(operation)

    def record_receipts(self, report: ReceiptQueryReport) -> SyncPhase:
        """合并查收据结果；ACK 丢失只允许恢复同一事件范围。"""

        def operation(state: PersistedState) -> SyncPhase:
            _ensure_override_state_mutable(state)
            _ensure_report_context(state, report.context)
            expected_range = state.local_events
            if expected_range is None:
                raise _transition("receipts_without_local_events")
            if report.range != expected_range:
                raise _error(ErrorCode.SEQUENCE_GAP, "receipt_query_range_mismatch")
            _validate_receipts(report.context, report.range, report.receipts, False)
            _invalidate_prepared_overrides(state)
            if report.receipts:
                _merge_receipts(state, report.receipts)
            _clear_download_gate(state)
            if report.outcome == UploadOutcome.COMPLETE:
                if state.upload_checksum != ChecksumStatus.VERIFIED:
                    state.phase = SyncPhase.UPLOAD_REQUIRED
                    state.last_error = (
                        ErrorCode.UPLOAD_CHECKSUM_INVALID
                        if state.upload_checksum == ChecksumStatus.INVALID
                        else ErrorCode.UPLOAD_CHECKSUM_UNKNOWN
                    )
                elif not _coverage_complete(expected_range, state.receipts):
                    state.phase = SyncPhase.RECONCILE_RECEIPTS
                    state.last_error = ErrorCode.RECEIPT_MISSING
                else:
                    _recompute_after_receipts(state)
            elif report.outcome == UploadOutcome.PARTIAL:
                state.phase = SyncPhase.RECONCILE_RECEIPTS
                state.last_error = ErrorCode.UPLOAD_PARTIAL
            elif report.outcome == UploadOutcome.FAILED:
                state.phase = SyncPhase.RECONCILE_RECEIPTS
                state.last_error = ErrorCode.UPLOAD_FAILED
            else:
                state.phase = SyncPhase.RECONCILE_RECEIPTS
                state.last_error = ErrorCode.UPLOAD_UNKNOWN
            return state.phase

        return self._atomic(operation)

    def record_anki_application(
        self,
        evidence: AnkiApplicationEvidence,
    ) -> SyncPhase:
        """记录未来 Anki adapter 的应用证据；未知证据阻断下载。"""

        def operation(state: PersistedState) -> SyncPhase:
            _ensure_override_state_mutable(state)
            _ensure_report_context(state, evidence.context)
            if (
                evidence.state == ApplicationState.APPLIED
                and not evidence.applied_watermark.is_known
            ):
                raise _error(
                    ErrorCode.APPLIED_WATERMARK_MISSING,
                    "applied_state_requires_watermark",
                )
            if (
                evidence.state == ApplicationState.NOT_REQUIRED
                and state.local_events is not None
            ):
                raise _invalid("events_cannot_be_not_required")
            if (
                state.applied_watermark.is_known
                and evidence.applied_watermark.is_known
                and evidence.applied_watermark.value < state.applied_watermark.value
            ):
                raise _error(
                    ErrorCode.APPLIED_WATERMARK_REGRESSION,
                    "applied_watermark_regressed",
                )
            _invalidate_prepared_overrides(state)
            _clear_download_gate(state)
            state.application_state = evidence.state
            state.applied_watermark = copy.deepcopy(evidence.applied_watermark)
            if evidence.state == ApplicationState.UNKNOWN:
                state.phase = SyncPhase.RECONCILE_APPLICATION
                state.last_error = ErrorCode.APPLY_UNKNOWN
            elif evidence.state == ApplicationState.PARTIAL:
                state.phase = SyncPhase.RECONCILE_APPLICATION
                state.last_error = ErrorCode.PARTIAL_APPLY
            elif evidence.state == ApplicationState.CONFLICT:
                state.phase = SyncPhase.BLOCKED
                state.last_error = ErrorCode.APPLY_CONFLICT
            elif evidence.state == ApplicationState.PENDING:
                state.phase = SyncPhase.PENDING_APPLY
                state.last_error = ErrorCode.APPLY_UNKNOWN
            elif state.local_events is not None and (
                not _coverage_complete(state.local_events, state.receipts)
                or not _all_receipts_applied(state.receipts)
            ):
                state.phase = SyncPhase.RECONCILE_RECEIPTS
                state.last_error = ErrorCode.APPLIED_RECEIPT_COVERAGE_INCOMPLETE
            else:
                _recompute_after_receipts(state)
            return state.phase

        return self._atomic(operation)

    def accept_snapshot(
        self,
        snapshot: SnapshotEvidence,
        request: PermitRequest,
    ) -> DownloadPermit:
        """接受匹配应用 watermark 的不可变快照并签发一次性 permit。"""

        def operation(state: PersistedState) -> DownloadPermit:
            if state.phase != SyncPhase.AWAITING_SNAPSHOT:
                raise _transition("snapshot_not_expected")
            _ensure_report_context(state, snapshot.context)
            _validate_snapshot(snapshot)
            required_watermark = state.applied_watermark.value
            if required_watermark is None:
                raise _error(
                    ErrorCode.APPLIED_WATERMARK_MISSING,
                    "snapshot_requires_known_applied_watermark",
                )
            if snapshot.applied_watermark < required_watermark:
                raise _error(
                    ErrorCode.SNAPSHOT_WATERMARK_BEHIND,
                    "snapshot_does_not_include_applied_watermark",
                )
            _validate_permit_request(request)
            if state.permit is not None:
                raise _error(
                    ErrorCode.PERMIT_ALREADY_ISSUED, "download_permit_already_issued"
                )
            permit = DownloadPermit(
                permit_id=request.permit_id,
                nonce=request.nonce,
                context=copy.deepcopy(snapshot.context),
                event_range=copy.deepcopy(state.local_events),
                applied_watermark=required_watermark,
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.snapshot_generation,
                consumed=False,
            )
            _validate_permit(permit)
            state.snapshot = copy.deepcopy(snapshot)
            state.permit = permit
            state.pull = None
            state.phase = SyncPhase.PULL_READY
            state.last_error = None
            return permit

        return self._atomic(operation)

    def open_pull(self, request: OpenPullRequest) -> SyncPhase:
        """打开分页下载并初始化零页进度。"""

        def operation(state: PersistedState) -> SyncPhase:
            if state.phase != SyncPhase.PULL_READY:
                raise _transition("pull_not_ready")
            _ensure_report_context(state, request.context)
            if state.permit is None:
                raise _error(ErrorCode.PERMIT_INVALID, "permit_missing")
            permit = state.permit
            if permit.permit_id != request.permit_id or permit.nonce != request.nonce:
                raise _error(ErrorCode.PERMIT_INVALID, "permit_binding_mismatch")
            if permit.consumed:
                raise _error(ErrorCode.PERMIT_ALREADY_USED, "permit_already_consumed")
            if state.snapshot is None:
                raise _error(ErrorCode.SNAPSHOT_MISMATCH, "snapshot_missing")
            state.pull = PullProgress(
                permit_id=permit.permit_id,
                nonce=permit.nonce,
                snapshot_id=state.snapshot.snapshot_id,
                snapshot_generation=state.snapshot.snapshot_generation,
                next_page_index=0,
                received_pages=0,
                received_bytes=0,
                complete=False,
            )
            state.phase = SyncPhase.PULLING
            state.last_error = None
            return state.phase

        return self._atomic(operation)

    def accept_page(self, page: PullPage) -> SyncPhase:
        """校验连续页；坏 checksum 直接清除下载 gate 并阻断。"""

        def operation(state: PersistedState) -> SyncPhase:
            if state.phase != SyncPhase.PULLING:
                raise _transition("page_not_expected")
            _ensure_report_context(state, page.context)
            _validate_page_shape(page)
            if state.permit is None:
                raise _error(ErrorCode.PERMIT_INVALID, "permit_missing")
            if state.pull is None:
                raise _error(ErrorCode.PULL_INCOMPLETE, "pull_progress_missing")
            if state.snapshot is None:
                raise _error(ErrorCode.SNAPSHOT_MISMATCH, "snapshot_missing")
            permit = state.permit
            progress = state.pull
            snapshot = state.snapshot
            if page.permit_id != permit.permit_id or page.nonce != permit.nonce:
                raise _error(ErrorCode.PERMIT_INVALID, "page_permit_binding_mismatch")
            if (
                page.snapshot_id != snapshot.snapshot_id
                or page.snapshot_generation != snapshot.snapshot_generation
                or page.snapshot_id != progress.snapshot_id
                or page.nonce != progress.nonce
            ):
                raise _error(
                    ErrorCode.SNAPSHOT_MISMATCH, "page_snapshot_or_permit_mismatch"
                )
            if page.page_index != progress.next_page_index:
                raise _error(ErrorCode.PAGE_OUT_OF_ORDER, "page_index_not_contiguous")
            if page.checksum != ChecksumStatus.VERIFIED:
                _clear_download_gate(state)
                state.phase = SyncPhase.BLOCKED
                state.last_error = ErrorCode.PAGE_CHECKSUM_INVALID
                return state.phase
            if page.complete != (page.cursor is None):
                raise _invalid("page_cursor_complete_mismatch")
            if not page.complete and page.page_bytes == 0:
                raise _invalid("empty_nonfinal_page")
            next_bytes = progress.received_bytes + page.page_bytes
            if next_bytes > LUA_SAFE_INTEGER_MAX:
                raise _invalid("pull_byte_count_overflow")
            next_page_index = progress.next_page_index + 1
            next_received_pages = progress.received_pages + 1
            if next_page_index > MAX_PAGE_INDEX or next_received_pages > MAX_PAGE_INDEX:
                raise _invalid("pull_page_count_overflow")
            state.pull = PullProgress(
                permit_id=progress.permit_id,
                nonce=progress.nonce,
                snapshot_id=progress.snapshot_id,
                snapshot_generation=progress.snapshot_generation,
                next_page_index=next_page_index,
                received_pages=next_received_pages,
                received_bytes=next_bytes,
                complete=page.complete,
            )
            state.phase = (
                SyncPhase.PULL_COMPLETE if page.complete else SyncPhase.PULLING
            )
            state.last_error = None
            return state.phase

        return self._atomic(operation)

    def abort_pull(self, request: PullAbortRequest) -> SyncPhase:
        """取消/超时/断线清除半页，保留未消费 permit 回到 PullReady。"""

        def operation(state: PersistedState) -> SyncPhase:
            if state.phase not in {SyncPhase.PULLING, SyncPhase.PULL_COMPLETE}:
                raise _transition("pull_not_active")
            _ensure_report_context(state, request.context)
            if state.permit is None:
                raise _error(ErrorCode.PERMIT_INVALID, "permit_missing")
            if (
                state.permit.permit_id != request.permit_id
                or state.permit.nonce != request.nonce
                or state.permit.consumed
            ):
                raise _error(ErrorCode.PERMIT_INVALID, "abort_permit_mismatch")
            state.pull = None
            state.phase = SyncPhase.PULL_READY
            state.last_error = ErrorCode.PULL_ABORTED
            return state.phase

        return self._atomic(operation)

    def commit(self, request: CommitRequest) -> SyncPhase:
        """二次校验完整分页和 permit 后，标记 permit consumed。"""

        def operation(state: PersistedState) -> SyncPhase:
            if state.phase != SyncPhase.PULL_COMPLETE:
                raise _error(ErrorCode.PULL_INCOMPLETE, "commit_requires_complete_pull")
            _ensure_report_context(state, request.context)
            if state.permit is None:
                raise _error(ErrorCode.PERMIT_INVALID, "permit_missing")
            if state.pull is None:
                raise _error(ErrorCode.PULL_INCOMPLETE, "pull_progress_missing")
            if state.snapshot is None:
                raise _error(ErrorCode.SNAPSHOT_MISMATCH, "snapshot_missing")
            permit = state.permit
            if permit.consumed:
                raise _error(ErrorCode.PERMIT_ALREADY_USED, "permit_already_consumed")
            if request.permit_id != permit.permit_id or request.nonce != permit.nonce:
                raise _error(ErrorCode.PERMIT_INVALID, "commit_permit_binding_mismatch")
            if (
                request.snapshot_id != state.snapshot.snapshot_id
                or request.snapshot_generation != state.snapshot.snapshot_generation
                or not state.pull.complete
            ):
                raise _error(ErrorCode.SNAPSHOT_MISMATCH, "commit_binding_mismatch")
            state.permit = copy.deepcopy(permit)
            state.permit.consumed = True
            state.pull = None
            state.phase = SyncPhase.COMMITTED
            state.last_error = None
            return state.phase

        return self._atomic(operation)

    def record_local_event(self, input_value: LocalEvent) -> SyncPhase:
        """接收更高 generation 的连续事件并使旧 snapshot/permit 失效。"""

        def operation(state: PersistedState) -> SyncPhase:
            _ensure_override_state_mutable(state)
            input_value.context.validate()
            if state.context is None:
                raise _transition("local_event_without_sync")
            _ensure_principal(state.context, input_value.context)
            if state.context.session_id != input_value.context.session_id:
                raise _error(ErrorCode.SESSION_MISMATCH, "local_event_session_mismatch")
            if input_value.context.local_generation <= state.context.local_generation:
                raise _error(
                    ErrorCode.GENERATION_NOT_ADVANCED,
                    "local_event_generation_not_advanced",
                )
            sequence = _safe_int(input_value.sequence, "sequence", minimum=1)
            if state.local_events is not None:
                expected = state.local_events.last_sequence + 1
                if expected > LUA_SAFE_INTEGER_MAX:
                    raise _invalid("sequence_overflow")
                if sequence != expected:
                    raise _error(ErrorCode.SEQUENCE_GAP, "local_event_not_contiguous")
            state.context = copy.deepcopy(input_value.context)
            _invalidate_prepared_overrides(state)
            state.local_events = (
                EventRange(state.local_events.first_sequence, sequence)
                if state.local_events is not None
                else EventRange(sequence, sequence)
            )
            _clear_download_gate(state)
            state.upload_checksum = ChecksumStatus.UNKNOWN
            state.application_state = ApplicationState.PENDING
            state.phase = SyncPhase.UPLOAD_REQUIRED
            state.last_error = None
            return state.phase

        return self._atomic(operation)

    def current_override_baseline(self) -> Optional[OverrideBaseline]:
        """返回当前覆盖预检基线，供外层协调器原样回传。"""

        if self._state.context is None:
            return None
        return _current_override_baseline(self._state)

    def current_apply_fence(
        self,
        fence_id: str,
        status: ApplyFenceStatus,
    ) -> Optional[ApplyCoordinationFence]:
        """构造当前实时基线的 fence 视图，不代表 domain 已冻结外层应用。"""

        if self._state.context is None:
            return None
        return _current_apply_fence(self._state, fence_id, status)

    def prepare_override(
        self,
        input_value: OverridePrepareRequest,
        now_ms: int,
    ) -> OverrideToken:
        """预检一次性覆盖授权；过期 sweep 与主操作分开原子提交。"""

        self._sweep_expired_prepared(now_ms)

        def operation(state: PersistedState) -> OverrideToken:
            _ensure_override_state_mutable(state)
            _ensure_report_context(state, input_value.context)
            input_value.scope.validate()
            if input_value.scope != input_value.context.scope:
                raise _error(
                    ErrorCode.SCOPE_MISMATCH, "override_scope_does_not_match_context"
                )
            if not input_value.token_id.strip() or not input_value.nonce.strip():
                raise _error(
                    ErrorCode.OVERRIDE_TOKEN_INVALID, "override_token_identity_empty"
                )
            _safe_int(input_value.expires_at_ms, "override_expires_at_ms", minimum=1)
            if input_value.expires_at_ms <= now_ms:
                raise _error(
                    ErrorCode.OVERRIDE_TOKEN_EXPIRED,
                    "override_expiration_not_in_future",
                )
            if input_value.abandoned_events != state.local_events:
                raise _error(
                    ErrorCode.OVERRIDE_BASELINE_CHANGED, "abandoned_event_range_changed"
                )
            if state.pull is not None:
                raise _transition("override_during_pull")
            _ensure_override_state_eligible(state)
            current_baseline = _current_override_baseline(state)
            if input_value.baseline != current_baseline:
                raise _error(
                    ErrorCode.OVERRIDE_BASELINE_CHANGED, "override_baseline_changed"
                )
            if any(
                token.status == OverrideTokenStatus.PREPARED
                or token.token_id == input_value.token_id
                or token.nonce == input_value.nonce
                for token in state.override_tokens
            ):
                raise _error(
                    ErrorCode.OVERRIDE_IN_PROGRESS, "override_token_already_present"
                )
            if input_value.fence.status != ApplyFenceStatus.QUIESCED:
                raise _error(
                    ErrorCode.OVERRIDE_FENCE_NOT_QUIESCED,
                    "override_fence_not_quiesced",
                )
            _validate_apply_fence(input_value.fence)
            current_fence = _current_apply_fence(
                state,
                input_value.fence.fence_id,
                ApplyFenceStatus.QUIESCED,
            )
            if input_value.fence != current_fence:
                raise _error(ErrorCode.OVERRIDE_FENCE_INVALID, "override_fence_changed")
            token = OverrideToken(
                token_id=input_value.token_id,
                nonce=input_value.nonce,
                context=copy.deepcopy(input_value.context),
                scope=copy.deepcopy(input_value.scope),
                abandoned_events=copy.deepcopy(input_value.abandoned_events),
                baseline=copy.deepcopy(input_value.baseline),
                fence=copy.deepcopy(input_value.fence),
                expires_at_ms=input_value.expires_at_ms,
                status=OverrideTokenStatus.PREPARED,
            )
            _validate_override_token(token)
            state.override_tokens.append(token)
            return token

        return self._atomic(operation)

    def confirm_override(
        self,
        input_value: OverrideConfirmRequest,
        now_ms: int,
    ) -> OverrideAuthorization:
        """确认覆盖授权并清除旧下载 gate，但不删除事件或 receipt。"""

        self._sweep_expired_prepared(now_ms)

        def operation(state: PersistedState) -> OverrideAuthorization:
            _ensure_report_context(state, input_value.context)
            if input_value.scope != input_value.context.scope:
                raise _error(
                    ErrorCode.SCOPE_MISMATCH, "override_confirm_scope_mismatch"
                )
            token = next(
                (
                    candidate
                    for candidate in state.override_tokens
                    if candidate.token_id == input_value.token_id
                ),
                None,
            )
            if token is None:
                raise _error(ErrorCode.OVERRIDE_TOKEN_INVALID, "override_token_unknown")
            if token.nonce != input_value.nonce:
                raise _error(
                    ErrorCode.OVERRIDE_TOKEN_INVALID, "override_nonce_mismatch"
                )
            if token.status == OverrideTokenStatus.CONFIRMED:
                raise _error(
                    ErrorCode.OVERRIDE_ALREADY_USED, "override_nonce_already_used"
                )
            if token.status == OverrideTokenStatus.EXPIRED:
                raise _error(ErrorCode.OVERRIDE_TOKEN_EXPIRED, "override_token_expired")
            if now_ms >= token.expires_at_ms:
                raise _error(ErrorCode.OVERRIDE_TOKEN_EXPIRED, "override_token_expired")
            _ensure_override_state_eligible(state)
            current_baseline = _current_override_baseline(state)
            if token.baseline != current_baseline:
                raise _error(
                    ErrorCode.OVERRIDE_BASELINE_CHANGED,
                    "override_baseline_changed_before_confirm",
                )
            if input_value.fence.status != ApplyFenceStatus.QUIESCED:
                raise _error(
                    ErrorCode.OVERRIDE_FENCE_NOT_QUIESCED,
                    "override_fence_not_quiesced",
                )
            _validate_apply_fence(input_value.fence)
            if input_value.fence != token.fence:
                raise _error(
                    ErrorCode.OVERRIDE_FENCE_INVALID, "override_fence_token_mismatch"
                )
            current_fence = _current_apply_fence(
                state,
                token.fence.fence_id,
                ApplyFenceStatus.QUIESCED,
            )
            if token.fence != current_fence:
                raise _error(
                    ErrorCode.OVERRIDE_FENCE_INVALID,
                    "override_fence_changed_before_confirm",
                )
            token_index = next(
                index
                for index, candidate in enumerate(state.override_tokens)
                if candidate.token_id == input_value.token_id
            )
            _clear_download_gate(state)
            state.override_tokens[token_index].status = OverrideTokenStatus.CONFIRMED
            confirmed = state.override_tokens[token_index]
            authorization = OverrideAuthorization(
                token_id=confirmed.token_id,
                nonce=confirmed.nonce,
                context=copy.deepcopy(confirmed.context),
                scope=copy.deepcopy(confirmed.scope),
                abandoned_events=copy.deepcopy(confirmed.abandoned_events),
                baseline=copy.deepcopy(confirmed.baseline),
                fence=copy.deepcopy(confirmed.fence),
            )
            state.phase = SyncPhase.OVERRIDE_PENDING_EXECUTION
            state.last_error = None
            return authorization

        return self._atomic(operation)

    def _atomic(self, operation: Callable[[PersistedState], Any]) -> Any:
        original = copy.deepcopy(self._state)
        try:
            value = operation(self._state)
            self._state.validate()
            return value
        except SyncV2Error:
            self._state = original
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            self._state = original
            raise _invalid(f"domain_operation_invalid:{type(exc).__name__}") from None

    def _sweep_expired_prepared(self, now_ms: int) -> None:
        _safe_int(now_ms, "now_ms")

        def operation(state: PersistedState) -> None:
            _expire_prepared_overrides_at(state, now_ms)

        self._atomic(operation)


def _ensure_principal(expected: SyncContext, observed: SyncContext) -> None:
    if expected.device_id != observed.device_id:
        raise _error(ErrorCode.DEVICE_MISMATCH, "device_id_mismatch")
    if expected.profile_id != observed.profile_id:
        raise _error(ErrorCode.PROFILE_MISMATCH, "profile_id_mismatch")
    if expected.scope != observed.scope:
        raise _error(ErrorCode.SCOPE_MISMATCH, "scope_mismatch")


def _ensure_exact_context(expected: SyncContext, observed: SyncContext) -> None:
    _ensure_principal(expected, observed)
    if expected.session_id != observed.session_id:
        raise _error(ErrorCode.SESSION_MISMATCH, "session_id_mismatch")
    if expected.local_generation != observed.local_generation:
        raise _error(ErrorCode.GENERATION_MISMATCH, "local_generation_mismatch")


def _ensure_report_context(state: PersistedState, observed: SyncContext) -> None:
    if state.context is None:
        raise _transition("operation_without_sync_context")
    observed.validate()
    _ensure_exact_context(state.context, observed)


def _ensure_override_state_mutable(state: PersistedState) -> None:
    if state.phase == SyncPhase.OVERRIDE_PENDING_EXECUTION:
        raise _transition("override_execution_pending")


def _validate_receipts(
    sync_context: SyncContext,
    selected_range: EventRange,
    receipt_values: Sequence[DurableReceipt],
    require_complete: bool,
) -> None:
    selected_range.validate()
    sync_context.validate()
    sequences: List[int] = []
    for receipt in receipt_values:
        receipt.validate_shape()
        if not selected_range.contains(receipt.sequence):
            raise _error(ErrorCode.SEQUENCE_GAP, "receipt_sequence_out_of_range")
        expected_event_id = f"{sync_context.session_id}:{receipt.sequence}"
        if receipt.event_id != expected_event_id:
            raise _invalid("receipt_event_id_sequence_mismatch")
        sequences.append(receipt.sequence)
    sorted_sequences = sorted(sequences)
    if any(
        left == right for left, right in zip(sorted_sequences, sorted_sequences[1:])
    ):
        raise _error(ErrorCode.SEQUENCE_GAP, "duplicate_receipt_sequence")
    if require_complete and (
        len(sorted_sequences) != selected_range.length()
        or (
            not sorted_sequences or sorted_sequences[0] != selected_range.first_sequence
        )
        or (
            not sorted_sequences or sorted_sequences[-1] != selected_range.last_sequence
        )
    ):
        raise _error(ErrorCode.RECEIPT_MISSING, "receipt_range_incomplete")


def _merge_receipts(state: PersistedState, incoming: Sequence[DurableReceipt]) -> None:
    for receipt in incoming:
        existing = next(
            (
                candidate
                for candidate in state.receipts
                if candidate.sequence == receipt.sequence
            ),
            None,
        )
        if existing is not None:
            if existing.event_id != receipt.event_id:
                raise _error(
                    ErrorCode.RECEIPT_CONFLICT, "receipt_event_identity_changed"
                )
            if existing.status in {
                ReceiptStatus.ABANDONED,
                ReceiptStatus.CONFLICT,
            } or (
                existing.status == ReceiptStatus.APPLIED
                and existing.status != receipt.status
            ):
                raise _error(ErrorCode.RECEIPT_REGRESSION, "receipt_status_regressed")
            existing.status = receipt.status
        else:
            state.receipts.append(copy.deepcopy(receipt))
    state.receipts.sort(key=lambda item: item.sequence)


def _coverage_complete(
    selected_range: EventRange, receipt_values: Sequence[DurableReceipt]
) -> bool:
    if len(receipt_values) != selected_range.length():
        return False
    return all(
        receipt.sequence == selected_range.first_sequence + index
        for index, receipt in enumerate(receipt_values)
    )


def _all_receipts_applied(receipt_values: Sequence[DurableReceipt]) -> bool:
    return bool(receipt_values) and all(
        receipt.status == ReceiptStatus.APPLIED for receipt in receipt_values
    )


def _recompute_after_receipts(state: PersistedState) -> None:
    if state.local_events is None:
        if (
            state.application_state
            in {ApplicationState.APPLIED, ApplicationState.NOT_REQUIRED}
            and state.applied_watermark.is_known
        ):
            state.phase = SyncPhase.AWAITING_SNAPSHOT
            state.last_error = None
        else:
            state.phase = SyncPhase.RECONCILE_APPLICATION
            state.last_error = ErrorCode.APPLIED_WATERMARK_MISSING
        return

    selected_range = state.local_events
    if any(
        receipt.status in {ReceiptStatus.CONFLICT, ReceiptStatus.ABANDONED}
        for receipt in state.receipts
    ):
        state.phase = SyncPhase.BLOCKED
        state.last_error = ErrorCode.RECEIPT_CONFLICT
        _clear_download_gate(state)
        return
    if not _coverage_complete(selected_range, state.receipts):
        state.phase = SyncPhase.RECONCILE_RECEIPTS
        state.last_error = ErrorCode.RECEIPT_MISSING
        _clear_download_gate(state)
        return
    if any(receipt.status == ReceiptStatus.UNKNOWN for receipt in state.receipts):
        state.phase = SyncPhase.RECONCILE_RECEIPTS
        state.last_error = ErrorCode.RECEIPT_UNKNOWN
        _clear_download_gate(state)
        return
    if state.application_state in {ApplicationState.UNKNOWN, ApplicationState.PARTIAL}:
        state.phase = SyncPhase.RECONCILE_APPLICATION
        state.last_error = (
            ErrorCode.APPLY_UNKNOWN
            if state.application_state == ApplicationState.UNKNOWN
            else ErrorCode.PARTIAL_APPLY
        )
        _clear_download_gate(state)
        return
    if state.application_state == ApplicationState.CONFLICT:
        state.phase = SyncPhase.BLOCKED
        state.last_error = ErrorCode.APPLY_CONFLICT
        _clear_download_gate(state)
        return
    if state.application_state in {
        ApplicationState.PENDING,
        ApplicationState.NOT_REQUIRED,
    }:
        state.phase = SyncPhase.PENDING_APPLY
        state.last_error = None
        _clear_download_gate(state)
        return
    applied_enough = (
        state.applied_watermark.is_known
        and state.applied_watermark.value >= selected_range.last_sequence
    )
    if not _all_receipts_applied(state.receipts):
        state.phase = SyncPhase.RECONCILE_RECEIPTS
        state.last_error = ErrorCode.APPLIED_RECEIPT_COVERAGE_INCOMPLETE
        _clear_download_gate(state)
    elif applied_enough:
        state.phase = SyncPhase.AWAITING_SNAPSHOT
        state.last_error = None
    else:
        state.phase = SyncPhase.RECONCILE_APPLICATION
        state.last_error = ErrorCode.PARTIAL_APPLY
        _clear_download_gate(state)


def _clear_download_gate(state: PersistedState) -> None:
    state.snapshot = None
    state.permit = None
    state.pull = None


def _invalidate_prepared_overrides(state: PersistedState) -> None:
    for token in state.override_tokens:
        if token.status == OverrideTokenStatus.PREPARED:
            token.status = OverrideTokenStatus.EXPIRED


def _expire_prepared_overrides_at(state: PersistedState, now_ms: int) -> None:
    for token in state.override_tokens:
        if (
            token.status == OverrideTokenStatus.PREPARED
            and token.expires_at_ms <= now_ms
        ):
            token.status = OverrideTokenStatus.EXPIRED


def _validate_snapshot(snapshot: SnapshotEvidence) -> None:
    snapshot.context.validate()
    _string(snapshot.snapshot_id, "snapshot_id", non_empty=True)
    _safe_int(snapshot.applied_watermark, "snapshot_applied_watermark", minimum=0)
    _safe_int(snapshot.snapshot_generation, "snapshot_generation", minimum=0)
    if not isinstance(snapshot.immutable, bool):
        raise _invalid("snapshot_immutable_must_be_boolean")
    if not snapshot.immutable or snapshot.checksum != ChecksumStatus.VERIFIED:
        raise _error(
            ErrorCode.SNAPSHOT_INVALID, "snapshot_must_be_immutable_and_verified"
        )


def _validate_permit_request(request: PermitRequest) -> None:
    if (
        not isinstance(request.permit_id, str)
        or not request.permit_id.strip()
        or not isinstance(request.nonce, str)
        or not request.nonce.strip()
    ):
        raise _error(ErrorCode.PERMIT_INVALID, "permit_identity_empty")


def _validate_permit(permit: DownloadPermit) -> None:
    _validate_permit_request(PermitRequest(permit.permit_id, permit.nonce))
    permit.context.validate()
    _string(permit.snapshot_id, "permit_snapshot_id", non_empty=True)
    _safe_int(permit.applied_watermark, "permit_applied_watermark", minimum=0)
    _safe_int(permit.snapshot_generation, "permit_snapshot_generation", minimum=0)
    if not isinstance(permit.consumed, bool):
        raise _invalid("permit_consumed_must_be_boolean")
    if permit.event_range is not None:
        permit.event_range.validate()


def _validate_pull_progress(progress: PullProgress) -> None:
    _string(progress.permit_id, "pull_permit_id", non_empty=True)
    _string(progress.nonce, "pull_nonce", non_empty=True)
    _string(progress.snapshot_id, "pull_snapshot_id", non_empty=True)
    _safe_int(progress.snapshot_generation, "pull_snapshot_generation", minimum=0)
    _safe_int(
        progress.next_page_index,
        "pull_next_page_index",
        minimum=0,
        maximum=MAX_PAGE_INDEX,
    )
    _safe_int(
        progress.received_pages,
        "pull_received_pages",
        minimum=0,
        maximum=MAX_PAGE_INDEX,
    )
    _safe_int(progress.received_bytes, "pull_received_bytes", minimum=0)
    if not isinstance(progress.complete, bool):
        raise _invalid("pull_complete_must_be_boolean")
    if progress.received_pages != progress.next_page_index:
        raise _invalid("pull_page_count_mismatch")
    if progress.complete and progress.received_pages == 0:
        raise _invalid("completed_pull_without_page")


def _validate_page_shape(page: PullPage) -> None:
    _string(page.permit_id, "page_permit_id", non_empty=True)
    _string(page.nonce, "page_nonce", non_empty=True)
    _string(page.snapshot_id, "page_snapshot_id", non_empty=True)
    _safe_int(page.snapshot_generation, "page_snapshot_generation", minimum=0)
    _safe_int(page.page_index, "page_index", minimum=0, maximum=MAX_PAGE_INDEX)
    _safe_int(page.page_bytes, "page_bytes", minimum=0)
    if page.cursor is not None:
        _string(page.cursor, "page_cursor")
    if not isinstance(page.complete, bool):
        raise _invalid("page_complete_must_be_boolean")
    if not isinstance(page.checksum, ChecksumStatus):
        raise _invalid("page_checksum_unknown")


def _validate_override_token(token: OverrideToken) -> None:
    if not token.token_id.strip() or not token.nonce.strip():
        raise _error(ErrorCode.OVERRIDE_TOKEN_INVALID, "override_token_identity_empty")
    _safe_int(token.expires_at_ms, "override_expires_at_ms", minimum=1)
    token.context.validate()
    token.scope.validate()
    if token.scope != token.context.scope or token.baseline.scope != token.scope:
        raise _error(ErrorCode.SCOPE_MISMATCH, "override_token_scope_mismatch")
    if token.baseline.local_generation != token.context.local_generation:
        raise _error(
            ErrorCode.OVERRIDE_BASELINE_CHANGED, "override_token_generation_mismatch"
        )
    if token.fence.status != ApplyFenceStatus.QUIESCED:
        raise _error(
            ErrorCode.OVERRIDE_FENCE_NOT_QUIESCED,
            "override_token_fence_not_quiesced",
        )
    _validate_apply_fence(token.fence)
    _ensure_exact_context(token.context, token.fence.context)
    if (
        token.fence.local_events != token.baseline.local_events
        or token.fence.receipts != token.baseline.receipts
        or token.fence.applied_watermark != token.baseline.applied_watermark
        or token.fence.application_state != token.baseline.application_state
    ):
        raise _error(
            ErrorCode.OVERRIDE_BASELINE_CHANGED,
            "override_token_fence_baseline_mismatch",
        )
    if token.abandoned_events != token.baseline.local_events:
        raise _error(
            ErrorCode.OVERRIDE_BASELINE_CHANGED,
            "override_token_baseline_range_mismatch",
        )


def _current_override_baseline(state: PersistedState) -> OverrideBaseline:
    if state.context is None:
        raise _transition("override_without_context")
    return OverrideBaseline(
        scope=copy.deepcopy(state.context.scope),
        local_generation=state.context.local_generation,
        local_events=copy.deepcopy(state.local_events),
        receipts=copy.deepcopy(state.receipts),
        applied_watermark=copy.deepcopy(state.applied_watermark),
        snapshot_id=None if state.snapshot is None else state.snapshot.snapshot_id,
        application_state=state.application_state,
    )


def _current_apply_fence(
    state: PersistedState,
    fence_id: str,
    status: ApplyFenceStatus,
) -> ApplyCoordinationFence:
    if state.context is None:
        raise _transition("apply_fence_without_context")
    return ApplyCoordinationFence(
        fence_id=fence_id,
        status=status,
        context=copy.deepcopy(state.context),
        local_events=copy.deepcopy(state.local_events),
        receipts=copy.deepcopy(state.receipts),
        applied_watermark=copy.deepcopy(state.applied_watermark),
        application_state=state.application_state,
    )


def _validate_apply_fence(fence: ApplyCoordinationFence) -> None:
    _string(fence.fence_id, "fence_id", non_empty=True)
    fence.context.validate()
    if fence.local_events is not None:
        _validate_receipts(fence.context, fence.local_events, fence.receipts, False)
        if any(
            left.sequence >= right.sequence
            for left, right in zip(fence.receipts, fence.receipts[1:])
        ):
            raise _error(
                ErrorCode.OVERRIDE_FENCE_INVALID, "override_fence_receipts_not_sorted"
            )
    elif fence.receipts:
        raise _error(
            ErrorCode.OVERRIDE_FENCE_INVALID,
            "override_fence_receipts_without_range",
        )


def _ensure_override_state_eligible(state: PersistedState) -> None:
    has_applied = False
    has_durable = False
    for receipt in state.receipts:
        if receipt.status == ReceiptStatus.APPLIED:
            has_applied = True
        elif receipt.status == ReceiptStatus.DURABLE_RECEIVED:
            has_durable = True
        else:
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE, "override_receipt_not_safe"
            )
    if has_applied and has_durable:
        raise _error(
            ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
            "override_mixed_applied_and_pending_receipts",
        )
    if state.application_state == ApplicationState.PENDING:
        if state.local_events is None:
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                "override_pending_without_event_range",
            )
        if has_applied:
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                "override_pending_application_mismatch",
            )
        if state.receipts and (
            not _coverage_complete(state.local_events, state.receipts)
            or not all(
                receipt.status == ReceiptStatus.DURABLE_RECEIVED
                for receipt in state.receipts
            )
        ):
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                "override_pending_receipt_coverage_incomplete",
            )
    elif state.application_state == ApplicationState.APPLIED:
        if has_durable:
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                "override_applied_state_has_pending_receipt",
            )
        if state.local_events is not None:
            if not (
                has_applied
                and _coverage_complete(state.local_events, state.receipts)
                and _all_receipts_applied(state.receipts)
                and state.applied_watermark.is_known
                and state.applied_watermark.value >= state.local_events.last_sequence
            ):
                raise _error(
                    ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                    "override_applied_state_incomplete",
                )
        elif state.receipts or not state.applied_watermark.is_known:
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                "override_baseline_not_required_state",
            )
    elif state.application_state == ApplicationState.NOT_REQUIRED:
        if (
            state.local_events is not None
            or state.receipts
            or not state.applied_watermark.is_known
        ):
            raise _error(
                ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
                "override_not_required_state_mismatch",
            )
    elif state.application_state == ApplicationState.UNKNOWN:
        raise _error(ErrorCode.CONCURRENT_APPLY_UNKNOWN, "override_application_unknown")
    else:
        raise _error(
            ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
            "override_application_not_complete",
        )


__all__ = [
    "SYNC_V2_SCHEMA_VERSION",
    "LUA_SAFE_INTEGER_MAX",
    "MAX_PAGE_INDEX",
    "ChecksumStatus",
    "UploadOutcome",
    "ReceiptStatus",
    "ApplicationState",
    "SyncPhase",
    "ApplyFenceStatus",
    "OverrideTokenStatus",
    "PullAbortReason",
    "ErrorCode",
    "SyncV2Error",
    "Watermark",
    "SyncScope",
    "SyncContext",
    "EventRange",
    "DurableReceipt",
    "BeginSync",
    "UploadReport",
    "ReceiptQueryReport",
    "AnkiApplicationEvidence",
    "SnapshotEvidence",
    "PermitRequest",
    "DownloadPermit",
    "OpenPullRequest",
    "PullPage",
    "PullProgress",
    "CommitRequest",
    "PullAbortRequest",
    "OverrideBaseline",
    "ApplyCoordinationFence",
    "OverridePrepareRequest",
    "OverrideToken",
    "OverrideConfirmRequest",
    "OverrideAuthorization",
    "SyncAction",
    "LocalEvent",
    "ImmutableReviewEvent",
    "PersistedState",
    "SyncMachine",
]
