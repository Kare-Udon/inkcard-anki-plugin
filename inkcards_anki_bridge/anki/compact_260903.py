"""Native compact application adapter for Anki 26.09.3.

The module has no import-time Anki dependency so the bridge's ordinary Python
tests can import its public API. Every native import is delayed until a
collection is explicitly supplied by an Anki collection worker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .adopted_restore import (
    ADOPTED_POLICY_ID,
    AdoptedApplicationContext,
    parse_adopted_source,
    verify_adopted_prestate,
)
from .application_context import SnapshotApplicationContext
from .compact_projection import (
    MAX_ANSWERED_AT_MS,
    MAX_DURATION_MS,
    PINNED_ANKI_BUILD,
    PINNED_ANKI_VERSION_CANONICAL,
    CompactApplyAuthorization,
    CompactProjectionError,
    build_marker_custom_data,
    canonical_json,
    collection_day,
    json_sha256,
    map_rating,
    normalize_preset_config,
    object_from_json,
    preset_fingerprint,
    project_review_state,
    resolve_snapshot_card,
    strict_integer,
    verify_card_snapshot,
    verify_scheduler_state,
)

MARKER_SCHEMA = 1
DEVICE_POLICY_ID = "fixed-ordinary-fuzz-v1"
LEGACY_POLICY_ID = "inkcards-compact-legacy-restore-v1"
DECK_COUNTER_FIELDS = (
    "last_day_studied",
    "new_studied",
    "review_studied",
    "milliseconds_studied",
    "learning_studied",
)
CARD_STATE_FIELDS = (
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
    "mod",
    "custom_data",
)
REVIEW_LOG_FIELDS = (
    "id",
    "card_id",
    "ease",
    "interval",
    "last_interval",
    "review_time",
    "type",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CompactNativeError(RuntimeError):
    """原生 collection 前置条件、原子评分或恢复证据错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "blocked",
        mutated: bool = False,
        changes: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.mutated = bool(mutated)
        self.changes = changes


@dataclass(frozen=True)
class CompactCapabilityReport:
    anki_version: Optional[str]
    anki_build: Optional[str]
    exact: bool
    write_enabled: bool
    disabled_reasons: tuple[str, ...] = ()
    official_api: tuple[str, ...] = ()

    @property
    def reason(self) -> Optional[str]:
        return self.disabled_reasons[0] if self.disabled_reasons else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "anki_version": self.anki_version,
            "anki_build": self.anki_build,
            "exact": self.exact,
            "write_enabled": self.write_enabled,
            "disabled_reasons": list(self.disabled_reasons),
            "official_api": list(self.official_api),
        }


@dataclass(frozen=True)
class CompactNativeResult:
    status: str
    reason: Optional[str]
    before: Mapping[str, Any]
    score_after: Optional[Mapping[str, Any]]
    final_after: Optional[Mapping[str, Any]]
    changes: Any = field(repr=False, compare=False)
    mutated: bool = False
    answer_card_calls: int = 0


@dataclass(frozen=True)
class RecoveryDecision:
    classification: str
    reason: str
    verification: Mapping[str, Any]
    observed_before: Optional[Mapping[str, Any]] = None
    score_after: Optional[Mapping[str, Any]] = None
    final_after: Optional[Mapping[str, Any]] = None
    collection_generation_after: Optional[int] = None
    marker_valid: bool = False
    score_proven: bool = False


@dataclass(frozen=True)
class PreparedCompactEvent:
    event: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    authorization: CompactApplyAuthorization
    intent_id: str
    attempt_id: str
    collection_identity: str
    collection_generation: int
    policy_json: Mapping[str, Any]
    before: Mapping[str, Any]
    plan: Mapping[str, Any]
    expected_score_after: Mapping[str, Any]
    expected_after: Mapping[str, Any]
    project_info: Mapping[str, Any]
    _answer: Any = field(repr=False, compare=False)
    _answer_card: Any = field(repr=False, compare=False)
    _undo_target: Optional[int] = field(default=None, repr=False, compare=False)


class _OpChangesAccumulator:
    """Merge actual Anki OpChanges fields without inventing a host result."""

    def __init__(self) -> None:
        _collection, changes_type, _card_answer, _buildinfo = _anki_types()
        self.value = changes_type()
        self._descriptor = self.value.DESCRIPTOR

    def add(self, change: Any) -> None:
        if change is None:
            return
        if not hasattr(change, "DESCRIPTOR"):
            raise CompactNativeError("opchanges_invalid", "native mutation did not return OpChanges")
        for descriptor in self._descriptor.fields:
            name = descriptor.name
            try:
                source = getattr(change, name)
            except (AttributeError, ValueError):
                continue
            if descriptor.is_repeated:
                target = getattr(self.value, name)
                target.extend(source)
            elif descriptor.message_type is not None:
                if source is not None:
                    getattr(self.value, name).MergeFrom(source)
            elif descriptor.cpp_type == descriptor.CPPTYPE_BOOL:
                setattr(self.value, name, bool(getattr(self.value, name)) or bool(source))
            elif source != descriptor.default_value:
                setattr(self.value, name, source)


def _anki_types() -> tuple[Any, Any, Any, Any]:
    # The order matters with the installed 26.09.3 bundle's bundled modules.
    import anki.collection  # type: ignore[import-not-found]  # noqa: F401
    from anki import buildinfo  # type: ignore[import-not-found]
    from anki.collection_pb2 import OpChanges  # type: ignore[import-not-found]
    from anki.scheduler_pb2 import CardAnswer  # type: ignore[import-not-found]

    return anki.collection, OpChanges, CardAnswer, buildinfo


def _canonical_version(version: Any) -> Optional[str]:
    if not isinstance(version, str):
        return None
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", version)
    if match is None:
        return None
    return ".".join(str(int(part)) for part in match.groups())


def _version_values() -> tuple[Optional[str], Optional[str]]:
    try:
        _collection, _changes, _answer, buildinfo = _anki_types()
    except Exception:
        return None, None
    version = _canonical_version(getattr(buildinfo, "version", None))
    build = getattr(buildinfo, "buildhash", None)
    return version, str(build).lower() if build is not None else None


def _plain(value: Any) -> Any:
    try:
        from google.protobuf.json_format import MessageToDict

        if hasattr(value, "DESCRIPTOR"):
            return MessageToDict(value, preserving_proto_field_name=True)
    except Exception:
        pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _plain(to_dict())
    if hasattr(value, "__dict__"):
        return {str(key): _plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    raise CompactNativeError("native_value_invalid", "native value cannot be made JSON-safe")


def _record_value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _snapshot_payload(snapshot: Any, snapshot_id: Optional[str] = None) -> dict[str, Any]:
    record_id = _record_value(snapshot, "snapshot_id")
    if record_id and snapshot_id and str(record_id) != str(snapshot_id):
        raise CompactNativeError("snapshot_id_mismatch", "事件与实际 SnapshotRecord 标识不符")
    payload_json = _record_value(snapshot, "payload_json")
    if isinstance(payload_json, str):
        payload = object_from_json(payload_json, "snapshot_invalid")
    elif isinstance(snapshot, Mapping):
        payload = object_from_json(snapshot, "snapshot_invalid")
    else:
        payload = object_from_json(_plain(snapshot), "snapshot_invalid")
    if record_id and payload.get("snapshot_id") and str(payload["snapshot_id"]) != str(record_id):
        raise CompactNativeError("snapshot_id_mismatch", "SnapshotRecord 载荷标识不符")
    if (record_id or snapshot_id) and not payload.get("snapshot_id"):
        payload["snapshot_id"] = str(record_id or snapshot_id)
    manifest_json = _record_value(snapshot, "manifest_json")
    if manifest_json and not payload.get("manifest"):
        payload["manifest"] = object_from_json(manifest_json, "snapshot_manifest_invalid")
    restoration_hash = _record_value(snapshot, "restoration_source_sha256")
    if restoration_hash is not None and _record_value(snapshot, "adopted_source") is None:
        # 显式本地恢复来源可补充旧 wire 未导出的 producer 元数据。
        # 输入只取核验过的历史 sidecar；原 payload/checksum 永不改写。
        context = _record_value(snapshot, "application_context")
        if not isinstance(context, SnapshotApplicationContext) or not _HEX64.fullmatch(restoration_hash):
            raise CompactNativeError("legacy_source_invalid", "恢复视图缺少完整历史来源")
        source = context.source
        manifest = object_from_json(payload.get("manifest", {}), "snapshot_manifest_invalid")
        for key, value in {
            "profile_identity": source.profile_identity, "profile_generation": source.collection_generation,
            "runtime_epoch": source.runtime_epoch, "anki_version": source.anki_version,
            "anki_build": source.anki_build, "timezone": source.timezone,
        }.items():
            if key not in manifest:
                manifest[key] = value
        payload["manifest"] = manifest
    return payload


def _snapshot_application_context(
    snapshot: Any,
    payload: Mapping[str, Any],
) -> Optional[SnapshotApplicationContext | AdoptedApplicationContext]:
    """读取并校验 payload 旁路的 Collection context，不把字段并入设备载荷。"""

    context = _record_value(snapshot, "application_context")
    if context is None:
        return None
    if not isinstance(context, (SnapshotApplicationContext, AdoptedApplicationContext)):
        raise CompactNativeError("snapshot_application_context_invalid", "快照旁路评分上下文类型无效")

    payload_json = _record_value(snapshot, "payload_json")
    checksum = _record_value(snapshot, "checksum")
    profile_id = _record_value(snapshot, "profile_id")
    record_id = _record_value(snapshot, "snapshot_id")
    snapshot_generation = _record_value(snapshot, "snapshot_generation")
    if (
        not isinstance(payload_json, str)
        or not isinstance(checksum, str)
        or not isinstance(profile_id, str)
        or not isinstance(record_id, str)
        or isinstance(snapshot_generation, bool)
        or not isinstance(snapshot_generation, int)
    ):
        raise CompactNativeError("snapshot_application_context_binding_invalid", "快照旁路评分上下文缺少记录绑定字段")
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != checksum:
        raise CompactNativeError("snapshot_checksum_mismatch", "快照 payload checksum 不符")
    if (
        context.profile_id != profile_id
        or context.snapshot_id != record_id
        or context.snapshot_id != str(payload.get("snapshot_id", ""))
        or context.snapshot_checksum != checksum
        or context.snapshot_generation != snapshot_generation
    ):
        raise CompactNativeError("snapshot_application_context_binding_mismatch", "快照与旁路评分上下文绑定不符")

    if isinstance(context, AdoptedApplicationContext):
        try:
            value = _record_value(snapshot, "adopted_source")
            if not isinstance(value, Mapping) or parse_adopted_source(snapshot, value) != context:
                raise ValueError("adopted context binding")
            if json_sha256(value) != _record_value(snapshot, "restoration_source_sha256"):
                raise ValueError("adopted source digest")
        except (KeyError, TypeError, ValueError) as exc:
            raise CompactNativeError("legacy_source_invalid", "采用上下文与明确来源绑定不符") from exc
        return context
    manifest = object_from_json(payload.get("manifest", {}), "snapshot_manifest_invalid")
    source = context.source
    manifest_identity = manifest.get("profile_identity", manifest.get("profile_principal"))
    manifest_generation = manifest.get("profile_generation")
    manifest_version = _canonical_version(manifest.get("anki_version"))
    source_version = _canonical_version(source.anki_version)
    try:
        manifest_generation = strict_integer(manifest_generation, "snapshot_generation_invalid", minimum=0)
    except CompactProjectionError as exc:
        raise CompactNativeError(exc.code, exc.message) from exc
    payload_generation = payload.get("snapshot_generation")
    if isinstance(payload_generation, bool) or not isinstance(payload_generation, int):
        raise CompactNativeError("snapshot_generation_invalid", "快照 generation 缺失或无效")
    if (
        context.source_sha256 != source.source_sha256
        or source.collection_generation != context.snapshot_generation
        or context.snapshot_generation != manifest_generation
        or context.snapshot_generation != payload_generation
        or source.profile_identity != manifest_identity
        or source.runtime_epoch != manifest.get("runtime_epoch")
        or source_version is None
        or source_version != manifest_version
        or source_version != PINNED_ANKI_VERSION_CANONICAL
        or source.anki_build.lower() != str(manifest.get("anki_build", "")).lower()
        or source.anki_build.lower() != PINNED_ANKI_BUILD
    ):
        raise CompactNativeError("snapshot_application_context_binding_mismatch", "旁路评分上下文与快照 producer 身份不符")
    return context


def _event_payload(event: Any) -> dict[str, Any]:
    return object_from_json(_plain(event), "event_invalid")


def _profile_binding(manifest: Mapping[str, Any], collection_identity: str) -> None:
    manifest_identity = manifest.get("profile_identity", manifest.get("collection_identity"))
    if not isinstance(manifest_identity, str) or not manifest_identity:
        raise CompactNativeError("snapshot_identity_missing", "snapshot profile identity is absent")
    if manifest_identity != collection_identity:
        raise CompactNativeError("collection_identity_mismatch", "snapshot and collection identities differ")
    version = _canonical_version(manifest.get("anki_version"))
    build = str(manifest.get("anki_build", "")).lower()
    if version != PINNED_ANKI_VERSION_CANONICAL or build != PINNED_ANKI_BUILD:
        raise CompactNativeError("snapshot_anki_build_mismatch", "snapshot Anki build differs from pinned build")


def _snapshot_timezone(
    snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
    application_context: Optional[SnapshotApplicationContext | AdoptedApplicationContext] = None,
) -> str:
    day_table = snapshot.get("day_table", {})
    if not isinstance(day_table, Mapping):
        day_table = {}
    if application_context is not None:
        zone = application_context.source.timezone
        payload_zone = manifest.get("timezone", snapshot.get("timezone", day_table.get("timezone")))
        if payload_zone is not None and payload_zone != zone:
            raise CompactNativeError("snapshot_timezone_conflict", "快照 payload 与旁路时区证据不一致")
    else:
        zone = manifest.get("timezone", snapshot.get("timezone", day_table.get("timezone")))
    if not isinstance(zone, str) or not zone:
        raise CompactNativeError("snapshot_timezone_missing", "snapshot timezone evidence is absent")
    if zone != "Asia/Shanghai":
        raise CompactNativeError("snapshot_timezone_unsupported", "本版本只验证了 Asia/Shanghai 的固定日界历史策略")
    return zone


def _snapshot_config(
    snapshot: Mapping[str, Any],
    snapshot_preset: Mapping[str, Any],
    snapshot_card: Mapping[str, Any],
    expected_preset_id: int,
    application_context: Optional[SnapshotApplicationContext | AdoptedApplicationContext] = None,
    deck_id: Optional[str] = None,
) -> dict[str, Any]:
    if application_context is not None:
        source_context = application_context.source
        preset_key = str(expected_preset_id)
        if deck_id is not None and str(source_context.deck_preset_ids.get(str(deck_id), "")) != preset_key:
            raise CompactNativeError("snapshot_preset_binding_mismatch", "旁路 context 的 deck/preset 对应关系不符")
        source = source_context.preset_configs.get(preset_key)
        if not isinstance(source, Mapping):
            raise CompactNativeError("snapshot_preset_context_missing", "旁路 context 缺少事件使用的原始 preset")
        source_map = object_from_json(source, "snapshot_preset_invalid")
        raw_id = source_map.get("id", source_map.get("preset_id"))
        if str(raw_id) != preset_key:
            raise CompactNativeError("snapshot_preset_binding_mismatch", "旁路 context 的 preset ID 不符")
        raw_params = source_map.get("fsrsParams6", source_map.get("fsrs_params_6"))
        parameter_identity = source_context.preset_parameter_identities.get(preset_key)
        if isinstance(raw_params, (list, tuple)) and len(raw_params) == 0:
            version = _canonical_version(source_context.anki_version)
            expected_identity = f"anki-native-default:{version}:{source_context.anki_build.lower()}"
        else:
            expected_identity = f"preset:{preset_key}:fsrsParams6"
        if not isinstance(parameter_identity, str) or parameter_identity != expected_identity:
            raise CompactNativeError("snapshot_parameter_identity_mismatch", "旁路 FSRS 参数身份不匹配")
        if source_context.fsrs_enabled is not True:
            raise CompactNativeError("fsrs_disabled_or_unknown", "旁路 context 未证明 Collection 已启用 FSRS")
        source_map["fsrs_enabled"] = source_context.fsrs_enabled
    else:
        source = snapshot_card.get("deck_config", snapshot_card.get("effective_preset", snapshot_preset))
        source_map = object_from_json(source, "snapshot_preset_invalid")
    if "fsrs_enabled" not in source_map:
        manifest = snapshot.get("manifest", {})
        enabled = manifest.get("fsrs_enabled") if isinstance(manifest, Mapping) else None
        # 版本支持 FSRS 并不证明历史快照发布时实际启用了 FSRS。
        if enabled is not None:
            source_map["fsrs_enabled"] = bool(enabled)
    return normalize_preset_config(source_map, expected_id=expected_preset_id)


def _snapshot_day_values(
    collection: Any,
    application_context: Optional[SnapshotApplicationContext | AdoptedApplicationContext],
) -> tuple[int, int]:
    live_today = strict_integer(int(collection.sched.today), "collection_day_invalid", minimum=0)
    live_cutoff = strict_integer(int(collection.sched.day_cutoff), "day_cutoff_invalid", minimum=1)
    if application_context is None:
        return live_today, live_cutoff
    source = application_context.source
    captured_today = source.scheduler_today
    captured_cutoff = source.day_cutoff_seconds
    if captured_today is None or captured_cutoff is None:
        raise CompactNativeError("snapshot_day_context_missing", "旁路 context 缺少 Collection today/cutoff")
    if source.collection_created_at_seconds is None:
        raise CompactNativeError("snapshot_creation_time_missing", "旁路 context 缺少 Collection 创建时间")
    if source.unsupported:
        raise CompactNativeError("snapshot_context_unsupported", "旁路 context 包含未验证的配置或时钟字段")
    if source.fsrs_short_term_with_steps_enabled is None or (
        source.fsrs_short_term_with_steps_enabled != collection.fsrs_short_term_with_steps_enabled
    ):
        raise CompactNativeError("snapshot_short_term_fsrs_conflict", "短期 FSRS 开关与捕获上下文不符")
    for key in ("rollover", "localOffset", "creationOffset"):
        if key not in source.clock_config or source.clock_config[key] != collection.get_config(key):
            raise CompactNativeError("snapshot_clock_config_conflict", "Collection 时钟配置与捕获上下文不符")
    if int(collection.crt) != source.collection_created_at_seconds:
        raise CompactNativeError("snapshot_creation_time_conflict", "Collection 创建时间与捕获上下文不符")
    # Asia/Shanghai 无夏令时。日编号与 epoch cutoff 必须同步推进，允许离线跨日。
    elapsed_days = live_today - captured_today
    if elapsed_days < 0 or live_cutoff - captured_cutoff != elapsed_days * 86400:
        raise CompactNativeError("snapshot_day_mapping_conflict", "捕获日界无法与当前 Collection 日界对应")
    now = int(time.time())
    if not live_cutoff - 86400 <= now < live_cutoff:
        raise CompactNativeError("live_day_clock_invalid", "当前 Collection cutoff 与实际时钟不符")
    return live_today, live_cutoff


def _live_config(collection: Any, card: Any, expected_preset_id: int) -> dict[str, Any]:
    decks = getattr(collection, "decks", None)
    reader = getattr(decks, "config_dict_for_deck_id", None)
    if not callable(reader):
        raise CompactNativeError("preset_api_unavailable", "DeckManager preset API is unavailable")
    effective_deck_id = int(getattr(card, "odid", 0) or getattr(card, "did"))
    native_deck = collection._backend.get_deck(effective_deck_id)
    if native_deck.HasField("normal") and native_deck.normal.HasField("desired_retention"):
        raise CompactNativeError("deck_retention_override_unsupported", "牌组级 retention 覆盖尚未纳入快照证据")
    try:
        raw = _plain(reader(effective_deck_id))
    except Exception as exc:
        raise CompactNativeError("preset_unavailable", "effective preset could not be read") from exc
    if not isinstance(raw, dict):
        raise CompactNativeError("preset_invalid", "effective preset is not a JSON object")
    fsrs = getattr(collection, "get_config", None)
    if not callable(fsrs):
        raise CompactNativeError("fsrs_config_unavailable", "FSRS enablement cannot be read")
    try:
        raw["fsrs_enabled"] = fsrs("fsrs")
    except Exception as exc:
        raise CompactNativeError("fsrs_config_unavailable", "FSRS enablement cannot be read") from exc
    try:
        return normalize_preset_config(raw, expected_id=expected_preset_id)
    except CompactProjectionError as exc:
        raise CompactNativeError(exc.code, exc.message) from exc


def _find_snapshot_deck(snapshot: Mapping[str, Any], deck_id: str) -> dict[str, Any]:
    decks = snapshot.get("decks")
    if isinstance(decks, Mapping):
        value = decks.get(deck_id)
        if value is None:
            value = decks.get(str(deck_id))
        if value is None:
            raise CompactNativeError("snapshot_deck_missing", "snapshot deck is absent")
        return object_from_json(value, "snapshot_deck_invalid")
    if isinstance(decks, list):
        for item in decks:
            if isinstance(item, Mapping) and str(item.get("deck_id", item.get("id", ""))) == deck_id:
                return object_from_json(item, "snapshot_deck_invalid")
    raise CompactNativeError("snapshot_deck_missing", "snapshot deck is absent")


def _identity_summary(event: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "profile_id": str(event.get("profile_id", "")),
        "device_id": str(event.get("device_id", "")),
        "session_id": str(event.get("session_id", "")),
        "sequence": int(event.get("sequence", 0)),
        "event_id": str(event.get("event_id", "")),
        "payload_hash": str(event.get("payload_hash", "")),
        "snapshot_id": str(event.get("snapshot_id", snapshot.get("snapshot_id", ""))),
        "card_id": str(event.get("card_id", "")),
        "card_index": int(event.get("card_index", 0)),
    }


def create_apply_authorization(
    collection: Any,
    event: Any,
    snapshot: Any,
    *,
    collection_identity: str,
    collection_generation: int,
    mode: str = "device_policy",
    legacy_source: Optional[Mapping[str, Any]] = None,
    predecessor_after: Optional[Mapping[str, Any]] = None,
    expected_counters: Optional[Mapping[str, Any]] = None,
) -> CompactApplyAuthorization:
    """Create an event-bound policy claim using read-only native state."""

    event_map = _event_payload(event)
    snapshot_map = _snapshot_payload(snapshot, str(event_map.get("snapshot_id", "")))
    application_context = _snapshot_application_context(snapshot, snapshot_map)
    manifest = object_from_json(snapshot_map.get("manifest", {}), "snapshot_manifest_invalid")
    version, build = _version_values()
    if version != PINNED_ANKI_VERSION_CANONICAL or build != PINNED_ANKI_BUILD:
        raise CompactNativeError("unsupported_anki_build", "native write requires Anki 26.09.3 / 29bb700b")
    if application_context is None:
        raise CompactNativeError("snapshot_application_context_missing", "评分必须使用冻结的原始 context")
    if mode not in ("device_policy", "legacy_restore", "adopted_restore"):
        raise CompactNativeError("authorization_mode_invalid", "authorization mode is unsupported")
    adopted = isinstance(application_context, AdoptedApplicationContext)
    if adopted != (mode == "adopted_restore"):
        raise CompactNativeError("authorization_mode_invalid", "采用来源只能走明确 adopted_restore 模式")
    if adopted:
        if predecessor_after is not None:
            raise CompactNativeError("adopted_prestate_conflict", "采用来源不接管已有前序收据")
        verify_adopted_prestate(collection, snapshot, event_map)
    if not isinstance(collection_identity, str) or not collection_identity:
        raise CompactNativeError("collection_identity_missing", "collection identity is required")
    if isinstance(collection_generation, bool) or not isinstance(collection_generation, int) or collection_generation < 0:
        raise CompactNativeError("collection_generation_invalid", "collection generation is invalid")
    try:
        _profile_binding(manifest, collection_identity)
        _snapshot_timezone(snapshot_map, manifest, application_context)
        _snapshot_day_values(collection, application_context)
        snapshot_card, snapshot_deck, snapshot_preset = resolve_snapshot_card(event_map, snapshot_map)
    except CompactProjectionError as exc:
        raise CompactNativeError(exc.code, exc.message) from exc

    card_id = strict_integer(int(event_map["card_id"]), "card_id_invalid", minimum=1)
    get_card = getattr(collection, "get_card", None)
    if not callable(get_card):
        raise CompactNativeError("collection_get_card_unavailable", "Collection.get_card is unavailable")
    try:
        card = get_card(card_id)
    except Exception as exc:
        raise CompactNativeError("card_not_found", "event card is absent from the current collection") from exc
    try:
        verify_scheduler_state(_capture_card(card))
        if predecessor_after is None:
            _verify_snapshot_card(collection, card, snapshot_card, application_context, adopted_missing_lrt=adopted)
            before_card = _capture_card(card)
        else:
            predecessor = object_from_json(predecessor_after, "predecessor_invalid")
            predecessor_card = predecessor.get("card", predecessor)
            if str(predecessor_card.get("card_id", predecessor_card.get("id", ""))) != str(card_id):
                raise CompactNativeError("predecessor_card_mismatch", "predecessor belongs to another card")
            before_card = _capture_card(card)
            if _snapshot_supersedes_predecessor(predecessor, application_context):
                _verify_snapshot_card(collection, card, snapshot_card, application_context, adopted_missing_lrt=adopted)
            else:
                _verify_predecessor_card(before_card, predecessor_card)
                if predecessor.get("native_card_hex") != _capture_native_card_hex(collection, card_id):
                    raise CompactNativeError("predecessor_native_state_conflict", "当前原始卡片状态与已应用收据的原生读回不符")
    except CompactProjectionError as exc:
        raise CompactNativeError(exc.code, exc.message) from exc

    current_custom = _custom_data_map(before_card.get("custom_data"))
    current_marker = current_custom.get("inkcards")
    if predecessor_after is None:
        if current_marker is not None:
            raise CompactNativeError("compact_marker_unowned", "existing inkcards marker has no preceding applied receipt")
    else:
        predecessor = object_from_json(predecessor_after, "predecessor_invalid")
        predecessor_card = predecessor.get("card", predecessor)
        predecessor_custom = _custom_data_map(predecessor_card.get("custom_data"))
        owned_marker = predecessor_custom.get("inkcards")
        if _snapshot_supersedes_predecessor(predecessor, application_context) and current_marker is None:
            pass  # Undo 可撤销 marker；已验证的新冻结卡态接管当前基线。
        elif (
            not isinstance(owned_marker, str)
            or _HEX64.fullmatch(owned_marker) is None
            or current_marker != owned_marker
        ):
            raise CompactNativeError("compact_marker_owner_mismatch", "current marker does not match the preceding applied receipt")

    try:
        verify_scheduler_state(before_card)
        rating, _ = map_rating(event_map.get("rating"))
        del rating
        deck_id = str(getattr(card, "did"))
        if str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))) != deck_id:
            raise CompactNativeError("snapshot_deck_mismatch", "card deck differs from its snapshot")
        preset_id = int(snapshot_deck.get("preset_id", snapshot_deck.get("config_id")))
        current_config = _live_config(collection, card, preset_id)
        snapshot_config = _snapshot_config(
            snapshot_map,
            snapshot_preset,
            snapshot_card,
            preset_id,
            application_context,
            str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))),
        )
        if current_config != snapshot_config:
            raise CompactNativeError("preset_snapshot_conflict", "effective preset differs from the event snapshot")
    except CompactProjectionError as exc:
        raise CompactNativeError(exc.code, exc.message) from exc

    _, verified_counters = _verify_snapshot_deck_context(
        collection, snapshot_map, deck_id, application_context,
        collection_generation, expected_counters,
    )

    event_id = str(event_map.get("event_id", ""))
    payload_hash = str(event_map.get("payload_hash", ""))
    snapshot_id = str(event_map.get("snapshot_id", snapshot_map.get("snapshot_id", "")))
    if not event_id or not _HEX64.fullmatch(payload_hash) or not snapshot_id:
        raise CompactNativeError("event_binding_invalid", "event, payload hash, or snapshot identity is absent")

    if mode == "device_policy":
        if legacy_source is not None:
            raise CompactNativeError("unexpected_legacy_source", "device policy does not accept legacy evidence")
        policy_id = DEVICE_POLICY_ID
        source_id = "compact-event-v1"
        source_hash = json_sha256(
            {
                "event": _identity_summary(event_map, snapshot_map),
                "snapshot": snapshot_id,
                "snapshot_checksum": _record_value(snapshot, "checksum"),
                "application_context_sha256": (
                    application_context.source_sha256 if application_context is not None else None
                ),
            }
        )
    else:
        if not isinstance(legacy_source, Mapping):
            raise CompactNativeError("legacy_source_required", "legacy recovery needs explicit source evidence")
        source_id = legacy_source.get("source_id")
        source_hash = legacy_source.get("source_sha256")
        if not isinstance(source_id, str) or not source_id:
            raise CompactNativeError("legacy_source_id_invalid", "legacy source id is invalid")
        if not isinstance(source_hash, str) or not _HEX64.fullmatch(source_hash):
            raise CompactNativeError("legacy_source_hash_invalid", "legacy source SHA-256 is invalid")
        for key, expected in (
            ("event_id", event_id),
            ("payload_hash", payload_hash),
            ("snapshot_id", snapshot_id),
            ("card_id", str(card_id)),
        ):
            if str(legacy_source.get(key, "")) != expected:
                raise CompactNativeError("legacy_source_binding_mismatch", "legacy source evidence names another event")
        policy_id = ADOPTED_POLICY_ID if adopted else LEGACY_POLICY_ID

    revision = str(before_card.get("schedule_revision", ""))
    if not revision:
        raise CompactNativeError("card_revision_missing", "card schedule revision is absent")
    current_fingerprint = json_sha256(current_config)
    authorization_body = {
        "mode": mode,
        "policy_id": policy_id,
        "event_id": event_id,
        "payload_hash": payload_hash,
        "snapshot_id": snapshot_id,
        "card_id": str(card_id),
        "card_schedule_revision": revision,
        "preset_id": str(preset_id),
        "preset_fingerprint": current_fingerprint,
        "collection_identity": collection_identity,
        "generation": collection_generation,
        "source_id": source_id,
        "source_sha256": source_hash,
        "predecessor_after_sha256": json_sha256(predecessor_after) if predecessor_after is not None else None,
        "native_card_sha256": json_sha256(_capture_native_card_hex(collection, card_id)),
        "verified_counters_sha256": json_sha256(verified_counters),
    }
    return CompactApplyAuthorization(
        mode=mode,  # type: ignore[arg-type]
        policy_id=policy_id,
        event_id=event_id,
        payload_hash=payload_hash,
        snapshot_id=snapshot_id,
        card_id=str(card_id),
        card_schedule_revision=revision,
        preset_id=str(preset_id),
        preset_fingerprint=current_fingerprint,
        collection_identity=collection_identity,
        generation=collection_generation,
        source_id=source_id,
        source_sha256=source_hash,
        evidence_sha256=json_sha256(authorization_body),
    )


def _capture_card(card: Any) -> dict[str, Any]:
    memory = getattr(card, "memory_state", None)
    return {
        "card_id": str(getattr(card, "id")),
        "note_id": str(getattr(card, "nid")),
        "deck_id": str(getattr(card, "did")),
        "type": int(getattr(card, "type")),
        "queue": int(getattr(card, "queue")),
        "due": int(getattr(card, "due")),
        "ivl": int(getattr(card, "ivl")),
        "factor": int(getattr(card, "factor")),
        "reps": int(getattr(card, "reps")),
        "lapses": int(getattr(card, "lapses")),
        "left": int(getattr(card, "left")),
        "last_review_time": int(getattr(card, "last_review_time") or 0),
        "schedule_revision": str(getattr(card, "mod")),
        "memory_state": (
            {
                "stability": float(memory.stability),
                "difficulty": float(memory.difficulty),
            }
            if memory is not None
            else None
        ),
        "custom_data": str(getattr(card, "custom_data", "") or ""),
    }


def _capture_native_card_hex(collection: Any, card_id: int) -> str:
    """以原生 protobuf 字节绑定全部卡片字段，包括 mod、USN 和 f32 位型。"""

    card = collection._backend.get_card(card_id)
    return card.SerializeToString(deterministic=True).hex()


def _as_f32(value: float) -> float:
    return struct.unpack(">f", struct.pack(">f", float(value)))[0]


def _anki_storage_memory(value: float, places: int) -> float:
    """按固定版 CardData 的 f32 规则预测落盘 memory。"""

    # Anki 26.09.3 source `rslib/src/storage/card/data.rs` at commit
    # 29bb700b951e3f0c0cb69b77c0180fc1fe33e6ba (SHA-256
    # aacbb588af4cf902eeb54d15cecc37a35b410441d67839f48929fea9fd515a89)
    # uses `(value * 10_f32.powi(places)).round() / factor`; f32::round
    # rounds ties away from zero. Compare the resulting stored f32 exactly.
    factor = _as_f32(10.0 ** places)
    scaled = _as_f32(_as_f32(value) * factor)
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return _as_f32(_as_f32(float(rounded)) / factor)


def _snapshot_supersedes_predecessor(predecessor: Mapping[str, Any], context: Optional[SnapshotApplicationContext | AdoptedApplicationContext]) -> bool:
    baseline = predecessor.get('superseding_snapshot_generation')
    if baseline is None:
        return False
    previous = predecessor.get('receipt_generation_after')
    if (context is None or isinstance(baseline, bool) or not isinstance(baseline, int)
        or isinstance(previous, bool) or not isinstance(previous, int)
        or previous < 0 or baseline <= previous or baseline != context.source.collection_generation):
        raise CompactNativeError('predecessor_baseline_invalid', '新冻结基线与前序已验证收据代数不符')
    return True


def _verify_predecessor_card(current: Mapping[str, Any], predecessor: Mapping[str, Any]) -> None:
    """Match the receipt's stable schedule state and marker, excluding volatile mod."""

    card = predecessor.get("card", predecessor)
    if not isinstance(card, Mapping):
        raise CompactNativeError("predecessor_invalid", "preceding applied state has no card object")
    fields = (
        "card_id", "note_id", "deck_id", "type", "queue", "due", "ivl", "factor",
        "reps", "lapses", "left", "last_review_time", "memory_state", "custom_data",
    )
    missing = [field for field in fields if field not in card]
    if missing:
        raise CompactNativeError("predecessor_incomplete", "preceding applied state is incomplete")
    for field in fields:
        left, right = current.get(field), card.get(field)
        if field == "memory_state":
            if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                raise CompactNativeError("predecessor_state_conflict", "FSRS memory state is missing")
            if (
                float(left.get("stability", float("nan")))
                != float(right.get("stability", float("nan")))
                or float(left.get("difficulty", float("nan")))
                != float(right.get("difficulty", float("nan")))
            ):
                raise CompactNativeError("predecessor_state_conflict", "current FSRS memory differs from receipt")
        elif str(left) != str(right) if field in {"card_id", "note_id", "deck_id"} else left != right:
            raise CompactNativeError("predecessor_state_conflict", "current card differs from preceding applied receipt")


def _proto_dict(value: Any) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(value, preserving_proto_field_name=True)


def _review_log(row: Any, card_id: int) -> dict[str, Any]:
    raw = _plain(row)
    if not isinstance(raw, Mapping):
        raise CompactNativeError("review_log_invalid", "review log row is not a mapping")
    # Collection.get_review_logs() in the pinned backend returns StatsRevlogEntry:
    # Unix seconds, button_chosen, and intervals in seconds, not raw revlog rows.
    if "time" not in raw or "review_kind" not in raw or "button_chosen" not in raw:
        raise CompactNativeError("review_log_fields_missing", "Anki StatsRevlogEntry lacks pinned fields")
    kind = raw["review_kind"]
    if kind not in ("REVIEW", 1):
        raise CompactNativeError("review_log_kind_unsupported", "only ordinary Review log entries are supported")
    try:
        interval_seconds = int(raw["interval"])
        last_interval_seconds = int(raw["last_interval"])
        taken_seconds = float(raw["taken_secs"])
        event_seconds = int(raw["time"])
        if interval_seconds < 0 or last_interval_seconds < 0 or interval_seconds % 86400 or last_interval_seconds % 86400:
            raise ValueError
        if not math.isfinite(taken_seconds) or taken_seconds < 0:
            raise ValueError
        return {
            "id": event_seconds,
            "card_id": int(card_id),
            "ease": int(raw["button_chosen"]),
            "interval": interval_seconds // 86400,
            "last_interval": last_interval_seconds // 86400,
            "review_time": int(round(taken_seconds * 1000)),
            "type": 1,
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CompactNativeError("review_log_fields_invalid", "Anki Review log fields are unsupported") from exc


def _get_review_logs(collection: Any, card_id: int) -> list[dict[str, Any]]:
    # 固定版 StatsRevlogEntry 将 ID 降到秒，不能证明实际 revlog ID。
    # 这里只使用 Anki DBProxy 的 SELECT；所有写入仍走原生评分/更新 API。
    reader = getattr(getattr(collection, "db", None), "all", None)
    if not callable(reader):
        raise CompactNativeError("review_logs_unavailable", "Anki DBProxy 只读接口不可用")
    try:
        rows = reader(
            "SELECT id,cid,ease,ivl,lastIvl,time,type FROM revlog WHERE cid=? ORDER BY id", card_id
        )
    except Exception as exc:
        raise CompactNativeError("review_logs_unavailable", "card review logs could not be read") from exc
    names = REVIEW_LOG_FIELDS
    parsed = [dict(zip(names, (int(value) for value in row))) for row in rows]
    return sorted(parsed, key=lambda row: (row["id"], row["card_id"]))


def _counter_deck_ids(snapshot: Mapping[str, Any], deck_id: str) -> tuple[int, ...]:
    decks = snapshot.get("decks")
    if isinstance(decks, list):
        deck_map = {
            str(item.get("deck_id", item.get("id", ""))): item
            for item in decks
            if isinstance(item, Mapping)
        }
    elif isinstance(decks, Mapping):
        deck_map = {str(key): _plain(value) for key, value in decks.items()}
    else:
        raise CompactNativeError("snapshot_decks_invalid", "snapshot deck tree is invalid")
    result: list[int] = []
    seen: set[str] = set()
    current = str(deck_id)
    while current:
        if current in seen:
            raise CompactNativeError("snapshot_deck_cycle", "snapshot deck parent graph has a cycle")
        seen.add(current)
        entry = deck_map.get(current)
        if entry is None:
            raise CompactNativeError("snapshot_parent_deck_missing", "snapshot omits a parent deck")
        result.append(strict_integer(int(current), "deck_id_invalid", minimum=1))
        parent = entry.get("parent_id")
        if parent in (None, "", 0, "0"):
            break
        current = str(parent)
    return tuple(result)


def _snapshot_deck_scope(
    collection: Any,
    snapshot: Mapping[str, Any],
    deck_id: str,
    context: Optional[SnapshotApplicationContext | AdoptedApplicationContext],
) -> tuple[int, ...]:
    """从冻结旁路确定历史父链；当前牌组 API 只用于核验身份与顺序。"""

    if context is None:
        deck_ids = _counter_deck_ids(snapshot, deck_id)
        if set(deck_ids) != {int(deck_id), *(int(row["id"]) for row in collection.decks.parents(int(deck_id)))}:
            raise CompactNativeError("snapshot_parent_deck_conflict", "快照父链与当前 Collection 不符")
        return deck_ids
    source = context.source
    ancestors = source.deck_ancestor_ids.get(deck_id)
    if ancestors is None or deck_id not in source.deck_contexts:
        raise CompactNativeError("snapshot_parent_deck_missing", "旁路缺少冻结牌组与父链")
    chain = (deck_id, *ancestors)
    live_parents = tuple(str(row["id"]) for row in reversed(collection.decks.parents(int(deck_id))))
    if tuple(ancestors) != live_parents:
        raise CompactNativeError("snapshot_parent_deck_conflict", "冻结祖先身份或顺序与当前 Collection 不符")
    for index, did in enumerate(chain):
        node = source.deck_contexts.get(did)
        if node is None or node["deck_id"] != did:
            raise CompactNativeError("snapshot_parent_deck_missing", "冻结祖先节点缺失")
        parent = chain[index + 1] if index + 1 < len(chain) else None
        if node["parent_id"] != parent:
            raise CompactNativeError("snapshot_parent_deck_conflict", "冻结祖先父关系不符")
        live = collection._backend.get_deck(int(did))
        if str(live.id) != did or live.name != node["name"]:
            raise CompactNativeError("snapshot_deck_name_conflict", "冻结牌组名称或身份与当前 Collection 不符")
    return tuple(int(did) for did in chain)


def _verify_snapshot_card(collection: Any, card: Any, snapshot_card: Mapping[str, Any],
                          application_context: Any, *, adopted_missing_lrt: bool = False) -> dict[str, Any]:
    from .application_context import capture_last_review_time_source
    source_map = getattr(application_context.source, "card_last_review_time_sources", {}) if application_context else {}
    frozen = source_map.get(str(card.id))
    if frozen is not None:
        try:
            current = capture_last_review_time_source(collection, card)
        except (ValueError, TypeError, AttributeError) as exc:
            raise CompactProjectionError("snapshot_last_review_source_unavailable", "复习时间来源不可用") from exc
        if dict(frozen) != current:
            raise CompactProjectionError("snapshot_last_review_source_conflict", "冻结的原生时间或评分历史已变化")
    return verify_card_snapshot(card, snapshot_card, adopted_missing_lrt=adopted_missing_lrt,
                                last_review_time_source=frozen)


def _verify_snapshot_deck_context(
    collection: Any,
    snapshot: Mapping[str, Any],
    deck_id: str,
    context: Optional[SnapshotApplicationContext | AdoptedApplicationContext],
    generation: int,
    expected_counters: Optional[Mapping[str, Any]],
) -> tuple[tuple[int, ...], Optional[dict[str, Any]]]:
    """计数只接受冻结帧或服务已验证的收据 overlay，不从 live 补基线。"""

    deck_ids = _snapshot_deck_scope(collection, snapshot, deck_id, context)
    if context is None:
        return deck_ids, None
    source = context.source
    if isinstance(context, AdoptedApplicationContext):
        if generation < source.collection_generation:
            raise CompactNativeError("snapshot_counter_generation_conflict", "当前代数早于采用来源")
        if expected_counters is not None:
            raise CompactNativeError("adopted_prestate_conflict", "采用规则不接受冻结计数 overlay")
        # 只读授权的 evidence_hash 绑定此当前帧；prepare 重算、intent.before
        # 和评分前完整 native_state 再验，之后原生事务原样保留该帧。
        return deck_ids, _capture_counters(collection, deck_ids)
    frozen = {
        did: {name: node[name] for name in DECK_COUNTER_FIELDS}
        for did, node in source.deck_contexts.items()
    }
    if generation < source.collection_generation:
        raise CompactNativeError("snapshot_counter_generation_conflict", "当前代数早于冻结基线")
    if expected_counters is None:
        if generation != source.collection_generation:
            raise CompactNativeError("snapshot_counter_receipt_path_missing", "当前代数需要验证冻结基线之后的收据链")
        expected_counters = frozen
    selected = {str(did): _plain(expected_counters.get(str(did))) for did in deck_ids}
    if generation == source.collection_generation and any(selected[did] != frozen[did] for did in selected):
        raise CompactNativeError("snapshot_counter_baseline_conflict", "同代数期望计数必须等于冻结原生帧")
    if _capture_counters(collection, deck_ids) != selected:
        raise CompactNativeError("snapshot_counter_baseline_conflict", "原生牌组计数与冻结基线及已验证收据不符")
    return deck_ids, selected


def _capture_counters(collection: Any, deck_ids: tuple[int, ...]) -> dict[str, dict[str, int]]:
    backend = getattr(collection, "_backend", None)
    getter = getattr(backend, "get_deck", None)
    if not callable(getter):
        raise CompactNativeError("deck_counter_api_unavailable", "native deck counter API is unavailable")
    counters: dict[str, dict[str, int]] = {}
    for deck_id in deck_ids:
        deck = getter(deck_id)
        common = getattr(deck, "common", None)
        if common is None:
            raise CompactNativeError("deck_counter_fields_missing", "deck common counters are unavailable")
        counters[str(deck_id)] = {
            field_name: int(getattr(common, field_name)) for field_name in DECK_COUNTER_FIELDS
        }
    return counters


def _capture_state(collection: Any, card_id: int, deck_ids: tuple[int, ...]) -> dict[str, Any]:
    card = collection.get_card(card_id)
    return {
        "card": _capture_card(card),
        "review_logs": _get_review_logs(collection, card_id),
        "counters": _capture_counters(collection, deck_ids),
        "native_card_hex": _capture_native_card_hex(collection, card_id),
        "native_revlogs": [
            list(row) for row in collection.db.all(
                "SELECT id,cid,usn,ease,ivl,lastIvl,factor,time,type FROM revlog WHERE cid=? ORDER BY id", card_id
            )
        ],
    }


def _stable_store_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Compact receipt shape shared by one event's after and the next before."""

    card = object_from_json(state.get("card"), "state_invalid")
    card.pop("schedule_revision", None)
    memory = card.get("memory_state")
    if isinstance(memory, Mapping):
        card["memory_state"] = {
            "stability": float(memory["stability"]),
            "difficulty": float(memory["difficulty"]),
        }
    logs = state.get("review_logs")
    if not isinstance(logs, (list, tuple)):
        raise CompactNativeError("review_logs_invalid", "stable state requires a review log list")
    return {
        "card": card,
        "review_logs": [object_from_json(row, "review_log_invalid") for row in logs],
        "counters": object_from_json(state.get("counters"), "counters_invalid"),
    }


def _stable_expected_after(prepared: PreparedCompactEvent) -> dict[str, Any]:
    logs = [dict(row) for row in prepared.before.get("review_logs", ())]
    logs.append(dict(prepared.expected_after["score_review_log"]))
    logs.sort(key=lambda row: (int(row["id"]), int(row["card_id"])))
    state = {
        "card": dict(prepared.expected_after["card"]),
        "review_logs": logs,
        "counters": prepared.expected_after["counters"],
    }
    return _stable_store_state(state)


def _stable_expected_score_after(prepared: PreparedCompactEvent) -> dict[str, Any]:
    logs = [dict(row) for row in prepared.before.get("review_logs", ())]
    logs.append(dict(prepared.expected_score_after["score_review_log"]))
    logs.sort(key=lambda row: (int(row["id"]), int(row["card_id"])))
    state = {
        "card": dict(prepared.expected_score_after["card"]),
        "review_logs": logs,
        "counters": prepared.expected_score_after["counters"],
    }
    return _stable_store_state(state)


def _stable_plan_score_after(plan: Mapping[str, Any]) -> dict[str, Any]:
    before = object_from_json(plan.get("before"), "before_invalid")
    score = object_from_json(plan.get("expected_score_after"), "expected_score_invalid")
    logs = [dict(row) for row in before.get("review_logs", ())]
    logs.append(dict(score["score_review_log"]))
    logs.sort(key=lambda row: (int(row["id"]), int(row["card_id"])))
    return _stable_store_state({"card": score["card"], "review_logs": logs, "counters": score["counters"]})


def _stable_plan_due_corrected(plan: Mapping[str, Any]) -> dict[str, Any]:
    score = object_from_json(plan.get("expected_score_after"), "expected_score_invalid")
    final_card = object_from_json(plan.get("expected_final_card"), "expected_final_invalid")
    logs = [dict(row) for row in object_from_json(plan.get("before"), "before_invalid").get("review_logs", ())]
    logs.append(dict(score["score_review_log"]))
    logs.sort(key=lambda row: (int(row["id"]), int(row["card_id"])))
    return _stable_store_state({"card": final_card, "review_logs": logs, "counters": score["counters"]})


def _stable_plan_final(plan: Mapping[str, Any]) -> dict[str, Any]:
    state = _stable_plan_due_corrected(plan)
    state["counters"] = object_from_json(plan.get("expected_final_counters"), "expected_counters_invalid")
    return state


def _preset_id_for(snapshot_deck: Mapping[str, Any]) -> int:
    value = snapshot_deck.get("preset_id", snapshot_deck.get("config_id"))
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CompactNativeError("preset_id_invalid", "snapshot preset id is invalid") from exc
    if isinstance(value, bool) or parsed < 1:
        raise CompactNativeError("preset_id_invalid", "snapshot preset id is invalid")
    return parsed


class AnkiCompactAdapter:
    """Anki 26.09.3 native read, apply, and evidence-gated recovery adapter."""

    @staticmethod
    def empty_changes() -> Any:
        """创建可直接返回给 CollectionOp 的 Anki 原生空 OpChanges。"""

        _collection, changes_type, _card_answer, _buildinfo = _anki_types()
        return changes_type()

    @staticmethod
    def state_for_store(state: Mapping[str, Any]) -> dict[str, Any]:
        """Return stable card/revlog/counter evidence for the compact receipt chain."""

        return _stable_store_state(state)

    @staticmethod
    def expected_after_for_store(prepared: PreparedCompactEvent) -> dict[str, Any]:
        return _stable_expected_after(prepared)

    @staticmethod
    def expected_score_after_for_store(prepared: PreparedCompactEvent) -> dict[str, Any]:
        return _stable_expected_score_after(prepared)

    def capability_report(self, collection: Any) -> CompactCapabilityReport:
        version, build = _version_values()
        reasons: list[str] = []
        exact = version == PINNED_ANKI_VERSION_CANONICAL and build == PINNED_ANKI_BUILD
        if not exact:
            reasons.append("unsupported_anki_version_or_build")
        methods = (
            (getattr(collection, "_backend", None), "db_begin", "Backend.db_begin"),
            (getattr(collection, "_backend", None), "db_commit", "Backend.db_commit"),
            (getattr(collection, "_backend", None), "db_rollback", "Backend.db_rollback"),
            (collection, "get_card", "Collection.get_card"),
            (collection, "update_card", "Collection.update_card"),
            (collection, "get_review_logs", "Collection.get_review_logs"),
            (getattr(collection, "sched", None), "answer_card", "Scheduler.answer_card"),
            (getattr(collection, "_backend", None), "get_scheduling_states", "Backend.get_scheduling_states"),
            (getattr(collection, "_backend", None), "get_deck", "Backend.get_deck"),
            (getattr(collection, "_backend", None), "update_deck", "Backend.update_deck"),
            (getattr(collection, "db", None), "all", "DBProxy.all (SELECT only)"),
            (collection, "add_custom_undo_entry", "Collection.add_custom_undo_entry"),
            (collection, "merge_undo_entries", "Collection.merge_undo_entries"),
        )
        official: list[str] = []
        for target, name, label in methods:
            if callable(getattr(target, name, None)):
                official.append(label)
            else:
                reasons.append(label.lower().replace(".", "_") + "_unavailable")
        return CompactCapabilityReport(
            anki_version=version,
            anki_build=build,
            exact=exact,
            write_enabled=not reasons,
            disabled_reasons=tuple(dict.fromkeys(reasons)),
            official_api=tuple(official),
        )

    def prepare_event(
        self,
        collection: Any,
        event: Any,
        snapshot: Any,
        authorization: CompactApplyAuthorization,
        *,
        collection_identity: str,
        collection_generation: int,
        intent_id: str,
        attempt_id: str,
        predecessor_after: Optional[Mapping[str, Any]] = None,
        expected_counters: Optional[Mapping[str, Any]] = None,
    ) -> PreparedCompactEvent:
        report = self.capability_report(collection)
        if not report.write_enabled:
            raise CompactNativeError(report.reason or "capability_disabled", "native compact write is disabled")
        event_map = _event_payload(event)
        event_map["card_index"] = int(event_map.get("card_index", 0))
        snapshot_map = _snapshot_payload(snapshot, str(event_map.get("snapshot_id", "")))
        application_context = _snapshot_application_context(snapshot, snapshot_map)
        manifest = object_from_json(snapshot_map.get("manifest", {}), "snapshot_manifest_invalid")

        recalculated = create_apply_authorization(
            collection,
            event_map,
            # Keep the SnapshotRecord wrapper: its typed context is an
            # immutable sidecar and is intentionally absent from snapshot_map.
            snapshot,
            collection_identity=collection_identity,
            collection_generation=collection_generation,
            mode=authorization.mode,
            legacy_source={"source_id": authorization.source_id, "source_sha256": authorization.source_sha256,
                           "event_id": authorization.event_id, "payload_hash": authorization.payload_hash,
                           "snapshot_id": authorization.snapshot_id, "card_id": authorization.card_id}
            if authorization.mode in ("legacy_restore", "adopted_restore")
            else None,
            predecessor_after=predecessor_after,
            expected_counters=expected_counters,
        )
        if recalculated != authorization:
            raise CompactNativeError("authorization_stale", "read-only authorization no longer matches the event")
        if intent_id == "" or attempt_id == "":
            raise CompactNativeError("intent_identity_missing", "intent and attempt ids are required")

        try:
            snapshot_card, snapshot_deck, snapshot_preset = resolve_snapshot_card(event_map, snapshot_map)
        except CompactProjectionError as exc:
            raise CompactNativeError(exc.code, exc.message) from exc
        card_id = strict_integer(int(event_map.get("card_id")), "card_id_invalid", minimum=1)
        card = collection.get_card(card_id)
        if predecessor_after is None:
            try:
                _verify_snapshot_card(collection, card, snapshot_card, application_context, adopted_missing_lrt=authorization.mode == "adopted_restore")
                before_card = _capture_card(card)
            except CompactProjectionError as exc:
                raise CompactNativeError(exc.code, exc.message) from exc
        else:
            predecessor = object_from_json(predecessor_after, "predecessor_invalid")
            predecessor_card = predecessor.get("card", predecessor)
            if str(predecessor_card.get("card_id", predecessor_card.get("id", ""))) != str(card_id):
                raise CompactNativeError("predecessor_card_mismatch", "predecessor belongs to another card")
            before_card = _capture_card(card)
            if _snapshot_supersedes_predecessor(predecessor, application_context):
                _verify_snapshot_card(collection, card, snapshot_card, application_context, adopted_missing_lrt=authorization.mode == "adopted_restore")
            else:
                _verify_predecessor_card(before_card, predecessor_card)
                if predecessor.get("native_card_hex") != _capture_native_card_hex(collection, card_id):
                    raise CompactNativeError("predecessor_native_state_conflict", "原始卡片状态与前序已应用收据不符")
        try:
            verify_scheduler_state(before_card)
            anki_rating, rating_name = map_rating(event_map.get("rating"))
        except CompactProjectionError as exc:
            raise CompactNativeError(exc.code, exc.message) from exc

        deck_id = str(getattr(card, "did"))
        if str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))) != deck_id:
            raise CompactNativeError("snapshot_deck_mismatch", "current card deck differs from snapshot")
        preset_id = _preset_id_for(snapshot_deck)
        current_config = _live_config(collection, card, preset_id)
        snapshot_config = _snapshot_config(
            snapshot_map,
            snapshot_preset,
            snapshot_card,
            preset_id,
            application_context,
            str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))),
        )
        fingerprint = json_sha256(current_config)
        if current_config != snapshot_config or fingerprint != authorization.preset_fingerprint:
            raise CompactNativeError("preset_snapshot_conflict", "effective preset changed after authorization")
        deck_ids, verified_counters = _verify_snapshot_deck_context(
            collection, snapshot_map, deck_id, application_context,
            collection_generation, expected_counters,
        )
        before = _capture_state(collection, card_id, deck_ids)
        if verified_counters is not None and before["counters"] != verified_counters:
            raise CompactNativeError("snapshot_counter_baseline_conflict", "只读准备期间牌组计数发生变化")
        if before["card"] != before_card:
            raise CompactNativeError("card_changed_during_preflight", "只读核验期间卡片状态发生变化")

        event_ms = strict_integer(event_map.get("answered_at_ms"), "answered_at_invalid", minimum=1)
        duration = strict_integer(event_map.get("duration_ms"), "duration_invalid", minimum=0)
        if event_ms > MAX_ANSWERED_AT_MS or duration > MAX_DURATION_MS:
            raise CompactNativeError("event_range_invalid", "event exceeds native answer field bounds")
        duration = min(duration, int(current_config["max_answer_seconds"]) * 1000)
        if duration > 0x7FFFFFFF or int(before["card"]["reps"]) >= 0xFFFFFFFF:
            raise CompactNativeError("native_counter_overflow_unsupported", "本事件超出原生计数字段的受支持范围")
        today, cutoff = _snapshot_day_values(collection, application_context)
        if (
            application_context is not None
            and event_ms // 1000 < int(application_context.source.collection_created_at_seconds)
        ):
            raise CompactNativeError("event_precedes_collection_creation", "事件早于快照绑定的 Collection 创建时间")
        event_day = collection_day(event_ms, today, cutoff)
        if application_context is not None:
            source = application_context.source
            frozen_event_day = collection_day(event_ms, int(source.scheduler_today), int(source.day_cutoff_seconds))
            if frozen_event_day != event_day:
                raise CompactNativeError("snapshot_event_day_conflict", "冻结与当前日界的历史映射不符")
            event_day = frozen_event_day
        if event_day > today:
            raise CompactNativeError("future_event_unsupported", "future compact events are unsupported")
        if event_day < 0:
            raise CompactNativeError("event_precedes_collection_creation", "事件早于 Collection 创建日")
        prior_review_seconds = int(before["card"]["last_review_time"])
        event_seconds = event_ms // 1000
        if event_seconds < prior_review_seconds:
            raise CompactNativeError("event_precedes_last_review", "event precedes current card review history")

        from .compact_transfer import project_native_answer
        transfer = project_native_answer(collection, before["card"], current_config,
                         {**event_map, "duration_ms": duration}, today=today, cutoff=cutoff, event_day=event_day)
        states = transfer["states"]
        selected_state = getattr(states, rating_name)
        _, _changes, card_answer_type, _buildinfo = _anki_types()
        answer = card_answer_type(card_id=card_id, current_state=states.current, new_state=selected_state,
                                  rating=anki_rating, answered_at_millis=event_ms, milliseconds_taken=duration)
        projection = transfer["projection"]
        expected_final_card = transfer["card"]
        expected_score_counters = copy.deepcopy(before["counters"])
        # 固定原生 answering stats：New 增 new，Review/DayLearn 增 review，分钟学习仅计时。
        field = "new_studied" if before["card"]["queue"] == 0 else "review_studied" if before["card"]["queue"] in (2, 3) else None
        for row in expected_score_counters.values():
            if row["last_day_studied"] != today:
                for key in DECK_COUNTER_FIELDS:
                    row[key] = 0
            if field is not None:
                row[field] += 1
            row["milliseconds_studied"] += duration
            row["last_day_studied"] = today
            if (field is not None and row[field] > 0x7FFFFFFF) or row["milliseconds_studied"] > 0x7FFFFFFF:
                raise CompactNativeError("native_counter_overflow_unsupported", "原生日计数将超出范围")
        historical = event_day < today or authorization.mode == "adopted_restore"
        expected_final_counters = copy.deepcopy(before["counters"] if historical else expected_score_counters)
        score_log = {key: transfer["log"][key] for key in REVIEW_LOG_FIELDS}
        if any(int(row["id"]) == event_ms for row in before["review_logs"]):
            raise CompactNativeError("revlog_id_collision", "event timestamp collides with a review log")
        # 外层事务只提交完整终态；阶段证据以该原子终态为评分完成见证。
        expected_score = {"card": dict(expected_final_card), "review_log_count": len(before["review_logs"]) + 1,
                          "score_review_log": score_log, "counters": expected_final_counters}
        core_plan = {
            "schema": "inkcards.compact.native-plan.v1",
            "atomic_final": True,
            "native_score_counters": expected_score_counters,
            "raw_score_factor": transfer["log"]["factor"],
            "expected_native_card": transfer["native_card"],
            "policy": authorization.to_dict(),
            "identity": _identity_summary(event_map, snapshot_map),
            "collection_identity": collection_identity,
            "collection_generation": collection_generation,
            "application_context_sha256": application_context.source_sha256 if application_context is not None else None,
            "snapshot_generation": application_context.snapshot_generation if application_context is not None else None,
            "intent_id": intent_id,
            "attempt_id": attempt_id,
            "event": {
                "rating": int(event_map["rating"]),
                "answered_at_ms": event_ms,
                "duration_ms": duration,
            },
            "preset": current_config,
            "preset_fingerprint": fingerprint,
            "before": before,
            "projected": {
                key: value for key, value in projection.items() if key != "native_after_state"
            },
            "native_current_state": _proto_dict(states.current),
            "native_after_state": _proto_dict(selected_state),
            "expected_score_after": expected_score,
            "expected_final_card": expected_final_card,
            "expected_final_counters": expected_final_counters,
            "required_evidence": ["score_applied", "due_corrected", "score_verified"],
            "source_kind": authorization.mode,
            "source_id": authorization.source_id,
            "source_sha256": authorization.source_sha256,
        }
        if authorization.mode == "adopted_restore":
            source = _record_value(snapshot, "adopted_source")
            core_plan.update({"context_kind": "adopted", "adopted_approval": source["approval"],
                              "adopted_basis": source["basis"], "original_snapshot_checksum": _record_value(snapshot, "checksum")})
        plan_hash_payload = copy.deepcopy(core_plan)
        plan_hash = json_sha256(plan_hash_payload)
        marker_binding = {
            "schema": MARKER_SCHEMA,
            "event_id": authorization.event_id,
            "payload_hash": authorization.payload_hash,
            "snapshot_id": authorization.snapshot_id,
            "attempt_id": attempt_id,
            "intent_id": intent_id,
            "source_id": authorization.source_id,
            "source_sha256": authorization.source_sha256,
            "collection_identity": collection_identity,
            "collection_generation": collection_generation,
            "collection_generation_after": collection_generation + 1,
            "plan_sha256": plan_hash,
            "before_sha256": json_sha256(before),
        }
        marker_digest = json_sha256(marker_binding)
        owned_marker_digest = None
        if predecessor_after is not None:
            predecessor = object_from_json(predecessor_after, "predecessor_invalid")
            predecessor_card = predecessor.get("card", predecessor)
            prior_custom = _custom_data_map(predecessor_card.get("custom_data"))
            owned_marker_digest = prior_custom.get("inkcards")
            if not isinstance(owned_marker_digest, str) or _HEX64.fullmatch(owned_marker_digest) is None:
                raise CompactNativeError("predecessor_marker_unproven", "preceding receipt does not own a valid inkcards marker")
            if _snapshot_supersedes_predecessor(predecessor, application_context) and _custom_data_map(before['card']['custom_data']).get('inkcards') is None:
                owned_marker_digest = None
        marked_custom_data = build_marker_custom_data(
            before["card"]["custom_data"],
            marker_digest,
            owned_marker_digest=owned_marker_digest,
        )
        answer.new_state.custom_data = marked_custom_data
        expected_score["card"]["custom_data"] = marked_custom_data
        expected_final_card["custom_data"] = marked_custom_data
        core_plan["expected_native_card"]["custom_data"] = marked_custom_data
        plan = {
            **core_plan,
            "plan_sha256": plan_hash,
            "plan_hash_payload": plan_hash_payload,
            "marker_binding": marker_binding,
            "marker_sha256": marker_digest,
            "expected_score_after": expected_score,
            "expected_final_card": expected_final_card,
            "expected_final_counters": expected_final_counters,
        }
        expected_after = {
            "card": expected_final_card,
            "review_log_count": len(before["review_logs"]) + 1,
            "score_review_log": score_log,
            "counters": expected_final_counters,
        }
        policy_json = {
            "schema": "inkcards.compact.policy.v1",
            "authorization": authorization.to_dict(),
            "mode": authorization.mode,
            "policy_id": authorization.policy_id,
            "source_id": authorization.source_id,
            "source_sha256": authorization.source_sha256,
            "evidence_sha256": authorization.evidence_sha256,
        }
        return PreparedCompactEvent(
            event=event_map,
            snapshot=snapshot_map,
            authorization=authorization,
            intent_id=intent_id,
            attempt_id=attempt_id,
            collection_identity=collection_identity,
            collection_generation=collection_generation,
            policy_json=policy_json,
            before=before,
            plan=plan,
            expected_score_after=expected_score,
            expected_after=expected_after,
            project_info={key: value for key, value in projection.items() if key != "native_after_state"},
            _answer=answer,
            _answer_card=collection.get_card(card_id),
        )

    def restore_prepared_event(
        self,
        collection: Any,
        event: Any,
        application: Any,
    ) -> PreparedCompactEvent:
        """Rebuild an answer from the durable plan without reading mutable scheduler output."""

        event_map = _event_payload(event)
        plan = object_from_json(_record_value(application, "plan_json"), "plan_invalid")
        before = object_from_json(_record_value(application, "before_json"), "before_invalid")
        expected_store_after = object_from_json(
            _record_value(application, "expected_after_json"), "expected_after_invalid"
        )
        intent_id = str(_record_value(application, "intent_id", ""))
        attempt_id = str(_record_value(application, "attempt_id", ""))
        if not intent_id or not attempt_id:
            raise CompactNativeError("intent_identity_missing", "durable compact intent is incomplete")
        if (
            str(_record_value(application, "event_id", "")) != str(event_map.get("event_id", ""))
            or str(_record_value(application, "payload_hash", "")) != str(event_map.get("payload_hash", ""))
        ):
            raise CompactNativeError("event_binding_invalid", "durable intent names another compact event")
        payload = plan.get("plan_hash_payload")
        if not isinstance(payload, Mapping) or json_sha256(payload) != plan.get("plan_sha256"):
            raise CompactNativeError("plan_hash_mismatch", "durable native plan hash is invalid")
        if _stable_store_state(plan.get("before", {})) != before:
            raise CompactNativeError("raw_before_binding_invalid", "原始 plan 前态与 Store 收据前态不符")
        restored_core = copy.deepcopy({
            key: value for key, value in plan.items()
            if key not in {"plan_sha256", "plan_hash_payload", "marker_binding", "marker_sha256"}
        })
        for key in ("expected_score_after", "expected_final_card"):
            card_value = restored_core[key]["card"] if key == "expected_score_after" else restored_core[key]
            card_value["custom_data"] = plan["before"]["card"]["custom_data"]
        if "expected_native_card" in restored_core:
            original_custom_data = plan["before"]["card"]["custom_data"]
            if original_custom_data:
                restored_core["expected_native_card"]["custom_data"] = original_custom_data
            else:
                # MessageToDict 省略空 proto3 字符串；恢复 hash 输入也须保持该规范。
                restored_core["expected_native_card"].pop("custom_data", None)
        if restored_core != payload:
            raise CompactNativeError("plan_payload_conflict", "持久计划与 marker 绑定的原始计划不符")
        binding = plan.get("marker_binding")
        if (
            not isinstance(binding, Mapping)
            or json_sha256(binding) != plan.get("marker_sha256")
            or binding.get("intent_id") != intent_id
            or binding.get("attempt_id") != attempt_id
            or binding.get("event_id") != event_map.get("event_id")
            or binding.get("payload_hash") != event_map.get("payload_hash")
            or binding.get("plan_sha256") != plan.get("plan_sha256")
            or binding.get("before_sha256") != json_sha256(plan["before"])
            or binding.get("collection_identity") != _record_value(application, "collection_identity")
            or binding.get("collection_generation") != _record_value(application, "collection_generation")
        ):
            raise CompactNativeError("marker_plan_binding_invalid", "durable atomic marker is not bound to this intent")
        auth_value = plan.get("policy")
        if not isinstance(auth_value, Mapping):
            raise CompactNativeError("authorization_invalid", "durable plan has no authorization")
        try:
            authorization = CompactApplyAuthorization(**dict(auth_value))
        except (TypeError, ValueError) as exc:
            raise CompactNativeError("authorization_invalid", "durable authorization is malformed") from exc
        identity = plan.get("identity", {})
        if (
            str(identity.get("event_id", "")) != str(event_map.get("event_id", ""))
            or str(identity.get("payload_hash", "")) != str(event_map.get("payload_hash", ""))
            or str(identity.get("card_id", "")) != str(event_map.get("card_id", ""))
            or authorization.event_id != str(event_map.get("event_id", ""))
            or authorization.payload_hash != str(event_map.get("payload_hash", ""))
        ):
            raise CompactNativeError("plan_event_mismatch", "durable scheduler plan differs from compact event")
        try:
            from anki.scheduler_pb2 import CardAnswer  # type: ignore[import-not-found]
            from google.protobuf.json_format import ParseDict

            answer = CardAnswer(card_id=int(event_map["card_id"]))
            ParseDict(plan["native_current_state"], answer.current_state)
            ParseDict(plan["native_after_state"], answer.new_state)
            answer.rating, _ = map_rating(event_map.get("rating"))
            answer.answered_at_millis = int(event_map["answered_at_ms"])
            answer.milliseconds_taken = int(plan["event"]["duration_ms"])
            answer.new_state.custom_data = str(plan["expected_score_after"]["card"]["custom_data"])
        except CompactNativeError:
            raise
        except Exception as exc:
            raise CompactNativeError("durable_native_state_invalid", "persisted native scheduling state is invalid") from exc
        expected_score = object_from_json(plan.get("expected_score_after"), "expected_score_invalid")
        expected_final_card = object_from_json(plan.get("expected_final_card"), "expected_final_invalid")
        expected_counters = object_from_json(plan.get("expected_final_counters"), "expected_counters_invalid")
        expected_after = {
            "card": expected_final_card,
            "review_log_count": int(expected_score["review_log_count"]),
            "score_review_log": expected_score["score_review_log"],
            "counters": expected_counters,
        }
        if _stable_expected_after(
            PreparedCompactEvent(
                event=event_map,
                snapshot={},
                authorization=authorization,
                intent_id=intent_id,
                attempt_id=attempt_id,
                collection_identity=str(_record_value(application, "collection_identity", "")),
                collection_generation=int(_record_value(application, "collection_generation", -1)),
                policy_json={},
                before=plan["before"],
                plan=plan,
                expected_score_after=expected_score,
                expected_after=expected_after,
                project_info=plan.get("projected", {}),
                _answer=answer,
                _answer_card=collection.get_card(int(event_map["card_id"])),
            )
        ) != expected_store_after:
            raise CompactNativeError("expected_after_mismatch", "durable final state differs from its receipt plan")
        return PreparedCompactEvent(
            event=event_map,
            snapshot={},
            authorization=authorization,
            intent_id=intent_id,
            attempt_id=attempt_id,
            collection_identity=str(_record_value(application, "collection_identity", "")),
            collection_generation=int(_record_value(application, "collection_generation", -1)),
            policy_json=object_from_json(_record_value(application, "policy_json"), "policy_invalid"),
            before=plan["before"],
            plan=plan,
            expected_score_after=expected_score,
            expected_after=expected_after,
            project_info=plan.get("projected", {}),
            _answer=answer,
            _answer_card=collection.get_card(int(event_map["card_id"])),
        )

    def _project_native_states(
        self,
        collection: Any,
        card_id: int,
        before_card: Mapping[str, Any],
        preset_config: Mapping[str, Any],
        *,
        today: int,
        event_day: int,
        answered_at_seconds: int,
        synthetic_now_seconds: int,
    ) -> Any:
        backend = getattr(collection, "_backend", None)
        getter = getattr(backend, "get_scheduling_states", None)
        if not callable(getter):
            raise CompactNativeError("scheduling_states_unavailable", "Backend.get_scheduling_states is unavailable")
        last_review = int(before_card["last_review_time"])
        elapsed_at_event = answered_at_seconds - last_review
        if elapsed_at_event < 0:
            raise CompactNativeError("event_precedes_last_review", "event precedes current card history")
        if int(getattr(collection.get_card(card_id), "odid", 0) or 0):
            raise CompactNativeError("filtered_card_unsupported", "filtered-deck cards are not supported")
        # Only this new temporary collection is written. The active collection is
        # queried for inputs but never used as a projection scratchpad. The fixed
        # ordinary policy always disables LB in scratch, independent of the active
        # collection's user-level setting.
        try:
            from anki.cards import FSRSMemoryState
            from anki.collection import Collection, Config

            with tempfile.TemporaryDirectory(prefix="inkcards-compact-projection-") as root:
                scratch = Collection(str(Path(root) / "projection.anki2"))
                try:
                    # In 26.09.3 Collection.crt is read-only. Align that metadata
                    # only in this disposable collection, and set clock config
                    # through the public Collection API.
                    for key in ("rollover", "localOffset", "creationOffset"):
                        value = collection.get_config(key)
                        if value is not None:
                            scratch.set_config(key, value)
                    scratch.db.execute("update col set crt=?", int(collection.crt))
                    if (
                        int(scratch.sched.today) != today
                        or int(scratch.sched.day_cutoff) != int(collection.sched.day_cutoff)
                    ):
                        raise CompactNativeError(
                            "projection_clock_mismatch",
                            "synthetic scheduler day/cutoff differs after clock alignment",
                        )
                    scratch.set_config("fsrs", True)
                    scratch.set_config_bool(Config.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED, collection.fsrs_short_term_with_steps_enabled)
                    scratch_config = scratch.decks.config_dict_for_deck_id(1)
                    scratch_config["fsrsParams6"] = list(preset_config["weights"])
                    scratch_config["desiredRetention"] = float(preset_config["desired_retention"])
                    scratch_config.setdefault("rev", {})["maxIvl"] = int(preset_config["max_interval_days"])
                    scratch_config.setdefault("new", {})["delays"] = list(preset_config["learning_steps"])
                    scratch_config.setdefault("lapse", {})["delays"] = list(preset_config["relearning_steps"])
                    scratch_config["maxTaken"] = int(preset_config["max_answer_seconds"])
                    scratch.decks.save(scratch_config)
                    scratch._backend.set_load_balancer_enabled(False)

                    note_type = scratch.models.by_name("Basic")
                    if note_type is None:
                        raise CompactNativeError("projection_notetype_missing", "synthetic Basic note type is unavailable")
                    note = scratch.new_note(note_type)
                    note.fields[0] = "inkcards projection front"
                    if len(note.fields) > 1:
                        note.fields[1] = "inkcards projection back"
                    scratch.add_note(note, 1)
                    generated_ids = list(scratch.card_ids_of_note(note.id))
                    if len(generated_ids) != 1:
                        raise CompactNativeError("projection_card_creation_failed", "synthetic collection did not create one card")
                    generated_id = int(generated_ids[0])
                    # The pinned backend only assigns generated IDs in add_note();
                    # re-keying is confined to this isolated one-card collection.
                    scratch.db.execute("update cards set id=? where id=?", card_id, generated_id)
                    card = scratch.get_card(card_id)
                    card.type = 2
                    card.queue = 2
                    card.due = int(scratch.sched.today) + int(before_card["due"]) - event_day
                    card.ivl = int(before_card["ivl"])
                    card.factor = int(before_card["factor"])
                    card.reps = int(before_card["reps"])
                    card.lapses = int(before_card["lapses"])
                    card.left = int(before_card["left"])
                    card.last_review_time = synthetic_now_seconds - elapsed_at_event
                    memory = before_card.get("memory_state")
                    if not isinstance(memory, Mapping):
                        raise CompactNativeError("memory_state_missing", "synthetic projection requires FSRS memory")
                    card.memory_state = FSRSMemoryState(
                        stability=float(memory["stability"]),
                        difficulty=float(memory["difficulty"]),
                    )
                    scratch.update_card(card, skip_undo_entry=True)
                    states = scratch._backend.get_scheduling_states(card_id)
                    return states
                finally:
                    scratch.close()
        except CompactNativeError:
            raise
        except Exception as exc:
            raise CompactNativeError("synthetic_projection_failed", "isolated native projection failed") from exc

    def apply_prepared(self, collection: Any, prepared: PreparedCompactEvent, *, intent: Any,
                       record_stage: Optional[Callable[[str, Mapping[str, Any]], Any]] = None) -> CompactNativeResult:
        if not prepared.plan.get("atomic_final"):
            return self._apply_legacy_prepared(collection, prepared, intent=intent, record_stage=record_stage)
        accumulator = _OpChangesAccumulator()
        backend = collection._backend
        begun = False
        committed = False
        calls = 0
        final = None
        card_id = int(prepared.event["card_id"])
        deck_ids = tuple(int(key) for key in prepared.before["counters"])
        try:
            if (str(_record_value(intent, "intent_id", "")) != prepared.intent_id
                or str(_record_value(intent, "attempt_id", "")) != prepared.attempt_id
                or _record_value(intent, "plan_json") != canonical_json(prepared.plan)
                or _record_value(intent, "before_json") != canonical_json(_stable_store_state(prepared.before))
                or _record_value(intent, "expected_after_json") != canonical_json(_stable_expected_after(prepared))):
                raise CompactNativeError("intent_binding_mismatch", "原生计划与持久 intent 不符")
            if not _state_matches_before(_capture_state(collection, card_id, deck_ids), prepared.before):
                raise CompactNativeError("native_state_changed_before_score", "评分前完整卡片/日志/计数已变化")
            config = _live_config(collection, collection.get_card(card_id), int(prepared.plan["preset"]["preset_id"]))
            if json_sha256(config) != prepared.authorization.preset_fingerprint:
                raise CompactNativeError("preset_changed_before_score", "评分前冻结预设已变化")
            backend.db_begin()
            begun = True
            undo = int(collection.add_custom_undo_entry("InkCards compact review"))
            accumulator.add(collection.update_card(collection.get_card(card_id)))
            prepared._answer.current_state.CopyFrom(backend.get_scheduling_states(card_id).current)
            calls = 1
            accumulator.add(collection.sched.answer_card(prepared._answer))
            scored = _capture_state(collection, card_id, deck_ids)
            # native 使用宿主 now 写 due；其余状态、完整旧日志和新日志必须精确吻合。
            core_card = {key:value for key,value in prepared.expected_after["card"].items() if key != "due"}
            expected_logs = _stable_expected_after(prepared)["review_logs"]
            old_raw = prepared.before["native_revlogs"]
            new_raw = scored["native_revlogs"]
            actual_event = [row for row in new_raw if int(row[0]) == int(prepared.event["answered_at_ms"])]
            if (not _card_schedule_matches(scored["card"], core_card)
                or scored["review_logs"] != expected_logs
                or [row for row in new_raw if int(row[0]) != int(prepared.event["answered_at_ms"])] != old_raw
                or len(actual_event) != 1 or int(actual_event[0][6]) != int(prepared.plan["raw_score_factor"])
                or scored["counters"] != prepared.plan["native_score_counters"]):
                raise CompactNativeError("native_score_differs_from_persisted_plan", "原生评分与冻结移植计划不符")
            card = collection.get_card(card_id)
            if int(card.due) != int(prepared.expected_after["card"]["due"]):
                card.due = int(prepared.expected_after["card"]["due"])
                accumulator.add(collection.update_card(card))
            if scored["counters"] != prepared.expected_after["counters"]:
                accumulator.add(self._restore_historical_counters(collection, prepared.expected_after["counters"], deck_ids))
            final = _capture_state(collection, card_id, deck_ids)
            if not _final_matches(prepared, final):
                raise CompactNativeError("native_final_state_differs_from_persisted_plan", "原生终态未达到冻结计划")
            accumulator.add(collection.merge_undo_entries(undo))
            backend.db_commit()
            begun = False
            committed = True
            # commit 后重新读回；只此持久终态可发布到另一个 Store 数据库。
            final = _capture_state(collection, card_id, deck_ids)
            if not _final_matches(prepared, final):
                raise CompactNativeError("native_commit_readback_mismatch", "提交后终态读回不符")
            if record_stage is not None:
                for stage in ("score_applied", "due_corrected", "score_verified"):
                    record_stage(stage, final)
            return CompactNativeResult("applied", None, prepared.before, final, final, accumulator.value,
                                       mutated=True, answer_card_calls=1)
        except Exception as exc:
            if begun:
                backend.db_rollback()
            if committed:
                return CompactNativeResult("unknown", getattr(exc, "code", "native_failure_after_commit"),
                          prepared.before, final, final, accumulator.value, mutated=True, answer_card_calls=calls)
            # rollback 清除持久变更；Undo 是进程内状态，发生评分尝试后要求重开恢复。
            raise CompactNativeError(getattr(exc, "code", "native_transaction_rolled_back"), "原生事务未提交",
                 status="unknown" if calls else "blocked", mutated=False, changes=self.empty_changes()) from exc

    def _apply_legacy_prepared(
        self,
        collection: Any,
        prepared: PreparedCompactEvent,
        *,
        intent: Any,
        record_stage: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
    ) -> CompactNativeResult:
        accumulator = _OpChangesAccumulator()
        scored = False
        answer_call_started = False
        score_after: Optional[dict[str, Any]] = None
        try:
            if (
                str(_record_value(intent, "intent_id", "")) != prepared.intent_id
                or str(_record_value(intent, "attempt_id", "")) != prepared.attempt_id
                or _record_value(intent, "plan_json") != canonical_json(prepared.plan)
                or _record_value(intent, "before_json") != canonical_json(_stable_store_state(prepared.before))
                or _record_value(intent, "expected_after_json") != canonical_json(_stable_expected_after(prepared))
            ):
                raise CompactNativeError("intent_binding_mismatch", "native attempt differs from the persisted intent")
            card_id = int(prepared.event["card_id"])
            deck_ids = tuple(int(key) for key in prepared.before["counters"])
            current_before = _capture_state(collection, card_id, deck_ids)
            if not _state_matches_before(current_before, prepared.before):
                raise CompactNativeError("native_state_changed_before_score", "card, counters, or review history changed after preflight")
            live_card = collection.get_card(card_id)
            live_config = _live_config(collection, live_card, int(prepared.plan["preset"]["preset_id"]))
            if json_sha256(live_config) != prepared.authorization.preset_fingerprint:
                raise CompactNativeError("preset_changed_before_score", "effective preset changed after preflight")
            current_state_reader = getattr(collection._backend, "get_scheduling_states", None)
            if not callable(current_state_reader):
                raise CompactNativeError("scheduling_states_unavailable", "native scheduler state is unavailable")
            current_states = current_state_reader(card_id)
            prepared._answer.current_state.CopyFrom(current_states.current)
            undo_target = int(collection.add_custom_undo_entry("InkCards compact review"))
            prepared = PreparedCompactEvent(
                **{**prepared.__dict__, "_undo_target": undo_target}
            )
            # update_card invalidates any old reviewer queue before direct answer_card.
            current = collection.get_card(card_id)
            accumulator.add(collection.update_card(current))
            current = collection.get_card(card_id)
            if _capture_card(current)["type"] != 2 or _capture_card(current)["queue"] != 2:
                raise CompactNativeError("card_state_changed", "card left ordinary Review before scoring")
            answer_card = getattr(collection.sched, "answer_card", None)
            if not callable(answer_card):
                raise CompactNativeError("answer_card_unavailable", "Scheduler.answer_card is unavailable")
            answer_call_started = True
            accumulator.add(answer_card(prepared._answer))
            scored = True
            score_after = _capture_state(collection, card_id, tuple(int(key) for key in prepared.before["counters"]))
            if not _score_matches(prepared, score_after):
                return CompactNativeResult(
                    "unknown",
                    "native_score_differs_from_persisted_plan",
                    prepared.before,
                    score_after,
                    None,
                    accumulator.value,
                    mutated=True,
                    answer_card_calls=1,
                )
            if record_stage is not None:
                record_stage("score_applied", score_after)
            card = collection.get_card(card_id)
            if int(card.due) != int(prepared.expected_after["card"]["due"]):
                card.due = int(prepared.expected_after["card"]["due"])
                accumulator.add(collection.update_card(card))
            due_corrected = _capture_state(
                collection, card_id, tuple(int(key) for key in prepared.before["counters"])
            )
            if record_stage is not None:
                record_stage("due_corrected", due_corrected)
            if prepared.project_info["event_collection_day"] < prepared.project_info["runtime_today"]:
                counters_now = _capture_counters(collection, tuple(int(key) for key in prepared.before["counters"]))
                if counters_now != prepared.expected_score_after["counters"]:
                    return CompactNativeResult(
                        "conflict",
                        "daily_counters_changed_after_native_answer",
                        prepared.before,
                        score_after,
                        None,
                        accumulator.value,
                        mutated=True,
                        answer_card_calls=1,
                    )
                accumulator.add(
                    self._restore_historical_counters(
                        collection,
                        prepared.before["counters"],
                        tuple(int(key) for key in prepared.before["counters"]),
                    )
                )
            final_after = _capture_state(collection, card_id, tuple(int(key) for key in prepared.before["counters"]))
            if not _final_matches(prepared, final_after):
                return CompactNativeResult(
                    "unknown",
                    "native_final_state_differs_from_persisted_plan",
                    prepared.before,
                    score_after,
                    final_after,
                    accumulator.value,
                    mutated=True,
                    answer_card_calls=1,
                )
            if record_stage is not None:
                record_stage("score_verified", final_after)
            if prepared._undo_target is not None:
                accumulator.add(collection.merge_undo_entries(prepared._undo_target))
            return CompactNativeResult(
                "applied",
                None,
                prepared.before,
                score_after,
                final_after,
                accumulator.value,
                mutated=True,
                answer_card_calls=1,
            )
        except Exception as exc:
            card_id = int(prepared.event["card_id"])
            try:
                observed = _capture_state(collection, card_id, tuple(int(key) for key in prepared.before["counters"]))
            except Exception:
                observed = None
            if answer_call_started or (observed is not None and _score_marker_matches(prepared, observed)):
                return CompactNativeResult(
                    "unknown",
                    getattr(exc, "code", "native_failure_after_possible_commit"),
                    prepared.before,
                    score_after or observed,
                    observed,
                    accumulator.value,
                    mutated=True,
                    answer_card_calls=1 if answer_call_started else 0,
                )
            raise CompactNativeError(
                getattr(exc, "code", "native_apply_failed"),
                "native compact application failed before a proven score",
                status=("unknown" if bool(accumulator.value.ListFields()) else getattr(exc, "status", "blocked")),
                mutated=bool(accumulator.value.ListFields()),
                changes=accumulator.value,
            ) from exc

    def _restore_historical_counters(
        self,
        collection: Any,
        before: Mapping[str, Mapping[str, int]],
        deck_ids: tuple[int, ...],
    ) -> Any:
        accumulator = _OpChangesAccumulator()
        backend = collection._backend
        for deck_id in deck_ids:
            deck = backend.get_deck(deck_id)
            common = deck.common
            expected = before[str(deck_id)]
            changed = False
            for name in DECK_COUNTER_FIELDS:
                value = int(expected[name])
                if int(getattr(common, name)) != value:
                    setattr(common, name, value)
                    changed = True
            if changed:
                accumulator.add(backend.update_deck(deck))
        return accumulator.value

    def inspect_recovery(
        self,
        collection: Any,
        event: Any,
        snapshot: Any,
        application: Any,
        evidence: Any,
        *,
        collection_identity: str,
        collection_generation: int,
    ) -> RecoveryDecision:
        app = _plain(application)
        app = object_from_json(app, "application_invalid")
        plan = object_from_json(_record_value(application, "plan_json"), "plan_invalid")
        before = object_from_json(_record_value(application, "before_json"), "before_invalid")
        status = str(app.get("status", ""))
        intent_id = str(app.get("intent_id", ""))
        attempt_id = str(app.get("attempt_id", ""))
        if not intent_id or not attempt_id or not bool(app.get("recovery_required", False)):
            return RecoveryDecision("conflict", "compact_recovery_intent_incomplete", {"status": status})
        if str(app.get("collection_identity", "")) != collection_identity:
            return RecoveryDecision("conflict", "collection_identity_changed", {"status": status})
        base_generation = int(app.get("collection_generation", -1))
        expected_generation_after = int(plan.get("marker_binding", {}).get("collection_generation_after", -1))
        if expected_generation_after != base_generation + 1:
            return RecoveryDecision("conflict", "collection_generation_step_invalid", {})
        # applied 收据由服务直接返回 idle；未闭合 intent 只能在其原代数恢复。
        if collection_generation != base_generation:
            return RecoveryDecision(
                "conflict",
                "collection_generation_changed",
                {"expected": base_generation, "observed": collection_generation},
            )
        event_map = _event_payload(event)
        snapshot_map = _snapshot_payload(snapshot, str(event_map.get("snapshot_id", "")))
        application_context = _snapshot_application_context(snapshot, snapshot_map)
        card_id = int(event_map["card_id"])
        deck_ids = tuple(int(key) for key in before.get("counters", {}))
        current = _capture_state(collection, card_id, deck_ids)
        # Store 的三字段稳定帧用于跨事件链；恢复是否尚未评分必须独立核验
        # plan 中包含原始 mod、完整 protobuf 位型和原始 revlog 的前态。
        expected_before = object_from_json(plan.get("before"), "raw_before_invalid")
        if _stable_store_state(expected_before) != before:
            return RecoveryDecision("conflict", "raw_before_binding_invalid", {})
        try:
            report = self.capability_report(collection)
            if not report.write_enabled:
                raise CompactNativeError(report.reason or "capability_disabled", "原生接口不可用")
            snapshot_card, snapshot_deck, snapshot_preset = resolve_snapshot_card(event_map, snapshot_map)
            _profile_binding(object_from_json(snapshot_map.get("manifest", {}), "snapshot_manifest_invalid"), collection_identity)
            if application_context is None:
                raise CompactNativeError("snapshot_application_context_missing", "恢复必须使用冻结或核验的历史上下文")
            if plan.get("policy", {}).get("mode") in ("legacy_restore", "adopted_restore") and (
                _record_value(snapshot, "restoration_source_sha256") != plan["policy"].get("source_sha256")
            ):
                raise CompactNativeError("legacy_source_changed", "恢复来源与已持久计划不符")
            _snapshot_day_values(collection, application_context)
            frozen_scope = _snapshot_deck_scope(
                collection, snapshot_map,
                str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))),
                application_context,
            )
            if set(frozen_scope) != set(deck_ids) or str(current["card"]["deck_id"]) != str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))):
                raise CompactNativeError("snapshot_parent_deck_conflict", "持久计划或当前卡片牌组范围与冻结父链不符")
            if application_context is not None and (
                plan.get("application_context_sha256") != application_context.source_sha256
                or plan.get("snapshot_generation") != application_context.snapshot_generation
            ):
                raise CompactNativeError("snapshot_application_context_binding_mismatch", "恢复计划的冻结 context/generation 绑定不符")
            preset_id = _preset_id_for(snapshot_deck)
            live_config = _live_config(collection, collection.get_card(card_id), preset_id)
            snapshot_config = _snapshot_config(
                snapshot_map,
                snapshot_preset,
                snapshot_card,
                preset_id,
                application_context,
                str(snapshot_deck.get("deck_id", snapshot_deck.get("id", ""))),
            )
            if live_config != snapshot_config or json_sha256(live_config) != plan.get("preset_fingerprint"):
                raise CompactNativeError("preset_changed_during_recovery", "恢复时有效预设已变化")
            if int(collection.sched.today) != int(plan["projected"]["runtime_today"]):
                raise CompactNativeError("recovery_rollover_unsupported", "恢复已跨 Collection 日界，日计数不能沿用旧计划")
        except (CompactNativeError, CompactProjectionError) as exc:
            return RecoveryDecision("conflict", exc.code, {}, observed_before=current)
        proof = self._recovery_binding(application, plan, event_map, collection_identity)
        marker_body = plan.get("marker_binding")
        marker_expected = str(plan.get("marker_sha256", ""))
        marker_valid = (
            isinstance(marker_body, Mapping)
            and _HEX64.fullmatch(marker_expected) is not None
            and json_sha256(marker_body) == marker_expected
            and marker_body.get("intent_id") == intent_id
            and marker_body.get("attempt_id") == attempt_id
            and marker_body.get("event_id") == event_map.get("event_id")
            and marker_body.get("payload_hash") == event_map.get("payload_hash")
            and marker_body.get("collection_identity") == collection_identity
            and int(marker_body.get("collection_generation", -1)) == base_generation
        )
        custom = _custom_data_map(current["card"].get("custom_data"))
        marker_present = "inkcards" in custom
        observed_marker = custom.get("inkcards")
        marker_on_card = marker_valid and observed_marker == marker_expected
        score_started = self._evidence_for(evidence, "score_started")
        score_evidence = self._evidence_for(evidence, "score_applied")
        if _state_matches_before(current, expected_before):
            if score_started is not None and not self._started_evidence_matches(score_started, plan):
                return RecoveryDecision(
                    "conflict",
                    "score_started_binding_conflicts_with_plan",
                    {**proof, "score_started": True, "marker_present": marker_present},
                    observed_before=current,
                )
            return RecoveryDecision(
                "not_started",
                "native_before_state_proves_not_started",
                {
                    **proof,
                    "marker_present": marker_present,
                    "review_log_count": len(current["review_logs"]),
                    "score_started": score_started is not None,
                    "before_state_exact": True,
                },
                observed_before=current,
                marker_valid=marker_valid,
            )
        if not marker_on_card:
            classification = "unknown" if marker_present or score_evidence is not None else "ambiguous"
            return RecoveryDecision(
                classification,
                "score_marker_missing_or_foreign",
                {**proof, "marker_present": marker_present, "marker_valid": marker_valid},
                observed_before=current,
                marker_valid=False,
            )
        if plan.get("atomic_final") and not _raw_card_matches(current, plan):
            return RecoveryDecision("conflict", "native_card_proto_conflicts_with_plan", proof, observed_before=current)
        expected_score = object_from_json(plan.get("expected_score_after"), "expected_score_invalid")
        expected_final_card = object_from_json(plan.get("expected_final_card"), "expected_final_invalid")
        expected_score_counters = object_from_json(expected_score.get("counters"), "expected_counters_invalid")
        expected_final_counters = object_from_json(plan.get("expected_final_counters"), "expected_counters_invalid")
        if not _partial_score_matches(current, expected_score, expected_final_card, expected_score_counters, expected_final_counters):
            return RecoveryDecision(
                "conflict",
                "native_score_state_conflicts_with_marker_plan",
                {**proof, "marker_valid": True},
                observed_before=current,
                marker_valid=True,
                score_proven=False,
            )
        if current["review_logs"] != _stable_plan_score_after(plan)["review_logs"]:
            return RecoveryDecision("conflict", "native_review_history_conflicts_with_plan", proof, observed_before=current)
        if score_evidence is not None and not self._score_evidence_matches(score_evidence, plan, current):
            return RecoveryDecision(
                "conflict",
                "durable_score_evidence_conflicts_with_native_state",
                {**proof, "marker_valid": True, "score_evidence": True},
                observed_before=current,
                marker_valid=True,
            )
        for stage in ("due_corrected", "score_verified"):
            saved = self._evidence_for(evidence, stage)
            if saved is not None and not self._stage_evidence_matches(stage, saved, plan):
                return RecoveryDecision(
                    "conflict",
                    "durable_%s_evidence_conflicts_with_native_plan" % stage,
                    {**proof, "marker_valid": True, "evidence_stage": stage},
                    observed_before=current,
                    marker_valid=True,
                )
        if score_evidence is None and _stable_store_state(current) != _stable_plan_score_after(plan):
            return RecoveryDecision(
                "ambiguous",
                "score_stage_readback_was_not_durably_observed",
                {**proof, "marker_valid": True, "score_evidence_present": False},
                observed_before=current,
                marker_valid=True,
            )
        return RecoveryDecision(
            "score_complete",
            "atomic_marker_and_native_score_match_plan",
            {**proof, "marker_valid": True, "score_evidence_present": score_evidence is not None},
            observed_before=current,
            # 没有评分阶段证据时，只允许当前仍为评分刚提交的完整读回。
            # 否则使用已校验的实际持久读回，而不制造过去阶段的观测值。
            score_after=score_evidence.get("native_observed", score_evidence["observed"]) if score_evidence is not None else current,
            final_after={
                "card": expected_final_card,
                "review_log_count": expected_score["review_log_count"],
                "score_review_log": expected_score["score_review_log"],
                "counters": expected_final_counters,
            },
            collection_generation_after=expected_generation_after,
            marker_valid=True,
            score_proven=True,
        )

    def repair_recovery(
        self,
        collection: Any,
        prepared: PreparedCompactEvent,
        decision: RecoveryDecision,
        *,
        record_stage: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
    ) -> CompactNativeResult:
        if decision.classification != "score_complete" or not decision.score_proven:
            raise CompactNativeError("recovery_not_proven", "recovery does not prove a committed native score")
        accumulator = _OpChangesAccumulator()
        card_id = int(prepared.event["card_id"])
        plan = prepared.plan
        current = _capture_state(collection, card_id, tuple(int(key) for key in prepared.before["counters"]))
        expected_score = plan["expected_score_after"]
        expected_final_card = plan["expected_final_card"]
        expected_score_counters = expected_score["counters"]
        expected_final_counters = plan["expected_final_counters"]
        if not _state_matches_before(current, decision.observed_before or {}) or not _partial_score_matches(
            current,
            expected_score,
            expected_final_card,
            expected_score_counters,
            expected_final_counters,
        ):
            return CompactNativeResult(
                "conflict",
                "native_state_changed_before_recovery_repair",
                prepared.before,
                decision.score_after,
                None,
                accumulator.value,
                mutated=False,
                answer_card_calls=0,
            )
        try:
            need_due = int(current["card"]["due"]) != int(expected_final_card["due"])
            need_counters = current["counters"] != expected_final_counters
            undo_target = collection.add_custom_undo_entry("InkCards compact repair") if need_due or need_counters else None
            if need_due:
                card = collection.get_card(card_id)
                card.due = int(expected_final_card["due"])
                accumulator.add(collection.update_card(card))
            due_corrected = _capture_state(
                collection, card_id, tuple(int(key) for key in prepared.before["counters"])
            )
            if record_stage is not None:
                record_stage("due_corrected", due_corrected)
            if need_counters:
                accumulator.add(
                    self._restore_historical_counters(
                        collection, expected_final_counters,
                        tuple(int(key) for key in prepared.before["counters"]),
                    )
                )
            final = _capture_state(collection, card_id, tuple(int(key) for key in prepared.before["counters"]))
            if not _final_matches(prepared, final):
                return CompactNativeResult(
                    "unknown", "recovery_repair_did_not_reach_final_plan", prepared.before,
                    decision.score_after, final, accumulator.value,
                    mutated=bool(accumulator.value.ListFields()), answer_card_calls=0,
                )
            if record_stage is not None:
                record_stage("score_verified", final)
            if undo_target is not None:
                accumulator.add(collection.merge_undo_entries(undo_target))
            return CompactNativeResult(
                "applied", "recovered_without_rescoring", prepared.before, decision.score_after,
                final, accumulator.value, mutated=bool(accumulator.value.ListFields()), answer_card_calls=0,
            )
        except Exception as exc:
            try:
                final = _capture_state(collection, card_id, tuple(int(key) for key in prepared.before["counters"]))
            except Exception:
                final = None
            return CompactNativeResult(
                "unknown", getattr(exc, "code", "native_recovery_repair_failed"), prepared.before,
                decision.score_after, final, accumulator.value,
                mutated=bool(accumulator.value.ListFields()), answer_card_calls=0,
            )

    @staticmethod
    def _recovery_binding(application: Any, plan: Mapping[str, Any], event: Mapping[str, Any], identity: str) -> dict[str, Any]:
        return {
            "intent_id": str(_record_value(application, "intent_id", "")),
            "attempt_id": str(_record_value(application, "attempt_id", "")),
            "plan_sha256": str(plan.get("plan_sha256", "")),
            "event_id": str(event.get("event_id", "")),
            "payload_hash": str(event.get("payload_hash", "")),
            "collection_identity": identity,
        }

    @staticmethod
    def _evidence_for(evidence: Any, stage: str) -> Optional[dict[str, Any]]:
        rows = evidence or ()
        for row in rows:
            if str(_record_value(row, "stage", "")) == stage:
                raw = _record_value(row, "evidence_json")
                digest = _record_value(row, "evidence_hash")
                if not isinstance(raw, str) or hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
                    raise CompactNativeError("score_evidence_hash_invalid", "持久阶段证据摘要不符")
                return object_from_json(raw, "score_evidence_invalid")
        return None

    @staticmethod
    def _score_evidence_matches(
        evidence: Mapping[str, Any], plan: Mapping[str, Any], current: Mapping[str, Any]
    ) -> bool:
        binding = evidence.get("binding")
        observed = evidence.get("observed")
        if not isinstance(binding, Mapping) or not isinstance(observed, Mapping):
            return False
        marker_binding = plan.get("marker_binding")
        binding_ok = (
            binding.get("plan_hash") == json_sha256(plan)
            and binding.get("marker_binding") == marker_binding
        )
        try:
            observed_ok = object_from_json(observed, "score_evidence_invalid") == _stable_plan_score_after(plan)
        except CompactProjectionError:
            return False
        return bool(binding_ok and observed_ok)

    @staticmethod
    def _started_evidence_matches(evidence: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
        binding = evidence.get("binding")
        return bool(
            isinstance(binding, Mapping)
            and binding.get("plan_hash") == json_sha256(plan)
            and binding.get("marker_binding") == plan.get("marker_binding")
        )

    @staticmethod
    def _stage_evidence_matches(stage: str, evidence: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
        binding = evidence.get("binding")
        observed = evidence.get("observed")
        if not isinstance(binding, Mapping) or not isinstance(observed, Mapping):
            return False
        if (
            binding.get("plan_hash") != json_sha256(plan)
            or binding.get("marker_binding") != plan.get("marker_binding")
        ):
            return False
        expected = {
            "score_applied": _stable_plan_score_after,
            "due_corrected": _stable_plan_due_corrected,
            "score_verified": _stable_plan_final,
        }.get(stage)
        if expected is None:
            return False
        try:
            return object_from_json(observed, "score_evidence_invalid") == expected(plan)
        except CompactProjectionError:
            return False


def _custom_data_map(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _review_log_matches(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(int(observed.get(key, -1)) == int(value) for key, value in expected.items())


def _card_schedule_matches(observed: Mapping[str, Any], expected: Mapping[str, Any], *, allow_due: tuple[int, ...] = ()) -> bool:
    ignored = {"schedule_revision"}
    for key, value in expected.items():
        if key in ignored:
            continue
        if key == "due" and allow_due:
            if int(observed.get(key, -1)) not in allow_due:
                return False
        elif key == "memory_state":
            state = observed.get(key)
            if not isinstance(state, Mapping) or any(
                float(state.get(memory_key, float("nan"))) != float(memory_value)
                for memory_key, memory_value in value.items()
            ):
                return False
        elif observed.get(key) != value:
            return False
    return True


def _score_matches(prepared: PreparedCompactEvent, observed: Mapping[str, Any]) -> bool:
    expected = prepared.expected_score_after
    observed_logs = list(observed.get("review_logs", ()))
    before_logs = list(prepared.before.get("review_logs", ()))
    if len(observed_logs) != len(before_logs) + 1:
        return False
    for old in before_logs:
        if not any(_review_log_matches(row, old) for row in observed_logs):
            return False
    if not _card_schedule_matches(observed.get("card", {}), expected["card"]):
        return False
    if observed.get("counters") != expected.get("counters"):
        return False
    return sum(_review_log_matches(row, expected["score_review_log"]) for row in observed_logs) == 1


def _score_marker_matches(prepared: PreparedCompactEvent, observed: Mapping[str, Any]) -> bool:
    custom = _custom_data_map(observed.get("card", {}).get("custom_data"))
    return custom.get("inkcards") == prepared.plan.get("marker_sha256")


def _state_matches_before(observed: Mapping[str, Any], before: Mapping[str, Any]) -> bool:
    card_before = before.get("card", {})
    # The raw plan before-state is stricter than Store's stable receipt frame:
    # preserve and compare mod, exact persisted FSRS f32 values, and custom_data.
    if observed.get("card", {}) != card_before:
        return False
    for raw_key in ("native_card_hex", "native_revlogs"):
        if raw_key in before and observed.get(raw_key) != before.get(raw_key):
            return False
    if observed.get("counters") != before.get("counters"):
        return False
    old_logs = before.get("review_logs", ())
    new_logs = observed.get("review_logs", ())
    return list(new_logs) == list(old_logs)


def _partial_score_matches(
    current: Mapping[str, Any],
    expected_score: Mapping[str, Any],
    expected_final_card: Mapping[str, Any],
    expected_score_counters: Mapping[str, Any],
    expected_final_counters: Mapping[str, Any],
) -> bool:
    score_card = expected_score.get("card", {})
    final_due = int(expected_final_card.get("due", -1))
    score_due = int(score_card.get("due", -1))
    if not _card_schedule_matches(current.get("card", {}), score_card, allow_due=(score_due, final_due)):
        return False
    expected_count = int(expected_score.get("review_log_count", -1))
    if len(current.get("review_logs", ())) != expected_count:
        return False
    if not any(_review_log_matches(row, expected_score.get("score_review_log", {})) for row in current.get("review_logs", ())):
        return False
    actual_counters = current.get("counters", {})
    for deck_id in expected_score_counters:
        if actual_counters.get(deck_id) not in (expected_score_counters[deck_id], expected_final_counters.get(deck_id)):
            return False
    return True


def _raw_card_matches(current: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    try:
        from anki.cards_pb2 import Card
        raw = Card.FromString(bytes.fromhex(current["native_card_hex"]))
        raw.mtime_secs, raw.usn = 0, 0
        return _proto_dict(raw) == plan["expected_native_card"]
    except (KeyError, TypeError, ValueError):
        return False


def _final_matches(prepared: PreparedCompactEvent, current: Mapping[str, Any]) -> bool:
    if prepared.plan.get("atomic_final") and not _raw_card_matches(current, prepared.plan):
        return False
    expected = prepared.expected_after
    if not _card_schedule_matches(current.get("card", {}), expected["card"]):
        return False
    if int(current.get("review_log_count", len(current.get("review_logs", ())))) != int(expected["review_log_count"]):
        return False
    logs = list(current.get("review_logs", ()))
    before_logs = list(prepared.before.get("review_logs", ()))
    if not all(any(_review_log_matches(row, old) for row in logs) for old in before_logs):
        return False
    if sum(_review_log_matches(row, expected["score_review_log"]) for row in logs) != 1:
        return False
    return current.get("counters") == expected.get("counters")


def _score_stage_readback_matches(current: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    """Accept only a complete native readback at one durable score substage."""

    try:
        observed = _stable_store_state(current)
        expected_states = (
            _stable_plan_score_after(plan),
            _stable_plan_due_corrected(plan),
            _stable_plan_final(plan),
        )
    except (CompactNativeError, CompactProjectionError, KeyError, TypeError, ValueError):
        return False
    return any(observed == expected for expected in expected_states)


__all__ = [
    "AnkiCompactAdapter",
    "CompactCapabilityReport",
    "CompactNativeError",
    "CompactNativeResult",
    "PreparedCompactEvent",
    "RecoveryDecision",
    "create_apply_authorization",
]
