"""0600 rendezvous 文件：同目录临时文件加 fsync 后原子替换。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import fcntl

from .protocol import PROTOCOL_VERSION


@dataclass(frozen=True)
class RendezvousRecord:
    host: str
    port: int
    token: str
    instance_id: str
    profile_principal: str
    anki_version: str
    python_version: str
    bridge_version: str
    created_at_ms: int

    def to_json(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, **asdict(self)}


class RendezvousStore:
    """只管理本插件实例拥有的 rendezvous 记录。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _ensure_parent(self) -> Path:
        parent = self.path.parent
        created_parent = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if created_parent:
            os.chmod(parent, 0o700)
        return parent

    @contextmanager
    def _locked(self):
        """串行化 publish 与 clear 的整个 replace/check+unlink 临界区。"""

        parent = self._ensure_parent()
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
        try:
            os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield parent
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def publish(self, record: RendezvousRecord) -> None:
        payload = json.dumps(
            record.to_json(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        with self._locked() as parent:
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
            temporary_path = Path(temporary)
            try:
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if fd != -1:
                    os.close(fd)
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def read(self) -> dict[str, Any]:
        with self.path.open("rb") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("rendezvous record is not an object")
        return value

    def clear_if_instance(self, instance_id: str) -> bool:
        with self._locked() as parent:
            try:
                value = self.read()
            except FileNotFoundError:
                return False
            except (OSError, ValueError, json.JSONDecodeError):
                return False
            if value.get("instance_id") != instance_id:
                return False
            try:
                self._unlink_record()
            except FileNotFoundError:
                return False
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return True

    def _unlink_record(self) -> None:
        """单独的方法让测试可以确定性地暂停 unlink 临界区。"""

        self.path.unlink()
