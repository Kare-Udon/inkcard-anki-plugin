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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Tuple

from ..storage import ProfileStore, SessionRecord


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


@dataclass
class _PairingRecord:
    code_hash: str
    expires_at_ms: int
    used_at_ms: Optional[int] = None


def _default_clock() -> int:
    return int(time.time() * 1000)


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.match(value))


class DeviceAuthenticator:
    """管理短时一次性 pairing code、长期 token hash 验证和 session。"""

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
        self._pairing_ttl_ms = pairing_ttl_ms
        self._max_pairing_attempts = max_pairing_attempts
        self._pairing_attempt_window_ms = pairing_attempt_window_ms
        self._lock = threading.RLock()
        self._pairings: Dict[str, _PairingRecord] = {}
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
        expired = [
            key
            for key, record in self._pairings.items()
            if record.used_at_ms is not None or record.expires_at_ms <= now_ms
        ]
        for key in expired:
            del self._pairings[key]

    def _record_failed_attempt(self, now_ms: int) -> None:
        self._attempts.append(now_ms)

    def _rate_limited(self, now_ms: int) -> bool:
        self._sweep(now_ms)
        return len(self._attempts) >= self._max_pairing_attempts

    def _pairing_failure(self, now_ms: int) -> AuthError:
        if self._rate_limited(now_ms):
            return AuthError("pairing_rate_limited", status=429, retryable=True)
        self._record_failed_attempt(now_ms)
        return AuthError("invalid_or_expired_pairing_code")

    def _randbelow(self, upper: int) -> int:
        generator = getattr(self._entropy, "randbelow", None)
        if callable(generator):
            value = generator(upper)
        else:
            raw = self._token_bytes(8)
            value = int.from_bytes(raw, "big") % upper
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < upper:
            raise ValueError("entropy returned an invalid integer")
        return value

    def _token_bytes(self, length: int) -> bytes:
        generator = getattr(self._entropy, "token_bytes", None)
        if not callable(generator):
            raise ValueError("entropy source must provide token_bytes")
        raw = generator(length)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != length:
            raise ValueError("entropy returned an invalid token")
        return bytes(raw)

    def issue_pairing_code(self, *, now_ms: Optional[int] = None, ttl_ms: Optional[int] = None) -> str:
        """生成一次性六位码；原文只由调用方拿到，不写入 storage。"""

        now = self._now(now_ms)
        ttl = self._pairing_ttl_ms if ttl_ms is None else ttl_ms
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("invalid pairing ttl")
        with self._lock:
            self._sweep(now)
            for _ in range(8):
                code = "%06d" % self._randbelow(1_000_000)
                digest = hashlib.sha256(code.encode("ascii")).hexdigest()
                if digest not in self._pairings:
                    self._pairings[digest] = _PairingRecord(digest, now + ttl)
                    return code
        raise AuthError("pairing_unavailable", status=503, retryable=True)

    create_pairing_code = issue_pairing_code

    def pair(self, code: str, device_id: str, *, now_ms: Optional[int] = None) -> DeviceCredential:
        """校验 code 后注册设备；只有 register_device 成功才消耗 code。"""

        now = self._now(now_ms)
        if not _valid_identifier(device_id):
            raise AuthError("invalid_pair_request", status=422)
        with self._lock:
            if self._rate_limited(now):
                raise AuthError("pairing_rate_limited", status=429, retryable=True)
            if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
                raise self._pairing_failure(now)
            digest = hashlib.sha256(code.encode("ascii")).hexdigest()
            record = self._pairings.get(digest)
            if record is None or record.used_at_ms is not None or record.expires_at_ms <= now:
                raise self._pairing_failure(now)
            token = self._token_bytes(32).hex()
            try:
                self.store.register_device(device_id, token, now_ms=now)
            except Exception:
                # 注册失败时不标记 code 已使用，调用方得到的错误不包含 token 或设备输入。
                raise AuthError("pairing_failed", status=503, retryable=True) from None
            record.used_at_ms = now
            return DeviceCredential(self.store.profile_id, device_id, token)

    pair_device = pair

    def authenticate(self, device_id: str, token: str) -> AuthPrincipal:
        """用 body/header 提供的 device_id 验证 Bearer token 的主体。"""

        if not _valid_identifier(device_id) or not isinstance(token, str) or not token or any(
            char.isspace() for char in token
        ):
            raise AuthError("invalid_token")
        try:
            valid = self.store.verify_device_token(device_id, token)
        except Exception:
            valid = False
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
        except Exception:
            raise AuthError("session_unavailable", status=409) from None
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
