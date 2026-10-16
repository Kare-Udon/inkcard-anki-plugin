from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.anki.snapshot_jobs import SnapshotJobState
from inkcards_anki_bridge.plugin import start_in_anki


class FakeHook:
    def append(self, _callback) -> None:
        pass

    def remove(self, _callback) -> None:
        pass


class FakeGuiHooks:
    profile_did_open = FakeHook()
    profile_will_close = FakeHook()
    addons_dialog_will_delete_addons = FakeHook()


class FakeAddonManager:
    def addonFromModule(self, _module_name: str) -> str:
        return "inkcards_anki_bridge"

    def getConfig(self, _addon_id: str):
        return {"host": "127.0.0.1", "port": 0}


class FakeTaskman:
    def run_on_main(self, callback) -> None:
        callback()


class FakeQueryOp:
    created = []

    def __init__(self, *, parent, op, success) -> None:
        self.parent = parent
        self.op = op
        self.success = success
        self.failure_callback = None
        self.started = False
        self.__class__.created.append(self)

    def failure(self, callback):
        self.failure_callback = callback
        return self

    def run_in_background(self) -> None:
        self.started = True


class FakeServer:
    def __init__(self, _router, *, host, port, limits=None) -> None:
        self.server_address = (host, 39123 if port == 0 else port)
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout=None) -> None:
        self.started = False


def fake_aqt(profile: Path):
    main_window = SimpleNamespace(
        pm=SimpleNamespace(profileFolder=lambda: str(profile)),
        addonManager=FakeAddonManager(),
        taskman=FakeTaskman(),
    )
    return SimpleNamespace(
        appVersion="26.08.1",
        build="39e4b0b4",
        mw=main_window,
        gui_hooks=FakeGuiHooks(),
        operations=SimpleNamespace(QueryOp=FakeQueryOp),
    )


class PluginSnapshotJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeQueryOp.created = []

    def test_start_in_anki_attaches_official_queryop_shape_without_aqt_in_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "inkcards_anki_bridge.runtime.DeviceServer", FakeServer
        ):
            aqt = fake_aqt(Path(temp) / "profile")
            runtime = start_in_anki(
                aqt_module=aqt,
                user_files_root=Path(temp) / "user_files",
                addon_module_name="inkcards_anki_bridge",
            )
            try:
                self.assertIsNotNone(runtime.snapshot_jobs)
                job = runtime.request_snapshot(card_ids=["1"])
                self.assertEqual(job.state, SnapshotJobState.QUEUED)
                self.assertEqual(len(FakeQueryOp.created), 1)
                query = FakeQueryOp.created[0]
                self.assertIs(query.parent, aqt.mw)
                self.assertTrue(callable(query.op))
                self.assertTrue(query.started)
                self.assertIsNotNone(query.failure_callback)
            finally:
                runtime.stop()

    def test_runtime_stop_cancels_queued_queryop_and_late_callback_cannot_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "inkcards_anki_bridge.runtime.DeviceServer", FakeServer
        ):
            aqt = fake_aqt(Path(temp) / "profile")
            runtime = start_in_anki(
                aqt_module=aqt,
                user_files_root=Path(temp) / "user_files",
                addon_module_name="inkcards_anki_bridge",
            )
            job = runtime.request_snapshot(card_ids=["1"])
            query = FakeQueryOp.created[0]
            runtime.stop()

            self.assertEqual(job.state, SnapshotJobState.CANCELED)
            self.assertIsNone(runtime.snapshot_jobs)
            # The host may still invoke a previously queued op callback. The
            # operation task is already canceled, so no collection is touched
            # and no snapshot is published after runtime stop.
            query.op(object())
            self.assertIsNone(runtime.last_snapshot)


if __name__ == "__main__":
    unittest.main()
