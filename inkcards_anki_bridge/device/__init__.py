"""X4-2 LAN device transport 的标准库实现。

这里的边界是可信局域网内的明文 HTTP：认证、请求有界性和 durable receipt
由本包负责；Anki collection、历史 application、snapshot pull 和 Lua 不在本阶段。
"""

from .auth import (
    AuthError,
    AuthPrincipal,
    DeviceAuthenticator,
    DeviceCredential,
    RuntimeSession,
)
from .codec import CodecError, decode_object, encode_json, error_envelope
from .routes import DeviceRouter, RouteLimits, RouteResponse
from .server import DeviceHTTPServer, DeviceServer, ServerLimits

__all__ = [
    "AuthError",
    "AuthPrincipal",
    "CodecError",
    "DeviceAuthenticator",
    "DeviceCredential",
    "DeviceHTTPServer",
    "DeviceRouter",
    "DeviceServer",
    "RouteLimits",
    "RouteResponse",
    "RuntimeSession",
    "ServerLimits",
    "decode_object",
    "encode_json",
    "error_envelope",
]
