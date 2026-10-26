"""x4-2 配对、Bearer 认证和运行时 session 边界。

设备 token 是 256-bit 随机值，仅以 hash 形式交给 :class:`ProfileStore`。本模块
不会把长期 token 放在自己的持久状态中；运行时 session 只绑定 profile/device/session
身份。HTTP 明文只适用于用户已信任的 LAN，不提供 TLS、互联网暴露或 CORS 安全边界。
"""

from __future__ import annotations

import hashlib
import re
import secrets as _stdlib_secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Tuple

from ..storage import ProfileStore, SessionRecord, StorageValidationError
from ..storage.device_management import PairingStateError


_IDENTIFIER = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


class AuthError(Exception):
    """认证/配对失败；字符串只包含稳定错误码，不回显 secret。"""

    def __init__(self, code: str, *, status: int = 401, retryable: bool = False) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return "AuthError(%r)" % self.code


@dataclass(frozen=True)
class AuthPrincipal:
    """Bearer 验证后的 profile/device 主体。"""

    profile_id: str
    device_id: str


@dataclass(frozen=True)
class DeviceCredential:
    """一次配对成功的设备凭据；repr 永远隐藏原始 token。"""

    profile_id: str
    device_id: str
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "DeviceCredential(profile_id=%r, device_id=%r, token=<redacted>)"
            % (self.profile_id, self.device_id)
        )


@dataclass(frozen=True)
class RuntimeSession:
    """不含长期 token 的运行时 session 绑定。"""

    profile_id: str
    device_id: str
    session_id: str
    high_water: int
    created_at_ms: int


class _CodeResult(dict):
    """管理调用可读取原码，但诊断 repr 不包含秘密。"""

    def __repr__(self):
        return repr({key: ("<redacted>" if key == "code" else value) for key, value in self.items()})

    __str__ = __repr__


def _default_clock() -> int:
    return int(time.time() * 1000)


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.match(value))


class DeviceAuthenticator:
    """管理固定配对码、持久凭据事务和运行时 session。"""

    def __init__(
        self,
        store: ProfileStore,
        *,
        clock: Optional[Callable[[], int]] = None,
        secrets_source: Optional[Any] = None,
        secrets: Optional[Any] = None,
        pairing_ttl_ms: int = 120_000,
        max_pairing_attempts: int = 5,
        pairing_attempt_window_ms: int = 60_000,
    ) -> None:
        if not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        if secrets_source is not None and secrets is not None:
            raise TypeError("provide only one secrets source")
        if (
            isinstance(pairing_ttl_ms, bool)
            or not isinstance(pairing_ttl_ms, int)
            or pairing_ttl_ms <= 0
            or isinstance(max_pairing_attempts, bool)
            or not isinstance(max_pairing_attempts, int)
            or max_pairing_attempts <= 0
            or isinstance(pairing_attempt_window_ms, bool)
            or not isinstance(pairing_attempt_window_ms, int)
            or pairing_attempt_window_ms <= 0
        ):
            raise ValueError("invalid pairing limits")
        self.store = store
        self._clock = clock or _default_clock
        self._entropy = secrets_source or secrets or _stdlib_secrets
        # 仅保留旧 Python 构造参数兼容；固定码不使用 TTL。
        self._max_pairing_attempts = max_pairing_attempts
        self._pairing_attempt_window_ms = pairing_attempt_window_ms
        self._lock = threading.RLock()
        self._attempts: Deque[int] = deque()
        self._sessions: Dict[Tuple[str, str, str], RuntimeSession] = {}

    def __repr__(self) -> str:
        return "DeviceAuthenticator(profile_id=%r, active_sessions=%d)" % (
            self.store.profile_id,
            len(self._sessions),
        )

    def _now(self, now_ms: Optional[int] = None) -> int:
        value = self._clock() if now_ms is None else now_ms
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("clock must return a non-negative integer")
        return value

    def _sweep(self, now_ms: int) -> None:
        cutoff = now_ms - self._pairing_attempt_window_ms
        while self._attempts and self._attempts[0] <= cutoff:
            self._attempts.popleft()

    def _record_failed_attempt(self, now_ms: int) -> None:
        self._attempts.append(now_ms)

    def _rate_limited(self, now_ms: int) -> bool:
        self._sweep(now_ms)
        return len(self._attempts) >= self._max_pairing_attempts

    def _pairing_failure(self, now_ms: int) -> AuthError:
        if self._rate_limited(now_ms):
            return AuthError("pairing_rate_limited", status=429, retryable=True)
        self._record_failed_attempt(now_ms)
        return AuthError("invalid_pairing_code")

    def _token_bytes(self, length: int) -> bytes:
        generator = getattr(self._entropy, "token_bytes", None)
        if not callable(generator):
            raise ValueError("entropy source must provide token_bytes")
        raw = generator(length)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != length:
            raise ValueError("entropy returned an invalid token")
        return bytes(raw)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    @staticmethod
    def _management_error(error: PairingStateError) -> AuthError:
        status = {
            "invalid_pairing_code": 401,
            "device_revoked": 403,
            "unknown_device_slot": 404,
        }.get(error.code, 409)
        return AuthError(error.code, status=status)

    def _code(self) -> str:
        # 与客户端 token 独立的系统熵；不依赖时间或进程内计数器。
        return "%012d" % (int.from_bytes(self._token_bytes(16), "big") % 1_000_000_000_000)

    def add_device(self, name: str) -> dict:
        if not _valid_identifier(name) or not name.strip():
            raise AuthError("invalid_device_name", status=422)
        slot_id = "slot_" + uuid.uuid4().hex
        for _ in range(8):
            code = self._code()
            try:
                self.store.add_device_slot(slot_id, name, self._digest(code), self._now())
                return _CodeResult(slot_id=slot_id, name=name, status="pending", code=code)
            except PairingStateError as error:
                if error.code != "pairing_code_collision":
                    raise self._management_error(error) from error
        raise AuthError("pairing_unavailable", status=503, retryable=True)

    def list_devices(self, include_revoked: bool = False) -> list:
        return self.store.list_device_slots(include_revoked)

    def _manage(self, slot_id: str, action: str, **fields):
        if not _valid_identifier(slot_id):
            raise AuthError("invalid_device_slot", status=422)
        try:
            return self.store.manage_device_slot(slot_id, action, now=self._now(), **fields)
        except PairingStateError as error:
            raise self._management_error(error) from error

    def allow_rebind(self, slot_id: str) -> None:
        self._manage(slot_id, "allow_rebind")

    def reset_pairing_code(self, slot_id: str) -> dict:
        for _ in range(8):
            code = self._code()
            try:
                self._manage(slot_id, "reset", code_hash=self._digest(code))
                return _CodeResult(slot_id=slot_id, code=code)
            except AuthError as error:
                if error.code != "pairing_code_collision":
                    raise
        raise AuthError("pairing_unavailable", status=503, retryable=True)

    def remove_device(self, slot_id: str) -> None:
        removed = self._manage(slot_id, "remove")
        # 持久撤销先完成；不在提交后再读数据库而产生误报失败。
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items() if value.device_id != removed}

    def rename_device(self, slot_id: str, name: str) -> None:
        if not _valid_identifier(name) or not name.strip():
            raise AuthError("invalid_device_name", status=422)
        self._manage(slot_id, "rename", name=name)

    def issue_pairing_code(self, *, now_ms: Optional[int] = None, ttl_ms: Optional[int] = None) -> str:
        """Python 夹具兼容入口：创建具名固定名额；HTTP 不提供此操作。"""
        return self.add_device("阅读器")["code"]

    create_pairing_code = issue_pairing_code

    def _validate_transaction(self, device_id, pairing_id, token):
        if not _valid_identifier(device_id) or not isinstance(pairing_id, str):
            raise AuthError("invalid_pair_request", status=422)
        try:
            valid_uuid = str(uuid.UUID(pairing_id)) == pairing_id
        except (ValueError, AttributeError):
            valid_uuid = False
        if not valid_uuid or not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise AuthError("invalid_pair_request", status=422)

    def _pairing_response(self, device_id, pairing_id, status, *, capabilities=False):
        result = {"protocol": "x4-2", "profile_id": self.store.profile_id, "device_id": device_id, "pairing_id": pairing_id, "status": status}
        if capabilities:
            result["capabilities"] = {"device_management_v1": True}
        return result

    def prepare_pairing(self, code: str, device_id: str, pairing_id: str, token: str, *, name=None, now_ms=None) -> dict:
        self._validate_transaction(device_id, pairing_id, token)
        if name is not None and not _valid_identifier(name):
            raise AuthError("invalid_pair_request", status=422)
        now = self._now(now_ms)
        with self._lock:
            if self._rate_limited(now):
                raise AuthError("pairing_rate_limited", status=429, retryable=True)
            if not isinstance(code, str) or re.fullmatch(r"[0-9]{12}", code) is None:
                if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code):
                    self._record_failed_attempt(now)
                    raise AuthError("pairing_code_upgrade_required", status=401)
                raise self._pairing_failure(now)
            try:
                status = self.store.prepare_device_pairing(self._digest(code), device_id, pairing_id, self._digest(token), now)
            except PairingStateError as error:
                if error.code == "invalid_pairing_code":
                    self._record_failed_attempt(now)
                raise self._management_error(error) from error
            return self._pairing_response(device_id, pairing_id, status, capabilities=True)

    def confirm_pairing(self, device_id: str, pairing_id: str, token: str, *, now_ms=None) -> dict:
        self._validate_transaction(device_id, pairing_id, token)
        try:
            removed = self.store.confirm_device_pairing(device_id, pairing_id, self._digest(token), self._now(now_ms))
        except PairingStateError as error:
            raise self._management_error(error) from error
        if removed is not None:
            with self._lock:
                self._sessions = {key: value for key, value in self._sessions.items() if value.device_id != removed}
        return self._pairing_response(device_id, pairing_id, "active")

    def pair(self, code: str, device_id: str, *, now_ms: Optional[int] = None) -> DeviceCredential:
        """仅 Python 测试便捷入口；完整经过 prepare 与 confirm。"""
        token = self._token_bytes(32).hex()
        pairing_id = str(uuid.uuid4())
        self.prepare_pairing(code, device_id, pairing_id, token, now_ms=now_ms)
        self.confirm_pairing(device_id, pairing_id, token, now_ms=now_ms)
        return DeviceCredential(self.store.profile_id, device_id, token)

    pair_device = pair

    def authenticate(self, device_id: str, token: str) -> AuthPrincipal:
        """用 body/header 提供的 device_id 验证 Bearer token 的主体。"""

        if not _valid_identifier(device_id) or not isinstance(token, str) or not token or any(
            char.isspace() for char in token
        ):
            raise AuthError("invalid_token")
        valid = self.store.verify_device_token(device_id, token)
        if not valid:
            raise AuthError("invalid_token")
        return AuthPrincipal(self.store.profile_id, device_id)

    verify = authenticate

    @staticmethod
    def _header(headers: Mapping[str, Any], name: str) -> Optional[str]:
        lowered = name.lower()
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == lowered:
                return value if isinstance(value, str) else None
        return None

    def authenticate_headers(self, headers: Mapping[str, Any], device_id: str) -> AuthPrincipal:
        """严格解析 Authorization: Bearer <opaque-token>，不把 token 放进错误。"""

        value = self._header(headers, "Authorization")
        if value is None:
            raise AuthError("bearer_token_required")
        parts = value.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or any(
            char.isspace() for char in parts[1]
        ):
            raise AuthError("bearer_token_required")
        return self.authenticate(device_id, parts[1])

    verify_bearer = authenticate_headers

    def open_session(
        self, principal: AuthPrincipal, session_id: str, *, now_ms: Optional[int] = None
    ) -> RuntimeSession:
        if not isinstance(principal, AuthPrincipal) or principal.profile_id != self.store.profile_id:
            raise AuthError("profile_mismatch", status=409)
        if not _valid_identifier(session_id):
            raise AuthError("invalid_session", status=422)
        try:
            record: SessionRecord = self.store.open_session(
                principal.device_id, session_id, now_ms=now_ms
            )
        except StorageValidationError as error:
            raise AuthError("session_unavailable", status=409) from error
        runtime = RuntimeSession(
            record.profile_id,
            record.device_id,
            record.session_id,
            record.high_water,
            record.created_at_ms,
        )
        with self._lock:
            self._sessions[(runtime.profile_id, runtime.device_id, runtime.session_id)] = runtime
        return runtime

    def new_session_id(self) -> str:
        """生成不含长期 token 的新 session 身份。"""

        return "s_" + self._token_bytes(18).hex()

    def runtime_sessions(self) -> Tuple[RuntimeSession, ...]:
        with self._lock:
            return tuple(self._sessions.values())


__all__ = [
    "AuthError",
    "AuthPrincipal",
    "DeviceAuthenticator",
    "DeviceCredential",
    "RuntimeSession",
]
