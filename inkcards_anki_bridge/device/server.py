"""有界标准库 HTTP server。

服务只接收已注册的 x4-2 route。固定 worker 数和有界 work queue 限制并发；每个
连接的读写 timeout、请求/响应字节上限和关闭 join timeout 都是硬边界。该服务是
可信 LAN 明文 HTTP，不提供 TLS、互联网认证代理、任意文件访问或 API 转发。
"""

from __future__ import annotations

import http.server
import queue
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .codec import encode_json, error_envelope
from .routes import DeviceRouter, RouteResponse


@dataclass(frozen=True)
class ServerLimits:
    max_workers: int = 4
    work_queue_size: int = 8
    max_connections: int = 12
    read_timeout: float = 2.0
    write_timeout: float = 2.0
    shutdown_timeout: float = 2.0

    def __post_init__(self) -> None:
        for value in (self.max_workers, self.work_queue_size, self.max_connections):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("server counts must be positive integers")
        if self.max_connections < self.max_workers:
            raise ValueError("max_connections must cover workers")
        for value in (self.read_timeout, self.write_timeout, self.shutdown_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("server timeouts must be positive")


def _raw_rejection(request: socket.socket, status: int, code: str) -> None:
    body = b'{"error":{"code":"%s"}}' % code.encode("ascii")
    response = (
        b"HTTP/1.1 "
        + str(status).encode("ascii")
        + b" Service Unavailable\r\n"
        + b"Content-Type: application/json; charset=utf-8\r\n"
        + b"Content-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    try:
        request.settimeout(0.25)
        request.sendall(response)
    except (OSError, socket.timeout):
        pass


class _BoundedHTTPServer(socketserver.TCPServer):
    allow_reuse_address = False

    def __init__(
        self,
        server_address: Tuple[str, int],
        handler_class: Any,
        router: DeviceRouter,
        limits: ServerLimits,
    ) -> None:
        self.router = router
        self.limits = limits
        self.request_queue_size = max(1, limits.work_queue_size)
        self._queue: "queue.Queue[Optional[Tuple[socket.socket, Any]]]" = queue.Queue(
            maxsize=limits.work_queue_size
        )
        self._slots = threading.BoundedSemaphore(limits.max_connections)
        self._connections = set()
        self._connections_lock = threading.RLock()
        self._closing = False
        self._workers = []
        self._workers_started = False
        super().__init__(server_address, handler_class, bind_and_activate=True)

    def start_workers(self) -> None:
        if self._workers_started:
            return
        self._workers_started = True
        for index in range(self.limits.max_workers):
            worker = threading.Thread(
                target=self._worker,
                name="inkcards-x4-worker-%d" % index,
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        accepted = False
        with self._connections_lock:
            # 关闭标记、连接登记和入队必须原子化，避免请求落到终止标记之后。
            if not self._closing and self._slots.acquire(False):
                self._connections.add(request)
                try:
                    self._queue.put_nowait((request, client_address))
                    accepted = True
                except queue.Full:
                    self._connections.discard(request)
                    self._slots.release()
        if not accepted:
            _raw_rejection(request, 503, "server_busy")
            self.shutdown_request(request)

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                # 关闭时队列可能容不下每个 worker 的终止标记。
                # 也让正在处理请求的 worker 完成后能够退出。
                if self._closing:
                    return
                continue
            try:
                if item is None:
                    return
                request, address = item
                try:
                    self.finish_request(request, address)
                except (OSError, socket.timeout):
                    pass
                except Exception:
                    # 处理器内部只返回稳定错误；这里不把 exception 文本发给客户端。
                    pass
                finally:
                    try:
                        self.shutdown_request(request)
                    finally:
                        with self._connections_lock:
                            self._connections.discard(request)
                        self._slots.release()
            finally:
                self._queue.task_done()

    def begin_shutdown(self) -> None:
        with self._connections_lock:
            self._closing = True
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if item is not None:
                    request, _ = item
                    with self._connections_lock:
                        self._connections.discard(request)
                    self._slots.release()
                    self.shutdown_request(request)
            finally:
                self._queue.task_done()
        with self._connections_lock:
            active = list(self._connections)
        for request in active:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break

    def join_workers(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in self._workers:
            if worker is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)


class _RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "InkCardsX4/0.1"
    sys_version = ""

    @property
    def _x4_server(self) -> _BoundedHTTPServer:
        return self.server  # type: ignore[return-value]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._x4_server.limits.read_timeout)

    def log_message(self, format: str, *args: Any) -> None:
        # 不记录 Authorization、请求 body 或异常原文。
        return

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def do_OPTIONS(self) -> None:
        self._handle_request("OPTIONS")

    def _write(self, status: int, body: bytes, headers: Optional[Mapping[str, str]] = None) -> None:
        self.close_connection = True
        try:
            self.connection.settimeout(self._x4_server.limits.write_timeout)
            self.send_response(status)
            values = dict(headers or {})
            values["Content-Length"] = str(len(body))
            values.setdefault("Content-Type", "application/json; charset=utf-8")
            values["Connection"] = "close"
            for key, value in values.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (OSError, socket.timeout):
            pass

    def _error(self, status: int, code: str) -> None:
        try:
            body = encode_json(error_envelope(code))
        except Exception:
            body = b'{"error":{"code":"internal_error"}}'
        self._write(status, body)

    def _read_body(self, method: str) -> Optional[bytes]:
        if method != "POST":
            return b""
        transfer = self.headers.get("Transfer-Encoding")
        if transfer:
            self._error(400, "unsupported_transfer_encoding")
            return None
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self._error(411, "content_length_required")
            return None
        try:
            length = int(content_length)
        except (TypeError, ValueError):
            self._error(400, "invalid_content_length")
            return None
        if length < 0:
            self._error(400, "invalid_content_length")
            return None
        if length > self._x4_server.router.limits.max_request_bytes:
            self._error(413, "request_too_large")
            return None
        chunks = []
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(remaining, 8192))
            if not chunk:
                self._error(400, "request_body_incomplete")
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _handle_request(self, method: str) -> None:
        body = self._read_body(method)
        if body is None:
            return
        try:
            response: RouteResponse = self._x4_server.router.dispatch(
                method, self.path, dict(self.headers.items()), body
            )
        except (OSError, socket.timeout):
            self._error(408, "request_timeout")
            return
        self._write(response.status, response.body, response.headers)


class DeviceServer:
    """对外提供有界生命周期的 x4-2 HTTP server。"""

    def __init__(
        self,
        router: DeviceRouter,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        limits: Optional[ServerLimits] = None,
    ) -> None:
        if not isinstance(router, DeviceRouter):
            raise TypeError("router must be a DeviceRouter")
        self.router = router
        self.limits = limits or ServerLimits()
        self._httpd = _BoundedHTTPServer(
            (host, port), _RequestHandler, router, self.limits
        )
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.RLock()
        self._stopped = False

    @property
    def server_address(self) -> Tuple[str, int]:
        return self._httpd.server_address

    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        with self._state_lock:
            if self._stopped:
                raise RuntimeError("server is stopped")
            if self._thread is not None:
                return
            try:
                self._httpd.start_workers()
                thread = threading.Thread(
                    target=self._httpd.serve_forever,
                    kwargs={"poll_interval": 0.05},
                    name="inkcards-x4-listener",
                    daemon=True,
                )
                thread.start()
                self._thread = thread
            except BaseException:
                # 未启动的 serve_forever 不能调用 shutdown，否则会永久等待。
                self._stopped = True
                self._httpd.begin_shutdown()
                self._httpd.server_close()
                self._httpd.join_workers(self.limits.shutdown_timeout)
                raise

    def stop(self, timeout: Optional[float] = None) -> None:
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
            self._httpd.begin_shutdown()
        if thread is not None:
            self._httpd.shutdown()
            if thread is not threading.current_thread():
                thread.join(timeout if timeout is not None else self.limits.shutdown_timeout)
        self._httpd.server_close()
        self._httpd.join_workers(timeout if timeout is not None else self.limits.shutdown_timeout)

    close = stop


DeviceHTTPServer = DeviceServer


__all__ = ["DeviceHTTPServer", "DeviceServer", "ServerLimits"]
