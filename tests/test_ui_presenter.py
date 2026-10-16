from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from inkcards_anki_bridge.ui.presenter import (
    InkCardsPresenter,
    build_status_view,
    is_valid_device_id,
    safe_error_message,
)


class FakeError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code)


class FakeJob:
    def __init__(self, job_id: str, *, result=None, state: str = "queued", error=None) -> None:
        self.job_id = job_id
        self.state = state
        self.result = result
        self.error = error
        self.cancel_calls = 0

    def cancel(self) -> bool:
        self.cancel_calls += 1
        self.state = "canceled"
        return True


def deck(deck_id: str, name: str, depth: int = 0):
    return SimpleNamespace(
        deck_id=deck_id,
        name=name,
        depth=depth,
        unsupported=(),
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.raw_status = {
            "state": "running",
            "running": True,
            "runtime_epoch": "DO-NOT-DISPLAY",
            "bind": {"host": "127.0.0.1", "port": 8766},
            "collection_generation": 7,
            "write_disabled_reason": "P3 blocked: historical application evidence is incomplete",
            "snapshot": {"count": 2},
            "catalog": {"available": False, "deck_count": 0},
            "preflight": {"available": False},
            "persistence": {
                "devices": 2,
                "events": 9,
                "conflicts": 1,
                "delivery_status": {"received": 2, "applied": 4, "unknown": 1, "conflict": 1},
                "application_status": {"pending_apply": 2, "applied": 3, "unknown": 1},
            },
        }
        self.catalog_jobs: list[FakeJob] = []
        self.preflight_jobs: list[FakeJob] = []
        self.snapshot_jobs: list[FakeJob] = []
        self.snapshot_requests: list[tuple[str, ...]] = []
        self.revoke_calls: list[str] = []

    def status(self):
        return self.raw_status

    def request_deck_catalog(self):
        job = FakeJob(
            f"catalog-{len(self.catalog_jobs) + 1}",
            result=SimpleNamespace(
                entries=(deck("10", "中文"), deck("20", "中文::子牌组", 1)),
            ),
        )
        self.catalog_jobs.append(job)
        return job

    def request_deck_preflight(self, *, deck_ids):
        selected = tuple(str(item) for item in deck_ids)
        job = FakeJob(
            f"preflight-{len(self.preflight_jobs) + 1}",
            result=SimpleNamespace(
                deck_ids=selected,
                card_count=3,
                snapshot_limit=4096,
                within_limit=True,
                include_subdecks=True,
            ),
        )
        self.preflight_jobs.append(job)
        return job

    def request_snapshot(self, *, deck_ids):
        selected = tuple(str(item) for item in deck_ids)
        self.snapshot_requests.append(selected)
        job = FakeJob(f"snapshot-{len(self.snapshot_jobs) + 1}")
        self.snapshot_jobs.append(job)
        return job

    def issue_pairing_code(self):
        return "123456"

    def revoke_device(self, device_id: str):
        self.revoke_calls.append(device_id)


class PresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.presenter = InkCardsPresenter(lambda: self.runtime)

    def load_catalog(self) -> None:
        self.presenter.request_catalog()
        self.runtime.catalog_jobs[-1].state = "published"
        self.presenter.poll()

    def test_status_projection_has_counts_and_user_words_without_secrets(self) -> None:
        view = build_status_view(self.runtime.raw_status)
        self.assertEqual(view.service_text, "服务运行中")
        self.assertEqual((view.host_text, view.port_text), ("127.0.0.1", "8766"))
        self.assertEqual(view.devices, 2)
        self.assertEqual(view.events, 9)
        self.assertEqual(view.conflicts, 1)
        self.assertEqual(view.snapshots, 2)
        self.assertEqual(view.generation_text, "7")
        self.assertEqual(view.delivery_counts()["received"], 2)
        self.assertEqual(view.application_counts()["applied"], 3)
        labels = {item.label for item in view.delivery + view.application}
        self.assertTrue({"已接收", "已应用", "未知", "冲突"}.issubset(labels))
        self.assertIn("复习已接收但尚不能安全写入 Anki", view.write_disabled_reason)
        self.assertNotIn("P3 blocked", view.write_disabled_reason)
        self.assertIn("局域网设备无法连接", view.lan_notice)
        self.assertNotIn("DO-NOT-DISPLAY", repr(view))

    def test_pairing_code_is_user_visible_but_never_printed(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.presenter.issue_pairing_code()
        self.assertEqual(code, "123456")
        self.assertIn("123456", self.presenter.view().pairing_message)
        self.assertEqual(output.getvalue(), "")

    def test_selection_preflight_snapshot_gate(self) -> None:
        self.load_catalog()
        self.presenter.select_decks(["20", "10"])
        self.assertFalse(self.presenter.view().buttons.snapshot)
        self.assertIsNone(self.presenter.request_snapshot())
        self.assertEqual(self.runtime.snapshot_requests, [])

        self.presenter.request_preflight()
        self.runtime.preflight_jobs[-1].state = "published"
        self.presenter.poll()
        self.assertTrue(self.presenter.snapshot_ready)
        self.assertTrue(self.presenter.view().buttons.snapshot)

        self.presenter.request_snapshot()
        self.assertEqual(self.runtime.snapshot_requests, [("10", "20")])

    def test_selection_change_invalidates_old_preflight(self) -> None:
        self.load_catalog()
        self.presenter.select_decks(["10"])
        self.presenter.request_preflight()
        self.runtime.preflight_jobs[-1].state = "published"
        self.presenter.poll()
        self.assertTrue(self.presenter.snapshot_ready)

        self.presenter.select_decks(["20"])
        self.assertFalse(self.presenter.snapshot_ready)
        self.assertIsNone(self.presenter.preflight)
        self.assertIsNone(self.presenter.request_snapshot())
        self.assertEqual(self.runtime.snapshot_requests, [])

    def test_poll_failure_cancel_and_stale_are_distinct_and_redacted(self) -> None:
        catalog = self.runtime.request_deck_catalog()
        catalog.state = "failed"
        catalog.error = FakeError("deck_not_found", "token=SHOULD-NOT-SHOW")
        self.presenter._jobs["catalog"] = catalog
        self.presenter.poll()
        self.assertIn("牌组已不存在", self.presenter.view().error_message)
        self.assertNotIn("SHOULD-NOT-SHOW", self.presenter.view().error_message)

        stale = FakeJob("catalog-stale", state="stale_result_dropped")
        self.presenter._jobs["catalog"] = stale
        self.presenter.poll()
        self.assertIn("过期", self.presenter.view().error_message)

        canceled = FakeJob("preflight-canceled", state="canceled")
        self.presenter._jobs["preflight"] = canceled
        self.presenter.poll()
        self.assertIn("取消", self.presenter.view().preflight_message)

    def test_close_cancels_all_active_jobs_and_disables_actions(self) -> None:
        self.load_catalog()
        self.presenter.select_decks(["10"])
        self.presenter.request_preflight()
        self.presenter.request_snapshot = lambda: None
        catalog = self.runtime.catalog_jobs[-1]
        preflight = self.runtime.preflight_jobs[-1]
        self.presenter.close()
        self.assertEqual(catalog.cancel_calls, 0)  # 已完成任务无需取消
        self.assertEqual(preflight.cancel_calls, 1)
        self.assertFalse(self.presenter.view().buttons.refresh)
        self.assertFalse(self.presenter.view().buttons.cancel)

    def test_unknown_exception_text_is_not_exposed(self) -> None:
        error = FakeError("new_internal_code", "secret-token=abc")
        message = safe_error_message(error)
        self.assertEqual(message, "操作失败，请稍后重试。")
        self.assertNotIn("abc", message)

    def test_failed_lan_config_is_visible(self) -> None:
        presenter = InkCardsPresenter(
            lambda: self.runtime,
            config_writer=lambda _enabled: (_ for _ in ()).throw(RuntimeError("secret")),
        )
        self.assertFalse(presenter.set_lan_enabled(True))
        self.assertIn("配置写入失败", presenter.view().error_message)
        self.assertNotIn("secret", presenter.view().error_message)

    def test_revoke_rejects_invalid_ids_before_runtime_mutation(self) -> None:
        self.assertFalse(is_valid_device_id(""))
        self.assertFalse(is_valid_device_id("bad\nvalue"))
        self.assertFalse(is_valid_device_id("x" * 129))
        self.assertTrue(is_valid_device_id("device-a"))
        self.assertFalse(self.presenter.revoke_device("bad\nvalue"))
        self.assertEqual(self.runtime.revoke_calls, [])


if __name__ == "__main__":
    unittest.main()
