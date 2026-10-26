"""快照旁路保存的 Anki 评分应用上下文。

这些字段用于电脑端评分，绝不参与设备 payload、checksum 或分页。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCE_SCHEMA = "inkcards.application-context-source.v2"
BOUND_SCHEMA = "inkcards.application-context.v2"
_PRE_ANCESTRY_SOURCE_SCHEMA = "inkcards.application-context-source.v1"
_PRE_ANCESTRY_BOUND_SCHEMA = "inkcards.application-context.v1"
_NATIVE_DEFAULT_PREFIX = "anki-native-default:"
_DECK_COUNTER_FIELDS = (
    "last_day_studied",
    "new_studied",
    "review_studied",
    "learning_studied",
    "milliseconds_studied",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _safe_json_mapping(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    result = {str(key): _plain(item) for key, item in value.items()}
    try:
        canonical_json(result)
    except (TypeError, ValueError):
        return None
    return result


@dataclass(frozen=True)
class CapturedApplicationContext:
    """从单次 QueryOp Collection 读取的原始评分上下文。"""

    profile_identity: str
    collection_generation: Optional[int]
    runtime_epoch: Optional[str]
    anki_version: str
    anki_build: str
    timezone: Optional[str] = None
    timezone_source: Optional[str] = None
    scheduler_today: Optional[int] = None
    day_cutoff_seconds: Optional[int] = None
    collection_created_at_seconds: Optional[int] = None
    fsrs_enabled: Optional[bool] = None
    fsrs_short_term_with_steps_enabled: Optional[bool] = None
    deck_preset_ids: Mapping[str, str] = field(default_factory=dict)
    preset_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    preset_parameter_identities: Mapping[str, str] = field(default_factory=dict)
    deck_ancestor_ids: Mapping[str, Sequence[str]] = field(default_factory=dict)
    deck_contexts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    clock_config: Mapping[str, Any] = field(default_factory=dict)
    source_summary: Mapping[str, str] = field(default_factory=dict)
    card_last_review_time_sources: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    card_review_log_hashes: Mapping[str, str] = field(default_factory=dict)
    unsupported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile_identity, str) or not self.profile_identity:
            raise ValueError("profile_identity is required")
        if self.runtime_epoch is not None and not isinstance(self.runtime_epoch, str):
            raise ValueError("runtime_epoch must be a string or None")
        for name in ("timezone", "timezone_source"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        for name in ("anki_version", "anki_build"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")
        generation = self.collection_generation
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 0
        ):
            raise ValueError("collection_generation must be a non-negative integer")
        for name in ("scheduler_today", "day_cutoff_seconds", "collection_created_at_seconds"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name} must be an integer or None")
        for name in ("fsrs_enabled", "fsrs_short_term_with_steps_enabled"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean or None")
        mapping_fields = (
            "deck_preset_ids",
            "preset_configs",
            "preset_parameter_identities",
            "deck_ancestor_ids",
            "deck_contexts",
            "clock_config",
            "source_summary",
        )
        if any(not isinstance(getattr(self, name), Mapping) for name in mapping_fields):
            raise ValueError("application context maps must be mappings")
        if any(not isinstance(value, Mapping) for value in self.preset_configs.values()):
            raise ValueError("preset_configs values must be mappings")
        if any(
            not isinstance(deck, str) or not isinstance(preset, str)
            for deck, preset in self.deck_preset_ids.items()
        ):
            raise ValueError("deck_preset_ids must map strings to strings")
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in self.preset_parameter_identities.items()):
            raise ValueError("preset_parameter_identities must map strings to strings")
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in self.source_summary.items()):
            raise ValueError("source_summary must map strings to strings")
        normalized_ancestors: dict[str, tuple[str, ...]] = {}
        for deck_id, raw_ancestors in self.deck_ancestor_ids.items():
            if not isinstance(deck_id, str) or not isinstance(raw_ancestors, (tuple, list)):
                raise ValueError("deck_ancestor_ids must map deck IDs to ID sequences")
            ancestors = tuple(raw_ancestors)
            if any(not isinstance(value, str) or not value for value in ancestors):
                raise ValueError("deck_ancestor_ids values must contain deck ID strings")
            if deck_id in ancestors or len(ancestors) != len(set(ancestors)):
                raise ValueError("deck_ancestor_ids must be acyclic and unique")
            normalized_ancestors[deck_id] = ancestors
        normalized_deck_contexts: dict[str, dict[str, Any]] = {}
        required_deck_fields = {"deck_id", "name", "parent_id", *_DECK_COUNTER_FIELDS}
        for key, raw_context in self.deck_contexts.items():
            if not isinstance(key, str) or not isinstance(raw_context, Mapping):
                raise ValueError("deck_contexts must map deck IDs to mappings")
            context = _safe_json_mapping(raw_context)
            if context is None or set(context) != required_deck_fields:
                raise ValueError("deck context has missing or unsupported fields")
            if context["deck_id"] != key or not isinstance(context["name"], str) or not context["name"]:
                raise ValueError("deck context identity is invalid")
            if context["parent_id"] is not None and (
                not isinstance(context["parent_id"], str) or not context["parent_id"]
            ):
                raise ValueError("deck context parent_id must be a deck ID or None")
            for field_name in _DECK_COUNTER_FIELDS:
                count = context[field_name]
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(f"deck context {field_name} must be a non-negative integer")
            normalized_deck_contexts[key] = context
        if not isinstance(self.unsupported, (tuple, list)) or any(
            not isinstance(item, str) for item in self.unsupported
        ):
            raise ValueError("unsupported must contain strings")
        unsupported_deck_context = any(
            item.startswith(("deck_ancestry_", "deck_context_")) for item in self.unsupported
        )
        if not unsupported_deck_context:
            if not set(self.deck_preset_ids).issubset(normalized_ancestors):
                raise ValueError("deck ancestry must cover every captured actual deck")
            expected_context_ids = set(normalized_ancestors)
            for deck_id, ancestors in normalized_ancestors.items():
                expected_context_ids.add(deck_id)
                expected_context_ids.update(ancestors)
            if set(normalized_deck_contexts) != expected_context_ids:
                raise ValueError("deck contexts must cover each actual deck and every ancestor")
            for deck_id, ancestors in normalized_ancestors.items():
                segments = normalized_deck_contexts[deck_id]["name"].split("::")
                expected_names = [
                    "::".join(segments[:index]) for index in range(len(segments) - 1, 0, -1)
                ]
                if any(not segment for segment in segments) or [
                    normalized_deck_contexts[value]["name"] for value in ancestors
                ] != expected_names:
                    raise ValueError("deck ancestor chain must cover the complete native name path")
                expected_parent = ancestors[0] if ancestors else None
                if normalized_deck_contexts[deck_id]["parent_id"] != expected_parent:
                    raise ValueError("actual deck parent does not match its ancestor chain")
                for index, ancestor_id in enumerate(ancestors):
                    parent_id = ancestors[index + 1] if index + 1 < len(ancestors) else None
                    if normalized_deck_contexts[ancestor_id]["parent_id"] != parent_id:
                        raise ValueError("ancestor parent does not match its ancestor chain")
                    if parent_id is not None:
                        parent_name = normalized_deck_contexts[parent_id]["name"]
                        child_name = normalized_deck_contexts[ancestor_id]["name"]
                        if not child_name.startswith(parent_name + "::"):
                            raise ValueError("deck name hierarchy does not match its parent IDs")
                if ancestors:
                    parent_name = normalized_deck_contexts[ancestors[0]]["name"]
                    child_name = normalized_deck_contexts[deck_id]["name"]
                    if not child_name.startswith(parent_name + "::"):
                        raise ValueError("deck name hierarchy does not match its parent IDs")
        object.__setattr__(self, "deck_preset_ids", _freeze(self.deck_preset_ids))
        object.__setattr__(self, "preset_configs", _freeze(self.preset_configs))
        object.__setattr__(self, "preset_parameter_identities", _freeze(self.preset_parameter_identities))
        object.__setattr__(self, "deck_ancestor_ids", _freeze(normalized_ancestors))
        object.__setattr__(self, "deck_contexts", _freeze(normalized_deck_contexts))
        object.__setattr__(self, "clock_config", _freeze(self.clock_config))
        object.__setattr__(self, "source_summary", _freeze(self.source_summary))
        for card_id, source in self.card_last_review_time_sources.items():
            if not str(card_id).isdigit() or not isinstance(source, Mapping):
                raise ValueError("invalid last review source")
            if source.get("native_seconds") is not None or isinstance(source.get("resolved_seconds"), bool) or not isinstance(source.get("resolved_seconds"), int) or source["resolved_seconds"] < 0:
                raise ValueError("last review source must describe a resolved missing native field")
            digest = source.get("revlog_sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid last review source digest")
        object.__setattr__(self, "card_last_review_time_sources", _freeze(self.card_last_review_time_sources))
        for card_id, digest in self.card_review_log_hashes.items():
            if not str(card_id).isdigit() or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid card review history digest")
        object.__setattr__(self, "card_review_log_hashes", _freeze(self.card_review_log_hashes))
        object.__setattr__(self, "unsupported", tuple(sorted(set(self.unsupported))))
        # Reject non-JSON values at capture time instead of failing later during save.
        canonical_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema": SOURCE_SCHEMA,
            "profile_identity": self.profile_identity,
            "collection_generation": self.collection_generation,
            "runtime_epoch": self.runtime_epoch,
            "anki_version": self.anki_version,
            "anki_build": self.anki_build,
            "timezone": self.timezone,
            "timezone_source": self.timezone_source,
            "scheduler_today": self.scheduler_today,
            "day_cutoff_seconds": self.day_cutoff_seconds,
            "collection_created_at_seconds": self.collection_created_at_seconds,
            "fsrs_enabled": self.fsrs_enabled,
            "fsrs_short_term_with_steps_enabled": self.fsrs_short_term_with_steps_enabled,
            "deck_preset_ids": _plain(self.deck_preset_ids),
            "preset_configs": _plain(self.preset_configs),
            "preset_parameter_identities": _plain(self.preset_parameter_identities),
            "deck_ancestor_ids": _plain(self.deck_ancestor_ids),
            "deck_contexts": _plain(self.deck_contexts),
            "clock_config": _plain(self.clock_config),
            "source_summary": _plain(self.source_summary),
            "unsupported": list(self.unsupported),
        }
        # 旧 context 的 canonical bytes/hash 不变。
        if self.card_last_review_time_sources:
            result["card_last_review_time_sources"] = _plain(self.card_last_review_time_sources)
        if self.card_review_log_hashes:
            result["card_review_log_hashes"] = _plain(self.card_review_log_hashes)
        return result

    @property
    def source_sha256(self) -> str:
        return _sha256(canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapturedApplicationContext":
        if not isinstance(value, Mapping):
            raise ValueError("application context source must be a mapping")
        if value.get("schema") == _PRE_ANCESTRY_SOURCE_SCHEMA:
            raise ValueError("incomplete pre-ancestry application context source; re-export snapshot")
        if value.get("schema") != SOURCE_SCHEMA:
            raise ValueError("unsupported application context source schema")
        if "deck_ancestor_ids" not in value or "deck_contexts" not in value:
            raise ValueError("application context v2 requires ancestry and deck context fields")
        unsupported = value.get("unsupported", ())
        if not isinstance(unsupported, (tuple, list)) or any(
            not isinstance(item, str) for item in unsupported
        ):
            raise ValueError("unsupported must contain strings")
        return cls(
            profile_identity=value["profile_identity"],
            collection_generation=value.get("collection_generation"),
            runtime_epoch=value.get("runtime_epoch"),
            anki_version=value["anki_version"],
            anki_build=value["anki_build"],
            timezone=value.get("timezone"),
            timezone_source=value.get("timezone_source"),
            scheduler_today=value.get("scheduler_today"),
            day_cutoff_seconds=value.get("day_cutoff_seconds"),
            collection_created_at_seconds=value.get("collection_created_at_seconds"),
            fsrs_enabled=value.get("fsrs_enabled"),
            fsrs_short_term_with_steps_enabled=value.get("fsrs_short_term_with_steps_enabled"),
            deck_preset_ids=value.get("deck_preset_ids", {}),
            preset_configs=value.get("preset_configs", {}),
            preset_parameter_identities=value.get("preset_parameter_identities", {}),
            deck_ancestor_ids=value["deck_ancestor_ids"],
            deck_contexts=value["deck_contexts"],
            clock_config=value.get("clock_config", {}),
            source_summary=value.get("source_summary", {}),
            card_last_review_time_sources=value.get("card_last_review_time_sources", {}),
            card_review_log_hashes=value.get("card_review_log_hashes", {}),
            unsupported=tuple(unsupported),
        )


@dataclass(frozen=True)
class SnapshotApplicationContext:
    """将 Collection 评分上下文绑定到一个已发布的不可变快照。"""

    profile_id: str
    snapshot_id: str
    snapshot_checksum: str
    snapshot_generation: int
    source: CapturedApplicationContext
    source_sha256: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_id, str)
            or not self.profile_id
            or not isinstance(self.snapshot_id, str)
            or not self.snapshot_id
        ):
            raise ValueError("profile_id and snapshot_id are required")
        if not isinstance(self.snapshot_checksum, str) or len(self.snapshot_checksum) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.snapshot_checksum
        ):
            raise ValueError("snapshot_checksum must be lowercase SHA-256")
        if (
            isinstance(self.snapshot_generation, bool)
            or not isinstance(self.snapshot_generation, int)
            or self.snapshot_generation < 0
        ):
            raise ValueError("snapshot_generation must be non-negative")
        if not isinstance(self.source, CapturedApplicationContext):
            raise ValueError("source must be CapturedApplicationContext")
        if self.source.collection_generation != self.snapshot_generation:
            raise ValueError("collection and snapshot generations differ")
        expected = self.source.source_sha256
        if self.source_sha256 and self.source_sha256 != expected:
            raise ValueError("application context source checksum mismatch")
        object.__setattr__(self, "source_sha256", expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BOUND_SCHEMA,
            "profile_id": self.profile_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_checksum": self.snapshot_checksum,
            "snapshot_generation": self.snapshot_generation,
            "source_sha256": self.source_sha256,
            "source": self.source.to_dict(),
        }

    @property
    def collection_generation(self) -> Optional[int]:
        """兼容直读：代际来自同一 QueryOp 的 source context。"""

        return self.source.collection_generation

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def bind(
        cls,
        source: CapturedApplicationContext,
        *,
        profile_id: str,
        snapshot_id: str,
        snapshot_checksum: str,
        snapshot_generation: int,
    ) -> "SnapshotApplicationContext":
        return cls(profile_id, snapshot_id, snapshot_checksum, snapshot_generation, source)

    @classmethod
    def from_json(cls, value: str) -> "SnapshotApplicationContext":
        decoded = json.loads(value)
        if isinstance(decoded, dict) and decoded.get("schema") == _PRE_ANCESTRY_BOUND_SCHEMA:
            raise ValueError("incomplete pre-ancestry application context; re-export snapshot")
        if not isinstance(decoded, dict) or decoded.get("schema") != BOUND_SCHEMA:
            raise ValueError("unsupported bound application context schema")
        source = CapturedApplicationContext.from_dict(decoded["source"])
        return cls(
            profile_id=decoded["profile_id"],
            snapshot_id=decoded["snapshot_id"],
            snapshot_checksum=decoded["snapshot_checksum"],
            snapshot_generation=decoded["snapshot_generation"],
            source=source,
            source_sha256=decoded.get("source_sha256", ""),
        )


def _read_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _timezone_identity() -> tuple[Optional[str], Optional[str]]:
    configured = os.environ.get("TZ")
    if configured:
        selected = configured.lstrip(":")
        if "/" in selected and not selected.startswith("/"):
            try:
                ZoneInfo(selected)
            except (ValueError, ZoneInfoNotFoundError):
                pass
            else:
                return selected, f"TZ={configured}"
    localtime = Path("/etc/localtime")
    try:
        resolved = localtime.resolve(strict=True)
        parts = resolved.parts
        if "zoneinfo" in parts:
            index = parts.index("zoneinfo")
            zone = "/".join(parts[index + 1 :])
            if zone:
                try:
                    ZoneInfo(zone)
                except (ValueError, ZoneInfoNotFoundError):
                    pass
                else:
                    return zone, f"/etc/localtime -> {resolved}"
    except OSError:
        pass
    return None, "IANA timezone unavailable from TZ or /etc/localtime"


def _mapping_or_attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return _read_attr(value, name)


def _native_deck_context(backend: Any, deck_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    reader = _read_attr(backend, "get_deck")
    if not callable(reader):
        return None, "deck_context_backend_get_deck_unavailable"
    try:
        native = reader(int(deck_id))
    except Exception:
        return None, f"deck_context_native_read_failed:{deck_id}"
    native_id = _integer(_mapping_or_attr(native, "id"))
    name = _mapping_or_attr(native, "name")
    if native_id is None or str(native_id) != deck_id:
        return None, f"deck_context_native_id_mismatch:{deck_id}"
    if not isinstance(name, str) or not name:
        return None, f"deck_context_native_name_unavailable:{deck_id}"
    common = _mapping_or_attr(native, "common")
    record: dict[str, Any] = {"deck_id": deck_id, "name": name, "parent_id": None}
    for field_name in _DECK_COUNTER_FIELDS:
        counter = _integer(_mapping_or_attr(common, field_name))
        if counter is None or counter < 0:
            return None, f"deck_context_counter_unavailable:{deck_id}:{field_name}"
        record[field_name] = counter
    return record, None


def _capture_deck_tree(
    collection: Any, deck_ids: Sequence[str]
) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]], list[str]]:
    """通过固定 Anki API 捕获牌组祖先链与原生 common 计数。"""

    manager = _read_attr(collection, "decks")
    parents_reader = _read_attr(manager, "parents")
    manager_get = _read_attr(manager, "get")
    backend = _read_attr(collection, "_backend")
    native_reader = _read_attr(backend, "get_deck")
    actual_ids = sorted({str(value) for value in deck_ids})
    ancestors_by_deck: dict[str, tuple[str, ...]] = {deck_id: () for deck_id in actual_ids}
    contexts: dict[str, dict[str, Any]] = {}
    unsupported: list[str] = []

    if not callable(parents_reader):
        unsupported.append("deck_ancestry_api_unavailable")
    if not callable(manager_get):
        unsupported.append("deck_context_manager_identity_api_unavailable")
    if not callable(native_reader):
        unsupported.append("deck_context_backend_get_deck_unavailable")
    if unsupported:
        return ancestors_by_deck, contexts, unsupported

    native_cache: dict[str, tuple[Optional[dict[str, Any]], Optional[str]]] = {}

    def native_for(deck_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if deck_id not in native_cache:
            native_cache[deck_id] = _native_deck_context(backend, deck_id)
        return native_cache[deck_id]

    for deck_id in actual_ids:
        try:
            numeric_id = int(deck_id)
        except (ValueError, TypeError):
            unsupported.append(f"deck_context_invalid_id:{deck_id}")
            continue
        try:
            raw_parents = parents_reader(numeric_id)
        except Exception:
            unsupported.append(f"deck_ancestry_read_failed:{deck_id}")
            continue
        if isinstance(raw_parents, (str, bytes)) or raw_parents is None:
            unsupported.append(f"deck_ancestry_invalid:{deck_id}")
            continue
        try:
            parent_items = list(raw_parents)
        except TypeError:
            unsupported.append(f"deck_ancestry_invalid:{deck_id}")
            continue

        try:
            manager_record = manager_get(numeric_id)
        except Exception:
            manager_record = None
        manager_id = _integer(_mapping_or_attr(manager_record, "id"))
        manager_name = _mapping_or_attr(manager_record, "name")
        native_actual, native_error = native_for(deck_id)
        if native_error is not None:
            unsupported.append(native_error)
            continue
        if manager_id is None or str(manager_id) != deck_id:
            unsupported.append(f"deck_context_manager_id_mismatch:{deck_id}")
            continue
        if not isinstance(manager_name, str) or manager_name != native_actual["name"]:
            unsupported.append(f"deck_context_name_mismatch:{deck_id}")
            continue

        api_ancestors: list[tuple[str, str]] = []
        invalid_parent = False
        for parent_item in parent_items:
            parent_id_raw = _mapping_or_attr(parent_item, "id")
            parent_name = _mapping_or_attr(parent_item, "name")
            parent_numeric_id = _integer(parent_id_raw)
            if (
                parent_numeric_id is None
                or not isinstance(parent_name, str)
                or not parent_name
            ):
                unsupported.append(f"deck_ancestry_parent_invalid:{deck_id}")
                invalid_parent = True
                break
            api_ancestors.append((str(parent_numeric_id), parent_name))
        if invalid_parent:
            continue

        actual_name = native_actual["name"]
        segments = actual_name.split("::")
        if any(not segment for segment in segments):
            unsupported.append(f"deck_ancestry_name_invalid:{deck_id}")
            continue
        expected_names = ["::".join(segments[:index]) for index in range(1, len(segments))]
        if [name for _, name in api_ancestors] != expected_names:
            unsupported.append(f"deck_ancestry_relationship_mismatch:{deck_id}")
            continue
        ancestor_ids = [ancestor_id for ancestor_id, _ in api_ancestors]
        if deck_id in ancestor_ids or len(ancestor_ids) != len(set(ancestor_ids)):
            unsupported.append(f"deck_ancestry_cycle_or_duplicate:{deck_id}")
            continue

        candidate_records: dict[str, dict[str, Any]] = {}
        ancestor_failed = False
        for index, (ancestor_id, ancestor_name) in enumerate(api_ancestors):
            ancestor_record, ancestor_error = native_for(ancestor_id)
            if ancestor_error is not None:
                unsupported.append(ancestor_error)
                ancestor_failed = True
                break
            if ancestor_record["name"] != ancestor_name:
                unsupported.append(f"deck_context_parent_name_mismatch:{ancestor_id}")
                ancestor_failed = True
                break
            ancestor_record = dict(ancestor_record)
            ancestor_record["parent_id"] = (
                api_ancestors[index - 1][0] if index > 0 else None
            )
            candidate_records[ancestor_id] = ancestor_record
        if ancestor_failed:
            continue

        actual_record = dict(native_actual)
        actual_record["parent_id"] = ancestor_ids[-1] if ancestor_ids else None
        candidate_records[deck_id] = actual_record
        conflict = False
        for candidate_id, candidate in candidate_records.items():
            existing = contexts.get(candidate_id)
            if existing is not None and canonical_json(existing) != canonical_json(candidate):
                unsupported.append(f"deck_context_conflict:{candidate_id}")
                conflict = True
                break
        if conflict:
            continue

        # 固定 build 的 parents() 返回根到近父；旁路公共接口保存近父到远祖先。
        ancestors_by_deck[deck_id] = tuple(reversed(ancestor_ids))
        contexts.update(candidate_records)

    return ancestors_by_deck, contexts, unsupported


def last_review_time_source_from_rows(native_seconds: Any, rows: Sequence[Sequence[int]]) -> dict[str, Any]:
    """固定版原生 runtime fallback；只读证据含完整原生历史摘要。"""
    if native_seconds is not None:
        if isinstance(native_seconds, bool) or not isinstance(native_seconds, int):
            raise ValueError("invalid native last review time")
        return {"native_seconds": native_seconds, "resolved_seconds": native_seconds,
                "revlog_sha256": None}
    normalized = [list(row) for row in rows]
    if any(len(row) != 9 or any(isinstance(value, bool) or not isinstance(value, int) for value in row)
           for row in normalized):
        raise ValueError("invalid native review history")
    normalized.sort(key=lambda row: row[0])
    selected = [row for row in normalized if 1 <= row[3] <= 4 and (row[8] != 3 or row[6] != 0)]
    return {"native_seconds": None,
            "resolved_seconds": selected[-1][0] // 1000 if selected else None,
            "revlog_sha256": _sha256(canonical_json(normalized))}


def capture_last_review_time_source(collection: Any, card: Any) -> dict[str, Any]:
    """使用 QueryOp/CollectionOp 注入的同一 Collection；不修改原生缓存。"""
    native = getattr(card, "last_review_time", None)
    if native is not None:
        return last_review_time_source_from_rows(native, ())
    reader = getattr(getattr(collection, "db", None), "all", None)
    if not callable(reader):
        raise ValueError("native review history unavailable")
    try:
        rows = reader("SELECT id,cid,usn,ease,ivl,lastIvl,factor,time,type FROM revlog WHERE cid=? ORDER BY id",
                      int(card.id))
    except Exception as exc:
        raise ValueError("native review history unavailable") from exc
    return last_review_time_source_from_rows(native, rows)


def capture_collection_context(
    collection: Any,
    *,
    profile_identity: str,
    collection_generation: Optional[int],
    runtime_epoch: Optional[str],
    anki_version: str,
    anki_build: str,
    deck_ids: Sequence[str],
    last_review_card_ids: Sequence[str] = (),
    review_log_card_ids: Sequence[str] = (),
) -> CapturedApplicationContext:
    """通过当前 QueryOp Collection 读取评分输入，不使用 exporter 的默认值。"""

    unsupported: list[str] = []
    summary = {
        "presets": "Collection.decks.config_dict_for_deck_id",
        "fsrs": "Collection.get_config('fsrs')",
        "short_term_steps": "Collection.fsrs_short_term_with_steps_enabled",
        "scheduler_today": "Collection.sched.today",
        "day_cutoff": "Collection.sched.day_cutoff",
        "creation_time": "Collection.crt",
        "clock_config": "Collection.get_config(rollover/localOffset/creationOffset)",
        "timezone": "/etc/localtime zoneinfo identity or TZ environment",
        "deck_ancestors": "Collection.decks.parents",
        "deck_context": "Collection._backend.get_deck().common",
    }
    manager = _read_attr(collection, "decks") or _read_attr(collection, "deck_manager")
    reader = _read_attr(manager, "config_dict_for_deck_id")
    deck_preset_ids: dict[str, str] = {}
    preset_configs: dict[str, dict[str, Any]] = {}
    parameter_identities: dict[str, str] = {}
    if not callable(reader):
        unsupported.append("preset_config_api_unavailable")
    else:
        for deck_id in sorted({str(value) for value in deck_ids}):
            try:
                config = _safe_json_mapping(reader(int(deck_id)))
            except Exception:
                config = None
            if config is None:
                unsupported.append(f"preset_config_unavailable:{deck_id}")
                continue
            raw_id = config.get("id")
            if raw_id is None:
                unsupported.append(f"preset_identity_unavailable:{deck_id}")
                continue
            preset_id = str(raw_id)
            deck_preset_ids[deck_id] = preset_id
            prior = preset_configs.get(preset_id)
            if prior is not None and canonical_json(prior) != canonical_json(config):
                unsupported.append(f"preset_config_conflict:{preset_id}")
                continue
            preset_configs[preset_id] = config
            raw_params = config.get("fsrsParams6")
            if isinstance(raw_params, list):
                parameter_identities[preset_id] = (
                    f"{_NATIVE_DEFAULT_PREFIX}{anki_version}:{anki_build}"
                    if not raw_params else f"preset:{preset_id}:fsrsParams6"
                )
            else:
                unsupported.append(f"preset_fsrs_params_unavailable:{preset_id}")
    get_config = _read_attr(collection, "get_config")
    fsrs_value: Any = None
    if callable(get_config):
        try:
            fsrs_value = get_config("fsrs")
        except Exception:
            unsupported.append("fsrs_toggle_unavailable")
    else:
        unsupported.append("fsrs_toggle_api_unavailable")
    fsrs_enabled = fsrs_value if isinstance(fsrs_value, bool) else None
    if fsrs_enabled is None:
        unsupported.append("fsrs_toggle_unknown")
    short_steps_value = _read_attr(collection, "fsrs_short_term_with_steps_enabled")
    short_steps = short_steps_value if isinstance(short_steps_value, bool) else None
    if short_steps is None:
        unsupported.append("fsrs_short_term_with_steps_unknown")
    scheduler = _read_attr(collection, "sched") or _read_attr(collection, "scheduler")
    today = _integer(_read_attr(scheduler, "today"))
    cutoff = _integer(_read_attr(scheduler, "day_cutoff"))
    created = _integer(_read_attr(collection, "crt"))
    if today is None:
        unsupported.append("scheduler_today_unavailable")
    if cutoff is None:
        unsupported.append("day_cutoff_unavailable")
    if created is None:
        unsupported.append("collection_creation_time_unavailable")
    clock_config: dict[str, Any] = {}
    if callable(get_config):
        for key in ("rollover", "localOffset", "creationOffset"):
            try:
                value = get_config(key)
            except Exception:
                value = None
            if value is not None and isinstance(value, (str, int, float, bool)):
                clock_config[key] = value
            else:
                unsupported.append(f"clock_config_unavailable:{key}")
    timezone, timezone_source = _timezone_identity()
    if timezone is None:
        unsupported.append("iana_timezone_unavailable")
    deck_ancestor_ids, deck_contexts, deck_context_unsupported = _capture_deck_tree(
        collection, deck_ids
    )
    unsupported.extend(deck_context_unsupported)
    return CapturedApplicationContext(
        profile_identity=profile_identity,
        collection_generation=collection_generation,
        runtime_epoch=runtime_epoch,
        anki_version=anki_version,
        anki_build=anki_build,
        timezone=timezone,
        timezone_source=timezone_source,
        scheduler_today=today,
        day_cutoff_seconds=cutoff,
        collection_created_at_seconds=created,
        fsrs_enabled=fsrs_enabled,
        fsrs_short_term_with_steps_enabled=short_steps,
        deck_preset_ids=deck_preset_ids,
        preset_configs=preset_configs,
        preset_parameter_identities=parameter_identities,
        deck_ancestor_ids=deck_ancestor_ids,
        deck_contexts=deck_contexts,
        clock_config=clock_config,
        source_summary=summary,
        card_last_review_time_sources={
            str(cid): capture_last_review_time_source(collection, collection.get_card(int(cid)))
            for cid in last_review_card_ids
        },
        card_review_log_hashes={
            str(cid): _sha256(canonical_json([list(row) for row in collection.db.all(
                "SELECT id,cid,usn,ease,ivl,lastIvl,factor,time,type FROM revlog WHERE cid=? ORDER BY id", int(cid)
            )])) for cid in review_log_card_ids
        } if callable(getattr(getattr(collection, "db", None), "all", None)) else {},
        unsupported=tuple(unsupported),
    )


__all__ = [
    "BOUND_SCHEMA",
    "CapturedApplicationContext",
    "SOURCE_SCHEMA",
    "SnapshotApplicationContext",
    "canonical_json",
    "capture_collection_context",
]
