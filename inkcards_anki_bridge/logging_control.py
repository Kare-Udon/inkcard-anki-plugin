"""插件内轻量、脱敏、按 profile 隔离的 JSONL 日志。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO


LOG_LEVELS = {
    "off": logging.CRITICAL + 1,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}
DEFAULT_LOGGING_POLICY = {"level": "warn", "console": False}
MAX_LOG_BYTES = 128 * 1024
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,119}\Z")
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"})
_ROUTES = frozenset(
    {
        "/v2/diagnostics/report", "/v2/pair", "/v2/pair/confirm", "/v2/handshake", "/v2/reviews", "/v2/receipts",
        "/v2/sync/status", "/v2/pull/open", "/v2/pull/refresh", "/v2/pull/page",
        "/v2/pull/commit", "/v2/artifacts/open", "/v2/artifacts/manifest",
        "/v2/artifacts/file", "/v2/artifacts/commit", "/v2/artifacts/status",
        "/v2/override/prepare", "/v2/override/confirm", "/v3/reviews/compact",
        "/v3/receipts/compact",
    }
)
_SAFE_FIELDS = frozenset(
    {
        "method", "route", "status", "error_code", "elapsed_ms",
        "request_body_bytes", "response_body_bytes", "stage", "code",
        "reason", "state", "exception_type", "traceback", "exception",
        "chain_truncated", "expected_body_bytes",
    }
)
_ROOT_UNSET = object()


def normalize_logging_policy(value: Any) -> dict[str, Any]:
    """返回完整策略；缺失或损坏的配置统一回落到 warn/false。"""

    if not isinstance(value, Mapping):
        return dict(DEFAULT_LOGGING_POLICY)
    level = value.get("level")
    console = value.get("console")
    if not isinstance(level, str) or level not in LOG_LEVELS or not isinstance(console, bool):
        return dict(DEFAULT_LOGGING_POLICY)
    return {"level": level, "console": console}


def logging_policy_from_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return dict(DEFAULT_LOGGING_POLICY)
    return normalize_logging_policy(config.get("logging"))


def _safe_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31 - 1:
        return None
    return value


def _safe_code(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _safe_frames(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, (list, tuple)):
        return None
    frames: list[dict[str, Any]] = []
    for frame in value[:16]:
        if not isinstance(frame, Mapping):
            continue
        module = frame.get("module")
        line = _safe_count(frame.get("line"))
        if isinstance(module, str) and _SAFE_NAME.fullmatch(module) and line is not None:
            frames.append({"module": module, "line": line})
    return frames


def _safe_exception(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    exception_type = value.get("type")
    if isinstance(exception_type, str) and _SAFE_NAME.fullmatch(exception_type):
        result["type"] = exception_type
    for key in ("code", "stage"):
        selected = _safe_code(value.get(key))
        if selected is not None:
            result[key] = selected
    frames = _safe_frames(value.get("traceback"))
    if frames:
        result["traceback"] = frames
    chain_value = value.get("chain")
    if isinstance(chain_value, (list, tuple)):
        chain = []
        for item in chain_value[:3]:
            if not isinstance(item, Mapping):
                continue
            node: dict[str, Any] = {}
            item_type = item.get("type")
            relation = item.get("relation")
            if isinstance(item_type, str) and _SAFE_NAME.fullmatch(item_type):
                node["type"] = item_type
            if relation in {"cause", "context"}:
                node["relation"] = relation
            item_code = _safe_code(item.get("code"))
            if item_code is not None:
                node["code"] = item_code
            item_frames = _safe_frames(item.get("traceback"))
            if item_frames:
                node["traceback"] = item_frames
            if node:
                chain.append(node)
        if chain:
            result["chain"] = chain
    truncated = value.get("chain_truncated")
    if isinstance(truncated, bool):
        result["chain_truncated"] = truncated
    return result or None


def _clean_field(key: str, value: Any) -> Any:
    if key not in _SAFE_FIELDS:
        return None
    if key in {"method"}:
        return value if isinstance(value, str) and value in _METHODS else "unknown"
    if key == "route":
        return value if isinstance(value, str) and value in _ROUTES else "unknown"
    if key in {"status"}:
        return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None
    if key in {"elapsed_ms", "request_body_bytes", "response_body_bytes", "expected_body_bytes"}:
        return _safe_count(value)
    if key in {"error_code", "code", "reason", "stage", "state"}:
        return _safe_code(value)
    if key == "exception_type":
        return value if isinstance(value, str) and _SAFE_NAME.fullmatch(value) else "Exception"
    if key == "traceback":
        return _safe_frames(value)
    if key == "exception":
        return _safe_exception(value)
    if key == "chain_truncated":
        return value if isinstance(value, bool) else None
    return None


class _SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "schema": "inkcards-plugin-log-v1",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": "warn" if record.levelno == logging.WARNING else record.levelname.lower(),
            "component": record.component,
            "event": record.event,
        }
        for key in _SAFE_FIELDS:
            if hasattr(record, key):
                value = _clean_field(key, getattr(record, key))
                if value is not None or key in {
                    "status", "error_code", "code", "reason", "state"
                }:
                    data[key] = value
        return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


class _SafeRotatingFileHandler(RotatingFileHandler):
    def __init__(self, path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> None:
        super().__init__(path, maxBytes=max_bytes, backupCount=1, encoding="utf-8", delay=True)
        self._directory_ready = False
        self.setFormatter(_SafeJsonFormatter())

    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, "a", encoding=self.encoding, errors=self.errors)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._directory_ready:
                directory = Path(self.baseFilename).parent
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass
                self._directory_ready = True
            super().emit(record)
        except Exception:
            # 日志系统错误不能改变插件业务；也不能让 logging.handleError 输出原始栈。
            return

    def handleError(self, record: logging.LogRecord) -> None:
        return


class _SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        return


class AddonLogger:
    """单 profile 插件日志器；过滤、序列化和文件创建由标准 logging 驱动。"""

    def __init__(
        self,
        root: str | Path | None,
        *,
        policy: Any = None,
        console_stream: TextIO | None = None,
        max_bytes: int = MAX_LOG_BYTES,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._lock = threading.RLock()
        self._root = Path(root) if root is not None else None
        self._logger = logging.getLogger(f"inkcards.plugin.{uuid.uuid4().hex}")
        self._logger.propagate = False
        self._console_stream = console_stream if console_stream is not None else sys.stderr
        self._max_bytes = max_bytes
        self._policy = dict(DEFAULT_LOGGING_POLICY)
        self._configure_handlers(normalize_logging_policy(policy))

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def path(self) -> Path | None:
        return self._root / "diagnostics" / "plugin.jsonl" if self._root is not None else None

    @property
    def policy(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._policy)

    def configure(
        self,
        policy: Any,
        *,
        root: str | Path | None | object = _ROOT_UNSET,
    ) -> dict[str, Any]:
        normalized = normalize_logging_policy(policy)
        with self._lock:
            if root is not _ROOT_UNSET:
                selected_root = Path(root) if root is not None else None
                if selected_root != self._root:
                    self._root = selected_root
            self._configure_handlers(normalized)
            return dict(self._policy)

    def _configure_handlers(self, policy: dict[str, Any]) -> None:
        for handler in tuple(self._logger.handlers):
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        self._policy = dict(policy)
        self._logger.setLevel(LOG_LEVELS[policy["level"]])
        if policy["level"] == "off":
            return
        if self._root is not None:
            self._logger.addHandler(_SafeRotatingFileHandler(self.path, max_bytes=self._max_bytes))
        if policy["console"]:
            console = _SafeStreamHandler(self._console_stream)
            console.setFormatter(_SafeJsonFormatter())
            self._logger.addHandler(console)
        if not self._logger.handlers:
            # Logger.callHandlers 在没有 handler 时会调用 logging.lastResort。
            # profile 根目录不可用时保留显式空接收器，避免日志泄漏到 stderr。
            self._logger.addHandler(logging.NullHandler())

    def enabled(self, level: str) -> bool:
        with self._lock:
            number = LOG_LEVELS.get(level) if isinstance(level, str) else None
            if number is None or level == "off":
                return False
            return self._logger.isEnabledFor(number)

    def emit(self, level: str, component: str, event: str, **fields: Any) -> bool:
        # 用同一把锁串行化 Logger.log/handler 调用与 configure()/close()。
        # 否则已移除的 handler 仍可能在热关闭返回后继续写入。
        with self._lock:
            number = LOG_LEVELS.get(level) if isinstance(level, str) else None
            if number is None or level == "off" or not self._logger.isEnabledFor(number):
                return False
            if (
                not isinstance(component, str)
                or not isinstance(event, str)
                or not _SAFE_CODE.fullmatch(component)
                or not _SAFE_CODE.fullmatch(event)
            ):
                return False
            extra: dict[str, Any] = {"component": component, "event": event}
            for key, value in fields.items():
                if key in _SAFE_FIELDS:
                    if self._policy["level"] != "debug":
                        if key == "traceback":
                            continue
                        if key == "exception" and isinstance(value, Mapping):
                            value = {
                                detail: value[detail]
                                for detail in ("type", "code", "stage")
                                if detail in value
                            }
                    extra[key] = value
            self._logger.log(number, "", extra=extra)
            return True

    @staticmethod
    def _redacted_traceback(error: BaseException) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        trace = error.__traceback__
        while trace is not None and len(frames) < 16:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if isinstance(module, str) and _SAFE_NAME.fullmatch(module):
                frames.append({"module": module, "line": trace.tb_lineno})
            trace = trace.tb_next
        return frames

    def log_exception(
        self,
        component: str,
        event: str,
        error: BaseException,
        *,
        stage: str,
    ) -> bool:
        with self._lock:
            if not self.enabled("error"):
                return False
            fields = self.exception_fields(error, stage=stage)
            return self.emit("error", component, event, **fields)

    def exception_fields(self, error: BaseException, *, stage: str) -> dict[str, Any]:
        with self._lock:
            if not self.enabled("error"):
                return {}
            exception_type = type(error).__name__
            if not _SAFE_NAME.fullmatch(exception_type):
                exception_type = "Exception"
            fields: dict[str, Any] = {
                "stage": stage,
                "exception_type": exception_type,
                "code": getattr(error, "code", None),
            }
            if self.enabled("debug"):
                fields["traceback"] = self._redacted_traceback(error)
            return fields

    def close(self) -> None:
        self.configure({"level": "off", "console": False})


__all__ = [
    "AddonLogger",
    "DEFAULT_LOGGING_POLICY",
    "LOG_LEVELS",
    "MAX_LOG_BYTES",
    "logging_policy_from_config",
    "normalize_logging_policy",
]
