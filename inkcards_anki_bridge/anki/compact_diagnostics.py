"""Compact 终态安全接入统一插件日志；不另行写常开诊断文件。"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from ..logging_control import AddonLogger


def stable_code(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value)
        else None
    )


class CompactDiagnostics:
    """聚合安全异常摘要，在作业终态时发到 profile 统一 JSONL。"""

    def __init__(self, root: Path, *, logger: AddonLogger | None = None) -> None:
        self.logger = logger or AddonLogger(root)
        self.path = self.logger.path
        self._lock = threading.RLock()
        self._exception: dict[str, Any] | None = None

    def begin(self) -> None:
        with self._lock:
            self._exception = None

    @staticmethod
    def _exception_frame(error: BaseException, *, include_traceback: bool) -> dict[str, Any]:
        record: dict[str, Any] = {"type": type(error).__name__}
        code = stable_code(getattr(error, "code", None))
        if code is not None:
            record["code"] = code
        if include_traceback:
            frames = AddonLogger._redacted_traceback(error)
            if frames:
                record["traceback"] = frames
        return record

    def record_exception(self, error: BaseException, stage: str) -> None:
        if not self.logger.enabled("error"):
            return
        include_traceback = self.logger.enabled("debug")
        record = self._exception_frame(error, include_traceback=include_traceback)
        record["stage"] = stable_code(stage)
        if include_traceback:
            chain = []
            seen = {id(error)}
            current = error
            while True:
                nested = current.__cause__
                relation = "cause"
                if nested is None and not current.__suppress_context__:
                    nested = current.__context__
                    relation = "context"
                if nested is None:
                    break
                if id(nested) in seen or len(chain) >= 3:
                    record["chain_truncated"] = True
                    break
                seen.add(id(nested))
                node = self._exception_frame(nested, include_traceback=True)
                node["relation"] = relation
                chain.append(node)
                current = nested
            if chain:
                record["chain"] = chain
        with self._lock:
            self._exception = record

    def record_terminal(self, status: str, reason: str | None) -> None:
        with self._lock:
            exception = self._exception.copy() if self._exception is not None else None
        if exception is not None:
            level = "error"
        elif status in {"failed", "unknown"}:
            level = "error"
        elif status in {"blocked", "rejected"}:
            level = "warn"
        else:
            level = "info"
        if not self.logger.enabled(level):
            return
        fields: dict[str, Any] = {
            "state": stable_code(status),
            "reason": stable_code(reason),
        }
        if exception is not None:
            fields["exception"] = exception
        self.logger.emit(level, "compact", "terminal", **fields)


__all__ = ["CompactDiagnostics", "stable_code"]
