from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.anki.compact_jobs import CompactJobManager
from inkcards_anki_bridge.anki.operations import (
    CollectionStartRejected,
    OperationContext,
)
from inkcards_anki_bridge.plugin import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    _attach_compact_jobs,
    register_in_anki,
    resolve_server_config,
    start_in_anki,
)


class FakeHook:
    def __init__(self) -> None:
        self.callbacks = []

    def append(self, callback) -> None:
        self.callbacks.append(callback)

    def remove(self, callback) -> None:
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class FakeGuiHooks:
    def __init__(self) -> None:
        self.profile_did_open = FakeHook()
        self.profile_will_close = FakeHook()
        self.addons_dialog_will_delete_addons = FakeHook()


class FakeAddonManager:
    def __init__(self, config=None) -> None:
        self.config = config or {}
        self.requested = []

    def addonFromModule(self, _module_name: str) -> str:
        return "inkcards_anki_bridge"

    def getConfig(self, addon_id: str):
        self.requested.append(addon_id)
        return dict(self.config)


def fake_aqt(profile_path: Path, *, version: str = "26.08.1", build: str = "39e4b0b4", config=None):
    manager = FakeAddonManager(config)
    pm = SimpleNamespace(profileFolder=lambda: str(profile_path))
    main_window = SimpleNamespace(pm=pm, addonManager=manager)
    return SimpleNamespace(appVersion=version, build=build, mw=main_window, gui_hooks=FakeGuiHooks())


class PluginRuntimeTests(unittest.TestCase):
    def test_default_service_port_is_separate_from_ankiconnect(self) -> None:
        self.assertEqual(DEFAULT_PORT, 8766)
        self.assertNotEqual(DEFAULT_PORT, 8765)

    def test_compact_attachment_uses_authorized_service_and_rechecks_reviewer_before_write(self) -> None:
        class FakeOpChanges:
            pass

        calls = []
        main_window = SimpleNamespace(state="deckBrowser")

        class FakeAdapter:
            def __init__(self) -> None:
                pass

        class FakeApplicationError(Exception):
            def __init__(self, code: str) -> None:
                self.code = code

        class FakeService:
            def __init__(self, _store, _adapter, *, diagnostics=None) -> None:
                self.diagnostics = diagnostics

            def authorize_next(self, collection, device_id, session_id, **kwargs):
                calls.append(("authorize", collection, device_id, session_id, kwargs))
                return "approved-authorization"

            def apply_pending(self, collection, device_id, session_id, *, authorization, **kwargs):
                calls.append(("apply", collection, device_id, session_id, authorization, kwargs))
                return SimpleNamespace(
                    status="applied",
                    changes=FakeOpChanges(),
                    generation_before=7,
                    generation_after=8,
                    refresh_context=True,
                    more_pending=False,
                )

            def recover_pending(self, *_args, **_kwargs):
                raise AssertionError("new event should use apply_pending")

        collection_pb2 = types.ModuleType("anki.collection_pb2")
        collection_pb2.OpChanges = FakeOpChanges
        anki_module = types.ModuleType("anki")
        anki_module.__path__ = []
        adapter_module = types.ModuleType("inkcards_anki_bridge.anki.compact_260903")
        adapter_module.AnkiCompactAdapter = FakeAdapter
        service_module = types.ModuleType("inkcards_anki_bridge.services.compact_application")
        service_module.CompactApplicationError = FakeApplicationError
        service_module.CompactApplicationService = FakeService
        fake_modules = {
            "anki": anki_module,
            "anki.collection_pb2": collection_pb2,
            "inkcards_anki_bridge.anki.compact_260903": adapter_module,
            "inkcards_anki_bridge.services.compact_application": service_module,
        }

        diagnostic_root = tempfile.TemporaryDirectory(prefix="inkcards-plugin-diagnostic-")
        self.addCleanup(diagnostic_root.cleanup)

        class Runtime:
            profile_id = "profile-a"
            store = SimpleNamespace(root=Path(diagnostic_root.name))

            def __init__(self) -> None:
                self._lock = threading.RLock()
                self.manager = None

            def snapshot_context(self):
                return OperationContext("profile-a", 7, "epoch-a")

            def update_collection_generation(self, _generation: int) -> None:
                pass

            def refresh_committed_collection_generation(self, _generation: int) -> None:
                pass

            def attach_compact_jobs(self, manager):
                self.manager = manager
                return manager

        runtime = Runtime()
        aqt = SimpleNamespace(operations=SimpleNamespace(CollectionOp=lambda **_kwargs: None))
        main_window.taskman = SimpleNamespace(run_on_main=lambda callback: None)
        with patch.dict(sys.modules, fake_modules):
            manager = _attach_compact_jobs(aqt_module=aqt, main_window=main_window, runtime=runtime)

        self.assertIsInstance(manager, CompactJobManager)
        self.assertIs(runtime.manager, manager)
        context = OperationContext("profile-a", 7, "epoch-a")
        collection = object()
        result = manager._apply_one(collection, "device-a", "session-a", context)
        self.assertEqual(result.status, "applied")
        self.assertEqual([item[0] for item in calls], ["authorize", "apply"])
        self.assertEqual(calls[0][4], {
            "collection_identity": "profile-a",
            "expected_generation": 7,
        })
        self.assertEqual(calls[1][4], "approved-authorization")

        # 只读授权拒绝不能被宿主包装成已写入后的 unknown。
        original_authorize = FakeService.authorize_next
        def reject_context(*_args, **_kwargs):
            raise FakeApplicationError("snapshot_application_context_missing")
        FakeService.authorize_next = reject_context
        try:
            rejected = manager._apply_one(collection, "device-a", "session-a", context)
            self.assertEqual(rejected.status, "blocked")
            self.assertEqual(rejected.reason, "snapshot_application_context_missing")
            self.assertIsInstance(rejected.changes, FakeOpChanges)
            self.assertEqual(rejected.generation_after, 7)
        finally:
            FakeService.authorize_next = original_authorize

        calls.clear()
        main_window.state = "review"
        with self.assertRaises(CollectionStartRejected) as rejected:
            manager._apply_one(collection, "device-a", "session-a", context)
        self.assertEqual(rejected.exception.code, "reviewer_owns_collection")
        self.assertEqual([item[0] for item in calls], ["authorize"])
        manager.stop()

    def test_anki_2693_starts_runtime_with_verified_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = start_in_anki(
                aqt_module=fake_aqt(
                    Path(temp) / "profile",
                    version="26.9.3",
                    build="29bb700b",
                    config={"device_server": {"host": "127.0.0.1", "port": 0}},
                ),
                user_files_root=Path(temp) / "user_files",
                addon_module_name="inkcards_anki_bridge",
            )
            try:
                self.assertTrue(runtime.running)
                self.assertEqual(runtime.status()["bind"]["host"], DEFAULT_HOST)
            finally:
                runtime.stop()

    def test_exact_version_and_build_are_required_without_collection_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            user_files = Path(temp) / "addon" / "user_files"
            aqt = fake_aqt(
                Path(temp) / "profile",
                config={"host": "127.0.0.1", "port": 0},
            )
            runtime = start_in_anki(
                aqt_module=aqt,
                user_files_root=user_files,
                addon_module_name="inkcards_anki_bridge",
            )
            try:
                self.assertTrue(runtime.running)
                self.assertTrue(runtime.root.is_relative_to(user_files))
                self.assertEqual(runtime.status()["bind"]["host"], DEFAULT_HOST)
                self.assertNotIn("rendezvous", {path.name for path in user_files.rglob("*")})
                self.assertIn("inkcards_anki_bridge", aqt.mw.addonManager.requested)
            finally:
                runtime.stop()

            with self.assertRaises(Exception) as raised:
                start_in_anki(
                    aqt_module=fake_aqt(Path(temp) / "profile", build="other"),
                    user_files_root=user_files,
                    addon_module_name="inkcards_anki_bridge",
                )
            self.assertEqual(getattr(raised.exception, "code", None), "unsupported_anki_build")

    def test_invalid_config_falls_back_and_valid_loopback_config_is_used(self) -> None:
        self.assertEqual(
            resolve_server_config({"host": "localhost", "port": -1}),
            (DEFAULT_HOST, DEFAULT_PORT),
        )
        self.assertEqual(
            resolve_server_config(
                {"device_server": {"host": "192.0.2.10", "port": 9876}}
            ),
            ("192.0.2.10", 9876),
        )
        self.assertEqual(
            resolve_server_config({"host": "127.0.0.1", "port": 0}),
            ("127.0.0.1", 0),
        )
        self.assertEqual(
            resolve_server_config({"host": "::1", "port": 65535}),
            ("::1", 65535),
        )
        self.assertEqual(
            resolve_server_config({"host": "192.0.2.10", "port": 8766}),
            ("192.0.2.10", 8766),
        )

    def test_register_only_adds_hooks_and_starts_after_profile_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            aqt = fake_aqt(Path(temp) / "profile")
            logs = []
            runtime = SimpleNamespace(running=True, stop=lambda: None)
            with patch("inkcards_anki_bridge.plugin.start_in_anki", return_value=runtime) as start:
                controller = register_in_anki(
                    aqt_module=aqt,
                    gui_hooks_module=aqt.gui_hooks,
                    addon_module_name="inkcards_anki_bridge",
                    logger=logs.append,
                )
                self.assertFalse(start.called)
                aqt.gui_hooks.profile_did_open.emit()
                self.assertEqual(start.call_count, 1)
                controller.shutdown()

    def test_profile_switch_close_and_unload_close_old_runtime_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            profile_a = Path(temp) / "profile-a"
            profile_b = Path(temp) / "profile-b"
            aqt = fake_aqt(
                profile_a,
                config={"host": "127.0.0.1", "port": 0},
            )
            controller = register_in_anki(
                aqt_module=aqt,
                gui_hooks_module=aqt.gui_hooks,
                addon_module_name="inkcards_anki_bridge",
                user_files_root=Path(temp) / "user_files",
            )
            aqt.gui_hooks.profile_did_open.emit()
            first = controller.runtime
            self.assertIsNotNone(first)
            first_port = first.port
            pairing = first.issue_pairing_code()
            connection = http.client.HTTPConnection("127.0.0.1", first_port, timeout=1)
            try:
                token = uuid.uuid4().hex + uuid.uuid4().hex
                pairing_id = str(uuid.uuid4())
                raw = json.dumps(
                    {"code": pairing, "device_id": "device-a", "pairing_id": pairing_id, "token": token},
                    separators=(",", ":"),
                ).encode("utf-8")
                connection.request("POST", "/v2/pair", raw)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read().decode("utf-8"))["status"], "pending")
            finally:
                connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", first_port, timeout=1)
            try:
                raw = json.dumps({"device_id": "device-a", "pairing_id": pairing_id}).encode()
                connection.request("POST", "/v2/pair/confirm", raw, {"Authorization": "Bearer " + token})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read().decode())["status"], "active")
            finally:
                connection.close()

            aqt.mw.pm.profileFolder = lambda: str(profile_b)
            aqt.gui_hooks.profile_did_open.emit()
            second = controller.runtime
            self.assertIsNotNone(second)
            self.assertIsNot(first, second)
            self.assertFalse(first.running)
            self.assert_port_closed(first_port)
            connection = http.client.HTTPConnection("127.0.0.1", second.port, timeout=1)
            try:
                raw = json.dumps(
                    {
                        "protocol": "x4-2",
                        "device_id": "device-a",
                        "session_id": "old-session",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                connection.request(
                    "POST",
                    "/v2/handshake",
                    raw,
                    {"Authorization": "Bearer " + token},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
            finally:
                connection.close()

            second_port = second.port
            aqt.gui_hooks.profile_will_close.emit()
            self.assertIsNone(controller.runtime)
            self.assert_port_closed(second_port)

            # close/unload/显式 shutdown 的重复到达不能重新打开或二次清理。
            controller.shutdown()

    @staticmethod
    def assert_port_closed(port: int) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
        try:
            with unittest.TestCase().assertRaises((ConnectionRefusedError, OSError, TimeoutError)):
                connection.request("POST", "/v2/sync/status", b"{}")
                connection.getresponse()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
