"""为 Anki profile 提供本机隔离且可恢复的随机身份。

profile 名称只用于 UI 展示，不能作为持久目录名或身份。这里仅使用 profile
folder 的不可逆 SHA-256 作为本机 namespace；namespace 内的身份文件保存一个
随机 UUID。身份文件损坏、权限不安全或 schema 不明时一律 fail closed。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple, Union


IDENTITY_SCHEMA = 1
IDENTITY_FILENAME = "profile_identity.json"
_IDENTITY_MODE = 0o600
_DIRECTORY_MODE = 0o700
_LOCK = threading.RLock()


class ProfileIdentityError(RuntimeError):
    """身份文件不可安全读取或创建。错误文本不包含 profile 路径。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProfileIdentity:
    """一个 profile 的稳定随机身份及其插件数据 namespace。"""

    profile_id: str
    namespace: str
    root: Path
    identity_path: Path

    @property
    def profile_root(self) -> Path:
        """与 ``root`` 同义的明确命名，供 runtime factory 使用。"""

        return self.root


def profile_namespace(profile_folder: Union[str, os.PathLike[str]]) -> str:
    """返回只用于本机隔离的不可逆 profile folder hash。"""

    if isinstance(profile_folder, (str, os.PathLike)):
        raw_path = os.fspath(profile_folder)
    else:  # pragma: no cover - 防御性分支
        raise ProfileIdentityError("invalid_profile_folder")
    if isinstance(raw_path, bytes):
        try:
            raw_path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            raise ProfileIdentityError("invalid_profile_folder") from None
    if not isinstance(raw_path, str) or not raw_path:
        raise ProfileIdentityError("invalid_profile_folder")
    try:
        canonical = os.path.realpath(os.path.abspath(raw_path))
        encoded = canonical.encode("utf-8", "strict")
    except (OSError, UnicodeError):
        raise ProfileIdentityError("invalid_profile_folder") from None
    return hashlib.sha256(encoded).hexdigest()


def _mkdir_secure(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
        try:
            os.chmod(str(path), _DIRECTORY_MODE)
        except OSError:
            # 文件本身仍会以 0600 写入；目录权限失败由后续文件操作决定。
            pass
    except OSError:
        raise ProfileIdentityError("identity_directory_unavailable") from None


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError("non_finite_json")


def _read_identity(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ProfileIdentityError("identity_missing") from None
    except OSError:
        raise ProfileIdentityError("identity_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != _IDENTITY_MODE:
        raise ProfileIdentityError("identity_permissions")
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        raise ProfileIdentityError("identity_invalid_json") from None
    if not isinstance(value, dict) or set(value) != {"schema", "profile_id"}:
        raise ProfileIdentityError("identity_schema")
    if value.get("schema") != IDENTITY_SCHEMA:
        raise ProfileIdentityError("identity_schema")
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str):
        raise ProfileIdentityError("identity_profile_id")
    try:
        parsed = uuid.UUID(profile_id)
    except (ValueError, AttributeError):
        raise ProfileIdentityError("identity_profile_id") from None
    if str(parsed) != profile_id or parsed.version != 4:
        raise ProfileIdentityError("identity_profile_id")
    return profile_id


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_identity(path: Path, profile_id: str) -> None:
    payload = json.dumps(
        {"profile_id": profile_id, "schema": IDENTITY_SCHEMA},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".profile_identity.", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, _IDENTITY_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
        _fsync_directory(path.parent)
        os.chmod(str(path), _IDENTITY_MODE)
    except OSError:
        raise ProfileIdentityError("identity_write_failed") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def load_or_create_profile_identity(
    user_files_root: Union[str, os.PathLike[str]],
    profile_folder: Union[str, os.PathLike[str]],
) -> ProfileIdentity:
    """读取或原子创建当前 profile 的随机 UUID identity。"""

    try:
        user_root = Path(user_files_root)
    except (TypeError, ValueError):
        raise ProfileIdentityError("invalid_user_files_root") from None
    namespace = profile_namespace(profile_folder)
    profile_root = user_root / "profiles" / namespace
    identity_path = profile_root / IDENTITY_FILENAME
    with _LOCK:
        _mkdir_secure(profile_root)
        try:
            identity_path.lstat()
        except FileNotFoundError:
            profile_id = str(uuid.uuid4())
            _write_identity(identity_path, profile_id)
        except OSError:
            raise ProfileIdentityError("identity_unreadable") from None
        else:
            profile_id = _read_identity(identity_path)
    return ProfileIdentity(profile_id, namespace, profile_root, identity_path)


class ProfileIdentityStore:
    """绑定一个插件 ``user_files`` 根目录的可测试 identity facade。"""

    def __init__(self, user_files_root: Union[str, os.PathLike[str]]) -> None:
        self.user_files_root = Path(user_files_root)

    def load_or_create(
        self, profile_folder: Union[str, os.PathLike[str]]
    ) -> ProfileIdentity:
        return load_or_create_profile_identity(self.user_files_root, profile_folder)

    resolve = load_or_create


# 对后续 Qt/UI 接线保留短别名；它们都走同一个 fail-closed 实现。
resolve_profile_identity = load_or_create_profile_identity
load_or_create_identity = load_or_create_profile_identity


__all__ = [
    "IDENTITY_FILENAME",
    "IDENTITY_SCHEMA",
    "ProfileIdentity",
    "ProfileIdentityError",
    "ProfileIdentityStore",
    "load_or_create_identity",
    "load_or_create_profile_identity",
    "profile_namespace",
    "resolve_profile_identity",
]
