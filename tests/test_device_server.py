from __future__ import annotations

import http.client
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter, RouteLimits
from inkcards_anki_bridge.device.server import DeviceServer, ServerLimits
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.storage import ProfileStore


class DeviceServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: int(time.time() * 1000))
        self.router = DeviceRouter(
            self.auth,
            InboxService(self.store),
            limits=RouteLimits(max_request_bytes=1024, max_response_bytes=4096),
        )
        self.server = DeviceServer(
            self.router,
            host="127.0.0.1",
            port=0,
            limits=ServerLimits(
                max_workers=1,
                work_queue_size=1,
                max_connections=2,
                read_timeout=0.25,
                write_timeout=0.25,
                shutdown_timeout=1.0,
            ),
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.store.close()
        self.tempdir.cleanup()

    def request(self, method: str, path: str, body: object, headers: dict | None = None):
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        connection.request(method, path, raw, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data.decode("utf-8"))

    def test_real_loopback_pair_handshake_and_deferred_pull(self) -> None:
        code = self.auth.issue_pairing_code()
        status, body = self.request("POST", "/v2/pair", {"code": code, "device_id": "device-a"})
        self.assertEqual(status, 200)
        token = body["token"]
        status, body = self.request(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"},
            {"Authorization": "Bearer " + token},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], "x4-2")
        status, body = self.request(
            "POST", "/v2/pull/open", {}, {"Authorization": "Bearer " + token}
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "capability_unavailable")

    def test_body_limit_and_unknown_route_are_bounded(self) -> None:
        status, body = self.request("POST", "/v2/unknown", {"large": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        raw = b"{" + b"x" * 1100 + b"}"
        connection.request(
            "POST",
            "/v2/pair",
            raw,
            {"Content-Type": "application/json", "Content-Length": str(len(raw))},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(body["error"]["code"], "request_too_large")
        connection.close()

    def test_slow_body_times_out_and_stop_is_idempotent(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
        sock.sendall(
            b"POST /v2/pair HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 20\r\n\r\n"
            b"{\"code\":"
        )
        started = time.monotonic()
        self.server.stop()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.server.stop()
        sock.close()

    def test_slow_head_is_closed_by_read_timeout(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
        sock.sendall(b"POST /v2/pair HTTP/1.1\r\nHost: localhost\r\n")
        sock.settimeout(1.5)
        started = time.monotonic()
        received = sock.recv(512)
        elapsed = time.monotonic() - started
        self.assertEqual(received, b"")
        self.assertLess(elapsed, 1.25)
        sock.close()

    def test_concurrent_connection_cap_rejects_excess_work(self) -> None:
        first = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
        second = None
        third = None
        try:
            first.sendall(
                b"POST /v2/pair HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\n"
                b"{\"code\":"
            )
            second = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
            second.sendall(
                b"POST /v2/pair HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\n"
                b"{\"code\":"
            )
            third = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
            third.sendall(b"GET /v2/sync/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            third.settimeout(2)
            rejected = third.recv(512)
            self.assertTrue(rejected.startswith(b"HTTP/1.1"))
            self.assertIn(b"503", rejected)
        finally:
            first.close()
            if second is not None:
                second.close()
            if third is not None:
                third.close()

    def test_port_occupation_is_reported(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        occupied_port = listener.getsockname()[1]
        try:
            with self.assertRaises(OSError):
                DeviceServer(self.router, host="127.0.0.1", port=occupied_port)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
