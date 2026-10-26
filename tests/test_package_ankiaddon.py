"""InkCards ``.ankiaddon`` 打包器的白名单、确定性和导入边界测试。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import package_ankiaddon


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class PackageAnkiaddonTests(unittest.TestCase):
    def _copy_source(self, temporary: Path) -> Path:
        target = temporary / "anki-addon"
        shutil.copytree(SOURCE_ROOT, target, symlinks=True)
        return target

    def _build(self, root: Path, output: Path) -> list[str]:
        return package_ankiaddon.write_package(root, output)

    def test_version_fields_and_archive_name_are_aligned(self) -> None:
        expected_version = "0.2.0-preview.20"
        manifest = json.loads((SOURCE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        from inkcards_anki_bridge import __version__

        self.assertEqual(manifest["human_version"], expected_version)
        self.assertEqual(__version__, expected_version)
        self.assertEqual(package_ankiaddon.HUMAN_VERSION, expected_version)
        self.assertEqual(
            package_ankiaddon.ARCHIVE_FILENAME,
            "InkCards-Anki-Bridge-0.2.0-preview.20.ankiaddon",
        )

    def test_real_build_contains_only_the_explicit_production_allowlist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            temporary = Path(name)
            root = self._copy_source(temporary)
            output = temporary / "build.ankiaddon"
            expected = package_ankiaddon.package_files(root)
            names = self._build(root, output)

            self.assertEqual(names, [archive_name for _, archive_name in expected])
            self.assertIn("inkcards_anki_bridge/logging_control.py", names)
            self.assertIn("inkcards_anki_bridge/sync_policy.py", names)
            with zipfile.ZipFile(output) as archive:
                infos = archive.infolist()
                self.assertEqual(archive.namelist(), names)
                self.assertEqual(len(names), len(set(names)))
                for info in infos:
                    self.assertEqual(info.date_time, package_ankiaddon.FIXED_DATE_TIME)
                    self.assertEqual(info.create_system, 3)
                    self.assertEqual(info.external_attr, 0o100644 << 16)
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                    package_ankiaddon.validate_archive_name(info.filename)
                    self.assertFalse(info.filename.startswith("anki-addon/"))
                    self.assertNotIn("__pycache__", info.filename)
                    self.assertFalse(info.filename.endswith((".pyc", ".pyo")))
                    self.assertNotIn("tests/", info.filename)
                    self.assertNotIn("research/", info.filename)
                    self.assertNotIn("tools/", info.filename)
                    self.assertNotIn("dist/", info.filename)
                    self.assertNotIn("companion-rs", info.filename)
                    self.assertNotIn(
                        info.filename,
                        {
                            f"inkcards_anki_bridge/{legacy}"
                            for legacy in package_ankiaddon.IGNORED_LEGACY_BRIDGE_FILES
                        },
                    )

    def test_two_independent_builds_have_identical_sha256(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            temporary = Path(name)
            root = self._copy_source(temporary)
            first = temporary / "first.ankiaddon"
            second = temporary / "second.ankiaddon"
            self._build(root, first)
            self._build(root, second)

            first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
            second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_config_defaults_match_runtime_and_packager_defaults(self) -> None:
        config = json.loads((SOURCE_ROOT / "config.json").read_text(encoding="utf-8"))
        device_server = config["device_server"]
        from inkcards_anki_bridge.runtime import DEFAULT_HOST as runtime_host
        from inkcards_anki_bridge.runtime import DEFAULT_PORT as runtime_port

        self.assertEqual(device_server["host"], package_ankiaddon.DEFAULT_HOST)
        self.assertEqual(device_server["port"], package_ankiaddon.DEFAULT_PORT)
        self.assertEqual(device_server["host"], runtime_host)
        self.assertEqual(device_server["port"], runtime_port)
        self.assertEqual(config["logging"], {"level": "warn", "console": False})

    def test_runtime_default_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            runtime_path = root / "inkcards_anki_bridge" / "runtime.py"
            runtime_source = runtime_path.read_text(encoding="utf-8")
            self.assertIn("DEFAULT_PORT = 8766", runtime_source)
            runtime_path.write_text(
                runtime_source.replace("DEFAULT_PORT = 8766", "DEFAULT_PORT = 8765", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(package_ankiaddon.PackageError, "runtime.py"):
                package_ankiaddon.package_files(root)

    def test_unknown_production_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            (root / "inkcards_anki_bridge" / "unexpected.py").write_text(
                "# 未授权生产路径\n", encoding="utf-8"
            )
            with self.assertRaises(package_ankiaddon.PackageError):
                package_ankiaddon.package_files(root)

    def test_repository_metadata_is_allowed_but_never_packaged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            for filename in ("README.md", "README.en.md", ".gitignore", ".gitattributes"):
                (root / filename).write_text("repository metadata\n", encoding="utf-8")
            git_directory = root / ".git"
            git_directory.mkdir(exist_ok=True)
            (git_directory / "private-test-marker").write_text("not for distribution\n")
            names = self._build(root, Path(name) / "repository.ankiaddon")
            self.assertFalse(set(names) & {"README.md", "README.en.md", ".gitignore", ".gitattributes"})
            self.assertFalse(any(member.startswith(".git/") for member in names))

    def test_unknown_repository_root_file_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            (root / "credentials.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(package_ankiaddon.PackageError, "unknown package-root path"):
                package_ankiaddon.package_files(root)

    def test_missing_required_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            (root / "config.json").unlink()
            with self.assertRaises(package_ankiaddon.PackageError):
                package_ankiaddon.package_files(root)

    def test_sensitive_production_suffix_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            (root / "inkcards_anki_bridge" / "anki" / "secret.pyc").write_bytes(
                b"not an installable source file"
            )
            with self.assertRaises(package_ankiaddon.PackageError):
                package_ankiaddon.package_files(root)

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_symbolic_link_in_production_tree_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            target = root / "inkcards_anki_bridge" / "adapter.py"
            target.unlink()
            os.symlink(root / "inkcards_anki_bridge" / "runtime.py", target)
            with self.assertRaises(package_ankiaddon.PackageError):
                package_ankiaddon.package_files(root)

    def test_duplicate_and_zip_slip_archive_names_fail_closed(self) -> None:
        for unsafe in ("../escape.txt", "/absolute.txt", "safe/../../escape.txt", "safe\\escape.txt"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(package_ankiaddon.PackageError):
                    package_ankiaddon.validate_archive_name(unsafe)

        with tempfile.TemporaryDirectory(prefix="inkcards-package-test-") as name:
            root = self._copy_source(Path(name))
            with self.assertRaises(package_ankiaddon.PackageError):
                package_ankiaddon.validate_entries(
                    [
                        (root / "__init__.py", "__init__.py"),
                        (root / "manifest.json", "__init__.py"),
                    ]
                )

    def test_importing_bridge_metadata_does_not_import_legacy_or_aqt(self) -> None:
        script = r'''
import sys


class BlockLegacyImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "aqt" or fullname.startswith("aqt."):
            raise AssertionError("aqt was imported")
        if fullname in {
            "inkcards_anki_bridge.protocol",
            "inkcards_anki_bridge.dispatcher",
            "inkcards_anki_bridge.rendezvous",
            "inkcards_anki_bridge.server",
            "inkcards_anki_bridge.service",
        }:
            raise AssertionError("legacy bridge module was imported")
        return None


sys.meta_path.insert(0, BlockLegacyImports())
import inkcards_anki_bridge

assert inkcards_anki_bridge.__version__ == "0.2.0-preview.20"
assert not any(
    name == "aqt" or name.startswith("aqt.")
    or name in {
        "inkcards_anki_bridge.protocol",
        "inkcards_anki_bridge.dispatcher",
        "inkcards_anki_bridge.rendezvous",
        "inkcards_anki_bridge.server",
        "inkcards_anki_bridge.service",
    }
    for name in sys.modules
)
print(inkcards_anki_bridge.__version__)
'''
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SOURCE_ROOT)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(SOURCE_ROOT.parent),
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "0.2.0-preview.20")


if __name__ == "__main__":
    unittest.main()
