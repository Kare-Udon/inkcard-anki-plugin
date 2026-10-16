"""InkCards storage 对外返回的无秘密数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional, Tuple

if TYPE_CHECKING:
    from ..anki.adopted_restore import AdoptedApplicationContext
    from ..anki.application_context import SnapshotApplicationContext


CompactApplicationStatus = Literal[
    "not_applied", "applying", "applied", "unknown", "conflict"
]


@dataclass(frozen=True)
class Receipt:
    """单个事件的持久交付收据；不包含设备 token 或 secret。"""

    event_id: str
    sequence: int
    status: str
    payload_hash: Optional[str] = None
    reason: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """把收据转换为不含认证材料的协议 payload。"""

        payload: Dict[str, Any] = {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "status": self.status,
        }
        if self.payload_hash is not None:
            payload["payload_hash"] = self.payload_hash
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class BatchReceipt:
    """一次已经成功提交的批次收据集合。"""

    receipts: Tuple[Receipt, ...]
    committed_at_ms: int
    durable: bool = True

    def to_payload(self) -> Dict[str, Any]:
        """把批次收据转换为不含认证材料的协议 payload。"""

        return {
            "durable": self.durable,
            "committed_at_ms": self.committed_at_ms,
            "receipts": [receipt.to_payload() for receipt in self.receipts],
        }


@dataclass(frozen=True)
class StoredEvent:
    """事件的持久摘要；原文和 payload 隐藏在 repr 之外以避免意外泄露。"""

    profile_id: str
    device_id: str
    session_id: str
    sequence: int
    event_id: str
    payload_hash: str
    payload: Dict[str, Any] = field(repr=False)
    raw_payload_json: str = field(repr=False)
    normalized_payload_json: str = field(repr=False)


@dataclass(frozen=True)
class ConflictRecord:
    """同一不可变事件键收到异文时留下的证据。"""

    device_id: str
    session_id: str
    sequence: int
    event_id: str
    observed_payload_hash: str
    reason: str
    created_at_ms: int
    observed_payload_json: str = field(repr=False)


@dataclass(frozen=True)
class ApplicationRecord:
    """独立于 durable receipt 的应用状态记录。"""

    event_id: str
    sequence: int
    status: str
    intent_id: Optional[str] = None
    attempt_id: Optional[str] = None
    reason: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """把应用查询结果转换为不含认证材料的 payload。"""

        payload: Dict[str, Any] = {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "status": self.status,
        }
        if self.intent_id is not None:
            payload["intent_id"] = self.intent_id
        if self.attempt_id is not None:
            payload["attempt_id"] = self.attempt_id
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class CompactEventRecord:
    """x4 compact 事件的类型化收件视图，与普通 StoredEvent 命名空间分离。"""

    profile_id: str
    device_id: str
    session_id: str
    sequence: int
    event_id: str
    snapshot_id: str
    card_id: str
    card_index: int
    rating: int
    answered_at_ms: int
    duration_ms: int
    payload_hash: str
    received_at_ms: int


@dataclass(frozen=True)
class CompactApplicationRecord:
    """Compact 应用计划、before/after 和终态收据；JSON 不进入 repr。"""

    profile_id: str
    device_id: str
    session_id: str
    sequence: int
    event_id: str
    payload_hash: str
    status: CompactApplicationStatus
    recovery_required: bool
    collection_identity: Optional[str]
    collection_generation: Optional[int]
    collection_generation_after: Optional[int] = None
    policy_json: Optional[str] = field(default=None, repr=False)
    before_json: Optional[str] = field(default=None, repr=False)
    before_hash: Optional[str] = None
    plan_json: Optional[str] = field(default=None, repr=False)
    expected_after_json: Optional[str] = field(default=None, repr=False)
    after_json: Optional[str] = field(default=None, repr=False)
    after_hash: Optional[str] = None
    intent_id: Optional[str] = None
    attempt_id: Optional[str] = None
    reason: Optional[str] = None
    receipt_json: Optional[str] = field(default=None, repr=False)
    receipt_hash: Optional[str] = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True)
class CompactScoreEvidenceRecord:
    """一个 compact 评分阶段的不可变 JSON 证据摘要。"""

    profile_id: str
    device_id: str
    session_id: str
    sequence: int
    stage: str
    evidence_json: str = field(repr=False)
    evidence_hash: str
    created_at_ms: int


@dataclass(frozen=True)
class ApplyIntent:
    """一次显式应用尝试的身份和证据边界。"""

    intent_id: str
    attempt_id: str
    status: str
    event_ids: Tuple[str, ...]
    created_at_ms: int
    reason: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        """把应用 intent 转换为不含认证材料的 payload。"""

        payload: Dict[str, Any] = {
            "intent_id": self.intent_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "event_ids": list(self.event_ids),
            "created_at_ms": self.created_at_ms,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class DeviceRecord:
    """设备身份摘要；永远不返回原始 token 或 token hash。"""

    profile_id: str
    device_id: str
    created_at_ms: int
    last_seen_at_ms: Optional[int]
    revoked_at_ms: Optional[int]


@dataclass(frozen=True)
class SessionRecord:
    """设备 session 的序号水位摘要。"""

    profile_id: str
    device_id: str
    session_id: str
    high_water: int
    created_at_ms: int
    last_seen_at_ms: int
    closed_at_ms: Optional[int]


@dataclass(frozen=True)
class PullGate:
    """从当前 SQLite receipt/application 记录计算出的 pull 门槛。"""

    allowed: bool
    reason: Optional[str]
    application_state: str
    applied_watermark: Optional[int]
    event_count: int
    high_water: int


@dataclass(frozen=True)
class SnapshotRecord:
    """不可变快照的持久摘要；payload 只在显式读取时返回。"""

    profile_id: str
    snapshot_id: str
    scope_id: str
    scope_generation: int
    snapshot_generation: int
    applied_watermark: int
    payload_json: str
    checksum: str
    manifest_json: str
    card_count: int
    included_card_count: int
    skipped_count: int
    payload_bytes: int
    page_count: int
    peak_bytes_estimate: int
    created_at_ms: int
    application_context: Optional["SnapshotApplicationContext | AdoptedApplicationContext"] = None
    # 仅电脑端核验来源的内存视图使用；不保存到原快照，不修改设备 checksum。
    restoration_source_sha256: str | None = None
    # 仅 adopted_restore 内存视图；原持久快照仍没有冻结 context。
    adopted_source: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class PermitRecord:
    """一次性 permit 的持久字段；只保存 nonce hash。"""

    profile_id: str
    permit_id: str
    device_id: str
    session_id: str
    scope_id: str
    local_generation: int
    snapshot_id: str
    snapshot_generation: int
    applied_watermark: int
    nonce_hash: str
    status: str
    expires_at_ms: int
    created_at_ms: int
    used_at_ms: Optional[int]


@dataclass(frozen=True)
class PullProgressRecord:
    """可恢复的分页进度；最后一页用于丢响应后的同页幂等重放。"""

    permit_id: str
    next_page_index: int
    received_pages: int
    received_bytes: int
    complete: bool
    last_page_index: int
    last_cursor: Optional[str]
    last_page_json: Optional[str]
