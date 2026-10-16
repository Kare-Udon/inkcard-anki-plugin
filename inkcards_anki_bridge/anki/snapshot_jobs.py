"""固定 Anki 26.08.1 的 QueryOp 只读快照任务协调。

本模块只接收显式 scope 和不可变的 operation context。collection 只会作为
官方 QueryOp 的 ``op(collection)`` 参数进入 exporter closure；任务管理器、
HTTP worker 和发布回调都不保存 collection 引用。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .adapter_260801 import (
    DEFAULT_EXPORT_BATCH_SIZE,
    DEFAULT_MAX_SCOPE_CARD_IDS,
    Anki260801Adapter,
)
from .operations import (
    OperationContext,
    OperationResult,
    OperationState,
    QueryOpAdapter,
)


DEFAULT_MAX_DECK_IDS = 64


class SnapshotJobError(Exception):
    """可安全返回给后续 UI/HTTP 层的任务错误。"""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class SnapshotJobState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTED = "started"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELED = "canceled"
    STALE_RESULT_DROPPED = "stale_result_dropped"


_FINAL_STATES = frozenset(
    {
        SnapshotJobState.PUBLISHED,
        SnapshotJobState.FAILED,
        SnapshotJobState.CANCELED,
        SnapshotJobState.STALE_RESULT_DROPPED,
    }
)


class _SnapshotCanceled(Exception):
    pass


class _SnapshotContextStale(Exception):
    pass


def _id_sort_key(value: str) -> Tuple[int, Any, str]:
    if value.isdigit():
        return (0, int(value), value)
    return (1, value, value)


def _normalize_ids(
    values: Optional[Sequence[Any]],
    name: str,
    *,
    maximum: int,
) -> Tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes)):
        raise SnapshotJobError("scope_required", f"{name} must be a non-empty sequence", status=422)
    result = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            raise SnapshotJobError("scope_invalid", f"{name} contains an invalid id", status=422)
        selected = str(value)
        if not selected or any(ord(char) < 0x20 or ord(char) == 0x7F for char in selected):
            raise SnapshotJobError("scope_invalid", f"{name} contains an invalid id", status=422)
        result.add(selected)
    if not result:
        raise SnapshotJobError("scope_required", f"{name} must not be empty", status=422)
    if len(result) > maximum:
        raise SnapshotJobError("scope_too_large", f"{name} exceeds the bounded limit", status=413)
    return tuple(sorted(result, key=_id_sort_key))


@dataclass(frozen=True)
class SnapshotScope:
    """显式 card/deck scope；同一 canonical scope 只有一个活动任务。"""

    kind: str
    ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"card", "deck"} or not self.ids:
            raise ValueError("invalid snapshot scope")
        object.__setattr__(self, "ids", tuple(self.ids))

    @property
    def key(self) -> Tuple[str, Tuple[str, ...]]:
        return self.kind, self.ids

    @property
    def scope_id(self) -> str:
        payload = json.dumps(
            {"kind": self.kind, "ids": list(self.ids)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"{self.kind}s:sha256:{hashlib.sha256(payload).hexdigest()[:32]}"


def make_scope(
    *,
    card_ids: Optional[Sequence[Any]],
    deck_ids: Optional[Sequence[Any]],
    max_card_ids: int = DEFAULT_MAX_SCOPE_CARD_IDS,
    max_deck_ids: int = DEFAULT_MAX_DECK_IDS,
) -> SnapshotScope:
    """规范化窄 scope API；两个字段同时出现或均为空都拒绝。"""

    if card_ids is not None and deck_ids is not None:
        raise SnapshotJobError("scope_invalid", "card_ids and deck_ids are mutually exclusive", status=422)
    if card_ids is not None:
        return SnapshotScope(
            "card",
            _normalize_ids(card_ids, "card_ids", maximum=max_card_ids),
        )
    if deck_ids is not None:
        return SnapshotScope(
            "deck",
            _normalize_ids(deck_ids, "deck_ids", maximum=max_deck_ids),
        )
    raise SnapshotJobError("scope_required", "card_ids or deck_ids is required", status=422)


def _context(value: Any) -> OperationContext:
    try:
        selected = OperationContext.from_value(value)
    except Exception as exc:
        raise SnapshotJobError("context_unavailable", "snapshot context is unavailable", status=409) from exc
    if not selected.profile_identity or selected.generation is None or selected.runtime_epoch is None:
        raise SnapshotJobError(
            "context_unavailable",
            "snapshot context must include profile identity, collection generation, and runtime epoch",
            status=409,
        )
    return selected


class SnapshotJob:
    """可取消的单次 scope 任务句柄。"""

    def __init__(
        self,
        manager: "SnapshotJobManager",
        job_id: str,
        scope: SnapshotScope,
        context: OperationContext,
    ) -> None:
        self._manager = manager
        self.job_id = job_id
        self.scope = scope
        self.context = context
        self._state = SnapshotJobState.CREATED
        self._cancel_requested = False
        self._operation_task: Any = None
        self._publication: Any = None
        self._error: Optional[BaseException] = None
        self._applied_watermark: Optional[int] = None

    @property
    def state(self) -> SnapshotJobState:
        with self._manager._lock:
            return self._state

    @property
    def scope_id(self) -> str:
        return self.scope.scope_id

    @property
    def result(self) -> Any:
        with self._manager._lock:
            return self._publication

    @property
    def error(self) -> Optional[BaseException]:
        with self._manager._lock:
            return self._error

    @property
    def cancel_requested(self) -> bool:
        with self._manager._lock:
            return self._cancel_requested

    @property
    def operation_task(self) -> Any:
        with self._manager._lock:
            return self._operation_task

    def cancel(self) -> bool:
        """请求取消；已开始的 QueryOp 不强杀，但其结果绝不发布。"""

        return self._manager.cancel(self.job_id)

    def to_dict(self) -> Dict[str, Any]:
        with self._manager._lock:
            return {
                "job_id": self.job_id,
                "scope_id": self.scope_id,
                "scope_kind": self.scope.kind,
                "state": self._state.value,
                "cancel_requested": self._cancel_requested,
                "profile_identity": self.context.profile_identity,
                "collection_generation": self.context.generation,
                "runtime_epoch": self.context.runtime_epoch,
            }


class SnapshotJobManager:
    """将 QueryOp、typed exporter 和现有 SnapshotService 窄接线。"""

    def __init__(
        self,
        *,
        operation_adapter: QueryOpAdapter,
        exporter: Anki260801Adapter,
        context_provider: Callable[[], Any],
        publish: Callable[..., Any],
        max_card_ids: int = DEFAULT_MAX_SCOPE_CARD_IDS,
        max_deck_ids: int = DEFAULT_MAX_DECK_IDS,
        export_batch_size: int = DEFAULT_EXPORT_BATCH_SIZE,
        max_active_jobs: int = 16,
        max_retained_jobs: int = 64,
    ) -> None:
        if not isinstance(operation_adapter, QueryOpAdapter):
            raise TypeError("operation_adapter must be a QueryOpAdapter")
        if not isinstance(exporter, Anki260801Adapter):
            raise TypeError("exporter must be an Anki260801Adapter")
        if not callable(context_provider) or not callable(publish):
            raise TypeError("context_provider and publish must be callable")
        if isinstance(max_card_ids, bool) or not isinstance(max_card_ids, int) or max_card_ids <= 0:
            raise ValueError("max_card_ids must be a positive integer")
        if isinstance(max_deck_ids, bool) or not isinstance(max_deck_ids, int) or max_deck_ids <= 0:
            raise ValueError("max_deck_ids must be a positive integer")
        if isinstance(export_batch_size, bool) or not isinstance(export_batch_size, int) or export_batch_size <= 0:
            raise ValueError("export_batch_size must be a positive integer")
        if isinstance(max_active_jobs, bool) or not isinstance(max_active_jobs, int) or max_active_jobs <= 0:
            raise ValueError("max_active_jobs must be a positive integer")
        if isinstance(max_retained_jobs, bool) or not isinstance(max_retained_jobs, int) or max_retained_jobs < max_active_jobs:
            raise ValueError("max_retained_jobs must be at least max_active_jobs")
        self._max_active_jobs = max_active_jobs
        self._max_retained_jobs = max_retained_jobs
        self._operation_adapter = operation_adapter
        self._exporter = exporter
        self._context_provider = context_provider
        self._publish = publish
        self._max_card_ids = max_card_ids
        self._max_deck_ids = max_deck_ids
        self._export_batch_size = export_batch_size
        self._lock = threading.RLock()
        self._closed = False
        self._next_id = 0
        self._active_by_scope: Dict[Tuple[str, Tuple[str, ...]], SnapshotJob] = {}
        self._jobs: Dict[str, SnapshotJob] = {}

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _read_context(self) -> OperationContext:
        try:
            value = self._context_provider()
        except SnapshotJobError:
            raise
        except Exception as exc:
            raise SnapshotJobError("context_unavailable", "快照上下文不可用", status=409) from exc
        return _context(value)

    def _safe_context(self) -> Optional[OperationContext]:
        try:
            return self._read_context()
        except Exception:
            return None

    def _remove(self, job: SnapshotJob) -> None:
        if self._active_by_scope.get(job.scope.key) is job:
            del self._active_by_scope[job.scope.key]

    def _trim_history(self) -> None:
        for job_id, job in tuple(self._jobs.items()):
            if len(self._jobs) <= self._max_retained_jobs:
                break
            if job._state in _FINAL_STATES:
                del self._jobs[job_id]

    def _finish(
        self,
        job: SnapshotJob,
        state: SnapshotJobState,
        *,
        value: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        job._state = state
        job._publication = value
        # 异常的 traceback/链会反向持有完整导出，仅归档稳定摘要。
        job._error = SnapshotJobError(
            str(getattr(error, "code", "snapshot_job_failed")),
            error.message if isinstance(error, SnapshotJobError) else "快照作业失败",
            status=getattr(error, "status", 409),
        ) if error is not None else None
        # 仅保留发布结果用于轮询，原始完整导出只应活到发布完成。
        job._operation_task = None
        self._remove(job)
        self._trim_history()

    def _ensure_active(self, job: SnapshotJob) -> None:
        with self._lock:
            if self._closed or job._cancel_requested or job._state == SnapshotJobState.CANCELED:
                raise _SnapshotCanceled()
        current = self._safe_context()
        if current != job.context:
            raise _SnapshotContextStale()

    def request_snapshot(
        self,
        *,
        card_ids: Optional[Sequence[Any]] = None,
        deck_ids: Optional[Sequence[Any]] = None,
        applied_watermark: Optional[int] = None,
    ) -> SnapshotJob:
        if applied_watermark is not None and (
            isinstance(applied_watermark, bool) or not isinstance(applied_watermark, int)
            or not 0 <= applied_watermark <= 9007199254740991
        ):
            raise SnapshotJobError("invalid_applied_watermark", "invalid applied watermark")
        scope = make_scope(
            card_ids=card_ids,
            deck_ids=deck_ids,
            max_card_ids=self._max_card_ids,
            max_deck_ids=self._max_deck_ids,
        )
        with self._lock:
            if self._closed:
                raise SnapshotJobError("runtime_stopped", "snapshot jobs are stopped", status=409)
            if scope.key in self._active_by_scope:
                raise SnapshotJobError("duplicate_scope", "the same snapshot scope is already queued", status=409)
            if len(self._active_by_scope) >= self._max_active_jobs:
                raise SnapshotJobError("job_capacity", "snapshot job queue is full", status=503)
            context = self._read_context()
            self._next_id += 1
            job = SnapshotJob(self, f"snapshot-{self._next_id}", scope, context)
            job._applied_watermark = applied_watermark
            job._state = SnapshotJobState.QUEUED
            self._active_by_scope[scope.key] = job
            self._jobs[job.job_id] = job
            self._trim_history()

        def operation(collection: Any) -> Any:
            with self._lock:
                if job._state == SnapshotJobState.QUEUED:
                    job._state = SnapshotJobState.STARTED
            self._ensure_active(job)
            return self._exporter.export_snapshot_from_collection(
                collection,
                card_ids=scope.ids if scope.kind == "card" else None,
                deck_ids=scope.ids if scope.kind == "deck" else None,
                context=job.context,
                max_card_ids=self._max_card_ids,
                batch_size=self._export_batch_size,
                cancel_check=lambda: self._ensure_active(job),
            )

        try:
            task = self._operation_adapter.submit(
                context,
                operation,
                on_complete=lambda result: self._on_operation_result(job, result),
            )
        except BaseException:
            with self._lock:
                self._finish(job, SnapshotJobState.FAILED, error=RuntimeError("query operation submission failed"))
            raise
        with self._lock:
            if job._state not in _FINAL_STATES:
                job._operation_task = task
        return job

    def _on_operation_result(self, job: SnapshotJob, result: OperationResult) -> None:
        # QueryOp success/failure callbacks are the only place where a completed
        # typed snapshot is considered for publication. The lock is held across
        # the atomic service publish boundary, so a concurrent cancel cannot turn
        # an in-flight publication into a half-canceled snapshot.
        with self._lock:
            if job._state in _FINAL_STATES:
                return
            if job._cancel_requested or self._closed:
                self._finish(job, SnapshotJobState.CANCELED)
                return
            if result.state is OperationState.STALE_RESULT_DROPPED or result.discarded:
                self._finish(
                    job,
                    SnapshotJobState.STALE_RESULT_DROPPED,
                    error=result.error,
                )
                return
            if result.state is not OperationState.COMPLETED:
                self._finish(
                    job,
                    SnapshotJobState.FAILED,
                    error=result.error or RuntimeError("query operation failed"),
                )
                return
            if self._safe_context() != job.context:
                self._finish(job, SnapshotJobState.STALE_RESULT_DROPPED)
                return
            job._state = SnapshotJobState.PUBLISHING
            try:
                publication = self._publish(
                    result.value,
                    scope_id=job.scope.scope_id,
                    generation=job.context.generation,
                    expected_context=job.context,
                    source_scope={"kind": job.scope.kind, "ids": list(job.scope.ids)},
                    **({"applied_watermark": job._applied_watermark}
                       if job._applied_watermark is not None else {}),
                )
            except BaseException as exc:
                # SnapshotService.publish() is transactional. The manager does
                # not replace its last publication on failure.
                self._finish(job, SnapshotJobState.FAILED, error=exc)
                return
            self._finish(job, SnapshotJobState.PUBLISHED, value=publication)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job._state in _FINAL_STATES or job._state is SnapshotJobState.PUBLISHING:
                return False
            job._cancel_requested = True
            operation_task = job._operation_task
            if operation_task is None:
                self._finish(job, SnapshotJobState.CANCELED)
                return True
            operation_task.cancel()
            if job._state is SnapshotJobState.QUEUED:
                self._finish(job, SnapshotJobState.CANCELED)
            # OperationTask.cancel() returns False after start, but records the
            # request; the completion callback will discard its value.
            return True

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._active_by_scope.values())
            for job in jobs:
                job._cancel_requested = True
                operation_task = job._operation_task
                if operation_task is None:
                    self._finish(job, SnapshotJobState.CANCELED)
                else:
                    operation_task.cancel()
                    if job._state is SnapshotJobState.QUEUED:
                        self._finish(job, SnapshotJobState.CANCELED)

    def get(self, job_id: str) -> Optional[SnapshotJob]:
        with self._lock:
            return self._jobs.get(job_id)


__all__ = [
    "DEFAULT_MAX_DECK_IDS",
    "SnapshotJob",
    "SnapshotJobError",
    "SnapshotJobManager",
    "SnapshotJobState",
    "SnapshotScope",
    "make_scope",
]
