from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.anki.catalog_jobs import CatalogJobState
from inkcards_anki_bridge.plugin import start_in_anki


class FakeDeckName:
    def __init__(self, deck_id: int, name: str) -> None:
        self.id = deck_id
        self.name = name


class FakeDecks:
    def all_names_and_ids(self, *, skip_empty_default: bool, include_filtered: bool):
        assert skip_empty_default is False
        assert include_filtered is True
        return [FakeDeckName(20, "中文::子牌组"), FakeDeckName(10, "中文")]

    def card_count(self, deck_ids, include_subdecks: bool):
        assert tuple(deck_ids) == (10, 20)
        assert include_subdecks is True
        return 2


class FakeCollection:
    def __init__(self) -> None:
        self.decks = FakeDecks()


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
        self.__class__.created.append(self)

    def failure(self, callback):
        self.failure_callback = callback
        return self

    def run_in_background(self) -> None:
        pass


class FakeServer:
    def __init__(self, _router, *, host, port, limits=None) -> None:
        self.server_address = (host, 39123 if port == 0 else port)

    def start(self) -> None:
        pass

    def stop(self, timeout=None) -> None:
        pass


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


class PluginCatalogJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeQueryOp.created = []

    def test_start_in_anki_attaches_catalog_queryop_and_runtime_narrow_api(self) -> None:
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
                job = runtime.request_deck_catalog()
                self.assertEqual(job.state, CatalogJobState.QUEUED)
                self.assertEqual(len(FakeQueryOp.created), 1)
                query = FakeQueryOp.created[0]
                self.assertIs(query.parent, aqt.mw)
                self.assertTrue(callable(query.op))
                query.op(FakeCollection())
                self.assertEqual(job.state, CatalogJobState.PUBLISHED)
                self.assertEqual(runtime.last_catalog.deck_ids, ("10", "20"))
                self.assertTrue(runtime.status()["catalog"]["available"])
                self.assertFalse(runtime.status()["write_enabled"])
            finally:
                runtime.stop()

    def test_runtime_stop_invalidates_late_catalog_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "inkcards_anki_bridge.runtime.DeviceServer", FakeServer
        ):
            aqt = fake_aqt(Path(temp) / "profile")
            runtime = start_in_anki(
                aqt_module=aqt,
                user_files_root=Path(temp) / "user_files",
                addon_module_name="inkcards_anki_bridge",
            )
            job = runtime.request_deck_catalog()
            query = FakeQueryOp.created[0]
            runtime.stop()
            query.op(FakeCollection())
            self.assertEqual(job.state, CatalogJobState.CANCELED)
            self.assertIsNone(runtime.last_catalog)


if __name__ == "__main__":
    unittest.main()
