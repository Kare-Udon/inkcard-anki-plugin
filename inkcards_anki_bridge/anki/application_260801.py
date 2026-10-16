"""Anki 26.08.1 历史 review 应用的 fail-closed 适配层。

这个模块只使用标准库，``anki``/``aqt`` 只在真正准备候选
``CardAnswer`` 的函数中延迟导入。固定 bundle 虽然能把
``answered_at_millis`` 写进 revlog，但现有证据不能证明它能按历史的
learning/day/time context 重建 scheduler。因此生产 capability 永远不会因为
“字段存在”而自动打开；必须由更强的固定版本证据替换本模块的硬门槛后才有
机会启用写路径。

本模块禁止 SQL 写入。所有 candidate write 都只经过官方
``Scheduler.answer_card(CardAnswer)``，而默认服务路径在 capability disabled
时不会调用 collection，也不会创建 apply intent。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .compatibility import (
    SUPPORTED_ANKI_BUILD,
    SUPPORTED_ANKI_VERSION,
    AnkiCompatibility,
    check_anki_compatibility,
)


MAX_SAFE_INTEGER = 9007199254740991
MAX_DURATION_MS = 0xFFFFFFFF
RATING_TO_ANKI: Dict[int, int] = {1: 0, 2: 1, 3: 2, 4: 3}
RATING_FIELD: Dict[int, str] = {
    0: "again",
    1: "hard",
    2: "good",
    3: "easy",
}
SCHEDULER_STATES = frozenset(("new", "learning", "review", "relearning"))
_CARD_FIELDS = (
    "id",
    "nid",
    "did",
    "type",
    "queue",
    "due",
    "ivl",
    "factor",
    "reps",
    "lapses",
    "left",
    "last_review_time",
    "last_review",
    "desired_retention",
    "decay",
    "custom_data",
)


class ApplicationError(Exception):
    """应用层可分类错误，不把 collection/path/payload 写进用户消息。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "unknown",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.category = str(category)
        self.retryable = bool(retryable)


class ApplicationConflictError(ApplicationError):
    """应用前置条件或应用后语义核验冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, category="conflict", retryable=False)


class ApplicationBlockedError(ApplicationError):
    """能力未开启或固定版本/API 不满足安全门槛。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, category="blocked", retryable=False)


ApplicationValidationError = ApplicationConflictError


@dataclass(frozen=True)
class CapabilityReport:
    """一次不可变的应用能力报告。

    ``exact`` 只表示版本/build 命中 allowlist，不表示历史调度已经安全。
    ``write_enabled`` 是更严格的产品门槛，目前固定为 False。
    """

    anki_version: Optional[str]
    anki_build: Optional[str]
    exact: bool
    write_enabled: bool
    disabled_reasons: Tuple[str, ...] = ()
    official_api: Tuple[str, ...] = ()
    historical_scheduler: bool = False
    external_ledger_transaction: bool = False

    @property
    def supported(self) -> bool:
        return self.exact

    @property
    def reason(self) -> Optional[str]:
        return self.disabled_reasons[0] if self.disabled_reasons else None

    @property
    def reasons(self) -> Tuple[str, ...]:
        return self.disabled_reasons

    @property
    def write_capability(self) -> bool:
        return self.write_enabled

    def require_write(self) -> None:
        if not self.write_enabled:
            raise ApplicationBlockedError(
                "history_application_disabled",
                "historical Anki scheduler application is disabled",
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anki_version": self.anki_version,
            "anki_build": self.anki_build,
            "exact": self.exact,
            "write_enabled": self.write_enabled,
            "historical_scheduler": self.historical_scheduler,
            "external_ledger_transaction": self.external_ledger_transaction,
            "official_api": list(self.official_api),
            "disabled_reasons": list(self.disabled_reasons),
        }


def _dedupe(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value))


def capability_report(
    anki_version: str,
    anki_build: Optional[str],
    *,
    collection: Any = None,
    scheduler: Any = None,
) -> CapabilityReport:
    """返回固定版本能力报告，并保持历史写回关闭。

    即使版本/build 精确匹配，现有固定 bundle 探针仍观察到 learning due
    依赖当前 scheduler clock，且 Anki 没有与插件 ledger 的公共原子事务；
    这两个原因是硬阻断，不能由调用方通过参数绕过。
    """

    compatibility: AnkiCompatibility = check_anki_compatibility(
        anki_version, anki_build
    )
    reasons: List[str] = list(compatibility.unsupported)
    official: List[str] = []
    if collection is not None:
        if callable(getattr(collection, "get_card", None)):
            official.append("Collection.get_card")
        scheduler_value = scheduler
        if scheduler_value is None:
            scheduler_value = getattr(collection, "sched", None)
        if callable(getattr(scheduler_value, "answer_card", None)):
            official.append("Scheduler.answer_card")
        backend = getattr(collection, "_backend", None)
        if callable(getattr(backend, "get_scheduling_states", None)):
            official.append("Collection._backend.get_scheduling_states")
        if callable(getattr(collection, "get_review_logs", None)):
            official.append("Collection.get_review_logs")

    if compatibility.supported:
        reasons.extend(
            (
                "historical_scheduler",
                "external_ledger_transaction",
            )
        )
    else:
        reasons.append("fixed_anki_version_or_build_required")
    missing_api = []
    if collection is not None:
        if "Collection.get_card" not in official:
            missing_api.append("collection_get_card_unavailable")
        if "Scheduler.answer_card" not in official:
            missing_api.append("scheduler_answer_card_unavailable")
        if "Collection._backend.get_scheduling_states" not in official:
            missing_api.append("scheduling_states_unavailable")
    reasons.extend(missing_api)
    return CapabilityReport(
        anki_version=compatibility.version,
        anki_build=compatibility.build,
        exact=compatibility.supported,
        write_enabled=False,
        disabled_reasons=_dedupe(reasons),
        official_api=_dedupe(official),
        historical_scheduler=False,
        external_ledger_transaction=False,
    )


def map_rating(rating_1_to_4: Any) -> int:
    """显式把 X4 业务 rating 1..4 转成 Anki enum 0..3。"""

    if (
        isinstance(rating_1_to_4, bool)
        or not isinstance(rating_1_to_4, int)
        or rating_1_to_4 not in RATING_TO_ANKI
    ):
        raise ApplicationConflictError("invalid_rating", "rating must be an integer in 1..4")
    return RATING_TO_ANKI[rating_1_to_4]


def _plain(value: Any) -> Any:
    """把 protobuf/fake 对象变成不含方法的有限 JSON 值。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    try:
        from google.protobuf.json_format import MessageToDict

        if hasattr(value, "DESCRIPTOR"):
            return MessageToDict(value, preserving_proto_field_name=True)
    except Exception:
        pass
    fields = getattr(value, "ListFields", None)
    if callable(fields):
        result: Dict[str, Any] = {}
        try:
            for descriptor, field_value in fields():
                name = getattr(descriptor, "name", None)
                if name:
                    result[str(name)] = _plain(field_value)
            return result
        except Exception:
            pass
    try:
        return {str(key): _plain(item) for key, item in vars(value).items()}
    except TypeError:
        return str(value)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ApplicationConflictError("invalid_json_value", "application value is not strict JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
        raise ApplicationConflictError(code, "application text field is invalid")
    return value


def _safe_integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApplicationConflictError(code, "application integer field is invalid")
    if value < minimum or value > MAX_SAFE_INTEGER:
        raise ApplicationConflictError(code, "application integer field is out of range")
    return value


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApplicationConflictError(code, "application state must be an object")
    return value


def _state(value: Any, card_id: str, name: str) -> Dict[str, Any]:
    source = dict(_mapping(value, "%s_missing" % name))
    state_card_id = source.get("card_id")
    if not isinstance(state_card_id, str) or state_card_id != card_id:
        raise ApplicationConflictError("%s_card_mismatch" % name, "event state card identity differs")
    state_name = source.get("state")
    if state_name not in SCHEDULER_STATES:
        raise ApplicationConflictError("%s_state_invalid" % name, "event scheduler state is unsupported")
    _text(source.get("schedule_revision"), "%s_revision_invalid" % name)
    _safe_integer(source.get("step"), "%s_step_invalid" % name)
    if "short_interval" not in source or not isinstance(source["short_interval"], bool):
        raise ApplicationConflictError("%s_short_interval_invalid" % name, "event short interval flag is invalid")
    if "memory" not in source:
        raise ApplicationConflictError("fsrs_memory_missing", "event FSRS memory is missing")
    if state_name != "new" and source.get("memory") is None:
        raise ApplicationConflictError("fsrs_memory_missing", "event FSRS memory is missing")
    if source.get("interval_days") is not None:
        _safe_integer(source["interval_days"], "%s_interval_invalid" % name, minimum=1)
    if source.get("due_value") is not None:
        _safe_integer(source["due_value"], "%s_due_invalid" % name, minimum=1)
    if source.get("due_at_ms") is not None:
        _safe_integer(source["due_at_ms"], "%s_due_at_invalid" % name)
    return source


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    sequence: int
    card_id: str
    rating: int
    rating_anki: int
    answered_at_ms: int
    duration_ms: int
    scheduler_version: Optional[str]
    fsrs_version: Optional[str]
    config_revision: Optional[str]
    before: Dict[str, Any]
    after: Dict[str, Any]
    context: Mapping[str, Any]
    payload: Dict[str, Any] = field(repr=False)


def normalize_event(event_id: str, sequence: int, payload: Mapping[str, Any]) -> NormalizedEvent:
    """校验不可变事件中应用所需的历史字段，不补造缺失上下文。"""

    event_id = _text(event_id, "invalid_event_id")
    sequence = _safe_integer(sequence, "invalid_sequence", minimum=1)
    if not isinstance(payload, Mapping):
        raise ApplicationConflictError("invalid_event_payload", "event payload must be an object")
    raw = dict(payload)
    card_id = _text(raw.get("card_id"), "invalid_card_id")
    rating = raw.get("rating")
    rating_anki = map_rating(rating)
    answered_value = raw.get("answered_at_ms", raw.get("timestamp_ms"))
    answered_at_ms = _safe_integer(answered_value, "invalid_answered_at_ms")
    duration_value = raw.get("duration_ms", raw.get("elapsed_ms"))
    duration_ms = _safe_integer(duration_value, "invalid_duration_ms")
    if duration_ms > MAX_DURATION_MS:
        raise ApplicationConflictError("invalid_duration_ms", "duration exceeds CardAnswer uint32")
    before = _state(raw.get("before"), card_id, "before")
    after = _state(raw.get("after"), card_id, "after")
    if before.get("schedule_revision") == after.get("schedule_revision"):
        raise ApplicationConflictError("schedule_revision_not_advanced", "event before/after revision did not advance")
    context = raw.get("day_context", raw.get("context", {}))
    context = _mapping(context, "invalid_time_context")
    return NormalizedEvent(
        event_id=event_id,
        sequence=sequence,
        card_id=card_id,
        rating=rating,
        rating_anki=rating_anki,
        answered_at_ms=answered_at_ms,
        duration_ms=duration_ms,
        scheduler_version=str(raw["scheduler_version"]) if raw.get("scheduler_version") is not None else None,
        fsrs_version=str(raw["fsrs_version"]) if raw.get("fsrs_version") is not None else None,
        config_revision=str(raw["config_revision"]) if raw.get("config_revision") is not None else None,
        before=before,
        after=after,
        context=context,
        payload=raw,
    )


@dataclass(frozen=True)
class GateDecision:
    ok: bool
    reason: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)


def check_before_gate(
    event: NormalizedEvent,
    observation: Mapping[str, Any],
    *,
    profile_principal: Optional[str] = None,
    generation: Any = None,
    collection_identity: Optional[str] = None,
    expected_revision: Optional[str] = None,
) -> GateDecision:
    """在调用 ``answer_card`` 前核对身份、generation、卡和稳定 revision。"""

    if observation.get("card_exists") is False:
        return GateDecision(False, "card_not_found")
    explicit_conflicts = (
        ("undo_detected", observation.get("undo_detected")),
        ("card_reset", observation.get("reset_detected")),
        ("card_paused_or_buried", observation.get("paused_or_buried")),
        ("deck_preset_changed", observation.get("preset_changed")),
        ("same_card_branch", observation.get("branch_detected")),
    )
    for reason, detected in explicit_conflicts:
        if detected is True:
            return GateDecision(False, reason)
    checks = (
        ("profile_changed", profile_principal, observation.get("profile_principal")),
        ("generation_changed", generation, observation.get("generation")),
        ("collection_changed", collection_identity, observation.get("collection_identity")),
        ("card_changed", event.card_id, str(observation.get("card_id", ""))),
        ("before_revision_mismatch", expected_revision, observation.get("stable_revision")),
    )
    for reason, expected, actual in checks:
        if expected is not None and str(expected) != str(actual):
            return GateDecision(False, reason, {"expected": str(expected), "actual": str(actual)})
    compact = observation.get("compact_state")
    if compact is not None:
        if not _compact_state_matches(event.before, compact):
            return GateDecision(False, "before_state_mismatch")
    return GateDecision(True)


def _compact_state_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    keys = (
        "card_id",
        "schedule_revision",
        "state",
        "step",
        "memory",
        "interval_days",
        "due_kind",
        "due_value",
        "due_at_ms",
        "due_collection_day",
        "short_interval",
    )
    for key in keys:
        if key in expected and key in actual and _plain(expected[key]) != _plain(actual[key]):
            return False
    return True


def _stable_card(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    card = snapshot.get("card", snapshot)
    result = dict(_plain(card)) if isinstance(_plain(card), Mapping) else {}
    result.pop("due", None)
    result.pop("collection_mod", None)
    result.pop("mod", None)
    return result


def _fingerprint_payload(snapshot: Mapping[str, Any], *, include_due: bool) -> Dict[str, Any]:
    card = snapshot.get("card", snapshot)
    card_plain = dict(_plain(card)) if isinstance(_plain(card), Mapping) else {}
    if not include_due:
        card_plain.pop("due", None)
    card_plain.pop("collection_mod", None)
    card_plain.pop("mod", None)
    return {
        "profile_principal": snapshot.get("profile_principal"),
        "collection_identity": snapshot.get("collection_identity"),
        "card_id": str(snapshot.get("card_id", card_plain.get("card_id", ""))),
        "card": card_plain,
        "review_logs": _plain(snapshot.get("review_logs", [])),
    }


def stable_recovery_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """计算跨秒稳定指纹，排除 due/mod/current scheduler elapsed 诊断字段。"""

    return _digest(_fingerprint_payload(snapshot, include_due=False))


def business_recovery_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """计算包含业务 due 的指纹；用于发现外部编辑/分支/预设变化。"""

    return _digest(_fingerprint_payload(snapshot, include_due=True))


@dataclass(frozen=True)
class RecoveryDecision:
    status: str
    replay_allowed: bool
    reason: str
    stable_fingerprint: str
    business_fingerprint: str


def _dynamic_due(snapshot: Mapping[str, Any]) -> bool:
    diagnostic = snapshot.get("diagnostic", {})
    if isinstance(diagnostic, Mapping) and diagnostic.get("due_is_dynamic") is True:
        return True
    card = snapshot.get("card", {})
    if isinstance(card, Mapping):
        return card.get("queue") == 1 or card.get("state") == "learning"
    return False


def classify_recovery(intent: Mapping[str, Any], current: Mapping[str, Any]) -> RecoveryDecision:
    """只做证据分类；只有明确 ``NOT_APPLIED`` 才允许 replay。"""

    current_stable = stable_recovery_fingerprint(current)
    current_business = business_recovery_fingerprint(current)
    terminal = str(intent.get("status", "")).lower()
    if terminal in ("unknown", "conflict"):
        return RecoveryDecision(terminal, False, "terminal recovery state blocks replay", current_stable, current_business)

    expected_after = intent.get("after_fingerprint")
    after_snapshot = intent.get("after_snapshot")
    if expected_after is None and isinstance(after_snapshot, Mapping):
        expected_after = stable_recovery_fingerprint(after_snapshot)
    if expected_after == current_stable:
        expected_business = intent.get("after_business_fingerprint")
        if expected_business is None and isinstance(after_snapshot, Mapping):
            expected_business = business_recovery_fingerprint(after_snapshot)
        if (
            expected_business is not None
            and expected_business != current_business
            and not _dynamic_due(current)
        ):
            return RecoveryDecision("conflict", False, "business due or card state changed", current_stable, current_business)
        return RecoveryDecision("applied", False, "stable after fingerprint verified", current_stable, current_business)

    expected_before = intent.get("before_fingerprint")
    before_snapshot = intent.get("before_snapshot")
    if expected_before is None and isinstance(before_snapshot, Mapping):
        expected_before = stable_recovery_fingerprint(before_snapshot)
    if expected_before == current_stable:
        expected_business = intent.get("before_business_fingerprint")
        if expected_business is None and isinstance(before_snapshot, Mapping):
            expected_business = business_recovery_fingerprint(before_snapshot)
        if (
            expected_business is not None
            and expected_business != current_business
            and not _dynamic_due(current)
        ):
            return RecoveryDecision("conflict", False, "business due changed before apply", current_stable, current_business)
        return RecoveryDecision("not_applied", True, "before fingerprint remains exact", current_stable, current_business)

    return RecoveryDecision("unknown", False, "no unique before or after evidence", current_stable, current_business)


def _collection_identity(collection: Any, anki_version: str, anki_build: str) -> str:
    path = getattr(collection, "path", None)
    creation = getattr(collection, "crt", None)
    if path is None:
        path = getattr(collection, "identity", None)
    return _digest({"path": str(path), "creation": _plain(creation), "version": anki_version, "build": anki_build})


def _card_value(card: Any, *names: str) -> Any:
    for name in names:
        if isinstance(card, Mapping) and name in card:
            return card[name]
        try:
            return getattr(card, name)
        except (AttributeError, TypeError):
            pass
    return None


def _card_type_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _memory_value(card: Any, collection: Any) -> Any:
    memory = _card_value(card, "memory_state", "memory")
    if memory is not None:
        normalized = _plain(memory)
        return normalized if normalized not in ({}, "") else None
    compute = getattr(collection, "compute_memory_state", None)
    if callable(compute):
        try:
            normalized = _plain(compute(_card_value(card, "id", "card_id")))
            return normalized if normalized not in ({}, "") else None
        except Exception:
            return None
    return None


def _review_log(entry: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for source, target in (
        ("time", "time_seconds"),
        ("review_kind", "review_kind"),
        ("button_chosen", "button_chosen"),
        ("interval", "interval"),
        ("ease", "ease"),
        ("taken_secs", "taken_secs"),
        ("last_interval", "last_interval"),
    ):
        value = _card_value(entry, source)
        if value is not None:
            result[target] = _plain(value)
    memory = _card_value(entry, "memory_state")
    if memory is not None:
        result["memory_state"] = _plain(memory)
    return result


def capture_card_observation(
    collection: Any,
    card_id: str,
    *,
    profile_principal: Optional[str] = None,
    generation: Any = None,
    collection_identity: Optional[str] = None,
    anki_version: str = SUPPORTED_ANKI_VERSION,
    anki_build: str = SUPPORTED_ANKI_BUILD,
) -> Dict[str, Any]:
    """通过官方 Collection 读入口捕获 card/revlog/FSRS 证据。"""

    reader = getattr(collection, "get_card", None)
    if not callable(reader):
        raise ApplicationConflictError("collection_get_card_unavailable", "Collection.get_card is unavailable")
    try:
        card = reader(int(card_id))
    except Exception as exc:
        raise ApplicationConflictError("card_not_found", "requested card is unavailable") from exc
    if card is None:
        raise ApplicationConflictError("card_not_found", "requested card is unavailable")
    actual_id = str(_card_value(card, "id", "card_id"))
    if actual_id != str(card_id):
        raise ApplicationConflictError("card_identity_changed", "card identity changed")
    queue = _card_value(card, "queue")
    try:
        queue_int = int(queue)
    except (TypeError, ValueError):
        queue_int = None
    memory = _memory_value(card, collection)
    card_type = _card_value(card, "type", "card_type")
    card_type_int = _card_type_int(card_type)
    if memory is None and (queue_int in (1, 2, 3) or card_type_int in (1, 2, 3)):
        raise ApplicationConflictError("fsrs_memory_missing", "Anki card FSRS memory is unavailable")
    card_data: Dict[str, Any] = {
        "card_id": actual_id,
        "note_id": str(_card_value(card, "nid", "note_id") or ""),
        "deck_id": str(_card_value(card, "did", "deck_id") or ""),
        "type": _plain(card_type),
        "queue": _plain(queue),
        "due": _plain(_card_value(card, "due")),
        "ivl": _plain(_card_value(card, "ivl", "interval_days")),
        "factor": _plain(_card_value(card, "factor")),
        "reps": _plain(_card_value(card, "reps")),
        "lapses": _plain(_card_value(card, "lapses")),
        "left": _plain(_card_value(card, "left", "learning_steps_left")),
        "last_review_time": _plain(_card_value(card, "last_review_time", "last_review")),
        "memory_state": memory,
        "desired_retention": _plain(_card_value(card, "desired_retention")),
        "decay": _plain(_card_value(card, "decay")),
        "custom_data": _plain(_card_value(card, "custom_data")),
    }
    log_reader = getattr(collection, "get_review_logs", None)
    if not callable(log_reader):
        raise ApplicationConflictError("review_logs_unavailable", "Collection.get_review_logs is unavailable")
    try:
        logs = [_review_log(entry) for entry in log_reader(int(card_id))]
    except Exception as exc:
        raise ApplicationConflictError("review_logs_unavailable", "review log query failed") from exc
    diagnostic: Dict[str, Any] = {
        "card_due": card_data["due"],
        "collection_mod": _plain(getattr(collection, "mod", None)),
        "due_is_dynamic": queue_int == 1,
    }
    backend = getattr(collection, "_backend", None)
    state_reader = getattr(backend, "get_scheduling_states", None)
    if callable(state_reader):
        try:
            states = state_reader(int(card_id))
            diagnostic["scheduler_current_state"] = _plain(getattr(states, "current", None))
        except Exception:
            diagnostic["scheduler_current_state"] = None
    observation: Dict[str, Any] = {
        "profile_principal": profile_principal,
        "generation": generation,
        "collection_identity": collection_identity
        or _collection_identity(collection, anki_version, anki_build),
        "card_id": actual_id,
        "card_exists": True,
        "card": card_data,
        "review_logs": logs,
        "diagnostic": diagnostic,
    }
    observation["stable_revision"] = stable_recovery_fingerprint(observation)
    observation["business_revision"] = business_recovery_fingerprint(observation)
    return observation


def _marker(event_id: str) -> str:
    # Anki 26.08.1 限制 custom_data key 的长度；这只是候选 after 证据，不能替代
    # event_id 在外部 ledger 中的原子幂等记录。
    return _canonical({"ic_pr": "p3", "ic_eid": event_id})


@dataclass(frozen=True)
class PreparedApplication:
    event: NormalizedEvent
    collection: Any = field(repr=False)
    before: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ApplicationResult:
    event_id: str
    card_id: str
    status: str
    before_fingerprint: Optional[str] = None
    after_fingerprint: Optional[str] = None
    stable_recovery_fingerprint: Optional[str] = None
    business_recovery_fingerprint: Optional[str] = None
    reason: Optional[str] = None
    changes: Any = field(default=None, repr=False)
    after: Optional[Mapping[str, Any]] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "event_id": self.event_id,
            "card_id": self.card_id,
            "status": self.status,
        }
        for key, value in (
            ("before_fingerprint", self.before_fingerprint),
            ("after_fingerprint", self.after_fingerprint),
            ("stable_recovery_fingerprint", self.stable_recovery_fingerprint),
            ("business_recovery_fingerprint", self.business_recovery_fingerprint),
            ("reason", self.reason),
        ):
            if value is not None:
                result[key] = value
        return result


def build_card_answer(collection: Any, event: NormalizedEvent, *, marker: bool = True) -> Any:
    """只通过固定 bundle 的 CardAnswer/Scheduler 状态入口构造答案。"""

    backend = getattr(collection, "_backend", None)
    state_reader = getattr(backend, "get_scheduling_states", None)
    if not callable(state_reader):
        raise ApplicationBlockedError("scheduling_states_unavailable", "historical scheduling states are unavailable")
    try:
        states = state_reader(int(event.card_id))
        current_state = getattr(states, "current")
        new_state = getattr(states, RATING_FIELD[event.rating_anki])
    except Exception as exc:
        raise ApplicationConflictError("scheduling_states_unavailable", "current scheduling states are unavailable") from exc
    try:
        from anki.scheduler_pb2 import CardAnswer
    except Exception as exc:
        raise ApplicationBlockedError("anki_api_unavailable", "fixed Anki CardAnswer API is unavailable") from exc
    answer = CardAnswer(
        card_id=int(event.card_id),
        rating=event.rating_anki,
        answered_at_millis=event.answered_at_ms,
        milliseconds_taken=event.duration_ms,
    )
    try:
        answer.current_state.CopyFrom(current_state)
        if marker and hasattr(new_state, "custom_data"):
            new_state.custom_data = _marker(event.event_id)
        answer.new_state.CopyFrom(new_state)
    except Exception as exc:
        raise ApplicationConflictError("card_answer_state_invalid", "CardAnswer state could not be constructed") from exc
    return answer


def _verify_after_result(
    event: NormalizedEvent,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    """核验官方入口的结果；失败时不把部分观察冒充 applied。"""

    before_logs = list(before.get("review_logs", []))
    after_logs = list(after.get("review_logs", []))
    if len(after_logs) != len(before_logs) + 1:
        raise ApplicationConflictError(
            "after_revlog_ambiguous",
            "Anki result does not contain exactly one new review log",
        )
    candidate = after_logs[-1]
    expected_seconds = event.answered_at_ms // 1000
    observed_seconds = candidate.get("time_seconds")
    if observed_seconds is not None and int(observed_seconds) != expected_seconds:
        raise ApplicationConflictError("after_timestamp_mismatch", "Anki revlog time differs from event")
    chosen = candidate.get("button_chosen")
    if chosen is not None and int(chosen) != event.rating:
        raise ApplicationConflictError("after_rating_mismatch", "Anki revlog rating differs from event")
    taken = candidate.get("taken_secs")
    if taken is not None and int(round(float(taken) * 1000)) != event.duration_ms:
        raise ApplicationConflictError("after_duration_mismatch", "Anki revlog duration differs from event")

    before_card = before.get("card", {})
    after_card = after.get("card", {})
    before_reps = before_card.get("reps") if isinstance(before_card, Mapping) else None
    after_reps = after_card.get("reps") if isinstance(after_card, Mapping) else None
    if before_reps is not None and after_reps is not None:
        if int(after_reps) != int(before_reps) + 1:
            raise ApplicationConflictError("after_reps_mismatch", "Anki reps did not advance exactly once")
    before_lapses = before_card.get("lapses") if isinstance(before_card, Mapping) else None
    after_lapses = after_card.get("lapses") if isinstance(after_card, Mapping) else None
    if before_lapses is not None and after_lapses is not None and int(after_lapses) < int(before_lapses):
        raise ApplicationConflictError("after_lapses_regressed", "Anki lapses regressed")
    if isinstance(after_card, Mapping) and "due" not in after_card:
        raise ApplicationConflictError("after_due_missing", "Anki result omitted due")
    if event.after.get("memory") is not None and isinstance(after_card, Mapping):
        if after_card.get("memory_state") is None:
            raise ApplicationConflictError("after_memory_missing", "Anki result omitted FSRS memory")
    last_review = after_card.get("last_review_time") if isinstance(after_card, Mapping) else None
    if last_review is not None and int(last_review) != expected_seconds:
        raise ApplicationConflictError("after_last_review_mismatch", "Anki card last review differs from event")


def _stored_event_parts(stored_event: Any) -> Tuple[str, int, Mapping[str, Any]]:
    if isinstance(stored_event, Mapping):
        event_id = stored_event.get("event_id")
        sequence = stored_event.get("sequence")
        payload = stored_event.get("payload", stored_event)
    else:
        event_id = getattr(stored_event, "event_id", None)
        sequence = getattr(stored_event, "sequence", None)
        payload = getattr(stored_event, "payload", None)
    if not isinstance(payload, Mapping):
        raise ApplicationConflictError("invalid_event_payload", "stored event payload is unavailable")
    return _text(event_id, "invalid_event_id"), _safe_integer(sequence, "invalid_sequence", minimum=1), payload


class Anki260801ApplicationAdapter:
    """固定 Anki 26.08.1 的候选应用器，默认且目前唯一安全状态为 disabled。"""

    def __init__(
        self,
        collection_provider: Any,
        profile_provider: Any,
        profile_principal: Optional[str] = None,
        *,
        anki_version: str = SUPPORTED_ANKI_VERSION,
        anki_build: str = SUPPORTED_ANKI_BUILD,
        generation_provider: Any = None,
        runtime_epoch_provider: Any = None,
    ) -> None:
        self._collection_provider = collection_provider
        self._profile_provider = profile_provider
        self._profile_principal = profile_principal
        self._anki_version = anki_version
        self._anki_build = anki_build
        self._generation_provider = generation_provider
        self._runtime_epoch_provider = runtime_epoch_provider

    def _read(self, provider: Any, default: Any = None) -> Any:
        if provider is None:
            return default
        if callable(provider):
            return provider()
        return provider

    def _profile(self) -> Tuple[Optional[str], Any, Any]:
        value = self._read(self._profile_provider)
        if isinstance(value, Mapping):
            principal = value.get("profile_principal", value.get("profile_identity", value.get("name")))
            generation = value.get("generation", value.get("collection_generation"))
            epoch = value.get("runtime_epoch", value.get("epoch"))
        else:
            principal = value
            generation = None
            epoch = None
        return (
            str(principal) if principal is not None else None,
            self._read(self._generation_provider, generation),
            self._read(self._runtime_epoch_provider, epoch),
        )

    def _collection(self) -> Any:
        collection = self._read(self._collection_provider)
        if collection is None:
            raise ApplicationBlockedError("profile_not_ready", "Anki collection is not ready")
        return collection

    def capability_report(self) -> CapabilityReport:
        try:
            collection = self._read(self._collection_provider)
        except Exception:
            collection = None
        return capability_report(
            self._anki_version,
            self._anki_build,
            collection=collection,
            scheduler=getattr(collection, "sched", None) if collection is not None else None,
        )

    def prepare_event(self, stored_event: Any) -> PreparedApplication:
        event_id, sequence, payload = _stored_event_parts(stored_event)
        event = normalize_event(event_id, sequence, payload)
        collection = self._collection()
        profile, generation, _epoch = self._profile()
        if self._profile_principal is not None and profile != self._profile_principal:
            raise ApplicationConflictError("profile_changed", "Anki profile changed before application")
        identity = _collection_identity(collection, self._anki_version, self._anki_build)
        observation = capture_card_observation(
            collection,
            event.card_id,
            profile_principal=profile,
            generation=generation,
            collection_identity=identity,
            anki_version=self._anki_version,
            anki_build=self._anki_build,
        )
        expected_revision = payload.get("before_fingerprint", payload.get("before_revision"))
        decision = check_before_gate(
            event,
            observation,
            profile_principal=self._profile_principal,
            generation=generation,
            collection_identity=identity,
            expected_revision=str(expected_revision) if expected_revision is not None else None,
        )
        if not decision.ok:
            raise ApplicationConflictError(decision.reason or "before_gate_failed", "before gate rejected event")
        return PreparedApplication(event, collection, observation)

    def apply_prepared(self, prepared: PreparedApplication) -> ApplicationResult:
        self.capability_report().require_write()
        answer = build_card_answer(prepared.collection, prepared.event)
        scheduler = getattr(prepared.collection, "sched", None)
        answer_card = getattr(scheduler, "answer_card", None)
        if not callable(answer_card):
            raise ApplicationBlockedError("scheduler_answer_card_unavailable", "official Scheduler.answer_card is unavailable")
        try:
            changes = answer_card(answer)
        except Exception as exc:
            raise ApplicationError("answer_card_failed", "Anki answer_card execution failed", category="unknown") from exc
        after = capture_card_observation(
            prepared.collection,
            prepared.event.card_id,
            profile_principal=prepared.before.get("profile_principal"),
            generation=prepared.before.get("generation"),
            collection_identity=prepared.before.get("collection_identity"),
            anki_version=self._anki_version,
            anki_build=self._anki_build,
        )
        _verify_after_result(prepared.event, prepared.before, after)
        return ApplicationResult(
            event_id=prepared.event.event_id,
            card_id=prepared.event.card_id,
            status="applied",
            before_fingerprint=prepared.before.get("stable_revision"),
            after_fingerprint=after.get("stable_revision"),
            stable_recovery_fingerprint=after.get("stable_revision"),
            business_recovery_fingerprint=after.get("business_revision"),
            changes=_plain(changes),
            after=after,
        )

    def recover_event(self, intent: Mapping[str, Any], current: Mapping[str, Any]) -> RecoveryDecision:
        """恢复只返回分类，不自动重放 UNKNOWN/CONFLICT。"""

        return classify_recovery(intent, current)


Anki260801Application = Anki260801ApplicationAdapter
HistoricalApplicationAdapter = Anki260801ApplicationAdapter


__all__ = [
    "Anki260801Application",
    "Anki260801ApplicationAdapter",
    "ApplicationBlockedError",
    "ApplicationConflictError",
    "ApplicationError",
    "ApplicationResult",
    "ApplicationValidationError",
    "CapabilityReport",
    "GateDecision",
    "HistoricalApplicationAdapter",
    "NormalizedEvent",
    "PreparedApplication",
    "RecoveryDecision",
    "RATING_TO_ANKI",
    "business_recovery_fingerprint",
    "build_card_answer",
    "capability_report",
    "capture_card_observation",
    "check_before_gate",
    "classify_recovery",
    "map_rating",
    "normalize_event",
    "stable_recovery_fingerprint",
]
