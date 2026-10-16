from __future__ import annotations

import threading
import unittest

from inkcards_anki_bridge.anki.operations import (
    AnkiCollectionOpHost,
    AnkiQueryOpHost,
    CollectionOpAdapter,
    CollectionStartRejected,
    OperationContext,
    OperationState,
    QueryOpAdapter,
)


class FakeHost:
    def __init__(self) -> None:
        self.query_pending = []
        self.collection_pending = []

    def submit_query(self, operation):
        self.query_pending.append(operation)

    def submit_collection(self, operation):
        self.collection_pending.append(operation)


class ContextSource:
    def __init__(self) -> None:
        self.current = OperationContext("profile-a", "generation-1", "epoch-1")

    def __call__(self):
        return self.current


class FakeOpChanges:
    pass


class FakeResultWithChanges:
    def __init__(self, changes):
        self.changes = changes


class FakeCollectionOp:
    def __init__(self, parent, op):
        self.parent = parent
        self.op = op
        self.success_callback = None
        self.failure_callback = None
        self.run_count = 0

    def success(self, callback):
        self.success_callback = callback
        return self

    def failure(self, callback):
        self.failure_callback = callback
        return self

    def run_in_background(self):
        self.run_count += 1


class FakeCollectionOpFactory:
    def __init__(self):
        self.created = []

    def __call__(self, *, parent, op):
        result = FakeCollectionOp(parent, op)
        self.created.append(result)
        return result


class OperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.source = ContextSource()
        self.expected = self.source.current

    def test_queryop_delayed_start_failure_finishes_task_without_escaping_qt(self):
        scheduled = []
        outcomes = []
        failure = RuntimeError("query factory unavailable")

        def factory(**_kwargs):
            raise failure

        host = AnkiQueryOpHost(factory, object(), schedule_on_main=scheduled.append)
        task = QueryOpAdapter(host, self.source).submit(
            self.expected, lambda: self.fail("不应读取集合"), on_complete=outcomes.append
        )
        scheduled.pop()()
        self.assertEqual(task.state, OperationState.FAILED)
        self.assertIs(task.result.error, failure)
        self.assertTrue(task.result.not_executed)
        self.assertEqual(outcomes, [task.result])

    def test_queryop_host_failure_is_terminal_and_late_callback_cannot_replace_success(self):
        scheduled = []
        created = []

        def factory(**kwargs):
            query = FakeCollectionOp(kwargs["parent"], kwargs["op"])
            created.append(query)
            return query

        host = AnkiQueryOpHost(factory, object(), schedule_on_main=scheduled.append)
        adapter = QueryOpAdapter(host, self.source)
        task = adapter.submit(self.expected, lambda: "ready")
        scheduled.pop()()
        created[-1].failure_callback(RuntimeError("host failed before operation"))
        self.assertEqual(task.state, OperationState.FAILED)
        self.assertTrue(task.result.not_executed)
        created[-1].op()
        self.assertEqual(task.state, OperationState.FAILED)

        task = adapter.submit(self.expected, lambda: "ready")
        scheduled.pop()()
        created[-1].op()
        created[-1].failure_callback(RuntimeError("late host failure"))
        self.assertEqual(task.state, OperationState.COMPLETED)
        self.assertEqual(task.result.value, "ready")

    def test_failure_or_cancel_during_context_read_cannot_restart_or_renotify(self):
        for terminal in ("failure", "cancel"):
            for context_result in ("same", "changed", "error"):
                with self.subTest(terminal=terminal, context_result=context_result):
                    entered, release = threading.Event(), threading.Event()
                    executed, outcomes = [], []

                    def provider():
                        entered.set()
                        release.wait(2)
                        if context_result == "error":
                            raise RuntimeError("context unavailable")
                        if context_result == "changed":
                            return OperationContext("other", "generation-1", "epoch-1")
                        return self.expected

                    host = FakeHost()
                    task = QueryOpAdapter(host, provider).submit(
                        self.expected, lambda: executed.append(True), on_complete=outcomes.append
                    )
                    worker = threading.Thread(target=host.query_pending.pop())
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        if terminal == "failure":
                            task._fail_before_start(RuntimeError("host failed"))
                        else:
                            self.assertTrue(task.cancel())
                        expected = task.result
                    finally:
                        release.set()
                        worker.join(1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(executed, [])
                    self.assertIs(task.result, expected)
                    self.assertEqual(outcomes, [expected])

    def test_cancel_before_start_never_executes_query(self) -> None:
        called = []
        adapter = QueryOpAdapter(self.host, self.source)
        task = adapter.submit(self.expected, lambda: called.append("ran"))
        self.assertTrue(task.cancel())
        self.host.query_pending.pop(0)()
        self.assertEqual(called, [])
        self.assertEqual(task.state, OperationState.CANCELED)
        self.assertTrue(task.result.not_executed)

    def test_start_then_timeout_or_cancel_never_claims_not_executed(self) -> None:
        called = []
        started = threading.Event()
        release = threading.Event()

        def operation():
            started.set()
            release.wait(timeout=1.0)
            called.append("ran")
            return "value"

        adapter = QueryOpAdapter(self.host, self.source)
        task = adapter.submit(self.expected, operation)
        operation = self.host.query_pending.pop(0)
        worker = threading.Thread(target=operation)
        worker.start()
        self.assertTrue(started.wait(timeout=1.0))
        task.timeout()
        self.assertFalse(task.cancel())
        release.set()
        worker.join(timeout=1.0)
        self.assertEqual(called, ["ran"])
        self.assertTrue(task.result.executed)
        self.assertFalse(task.result.not_executed)
        self.assertTrue(task.result.timed_out)

    def test_generation_or_epoch_change_at_end_discards_late_result(self) -> None:
        adapter = CollectionOpAdapter(self.host, self.source)

        def operation():
            self.source.current = OperationContext("profile-a", "generation-2", "epoch-1")
            return "must-drop"

        task = adapter.submit(self.expected, operation)
        self.host.collection_pending.pop(0)()
        self.assertEqual(task.state, OperationState.STALE_RESULT_DROPPED)
        self.assertTrue(task.result.discarded)
        self.assertIsNone(task.result.value)
        self.assertTrue(task.result.executed)

    def test_profile_close_before_start_rejects_without_running(self) -> None:
        called = []
        adapter = QueryOpAdapter(self.host, self.source)
        task = adapter.submit(self.expected, lambda: called.append("ran"))
        self.source.current = OperationContext("profile-closed", "generation-2", "epoch-2")
        self.host.query_pending.pop(0)()
        self.assertEqual(called, [])
        self.assertEqual(task.state, OperationState.STALE_RESULT_DROPPED)
        self.assertTrue(task.result.not_executed)

    def test_submit_is_non_blocking_and_uses_separate_collection_queue(self) -> None:
        adapter = CollectionOpAdapter(self.host, self.source)
        task = adapter.submit(self.expected, lambda: "ok")
        self.assertEqual(task.state, OperationState.QUEUED)
        self.assertEqual(len(self.host.collection_pending), 1)
        self.assertEqual(len(self.host.query_pending), 0)

    def _collection_op_adapter(self, *, schedule_on_main=None, empty_changes=None):
        factory = FakeCollectionOpFactory()
        empty_changes = empty_changes or FakeOpChanges()
        scheduled = []
        schedule = schedule_on_main if schedule_on_main is not None else scheduled.append
        host = AnkiCollectionOpHost(
            factory,
            parent=object(),
            op_changes_type=FakeOpChanges,
            empty_changes_factory=lambda: empty_changes,
            schedule_on_main=schedule,
        )
        return CollectionOpAdapter(host, self.source), factory, scheduled, empty_changes

    def test_collectionop_callback_returns_actual_changes_and_schedules_on_main(self) -> None:
        adapter, factory, scheduled, _empty_changes = self._collection_op_adapter()
        changes = FakeOpChanges()
        task = adapter.submit(self.expected, lambda _collection: changes)

        self.assertEqual(task.state, OperationState.QUEUED)
        self.assertEqual(factory.created, [])
        self.assertEqual(len(scheduled), 1)
        scheduled.pop()()

        self.assertEqual(len(factory.created), 1)
        collection_op = factory.created[0]
        self.assertEqual(collection_op.run_count, 1)
        returned = collection_op.op(object())

        self.assertIs(returned, changes)
        self.assertEqual(task.state, OperationState.COMPLETED)

    def test_collectionop_completion_callback_is_deferred_and_receives_result(self) -> None:
        factory = FakeCollectionOpFactory()
        scheduled = []
        after_collection_op = []
        completed = []
        host = AnkiCollectionOpHost(
            factory,
            parent=object(),
            op_changes_type=FakeOpChanges,
            empty_changes_factory=FakeOpChanges,
            schedule_on_main=scheduled.append,
            schedule_after_collection_op=after_collection_op.append,
        )
        adapter = CollectionOpAdapter(host, self.source)
        result = FakeResultWithChanges(FakeOpChanges())
        task = adapter.submit(
            self.expected,
            lambda _collection: result,
            on_collection_complete=completed.append,
        )
        self.assertEqual(task.state, OperationState.QUEUED)
        scheduled.pop()()
        collection_op = factory.created[0]
        callback_result = collection_op.op(object())

        self.assertIs(callback_result, result)
        self.assertEqual(completed, [])
        collection_op.success_callback(callback_result)
        self.assertEqual(completed, [])
        self.assertEqual(len(after_collection_op), 1)
        after_collection_op.pop()()
        self.assertEqual(completed, [result])

    def test_reviewer_ownership_gate_rejects_before_collectionop_creation(self) -> None:
        factory = FakeCollectionOpFactory()
        scheduled = []
        failures = []
        operation_calls = []
        host = AnkiCollectionOpHost(
            factory,
            parent=object(),
            op_changes_type=FakeOpChanges,
            empty_changes_factory=FakeOpChanges,
            schedule_on_main=scheduled.append,
        )
        adapter = CollectionOpAdapter(host, self.source)
        task = adapter.submit(
            self.expected,
            lambda _collection: operation_calls.append("ran"),
            on_collection_failure=failures.append,
            before_collection_start=lambda: False,
        )

        scheduled.pop()()
        self.assertEqual(factory.created, [])
        self.assertEqual(operation_calls, [])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], CollectionStartRejected)
        self.assertEqual(failures[0].code, "reviewer_owns_collection")
        self.assertEqual(task.state, OperationState.FAILED)
        self.assertFalse(task.result.executed)

    def test_collectionop_main_thread_start_failure_reaches_failure_callback(self) -> None:
        scheduled = []
        failures = []

        def broken_factory(*, parent, op):
            raise RuntimeError("CollectionOp creation failed")

        host = AnkiCollectionOpHost(
            broken_factory,
            parent=object(),
            op_changes_type=FakeOpChanges,
            empty_changes_factory=FakeOpChanges,
            schedule_on_main=scheduled.append,
        )
        adapter = CollectionOpAdapter(host, self.source)
        task = adapter.submit(
            self.expected,
            lambda _collection: FakeOpChanges(),
            on_collection_failure=failures.append,
        )

        scheduled.pop()()

        self.assertEqual(task.state, OperationState.FAILED)
        self.assertFalse(task.result.executed)
        self.assertEqual(len(failures), 1)
        self.assertRegex(str(failures[0]), "CollectionOp creation failed")

    def test_synchronous_collection_submit_failure_reaches_failure_callback(self) -> None:
        failures = []
        failure = RuntimeError("main-thread scheduling failed")

        class BrokenHost:
            def submit_collection(self, _operation, **_kwargs):
                raise failure

        adapter = CollectionOpAdapter(BrokenHost(), self.source)
        task = adapter.submit(
            self.expected,
            lambda _collection: FakeOpChanges(),
            on_collection_failure=failures.append,
        )

        self.assertEqual(task.state, OperationState.FAILED)
        self.assertFalse(task.result.executed)
        self.assertEqual(failures, [failure])

    def test_collectionop_callback_accepts_result_with_real_changes(self) -> None:
        factory = FakeCollectionOpFactory()
        changes = FakeOpChanges()
        wrapped = FakeResultWithChanges(changes)
        host = AnkiCollectionOpHost(
            factory,
            parent=object(),
            op_changes_type=FakeOpChanges,
            empty_changes_factory=FakeOpChanges,
        )
        host.submit_collection(lambda _collection: wrapped)

        self.assertIs(factory.created[0].op(object()), wrapped)

    def test_stale_after_collection_commit_still_returns_real_changes_to_anki(self) -> None:
        adapter, factory, scheduled, _empty_changes = self._collection_op_adapter()
        changes = FakeOpChanges()

        def operation(_collection):
            self.source.current = OperationContext("profile-a", "generation-2", "epoch-1")
            return changes

        task = adapter.submit(self.expected, operation)
        scheduled.pop()()
        returned = factory.created[0].op(object())

        self.assertEqual(task.state, OperationState.STALE_RESULT_DROPPED)
        self.assertTrue(task.result.discarded)
        self.assertIsNone(task.result.value)
        self.assertIs(returned, changes)

    def test_canceled_before_collectionop_start_returns_empty_opchanges(self) -> None:
        empty_changes = FakeOpChanges()
        adapter, factory, scheduled, _ = self._collection_op_adapter(empty_changes=empty_changes)
        called = []
        task = adapter.submit(self.expected, lambda _collection: called.append("ran"))
        self.assertTrue(task.cancel())
        scheduled.pop()()

        returned = factory.created[0].op(object())
        self.assertEqual(called, [])
        self.assertEqual(task.state, OperationState.CANCELED)
        self.assertIs(returned, empty_changes)

    def test_collectionop_failure_propagates_and_does_not_forge_changes(self) -> None:
        adapter, factory, scheduled, _empty_changes = self._collection_op_adapter()
        failure = RuntimeError("collection operation failed")

        def operation(_collection):
            raise failure

        task = adapter.submit(self.expected, operation)
        scheduled.pop()()
        with self.assertRaisesRegex(RuntimeError, "collection operation failed") as raised:
            factory.created[0].op(object())

        self.assertIs(raised.exception, failure)
        self.assertEqual(task.state, OperationState.FAILED)
        self.assertTrue(task.result.executed)

    def test_collectionop_rejects_non_opchanges_result(self) -> None:
        factory = FakeCollectionOpFactory()
        host = AnkiCollectionOpHost(
            factory,
            parent=object(),
            op_changes_type=FakeOpChanges,
            empty_changes_factory=FakeOpChanges,
        )
        host.submit_collection(lambda _collection: "not changes")

        with self.assertRaisesRegex(TypeError, "must return OpChanges"):
            factory.created[0].op(object())


if __name__ == "__main__":
    unittest.main()
