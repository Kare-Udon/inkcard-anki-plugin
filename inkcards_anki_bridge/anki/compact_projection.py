"""Compact 26.09.3 input validation and pure projection helpers.

This module imports only the standard library. Anki protobuf values are
provided by the native adapter and are never loaded here at import time.
"""

from __future__ import annotations

import hashlib
import json
import math
import dataclasses
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional


PINNED_ANKI_VERSION = "26.09.3"
PINNED_ANKI_VERSION_CANONICAL = "26.9.3"
PINNED_ANKI_BUILD = "29bb700b"
PINNED_FSRS_VERSION = "6.6.2"
MARKER_KEY = "inkcards"
# 插件采用 99 字节保守政策。固定 Anki 源码的原生条件为 <=100，
# 错误文案中的 “under 100” 不能代替实际边界证据。
MAX_CUSTOM_DATA_BYTES = 99
MAX_ANSWERED_AT_MS = 9007199254740991
MAX_DURATION_MS = 0xFFFFFFFF
RATING_TO_ANKI = {1: 0, 2: 1, 3: 2, 4: 3}
RATING_NAMES = {1: "again", 2: "hard", 3: "good", 4: "easy"}
DAY_SECONDS = 86400


class CompactProjectionError(ValueError):
    """输入、来源或调度状态未达到本版本的应用门槛。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def strict_integer(value: Any, code: str, *, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompactProjectionError(code, "integer field is invalid")
    if minimum is not None and value < minimum:
        raise CompactProjectionError(code, "integer field is outside supported bounds")
    return value


def finite_number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompactProjectionError(code, "numeric field is invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CompactProjectionError(code, "numeric field is not finite")
    return parsed


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _plain(to_dict())
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return {str(key): _plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    raise CompactProjectionError("json_value_invalid", "value is not a strict JSON object")


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompactProjectionError("json_value_invalid", "value is not strict JSON") from exc


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def object_from_json(value: Any, code: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise CompactProjectionError(code, "JSON object is invalid") from exc
    value = _plain(value)
    if not isinstance(value, dict):
        raise CompactProjectionError(code, "JSON value must be an object")
    return value


def collection_day(timestamp_ms: int, today: int, day_cutoff_seconds: int) -> int:
    """按 Anki next-day cutoff 映射 collection day。"""

    timestamp_ms = strict_integer(timestamp_ms, "answered_at_invalid", minimum=0)
    today = strict_integer(today, "collection_day_invalid", minimum=0)
    day_cutoff_seconds = strict_integer(day_cutoff_seconds, "day_cutoff_invalid", minimum=1)
    return today + ((timestamp_ms // 1000 - day_cutoff_seconds) // DAY_SECONDS) + 1


def map_rating(rating: Any) -> tuple[int, str]:
    rating = strict_integer(rating, "rating_invalid", minimum=1)
    if rating not in RATING_TO_ANKI:
        raise CompactProjectionError("rating_invalid", "rating must be an integer in 1..4")
    return RATING_TO_ANKI[rating], RATING_NAMES[rating]


def normalize_preset_config(config: Any, *, expected_id: Optional[int] = None) -> dict[str, Any]:
    """Return only the exact scheduling fields supported by the native plan.

    P1/P2 labels and ids carry no compatibility meaning. An empty parameter
    list is retained as Anki's pinned-build native default; it is not replaced
    with another preset's optimized vector. Any other missing setting blocks.
    """

    raw = object_from_json(config, "preset_config_invalid")
    nested = raw.get("effective_config", raw.get("config"))
    if isinstance(nested, Mapping):
        raw = object_from_json(nested, "preset_config_invalid")

    preset_id = raw.get("id", raw.get("preset_id"))
    if preset_id is None:
        raise CompactProjectionError("preset_id_missing", "preset id is absent")
    try:
        preset_id = int(preset_id)
    except (TypeError, ValueError) as exc:
        raise CompactProjectionError("preset_id_invalid", "preset id is invalid") from exc
    if isinstance(preset_id, bool) or (expected_id is not None and preset_id != int(expected_id)):
        raise CompactProjectionError("preset_id_mismatch", "effective preset id differs from snapshot")

    fsrs = raw.get("fsrs_enabled", raw.get("fsrs"))
    if isinstance(fsrs, Mapping):
        fsrs = fsrs.get("enabled")
    if fsrs is not True:
        raise CompactProjectionError("fsrs_disabled_or_unknown", "FSRS must be explicitly enabled")

    weights = raw.get("fsrsParams6", raw.get("fsrs_params_6"))
    params_version: Any = raw.get("fsrs_version", raw.get("scheduler_version"))
    if weights is None:
        fsrs_params = raw.get("fsrs_params", raw.get("fsrsParams"))
        if isinstance(fsrs_params, Mapping):
            params_version = fsrs_params.get("version", params_version)
            weights = fsrs_params.get("params", fsrs_params.get("weights"))
        elif isinstance(fsrs_params, (list, tuple)):
            weights = fsrs_params
    if not isinstance(weights, (list, tuple)) or len(weights) not in (0, 17, 19, 21):
        raise CompactProjectionError("fsrs_params_invalid", "FSRS parameter vector is unsupported")
    normalized_weights = [finite_number(item, "fsrs_params_invalid") for item in weights]
    if params_version not in (None, 6, "6", "6.6", "6.6.2"):
        raise CompactProjectionError("fsrs_version_mismatch", "preset FSRS version is unsupported")

    retention = raw.get("desiredRetention", raw.get("desired_retention"))
    if isinstance(raw.get("fsrs"), Mapping):
        retention = raw["fsrs"].get("desiredRetention", raw["fsrs"].get("desired_retention", retention))
    retention = finite_number(retention, "desired_retention_invalid")
    if not 0.0 < retention < 1.0:
        raise CompactProjectionError("desired_retention_invalid", "retention must be between zero and one")

    max_interval = raw.get("max_interval_days", raw.get("maxIvl"))
    if max_interval is None and isinstance(raw.get("rev"), Mapping):
        max_interval = raw["rev"].get("maxIvl")
    max_interval = strict_integer(max_interval, "max_interval_invalid", minimum=1)

    max_answer_seconds = raw.get("max_answer_seconds", raw.get("maxTaken"))
    max_answer_seconds = strict_integer(max_answer_seconds, "answer_time_cap_missing", minimum=0)
    if max_answer_seconds > MAX_DURATION_MS // 1000:
        raise CompactProjectionError("answer_time_cap_invalid", "回答时间上限超出原生字段范围")

    new_steps = raw.get("learning_steps")
    if new_steps is None and isinstance(raw.get("new"), Mapping):
        new_steps = raw["new"].get("delays", raw["new"].get("steps"))
    lapse_steps = raw.get("relearning_steps")
    if lapse_steps is None and isinstance(raw.get("lapse"), Mapping):
        lapse_steps = raw["lapse"].get("delays", raw["lapse"].get("steps"))
    if not isinstance(new_steps, (list, tuple)) or not isinstance(lapse_steps, (list, tuple)):
        raise CompactProjectionError("steps_missing", "learning and relearning steps must be captured")

    return {
        "preset_id": preset_id,
        "fsrs_version": PINNED_FSRS_VERSION,
        "weights": normalized_weights,
        "desired_retention": retention,
        "max_interval_days": max_interval,
        "max_answer_seconds": max_answer_seconds,
        "learning_steps": _plain(new_steps),
        "relearning_steps": _plain(lapse_steps),
        # 完整配置进入 plan；历史设置不能由 scratch 默认值或当前设置补齐。
        "native_config": {key: _plain(value) for key, value in raw.items()
                          if key not in {"fsrs_enabled", "fsrs", "mod", "usn", "name"}},
    }


def preset_fingerprint(config: Any, *, expected_id: Optional[int] = None) -> str:
    return json_sha256(normalize_preset_config(config, expected_id=expected_id))


def resolve_snapshot_card(event: Any, snapshot: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    event_map = object_from_json(event, "event_invalid")
    snapshot_map = object_from_json(snapshot, "snapshot_invalid")
    index = strict_integer(event_map.get("card_index"), "card_index_invalid", minimum=1)
    cards = snapshot_map.get("cards")
    if not isinstance(cards, list) or index > len(cards):
        raise CompactProjectionError("card_index_invalid", "event index is absent from snapshot")
    card = object_from_json(cards[index - 1], "snapshot_card_invalid")
    event_card_id = str(event_map.get("card_id", ""))
    snapshot_card_id = str(card.get("card_id", ""))
    if not event_card_id or event_card_id != snapshot_card_id:
        raise CompactProjectionError("snapshot_card_mismatch", "event and snapshot card identities differ")
    event_snapshot_id = str(event_map.get("snapshot_id", ""))
    snapshot_id = str(snapshot_map.get("snapshot_id", ""))
    if event_snapshot_id and snapshot_id and event_snapshot_id != snapshot_id:
        raise CompactProjectionError("snapshot_id_mismatch", "event references another snapshot")

    manifest = object_from_json(snapshot_map.get("manifest", {}), "snapshot_manifest_invalid")
    deck_id = str(card.get("deck_id", card.get("did", "")))
    decks = snapshot_map.get("decks", [])
    presets = snapshot_map.get("presets", [])
    if isinstance(decks, Mapping):
        deck_map = {str(key): object_from_json(value, "snapshot_deck_invalid") for key, value in decks.items()}
    elif isinstance(decks, list):
        deck_map = {
            str(item.get("deck_id", item.get("id", ""))): object_from_json(item, "snapshot_deck_invalid")
            for item in decks
            if isinstance(item, Mapping)
        }
    else:
        raise CompactProjectionError("snapshot_decks_invalid", "snapshot decks are invalid")
    deck = deck_map.get(deck_id)
    if deck is None:
        raise CompactProjectionError("snapshot_deck_missing", "event deck is absent from snapshot")
    preset_id = deck.get("preset_id", deck.get("config_id"))
    if preset_id is None:
        raise CompactProjectionError("snapshot_preset_missing", "snapshot deck preset id is absent")
    preset_id_text = str(preset_id)
    if isinstance(presets, Mapping):
        preset = presets.get(preset_id_text)
    elif isinstance(presets, list):
        preset = next(
            (
                item
                for item in presets
                if isinstance(item, Mapping)
                and str(item.get("preset_id", item.get("id", ""))) == preset_id_text
            ),
            None,
        )
    else:
        raise CompactProjectionError("snapshot_presets_invalid", "snapshot presets are invalid")
    if not isinstance(preset, Mapping):
        # Some protocol snapshots capture the effective config on each card.
        preset = card.get("deck_config", card.get("effective_preset"))
    if not isinstance(preset, Mapping):
        raise CompactProjectionError("snapshot_preset_missing", "full preset settings are absent")
    return card, deck, {**object_from_json(preset, "snapshot_preset_invalid"), "id": preset.get("id", preset.get("preset_id", preset_id))}


def verify_card_snapshot(card: Any, snapshot_card: Mapping[str, Any], *, adopted_missing_lrt: bool = False,
                         last_review_time_source: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Compare live Anki fields with every schedule field recorded in snapshot."""

    def attr(name: str, *alternates: str) -> Any:
        for candidate in (name, *alternates):
            if hasattr(card, candidate):
                return getattr(card, candidate)
            if isinstance(card, Mapping) and candidate in card:
                return card[candidate]
        return None

    required = {
        "card_id": ("id", "card_id"),
        "note_id": ("nid", "note_id"),
        "deck_id": ("did", "deck_id"),
        "type": ("type",),
        "queue": ("queue",),
        "interval_days": ("ivl", "interval_days"),
        "factor": ("factor",),
        "reps": ("reps",),
        "lapses": ("lapses",),
        "last_review_time": ("last_review_time",),
        "schedule_revision": ("mod", "schedule_revision"),
    }
    live = {
        "card_id": str(attr("id", "card_id")),
        "note_id": str(attr("nid", "note_id")),
        "deck_id": str(attr("did", "deck_id")),
        "type": attr("type"),
        "queue": attr("queue"),
        "interval_days": attr("ivl", "interval_days"),
        "factor": attr("factor"),
        "reps": attr("reps"),
        "lapses": attr("lapses"),
        "last_review_time": attr("last_review_time"),
        "schedule_revision": attr("mod", "schedule_revision"),
    }
    aliases = {
        "card_id": ("card_id", "id"),
        "note_id": ("note_id", "nid"),
        "deck_id": ("deck_id", "did"),
        "type": ("type", "card_type"),
        "queue": ("queue",),
        "interval_days": ("interval_days", "ivl"),
        "factor": ("factor",),
        "reps": ("reps",),
        "lapses": ("lapses",),
        "last_review_time": ("last_review_time_seconds", "last_review_time", "last_review_time_ms"),
        "schedule_revision": ("schedule_revision", "mod"),
    }
    if last_review_time_source is not None:
        if live["last_review_time"] != last_review_time_source["native_seconds"]:
            raise CompactProjectionError("card_snapshot_conflict", "原生复习时间来源发生变化")
        # 仅用于 wire 比较；返回的原生前态仍保留 None，不假冒原生缓存。
        resolved_last_review = last_review_time_source["resolved_seconds"]
    else:
        resolved_last_review = live["last_review_time"]
    for field, names in aliases.items():
        expected = next((snapshot_card[name] for name in names if name in snapshot_card), None)
        observed = live[field]
        if field == "last_review_time":
            observed = resolved_last_review
        if field == "last_review_time" and int(live["type"]) == 0 and expected in (None, 0) and observed in (None, 0):
            continue
        if expected is None and field == "last_review_time" and adopted_missing_lrt:
            # 仅经显式采用来源核验完整原生前态后允许；不补写旧 wire 字段。
            continue
        if expected is None:
            raise CompactProjectionError("snapshot_card_incomplete", f"snapshot lacks {field}")
        if field == "last_review_time" and "last_review_time_ms" in snapshot_card:
            expected = int(snapshot_card["last_review_time_ms"]) // 1000
        if field == "type" and isinstance(expected, str):
            types = {"new": 0, "learning": 1, "review": 2, "relearning": 3}
            equal = int(observed) == types.get(expected.lower(), -1)
        elif field in {"card_id", "note_id", "deck_id", "schedule_revision"}:
            equal = str(expected) == str(observed)
        else:
            try:
                equal = int(expected) == int(observed)
            except (TypeError, ValueError):
                equal = False
        if not equal:
            raise CompactProjectionError("card_snapshot_conflict", f"current card {field} differs from snapshot")

    due_expected = snapshot_card.get("due")
    if isinstance(due_expected, Mapping):
        if due_expected.get("unsupported"):
            raise CompactProjectionError("snapshot_due_unsupported", "snapshot due has unsupported evidence")
        if due_expected.get("kind") not in ("review_day", "review_days", "collection_day", "new_position", "learning_timestamp", "day_learning_day"):
            raise CompactProjectionError("snapshot_due_kind_unsupported", "only review-day due is supported")
        if due_expected.get("kind") == "learning_timestamp":
            if due_expected.get("unit") != "unix_ms":
                raise CompactProjectionError("snapshot_due_kind_unsupported", "学习 due 单位必须为 unix_ms")
            due_expected = int(due_expected["value"]) // 1000
        else:
            due_expected = due_expected.get("value", due_expected.get("review_day"))
    if due_expected is None:
        raise CompactProjectionError("snapshot_due_missing", "snapshot due is absent")
    due_live = int(live_value(card, "due"))
    if int(due_expected) != due_live:
        raise CompactProjectionError("card_snapshot_conflict", "current card due differs from snapshot")

    memory_expected = snapshot_card.get("memory_state")
    memory_live = attr("memory_state")
    if int(live["type"]) == 0 and memory_expected is None and memory_live is None:
        return {"new_snapshot_verified": True}
    if not isinstance(memory_expected, Mapping) or memory_live is None:
        raise CompactProjectionError("memory_state_missing", "snapshot/live FSRS memory is absent")
    for key in ("stability", "difficulty"):
        if key not in memory_expected:
            raise CompactProjectionError("memory_state_missing", f"snapshot memory lacks {key}")
        expected_number = finite_number(memory_expected[key], "memory_state_invalid")
        observed_number = finite_number(live_value(memory_live, key), "memory_state_invalid")
        if expected_number != observed_number:
            raise CompactProjectionError("memory_state_conflict", "current FSRS memory differs from snapshot")
    custom_data = attr("custom_data")
    return {
        "card_id": live["card_id"],
        "note_id": live["note_id"],
        "deck_id": live["deck_id"],
        "type": int(live["type"]),
        "queue": int(live["queue"]),
        "due": due_live,
        "ivl": int(live["interval_days"]),
        "factor": int(live["factor"]),
        "reps": int(live["reps"]),
        "lapses": int(live["lapses"]),
        "left": int(attr("left") or 0),
        "last_review_time": int(live["last_review_time"] or 0),
        "schedule_revision": str(live["schedule_revision"]),
        "memory_state": {
            "stability": observed_number_or_attr(memory_live, "stability"),
            "difficulty": observed_number_or_attr(memory_live, "difficulty"),
        },
        "custom_data": custom_data or "",
    }


def live_value(card: Any, name: str) -> Any:
    if hasattr(card, name):
        return getattr(card, name)
    if isinstance(card, Mapping):
        return card[name]
    raise CompactProjectionError("card_field_missing", f"card lacks {name}")


def observed_number_or_attr(value: Any, name: str) -> float:
    return finite_number(live_value(value, name), "memory_state_invalid")


def verify_scheduler_state(card_state: Mapping[str, Any]) -> None:
    valid = {(0, 0), (1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}
    if (card_state.get("type"), card_state.get("queue")) not in valid:
        raise CompactProjectionError("scheduler_state_unsupported", "只支持普通 New/Learning/Review/Relearning")


def verify_review_state(card_state: Mapping[str, Any]) -> None:
    if card_state.get("type") != 2 or card_state.get("queue") != 2:
        raise CompactProjectionError("review_state_unsupported", "only ordinary Review type 2 / queue 2 is supported")


def project_review_state(
    event: Any,
    current_state: Any,
    new_state: Any,
    *,
    today: int,
    day_cutoff_seconds: int,
) -> dict[str, Any]:
    """Project one native Review answer to its historical collection-day due."""

    event_map = object_from_json(event, "event_invalid")
    rating = strict_integer(event_map.get("rating"), "rating_invalid", minimum=1)
    answer_enum, rating_name = map_rating(rating)
    answered_at_ms = strict_integer(event_map.get("answered_at_ms"), "answered_at_invalid", minimum=1)
    duration_ms = strict_integer(event_map.get("duration_ms"), "duration_invalid", minimum=0)
    if answered_at_ms > MAX_ANSWERED_AT_MS or duration_ms > MAX_DURATION_MS:
        raise CompactProjectionError("event_range_invalid", "event exceeds native scheduler field bounds")
    event_day = collection_day(answered_at_ms, today, day_cutoff_seconds)
    today = strict_integer(today, "collection_day_invalid", minimum=0)
    if event_day > today:
        raise CompactProjectionError("future_event_unsupported", "future-dated compact events are unsupported")

    normal = getattr(new_state, "normal", None)
    if normal is None or not normal.HasField("review"):
        raise CompactProjectionError(
            "answer_state_not_review",
            "selected rating leaves ordinary Review; learning/relearning projection is not verified",
        )
    review = normal.review
    interval = strict_integer(int(review.scheduled_days), "scheduled_days_invalid", minimum=0)
    due = event_day + interval
    return {
        "rating": rating,
        "rating_name": rating_name,
        "anki_rating": answer_enum,
        "answered_at_ms": answered_at_ms,
        "duration_ms": duration_ms,
        "event_collection_day": event_day,
        "runtime_today": today,
        "runtime_due": today + interval,
        "historical_due": due,
        "interval_days": interval,
        "native_after_state": new_state,
    }


def build_marker_custom_data(
    existing_custom_data: Any,
    marker_value: str,
    *,
    owned_marker_digest: Optional[str] = None,
) -> str:
    """Validate limits, preserve other keys, and replace only an owned marker."""

    if (
        not isinstance(marker_value, str)
        or len(marker_value) != 64
        or any(character not in "0123456789abcdef" for character in marker_value)
    ):
        raise CompactProjectionError("marker_value_invalid", "compact marker exceeds its value limit")
    if existing_custom_data in (None, ""):
        payload: dict[str, Any] = {}
    elif isinstance(existing_custom_data, str):
        payload = object_from_json(existing_custom_data, "custom_data_invalid")
    elif isinstance(existing_custom_data, Mapping):
        payload = object_from_json(existing_custom_data, "custom_data_invalid")
    else:
        raise CompactProjectionError("custom_data_invalid", "custom_data must be a JSON object")

    for key in payload:
        if len(str(key).encode("utf-8")) > 8:
            raise CompactProjectionError("custom_data_key_too_long", "custom_data key exceeds Anki's 8-byte limit")
    previous_marker = payload.get(MARKER_KEY)
    if MARKER_KEY in payload:
        if (
            not isinstance(owned_marker_digest, str)
            or not owned_marker_digest
            or previous_marker != owned_marker_digest
        ):
            raise CompactProjectionError(
                "compact_marker_unowned",
                "existing inkcards marker is not proven to belong to the preceding applied receipt",
            )
    elif owned_marker_digest is not None:
        raise CompactProjectionError(
            "compact_marker_owner_mismatch",
            "preceding receipt names a marker that is absent from current card data",
        )
    payload[MARKER_KEY] = marker_value
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > MAX_CUSTOM_DATA_BYTES:
        raise CompactProjectionError("custom_data_too_large", "custom_data exceeds the supported UTF-8 capacity")
    return encoded


@dataclass(frozen=True)
class CompactApplyAuthorization:
    mode: Literal["device_policy", "legacy_restore", "adopted_restore"]
    policy_id: str
    event_id: str
    payload_hash: str
    snapshot_id: str
    card_id: str
    card_schedule_revision: str
    preset_id: str
    preset_fingerprint: str
    collection_identity: str
    generation: int
    source_id: str
    source_sha256: str
    evidence_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_id": self.policy_id,
            "event_id": self.event_id,
            "payload_hash": self.payload_hash,
            "snapshot_id": self.snapshot_id,
            "card_id": self.card_id,
            "card_schedule_revision": self.card_schedule_revision,
            "preset_id": self.preset_id,
            "preset_fingerprint": self.preset_fingerprint,
            "collection_identity": self.collection_identity,
            "generation": self.generation,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "evidence_sha256": self.evidence_sha256,
        }


__all__ = [
    "CompactApplyAuthorization",
    "CompactProjectionError",
    "DAY_SECONDS",
    "MARKER_KEY",
    "MAX_ANSWERED_AT_MS",
    "MAX_CUSTOM_DATA_BYTES",
    "MAX_DURATION_MS",
    "PINNED_ANKI_BUILD",
    "PINNED_ANKI_VERSION",
    "PINNED_FSRS_VERSION",
    "RATING_NAMES",
    "RATING_TO_ANKI",
    "build_marker_custom_data",
    "canonical_json",
    "collection_day",
    "finite_number",
    "json_sha256",
    "map_rating",
    "normalize_preset_config",
    "object_from_json",
    "preset_fingerprint",
    "project_review_state",
    "resolve_snapshot_card",
    "strict_integer",
    "verify_card_snapshot",
    "verify_review_state",
]
