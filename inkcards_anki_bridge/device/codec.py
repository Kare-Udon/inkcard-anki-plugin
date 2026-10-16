"""x4-2 wire JSON 的严格、有界编解码。

请求和响应都必须是 UTF-8 JSON object。字节上限在 decode/encode 的边界检查，
因此预算按实际 wire bytes 计算，而不是按 Python 字符数计算。
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Union


class CodecError(ValueError):
    """可安全返回给 route 的稳定编解码错误；不携带原始输入。"""

    def __init__(
        self, code: str, *, status: int = 400, retryable: bool = False
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return "CodecError(%r)" % self.code


def _reject_constant(_: str) -> None:
    raise CodecError("non_finite_number")


def _object_pairs(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodecError("duplicate_key")
        result[key] = value
    return result


def _limit(value: Optional[int], code: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid byte limit")
    return value


def decode_json(raw: Union[bytes, bytearray, memoryview], *, max_bytes: Optional[int] = None) -> Any:
    """严格解析 UTF-8 JSON，拒绝重复 key、非有限数字和坏字节。"""

    limit = _limit(max_bytes, "request_too_large")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise CodecError("invalid_wire_bytes")
    wire = bytes(raw)
    if limit is not None and len(wire) > limit:
        raise CodecError("request_too_large", status=413)
    try:
        text = wire.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise CodecError("invalid_utf8") from None
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except CodecError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise CodecError("invalid_json") from None


def decode_object(
    raw: Union[bytes, bytearray, memoryview],
    *,
    max_bytes: Optional[int] = None,
    required: Optional[Iterable[str]] = None,
    allowed: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """解析顶层 object 并执行 endpoint 的 required/allowlist 校验。"""

    value = decode_json(raw, max_bytes=max_bytes)
    if not isinstance(value, dict):
        raise CodecError("top_level_object_required")
    required_set: Set[str] = set(required or ())
    allowed_set: Optional[Set[str]] = set(allowed) if allowed is not None else None
    if allowed_set is not None and any(key not in allowed_set for key in value):
        raise CodecError("unknown_field")
    if any(key not in value for key in required_set):
        raise CodecError("missing_field")
    return value


def _validate_output(value: Any) -> None:
    """提前拒绝会导致 JSON 非严格或 key 不可控的 Python 值。"""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise CodecError("invalid_response")
            _validate_output(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_output(nested)
    elif isinstance(value, float) and not math.isfinite(value):
        raise CodecError("non_finite_number")
    elif value is not None and not isinstance(value, (str, int, bool, float)):
        raise CodecError("invalid_response")


def encode_json(value: Any, *, max_bytes: Optional[int] = None) -> bytes:
    """生成确定性 UTF-8 JSON，并按编码后的真实字节检查响应预算。"""

    limit = _limit(max_bytes, "response_too_large")
    _validate_output(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        wire = text.encode("utf-8")
    except CodecError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise CodecError("invalid_response") from None
    if limit is not None and len(wire) > limit:
        raise CodecError("response_too_large", status=413)
    return wire


def error_envelope(code: str, *, retryable: bool = False) -> Dict[str, Any]:
    """返回统一、稳定且不回显输入的错误 envelope。"""

    if not isinstance(code, str) or not code:
        code = "internal_error"
    return {
        "error": {
            "code": code,
            "message": code,
            "retryable": bool(retryable),
        }
    }


__all__ = [
    "CodecError",
    "decode_json",
    "decode_object",
    "encode_json",
    "error_envelope",
]
