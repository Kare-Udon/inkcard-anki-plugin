"""普通 Python 可测试的 Anki QueryOp/CollectionOp 宿主边界。

HTTP worker 只调用 ``submit`` 并立即返回 task；真正读取 collection 的 closure 由
Anki/Qt 宿主稍后调度。task 在开始和完成处都重验 profile identity、generation 和
runtime epoch。超时/取消不会把已经开始的 closure 伪装成“未执行”。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol


class OperationKind(str, Enum):
    QUERY = "query"
    COLLECTION = "collection"


class OperationState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    STALE_RESULT_DROPPED = "stale_result_dropped"


@dataclass(frozen=True)
class OperationContext:
    """一次作业绑定的宿主身份三元组。"""

    profile_identity: str
    generation: Any
    runtime_epoch: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_identity", str(self.profile_identity))

    @property
    def profile_id(self) -> str:
        return self.profile_identity

    @property
    def collection_generation(self) -> Any:
        return self.generation

    @property
    def epoch(self) -> Any:
        return self.runtime_epoch

    @classmethod
    def from_value(cls, value: Any) -> "OperationContext":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            identity = value.get(
                "profile_identity",
                value.get("profile_principal", value.get("profile_id", value.get("identity"))),
            )
            generation = value.get("generation", value.get("collection_generation"))
            epoch = value.get("runtime_epoch", value.get("epoch"))
            if identity is None:
                raise ValueError("operation context lacks profile identity")
            return cls(str(identity), generation, epoch)
        if isinstance(value, tuple) and len(value) == 3:
            return cls(str(value[0]), value[1], value[2])
        identity = getattr(
            value,
            "profile_identity",
            getattr(value, "profile_principal", getattr(value, "profile_id", None)),
        )
        if identity is not None:
            generation = getattr(value, "generation", getattr(value, "collection_generation", None))
            epoch = getattr(value, "runtime_epoch", getattr(value, "epoch", None))
            return cls(str(identity), generation, epoch)
        raise TypeError("context provider must return OperationContext or a compatible mapping")


class OperationContextProvider(Protocol):
    def __call__(self) -> OperationContext:
        ...


class OperationHost(Protocol):
    """最小 fake/宿主协议；方法只负责排队，不要求调用方等待。"""

    def submit_query(self, operation: Callable[[], Any]) -> Any:
        ...

    def submit_collection(
        self,
        operation: Callable[[], Any],
        *,
        before_start: Optional[Callable[[], bool]] = None,
        on_complete: Optional[Callable[[Any], None]] = None,
        on_failure: Optional[Callable[[BaseException], None]] = None,
    ) -> Any:
        ...


def _read_context(provider: Any) -> Any:
    if callable(provider):
        return provider()
    reader = getattr(provider, "current_context", None)
    if callable(reader):
        return reader()
    raise TypeError("context provider must be callable or expose current_context()")


@dataclass(frozen=True)
class OperationResult:
    state: OperationState
    value: Any = None
    error: Optional[BaseException] = None
    executed: Optional[bool] = None
    timed_out: bool = False
    discarded: bool = False

    @property
    def not_executed(self) -> bool:
        """只有显式取消/开始前 stale 才返回 True；超时中的未知状态不会猜测。"""

        return self.executed is False


_NO_COLLECTION_RESULT = object()


class CollectionStartRejected(RuntimeError):
    """CollectionOp 尚未启动时，宿主安全门拒绝本次操作。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = reason


@dataclass(frozen=True)
class _CollectionOpExecution:
    """供 Anki CollectionOp 宿主适配的内部结果。"""

    outcome: OperationResult
    callback_result: Any = _NO_COLLECTION_RESULT


class OperationTask:
    """一个已绑定上下文的、不可阻塞等待的宿主作业句柄。"""

    def __init__(
        self,
        kind: OperationKind,
        expected: OperationContext,
        operation: Callable[..., Any],
        on_complete: Optional[Callable[[OperationResult], None]] = None,
    ) -> None:
        self.kind = kind
        self.expected = expected
        self.operation = operation
        self.state = OperationState.CREATED
        self.started = False
        self.cancel_requested = False
        self._timed_out = False
        self._result = OperationResult(OperationState.CREATED)
        self._collection_callback_result: Any = _NO_COLLECTION_RESULT
        self._on_complete = on_complete
        self._lock = threading.RLock()

    @property
    def result(self) -> OperationResult:
        with self._lock:
            return self._result

    @property
    def timed_out(self) -> bool:
        with self._lock:
            return self._timed_out

    @property
    def operation_started(self) -> bool:
        return self.started

    @property
    def collection_callback_result(self) -> Any:
        """取回 closure 的真实返回对象，即使后续 epoch 检查丢弃了任务结果。"""

        with self._lock:
            return self._collection_callback_result

    def _notify(self, result: OperationResult) -> None:
        callback = self._on_complete
        if callback is not None:
            try:
                callback(result)
            except Exception:
                # 宿主回调不能改变已经落定的操作状态。
                pass

    def _fail_before_start(self, error: BaseException) -> None:
        self._finish_before_start(OperationState.FAILED, error=error)

    def _finish_before_start(
        self,
        state: OperationState,
        *,
        error: Optional[BaseException] = None,
        discarded: bool = False,
    ) -> OperationResult:
        with self._lock:
            if self.state is not OperationState.QUEUED:
                return self._result
            self.state = state
            self._result = OperationResult(
                state=state,
                error=error,
                executed=False,
                timed_out=self._timed_out,
                discarded=discarded,
            )
            result = self._result
        # 通知管理器前释放 task lock，避免与管理器取消操作的锁顺序冲突。
        self._notify(result)
        return result

    def _finish(
        self,
        state: OperationState,
        *,
        value: Any = None,
        error: Optional[BaseException] = None,
        executed: Optional[bool] = None,
        discarded: bool = False,
    ) -> OperationResult:
        with self._lock:
            self.state = state
            self._result = OperationResult(
                state=state,
                value=value,
                error=error,
                executed=executed,
                timed_out=self._timed_out,
                discarded=discarded,
            )
            result = self._result
        self._notify(result)
        return result

    def cancel(self) -> bool:
        """只取消尚未开始的 closure；已开始时只记录请求并返回 False。"""

        with self._lock:
            if self.state != OperationState.QUEUED:
                if self.started:
                    self.cancel_requested = True
                return False
            self.cancel_requested = True
            # 状态转换必须和检查在同一临界区完成，否则宿主 closure 可能在
            # cancel() 释放锁后抢先进入 STARTED。
            self.state = OperationState.CANCELED
            result = OperationResult(
                OperationState.CANCELED,
                executed=False,
                timed_out=self._timed_out,
            )
            self._result = result
        self._notify(result)
        return True

    def timeout(self) -> bool:
        """记录等待超时，不撤销已排队/已开始的 closure。"""

        with self._lock:
            if self.state in {
                OperationState.COMPLETED,
                OperationState.FAILED,
                OperationState.CANCELED,
                OperationState.STALE_RESULT_DROPPED,
            }:
                return False
            self._timed_out = True
            self._result = OperationResult(
                state=self.state,
                value=self._result.value,
                error=self._result.error,
                executed=self._result.executed,
                timed_out=True,
                discarded=self._result.discarded,
            )
            return True

    mark_timeout = timeout

    def run(
        self,
        context_provider: OperationContextProvider,
        *operation_args: Any,
    ) -> OperationResult:
        """由 QueryOp/CollectionOp 宿主 closure 调用；公共方法便于 fake 测试。"""

        with self._lock:
            if self.state == OperationState.CANCELED:
                return self._result
            if self.state != OperationState.QUEUED:
                return self._result
        try:
            current = OperationContext.from_value(_read_context(context_provider))
        except Exception as exc:
            return self._finish_before_start(OperationState.STALE_RESULT_DROPPED, error=exc, discarded=True)
        if current != self.expected:
            return self._finish_before_start(OperationState.STALE_RESULT_DROPPED, discarded=True)

        with self._lock:
            # context 读取期间可能已取消或收到宿主失败，任何终态都不能重启。
            if self.state is not OperationState.QUEUED:
                return self._result
            self.state = OperationState.STARTED
            self.started = True

        try:
            # 官方 aqt.operations.QueryOp 会把 mw.col 作为唯一参数传给
            # op(collection)。普通 fake host 可以不传参数，保留旧测试/宿主
            # 的零参数 closure 形状。
            value = self.operation(*operation_args)
        except BaseException as exc:
            try:
                after = OperationContext.from_value(_read_context(context_provider))
            except Exception:
                after = None
            if after != self.expected:
                return self._finish(OperationState.STALE_RESULT_DROPPED, error=exc, executed=True, discarded=True)
            return self._finish(OperationState.FAILED, error=exc, executed=True)

        if self.kind is OperationKind.COLLECTION:
            # Anki 的 CollectionOp 必须在返回后拿到这份真实 OpChanges，
            # 即使 profile/epoch 在 closure 完成后变化，任务结果仍须丢弃。
            with self._lock:
                self._collection_callback_result = value

        try:
            after = OperationContext.from_value(_read_context(context_provider))
        except Exception as exc:
            return self._finish(OperationState.STALE_RESULT_DROPPED, error=exc, executed=True, discarded=True)
        if after != self.expected:
            return self._finish(OperationState.STALE_RESULT_DROPPED, executed=True, discarded=True)
        return self._finish(OperationState.COMPLETED, value=value, executed=True)


class _BaseOpAdapter:
    def __init__(
        self,
        host: Optional[OperationHost] = None,
        context_provider: Optional[OperationContextProvider] = None,
        *,
        submitter: Optional[Callable[[Callable[..., Any]], Any]] = None,
    ) -> None:
        if context_provider is None:
            raise TypeError("context_provider is required")
        self._host = host
        self._submitter = submitter
        self._context_provider = context_provider

    _kind = OperationKind.QUERY
    _submit_name = "submit_query"

    def submit(
        self,
        expected: OperationContext,
        operation: Callable[..., Any],
        on_complete: Optional[Callable[[OperationResult], None]] = None,
        *,
        on_collection_complete: Optional[Callable[[Any], None]] = None,
        on_collection_failure: Optional[Callable[[BaseException], None]] = None,
        before_collection_start: Optional[Callable[[], bool]] = None,
    ) -> OperationTask:
        if self._kind is OperationKind.QUERY and (
            on_collection_complete is not None
            or on_collection_failure is not None
            or before_collection_start is not None
        ):
            raise TypeError("collection completion callbacks require CollectionOpAdapter")
        if before_collection_start is not None and on_collection_failure is None:
            raise TypeError("before_collection_start requires on_collection_failure")
        expected_context = OperationContext.from_value(expected)
        task = OperationTask(self._kind, expected_context, operation, on_complete)
        task.state = OperationState.QUEUED
        task._result = OperationResult(OperationState.QUEUED)

        def collection_failure(error: BaseException) -> None:
            if task.state is OperationState.QUEUED:
                task._finish(OperationState.FAILED, error=error, executed=False)
            if on_collection_failure is not None:
                try:
                    on_collection_failure(error)
                except Exception:
                    pass

        def query_failure(error: BaseException) -> None:
            # 宿主可能在 Qt 排队后、真正运行 closure 前失败。
            # 已结束或已开始的任务由其自身结果负责，迟到通知不能覆盖它。
            task._fail_before_start(error)

        submitter = self._submitter
        if submitter is None and self._host is not None:
            submitter = getattr(self._host, self._submit_name, None)
            if not callable(submitter):
                alternate_name = "query_op" if self._kind is OperationKind.QUERY else "collection_op"
                submitter = getattr(self._host, alternate_name, None)
        if not callable(submitter):
            error = RuntimeError("host does not provide the required operation submitter")
            if self._kind is OperationKind.COLLECTION:
                collection_failure(error)
            else:
                task._finish(OperationState.FAILED, error=error, executed=False)
            return task
        try:
            # 真实 Anki 宿主应把 closure 放入 QueryOp/CollectionOp；这里不等待返回值。
            # QueryOp 的 closure 会收到官方提供的 collection；fake host 可以
            # 省略该参数。
            def run_operation(*args: Any) -> Any:
                outcome = task.run(self._context_provider, *args)
                if self._kind is OperationKind.COLLECTION:
                    return _CollectionOpExecution(
                        outcome=outcome,
                        callback_result=task.collection_callback_result,
                    )
                return outcome

            if self._kind is OperationKind.COLLECTION and (
                on_collection_complete is not None
                or on_collection_failure is not None
                or before_collection_start is not None
            ):
                submitter(
                    run_operation,
                    before_start=before_collection_start,
                    on_complete=on_collection_complete,
                    on_failure=collection_failure,
                )
            elif self._kind is OperationKind.QUERY and isinstance(self._host, AnkiQueryOpHost) and self._submitter is None:
                submitter(run_operation, on_failure=query_failure)
            else:
                submitter(run_operation)
        except BaseException as exc:
            if self._kind is OperationKind.COLLECTION:
                collection_failure(exc)
            else:
                query_failure(exc)
        return task


class QueryOpAdapter(_BaseOpAdapter):
    """只读 QueryOp façade；submit 本身不阻塞 HTTP 或 Qt 主线程。"""

    _kind = OperationKind.QUERY
    _submit_name = "submit_query"


class CollectionOpAdapter(_BaseOpAdapter):
    """CollectionOp façade；task 完成与 Anki 完成回调可分别观察。"""

    _kind = OperationKind.COLLECTION
    _submit_name = "submit_collection"


class AnkiQueryOpHost:
    """把标准库 operation façade 接到 Anki 26.09.3 的官方 QueryOp。

    这里故意不导入 ``aqt``。插件入口在 Anki 进程内延迟传入
    ``aqt.operations.QueryOp`` 工厂和 ``mw`` parent。官方构造形状是
    ``QueryOp(parent=..., op=lambda col: ..., success=...).run_in_background()``；
    因此 collection 只存在于 QueryOp 的 operation closure 调用栈内。
    """

    def __init__(
        self,
        query_op_factory: Callable[..., Any],
        parent: Any,
        *,
        schedule_on_main: Optional[Callable[[Callable[[], None]], Any]] = None,
    ) -> None:
        if not callable(query_op_factory):
            raise TypeError("query_op_factory must be callable")
        self._query_op_factory = query_op_factory
        self._parent = parent
        self._schedule_on_main = schedule_on_main

    def submit_query(
        self,
        operation: Callable[..., Any],
        *,
        on_failure: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        def report_failure(error: BaseException) -> None:
            if on_failure is None:
                raise error
            on_failure(error)

        def start() -> None:
            try:
                query_op = self._query_op_factory(
                    parent=self._parent,
                    op=operation,
                    success=lambda _value: None,
                )
                failure = getattr(query_op, "failure", None)
                if callable(failure):
                    failure(report_failure)
                runner = getattr(query_op, "run_in_background", None)
                if not callable(runner):
                    raise RuntimeError("Anki QueryOp does not provide run_in_background()")
                runner()
            except BaseException as exc:
                report_failure(exc)

        # HTTP/runtime 线程只把无 collection 的 start closure 投递回 Qt；
        # QueryOp 自己仍在其官方后台 worker 中接收 collection。
        if self._schedule_on_main is not None:
            self._schedule_on_main(start)
        else:
            start()


@dataclass(frozen=True)
class CompactOperationInitiator:
    """宿主 hook 识别自己/旧 profile 的 compact 操作，避免重复推进 generation。"""
    profile_identity: str
    runtime_epoch: Any


class AnkiCollectionOpHost:
    """接入 Anki CollectionOp，并保持其 callback 的 OpChanges 返回契约。

    已安装 Anki 的 CollectionOp 只接受 OpChanges 或有 OpChanges ``changes``
    属性的结果。取消/过时且尚未执行的任务返回一个真实的空 OpChanges；
    closure 已执行时则原样返回它的结果，即使任务的后置 epoch 检查将业务结果丢弃。
    """

    def __init__(
        self,
        collection_op_factory: Callable[..., Any],
        parent: Any,
        *,
        op_changes_type: type[Any],
        empty_changes_factory: Callable[[], Any],
        schedule_on_main: Optional[Callable[[Callable[[], None]], Any]] = None,
        schedule_after_collection_op: Optional[Callable[[Callable[[], None]], Any]] = None,
        initiator: Any = None,
    ) -> None:
        if not callable(collection_op_factory):
            raise TypeError("collection_op_factory must be callable")
        if not isinstance(op_changes_type, type):
            raise TypeError("op_changes_type must be a type")
        if not callable(empty_changes_factory):
            raise TypeError("empty_changes_factory must be callable")
        if schedule_after_collection_op is not None and not callable(schedule_after_collection_op):
            raise TypeError("schedule_after_collection_op must be callable")
        self._collection_op_factory = collection_op_factory
        self._parent = parent
        self._op_changes_type = op_changes_type
        self._empty_changes_factory = empty_changes_factory
        self._schedule_on_main = schedule_on_main
        self._schedule_after_collection_op = schedule_after_collection_op
        self._initiator = initiator

    def _validate_changes_result(self, result: Any) -> Any:
        if isinstance(result, self._op_changes_type):
            return result
        try:
            changes = getattr(result, "changes")
        except (AttributeError, TypeError) as exc:
            raise TypeError(
                "Anki CollectionOp callback must return OpChanges or an object with OpChanges changes"
            ) from exc
        if not isinstance(changes, self._op_changes_type):
            raise TypeError("Anki CollectionOp result.changes must be an OpChanges instance")
        return result

    def _changes_for_execution(self, execution: _CollectionOpExecution) -> Any:
        if execution.callback_result is not _NO_COLLECTION_RESULT:
            return self._validate_changes_result(execution.callback_result)
        if execution.outcome.executed is False:
            return self._validate_changes_result(self._empty_changes_factory())
        if execution.outcome.error is not None:
            raise execution.outcome.error
        raise RuntimeError("CollectionOp task completed without returning OpChanges")

    def submit_collection(
        self,
        operation: Callable[..., Any],
        *,
        before_start: Optional[Callable[[], bool]] = None,
        on_complete: Optional[Callable[[Any], None]] = None,
        on_failure: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        if not callable(operation):
            raise TypeError("operation must be callable")
        if before_start is not None and not callable(before_start):
            raise TypeError("before_start must be callable")
        if before_start is not None and on_failure is None:
            raise TypeError("before_start requires on_failure")
        if on_complete is not None and self._schedule_after_collection_op is None:
            raise RuntimeError("CollectionOp completion requires a deferred completion scheduler")

        def report_failure(error: BaseException) -> None:
            if on_failure is None:
                raise error
            try:
                on_failure(error)
            except Exception:
                # CollectionOp 状态已由 Anki 落定；插件通知不能反向破坏回调。
                pass

        def start() -> None:
            try:
                if before_start is not None:
                    allowed = before_start()
                    if allowed is not True:
                        raise CollectionStartRejected("reviewer_owns_collection")

                def collection_operation(collection: Any) -> Any:
                    result = operation(collection)
                    if isinstance(result, _CollectionOpExecution):
                        return self._changes_for_execution(result)
                    return self._validate_changes_result(result)

                collection_op = self._collection_op_factory(
                    parent=self._parent,
                    op=collection_operation,
                )
                success = getattr(collection_op, "success", None)
                if on_complete is not None:
                    if not callable(success):
                        raise RuntimeError("Anki CollectionOp does not provide success()")

                    def completed(result: Any) -> None:
                        scheduler = self._schedule_after_collection_op
                        if scheduler is not None:
                            scheduler(lambda: on_complete(result))

                    success(completed)
                failure = getattr(collection_op, "failure", None)
                if callable(failure):
                    failure(report_failure)
                runner = getattr(collection_op, "run_in_background", None)
                if not callable(runner):
                    raise RuntimeError("Anki CollectionOp does not provide run_in_background()")
                if self._initiator is None:
                    runner()
                else:
                    runner(initiator=self._initiator)
            except BaseException as exc:
                report_failure(exc)

        # CollectionOp 的启动和 Qt/UI 回调都由主线程创建；collection callback
        # 本身仍由 Anki 的后台 operation worker 执行。
        if self._schedule_on_main is not None:
            self._schedule_on_main(start)
        else:
            start()


# 兼容更短的宿主命名。
QueryOp = QueryOpAdapter
CollectionOp = CollectionOpAdapter


__all__ = [
    "CollectionOp",
    "CollectionStartRejected",
    "CollectionOpAdapter",
    "AnkiCollectionOpHost",
    "AnkiQueryOpHost",
    "OperationContext",
    "OperationContextProvider",
    "OperationHost",
    "OperationKind",
    "OperationResult",
    "OperationState",
    "OperationTask",
    "QueryOp",
    "QueryOpAdapter",
]
