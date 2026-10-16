from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.lifecycle import LifecycleController
from inkcards_anki_bridge.plugin import register_in_anki


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


class FakeRuntime:
    def __init__(self, instance_id: str, *, running: bool = True) -> None:
        self.instance_id = instance_id
        self.running = running
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


class LifecycleTests(unittest.TestCase):
    def test_external_changes_advance_current_runtime_and_own_late_callbacks_do_not(self) -> None:
        from inkcards_anki_bridge.anki.operations import CompactOperationInitiator
        self.hooks.operation_did_execute = FakeHook()
        self.controller.register()
        self.hooks.profile_did_open.emit()
        runtime = self.created[0]
        generations = []
        runtime.note_external_collection_change = lambda: generations.append(len(generations) + 1)
        hook = self.hooks.operation_did_execute
        hook.emit(SimpleNamespace(card=True), None)
        self.assertEqual(generations, [1])
        hook.emit(SimpleNamespace(card=True), CompactOperationInitiator('old-profile', 'old-epoch'))
        hook.emit(SimpleNamespace(card=False), None)
        self.profile = 'Profile B'
        hook.emit(SimpleNamespace(card=True), None)
        self.assertEqual(generations, [1])
        self.hooks.profile_will_close.emit()
        hook.emit(SimpleNamespace(card=True), None)
        self.assertEqual(generations, [1])
        self.controller.unregister()
        self.assertEqual(hook.callbacks, [])

    def setUp(self) -> None:
        self.hooks = FakeGuiHooks()
        self.profile = "Profile A"
        self.created: list[FakeRuntime] = []
        self.logs: list[str] = []

        def factory() -> FakeRuntime:
            runtime = FakeRuntime(f"instance-{len(self.created) + 1}")
            self.created.append(runtime)
            return runtime

        self.controller = LifecycleController(
            gui_hooks=self.hooks,
            runtime_factory=factory,
            profile_provider=lambda: self.profile,
            addon_id_provider=lambda: "inkcards_anki_bridge",
            logger=self.logs.append,
        )

    def test_first_open_duplicate_open_and_idempotent_registration(self) -> None:
        self.controller.register()
        self.controller.register()
        self.assertEqual(len(self.hooks.profile_did_open.callbacks), 1)
        self.assertEqual(len(self.hooks.profile_will_close.callbacks), 1)

        self.hooks.profile_did_open.emit()
        first = self.controller.runtime
        self.hooks.profile_did_open.emit()
        self.assertIs(self.controller.runtime, first)
        self.assertEqual(len(self.created), 1)

    def test_profile_switch_stops_old_instance_before_new_start(self) -> None:
        self.controller.register()
        self.hooks.profile_did_open.emit()
        first = self.created[0]

        self.profile = "Profile B"
        self.hooks.profile_will_close.emit()
        self.hooks.profile_did_open.emit()

        self.assertEqual(first.stop_calls, 1)
        self.assertEqual(len(self.created), 2)
        self.assertNotEqual(self.created[0].instance_id, self.created[1].instance_id)
        self.assertIs(self.controller.runtime, self.created[1])

    def test_repeated_profile_open_with_changed_identity_replaces_stale_runtime(self) -> None:
        self.controller.register()
        self.hooks.profile_did_open.emit()
        first = self.created[0]
        self.profile = "Profile B"
        self.hooks.profile_did_open.emit()
        self.assertEqual(first.stop_calls, 1)
        self.assertEqual(len(self.created), 2)

    def test_close_is_idempotent(self) -> None:
        self.controller.register()
        self.hooks.profile_did_open.emit()
        runtime = self.created[0]
        self.hooks.profile_will_close.emit()
        self.hooks.profile_will_close.emit()
        self.assertEqual(runtime.stop_calls, 1)
        self.assertIsNone(self.controller.runtime)

    def test_start_failure_stops_partial_runtime_and_keeps_hooks_alive(self) -> None:
        failed = FakeRuntime("failed", running=False)

        def failing_factory() -> FakeRuntime:
            return failed

        controller = LifecycleController(
            gui_hooks=self.hooks,
            runtime_factory=failing_factory,
            profile_provider=lambda: self.profile,
            logger=self.logs.append,
        )
        controller.register()
        self.hooks.profile_did_open.emit()
        self.assertEqual(failed.stop_calls, 1)
        self.assertIsNone(controller.runtime)
        self.assertTrue(controller.registered)
        self.assertTrue(any("startup" in message for message in self.logs))

    def test_addon_unload_and_exit_stop_runtime_and_unregister_hooks(self) -> None:
        self.controller.register()
        self.hooks.profile_did_open.emit()
        runtime = self.created[0]
        self.hooks.addons_dialog_will_delete_addons.emit(None, ["inkcards_anki_bridge"])
        self.assertEqual(runtime.stop_calls, 1)
        self.assertFalse(self.controller.enabled)
        self.assertFalse(self.controller.registered)
        self.assertEqual(len(self.hooks.profile_did_open.callbacks), 0)

        # 显式 shutdown 与 profile/Anki 退出重复到达时仍不产生第二次 stop。
        self.controller.shutdown()
        self.hooks.profile_will_close.emit()
        self.assertEqual(runtime.stop_calls, 1)

    def test_plugin_registration_is_hook_only_and_idempotent(self) -> None:
        main_window = SimpleNamespace(
            pm=SimpleNamespace(
                name="Profile A",
                profileFolder=lambda: "/tmp/inkcards-fake-profile-a",
            ),
            addonManager=SimpleNamespace(addonFromModule=lambda _name: "inkcards_anki_bridge"),
        )
        fake_aqt = SimpleNamespace(mw=main_window, gui_hooks=self.hooks)
        runtime = FakeRuntime("plugin-instance")
        with patch("inkcards_anki_bridge.plugin.start_in_anki", return_value=runtime) as start:
            controller = register_in_anki(
                aqt_module=fake_aqt,
                gui_hooks_module=self.hooks,
                addon_module_name="inkcards_anki_bridge",
                logger=self.logs.append,
            )
            same_controller = register_in_anki(
                aqt_module=fake_aqt,
                gui_hooks_module=self.hooks,
                addon_module_name="inkcards_anki_bridge",
                logger=self.logs.append,
            )
            self.assertIs(controller, same_controller)
            self.assertFalse(start.called)
            self.hooks.profile_did_open.emit()
            self.assertEqual(start.call_count, 1)
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
