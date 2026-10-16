"""本地终态诊断：只记录稳定代码、类型、模块与行号，写入失败不影响作业。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def stable_code(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value)
        else None
    )


class CompactDiagnostics:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "compact-diagnostic.json"
        self._lock = threading.RLock()
        self._exception: dict[str, Any] | None = None

    def begin(self) -> None:
        with self._lock:
            self._exception = None

    @staticmethod
    def _exception_frame(error: BaseException) -> dict[str, Any]:
        # 不读取 message、args、locals 或文件路径。
        frames = []
        tb = error.__traceback__
        while tb is not None:
            module = tb.tb_frame.f_globals.get("__name__", "")
            if isinstance(module, str) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.]*", module
            ):
                frames.append({"module": module, "line": tb.tb_lineno})
            tb = tb.tb_next
        return {
            "type": type(error).__name__,
            "code": stable_code(getattr(error, "code", None)),
            "traceback": frames[-16:],
        }

    def record_exception(self, error: BaseException, stage: str) -> None:
        record = self._exception_frame(error)
        record["stage"] = stable_code(stage)
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
            # 外层加最多三层；环和更深链只标记截断。
            if id(nested) in seen or len(chain) >= 3:
                record["chain_truncated"] = True
                break
            seen.add(id(nested))
            node = self._exception_frame(nested)
            node["relation"] = relation
            chain.append(node)
            current = nested
        if chain:
            record["chain"] = chain
        with self._lock:
            self._exception = record

    def record_terminal(self, status: str, reason: str | None) -> None:
        temporary = None
        try:
            with self._lock:
                data: dict[str, Any] = {
                    "schema": "inkcards-compact-diagnostic-v1",
                    "status": stable_code(status),
                    "reason": stable_code(reason),
                }
                if self._exception is not None and status in {
                    "blocked",
                    "unknown",
                    "failed",
                }:
                    data["exception"] = self._exception.copy()
                tz = os.environ.get("TZ")
                try:
                    if tz is None or not re.fullmatch(
                        r"[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*", tz
                    ):
                        tz = None
                    elif tz:
                        ZoneInfo(tz)
                except (ZoneInfoNotFoundError, ValueError):
                    tz = None
                data["process_clock"] = {
                    "tz_env": tz,
                    "local_utc_offset_seconds": getattr(
                        time.localtime(), "tm_gmtoff", None
                    ),
                }
                fd, temporary = tempfile.mkstemp(
                    prefix=".compact-diagnostic-", dir=self.path.parent
                )
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
        except Exception:  # noqa: BLE001,S110 - 诊断失败不得改变作业状态。
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
