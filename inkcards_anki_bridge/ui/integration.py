"""Anki Tools 菜单的最小、幂等 UI 接线。

模块顶层不加载 Qt；只有 ``register_tools_action()`` 被 Anki 宿主调用时才取得
``aqt.qt``。对话框实例挂在 ``mw`` 上并复用，卸载时只移除 action/对话框，不停止
runtime 本身（runtime 的关闭仍由 lifecycle controller 负责）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


MENU_LABEL = "InkCards…"
_REGISTRATION_ATTR = "_inkcards_tools_ui_registration"
_ACTION_REGISTRATION_ATTR = "_inkcards_tools_ui_registration"


def _load_qt() -> Any:
    from aqt import qt  # type: ignore[import-not-found]

    return qt


def _menu_actions(menu: Any) -> tuple[Any, ...]:
    reader = getattr(menu, "actions", None)
    if not callable(reader):
        return ()
    try:
        value = reader()
    except Exception:
        return ()
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _action_text(action: Any) -> str:
    value = getattr(action, "text", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            return ""
    return value if isinstance(value, str) else ""


class ToolsUiRegistration:
    def __init__(
        self,
        *,
        main_window: Any,
        action: Any,
        runtime_provider: Callable[[], Any],
        config_writer: Optional[Callable[[bool], Any]],
        dialog_factory: Optional[Callable[..., Any]],
        qt_module: Any,
    ) -> None:
        self.main_window = main_window
        self.action = action
        self._runtime_provider = runtime_provider
        self._config_writer = config_writer
        self._dialog_factory = dialog_factory
        self._qt = qt_module
        self._dialog: Any = None
        self._closed = False

    @property
    def dialog(self) -> Any:
        return self._dialog

    def show(self) -> None:
        self._closed = False
        if self._dialog is None:
            factory = self._dialog_factory
            if factory is None:
                from .dialog import InkCardsDialog

                factory = InkCardsDialog
            self._dialog = factory(
                parent=self.main_window,
                runtime_provider=self._runtime_provider,
                config_writer=self._config_writer,
                qt_module=self._qt,
            )
        self._dialog.show()

    def close(self) -> None:
        self._closed = True
        if self._dialog is not None:
            close = getattr(self._dialog, "dispose", None)
            if not callable(close):
                close = getattr(self._dialog, "close", None)
            if callable(close):
                close()
            self._dialog = None

    def unregister(self) -> None:
        self.close()
        menu = getattr(getattr(self.main_window, "form", None), "menuTools", None)
        remover = getattr(menu, "removeAction", None)
        if callable(remover):
            try:
                remover(self.action)
            except Exception:
                pass
        try:
            delattr(self.main_window, _REGISTRATION_ATTR)
        except AttributeError:
            pass


def register_tools_action(
    *,
    aqt_module: Any,
    runtime_provider: Callable[[], Any],
    config_writer: Optional[Callable[[bool], Any]] = None,
    qt_module: Any = None,
    dialog_factory: Optional[Callable[..., Any]] = None,
) -> Optional[ToolsUiRegistration]:
    """在 ``mw.form.menuTools`` 只注册一个可复用的 InkCards action。"""

    main_window = getattr(aqt_module, "mw", None)
    if main_window is None:
        return None
    existing = getattr(main_window, _REGISTRATION_ATTR, None)
    if isinstance(existing, ToolsUiRegistration):
        return existing
    menu = getattr(getattr(main_window, "form", None), "menuTools", None)
    if menu is None:
        return None
    qt = qt_module or _load_qt()
    action = None
    for candidate in _menu_actions(menu):
        if _action_text(candidate) != MENU_LABEL:
            continue
        action = candidate
        action_registration = getattr(candidate, _ACTION_REGISTRATION_ATTR, None)
        if isinstance(action_registration, ToolsUiRegistration):
            setattr(main_window, _REGISTRATION_ATTR, action_registration)
            return action_registration
        break
    if action is None:
        action = qt.QAction(MENU_LABEL, main_window)
        menu.addAction(action)

    registration = ToolsUiRegistration(
        main_window=main_window,
        action=action,
        runtime_provider=runtime_provider,
        config_writer=config_writer,
        dialog_factory=dialog_factory,
        qt_module=qt,
    )
    triggered = getattr(action, "triggered", None)
    connect = getattr(triggered, "connect", None)
    if callable(connect) and not bool(getattr(action, "_inkcards_trigger_connected", False)):
        # QAction.triggered 在不同 Qt 绑定中可能携带 checked(bool) 参数。
        connect(lambda *_args: registration.show())
        setattr(action, "_inkcards_trigger_connected", True)
    setattr(action, _ACTION_REGISTRATION_ATTR, registration)
    setattr(main_window, _REGISTRATION_ATTR, registration)
    return registration


def unregister_tools_action(aqt_module: Any) -> None:
    main_window = getattr(aqt_module, "mw", None)
    registration = getattr(main_window, _REGISTRATION_ATTR, None)
    if isinstance(registration, ToolsUiRegistration):
        registration.unregister()


__all__ = [
    "MENU_LABEL",
    "ToolsUiRegistration",
    "register_tools_action",
    "unregister_tools_action",
]
