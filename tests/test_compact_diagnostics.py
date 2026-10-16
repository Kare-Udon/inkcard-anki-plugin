"""诊断只保留稳定代码、异常类型和源码位置；不读取 Collection。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import anki.collection  # noqa: F401
from inkcards_anki_bridge.anki.compact_260903 import CompactNativeError
from inkcards_anki_bridge.anki.compact_diagnostics import CompactDiagnostics
from inkcards_anki_bridge.anki.compact_jobs import CompactJobManager
from inkcards_anki_bridge.anki.compact_projection import json_sha256
from inkcards_anki_bridge.anki.operations import CollectionOpAdapter
from inkcards_anki_bridge.ui.presenter import InkCardsPresenter
from test_adopted_restore import adopted_fixture
from test_compact_jobs import (
    ContextSource,
    FakeCollectionHost,
    FakeCompactResult,
    FakeOpChanges,
)
from test_compact_service import DEVICE, SESSION, SyntheticFixture


class CompactDiagnosticsTests(unittest.TestCase):
    def test_exception_privacy_terminal_and_reset(self):
        with tempfile.TemporaryDirectory() as root:
            sink = CompactDiagnostics(Path(root))
            sink.begin()
            try:
                raise CompactNativeError(
                    "snapshot_clock_config_conflict", "TOKEN-private-card-profile"
                )
            except CompactNativeError as error:
                sink.record_exception(error, "prepare")
            sink.record_terminal("blocked", "snapshot_clock_config_conflict")
            raw = sink.path.read_text()
            data = json.loads(raw)
            self.assertNotIn("TOKEN", raw)
            self.assertEqual(data["reason"], "snapshot_clock_config_conflict")
            self.assertEqual(data["exception"]["type"], "CompactNativeError")
            self.assertTrue(data["exception"]["traceback"])
            self.assertEqual(set(data["exception"]["traceback"][0]), {"module", "line"})
            sink.begin()
            sink.record_terminal("applied", None)
            self.assertNotIn("exception", json.loads(sink.path.read_text()))
            with patch(
                "inkcards_anki_bridge.anki.compact_diagnostics.os.replace",
                side_effect=OSError("secret"),
            ):
                sink.record_terminal("blocked", "snapshot_clock_config_conflict")

    def test_prepare_blocked_return_retains_exception_and_no_intent(self):
        with tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-diagnostic-", dir="/private/tmp"
        ) as root:
            fixture = SyntheticFixture(Path(root))
            try:
                source = adopted_fixture(fixture, missing_persisted_lrt=True)
                fixture.service.import_adopted_source(
                    source, expected_sha256=json_sha256(source)
                )
                sink = CompactDiagnostics(fixture.store.root)
                fixture.service.diagnostics = sink
                sink.begin()
                with patch.object(
                    fixture.adapter,
                    "prepare_event",
                    side_effect=CompactNativeError(
                        "snapshot_clock_config_conflict", "TOKEN-private"
                    ),
                ):
                    result = fixture.apply_one()
                self.assertEqual(result.status, "blocked")
                sink.record_terminal(result.status, result.reason)
                raw = sink.path.read_text()
                self.assertIn("CompactNativeError", raw)
                self.assertNotIn("TOKEN", raw)
                self.assertIsNone(
                    fixture.store.query_compact_applications(DEVICE, SESSION)[
                        0
                    ].intent_id
                )
            finally:
                fixture.close()

    def test_recovery_ui_displays_code_without_message(self):
        job = SimpleNamespace(
            job_id="test",
            state="blocked",
            to_dict=lambda: {
                "reason": "snapshot_clock_config_conflict",
                "message": "TOKEN-private",
            },
        )
        runtime = SimpleNamespace(
            request_approved_adopted_recovery=Mock(return_value=job),
            status=lambda: {"state": "running", "running": True},
        )
        presenter = InkCardsPresenter(lambda: runtime)
        presenter.request_approved_recovery()
        message = presenter.poll().recovery_message
        self.assertIn("snapshot_clock_config_conflict", message)
        self.assertNotIn("TOKEN", message)

    def test_manager_records_returned_block_and_thrown_failure(self):
        with tempfile.TemporaryDirectory() as root:
            for thrown in (False, True):
                with self.subTest(thrown=thrown):
                    sink = CompactDiagnostics(Path(root))
                    source, host = ContextSource(), FakeCollectionHost()

                    def apply_one(*args, sink=sink, thrown=thrown):
                        sink.begin()
                        if thrown:
                            raise CompactNativeError(
                                "snapshot_clock_config_conflict", "TOKEN-private"
                            )
                        return FakeCompactResult(
                            "blocked",
                            0,
                            0,
                            False,
                            False,
                            FakeOpChanges(),
                            "snapshot_clock_config_conflict",
                        )

                    manager = CompactJobManager(
                        operation_adapter=CollectionOpAdapter(host, source),
                        context_provider=source,
                        apply_one=apply_one,
                        update_collection_generation=lambda generation: None,
                        reviewer_available=lambda: True,
                        diagnostics=sink,
                    )
                    job = manager.request("synthetic-device", "synthetic-session")
                    host.finish_next(object())
                    data = json.loads(sink.path.read_text())
                    self.assertEqual(data["status"], job.state.value)
                    self.assertEqual(data["reason"], job.to_dict()["reason"])
                    if thrown:
                        self.assertEqual(
                            data["exception"]["code"], "snapshot_clock_config_conflict"
                        )
                    self.assertNotIn("TOKEN", sink.path.read_text())
                    self.assertNotIn("synthetic-device", sink.path.read_text())

    def test_process_clock_drops_invalid_environment_and_ui_reason(self):
        with tempfile.TemporaryDirectory() as root:
            sink = CompactDiagnostics(Path(root))
            with patch.dict("os.environ", {"TZ": "/private/TOKEN-secret"}):
                sink.record_terminal("blocked", "bad reason TOKEN-secret")
            data = json.loads(sink.path.read_text())
            self.assertIsNone(data["reason"])
            self.assertIsNone(data["process_clock"]["tz_env"])
            self.assertNotIn("TOKEN", sink.path.read_text())
        job = SimpleNamespace(
            job_id="test",
            state="blocked",
            to_dict=lambda: {"reason": "bad reason TOKEN-private"},
        )
        runtime = SimpleNamespace(
            request_approved_adopted_recovery=Mock(return_value=job),
            status=lambda: {"state": "running", "running": True},
        )
        presenter = InkCardsPresenter(lambda: runtime)
        presenter.request_approved_recovery()
        self.assertNotIn("TOKEN", presenter.poll().recovery_message)

    def test_cause_context_chain_privacy_and_cycle_bound(self):
        with tempfile.TemporaryDirectory() as root:
            sink = CompactDiagnostics(Path(root))
            try:
                try:
                    raise ValueError("TOKEN-profile-card-source")
                except ValueError:
                    raise RuntimeError("TOKEN-inner")
            except RuntimeError as inner:
                try:
                    raise CompactNativeError(
                        "isolated_projection_failed", "TOKEN-outer"
                    ) from inner
                except CompactNativeError as outer:
                    sink.record_exception(outer, "prepare")
            sink.record_terminal("blocked", "isolated_projection_failed")
            raw = sink.path.read_text()
            chain = json.loads(raw)["exception"]["chain"]
            self.assertEqual(
                [node["type"] for node in chain], ["RuntimeError", "ValueError"]
            )
            self.assertEqual([node["relation"] for node in chain], ["cause", "context"])
            self.assertTrue(all(node["traceback"] for node in chain))
            self.assertNotIn("TOKEN", raw)
            nodes = [RuntimeError("TOKEN-cycle") for _ in range(8)]
            for index, node in enumerate(nodes):
                node.__cause__ = nodes[(index + 1) % len(nodes)]
            sink.record_exception(nodes[0], "prepare")
            sink.record_terminal("blocked", "isolated_projection_failed")
            data = json.loads(sink.path.read_text())["exception"]
            self.assertLessEqual(len(data["chain"]), 3)
            self.assertTrue(data["chain_truncated"])
            nodes[0].__cause__ = nodes[0]
            sink.record_exception(nodes[0], "prepare")
            sink.record_terminal("blocked", "isolated_projection_failed")
            self.assertTrue(
                json.loads(sink.path.read_text())["exception"]["chain_truncated"]
            )
