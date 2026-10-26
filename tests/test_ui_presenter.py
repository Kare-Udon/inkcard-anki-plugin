from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from inkcards_anki_bridge.ui.presenter import (
    InkCardsPresenter,
    build_status_view,
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


def deck(
    deck_id: str,
    name: str,
    depth: int = 0,
    *,
    parent_id: str | None = None,
    card_count: int | None = None,
):
    return SimpleNamespace(
        deck_id=deck_id,
        name=name,
        depth=depth,
        path=tuple(name.split("::")),
        parent_id=parent_id,
        card_count=card_count,
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
        self.catalog_entries = (
            deck("10", "中文", card_count=3),
            deck("20", "中文::子牌组", 1, parent_id="10", card_count=1),
        )
        self.policy = {"excluded_deck_ids": [], "revision": 0}
        self.policy_writes: list[tuple[str, ...]] = []
        self.fail_policy_save = False
        self.devices = []

    def get_sync_policy(self):
        return dict(self.policy)

    def set_excluded_decks(self, deck_ids):
        if self.fail_policy_save:
            raise FakeError("storage_busy", "secret policy storage detail")
        selected = tuple(sorted(str(item) for item in deck_ids))
        self.policy_writes.append(selected)
        self.policy = {
            "excluded_deck_ids": list(selected),
            "revision": self.policy["revision"] + 1,
        }
        return dict(self.policy)

    def list_devices(self, *, include_revoked=False):
        return list(self.devices)

    def status(self):
        return self.raw_status

    def request_deck_catalog(self):
        job = FakeJob(
            f"catalog-{len(self.catalog_jobs) + 1}",
            result=SimpleNamespace(
                entries=self.catalog_entries,
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
        self.assertIn("牌组选择数量", safe_error_message(FakeError("scope_too_large")))

    def test_failed_lan_config_is_visible(self) -> None:
        presenter = InkCardsPresenter(
            lambda: self.runtime,
            config_writer=lambda _enabled: (_ for _ in ()).throw(RuntimeError("secret")),
        )
        self.assertFalse(presenter.set_lan_enabled(True))
        self.assertIn("配置写入失败", presenter.view().error_message)
        self.assertNotIn("secret", presenter.view().error_message)

    def test_default_sync_policy_excludes_only_explicit_ids_and_preserves_unknown_ids(self):
        self.runtime.policy = {
            "excluded_deck_ids": ["10", "deleted-deck"],
            "revision": 7,
        }
        self.presenter.refresh_sync_policy()
        self.load_catalog()

        state = self.presenter.view()
        self.assertEqual(set(state.excluded_deck_ids), {"10", "deleted-deck"})
        self.assertEqual(set(state.effective_excluded_deck_ids), {"10", "20"})
        self.assertFalse(self.presenter.set_deck_excluded("20", False))
        self.assertEqual(self.runtime.policy_writes, [])
        self.assertIn("上级牌组", self.presenter.view().sync_policy_error)

        self.assertTrue(self.presenter.set_deck_excluded("10", False))
        self.assertEqual(
            self.runtime.policy_writes[-1],
            ("deleted-deck",),
            "changing a known deck must retain excluded IDs missing from this catalog",
        )
        self.assertEqual(self.presenter.view().effective_excluded_deck_ids, ())

        self.runtime.policy = {
            "excluded_deck_ids": ["10", "20"],
            "revision": 9,
        }
        self.presenter.refresh_sync_policy()
        state = self.presenter.view()
        self.assertEqual(set(state.inherited_excluded_deck_ids), {"20"})
        calls_before_inherited_uncheck = list(self.runtime.policy_writes)
        self.assertFalse(self.presenter.set_deck_excluded("20", False))
        self.assertEqual(self.runtime.policy_writes, calls_before_inherited_uncheck)

    def test_new_catalog_decks_join_by_default_and_failed_policy_save_reverts(self):
        self.presenter.refresh_sync_policy()
        self.load_catalog()
        self.assertTrue(self.presenter.set_deck_excluded("10", True))
        self.assertEqual(set(self.presenter.view().effective_excluded_deck_ids), {"10", "20"})

        self.runtime.catalog_entries = self.runtime.catalog_entries + (
            deck("30", "新牌组", card_count=None),
        )
        self.presenter.request_catalog()
        self.runtime.catalog_jobs[-1].state = "published"
        self.presenter.poll()
        self.assertNotIn("30", self.presenter.view().effective_excluded_deck_ids)
        self.assertIsNone(next(item for item in self.presenter.view().decks if item.deck_id == "30").card_count)

        self.runtime.fail_policy_save = True
        self.assertFalse(self.presenter.set_deck_excluded("10", False))
        state = self.presenter.view()
        self.assertEqual(set(state.excluded_deck_ids), {"10"})
        self.assertIn("正忙", state.sync_policy_error)
        self.assertNotIn("secret", state.sync_policy_error)
        self.assertNotIn("已保存", state.sync_policy_message)

    def test_unavailable_policy_is_explicit_and_cannot_report_a_saved_change(self):
        self.runtime.get_sync_policy = None
        self.presenter.refresh_sync_policy()
        self.load_catalog()

        state = self.presenter.view()
        self.assertFalse(state.sync_policy_available)
        self.assertEqual(state.effective_excluded_deck_ids, ())
        self.assertIn("暂不可用", state.sync_policy_error)
        self.assertFalse(self.presenter.set_deck_excluded("10", True))
        self.assertEqual(self.runtime.policy_writes, [])
        self.assertNotIn("已保存", self.presenter.view().sync_policy_message)

    def test_missing_sync_summary_never_infers_completion_or_device_online(self):
        self.runtime.devices = [
            {
                "slot_id": "slot-a",
                "name": "阅读器 A",
                "status": "bound",
                "last_seen_at_ms": 1_790_000_000_000,
            }
        ]
        self.presenter.refresh_status()
        self.presenter.refresh_devices()
        overview = self.presenter.view().sync_overview

        self.assertEqual(overview.headline, "已就绪 · 等待设备发起同步")
        self.assertEqual(overview.recent_text, "暂无同步记录")
        self.assertNotIn("在线", repr(overview))
        self.assertNotIn("同步完成", repr(overview))

        self.runtime.raw_status = {"state": "stopped", "running": False}
        self.presenter.refresh_status()
        self.assertEqual(self.presenter.view().sync_overview.headline, "服务未启用")

    def test_runtime_sync_states_and_capacity_errors_use_current_status_shape(self):
        self.runtime.devices = [
            {
                "slot_id": "slot-a",
                "name": "阅读器 A",
                "status": "bound",
                "last_seen_at_ms": 1_790_000_000_000,
            }
        ]
        self.runtime.raw_status["sync"] = {
            "state": "active",
            "stage": "preparing",
            "error": None,
        }
        self.presenter.refresh_status()
        self.presenter.refresh_devices()
        overview = self.presenter.view().sync_overview
        self.assertEqual(overview.headline, "正在同步")
        self.assertEqual(overview.detail, "正在准备同步数据")

        self.runtime.raw_status["sync"] = {
            "state": "failed",
            "stage": "failed",
            "error": {
                "code": "scope_too_large",
                "message": "实际参与同步的牌组超过容量。",
            },
        }
        self.presenter.refresh_status()
        detail = self.presenter.view().sync_overview.detail
        self.assertIn("32,768", detail)
        self.assertIn("排除一些牌组", detail)

        self.runtime.raw_status["sync"]["error"] = {
            "code": "source_scan_too_large",
            "message": "源扫描预算超限",
        }
        self.presenter.refresh_status()
        detail = self.presenter.view().sync_overview.detail
        self.assertIn("排除牌组无法缩小", detail)

        self.runtime.raw_status["sync"]["error"] = {
            "code": "snapshot_content_unsupported",
            "message": "牌组 N1::临时包含暂不支持的内容。",
        }
        self.presenter.refresh_status()
        detail = self.presenter.view().sync_overview.detail
        self.assertIn("N1::临时", detail)
        self.assertNotIn("卡片正文", detail)

    def test_damaged_policy_is_explicitly_blocked_without_showing_raw_error(self):
        self.presenter.refresh_sync_policy()
        self.load_catalog()
        with patch.object(
            self.runtime, "get_sync_policy",
            side_effect=FakeError("sync_policy_corrupt", "TOKEN private-path"),
        ):
            self.presenter.refresh_sync_policy()
        state = self.presenter.view()
        self.assertFalse(state.sync_policy_available)
        self.assertIn("损坏", state.sync_policy_error)
        self.assertIn("阻断", state.sync_policy_error)
        self.assertNotIn("TOKEN", state.sync_policy_error)
        self.assertEqual(state.sync_policy_message, "")

    def test_catalog_projection_and_inherited_policy_are_cached_per_catalog(self):
        entries = tuple(
            deck(str(index), f"牌组 {index}", card_count=index % 7)
            for index in range(4096)
        )
        self.presenter._catalog = SimpleNamespace(entries=entries)
        self.runtime.policy = {"excluded_deck_ids": ["0"], "revision": 1}
        self.presenter.refresh_sync_policy()

        with patch(
            "inkcards_anki_bridge.ui.presenter._deck_id",
            side_effect=lambda entry: entry.deck_id,
        ) as deck_id_reader:
            state = self.presenter.view()
            calls_after_first_view = deck_id_reader.call_count
            repeated = self.presenter.view()

        self.assertEqual(len(state.decks), 4096)
        self.assertEqual(state.effective_excluded_deck_ids, ("0",))
        self.assertEqual(repeated.effective_excluded_deck_ids, ("0",))
        self.assertLessEqual(calls_after_first_view, 4096)
        self.assertEqual(deck_id_reader.call_count, calls_after_first_view)

if __name__ == "__main__":
    unittest.main()
