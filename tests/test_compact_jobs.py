from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Callable, Optional
from unittest.mock import Mock, patch

from inkcards_anki_bridge.anki.compact_jobs import (
    CompactJobError,
    CompactJobManager,
    CompactJobState,
)
from inkcards_anki_bridge.anki.operations import (
    CollectionOpAdapter,
    CollectionStartRejected,
    OperationContext,
    OperationState,
)


class FakeOpChanges:
    pass


@dataclass
class FakeCompactResult:
    status: str
    generation_before: int
    generation_after: int
    refresh_context: bool
    more_pending: bool
    changes: FakeOpChanges
    reason: Optional[str] = None


@dataclass
class PendingCollection:
    operation: Callable[..., Any]
    before_start: Optional[Callable[[], bool]]
    on_complete: Optional[Callable[[Any], None]]
    on_failure: Optional[Callable[[BaseException], None]]
    started: bool = False


class FakeCollectionHost:
    """仅供宿主测试的 CollectionOp 接口，不导入或打开 Anki 数据。"""

    def __init__(self) -> None:
        self.pending: list[PendingCollection] = []

    def submit_collection(
        self,
        operation: Callable[..., Any],
        *,
        before_start=None,
        on_complete=None,
        on_failure=None,
    ) -> None:
        self.pending.append(PendingCollection(operation, before_start, on_complete, on_failure))

    def start_next(self) -> PendingCollection:
        item = self.pending[0]
        if item.started:
            return item
        item.started = True
        if item.before_start is not None:
            try:
                allowed = item.before_start()
            except BaseException as exc:
                if item.on_failure is not None:
                    item.on_failure(exc)
                return item
            if allowed is not True:
                if item.on_failure is not None:
                    item.on_failure(CollectionStartRejected("reviewer_owns_collection"))
                return item
        return item

    def finish_next(self, collection: Any, *, after_operation=None) -> Any:
        item = self.start_next()
        if not item.started or not self.pending or self.pending[0] is not item:
            return None
        execution = item.operation(collection)
        if after_operation is not None:
            after_operation()
        outcome = execution.outcome
        if outcome.error is not None:
            if item.on_failure is not None:
                item.on_failure(outcome.error)
        elif item.on_complete is not None:
            # 成功时，Anki 包装层会传回原生结果；OpChanges 的真实类型与形状由
            # test_anki_operations 覆盖。
            item.on_complete(execution.callback_result)
        self.pending.pop(0)
        return outcome


class ContextSource:
    def __init__(self) -> None:
        self.current = OperationContext("profile-a", 7, "epoch-a")

    def __call__(self) -> OperationContext:
        return self.current


class CompactJobTests(unittest.TestCase):
    def test_optional_job_generation_preserves_unknown_and_known_values(self):
        from inkcards_anki_bridge.device.routes import DeviceRouter
        self.assertEqual(DeviceRouter._compact_job_view({
            "state": "idle", "generation_before": None, "generation_after": None
        }), {"state": "idle", "generation_before": None, "generation_after": None})
        self.assertEqual(DeviceRouter._compact_job_view({
            "state": "applied", "generation_before": 0, "generation_after": 1
        }), {"state": "applied", "generation_before": 0, "generation_after": 1})

    def setUp(self) -> None:
        self.source = ContextSource()
        self.host = FakeCollectionHost()
        self.reviewer_owned = False
        self.reviewer_checks = 0
        self.applied_calls: list[tuple[str, str, OperationContext]] = []
        self.generation_updates: list[int] = []
        self.results: list[Any] = []

        def reviewer_available() -> bool:
            self.reviewer_checks += 1
            return not self.reviewer_owned

        def apply_one(_collection: Any, device: str, session: str, context: OperationContext):
            self.applied_calls.append((device, session, context))
            if isinstance(self.results[0], BaseException):
                raise self.results.pop(0)
            return self.results.pop(0)

        def update_generation(generation: int) -> None:
            self.generation_updates.append(generation)
            self.source.current = OperationContext(
                self.source.current.profile_identity,
                generation,
                self.source.current.runtime_epoch,
            )

        self.manager = CompactJobManager(
            operation_adapter=CollectionOpAdapter(self.host, self.source),
            context_provider=self.source,
            apply_one=apply_one,
            update_collection_generation=update_generation,
            reviewer_available=reviewer_available,
            max_active_jobs=2,
            max_retained_jobs=8,
        )

    @staticmethod
    def result(
        status: str,
        generation_before: int = 7,
        generation_after: int = 7,
        *,
        refresh_context: bool = False,
        more_pending: bool = False,
        reason: Optional[str] = None,
    ) -> FakeCompactResult:
        return FakeCompactResult(
            status,
            generation_before,
            generation_after,
            refresh_context,
            more_pending,
            FakeOpChanges(),
            reason,
        )

    def test_context_provider_failure_on_request_is_classified(self):
        with patch.object(self.manager, "_context_provider", side_effect=RuntimeError("profile closed")):
            with self.assertRaises(CompactJobError) as raised:
                self.manager.request("device-a", "session-a")
        self.assertEqual(raised.exception.code, "context_unavailable")
        self.assertEqual(self.host.pending, [])

    def test_context_failure_after_committed_answer_preserves_applied_and_stops_chain(self):
        self.results = [self.result("applied", 7, 8, refresh_context=True, more_pending=True)]
        job = self.manager.request("device-a", "session-a")
        self.host.finish_next(object(), after_operation=lambda: setattr(
            self.manager, "_context_provider", Mock(side_effect=RuntimeError("profile closed"))
        ))
        self.assertEqual(job.state, CompactJobState.APPLIED)
        self.assertEqual(job.to_dict()["reason"], "runtime_context_changed_after_collection")
        self.assertEqual(len(self.applied_calls), 1)
        self.assertEqual(self.generation_updates, [])
        self.assertEqual(self.host.pending, [])

    def test_applies_one_event_per_collectionop_and_refreshes_context(self) -> None:
        self.results = [
            self.result("applied", 7, 8, refresh_context=True, more_pending=True),
            self.result("applied", 8, 9, refresh_context=True),
        ]
        job = self.manager.request("device-a", "session-a")
        self.assertEqual(len(self.host.pending), 1)

        first = self.host.finish_next(object())
        self.assertEqual(first.state, OperationState.COMPLETED)
        self.assertEqual(self.generation_updates, [8])
        self.assertEqual(len(self.host.pending), 1)
        self.assertEqual(job.state, CompactJobState.STARTED)
        self.assertEqual(self.applied_calls[0][2].generation, 7)
        self.assertEqual(job.operation_task.expected.generation, 8)

        second = self.host.finish_next(object())
        self.assertEqual(second.state, OperationState.COMPLETED)
        self.assertEqual(self.applied_calls[1][2].generation, 8)
        self.assertEqual(self.generation_updates, [8, 9])
        self.assertEqual(job.state, CompactJobState.APPLIED)
        self.assertEqual(job.to_dict()["events_applied"], 2)

    def test_terminal_native_statuses_are_kept_distinct(self) -> None:
        cases = {
            "idle": CompactJobState.IDLE,
            "blocked": CompactJobState.BLOCKED,
            "unknown": CompactJobState.UNKNOWN,
            "conflict": CompactJobState.CONFLICT,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.source.current = OperationContext("profile-a", 7, "epoch-a")
                self.host.pending.clear()
                self.applied_calls.clear()
                self.results = [self.result(status, reason="native_reason")]
                job = self.manager.request("device-a", f"session-{status}")
                self.host.finish_next(object())
                self.assertEqual(job.state, expected)
                self.assertEqual(job.to_dict()["reason"], "native_reason")
                self.assertEqual(self.generation_updates, [])

    def test_reviewer_is_checked_at_start_and_again_before_service_write(self) -> None:
        self.results = [self.result("applied", 7, 8, refresh_context=True)]
        job = self.manager.request("device-a", "session-a")
        pending = self.host.start_next()
        self.assertEqual(self.reviewer_checks, 1)

        self.reviewer_owned = True
        outcome = self.host.finish_next(object())
        self.assertEqual(outcome.state, OperationState.FAILED)
        self.assertEqual(job.state, CompactJobState.BLOCKED)
        self.assertEqual(job.to_dict()["reason"], "reviewer_owns_collection")
        self.assertEqual(self.reviewer_checks, 2)
        self.assertEqual(self.applied_calls, [])
        self.assertIsNotNone(pending)

    def test_live_requests_coalesce_capacity_is_bounded_and_queued_cancel_stops(self) -> None:
        self.results = [self.result("idle")]
        first = self.manager.request("device-a", "session-a")
        self.assertIs(self.manager.request("device-a", "session-a"), first)
        queued = self.manager.request("device-a", "session-b")
        with self.assertRaises(CompactJobError) as raised:
            self.manager.request("device-b", "session-c")
        self.assertEqual(raised.exception.code, "job_capacity")

        self.assertTrue(queued.cancel())
        self.assertEqual(queued.state, CompactJobState.CANCELED)
        self.host.finish_next(object())
        self.assertEqual(first.state, CompactJobState.IDLE)
        self.assertEqual(self.host.pending, [])
        self.assertEqual(self.applied_calls, [("device-a", "session-a", self.source.current)])

    def test_cancel_after_native_apply_finishes_current_event_without_scheduling_next(self) -> None:
        self.results = [
            self.result("applied", 7, 8, refresh_context=True, more_pending=True),
            self.result("applied", 8, 9, refresh_context=True),
        ]
        job = self.manager.request("device-a", "session-a")

        outcome = self.host.finish_next(object(), after_operation=job.cancel)

        self.assertEqual(outcome.state, OperationState.COMPLETED)
        self.assertTrue(job.cancel_requested)
        self.assertEqual(job.state, CompactJobState.APPLIED)
        self.assertEqual(job.to_dict()["events_applied"], 1)
        self.assertEqual(self.generation_updates, [8])
        self.assertEqual(len(self.applied_calls), 1)
        self.assertEqual(self.host.pending, [])

    def test_context_switch_after_commit_never_updates_new_runtime(self) -> None:
        result = self.result("applied", 7, 8, refresh_context=True)
        self.results = [result]
        job = self.manager.request("device-a", "session-a")

        outcome = self.host.finish_next(
            object(),
            after_operation=lambda: setattr(
                self.source,
                "current",
                OperationContext("profile-b", 2, "epoch-b"),
            ),
        )
        self.assertEqual(outcome.state, OperationState.COMPLETED)
        self.assertEqual(job.state, CompactJobState.APPLIED)
        self.assertEqual(job.to_dict()["reason"], "runtime_context_changed_after_collection")
        self.assertEqual(self.generation_updates, [])
        self.assertEqual(self.source.current, OperationContext("profile-b", 2, "epoch-b"))
        self.assertIs(outcome.value, result)

    def test_exception_after_operation_start_is_unknown_and_does_not_retry(self) -> None:
        self.results = [RuntimeError("failure after the service began")]
        job = self.manager.request("device-a", "session-a")
        outcome = self.host.finish_next(object())

        self.assertEqual(outcome.state, OperationState.FAILED)
        self.assertTrue(outcome.executed)
        self.assertEqual(job.state, CompactJobState.UNKNOWN)
        self.assertEqual(job.to_dict()["reason"], "collection_operation_failed_after_start")
        self.assertEqual(len(self.applied_calls), 1)
        self.assertEqual(self.host.pending, [])

    def test_stale_context_before_execution_drops_without_service_call(self) -> None:
        self.results = [self.result("applied", 7, 8, refresh_context=True)]
        job = self.manager.request("device-a", "session-a")
        self.source.current = OperationContext("profile-a", 8, "epoch-b")

        outcome = self.host.finish_next(object())
        self.assertEqual(outcome.state, OperationState.STALE_RESULT_DROPPED)
        self.assertFalse(outcome.executed)
        self.assertEqual(job.state, CompactJobState.STALE_RESULT_DROPPED)
        self.assertEqual(self.applied_calls, [])
        self.assertEqual(self.generation_updates, [])


if __name__ == "__main__":
    unittest.main()
