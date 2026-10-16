"""Anki 宿主接线；这是唯一按需导入 ``aqt`` 的模块。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Tuple

from .anki.adapter_260801 import Anki260801Adapter
from .anki.catalog_jobs import CatalogJobManager
from .anki.compact_jobs import CompactJobManager
from .anki.compatibility import require_anki_compatibility
from .anki.operations import (
    AnkiCollectionOpHost,
    AnkiQueryOpHost,
    CollectionOpAdapter,
    CollectionStartRejected,
    QueryOpAdapter,
)
from .anki.snapshot_jobs import SnapshotJobManager
from .lifecycle import LifecycleController
from .profile_identity import load_or_create_profile_identity
from .runtime import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    AddonRuntime,
    validate_bind_host,
    validate_bind_port,
)

_CONTROLLER_ATTR = "_inkcards_anki_bridge_lifecycle"


def _profile_folder(main_window: Any) -> Path:
    manager = getattr(main_window, "pm", None)
    if manager is None:
        raise RuntimeError("Anki profile manager is unavailable")
    reader = getattr(manager, "profileFolder", None)
    value = reader() if callable(reader) else reader
    if not isinstance(value, (str, Path)) or not str(value):
        raise RuntimeError("Anki profile folder is unavailable")
    return Path(value)


def _anki_build(aqt_module: Any) -> Optional[str]:
    for name in ("build", "buildHash", "build_hash"):
        value = getattr(aqt_module, name, None)
        if isinstance(value, str):
            return value
    for name in ("buildinfo", "build_info"):
        value = getattr(aqt_module, name, None)
        if isinstance(value, Mapping):
            for key in ("buildhash", "build_hash", "build"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate
        else:
            for attr in ("buildhash", "build_hash", "build"):
                candidate = getattr(value, attr, None)
                if isinstance(candidate, str):
                    return candidate
    # Anki 26.08.1 normally exposes the buildinfo module rather than copying
    # buildhash onto aqt.  This import remains confined to the Anki-only layer;
    # ordinary Python tests simply take the injected fake attribute path above.
    try:
        from anki import buildinfo  # type: ignore[import-not-found]
    except Exception:
        buildinfo = None
    if buildinfo is not None:
        for attr in ("buildhash", "build_hash", "build"):
            candidate = getattr(buildinfo, attr, None)
            if isinstance(candidate, str):
                return candidate
    return None


def _literal_host_or_default(value: Any, default: str = DEFAULT_HOST) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return validate_bind_host(value)
    except ValueError:
        return default


def _port_or_default(value: Any, default: int = DEFAULT_PORT) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    try:
        return validate_bind_port(value)
    except ValueError:
        return default


def resolve_server_config(config: Optional[Mapping[str, Any]]) -> Tuple[str, int]:
    """读取有界设备服务配置；缺失/非法值安全回落到 loopback/8766。"""

    if not isinstance(config, Mapping):
        config = {}
    nested = config.get("device_server")
    if not isinstance(nested, Mapping):
        nested = config.get("server")
    selected: Mapping[str, Any] = nested if isinstance(nested, Mapping) else config
    return (
        _literal_host_or_default(selected.get("host"), DEFAULT_HOST),
        _port_or_default(selected.get("port"), DEFAULT_PORT),
    )


def _addon_id(main_window: Any, module_name: str) -> Optional[str]:
    manager = getattr(main_window, "addonManager", None)
    resolver = getattr(manager, "addonFromModule", None)
    if not callable(resolver):
        return None
    value = resolver(module_name)
    return value if isinstance(value, str) and value else None


def _addon_root(main_window: Any, addon_id: Optional[str]) -> Path:
    manager = getattr(main_window, "addonManager", None)
    reader = getattr(manager, "addonFolder", None)
    if callable(reader) and addon_id is not None:
        try:
            value = reader(addon_id)
        except Exception:
            value = None
        if isinstance(value, (str, Path)) and str(value):
            return Path(value)
    # plugin.py 位于 <addon-root>/inkcards_anki_bridge/plugin.py。
    return Path(__file__).resolve().parents[1]


def _addon_config(
    main_window: Any, addon_id: Optional[str], supplied: Optional[Mapping[str, Any]]
) -> Mapping[str, Any]:
    if isinstance(supplied, Mapping):
        return supplied
    manager = getattr(main_window, "addonManager", None)
    reader = getattr(manager, "getConfig", None)
    if callable(reader) and addon_id is not None:
        try:
            value = reader(addon_id)
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return value
    return {}


def write_device_server_host(
    *,
    aqt_module: Any,
    addon_module_name: str,
    enabled: bool,
) -> Mapping[str, Any]:
    """通过 Anki addonManager 写入局域网监听开关，保留当前有效端口。

    这里只写配置文件，不热重启当前 runtime。调用方必须提示用户重启 Anki；这样
    正在运行的服务地址不会出现 UI 所见与实际监听不一致的竞态。
    """

    main_window = getattr(aqt_module, "mw", None)
    manager = getattr(main_window, "addonManager", None)
    addon_id = _addon_id(main_window, addon_module_name)
    if manager is None or addon_id is None:
        raise RuntimeError("Anki add-on configuration manager is unavailable")
    current = dict(_addon_config(main_window, addon_id, None))
    nested_value = current.get("device_server")
    nested = dict(nested_value) if isinstance(nested_value, Mapping) else {}
    _current_host, current_port = resolve_server_config(current)
    nested["host"] = "0.0.0.0" if bool(enabled) else "127.0.0.1"
    if "port" not in nested:
        nested["port"] = current_port
    current["device_server"] = nested

    writer = getattr(manager, "writeConfig", None)
    if not callable(writer):
        writer = getattr(manager, "setConfig", None)
    if not callable(writer):
        raise RuntimeError("Anki add-on configuration writer is unavailable")
    writer(addon_id, current)
    return current


def _query_op_factory(aqt_module: Any) -> Any:
    """只在 Anki 入口解析官方 QueryOp；普通 Python import 不触碰 aqt。"""

    operations = getattr(aqt_module, "operations", None)
    factory = getattr(operations, "QueryOp", None)
    if callable(factory):
        return factory
    try:
        from aqt.operations import QueryOp  # type: ignore[import-not-found]
    except Exception:
        return None
    return QueryOp if callable(QueryOp) else None


def _collection_op_factory(aqt_module: Any) -> Any:
    """只在 Anki 入口解析官方 CollectionOp。"""

    operations = getattr(aqt_module, "operations", None)
    factory = getattr(operations, "CollectionOp", None)
    if callable(factory):
        return factory
    try:
        from aqt.operations import CollectionOp  # type: ignore[import-not-found]
    except Exception:
        return None
    return CollectionOp if callable(CollectionOp) else None


def _attach_compact_jobs(
    *,
    aqt_module: Any,
    main_window: Any,
    runtime: AddonRuntime,
) -> Optional[CompactJobManager]:
    """将确认过的原生 compact service 接入 Anki CollectionOp。"""

    factory = _collection_op_factory(aqt_module)
    if factory is None:
        # 普通 Python/fake host 可以启动只读路由，但不会伪装成已接入 Anki。
        return None
    taskman = getattr(main_window, "taskman", None)
    schedule_on_main = getattr(taskman, "run_on_main", None)
    if not callable(schedule_on_main):
        return None
    try:
        from anki.collection_pb2 import OpChanges  # type: ignore[import-not-found]

        from .anki.compact_260903 import AnkiCompactAdapter
        from .services.compact_application import (
            CompactApplicationError,
            CompactApplicationService,
        )
    except Exception:
        # 目标原生 API 不可用时不创建一个半接线的 manager。
        return None

    from .anki.operations import CompactOperationInitiator
    host = AnkiCollectionOpHost(
        factory,
        main_window,
        op_changes_type=OpChanges,
        empty_changes_factory=OpChanges,
        schedule_on_main=schedule_on_main,
        schedule_after_collection_op=schedule_on_main,
        initiator=CompactOperationInitiator(runtime.profile_id, runtime.snapshot_context().runtime_epoch),
    )
    operation_adapter = CollectionOpAdapter(
        host=host,
        context_provider=runtime.snapshot_context,
    )
    store = runtime.store
    if store is None:
        raise RuntimeError("runtime Store is unavailable")
    adapter = AnkiCompactAdapter()
    from .anki.compact_diagnostics import CompactDiagnostics

    diagnostics = CompactDiagnostics(store.root)
    service = CompactApplicationService(store, adapter, diagnostics=diagnostics)
    capability_writer = getattr(runtime, "set_compact_capability", None)
    if callable(capability_writer):
        capability_writer(adapter.capability_report(getattr(main_window, "col", None)).to_dict())

    def reviewer_available() -> bool:
        # Anki 的主窗口状态是轻量字符串；未知状态按失败关闭处理。
        state = getattr(main_window, "state", None)
        return isinstance(state, str) and state != "review"

    def apply_one(collection: Any, device_id: str, session_id: str, context: Any) -> Any:
        diagnostics.begin()
        if context.profile_identity != runtime.profile_id:
            raise RuntimeError("compact collection identity changed")
        kwargs = {
            "collection_identity": runtime.profile_id,
            "expected_generation": context.generation,
        }
        try:
            authorization = service.authorize_next(
                collection,
                device_id,
                session_id,
                **kwargs,
            )
        except CompactApplicationError as error:
            if error.code not in {"compact_no_pending_event", "compact_recovery_required"}:
                diagnostics.record_exception(error, "authorize")
            if error.code == "compact_no_pending_event":
                # 重复上传可以排入作业；完整终态核验后返回真实 empty OpChanges。
                return service.recover_pending(collection, device_id, session_id, **kwargs)
            # 仅处理已知的只读前置拒绝；恢复/写入路径的异常仍由 manager 保守判定。
            if error.code in {
                "snapshot_application_context_missing", "legacy_source_invalid",
                "legacy_source_binding_mismatch", "legacy_source_required",
                "legacy_source_changed", "preset_snapshot_conflict", "adopted_prestate_conflict",
                "snapshot_day_context_missing", "snapshot_creation_time_missing",
                "snapshot_clock_config_conflict", "snapshot_context_unsupported",
                "snapshot_counter_receipt_path_invalid", "snapshot_counter_generation_conflict",
                "snapshot_counter_baseline_conflict", "snapshot_counter_receipt_path_missing",
            }:
                return SimpleNamespace(
                    status="blocked", reason=error.code, changes=OpChanges(),
                    generation_before=context.generation, generation_after=context.generation,
                    refresh_context=False, more_pending=False,
                )
            if error.code != "compact_recovery_required":
                raise
            # authorize_next 是只读的；恢复写入前重新检查 Reviewer 所有权。
            if not reviewer_available():
                raise CollectionStartRejected("reviewer_owns_collection") from None
            return service.recover_pending(
                collection,
                device_id,
                session_id,
                **kwargs,
            )
        # authorize_next 只读 Collection；紧邻 apply_pending 再检查一次 Reviewer。
        if not reviewer_available():
            raise CollectionStartRejected("reviewer_owns_collection")
        return service.apply_pending(
            collection,
            device_id,
            session_id,
            authorization=authorization,
            **kwargs,
        )

    manager = CompactJobManager(
        operation_adapter=operation_adapter,
        context_provider=runtime.snapshot_context,
        apply_one=apply_one,
        update_collection_generation=runtime.refresh_committed_collection_generation,
        reviewer_available=reviewer_available,
        diagnostics=diagnostics,
    )
    return runtime.attach_compact_jobs(manager)


def _attach_snapshot_jobs(
    *,
    aqt_module: Any,
    main_window: Any,
    runtime: AddonRuntime,
    compatibility: Any,
) -> Optional[SnapshotJobManager]:
    """在宿主进程内把 official QueryOp 接到标准库 runtime。"""

    factory = _query_op_factory(aqt_module)
    if factory is None:
        # 隔离 fake host/普通 Python 环境没有 aqt 时保持 runtime 可启动；
        # 真实宿主检查状态在证据中明确标记 DEFERRED。
        return None
    taskman = getattr(main_window, "taskman", None)
    schedule_on_main = getattr(taskman, "run_on_main", None)
    if not callable(schedule_on_main):
        schedule_on_main = None
    host = AnkiQueryOpHost(
        factory,
        main_window,
        schedule_on_main=schedule_on_main,
    )
    operation_adapter = QueryOpAdapter(
        host=host,
        context_provider=runtime.snapshot_context,
    )
    exporter = Anki260801Adapter(
        # 新的 manager 永远使用 export_snapshot_from_collection(); provider
        # 仅保留给兼容旧 façade，不会在 HTTP/runtime 线程中被调用。
        collection_provider=lambda: None,
        profile_provider=runtime.snapshot_context,
        profile_identity=runtime.profile_id,
        anki_version=compatibility.version or "26.08.1",
        anki_build=compatibility.build or "39e4b0b4",
        generation_provider=lambda: runtime.collection_generation,
        runtime_epoch_provider=lambda: runtime.runtime_epoch,
    )
    manager = SnapshotJobManager(
        operation_adapter=operation_adapter,
        exporter=exporter,
        context_provider=runtime.snapshot_context,
        publish=runtime.publish_snapshot,
    )
    return runtime.attach_snapshot_jobs(manager)


def _attach_catalog_jobs(
    *,
    aqt_module: Any,
    main_window: Any,
    runtime: AddonRuntime,
    compatibility: Any,
) -> Optional[CatalogJobManager]:
    """在宿主进程内把牌组目录/预检接到官方 QueryOp。"""

    factory = _query_op_factory(aqt_module)
    if factory is None:
        return None
    taskman = getattr(main_window, "taskman", None)
    schedule_on_main = getattr(taskman, "run_on_main", None)
    if not callable(schedule_on_main):
        schedule_on_main = None
    host = AnkiQueryOpHost(
        factory,
        main_window,
        schedule_on_main=schedule_on_main,
    )
    operation_adapter = QueryOpAdapter(
        host=host,
        context_provider=runtime.snapshot_context,
    )
    adapter = Anki260801Adapter(
        collection_provider=lambda: None,
        profile_provider=runtime.snapshot_context,
        profile_identity=runtime.profile_id,
        anki_version=compatibility.version or "26.08.1",
        anki_build=compatibility.build or "39e4b0b4",
        generation_provider=lambda: runtime.collection_generation,
        runtime_epoch_provider=lambda: runtime.runtime_epoch,
    )
    manager = CatalogJobManager(
        operation_adapter=operation_adapter,
        adapter=adapter,
        context_provider=runtime.snapshot_context,
    )
    return runtime.attach_catalog_jobs(manager)


def start_in_anki(
    *,
    aqt_module: Any | None = None,
    user_files_root: str | Path | None = None,
    addon_module_name: str | None = None,
    config: Mapping[str, Any] | None = None,
    host: str | None = None,
    port: int | None = None,
    route_limits: Any = None,
    server_limits: Any = None,
) -> AddonRuntime:
    """在 profile 已打开后创建并启动当前 profile 的新 runtime。"""

    if aqt_module is None:
        import aqt as aqt_module  # type: ignore[no-redef]

    raw_version = getattr(aqt_module, "appVersion", None)
    build = _anki_build(aqt_module)
    compatibility = require_anki_compatibility(raw_version, build)
    main_window = getattr(aqt_module, "mw", None)
    if main_window is None:
        raise RuntimeError("aqt.mw is unavailable")
    profile_folder = _profile_folder(main_window)
    module_name = addon_module_name or __name__
    addon_id = _addon_id(main_window, module_name)
    addon_root = _addon_root(main_window, addon_id)
    selected_config = _addon_config(main_window, addon_id, config)
    configured_host, configured_port = resolve_server_config(selected_config)
    selected_host = (
        _literal_host_or_default(host, configured_host)
        if host is not None
        else configured_host
    )
    selected_port = (
        _port_or_default(port, configured_port)
        if port is not None
        else configured_port
    )
    identity_root = (
        Path(user_files_root)
        if user_files_root is not None
        else addon_root / "user_files"
    )
    identity = load_or_create_profile_identity(identity_root, profile_folder)
    runtime = AddonRuntime(
        profile_id=identity.profile_id,
        root=identity.root,
        host=selected_host,
        port=selected_port,
        route_limits=route_limits,
        server_limits=server_limits,
    )
    try:
        runtime.start()
        _attach_snapshot_jobs(
            aqt_module=aqt_module,
            main_window=main_window,
            runtime=runtime,
            compatibility=compatibility,
        )
        _attach_catalog_jobs(
            aqt_module=aqt_module,
            main_window=main_window,
            runtime=runtime,
            compatibility=compatibility,
        )
        _attach_compact_jobs(
            aqt_module=aqt_module,
            main_window=main_window,
            runtime=runtime,
        )
    except Exception:
        runtime.stop()
        raise
    # 变量保留给调试器/类型检查器，避免未来误把兼容性结果丢弃成隐式检查。
    del compatibility
    return runtime


def register_in_anki(
    *,
    aqt_module: Any | None = None,
    gui_hooks_module: Any | None = None,
    addon_module_name: str | None = None,
    user_files_root: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    host: str | None = None,
    port: int | None = None,
    route_limits: Any = None,
    server_limits: Any = None,
    logger: Any = print,
) -> LifecycleController:
    """加载 add-on 时只注册 hook；profile 打开后才启动 runtime。"""

    if aqt_module is None:
        import aqt as aqt_module  # type: ignore[no-redef]

    existing = getattr(aqt_module, _CONTROLLER_ATTR, None)
    if isinstance(existing, LifecycleController) and existing.registered and existing.enabled:
        try:
            from .ui.integration import register_tools_action, unregister_tools_action

            register_tools_action(
                aqt_module=aqt_module,
                runtime_provider=lambda: existing.runtime,
                config_writer=lambda enabled: write_device_server_host(
                    aqt_module=aqt_module,
                    addon_module_name=addon_module_name or __name__,
                    enabled=enabled,
                ),
            )
            existing.set_ui_cleanup(lambda: unregister_tools_action(aqt_module))
        except Exception as exc:
            logger(f"[InkCards] UI registration failed: {type(exc).__name__}: {exc}")
        return existing
    if gui_hooks_module is None:
        gui_hooks_module = getattr(aqt_module, "gui_hooks", None)
    if gui_hooks_module is None:
        from aqt import gui_hooks as gui_hooks_module  # type: ignore[no-redef]

    main_window = getattr(aqt_module, "mw", None)
    if main_window is None:
        raise RuntimeError("aqt.mw is unavailable")
    module_name = addon_module_name or __name__

    def addon_id_provider() -> str | None:
        return _addon_id(main_window, module_name)

    controller = LifecycleController(
        gui_hooks=gui_hooks_module,
        runtime_factory=lambda: start_in_anki(
            aqt_module=aqt_module,
            user_files_root=user_files_root,
            addon_module_name=module_name,
            config=config,
            host=host,
            port=port,
            route_limits=route_limits,
            server_limits=server_limits,
        ),
        # 只用 profile folder 做生命周期切换比较；identity namespace 由
        # start_in_anki 内的 ProfileIdentityStore 再次计算，绝不使用显示名建目录。
        profile_provider=lambda: str(_profile_folder(main_window)),
        addon_id_provider=addon_id_provider,
        logger=logger,
    )
    controller.register()
    try:
        from .ui.integration import register_tools_action, unregister_tools_action

        register_tools_action(
            aqt_module=aqt_module,
            runtime_provider=lambda: controller.runtime,
            config_writer=lambda enabled: write_device_server_host(
                aqt_module=aqt_module,
                addon_module_name=module_name,
                enabled=enabled,
            ),
        )
        controller.set_ui_cleanup(lambda: unregister_tools_action(aqt_module))
    except Exception as exc:
        # UI 缺失不能阻止 runtime/lifecycle hook 启动；真实宿主有 aqt.qt，
        # fake host 则在专门的 UI 测试中注入 fake Qt。
        logger(f"[InkCards] UI registration failed: {type(exc).__name__}: {exc}")
    setattr(aqt_module, _CONTROLLER_ATTR, controller)
    return controller


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "register_in_anki",
    "resolve_server_config",
    "start_in_anki",
    "write_device_server_host",
]
