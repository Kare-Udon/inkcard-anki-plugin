"""InkCards Anki bridge v1 的纯协议定义与边界校验。"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any


BRIDGE_VERSION = "0.1.0-m1"
PROTOCOL_VERSION = "inkcards-anki-bridge-v1"
SUPPORTED_ANKI_VERSION = "26.8.1"

LOOPBACK_HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_REQUEST_ID_BYTES = 128
MAX_CARD_IDS = 256
DISPATCH_TIMEOUT_SECONDS = 2.0
REQUEST_SOCKET_TIMEOUT_SECONDS = 5.0

ENDPOINTS = {
    "/v1/hello": "hello",
    "/v1/capabilities": "capabilities",
    "/v1/export/scheduling_snapshot": "export_scheduling_snapshot",
    "/v1/apply/events": "apply_events",
    "/v1/status": "status",
    "/v1/watermark": "watermark",
}

CAPABILITIES = {
    "hello": True,
    "capabilities": True,
    "export_scheduling_snapshot": True,
    "apply_events": False,
    "status": True,
    "watermark": True,
}


class ProtocolError(Exception):
    """可以安全返回给 RPC 对端的协议错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class Request:
    """已通过边界校验的 JSON 请求。"""

    method: str
    request_id: str
    params: dict[str, Any]


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_json_key", "JSON object contains duplicate keys")
        result[key] = value
    return result


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError("invalid_request_id", "request_id must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_REQUEST_ID_BYTES:
        raise ProtocolError("invalid_request_id", "request_id is too large")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ProtocolError("invalid_request_id", "request_id contains a control character")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def parse_request(raw: bytes, expected_method: str) -> Request:
    """解析一个请求体；未知字段允许保留以便未来向前兼容。"""

    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError(
            "request_too_large",
            "request body exceeds the configured byte limit",
            status=413,
        )

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProtocolError("malformed_json", "request body is not valid UTF-8 JSON") from None

    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "request body must be a JSON object")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_protocol_version",
            "protocol_version is not supported",
            status=422,
        )
    if payload.get("method") != expected_method:
        raise ProtocolError("method_mismatch", "request method does not match the endpoint")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ProtocolError("invalid_params", "params must be a JSON object")

    return Request(
        method=expected_method,
        request_id=_request_id(payload.get("request_id")),
        params=params,
    )


def json_bytes(payload: dict[str, Any]) -> bytes:
    """以严格 JSON 编码结果，禁止 NaN/Infinity 泄漏到协议。"""

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("non_json_result", "result is not JSON serializable") from exc
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ProtocolError(
            "response_too_large",
            "response exceeds the configured byte limit",
            status=413,
        )
    return encoded


def success_response(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


def error_response(request_id: str | None, error: ProtocolError) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        },
    }
