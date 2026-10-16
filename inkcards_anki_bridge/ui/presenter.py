"""InkCards UI 的纯 Python 状态投影与异步作业门控。

本模块故意不导入 ``aqt``、PyQt/PySide 或任何 Anki UI 类型。它只消费
runtime/catalog/preflight/snapshot 的窄接口，因此可以在普通 Python 测试中验证
文案、计数和“预检通过后才允许准备快照”的不变量。
"""

from __future__ import annotations

import ipaddress

from ..anki.compact_diagnostics import stable_code
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple


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
    "scope_too_large": "选择的牌组数量超过上限。",
    "snapshot_limit_exceeded": "所选牌组超过当前快照卡片上限。",
    "snapshot_unpageable": "所选范围无法生成可下载快照。",
    "unsupported_anki_build": "当前 Anki 版本不受支持。",
    "adopted_source_missing": "尚未导入已批准的恢复来源，请先在电脑端导入。",
    "adopted_source_ambiguous": "发现多个恢复来源，请先核对唯一目标会话。",
    "legacy_source_invalid": "恢复来源无效，请核验电脑端文件。",
    "legacy_source_binding_mismatch": "恢复来源与原评分记录不符，已阻止恢复。",
    "adopted_prestate_conflict": "卡片或复习历史已变化，已阻止恢复，请重新核验。",
    "reviewer_owns_collection": "请退出复习页面后再恢复。",
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

    def delivery_counts(self) -> Dict[str, int]:
        return {item.key: item.count for item in self.delivery}

    def application_counts(self) -> Dict[str, int]:
        return {item.key: item.count for item in self.application}


@dataclass(frozen=True)
class ButtonState:
    refresh: bool
    pairing: bool
    revoke: bool
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
    pairing_message: str
    preflight_message: str
    snapshot_message: str
    config_message: str
    error_message: str
    buttons: ButtonState
    recovery_message: str = ""


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
    lan_enabled = not _is_loopback(host)
    lan_notice = (
        "当前仅监听本机，局域网设备无法连接。"
        if not lan_enabled
        else "当前允许局域网设备连接；配置变更需重启 Anki 才生效。"
    )
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
        self._catalog: Any = None
        self._selected_ids: Tuple[str, ...] = ()
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
        self._closed = False

    @property
    def catalog(self) -> Any:
        return self._catalog

    @property
    def selected_ids(self) -> Tuple[str, ...]:
        return self._selected_ids

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
            self._raw_status = {"state": "stopped"}
            self._error_message = "请先打开 Anki profile。"
            return self.view()
        try:
            value = runtime.status()
            self._raw_status = value if isinstance(value, Mapping) else {"state": "unknown"}
            if self._raw_status.get("running") is True:
                self._error_message = ""
        except Exception as error:
            self._error_message = safe_error_message(error)
        return self.view()

    def refresh(self) -> ViewState:
        self.refresh_status()
        self.request_catalog()
        return self.view()

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

    def issue_pairing_code(self) -> Optional[str]:
        runtime = self._runtime()
        if runtime is None:
            self._error_message = "请先打开 Anki profile。"
            return None
        try:
            code = runtime.issue_pairing_code()
        except Exception as error:
            self._error_message = safe_error_message(error)
            return None
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            self._error_message = "配对码生成失败，请稍后重试。"
            return None
        self._pairing_message = f"配对码：{code}（两分钟有效；不会写入日志）"
        self._error_message = ""
        return code

    def revoke_device(self, device_id: str) -> bool:
        selected = device_id.strip() if isinstance(device_id, str) else ""
        if not is_valid_device_id(selected):
            self._error_message = "请输入 1 至 128 个字符且不含控制字符的设备 ID。"
            return False
        runtime = self._runtime()
        if runtime is None:
            self._error_message = "请先打开 Anki profile。"
            return False
        try:
            runtime.revoke_device(selected)
        except Exception as error:
            self._error_message = safe_error_message(error)
            return False
        self._error_message = ""
        self._config_message = "设备已撤销。"
        self.refresh_status()
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
        return tuple(
            DeckItem(
                deck_id=_deck_id(entry),
                label=_deck_name(entry),
                depth=_deck_depth(entry),
                unsupported=tuple(getattr(entry, "unsupported", ()) or ()),
            )
            for entry in _catalog_entries(self._catalog)
            if _deck_id(entry)
        )

    def _active_job_exists(self) -> bool:
        return any(not _job_is_final(job) for job in self._jobs.values())

    def view(self) -> ViewState:
        status = build_status_view(
            self._raw_status,
            catalog=self._catalog,
            preflight=self._preflight,
        )
        runtime_available = self._runtime() is not None
        preflight_active = "preflight" in self._jobs and not _job_is_final(self._jobs["preflight"])
        snapshot_active = "snapshot" in self._jobs and not _job_is_final(self._jobs["snapshot"])
        buttons = ButtonState(
            refresh=runtime_available and not self._closed,
            pairing=runtime_available and not self._closed,
            revoke=runtime_available and not self._closed,
            preflight=runtime_available and bool(self._selected_ids) and not preflight_active and not self._closed,
            snapshot=runtime_available and self.snapshot_ready and not snapshot_active and not self._closed,
            cancel=self._active_job_exists() and not self._closed,
            lan=not self._closed,
            recovery=runtime_available and callable(getattr(self._runtime(), "request_approved_adopted_recovery", None))
                and not self._closed and not ("recovery" in self._jobs and not _job_is_final(self._jobs["recovery"])),
        )
        return ViewState(
            status=status,
            decks=self.deck_items,
            selected_ids=self._selected_ids,
            pairing_message=self._pairing_message,
            preflight_message=self._preflight_message,
            snapshot_message=self._snapshot_message,
            config_message=self._config_message,
            error_message=self._error_message,
            buttons=buttons,
            recovery_message=self._recovery_message,
        )


__all__ = [
    "ButtonState",
    "CounterView",
    "DeckItem",
    "InkCardsPresenter",
    "StatusView",
    "ViewState",
    "build_status_view",
    "is_valid_device_id",
    "safe_error_message",
]
