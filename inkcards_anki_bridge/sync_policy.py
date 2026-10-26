"""按 profile 原子持久化的同步排除策略；损坏时关闭同步入口。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Sequence


class SyncPolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def normalize_excluded_ids(ids: Sequence[str]) -> list[str]:
    if not isinstance(ids, (list, tuple)) or len(ids) > 4096:
        raise SyncPolicyError("sync_policy_invalid", "排除牌组必须是最多4096项的ID列表")
    if any(not isinstance(item, str) or not item.isascii() or not item.isdigit()
           or int(item) <= 0 for item in ids):
        raise SyncPolicyError("sync_policy_invalid", "牌组ID必须是正整数字符串")
    return sorted({str(int(item)) for item in ids}, key=int)


class SyncPolicyStore:
    def __init__(self, root: Path, profile_id: str) -> None:
        self.path = root / "sync-policy.json"
        self.profile_id = profile_id
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"excluded_deck_ids": [], "revision": 0}
            except (OSError, ValueError) as exc:
                raise SyncPolicyError("sync_policy_corrupt", "无法读取排除策略；已阻断同步") from exc
            try:
                if not isinstance(raw, dict) or set(raw) != {"profile_id", "excluded_deck_ids", "revision"}:
                    raise ValueError()
                if raw["profile_id"] != self.profile_id or type(raw["revision"]) is not int or raw["revision"] < 0:
                    raise ValueError()
                ids = normalize_excluded_ids(raw["excluded_deck_ids"])
                if ids != raw["excluded_deck_ids"]:
                    raise ValueError()
            except (ValueError, SyncPolicyError) as exc:
                raise SyncPolicyError("sync_policy_corrupt", "排除策略内容损坏；已阻断同步") from exc
            return {"excluded_deck_ids": ids, "revision": raw["revision"]}

    def save(self, ids: Sequence[str]) -> dict[str, Any]:
        selected = normalize_excluded_ids(ids)
        with self._lock:
            previous = self.read()
            if previous["excluded_deck_ids"] == selected:
                return previous
            result = {"excluded_deck_ids": selected, "revision": previous["revision"] + 1}
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".sync-policy-", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump({"profile_id": self.profile_id, **result}, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except OSError as exc:
                raise SyncPolicyError("sync_policy_save_failed", "排除策略保存失败；原策略保留") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return result
