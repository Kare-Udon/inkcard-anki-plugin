"""普通 Python 可导入的 InkCards add-on 运行时。

一个 :class:`AddonRuntime` 只拥有一个 profile 的 ProfileStore、设备认证、收件、
只读 snapshot facade、route dispatcher 和有界 DeviceServer。这里不导入 ``aqt``，
不访问 Anki collection，也不依赖旧的进程间发现/loopback bridge。
"""

from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .device.auth import DeviceAuthenticator
from .device.routes import DeviceRouter, RouteLimits
from .device.server import DeviceServer, ServerLimits
from .anki.catalog_jobs import CatalogJob, CatalogJobManager, DeckCatalog, DeckPreflight
from .anki.compact_jobs import CompactJob, CompactJobManager
from .anki.operations import OperationContext
from .anki.snapshot_jobs import SnapshotJob, SnapshotJobError, SnapshotJobManager, SnapshotJobState
from .services.inbox import InboxService
from .services.snapshots import PublishedSnapshot, SnapshotError, SnapshotService
from .storage import ProfileStore, SCHEMA_VERSION


DEFAULT_HOST = "127.0.0.1"
# 8765 是 AnkiConnect 的常用默认端口；插件使用独立端口，仍允许用户显式配置。
DEFAULT_PORT = 8766
P3_WRITE_DISABLED_REASON = (
    "P3 blocked: historical application and crash-recovery evidence is incomplete"
)


def validate_bind_host(value: str) -> str:
    """只接受显式 IPv4/IPv6 字面量，不触发主机名解析。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("host must be an IP literal")
    selected = value.strip()
    try:
        ipaddress.ip_address(selected)
    except ValueError:
        raise ValueError("host must be an IP literal") from None
    return selected


def validate_bind_port(value: int) -> int:
    """校验允许操作系统分配的 0 以及完整 TCP 端口范围。"""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return value


class AddonRuntime:
    """一个 profile 的设备服务及其插件持久状态。"""

    def __init__(
        self,
        *,
        profile_id: str,
        root: Union[str, Path, None] = None,
        profile_root: Union[str, Path, None] = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        route_limits: Optional[RouteLimits] = None,
        server_limits: Optional[ServerLimits] = None,
    ) -> None:
        if root is None:
            root = profile_root
        if root is None:
            raise TypeError("root is required")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("profile_id is required")
        self._profile_id = profile_id
        self._root = Path(root)
        self._host = validate_bind_host(host)
        self._port = validate_bind_port(port)
        self._route_limits = route_limits
        self._server_limits = server_limits
        self._lock = threading.RLock()
        self._state = "stopped"
        self._accepting = False
        self._runtime_epoch: Optional[str] = None
        self._collection_generation: Optional[int] = None
        self._last_bind: Optional[Tuple[str, int]] = None
        self._last_snapshot: Optional[PublishedSnapshot] = None
        self._last_counts: Dict[str, Any] = {}
        self._store: Optional[ProfileStore] = None
        self._auth: Optional[DeviceAuthenticator] = None
        self._inbox: Optional[InboxService] = None
        self._snapshots: Optional[SnapshotService] = None
        self._router: Optional[DeviceRouter] = None
        self._server: Optional[DeviceServer] = None
        self._snapshot_jobs: Optional[SnapshotJobManager] = None
        self._catalog_jobs: Optional[CatalogJobManager] = None
        self._compact_jobs: Optional[CompactJobManager] = None
        self._compact_capability: Dict[str, Any] = {}
        self._refresh_lock = threading.RLock()
        self._refresh_requests: Dict[str, Dict[str, Any]] = {}

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def root(self) -> Path:
        return self._root

    @property
    def runtime_epoch(self) -> Optional[str]:
        with self._lock:
            return self._runtime_epoch

    @property
    def collection_generation(self) -> Optional[int]:
        with self._lock:
            return self._collection_generation

    @property
    def store(self) -> Optional[ProfileStore]:
        return self._store

    @property
    def auth(self) -> Optional[DeviceAuthenticator]:
        return self._auth

    @property
    def inbox(self) -> Optional[InboxService]:
        return self._inbox

    @property
    def snapshots(self) -> Optional[SnapshotService]:
        return self._snapshots

    @property
    def router(self) -> Optional[DeviceRouter]:
        return self._router

    @property
    def server(self) -> Optional[DeviceServer]:
        return self._server

    @property
    def snapshot_jobs(self) -> Optional[SnapshotJobManager]:
        return self._snapshot_jobs

    @property
    def catalog_jobs(self) -> Optional[CatalogJobManager]:
        return self._catalog_jobs

    @property
    def compact_jobs(self) -> Optional[CompactJobManager]:
        return self._compact_jobs

    @property
    def last_snapshot(self) -> Optional[PublishedSnapshot]:
        with self._lock:
            return self._last_snapshot

    @property
    def last_catalog(self) -> Optional[DeckCatalog]:
        with self._lock:
            if self._state != "running" or self._catalog_jobs is None:
                return None
            manager = self._catalog_jobs
        return manager.last_catalog

    @property
    def last_preflight(self) -> Optional[DeckPreflight]:
        with self._lock:
            if self._state != "running" or self._catalog_jobs is None:
                return None
            manager = self._catalog_jobs
        return manager.last_preflight

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state == "running" and self._server is not None

    @property
    def accepting_requests(self) -> bool:
        with self._lock:
            return self._accepting

    @property
    def server_address(self) -> Optional[Tuple[str, int]]:
        with self._lock:
            return self._last_bind

    @property
    def host(self) -> str:
        address = self.server_address
        return address[0] if address is not None else self._host

    @property
    def port(self) -> int:
        address = self.server_address
        return address[1] if address is not None else self._port

    def start(self) -> "AddonRuntime":
        """创建并启动所有 profile 资源；已运行时幂等返回自身。"""

        with self._lock:
            if self._state == "running":
                return self
            if self._state in {"starting", "stopping"}:
                raise RuntimeError("addon runtime transition is already in progress")
            self._state = "starting"
            self._accepting = False
            runtime_epoch = secrets.token_hex(16)
            store: Optional[ProfileStore] = None
            auth: Optional[DeviceAuthenticator] = None
            inbox: Optional[InboxService] = None
            snapshots: Optional[SnapshotService] = None
            router: Optional[DeviceRouter] = None
            server: Optional[DeviceServer] = None
            try:
                store = ProfileStore(self._root, profile_id=self._profile_id)
                collection_generation = store.get_collection_generation()
                auth = DeviceAuthenticator(store)
                inbox = InboxService(store)
                snapshots = SnapshotService(store)
                # P4 snapshot/pull route 只有在这里明确注入后才存在；没有发布
                # snapshot 或有 pending/unknown application 时，storage gate 仍拒绝 pull。
                router = DeviceRouter(
                    auth,
                    inbox,
                    snapshot_service=snapshots,
                    refresh_handler=self.refresh_pull,
                    compact_job_requester=self.request_compact_apply,
                    compact_job_status_reader=self.compact_apply_status,
                    compact_capability_reader=self.compact_capabilities,
                    limits=self._route_limits,
                )
                server = DeviceServer(
                    router,
                    host=self._host,
                    port=self._port,
                    limits=self._server_limits,
                )
                server.start()
                self._store = store
                self._auth = auth
                self._inbox = inbox
                self._snapshots = snapshots
                self._router = router
                self._server = server
                self._snapshot_jobs = None
                self._catalog_jobs = None
                self._compact_jobs = None
                self._runtime_epoch = runtime_epoch
                self._collection_generation = collection_generation
                self._last_bind = tuple(server.server_address)
                self._last_snapshot = None
                self._state = "running"
                self._accepting = True
                return self
            except BaseException:
                try:
                    self._stop_server(server)
                finally:
                    self._close_services(snapshots, store)
                self._store = None
                self._auth = None
                self._inbox = None
                self._snapshots = None
                self._router = None
                self._server = None
                self._snapshot_jobs = None
                self._catalog_jobs = None
                self._runtime_epoch = None
                self._collection_generation = None
                self._state = "stopped"
                self._accepting = False
                raise

    def stop(self, timeout: Optional[float] = None) -> None:
        """拒绝新请求后有界停止 server，再关闭 services/store；可重复调用。"""

        with self._lock:
            if self._state == "stopped" and self._server is None and self._store is None:
                return
            if self._state == "stopping":
                return
            self._state = "stopping"
            self._accepting = False
            server = self._server
            snapshots = self._snapshots
            store = self._store
            snapshot_jobs = self._snapshot_jobs
            catalog_jobs = self._catalog_jobs
            compact_jobs = self._compact_jobs
            self._server = None
            self._router = None
            self._auth = None
            self._inbox = None
            self._snapshots = None
            self._store = None
            self._snapshot_jobs = None
            self._catalog_jobs = None
            self._compact_jobs = None
            # 立即使已排队/已运行的 QueryOp、CollectionOp 上下文失效；实际
            # closure 可能仍由 Anki 宿主完成，但不能再更新旧 runtime 缓存。
            self._runtime_epoch = None
        try:
            if snapshot_jobs is not None:
                snapshot_jobs.stop()
            if catalog_jobs is not None:
                catalog_jobs.stop()
            if compact_jobs is not None:
                compact_jobs.stop()
            self._stop_server(server, timeout=timeout)
        finally:
            self._close_services(snapshots, store)
            with self._refresh_lock:
                self._refresh_requests.clear()
            with self._lock:
                self._runtime_epoch = None
                self._last_snapshot = None
                self._state = "stopped"
                self._accepting = False

    close = stop
    shutdown = stop

    def issue_pairing_code(self, **kwargs: Any) -> str:
        """给后续 Qt UI 的窄配对入口；不返回长期 token。"""

        auth = self._require_running().auth
        assert auth is not None
        return auth.issue_pairing_code(**kwargs)

    def revoke_device(self, device_id: str) -> None:
        """撤销一个设备及其后续认证能力。"""

        store = self._require_running().store
        assert store is not None
        store.revoke_device(device_id)

    def publish_snapshot(
        self,
        snapshot: Any,
        *,
        scope_id: str,
        expected_context: Any = None,
        **kwargs: Any,
    ) -> PublishedSnapshot:
        """发布已准备好的只读 snapshot，并原子校验可选的启动上下文。"""

        with self._lock:
            if self._state != "running" or self._server is None:
                raise RuntimeError("addon runtime is not running")
            service = self._snapshots
            assert service is not None
            if expected_context is not None:
                expected = OperationContext.from_value(expected_context)
                current = self.snapshot_context()
                if current != expected:
                    raise RuntimeError("snapshot context changed before publication")
            if "generation" not in kwargs and self._collection_generation is not None:
                kwargs["generation"] = self._collection_generation
            # 保持 runtime lock 直到 SnapshotService 完成事务，使 generation
            # 更新和 expected-context 校验之间没有可发布的竞态窗口。
            result = service.publish(snapshot, scope_id=scope_id, **kwargs)
            self._last_snapshot = result
            return result

    def snapshot_context(self) -> Optional[OperationContext]:
        """返回 QueryOp 作业必须绑定的 profile/generation/epoch 三元组。"""

        with self._lock:
            if self._state != "running" or self._runtime_epoch is None:
                return None
            if self._collection_generation is None:
                return None
            return OperationContext(
                self._profile_id,
                self._collection_generation,
                self._runtime_epoch,
            )

    def attach_snapshot_jobs(self, manager: SnapshotJobManager) -> SnapshotJobManager:
        """由 plugin.py 在 Anki 宿主内注入 QueryOp-backed job manager。"""

        if not isinstance(manager, SnapshotJobManager):
            raise TypeError("manager must be a SnapshotJobManager")
        with self._lock:
            if self._state != "running":
                raise RuntimeError("addon runtime is not running")
            if self._snapshot_jobs is not None:
                raise RuntimeError("snapshot job manager is already attached")
            self._snapshot_jobs = manager
        return manager

    def attach_catalog_jobs(self, manager: CatalogJobManager) -> CatalogJobManager:
        """由 plugin.py 在 Anki 宿主内注入 QueryOp-backed 目录任务。"""

        if not isinstance(manager, CatalogJobManager):
            raise TypeError("manager must be a CatalogJobManager")
        with self._lock:
            if self._state != "running":
                raise RuntimeError("addon runtime is not running")
            if self._catalog_jobs is not None:
                raise RuntimeError("catalog job manager is already attached")
            self._catalog_jobs = manager
        return manager

    def attach_compact_jobs(self, manager: CompactJobManager) -> CompactJobManager:
        """由 plugin.py 注入一事件一 CollectionOp 的 compact 应用管理器。"""

        if not isinstance(manager, CompactJobManager):
            raise TypeError("manager must be a CompactJobManager")
        with self._lock:
            if self._state != "running":
                raise RuntimeError("addon runtime is not running")
            if self._compact_jobs is not None:
                raise RuntimeError("compact job manager is already attached")
            self._compact_jobs = manager
        return manager

    def request_compact_apply(self, device_id: str, session_id: str) -> CompactJob:
        """只排队，不接触 Anki Collection；可从设备 HTTP worker 调用。"""

        with self._lock:
            manager = self._compact_jobs
            running = self._state == "running"
        if not running or manager is None:
            raise RuntimeError("compact jobs are unavailable")
        return manager.request(device_id, session_id)

    def request_approved_adopted_recovery(self) -> CompactJob:
        """显式本机 UI 入口；核验本地来源后仅排原会话的既有宿主作业。"""
        from .services.compact_application import CompactApplicationError, CompactApplicationService

        with self._lock:
            store, manager = self._store, self._compact_jobs
            if self._state != "running" or store is None or manager is None:
                raise CompactApplicationError("runtime_stopped", "恢复宿主当前不可用")
        device, session = CompactApplicationService(store).approved_adopted_scope()
        with self._lock:
            if self._state != "running" or store is not self._store or manager is not self._compact_jobs:
                raise CompactApplicationError("runtime_stopped", "核验期间宿主已切换")
        return manager.request(device, session)

    def compact_apply_status(self, device_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """读取已缓存的 compact 作业状态，不读取 Anki Collection。"""

        with self._lock:
            manager = self._compact_jobs
            running = self._state == "running"
        if not running or manager is None:
            return None
        return manager.status_for(device_id, session_id)

    def request_snapshot(self, *, card_ids: Any = None, deck_ids: Any = None) -> SnapshotJob:
        """后续 UI/HTTP 使用的最小只读请求入口。"""

        with self._lock:
            manager = self._snapshot_jobs
            running = self._state == "running"
        if not running or manager is None:
            raise RuntimeError("snapshot jobs are unavailable")
        return manager.request_snapshot(card_ids=card_ids, deck_ids=deck_ids)

    def refresh_pull(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        base_snapshot_id: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """设备显式同步时提交/轮询 QueryOp；HTTP worker 不读取 collection。"""

        with self._lock:
            manager, service, store = self._snapshot_jobs, self._snapshots, self._store
            running = self._state == "running"
        if not running or manager is None or service is None or store is None:
            raise SnapshotError("snapshot_refresh_unavailable", status=503, retryable=True)
        gate = store.pull_gate(device_id, session_id)
        if not gate.allowed:
            raise SnapshotError(gate.reason or "pull_gate_blocked", status=409)
        now = time.monotonic()
        with self._refresh_lock:
            for token, item in tuple(self._refresh_requests.items()):
                if now - item["created_at"] > 600:
                    del self._refresh_requests[token]
            if refresh_token is None:
                if len(self._refresh_requests) >= 64:
                    raise SnapshotError("snapshot_refresh_busy", status=503, retryable=True)
                source = service.refresh_scope(scope_id)
                try:
                    job = manager.request_snapshot(
                        card_ids=source["ids"] if source["kind"] == "card" else None,
                        deck_ids=source["ids"] if source["kind"] == "deck" else None,
                        applied_watermark=gate.applied_watermark or 0,
                    )
                except SnapshotJobError as error:
                    busy = error.code in {"duplicate_scope", "job_capacity"}
                    raise SnapshotError(
                        "snapshot_refresh_busy" if busy else "snapshot_refresh_unavailable",
                        status=503 if busy else 409,
                        retryable=busy,
                    ) from None
                if job.scope_id != scope_id:
                    job.cancel()
                    raise SnapshotError("snapshot_refresh_scope_invalid", status=409)
                refresh_token = "r_" + secrets.token_hex(16)
                item = {
                    "device_id": device_id,
                    "session_id": session_id,
                    "scope_id": scope_id,
                    "base_snapshot_id": base_snapshot_id,
                    "job": job,
                    "created_at": now,
                }
                self._refresh_requests[refresh_token] = item
            else:
                item = self._refresh_requests.get(refresh_token)
                if item is None or any(item[key] != value for key, value in (
                    ("device_id", device_id), ("session_id", session_id),
                    ("scope_id", scope_id), ("base_snapshot_id", base_snapshot_id),
                )):
                    raise SnapshotError("snapshot_refresh_token_invalid", status=409)
            job = item["job"]
        common: Dict[str, Any] = {
            "protocol": "x4-2",
            "profile_id": self.profile_id,
            "scope_id": scope_id,
            "refresh_token": refresh_token,
        }
        state = job.state
        if state in {SnapshotJobState.CREATED, SnapshotJobState.QUEUED,
                     SnapshotJobState.STARTED, SnapshotJobState.PUBLISHING}:
            return 202, {**common, "state": "pending"}
        if state is SnapshotJobState.STALE_RESULT_DROPPED:
            raise SnapshotError("snapshot_refresh_context_changed", status=409)
        if state is SnapshotJobState.CANCELED:
            raise SnapshotError("snapshot_refresh_canceled", status=409)
        if state is not SnapshotJobState.PUBLISHED or job.result is None:
            raise SnapshotError("snapshot_refresh_failed", status=409)
        publication = job.result
        if publication.scope_id != scope_id:
            raise SnapshotError("snapshot_refresh_scope_invalid", status=409)
        gate = store.pull_gate(device_id, session_id)
        if not gate.allowed:
            raise SnapshotError(gate.reason or "pull_gate_blocked", status=409)
        result = service.refresh_result(scope_id, base_snapshot_id, publication.snapshot_id)
        return 200, {**common, "state": "ready", **result}

    def cancel_snapshot(self, job_id: str) -> bool:
        with self._lock:
            manager = self._snapshot_jobs
        return manager.cancel(job_id) if manager is not None else False

    def request_deck_catalog(self) -> CatalogJob:
        """异步请求固定版 Anki 牌组目录；collection 不离开 QueryOp。"""

        with self._lock:
            manager = self._catalog_jobs
            running = self._state == "running"
        if not running or manager is None:
            raise RuntimeError("catalog jobs are unavailable")
        return manager.request_catalog()

    def request_deck_preflight(self, *, deck_ids: Any) -> CatalogJob:
        """异步预检显式牌组范围，供后续 UI 决定是否调用 request_snapshot。"""

        with self._lock:
            manager = self._catalog_jobs
            running = self._state == "running"
        if not running or manager is None:
            raise RuntimeError("catalog jobs are unavailable")
        return manager.request_preflight(deck_ids=deck_ids)

    # 后续 Qt UI 使用的更短别名；仍然只读且保持相同的 QueryOp 边界。
    request_catalog = request_deck_catalog
    request_preflight = request_deck_preflight

    def cancel_catalog_job(self, job_id: str) -> bool:
        with self._lock:
            manager = self._catalog_jobs
        return manager.cancel(job_id) if manager is not None else False

    cancel_catalog = cancel_catalog_job

    def update_collection_generation(self, generation: int) -> None:
        """持久化外部确认的 collection 基线变化并更新 runtime 缓存。"""

        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        with self._lock:
            if self._state != "running" or self._store is None:
                raise RuntimeError("addon runtime is not running")
            self._store.set_collection_generation(generation)
            self._collection_generation = generation

    def refresh_committed_collection_generation(self, generation: int) -> None:
        """只刷新 compact service 已提交的 Store generation，不重复写 Store。"""

        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        with self._lock:
            if self._state != "running" or self._store is None:
                raise RuntimeError("addon runtime is not running")
            current = int(self._store.get_collection_generation())
            if current != generation:
                raise RuntimeError("Store generation changed before compact cache refresh")
            self._collection_generation = generation

    def set_compact_capability(self, report: Mapping[str, Any]) -> None:
        with self._lock:
            self._compact_capability = dict(report)

    def compact_capabilities(self) -> Dict[str, Any]:
        with self._lock:
            enabled = (self._state == "running" and self._compact_jobs is not None
                       and self._compact_capability.get("write_enabled") is True)
        return {"protocol": "x4-3", "apply_enabled": enabled,
                "states": ["New", "Learning", "Review", "Relearning"], "ratings": [1, 2, 3, 4],
                "requires_frozen_context": True, "native_version": "26.09.3", "native_build": "29bb700b",
                "historical_clock": "isolated-relative-transfer",
                "cutoff_policy": "reject_unproven_learning_day_classification",
                "offline_schedule": "prediction",
                "post_apply_snapshot": "native_authoritative",
                "learning_fuzz": "native_only",
                "device_snapshot_restrictions": ["fsrs_short_term_with_steps", "empty_learning_steps", "empty_relearning_steps"]}

    def note_external_collection_change(self) -> int:
        """外部评分/Undo/配置编辑使旧授权与 permit 失效；历史收据不改写。"""
        with self._lock:
            if self._state != "running" or self._store is None:
                raise RuntimeError("runtime is not running")
            before = int(self._store.get_collection_generation())
            self._store.set_collection_generation(before + 1)
            self._collection_generation = before + 1
            return before + 1

    def status(self) -> Dict[str, Any]:
        """返回不包含 profile path、显示名、token 或 raw payload 的状态。"""

        with self._lock:
            state = self._state
            runtime_epoch = self._runtime_epoch
            generation = self._collection_generation
            address = self._last_bind
            store = self._store
            last_snapshot = self._last_snapshot
            catalog_jobs = self._catalog_jobs
            cached_counts = dict(self._last_counts)
        last_catalog = catalog_jobs.last_catalog if state == "running" and catalog_jobs is not None else None
        last_preflight = catalog_jobs.last_preflight if state == "running" and catalog_jobs is not None else None
        counts = self._safe_persistent_counts(store, cached_counts)
        if counts:
            with self._lock:
                self._last_counts = dict(counts)
        snapshot_count = int(counts.get("snapshots", 0))
        return {
            "state": state,
            "running": state == "running",
            "accepting_requests": state == "running" and self.accepting_requests,
            "profile_id": self._profile_id,
            "runtime_epoch": runtime_epoch,
            "bind": {
                "host": address[0] if address is not None else self._host,
                "port": address[1] if address is not None else self._port,
            },
            "schema": SCHEMA_VERSION,
            "collection_generation": generation,
            "write_enabled": False,
            "write_scope": "legacy_p3",
            "compact_application_capability": self.compact_capabilities(),
            "write_disabled_reason": P3_WRITE_DISABLED_REASON,
            "snapshot": {
                "available": bool(snapshot_count or last_snapshot is not None),
                "count": snapshot_count,
            },
            "catalog": {
                "available": last_catalog is not None,
                "deck_count": len(last_catalog.entries) if last_catalog is not None else 0,
            },
            "preflight": {
                "available": last_preflight is not None,
                "card_count": last_preflight.card_count if last_preflight is not None else None,
                "snapshot_limit": last_preflight.snapshot_limit if last_preflight is not None else None,
                "within_limit": last_preflight.within_limit if last_preflight is not None else None,
            },
            "persistence": counts,
        }

    diagnostics = status

    def _require_running(self) -> "AddonRuntime":
        if not self.running:
            raise RuntimeError("addon runtime is not running")
        return self

    @staticmethod
    def _stop_server(server: Optional[DeviceServer], timeout: Optional[float] = None) -> None:
        if server is None:
            return
        server.stop(timeout=timeout)

    @staticmethod
    def _close_services(
        snapshots: Optional[SnapshotService], store: Optional[ProfileStore]
    ) -> None:
        try:
            if snapshots is not None:
                close = getattr(snapshots, "close", None)
                if callable(close):
                    close()
        finally:
            if store is not None:
                store.close()

    @staticmethod
    def _safe_persistent_counts(
        store: Optional[ProfileStore], fallback: Mapping[str, Any]
    ) -> Dict[str, Any]:
        if store is None:
            return dict(fallback)
        try:
            raw = store.diagnostics()
            result: Dict[str, Any] = {
                "devices": int(raw.get("devices", 0)),
                "sessions": int(raw.get("sessions", 0)),
                "events": int(raw.get("events", 0)),
                "conflicts": int(raw.get("conflicts", 0)),
                "delivery_status": dict(raw.get("delivery_status", {})),
                "application_status": dict(raw.get("application_status", {})),
            }
            connection = store.connect()
            try:
                result["snapshots"] = int(
                    connection.execute(
                        "SELECT count(*) FROM snapshots WHERE profile_id=?",
                        (store.profile_id,),
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            return result
        except Exception:
            return dict(fallback)


InkCardsRuntime = AddonRuntime
# 仅保留旧导入名作为新 runtime 的别名；不恢复旧 bridge 行为或依赖。
BridgeRuntime = AddonRuntime


__all__ = [
    "AddonRuntime",
    "BridgeRuntime",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "InkCardsRuntime",
    "P3_WRITE_DISABLED_REASON",
    "validate_bind_host",
    "validate_bind_port",
]
