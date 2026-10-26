from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from inkcards_anki_bridge.logging_control import AddonLogger, DEFAULT_LOGGING_POLICY


class AddonLoggerTests(unittest.TestCase):
    def read_rows(self, root: Path) -> list[dict]:
        rows = []
        for name in ("plugin.jsonl", "plugin.jsonl.1"):
            path = root / "diagnostics" / name
            if path.exists():
                rows.extend(json.loads(line) for line in path.read_text().splitlines())
        return rows

    def test_default_warn_and_level_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root)
            self.assertEqual(logger.policy, DEFAULT_LOGGING_POLICY)
            logger.emit("debug", "test", "debug_event")
            logger.emit("info", "test", "info_event")
            logger.emit("warn", "test", "warn_event")
            logger.emit("error", "test", "error_event")
            self.assertEqual([row["event"] for row in self.read_rows(root)], ["warn_event", "error_event"])

        expected = {
            "off": [],
            "error": ["error_event"],
            "warn": ["warn_event", "error_event"],
            "info": ["info_event", "warn_event", "error_event"],
            "debug": ["debug_event", "info_event", "warn_event", "error_event"],
        }
        for level, events in expected.items():
            with self.subTest(level=level), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                logger = AddonLogger(root, policy={"level": level, "console": False})
                for selected in ("debug", "info", "warn", "error"):
                    logger.emit(selected, "test", f"{selected}_event")
                self.assertEqual([row["event"] for row in self.read_rows(root)], events)

    def test_invalid_policy_falls_back_to_warn_without_console(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            logger = AddonLogger(Path(name), policy={"level": "verbose", "console": True})
            self.assertEqual(logger.policy, DEFAULT_LOGGING_POLICY)

    def test_missing_profile_and_disabled_console_never_use_logging_last_resort(self) -> None:
        logger = AddonLogger(None, policy={"level": "error", "console": False})
        stderr = io.StringIO()
        try:
            raise RuntimeError("TOKEN-private-message")
        except RuntimeError as error:
            with redirect_stderr(stderr):
                self.assertTrue(logger.emit("error", "lifecycle", "registration_failed"))
                self.assertTrue(logger.log_exception("lifecycle", "failure", error, stage="startup"))
        self.assertEqual(stderr.getvalue(), "")
        self.assertIsNone(logger.path)

    def test_off_short_circuits_encoding_clock_io_and_stack_capture(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root, policy={"level": "off", "console": False})
            error = RuntimeError("TOKEN-card-profile-secret")
            with (
                patch("inkcards_anki_bridge.logging_control.json.dumps", side_effect=AssertionError("encode")) as encode,
                patch("inkcards_anki_bridge.logging_control.datetime") as clock,
                patch("inkcards_anki_bridge.logging_control.Path.mkdir", side_effect=AssertionError("mkdir")) as mkdir,
                patch.object(AddonLogger, "_redacted_traceback", wraps=AddonLogger._redacted_traceback) as traceback,
            ):
                self.assertFalse(logger.emit("error", "test", "disabled_event"))
                self.assertFalse(logger.log_exception("test", "exception", error, stage="prepare"))
            encode.assert_not_called()
            clock.assert_not_called()
            mkdir.assert_not_called()
            traceback.assert_not_called()
            self.assertFalse((root / "diagnostics").exists())

    def test_debug_exception_has_redacted_frames_but_no_message_or_path(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root, policy={"level": "debug", "console": False})
            try:
                raise RuntimeError("TOKEN-card-profile-secret")
            except RuntimeError as error:
                self.assertTrue(logger.log_exception("lifecycle", "failure", error, stage="startup"))
            rows = self.read_rows(root)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["traceback"])
            self.assertEqual(set(rows[0]["traceback"][0]), {"module", "line"})
            raw = (root / "diagnostics" / "plugin.jsonl").read_text()
            self.assertNotIn("TOKEN", raw)
            self.assertNotIn(name, raw)
            self.assertNotIn("RuntimeError(", raw)

    def test_warn_exception_records_safe_type_without_collecting_stack(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root)
            try:
                raise RuntimeError("TOKEN-private-message")
            except RuntimeError as error:
                with patch.object(
                    AddonLogger,
                    "_redacted_traceback",
                    wraps=AddonLogger._redacted_traceback,
                ) as traceback:
                    logger.log_exception("lifecycle", "failure", error, stage="startup")
            traceback.assert_not_called()
            row = self.read_rows(root)[0]
            self.assertEqual(row["exception_type"], "RuntimeError")
            self.assertNotIn("traceback", row)
            self.assertNotIn("TOKEN", (root / "diagnostics" / "plugin.jsonl").read_text())

    def test_sensitive_fields_are_ignored_and_http_severity_is_classified(self) -> None:
        from inkcards_anki_bridge.device.request_log import DeviceRequestLog

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root, policy={"level": "debug", "console": False})
            request_log = DeviceRequestLog(root, logger=logger)
            for status, code in ((200, None), (401, "auth_required"), (503, "internal_error")):
                request_log.record(
                    method="POST",
                    target="/v2/pair",
                    status=status,
                    error_code=code,
                    elapsed_ms=1,
                    request_body_bytes=2,
                    response_body_bytes=3,
                )
            rows = self.read_rows(root)
            self.assertEqual([row["level"] for row in rows], ["info", "warn", "error"])
            request_log.record(
                method="POST",
                target="/v2/pair?token=QUERY-SECRET",
                status=401,
                error_code="auth_required",
                elapsed_ms=1,
                request_body_bytes=2,
                response_body_bytes=3,
            )
            raw = (root / "diagnostics" / "plugin.jsonl").read_text()
            self.assertNotIn("QUERY-SECRET", raw)
            self.assertNotIn("/v2/pair?", raw)

    def test_non_debug_policy_strips_top_level_and_nested_exception_details_at_emit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root, policy={"level": "warn", "console": False})
            logger.emit(
                "error",
                "compact",
                "terminal",
                traceback=[{"module": "private.module", "line": 17}],
                exception={
                    "type": "RuntimeError",
                    "stage": "prepare",
                    "traceback": [{"module": "private.module", "line": 18}],
                    "chain": [
                        {
                            "type": "ValueError",
                            "relation": "cause",
                            "traceback": [{"module": "private.module", "line": 19}],
                        }
                    ],
                    "chain_truncated": True,
                },
            )
            raw = (root / "diagnostics" / "plugin.jsonl").read_text()
            exception = self.read_rows(root)[0]["exception"]
            self.assertNotIn("traceback", raw)
            self.assertNotIn("chain", raw)
            self.assertNotIn("chain_truncated", raw)
            self.assertEqual(exception, {"type": "RuntimeError", "stage": "prepare"})

    def test_live_reconfiguration_changes_threshold_and_console_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            console = io.StringIO()
            logger = AddonLogger(root, console_stream=console)
            logger.emit("info", "test", "before")
            logger.configure({"level": "info", "console": True})
            logger.emit("info", "test", "after")
            self.assertEqual([row["event"] for row in self.read_rows(root)], ["after"])
            self.assertIn('"event":"after"', console.getvalue())
            logger.configure({"level": "error", "console": False})
            logger.emit("warn", "test", "filtered_after_update")
            logger.emit("error", "test", "error_after_update")
            self.assertEqual(
                [row["event"] for row in self.read_rows(root)],
                ["after", "error_after_update"],
            )

    def test_profile_loggers_are_isolated_and_do_not_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            first_root = Path(parent) / "profile-one"
            second_root = Path(parent) / "profile-two"
            first = AddonLogger(first_root, policy={"level": "info", "console": False})
            second = AddonLogger(second_root, policy={"level": "info", "console": False})
            first.emit("info", "app", "first_profile")
            second.emit("info", "app", "second_profile")
            self.assertFalse(first._logger.propagate)
            self.assertFalse(second._logger.propagate)
            self.assertEqual([row["event"] for row in self.read_rows(first_root)], ["first_profile"])
            self.assertEqual([row["event"] for row in self.read_rows(second_root)], ["second_profile"])

    def test_concurrent_writes_rotate_with_one_bounded_backup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logger = AddonLogger(root, policy={"level": "info", "console": False})
            barrier = threading.Barrier(9)

            def write(worker: int) -> None:
                barrier.wait()
                for sequence in range(240):
                    logger.emit(
                        "info",
                        "test",
                        "concurrent",
                        elapsed_ms=sequence,
                        request_body_bytes=worker,
                    )

            threads = [threading.Thread(target=write, args=(worker,)) for worker in range(8)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))

            current = root / "diagnostics" / "plugin.jsonl"
            backup = root / "diagnostics" / "plugin.jsonl.1"
            self.assertLessEqual(current.stat().st_size, 128 * 1024)
            self.assertLessEqual(backup.stat().st_size, 128 * 1024)
            rows = self.read_rows(root)
            self.assertTrue(rows)
            self.assertTrue(all(row["event"] == "concurrent" for row in rows))
            self.assertEqual(
                len({(row["request_body_bytes"], row["elapsed_ms"]) for row in rows}),
                len(rows),
            )

    def test_hot_disable_waits_for_inflight_emit_and_blocks_later_output(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            console = io.StringIO()
            logger = AddonLogger(
                root,
                policy={"level": "info", "console": True},
                console_stream=console,
            )
            file_handler = next(handler for handler in logger._logger.handlers if handler is not None)
            original_emit = file_handler.emit
            emit_entered = threading.Event()
            release_emit = threading.Event()
            configure_attempted = threading.Event()
            configure_returned = threading.Event()
            order: list[str] = []
            order_lock = threading.Lock()

            def blocked_emit(record) -> None:
                if record.event == "in_flight":
                    emit_entered.set()
                    if not release_emit.wait(timeout=3):
                        raise AssertionError("timed out waiting to resume the in-flight emit")
                original_emit(record)
                with order_lock:
                    order.append("write_completed")

            file_handler.emit = blocked_emit

            def write() -> None:
                logger.emit("info", "test", "in_flight")

            def disable() -> None:
                configure_attempted.set()
                logger.configure({"level": "off", "console": False})
                with order_lock:
                    order.append("configure_returned")
                configure_returned.set()

            writer = threading.Thread(target=write)
            configurator = threading.Thread(target=disable)
            writer.start()
            self.assertTrue(emit_entered.wait(timeout=2))
            configurator.start()
            self.assertTrue(configure_attempted.wait(timeout=2))
            self.assertFalse(configure_returned.wait(timeout=0.1))
            release_emit.set()
            writer.join(timeout=3)
            configurator.join(timeout=3)

            self.assertFalse(writer.is_alive())
            self.assertFalse(configurator.is_alive())
            self.assertTrue(configure_returned.is_set())
            self.assertLess(order.index("write_completed"), order.index("configure_returned"))
            self.assertFalse(logger.emit("error", "test", "after_disable"))
            self.assertEqual([row["event"] for row in self.read_rows(root)], ["in_flight"])
            self.assertIn('"event":"in_flight"', console.getvalue())
            self.assertNotIn("after_disable", console.getvalue())


if __name__ == "__main__":
    unittest.main()
