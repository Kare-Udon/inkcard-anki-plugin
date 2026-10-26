"""InkCards UI 的纯 Python 状态投影与异步作业门控。

本模块故意不导入 ``aqt``、PyQt/PySide 或任何 Anki UI 类型。它只消费
runtime 的设备管理、catalog/preflight/snapshot 窄接口，因此可以在普通 Python
测试中验证文案、计数、脱敏和“预检通过后才允许准备快照”的不变量。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ..anki.compact_diagnostics import stable_code


_STATUS_ORDER = (
    "received",
    "pending_apply",
    "applying",
    "applied",
    "unknown",
    "conflict",
    "abandoned",
)

_STATUS_LABELS = {
    "received": "已接收",
    "pending_apply": "待应用",
    "applying": "应用中",
    "applied": "已应用",
    "unknown": "未知",
    "conflict": "冲突",
    "abandoned": "已放弃",
}

_ERROR_LABELS = {
    "catalog_unavailable": "牌组目录暂不可用。",
    "context_unavailable": "Anki 当前状态不可用，请稍后刷新。",
    "deck_name_invalid": "发现无法识别的牌组层级。",
    "deck_not_found": "所选牌组已不存在，请刷新目录。",
    "duplicate_catalog": "牌组目录刷新已在进行中。",
    "duplicate_preflight": "相同的范围预检已在进行中。",
    "duplicate_scope": "相同的快照范围已在准备中。",
    "invalid_pair_request": "设备配对信息无效。",
    "invalid_or_expired_pairing_code": "配对码无效或已过期。",
    "pairing_failed": "设备配对失败，请稍后重试。",
    "pairing_rate_limited": "配对尝试过于频繁，请稍后重试。",
    "pairing_unavailable": "暂时无法生成配对码。",
    "runtime_stopped": "InkCards 服务当前未运行。",
    "scope_invalid": "牌组选择无效，请重新选择。",
    "scope_required": "请先选择至少一个牌组。",
    "sync_policy_corrupt": "排除设置损坏，已阻断同步；请修复电脑端排除设置后重试。",
    "sync_policy_invalid": "排除设置无效，请刷新牌组目录后重试。",
    "sync_policy_unavailable": "排除设置暂不可用，已阻断同步；请检查电脑端服务。",
    "sync_policy_save_failed": "排除设置未保存，已保留原值；请检查电脑端存储后重试。",
    "snapshot_refresh_scope_changed": "同步准备期间范围已变化，设备原卡库保留；请重新同步。",
    "scope_too_large": "牌组选择数量超过上限，请减少牌组数量。",
    "snapshot_limit_exceeded": "所选牌组超过当前快照卡片上限。",
    "source_scan_too_large": "源 Anki 库超过安全扫描预算；排除牌组无法缩小源库扫描范围。请减少源库卡片量或更新支持版本。",
    "snapshot_content_unsupported": "部分牌组内容暂不支持同步，请查看受影响牌组并排除后重试。",
    "snapshot_unpageable": "所选范围无法生成可下载快照。",
    "unsupported_anki_build": "当前 Anki 版本不受支持。",
    "adopted_source_missing": "尚未导入已批准的恢复来源，请先在电脑端导入。",
    "adopted_source_ambiguous": "发现多个恢复来源，请先核对唯一目标会话。",
    "legacy_source_invalid": "恢复来源无效，请核验电脑端文件。",
    "legacy_source_binding_mismatch": "恢复来源与原评分记录不符，已阻止恢复。",
    "adopted_prestate_conflict": "卡片或复习历史已变化，已阻止恢复，请重新核验。",
    "reviewer_owns_collection": "请退出复习页面后再恢复。",
    "device_already_bound": "此名额已绑定设备；请先允许重新绑定。",
    "device_not_found": "所选设备已不存在，请刷新列表。",
    "unknown_device_slot": "所选设备已不存在，请刷新列表。",
    "invalid_device_slot": "所选设备无效，请刷新列表后重选。",
    "device_revoked": "此设备已移除，请重新添加。",
    "device_identity_conflict": "此设备已有记录，请选择原设备并允许重新绑定。",
    "rebind_required": "请先选中原设备并允许重新绑定。",
    "pairing_in_progress": "此设备仍有待确认配对，可重置配对码后重新操作。",
    "invalid_device_name": "设备名称无效，请使用 1 至 128 个可见字符。",
    "storage_busy": "设备存储正忙，请稍后重试。",
    "sqlite_error": "电脑端存储出错，请查看 Anki 插件日志。",
    "sqlite_busy_timeout": "电脑端存储正忙，请稍后重试。",
    "snapshot_prepare_failed": "电脑端准备失败，请查看 Anki 插件日志。",
    "snapshot_not_prepared": "电脑正在后台准备同步数据，请稍后重试。",
}

_SERVICE_LABELS = {
    "starting": "服务启动中",
    "running": "服务运行中",
    "stopping": "服务停止中",
    "stopped": "服务未运行",
}

_FINAL_JOB_STATES = frozenset(
    {"published", "completed", "failed", "canceled", "stale_result_dropped", "applied", "idle", "blocked", "unknown", "rejected"}
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _safe_text(value: Any, default: str = "未知") -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _safe_state(job: Any) -> str:
    state = getattr(job, "state", "unknown")
    value = getattr(state, "value", state)
    return str(value)


def _job_is_final(job: Any) -> bool:
    return _safe_state(job) in _FINAL_JOB_STATES


def _deck_id(entry: Any) -> str:
    value = getattr(entry, "deck_id", getattr(entry, "id", None))
    if value is None and isinstance(entry, Mapping):
        value = entry.get("deck_id", entry.get("id"))
    return str(value) if value is not None else ""


def _deck_name(entry: Any) -> str:
    value = getattr(entry, "name", None)
    if value is None and isinstance(entry, Mapping):
        value = entry.get("name")
    if isinstance(value, str) and value:
        return value
    path = getattr(entry, "path", None)
    if path is None and isinstance(entry, Mapping):
        path = entry.get("path")
    if isinstance(path, (list, tuple)):
        return "::".join(str(part) for part in path)
    return "未命名牌组"


def _deck_path(entry: Any) -> Tuple[str, ...]:
    value = getattr(entry, "path", None)
    if value is None and isinstance(entry, Mapping):
        value = entry.get("path")
    if isinstance(value, (list, tuple)) and value:
        return tuple(str(part) for part in value)
    return tuple(part for part in _deck_name(entry).split("::") if part)


def _deck_parent_id(entry: Any) -> Optional[str]:
    value = getattr(entry, "parent_id", None)
    if value is None and isinstance(entry, Mapping):
        value = entry.get("parent_id")
    return str(value) if value is not None and str(value) else None


def _deck_card_count(entry: Any) -> Optional[int]:
    value = getattr(entry, "card_count", None)
    if value is None and isinstance(entry, Mapping):
        value = entry.get("card_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _deck_depth(entry: Any) -> int:
    value = getattr(entry, "depth", getattr(entry, "level", None))
    if value is None and isinstance(entry, Mapping):
        value = entry.get("depth", entry.get("level"))
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _numeric_id_key(value: str) -> Tuple[int, Any, str]:
    return (0, int(value), value) if value.isdigit() else (1, value, value)


def _normalise_ids(values: Iterable[Any]) -> Tuple[str, ...]:
    result = {str(value) for value in values if value is not None and str(value)}
    return tuple(sorted(result, key=_numeric_id_key))


def is_valid_device_id(value: Any) -> bool:
    """匹配设备协议的 1..128 字符、无控制字符约束。"""

    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in value)
    )


def is_valid_device_name(value: Any) -> bool:
    """限制设备名称长度，并拒绝控制字符。"""

    return bool(
        isinstance(value, str)
        and 1 <= len(value.strip()) <= 128
        and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in value.strip())
    )


def _catalog_entries(catalog: Any) -> Tuple[Any, ...]:
    entries = getattr(catalog, "entries", None)
    if entries is None:
        entries = getattr(catalog, "decks", None)
    if entries is None and isinstance(catalog, Mapping):
        entries = catalog.get("entries", catalog.get("decks", ()))
    return tuple(entries) if isinstance(entries, (list, tuple)) else ()


def _result_deck_ids(result: Any) -> Tuple[str, ...]:
    value = getattr(result, "deck_ids", None)
    if value is None and isinstance(result, Mapping):
        value = result.get("deck_ids")
    if value is not None:
        return _normalise_ids(value)
    selected = getattr(result, "selected_decks", None)
    if selected is None and isinstance(result, Mapping):
        selected = result.get("selected_decks")
    return _normalise_ids(_deck_id(entry) for entry in (selected or ()))


def _is_within_limit(result: Any) -> bool:
    value = getattr(result, "within_limit", None)
    if value is None and isinstance(result, Mapping):
        value = result.get("within_limit")
    return value is True


@dataclass(frozen=True)
class CounterView:
    key: str
    label: str
    count: int


@dataclass(frozen=True)
class DeckItem:
    """UI 牌组行；``deck_id`` 永远保留为操作用值，不依赖显示文本。"""

    deck_id: str
    label: str
    depth: int
    unsupported: Tuple[str, ...] = ()
    parent_id: Optional[str] = None
    path: Tuple[str, ...] = ()
    card_count: Optional[int] = None


@dataclass(frozen=True)
class DeviceItem:
    """不含设备凭据的设备列表行。"""

    slot_id: str = field(repr=False)
    name: str
    status_key: str = field(repr=False)
    status_text: str
    last_seen_text: str
    label: str


_DEVICE_STATUS_LABELS = {
    "pending": "待绑定",
    "bound": "已绑定",
    "rebind_ready": "允许重绑",
    "revoked": "已移除",
}


def _last_seen_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "从未连接"
    try:
        return (
            datetime.fromtimestamp(value / 1000)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except (OverflowError, OSError, ValueError):
        return "未知"


def _device_item(value: Any) -> Optional[DeviceItem]:
    raw = _mapping(value)
    slot_id = raw.get("slot_id")
    name = raw.get("name")
    status_key = raw.get("status")
    if (
        not isinstance(slot_id, str)
        or not slot_id
        or not isinstance(name, str)
        or not name
    ):
        return None
    status_key = status_key if isinstance(status_key, str) else "unknown"
    status_text = _DEVICE_STATUS_LABELS.get(status_key, "状态未知")
    last_seen_text = _last_seen_text(raw.get("last_seen_at_ms"))
    return DeviceItem(
        slot_id=slot_id,
        name=name,
        status_key=status_key,
        status_text=status_text,
        last_seen_text=last_seen_text,
        label=f"{name} · {status_text} · 最近连接：{last_seen_text}",
    )


@dataclass(frozen=True)
class StatusView:
    service_text: str
    host_text: str
    port_text: str
    devices: int
    events: int
    delivery: Tuple[CounterView, ...]
    application: Tuple[CounterView, ...]
    conflicts: int
    snapshots: int
    generation_text: str
    catalog_text: str
    preflight_text: str
    write_disabled_reason: str
    lan_notice: str
    lan_enabled: bool
    lan_known: bool

    def delivery_counts(self) -> Dict[str, int]:
        return {item.key: item.count for item in self.delivery}

    def application_counts(self) -> Dict[str, int]:
        return {item.key: item.count for item in self.application}


@dataclass(frozen=True)
class SyncOverview:
    headline: str
    detail: str
    recent_text: str
    recent_time: str
    device_lines: Tuple[str, ...]
    service_running: bool
    paired_device_count: int
    has_sync_status: bool


@dataclass(frozen=True)
class ButtonState:
    refresh: bool
    add_device: bool
    allow_rebind: bool
    reset_pairing_code: bool
    remove_device: bool
    rename_device: bool
    preflight: bool
    snapshot: bool
    cancel: bool
    lan: bool
    recovery: bool = False


@dataclass(frozen=True)
class ViewState:
    status: StatusView
    decks: Tuple[DeckItem, ...]
    selected_ids: Tuple[str, ...]
    devices: Tuple[DeviceItem, ...]
    selected_slot_id: Optional[str] = field(repr=False)
    show_removed: bool
    pairing_message: str = field(repr=False)
    preflight_message: str
    snapshot_message: str
    config_message: str
    error_message: str
    buttons: ButtonState
    recovery_message: str = ""
    excluded_deck_ids: Tuple[str, ...] = ()
    effective_excluded_deck_ids: Tuple[str, ...] = ()
    inherited_excluded_deck_ids: Tuple[str, ...] = ()
    sync_policy_available: bool = False
    sync_policy_message: str = ""
    sync_policy_error: str = ""
    sync_overview: SyncOverview = field(
        default_factory=lambda: SyncOverview(
            headline="服务状态未知",
            detail="暂时无法读取 InkCards 状态。",
            recent_text="同步状态未知",
            recent_time="",
            device_lines=(),
            service_running=False,
            paired_device_count=0,
            has_sync_status=False,
        )
    )


def _counter_views(raw: Any) -> Tuple[CounterView, ...]:
    source = _mapping(raw)
    return tuple(
        CounterView(key, _STATUS_LABELS[key], _safe_count(source.get(key)))
        for key in _STATUS_ORDER
    )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sync_time_text(raw: Mapping[str, Any]) -> str:
    value = next(
        (
            raw.get(key)
            for key in (
                "completed_at_ms",
                "last_sync_at_ms",
                "updated_at_ms",
                "completed_at",
                "last_sync_at",
            )
            if raw.get(key) is not None
        ),
        None,
    )
    try:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            moment = datetime.fromtimestamp(value / 1000).astimezone()
        elif isinstance(value, str) and value:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        else:
            return ""
        return moment.strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _sync_stage_text(stage: Any) -> str:
    if not isinstance(stage, str):
        return ""
    return {
        "queued": "已排队，等待设备",
        "waiting": "等待设备发起同步",
        "waiting_device": "等待设备发起同步",
        "preparing": "正在准备同步数据",
        "prepared": "同步数据已准备",
        "catalog": "读取牌组目录",
        "prepare": "准备同步数据",
        "download": "发送卡片到设备",
        "downloading": "正在向设备发送数据",
        "pull": "发送卡片到设备",
        "upload": "接收复习记录",
        "apply": "应用复习记录",
        "committed": "设备已确认同步",
        "failed": "同步失败",
        "complete": "同步完成",
        "completed": "同步完成",
    }.get(stage.lower(), "")


def _sync_error_text(value: Any) -> str:
    error = _mapping(value)
    code = error.get("code")
    message = error.get("message")
    if code == "scope_too_large":
        return "本次实际参与同步的卡片超过 32,768 张，请排除一些牌组后重试。"
    if code == "snapshot_content_unsupported":
        # runtime 为此稳定错误码生成限长的牌组名称提示；卡片正文不会进入此字段。
        if isinstance(message, str):
            cleaned = " ".join(
                "".join(char for char in message if ord(char) >= 0x20 and ord(char) != 0x7F)
                .split()
            )
            if cleaned:
                return cleaned[:240]
        return "部分牌组内容暂不支持同步，请查看受影响牌组并排除后重试。"
    if isinstance(code, str):
        return _ERROR_LABELS.get(code, "同步遇到问题，请在设备端查看状态。")
    if isinstance(value, str):
        return _ERROR_LABELS.get(value, "同步遇到问题，请在设备端查看状态。")
    return "同步遇到问题，请在设备端查看状态。"


def build_sync_overview(
    status: Any,
    devices: Sequence[DeviceItem],
    *,
    devices_loaded: bool,
) -> SyncOverview:
    """只展示 runtime 明确报告的同步结果，不从快照或心跳推断完成/在线。"""

    raw = _mapping(status)
    state = raw.get("state")
    service_running = raw.get("running") is True or state == "running"
    paired = tuple(item for item in devices if item.status_key in {"bound", "rebind_ready"})
    device_lines = tuple(
        f"{item.name} · 最近连接：{item.last_seen_text}" for item in paired
    )
    sync_value = raw.get("sync")
    sync = _mapping(sync_value)
    has_sync_status = isinstance(sync_value, Mapping)

    if not service_running:
        if state in {"stopped", "starting", "stopping"} or raw.get("running") is False:
            headline = "服务未启用"
            detail = "InkCards 服务未运行。启动 Anki 后，设备同步服务会启用。"
        else:
            headline = "服务状态未知"
            detail = "暂时无法确认设备同步服务状态。"
        return SyncOverview(
            headline,
            detail,
            "暂无同步记录" if not has_sync_status else "同步状态未知",
            "",
            device_lines,
            False,
            len(paired),
            has_sync_status,
        )

    if not devices_loaded:
        headline = "设备状态未知"
        detail = "服务已启动，但暂时无法读取设备列表。"
    elif not paired:
        if any(item.status_key == "pending" for item in devices):
            headline = "等待设备完成配对"
            detail = "在阅读器上输入配对码，完成后即可发起同步。"
        else:
            headline = "尚未配对设备"
            detail = "添加设备并完成配对后，可在 X4 上发起同步。"
    elif not has_sync_status:
        headline = "已就绪 · 等待设备发起同步"
        detail = "在 X4 上点击「同步」即可；Anki 需要保持打开。"
    else:
        sync_state = sync.get("state")
        sync_state = sync_state.lower() if isinstance(sync_state, str) else ""
        stage_text = _sync_stage_text(sync.get("stage"))
        if sync_state in {"running", "syncing", "in_progress", "active"}:
            headline = "正在同步"
            detail = stage_text or "设备同步正在进行。"
        elif sync_state in {"completed", "complete", "succeeded", "success", "finished"}:
            headline = "同步完成"
            counts = []
            for key, label, unit in (
                ("received_count", "接收复习记录", "条"),
                ("updated_count", "更新卡片", "张"),
            ):
                count = sync.get(key)
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    counts.append(f"{label} {count} {unit}")
            detail = " · ".join(counts) or stage_text or "最近一次设备同步已完成。"
        elif sync_state in {"failed", "error", "blocked", "rejected"}:
            headline = "最近同步未完成"
            detail = _sync_error_text(sync.get("error"))
        elif sync_state in {"waiting", "waiting_device", "idle", "ready"}:
            headline = "已就绪 · 等待设备发起同步"
            detail = stage_text or "在 X4 上点击「同步」即可；Anki 需要保持打开。"
        else:
            headline = "同步状态未知"
            detail = "服务已启动，但 runtime 没有提供可识别的同步状态。"

    preparation = _mapping(raw.get("preparation"))
    # 背景准备与设备传输分别展示，不把旧的“同步完成”误报为新版已就绪。
    if paired and sync.get("state") not in {"active", "running", "syncing"}:
        prepared_state = preparation.get("state")
        if prepared_state == "preparing":
            headline = "正在后台准备同步数据"
            detail = {"scanning": "正在检查卡片与媒体变化。",
                      "exporting": "正在准备卡片和图片，设备可稍后同步。",
                      "packing": "正在生成传输包。"}.get(preparation.get("stage"), "正在准备同步数据。")
        elif prepared_state == "ready":
            headline = "数据已准备好 · 等待设备同步"
            detail = "图片和传输包已在电脑端生成，设备同步时直接领取。"
        elif prepared_state == "failed":
            headline = "同步数据准备失败"
            detail = _sync_error_text({"code": preparation.get("error_code") or "snapshot_prepare_failed"})

    if not has_sync_status:
        recent_text = "暂无同步记录"
    else:
        recent_state = sync.get("state")
        recent_state = recent_state.lower() if isinstance(recent_state, str) else ""
        recent_text = {
            "completed": "同步完成",
            "complete": "同步完成",
            "succeeded": "同步完成",
            "success": "同步完成",
            "finished": "同步完成",
            "running": "同步进行中",
            "syncing": "同步进行中",
            "in_progress": "同步进行中",
            "active": "同步进行中",
            "failed": "同步未完成",
            "error": "同步未完成",
            "blocked": "同步未完成",
            "rejected": "同步未完成",
            "waiting": "等待设备发起同步",
            "waiting_device": "等待设备发起同步",
            "idle": "暂无同步记录",
            "ready": "暂无同步记录",
        }.get(recent_state, "同步状态未知")
    return SyncOverview(
        headline=headline,
        detail=detail,
        recent_text=recent_text,
        recent_time=_sync_time_text(sync),
        device_lines=device_lines,
        service_running=True,
        paired_device_count=len(paired),
        has_sync_status=has_sync_status,
    )


def build_status_view(
    status: Optional[Mapping[str, Any]],
    *,
    catalog: Any = None,
    preflight: Any = None,
) -> StatusView:
    """把 runtime 脱敏状态投影成可直接显示的用户状态。"""

    raw = _mapping(status)
    state = _safe_text(raw.get("state"), "stopped")
    service_text = _SERVICE_LABELS.get(state, "服务状态未知")
    bind = _mapping(raw.get("bind"))
    host = _safe_text(bind.get("host"), "未知")
    port = bind.get("port")
    port_text = str(port) if isinstance(port, int) and not isinstance(port, bool) else "未知"
    persistence = _mapping(raw.get("persistence"))
    devices = _safe_count(persistence.get("devices"))
    events = _safe_count(persistence.get("events"))
    conflicts = _safe_count(persistence.get("conflicts"))
    snapshots = _safe_count(_mapping(raw.get("snapshot")).get("count"))
    generation = raw.get("collection_generation")
    generation_text = str(generation) if isinstance(generation, int) and not isinstance(generation, bool) else "未知"

    catalog_status = _mapping(raw.get("catalog"))
    if catalog is not None:
        catalog_text = f"已加载（{len(_catalog_entries(catalog))} 个牌组）"
    elif catalog_status.get("available") is True:
        catalog_text = f"已加载（{_safe_count(catalog_status.get('deck_count'))} 个牌组）"
    else:
        catalog_text = "尚未加载"

    preflight_status = _mapping(raw.get("preflight"))
    selected_preflight = preflight if preflight is not None else (preflight_status if preflight_status.get("available") else None)
    if selected_preflight is None:
        preflight_text = "尚未预检"
    else:
        card_count = getattr(selected_preflight, "card_count", None)
        limit = getattr(selected_preflight, "snapshot_limit", None)
        # P4-C 的预检语义固定为 include_subdecks=True；若 status 是旧的
        # 脱敏投影而没有重复携带该字段，也可以如实显示这一固定语义。
        include_subdecks = getattr(selected_preflight, "include_subdecks", True)
        if isinstance(selected_preflight, Mapping):
            card_count = selected_preflight.get("card_count", card_count)
            limit = selected_preflight.get("snapshot_limit", limit)
            include_subdecks = selected_preflight.get("include_subdecks", True)
        count_text = str(_safe_count(card_count)) if isinstance(card_count, int) else "未知"
        limit_text = str(_safe_count(limit)) if isinstance(limit, int) else "未知"
        within = _is_within_limit(selected_preflight)
        result_text = "可准备快照" if within else "超过上限"
        parent_text = "是" if include_subdecks is True else "否" if include_subdecks is False else "未知"
        preflight_text = f"{count_text} 张 / 上限 {limit_text}；父组含子组：{parent_text}；{result_text}"

    write_enabled = raw.get("write_enabled") is True
    compact = raw.get("compact_application_capability", {})
    if isinstance(compact, Mapping) and compact.get("apply_enabled") is True:
        write_reason = "x4-3 四态评分由 Anki 后台应用；离线间隔为预估，同步后以 Anki 为准。不确定日切会阻塞；旧 P3 写回关闭。"
    elif write_enabled:
        write_reason = "复习写回已启用。"
    else:
        # 后端的 P3 code/英文 reason 只用于诊断；UI 固定给出用户能理解且不
        # 伪装同步完成的安全边界。
        write_reason = "复习已接收但尚不能安全写入 Anki；历史复习应用与崩溃恢复验证尚未闭合。"
    host_known = isinstance(bind.get("host"), str) and bool(bind.get("host"))
    lan_enabled = host_known and not _is_loopback(host)
    if not host_known:
        lan_notice = "监听地址未知；请在设置中检查局域网配置。"
    elif not lan_enabled:
        lan_notice = "当前仅监听本机，局域网设备无法连接。"
    else:
        lan_notice = "当前允许局域网设备连接；配置变更需重启 Anki 才生效。"
    return StatusView(
        service_text=service_text,
        host_text=host,
        port_text=port_text,
        devices=devices,
        events=events,
        delivery=_counter_views(persistence.get("delivery_status")),
        application=_counter_views(persistence.get("application_status")),
        conflicts=conflicts,
        snapshots=snapshots,
        generation_text=generation_text,
        catalog_text=catalog_text,
        preflight_text=preflight_text,
        write_disabled_reason=write_reason,
        lan_notice=lan_notice,
        lan_enabled=lan_enabled,
        lan_known=host_known,
    )


def safe_error_message(error: Any) -> str:
    """只按稳定错误码映射文案，绝不把异常原文/token/raw payload 放入 UI。"""

    code = getattr(error, "code", None)
    if not isinstance(code, str):
        return "操作失败，请稍后重试。"
    return _ERROR_LABELS.get(code, "操作失败，请稍后重试。")


class InkCardsPresenter:
    """驱动 UI 的纯 Python presenter；所有 QueryOp 只排队，不等待。"""

    def __init__(
        self,
        runtime_provider: Callable[[], Any],
        *,
        config_writer: Optional[Callable[[bool], Any]] = None,
    ) -> None:
        if not callable(runtime_provider):
            raise TypeError("runtime_provider must be callable")
        self._runtime_provider = runtime_provider
        self._config_writer = config_writer
        self._raw_status: Mapping[str, Any] = {}
        self._status_available = False
        self._catalog: Any = None
        self._catalog_cache_owner: Any = object()
        self._catalog_decks_cache: Tuple[DeckItem, ...] = ()
        self._catalog_parents_cache: Dict[str, Optional[str]] = {}
        self._policy_cache_signature: Any = None
        self._policy_state_cache: Tuple[Dict[str, bool], Tuple[str, ...], Tuple[str, ...]] = (
            {},
            (),
            (),
        )
        self._selected_ids: Tuple[str, ...] = ()
        self._devices: Tuple[DeviceItem, ...] = ()
        self._devices_loaded = False
        self._selected_slot_id: Optional[str] = None
        self._show_removed = False
        self._preflight: Any = None
        self._preflight_selection: Tuple[str, ...] = ()
        self._jobs: Dict[str, Any] = {}
        self._handled_jobs: set[str] = set()
        self._pairing_message = ""
        self._preflight_message = ""
        self._snapshot_message = ""
        self._config_message = ""
        self._recovery_message = ""
        self._error_message = ""
        self._excluded_deck_ids: Tuple[str, ...] = ()
        self._policy_revision: Optional[int] = None
        self._policy_loaded = False
        self._policy_message = ""
        self._policy_error = ""
        self._closed = False

    @property
    def catalog(self) -> Any:
        return self._catalog

    @property
    def selected_ids(self) -> Tuple[str, ...]:
        return self._selected_ids

    @property
    def excluded_deck_ids(self) -> Tuple[str, ...]:
        return self._excluded_deck_ids

    @property
    def sync_policy_available(self) -> bool:
        return self._policy_loaded

    @property
    def preflight(self) -> Any:
        return self._preflight

    @property
    def snapshot_ready(self) -> bool:
        return bool(
            self._selected_ids
            and self._preflight is not None
            and self._preflight_selection == self._selected_ids
            and _is_within_limit(self._preflight)
        )

    def _runtime(self) -> Any:
        try:
            return self._runtime_provider()
        except Exception:
            return None

    def reopen(self) -> None:
        self._closed = False

    def refresh_status(self) -> ViewState:
        runtime = self._runtime()
        if runtime is None:
            self._raw_status = {}
            self._status_available = False
            self._error_message = "请先打开 Anki profile。"
            return self.view()
        try:
            reader = getattr(runtime, "get_status", None)
            if not callable(reader):
                reader = getattr(runtime, "status", None)
            if not callable(reader):
                self._raw_status = {}
                self._status_available = False
                self._error_message = "InkCards 状态暂不可用。"
                return self.view()
            value = reader()
            self._raw_status = value if isinstance(value, Mapping) else {}
            self._status_available = isinstance(value, Mapping)
            if self._raw_status.get("running") is True:
                self._error_message = ""
        except Exception as error:
            self._raw_status = {}
            self._status_available = False
            self._error_message = safe_error_message(error)
        return self.view()

    def refresh(self) -> ViewState:
        self.refresh_status()
        self.refresh_devices()
        self.refresh_sync_policy()
        self.request_catalog()
        return self.view()

    def refresh_sync_policy(self) -> ViewState:
        """读取显式排除 ID；目录不完整时也保留未知 ID。"""

        runtime = self._runtime()
        reader = getattr(runtime, "get_sync_policy", None) if runtime is not None else None
        if not callable(reader):
            self._policy_loaded = False
            self._policy_error = "排除设置暂不可用，当前按全部牌组显示。"
            self._policy_message = ""
            return self.view()
        try:
            value = reader()
            if not isinstance(value, Mapping):
                raise ValueError("invalid sync policy")
            excluded = value.get("excluded_deck_ids")
            revision = value.get("revision")
            if (
                not isinstance(excluded, (list, tuple, set, frozenset))
                or any(not isinstance(item, str) or not item for item in excluded)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise ValueError("invalid sync policy")
            self._excluded_deck_ids = _normalise_ids(excluded)
            self._policy_revision = revision
            self._policy_loaded = True
            self._policy_error = ""
            self._policy_message = "排除设置已自动保存；下次同步生效。"
        except Exception as error:
            self._policy_loaded = False
            self._policy_error = (
                "排除设置响应无效，无法确认当前保存状态。"
                if isinstance(error, ValueError)
                else safe_error_message(error)
            )
            self._policy_message = ""
        return self.view()

    def _deck_parent_map(self) -> Dict[str, Optional[str]]:
        self.deck_items
        return self._catalog_parents_cache

    def _ancestor_ids(self, deck_id: str) -> Tuple[str, ...]:
        parents = self._deck_parent_map()
        result = []
        seen = {deck_id}
        current = parents.get(deck_id)
        while current is not None and current not in seen:
            result.append(current)
            seen.add(current)
            current = parents.get(current)
        return tuple(result)

    def is_deck_excluded(self, deck_id: Any) -> bool:
        selected = str(deck_id) if deck_id is not None else ""
        if not selected:
            return False
        effective, _effective_ids, _inherited_ids = self._effective_policy_state()
        return effective.get(selected, False)

    def _effective_policy_state(
        self, decks: Tuple[DeckItem, ...] | None = None
    ) -> Tuple[Dict[str, bool], Tuple[str, ...], Tuple[str, ...]]:
        signature = (id(self._catalog), self._excluded_deck_ids)
        if signature == self._policy_cache_signature:
            return self._policy_state_cache
        if decks is None:
            decks = self.deck_items
        parents = self._deck_parent_map()
        explicit = set(self._excluded_deck_ids)
        effective: Dict[str, bool] = {}
        inherited: Dict[str, bool] = {}

        def has_excluded_ancestor(deck_id: str) -> bool:
            if deck_id in inherited:
                return inherited[deck_id]
            chain = []
            visited = {deck_id}
            current = parents.get(deck_id)
            while (
                current is not None
                and current not in explicit
                and current not in inherited
                and current not in visited
            ):
                visited.add(current)
                chain.append(current)
                current = parents.get(current)
            if current in explicit:
                result = True
            elif current is not None and current in inherited:
                result = inherited[current]
            else:
                result = False
            for item_id in reversed(chain):
                inherited[item_id] = result
            inherited[deck_id] = result
            return result

        for item in decks:
            inherited_status = has_excluded_ancestor(item.deck_id)
            effective[item.deck_id] = (
                item.deck_id in explicit or inherited_status
            )
        effective_ids = tuple(
            item.deck_id for item in decks if effective.get(item.deck_id, False)
        )
        inherited_ids = tuple(
            item.deck_id
            for item in decks
            if inherited.get(item.deck_id, False)
        )
        state = (effective, effective_ids, inherited_ids)
        self._policy_cache_signature = signature
        self._policy_state_cache = state
        return state

    def set_deck_excluded(self, deck_id: Any, excluded: bool) -> bool:
        """保存一个牌组的显式排除状态；父级排除对子级有不可覆盖的继承。"""

        selected = str(deck_id) if deck_id is not None else ""
        if not selected or selected not in {item.deck_id for item in self.deck_items}:
            self._policy_message = ""
            self._policy_error = "牌组目录已变化，请刷新后重试。"
            return False
        if not self._policy_loaded:
            self._policy_message = ""
            if not self._policy_error:
                self._policy_error = "排除设置暂不可用，无法确认已保存状态。"
            return False
        if not excluded and any(
            parent in set(self._excluded_deck_ids)
            for parent in self._ancestor_ids(selected)
        ):
            self._policy_message = ""
            self._policy_error = "此牌组由上级牌组排除；先取消上级排除后才能更改。"
            return False

        previous = self._excluded_deck_ids
        updated = set(previous)
        if excluded:
            updated.add(selected)
        else:
            updated.discard(selected)
        candidate = _normalise_ids(updated)
        if candidate == previous:
            self._policy_message = "排除设置已自动保存；下次同步生效。"
            self._policy_error = ""
            return True

        runtime = self._runtime()
        writer = getattr(runtime, "set_excluded_decks", None) if runtime is not None else None
        if not callable(writer):
            self._policy_message = ""
            self._policy_error = "排除设置暂不可用，无法保存更改。"
            return False
        try:
            value = writer(list(candidate))
            if not isinstance(value, Mapping):
                raise ValueError("invalid sync policy response")
            returned = value.get("excluded_deck_ids")
            revision = value.get("revision")
            if (
                not isinstance(returned, (list, tuple, set, frozenset))
                or any(not isinstance(item, str) or not item for item in returned)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
                or not set(candidate).issubset(set(returned))
            ):
                raise ValueError("invalid sync policy response")
            self._excluded_deck_ids = _normalise_ids(returned)
            self._policy_revision = revision
            self._policy_loaded = True
            self._policy_message = "排除设置已自动保存；下次同步生效。"
            self._policy_error = ""
            return True
        except Exception as error:
            self._excluded_deck_ids = previous
            self._policy_message = ""
            self._policy_error = (
                "排除设置保存响应无效，已恢复到上次确认的状态。"
                if isinstance(error, ValueError)
                else safe_error_message(error)
            )
            return False

    def _request_job(self, kind: str, request: Callable[[Any], Any]) -> Any:
        runtime = self._runtime()
        if runtime is None:
            self._error_message = "请先打开 Anki profile。"
            return None
        try:
            job = request(runtime)
        except Exception as error:
            self._error_message = safe_error_message(error)
            return None
        self._jobs[kind] = job
        self._handled_jobs.discard(str(getattr(job, "job_id", id(job))))
        self._error_message = ""
        return job

    def request_approved_recovery(self) -> Any:
        if self._closed:
            return None
        previous = self._jobs.get("recovery")
        if previous is not None and not _job_is_final(previous):
            return previous
        job = self._request_job("recovery", lambda runtime: runtime.request_approved_adopted_recovery())
        if job is not None:
            self._recovery_message = "已排队恢复已批准的评分，请等待宿主作业完成。"
        return job

    def request_catalog(self) -> Any:
        return self._request_job("catalog", lambda runtime: runtime.request_deck_catalog())

    def select_decks(self, deck_ids: Sequence[Any]) -> ViewState:
        selected = _normalise_ids(deck_ids)
        if selected == self._selected_ids:
            return self.view()
        old_preflight = self._jobs.get("preflight")
        if old_preflight is not None and not _job_is_final(old_preflight):
            try:
                old_preflight.cancel()
            except Exception:
                pass
        self._selected_ids = selected
        self._preflight = None
        self._preflight_selection = ()
        self._preflight_message = ""
        self._snapshot_message = ""
        self._error_message = ""
        return self.view()

    def request_preflight(self) -> Any:
        if not self._selected_ids:
            self._error_message = _ERROR_LABELS["scope_required"]
            return None
        selected = self._selected_ids
        self._preflight = None
        self._preflight_selection = ()
        self._preflight_message = "正在预检所选牌组…"
        return self._request_job(
            "preflight",
            lambda runtime: runtime.request_deck_preflight(deck_ids=list(selected)),
        )

    def request_snapshot(self) -> Any:
        if not self.snapshot_ready:
            self._error_message = "请先完成与当前选择匹配的预检，并确认未超过上限。"
            return None
        selected = self._selected_ids
        self._snapshot_message = "正在准备只读快照…"
        return self._request_job(
            "snapshot",
            lambda runtime: runtime.request_snapshot(deck_ids=list(selected)),
        )

    def refresh_devices(self) -> ViewState:
        runtime = self._runtime()
        reader = getattr(runtime, "list_devices", None) if runtime is not None else None
        if not callable(reader):
            self._devices = ()
            self._devices_loaded = False
            self._selected_slot_id = None
            if runtime is not None:
                self._error_message = "设备管理暂不可用，请刷新或重启 InkCards。"
            return self.view()
        try:
            raw_devices = reader(include_revoked=self._show_removed)
        except Exception as error:
            self._devices = ()
            self._devices_loaded = False
            self._selected_slot_id = None
            self._error_message = safe_error_message(error)
            return self.view()
        entries = raw_devices if isinstance(raw_devices, (list, tuple)) else ()
        self._devices_loaded = True
        devices = tuple(
            item
            for raw in entries
            if (item := _device_item(raw)) is not None
            and (self._show_removed or item.status_key != "revoked")
        )
        self._devices = devices
        if not any(item.slot_id == self._selected_slot_id for item in devices):
            self._selected_slot_id = None
        self._error_message = ""
        return self.view()

    def set_show_removed(self, enabled: bool) -> ViewState:
        selected = bool(enabled)
        if self._show_removed != selected:
            self._show_removed = selected
            self._selected_slot_id = None
        return self.refresh_devices()

    def select_device(self, slot_id: Any) -> ViewState:
        selected = slot_id if isinstance(slot_id, str) else None
        self._selected_slot_id = (
            selected
            if any(item.slot_id == selected for item in self._devices)
            else None
        )
        self._pairing_message = ""
        self._config_message = ""
        self._error_message = ""
        return self.view()

    def _selected_device(self) -> Optional[DeviceItem]:
        return next(
            (item for item in self._devices if item.slot_id == self._selected_slot_id),
            None,
        )

    def _device_runtime(self, method_name: str) -> Any:
        runtime = self._runtime()
        if runtime is None:
            self._error_message = "请先打开 Anki profile。"
            return None
        if self._closed:
            return None
        if not callable(getattr(runtime, method_name, None)):
            self._error_message = "设备管理暂不可用，请刷新或重启 InkCards。"
            return None
        return runtime

    @staticmethod
    def _format_pairing_code(value: Any) -> Optional[str]:
        if not isinstance(value, str) or len(value) != 12 or not value.isdigit():
            return None
        return f"{value[:4]}-{value[4:8]}-{value[8:]}"

    def add_device(self, name: Any) -> Optional[str]:
        self._pairing_message = ""
        self._config_message = ""
        selected = name.strip() if isinstance(name, str) else ""
        if not is_valid_device_name(selected):
            self._error_message = "设备名称无效，请使用 1 至 128 个可见字符。"
            return None
        runtime = self._device_runtime("add_device")
        if runtime is None:
            return None
        try:
            result = _mapping(runtime.add_device(selected))
        except Exception as error:
            self._error_message = safe_error_message(error)
            return None
        code = self._format_pairing_code(result.get("code"))
        slot_id = result.get("slot_id")
        if code is None or not isinstance(slot_id, str) or not slot_id:
            self._error_message = "添加设备响应无效，请刷新列表后重置配对码。"
            self.refresh_devices()
            return None
        self._error_message = ""
        self._config_message = "设备已添加。"
        self._pairing_message = (
            f"固定配对码：{code}（不会自动过期。请现在保存；遗失后可在列表中重置。）"
        )
        self.refresh_devices()
        self._selected_slot_id = slot_id
        return code

    def allow_rebind_selected(self) -> bool:
        self._pairing_message = ""
        self._config_message = ""
        selected = self._selected_device()
        if selected is None:
            self._error_message = "请先从设备列表选择一台设备。"
            return False
        if selected.status_key != "bound":
            self._error_message = "只有已绑定设备可以允许重新绑定。"
            return False
        runtime = self._device_runtime("allow_rebind")
        if runtime is None:
            return False
        try:
            runtime.allow_rebind(selected.slot_id)
        except Exception as error:
            self._error_message = safe_error_message(error)
            return False
        self._error_message = ""
        self._config_message = "已允许此名额重新绑定。"
        self.refresh_devices()
        return True

    def reset_pairing_code_selected(self) -> Optional[str]:
        self._pairing_message = ""
        self._config_message = ""
        selected = self._selected_device()
        if selected is None:
            self._error_message = "请先从设备列表选择一台设备。"
            return None
        if selected.status_key == "revoked":
            self._error_message = "已移除的设备不能重置配对码。"
            return None
        runtime = self._device_runtime("reset_pairing_code")
        if runtime is None:
            return None
        try:
            result = _mapping(runtime.reset_pairing_code(selected.slot_id))
        except Exception as error:
            self._error_message = safe_error_message(error)
            return None
        code = self._format_pairing_code(result.get("code"))
        if code is None:
            self._error_message = "重置配对码响应无效，请重试。"
            return None
        self._error_message = ""
        self._config_message = "配对码已重置，旧码已失效。"
        self._pairing_message = f"固定配对码：{code}（不会自动过期。请现在保存。）"
        self.refresh_devices()
        return code

    def remove_selected_device(self) -> bool:
        self._pairing_message = ""
        self._config_message = ""
        selected = self._selected_device()
        if selected is None:
            self._error_message = "请先从设备列表选择一台设备。"
            return False
        if selected.status_key == "revoked":
            self._error_message = "此设备已移除。"
            return False
        runtime = self._device_runtime("remove_device")
        if runtime is None:
            return False
        try:
            runtime.remove_device(selected.slot_id)
        except Exception as error:
            self._error_message = safe_error_message(error)
            return False
        self._error_message = ""
        self._config_message = "设备已移除，连接凭据已撤销，历史记录保留。"
        self._selected_slot_id = None
        self.refresh_devices()
        return True

    def rename_selected_device(self, name: Any) -> bool:
        self._pairing_message = ""
        self._config_message = ""
        selected = self._selected_device()
        if selected is None:
            self._error_message = "请先从设备列表选择一台设备。"
            return False
        if selected.status_key == "revoked":
            self._error_message = "已移除的设备不能改名。"
            return False
        renamed = name.strip() if isinstance(name, str) else ""
        if not is_valid_device_name(renamed):
            self._error_message = "设备名称无效，请使用 1 至 128 个可见字符。"
            return False
        runtime = self._device_runtime("rename_device")
        if runtime is None:
            return False
        try:
            runtime.rename_device(selected.slot_id, renamed)
        except Exception as error:
            self._error_message = safe_error_message(error)
            return False
        self._error_message = ""
        self._config_message = "设备名称已更新。"
        self.refresh_devices()
        return True

    def set_lan_enabled(self, enabled: bool) -> bool:
        if self._config_writer is None:
            self._error_message = "当前环境不支持写入局域网配置。"
            return False
        try:
            self._config_writer(bool(enabled))
        except Exception:
            self._error_message = "局域网配置写入失败，当前运行服务未改变。"
            return False
        self._config_message = (
            "已写入局域网监听配置；请重启 Anki 后生效。"
            if enabled
            else "已写入本机监听配置；请重启 Anki 后生效。"
        )
        self._error_message = ""
        return True

    def cancel_active(self) -> None:
        for job in tuple(self._jobs.values()):
            if _job_is_final(job):
                continue
            try:
                job.cancel()
            except Exception:
                pass

    def close(self) -> None:
        self.cancel_active()
        self._closed = True

    def _poll_job(self, kind: str, job: Any) -> None:
        job_key = str(getattr(job, "job_id", id(job)))
        if job_key in self._handled_jobs or not _job_is_final(job):
            return
        self._handled_jobs.add(job_key)
        state = _safe_state(job)
        if kind == "recovery":
            if state == "applied":
                self._recovery_message = "已批准评分恢复完成。"
            elif state == "idle":
                self._recovery_message = "已批准评分均已有终态，无需再次评分。"
            elif state == "unknown":
                self._recovery_message = "恢复结果尚未确定，请保留原记录并核验。"
            elif state == "canceled":
                self._recovery_message = "恢复作业已取消，请核验当前状态。"
            else:
                reader = getattr(job, "to_dict", None)
                details = _mapping(reader()) if callable(reader) else {}
                self._recovery_message = _ERROR_LABELS.get(details.get("reason"), "恢复被阻止，请核验来源或当前 Anki 状态。")
            reader = getattr(job, "to_dict", None)
            details = _mapping(reader()) if callable(reader) else {}
            reason = stable_code(details.get("reason"))
            if reason:
                self._recovery_message += f" 原因：{reason}"
            self.refresh_status()
            return
        if state in {"published", "completed"}:
            result = getattr(job, "result", None)
            if kind == "catalog":
                self._catalog = result
                available = {item.deck_id for item in self.deck_items}
                selected = tuple(item for item in self._selected_ids if item in available)
                if selected != self._selected_ids:
                    self._selected_ids = selected
                    self._preflight = None
                    self._preflight_selection = ()
                self._error_message = ""
            elif kind == "preflight":
                result_ids = _result_deck_ids(result)
                if result_ids == self._selected_ids:
                    self._preflight = result
                    self._preflight_selection = result_ids
                    self._preflight_message = "预检完成。"
                else:
                    self._preflight_message = "预检结果已过期，请重新预检。"
                    self._preflight = None
                    self._preflight_selection = ()
            elif kind == "snapshot":
                self._snapshot_message = "只读快照已准备，设备可按上传优先规则获取。"
            self.refresh_status()
        elif state == "failed":
            error = getattr(job, "error", None)
            message = safe_error_message(error)
            if kind == "preflight":
                self._preflight_message = "预检失败。"
            elif kind == "snapshot":
                self._snapshot_message = "快照准备失败。"
            self._error_message = message
        elif state == "canceled":
            if kind == "preflight":
                self._preflight_message = "预检已取消。"
            elif kind == "snapshot":
                self._snapshot_message = "快照准备已取消。"
        elif state == "stale_result_dropped":
            self._error_message = "任务结果已过期，请刷新后重试。"

    def poll(self) -> ViewState:
        if not self._closed:
            for kind, job in tuple(self._jobs.items()):
                self._poll_job(kind, job)
        return self.view()

    @property
    def deck_items(self) -> Tuple[DeckItem, ...]:
        if self._catalog_cache_owner is self._catalog:
            return self._catalog_decks_cache
        result = []
        for entry in _catalog_entries(self._catalog):
            deck_id = _deck_id(entry)
            if not deck_id:
                continue
            path = _deck_path(entry)
            name = _deck_name(entry)
            label = path[-1] if path else name.rsplit("::", 1)[-1]
            depth = max(_deck_depth(entry), len(path) - 1)
            unsupported = getattr(entry, "unsupported", ())
            if isinstance(entry, Mapping):
                unsupported = entry.get("unsupported", unsupported)
            result.append(
                DeckItem(
                    deck_id=deck_id,
                    label=label,
                    depth=depth,
                    unsupported=tuple(unsupported or ()),
                    parent_id=_deck_parent_id(entry),
                    path=path,
                    card_count=_deck_card_count(entry),
                )
            )
        decks = tuple(result)
        path_ids = {item.path: item.deck_id for item in decks if item.path}
        parents: Dict[str, Optional[str]] = {}
        for item in decks:
            parent_id = item.parent_id
            if parent_id == item.deck_id:
                parent_id = None
            if parent_id is None and len(item.path) > 1:
                parent_id = path_ids.get(item.path[:-1])
            parents[item.deck_id] = parent_id
        self._catalog_cache_owner = self._catalog
        self._catalog_decks_cache = decks
        self._catalog_parents_cache = parents
        return decks

    def _active_job_exists(self) -> bool:
        return any(not _job_is_final(job) for job in self._jobs.values())

    def view(self) -> ViewState:
        status = build_status_view(
            self._raw_status,
            catalog=self._catalog,
            preflight=self._preflight,
        )
        runtime = self._runtime()
        runtime_available = runtime is not None
        selected = self._selected_device()
        selected_active = selected is not None and selected.status_key != "revoked"
        device_api = runtime_available and not self._closed
        preflight_active = "preflight" in self._jobs and not _job_is_final(self._jobs["preflight"])
        snapshot_active = "snapshot" in self._jobs and not _job_is_final(self._jobs["snapshot"])
        buttons = ButtonState(
            refresh=runtime_available and not self._closed,
            add_device=device_api and callable(getattr(runtime, "add_device", None)),
            allow_rebind=(
                device_api
                and selected is not None
                and selected.status_key == "bound"
                and callable(getattr(runtime, "allow_rebind", None))
            ),
            reset_pairing_code=(
                device_api
                and selected_active
                and callable(getattr(runtime, "reset_pairing_code", None))
            ),
            remove_device=(
                device_api
                and selected_active
                and callable(getattr(runtime, "remove_device", None))
            ),
            rename_device=(
                device_api
                and selected is not None
                and selected.status_key != "revoked"
                and callable(getattr(runtime, "rename_device", None))
            ),
            preflight=runtime_available and bool(self._selected_ids) and not preflight_active and not self._closed,
            snapshot=runtime_available and self.snapshot_ready and not snapshot_active and not self._closed,
            cancel=self._active_job_exists() and not self._closed,
            lan=not self._closed,
            recovery=runtime_available and callable(getattr(self._runtime(), "request_approved_adopted_recovery", None))
                and not self._closed and not ("recovery" in self._jobs and not _job_is_final(self._jobs["recovery"])),
        )
        decks = self.deck_items
        _effective_map, effective_excluded, inherited_excluded = (
            self._effective_policy_state(decks)
        )
        overview = build_sync_overview(
            self._raw_status,
            self._devices,
            devices_loaded=self._devices_loaded and self._status_available,
        )
        return ViewState(
            status=status,
            decks=decks,
            selected_ids=self._selected_ids,
            devices=self._devices,
            selected_slot_id=self._selected_slot_id,
            show_removed=self._show_removed,
            pairing_message=self._pairing_message,
            preflight_message=self._preflight_message,
            snapshot_message=self._snapshot_message,
            config_message=self._config_message,
            error_message=self._error_message,
            buttons=buttons,
            recovery_message=self._recovery_message,
            excluded_deck_ids=self._excluded_deck_ids,
            effective_excluded_deck_ids=effective_excluded,
            inherited_excluded_deck_ids=inherited_excluded,
            sync_policy_available=self._policy_loaded,
            sync_policy_message=self._policy_message,
            sync_policy_error=self._policy_error,
            sync_overview=overview,
        )


__all__ = [
    "ButtonState",
    "CounterView",
    "DeckItem",
    "DeviceItem",
    "InkCardsPresenter",
    "StatusView",
    "SyncOverview",
    "ViewState",
    "build_sync_overview",
    "build_status_view",
    "is_valid_device_id",
    "is_valid_device_name",
    "safe_error_message",
]
