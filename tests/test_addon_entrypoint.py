from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import inkcards_anki_bridge
from inkcards_anki_bridge import plugin
from inkcards_anki_bridge.logging_control import AddonLogger
from inkcards_anki_bridge.profile_identity import profile_namespace


ADDON_ROOT = Path(__file__).resolve().parents[1]


class FakeAddonManager:
    def __init__(self, addon_root: Path, config=None) -> None:
        self.addon_root = addon_root
        self.config = config or {}

    def addonFromModule(self, _module_name: str) -> str:
        return "inkcards_anki_bridge"

    def addonFolder(self, _addon_id: str) -> str:
        return str(self.addon_root)

    def getConfig(self, _addon_id: str):
        return dict(self.config)


def make_aqt(addon_root: Path, profile_folder, config=None):
    return SimpleNamespace(
        mw=SimpleNamespace(
            addonManager=FakeAddonManager(addon_root, config),
            pm=SimpleNamespace(profileFolder=profile_folder),
        )
    )


class AddonEntrypointTests(unittest.TestCase):
    def test_entrypoint_routes_registration_failure_to_safe_helper(self) -> None:
        package_name = f"inkcards_entrypoint_test_{uuid.uuid4().hex}"
        aqt = SimpleNamespace()
        error = RuntimeError("TOKEN-private startup failure")
        reporter = Mock()
        spec = importlib.util.spec_from_file_location(
            package_name,
            ADDON_ROOT / "__init__.py",
            submodule_search_locations=[str(ADDON_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        aliases = {
            package_name: module,
            f"{package_name}.inkcards_anki_bridge": inkcards_anki_bridge,
            f"{package_name}.inkcards_anki_bridge.plugin": plugin,
            "aqt": aqt,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, aliases):
            with (
                patch.object(plugin, "register_in_anki", side_effect=error),
                patch.object(plugin, "report_startup_failure", reporter),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                spec.loader.exec_module(module)
        reporter.assert_called_once_with(
            error,
            aqt_module=aqt,
            addon_module_name=package_name,
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_startup_failure_uses_default_safe_file_log_without_console(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            profile = base / "profile"
            user_files = base / "user_files"
            aqt = make_aqt(base / "addon", lambda: str(profile))
            error = RuntimeError("TOKEN-private startup failure")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertTrue(
                    plugin.report_startup_failure(
                        error,
                        aqt_module=aqt,
                        addon_module_name="inkcards_anki_bridge",
                        user_files_root=user_files,
                    )
                )
            profile_root = user_files / "profiles" / profile_namespace(profile)
            log_path = profile_root / "diagnostics" / "plugin.jsonl"
            row = json.loads(log_path.read_text().splitlines()[-1])
            self.assertEqual(row["event"], "startup_failed")
            self.assertEqual(row["level"], "error")
            self.assertEqual(row["exception_type"], "RuntimeError")
            self.assertNotIn("TOKEN", log_path.read_text())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

    def test_off_startup_failure_does_not_print_capture_stack_or_create_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            profile = base / "profile"
            user_files = base / "user_files"
            aqt = make_aqt(
                base / "addon",
                lambda: str(profile),
                {"logging": {"level": "off", "console": True}},
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.object(
                    AddonLogger,
                    "_redacted_traceback",
                    side_effect=AssertionError("stack capture forbidden"),
                ) as traceback,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertFalse(
                    plugin.report_startup_failure(
                        RuntimeError("TOKEN-private startup failure"),
                        aqt_module=aqt,
                        addon_module_name="inkcards_anki_bridge",
                        user_files_root=user_files,
                    )
                )
            traceback.assert_not_called()
            self.assertFalse((user_files / "profiles").exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

    def test_missing_profile_root_is_silent_even_if_console_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            user_files = base / "user_files"

            def unavailable_profile():
                raise RuntimeError("profile unavailable")

            aqt = make_aqt(
                base / "addon",
                unavailable_profile,
                {"logging": {"level": "debug", "console": True}},
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.object(
                    AddonLogger,
                    "_redacted_traceback",
                    side_effect=AssertionError("stack capture forbidden"),
                ) as traceback,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertFalse(
                    plugin.report_startup_failure(
                        RuntimeError("TOKEN-private startup failure"),
                        aqt_module=aqt,
                        addon_module_name="inkcards_anki_bridge",
                        user_files_root=user_files,
                    )
                )
            traceback.assert_not_called()
            self.assertFalse(user_files.exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
