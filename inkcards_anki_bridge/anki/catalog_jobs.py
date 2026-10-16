"""固定 Anki 26.08.1 的只读牌组目录与范围预检。

目录和预检只接受不可变的 scope/context。真正的牌组读取由
``QueryOpAdapter`` 把 ``op(collection)`` 闭包交给 Anki；本模块的作业句柄、
状态和最后成功结果都不保存 collection 引用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from ..adapter import AnkiAdapterError
from .adapter_260801 import (
    DEFAULT_MAX_DECK_CATALOG_SIZE,
    DEFAULT_MAX_SCOPE_CARD_IDS,
    Anki260801Adapter,
)
from .operations import OperationContext, OperationResult, OperationState, QueryOpAdapter


DEFAULT_MAX_DECK_IDS = 64
DEFAULT_SNAPSHOT_CARD_LIMIT = DEFAULT_MAX_SCOPE_CARD_IDS


class CatalogJobError(Exception):
    """可安全暴露给后续 Qt UI 的有界目录/预检错误。"""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


DeckCatalogError = CatalogJobError


def _id_sort_key(value: str) -> Tuple[int, Any, str]:
    if value.isdigit():
        return (0, int(value), value)
    return (1, value, value)


def _normalize_deck_ids(
    values: Optional[Sequence[Any]],
    *,
    maximum: int = DEFAULT_MAX_DECK_IDS,
) -> Tuple[str, ...]:
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("maximum must be a positive integer")
    if values is None or isinstance(values, (str, bytes)):
        raise CatalogJobError("scope_required", "deck_ids must be a non-empty sequence", status=422)
    result = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            raise CatalogJobError("scope_invalid", "deck_ids contains an invalid ID", status=422)
        selected = str(value).strip()
        if (
            not selected
            or not selected.isdigit()
            or int(selected) <= 0
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in selected)
        ):
            raise CatalogJobError("scope_invalid", "deck_ids must contain positive numeric IDs", status=422)
        result.add(str(int(selected)))
    if not result:
        raise CatalogJobError("scope_required", "deck_ids must not be empty", status=422)
    if len(result) > maximum:
        raise CatalogJobError("scope_too_large", "deck_ids exceeds the bounded limit", status=413)
    return tuple(sorted(result, key=_id_sort_key))


def make_preflight_scope(
    deck_ids: Optional[Sequence[Any]], *, maximum: int = DEFAULT_MAX_DECK_IDS
) -> Tuple[str, ...]:
    """规范化后续 UI 的显式牌组 scope。"""

    return _normalize_deck_ids(deck_ids, maximum=maximum)


def _translate_adapter_error(error: AnkiAdapterError) -> CatalogJobError:
    return CatalogJobError(error.code, error.message, status=error.status)


@dataclass(frozen=True)
class DeckCatalogEntry:
    """一个不可变的牌组目录条目；层级由 Anki 的 ``::`` 名称语义推导。"""

    deck_id: str
    name: str
    parent_name: Optional[str]
    parent_id: Optional[str]
    path: Tuple[str, ...]
    depth: int
    unsupported: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "deck_id", str(self.deck_id))
        object.__setattr__(self, "name", str(self.name))
        if self.parent_name is not None:
            object.__setattr__(self, "parent_name", str(self.parent_name))
        if self.parent_id is not None:
            object.__setattr__(self, "parent_id", str(self.parent_id))
        object.__setattr__(self, "path", tuple(str(part) for part in self.path))
        object.__setattr__(self, "unsupported", tuple(str(item) for item in self.unsupported))

    @property
    def id(self) -> str:
        return self.deck_id

    @property
    def level(self) -> int:
        return self.depth

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deck_id": self.deck_id,
            "name": self.name,
            "parent_name": self.parent_name,
            "parent_id": self.parent_id,
            "path": list(self.path),
            "depth": self.depth,
            "level": self.level,
            "unsupported": list(self.unsupported),
        }


@dataclass(frozen=True)
class DeckCatalog:
    """确定性排序且 ID 唯一的只读牌组目录。"""

    entries: Tuple[DeckCatalogEntry, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if len({entry.deck_id for entry in entries}) != len(entries):
            raise ValueError("deck catalog IDs must be unique")
        object.__setattr__(self, "entries", tuple(sorted(entries, key=_entry_sort_key)))

    @property
    def decks(self) -> Tuple[DeckCatalogEntry, ...]:
        return self.entries

    @property
    def deck_ids(self) -> Tuple[str, ...]:
        return tuple(entry.deck_id for entry in self.entries)

    def by_id(self, deck_id: Any) -> Optional[DeckCatalogEntry]:
        selected = str(deck_id)
        return next((entry for entry in self.entries if entry.deck_id == selected), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "inkcards.anki-deck-catalog.v1",
            "decks": [entry.to_dict() for entry in self.entries],
            "deck_count": len(self.entries),
        }


def _entry_sort_key(entry: DeckCatalogEntry) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[int, Any, str]]:
    folded = tuple(part.casefold() for part in entry.path)
    return folded, entry.path, _id_sort_key(entry.deck_id)


@dataclass(frozen=True)
class DeckPreflight:
    """一个显式 scope 的只读卡片数量与快照容量判断。"""

    selected_decks: Tuple[DeckCatalogEntry, ...]
    card_count: int
    snapshot_limit: int
    within_limit: bool
    include_subdecks: bool = True
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        selected = tuple(self.selected_decks)
        if isinstance(self.card_count, bool) or not isinstance(self.card_count, int) or self.card_count < 0:
            raise ValueError("card_count must be a non-negative integer")
        if isinstance(self.snapshot_limit, bool) or not isinstance(self.snapshot_limit, int) or self.snapshot_limit <= 0:
            raise ValueError("snapshot_limit must be a positive integer")
        if not isinstance(self.include_subdecks, bool):
            raise ValueError("include_subdecks must be a boolean")
        object.__setattr__(self, "selected_decks", selected)
        if self.card_count > self.snapshot_limit and self.error_code is None:
            object.__setattr__(self, "error_code", "snapshot_limit_exceeded")
            object.__setattr__(
                self,
                "error_message",
                "selected decks exceed the current snapshot card limit",
            )
        object.__setattr__(self, "within_limit", bool(self.card_count <= self.snapshot_limit and self.within_limit))

    @property
    def selected(self) -> Tuple[DeckCatalogEntry, ...]:
        return self.selected_decks

    @property
    def deck_ids(self) -> Tuple[str, ...]:
        return tuple(entry.deck_id for entry in self.selected_decks)

    @property
    def limit(self) -> int:
        return self.snapshot_limit

    @property
    def exceeds_limit(self) -> bool:
        return not self.within_limit

    @property
    def error(self) -> Optional[Mapping[str, Any]]:
        if self.error_code is None:
            return None
        return {"code": self.error_code, "message": self.error_message or self.error_code}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "inkcards.anki-deck-preflight.v1",
            "deck_ids": list(self.deck_ids),
            "selected_decks": [entry.to_dict() for entry in self.selected_decks],
            "card_count": self.card_count,
            "snapshot_limit": self.snapshot_limit,
            "within_limit": self.within_limit,
            "include_subdecks": self.include_subdecks,
            "error": dict(self.error) if self.error is not None else None,
        }


def _build_entries(pairs: Sequence[Tuple[str, str]]) -> DeckCatalog:
    by_name = {name: deck_id for deck_id, name in pairs}
    entries = []
    for deck_id, name in pairs:
        parts = tuple(name.split("::"))
        if not name.strip() or not parts or any(not part.strip() for part in parts):
            raise CatalogJobError("deck_name_invalid", "deck name has an invalid hierarchy", status=422)
        parent_name = "::".join(parts[:-1]) or None
        parent_id = by_name.get(parent_name) if parent_name is not None else None
        unsupported = () if parent_name is None or parent_id is not None else ("parent_missing",)
        entries.append(
            DeckCatalogEntry(
                deck_id=deck_id,
                name=name,
                parent_name=parent_name,
                parent_id=parent_id,
                path=parts,
                depth=len(parts) - 1,
                unsupported=unsupported,
            )
        )
    return DeckCatalog(tuple(entries))


def build_deck_catalog(
    collection: Any,
    adapter: Anki260801Adapter,
    *,
    max_decks: int = DEFAULT_MAX_DECK_CATALOG_SIZE,
    cancel_check: Optional[Callable[[], None]] = None,
) -> DeckCatalog:
    """在 QueryOp closure 内构造不可变牌组目录。"""

    try:
        pairs = adapter.read_deck_names_and_ids(
            collection,
            max_decks=max_decks,
            cancel_check=cancel_check,
        )
    except AnkiAdapterError as exc:
        raise _translate_adapter_error(exc) from exc
    return _build_entries(pairs)


def build_deck_preflight(
    collection: Any,
    adapter: Anki260801Adapter,
    *,
    deck_ids: Sequence[Any],
    snapshot_limit: int = DEFAULT_SNAPSHOT_CARD_LIMIT,
    max_decks: int = DEFAULT_MAX_DECK_CATALOG_SIZE,
    max_scope_ids: int = DEFAULT_MAX_DECK_IDS,
    cancel_check: Optional[Callable[[], None]] = None,
) -> DeckPreflight:
    """在 QueryOp closure 内按固定含子牌组语义完成一次范围预检。"""

    if isinstance(snapshot_limit, bool) or not isinstance(snapshot_limit, int) or snapshot_limit <= 0:
        raise ValueError("snapshot_limit must be a positive integer")
    selected_ids = _normalize_deck_ids(deck_ids, maximum=max_scope_ids)
    catalog = build_deck_catalog(
        collection,
        adapter,
        max_decks=max_decks,
        cancel_check=cancel_check,
    )
    selected = tuple(catalog.by_id(deck_id) for deck_id in selected_ids)
    missing = [deck_id for deck_id, entry in zip(selected_ids, selected) if entry is None]
    if missing:
        raise CatalogJobError(
            "deck_not_found",
            "requested deck is unavailable",
            status=404,
        )
    selected_entries = tuple(entry for entry in selected if entry is not None)
    if cancel_check is not None:
        cancel_check()
    checker = getattr(adapter, 'device_preset_restrictions', None)
    if callable(checker):
        # 预检包含子组，必须检查每个被选祖先下实际覆盖的 preset。
        covered = tuple(entry.deck_id for entry in catalog.entries if any(
            entry.path[:len(parent.path)] == parent.path for parent in selected_entries
        ))
        restrictions = checker(collection, covered)
        if restrictions:
            raise CatalogJobError(restrictions[0], '当前设备不支持该 FSRS 短期或空学习步配置', status=409)
    try:
        # adapter 只暴露官方 DeckManager.card_count()；这里固定传入
        # include_subdecks=True，以保持 Anki 搜索语义。
        card_count = adapter.count_deck_cards(collection, selected_ids)
    except AnkiAdapterError as exc:
        raise _translate_adapter_error(exc) from exc
    if cancel_check is not None:
        cancel_check()
    within_limit = card_count <= snapshot_limit
    error_code = None if within_limit else "snapshot_limit_exceeded"
    error_message = (
        None
        if within_limit
        else "selected decks exceed the current snapshot card limit"
    )
    return DeckPreflight(
        selected_decks=selected_entries,
        card_count=card_count,
        snapshot_limit=snapshot_limit,
        within_limit=within_limit,
        include_subdecks=True,
        error_code=error_code,
        error_message=error_message,
    )


catalog_from_collection = build_deck_catalog
preflight_from_collection = build_deck_preflight


class CatalogJobState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    STARTED = "started"
    PUBLISHED = "published"
    COMPLETED = "published"
    FAILED = "failed"
    CANCELED = "canceled"
    STALE_RESULT_DROPPED = "stale_result_dropped"


_FINAL_STATES = frozenset(
    {
        CatalogJobState.PUBLISHED,
        CatalogJobState.FAILED,
        CatalogJobState.CANCELED,
        CatalogJobState.STALE_RESULT_DROPPED,
    }
)


class _CatalogCanceled(Exception):
    pass


class _CatalogContextStale(Exception):
    pass


def _context(value: Any) -> OperationContext:
    try:
        selected = OperationContext.from_value(value)
    except Exception as exc:
        raise CatalogJobError("context_unavailable", "catalog context is unavailable", status=409) from exc
    if not selected.profile_identity or selected.generation is None or selected.runtime_epoch is None:
        raise CatalogJobError(
            "context_unavailable",
            "catalog context must include profile identity, collection generation, and runtime epoch",
            status=409,
        )
    return selected


class CatalogJob:
    """一个可取消的目录或预检 QueryOp 任务句柄。"""

    def __init__(
        self,
        manager: "CatalogJobManager",
        job_id: str,
        kind: str,
        scope_key: Tuple[str, Tuple[str, ...]],
        context: OperationContext,
        request_token: int,
    ) -> None:
        self._manager = manager
        self.job_id = job_id
        self.kind = kind
        self.scope_key = scope_key
        self.context = context
        self.request_token = request_token
        self._state = CatalogJobState.CREATED
        self._cancel_requested = False
        self._operation_task: Any = None
        self._result: Any = None
        self._error: Optional[BaseException] = None

    @property
    def state(self) -> CatalogJobState:
        with self._manager._lock:
            return self._state

    @property
    def result(self) -> Any:
        with self._manager._lock:
            return self._result

    @property
    def error(self) -> Optional[BaseException]:
        with self._manager._lock:
            return self._error

    @property
    def operation_task(self) -> Any:
        with self._manager._lock:
            return self._operation_task

    @property
    def cancel_requested(self) -> bool:
        with self._manager._lock:
            return self._cancel_requested

    def cancel(self) -> bool:
        return self._manager.cancel(self.job_id)

    def to_dict(self) -> Dict[str, Any]:
        with self._manager._lock:
            return {
                "job_id": self.job_id,
                "kind": self.kind,
                "scope_kind": self.scope_key[0],
                "deck_ids": list(self.scope_key[1]),
                "state": self._state.value,
                "cancel_requested": self._cancel_requested,
                "profile_identity": self.context.profile_identity,
                "collection_generation": self.context.generation,
                "runtime_epoch": self.context.runtime_epoch,
            }


class CatalogJobManager:
    """组合 P4-B QueryOp 边界，管理目录/预检的有界异步状态。"""

    def __init__(
        self,
        *,
        operation_adapter: QueryOpAdapter,
        adapter: Anki260801Adapter,
        context_provider: Callable[[], Any],
        max_decks: int = DEFAULT_MAX_DECK_CATALOG_SIZE,
        max_scope_ids: int = DEFAULT_MAX_DECK_IDS,
        snapshot_limit: int = DEFAULT_SNAPSHOT_CARD_LIMIT,
        max_active_jobs: int = 16,
        max_retained_jobs: int = 64,
    ) -> None:
        if not isinstance(operation_adapter, QueryOpAdapter):
            raise TypeError("operation_adapter must be a QueryOpAdapter")
        if not isinstance(adapter, Anki260801Adapter):
            raise TypeError("adapter must be an Anki260801Adapter")
        if not callable(context_provider):
            raise TypeError("context_provider must be callable")
        if isinstance(max_decks, bool) or not isinstance(max_decks, int) or max_decks <= 0:
            raise ValueError("max_decks must be a positive integer")
        if isinstance(max_scope_ids, bool) or not isinstance(max_scope_ids, int) or max_scope_ids <= 0:
            raise ValueError("max_scope_ids must be a positive integer")
        if isinstance(snapshot_limit, bool) or not isinstance(snapshot_limit, int) or snapshot_limit <= 0:
            raise ValueError("snapshot_limit must be a positive integer")
        if isinstance(max_active_jobs, bool) or not isinstance(max_active_jobs, int) or max_active_jobs <= 0:
            raise ValueError("max_active_jobs must be a positive integer")
        if isinstance(max_retained_jobs, bool) or not isinstance(max_retained_jobs, int) or max_retained_jobs < max_active_jobs:
            raise ValueError("max_retained_jobs must be at least max_active_jobs")
        self._max_active_jobs = max_active_jobs
        self._max_retained_jobs = max_retained_jobs
        self._operation_adapter = operation_adapter
        self._adapter = adapter
        self._context_provider = context_provider
        self._max_decks = max_decks
        self._max_scope_ids = max_scope_ids
        self._snapshot_limit = snapshot_limit
        self._lock = threading.RLock()
        self._closed = False
        self._next_id = 0
        self._next_token = {"catalog": 0, "preflight": 0}
        self._active: Dict[Tuple[str, Tuple[str, ...]], CatalogJob] = {}
        self._jobs: Dict[str, CatalogJob] = {}
        self._last_catalog: Optional[DeckCatalog] = None
        self._last_preflight: Optional[DeckPreflight] = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def last_catalog(self) -> Optional[DeckCatalog]:
        with self._lock:
            return self._last_catalog

    @property
    def last_preflight(self) -> Optional[DeckPreflight]:
        with self._lock:
            return self._last_preflight

    @property
    def snapshot_limit(self) -> int:
        return self._snapshot_limit

    def _read_context(self) -> OperationContext:
        try:
            value = self._context_provider()
        except CatalogJobError:
            raise
        except Exception as exc:
            raise CatalogJobError("context_unavailable", "牌组上下文不可用", status=409) from exc
        return _context(value)

    def _safe_context(self) -> Optional[OperationContext]:
        try:
            return self._read_context()
        except Exception:
            return None

    def _remove(self, job: CatalogJob) -> None:
        if self._active.get(job.scope_key) is job:
            del self._active[job.scope_key]

    def _trim_history(self) -> None:
        for job_id, job in tuple(self._jobs.items()):
            if len(self._jobs) <= self._max_retained_jobs:
                break
            if job._state in _FINAL_STATES:
                del self._jobs[job_id]

    def _finish(
        self,
        job: CatalogJob,
        state: CatalogJobState,
        *,
        value: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        job._state = state
        job._result = value
        # 终态记录不保留异常调用栈内的集合、目录或预检临时对象。
        job._error = CatalogJobError(
            str(getattr(error, "code", "catalog_job_failed")),
            error.message if isinstance(error, CatalogJobError) else "牌组作业失败",
            status=getattr(error, "status", 409),
        ) if error is not None else None
        job._operation_task = None
        self._remove(job)
        self._trim_history()

    def _ensure_active(self, job: CatalogJob) -> None:
        with self._lock:
            if self._closed or job._cancel_requested or job._state == CatalogJobState.CANCELED:
                raise _CatalogCanceled()
        if self._safe_context() != job.context:
            raise _CatalogContextStale()

    def _submit(
        self,
        *,
        kind: str,
        scope_key: Tuple[str, Tuple[str, ...]],
        operation_factory: Callable[[Any, Callable[[], None]], Any],
    ) -> CatalogJob:
        with self._lock:
            if self._closed:
                raise CatalogJobError("runtime_stopped", "catalog jobs are stopped", status=409)
            if scope_key in self._active:
                code = "duplicate_catalog" if kind == "catalog" else "duplicate_preflight"
                raise CatalogJobError(code, "the same catalog request is already queued", status=409)
            if len(self._active) >= self._max_active_jobs:
                raise CatalogJobError("job_capacity", "catalog job queue is full", status=503)
            context = self._read_context()
            self._next_id += 1
            self._next_token[kind] += 1
            job = CatalogJob(
                self,
                f"catalog-{self._next_id}",
                kind,
                scope_key,
                context,
                self._next_token[kind],
            )
            job._state = CatalogJobState.QUEUED
            self._active[scope_key] = job
            self._jobs[job.job_id] = job
            self._trim_history()

        def operation(collection: Any) -> Any:
            with self._lock:
                if job._state == CatalogJobState.QUEUED:
                    job._state = CatalogJobState.STARTED
            self._ensure_active(job)
            value = operation_factory(collection, lambda: self._ensure_active(job))
            self._ensure_active(job)
            return value

        try:
            task = self._operation_adapter.submit(
                context,
                operation,
                on_complete=lambda result: self._on_operation_result(job, result),
            )
        except BaseException as exc:
            with self._lock:
                self._finish(job, CatalogJobState.FAILED, error=exc)
            raise
        with self._lock:
            if job._state not in _FINAL_STATES:
                job._operation_task = task
        return job

    def request_catalog(self) -> CatalogJob:
        return self._submit(
            kind="catalog",
            scope_key=("catalog", ()),
            operation_factory=lambda collection, cancel: build_deck_catalog(
                collection,
                self._adapter,
                max_decks=self._max_decks,
                cancel_check=cancel,
            ),
        )

    def request_preflight(self, *, deck_ids: Sequence[Any]) -> CatalogJob:
        selected_ids = _normalize_deck_ids(deck_ids, maximum=self._max_scope_ids)
        return self._submit(
            kind="preflight",
            scope_key=("preflight", selected_ids),
            operation_factory=lambda collection, cancel: build_deck_preflight(
                collection,
                self._adapter,
                deck_ids=selected_ids,
                snapshot_limit=self._snapshot_limit,
                max_decks=self._max_decks,
                max_scope_ids=self._max_scope_ids,
                cancel_check=cancel,
            ),
        )

    def _on_operation_result(self, job: CatalogJob, result: OperationResult) -> None:
        with self._lock:
            if job._state in _FINAL_STATES:
                return
            if job._cancel_requested or self._closed:
                self._finish(job, CatalogJobState.CANCELED)
                return
            if self._next_token[job.kind] != job.request_token:
                self._finish(job, CatalogJobState.STALE_RESULT_DROPPED, error=result.error)
                return
            if result.state is OperationState.STALE_RESULT_DROPPED or result.discarded:
                self._finish(job, CatalogJobState.STALE_RESULT_DROPPED, error=result.error)
                return
            if result.state is not OperationState.COMPLETED:
                self._finish(
                    job,
                    CatalogJobState.FAILED,
                    error=result.error or RuntimeError("catalog operation failed"),
                )
                return
            if self._safe_context() != job.context:
                self._finish(job, CatalogJobState.STALE_RESULT_DROPPED)
                return
            if job.kind == "catalog" and not isinstance(result.value, DeckCatalog):
                self._finish(job, CatalogJobState.FAILED, error=RuntimeError("invalid catalog result"))
                return
            if job.kind == "preflight" and not isinstance(result.value, DeckPreflight):
                self._finish(job, CatalogJobState.FAILED, error=RuntimeError("invalid preflight result"))
                return
            if job.kind == "catalog":
                self._last_catalog = result.value
            else:
                self._last_preflight = result.value
            self._finish(job, CatalogJobState.PUBLISHED, value=result.value)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job._state in _FINAL_STATES:
                return False
            job._cancel_requested = True
            task = job._operation_task
            if task is None:
                self._finish(job, CatalogJobState.CANCELED)
                return True
            task.cancel()
            if job._state is CatalogJobState.QUEUED:
                self._finish(job, CatalogJobState.CANCELED)
            return True

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._active.values())
            for job in jobs:
                job._cancel_requested = True
                task = job._operation_task
                if task is None:
                    self._finish(job, CatalogJobState.CANCELED)
                else:
                    task.cancel()
                    if job._state is CatalogJobState.QUEUED:
                        self._finish(job, CatalogJobState.CANCELED)

    def get(self, job_id: str) -> Optional[CatalogJob]:
        with self._lock:
            return self._jobs.get(job_id)


__all__ = [
    "CatalogJob",
    "CatalogJobError",
    "CatalogJobManager",
    "CatalogJobState",
    "DEFAULT_MAX_DECK_IDS",
    "DEFAULT_MAX_DECK_CATALOG_SIZE",
    "DEFAULT_SNAPSHOT_CARD_LIMIT",
    "DeckCatalog",
    "DeckCatalogEntry",
    "DeckCatalogError",
    "DeckPreflight",
    "build_deck_catalog",
    "build_deck_preflight",
    "catalog_from_collection",
    "make_preflight_scope",
    "preflight_from_collection",
]
