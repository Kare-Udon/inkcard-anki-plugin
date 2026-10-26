"""Anki 原生 Qt 设置与批准恢复辅助窗口。"""

from __future__ import annotations

from typing import Any, Callable

from ..logging_control import DEFAULT_LOGGING_POLICY, normalize_logging_policy
from .presenter import InkCardsPresenter, ViewState


class SettingsDialog:
    """局域网、日志和批准恢复设置；窗口关闭不会停止 runtime。"""

    def __init__(
        self,
        *,
        parent: Any,
        runtime_provider: Callable[[], Any],
        config_writer: Callable[[bool], Any] | None = None,
        logging_policy_provider: Callable[[], Any] | None = None,
        logging_policy_writer: Callable[[Any], Any] | None = None,
        qt_module: Any = None,
        presenter: InkCardsPresenter | None = None,
    ) -> None:
        self._qt = qt_module or self._load_qt()
        self._presenter = presenter or InkCardsPresenter(
            runtime_provider,
            config_writer=config_writer,
        )
        try:
            policy = (
                logging_policy_provider()
                if logging_policy_provider is not None
                else None
            )
        except Exception:
            policy = None
        self._logging_policy = normalize_logging_policy(
            policy or DEFAULT_LOGGING_POLICY
        )
        self._dialog = self._qt.QDialog(parent)
        self._dialog.setWindowTitle("InkCards · 设置")
        set_modal = getattr(self._dialog, "setModal", None)
        if callable(set_modal):
            set_modal(False)
        self._logging_policy_writer = logging_policy_writer
        self._cleaned = False
        self._build_widgets()
        finished = getattr(self._dialog, "finished", None)
        if finished is not None and callable(getattr(finished, "connect", None)):
            finished.connect(self._on_finished)

    @staticmethod
    def _load_qt() -> Any:
        from aqt import qt  # type: ignore[import-not-found]

        return qt

    @property
    def dialog(self) -> Any:
        return self._dialog

    @property
    def presenter(self) -> InkCardsPresenter:
        return self._presenter

    def _build_widgets(self) -> None:
        qt = self._qt
        root = qt.QVBoxLayout(self._dialog)
        root.addWidget(qt.QLabel("设置"))

        self._service_label = qt.QLabel()
        self._service_label.setWordWrap(True)
        root.addWidget(self._service_label)
        self._lan_notice = qt.QLabel()
        self._lan_notice.setWordWrap(True)
        root.addWidget(self._lan_notice)
        self._lan_checkbox = qt.QCheckBox("允许局域网设备连接")
        root.addWidget(self._lan_checkbox)

        logging_row = qt.QHBoxLayout()
        logging_row.addWidget(qt.QLabel("日志详细度"))
        self._logging_level = qt.QComboBox()
        for label, level in (
            ("关闭", "off"),
            ("仅错误", "error"),
            ("警告及错误", "warn"),
            ("常规", "info"),
            ("详细", "debug"),
        ):
            self._logging_level.addItem(label, level)
        self._logging_level.setCurrentIndex(
            ("off", "error", "warn", "info", "debug").index(
                self._logging_policy["level"]
            )
        )
        self._logging_console = qt.QCheckBox("输出到系统控制台")
        self._logging_console.setChecked(self._logging_policy["console"])
        self._logging_level.setEnabled(self._logging_policy_writer is not None)
        self._logging_console.setEnabled(self._logging_policy_writer is not None)
        logging_row.addWidget(self._logging_level)
        logging_row.addWidget(self._logging_console)
        root.addLayout(logging_row)
        self._logging_message = qt.QLabel()
        self._logging_message.setWordWrap(True)
        root.addWidget(self._logging_message)

        self._recovery_button = qt.QPushButton("恢复已批准的评分")
        root.addWidget(self._recovery_button)
        self._message_label = qt.QLabel()
        self._message_label.setWordWrap(True)
        root.addWidget(self._message_label)

        self._lan_checkbox.toggled.connect(self._lan_toggled)
        self._logging_level.currentIndexChanged.connect(self._logging_changed)
        self._logging_console.toggled.connect(self._logging_changed)
        self._recovery_button.clicked.connect(self._request_approved_recovery)
        self._timer = qt.QTimer(self._dialog)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._render(self._presenter.view())

    def _set_lan_checkbox(self, enabled: bool) -> None:
        blocker = getattr(self._lan_checkbox, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        self._lan_checkbox.setChecked(enabled)
        if callable(blocker):
            blocker(False)

    def _lan_toggled(self, enabled: bool) -> None:
        self._presenter.set_lan_enabled(enabled)
        self._render(self._presenter.view())

    def _logging_changed(self, *_args: Any) -> None:
        writer = self._logging_policy_writer
        if writer is None:
            self._restore_logging_controls()
            return
        selected = normalize_logging_policy(
            {
                "level": self._logging_level.currentData(),
                "console": self._logging_console.isChecked(),
            }
        )
        try:
            writer(selected)
        except Exception:
            self._restore_logging_controls()
            self._logging_message.setText("日志设置未保存，已保留原值。")
            return
        self._logging_policy = selected
        self._logging_message.setText("日志设置已保存并立即生效。")

    def _restore_logging_controls(self) -> None:
        level_index = ("off", "error", "warn", "info", "debug").index(
            self._logging_policy["level"]
        )
        for widget, setter, value in (
            (self._logging_level, "setCurrentIndex", level_index),
            (self._logging_console, "setChecked", self._logging_policy["console"]),
        ):
            blocker = getattr(widget, "blockSignals", None)
            if callable(blocker):
                blocker(True)
            getattr(widget, setter)(value)
            if callable(blocker):
                blocker(False)

    def _request_approved_recovery(self) -> None:
        self._presenter.request_approved_recovery()
        self._render(self._presenter.view())

    def _poll(self) -> None:
        self._render(self._presenter.poll())

    def _render(self, state: ViewState) -> None:
        self._service_label.setText(state.sync_overview.headline)
        self._lan_notice.setText(state.status.lan_notice)
        self._set_lan_checkbox(state.status.lan_enabled)
        self._lan_checkbox.setEnabled(state.buttons.lan)
        self._recovery_button.setEnabled(state.buttons.recovery)
        self._message_label.setText(
            "\n".join(
                item
                for item in (
                    state.config_message,
                    state.error_message,
                    state.recovery_message,
                )
                if item
            )
        )

    def _on_finished(self, _result: Any = None) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        if self._timer.isActive():
            self._timer.stop()

    def show(self) -> None:
        self._cleaned = False
        self._presenter.reopen()
        self._render(self._presenter.refresh_status())
        self._dialog.show()
        raise_window = getattr(self._dialog, "raise_", None)
        if callable(raise_window):
            raise_window()
        activate = getattr(self._dialog, "activateWindow", None)
        if callable(activate):
            activate()
        self._timer.start()

    def close(self) -> None:
        self._cleanup()
        self._dialog.close()

    def dispose(self) -> None:
        self.close()
        delete_later = getattr(self._dialog, "deleteLater", None)
        if callable(delete_later):
            delete_later()


__all__ = ["SettingsDialog"]
