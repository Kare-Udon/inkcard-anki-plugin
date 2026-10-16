from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.plugin import register_in_anki, write_device_server_host
from inkcards_anki_bridge.ui.dialog import InkCardsDialog
from inkcards_anki_bridge.ui.integration import register_tools_action


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class FakeAction:
    def __init__(self, text: str, parent) -> None:
        self._text = text
        self.parent = parent
        self.triggered = FakeSignal()
        self._inkcards_trigger_connected = False

    def text(self) -> str:
        return self._text


class FakeMenu:
    def __init__(self) -> None:
        self._actions = []

    def addAction(self, action) -> None:
        self._actions.append(action)

    def removeAction(self, action) -> None:
        if action in self._actions:
            self._actions.remove(action)

    def actions(self):
        return list(self._actions)


class FakeDialog:
    instances = []

    def __init__(self, *, parent, runtime_provider, config_writer, qt_module) -> None:
        self.parent = parent
        self.runtime_provider = runtime_provider
        self.config_writer = config_writer
        self.qt_module = qt_module
        self.show_calls = 0
        self.dispose_calls = 0
        self.__class__.instances.append(self)

    def show(self) -> None:
        self.show_calls += 1

    def dispose(self) -> None:
        self.dispose_calls += 1


class FakeQt:
    QAction = FakeAction


class FakeHook:
    def __init__(self) -> None:
        self.callbacks = []

    def append(self, callback) -> None:
        self.callbacks.append(callback)

    def remove(self, callback) -> None:
        if callback in self.callbacks:
            self.callbacks.remove(callback)


class FakeHooks:
    def __init__(self) -> None:
        self.profile_did_open = FakeHook()
        self.profile_will_close = FakeHook()
        self.addons_dialog_will_delete_addons = FakeHook()


class FakeAddonManager:
    def __init__(self, config=None, *, writer=True) -> None:
        self.config = dict(config or {})
        self.writer = writer
        self.writes = []

    def addonFromModule(self, _module_name: str) -> str:
        return "inkcards_anki_bridge"

    def getConfig(self, _addon_id: str):
        return dict(self.config)

    def writeConfig(self, addon_id: str, config) -> None:
        if not self.writer:
            raise OSError("secret manager failure")
        self.writes.append((addon_id, config))
        self.config = dict(config)


def make_aqt(*, manager=None):
    menu = FakeMenu()
    main_window = SimpleNamespace(
        form=SimpleNamespace(menuTools=menu),
        addonManager=manager or FakeAddonManager(),
        pm=SimpleNamespace(profileFolder=lambda: "/tmp/inkcards-ui-profile"),
    )
    return SimpleNamespace(mw=main_window, gui_hooks=FakeHooks()), menu, main_window


class PluginUiTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeDialog.instances = []

    def test_tools_action_is_idempotent_and_dialog_is_reused_with_mw_parent(self) -> None:
        aqt, menu, main_window = make_aqt()
        first = register_tools_action(
            aqt_module=aqt,
            runtime_provider=lambda: None,
            qt_module=FakeQt,
            dialog_factory=FakeDialog,
        )
        second = register_tools_action(
            aqt_module=aqt,
            runtime_provider=lambda: None,
            qt_module=FakeQt,
            dialog_factory=FakeDialog,
        )
        self.assertIs(first, second)
        self.assertEqual(len(menu.actions()), 1)
        self.assertEqual(menu.actions()[0].text(), "InkCards…")

        menu.actions()[0].triggered.emit(False)
        menu.actions()[0].triggered.emit(False)
        self.assertEqual(len(FakeDialog.instances), 1)
        self.assertEqual(FakeDialog.instances[0].show_calls, 2)
        self.assertIs(FakeDialog.instances[0].parent, main_window)

        first.unregister()
        self.assertEqual(menu.actions(), [])
        self.assertEqual(FakeDialog.instances[0].dispose_calls, 1)

    def test_write_lan_config_preserves_port_and_uses_addon_manager(self) -> None:
        manager = FakeAddonManager({"device_server": {"host": "127.0.0.1", "port": 9876}})
        aqt, _menu, _main_window = make_aqt(manager=manager)
        enabled = write_device_server_host(
            aqt_module=aqt,
            addon_module_name="inkcards_anki_bridge",
            enabled=True,
        )
        self.assertEqual(enabled["device_server"]["host"], "0.0.0.0")
        self.assertEqual(enabled["device_server"]["port"], 9876)
        disabled = write_device_server_host(
            aqt_module=aqt,
            addon_module_name="inkcards_anki_bridge",
            enabled=False,
        )
        self.assertEqual(disabled["device_server"]["host"], "127.0.0.1")
        self.assertEqual(disabled["device_server"]["port"], 9876)
        self.assertEqual(len(manager.writes), 2)

    def test_config_writer_failure_is_a_visible_error_and_does_not_restart(self) -> None:
        manager = FakeAddonManager(writer=False)
        aqt, _menu, _main_window = make_aqt(manager=manager)
        with patch("inkcards_anki_bridge.ui.integration._load_qt", return_value=FakeQt):
            registration = register_tools_action(
                aqt_module=aqt,
                runtime_provider=lambda: None,
                config_writer=lambda enabled: write_device_server_host(
                    aqt_module=aqt,
                    addon_module_name="inkcards_anki_bridge",
                    enabled=enabled,
                ),
                dialog_factory=FakeDialog,
            )
        self.assertIsNotNone(registration)
        self.assertRaises(
            OSError,
            write_device_server_host,
            aqt_module=aqt,
            addon_module_name="inkcards_anki_bridge",
            enabled=True,
        )
        self.assertEqual(manager.writes, [])
        registration.unregister()

    def test_register_in_anki_reuses_controller_and_ui_action(self) -> None:
        aqt, menu, _main_window = make_aqt()
        with patch("inkcards_anki_bridge.ui.integration._load_qt", return_value=FakeQt):
            first = register_in_anki(
                aqt_module=aqt,
                gui_hooks_module=aqt.gui_hooks,
                addon_module_name="inkcards_anki_bridge",
            )
            second = register_in_anki(
                aqt_module=aqt,
                gui_hooks_module=aqt.gui_hooks,
                addon_module_name="inkcards_anki_bridge",
            )
        self.assertIs(first, second)
        self.assertEqual([action.text() for action in menu.actions()], ["InkCards…"])
        first.shutdown()
        self.assertEqual(menu.actions(), [])

    def test_dialog_cleanup_stops_timer_and_cancels_presenter_without_runtime_stop(self) -> None:
        timer = SimpleNamespace(active=True, stop_calls=0, isActive=lambda: timer.active)

        def stop() -> None:
            timer.stop_calls += 1
            timer.active = False

        timer.stop = stop
        presenter = SimpleNamespace(close_calls=0, close=lambda: setattr(presenter, "close_calls", 1))
        dialog = object.__new__(InkCardsDialog)
        dialog._cleaned = False
        dialog._timer = timer
        dialog._presenter = presenter
        InkCardsDialog._cleanup(dialog)
        InkCardsDialog._cleanup(dialog)
        self.assertEqual(timer.stop_calls, 1)
        self.assertEqual(presenter.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
