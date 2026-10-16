"""关闭与资源上限的回归；只使用临时库和隔离 HTTP 服务。"""

from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from inkcards_anki_bridge.device.server import DeviceServer, ServerLimits, _BoundedHTTPServer
from inkcards_anki_bridge.storage import ProfileStore, StorageError


class ShutdownBoundariesTests(unittest.TestCase):
    def test_shutdown_racing_connection_admission_never_strands_a_socket(self):
        with patch("socketserver.TCPServer.server_bind"), patch("socketserver.TCPServer.server_activate"):
            server = _BoundedHTTPServer(
                ("127.0.0.1", 0), Mock(), Mock(),
                ServerLimits(max_workers=1, work_queue_size=2, max_connections=2),
            )
        entered, release, shutdown_done = threading.Event(), threading.Event(), threading.Event()
        slots = server._slots

        class DelayedSlots:
            def acquire(self, blocking):
                entered.set()
                release.wait(2)
                return slots.acquire(blocking)

            def release(self):
                slots.release()

        server._slots = DelayedSlots()
        request = Mock()
        closed = []
        server.finish_request = lambda *_args: None
        server.shutdown_request = closed.append
        server.start_workers()
        admission = threading.Thread(target=lambda: server.process_request(request, ("127.0.0.1", 1)))

        def shutdown():
            server.begin_shutdown()
            shutdown_done.set()

        stopping = threading.Thread(target=shutdown)
        try:
            admission.start()
            self.assertTrue(entered.wait(1))
            stopping.start()
            if shutdown_done.wait(0.1):
                server.join_workers(0.5)
            release.set()
            admission.join(1)
            stopping.join(1)
            server.join_workers(1)
            self.assertFalse(admission.is_alive() or stopping.is_alive())
            self.assertFalse(any(worker.is_alive() for worker in server._workers))
            self.assertEqual(closed, [request])
            self.assertEqual(server._connections, set())
        finally:
            release.set()
            admission.join(1)
            if stopping.ident is not None:
                stopping.join(1)
            server.begin_shutdown()
            server.join_workers(1)
            server.server_close()

    def test_listener_start_failure_does_not_shutdown_a_never_started_loop(self):
        server = DeviceServer.__new__(DeviceServer)
        server._httpd = Mock()
        server._thread = None
        server._state_lock = threading.RLock()
        server._stopped = False
        server.limits = ServerLimits()
        with patch("threading.Thread.start", side_effect=RuntimeError("no thread resources")):
            with self.assertRaisesRegex(RuntimeError, "no thread resources"):
                server.start()
        server.stop()
        # BaseServer.shutdown 在 serve_forever 未启动时永远等待，不能调用。
        server._httpd.shutdown.assert_not_called()
        server._httpd.server_close.assert_called_once()
        server._httpd.join_workers.assert_called_once()

    def test_partial_worker_start_failure_can_be_cleanly_joined(self):
        with patch("socketserver.TCPServer.server_bind"), patch("socketserver.TCPServer.server_activate"):
            server = _BoundedHTTPServer(("127.0.0.1", 0), Mock(), Mock(), ServerLimits())
        original_start = threading.Thread.start

        def start(worker):
            if worker.name.endswith("-1"):
                raise RuntimeError("worker limit")
            return original_start(worker)

        try:
            with patch("threading.Thread.start", start):
                with self.assertRaisesRegex(RuntimeError, "worker limit"):
                    server.start_workers()
            server.begin_shutdown()
            server.join_workers(1)
            self.assertFalse(any(worker.is_alive() for worker in server._workers))
        finally:
            server.server_close()

    def test_all_workers_exit_when_shutdown_queue_is_smaller_than_worker_count(self):
        limits = ServerLimits(max_workers=4, work_queue_size=1, max_connections=4)
        with patch("socketserver.TCPServer.server_bind"), patch("socketserver.TCPServer.server_activate"):
            server = _BoundedHTTPServer(("127.0.0.1", 0), Mock(), Mock(), limits)
        entered = queue.Queue()
        release = threading.Event()

        def handle(request, _address):
            entered.put(request)
            release.wait(2)

        server.finish_request = handle
        server.shutdown_request = lambda _request: None
        server.start_workers()
        try:
            for index in range(limits.max_workers):
                server.process_request(Mock(), ("127.0.0.1", index))
                entered.get(timeout=1)
            server.begin_shutdown()
            release.set()
            server.join_workers(0.5)
            self.assertFalse(any(worker.is_alive() for worker in server._workers))
        finally:
            release.set()
            # RED 时也回收旧实现留下的 worker，不污染其余测试。
            for worker in server._workers:
                if worker.is_alive():
                    server._queue.put(None, timeout=1)
            server.join_workers(1)
            server.server_close()

    def test_closed_store_rejects_new_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ProfileStore(Path(temporary), profile_id="closed-profile")
            store.close()
            for operation in (
                store.get_collection_generation,
                lambda: store.register_device("late-device", "late-token"),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(StorageError) as raised:
                        operation()
                    self.assertEqual(raised.exception.code, "storage_closed")
            reopened = ProfileStore(Path(temporary), profile_id="closed-profile")
            try:
                self.assertFalse(reopened.verify_device_token("late-device", "late-token"))
            finally:
                reopened.close()
