"""显式的 Anki 执行上下文边界。"""

from __future__ import annotations

import threading
from typing import Callable, Protocol, TypeVar


T = TypeVar("T")


class DispatcherError(Exception):
    """调度器错误。"""


class DispatchTimeout(DispatcherError):
    """主线程没有在规定时间内完成任务。"""


class Dispatcher(Protocol):
    def submit(self, operation: Callable[[], T], timeout_s: float) -> T:
        """在宿主允许的上下文执行 operation。"""


class InlineDispatcher:
    """仅用于普通 Python 测试；生产 Anki 不使用此实现。"""

    def submit(self, operation: Callable[[], T], timeout_s: float) -> T:
        del timeout_s
        return operation()


class MainThreadDispatcher:
    """把闭包投递给 ``aqt.mw.taskman.run_on_main``。"""

    def __init__(self, schedule_on_main: Callable[[Callable[[], None]], None]) -> None:
        self._schedule_on_main = schedule_on_main

    def submit(self, operation: Callable[[], T], timeout_s: float) -> T:
        completed = threading.Event()
        result: list[T] = []
        failure: list[BaseException] = []

        def run() -> None:
            try:
                result.append(operation())
            except BaseException as exc:  # 由调用线程重新抛出，避免吞错
                failure.append(exc)
            finally:
                completed.set()

        self._schedule_on_main(run)
        if not completed.wait(timeout_s):
            raise DispatchTimeout("main-thread operation timed out")
        if failure:
            raise failure[0]
        if not result:
            raise DispatcherError("main-thread operation returned no result")
        return result[0]
