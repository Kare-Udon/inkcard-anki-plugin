"""Anki 原生 Qt 对话框。

本文件不在模块导入阶段加载 Qt。``InkCardsDialog`` 只是一个小型 wrapper，真正的
QDialog/QTimer/QWidget 类型由 ``_load_qt()`` 在 Anki 菜单 action 第一次注册时取得，
因此普通 Python 导入 presenter/plugin 不会触碰 aqt/PyQt。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .presenter import InkCardsPresenter, ViewState, is_valid_device_id


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


class InkCardsDialog:
    """可复用的非模态 QDialog；关闭时只取消 UI 作业，不停止 runtime。"""

    def __init__(
        self,
        *,
        parent: Any,
        runtime_provider: Callable[[], Any],
        config_writer: Optional[Callable[[bool], Any]] = None,
        qt_module: Any = None,
        presenter: InkCardsPresenter | None = None,
    ) -> None:
        self._qt = qt_module or _load_qt()
        self._presenter = presenter or InkCardsPresenter(
            runtime_provider,
            config_writer=config_writer,
        )
        self._dialog = self._qt.QDialog(parent)
        self._dialog.setWindowTitle("InkCards")
        set_modal = getattr(self._dialog, "setModal", None)
        if callable(set_modal):
            set_modal(False)
        self._cleaned = False
        self._catalog_ids: tuple[str, ...] = ()
        self._build_widgets()
        finished = getattr(self._dialog, "finished", None)
        if finished is not None and callable(getattr(finished, "connect", None)):
            finished.connect(self._on_finished)

    @property
    def presenter(self) -> InkCardsPresenter:
        return self._presenter

    @property
    def action_parent(self) -> Any:
        return self._dialog

    @property
    def dialog(self) -> Any:
        return self._dialog

    def _build_widgets(self) -> None:
        qt = self._qt
        root = qt.QVBoxLayout(self._dialog)
        self._status_label = qt.QLabel()
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        top = qt.QHBoxLayout()
        self._refresh_button = qt.QPushButton("刷新")
        self._pair_button = qt.QPushButton("生成配对码")
        self._recovery_button = qt.QPushButton("恢复已批准的评分")
        top.addWidget(self._refresh_button)
        top.addWidget(self._pair_button)
        top.addWidget(self._recovery_button)
        root.addLayout(top)

        self._pairing_label = qt.QLabel()
        self._pairing_label.setWordWrap(True)
        root.addWidget(self._pairing_label)

        revoke_row = qt.QHBoxLayout()
        self._device_input = qt.QLineEdit()
        self._device_input.setPlaceholderText("设备 ID")
        self._revoke_button = qt.QPushButton("撤销设备")
        revoke_row.addWidget(self._device_input)
        revoke_row.addWidget(self._revoke_button)
        root.addLayout(revoke_row)

        self._lan_checkbox = qt.QCheckBox("允许局域网设备连接")
        root.addWidget(self._lan_checkbox)

        self._deck_list = qt.QListWidget()
        root.addWidget(self._deck_list)

        deck_actions = qt.QHBoxLayout()
        self._preflight_button = qt.QPushButton("预检所选牌组")
        self._snapshot_button = qt.QPushButton("准备快照")
        self._cancel_button = qt.QPushButton("取消当前作业")
        deck_actions.addWidget(self._preflight_button)
        deck_actions.addWidget(self._snapshot_button)
        deck_actions.addWidget(self._cancel_button)
        root.addLayout(deck_actions)

        self._preflight_label = qt.QLabel()
        self._preflight_label.setWordWrap(True)
        self._snapshot_label = qt.QLabel()
        self._snapshot_label.setWordWrap(True)
        self._message_label = qt.QLabel()
        self._message_label.setWordWrap(True)
        root.addWidget(self._preflight_label)
        root.addWidget(self._snapshot_label)
        root.addWidget(self._message_label)

        self._refresh_button.clicked.connect(self._refresh)
        self._pair_button.clicked.connect(self._issue_pairing_code)
        self._recovery_button.clicked.connect(self._request_approved_recovery)
        self._revoke_button.clicked.connect(self._revoke_device)
        self._preflight_button.clicked.connect(self._request_preflight)
        self._snapshot_button.clicked.connect(self._request_snapshot)
        self._cancel_button.clicked.connect(self._cancel_active)
        self._deck_list.itemChanged.connect(self._selection_changed)
        self._lan_checkbox.toggled.connect(self._lan_toggled)

        self._timer = qt.QTimer(self._dialog)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._poll)
        self._render(self._presenter.view())

    def _refresh(self) -> None:
        self._render(self._presenter.refresh())

    def _issue_pairing_code(self) -> None:
        self._presenter.issue_pairing_code()
        self._render(self._presenter.view())

    def _request_approved_recovery(self) -> None:
        self._presenter.request_approved_recovery()
        self._render(self._presenter.view())

    def _revoke_device(self) -> None:
        device_id = self._device_input.text()
        if not is_valid_device_id(device_id):
            self._presenter.revoke_device(device_id)
            self._render(self._presenter.view())
            return
        message_box = self._qt.QMessageBox
        yes = _message_button(message_box, "Yes")
        no = _message_button(message_box, "No")
        answer = message_box.question(
            self._dialog,
            "确认撤销",
            "撤销后该设备将不能继续认证，是否继续？",
            yes,
            no,
        )
        if answer == yes:
            self._presenter.revoke_device(device_id)
        self._render(self._presenter.view())

    def _request_preflight(self) -> None:
        self._presenter.request_preflight()
        self._render(self._presenter.view())

    def _request_snapshot(self) -> None:
        self._presenter.request_snapshot()
        self._render(self._presenter.view())

    def _cancel_active(self) -> None:
        self._presenter.cancel_active()
        self._render(self._presenter.view())

    def _selection_changed(self, _item: Any) -> None:
        selected = []
        count = self._deck_list.count()
        checked = _enum_value(self._qt.Qt, "Checked", None)
        for index in range(count):
            item = self._deck_list.item(index)
            if item.checkState() == checked:
                role = _enum_value(self._qt.Qt, "UserRole", 32)
                value = item.data(role)
                if value is not None:
                    selected.append(str(value))
        self._presenter.select_decks(selected)
        self._render(self._presenter.view())

    def _lan_toggled(self, enabled: bool) -> None:
        if self._presenter.set_lan_enabled(enabled):
            self._show_information(self._presenter.view().config_message)
        else:
            self._set_lan_checkbox(self._presenter.view().status.lan_enabled)
        self._render(self._presenter.view())

    def _show_information(self, text: str) -> None:
        if not text:
            return
        information = getattr(self._qt.QMessageBox, "information", None)
        if callable(information):
            information(self._dialog, "InkCards", text)

    def _set_lan_checkbox(self, enabled: bool) -> None:
        blocker = getattr(self._lan_checkbox, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        self._lan_checkbox.setChecked(enabled)
        if callable(blocker):
            blocker(False)

    def _poll(self) -> None:
        self._render(self._presenter.poll())

    def _render(self, state: ViewState) -> None:
        status = state.status
        delivery = "、".join(f"{item.label}{item.count}" for item in status.delivery)
        application = "、".join(f"{item.label}{item.count}" for item in status.application)
        self._status_label.setText(
            "\n".join(
                (
                    status.service_text,
                    f"监听：{status.host_text}:{status.port_text}",
                    f"设备：{status.devices}；事件：{status.events}；冲突：{status.conflicts}",
                    f"交付：{delivery}",
                    f"应用：{application}",
                    f"快照：{status.snapshots}；generation：{status.generation_text}",
                    f"牌组目录：{status.catalog_text}",
                    f"范围预检：{status.preflight_text}",
                    f"写回状态：{status.write_disabled_reason}",
                    status.lan_notice,
                )
            )
        )
        self._pairing_label.setText(state.pairing_message)
        self._preflight_label.setText(state.preflight_message or status.preflight_text)
        self._snapshot_label.setText(state.snapshot_message)
        messages = "\n".join(item for item in (state.config_message, state.error_message, state.recovery_message) if item)
        self._message_label.setText(messages)
        self._pairing_button_set_enabled(state.buttons.pairing)
        self._revoke_button.setEnabled(state.buttons.revoke)
        self._preflight_button.setEnabled(state.buttons.preflight)
        self._snapshot_button.setEnabled(state.buttons.snapshot)
        self._cancel_button.setEnabled(state.buttons.cancel)
        self._refresh_button.setEnabled(state.buttons.refresh)
        self._recovery_button.setEnabled(state.buttons.recovery)
        self._lan_checkbox.setEnabled(state.buttons.lan)
        self._set_lan_checkbox(status.lan_enabled)

        ids = tuple(item.deck_id for item in state.decks)
        if ids != self._catalog_ids:
            self._catalog_ids = ids
            blocker = getattr(self._deck_list, "blockSignals", None)
            if callable(blocker):
                blocker(True)
            self._deck_list.clear()
            role = _enum_value(self._qt.Qt, "UserRole", 32)
            unchecked = _enum_value(self._qt.Qt, "Unchecked", 0)
            user_checkable = _enum_value(self._qt.Qt, "ItemIsUserCheckable", 16)
            for deck in state.decks:
                item = self._qt.QListWidgetItem(("  " * deck.depth) + deck.label)
                item.setData(role, deck.deck_id)
                item.setFlags(item.flags() | user_checkable)
                item.setCheckState(
                    _enum_value(self._qt.Qt, "Checked", 2)
                    if deck.deck_id in state.selected_ids
                    else unchecked
                )
                self._deck_list.addItem(item)
            if callable(blocker):
                blocker(False)

    def _pairing_button_set_enabled(self, enabled: bool) -> None:
        self._pair_button.setEnabled(enabled)

    def _on_finished(self, _result: Any = None) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        if self._timer.isActive():
            self._timer.stop()
        self._presenter.close()

    def show(self) -> None:
        self._cleaned = False
        self._presenter.reopen()
        self._dialog.show()
        raise_window = getattr(self._dialog, "raise_", None)
        if callable(raise_window):
            raise_window()
        activate = getattr(self._dialog, "activateWindow", None)
        if callable(activate):
            activate()
        self._timer.start()
        self._refresh()

    def close(self) -> None:
        self._cleanup()
        self._dialog.close()

    def is_visible(self) -> bool:
        visible = getattr(self._dialog, "isVisible", None)
        return bool(visible()) if callable(visible) else False

    def dispose(self) -> None:
        self.close()
        delete_later = getattr(self._dialog, "deleteLater", None)
        if callable(delete_later):
            delete_later()


__all__ = ["InkCardsDialog"]
