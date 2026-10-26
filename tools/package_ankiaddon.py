#!/usr/bin/env python3
"""按固定白名单生成可手工安装的确定性 ``.ankiaddon``。

打包器只读取生产源文件，不执行 Anki、不访问用户 profile，也不把当前源树中
历史的 bridge、research、测试、工具或缓存文件带入安装包。未知生产路径和
不安全文件一律失败，避免在发布前静默扩大包的内容边界。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence, Tuple


PACKAGE_ID = "inkcards_anki_bridge"
HUMAN_VERSION = "0.2.0-preview.20"
VERSION = HUMAN_VERSION
ARCHIVE_FILENAME = f"InkCards-Anki-Bridge-{HUMAN_VERSION}.ankiaddon"
MIN_POINT_VERSION = 260801
MAX_POINT_VERSION = 260903
DEFAULT_HOST = "127.0.0.1"
# 与 AnkiConnect 常用的 8765 分离；用户仍可在 config.json 中显式配置端口。
DEFAULT_PORT = 8766
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

ROOT_FILES = frozenset(
    {
        "__init__.py",
        "manifest.json",
        "config.json",
        "config.md",
    }
)
ROOT_DIRECTORIES = frozenset({"inkcards_anki_bridge", "user_files", "vendor"})
# 这些目录是源码树中的开发资产，明确不属于安装包；它们不参与候选文件发现。
IGNORED_ROOT_DIRECTORIES = frozenset(
    {".git", "__pycache__", ".pytest_cache", "dist", "research", "tests", "tools"}
)
IGNORED_ROOT_FILES = frozenset({"README.md", "README.en.md", ".gitignore", ".gitattributes"})
BRIDGE_TOP_FILES = frozenset(
    {
        "__init__.py",
        "adapter.py",
        "lifecycle.py",
        "logging_control.py",
        "plugin.py",
        "profile_identity.py",
        "runtime.py",
        "sync_policy.py",
    }
)
# 旧模块仍可作为历史源码留在工作区，但不能进入新的单一 Anki 插件产物。
IGNORED_LEGACY_BRIDGE_FILES = frozenset(
    {"dispatcher.py", "rendezvous.py", "protocol.py", "server.py", "service.py"}
)
USER_FILES_README = "user_files/README.txt"

Entry = Tuple[Path, str]


class PackageError(ValueError):
    """源树或归档内容不满足安装包契约。"""


def source_root() -> Path:
    """返回 ``anki-addon`` 源根目录。"""

    return Path(__file__).resolve().parents[1]


def _fail(message: str) -> None:
    raise PackageError(message)


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink():
        _fail(f"symbolic links are forbidden: {label}")
    if not path.is_file():
        _fail(f"missing regular file: {label}")


def _require_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        _fail(f"symbolic links are forbidden: {label}")
    if not path.is_dir():
        _fail(f"missing directory: {label}")


def _relative_name(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        _fail(f"source path escapes package root: {path}")
    raise AssertionError("unreachable")


def _validate_json_file(path: Path, label: str) -> Mapping[str, object]:
    _require_regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"invalid JSON in {label}: {exc}")
    if not isinstance(value, dict):
        _fail(f"JSON object required: {label}")
    return value


def _validate_manifest(root: Path) -> None:
    manifest = _validate_json_file(root / "manifest.json", "manifest.json")
    expected = {
        "package": PACKAGE_ID,
        "human_version": HUMAN_VERSION,
        "min_point_version": MIN_POINT_VERSION,
        "max_point_version": MAX_POINT_VERSION,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            _fail(f"manifest {key!r} must be {value!r}")


def _validate_runtime_defaults(root: Path) -> Tuple[str, int]:
    """读取并校验 runtime.py 的模块级默认 host/port 常量。"""

    runtime_path = root / "inkcards_anki_bridge" / "runtime.py"
    _require_regular_file(runtime_path, "inkcards_anki_bridge/runtime.py")
    try:
        tree = ast.parse(
            runtime_path.read_text(encoding="utf-8"),
            filename=str(runtime_path),
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        _fail(f"invalid runtime.py: {exc}")

    values: dict[str, object] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in {"DEFAULT_HOST", "DEFAULT_PORT"}:
                continue
            if len(targets) != 1:
                _fail(f"runtime.py {target.id} must use a single assignment")
            if target.id in values:
                _fail(f"runtime.py {target.id} must be defined once")
            try:
                values[target.id] = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError) as exc:
                _fail(f"runtime.py {target.id} must be a literal: {exc}")

    runtime_host = values.get("DEFAULT_HOST")
    runtime_port = values.get("DEFAULT_PORT")
    if runtime_host != DEFAULT_HOST:
        _fail(f"runtime.py DEFAULT_HOST must be {DEFAULT_HOST!r}")
    if runtime_port != DEFAULT_PORT:
        _fail(f"runtime.py DEFAULT_PORT must be {DEFAULT_PORT!r}")
    return DEFAULT_HOST, DEFAULT_PORT


def _validate_config(root: Path) -> None:
    runtime_host, runtime_port = _validate_runtime_defaults(root)
    config = _validate_json_file(root / "config.json", "config.json")
    device_server = config.get("device_server")
    if not isinstance(device_server, dict):
        _fail("config.json must contain a device_server object")
    if device_server.get("host") != runtime_host:
        _fail(f"config.json device_server.host must match runtime default {runtime_host!r}")
    if device_server.get("port") != runtime_port:
        _fail(f"config.json device_server.port must match runtime default {runtime_port!r}")
    logging = config.get("logging")
    if not isinstance(logging, dict) or logging != {"level": "warn", "console": False}:
        _fail("config.json logging must default to warn with console disabled")


def _validate_root_layout(root: Path) -> None:
    _require_directory(root, "package root")
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            _fail(f"symbolic links are forbidden: {_relative_name(root, entry)}")
        name = entry.name
        if name in ROOT_FILES or name in IGNORED_ROOT_FILES:
            _require_regular_file(entry, name)
        elif name in ROOT_DIRECTORIES:
            _require_directory(entry, name)
        elif name in IGNORED_ROOT_DIRECTORIES:
            _require_directory(entry, name)
        else:
            _fail(f"unknown package-root path: {_relative_name(root, entry)}")
    _validate_manifest(root)
    _validate_config(root)


def _collect_vendor_files(root: Path, entries: list[Entry]) -> None:
    """原生图像依赖按逐文件摘要白名单收录，未知路径与字节均拒绝。"""
    vendor = root / "vendor"
    manifest = _validate_json_file(vendor / "manifest.json", "vendor/manifest.json")
    if (manifest.get("name"), manifest.get("version"), manifest.get("platform")) != (
        "Pillow", "12.3.0", "cp313-cp313-macosx_11_0_arm64"
    ):
        _fail("unexpected image dependency")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        _fail("image dependency file manifest missing")
    actual = set()
    for current, directories, files in os.walk(vendor, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                _fail("symbolic links are forbidden in image dependency")
        directories[:] = [d for d in directories if d != "__pycache__"]
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(vendor).as_posix()
            if relative == "manifest.json":
                continue
            _require_regular_file(path, relative)
            validate_archive_name(relative)
            if not relative.startswith(("PIL/", "licenses/")):
                _fail("unknown image dependency path: " + relative)
            if relative not in expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected[relative]:
                _fail("image dependency checksum mismatch: " + relative)
            actual.add(relative)
            entries.append((path, "vendor/" + relative))
    if actual != set(expected):
        _fail("image dependency files missing")
    entries.append((vendor / "manifest.json", "vendor/manifest.json"))


def _collect_python_tree(root: Path, start: Path, entries: list[Entry]) -> None:
    """收集白名单子目录中的 Python 源码，并拒绝其他文件类型。"""

    _require_directory(start, _relative_name(root, start))
    for current, directories, files in os.walk(start, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for directory_name in sorted(directories):
            directory = current_path / directory_name
            if directory.is_symlink():
                _fail(f"symbolic links are forbidden: {_relative_name(root, directory)}")
            if directory_name == "__pycache__":
                continue
            if not directory.is_dir():
                _fail(f"production path is not a directory: {_relative_name(root, directory)}")
            kept_directories.append(directory_name)
        directories[:] = kept_directories
        for file_name in sorted(files):
            path = current_path / file_name
            archive_name = _relative_name(root, path)
            _require_regular_file(path, archive_name)
            if path.suffix != ".py":
                _fail(f"non-Python production file is forbidden: {archive_name}")
            entries.append((path, archive_name))


def _collect_bridge_files(root: Path, entries: list[Entry]) -> None:
    package_root = root / "inkcards_anki_bridge"
    _require_directory(package_root, "inkcards_anki_bridge")
    for entry in sorted(package_root.iterdir(), key=lambda item: item.name):
        archive_name = _relative_name(root, entry)
        if entry.is_symlink():
            _fail(f"symbolic links are forbidden: {archive_name}")
        if entry.is_dir():
            if entry.name == "__pycache__":
                continue
            if entry.name not in {"anki", "device", "domain", "services", "storage", "ui"}:
                _fail(f"unknown production directory: {archive_name}")
            _collect_python_tree(root, entry, entries)
            continue
        if not entry.is_file():
            _fail(f"unsupported production path: {archive_name}")
        if entry.name in BRIDGE_TOP_FILES:
            if entry.suffix != ".py":
                _fail(f"production module must be Python: {archive_name}")
            entries.append((entry, archive_name))
        elif entry.name in IGNORED_LEGACY_BRIDGE_FILES:
            continue
        else:
            _fail(f"unknown production file: {archive_name}")


def _collect_user_files(root: Path, entries: list[Entry]) -> None:
    user_root = root / "user_files"
    _require_directory(user_root, "user_files")
    readme = user_root / "README.txt"
    for entry in sorted(user_root.iterdir(), key=lambda item: item.name):
        archive_name = _relative_name(root, entry)
        if entry.is_symlink():
            _fail(f"symbolic links are forbidden: {archive_name}")
        if entry != readme:
            _fail(f"unknown user_files path: {archive_name}")
        _require_regular_file(entry, archive_name)
    _require_regular_file(readme, USER_FILES_README)
    entries.append((readme, USER_FILES_README))


def validate_archive_name(archive_name: str) -> str:
    """验证归档成员名，拒绝绝对路径、反斜杠和 zip-slip 片段。"""

    if not isinstance(archive_name, str) or not archive_name:
        _fail("archive member name must be a non-empty string")
    if "\\" in archive_name or "\x00" in archive_name:
        _fail(f"unsafe archive member name: {archive_name!r}")
    if archive_name.startswith("/"):
        _fail(f"absolute archive member name: {archive_name!r}")
    path = PurePosixPath(archive_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail(f"zip-slip archive member name: {archive_name!r}")
    if path.as_posix() != archive_name:
        _fail(f"non-canonical archive member name: {archive_name!r}")
    return archive_name


def validate_entries(entries: Sequence[Entry]) -> list[Entry]:
    """验证待写入条目，尤其是路径、普通文件和重复归档名。"""

    seen: set[str] = set()
    validated: list[Entry] = []
    for path, archive_name in entries:
        name = validate_archive_name(archive_name)
        if name in seen:
            _fail(f"duplicate archive member name: {name}")
        seen.add(name)
        _require_regular_file(path, name)
        validated.append((path, name))
    return validated


def package_files(root: Path | None = None) -> list[Entry]:
    """按显式白名单返回生产包文件，结果按归档名稳定排序。"""

    selected_root = (root or source_root()).resolve()
    _validate_root_layout(selected_root)
    entries: list[Entry] = []
    for name in ("__init__.py", "manifest.json", "config.json", "config.md"):
        path = selected_root / name
        _require_regular_file(path, name)
        entries.append((path, name))
    _collect_vendor_files(selected_root, entries)
    _collect_user_files(selected_root, entries)
    _collect_bridge_files(selected_root, entries)
    validated = validate_entries(entries)
    return sorted(validated, key=lambda item: item[1])


def _write_zip(entries: Iterable[Entry], output: Path) -> None:
    validated = validate_entries(list(entries))
    ordered = sorted(validated, key=lambda item: item[1])
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path, archive_name in ordered:
            info = zipfile.ZipInfo(archive_name, FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.create_version = 20
            info.extract_version = 20
            info.flag_bits = 0
            info.internal_attr = 0
            info.external_attr = 0o100644 << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def write_package(root: Path, output: Path) -> list[str]:
    """原子替换输出文件并返回包内成员名。"""

    entries = package_files(root)
    destination = Path(output).resolve()
    if destination.suffix != ".ankiaddon":
        _fail("output must use the .ankiaddon suffix")
    if destination.exists() and destination.is_symlink():
        _fail(f"output symbolic link is forbidden: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        _write_zip(entries, temporary)
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
    finally:
        temporary.unlink(missing_ok=True)
    return [archive_name for _, archive_name in entries]


def sha256(path: Path) -> str:
    """返回产物 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=source_root())
    parser.add_argument(
        "--output",
        type=Path,
        default=source_root() / "dist" / ARCHIVE_FILENAME,
    )
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    entries = write_package(root, output)
    print(f"package={output}")
    print(f"entries={len(entries)}")
    for entry in entries:
        print(f"entry={entry}")
    print(f"sha256={sha256(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
