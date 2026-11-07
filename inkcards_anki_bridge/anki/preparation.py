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
from dataclasses import dataclass
from pathlib import Path

from ..services.snapshots import SnapshotError
from ..storage import StorageError
from .operations import OperationState
from .snapshot_jobs import SnapshotCaptureDeferred, SnapshotJobState


@dataclass(frozen=True)
class SourceSignature:
    signature: str
    media_dir: str
    media: str
    day_cutoff: int


def media_entries(directory):
    with os.scandir(directory) as entries:
        return sorted(
            (entry.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            for entry in entries
            if not entry.name.startswith(".") and entry.is_file(follow_symlinks=False)
            for stat in (entry.stat(follow_symlinks=False),)
        )


def media_signature(directory):
    return hashlib.sha256(json.dumps(media_entries(directory)).encode()).hexdigest()


def source_signature(collection, excluded):
    """仅在官方 QueryOp worker 中扫描，媒体原地编辑也会使版本失效。"""
    records = [
        collection.db.all("select count(*), max(mod), sum(mod) from " + table)
        for table in ("cards", "notes")
    ]
    records.append(collection.db.all("select count(*), max(id) from revlog"))
    records.append(collection.db.all("select mod, scm, conf from col"))
    records.append(collection.db.all("select * from config order by key"))
    media_dir = collection.media.dir()
    media = media_entries(media_dir)
    records.extend((media, list(excluded), int(collection.sched.today)))
    raw = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        default=lambda value: bytes(value).hex(),
    )
    return SourceSignature(
        hashlib.sha256(raw.encode()).hexdigest(),
        str(media_dir),
        hashlib.sha256(json.dumps(media).encode()).hexdigest(),
        int(collection.sched.day_cutoff),
    )


class PreparedSync:
    """一个 profile、一个后台作业；准备版本绑定源摘要、generation 与排除规则。"""

    def __init__(self, runtime, operations, *, clock=time.monotonic, on_demand=False, host_available=lambda: True):
        self.runtime, self.operations, self.clock = runtime, operations, clock
        self.path = runtime.root / "prepared-sync.json"
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="inkcards-prepare"
        )
        self._closed = False
        self._job = None
        self._job_revision = None
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
        self._source = None
        self._source_recheck = False
        self._source_revision = 0
        self._on_demand = on_demand
        self._requested = False
        self._validated_at = None
        self._host_available = host_available
        try:
            saved = json.loads(self.path.read_text())
            self._saved = (
                saved if saved.get("schema") == "inkcards-prepared-v2" else None
            )
        except (OSError, ValueError, AttributeError):
            self._saved = None
        bases = (self._saved or {}).get("fast_bases", {})
        self._fast_bases = bases if isinstance(bases, dict) else {}

    def can_skip_pull(self, device_id, session_id, snapshot_id):
        """只读证明。旧基线、换日、媒体变化或来源不明均走原拉取路径。"""
        with self._lock:
            base = self._fast_bases.get(snapshot_id)
            source = self._source
            if (
                self._closed
                or self._source_recheck
                or not isinstance(base, dict)
                or source is None
            ):
                return False
        cutoff = base.get("day_cutoff")
        if (
            not isinstance(cutoff, int)
            or source.day_cutoff != cutoff
            or time.time() >= cutoff
        ):
            return False
        try:
            if base.get("media") != media_signature(source.media_dir):
                return False
            skip = self.runtime.store.snapshot_has_only_own_reviews(
                device_id, session_id, snapshot_id, self.runtime.default_sync_scope_id()
            )
            with self._lock:
                return skip and not self._closed and not self._source_recheck
        except (OSError, RuntimeError, StorageError):
            return False

    @staticmethod
    def _remember_fast_base(bases, snapshot_id, source):
        if source is not None:
            bases.pop(snapshot_id, None)
            bases[snapshot_id] = {
                "media": source.media,
                "day_cutoff": source.day_cutoff,
            }

    def invalidate(self):
        with self._lock:
            self._revision += 1
            self._ready = None
            self._error = None
            self._next_scan = 0

    def recheck_source(self):
        with self._lock:
            self._source_recheck = True
            self._source_revision += 1
            self._revision += 1
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
            job, job_revision, packing = self._job, self._job_revision, self._packing
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
                if job_revision != self._revision:
                    return
            if state is SnapshotJobState.PUBLISHED:
                with self._lock:
                    self._packing = True
                self._pool.submit(self._pack, job.result, job_revision)
            elif state is SnapshotJobState.STALE_RESULT_DROPPED and job.error is None:
                # 编码期间有新变更，旧版本作废，下次 tick 重新准备。
                self.invalidate()
            else:
                with self._lock:
                    self._ready = None
                    self._error = (
                        getattr(job.error, "code", None) or "snapshot_refresh_failed"
                    )
                    self._next_scan = self.clock() + 5
            return
        with self._lock:
            if (
                (self._on_demand and not self._requested)
                or not self._host_available()
                or self._scan_busy
                or self.clock() < self._next_scan
            ):
                return
            self._scan_busy = True
            self._next_scan = self.clock() + 3
            source_revision = self._source_revision
        # 上次发布已结束，以下短操作不与发布的大锁竞争。
        try:
            context = self.runtime.snapshot_context()
            excluded = tuple(self.runtime.get_sync_policy()["excluded_deck_ids"])
            self.operations.submit(
                context,
                lambda col: self._read_source(col, excluded),
                on_complete=lambda result: self._scanned(
                    result, excluded, source_revision
                ),
            )
        except Exception as error:  # noqa: BLE001 - 后台边界将所有失败记入状态与日志。
            self._failed(error)

    def _read_source(self, collection, excluded):
        if not self._host_available():
            raise SnapshotCaptureDeferred()
        return source_signature(collection, excluded)

    def _scanned(self, result, excluded, source_revision):
        with self._lock:
            self._scan_busy = False
            if self._closed:
                return
            if source_revision != self._source_revision:
                self._next_scan = 0
                return
            if isinstance(result.error, SnapshotCaptureDeferred):
                self._next_scan = 0
                return
            if (
                result.state is OperationState.STALE_RESULT_DROPPED
                and result.error is None
            ):
                # 评分写回可能推进版本。旧结果已丢弃，下次 tick 重新扫描。
                self._ready = None
                self._error = None
                self._next_scan = 0
                return
        if result.state is not OperationState.COMPLETED:
            self._failed(result.error or RuntimeError("source scan failed"))
            return
        try:
            source = result.value if isinstance(result.value, SourceSignature) else None
            signature = source.signature if source is not None else result.value
            generation = self.runtime.collection_generation
            scope = self.runtime.default_sync_scope_id()
            with self._lock:
                if source_revision != self._source_revision:
                    return
                if self._ready and (signature, generation, scope) == (
                    self._signature,
                    self._generation,
                    self._scope,
                ):
                    self._source = source
                    self._source_recheck = False
                    self._requested = False
                    self._validated_at = self.clock()
                    return
                recheck = self._source_recheck
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
                        self._source = source
                        if source_revision == self._source_revision:
                            self._source_recheck = False
                            self._requested = False
                            self._validated_at = self.clock()
                        self._remember_fast_base(
                            self._fast_bases, snapshot.snapshot_id, source
                        )
                    return
            # operation hook 已推进的 generation 不再重复推进；单独媒体变动也推进。
            if (
                prior_signature is not None
                and signature != prior_signature
                and (generation == prior_generation or recheck)
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
                self._source = source
                if source_revision == self._source_revision:
                    self._source_recheck = False
                job_revision = self._revision
            watermark = self._watermark()
            job = self.runtime.snapshot_jobs.request_snapshot(
                excluded_deck_ids=excluded, applied_watermark=watermark
            )
            with self._lock:
                self._job = job
                self._job_revision = job_revision
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
        started = self.clock()
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
                bases = dict(self._fast_bases)
                self._remember_fast_base(bases, snapshot.snapshot_id, self._source)
            if len(bases) > 64:
                active_bases = store.active_artifact_bases()
                recent = set(list(bases)[-64:])
                bases = {
                    key: value
                    for key, value in bases.items()
                    if key in active_bases or key in recent
                }
            saved["fast_bases"] = bases
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
                    self._fast_bases = bases
                    self._ready = saved
                    self._error = None
                    self._requested = False
                    self._validated_at = self.clock()
        except Exception as error:  # noqa: BLE001 - 后台边界将所有失败记入状态与日志。
            self._failed(error)
        finally:
            with self._lock:
                self._packing = None
            self.runtime.logging_core.timing("artifact_pack", int((self.clock() - started) * 1000))

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
        gate = self.runtime.store.pull_gate(device_id, session_id)
        if not gate.allowed:
            raise SnapshotError(gate.reason or "pull_gate_blocked", status=409)
        if scope_id != self.runtime.default_sync_scope_id():
            raise SnapshotError("snapshot_refresh_scope_changed", status=409)
        with self._lock:
            if self._on_demand and not self._requested and (
                self._ready is None or self._source_recheck or self._validated_at is None
                or now - self._validated_at >= 3
            ):
                self._requested = True
                self._next_scan = 0
            ready = self._ready if not self._source_recheck and not self._requested else None
            error = self._error
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
