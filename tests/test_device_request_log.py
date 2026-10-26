from __future__ import annotations

import concurrent.futures
import http.client
import json
import socket
import tempfile
import time
import unittest
import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter, RouteLimits
from inkcards_anki_bridge.device.request_log import DeviceRequestLog
from inkcards_anki_bridge.device.server import DeviceServer, ServerLimits
from inkcards_anki_bridge.logging_control import AddonLogger
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.storage import ProfileStore


class DeviceRequestLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="inkcards-http-log-")
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: int(time.time() * 1000))
        self.router = DeviceRouter(
            self.auth,
            InboxService(self.store),
            limits=RouteLimits(max_request_bytes=1024, max_response_bytes=4096),
        )
        self.logging_core = AddonLogger(
            self.store.root, policy={"level": "info", "console": False}
        )
        self.server = DeviceServer(
            self.router,
            host="127.0.0.1",
            port=0,
            limits=ServerLimits(
                max_workers=4,
                work_queue_size=8,
                max_connections=12,
                read_timeout=0.2,
                body_timeout=0.2,
                write_timeout=0.25,
                shutdown_timeout=1.0,
            ),
            request_log=DeviceRequestLog(self.store.root, logger=self.logging_core),
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.store.close()
        self.tempdir.cleanup()

    @property
    def log_path(self) -> Path:
        return self.store.root / "diagnostics" / "plugin.jsonl"

    def request(
        self,
        method: str,
        path: str,
        payload: object,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict, bytes, bytes]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        connection.request(
            method,
            path,
            raw,
            {"Content-Type": "application/json", **(headers or {})},
        )
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            payload = {}
        return status, payload, raw, body

    def events(self) -> list[dict]:
        if not self.log_path.is_file():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def wait_for_events(self, count: int) -> list[dict]:
        deadline = time.monotonic() + 1.0
        rows = self.events()
        while len(rows) < count and time.monotonic() < deadline:
            time.sleep(0.01)
            rows = self.events()
        return rows

    def test_info_level_server_logs_only_fixed_request_metadata(self) -> None:
        code = self.auth.issue_pairing_code()
        token = secrets.token_hex(32)
        pairing_id = str(uuid.uuid4())
        status, paired, pair_request, pair_response = self.request(
            "POST",
            "/v2/pair",
            {"code": code, "device_id": "DEVICE-ID-SECRET", "pairing_id": pairing_id, "token": token},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("token", paired)
        self.auth.confirm_pairing("DEVICE-ID-SECRET", pairing_id, token)

        status, _, handshake_request, handshake_response = self.request(
            "POST",
            "/v2/handshake",
            {
                "protocol": "x4-2",
                "device_id": "DEVICE-ID-SECRET",
                "session_id": "SESSION-ID-SECRET",
            },
            {"Authorization": "Bearer " + token},
        )
        self.assertEqual(status, 200)

        status, denied, denied_request, denied_response = self.request(
            "POST",
            "/v2/handshake",
            {
                "protocol": "x4-2",
                "device_id": "DEVICE-ID-SECRET",
                "session_id": "SESSION-ID-SECRET",
            },
            {"Authorization": "Bearer AUTHORIZATION-SECRET"},
        )
        self.assertEqual(status, 401)

        status, missing, query_request, query_response = self.request(
            "POST",
            "/v2/pair?token=QUERY-SECRET",
            {
                "code": "BODY-SECRET",
                "profile_id": "PROFILE-ID-SECRET",
                "card": "CARD-CONTENT-SECRET",
            },
        )
        self.assertEqual(status, 404)

        status, unsupported, method_request, method_response = self.request(
            "X-PRIVATE-METHOD", "/v2/pair", {}
        )
        self.assertEqual(status, 501)
        self.assertEqual(unsupported, {})
        self.assertGreater(len(method_request), 0)

        rows = self.wait_for_events(5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["method"], "POST")
        self.assertEqual(rows[0]["route"], "/v2/pair")
        self.assertEqual(rows[0]["status"], 200)
        self.assertIsNone(rows[0]["error_code"])
        self.assertEqual(rows[0]["request_body_bytes"], len(pair_request))
        self.assertEqual(rows[0]["response_body_bytes"], len(pair_response))
        self.assertEqual(rows[2]["status"], 401)
        self.assertEqual(rows[2]["error_code"], denied["error"]["code"])
        self.assertEqual(rows[2]["request_body_bytes"], len(denied_request))
        self.assertEqual(rows[2]["response_body_bytes"], len(denied_response))
        self.assertEqual(rows[3]["route"], "unknown")
        self.assertEqual(rows[3]["error_code"], missing["error"]["code"])
        self.assertEqual(rows[3]["request_body_bytes"], len(query_request))
        self.assertEqual(rows[3]["response_body_bytes"], len(query_response))
        self.assertEqual(rows[4]["method"], "unknown")
        self.assertEqual(rows[4]["route"], "/v2/pair")
        self.assertEqual(rows[4]["status"], status)
        self.assertEqual(rows[4]["error_code"], "unsupported_method")
        self.assertEqual(rows[4]["response_body_bytes"], len(method_response))
        self.assertTrue(all(row["elapsed_ms"] >= 0 for row in rows))
        raw_log = self.log_path.read_text()
        for secret in (
            code,
            token,
            "Authorization",
            "profile-a",
            "PROFILE-ID-SECRET",
            "DEVICE-ID-SECRET",
            "SESSION-ID-SECRET",
            "AUTHORIZATION-SECRET",
            "QUERY-SECRET",
            "BODY-SECRET",
            "CARD-CONTENT-SECRET",
            "X-PRIVATE-METHOD",
        ):
            self.assertNotIn(secret, raw_log)
        self.assertNotIn("/v2/pair?", raw_log)

    def test_old_server_constructor_remains_compatible_with_default_warn(self) -> None:
        server = DeviceServer(self.router, host="127.0.0.1", port=0)
        try:
            self.assertEqual(server.request_log.logger.policy, {"level": "warn", "console": False})
            server.start()
            self.assertTrue(server.port > 0)
        finally:
            server.stop()

    def test_off_server_path_skips_diagnostic_clock_encoding_and_directory(self) -> None:
        core = AddonLogger(
            self.store.root, policy={"level": "off", "console": False}
        )
        server = DeviceServer(
            self.router,
            host="127.0.0.1",
            port=0,
            request_log=DeviceRequestLog(self.store.root, logger=core),
        )
        server.start()
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        sock.settimeout(2)
        clock = Mock(side_effect=AssertionError("diagnostic clock used"))
        try:
            with (
                patch("inkcards_anki_bridge.device.server.time", SimpleNamespace(monotonic=clock)),
                patch("inkcards_anki_bridge.device.request_log.json.loads", side_effect=AssertionError("decode")) as decode,
                patch("inkcards_anki_bridge.logging_control._SafeJsonFormatter.format", side_effect=AssertionError("format")) as formatter,
            ):
                sock.sendall(b"GET /v2/pair HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                response = sock.recv(1024)
            self.assertTrue(response.startswith(b"HTTP/1.1 4"), response[:40])
            clock.assert_not_called()
            decode.assert_not_called()
            formatter.assert_not_called()
            self.assertFalse((self.store.root / "diagnostics").exists())
        finally:
            sock.close()
            server.stop()

    def test_preflight_rejection_records_status_and_business_code(self) -> None:
        status, body, request_body, response_body = self.request(
            "POST", "/v2/pair", {"padding": "x" * 1100}
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "request_too_large")
        rows = self.wait_for_events(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "/v2/pair")
        self.assertEqual(rows[0]["status"], status)
        self.assertEqual(rows[0]["error_code"], body["error"]["code"])
        self.assertEqual(rows[0]["request_body_bytes"], 0)
        self.assertEqual(rows[0]["response_body_bytes"], len(response_body))
        self.assertGreater(len(request_body), rows[0]["request_body_bytes"])

    def test_connection_admission_rejection_uses_unknown_route(self) -> None:
        self.server.stop()
        self.server = DeviceServer(
            self.router,
            host="127.0.0.1",
            port=0,
            limits=ServerLimits(
                max_workers=1,
                work_queue_size=1,
                max_connections=2,
                read_timeout=0.5,
                write_timeout=0.25,
                shutdown_timeout=1.0,
            ),
        )
        self.server.start()
        sockets: list[socket.socket] = []
        try:
            for _ in range(2):
                pending = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
                pending.sendall(
                    b"POST /v2/pair HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Length: 100\r\n\r\n"
                    b"{\"code\":"
                )
                sockets.append(pending)
            rejected = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
            rejected.sendall(
                b"POST /v2/pair?token=ADMISSION-SECRET HTTP/1.1\r\n"
                b"Host: localhost\r\nContent-Length: 0\r\n\r\n"
            )
            response = rejected.recv(512)
            self.assertTrue(response.startswith(b"HTTP/1.1 503"))
            rejected.close()
        finally:
            for pending in sockets:
                pending.close()

        rows = self.wait_for_events(1)
        self.assertTrue(any(row["error_code"] == "server_busy" for row in rows))
        rejection = next(row for row in rows if row["error_code"] == "server_busy")
        self.assertEqual(rejection["status"], 503)
        self.assertEqual(rejection["method"], "unknown")
        self.assertEqual(rejection["route"], "unknown")
        self.assertNotIn("ADMISSION-SECRET", self.log_path.read_text())

    def test_body_read_timeout_records_no_exception_or_body_text(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
        body = b'{"secret":"REQUEST-BODY-SECRET'
        sock.sendall(
            b"POST /v2/pair HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 200\r\n\r\n"
            + body
        )
        time.sleep(0.35)
        sock.settimeout(0.5)
        try:
            sock.recv(512)
        except OSError:
            pass
        finally:
            sock.close()

        rows = self.wait_for_events(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "/v2/pair")
        self.assertEqual(rows[0]["status"], 408)
        self.assertEqual(rows[0]["error_code"], "request_body_timeout")
        self.assertEqual(rows[0]["request_body_bytes"], len(body))
        self.assertEqual(rows[0]["expected_body_bytes"], 200)
        self.assertEqual(rows[0]["stage"], "request_body")
        self.assertGreater(rows[0]["response_body_bytes"], 0)
        raw_log = self.log_path.read_text()
        self.assertNotIn("REQUEST-BODY-SECRET", raw_log)
        self.assertNotIn("timed out", raw_log)

    def test_concurrent_http_requests_all_append_records(self) -> None:
        def send(index: int) -> int:
            status, _, _, _ = self.request(
                "POST",
                "/v2/handshake",
                {
                    "protocol": "x4-2",
                    "device_id": "DEVICE-ID-SECRET-%d" % index,
                    "session_id": "SESSION-ID-SECRET-%d" % index,
                },
                {"Authorization": "Bearer TOKEN-SECRET-%d" % index},
            )
            return status

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(send, range(8)))
        self.assertEqual(statuses, [401] * 8)
        rows = self.wait_for_events(8)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["route"] == "/v2/handshake" for row in rows))
        self.assertTrue(all(row["error_code"] for row in rows))
        raw_log = self.log_path.read_text()
        self.assertNotIn("TOKEN-SECRET", raw_log)
        self.assertNotIn("DEVICE-ID-SECRET", raw_log)
        self.assertNotIn("SESSION-ID-SECRET", raw_log)

    def test_rotation_keeps_one_bounded_backup(self) -> None:
        from inkcards_anki_bridge.device.request_log import DeviceRequestLog

        with tempfile.TemporaryDirectory(prefix="inkcards-http-rotation-") as root:
            core = AddonLogger(
                Path(root), policy={"level": "info", "console": False}, max_bytes=420
            )
            log = DeviceRequestLog(Path(root), max_bytes=420, logger=core)
            for index in range(20):
                log.record(
                    method="POST",
                    target="/v2/pair",
                    status=200,
                    error_code=None,
                    elapsed_ms=index,
                    request_body_bytes=100,
                    response_body_bytes=200,
                )
            current = Path(root) / "diagnostics" / "plugin.jsonl"
            backup = current.with_name("plugin.jsonl.1")
            self.assertTrue(current.is_file())
            self.assertTrue(backup.is_file())
            self.assertLessEqual(current.stat().st_size, 420)
            self.assertLessEqual(backup.stat().st_size, 420)
            self.assertEqual(
                {path.name for path in current.parent.iterdir()},
                {"plugin.jsonl", "plugin.jsonl.1"},
            )
            lines = current.read_text().splitlines() + backup.read_text().splitlines()
            self.assertGreaterEqual(len(lines), 2)
            for line in lines:
                self.assertEqual(json.loads(line)["route"], "/v2/pair")

    def test_log_write_failure_does_not_change_http_response(self) -> None:
        diagnostics_blocker = self.store.root / "diagnostics"
        diagnostics_blocker.write_text("not a directory")
        code = self.auth.issue_pairing_code()
        status, body, _, _ = self.request(
            "POST", "/v2/pair", {"code": code, "device_id": "device-a", "pairing_id": str(uuid.uuid4()), "token": secrets.token_hex(32)}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")
        self.assertNotIn("token", body)
        self.assertEqual(diagnostics_blocker.read_text(), "not a directory")


if __name__ == "__main__":
    unittest.main()
