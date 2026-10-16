"""Anki 26.08.1 的隔离适配层；核心 RPC 不导入此模块。"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional


SUPPORTED_ANKI_VERSION = "26.8.1"
SUPPORTED_ANKI_BUILD = "39e4b0b4"
_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?\s*$")


class AnkiAdapterError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 503, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


def canonical_anki_version(value: str) -> str:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise AnkiAdapterError("invalid_anki_version", "Anki version is not numeric", status=422, retryable=False)
    major, minor, patch = (int(part) for part in match.groups(default="0"))
    return f"{major}.{minor}.{patch}"


def require_supported_anki_version(value: str) -> str:
    canonical = canonical_anki_version(value)
    if canonical != SUPPORTED_ANKI_VERSION:
        raise AnkiAdapterError(
            "unsupported_anki_version",
            f"only Anki {SUPPORTED_ANKI_VERSION} is enabled in M1",
            status=422,
            retryable=False,
        )
    return canonical


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int)):
        return enum_value
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return _scalar(value)


def _message_to_json(value: Any) -> Any:
    """尽力把 protobuf 风格的只读对象转为 JSON，不猜字段。"""

    list_fields = getattr(value, "ListFields", None)
    if callable(list_fields):
        result: dict[str, Any] = {}
        for descriptor, field_value in list_fields():
            key = getattr(descriptor, "json_name", None) or getattr(descriptor, "name", None)
            if not isinstance(key, str):
                continue
            if getattr(descriptor, "label", None) == getattr(descriptor, "LABEL_REPEATED", object()):
                result[key] = [_message_to_json(item) for item in field_value]
            elif hasattr(field_value, "ListFields"):
                result[key] = _message_to_json(field_value)
            else:
                result[key] = _scalar(field_value)
        return result
    return _json_safe(value)


def _optional_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except (AttributeError, TypeError):
        return None


def _due_value(card: Any) -> dict[str, Any]:
    due = _optional_attr(card, "due")
    queue = _scalar(_optional_attr(card, "queue"))
    try:
        queue_int = int(queue)
    except (TypeError, ValueError):
        queue_int = -1
    # 固定 Anki 版本的 queue 优先于 type：queue=3 是 DayLearn，暂停/埋藏
    # 的负 queue 也不能用 card.type 猜回原队列。
    if queue_int == 0:
        kind = "new_position"
    elif queue_int == 1:
        kind = "learning_timestamp_seconds"
    elif queue_int == 2:
        kind = "review_day"
    elif queue_int == 3:
        kind = "day_learning_day"
    elif queue_int == 4:
        kind = "unsupported_preview"
    elif queue_int in (-1, -2, -3):
        kind = "unknown_paused_or_buried_queue"
    else:
        kind = "unknown"
    return {"kind": kind, "value": _scalar(due)}


def _memory_state(card: Any, collection: Any) -> tuple[dict[str, Any] | None, list[str]]:
    limitations: list[str] = []
    result: dict[str, Any] = {}
    source = _optional_attr(card, "memory_state")
    if source is not None:
        for field in ("stability", "difficulty"):
            value = _optional_attr(source, field)
            if value is not None:
                result[field] = _scalar(value)
    for field in ("desired_retention", "decay"):
        value = _optional_attr(card, field)
        if value is not None:
            result[field] = _scalar(value)

    compute = getattr(collection, "compute_memory_state", None)
    if callable(compute):
        try:
            computed = compute(_optional_attr(card, "id"))
        except Exception:
            limitations.append("BLOCKED: Collection.compute_memory_state(card_id) failed for this card")
        else:
            if computed is not None:
                for field in ("state", "desired_retention", "stability", "difficulty", "decay"):
                    value = _optional_attr(computed, field)
                    if value is not None:
                        result[field] = _scalar(value)
                result["computed_by"] = "Collection.compute_memory_state"
    else:
        limitations.append("BLOCKED: Collection.compute_memory_state(card_id) is unavailable")

    if not result:
        limitations.append("BLOCKED: Card.memory_state/compute_memory_state exposed no FSRS fields")
        return None, limitations
    return result, limitations


class AnkiSnapshotExporter:
    """只读选定卡片；collection 只在 dispatcher 执行的闭包中取得。"""

    def __init__(
        self,
        collection_provider: Callable[[], Any],
        profile_provider: Callable[[], str],
        profile_principal: str,
        anki_version: str,
        anki_build: str = SUPPORTED_ANKI_BUILD,
        generation_provider: Optional[Callable[[], Any]] = None,
        runtime_epoch_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._collection_provider = collection_provider
        self._profile_provider = profile_provider
        self._profile_principal = profile_principal
        self._anki_version = anki_version
        self._anki_build = anki_build
        self._generation_provider = generation_provider
        self._runtime_epoch_provider = runtime_epoch_provider

    def export(self, card_ids: list[str]) -> dict[str, Any]:
        try:
            current_profile = self._profile_provider()
        except Exception as exc:
            raise AnkiAdapterError("profile_not_ready", "Anki profile identity is unavailable") from exc
        if current_profile != self._profile_principal:
            raise AnkiAdapterError("profile_changed", "Anki profile changed; rotate the bridge instance", status=409, retryable=False)

        collection = self._collection_provider()
        if collection is None:
            raise AnkiAdapterError("profile_not_ready", "Anki collection is not ready")
        cards: list[dict[str, Any]] = []
        limitations = [
            "BLOCKED: historical event application is not implemented; apply_events is disabled",
            "BLOCKED: Scheduler.answerCard/card answer_card APIs expose current scheduling, not a proven historical answered_at input",
            "BLOCKED: durable application watermark and transactionally coupled receipt are not available in M1",
        ]

        scheduler_version = _optional_attr(collection, "sched_ver")
        v3_enabled = None
        v3 = getattr(collection, "v3_scheduler", None)
        if callable(v3):
            try:
                v3_enabled = bool(v3())
            except Exception:
                limitations.append("BLOCKED: Collection.v3_scheduler() could not be read")

        get_card = getattr(collection, "get_card", None)
        if not callable(get_card):
            raise AnkiAdapterError("api_unavailable", "Collection.get_card(id) is unavailable", status=503, retryable=False)

        for raw_id in card_ids:
            try:
                card_id = int(raw_id)
                card = get_card(card_id)
            except Exception as exc:
                raise AnkiAdapterError("card_not_found", "requested card is unavailable", status=404, retryable=False) from exc

            memory, memory_limitations = _memory_state(card, collection)
            limitations.extend(memory_limitations)
            card_data: dict[str, Any] = {
                "card_id": str(_optional_attr(card, "id") or card_id),
                "note_id": str(_optional_attr(card, "nid") or _optional_attr(card, "note_id") or ""),
                "deck_id": str(_optional_attr(card, "did") or _optional_attr(card, "deck_id") or ""),
                "ordinal": _scalar(_optional_attr(card, "ord")),
                "type": _scalar(_optional_attr(card, "type")),
                "queue": _scalar(_optional_attr(card, "queue")),
                "due": _due_value(card),
                "interval_days": _scalar(_optional_attr(card, "ivl")),
                "factor": _scalar(_optional_attr(card, "factor")),
                "reps": _scalar(_optional_attr(card, "reps")),
                "lapses": _scalar(_optional_attr(card, "lapses")),
                "learning_steps_left": _scalar(_optional_attr(card, "left")),
                "last_review_time_seconds": _scalar(_optional_attr(card, "last_review_time")),
                "memory_state": memory,
                "schedule_revision": _scalar(_optional_attr(card, "mod")),
            }

            decks = _optional_attr(collection, "decks")
            config_reader = getattr(decks, "config_dict_for_deck_id", None)
            if callable(config_reader):
                try:
                    card_data["deck_config"] = _json_safe(config_reader(card_data["deck_id"]))
                except Exception:
                    limitations.append("BLOCKED: DeckManager.config_dict_for_deck_id(deck_id) failed")
            else:
                limitations.append("BLOCKED: DeckManager.config_dict_for_deck_id(deck_id) is unavailable")

            logs_reader = getattr(collection, "get_review_logs", None)
            if callable(logs_reader):
                try:
                    card_data["review_history"] = _message_to_json(logs_reader(card_id))
                except Exception:
                    limitations.append("BLOCKED: Collection.get_review_logs(card_id) could not be serialized")
            else:
                limitations.append("BLOCKED: Collection.get_review_logs(card_id) is unavailable")
            cards.append(card_data)

        return {
            "snapshot_schema": "fsrs-scheduling-snapshot-v1",
            "profile_principal": self._profile_principal,
            "anki_version": self._anki_version,
            "scheduler": {
                "anki_scheduler_version": _scalar(scheduler_version),
                "v3_enabled": v3_enabled,
                "source_calls": [
                    "Card.memory_state",
                    "Card.last_review_time",
                    "Collection.compute_memory_state(card_id)",
                    "Collection.get_review_logs(card_id)",
                    "DeckManager.config_dict_for_deck_id(deck_id)",
                ],
            },
            "scope": {"card_ids": list(card_ids)},
            "cards": cards,
            "complete": False,
            "limitations": sorted(set(limitations)),
        }

    def export_snapshot(
        self,
        card_ids: Optional[list[str]] = None,
        *,
        deck_ids: Optional[list[str]] = None,
    ) -> Any:
        """返回 P1-B typed snapshot；旧 ``export()`` dict façade 保持不变。"""

        from .anki.adapter_260801 import Anki260801Adapter

        adapter = Anki260801Adapter(
            collection_provider=self._collection_provider,
            profile_provider=self._profile_provider,
            profile_identity=self._profile_principal,
            anki_version=self._anki_version,
            anki_build=self._anki_build,
            generation_provider=self._generation_provider,
            runtime_epoch_provider=self._runtime_epoch_provider,
        )
        return adapter.export_snapshot(card_ids=card_ids, deck_ids=deck_ids)

    export_typed = export_snapshot
