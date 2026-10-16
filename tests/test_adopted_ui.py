"""正式本地恢复按钮只发现已批准原会话，不依赖设备 token。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anki.collection  # noqa: F401
from inkcards_anki_bridge.anki.compact_projection import canonical_json, json_sha256
from inkcards_anki_bridge.runtime import AddonRuntime
from inkcards_anki_bridge.services.compact_application import CompactApplicationError
from inkcards_anki_bridge.ui.dialog import InkCardsDialog
from inkcards_anki_bridge.ui.presenter import InkCardsPresenter
from test_adopted_restore import adopted_fixture
from test_compact_service import DEVICE, SESSION, SyntheticFixture


class ApprovedRecoveryUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-adopted-ui-", dir="/private/tmp"
        )
        self.fixture = SyntheticFixture(Path(self.temp.name))
        self.runtime = object.__new__(AddonRuntime)
        self.runtime._lock = threading.RLock()
        self.runtime._state = "running"
        self.runtime._store = self.fixture.store
        self.runtime._compact_jobs = Mock()
        self.runtime._compact_jobs.request.return_value = SimpleNamespace(
            job_id="ui-restore", state="queued"
        )

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def test_missing_source_and_bad_binding_never_queue_or_create_events(self):
        source = adopted_fixture(self.fixture)
        original = self.fixture.store.query_compact_events(DEVICE, SESSION)
        with self.assertRaises(CompactApplicationError):
            self.runtime.request_approved_adopted_recovery()
        self.fixture.service.import_adopted_source(
            source, expected_sha256=json_sha256(source)
        )
        source["events"][0]["payload_hash"] = "f" * 64
        self.fixture.service._legacy_path(source["snapshot"]["snapshot_id"]).write_text(
            canonical_json(source)
        )
        with self.assertRaises(CompactApplicationError):
            self.runtime.request_approved_adopted_recovery()
        self.runtime._compact_jobs.request.assert_not_called()
        self.assertEqual(
            self.fixture.store.query_compact_events(DEVICE, SESSION), original
        )

    def test_unique_imported_scope_queues_original_job_and_ambiguous_sources_refuse(
        self,
    ):
        source = adopted_fixture(self.fixture)
        self.fixture.service.import_adopted_source(
            source, expected_sha256=json_sha256(source)
        )
        job = self.runtime.request_approved_adopted_recovery()
        self.assertIs(job, self.runtime._compact_jobs.request.return_value)
        self.runtime._compact_jobs.request.assert_called_once_with(DEVICE, SESSION)
        self.fixture.store.open_session(DEVICE, "another-approved-session")
        second = adopted_fixture(self.fixture, session="another-approved-session")
        self.fixture.service.import_adopted_source(
            second, expected_sha256=json_sha256(second)
        )
        self.runtime._compact_jobs.request.reset_mock()
        with self.assertRaises(CompactApplicationError) as caught:
            self.runtime.request_approved_adopted_recovery()
        self.assertEqual(caught.exception.code, "adopted_source_ambiguous")
        self.runtime._compact_jobs.request.assert_not_called()

    def test_dialog_explicit_click_presenter_poll_and_refusal_are_visible(self):
        job = SimpleNamespace(job_id="approved-job", state="queued")
        runtime = SimpleNamespace(
            request_approved_adopted_recovery=Mock(return_value=job),
            status=lambda: {"state": "running", "running": True},
        )
        presenter = InkCardsPresenter(lambda: runtime)
        dialog = object.__new__(InkCardsDialog)
        dialog._presenter = presenter
        dialog._render = Mock()
        presenter.view()
        presenter.poll()
        runtime.request_approved_adopted_recovery.assert_not_called()
        InkCardsDialog._request_approved_recovery(dialog)
        runtime.request_approved_adopted_recovery.assert_called_once_with()
        self.assertFalse(presenter.view().buttons.recovery)
        job.state = "applied"
        self.assertIn("恢复完成", presenter.poll().recovery_message)
        self.assertTrue(presenter.view().buttons.recovery)
        runtime.request_approved_adopted_recovery.side_effect = CompactApplicationError(
            "adopted_source_missing", "secret-not-for-display"
        )
        InkCardsDialog._request_approved_recovery(dialog)
        self.assertIn("尚未导入", presenter.view().error_message)
        self.assertNotIn("secret", presenter.view().error_message)
