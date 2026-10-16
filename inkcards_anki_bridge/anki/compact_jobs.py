"""为 compact 应用提供有界的 CollectionOp 作业编排。

每个 CollectionOp 闭包只调用一次注入的 ``apply_one``，并返回已批准的 compact
操作结果。本模块不导入或构造原生服务、适配器、授权或旧事件来源证据；这些对象
由原生应用模块按已确认的接口创建。
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from enum import Enum
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from .operations import (
    CollectionOpAdapter,
    CollectionStartRejected,
    OperationContext,
    OperationResult,
    OperationState,
)


class CompactJobError(Exception):
    """可安全返回给路由层的 compact 作业请求错误。"""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class CompactJobState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTED = "started"
    IDLE = "idle"
    APPLIED = "applied"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    FAILED = "failed"
    CANCELED = "canceled"
    STALE_RESULT_DROPPED = "stale_result_dropped"


_FINAL_STATES = frozenset(
    {
        CompactJobState.IDLE,
        CompactJobState.APPLIED,
        CompactJobState.BLOCKED,
        CompactJobState.UNKNOWN,
        CompactJobState.CONFLICT,
        CompactJobState.FAILED,
        CompactJobState.CANCELED,
        CompactJobState.STALE_RESULT_DROPPED,
    }
)


def _identifier(value: Any, name: str) -> str:
    if value is None or isinstance(value, (bool, bytes, bytearray)):
        raise CompactJobError("invalid_identity", f"{name} is invalid", status=422)
    selected = str(value)
    if (
        not selected
        or len(selected) > 128
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in selected)
    ):
        raise CompactJobError("invalid_identity", f"{name} is invalid", status=422)
    return selected


def _context(value: Any) -> OperationContext:
    try:
        selected = OperationContext.from_value(value)
    except Exception as exc:
        raise CompactJobError("context_unavailable", "collection context is unavailable") from exc
    if (
        not selected.profile_identity
        or selected.runtime_epoch is None
        or isinstance(selected.generation, bool)
        or not isinstance(selected.generation, int)
        or selected.generation < 0
    ):
        raise CompactJobError("context_unavailable", "collection context is incomplete")
    return selected


class CompactJob:
    """一个有界的会话作业请求；对象不会保存 Anki Collection。"""

    def __init__(
        self,
        manager: "CompactJobManager",
        job_id: str,
        device_id: str,
        session_id: str,
        request_context: OperationContext,
    ) -> None:
        self._manager = manager
        self.job_id = job_id
        self.device_id = device_id
        self.session_id = session_id
        self.request_context = request_context
        self.context = request_context
        self._state = CompactJobState.CREATED
        self._cancel_requested = False
        self._operation_task: Any = None
        self._operation_result: Optional[OperationResult] = None
        self._result_status: Optional[str] = None
        self._reason: Optional[str] = None
        self._generation_before: Optional[int] = None
        self._generation_after: Optional[int] = None
        self._events_applied = 0

    @property
    def state(self) -> CompactJobState:
        with self._manager._lock:
            return self._state

    @property
    def operation_task(self) -> Any:
        with self._manager._lock:
            return self._operation_task

    @property
    def cancel_requested(self) -> bool:
        with self._manager._lock:
            return self._cancel_requested

    def cancel(self) -> bool:
        return self._manager.cancel(self.job_id)

    def to_dict(self) -> Dict[str, Any]:
        """返回精简状态视图，不包含事件正文、卡片数据或 profile 路径。"""

        with self._manager._lock:
            return {
                "job_id": self.job_id,
                "device_id": self.device_id,
                "session_id": self.session_id,
                "state": self._state.value,
                "result_status": self._result_status,
                "reason": self._reason,
                "cancel_requested": self._cancel_requested,
                "profile_identity": self.context.profile_identity,
                "collection_generation": self.context.generation,
                "runtime_epoch": self.context.runtime_epoch,
                "generation_before": self._generation_before,
                "generation_after": self._generation_after,
                "events_applied": self._events_applied,
            }


class CompactJobManager:
    """通过 CollectionOp 适配器串行执行一次一条事件的原生调用。

    ``apply_one(collection, device_id, session_id, context)`` 是唯一服务接口。
    注入的函数必须使用原生任务已确认的适配器和授权辅助函数，每次至多处理一条
    事件，并返回带有 ``status``、``generation_before``、``generation_after``、
    ``refresh_context``、``more_pending`` 和真实 ``changes`` 的结果。HTTP 调用方
    只排入会话作业并读取缓存状态；只有 CollectionOp 闭包会收到 ``collection``。
    """

    def __init__(
        self,
        *,
        operation_adapter: CollectionOpAdapter,
        context_provider: Callable[[], Any],
        apply_one: Callable[[Any, str, str, OperationContext], Any],
        update_collection_generation: Callable[[int], None],
        reviewer_available: Callable[[], bool],
        max_active_jobs: int = 16,
        max_retained_jobs: int = 64,
        diagnostics: Any = None,
    ) -> None:
        if not isinstance(operation_adapter, CollectionOpAdapter):
            raise TypeError("operation_adapter must be a CollectionOpAdapter")
        for callback, name in (
            (context_provider, "context_provider"),
            (apply_one, "apply_one"),
            (update_collection_generation, "update_collection_generation"),
            (reviewer_available, "reviewer_available"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if isinstance(max_active_jobs, bool) or not isinstance(max_active_jobs, int) or max_active_jobs < 1:
            raise ValueError("max_active_jobs must be a positive integer")
        if (
            isinstance(max_retained_jobs, bool)
            or not isinstance(max_retained_jobs, int)
            or max_retained_jobs < max_active_jobs
        ):
            raise ValueError("max_retained_jobs must be at least max_active_jobs")

        self._diagnostics = diagnostics
        self._operation_adapter = operation_adapter
        self._context_provider = context_provider
        self._apply_one = apply_one
        self._update_collection_generation = update_collection_generation
        self._reviewer_available = reviewer_available
        self._max_active_jobs = max_active_jobs
        self._max_retained_jobs = max_retained_jobs
        self._lock = threading.RLock()
        self._closed = False
        self._next_id = 0
        self._queue: Deque[CompactJob] = deque()
        self._active_by_key: Dict[Tuple[str, str], CompactJob] = {}
        self._latest_by_key: Dict[Tuple[str, str], CompactJob] = {}
        self._jobs: "OrderedDict[str, CompactJob]" = OrderedDict()
        self._current: Optional[CompactJob] = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _read_context(self) -> OperationContext:
        try:
            value = self._context_provider()
        except CompactJobError:
            raise
        except Exception as exc:
            raise CompactJobError("context_unavailable", "评分上下文不可用") from exc
        return _context(value)

    def _safe_context(self) -> Optional[OperationContext]:
        try:
            return self._read_context()
        except Exception:
            return None

    def _trim_history(self) -> None:
        while len(self._jobs) > self._max_retained_jobs:
            removable = next(
                (job_id for job_id, job in self._jobs.items() if job._state in _FINAL_STATES),
                None,
            )
            if removable is None:
                return
            job = self._jobs.pop(removable)
            key = (job.device_id, job.session_id)
            if self._latest_by_key.get(key) is job:
                self._latest_by_key.pop(key, None)

    def _finish(
        self,
        job: CompactJob,
        state: CompactJobState,
        *,
        result_status: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        job._state = state
        job._result_status = result_status
        job._reason = reason
        if self._diagnostics is not None:
            self._diagnostics.record_terminal(state.value, reason)
        key = (job.device_id, job.session_id)
        if self._active_by_key.get(key) is job:
            self._active_by_key.pop(key, None)
        if self._current is job:
            self._current = None

    def request(self, device_id: Any, session_id: Any) -> CompactJob:
        """合并同一活跃会话请求并排队，不读取 Collection。"""

        device = _identifier(device_id, "device_id")
        session = _identifier(session_id, "session_id")
        key = (device, session)
        with self._lock:
            if self._closed:
                raise CompactJobError("runtime_stopped", "compact jobs are stopped")
            active = self._active_by_key.get(key)
            if active is not None:
                return active
            if len(self._active_by_key) >= self._max_active_jobs:
                raise CompactJobError("job_capacity", "compact job queue is full", status=503)
            context = self._read_context()
            self._next_id += 1
            job = CompactJob(self, f"compact-{self._next_id}", device, session, context)
            job._state = CompactJobState.QUEUED
            self._active_by_key[key] = job
            self._latest_by_key[key] = job
            self._jobs[job.job_id] = job
            self._trim_history()
            self._queue.append(job)
        self._start_next()
        return job

    def get(self, job_id: str) -> Optional[CompactJob]:
        with self._lock:
            return self._jobs.get(str(job_id))

    def status_for(self, device_id: Any, session_id: Any) -> Optional[Dict[str, Any]]:
        key = (_identifier(device_id, "device_id"), _identifier(session_id, "session_id"))
        with self._lock:
            job = self._active_by_key.get(key) or self._latest_by_key.get(key)
        return job.to_dict() if job is not None else None

    def _reviewer_gate(self) -> bool:
        try:
            return self._reviewer_available() is True
        except BaseException:
            return False

    def _start_next(self) -> None:
        while True:
            with self._lock:
                if self._closed or self._current is not None:
                    return
                if not self._queue:
                    return
                job = self._queue.popleft()
                if job._state in _FINAL_STATES or job._cancel_requested:
                    self._finish(job, CompactJobState.CANCELED, reason="canceled_before_start")
                    continue
                current = self._safe_context()
                if (
                    current is None
                    or current.profile_identity != job.request_context.profile_identity
                    or current.runtime_epoch != job.request_context.runtime_epoch
                ):
                    self._finish(job, CompactJobState.STALE_RESULT_DROPPED, reason="runtime_context_changed")
                    continue
                # 前一条评分可能推进了 generation；每个新 CollectionOp 都重新读取 context。
                job.context = current
                job._state = CompactJobState.STARTED
                self._current = job
                break

        def operation(collection: Any) -> Any:
            # 主线程启动时先检查 Reviewer。排队期间所有权可能变化，因此在服务写入前再查一次。
            if not self._reviewer_gate():
                raise CollectionStartRejected("reviewer_owns_collection")
            return self._apply_one(collection, job.device_id, job.session_id, job.context)

        try:
            task = self._operation_adapter.submit(
                job.context,
                operation,
                on_complete=lambda result: self._on_operation_result(job, result),
                on_collection_complete=lambda result: self._on_collection_complete(job, result),
                on_collection_failure=lambda error: self._on_collection_failure(job, error),
                before_collection_start=self._reviewer_gate,
            )
        except BaseException:
            with self._lock:
                self._finish(job, CompactJobState.FAILED, reason="operation_submit_failed")
            self._start_next()
            return

        with self._lock:
            job._operation_task = task
            canceled = job._cancel_requested and job._state not in _FINAL_STATES
            finalized = job._state in _FINAL_STATES
            task_state = task.state
        if canceled:
            task.cancel()
        if task_state is OperationState.FAILED and not finalized:
            error = task.result.error
            self._on_collection_failure(job, error or RuntimeError("collection operation submission failed"))

    def _on_operation_result(self, job: CompactJob, result: OperationResult) -> None:
        with self._lock:
            if job._state in _FINAL_STATES:
                return
            job._operation_result = result

    def _on_collection_complete(self, job: CompactJob, result: Any) -> None:
        with self._lock:
            if job._state in _FINAL_STATES:
                if self._current is job:
                    self._current = None
                should_drain = True
                operation_result = job._operation_result
            else:
                should_drain = False
                operation_result = job._operation_result

        if should_drain:
            self._start_next()
            return
        if operation_result is None:
            self._on_collection_failure(job, RuntimeError("CollectionOp completed without task result"))
            return
        if operation_result.state is OperationState.CANCELED:
            self._finish_and_drain(job, CompactJobState.CANCELED, reason="canceled_before_start")
            return
        if operation_result.state is OperationState.STALE_RESULT_DROPPED and operation_result.executed is False:
            self._finish_and_drain(job, CompactJobState.STALE_RESULT_DROPPED, reason="context_stale_before_start")
            return
        if operation_result.state is OperationState.FAILED:
            state = CompactJobState.UNKNOWN if operation_result.executed is True else CompactJobState.FAILED
            reason = "operation_failed_after_start" if state is CompactJobState.UNKNOWN else "operation_failed"
            self._finish_and_drain(job, state, reason=reason)
            return

        self._consume_result(job, result, operation_result)

    def _consume_result(
        self,
        job: CompactJob,
        result: Any,
        operation_result: OperationResult,
    ) -> None:
        try:
            status = getattr(result, "status")
            generation_before = getattr(result, "generation_before")
            generation_after = getattr(result, "generation_after")
            refresh_context = getattr(result, "refresh_context")
            more_pending = getattr(result, "more_pending")
            reason_value = getattr(result, "reason", None)
        except Exception:
            self._finish_and_drain(job, CompactJobState.UNKNOWN, reason="invalid_operation_result")
            return

        if status not in {"idle", "applied", "blocked", "unknown", "conflict"}:
            self._finish_and_drain(job, CompactJobState.UNKNOWN, reason="invalid_operation_status")
            return
        if (
            isinstance(generation_before, bool)
            or not isinstance(generation_before, int)
            or isinstance(generation_after, bool)
            or not isinstance(generation_after, int)
            or generation_before < 0
            or generation_after < 0
            or not isinstance(refresh_context, bool)
            or not isinstance(more_pending, bool)
        ):
            self._finish_and_drain(job, CompactJobState.UNKNOWN, reason="invalid_operation_generation")
            return
        if generation_before != job.context.generation:
            self._finish_and_drain(job, CompactJobState.UNKNOWN, result_status=status, reason="generation_before_mismatch")
            return
        if status == "applied" and (
            not refresh_context or generation_after != generation_before + 1
        ):
            self._finish_and_drain(job, CompactJobState.UNKNOWN, result_status=status, reason="generation_transition_invalid")
            return
        if generation_after < generation_before or (status != "applied" and more_pending):
            self._finish_and_drain(job, CompactJobState.UNKNOWN, result_status=status, reason="operation_result_inconsistent")
            return

        owner_unchanged = False
        refreshed_context: Optional[OperationContext] = None
        context_error: Optional[str] = None
        current = self._safe_context()
        if current is not None:
            owner_unchanged = (
                current.profile_identity == job.context.profile_identity
                and current.runtime_epoch == job.context.runtime_epoch
            )
        if refresh_context:
            if not owner_unchanged:
                context_error = "runtime_context_changed_after_collection"
            elif current is None or current.generation not in {generation_before, generation_after}:
                context_error = "runtime_generation_changed_after_collection"
            else:
                try:
                    if current.generation != generation_after:
                        self._update_collection_generation(generation_after)
                    refreshed_context = self._read_context()
                except Exception:
                    context_error = "runtime_generation_sync_failed"
                if refreshed_context is not None and (
                    refreshed_context.profile_identity != job.context.profile_identity
                    or refreshed_context.runtime_epoch != job.context.runtime_epoch
                    or refreshed_context.generation != generation_after
                ):
                    context_error = "runtime_context_refresh_mismatch"
                    refreshed_context = None

        if status == "applied":
            with self._lock:
                job._events_applied += 1
                job._generation_before = generation_before
                job._generation_after = generation_after
                job._result_status = status
                job._reason = context_error or (str(reason_value) if reason_value else None)
                can_continue = (
                    more_pending
                    and context_error is None
                    and refreshed_context is not None
                    and operation_result.state is OperationState.COMPLETED
                    and not job._cancel_requested
                    and not self._closed
                )
                if can_continue:
                    job.context = refreshed_context
                    job._operation_task = None
                    job._operation_result = None
                    job._state = CompactJobState.QUEUED
                    self._current = None
                    self._queue.appendleft(job)
                    self._active_by_key[(job.device_id, job.session_id)] = job
                    should_drain = True
                else:
                    job._state = CompactJobState.APPLIED
                    self._finish(job, CompactJobState.APPLIED, result_status=status, reason=job._reason)
                    should_drain = True
            if should_drain:
                self._start_next()
            return

        state_by_status = {
            "idle": CompactJobState.IDLE,
            "blocked": CompactJobState.BLOCKED,
            "unknown": CompactJobState.UNKNOWN,
            "conflict": CompactJobState.CONFLICT,
        }
        state = state_by_status[status]
        reason = context_error or (str(reason_value) if reason_value else None)
        self._finish_and_drain(job, state, result_status=status, reason=reason)

    def _on_collection_failure(self, job: CompactJob, error: BaseException) -> None:
        code = getattr(error, "code", None)
        if self._diagnostics is not None:
            self._diagnostics.record_exception(error, "collection")
        with self._lock:
            if job._state in _FINAL_STATES:
                if self._current is job:
                    self._current = None
                should_drain = True
                operation_result = job._operation_result
            else:
                should_drain = False
                operation_result = job._operation_result
        if should_drain:
            self._start_next()
            return
        if code == "reviewer_owns_collection":
            state, reason = CompactJobState.BLOCKED, "reviewer_owns_collection"
        elif operation_result is not None and operation_result.executed is True:
            state, reason = CompactJobState.UNKNOWN, "collection_operation_failed_after_start"
        elif operation_result is not None and operation_result.state is OperationState.CANCELED:
            state, reason = CompactJobState.CANCELED, "canceled_before_start"
        else:
            state, reason = CompactJobState.FAILED, "collection_operation_failed"
        self._finish_and_drain(job, state, reason=reason)

    def _finish_and_drain(
        self,
        job: CompactJob,
        state: CompactJobState,
        *,
        result_status: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        with self._lock:
            if job._state not in _FINAL_STATES:
                self._finish(job, state, result_status=result_status, reason=reason)
            elif self._current is job:
                self._current = None
        self._start_next()

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None or job._state in _FINAL_STATES:
                return False
            job._cancel_requested = True
            if self._current is not job:
                self._queue = deque(item for item in self._queue if item is not job)
                self._finish(job, CompactJobState.CANCELED, reason="canceled_before_start")
                should_drain = True
                task = None
            else:
                task = job._operation_task
                should_drain = False
        if task is not None:
            task.cancel()
        elif should_drain:
            self._start_next()
        return True

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            queued = list(self._queue)
            self._queue.clear()
            for job in queued:
                job._cancel_requested = True
                self._finish(job, CompactJobState.CANCELED, reason="runtime_stopped")
            current = self._current
            task = current._operation_task if current is not None else None
            if current is not None:
                current._cancel_requested = True
        if task is not None:
            task.cancel()


__all__ = ["CompactJob", "CompactJobError", "CompactJobManager", "CompactJobState"]
