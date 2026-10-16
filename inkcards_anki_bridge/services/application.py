"""InkCards 事件到 Anki application intent 的最小协调器。

服务层只协调 ProfileStore 与显式 application adapter。它不直接读写 Anki
SQLite，也不把 durable delivery receipt 当作 applied。固定 Anki 26.08.1
历史 scheduler capability 当前 disabled，因此 disabled 路径不创建 intent，
保留事件的 ``pending_apply`` 证据并返回 ``blocked``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from ..anki.application_260801 import (
    ApplicationBlockedError,
    ApplicationConflictError,
    ApplicationError,
    ApplicationResult,
    CapabilityReport,
)
from ..storage import ApplicationRecord, ProfileStore


@dataclass(frozen=True)
class ServiceApplicationResult:
    event_id: str
    sequence: int
    status: str
    reason: Optional[str] = None
    intent_id: Optional[str] = None
    attempt_id: Optional[str] = None
    adapter_result: Optional[ApplicationResult] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        result = {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "status": self.status,
        }
        for key, value in (
            ("reason", self.reason),
            ("intent_id", self.intent_id),
            ("attempt_id", self.attempt_id),
        ):
            if value is not None:
                result[key] = value
        if self.adapter_result is not None:
            result["adapter"] = self.adapter_result.to_dict()
        return result


@dataclass(frozen=True)
class ApplicationBatchResult:
    status: str
    results: Tuple[ServiceApplicationResult, ...]
    capability: CapabilityReport
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "results": [item.to_dict() for item in self.results],
            "capability": self.capability.to_dict(),
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class ApplicationService:
    """串行应用 pending event，并把明确结果连接到 ProfileStore 状态机。"""

    def __init__(self, store: ProfileStore, adapter: Any) -> None:
        if not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        if adapter is None or not callable(getattr(adapter, "capability_report", None)):
            raise TypeError("adapter must expose capability_report()")
        self.store = store
        self.adapter = adapter

    def capability_report(self) -> CapabilityReport:
        report = self.adapter.capability_report()
        if not isinstance(report, CapabilityReport):
            raise TypeError("adapter capability_report() must return CapabilityReport")
        return report

    def apply_pending(
        self,
        device_id: str,
        session_id: str,
        *,
        limit: Optional[int] = None,
    ) -> ApplicationBatchResult:
        """按 sequence 顺序应用明确 pending；UNKNOWN 永不自动重放。"""

        report = self.capability_report()
        pending = self.store.pending_apply(device_id, session_id)
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("limit must be a positive integer")
            pending = pending[:limit]
        if not report.write_enabled:
            results = tuple(
                ServiceApplicationResult(
                    event_id=record.event_id,
                    sequence=record.sequence,
                    status="blocked",
                    reason=report.reason or "history_application_disabled",
                )
                for record in pending
            )
            return ApplicationBatchResult(
                "blocked", results, report, report.reason or "history_application_disabled"
            )
        if not pending:
            return ApplicationBatchResult("idle", (), report)

        results: List[ServiceApplicationResult] = []
        for record in pending:
            results.append(self._apply_one(device_id, session_id, record))
        status = self._batch_status(results)
        return ApplicationBatchResult(status, tuple(results), report)

    def _apply_one(
        self, device_id: str, session_id: str, record: ApplicationRecord
    ) -> ServiceApplicationResult:
        event = self.store.get_event(device_id, session_id, record.sequence)
        intent = self.store.begin_apply(device_id, session_id, [record.sequence])
        try:
            # prepare_event 必须在 intent 之后再次读取 profile/generation/card revision
            # 和 before state；旧的 delivery receipt 不能替代这次实时 gate。
            prepared = self.adapter.prepare_event(event)
            adapter_result = self.adapter.apply_prepared(prepared)
            if not isinstance(adapter_result, ApplicationResult):
                raise ApplicationError(
                    "invalid_adapter_result", "application adapter returned an invalid result"
                )
            if adapter_result.event_id != event.event_id:
                raise ApplicationConflictError("event_identity_changed", "adapter result event identity changed")
            if adapter_result.status != "applied":
                if adapter_result.status == "conflict":
                    self.store.transition_event(
                        device_id,
                        session_id,
                        record.sequence,
                        "conflict",
                        reason=adapter_result.reason or "adapter_conflict",
                    )
                    return ServiceApplicationResult(
                        event.event_id,
                        record.sequence,
                        "conflict",
                        adapter_result.reason or "adapter_conflict",
                        intent.intent_id,
                        intent.attempt_id,
                        adapter_result,
                    )
                self.store.finish_apply(
                    intent.intent_id,
                    applied=False,
                    reason=adapter_result.reason or "adapter_result_unknown",
                )
                return ServiceApplicationResult(
                    event.event_id,
                    record.sequence,
                    "unknown",
                    adapter_result.reason or "adapter_result_unknown",
                    intent.intent_id,
                    intent.attempt_id,
                    adapter_result,
                )
            self.store.finish_apply(
                intent.intent_id,
                applied=True,
                reason="anki_result_verified",
            )
            return ServiceApplicationResult(
                event.event_id,
                record.sequence,
                "applied",
                "anki_result_verified",
                intent.intent_id,
                intent.attempt_id,
                adapter_result,
            )
        except ApplicationConflictError as exc:
            self.store.transition_event(
                device_id,
                session_id,
                record.sequence,
                "conflict",
                reason=exc.code,
            )
            return ServiceApplicationResult(
                event.event_id,
                record.sequence,
                "conflict",
                exc.code,
                intent.intent_id,
                intent.attempt_id,
            )
        except ApplicationBlockedError as exc:
            # capability 在 intent 后发生变化，无法证明是否有 Anki mutation；
            # 按未知处理，不能回到 pending 或自动重放。
            self.store.finish_apply(intent.intent_id, applied=False, reason=exc.code)
            return ServiceApplicationResult(
                event.event_id,
                record.sequence,
                "unknown",
                exc.code,
                intent.intent_id,
                intent.attempt_id,
            )
        except Exception as exc:
            code = getattr(exc, "code", "apply_unknown")
            self.store.finish_apply(intent.intent_id, applied=False, reason=str(code))
            return ServiceApplicationResult(
                event.event_id,
                record.sequence,
                "unknown",
                str(code),
                intent.intent_id,
                intent.attempt_id,
            )

    def recover(self, device_id: str, session_id: str) -> ApplicationBatchResult:
        """读取持久 application 分类；不把 UNKNOWN 重新送入 adapter。"""

        report = self.capability_report()
        records = self.store.query_applications(device_id, session_id)
        results: List[ServiceApplicationResult] = []
        for record in records:
            if record.status == "applied":
                status = "applied"
                reason = "durable_application_record"
            elif record.status == "conflict":
                status = "conflict"
                reason = record.reason or "durable_conflict_record"
            elif record.status in ("unknown", "applying"):
                status = "unknown"
                reason = record.reason or "unknown_application_is_not_retryable"
            else:
                status = record.status
                reason = record.reason
            results.append(
                ServiceApplicationResult(
                    record.event_id,
                    record.sequence,
                    status,
                    reason,
                    record.intent_id,
                    record.attempt_id,
                )
            )
        if any(item.status == "unknown" for item in results):
            status = "unknown"
        elif any(item.status == "conflict" for item in results):
            status = "conflict"
        elif any(item.status == "pending_apply" for item in results):
            status = "pending_apply"
        elif results:
            status = "applied"
        else:
            status = "idle"
        return ApplicationBatchResult(status, tuple(results), report)

    @staticmethod
    def _batch_status(results: List[ServiceApplicationResult]) -> str:
        if any(item.status == "unknown" for item in results):
            return "unknown"
        if any(item.status == "conflict" for item in results):
            return "conflict"
        if any(item.status == "blocked" for item in results):
            return "blocked"
        if results and all(item.status == "applied" for item in results):
            return "applied"
        return "unknown"


AnkiApplicationService = ApplicationService


__all__ = [
    "AnkiApplicationService",
    "ApplicationBatchResult",
    "ApplicationService",
    "ServiceApplicationResult",
]
