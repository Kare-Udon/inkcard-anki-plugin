from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from inkcards_anki_bridge.ui.presenter import InkCardsPresenter


class FakeRuntime:
    def __init__(self) -> None:
        self.devices = [
            {
                "slot_id": "slot-a",
                "name": "阅读器 A",
                "status": "bound",
                "device_id": "device-private",
                "last_seen_at_ms": 1_790_000_000_000,
                "created_at_ms": 1_789_000_000_000,
                "token": "TOKEN-PRIVATE",
                "code": "CODE-PRIVATE",
            },
            {
                "slot_id": "slot-b",
                "name": "未绑定设备",
                "status": "pending",
                "device_id": None,
                "last_seen_at_ms": None,
                "created_at_ms": 1_789_000_000_000,
            },
            {
                "slot_id": "slot-old",
                "name": "旧设备",
                "status": "revoked",
                "device_id": "device-old",
                "last_seen_at_ms": 1_788_000_000_000,
                "created_at_ms": 1_787_000_000_000,
            },
        ]
        self.list_calls: list[bool] = []
        self.calls: list[tuple] = []
        self.fail_operation: str | None = None

    def _maybe_fail(self, operation: str) -> None:
        if self.fail_operation == operation:
            error = RuntimeError("TOKEN-PRIVATE storage failure")
            error.code = "storage_busy"
            raise error

    def list_devices(self, *, include_revoked=False):
        self.list_calls.append(include_revoked)
        values = self.devices
        if not include_revoked:
            values = [item for item in values if item["status"] != "revoked"]
        return values

    def add_device(self, name):
        self._maybe_fail("add")
        self.calls.append(("add", name))
        return {
            "slot_id": "slot-new",
            "name": name,
            "status": "pending",
            "code": "123456789012",
        }

    def allow_rebind(self, slot_id):
        self._maybe_fail("allow_rebind")
        self.calls.append(("allow_rebind", slot_id))

    def reset_pairing_code(self, slot_id):
        self._maybe_fail("reset_pairing_code")
        self.calls.append(("reset_pairing_code", slot_id))
        return {"slot_id": slot_id, "code": "987654321098"}

    def remove_device(self, slot_id):
        self._maybe_fail("remove_device")
        self.calls.append(("remove_device", slot_id))

    def rename_device(self, slot_id, name):
        self._maybe_fail("rename_device")
        self.calls.append(("rename_device", slot_id, name))


class DeviceManagementPresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.presenter = InkCardsPresenter(lambda: self.runtime)

    def test_device_list_hides_removed_by_default_and_projects_safe_fields(
        self,
    ) -> None:
        state = self.presenter.refresh_devices()

        self.assertEqual(self.runtime.list_calls, [False])
        self.assertEqual([item.slot_id for item in state.devices], ["slot-a", "slot-b"])
        self.assertEqual(state.devices[0].name, "阅读器 A")
        self.assertEqual(state.devices[0].status_text, "已绑定")
        self.assertIn("最近连接", state.devices[0].label)
        self.assertEqual(state.devices[1].status_text, "待绑定")
        self.assertEqual(state.devices[1].last_seen_text, "从未连接")
        rendered = repr(state)
        self.assertNotIn("TOKEN-PRIVATE", rendered)
        self.assertNotIn("CODE-PRIVATE", rendered)
        self.assertNotIn("device-private", rendered)

    def test_removed_devices_are_only_returned_when_explicitly_enabled(self) -> None:
        state = self.presenter.set_show_removed(True)

        self.assertEqual(self.runtime.list_calls, [True])
        self.assertIn("slot-old", [item.slot_id for item in state.devices])
        self.assertEqual(state.devices[-1].status_text, "已移除")

    def test_add_and_reset_show_grouped_non_expiring_code_without_repr_leak(
        self,
    ) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.presenter.add_device("新阅读器")
        self.assertEqual(code, "1234-5678-9012")
        self.assertEqual(output.getvalue(), "")
        state = self.presenter.view()
        self.assertIn("1234-5678-9012", state.pairing_message)
        self.assertIn("不会自动过期", state.pairing_message)
        self.assertNotIn("1234-5678-9012", repr(state))
        self.assertEqual(self.runtime.calls, [("add", "新阅读器")])

        self.presenter.select_device("slot-a")
        reset_code = self.presenter.reset_pairing_code_selected()
        self.assertEqual(reset_code, "9876-5432-1098")
        self.assertIn(reset_code, self.presenter.view().pairing_message)
        self.assertNotIn(reset_code, repr(self.presenter.view()))
        self.assertEqual(self.runtime.calls[-1], ("reset_pairing_code", "slot-a"))

    def test_selected_slot_drives_actions_and_invalid_or_missing_selection_is_ignored(
        self,
    ) -> None:
        self.assertFalse(self.presenter.allow_rebind_selected())
        self.assertEqual(self.runtime.calls, [])

        self.presenter.refresh_devices()
        self.assertTrue(self.presenter.select_device("slot-a").buttons.allow_rebind)
        self.assertTrue(self.presenter.allow_rebind_selected())
        self.assertTrue(self.presenter.rename_selected_device("客厅阅读器"))
        self.assertTrue(self.presenter.remove_selected_device())
        self.assertEqual(
            self.runtime.calls,
            [
                ("allow_rebind", "slot-a"),
                ("rename_device", "slot-a", "客厅阅读器"),
                ("remove_device", "slot-a"),
            ],
        )
        self.assertIn("历史记录保留", self.presenter.view().config_message)

    def test_storage_failures_never_report_success_or_expose_exception_text(
        self,
    ) -> None:
        self.runtime.fail_operation = "remove_device"
        self.presenter.refresh_devices()
        self.presenter.select_device("slot-a")

        self.assertFalse(self.presenter.remove_selected_device())
        view = self.presenter.view()
        self.assertNotIn("已移除", view.config_message)
        self.assertIn("正忙", view.error_message)
        self.assertNotIn("TOKEN-PRIVATE", view.error_message)

    def test_each_mutation_failure_returns_no_code_and_no_success_message(self) -> None:
        for operation in (
            "add",
            "allow_rebind",
            "reset_pairing_code",
            "remove_device",
            "rename_device",
        ):
            with self.subTest(operation=operation):
                runtime = FakeRuntime()
                runtime.fail_operation = operation
                presenter = InkCardsPresenter(lambda runtime=runtime: runtime)
                if operation == "add":
                    result = presenter.add_device("新设备")
                else:
                    presenter.refresh_devices()
                    presenter.select_device("slot-a")
                    result = {
                        "allow_rebind": presenter.allow_rebind_selected,
                        "reset_pairing_code": presenter.reset_pairing_code_selected,
                        "remove_device": presenter.remove_selected_device,
                        "rename_device": lambda target=presenter: (
                            target.rename_selected_device("新名称")
                        ),
                    }[operation]()
                self.assertFalse(result)
                view = presenter.view()
                self.assertEqual(view.config_message, "")
                self.assertIn("稍后重试", view.error_message)
                self.assertNotIn("TOKEN-PRIVATE", repr(view))

    def test_invalid_names_are_rejected_before_runtime_mutation(self) -> None:
        self.assertIsNone(self.presenter.add_device("  "))
        self.assertEqual(self.runtime.calls, [])
        self.presenter.refresh_devices()
        self.presenter.select_device("slot-a")
        self.assertFalse(self.presenter.rename_selected_device("bad\nname"))
        self.assertEqual(self.runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
