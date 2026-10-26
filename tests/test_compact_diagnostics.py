"""Compact diagnostics share the configurable logger without changing receipts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anki.collection  # noqa: F401
from inkcards_anki_bridge.anki.compact_260903 import CompactNativeError
from inkcards_anki_bridge.anki.compact_diagnostics import CompactDiagnostics
from inkcards_anki_bridge.anki.compact_jobs import CompactJobManager
from inkcards_anki_bridge.anki.compact_projection import json_sha256
from inkcards_anki_bridge.anki.operations import CollectionOpAdapter
from inkcards_anki_bridge.logging_control import AddonLogger
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
    @staticmethod
    def make_sink(root: Path, *, level: str = "debug") -> CompactDiagnostics:
        return CompactDiagnostics(
            root,
            logger=AddonLogger(root, policy={"level": level, "console": False}),
        )

    @staticmethod
    def last_record(sink: CompactDiagnostics) -> dict:
        assert sink.path is not None
        return json.loads(sink.path.read_text().splitlines()[-1])

    def test_exception_privacy_terminal_and_reset(self):
        with tempfile.TemporaryDirectory() as root:
            sink = self.make_sink(Path(root))
            sink.begin()
            try:
                raise CompactNativeError(
                    "snapshot_clock_config_conflict", "TOKEN-private-card-profile"
                )
            except CompactNativeError as error:
                sink.record_exception(error, "prepare")
            sink.record_terminal("blocked", "snapshot_clock_config_conflict")
            raw = sink.path.read_text()
            data = self.last_record(sink)
            self.assertNotIn("TOKEN", raw)
            self.assertEqual(data["reason"], "snapshot_clock_config_conflict")
            self.assertEqual(data["exception"]["type"], "CompactNativeError")
            self.assertTrue(data["exception"]["traceback"])
            self.assertEqual(set(data["exception"]["traceback"][0]), {"module", "line"})
            sink.begin()
            sink.record_terminal("applied", None)
            self.assertNotIn("exception", self.last_record(sink))
            self.assertFalse((Path(root) / "compact-diagnostic.json").exists())

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
                sink = self.make_sink(fixture.store.root)
                fixture.service.diagnostics = sink
                sink.begin()
                from unittest.mock import patch

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
                    fixture.store.query_compact_applications(DEVICE, SESSION)[0].intent_id
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
                    sink = self.make_sink(Path(root))
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
                    data = self.last_record(sink)
                    self.assertEqual(data["state"], job.state.value)
                    self.assertEqual(data["reason"], job.to_dict()["reason"])
                    if thrown:
                        self.assertEqual(
                            data["exception"]["code"], "snapshot_clock_config_conflict"
                        )
                    self.assertNotIn("TOKEN", sink.path.read_text())
                    self.assertNotIn("synthetic-device", sink.path.read_text())

    def test_logging_off_does_not_disable_compact_receipt_or_write_diagnostics(self):
        with tempfile.TemporaryDirectory(
            prefix="inkcards-native-service-", dir="/private/tmp"
        ) as root:
            fixture = SyntheticFixture(Path(root) / "main")
            try:
                sink = self.make_sink(fixture.store.root, level="off")
                fixture.service.diagnostics = sink
                snapshot_id = fixture.publish_production_snapshot()
                fixture.receive(snapshot_id, [[1, 3, fixture.times()[0], 120]])
                result = fixture.apply_one()
                sink.record_terminal(result.status, result.reason)
                self.assertEqual(result.status, "applied", result.to_dict())
                application = fixture.store.query_compact_applications(DEVICE, SESSION)[0]
                self.assertIsNotNone(application.receipt_hash)
                self.assertEqual(fixture.store.get_collection_generation(), 1)
                self.assertFalse((fixture.store.root / "diagnostics").exists())
            finally:
                fixture.close()

    def test_unstable_reason_is_dropped_and_ui_reason_remains_redacted(self):
        with tempfile.TemporaryDirectory() as root:
            sink = self.make_sink(Path(root))
            sink.record_terminal("blocked", "bad reason TOKEN-secret")
            data = self.last_record(sink)
            self.assertIsNone(data["reason"])
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
            sink = self.make_sink(Path(root))
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
            chain = self.last_record(sink)["exception"]["chain"]
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
            data = self.last_record(sink)["exception"]
            self.assertLessEqual(len(data["chain"]), 3)
            self.assertTrue(data["chain_truncated"])
            nodes[0].__cause__ = nodes[0]
            sink.record_exception(nodes[0], "prepare")
            sink.record_terminal("blocked", "isolated_projection_failed")
            self.assertTrue(self.last_record(sink)["exception"]["chain_truncated"])

    def test_terminal_after_debug_to_warn_switch_drops_cached_exception_details(self):
        with tempfile.TemporaryDirectory() as root:
            sink = self.make_sink(Path(root), level="debug")
            try:
                raise ValueError("TOKEN-cause")
            except ValueError as cause:
                try:
                    raise RuntimeError("TOKEN-main") from cause
                except RuntimeError as error:
                    sink.record_exception(error, "prepare")

            sink.logger.configure({"level": "warn", "console": False})
            sink.record_terminal("failed", "isolated_projection_failed")
            row = self.last_record(sink)
            self.assertEqual(row["level"], "error")
            self.assertEqual(row["exception"]["type"], "RuntimeError")
            self.assertNotIn("traceback", row["exception"])
            self.assertNotIn("chain", row["exception"])
            self.assertNotIn("chain_truncated", row["exception"])
            raw = sink.path.read_text()
            self.assertNotIn("TOKEN", raw)
            self.assertNotIn("traceback", raw)
            self.assertNotIn("chain", raw)

    def test_cached_exception_promotes_otherwise_info_terminal_to_error(self):
        with tempfile.TemporaryDirectory() as root:
            sink = self.make_sink(Path(root), level="warn")
            try:
                raise RuntimeError("TOKEN-private")
            except RuntimeError as error:
                sink.record_exception(error, "prepare")

            sink.record_terminal("done", None)
            row = self.last_record(sink)
            self.assertEqual(row["level"], "error")
            self.assertEqual(row["state"], "done")
            self.assertEqual(row["exception"]["type"], "RuntimeError")
            self.assertNotIn("TOKEN", sink.path.read_text())


if __name__ == "__main__":
    unittest.main()
