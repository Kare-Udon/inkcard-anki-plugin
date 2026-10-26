"""电脑主动准备不可变快照；网络请求只查询状态或领取已完成的数据。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..services.snapshots import SnapshotError
from .operations import OperationState
from .snapshot_jobs import SnapshotJobState


def source_signature(collection, excluded):
    """仅在官方 QueryOp worker 中扫描，媒体原地编辑也会使版本失效。"""
    records = [
        collection.db.all("select count(*), max(mod), sum(mod) from " + table)
        for table in ("cards", "notes")
    ]
    records.append(collection.db.all("select count(*), max(id) from revlog"))
    records.append(collection.db.all("select mod, scm, conf from col"))
    records.append(collection.db.all("select * from config order by key"))
    media = []
    for entry in os.scandir(collection.media.dir()):
        if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
            stat = entry.stat(follow_symlinks=False)
            media.append((entry.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    records.extend((sorted(media), list(excluded), int(collection.sched.today)))
    raw = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        default=lambda value: bytes(value).hex(),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class PreparedSync:
    """一个 profile、一个后台作业；准备版本绑定源摘要、generation 与排除规则。"""

    def __init__(self, runtime, operations, *, clock=time.monotonic):
        self.runtime, self.operations, self.clock = runtime, operations, clock
        self.path = runtime.root / "prepared-sync.json"
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="inkcards-prepare"
        )
        self._closed = False
        self._job = None
        self._packing = None
        self._scan_busy = False
        self._next_scan = 0
        self._ready = None
        self._signature = None
        self._generation = None
        self._scope = None
        self._error = None
        self._revision = 0
        self._tokens = {}
        self._timer = None
        try:
            saved = json.loads(self.path.read_text())
            self._saved = (
                saved if saved.get("schema") == "inkcards-prepared-v2" else None
            )
        except (OSError, ValueError, AttributeError):
            self._saved = None

    def invalidate(self):
        with self._lock:
            self._revision += 1
            self._ready = None
            self._error = None
            self._next_scan = 0

    def status(self):
        with self._lock:
            return {
                "state": "failed"
                if self._error
                else "ready"
                if self._ready
                else "preparing",
                "stage": "packing"
                if self._packing
                else "exporting"
                if self._job
                else "scanning",
                "error_code": self._error,
            }

    def tick(self):
        """Qt timer 只调度，不读文件/collection，不等待后台作业。"""
        with self._lock:
            if self._closed:
                return
            job, packing = self._job, self._packing
        if packing is not None:
            return
        if job is not None:
            state = job.poll_state()
            if state in (
                None,
                SnapshotJobState.CREATED,
                SnapshotJobState.QUEUED,
                SnapshotJobState.STARTED,
                SnapshotJobState.PUBLISHING,
            ):
                return
            with self._lock:
                self._job = None
            if state is SnapshotJobState.PUBLISHED:
                with self._lock:
                    self._packing = True
                self._pool.submit(self._pack, job.result, self._revision)
            else:
                with self._lock:
                    self._ready = None
                    self._error = (
                        getattr(job.error, "code", None) or "snapshot_refresh_failed"
                    )
                    self._next_scan = self.clock() + 5
            return
        with self._lock:
            if self._scan_busy or self.clock() < self._next_scan:
                return
            self._scan_busy = True
            self._next_scan = self.clock() + 3
        # 上次发布已结束，以下短操作不与发布的大锁竞争。
        try:
            context = self.runtime.snapshot_context()
            excluded = tuple(self.runtime.get_sync_policy()["excluded_deck_ids"])
            self.operations.submit(
                context,
                lambda col: source_signature(col, excluded),
                on_complete=lambda result: self._scanned(result, excluded),
            )
        except Exception as error:  # noqa: BLE001 - 后台边界将所有失败记入状态与日志。
            self._failed(error)

    def _scanned(self, result, excluded):
        with self._lock:
            self._scan_busy = False
            if self._closed:
                return
        if result.state is not OperationState.COMPLETED:
            self._failed(result.error or RuntimeError("source scan failed"))
            return
        try:
            signature = result.value
            generation = self.runtime.collection_generation
            scope = self.runtime.default_sync_scope_id()
            with self._lock:
                if self._ready and (signature, generation, scope) == (
                    self._signature,
                    self._generation,
                    self._scope,
                ):
                    return
                prior_signature = self._signature or (self._saved or {}).get(
                    "signature"
                )
                prior_generation = (
                    self._generation
                    if self._generation is not None
                    else (self._saved or {}).get("generation")
                )
                saved = self._saved
                self._saved = None
            if saved and (signature, generation, scope) == (
                saved.get("signature"),
                saved.get("generation"),
                saved.get("scope_id"),
            ):
                snapshot = self.runtime.store.get_snapshot(
                    saved["snapshot_id"], scope_id=scope
                )
                manifest = self.runtime.router.snapshot_files_service._manifest(
                    snapshot.snapshot_id
                )
                if manifest is not None and snapshot.snapshot_generation == generation:
                    with self._lock:
                        self._ready = saved
                        self._signature = signature
                        self._generation = generation
                        self._scope = scope
                        self._error = None
                    return
            # operation hook 已推进的 generation 不再重复推进；单独媒体变动也推进。
            if (
                prior_signature is not None
                and signature != prior_signature
                and generation == prior_generation
            ):
                generation = self.runtime.note_external_collection_change()
            with self._lock:
                self._signature, self._generation, self._scope = (
                    signature,
                    generation,
                    scope,
                )
                self._ready = None
                self._error = None
            watermark = self._watermark()
            job = self.runtime.snapshot_jobs.request_snapshot(
                excluded_deck_ids=excluded, applied_watermark=watermark
            )
            with self._lock:
                self._job = job
        except Exception as error:  # noqa: BLE001 - 后台边界将所有失败记入状态与日志。
            self._failed(error)

    def _watermark(self):
        store = self.runtime.store
        sessions = store._read(
            lambda db: db.execute(
                "select device_id,session_id from sessions where profile_id=? and closed_at_ms is null",
                (store.profile_id,),
            ).fetchall()
        )
        return max(
            [0]
            + [
                store.pull_gate(row[0], row[1]).applied_watermark or 0
                for row in sessions
            ]
        )

    def _pack(self, publication, revision):
        try:
            store = self.runtime.store
            snapshot = store.get_snapshot(
                publication.snapshot_id, scope_id=publication.scope_id
            )
            self.runtime.router.snapshot_files_service._prepare_snapshot(snapshot)
            saved = {
                "schema": "inkcards-prepared-v2",
                "signature": self._signature,
                "generation": publication.snapshot_generation,
                "scope_id": publication.scope_id,
                "snapshot_id": publication.snapshot_id,
                "watermark": snapshot.applied_watermark,
            }
            with self._lock:
                if self._closed or revision != self._revision:
                    return
            if publication.snapshot_generation != self.runtime.collection_generation:
                return
            fd, name = tempfile.mkstemp(
                prefix="prepared-", suffix=".tmp", dir=self.runtime.root
            )
            try:
                with os.fdopen(fd, "w") as file:
                    json.dump(saved, file)
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
            with self._lock:
                if not self._closed and revision == self._revision:
                    self._ready = saved
                    self._error = None
        except Exception as error:  # noqa: BLE001 - 后台边界将所有失败记入状态与日志。
            self._failed(error)
        finally:
            with self._lock:
                self._packing = None

    def _failed(self, error):
        with self._lock:
            self._scan_busy = False
            self._ready = None
            self._error = getattr(error, "code", None) or "snapshot_prepare_failed"
            self._next_scan = self.clock() + 5
        self.runtime.logging_core.log_exception(
            "preparation", "failed", error, stage="preparation"
        )

    def refresh(
        self,
        *,
        device_id,
        session_id,
        scope_id,
        base_snapshot_id=None,
        refresh_token=None,
    ):
        """不导出、不转换、不打包；只返回已准备版本或快速 pending。"""
        now = self.clock()
        binding = (device_id, session_id, scope_id, base_snapshot_id)
        with self._lock:
            self._tokens = {
                key: item for key, item in self._tokens.items() if now - item[1] < 600
            }
            if refresh_token is None:
                if len(self._tokens) >= 64:
                    raise SnapshotError(
                        "snapshot_refresh_busy", status=503, retryable=True
                    )
                refresh_token = "r_" + secrets.token_hex(16)
                self._tokens[refresh_token] = (binding, now)
            elif self._tokens.get(refresh_token, (None,))[0] != binding:
                raise SnapshotError("snapshot_refresh_token_invalid", status=409)
            ready, error = self._ready, self._error
        gate = self.runtime.store.pull_gate(device_id, session_id)
        if not gate.allowed:
            raise SnapshotError(gate.reason or "pull_gate_blocked", status=409)
        if scope_id != self.runtime.default_sync_scope_id():
            raise SnapshotError("snapshot_refresh_scope_changed", status=409)
        common = {
            "protocol": "x4-2",
            "profile_id": self.runtime.profile_id,
            "scope_id": scope_id,
            "refresh_token": refresh_token,
        }
        if error:
            raise SnapshotError(error, status=409)
        if (
            ready
            and ready["generation"] == self.runtime.collection_generation
            and ready["watermark"] >= (gate.applied_watermark or 0)
        ):
            return 200, {
                **common,
                "state": "ready",
                "mode": "full",
                "snapshot_id": ready["snapshot_id"],
                "snapshot_generation": ready["generation"],
            }
        return 202, {
            **common,
            "state": "pending",
            "preparation": self.status()["stage"],
        }

    def close(self):
        with self._lock:
            self._closed = True
            self._ready = None
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
