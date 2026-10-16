"""Anki 26.08.1/build 39e4b0b4 的只读快照适配器。

这里不导入 ``aqt`` 或 ``anki``。Anki 宿主在 QueryOp 中把 collection provider 注入；
普通 Python 测试只需提供同名的 fake 对象。适配器只读取，不执行 answer_card、
CollectionOp 写回或媒体下载。
"""

from __future__ import annotations

import math
import re
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..adapter import AnkiAdapterError
from .compatibility import (
    SUPPORTED_ANKI_BUILD,
    SUPPORTED_ANKI_VERSION,
    AnkiCompatibility,
    CompatibilityError,
    require_anki_compatibility,
)
from .application_context import capture_collection_context, capture_last_review_time_source
from .day_calendar import local_day_boundaries
from .models import (
    CardSnapshot,
    DayTable,
    DeckSnapshot,
    DueKind,
    MemoryState,
    PresetSnapshot,
    SchedulerDue,
    SkipReason,
    SkipRecord,
    SnapshotManifest,
    TypedSnapshot,
)


_MISSING = object()
_MEDIA_TOKEN_RE = re.compile(
    r"(?:\[sound:[^\]]+\]|\.(?:png|jpe?g|gif|webp|svg|bmp|mp3|wav|ogg|m4a|aac|flac|mp4|webm|mov)(?:[?#\s'\"]|$))",
    re.IGNORECASE,
)
_QUEUE_NAMES = {
    "new": 0,
    "learning": 1,
    "learn": 1,
    "review": 2,
    "daylearning": 3,
    "daylearn": 3,
    "preview": 4,
    "suspended": -1,
    "userburied": -2,
    "schedburied": -3,
    "buried": -2,
}
_TYPE_NAMES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}
_MEDIA_TAGS = {"img", "audio", "video", "source", "object", "embed"}
DEFAULT_MAX_SCOPE_CARD_IDS = 4096
DEFAULT_EXPORT_BATCH_SIZE = 128
DEFAULT_MAX_DECK_CATALOG_SIZE = 4096

# FSRS-6 默认权重。Anki 26.09 的空 fsrsParams6 表示采用默认参数；只有读取到
# collection 级 FSRS 开关为 true 时，才把该默认值作为可执行参数导出。
_ANKI_FSRS6_DEFAULT_PARAMETERS = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194,
    0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629,
    1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        return vars(value)
    except TypeError:
        return {}


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        try:
            return getattr(obj, name)
        except (AttributeError, TypeError):
            continue
    return default


def _first(obj: Any, *names: str) -> Any:
    for name in names:
        item = _value(obj, name, default=_MISSING)
        if item is not _MISSING and item is not None:
            return item
    return None


def _nested(obj: Any, *paths: str) -> Any:
    for path in paths:
        current = obj
        found = True
        for part in path.split("."):
            current = _value(current, part, default=_MISSING)
            if current is _MISSING or current is None:
                found = False
                break
        if found:
            return current
    return None


def _call_or_value(obj: Any, *names: str) -> Any:
    for name in names:
        item = _value(obj, name, default=_MISSING)
        if item is _MISSING:
            continue
        if callable(item):
            try:
                return item()
            except TypeError:
                continue
        return item
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _scheduler_today(collection: Any) -> Optional[int]:
    scheduler = _value(collection, "sched", "scheduler", default=None)
    today = _integer(_first(scheduler, "today", "today_day", "day"))
    if today is None:
        today = _integer(_first(collection, "today", "today_day"))
    return today


def _today_count(record: Mapping[str, Any], names: Sequence[str], today: Optional[int]) -> Optional[int]:
    raw = _first(record, *names)
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2 or today is None:
            return None
        day, amount = _integer(raw[0]), _integer(raw[1])
        if day is None or day < 0 or amount is None or amount < 0:
            return None
        return amount if day == today else 0
    return _integer(raw)


def _global_fsrs_enabled(manager: Any, deck_id: str) -> Optional[bool]:
    """Read Anki's collection-wide FSRS toggle through its deck-config API."""

    reader = _value(manager, "get_deck_configs_for_update", default=None)
    if not callable(reader):
        return None
    try:
        lookup_id: Any = int(deck_id)
    except (TypeError, ValueError):
        lookup_id = deck_id
    try:
        state = reader(lookup_id)
    except Exception:
        return None
    enabled = _value(state, "fsrs", default=_MISSING)
    return enabled if isinstance(enabled, bool) else None


def _enum_int(value: Any, names: Mapping[str, int]) -> Optional[int]:
    if value is None:
        return None
    number = _integer(value)
    if number is not None:
        return number
    enum_value = _value(value, "value", default=_MISSING)
    if enum_value is not _MISSING and enum_value is not value:
        number = _integer(enum_value)
        if number is not None:
            return number
    name = _value(value, "name", default=_MISSING)
    if name is _MISSING:
        name = value if isinstance(value, str) else None
    if isinstance(name, str):
        normalized = re.sub(r"[^a-z]", "", name.lower())
        return names.get(normalized)
    return None


def _safe_id(value: Any, fallback: Any = "") -> str:
    if value is None:
        value = fallback
    return str(value)


def _id_sort_key(value: str) -> Tuple[int, Any, str]:
    """对 Anki 数字 ID 和 fake host 字符串 ID 都提供稳定排序。"""

    if value.isdigit():
        return (0, int(value), value)
    return (1, value, value)


def _normalize_ids(values: Sequence[Any], name: str) -> List[str]:
    if isinstance(values, (str, bytes)):
        raise AnkiAdapterError("scope_invalid", f"{name} must be a non-empty sequence", status=422, retryable=False)
    result = set()
    for value in values:
        if isinstance(value, bool) or value is None:
            raise AnkiAdapterError("scope_invalid", f"{name} contains an invalid id", status=422, retryable=False)
        selected = str(value)
        if not selected or any(ord(char) < 0x20 or ord(char) == 0x7F for char in selected):
            raise AnkiAdapterError("scope_invalid", f"{name} contains an invalid id", status=422, retryable=False)
        result.add(selected)
    if not result:
        raise AnkiAdapterError("scope_required", f"{name} must not be empty", status=422, retryable=False)
    return sorted(result, key=_id_sort_key)


class _TextCleaner(HTMLParser):
    """只取 rendered HTML 的文字，不触碰媒体文件。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.media = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        normalized = tag.lower()
        if normalized in _MEDIA_TAGS:
            self.media = True
        if normalized in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in _MEDIA_TAGS:
            self.media = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _clean_text(raw: Optional[str]) -> Tuple[Optional[str], bool]:
    if raw is None:
        return None, False
    parser = _TextCleaner()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # HTML 解析失败不把原始标记当作可学习文本；调用方会记录未知模板。
        return None, False
    media = parser.media or bool(_MEDIA_TOKEN_RE.search(raw))
    text = " ".join("".join(parser.parts).split())
    return text, media


def _rendered(card: Any, side: str) -> Optional[str]:
    names = (
        ("rendered_question", "question", "front")
        if side == "question"
        else ("rendered_answer", "answer", "back")
    )
    for name in names:
        item = _value(card, name, default=_MISSING)
        if item is _MISSING:
            continue
        if callable(item):
            try:
                item = item()
            except TypeError:
                continue
        if item is None:
            return None
        return str(item)
    return None


def _queue_code(card: Any) -> Optional[int]:
    return _enum_int(_value(card, "queue", default=None), _QUEUE_NAMES)


def _type_code(card: Any) -> Optional[int]:
    return _enum_int(_value(card, "type", "card_type", default=None), _TYPE_NAMES)


def _type_name(card: Any) -> Optional[str]:
    code = _type_code(card)
    if code is None:
        return None
    return _TYPE_NAMES.get(code)


def _due(card: Any, queue: Optional[int]) -> Tuple[SchedulerDue, Tuple[str, ...]]:
    raw_due = _value(card, "due", default=None)
    if queue == 0:
        position = None if raw_due is None else str(raw_due)
        if position is None:
            reason = "new_position_missing"
            return SchedulerDue(DueKind.UNSUPPORTED, unsupported=(reason,)), (reason,)
        return SchedulerDue(DueKind.NEW_POSITION, position, "position", raw_due), ()
    if queue == 1:
        seconds = _number(raw_due)
        if seconds is None:
            reason = "learning_timestamp_seconds_invalid"
            return SchedulerDue(DueKind.UNSUPPORTED, raw_value=raw_due, unsupported=(reason,)), (reason,)
        millis = int(round(seconds * 1000))
        return SchedulerDue(DueKind.LEARNING_TIMESTAMP, millis, "unix_ms", raw_due), ()
    if queue == 2:
        day = _integer(raw_due)
        if day is None:
            reason = "review_day_invalid"
            return SchedulerDue(DueKind.UNSUPPORTED, raw_value=raw_due, unsupported=(reason,)), (reason,)
        return SchedulerDue(DueKind.REVIEW_DAY, day, "day", raw_due), ()
    if queue == 3:
        day = _integer(raw_due)
        if day is None:
            reason = "day_learning_day_invalid"
            return SchedulerDue(DueKind.UNSUPPORTED, raw_value=raw_due, unsupported=(reason,)), (reason,)
        return SchedulerDue(DueKind.DAY_LEARNING_DAY, day, "day", raw_due), ()
    if queue == 4:
        reason = "preview_queue_unsupported"
        return SchedulerDue(DueKind.UNSUPPORTED, raw_value=raw_due, unsupported=(reason,)), (reason,)
    if queue in (-1, -2, -3):
        reason = "paused_or_buried_original_queue_unknown"
        return SchedulerDue(DueKind.UNKNOWN, raw_value=raw_due, unsupported=(reason,)), (reason,)
    reason = "unknown_queue"
    return SchedulerDue(DueKind.UNKNOWN, raw_value=raw_due, unsupported=(reason,)), (reason,)


def _memory_state(card: Any, collection: Any) -> Tuple[Optional[MemoryState], Tuple[str, ...]]:
    source = _value(card, "memory_state", "memory", default=None)
    source_name = "card.memory_state" if source is not None else None
    if source is None and _type_code(card) == 0:
        # New 的空原生 memory 是权威前态，不从 compute_memory_state 补历史。
        return None, ()
    if source is None:
        compute = _value(collection, "compute_memory_state", default=None)
        if callable(compute):
            try:
                source = compute(_value(card, "id", "card_id", default=None))
                source_name = "Collection.compute_memory_state" if source is not None else None
            except Exception:
                source = None
    if source is None:
        return None, ("learned_memory_missing",)

    values = {
        "state": _value(source, "state", default=None),
        "stability": _value(source, "stability", default=None),
        "difficulty": _value(source, "difficulty", default=None),
        "desired_retention": _value(source, "desired_retention", "desiredRetention", default=None),
        "decay": _value(source, "decay", default=None),
    }
    for key in ("desired_retention", "decay"):
        if values[key] is None:
            values[key] = _value(card, key, "desiredRetention" if key == "desired_retention" else key, default=None)
    if not any(value is not None for value in values.values()):
        return None, ("learned_memory_missing",)
    return MemoryState(source=source_name, **values), ()


def _profile_marker(raw: Any) -> Tuple[Optional[str], Any, Any]:
    if raw is None:
        return None, None, None
    if isinstance(raw, str):
        return raw, None, None
    identity = _first(raw, "profile_identity", "profile_principal", "profile_id", "identity", "name")
    generation = _first(raw, "generation", "collection_generation", "profile_generation")
    epoch = _first(raw, "runtime_epoch", "epoch")
    return (str(identity) if identity is not None else None), generation, epoch


class Anki260801Adapter:
    """固定版本 collection 的只读 typed snapshot 导出器。"""

    def __init__(
        self,
        collection_provider: Callable[[], Any],
        profile_provider: Callable[[], Any],
        profile_identity: Optional[str] = None,
        anki_version: str = SUPPORTED_ANKI_VERSION,
        anki_build: str = SUPPORTED_ANKI_BUILD,
        generation_provider: Optional[Callable[[], Any]] = None,
        runtime_epoch_provider: Optional[Callable[[], Any]] = None,
        *,
        profile_principal: Optional[str] = None,
    ) -> None:
        self._collection_provider = collection_provider
        self._profile_provider = profile_provider
        selected_identity = profile_identity if profile_identity is not None else profile_principal
        if selected_identity is None:
            raise AnkiAdapterError("profile_not_ready", "profile_identity is required", status=422, retryable=False)
        self._profile_identity = str(selected_identity)
        self._generation_provider = generation_provider
        self._runtime_epoch_provider = runtime_epoch_provider
        try:
            self.compatibility: AnkiCompatibility = require_anki_compatibility(anki_version, anki_build)
        except CompatibilityError as exc:
            raise AnkiAdapterError(exc.code, exc.message, status=422, retryable=False) from exc

    def _marker(self) -> Tuple[str, Any, Any]:
        try:
            raw = self._profile_provider()
        except Exception as exc:
            raise AnkiAdapterError("profile_not_ready", "Anki profile identity is unavailable") from exc
        identity, generation, epoch = _profile_marker(raw)
        if identity is None:
            raise AnkiAdapterError("profile_not_ready", "Anki profile identity is unavailable")
        if self._generation_provider is not None:
            try:
                generation = self._generation_provider()
            except Exception as exc:
                raise AnkiAdapterError("profile_not_ready", "Anki collection generation is unavailable") from exc
        if self._runtime_epoch_provider is not None:
            try:
                epoch = self._runtime_epoch_provider()
            except Exception as exc:
                raise AnkiAdapterError("profile_not_ready", "Anki runtime epoch is unavailable") from exc
        return identity, generation, epoch

    def _validate_start(self) -> Tuple[Tuple[str, Any, Any], Any]:
        marker = self._marker()
        if marker[0] != self._profile_identity:
            raise AnkiAdapterError(
                "profile_changed",
                "Anki profile changed; rotate the bridge instance",
                status=409,
                retryable=False,
            )
        collection = self._collection_provider()
        if collection is None:
            raise AnkiAdapterError("profile_not_ready", "Anki collection is not ready")
        return marker, collection

    def _validate_end(self, before: Tuple[str, Any, Any]) -> None:
        after = self._marker()
        if after != before:
            raise AnkiAdapterError(
                "profile_changed",
                "Anki profile, collection generation, or runtime epoch changed during snapshot",
                status=409,
                retryable=False,
            )

    def read_deck_names_and_ids(
        self,
        collection: Any,
        *,
        max_decks: int = DEFAULT_MAX_DECK_CATALOG_SIZE,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> Tuple[Tuple[str, str], ...]:
        """读取固定版官方牌组目录所需的 ``(deck_id, name)``。

        Anki 26.08.1 的正式只读入口是
        ``DeckManager.all_names_and_ids(skip_empty_default=False,
        include_filtered=True)``。这里不访问旧的 ``decks`` 字典，也不
        访问 ``collection.db``；调用方必须把本方法放在 QueryOp 的
        ``op(collection)`` 闭包内。
        """

        if isinstance(max_decks, bool) or not isinstance(max_decks, int) or max_decks <= 0:
            raise ValueError("max_decks must be a positive integer")
        if collection is None:
            raise AnkiAdapterError("profile_not_ready", "Anki collection is not ready")
        manager = _value(collection, "decks", "deck_manager", default=None)
        reader = _value(manager, "all_names_and_ids", default=None)
        if not callable(reader):
            raise AnkiAdapterError(
                "api_unavailable",
                "DeckManager.all_names_and_ids() is required for deck catalog",
                status=503,
                retryable=False,
            )
        try:
            # 这是固定版 26.08.1 的关键字参数；不回退到已弃用的字典访问，
            # 也不猜测其他调用形状。
            raw_entries = reader(skip_empty_default=False, include_filtered=True)
        except Exception as exc:
            raise AnkiAdapterError(
                "catalog_read_failed",
                "Anki deck catalog could not be read",
                status=503,
            ) from exc
        if isinstance(raw_entries, (str, bytes)) or raw_entries is None:
            raise AnkiAdapterError(
                "catalog_invalid",
                "DeckManager.all_names_and_ids() returned invalid entries",
                status=422,
                retryable=False,
            )

        by_id: Dict[str, str] = {}
        by_name: Dict[str, str] = {}
        try:
            iterator = iter(raw_entries)
        except TypeError as exc:
            raise AnkiAdapterError(
                "catalog_invalid",
                "DeckManager.all_names_and_ids() returned a non-iterable",
                status=422,
                retryable=False,
            ) from exc
        for raw_entry in iterator:
            if cancel_check is not None:
                cancel_check()
            raw_id = _first(raw_entry, "id", "deck_id")
            raw_name = _first(raw_entry, "name", "deck_name")
            if isinstance(raw_id, bool) or raw_id is None:
                raise AnkiAdapterError(
                    "catalog_invalid",
                    "deck catalog contains an invalid ID",
                    status=422,
                    retryable=False,
                )
            try:
                if isinstance(raw_id, float) or isinstance(raw_id, bytes):
                    raise ValueError
                selected_id = str(int(str(raw_id).strip()))
            except (TypeError, ValueError):
                raise AnkiAdapterError(
                    "catalog_invalid",
                    "deck catalog contains a non-numeric ID",
                    status=422,
                    retryable=False,
                ) from None
            if int(selected_id) < 0:
                raise AnkiAdapterError(
                    "catalog_invalid",
                    "deck catalog contains a negative ID",
                    status=422,
                    retryable=False,
                )
            if not isinstance(raw_name, str) or not raw_name.strip() or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in raw_name
            ):
                raise AnkiAdapterError(
                    "deck_name_invalid",
                    "deck catalog contains an invalid name",
                    status=422,
                    retryable=False,
                )
            previous_name = by_id.get(selected_id)
            if previous_name is not None and previous_name != raw_name:
                raise AnkiAdapterError(
                    "catalog_duplicate_id",
                    "deck catalog contains one ID with multiple names",
                    status=422,
                    retryable=False,
                )
            previous_id = by_name.get(raw_name)
            if previous_id is not None and previous_id != selected_id:
                raise AnkiAdapterError(
                    "catalog_duplicate_name",
                    "deck catalog contains one name with multiple IDs",
                    status=422,
                    retryable=False,
                )
            if previous_name is None:
                if len(by_id) >= max_decks:
                    raise AnkiAdapterError(
                        "catalog_too_large",
                        "deck catalog exceeds the bounded deck limit",
                        status=413,
                        retryable=False,
                    )
                by_id[selected_id] = raw_name
                by_name[raw_name] = selected_id
        return tuple(sorted(by_id.items(), key=lambda item: _id_sort_key(item[0])))

    def device_preset_restrictions(self, collection: Any, deck_ids: Sequence[str]) -> Tuple[str, ...]:
        """预检直接读取选定范围的原始配置，拒绝设备未支持的短期状态机。"""
        reasons = set()
        if _value(collection, 'fsrs_short_term_with_steps_enabled', default=None) is True:
            reasons.add('fsrs_short_term_with_steps_device_unsupported')
        for record in self._deck_records(collection, deck_ids).values():
            config = _value(record, 'config', default=None)
            for path, code in (('new.delays', 'fsrs_empty_learning_steps_device_unsupported'),
                               ('lapse.delays', 'fsrs_empty_relearning_steps_device_unsupported')):
                steps = _nested(config, path)
                if isinstance(steps, (list, tuple)) and not steps:
                    reasons.add(code)
        return tuple(sorted(reasons))

    def count_deck_cards(self, collection: Any, deck_ids: Sequence[str]) -> int:
        """用固定版 ``DeckManager.card_count()`` 计算含子牌组的数量。

        ``include_subdecks=True`` 是 P4-C 的固定范围语义，与 Anki
        ``deck:"父牌组"`` 搜索一致；不得由 UI 请求静默改成只算当前牌组。
        """

        normalized = _normalize_ids(deck_ids, "deck_ids")
        try:
            numeric_ids = [int(value) for value in normalized]
        except (TypeError, ValueError):
            raise AnkiAdapterError(
                "scope_invalid",
                "deck_ids must contain numeric Anki deck IDs",
                status=422,
                retryable=False,
            ) from None
        manager = _value(collection, "decks", "deck_manager", default=None)
        counter = _value(manager, "card_count", default=None)
        if not callable(counter):
            raise AnkiAdapterError(
                "api_unavailable",
                "DeckManager.card_count() is required for deck preflight",
                status=503,
                retryable=False,
            )
        try:
            count = counter(numeric_ids, include_subdecks=True)
        except Exception as exc:
            raise AnkiAdapterError(
                "count_read_failed",
                "Anki deck card count could not be read",
                status=503,
            ) from exc
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AnkiAdapterError(
                "count_invalid",
                "DeckManager.card_count() returned an invalid count",
                status=422,
                retryable=False,
            )
        return count

    def resolve_deck_card_ids(
        self,
        collection: Any,
        deck_ids: Sequence[str],
        *,
        max_card_ids: int = DEFAULT_MAX_SCOPE_CARD_IDS,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> List[str]:
        """用固定版本官方 ``Collection.find_cards()`` 解析 deck scope。

        Anki 26.08.1 的 ``DeckManager.name(DeckId, default=False)`` 提供搜索名，
        ``Collection.find_cards(query, order=False)`` 返回卡 ID。这里不访问
        ``collection.db``，也不从 ``collection.cards`` 推断 scope。
        """

        if isinstance(max_card_ids, bool) or not isinstance(max_card_ids, int) or max_card_ids <= 0:
            raise ValueError("max_card_ids must be a positive integer")
        normalized_decks = _normalize_ids(deck_ids, "deck_ids")
        manager = _value(collection, "decks", "deck_manager", default=None)
        name_if_exists_reader = _value(manager, "name_if_exists", default=None)
        name_reader = _value(manager, "name", default=None)
        finder = _value(collection, "find_cards", default=None)
        if (not callable(name_if_exists_reader) and not callable(name_reader)) or not callable(finder):
            raise AnkiAdapterError(
                "api_unavailable",
                "Collection.find_cards() and DeckManager.name() are required for deck scope",
                status=503,
                retryable=False,
            )

        selected = set()
        for deck_id in normalized_decks:
            if cancel_check is not None:
                cancel_check()
            try:
                # The fixed Anki API exposes name_if_exists(DeckId). When an
                # isolated fake only exposes name(), use its documented
                # default=False form so a missing ID cannot select Default.
                if callable(name_if_exists_reader):
                    name = name_if_exists_reader(int(deck_id))
                else:
                    try:
                        name = name_reader(int(deck_id), default=False)
                    except TypeError:
                        name = name_reader(int(deck_id))
            except Exception as exc:
                raise AnkiAdapterError(
                    "deck_not_found",
                    "requested deck is unavailable",
                    status=404,
                    retryable=False,
                ) from exc
            if not isinstance(name, str) or not name:
                raise AnkiAdapterError(
                    "deck_not_found",
                    "requested deck is unavailable",
                    status=404,
                    retryable=False,
                )
            escaped = name.replace("\\", "\\\\").replace('"', '\\"')
            query = f'deck:"{escaped}"'
            try:
                # This is the official 26.08.1 search entry point and its
                # explicit order=False keeps the adapter's own ordering rule.
                matches = finder(query, order=False)
            except TypeError:
                matches = finder(query)
            if isinstance(matches, (str, bytes)):
                raise AnkiAdapterError("scope_invalid", "Collection.find_cards() returned invalid IDs", status=422, retryable=False)
            for raw_id in matches:
                if cancel_check is not None:
                    cancel_check()
                if isinstance(raw_id, bool) or raw_id is None:
                    raise AnkiAdapterError("scope_invalid", "Collection.find_cards() returned an invalid ID", status=422, retryable=False)
                selected_id = str(raw_id)
                if not selected_id or any(ord(char) < 0x20 or ord(char) == 0x7F for char in selected_id):
                    raise AnkiAdapterError("scope_invalid", "Collection.find_cards() returned an invalid ID", status=422, retryable=False)
                selected.add(selected_id)
                if len(selected) > max_card_ids:
                    raise AnkiAdapterError(
                        "scope_too_large",
                        "requested scope exceeds the bounded card limit",
                        status=413,
                        retryable=False,
                    )
        return sorted(selected, key=_id_sort_key)

    def _card_ids(
        self,
        collection: Any,
        card_ids: Optional[Sequence[str]],
        deck_ids: Optional[Sequence[str]] = None,
        *,
        max_card_ids: int = DEFAULT_MAX_SCOPE_CARD_IDS,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> List[str]:
        if card_ids is not None:
            result = _normalize_ids(card_ids, "card_ids")
            if len(result) > max_card_ids:
                raise AnkiAdapterError("scope_too_large", "requested scope exceeds the bounded card limit", status=413, retryable=False)
            return result
        if deck_ids is not None:
            return self.resolve_deck_card_ids(
                collection,
                deck_ids,
                max_card_ids=max_card_ids,
                cancel_check=cancel_check,
            )
        raise AnkiAdapterError("scope_required", "read-only snapshot requires explicit card_ids or deck_ids", status=422, retryable=False)

    def _get_card(self, collection: Any, card_id: str) -> Any:
        reader = _value(collection, "get_card", default=None)
        if not callable(reader):
            raise AnkiAdapterError("api_unavailable", "Collection.get_card(id) is unavailable", status=503, retryable=False)
        try:
            return reader(int(card_id))
        except (ValueError, TypeError):
            try:
                return reader(card_id)
            except Exception as exc:
                raise AnkiAdapterError("card_not_found", "requested card is unavailable", status=404, retryable=False) from exc
        except Exception as exc:
            raise AnkiAdapterError("card_not_found", "requested card is unavailable", status=404, retryable=False) from exc

    def _deck_record(self, manager: Any, deck_id: str) -> Mapping[str, Any]:
        record: Any = None
        if isinstance(manager, Mapping):
            record = manager.get(int(deck_id), manager.get(deck_id))
        else:
            for name in ("get", "get_deck"):
                reader = _value(manager, name, default=None)
                if callable(reader):
                    try:
                        record = reader(int(deck_id))
                        break
                    except Exception:
                        continue
        if record is None:
            record = {}
        result = dict(_mapping(record))
        result.setdefault("id", deck_id)
        config_reader = _value(manager, "config_dict_for_deck_id", "get_config", default=None)
        if callable(config_reader):
            try:
                config = config_reader(deck_id)
            except Exception:
                config = None
            if config is not None:
                result["config"] = config
        return result

    def _deck_records(self, collection: Any, deck_ids: Iterable[str]) -> Dict[str, Mapping[str, Any]]:
        manager = _value(collection, "decks", "deck_manager", default=None)
        records: Dict[str, Mapping[str, Any]] = {}
        for deck_id in deck_ids:
            records[str(deck_id)] = self._deck_record(manager, str(deck_id)) if manager is not None else {"id": deck_id}
        return records

    def _deck_and_presets(
        self, collection: Any, deck_ids: Iterable[str]
    ) -> Tuple[Tuple[DeckSnapshot, ...], Tuple[PresetSnapshot, ...], Tuple[str, ...], Dict[str, Optional[int]]]:
        manager = _value(collection, "decks", "deck_manager", default=None)
        records = self._deck_records(collection, deck_ids)
        decks: List[DeckSnapshot] = []
        presets: Dict[str, PresetSnapshot] = {}
        aggregate: Dict[str, Optional[int]] = {}
        today = _scheduler_today(collection)
        first_deck_id = next(iter(sorted(records)), None)
        fsrs_enabled = (
            _global_fsrs_enabled(manager, first_deck_id)
            if manager is not None and first_deck_id is not None
            else None
        )
        for deck_id in sorted(records, key=str):
            record = records[deck_id]
            config = _value(record, "config", default=None)
            if config is None:
                config = {}
            name = _first(record, "name", "deck_name") or _first(config, "name", "deck_name")
            parent = _first(record, "parent_id", "parentId", "parent")
            preset_id = _first(record, "preset_id", "presetId", "conf_id", "config_id")
            if preset_id is None:
                preset_id = _first(config, "preset_id", "presetId", "conf_id", "config_id")
            if preset_id is None and _first(config, "id") is not None:
                preset_id = _first(config, "id")
            preset_name = _first(record, "preset_name") or _first(config, "preset_name", "name")
            unsupported: List[str] = []
            if preset_id is None:
                unsupported.append("preset_identity_unavailable")
            if not config:
                unsupported.append("deck_preset_config_unavailable")

            new_limit = _integer(
                _first(record, "new_limit", "new_per_day")
                if _first(record, "new_limit", "new_per_day") is not None
                else _nested(config, "new.perDay", "new.per_day", "new_limit")
            )
            review_limit = _integer(
                _first(record, "review_limit", "review_per_day")
                if _first(record, "review_limit", "review_per_day") is not None
                else _nested(config, "rev.perDay", "review.per_day", "review_limit")
            )
            learning_limit_raw = _first(record, "learning_limit", "learn_per_day")
            if learning_limit_raw is None:
                learning_limit_raw = _nested(config, "learn.perDay", "learning.per_day", "learning_limit")
            learning_limit = _integer(learning_limit_raw)
            if new_limit is None:
                unsupported.append("new_limit_unavailable")
            if review_limit is None:
                unsupported.append("review_limit_unavailable")
            if learning_limit_raw is not None and learning_limit is None:
                unsupported.append("learning_limit_invalid")
            new_today = _today_count(record, ("newToday", "new_today", "new_count"), today)
            review_today = _today_count(record, ("revToday", "review_today", "review_count"), today)
            learning_today = _today_count(
                record, ("lrnToday", "learning_today", "learning_count", "learn_today"), today
            )
            if new_today is None:
                unsupported.append("new_today_count_unavailable")
            if review_today is None:
                unsupported.append("review_today_count_unavailable")
            if learning_today is None:
                unsupported.append("learning_today_count_unavailable")
            for key, value in (
                ("new", new_today),
                ("review", review_today),
                ("learning", learning_today),
            ):
                if value is not None:
                    aggregate[key] = (aggregate.get(key) or 0) + value

            parent_new = None
            parent_review = None
            parent_learning = None
            manager = _value(collection, "decks", "deck_manager", default=None)
            if parent is not None and manager is not None:
                parent_record = self._deck_record(manager, str(parent))
                parent_config = _value(parent_record, "config", default={}) or {}
                parent_new = _integer(_nested(parent_config, "new.perDay", "new.per_day", "new_limit"))
                parent_review = _integer(_nested(parent_config, "rev.perDay", "review.per_day", "review_limit"))
                parent_learning_raw = _nested(parent_config, "learn.perDay", "learning.per_day", "learning_limit")
                parent_learning = _integer(parent_learning_raw)
                if parent_new is None:
                    unsupported.append("parent_new_limit_unavailable")
                if parent_review is None:
                    unsupported.append("parent_review_limit_unavailable")
                if parent_learning_raw is not None and parent_learning is None:
                    unsupported.append("parent_learning_limit_invalid")

            deck = DeckSnapshot(
                deck_id=deck_id,
                name=str(name) if name is not None else None,
                parent_id=str(parent) if parent is not None else None,
                preset_id=str(preset_id) if preset_id is not None else None,
                preset_name=str(preset_name) if preset_name is not None else None,
                new_limit=new_limit,
                review_limit=review_limit,
                learning_limit=learning_limit,
                new_today=new_today,
                review_today=review_today,
                learning_today=learning_today,
                parent_limit_new=parent_new,
                parent_limit_review=parent_review,
                parent_limit_learning=parent_learning,
                unsupported=tuple(sorted(set(unsupported))),
            )
            decks.append(deck)

            if preset_id is not None and str(preset_id) not in presets:
                raw_params6 = _first(config, "fsrsParams6", "fsrs_params_6")
                fsrs_params: Dict[str, Any] = {}
                fsrs = _first(config, "fsrs", "fsrs_params")
                if not isinstance(fsrs, Mapping):
                    fsrs = {}
                desired = _first(fsrs, "desiredRetention", "desired_retention")
                if desired is None:
                    desired = _first(config, "desiredRetention", "desired_retention")
                learning_steps = _nested(config, "new.delays", "new.steps")
                relearning_steps = _nested(config, "lapse.delays", "lapse.steps")
                preset_unsupported = []
                if _value(collection, 'fsrs_short_term_with_steps_enabled', default=None) is True:
                    preset_unsupported.append('fsrs_short_term_with_steps_device_unsupported')
                if isinstance(learning_steps, (list, tuple)) and not learning_steps:
                    preset_unsupported.append('fsrs_empty_learning_steps_device_unsupported')
                if isinstance(relearning_steps, (list, tuple)) and not relearning_steps:
                    preset_unsupported.append('fsrs_empty_relearning_steps_device_unsupported')
                maximum_interval = _nested(config, 'rev.maxIvl')
                if maximum_interval is not None and (
                    isinstance(maximum_interval, bool) or not isinstance(maximum_interval, int)
                    or maximum_interval < 1
                ):
                    preset_unsupported.append('maximum_interval_invalid')
                    maximum_interval = None
                if fsrs_enabled is False:
                    preset_unsupported.append("fsrs_disabled")
                elif fsrs_enabled is None:
                    preset_unsupported.append("fsrs_status_unavailable")
                elif raw_params6 is not None:
                    if isinstance(raw_params6, (list, tuple)):
                        if not raw_params6:
                            fsrs_params = {
                                "version": 6,
                                "params": list(_ANKI_FSRS6_DEFAULT_PARAMETERS),
                            }
                        else:
                            params = [_number(item) for item in raw_params6]
                            if len(params) in (17, 19, 21) and all(item is not None for item in params):
                                fsrs_params = {"version": 6, "params": params}
                            else:
                                preset_unsupported.append("fsrs_parameters_invalid")
                    else:
                        preset_unsupported.append("fsrs_parameters_invalid")
                else:
                    fsrs_params = dict(fsrs)
                    if _first(fsrs, "params") is None and _first(config, "params", "fsrs_params") is not None:
                        fsrs_params["params"] = _first(config, "params", "fsrs_params")
                    if not fsrs_params:
                        preset_unsupported.append("fsrs_parameters_unavailable")
                if desired is None:
                    preset_unsupported.append("desired_retention_unavailable")
                if learning_steps is None:
                    preset_unsupported.append("learning_steps_unavailable")
                    learning_steps = ()
                if relearning_steps is None:
                    preset_unsupported.append("relearning_steps_unavailable")
                    relearning_steps = ()
                presets[str(preset_id)] = PresetSnapshot(
                    preset_id=str(preset_id),
                    name=str(preset_name) if preset_name is not None else None,
                    fsrs_params=fsrs_params,
                    desired_retention=_number(desired),
                    learning_steps=tuple(learning_steps) if not isinstance(learning_steps, str) else (learning_steps,),
                    relearning_steps=tuple(relearning_steps) if not isinstance(relearning_steps, str) else (relearning_steps,),
                    new_limit=new_limit,
                    review_limit=review_limit,
                    unsupported=tuple(preset_unsupported),
                    max_interval_days=maximum_interval,
                    learning_steps_unit='minutes',
                )
        unsupported = tuple(
            sorted(
                {item for deck in decks for item in deck.unsupported}
                | {item for preset in presets.values() for item in preset.unsupported}
            )
        )
        return tuple(decks), tuple(presets.values()), unsupported, aggregate

    def _day_table(self, collection: Any, counts: Mapping[str, Optional[int]]) -> DayTable:
        scheduler = _value(collection, "sched", "scheduler", default=None)
        today = _integer(_first(scheduler, "today", "today_day", "day"))
        if today is None:
            today = _integer(_first(collection, "today", "today_day"))
        day_learning = _integer(_first(scheduler, "day_learning_day", "dayLearnToday"))
        if day_learning is None:
            day_learning = today
        cutoff = _number(_first(scheduler, "day_cutoff", "day_cutoff_seconds", "dayCutoff"))
        cutoff_ms = int(round(cutoff * 1000)) if cutoff is not None and cutoff < 10**12 else _integer(cutoff)
        day_boundaries = local_day_boundaries(cutoff_ms) if cutoff_ms is not None else None
        unsupported: List[str] = []
        if today is None:
            unsupported.append("review_day_unavailable")
        if day_learning is None:
            unsupported.append("day_learning_day_unavailable")
        if day_boundaries is None:
            unsupported.append("future_day_table_unavailable")
        return DayTable(
            review_day=today,
            day_learning_day=day_learning,
            day_cutoff_ms=cutoff_ms,
            first_day_start_ms=day_boundaries[0] if day_boundaries is not None else None,
            future_cutoffs_ms=day_boundaries[1] if day_boundaries is not None else (),
            counts=dict(counts),
            unsupported=tuple(unsupported),
        )

    def _card_snapshot(self, card: Any, collection: Any) -> Tuple[Optional[CardSnapshot], Tuple[str, ...]]:
        card_id = _safe_id(_first(card, "id", "card_id"))
        question_raw = _rendered(card, "question")
        answer_raw = _rendered(card, "answer")
        question, question_media = _clean_text(question_raw)
        answer, answer_media = _clean_text(answer_raw)
        if question_raw is None or answer_raw is None:
            return None, (SkipReason.UNKNOWN_TEMPLATE.value,)
        if question_media or answer_media:
            if not question or not answer:
                return None, (SkipReason.MEDIA_ONLY.value,)
            return None, (SkipReason.MEDIA_REFERENCE.value,)
        if not question:
            return None, (SkipReason.EMPTY_QUESTION.value,)
        if not answer:
            return None, (SkipReason.EMPTY_ANSWER.value,)

        queue = _queue_code(card)
        due, due_unsupported = _due(card, queue)
        memory, memory_unsupported = _memory_state(card, collection)
        type_name = _type_name(card)
        unsupported = list(due_unsupported)
        if memory is None and (queue in (1, 2, 3) or _type_code(card) in (1, 2, 3)):
            unsupported.extend(memory_unsupported)
        if queue is None:
            unsupported.append("queue_missing")
        note_id = _safe_id(_first(card, "nid", "note_id"))
        deck_id = _safe_id(_first(card, "did", "deck_id"))
        last_review = _first(card, "last_review_time_ms")
        if last_review is None:
            seconds = _number(_first(card, "last_review_time", "last_review"))
            last_review = int(round(seconds * 1000)) if seconds is not None else None
        if last_review is None and type_name != "new":
            try:
                seconds = capture_last_review_time_source(collection, card)["resolved_seconds"]
                last_review = seconds * 1000 if seconds is not None else None
            except (ValueError, TypeError, AttributeError):
                unsupported.append("scheduler_last_review_time_source_unavailable")
            if last_review is None:
                unsupported.append("scheduler_last_review_time_missing")
        snapshot = CardSnapshot(
            card_id=card_id,
            note_id=note_id,
            deck_id=deck_id,
            ordinal=_integer(_first(card, "ord", "ordinal")),
            card_type=type_name,
            queue=queue,
            due=due,
            question=question,
            answer=answer,
            interval_days=_integer(_first(card, "ivl", "interval_days")),
            factor=_integer(_first(card, "factor")),
            reps=_integer(_first(card, "reps")),
            lapses=_integer(_first(card, "lapses")),
            learning_steps_left=_integer(_first(card, "left", "learning_steps_left")),
            last_review_time_ms=last_review,
            memory_state=memory,
            schedule_revision=_first(card, "mod", "schedule_revision"),
            unsupported=tuple(sorted(set(unsupported))),
        )
        return snapshot, ()

    def export_snapshot_from_collection(
        self,
        collection: Any,
        *,
        card_ids: Optional[Sequence[str]] = None,
        deck_ids: Optional[Sequence[str]] = None,
        context: Any = None,
        max_card_ids: int = DEFAULT_MAX_SCOPE_CARD_IDS,
        batch_size: int = DEFAULT_EXPORT_BATCH_SIZE,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TypedSnapshot:
        """在官方 QueryOp 提供的 collection 上执行一次有界只读导出。

        ``collection`` 必须由 QueryOp 的 ``op(collection)`` 参数提供；本方法
        不调用 ``_collection_provider``。生产 QueryOp 必须传入
        ``OperationContext``；只有该入口会捕获本次 Collection 的旁路评分上下文。
        因此 HTTP/runtime 线程只能传递 scope 和上下文，不能持有 collection。
        """

        if collection is None:
            raise AnkiAdapterError("profile_not_ready", "Anki collection is not ready")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if isinstance(max_card_ids, bool) or not isinstance(max_card_ids, int) or max_card_ids <= 0:
            raise ValueError("max_card_ids must be a positive integer")
        if context is None:
            before = self._marker()
        elif isinstance(context, tuple) and len(context) == 3:
            before = (str(context[0]), context[1], context[2])
        else:
            identity, generation, epoch = _profile_marker(context)
            if identity is None:
                raise AnkiAdapterError("profile_not_ready", "snapshot context is unavailable")
            before = (identity, generation, epoch)
        if before[0] != self._profile_identity:
            raise AnkiAdapterError(
                "profile_changed",
                "Anki profile changed; rotate the bridge instance",
                status=409,
                retryable=False,
            )
        context_generation = _integer(before[1])
        if context is not None and (
            context_generation is None
            or context_generation < 0
            or before[2] is None
        ):
            raise AnkiAdapterError(
                "profile_not_ready",
                "snapshot context must include collection generation and runtime epoch",
                status=409,
                retryable=False,
            )

        if cancel_check is not None:
            cancel_check()
        selected_ids = self._card_ids(
            collection,
            card_ids,
            deck_ids,
            max_card_ids=max_card_ids,
            cancel_check=cancel_check,
        )
        # 牌组搜索已包含子牌组；仅在调用方显式给出 card_ids 时按 deck_ids
        # 交集过滤，不能把搜索所得的子牌组卡再按父牌组 ID 丢弃。
        selected_decks = (
            set(_normalize_ids(deck_ids, "deck_ids"))
            if deck_ids is not None and card_ids is not None
            else None
        )
        cards: List[CardSnapshot] = []
        skipped: List[SkipRecord] = []
        raw_cards: List[Any] = []
        # The hard scope cap plus fixed batches bound the amount of work in a
        # QueryOp. No partial list is published if cancellation/failure occurs.
        for offset in range(0, len(selected_ids), batch_size):
            if cancel_check is not None:
                cancel_check()
            for card_id in selected_ids[offset : offset + batch_size]:
                if cancel_check is not None:
                    cancel_check()
                try:
                    card = self._get_card(collection, card_id)
                except AnkiAdapterError:
                    skipped.append(SkipRecord(card_id, SkipReason.CARD_NOT_FOUND))
                    continue
                raw_cards.append(card)
                actual_deck = _safe_id(_first(card, "did", "deck_id"))
                if selected_decks is not None and actual_deck not in selected_decks:
                    skipped.append(SkipRecord(card_id, SkipReason.DECK_FILTERED))
                    continue
                snapshot, reasons = self._card_snapshot(collection=collection, card=card)
                if snapshot is None:
                    reason = SkipReason(reasons[0]) if reasons and reasons[0] in SkipReason._value2member_map_ else SkipReason.UNKNOWN_TEMPLATE
                    skipped.append(SkipRecord(card_id, reason))
                else:
                    cards.append(snapshot)

        deck_ids_seen = {str(_first(card, "did", "deck_id")) for card in raw_cards if _first(card, "did", "deck_id") is not None}
        if selected_decks is not None:
            deck_ids_seen.update(selected_decks)
        captured_context = None
        if context is not None:
            native_by_id = {str(_first(card, "id", "card_id")): card for card in raw_cards}
            last_review_card_ids = tuple(
                item.card_id for item in cards
                if item.card_type != "new" and item.last_review_time_ms is not None
                and _first(native_by_id[item.card_id], "last_review_time_ms", "last_review_time", "last_review") is None
            )
            captured_context = capture_collection_context(
                collection,
                profile_identity=before[0],
                collection_generation=context_generation,
                runtime_epoch=str(before[2]) if before[2] is not None else None,
                anki_version=self.compatibility.version or SUPPORTED_ANKI_VERSION,
                anki_build=self.compatibility.build or SUPPORTED_ANKI_BUILD,
                deck_ids=tuple(deck_ids_seen),
                last_review_card_ids=last_review_card_ids,
            )
            for item in cards:
                source = captured_context.card_last_review_time_sources.get(item.card_id)
                if source is not None and source["resolved_seconds"] * 1000 != item.last_review_time_ms:
                    raise AnkiAdapterError("snapshot_last_review_source_changed", "复习时间来源在导出中发生变化", status=409, retryable=False)
        decks, presets, deck_unsupported, baseline_counts = self._deck_and_presets(collection, deck_ids_seen)
        day_table = self._day_table(collection, baseline_counts)
        unsupported = set(deck_unsupported) | set(day_table.unsupported)
        unsupported.update(item for card in cards for item in card.unsupported)
        manifest = SnapshotManifest(
            schema="inkcards-anki-snapshot-v2",
            profile_identity=before[0],
            profile_generation=before[1],
            runtime_epoch=before[2],
            anki_version=self.compatibility.version or SUPPORTED_ANKI_VERSION,
            anki_build=self.compatibility.build or SUPPORTED_ANKI_BUILD,
            scope_card_ids=tuple(selected_ids),
            scope_deck_ids=tuple(sorted(deck_ids_seen)),
            card_count=len(selected_ids),
            included_card_count=len(cards),
            skipped=tuple(skipped),
            baseline_counts=baseline_counts,
            complete=True,
            unsupported=tuple(sorted(unsupported)),
            capabilities=self.compatibility.capabilities,
        )
        if captured_context is not None:
            post_context = capture_collection_context(
                collection,
                profile_identity=before[0],
                collection_generation=context_generation,
                runtime_epoch=str(before[2]) if before[2] is not None else None,
                anki_version=self.compatibility.version or SUPPORTED_ANKI_VERSION,
                anki_build=self.compatibility.build or SUPPORTED_ANKI_BUILD,
                deck_ids=tuple(deck_ids_seen),
                last_review_card_ids=last_review_card_ids,
            )
            if captured_context.source_sha256 != post_context.source_sha256:
                raise AnkiAdapterError(
                    "snapshot_context_changed",
                    "Anki scoring configuration changed during snapshot export",
                    status=409,
                    retryable=False,
                )
        self._validate_end(before)
        return TypedSnapshot(
            manifest,
            decks,
            presets,
            day_table,
            tuple(cards),
            application_context=captured_context,
        )

    def export_snapshot(
        self,
        card_ids: Optional[Sequence[str]] = None,
        deck_ids: Optional[Sequence[str]] = None,
    ) -> TypedSnapshot:
        """兼容旧同步调用；没有 OperationContext，因此不捕获评分旁路上下文。

        新宿主路径必须通过 QueryOp 调用 ``export_snapshot_from_collection``。
        """

        _before, collection = self._validate_start()
        return self.export_snapshot_from_collection(
            collection,
            card_ids=card_ids,
            deck_ids=deck_ids,
        )

    # 直观别名，便于宿主把 operation 的返回值命名为 export。
    export = export_snapshot
    export_typed = export_snapshot


Anki260801ReadOnlyAdapter = Anki260801Adapter
ReadOnlyAnkiAdapter = Anki260801Adapter


__all__ = [
    "Anki260801Adapter",
    "Anki260801ReadOnlyAdapter",
    "DEFAULT_EXPORT_BATCH_SIZE",
    "DEFAULT_MAX_DECK_CATALOG_SIZE",
    "DEFAULT_MAX_SCOPE_CARD_IDS",
    "ReadOnlyAnkiAdapter",
]
