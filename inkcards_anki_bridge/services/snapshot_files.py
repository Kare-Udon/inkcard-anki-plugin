"""冻结现有 Anki snapshot 的有界、内容寻址文件包。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..storage import ProfileStore, StorageValidationError
from .snapshots import SnapshotError, SnapshotService


MAX_RECORD_BYTES = 32768
MAX_PACKAGE_BYTES = 98304
MAX_PAGE_BYTES = 32768
MAX_FILE_IDS = 512
MAX_PACKAGE_RECORDS = 24576
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(Exception):
    """artifact route 的稳定错误。"""

    def __init__(self, code: str, *, status: int = 422, retryable: bool = False) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(code)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _wire_numbers(value: Any) -> Any:
    """把超出有符号 int32 的协议整数编码成十进制字符串。"""

    if isinstance(value, Mapping):
        return {str(key): _wire_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_numbers(item) for item in value]
    if _integer(value) and not -(2**31) <= value <= 2**31 - 1:
        return str(value)
    return value


def _json(value: Any) -> bytes:
    try:
        return json.dumps(
            _wire_numbers(value), ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ArtifactError("artifact_json_invalid") from None


def _package(records: Sequence[Mapping[str, Any]], *, limit: int) -> bytes:
    if len(records) > MAX_PACKAGE_RECORDS:
        raise ArtifactError("artifact_package_record_limit", status=413)
    out = bytearray(b"ICPK1\n")
    for record in records:
        raw = _json(record)
        if not raw or len(raw) > MAX_RECORD_BYTES:
            raise ArtifactError("artifact_record_too_large", status=413)
        frame = str(len(raw)).encode("ascii") + b"\n" + raw + b"\n"
        if len(out) + len(frame) > limit:
            raise ArtifactError("artifact_package_too_large", status=413)
        out.extend(frame)
    if len(records) == 0:
        raise ArtifactError("artifact_package_empty")
    return bytes(out)


def _descriptor(kind: str, body: bytes, record_count: int) -> Dict[str, Any]:
    digest = hashlib.sha256(body).hexdigest()
    prefix = {"text": "t", "schedule": "s", "index": "i"}[kind]
    return {
        "id": digest,
        "name": "%s-%s.bin" % (prefix, digest[:32]),
        "kind": kind,
        "bytes": len(body),
        "sha256": digest,
        "record_count": record_count,
    }


def _queue_summary(indexes: Sequence[Mapping[str, Any]], schedules: Mapping[str, Mapping[str, Any]]) -> List[List[Any]]:
    """索引包的保守排程下界；设备据此只读可能排在前面的包。"""
    groups: Dict[Tuple[str, str, str], List[Any]] = {}
    for index in indexes:
        card = schedules[index["card_id"]]
        if any(card.get(flag) is True for flag in ("suspended", "paused", "buried")):
            continue
        state = str(card["state"])
        category = "learning" if state in ("learning", "relearning") else state
        due = int(card.get("due_at_ms", 0)) if category == "learning" else int(card.get("due_collection_day", 0)) if category == "review" else 0
        key = (str(card["deck_id"]), category, str(card.get("buried_until_day") or ""))
        rank, card_id = int(card["queue_order"]), str(card["card_id"])
        group = groups.get(key)
        if group is None:
            groups[key] = [*key, 1, due, due, rank, card_id]
        else:
            group[3] += 1
            group[4], group[5] = min(group[4], due), max(group[5], due)
            group[6], group[7] = min(group[6], rank), min(group[7], card_id)
    return [groups[key] for key in sorted(groups)]


class SnapshotFilesService:
    """为已发布 snapshot 准备文件包并管理 artifact 传输收据。"""

    def __init__(self, store: ProfileStore, *, clock: Optional[Any] = None) -> None:
        if not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        self.store = store
        self.require_prepared = False
        self._clock = clock or _now_ms

    def _now(self) -> int:
        value = self._clock()
        if not _integer(value) or value < 0:
            raise ArtifactError("invalid_timestamp")
        return value

    @staticmethod
    def _fail_storage(error: StorageValidationError) -> ArtifactError:
        code = error.code
        status = 409 if code in {
            "identity_mismatch", "snapshot_not_found", "artifact_transfer_conflict",
            "artifact_snapshot_conflict", "artifact_file_collision", "artifact_context_mismatch",
        } else 422
        return ArtifactError(code, status=status)

    @staticmethod
    def _step_seconds(values: Any, unit: Any) -> List[float]:
        if not isinstance(values, list) or unit not in (None, "minutes", "seconds"):
            raise ArtifactError("scheduler_preset_invalid", status=409)
        multiplier = 60 if unit == "minutes" else 1
        result: List[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ArtifactError("scheduler_preset_invalid", status=409)
            seconds = value * multiplier
            if seconds < 1 or seconds > 65535 or seconds != math.floor(seconds):
                raise ArtifactError("scheduler_preset_invalid", status=409)
            result.append(int(seconds))
        return result

    @staticmethod
    def _numeric_tree(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): SnapshotFilesService._numeric_tree(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [SnapshotFilesService._numeric_tree(item) for item in value]
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        raise ArtifactError("scheduler_preset_invalid", status=409)

    @staticmethod
    def _preset_projection(preset: Mapping[str, Any]) -> Dict[str, Any]:
        unsupported = preset.get("unsupported", [])
        if not isinstance(unsupported, list) or unsupported:
            raise ArtifactError("scheduler_preset_unsupported", status=409)
        params = preset.get("fsrs_params")
        if not isinstance(params, Mapping):
            raise ArtifactError("scheduler_preset_invalid", status=409)
        if params.get("parameters") is not None:
            params = params["parameters"]
        elif params.get("params") is not None:
            params = params["params"]
        max_interval = preset.get("max_interval_days")
        if max_interval is None:
            max_interval = 36500
        if not _integer(max_interval) or max_interval < 1:
            raise ArtifactError("scheduler_preset_invalid", status=409)
        for limit_name in ("new_limit", "review_limit"):
            limit = preset.get(limit_name)
            if limit is not None and (not _integer(limit) or limit < 0):
                raise ArtifactError("scheduler_preset_invalid", status=409)
        retention = preset.get("desired_retention")
        if retention is not None and (
            isinstance(retention, bool) or not isinstance(retention, (int, float))
            or not math.isfinite(retention)
        ):
            raise ArtifactError("scheduler_preset_invalid", status=409)
        return {
            "fsrs_params": SnapshotFilesService._numeric_tree(params),
            "desired_retention": retention,
            "learning_steps": SnapshotFilesService._step_seconds(
                preset.get("learning_steps"), preset.get("learning_steps_unit")
            ),
            "relearning_steps": SnapshotFilesService._step_seconds(
                preset.get("relearning_steps"), preset.get("learning_steps_unit")
            ),
            "new_limit": preset.get("new_limit"),
            "review_limit": preset.get("review_limit"),
            "max_interval_days": max_interval,
        }

    @staticmethod
    def _catalog(metadata: Mapping[str, Any], cards: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        decks = metadata.get("decks")
        if not isinstance(decks, list):
            raise ArtifactError("snapshot_corrupt", status=503)
        by_id: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for deck in decks:
            if not isinstance(deck, Mapping) or not isinstance(deck.get("deck_id"), str):
                raise ArtifactError("snapshot_corrupt", status=503)
            deck_id = deck["deck_id"]
            if deck_id in by_id:
                raise ArtifactError("duplicate_deck", status=409)
            by_id[deck_id] = dict(deck)
            order.append(deck_id)
        direct = {deck_id: 0 for deck_id in order}
        for card in cards:
            deck_id = card.get("deck_id")
            if not isinstance(deck_id, str) or deck_id not in by_id:
                raise ArtifactError("card_deck_missing", status=409)
            direct[deck_id] += 1
        totals = dict(direct)
        children = {deck_id: False for deck_id in order}
        for deck_id in order:
            parent = by_id[deck_id].get("parent_id")
            seen = {deck_id}
            while parent is not None:
                if not isinstance(parent, str) or parent not in by_id or parent in seen:
                    raise ArtifactError("deck_parent_invalid", status=409)
                seen.add(parent)
                children[parent] = True
                totals[parent] += direct[deck_id]
                parent = by_id[parent].get("parent_id")
        return [
            {
                "deck_id": deck_id,
                "name": by_id[deck_id].get("name"),
                "parent_id": by_id[deck_id].get("parent_id"),
                "card_count": totals[deck_id],
                "direct_card_count": direct[deck_id],
                "has_children": children[deck_id],
            }
            for deck_id in order
        ]

    @staticmethod
    def _config(metadata: Mapping[str, Any], cards: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        decks = metadata.get("decks")
        presets = metadata.get("presets")
        day = metadata.get("day_table")
        if not isinstance(decks, list) or not isinstance(presets, list) or not isinstance(day, Mapping):
            raise ArtifactError("snapshot_corrupt", status=503)
        decks_by_id = {d.get("deck_id"): d for d in decks if isinstance(d, Mapping)}
        presets_by_id: Dict[str, Dict[str, Any]] = {}
        for preset in presets:
            if not isinstance(preset, Mapping) or not isinstance(preset.get("preset_id"), str):
                raise ArtifactError("snapshot_corrupt", status=503)
            key = str(preset["preset_id"])
            if key in presets_by_id:
                raise ArtifactError("scheduler_duplicate_preset", status=409)
            presets_by_id[key] = SnapshotFilesService._preset_projection(preset)
        first_deck = decks_by_id.get(cards[0].get("deck_id")) if cards else (decks[0] if decks else None)
        if first_deck is None:
            return None
        preset_id = first_deck.get("preset_id")
        selected = presets_by_id.get(preset_id)
        if selected is None:
            if cards:
                raise ArtifactError("scheduler_preset_missing", status=409)
            return None
        limits = (first_deck.get("new_limit"), first_deck.get("review_limit"))
        for card in cards:
            deck = decks_by_id.get(card.get("deck_id"))
            if deck is None or deck.get("preset_id") not in presets_by_id:
                raise ArtifactError("scheduler_preset_missing", status=409)
            if presets_by_id[deck["preset_id"]] != selected:
                raise ArtifactError("scheduler_mixed_preset_unsupported", status=409)
            if (deck.get("new_limit"), deck.get("review_limit")) != limits:
                raise ArtifactError("scheduler_mixed_limits_unsupported", status=409)
        return {
            "params": selected["fsrs_params"],
            "retention": selected["desired_retention"],
            "learning": selected["learning_steps"],
            "relearning": selected["relearning_steps"],
            "max_interval_days": selected["max_interval_days"],
            "new_limit": limits[0] if limits[0] is not None else 20,
            "review_limit": limits[1] if limits[1] is not None else 200,
            "day": [
                day.get("review_day"), day.get("day_learning_day"), day.get("day_cutoff_ms"),
                day.get("first_day_start_ms"), day.get("future_cutoffs_ms", []),
            ],
        }

    @staticmethod
    def _day_for_timestamp(day: Mapping[str, Any], timestamp: Any) -> Optional[int]:
        first = day.get("first_day_start_ms")
        cutoffs = day.get("future_cutoffs_ms")
        review_day = day.get("review_day")
        if not _integer(timestamp) or not _integer(first) or not isinstance(cutoffs, list) or len(cutoffs) != 90:
            return None
        boundaries = [first] + cutoffs
        if timestamp < first or timestamp >= cutoffs[-1]:
            return None
        low, high = 0, 90
        while low < high:
            middle = (low + high) // 2
            if timestamp < boundaries[middle + 1]:
                high = middle
            else:
                low = middle + 1
        return review_day + low if _integer(review_day) else None

    @staticmethod
    def _day_start(day: Mapping[str, Any], collection_day: Any) -> Optional[int]:
        start_day, first, cutoffs = day.get("day_learning_day"), day.get("first_day_start_ms"), day.get("future_cutoffs_ms")
        if not _integer(collection_day) or not _integer(start_day) or not _integer(first) or not isinstance(cutoffs, list):
            return None
        offset = collection_day - start_day
        if offset < 0 or offset > 89 or len(cutoffs) != 90:
            return None
        return first if offset == 0 else cutoffs[offset - 1]

    @staticmethod
    def _schedule(card: Mapping[str, Any], index: int, deck: Mapping[str, Any], presets: Mapping[str, Mapping[str, Any]], day: Mapping[str, Any]) -> Dict[str, Any]:
        kind = card.get("type")
        queue = card.get("queue")
        states = {"new": "new", "learning": "learning", "review": "review", "relearning": "relearning"}
        state = states.get(kind)
        if state is None or not _integer(queue):
            raise ArtifactError("scheduler_card_type_unsupported", status=409)
        if not (
            state == "new" and queue == 0 or state == "review" and queue == 2
            or state in {"learning", "relearning"} and queue in {1, 3}
        ):
            raise ArtifactError("scheduler_card_type_queue_mismatch", status=409)
        preset = presets.get(str(deck.get("preset_id")))
        if preset is None:
            raise ArtifactError("scheduler_preset_missing", status=409)
        schedule: Dict[str, Any] = {
            "card_id": str(card["card_id"]),
            "note_id": str(card["note_id"]),
            "deck_id": str(card["deck_id"]),
            "schedule_revision": str(card.get("schedule_revision") or "snapshot-%s" % index),
            "state": state,
            "step": 0,
            "queue_order": card.get("ordinal") if _integer(card.get("ordinal")) else index,
            "short_interval": False,
            "suspended": False,
            "paused": False,
            "buried": False,
        }
        last = card.get("last_review_time_ms")
        if state == "new":
            if last is not None:
                raise ArtifactError("scheduler_new_due_invalid", status=409)
        else:
            if not _integer(last):
                raise ArtifactError("scheduler_last_review_time_missing", status=409)
            schedule["last_answered_at_ms"] = last
            last_day = SnapshotFilesService._day_for_timestamp(day, last)
            if last_day is not None:
                schedule["last_answered_collection_day_index"] = last_day
        due = card.get("due")
        if not isinstance(due, Mapping):
            raise ArtifactError("scheduler_due_missing", status=409)
        if state == "review":
            memory = card.get("memory_state")
            due_value = due.get("value")
            if (
                not isinstance(memory, Mapping)
                or not isinstance(memory.get("stability"), (int, float))
                or not isinstance(memory.get("difficulty"), (int, float))
                or due.get("kind") != "review_day" or due.get("unit") != "day"
                or not _integer(due_value) or due_value < 1
            ):
                raise ArtifactError("scheduler_review_invalid", status=409)
            schedule.update({
                "memory": {"stability": memory["stability"], "difficulty": memory["difficulty"]},
                "interval_days": card.get("interval_days") if _integer(card.get("interval_days")) and card["interval_days"] >= 1 else 1,
                "due_kind": "review_days",
                "due_value": due_value,
                "due_collection_day": due_value,
            })
        elif state in {"learning", "relearning"}:
            steps = preset["learning_steps"] if state == "learning" else preset["relearning_steps"]
            remaining = card.get("learning_steps_left")
            if not steps:
                raise ArtifactError("scheduler_learning_steps_unsupported", status=409)
            if not _integer(remaining) or remaining < 1 or remaining > len(steps):
                raise ArtifactError("scheduler_learning_steps_left_invalid", status=409)
            step = len(steps) - remaining + 1
            schedule["step"] = step
            schedule["due_kind"] = "step_seconds"
            schedule["due_value"] = steps[step - 1]
            if card.get("memory_state") is not None:
                memory = card["memory_state"]
                if not isinstance(memory, Mapping) or "stability" not in memory or "difficulty" not in memory:
                    raise ArtifactError("scheduler_learning_memory_invalid", status=409)
                schedule["memory"] = {"stability": memory["stability"], "difficulty": memory["difficulty"]}
            if _integer(card.get("interval_days")) and card["interval_days"] >= 1:
                schedule["interval_days"] = card["interval_days"]
            if queue == 1:
                if due.get("kind") != "learning_timestamp" or due.get("unit") != "unix_ms" or not _integer(due.get("value")):
                    raise ArtifactError("scheduler_learning_due_invalid", status=409)
                schedule["due_at_ms"] = due["value"]
            else:
                if due.get("kind") != "day_learning_day" or due.get("unit") != "day":
                    raise ArtifactError("scheduler_day_learning_unsupported", status=409)
                schedule["due_at_ms"] = SnapshotFilesService._day_start(day, due.get("value"))
                if schedule["due_at_ms"] is None:
                    raise ArtifactError("scheduler_day_learning_unsupported", status=409)
        else:
            if due.get("kind") != "new_position" or last is not None or card.get("memory_state") is not None:
                raise ArtifactError("scheduler_new_due_invalid", status=409)
        return schedule

    @staticmethod
    def _frame(record: Mapping[str, Any]) -> bytes:
        raw = _json(record)
        if len(raw) > MAX_RECORD_BYTES:
            raise ArtifactError("artifact_record_too_large", status=413)
        return str(len(raw)).encode("ascii") + b"\n" + raw + b"\n"

    @staticmethod
    def _chunk_group(
        entries: Sequence[Tuple[str, Mapping[str, Any]]], *, limit: int
    ) -> List[List[Tuple[str, Mapping[str, Any]]]]:
        groups: List[List[Tuple[str, Mapping[str, Any]]]] = []
        current: List[Tuple[str, Mapping[str, Any]]] = []
        used = len(b"ICPK1\n")
        for entry in entries:
            frame = SnapshotFilesService._frame(entry[1])
            if (used + len(frame) > limit or len(current) >= MAX_PACKAGE_RECORDS) and current:
                groups.append(current)
                current = []
                used = len(b"ICPK1\n")
            if used + len(frame) > limit:
                raise ArtifactError("artifact_package_too_large", status=413)
            current.append(entry)
            used += len(frame)
        if current:
            groups.append(current)
        return groups

    def _old_slots(self, scope_id: str, kind: str) -> Dict[int, List[str]]:
        def read(db: Any) -> Dict[int, List[str]]:
            rows = db.execute(
                "SELECT slot_index,record_index,card_id FROM artifact_slot_members "
                "WHERE profile_id=? AND scope_id=? AND kind=? ORDER BY slot_index,record_index",
                (self.store.profile_id, scope_id, kind),
            ).fetchall()
            result: Dict[int, List[str]] = defaultdict(list)
            for row in rows:
                result[int(row["slot_index"])].append(str(row["card_id"]))
            return dict(result)

        return self.store._read(read)

    def _stable_files(
        self,
        *,
        scope_id: str,
        kind: str,
        records: Mapping[str, Mapping[str, Any]],
        package_limit: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[str, int]], List[Tuple[str, int, int, str, str]]]:
        old = self._old_slots(scope_id, kind)
        old_ids = {card_id for members in old.values() for card_id in members}
        remaining_new = [card_id for card_id in records if card_id not in old_ids]
        groups: List[List[Tuple[str, Mapping[str, Any]]]] = []
        for slot_index in sorted(old):
            entries = [(card_id, records[card_id]) for card_id in old[slot_index] if card_id in records]
            groups.extend(self._chunk_group(entries, limit=package_limit))
        # 新卡只进入最后的可用槽位，避免排序变化导致旧正文整批重打包。
        header_bytes = len(b"ICPK1\n")
        used = header_bytes + sum(len(self._frame(record)) for _, record in groups[-1]) if groups else header_bytes
        for card_id in remaining_new:
            entry = (card_id, records[card_id])
            frame_bytes = len(self._frame(entry[1]))
            if header_bytes + frame_bytes > package_limit:
                raise ArtifactError("artifact_package_too_large", status=413)
            if groups and used + frame_bytes <= package_limit and len(groups[-1]) < MAX_PACKAGE_RECORDS:
                groups[-1].append(entry)
                used += frame_bytes
                continue
            groups.append([entry])
            used = header_bytes + frame_bytes
        descriptors: List[Dict[str, Any]] = []
        locations: Dict[str, Tuple[str, int]] = {}
        members: List[Tuple[str, int, int, str, str]] = []
        for slot_index, group in enumerate(groups):
            chunks = self._chunk_group(group, limit=package_limit)
            if len(chunks) != 1:
                raise ArtifactError("artifact_slot_invalid")
            chunk = chunks[0]
            body = _package([record for _, record in chunk], limit=package_limit)
            descriptor = _descriptor(kind, body, len(chunk))
            descriptors.append(dict(descriptor, _body=body))
            for record_index, (card_id, record) in enumerate(chunk, 1):
                locations[card_id] = (descriptor["name"], record_index)
                members.append((kind, slot_index, record_index, card_id, _json(record).decode("utf-8")))
        return descriptors, locations, members

    @staticmethod
    def _descriptor_page(files: Sequence[Mapping[str, Any]]) -> Tuple[bytes, Dict[str, Any]]:
        raw = _json({"files": [dict(item) for item in files]})
        digest = hashlib.sha256(raw).hexdigest()
        return raw, {
            "id": digest,
            "name": "p-%s.json" % digest[:32],
            "kind": "index",
            "sha256": digest,
            "bytes": len(raw),
            "record_count": len(files),
        }

    @staticmethod
    def _image_ids_only(value: Any) -> Any:
        """图片的物理位置属于媒体索引，不属于卡片正文。"""
        if isinstance(value, list):
            return [SnapshotFilesService._image_ids_only(item) for item in value]
        if isinstance(value, dict):
            return {key: SnapshotFilesService._image_ids_only(item) for key, item in value.items()
                    if not (value.get("type") == "image" and key in {"file", "offset", "length"})}
        return value

    def _legacy_text(self, scope_id: str) -> Dict[str, Any]:
        """只以已确认交付的版本为兼容基线，失败候选不能污染复用结果。"""
        def read(db):
            row = db.execute(
                "SELECT t.snapshot_id,m.root_json FROM artifact_transfers t "
                "JOIN artifact_manifests m ON m.profile_id=t.profile_id AND m.snapshot_id=t.snapshot_id "
                "WHERE t.profile_id=? AND t.scope_id=? AND t.status='committed' "
                "ORDER BY t.updated_at_ms DESC LIMIT 1", (self.store.profile_id, scope_id)).fetchone()
            if row is None:
                return []
            root = json.loads(row["root_json"])
            if root.get("format") == 7 and not root.get("metadata", {}).get("legacy_bound_media"):
                return []
            return db.execute(
                "SELECT f.package FROM artifact_snapshot_files s JOIN artifact_files f "
                "ON f.profile_id=s.profile_id AND f.file_id=s.file_id "
                "WHERE s.profile_id=? AND s.snapshot_id=? AND s.kind='text'",
                (self.store.profile_id, row["snapshot_id"])).fetchall()
        result = {}
        for row in self.store._read(read):
            raw = bytes(row[0])
            if not raw.startswith(b"ICPK1\n"):
                raise ArtifactError("artifact_package_corrupt", status=503)
            at = 6
            while at < len(raw):
                newline = raw.index(b"\n", at)
                size = int(raw[at:newline]); end = newline + 1 + size
                if size < 1 or size > MAX_RECORD_BYTES or raw[end:end+1] != b"\n":
                    raise ArtifactError("artifact_package_corrupt", status=503)
                record = json.loads(raw[newline+1:end]); at = end + 1
                result[record["card_id"]] = record
        return result

    def _retain_bound_media(self, records: Mapping[str, Any], files: List[Dict[str, Any]]) -> bool:
        names = set()
        def visit(value):
            if isinstance(value, dict):
                if value.get("type") == "image" and value.get("file"):
                    names.add(value["file"])
                for item in value.values(): visit(item)
            elif isinstance(value, list):
                for item in value: visit(item)
        visit(list(records.values()))
        present = {item["name"] for item in files}
        for name in sorted(names - present):
            row = self.store._read(lambda db: db.execute(
                "SELECT * FROM artifact_files WHERE profile_id=? AND name=? AND kind='media'",
                (self.store.profile_id, name)).fetchone())
            if row is None:
                raise ArtifactError("legacy_media_missing", status=503)
            files.append({"id":row["file_id"], "name":name, "kind":"media", "bytes":row["bytes"],
                          "sha256":row["sha256"], "record_count":row["record_count"], "_body":bytes(row["package"])})
        return bool(names)

    def _prepare_snapshot(self, snapshot: Any) -> Dict[str, Any]:
        existing = self._manifest(snapshot.snapshot_id)
        if existing is not None:
            if (
                existing.get("schema") != "inkcards.files.v1"
                or existing.get("snapshot_id") != snapshot.snapshot_id
                or existing.get("profile_id") != snapshot.profile_id
                or existing.get("scope_id") != snapshot.scope_id
                or str(existing.get("known_watermark")) != str(snapshot.applied_watermark)
            ):
                raise ArtifactError("artifact_snapshot_conflict", status=409)
            return existing
        try:
            metadata, _ = SnapshotService._snapshot_metadata(
                snapshot,
                profile_id=self.store.profile_id,
                scope_id=snapshot.scope_id,
                snapshot_generation=snapshot.snapshot_generation,
                applied_watermark=snapshot.applied_watermark,
            )
            payload = json.loads(snapshot.payload_json)
        except SnapshotError as error:
            raise ArtifactError(error.code, status=error.status, retryable=error.retryable) from None
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactError("snapshot_corrupt", status=503, retryable=True) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
            raise ArtifactError("snapshot_corrupt", status=503, retryable=True)
        cards = payload["cards"]
        if any(not isinstance(card, Mapping) for card in cards):
            raise ArtifactError("snapshot_corrupt", status=503, retryable=True)
        decks = metadata.get("decks")
        presets = metadata.get("presets")
        day = metadata.get("day_table")
        if not isinstance(decks, list) or not isinstance(presets, list) or not isinstance(day, Mapping):
            raise ArtifactError("snapshot_corrupt", status=503)
        decks_by_id: Dict[str, Mapping[str, Any]] = {}
        for deck in decks:
            if not isinstance(deck, Mapping) or not isinstance(deck.get("deck_id"), str):
                raise ArtifactError("snapshot_corrupt", status=503)
            decks_by_id[str(deck["deck_id"])] = deck
        presets_by_id: Dict[str, Mapping[str, Any]] = {}
        for preset in presets:
            if not isinstance(preset, Mapping) or not isinstance(preset.get("preset_id"), str):
                raise ArtifactError("snapshot_corrupt", status=503)
            key = str(preset["preset_id"])
            if key in presets_by_id:
                raise ArtifactError("scheduler_duplicate_preset", status=409)
            presets_by_id[key] = self._preset_projection(preset)
        from ..anki.media_lab import media_packages
        media_files, media_locations = media_packages([asset for card in cards for asset in card.get("media_assets", [])])
        legacy = self._legacy_text(snapshot.scope_id)
        text_records: Dict[str, Dict[str, Any]] = {}
        schedule_records: Dict[str, Dict[str, Any]] = {}
        for index, card in enumerate(cards, 1):
            card_id = card.get("card_id")
            note_id = card.get("note_id")
            deck_id = card.get("deck_id")
            if not all(isinstance(value, str) and value for value in (card_id, note_id, deck_id)):
                raise ArtifactError("snapshot_card_identity_invalid", status=409)
            if card_id in text_records:
                raise ArtifactError("snapshot_card_duplicate", status=409)
            if not isinstance(card.get("question"), str) or not isinstance(card.get("answer"), str):
                raise ArtifactError("snapshot_card_text_invalid", status=409)
            deck = decks_by_id.get(deck_id)
            if deck is None:
                raise ArtifactError("card_deck_missing", status=409)
            text_records[card_id] = {
                "card_id": card_id,
                "note_id": note_id,
                "deck_id": deck_id,
                "question": card["question"],
                "answer": card["answer"],
            }
            if "question_blocks" in card:
                for side in ("question_blocks", "answer_blocks"):
                    text_records[card_id][side] = self._image_ids_only(card[side])
            previous = legacy.get(card_id)
            if previous is not None and _json(self._image_ids_only(previous)) == _json(text_records[card_id]):
                text_records[card_id] = previous
            schedule_records[card_id] = self._schedule(card, index, deck, presets_by_id, day)

        catalog = self._catalog(metadata, cards)
        config = self._config(metadata, cards)
        metadata = dict(metadata)
        # 未知可选字段保持缺省；省去重复null键，避免真实长度牌组ID使根清单无谓溢出。
        metadata["decks"] = [{key: value for key, value in deck.items() if value is not None}
                             for deck in decks]
        # 文件 manifest 的水位只出现在根层十进制字符串字段。
        metadata.pop("applied_watermark", None)
        metadata["catalog"] = catalog
        metadata["queue_index"] = 1
        if config is not None:
            metadata["config"] = config

        text_files, text_locations, text_members = self._stable_files(
            scope_id=snapshot.scope_id, kind="text", records=text_records,
            package_limit=MAX_PACKAGE_BYTES,
        )
        schedule_files, schedule_locations, schedule_members = self._stable_files(
            scope_id=snapshot.scope_id, kind="schedule", records=schedule_records,
            package_limit=MAX_PAGE_BYTES,
        )
        files = text_files + schedule_files + media_files
        index_records: List[Dict[str, Any]] = []
        for ordinal, card in enumerate(cards, 1):
            card_id = str(card["card_id"])
            text_file, text_record = text_locations[card_id]
            schedule_file, schedule_record = schedule_locations[card_id]
            index_records.append({
                "ordinal": ordinal,
                "card_id": card_id,
                "text_file": text_file,
                "text_record": text_record,
                "schedule_file": schedule_file,
                "schedule_record": schedule_record,
            })
        index_groups: List[List[Mapping[str, Any]]] = []
        group: List[Mapping[str, Any]] = []
        for record in index_records:
            candidate = group + [record]
            try:
                _package(candidate, limit=MAX_PAGE_BYTES)
                group = candidate
            except ArtifactError:
                if not group:
                    raise
                index_groups.append(group)
                group = [record]
        if group:
            index_groups.append(group)
        index_files: List[Dict[str, Any]] = []
        for group in index_groups:
            body = _package(group, limit=MAX_PAGE_BYTES)
            index_files.append(dict(_descriptor("index", body, len(group)),
                                    queue=_queue_summary(group, schedule_records), _body=body))
        files.extend(index_files)
        media_indexes = []
        media_records = [{"id":image_id, **location} for image_id, location in sorted(media_locations.items())]
        for group in self._chunk_group([(record["id"],record) for record in media_records], limit=MAX_PAGE_BYTES):
            body = _package([record for _,record in group], limit=MAX_PAGE_BYTES)
            descriptor = dict(_descriptor("index",body,len(group)),media_index=True,_body=body)
            files.append(descriptor)
            media_indexes.append({"file":descriptor["name"],"first":group[0][0],"last":group[-1][0]})
        if media_indexes:
            metadata["media_indexes"] = media_indexes
        if self._retain_bound_media(text_records, files):
            metadata["legacy_bound_media"] = True
        if len(files) > MAX_FILE_IDS:
            raise ArtifactError("artifact_file_count_limit", status=413)
        public_files = [
            {key: value for key, value in item.items() if key != "_body"}
            for item in files
        ]
        descriptor_pages: List[Tuple[bytes, Dict[str, Any]]] = []
        current: List[Mapping[str, Any]] = []
        for descriptor in public_files:
            candidate = current + [descriptor]
            raw, _ = self._descriptor_page(candidate)
            if len(raw) <= MAX_PAGE_BYTES:
                current = candidate
                continue
            if not current:
                raise ArtifactError("artifact_descriptor_too_large", status=413)
            descriptor_pages.append(self._descriptor_page(current))
            current = [descriptor]
        if current:
            descriptor_pages.append(self._descriptor_page(current))
        names: Dict[str, str] = {}
        for item in files:
            previous = names.setdefault(str(item["name"]), str(item["id"]))
            if previous != str(item["id"]):
                raise ArtifactError("artifact_file_collision", status=409)
        for _, descriptor in descriptor_pages:
            previous = names.setdefault(str(descriptor["name"]), str(descriptor["id"]))
            if previous != str(descriptor["id"]):
                raise ArtifactError("artifact_file_collision", status=409)
        root = {
            "schema": "inkcards.files.v1",
            "format": 7 if media_files else 5,
            "snapshot_id": snapshot.snapshot_id,
            "generation": snapshot.snapshot_generation,
            "profile_id": snapshot.profile_id,
            "scope_id": snapshot.scope_id,
            "known_watermark": str(snapshot.applied_watermark),
            "card_count": len(cards),
            "metadata": metadata,
            "descriptor_pages": [item[1] for item in descriptor_pages],
        }
        root_raw = _json(root)
        if len(root_raw) > MAX_PAGE_BYTES:
            raise ArtifactError("artifact_manifest_too_large", status=413)
        all_members = text_members + schedule_members
        self._persist_snapshot(
            snapshot=snapshot,
            root_raw=root_raw,
            files=files,
            descriptor_pages=descriptor_pages,
            slot_members=all_members,
        )
        return json.loads(root_raw.decode("utf-8"))

    def _manifest(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        def read(db: Any) -> Optional[Dict[str, Any]]:
            row = db.execute(
                "SELECT root_json FROM artifact_manifests WHERE profile_id=? AND snapshot_id=?",
                (self.store.profile_id, snapshot_id),
            ).fetchone()
            if row is None:
                return None
            try:
                result = json.loads(str(row["root_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                raise StorageValidationError("artifact manifest is corrupt", "artifact_snapshot_conflict") from None
            if not isinstance(result, dict):
                raise StorageValidationError("artifact manifest is corrupt", "artifact_snapshot_conflict")
            return result

        try:
            return self.store._read(read)
        except StorageValidationError as error:
            raise self._fail_storage(error) from None

    def _persist_snapshot(
        self,
        *,
        snapshot: Any,
        root_raw: bytes,
        files: Sequence[Mapping[str, Any]],
        descriptor_pages: Sequence[Tuple[bytes, Mapping[str, Any]]],
        slot_members: Sequence[Tuple[str, int, int, str, str]],
    ) -> None:
        now = self._now()
        root_json = root_raw.decode("utf-8")
        try:
            with self.store._transaction() as db:
                row = db.execute(
                    "SELECT checksum,scope_id,snapshot_generation,application_context_json FROM snapshots "
                    "WHERE profile_id=? AND snapshot_id=?",
                    (self.store.profile_id, snapshot.snapshot_id),
                ).fetchone()
                if row is None or str(row["checksum"]) != snapshot.checksum or str(row["scope_id"]) != snapshot.scope_id or int(row["snapshot_generation"]) != snapshot.snapshot_generation:
                    raise StorageValidationError("snapshot changed during artifact export", "artifact_context_mismatch")
                context = snapshot.application_context
                if context is None or row["application_context_json"] != context.to_json():
                    raise StorageValidationError("snapshot context changed during artifact export", "artifact_context_mismatch")
                existing = db.execute(
                    "SELECT root_json FROM artifact_manifests WHERE profile_id=? AND snapshot_id=?",
                    (self.store.profile_id, snapshot.snapshot_id),
                ).fetchone()
                if existing is not None:
                    if str(existing["root_json"]) != root_json:
                        raise StorageValidationError("artifact snapshot content conflict", "artifact_snapshot_conflict")
                    return
                def register_name(name: str, file_id: str, kind: str) -> None:
                    prior = db.execute(
                        "SELECT file_id,kind FROM artifact_names WHERE profile_id=? AND name=?",
                        (self.store.profile_id, name),
                    ).fetchone()
                    if prior is not None:
                        if str(prior["file_id"]) != file_id or str(prior["kind"]) != kind:
                            raise StorageValidationError("artifact name truncation collision", "artifact_file_collision")
                        return
                    db.execute(
                        "INSERT INTO artifact_names(profile_id,name,file_id,kind) VALUES(?,?,?,?)",
                        (self.store.profile_id, name, file_id, kind),
                    )
                for item in files:
                    body = bytes(item["_body"])
                    digest = hashlib.sha256(body).hexdigest()
                    if digest != item["id"] or digest != item["sha256"] or len(body) != item["bytes"]:
                        raise StorageValidationError("artifact file digest mismatch", "artifact_file_collision")
                    register_name(str(item["name"]), digest, str(item["kind"]))
                    prior_id = db.execute(
                        "SELECT name,kind,bytes,sha256,record_count,package FROM artifact_files WHERE profile_id=? AND file_id=?",
                        (self.store.profile_id, digest),
                    ).fetchone()
                    if prior_id is not None:
                        if (
                            bytes(prior_id["package"]) != body
                            or str(prior_id["name"]) != item["name"]
                            or str(prior_id["kind"]) != item["kind"]
                            or int(prior_id["bytes"]) != len(body)
                            or str(prior_id["sha256"]) != digest
                            or int(prior_id["record_count"]) != int(item["record_count"])
                        ):
                            raise StorageValidationError("artifact file id collision", "artifact_file_collision")
                    else:
                        name_row = db.execute(
                            "SELECT file_id FROM artifact_files WHERE profile_id=? AND name=?",
                            (self.store.profile_id, item["name"]),
                        ).fetchone()
                        if name_row is not None and str(name_row["file_id"]) != digest:
                            raise StorageValidationError("artifact file name collision", "artifact_file_collision")
                        db.execute(
                            "INSERT INTO artifact_files(profile_id,file_id,name,kind,bytes,sha256,record_count,package,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                            (self.store.profile_id, digest, item["name"], item["kind"], len(body), digest, item["record_count"], body, now),
                        )
                    db.execute(
                        "INSERT INTO artifact_snapshot_files(profile_id,snapshot_id,file_id,kind,name,bytes,sha256,record_count) VALUES(?,?,?,?,?,?,?,?)",
                        (self.store.profile_id, snapshot.snapshot_id, digest, item["kind"], item["name"], len(body), digest, item["record_count"]),
                    )
                for page_index, (body, descriptor) in enumerate(descriptor_pages):
                    digest = hashlib.sha256(body).hexdigest()
                    if digest != descriptor["id"] or len(body) != descriptor["bytes"] or len(body) > MAX_PAGE_BYTES:
                        raise StorageValidationError("artifact descriptor digest mismatch", "artifact_file_collision")
                    register_name(str(descriptor["name"]), digest, "index")
                    db.execute(
                        "INSERT INTO artifact_descriptor_pages(profile_id,snapshot_id,page_index,file_id,name,sha256,bytes,page_json) VALUES(?,?,?,?,?,?,?,?)",
                        (self.store.profile_id, snapshot.snapshot_id, page_index, digest, descriptor["name"], digest, len(body), body),
                    )
                db.execute(
                    "INSERT INTO artifact_manifests(profile_id,snapshot_id,root_json,descriptor_page_count,created_at_ms) VALUES(?,?,?,?,?)",
                    (self.store.profile_id, snapshot.snapshot_id, root_json, len(descriptor_pages), now),
                )
                db.execute(
                    "DELETE FROM artifact_slot_members WHERE profile_id=? AND scope_id=? AND kind IN ('text','schedule')",
                    (self.store.profile_id, snapshot.scope_id),
                )
                for kind, slot, record_index, card_id, record_json in slot_members:
                    db.execute(
                        "INSERT INTO artifact_slot_members(profile_id,scope_id,kind,slot_index,record_index,card_id,record_json) VALUES(?,?,?,?,?,?,?)",
                        (self.store.profile_id, snapshot.scope_id, kind, slot, record_index, card_id, record_json),
                    )
        except StorageValidationError as error:
            raise self._fail_storage(error) from None

    @staticmethod
    def _ids(value: Any, *, name: str) -> List[str]:
        if not isinstance(value, list) or len(value) > MAX_FILE_IDS:
            raise ArtifactError("%s_invalid" % name)
        if any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in value):
            raise ArtifactError("%s_invalid" % name)
        if len(set(value)) != len(value):
            raise ArtifactError("%s_invalid" % name)
        return sorted(value)

    @staticmethod
    def _commit_key(value: Any) -> str:
        if not isinstance(value, str) or len(value) != 36:
            raise ArtifactError("invalid_commit_key")
        try:
            normalized = str(uuid.UUID(value))
        except (ValueError, AttributeError):
            raise ArtifactError("invalid_commit_key") from None
        if value.lower() != normalized:
            raise ArtifactError("invalid_commit_key")
        return normalized

    def _snapshot_context(self, snapshot_id: str, scope_id: str) -> Tuple[Any, str, Dict[str, Any]]:
        try:
            snapshot = self.store.get_snapshot(snapshot_id, scope_id=scope_id)
        except StorageValidationError as error:
            raise ArtifactError(error.code, status=404 if error.code == "snapshot_not_found" else 422) from None
        context = snapshot.application_context
        if context is None:
            raise ArtifactError("snapshot_context_missing", status=409)
        if (
            context.profile_id != self.store.profile_id
            or context.snapshot_id != snapshot.snapshot_id
            or context.snapshot_checksum != snapshot.checksum
            or context.snapshot_generation != snapshot.snapshot_generation
        ):
            raise ArtifactError("snapshot_context_mismatch", status=409)
        try:
            generation = self.store.get_collection_generation()
        except StorageValidationError as error:
            raise ArtifactError(error.code, status=409) from None
        if generation != snapshot.snapshot_generation:
            raise ArtifactError("snapshot_generation_stale", status=409)
        if self.require_prepared:
            manifest = self._manifest(snapshot.snapshot_id)
            if manifest is None:
                raise ArtifactError("snapshot_not_prepared", status=409, retryable=True)
        else:
            manifest = self._prepare_snapshot(snapshot)
        context_json = context.to_json()
        return snapshot, context_json, manifest

    def open_transfer(
        self,
        *,
        device_id: str,
        session_id: str,
        snapshot_id: str,
        scope_id: str,
        known_file_ids: Any = None,
        expected_profile_id: Optional[str] = None,
        expected_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        """为显式指定且已刷新的既有 snapshot 建立持久传输。"""

        try:
            gate = self.store.pull_gate(device_id, session_id)
        except StorageValidationError as error:
            raise ArtifactError(error.code, status=409 if error.code == "identity_mismatch" else 422) from None
        if not gate.allowed:
            raise ArtifactError(gate.reason or "pull_gate_blocked", status=409)
        snapshot, context_json, manifest = self._snapshot_context(snapshot_id, scope_id)
        if snapshot.applied_watermark < (gate.applied_watermark or 0):
            raise ArtifactError("snapshot_watermark_behind", status=409)
        if expected_profile_id is not None and expected_profile_id != snapshot.profile_id:
            raise ArtifactError("profile_mismatch", status=409)
        if expected_generation is not None and expected_generation != snapshot.snapshot_generation:
            raise ArtifactError("generation_mismatch", status=409)
        known = self._ids([] if known_file_ids is None else known_file_ids, name="known_file_ids")
        transfer_watermark = gate.applied_watermark or 0
        transfer_manifest = dict(manifest)
        transfer_manifest["known_watermark"] = str(transfer_watermark)
        manifest_hash = hashlib.sha256(_json(transfer_manifest)).hexdigest()
        transfer_id = str(uuid.uuid4())
        now = self._now()
        try:
            with self.store._transaction() as db:
                session = db.execute(
                    "SELECT 1 FROM sessions WHERE profile_id=? AND device_id=? AND session_id=? AND closed_at_ms IS NULL",
                    (self.store.profile_id, device_id, session_id),
                ).fetchone()
                if session is None:
                    raise StorageValidationError("identity_mismatch", "identity_mismatch")
                row = db.execute(
                    "SELECT checksum,scope_id,snapshot_generation,application_context_json FROM snapshots WHERE profile_id=? AND snapshot_id=?",
                    (self.store.profile_id, snapshot_id),
                ).fetchone()
                if row is None or row["checksum"] != snapshot.checksum or row["application_context_json"] != context_json:
                    raise StorageValidationError("artifact context changed", "artifact_context_mismatch")
                profile = db.execute(
                    "SELECT collection_generation FROM profile_meta WHERE profile_id=?",
                    (self.store.profile_id,),
                ).fetchone()
                if profile is None or int(profile["collection_generation"]) != snapshot.snapshot_generation:
                    raise StorageValidationError("snapshot changed before artifact transfer", "artifact_context_mismatch")
                db.execute(
                    "INSERT INTO artifact_transfers(profile_id,transfer_id,device_id,session_id,snapshot_id,scope_id,generation,known_watermark,snapshot_checksum,context_json,manifest_hash,status,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                    (self.store.profile_id, transfer_id, device_id, session_id, snapshot_id,
                     scope_id, snapshot.snapshot_generation, transfer_watermark,
                     snapshot.checksum, context_json, manifest_hash, now, now),
                )
                file_rows = db.execute(
                    "SELECT file_id FROM artifact_snapshot_files WHERE profile_id=? AND snapshot_id=?",
                    (self.store.profile_id, snapshot_id),
                ).fetchall()
                manifest_ids = {str(row["file_id"]) for row in file_rows}
                known_ids = set(known) & manifest_ids
                for file_id in sorted(manifest_ids):
                    db.execute(
                        "INSERT INTO artifact_transfer_files(profile_id,transfer_id,file_id,known,served) VALUES(?,?,?,?,0)",
                        (self.store.profile_id, transfer_id, file_id, int(file_id in known_ids)),
                    )
                page_rows = db.execute(
                    "SELECT page_index FROM artifact_descriptor_pages WHERE profile_id=? AND snapshot_id=? ORDER BY page_index",
                    (self.store.profile_id, snapshot_id),
                ).fetchall()
                for page in page_rows:
                    db.execute(
                        "INSERT INTO artifact_transfer_pages(profile_id,transfer_id,page_index,served) VALUES(?,?,?,0)",
                        (self.store.profile_id, transfer_id, int(page["page_index"])),
                    )
        except StorageValidationError as error:
            raise self._fail_storage(error) from None
        return {"transfer_id": transfer_id, "manifest": transfer_manifest}

    def _transfer(self, db: Any, *, device_id: str, session_id: str, transfer_id: str) -> Any:
        row = db.execute(
            "SELECT * FROM artifact_transfers WHERE profile_id=? AND device_id=? AND session_id=? AND transfer_id=?",
            (self.store.profile_id, device_id, session_id, transfer_id),
        ).fetchone()
        if row is None:
            raise ArtifactError("artifact_transfer_not_found", status=404)
        return row

    @staticmethod
    def _check_transfer_snapshot(db: Any, profile_id: str, transfer: Any) -> None:
        row = db.execute(
            "SELECT checksum,scope_id,snapshot_generation,application_context_json FROM snapshots WHERE profile_id=? AND snapshot_id=?",
            (profile_id, str(transfer["snapshot_id"])),
        ).fetchone()
        if (
            row is None
            or str(row["checksum"]) != str(transfer["snapshot_checksum"])
            or str(row["scope_id"]) != str(transfer["scope_id"])
            or int(row["snapshot_generation"]) != int(transfer["generation"])
            or str(row["application_context_json"]) != str(transfer["context_json"])
        ):
            raise ArtifactError("artifact_context_mismatch", status=409)
        manifest = db.execute(
            "SELECT root_json FROM artifact_manifests WHERE profile_id=? AND snapshot_id=?",
            (profile_id, str(transfer["snapshot_id"])),
        ).fetchone()
        if manifest is None:
            raise ArtifactError("artifact_context_mismatch", status=409)
        try:
            parsed = json.loads(str(manifest["root_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactError("artifact_manifest_corrupt", status=503, retryable=True) from None
        transfer_watermark = transfer["known_watermark"]
        if not _integer(transfer_watermark) or transfer_watermark < 0:
            raise ArtifactError("artifact_context_mismatch", status=409)
        if isinstance(parsed, dict):
            parsed = dict(parsed)
            parsed["known_watermark"] = str(transfer_watermark)
        if not isinstance(parsed, dict) or hashlib.sha256(_json(parsed)).hexdigest() != str(transfer["manifest_hash"]):
            raise ArtifactError("artifact_context_mismatch", status=409)

    def manifest_page(self, *, device_id: str, session_id: str, transfer_id: str, page_index: Any) -> bytes:
        if not _integer(page_index) or page_index < 0:
            raise ArtifactError("invalid_page_index")
        with self.store._transaction() as db:
            transfer = self._transfer(db, device_id=device_id, session_id=session_id, transfer_id=transfer_id)
            self._check_transfer_snapshot(db, self.store.profile_id, transfer)
            row = db.execute(
                "SELECT page_json,bytes,sha256 FROM artifact_descriptor_pages WHERE profile_id=? AND snapshot_id=? AND page_index=?",
                (self.store.profile_id, str(transfer["snapshot_id"]), page_index),
            ).fetchone()
            if row is None:
                raise ArtifactError("artifact_manifest_page_not_found", status=404)
            body = bytes(row["page_json"])
            if len(body) != int(row["bytes"]) or hashlib.sha256(body).hexdigest() != str(row["sha256"]):
                raise ArtifactError("artifact_manifest_page_corrupt", status=503, retryable=True)
            db.execute(
                "UPDATE artifact_transfer_pages SET served=1 WHERE profile_id=? AND transfer_id=? AND page_index=?",
                (self.store.profile_id, transfer_id, page_index),
            )
            return body

    def file(self, *, device_id: str, session_id: str, transfer_id: str, file_id: str) -> bytes:
        if not isinstance(file_id, str) or not _SHA256.fullmatch(file_id):
            raise ArtifactError("invalid_file_id")
        with self.store._transaction() as db:
            transfer = self._transfer(db, device_id=device_id, session_id=session_id, transfer_id=transfer_id)
            self._check_transfer_snapshot(db, self.store.profile_id, transfer)
            declared = db.execute(
                "SELECT 1 FROM artifact_transfer_files WHERE profile_id=? AND transfer_id=? AND file_id=?",
                (self.store.profile_id, transfer_id, file_id),
            ).fetchone()
            if declared is None:
                raise ArtifactError("artifact_file_not_in_manifest", status=404)
            row = db.execute(
                "SELECT package,bytes,sha256 FROM artifact_files WHERE profile_id=? AND file_id=?",
                (self.store.profile_id, file_id),
            ).fetchone()
            if row is None:
                raise ArtifactError("artifact_file_missing", status=503, retryable=True)
            body = bytes(row["package"])
            if len(body) != int(row["bytes"]) or hashlib.sha256(body).hexdigest() != str(row["sha256"]):
                raise ArtifactError("artifact_file_corrupt", status=503, retryable=True)
            db.execute(
                "UPDATE artifact_transfer_files SET served=1 WHERE profile_id=? AND transfer_id=? AND file_id=?",
                (self.store.profile_id, transfer_id, file_id),
            )
            return body

    def commit(
        self,
        *,
        device_id: str,
        session_id: str,
        transfer_id: str,
        commit_key: Any,
        present_file_ids: Any = None,
    ) -> Dict[str, Any]:
        key = self._commit_key(commit_key)
        present = self._ids([] if present_file_ids is None else present_file_ids, name="present_file_ids")
        normalized_present = json.dumps(present, separators=(",", ":"))
        now = self._now()

        def read_existing(db: Any) -> Optional[Dict[str, Any]]:
            transfer = self._transfer(db, device_id=device_id, session_id=session_id, transfer_id=transfer_id)
            self._check_transfer_snapshot(db, self.store.profile_id, transfer)
            if transfer["status"] != "committed":
                return None
            if str(transfer["commit_key"]) != key or str(transfer["present_file_ids_json"]) != normalized_present:
                raise ArtifactError("artifact_transfer_conflict", status=409)
            try:
                receipt = json.loads(str(transfer["receipt_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ArtifactError("artifact_receipt_corrupt", status=503, retryable=True) from None
            if not isinstance(receipt, dict):
                raise ArtifactError("artifact_receipt_corrupt", status=503, retryable=True)
            return receipt

        prior = self.store._read(read_existing)
        if prior is not None:
            return prior
        try:
            gate = self.store.pull_gate(device_id, session_id)
        except StorageValidationError as error:
            raise ArtifactError(error.code, status=409 if error.code == "identity_mismatch" else 422) from None
        if not gate.allowed:
            raise ArtifactError(gate.reason or "pull_gate_blocked", status=409)
        with self.store._transaction() as db:
            transfer = self._transfer(db, device_id=device_id, session_id=session_id, transfer_id=transfer_id)
            self._check_transfer_snapshot(db, self.store.profile_id, transfer)
            profile = db.execute(
                "SELECT collection_generation FROM profile_meta WHERE profile_id=?",
                (self.store.profile_id,),
            ).fetchone()
            if profile is None or int(profile["collection_generation"]) != int(transfer["generation"]):
                raise ArtifactError("snapshot_generation_stale", status=409)
            if transfer["status"] == "committed":
                if str(transfer["commit_key"]) != key or str(transfer["present_file_ids_json"]) != normalized_present:
                    raise ArtifactError("artifact_transfer_conflict", status=409)
                try:
                    receipt = json.loads(str(transfer["receipt_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ArtifactError("artifact_receipt_corrupt", status=503, retryable=True) from None
                if not isinstance(receipt, dict):
                    raise ArtifactError("artifact_receipt_corrupt", status=503, retryable=True)
                return receipt
            file_rows = db.execute(
                "SELECT file_id,known,served FROM artifact_transfer_files WHERE profile_id=? AND transfer_id=?",
                (self.store.profile_id, transfer_id),
            ).fetchall()
            manifest_ids = {str(row["file_id"]) for row in file_rows}
            if not set(present).issubset(manifest_ids):
                raise ArtifactError("present_file_ids_invalid")
            available = set(present) | {
                str(row["file_id"]) for row in file_rows
                if int(row["known"]) == 1 or int(row["served"]) == 1
            }
            if available != manifest_ids:
                raise ArtifactError("artifact_files_incomplete", status=409)
            page_rows = db.execute(
                "SELECT served FROM artifact_transfer_pages WHERE profile_id=? AND transfer_id=?",
                (self.store.profile_id, transfer_id),
            ).fetchall()
            if any(int(row["served"]) != 1 for row in page_rows):
                raise ArtifactError("artifact_manifest_pages_incomplete", status=409)
            receipt = {
                "status": "committed",
                "transfer_id": transfer_id,
                "snapshot_id": str(transfer["snapshot_id"]),
                "generation": _wire_numbers(int(transfer["generation"])),
                "known_watermark": str(transfer["known_watermark"]),
                "commit_key": key,
            }
            receipt_json = _json(receipt).decode("utf-8")
            db.execute(
                "UPDATE artifact_transfers SET commit_key=?,present_file_ids_json=?,receipt_json=?,status='committed',updated_at_ms=? WHERE profile_id=? AND transfer_id=? AND status='prepared'",
                (key, normalized_present, receipt_json, now, self.store.profile_id, transfer_id),
            )
            return receipt

    def status(self, *, device_id: str, session_id: str, transfer_id: str, commit_key: Any) -> Dict[str, Any]:
        key = self._commit_key(commit_key)
        def read(db: Any) -> Dict[str, Any]:
            transfer = self._transfer(db, device_id=device_id, session_id=session_id, transfer_id=transfer_id)
            self._check_transfer_snapshot(db, self.store.profile_id, transfer)
            if transfer["status"] == "committed":
                if str(transfer["commit_key"]) != key:
                    raise ArtifactError("artifact_transfer_conflict", status=409)
                try:
                    result = json.loads(str(transfer["receipt_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ArtifactError("artifact_receipt_corrupt", status=503, retryable=True) from None
                if not isinstance(result, dict):
                    raise ArtifactError("artifact_receipt_corrupt", status=503, retryable=True)
                return result
            return {"status": "prepared"}
        return self.store._read(read)


__all__ = ["ArtifactError", "SnapshotFilesService"]


