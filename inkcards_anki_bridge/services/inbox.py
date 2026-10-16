"""持久收件与 receipt/application 查询服务。

这里不启动 HTTP、不访问 Anki collection，也不把 durable receipt 解释为已应用。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from ..storage import (
    ApplicationRecord,
    ApplyIntent,
    BatchReceipt,
    CompactApplicationRecord,
    ProfileStore,
    Receipt,
)


class InboxService:
    """为未来 LAN route 提供不依赖 transport 的持久收件 facade。"""

    def __init__(self, store: ProfileStore) -> None:
        """绑定一个已打开的 profile store。"""

        if not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        self.store = store

    def receive_batch(
        self,
        device_id: str,
        session_id: str,
        events: Sequence[Any],
        *,
        now_ms: Optional[int] = None,
    ) -> BatchReceipt:
        """接收一批不可变事件，并仅在 SQLite COMMIT 成功后返回 durable receipt。"""

        receipts = self.store.receive_batch(
            device_id, session_id, events, now_ms=now_ms
        )
        committed_at_ms = now_ms if now_ms is not None else self._committed_time()
        return BatchReceipt(tuple(receipts), committed_at_ms, True)

    def query_receipts(
        self,
        device_id: str,
        session_id: str,
        *,
        event_ids: Optional[Sequence[str]] = None,
        after_sequence: Optional[int] = None,
    ) -> List[Receipt]:
        """查询 durable delivery receipts，解决 ACK 丢失而不触发重放。"""

        return self.store.query_receipts(
            device_id,
            session_id,
            event_ids=event_ids,
            after_sequence=after_sequence,
        )

    def query_applications(
        self, device_id: str, session_id: str
    ) -> List[ApplicationRecord]:
        """查询独立的 Anki application 状态。"""

        return self.store.query_applications(device_id, session_id)

    def query_compact_receipts(self, device_id: str, session_id: str) -> List[Receipt]:
        """查询仍表示 received 的 compact 交付回执。"""

        return self.store.query_compact_receipts(device_id, session_id)

    def query_compact_applications(
        self, device_id: str, session_id: str
    ) -> List[CompactApplicationRecord]:
        """查询独立的 compact 应用终态与阻塞状态。"""

        return self.store.query_compact_applications(device_id, session_id)

    def compact_applied_watermark(self, device_id: str, session_id: str) -> Optional[int]:
        """返回连续且终态 receipt / 证据 hash 全部核验通过的 compact watermark。"""

        return self.store.compact_applied_watermark(device_id, session_id)

    def begin_apply(
        self,
        device_id: str,
        session_id: str,
        sequences: Sequence[int],
        *,
        intent_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> ApplyIntent:
        """创建显式 apply intent；存储层不执行 Anki。"""

        return self.store.begin_apply(
            device_id,
            session_id,
            sequences,
            intent_id=intent_id,
            attempt_id=attempt_id,
        )

    def finish_apply(
        self, intent_id: str, *, applied: bool, reason: Optional[str] = None
    ) -> ApplyIntent:
        """记录 apply 成功或未知，不把 UNKNOWN 自动放回可重试队列。"""

        return self.store.finish_apply(intent_id, applied=applied, reason=reason)

    def retryable(self, device_id: str, session_id: str) -> List[ApplicationRecord]:
        """返回可进入新 apply intent 的事件；UNKNOWN、冲突和已应用均排除。"""

        return self.store.pending_apply(device_id, session_id)

    def _committed_time(self) -> int:
        """读取最近一条 receipt 的时间不可用时返回本地查询时间。"""

        import time

        return int(time.time() * 1000)


Inbox = InboxService

__all__ = ["Inbox", "InboxService"]
