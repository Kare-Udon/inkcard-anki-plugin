"""Anki profile/add-on 生命周期控制；不直接依赖 ``aqt``。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any, Protocol


class RuntimeLike(Protocol):
    @property
    def running(self) -> bool:
        ...

    def stop(self) -> None:
        ...


class HookLike(Protocol):
    def append(self, callback: Callable[..., Any]) -> None:
        ...

    def remove(self, callback: Callable[..., Any]) -> None:
        ...


class LifecycleController:
    """把宿主 hook 映射到可测试、幂等的 runtime 生命周期。"""

    def __init__(
        self,
        *,
        gui_hooks: Any,
        runtime_factory: Callable[[], RuntimeLike],
        profile_provider: Callable[[], str],
        addon_id_provider: Callable[[], str | None] | None = None,
        logger: Callable[[str], None] = print,
        ui_cleanup: Callable[[], None] | None = None,
    ) -> None:
        self._gui_hooks = gui_hooks
        self._runtime_factory = runtime_factory
        self._profile_provider = profile_provider
        self._addon_id_provider = addon_id_provider or (lambda: None)
        self._logger = logger
        self._lock = threading.RLock()
        self._registered = False
        self._enabled = True
        self._runtime: RuntimeLike | None = None
        self._runtime_profile: str | None = None
        self._ui_cleanup = ui_cleanup

    @property
    def registered(self) -> bool:
        with self._lock:
            return self._registered

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def runtime(self) -> RuntimeLike | None:
        with self._lock:
            return self._runtime

    def set_ui_cleanup(self, cleanup: Callable[[], None] | None) -> None:
        """设置卸载时的窄 UI 清理回调；不触碰 runtime 生命周期。"""

        if cleanup is not None and not callable(cleanup):
            raise TypeError("cleanup must be callable or None")
        with self._lock:
            self._ui_cleanup = cleanup

    def register(self) -> None:
        """注册一次；重复调用不会重复追加同一个 callback。"""

        with self._lock:
            if self._registered:
                return
            required = (
                ("profile_did_open", self._on_profile_did_open),
                ("profile_will_close", self._on_profile_will_close),
            )
            optional = (("addons_dialog_will_delete_addons", self._on_addon_will_delete),
                        ("operation_did_execute", self._on_operation_did_execute))
            added: list[tuple[HookLike, Callable[..., Any]]] = []
            try:
                for name, callback in (*required, *optional):
                    hook = getattr(self._gui_hooks, name, None)
                    if hook is None:
                        if (name, callback) in required:
                            raise RuntimeError(f"aqt.gui_hooks.{name} is unavailable")
                        continue
                    hook.append(callback)
                    added.append((hook, callback))
            except Exception:
                for hook, callback in reversed(added):
                    try:
                        hook.remove(callback)
                    except Exception:
                        pass
                raise
            self._registered = True

    def unregister(self) -> None:
        with self._lock:
            if not self._registered:
                return
            callbacks = (
                ("profile_did_open", self._on_profile_did_open),
                ("profile_will_close", self._on_profile_will_close),
                ("addons_dialog_will_delete_addons", self._on_addon_will_delete),
                ("operation_did_execute", self._on_operation_did_execute),
            )
            self._registered = False
        for name, callback in callbacks:
            hook = getattr(self._gui_hooks, name, None)
            if hook is None:
                continue
            try:
                hook.remove(callback)
            except Exception as exc:
                self._report("unregister hook", exc)

    def shutdown(self) -> None:
        """插件卸载/显式退出入口；停止 runtime 并解除所有 hook。"""

        with self._lock:
            self._enabled = False
            runtime = self._detach_runtime_locked()
            ui_cleanup = self._ui_cleanup
            self._ui_cleanup = None
        self._safe_stop(runtime, "shutdown")
        self.unregister()
        if ui_cleanup is not None:
            try:
                ui_cleanup()
            except Exception as exc:
                self._report("ui cleanup", exc)

    def _on_profile_did_open(self) -> None:
        try:
            self._profile_did_open()
        except Exception as exc:
            # hook 异常不能回传到 Anki；尤其不能把 token/stack trace 写入协议。
            self._report("profile_did_open", exc)

    def _on_profile_will_close(self) -> None:
        try:
            self._profile_will_close()
        except Exception as exc:
            self._report("profile_will_close", exc)

    def _on_operation_did_execute(self, changes: Any, initiator: Any) -> None:
        from .anki.operations import CompactOperationInitiator
        # compact 收据已在原子 Store 事务推进 generation；旧 profile 的迟到回调也不推进新 profile。
        if isinstance(initiator, CompactOperationInitiator):
            return
        relevant = ("card", "note", "deck", "notetype", "config", "deck_config", "study_queues")
        if not any(getattr(changes, key, False) is True for key in relevant):
            return
        with self._lock:
            runtime, profile = self._runtime, self._runtime_profile
        try:
            if runtime is None or not self._is_running(runtime) or self._profile_provider() != profile:
                return
            observer = getattr(runtime, "note_external_collection_change", None)
            if callable(observer):
                observer()
        except Exception as exc:
            self._report("external collection change", exc)

    def _on_addon_will_delete(self, _dialog: Any, ids: Sequence[str]) -> None:
        try:
            addon_id = self._addon_id_provider()
            if addon_id is not None and any(str(item) == addon_id for item in ids):
                self.shutdown()
        except Exception as exc:
            self._report("addons_dialog_will_delete_addons", exc)

    def _profile_did_open(self) -> None:
        with self._lock:
            if not self._enabled:
                return
        profile = self._profile_provider()
        if not isinstance(profile, str) or not profile:
            raise RuntimeError("Anki profile is not ready")

        with self._lock:
            current = self._runtime
            current_profile = self._runtime_profile
        if current is not None:
            if current_profile == profile and self._is_running(current):
                return
            self._safe_stop(current, "profile replacement")
            with self._lock:
                if self._runtime is current:
                    self._runtime = None
                    self._runtime_profile = None

        runtime: RuntimeLike | None = None
        try:
            runtime = self._runtime_factory()
            if runtime is None or not self._is_running(runtime):
                raise RuntimeError("runtime factory did not return a running add-on runtime")
        except Exception as exc:
            self._safe_stop(runtime, "failed startup")
            with self._lock:
                self._runtime = None
                self._runtime_profile = None
            self._report("profile_did_open startup", exc)
            return

        with self._lock:
            if not self._enabled:
                discard = True
            else:
                self._runtime = runtime
                self._runtime_profile = profile
                discard = False
        if discard:
            self._safe_stop(runtime, "startup raced with shutdown")

    def _profile_will_close(self) -> None:
        with self._lock:
            runtime = self._detach_runtime_locked()
        self._safe_stop(runtime, "profile close")

    def _detach_runtime_locked(self) -> RuntimeLike | None:
        runtime = self._runtime
        self._runtime = None
        self._runtime_profile = None
        return runtime

    @staticmethod
    def _is_running(runtime: RuntimeLike) -> bool:
        try:
            return bool(runtime.running)
        except Exception:
            return False

    def _safe_stop(self, runtime: RuntimeLike | None, reason: str) -> None:
        if runtime is None:
            return
        try:
            runtime.stop()
        except Exception as exc:
            self._report(f"stop runtime ({reason})", exc)

    def _report(self, action: str, exc: Exception) -> None:
        try:
            self._logger(f"[InkCards] {action} failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
