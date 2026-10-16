"""仅绑定 127.0.0.1 的标准库 HTTP 服务器。"""

from __future__ import annotations

import hmac
import http.server
import socket
import socketserver
from urllib.parse import urlsplit

from .protocol import (
    ENDPOINTS,
    LOOPBACK_HOST,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    REQUEST_SOCKET_TIMEOUT_SECONDS,
    ProtocolError,
    error_response,
    json_bytes,
    parse_request,
    success_response,
)
from .service import BridgeService


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class BridgeHTTPServer(_ThreadingHTTPServer):
    """服务对象只持有 dispatcher 和 exporter，不持有 collection。"""

    def __init__(self, token: str, service: BridgeService) -> None:
        self.token = token
        self.service = service
        super().__init__((LOOPBACK_HOST, 0), BridgeRequestHandler)


class BridgeRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # 不把 Authorization、token、profile 或请求体写入默认日志。
        del format, args

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._write_error(405, None, ProtocolError("method_not_allowed", "POST is required", status=405))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment or parsed.path not in ENDPOINTS:
            self._write_error(404, None, ProtocolError("unknown_endpoint", "endpoint is not supported", status=404))
            return

        if not self._authorized():
            self._write_error(401, None, ProtocolError("unauthorized", "invalid bearer token", status=401))
            return

        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._write_error(
                415,
                None,
                ProtocolError("invalid_content_type", "Content-Type must be application/json", status=415),
            )
            return

        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._write_error(411, None, ProtocolError("length_required", "Content-Length is required", status=411))
            return
        if length > MAX_REQUEST_BYTES:
            self._write_error(
                413,
                None,
                ProtocolError("request_too_large", "request body exceeds the configured byte limit", status=413),
            )
            return

        self.connection.settimeout(REQUEST_SOCKET_TIMEOUT_SECONDS)
        try:
            raw = self.rfile.read(length)
        except socket.timeout:
            self._write_error(
                408,
                None,
                ProtocolError("request_timeout", "request body read timed out", status=408, retryable=True),
            )
            return
        if len(raw) != length:
            self._write_error(400, None, ProtocolError("malformed_json", "request body is truncated"))
            return

        request_id: str | None = None
        try:
            request = parse_request(raw, ENDPOINTS[parsed.path])
            request_id = request.request_id
            result = self.server.service.handle(request.method, request.request_id, request.params)
            payload = success_response(request.request_id, result)
            status = 200
        except ProtocolError as error:
            payload = error_response(request_id, error)
            status = error.status
        except Exception:
            payload = error_response(
                request_id,
                ProtocolError("internal_error", "bridge request failed", status=500),
            )
            status = 500
        self._write_payload(status, payload)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        supplied = header[len(prefix) :]
        return hmac.compare_digest(supplied.encode("utf-8"), self.server.token.encode("utf-8"))

    def _write_error(self, status: int, request_id: str | None, error: ProtocolError) -> None:
        self._write_payload(status, error_response(request_id, error))

    def _write_payload(self, status: int, payload: dict[str, object]) -> None:
        try:
            body = json_bytes(payload)
        except ProtocolError:
            body = b'{"protocol_version":"' + PROTOCOL_VERSION.encode("ascii") + b'","ok":false}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
