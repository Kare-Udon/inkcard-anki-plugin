"""Anki 原生 Qt 设备管理辅助窗口。"""

from __future__ import annotations

from typing import Any, Callable

from .presenter import InkCardsPresenter, ViewState


def _load_qt() -> Any:
    """优先使用 Anki 自带 ``aqt.qt``，不创建 QApplication。"""

    from aqt import qt  # type: ignore[import-not-found]

    return qt


def _enum_value(container: Any, name: str, fallback: Any = None) -> Any:
    value = getattr(container, name, None)
    if value is not None:
        return value
    for group_name in ("ItemFlag", "CheckState", "ItemDataRole", "WidgetAttribute"):
        group = getattr(container, group_name, None)
        value = getattr(group, name, None) if group is not None else None
        if value is not None:
            return value
    return fallback


def _message_button(message_box: Any, name: str) -> Any:
    """兼容 PyQt5 的 ``QMessageBox.Yes`` 与 PyQt6 的 StandardButton。"""

    direct = getattr(message_box, name, None)
    if direct is not None:
        return direct
    standard_button = getattr(message_box, "StandardButton", None)
    return getattr(standard_button, name, None) if standard_button is not None else None


class DeviceManagementDialog:
    """管理配对设备；关闭窗口不会关闭 presenter 或 runtime。"""

    def __init__(
        self,
        *,
        parent: Any,
        runtime_provider: Callable[[], Any],
        qt_module: Any = None,
        presenter: InkCardsPresenter | None = None,
    ) -> None:
        self._qt = qt_module or _load_qt()
        self._presenter = presenter or InkCardsPresenter(runtime_provider)
        self._dialog = self._qt.QDialog(parent)
        self._dialog.setWindowTitle("InkCards · 设备管理")
        set_modal = getattr(self._dialog, "setModal", None)
        if callable(set_modal):
            set_modal(False)
        self._device_signature: tuple[tuple[str, str], ...] = ()
        self._closed = False
        self._build_widgets()
        finished = getattr(self._dialog, "finished", None)
        if finished is not None and callable(getattr(finished, "connect", None)):
            finished.connect(self._on_finished)

    @property
    def presenter(self) -> InkCardsPresenter:
        return self._presenter

    @property
    def dialog(self) -> Any:
        return self._dialog

    @property
    def action_parent(self) -> Any:
        return self._dialog

    def _build_widgets(self) -> None:
        qt = self._qt
        root = qt.QVBoxLayout(self._dialog)
        title = qt.QLabel("设备管理")
        root.addWidget(title)

        top = qt.QHBoxLayout()
        self._refresh_button = qt.QPushButton("刷新")
        self._add_device_button = qt.QPushButton("添加设备")
        self._show_removed_checkbox = qt.QCheckBox("显示已移除")
        top.addWidget(self._refresh_button)
        top.addWidget(self._add_device_button)
        top.addWidget(self._show_removed_checkbox)
        root.addLayout(top)

        self._pairing_label = qt.QLabel()
        self._pairing_label.setWordWrap(True)
        root.addWidget(self._pairing_label)
        self._device_list = qt.QListWidget()
        root.addWidget(self._device_list)

        actions = qt.QHBoxLayout()
        self._allow_rebind_button = qt.QPushButton("允许重新绑定")
        self._reset_pairing_button = qt.QPushButton("重置配对码")
        self._rename_device_button = qt.QPushButton("改名")
        self._remove_device_button = qt.QPushButton("移除设备")
        for button in (
            self._allow_rebind_button,
            self._reset_pairing_button,
            self._rename_device_button,
            self._remove_device_button,
        ):
            actions.addWidget(button)
        root.addLayout(actions)

        self._message_label = qt.QLabel()
        self._message_label.setWordWrap(True)
        root.addWidget(self._message_label)

        self._refresh_button.clicked.connect(self._refresh)
        self._add_device_button.clicked.connect(self._add_device)
        self._allow_rebind_button.clicked.connect(self._allow_rebind)
        self._reset_pairing_button.clicked.connect(self._reset_pairing_code)
        self._rename_device_button.clicked.connect(self._rename_device)
        self._remove_device_button.clicked.connect(self._remove_device)
        self._device_list.itemSelectionChanged.connect(self._device_selection_changed)
        self._show_removed_checkbox.toggled.connect(self._show_removed_toggled)
        self._render(self._presenter.view())

    def _refresh(self) -> None:
        self._render(self._presenter.refresh_devices())

    def _add_device(self) -> None:
        name, accepted = self._qt.QInputDialog.getText(
            self._dialog,
            "添加设备",
            "设备名称（1 至 128 个可见字符）",
        )
        if accepted:
            self._presenter.add_device(name)
        self._render(self._presenter.view())

    def _confirm(self, title: str, message: str) -> bool:
        message_box = self._qt.QMessageBox
        yes = _message_button(message_box, "Yes")
        no = _message_button(message_box, "No")
        answer = message_box.question(self._dialog, title, message, yes, no)
        return answer == yes

    def _allow_rebind(self) -> None:
        if self._confirm(
            "允许重新绑定", "下次使用固定配对码时，此名额可绑定另一台设备。继续？"
        ):
            self._presenter.allow_rebind_selected()
        self._render(self._presenter.view())

    def _reset_pairing_code(self) -> None:
        if self._confirm(
            "重置配对码",
            "旧配对码和未完成的配对尝试会失效；已绑定设备的连接保持不变。继续？",
        ):
            self._presenter.reset_pairing_code_selected()
        self._render(self._presenter.view())

    def _rename_device(self) -> None:
        state = self._presenter.view()
        selected = next(
            (item for item in state.devices if item.slot_id == state.selected_slot_id),
            None,
        )
        if selected is not None:
            name, accepted = self._qt.QInputDialog.getText(
                self._dialog,
                "修改设备名称",
                "设备名称（1 至 128 个可见字符）",
                text=selected.name,
            )
            if accepted:
                self._presenter.rename_selected_device(name)
        self._render(self._presenter.view())

    def _remove_device(self) -> None:
        if self._confirm(
            "确认移除设备",
            "移除会撤销该设备的连接凭据和会话，但保留既有同步与评分历史。是否继续？",
        ):
            self._presenter.remove_selected_device()
        self._render(self._presenter.view())

    def _device_selection_changed(self) -> None:
        item = self._device_list.currentItem()
        role = _enum_value(self._qt.Qt, "UserRole", 32)
        slot_id = item.data(role) if item is not None else None
        self._presenter.select_device(slot_id)
        self._render(self._presenter.view())

    def _show_removed_toggled(self, enabled: bool) -> None:
        self._render(self._presenter.set_show_removed(enabled))

    def _set_show_removed_checkbox(self, enabled: bool) -> None:
        blocker = getattr(self._show_removed_checkbox, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        self._show_removed_checkbox.setChecked(enabled)
        if callable(blocker):
            blocker(False)

    def _render(self, state: ViewState) -> None:
        self._pairing_label.setText(state.pairing_message)
        self._message_label.setText(
            "\n".join(
                item for item in (state.config_message, state.error_message) if item
            )
        )
        for button, enabled in (
            (self._refresh_button, state.buttons.refresh),
            (self._add_device_button, state.buttons.add_device),
            (self._allow_rebind_button, state.buttons.allow_rebind),
            (self._reset_pairing_button, state.buttons.reset_pairing_code),
            (self._remove_device_button, state.buttons.remove_device),
            (self._rename_device_button, state.buttons.rename_device),
        ):
            button.setEnabled(enabled)
        self._set_show_removed_checkbox(state.show_removed)

        signature = tuple((item.slot_id, item.label) for item in state.devices)
        if signature != self._device_signature:
            self._device_signature = signature
            blocker = getattr(self._device_list, "blockSignals", None)
            if callable(blocker):
                blocker(True)
            self._device_list.clear()
            role = _enum_value(self._qt.Qt, "UserRole", 32)
            for device in state.devices:
                item = self._qt.QListWidgetItem(device.label)
                item.setData(role, device.slot_id)
                self._device_list.addItem(item)
            if callable(blocker):
                blocker(False)
        selected_index = next(
            (
                index
                for index, item in enumerate(state.devices)
                if item.slot_id == state.selected_slot_id
            ),
            -1,
        )
        blocker = getattr(self._device_list, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        self._device_list.setCurrentRow(selected_index)
        if callable(blocker):
            blocker(False)

    def _on_finished(self, _result: Any = None) -> None:
        self._closed = True

    def show(self) -> None:
        self._closed = False
        self._presenter.reopen()
        self._dialog.show()
        raise_window = getattr(self._dialog, "raise_", None)
        if callable(raise_window):
            raise_window()
        activate = getattr(self._dialog, "activateWindow", None)
        if callable(activate):
            activate()
        self._refresh()

    def close(self) -> None:
        self._closed = True
        self._dialog.close()

    def is_visible(self) -> bool:
        visible = getattr(self._dialog, "isVisible", None)
        return bool(visible()) if callable(visible) else False

    def dispose(self) -> None:
        self.close()
        delete_later = getattr(self._dialog, "deleteLater", None)
        if callable(delete_later):
            delete_later()


__all__ = ["DeviceManagementDialog"]
