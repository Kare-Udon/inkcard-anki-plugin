from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from inkcards_anki_bridge.logging_control import AddonLogger
from inkcards_anki_bridge.plugin import (
    register_in_anki,
    write_device_server_host,
    write_logging_policy,
)
from inkcards_anki_bridge.ui.default_sync_dialog import InkCardsDialog
from inkcards_anki_bridge.ui.dialog import DeviceManagementDialog
from inkcards_anki_bridge.ui.integration import register_tools_action
from inkcards_anki_bridge.ui.presenter import InkCardsPresenter
from inkcards_anki_bridge.ui.settings_dialog import SettingsDialog


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


class FakeWidget:
    def __init__(self, *_args) -> None:
        self.clicked = FakeSignal()
        self.toggled = FakeSignal()
        self.timeout = FakeSignal()
        self._blocked = False
        self._checked = False
        self._enabled = True
        self.text_value = ""

    def setWordWrap(self, _enabled) -> None:
        pass

    def setText(self, text) -> None:
        self.text_value = text

    def setEnabled(self, _enabled) -> None:
        self._enabled = bool(_enabled)

    def setPlaceholderText(self, _text) -> None:
        pass

    def setInterval(self, _interval) -> None:
        pass

    def blockSignals(self, blocked):
        previous, self._blocked = self._blocked, blocked
        return previous

    def setChecked(self, checked) -> None:
        checked = bool(checked)
        changed = checked != self._checked
        self._checked = checked
        if changed and not self._blocked:
            self.toggled.emit(checked)

    def isChecked(self):
        return self._checked

    def text(self):
        return self.text_value


class FakeLayout:
    def __init__(self, *_args) -> None:
        pass

    def addWidget(self, _widget) -> None:
        pass

    def addLayout(self, _layout) -> None:
        pass


class FakeComboBox(FakeWidget):
    def __init__(self, *_args) -> None:
        super().__init__()
        self.currentIndexChanged = FakeSignal()
        self.items = []
        self._index = -1

    def addItem(self, label, value=None) -> None:
        self.items.append((label, value))
        if self._index < 0:
            self._index = 0

    def currentData(self):
        return self.items[self._index][1] if self._index >= 0 else None

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, index) -> None:
        changed = index != self._index
        self._index = index
        if changed and not self._blocked:
            self.currentIndexChanged.emit(index)


class FakeListWidget(FakeWidget):
    def __init__(self, *_args) -> None:
        super().__init__()
        self.itemChanged = FakeSignal()
        self.itemSelectionChanged = FakeSignal()
        self.items = []
        self._current_row = -1

    def count(self):
        return len(self.items)

    def clear(self) -> None:
        self.items.clear()
        self._current_row = -1

    def addItem(self, item) -> None:
        self.items.append(item)

    def item(self, index):
        return self.items[index]

    def currentItem(self):
        return (
            self.items[self._current_row]
            if 0 <= self._current_row < len(self.items)
            else None
        )

    def currentRow(self):
        return self._current_row

    def setCurrentRow(self, index) -> None:
        changed = index != self._current_row
        self._current_row = index
        if changed and not self._blocked:
            self.itemSelectionChanged.emit()


class FakeListItem:
    def __init__(self, text="") -> None:
        self.text_value = text
        self._data = {}
        self._flags = 0
        self._check_state = 0

    def setData(self, role, value) -> None:
        self._data[role] = value

    def data(self, role):
        return self._data.get(role)

    def flags(self):
        return self._flags

    def setFlags(self, flags) -> None:
        self._flags = flags

    def setCheckState(self, value) -> None:
        self._check_state = value

    def checkState(self):
        return self._check_state


class FakeDialogWidget(FakeWidget):
    def __init__(self, *_args) -> None:
        super().__init__()
        self.finished = FakeSignal()

    def setWindowTitle(self, _title) -> None:
        pass

    def setModal(self, _modal) -> None:
        pass

    def show(self) -> None:
        pass


class FakeTimer(FakeWidget):
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def isActive(self):
        return False


class FakeInputDialog:
    responses: ClassVar[list] = []
    calls: ClassVar[list] = []

    @classmethod
    def getText(cls, *args, **kwargs):
        cls.calls.append((args, kwargs))
        return cls.responses.pop(0) if cls.responses else ("", False)


class FakeMessageBox:
    Yes = 1
    No = 0
    response = No
    calls: ClassVar[list] = []

    @classmethod
    def question(cls, *args):
        cls.calls.append(args)
        return cls.response

    @classmethod
    def information(cls, *_args):
        return None


class FakeDialogQt:
    QDialog = FakeDialogWidget
    QVBoxLayout = FakeLayout
    QHBoxLayout = FakeLayout
    QLabel = FakeWidget
    QPushButton = FakeWidget
    QCheckBox = FakeWidget
    QComboBox = FakeComboBox
    QLineEdit = FakeWidget
    QListWidget = FakeListWidget
    QTimer = FakeTimer
    QListWidgetItem = FakeListItem
    QInputDialog = FakeInputDialog
    Qt = SimpleNamespace(
        Checked=2,
        Unchecked=0,
        ItemIsUserCheckable=16,
        UserRole=32,
    )
    QMessageBox = FakeMessageBox


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


class FakeDeviceManagementRuntime:
    def __init__(self):
        self.devices = [
            {
                "slot_id": "slot-a",
                "name": "客厅阅读器",
                "status": "bound",
                "device_id": "device-private",
                "last_seen_at_ms": 1_790_000_000_000,
            },
            {
                "slot_id": "slot-old",
                "name": "旧设备",
                "status": "revoked",
                "device_id": "old-device-private",
                "last_seen_at_ms": None,
            },
        ]
        self.calls = []

    def list_devices(self, *, include_revoked=False):
        values = self.devices
        return (
            values
            if include_revoked
            else [item for item in values if item["status"] != "revoked"]
        )

    def add_device(self, name):
        self.calls.append(("add_device", name))
        self.devices.append(
            {
                "slot_id": "slot-new",
                "name": name,
                "status": "pending",
                "last_seen_at_ms": None,
            }
        )
        return {
            "slot_id": "slot-new",
            "name": name,
            "status": "pending",
            "code": "123456789012",
        }

    def allow_rebind(self, slot_id):
        self.calls.append(("allow_rebind", slot_id))
        self.devices[0]["status"] = "rebind_ready"

    def reset_pairing_code(self, slot_id):
        self.calls.append(("reset_pairing_code", slot_id))
        return {"slot_id": slot_id, "code": "987654321098"}

    def remove_device(self, slot_id):
        self.calls.append(("remove_device", slot_id))
        next(item for item in self.devices if item["slot_id"] == slot_id)["status"] = (
            "revoked"
        )

    def rename_device(self, slot_id, name):
        self.calls.append(("rename_device", slot_id, name))
        next(item for item in self.devices if item["slot_id"] == slot_id)["name"] = name


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
        FakeInputDialog.responses = []
        FakeInputDialog.calls = []
        FakeMessageBox.response = FakeMessageBox.No
        FakeMessageBox.calls = []

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

    def test_write_logging_policy_preserves_existing_config_and_hot_updates(self) -> None:
        manager = FakeAddonManager(
            {
                "device_server": {"host": "0.0.0.0", "port": 9876},
                "unrelated": {"keep": True},
            }
        )
        aqt, _menu, _main_window = make_aqt(manager=manager)
        with tempfile.TemporaryDirectory() as root:
            logger = AddonLogger(Path(root))
            result = write_logging_policy(
                aqt_module=aqt,
                addon_module_name="inkcards_anki_bridge",
                policy={"level": "debug", "console": True},
                logger=logger,
            )
            self.assertEqual(result["logging"], {"level": "debug", "console": True})
            self.assertEqual(result["device_server"], {"host": "0.0.0.0", "port": 9876})
            self.assertEqual(result["unrelated"], {"keep": True})
            self.assertEqual(logger.policy, {"level": "debug", "console": True})

            manager.writer = False
            with self.assertRaises(OSError):
                write_logging_policy(
                    aqt_module=aqt,
                    addon_module_name="inkcards_anki_bridge",
                    policy={"level": "off", "console": False},
                    logger=logger,
                )
            self.assertEqual(logger.policy, {"level": "debug", "console": True})
            self.assertEqual(manager.config["logging"], {"level": "debug", "console": True})

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

    def test_logging_controls_persist_immediately_and_restore_on_save_failure(self) -> None:
        policy = {"level": "warn", "console": False}
        writes = []
        fail = False

        def write_logging(value):
            if fail:
                raise OSError("TOKEN-private write failure")
            policy.update(value)
            writes.append(dict(value))

        dialog = SettingsDialog(
            parent=None,
            runtime_provider=lambda: None,
            qt_module=FakeDialogQt,
            presenter=InkCardsPresenter(lambda: None),
            logging_policy_provider=lambda: dict(policy),
            logging_policy_writer=write_logging,
        )
        self.assertEqual(dialog._logging_level.currentData(), "warn")
        dialog._logging_level.setCurrentIndex(4)
        self.assertEqual(policy, {"level": "debug", "console": False})
        self.assertEqual(writes[-1], policy)

        dialog._logging_console.setChecked(True)
        self.assertEqual(policy, {"level": "debug", "console": True})
        fail = True
        dialog._logging_level.setCurrentIndex(1)
        self.assertEqual(policy, {"level": "debug", "console": True})
        self.assertEqual(dialog._logging_level.currentData(), "debug")
        self.assertTrue(dialog._logging_console.isChecked())
        self.assertIn("未保存", dialog._logging_message.text_value)
        self.assertNotIn("TOKEN", dialog._logging_message.text_value)

    def test_device_list_actions_use_selected_slot_and_cancellation_is_unchanged(
        self,
    ) -> None:
        runtime = FakeDeviceManagementRuntime()
        presenter = InkCardsPresenter(lambda: runtime)
        dialog = DeviceManagementDialog(
            parent=None,
            runtime_provider=lambda: runtime,
            qt_module=FakeDialogQt,
            presenter=presenter,
        )
        dialog._render(presenter.refresh_devices())

        self.assertEqual(dialog._device_list.count(), 1)
        self.assertNotIn("device-private", dialog._device_list.item(0).text_value)
        self.assertFalse(hasattr(dialog, "_device_input"))

        FakeInputDialog.responses = [("ignored", False)]
        dialog._add_device()
        self.assertEqual(runtime.calls, [])
        dialog._device_list.setCurrentRow(0)
        FakeMessageBox.response = FakeMessageBox.No
        dialog._allow_rebind()
        dialog._reset_pairing_code()
        dialog._remove_device()
        FakeInputDialog.responses = [("ignored", False)]
        dialog._rename_device()
        self.assertEqual(runtime.calls, [])

        dialog._show_removed_checkbox.setChecked(True)
        self.assertEqual(dialog._device_list.count(), 2)
        dialog._show_removed_checkbox.setChecked(False)
        self.assertEqual(dialog._device_list.count(), 1)
        dialog._device_list.setCurrentRow(0)

        FakeInputDialog.responses = [("新设备", True)]
        dialog._add_device()
        self.assertEqual(runtime.calls[-1], ("add_device", "新设备"))
        self.assertIn("1234-5678-9012", dialog._pairing_label.text_value)
        self.assertIn("不会自动过期", dialog._pairing_label.text_value)
        self.assertNotIn("1234-5678-9012", repr(presenter.view()))

        calls_before_cancel = list(runtime.calls)
        code_before_cancel = dialog._pairing_label.text_value
        FakeMessageBox.response = FakeMessageBox.No
        dialog._allow_rebind()
        dialog._reset_pairing_code()
        dialog._remove_device()
        FakeInputDialog.responses = [("忽略", False)]
        dialog._rename_device()
        self.assertEqual(runtime.calls, calls_before_cancel)
        self.assertEqual(dialog._pairing_label.text_value, code_before_cancel)

        dialog._device_list.setCurrentRow(0)
        FakeMessageBox.response = FakeMessageBox.Yes
        dialog._allow_rebind()
        dialog._reset_pairing_code()
        self.assertIn("9876-5432-1098", dialog._pairing_label.text_value)
        FakeInputDialog.responses = [("书房阅读器", True)]
        dialog._rename_device()
        dialog._remove_device()
        self.assertEqual(
            runtime.calls[1:],
            [
                ("allow_rebind", "slot-a"),
                ("reset_pairing_code", "slot-a"),
                ("rename_device", "slot-a", "书房阅读器"),
                ("remove_device", "slot-a"),
            ],
        )
        remove_prompt = next(
            call[2] for call in FakeMessageBox.calls if call[1] == "确认移除设备"
        )
        self.assertIn("保留既有同步与评分历史", remove_prompt)

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

    def test_ui_registration_failure_uses_safe_plugin_logger(self) -> None:
        manager = FakeAddonManager({"logging": {"level": "error", "console": False}})
        aqt, _menu, _main_window = make_aqt(manager=manager)
        with tempfile.TemporaryDirectory() as root:
            core = AddonLogger(Path(root))
            with patch(
                "inkcards_anki_bridge.ui.integration.register_tools_action",
                side_effect=RuntimeError("TOKEN-private failure"),
            ):
                controller = register_in_anki(
                    aqt_module=aqt,
                    gui_hooks_module=aqt.gui_hooks,
                    addon_module_name="inkcards_anki_bridge",
                    user_files_root=Path(root) / "user_files",
                    logging_core=core,
                )
            self.assertIs(controller.logging_core, core)
            self.assertIsNotNone(core.path)
            raw = core.path.read_text()
            self.assertIn('"event":"registration_failed"', raw)
            self.assertIn('"exception_type":"RuntimeError"', raw)
            self.assertNotIn("TOKEN", raw)
            controller.shutdown()

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
        dialog._device_dialog = None
        dialog._settings_dialog = None
        InkCardsDialog._cleanup(dialog)
        InkCardsDialog._cleanup(dialog)
        self.assertEqual(timer.stop_calls, 1)
        self.assertEqual(presenter.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
