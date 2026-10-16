"""Anki 只读快照的不可变 typed 模型。

模型只使用标准库和 JSON 可表达的标量；Anki ID 始终在模型边界转换为字符串。
缺失字段保持 ``None`` 或出现在 ``unsupported``，不填入伪造的默认调度值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .application_context import CapturedApplicationContext


Scalar = Union[None, bool, int, float, str]


class DueKind(str, Enum):
    """协议中彼此不可互换的 due 语义。"""

    NEW_POSITION = "new_position"
    LEARNING_TIMESTAMP = "learning_timestamp"
    REVIEW_DAY = "review_day"
    DAY_LEARNING_DAY = "day_learning_day"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class SkipReason(str, Enum):
    """文本首版过滤的可计数原因。"""

    MEDIA_REFERENCE = "media_reference"
    MEDIA_ONLY = "media_only"
    EMPTY_QUESTION = "empty_question"
    EMPTY_ANSWER = "empty_answer"
    UNKNOWN_TEMPLATE = "unknown_template"
    CARD_NOT_FOUND = "card_not_found"
    DECK_FILTERED = "deck_filtered"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze(item) for item in value))
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class MemoryState:
    """已学习卡的真实 FSRS memory；缺失时保持 None，不初始化成新卡。"""

    state: Optional[Union[str, int]] = None
    stability: Optional[float] = None
    difficulty: Optional[float] = None
    desired_retention: Optional[float] = None
    decay: Optional[float] = None
    source: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": _plain(self.state),
            "stability": self.stability,
            "difficulty": self.difficulty,
            "desired_retention": self.desired_retention,
            "decay": self.decay,
            "source": self.source,
        }


@dataclass(frozen=True)
class SchedulerDue:
    """带单位的调度 due；``value`` 不允许跨 kind 猜单位。"""

    kind: DueKind
    value: Optional[Union[int, str]] = None
    unit: Optional[str] = None
    raw_value: Optional[Scalar] = None
    unsupported: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "unsupported", tuple(self.unsupported))

    @property
    def new_position(self) -> Optional[str]:
        return str(self.value) if self.kind is DueKind.NEW_POSITION and self.value is not None else None

    @property
    def learning_timestamp_ms(self) -> Optional[int]:
        return int(self.value) if self.kind is DueKind.LEARNING_TIMESTAMP and self.value is not None else None

    @property
    def review_day(self) -> Optional[int]:
        return int(self.value) if self.kind is DueKind.REVIEW_DAY and self.value is not None else None

    @property
    def day_learning_day(self) -> Optional[int]:
        return int(self.value) if self.kind is DueKind.DAY_LEARNING_DAY and self.value is not None else None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "value": _plain(self.value),
            "unit": self.unit,
            "raw_value": _plain(self.raw_value),
            "unsupported": list(self.unsupported),
        }


@dataclass(frozen=True)
class SkipRecord:
    card_id: str
    reason: SkipReason
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_id", str(self.card_id))

    def to_dict(self) -> dict:
        return {"card_id": self.card_id, "reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True)
class PresetSnapshot:
    preset_id: str
    name: Optional[str] = None
    fsrs_params: Mapping[str, Any] = field(default_factory=dict)
    desired_retention: Optional[float] = None
    learning_steps: Tuple[Any, ...] = ()
    relearning_steps: Tuple[Any, ...] = ()
    new_limit: Optional[int] = None
    review_limit: Optional[int] = None
    unsupported: Tuple[str, ...] = ()
    max_interval_days: Optional[int] = None
    learning_steps_unit: Optional[str] = None

    def __post_init__(self) -> None:
        if self.max_interval_days is not None and (
            isinstance(self.max_interval_days, bool) or not isinstance(self.max_interval_days, int)
            or self.max_interval_days < 1
        ):
            raise ValueError('max_interval_days must be a positive integer')
        if self.learning_steps_unit not in (None, 'minutes', 'seconds'):
            raise ValueError('learning_steps_unit must be minutes or seconds')
        object.__setattr__(self, "preset_id", str(self.preset_id))
        object.__setattr__(self, "fsrs_params", _freeze(self.fsrs_params))
        object.__setattr__(self, "learning_steps", tuple(_freeze(item) for item in self.learning_steps))
        object.__setattr__(self, "relearning_steps", tuple(_freeze(item) for item in self.relearning_steps))
        object.__setattr__(self, "unsupported", tuple(self.unsupported))

    @property
    def id(self) -> str:
        return self.preset_id

    def to_dict(self) -> dict:
        result = {
            "preset_id": self.preset_id,
            "name": self.name,
            "fsrs_params": _plain(self.fsrs_params),
            "desired_retention": self.desired_retention,
            "learning_steps": _plain(self.learning_steps),
            "relearning_steps": _plain(self.relearning_steps),
            "new_limit": self.new_limit,
            "review_limit": self.review_limit,
            "unsupported": list(self.unsupported),
        }
        if self.max_interval_days is not None:
            result['max_interval_days'] = self.max_interval_days
        if self.learning_steps_unit is not None:
            result['learning_steps_unit'] = self.learning_steps_unit
        return result


@dataclass(frozen=True)
class DeckSnapshot:
    deck_id: str
    name: Optional[str] = None
    parent_id: Optional[str] = None
    preset_id: Optional[str] = None
    preset_name: Optional[str] = None
    new_limit: Optional[int] = None
    review_limit: Optional[int] = None
    learning_limit: Optional[int] = None
    new_today: Optional[int] = None
    review_today: Optional[int] = None
    learning_today: Optional[int] = None
    parent_limit_new: Optional[int] = None
    parent_limit_review: Optional[int] = None
    parent_limit_learning: Optional[int] = None
    unsupported: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "deck_id", str(self.deck_id))
        if self.parent_id is not None:
            object.__setattr__(self, "parent_id", str(self.parent_id))
        if self.preset_id is not None:
            object.__setattr__(self, "preset_id", str(self.preset_id))
        object.__setattr__(self, "unsupported", tuple(self.unsupported))

    @property
    def id(self) -> str:
        return self.deck_id

    def to_dict(self) -> dict:
        return {
            "deck_id": self.deck_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "new_limit": self.new_limit,
            "review_limit": self.review_limit,
            "learning_limit": self.learning_limit,
            "new_today": self.new_today,
            "review_today": self.review_today,
            "learning_today": self.learning_today,
            "parent_limit_new": self.parent_limit_new,
            "parent_limit_review": self.parent_limit_review,
            "parent_limit_learning": self.parent_limit_learning,
            "unsupported": list(self.unsupported),
        }


@dataclass(frozen=True)
class DayTable:
    review_day: Optional[int] = None
    day_learning_day: Optional[int] = None
    day_cutoff_ms: Optional[int] = None
    counts: Mapping[str, Optional[int]] = field(default_factory=dict)
    unsupported: Tuple[str, ...] = ()
    first_day_start_ms: Optional[int] = None
    future_cutoffs_ms: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", _freeze(self.counts))
        object.__setattr__(self, "future_cutoffs_ms", tuple(self.future_cutoffs_ms))

    @property
    def day_index(self) -> Optional[int]:
        return self.review_day

    @property
    def today(self) -> Optional[int]:
        return self.review_day

    def to_dict(self) -> dict:
        return {
            "review_day": self.review_day,
            "day_learning_day": self.day_learning_day,
            "day_cutoff_ms": self.day_cutoff_ms,
            "first_day_start_ms": self.first_day_start_ms,
            "future_cutoffs_ms": list(self.future_cutoffs_ms),
            "counts": _plain(self.counts),
            "unsupported": list(self.unsupported),
        }


@dataclass(frozen=True)
class CardSnapshot:
    card_id: str
    note_id: str
    deck_id: str
    ordinal: Optional[int]
    card_type: Optional[str]
    queue: Optional[int]
    due: SchedulerDue
    question: str
    answer: str
    interval_days: Optional[int] = None
    factor: Optional[int] = None
    reps: Optional[int] = None
    lapses: Optional[int] = None
    learning_steps_left: Optional[int] = None
    last_review_time_ms: Optional[int] = None
    memory_state: Optional[MemoryState] = None
    schedule_revision: Optional[Union[int, str]] = None
    unsupported: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("card_id", "note_id", "deck_id"):
            object.__setattr__(self, field_name, str(getattr(self, field_name)))
        object.__setattr__(self, "unsupported", tuple(self.unsupported))

    @property
    def type(self) -> Optional[str]:
        return self.card_type

    @property
    def supported(self) -> bool:
        return not self.unsupported

    def to_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "note_id": self.note_id,
            "deck_id": self.deck_id,
            "ordinal": self.ordinal,
            "type": self.card_type,
            "queue": self.queue,
            "due": self.due.to_dict(),
            "question": self.question,
            "answer": self.answer,
            "interval_days": self.interval_days,
            "factor": self.factor,
            "reps": self.reps,
            "lapses": self.lapses,
            "learning_steps_left": self.learning_steps_left,
            "last_review_time_ms": self.last_review_time_ms,
            "memory_state": self.memory_state.to_dict() if self.memory_state else None,
            "schedule_revision": (
                str(self.schedule_revision) if self.schedule_revision is not None else None
            ),
            "unsupported": list(self.unsupported),
            "supported": self.supported,
        }


@dataclass(frozen=True)
class SnapshotManifest:
    schema: str
    profile_identity: str
    profile_generation: Optional[Union[int, str]]
    runtime_epoch: Optional[Union[int, str]]
    anki_version: str
    anki_build: str
    scope_card_ids: Tuple[str, ...]
    scope_deck_ids: Tuple[str, ...]
    card_count: int
    included_card_count: int
    skipped: Tuple[SkipRecord, ...] = ()
    baseline_counts: Mapping[str, Optional[int]] = field(default_factory=dict)
    complete: bool = True
    unsupported: Tuple[str, ...] = ()
    capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_identity", str(self.profile_identity))
        object.__setattr__(self, "anki_version", str(self.anki_version))
        object.__setattr__(self, "anki_build", str(self.anki_build))
        object.__setattr__(self, "scope_card_ids", tuple(str(item) for item in self.scope_card_ids))
        object.__setattr__(self, "scope_deck_ids", tuple(str(item) for item in self.scope_deck_ids))
        object.__setattr__(self, "skipped", tuple(self.skipped))
        object.__setattr__(self, "baseline_counts", _freeze(self.baseline_counts))
        object.__setattr__(self, "unsupported", tuple(self.unsupported))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def skip_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self.skipped:
            key = item.reason.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def skip_reasons(self) -> Tuple[SkipRecord, ...]:
        return self.skipped

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "profile_identity": self.profile_identity,
            "profile_generation": _plain(self.profile_generation),
            "runtime_epoch": _plain(self.runtime_epoch),
            "anki_version": self.anki_version,
            "anki_build": self.anki_build,
            "scope": {
                "card_ids": list(self.scope_card_ids),
                "deck_ids": list(self.scope_deck_ids),
            },
            "card_count": self.card_count,
            "included_card_count": self.included_card_count,
            "skipped_count": self.skipped_count,
            "skipped": [item.to_dict() for item in self.skipped],
            "skip_counts": self.skip_counts,
            "baseline_counts": _plain(self.baseline_counts),
            "complete": self.complete,
            "unsupported": list(self.unsupported),
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class TypedSnapshot:
    manifest: SnapshotManifest
    decks: Tuple[DeckSnapshot, ...]
    presets: Tuple[PresetSnapshot, ...]
    day_table: DayTable
    cards: Tuple[CardSnapshot, ...]
    application_context: Optional[CapturedApplicationContext] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decks", tuple(self.decks))
        object.__setattr__(self, "presets", tuple(self.presets))
        object.__setattr__(self, "cards", tuple(self.cards))

    @property
    def complete(self) -> bool:
        return self.manifest.complete

    @property
    def unsupported(self) -> Tuple[str, ...]:
        return self.manifest.unsupported

    def to_dict(self) -> dict:
        return {
            "snapshot_schema": self.manifest.schema,
            "manifest": self.manifest.to_dict(),
            "decks": [item.to_dict() for item in self.decks],
            "presets": [item.to_dict() for item in self.presets],
            "day_table": self.day_table.to_dict(),
            "cards": [item.to_dict() for item in self.cards],
            "complete": self.complete,
            "unsupported": list(self.unsupported),
        }

    def as_dict(self) -> dict:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


AnkiSnapshot = TypedSnapshot
Snapshot = TypedSnapshot
SchedulerDueKind = DueKind
SkipReasonCode = SkipReason


__all__ = [
    "AnkiSnapshot",
    "CardSnapshot",
    "CapturedApplicationContext",
    "DayTable",
    "DeckSnapshot",
    "DueKind",
    "MemoryState",
    "PresetSnapshot",
    "SchedulerDue",
    "SchedulerDueKind",
    "SkipReason",
    "SkipReasonCode",
    "SkipRecord",
    "Snapshot",
    "SnapshotManifest",
    "TypedSnapshot",
]
