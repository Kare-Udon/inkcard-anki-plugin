"""HTTP 请求到统一插件日志器的安全适配。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging_control import AddonLogger, MAX_LOG_BYTES


_SAFE_CODE_LENGTH = 64


def _safe_code(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= _SAFE_CODE_LENGTH:
        return None
    if not ("a" <= value[0] <= "z") or not all(
        "a" <= char <= "z" or "0" <= char <= "9" or char == "_"
        for char in value
    ):
        return None
    return value


class DeviceRequestLog:
    """保持旧 server 接口，HTTP 事件由 profile 统一日志器筛选和写盘。"""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        max_bytes: int = MAX_LOG_BYTES,
        logger: AddonLogger | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.logger = logger or AddonLogger(runtime_root, max_bytes=max_bytes)
        self.max_bytes = max_bytes

    @property
    def path(self) -> Path | None:
        return self.logger.path

    @property
    def backup_path(self) -> Path | None:
        path = self.path
        return path.with_name(path.name + ".1") if path is not None else None

    def enabled(self, level: str) -> bool:
        return self.logger.enabled(level)

    def should_measure_request(self) -> bool:
        """仅在可能写请求诊断时启用计时和 body 字节计数。"""

        return any(self.enabled(level) for level in ("info", "warn", "error"))

    def _level_for_status(self, status: Any, error_code: Any) -> str:
        if isinstance(status, int) and not isinstance(status, bool):
            if 500 <= status <= 599:
                return "error"
            if 400 <= status <= 499:
                return "warn"
            return "info"
        if isinstance(error_code, str) and error_code in {
            "request_read_error", "request_timeout", "request_body_incomplete"
        }:
            return "warn"
        return "error"

    def error_code_from_response(self, status: int, body: bytes) -> str | None:
        level = self._level_for_status(status, None)
        if not self.logger.enabled(level) or 200 <= status < 400:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
            error = payload.get("error") if isinstance(payload, dict) else None
            candidate = error.get("code") if isinstance(error, dict) else None
        except Exception:
            candidate = None
        return _safe_code(candidate) or "unknown"

    def record(
        self,
        *,
        method: Any,
        target: Any,
        status: Any,
        error_code: Any,
        elapsed_ms: Any,
        request_body_bytes: Any,
        response_body_bytes: Any,
        stage: str | None = None,
        exception: BaseException | None = None,
        expected_body_bytes: Any = None,
    ) -> bool:
        level = self._level_for_status(status, error_code)
        if not self.logger.enabled(level):
            return False
        safe_status = (
            status
            if isinstance(status, int)
            and not isinstance(status, bool)
            and 100 <= status <= 599
            else None
        )
        fields: dict[str, Any] = {
            "method": method,
            "route": target,
            "status": safe_status,
            "error_code": _safe_code(error_code) or (
                "unknown" if safe_status is not None and not 200 <= safe_status < 300 else None
            ),
            "elapsed_ms": elapsed_ms,
            "request_body_bytes": request_body_bytes,
            "response_body_bytes": response_body_bytes,
            "stage": stage,
            "expected_body_bytes": expected_body_bytes,
        }
        if exception is not None:
            fields.update(
                self.logger.exception_fields(
                    exception,
                    stage=stage if stage is not None else "request_handler",
                )
            )
        return self.logger.emit(
            level,
            "device",
            "http_request",
            **fields,
        )


__all__ = ["DeviceRequestLog", "MAX_LOG_BYTES"]
