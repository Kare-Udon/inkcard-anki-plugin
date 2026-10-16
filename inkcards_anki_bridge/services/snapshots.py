"""只读 typed snapshot 发布、上传优先 gate、分页和一次性 permit。

本模块只消费已经由 Anki 适配器生成的 TypedSnapshot，不取得 collection，
不读取媒体，也不执行任何 Anki 写回。所有 permit/page/commit 状态都委托给同一
profile 的 SQLite ProfileStore，因此进程重启不会把内存计数当成完成证据。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets as _stdlib_secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..anki.models import TypedSnapshot
from ..anki.application_context import CapturedApplicationContext, SnapshotApplicationContext
from ..anki.snapshot_jobs import make_scope, SnapshotJobError
from ..domain.sync_gate import LUA_SAFE_INTEGER_MAX
from ..storage import (
    CommitUnknownError,
    ProfileStore,
    StorageValidationError,
    PermitRecord,
    PullProgressRecord,
)


class SnapshotError(Exception):
    """可安全返回给 device route 的稳定 snapshot 错误。"""

    def __init__(self, code: str, *, status: int = 422, retryable: bool = False) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class PublishedSnapshot:
    """发布成功后的不可变快照摘要。"""

    snapshot_id: str
    scope_id: str
    snapshot_generation: int
    applied_watermark: int
    checksum: str
    manifest: Dict[str, Any]
    payload_bytes: bytes

    @property
    def generation(self) -> int:
        return self.snapshot_generation

    def to_payload(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "scope_id": self.scope_id,
            "snapshot_generation": self.snapshot_generation,
            "applied_watermark": {"known": self.applied_watermark},
            "checksum": self.checksum,
            "manifest": dict(self.manifest),
        }


SnapshotPublication = PublishedSnapshot


_SNAPSHOT_PAYLOAD_KEYS = frozenset(
    {
        "snapshot_schema",
        "manifest",
        "decks",
        "presets",
        "day_table",
        "cards",
        "complete",
        "unsupported",
        "profile_id",
        "scope_id",
        "snapshot_generation",
        "applied_watermark",
    }
)
_SNAPSHOT_METADATA_KEYS = frozenset(
    {
        "schema",
        "snapshot_schema",
        "manifest",
        "decks",
        "presets",
        "day_table",
        "complete",
        "unsupported",
        "profile_id",
        "scope_id",
        "snapshot_generation",
        "applied_watermark",
    }
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SnapshotError("invalid_%s" % name)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SnapshotError("invalid_%s" % name)
    return value


def _safe_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > LUA_SAFE_INTEGER_MAX
    ):
        raise SnapshotError("invalid_%s" % name)
    return value


def _watermark(value: Any) -> int:
    if isinstance(value, Mapping):
        if set(value) != {"known"}:
            raise SnapshotError("invalid_applied_watermark")
        value = value["known"]
    return _safe_int(value, "applied_watermark")


def _plain_json(value: Any) -> Any:
    """将 typed 模型递归转换为有限 JSON 值，不猜测未知对象字段。"""

    if isinstance(value, Enum):
        return _plain_json(value.value)
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SnapshotError("snapshot_invalid")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if value < -LUA_SAFE_INTEGER_MAX or value > LUA_SAFE_INTEGER_MAX:
            raise SnapshotError("snapshot_invalid")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError("snapshot_invalid")
        return value
    raise SnapshotError("snapshot_invalid")


def _canonical(value: Any) -> bytes:
    try:
        plain = _plain_json(value)
        return json.dumps(
            plain,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except SnapshotError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise SnapshotError("snapshot_invalid") from None


def _decode_object(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise SnapshotError("snapshot_corrupt") from None
    if not isinstance(value, dict):
        raise SnapshotError("snapshot_corrupt")
    return value


class SnapshotService:
    """绑定一个 profile store 的只读快照服务。"""

    def __init__(
        self,
        store: ProfileStore,
        *,
        page_bytes: int = 8192,
        max_cards_per_page: int = 100,
        permit_ttl_ms: int = 1_800_000,
        clock: Optional[Callable[[], int]] = None,
        secrets_source: Optional[Any] = None,
    ) -> None:
        if not isinstance(store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        if (
            isinstance(page_bytes, bool)
            or not isinstance(page_bytes, int)
            or page_bytes <= 0
            or page_bytes > 1_048_576
        ):
            raise ValueError("page_bytes must be a positive bounded integer")
        if (
            isinstance(max_cards_per_page, bool)
            or not isinstance(max_cards_per_page, int)
            or max_cards_per_page <= 0
        ):
            raise ValueError("max_cards_per_page must be a positive integer")
        if (
            isinstance(permit_ttl_ms, bool)
            or not isinstance(permit_ttl_ms, int)
            or permit_ttl_ms <= 0
        ):
            raise ValueError("permit_ttl_ms must be a positive integer")
        self.store = store
        self.page_bytes = page_bytes
        self.max_cards_per_page = max_cards_per_page
        self.permit_ttl_ms = permit_ttl_ms
        self._clock = clock or _now_ms
        self._entropy = secrets_source or _stdlib_secrets

    def close(self) -> None:
        """服务无独立连接；保留显式 no-op 以便调用方按生命周期关闭。"""

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SnapshotError("invalid_timestamp")
        return value

    def _token_bytes(self, length: int) -> bytes:
        generator = getattr(self._entropy, "token_bytes", None)
        if not callable(generator):
            raise SnapshotError("snapshot_entropy_unavailable", status=503, retryable=True)
        value = generator(length)
        if not isinstance(value, (bytes, bytearray)) or len(value) != length:
            raise SnapshotError("snapshot_entropy_unavailable", status=503, retryable=True)
        return bytes(value)

    @staticmethod
    def _storage_error(error: StorageValidationError) -> SnapshotError:
        status = 409 if error.code in {
            "identity_mismatch",
            "generation_mismatch",
            "permit_already_issued",
            "permit_already_used",
            "permit_expired",
            "permit_recovery_conflict",
            "permit_recovery_ambiguous",
            "snapshot_immutable_conflict",
            "snapshot_context_conflict",
            "snapshot_context_binding_mismatch",
            "page_out_of_order",
            "pull_incomplete",
        } else 422
        return SnapshotError(error.code, status=status)

    @staticmethod
    def _snapshot_dict(snapshot: Any) -> Dict[str, Any]:
        if isinstance(snapshot, TypedSnapshot):
            value = snapshot.to_dict()
        elif isinstance(snapshot, Mapping):
            value = dict(snapshot)
        else:
            exporter = getattr(snapshot, "to_dict", None)
            if not callable(exporter):
                exporter = getattr(snapshot, "as_dict", None)
            if not callable(exporter):
                raise SnapshotError("snapshot_invalid")
            value = exporter()
        if not isinstance(value, Mapping):
            raise SnapshotError("snapshot_invalid")
        result = _plain_json(value)
        if not isinstance(result, dict):
            raise SnapshotError("snapshot_invalid")
        if result.get("complete") is False:
            raise SnapshotError("snapshot_incomplete", status=409)
        # 已学卡没有可证明的时间时，准备阶段即拒绝，避免下载后才失败。
        for code in ("scheduler_last_review_time_missing", "scheduler_last_review_time_source_unavailable"):
            if code in result.get("unsupported", ()):
                raise SnapshotError(code, status=409)
        cards = result.get("cards")
        if not isinstance(cards, list):
            raise SnapshotError("snapshot_invalid")
        return result

    @staticmethod
    def _snapshot_metadata(
        snapshot: Any,
        *,
        profile_id: str,
        scope_id: str,
        snapshot_generation: int,
        applied_watermark: int,
    ) -> Tuple[Dict[str, Any], str]:
        """从已持久化的完整 payload 严格投影出无 cards metadata。"""

        try:
            if not isinstance(snapshot.payload_json, str):
                raise SnapshotError("snapshot_corrupt")
            payload_wire = snapshot.payload_json.encode("utf-8")
            if (
                not isinstance(snapshot.checksum, str)
                or hashlib.sha256(payload_wire).hexdigest() != snapshot.checksum
                or not isinstance(snapshot.payload_bytes, int)
                or isinstance(snapshot.payload_bytes, bool)
                or snapshot.payload_bytes != len(payload_wire)
            ):
                raise SnapshotError("snapshot_corrupt")
            payload = _decode_object(snapshot.payload_json)
            if set(payload) != _SNAPSHOT_PAYLOAD_KEYS:
                raise SnapshotError("snapshot_corrupt")
            if _canonical(payload) != payload_wire:
                raise SnapshotError("snapshot_corrupt")

            if (
                not isinstance(payload["snapshot_schema"], str)
                or not payload["snapshot_schema"]
                or not isinstance(payload["manifest"], dict)
                or not isinstance(payload["decks"], list)
                or not isinstance(payload["presets"], list)
                or not isinstance(payload["day_table"], dict)
                or not isinstance(payload["cards"], list)
                or any(not isinstance(card, dict) for card in payload["cards"])
                or not isinstance(payload["complete"], bool)
                or payload["complete"] is not True
                or not isinstance(payload["unsupported"], list)
                or any(not isinstance(item, str) for item in payload["unsupported"])
                or not isinstance(payload["profile_id"], str)
                or not isinstance(payload["scope_id"], str)
            ):
                raise SnapshotError("snapshot_corrupt")
            payload_generation = _safe_int(
                payload["snapshot_generation"], "snapshot_generation"
            )
            payload_watermark = payload["applied_watermark"]
            if (
                not isinstance(payload_watermark, dict)
                or set(payload_watermark) != {"known"}
            ):
                raise SnapshotError("snapshot_corrupt")
            payload_watermark_value = _safe_int(
                payload_watermark["known"], "applied_watermark"
            )
            if (
                payload["profile_id"] != profile_id
                or payload["profile_id"] != snapshot.profile_id
                or payload["scope_id"] != scope_id
                or payload["scope_id"] != snapshot.scope_id
                or payload_generation != snapshot_generation
                or payload_generation != snapshot.snapshot_generation
                or snapshot.scope_generation != snapshot.snapshot_generation
                or payload_watermark_value != applied_watermark
                or payload_watermark_value != snapshot.applied_watermark
            ):
                raise SnapshotError("snapshot_corrupt")

            metadata = {
                "schema": "inkcards.snapshot-metadata.v1",
                "snapshot_schema": payload["snapshot_schema"],
                "manifest": payload["manifest"],
                "decks": payload["decks"],
                "presets": payload["presets"],
                "day_table": payload["day_table"],
                "complete": payload["complete"],
                "unsupported": payload["unsupported"],
                "profile_id": payload["profile_id"],
                "scope_id": payload["scope_id"],
                "snapshot_generation": payload["snapshot_generation"],
                "applied_watermark": payload["applied_watermark"],
            }
            if set(metadata) != _SNAPSHOT_METADATA_KEYS or "cards" in metadata:
                raise SnapshotError("snapshot_corrupt")
            metadata_hash = hashlib.sha256(_canonical(metadata)).hexdigest()
            return metadata, metadata_hash
        except SnapshotError:
            raise SnapshotError("snapshot_corrupt", status=503, retryable=True) from None
        except (TypeError, UnicodeError, ValueError, OverflowError):
            raise SnapshotError("snapshot_corrupt", status=503, retryable=True) from None

    @staticmethod
    def _generation(
        generation: Optional[int],
        snapshot_generation: Optional[int],
        scope_generation: Optional[int],
    ) -> Optional[int]:
        values = [
            value
            for value in (generation, snapshot_generation, scope_generation)
            if value is not None
        ]
        if not values:
            return None
        normalized = [_safe_int(value, "snapshot_generation") for value in values]
        if len(set(normalized)) != 1:
            raise SnapshotError("generation_mismatch", status=409)
        return normalized[0]

    @staticmethod
    def _cursor(snapshot_id: str, page_index: int) -> str:
        digest = hashlib.sha256(
            ("%s:%s" % (snapshot_id, page_index)).encode("utf-8")
        ).hexdigest()
        return "c_" + digest[:48]

    def _page_wire(
        self,
        *,
        snapshot_id: str,
        snapshot_hash: str,
        scope_id: str,
        generation: int,
        applied_watermark: int,
        page_index: int,
        cards: Sequence[Mapping[str, Any]],
        is_last: bool,
    ) -> Tuple[bytes, str, Optional[str]]:
        card_list = [_plain_json(card) for card in cards]
        page_hash = hashlib.sha256(
            _canonical({"page_index": page_index, "cards": card_list})
        ).hexdigest()
        cursor = None if is_last else self._cursor(snapshot_id, page_index)
        page: Dict[str, Any] = {
            "protocol": "x4-2",
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
            "scope_id": scope_id,
            "snapshot_generation": generation,
            "applied_watermark": {"known": applied_watermark},
            "page_index": page_index,
            "cards": card_list,
            "page_hash": page_hash,
            "cursor": cursor,
            "complete": is_last,
            "next_page_index": page_index + 1,
            "received_pages": page_index + 1,
            "page_bytes": 0,
        }
        # page_bytes is part of the wire envelope. Iterate to its fixed
        # decimal representation so the stored budget is the actual UTF-8 size.
        for _ in range(4):
            wire = _canonical(page)
            measured = len(wire)
            if page["page_bytes"] == measured:
                break
            page["page_bytes"] = measured
        wire = _canonical(page)
        return wire, page_hash, cursor

    def publish(
        self,
        snapshot: Any,
        *,
        scope_id: str,
        generation: Optional[int] = None,
        applied_watermark: Optional[int] = None,
        snapshot_id: Optional[str] = None,
        snapshot_generation: Optional[int] = None,
        scope_generation: Optional[int] = None,
        source_scope: Optional[Mapping[str, Any]] = None,
    ) -> PublishedSnapshot:
        """将 typed snapshot 转成 canonical bytes、预编码页并一次事务发布。"""

        scope_id = _validate_text(scope_id, "scope_id", 128)
        normalized_source: Optional[Dict[str, Any]] = None
        if source_scope is not None:
            normalized_source = self._validated_source_scope(source_scope, scope_id)
        root = self._snapshot_dict(snapshot)
        captured_context = getattr(snapshot, "application_context", None)
        if captured_context is not None and not isinstance(
            captured_context, CapturedApplicationContext
        ):
            raise SnapshotError("snapshot_context_invalid")
        selected_generation = self._generation(
            generation, snapshot_generation, scope_generation
        )
        if selected_generation is None:
            selected_generation = 0
        selected_watermark = (
            0 if applied_watermark is None else _watermark(applied_watermark)
        )
        manifest_source = root.get("manifest")
        if not isinstance(manifest_source, Mapping):
            manifest_source = {}
        manifest_source = _plain_json(manifest_source)
        if not isinstance(manifest_source, dict):
            raise SnapshotError("snapshot_invalid")
        if captured_context is not None:
            if (
                captured_context.collection_generation != selected_generation
                or captured_context.collection_generation
                != manifest_source.get("profile_generation")
            ):
                raise SnapshotError("snapshot_context_generation_mismatch", status=409)
            if (
                captured_context.profile_identity != manifest_source.get("profile_identity")
                or captured_context.runtime_epoch != manifest_source.get("runtime_epoch")
                or captured_context.anki_version != manifest_source.get("anki_version")
                or captured_context.anki_build != manifest_source.get("anki_build")
            ):
                raise SnapshotError("snapshot_context_binding_mismatch", status=409)
        cards = root["cards"]
        card_count = manifest_source.get("card_count", len(cards))
        included_card_count = manifest_source.get("included_card_count", len(cards))
        skipped = manifest_source.get("skipped", [])
        skipped_count = manifest_source.get(
            "skipped_count", len(skipped) if isinstance(skipped, list) else 0
        )
        card_count = _safe_int(card_count, "card_count")
        included_card_count = _safe_int(included_card_count, "included_card_count")
        skipped_count = _safe_int(skipped_count, "skipped_count")
        if included_card_count != len(cards) or card_count < included_card_count:
            raise SnapshotError("snapshot_invalid")
        payload_object = dict(root)
        payload_object["profile_id"] = self.store.profile_id
        payload_object["scope_id"] = scope_id
        payload_object["snapshot_generation"] = selected_generation
        payload_object["applied_watermark"] = {"known": selected_watermark}
        payload_bytes = _canonical(payload_object)
        checksum = hashlib.sha256(payload_bytes).hexdigest()
        selected_snapshot_id = (
            _validate_text(snapshot_id, "snapshot_id", 256)
            if snapshot_id is not None
            else "sha256:" + checksum
        )
        bound_context_json = None
        if captured_context is not None:
            try:
                bound_context_json = SnapshotApplicationContext.bind(
                    captured_context,
                    profile_id=self.store.profile_id,
                    snapshot_id=selected_snapshot_id,
                    snapshot_checksum=checksum,
                    snapshot_generation=selected_generation,
                ).to_json()
            except (TypeError, ValueError):
                raise SnapshotError("snapshot_context_binding_mismatch", status=409) from None
        prepared_cards = [card for card in cards if isinstance(card, Mapping)]
        if len(prepared_cards) != len(cards):
            raise SnapshotError("snapshot_invalid")

        # Greedy packing is deterministic. The full response envelope, including
        # escaped UTF-8 card text and page metadata, consumes page_bytes.
        chunks: List[List[Mapping[str, Any]]] = []
        current: List[Mapping[str, Any]] = []
        for card in prepared_cards:
            if len(current) >= self.max_cards_per_page:
                chunks.append(current)
                current = []
            candidate = current + [card]
            probe, _, _ = self._page_wire(
                snapshot_id=selected_snapshot_id,
                snapshot_hash=checksum,
                scope_id=scope_id,
                generation=selected_generation,
                applied_watermark=selected_watermark,
                page_index=len(chunks),
                cards=candidate,
                is_last=False,
            )
            if len(probe) > self.page_bytes:
                if not current:
                    if len(prepared_cards) == 1 and not chunks:
                        final_probe, _, _ = self._page_wire(
                            snapshot_id=selected_snapshot_id,
                            snapshot_hash=checksum,
                            scope_id=scope_id,
                            generation=selected_generation,
                            applied_watermark=selected_watermark,
                            page_index=0,
                            cards=[card],
                            is_last=True,
                        )
                        if len(final_probe) <= self.page_bytes:
                            current = [card]
                            continue
                    raise SnapshotError("snapshot_unpageable", status=413)
                chunks.append(current)
                current = [card]
            else:
                current = candidate
        if current or not chunks:
            chunks.append(current)

        pages: List[Tuple[int, str, str, int, Optional[str], bool]] = []
        page_wire_sizes: List[int] = []
        for page_index, chunk in enumerate(chunks):
            wire, page_hash, cursor = self._page_wire(
                snapshot_id=selected_snapshot_id,
                snapshot_hash=checksum,
                scope_id=scope_id,
                generation=selected_generation,
                applied_watermark=selected_watermark,
                page_index=page_index,
                cards=chunk,
                is_last=page_index == len(chunks) - 1,
            )
            if len(wire) > self.page_bytes:
                raise SnapshotError("snapshot_unpageable", status=413)
            page_json = wire.decode("utf-8")
            pages.append(
                (
                    page_index,
                    page_json,
                    page_hash,
                    len(wire),
                    cursor,
                    page_index == len(chunks) - 1,
                )
            )
            page_wire_sizes.append(len(wire))
        preflight = {
            "schema": "inkcards.snapshot-preflight.v1",
            "profile_id": self.store.profile_id,
            "scope_id": scope_id,
            "snapshot_generation": selected_generation,
            "applied_watermark": {"known": selected_watermark},
            "card_count": card_count,
            "included_card_count": included_card_count,
            "skipped_count": skipped_count,
            "skipped": skipped if isinstance(skipped, list) else [],
            "skip_counts": manifest_source.get("skip_counts", {}),
            "payload_bytes": len(payload_bytes),
            "page_count": len(pages),
            "peak_bytes_estimate": len(payload_bytes) + max(page_wire_sizes),
        }
        if normalized_source is not None:
            preflight["source_scope"] = normalized_source
        preflight_bytes = _canonical(preflight)
        try:
            record = self.store.save_snapshot(
                snapshot_id=selected_snapshot_id,
                scope_id=scope_id,
                generation=selected_generation,
                applied_watermark=selected_watermark,
                payload_json=payload_bytes.decode("utf-8"),
                checksum=checksum,
                manifest_json=preflight_bytes.decode("utf-8"),
                card_count=card_count,
                included_card_count=included_card_count,
                skipped_count=skipped_count,
                payload_bytes=len(payload_bytes),
                page_count=len(pages),
                peak_bytes_estimate=len(payload_bytes) + max(page_wire_sizes),
                pages=pages,
                application_context_json=bound_context_json,
                now_ms=self._now(),
            )
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        return PublishedSnapshot(
            snapshot_id=record.snapshot_id,
            scope_id=record.scope_id,
            snapshot_generation=record.snapshot_generation,
            applied_watermark=record.applied_watermark,
            checksum=record.checksum,
            manifest=_decode_object(record.manifest_json),
            payload_bytes=record.payload_json.encode("utf-8"),
        )

    @staticmethod
    def _validated_source_scope(source: Any, scope_id: str) -> Dict[str, Any]:
        if not isinstance(source, Mapping) or set(source) != {"kind", "ids"}:
            raise SnapshotError("snapshot_refresh_scope_invalid", status=409)
        kind, ids = source["kind"], source["ids"]
        if kind not in {"card", "deck"} or not isinstance(ids, (list, tuple)):
            raise SnapshotError("snapshot_refresh_scope_invalid", status=409)
        try:
            scope = make_scope(card_ids=ids if kind == "card" else None,
                               deck_ids=ids if kind == "deck" else None)
        except SnapshotJobError:
            raise SnapshotError("snapshot_refresh_scope_invalid", status=409) from None
        if scope.scope_id != scope_id:
            raise SnapshotError("snapshot_refresh_scope_invalid", status=409)
        return {"kind": scope.kind, "ids": list(scope.ids)}

    def refresh_scope(self, scope_id: str) -> Dict[str, Any]:
        """从持久快照取原始选择范围，绝不推测父牌组。"""

        scope_id = _validate_text(scope_id, "scope_id", 128)
        try:
            snapshot = self.store.latest_snapshot(scope_id)
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        preflight = _decode_object(snapshot.manifest_json)
        source = preflight.get("source_scope")
        if source is None:
            # 旧快照只记录实际 did/card id；仅当其 hash 恰好等于请求
            # scope，才能证明这是同一选择范围。
            payload = _decode_object(snapshot.payload_json)
            manifest = payload.get("manifest")
            if not isinstance(manifest, dict):
                raise SnapshotError("snapshot_refresh_scope_unavailable", status=409)
            for kind, key in (("deck", "scope_deck_ids"), ("card", "scope_card_ids")):
                ids = manifest.get(key)
                if not isinstance(ids, list) or not ids:
                    continue
                try:
                    return self._validated_source_scope({"kind": kind, "ids": ids}, scope_id)
                except SnapshotError:
                    pass
            raise SnapshotError("snapshot_refresh_scope_unavailable", status=409)
        try:
            return self._validated_source_scope(source, scope_id)
        except SnapshotError:
            raise SnapshotError("snapshot_refresh_scope_unavailable", status=409) from None

    def refresh_result(
        self, scope_id: str, base_snapshot_id: Optional[str], new_snapshot_id: str
    ) -> Dict[str, Any]:
        """仅当两份不可变 payload 除日期表外完全相同时允许省略卡片。"""

        scope_id = _validate_text(scope_id, "scope_id", 128)
        new_snapshot_id = _validate_text(new_snapshot_id, "snapshot_id", 256)
        try:
            new = self.store.get_snapshot(new_snapshot_id, scope_id=scope_id)
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        new_bytes = new.payload_json.encode("utf-8")
        if hashlib.sha256(new_bytes).hexdigest() != new.checksum:
            raise SnapshotError("snapshot_corrupt", status=409)
        new_payload = _decode_object(new.payload_json)
        if _canonical(new_payload) != new_bytes:
            raise SnapshotError("snapshot_corrupt", status=409)
        result: Dict[str, Any] = {
            "mode": "full",
            "snapshot_id": new.snapshot_id,
            "snapshot_generation": new.snapshot_generation,
        }
        if base_snapshot_id is None:
            return result
        base_snapshot_id = _validate_text(base_snapshot_id, "snapshot_id", 256)
        try:
            base = self.store.get_snapshot(base_snapshot_id, scope_id=scope_id)
        except StorageValidationError:
            return result
        base_bytes = base.payload_json.encode("utf-8")
        if hashlib.sha256(base_bytes).hexdigest() != base.checksum:
            return result
        before = _decode_object(base.payload_json)
        if _canonical(before) != base_bytes:
            return result
        after = new_payload
        day_table = after.get("day_table")
        if not isinstance(day_table, dict):
            raise SnapshotError("snapshot_corrupt", status=409)
        before.pop("day_table", None)
        after.pop("day_table", None)
        if before == after:
            result["mode"] = "date_only"
            result["day_table"] = day_table
        return result

    def _check_profile(self, profile_id: Optional[str]) -> None:
        if profile_id is not None and profile_id != self.store.profile_id:
            raise SnapshotError("profile_mismatch", status=409)

    @staticmethod
    def _nonce(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 512:
            raise SnapshotError("permit_invalid", status=409)
        if any(char.isspace() for char in value):
            raise SnapshotError("permit_invalid", status=409)
        return value

    def _permit_values(
        self, permit_id: Optional[str], nonce: Optional[str]
    ) -> Tuple[str, str, Optional[str]]:
        if permit_id is None and nonce is None:
            raw_nonce = self._token_bytes(32).hex()
            return "p_" + self._token_bytes(16).hex(), raw_nonce, None
        if permit_id is None or nonce is None:
            raise SnapshotError("permit_invalid", status=409)
        selected_permit = _validate_text(permit_id, "permit_id", 256)
        return selected_permit, self._nonce(nonce), selected_permit

    @staticmethod
    def _pull_open_payload(
        *,
        profile_id: str,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        permit_id: str,
        nonce: str,
        expires_at_ms: int,
        snapshot: Any,
        snapshot_metadata: Mapping[str, Any],
        snapshot_metadata_hash: str,
        selected_watermark: int,
        preflight: Mapping[str, Any],
        progress: PullProgressRecord,
    ) -> Dict[str, Any]:
        return {
            "protocol": "x4-2",
            "profile_id": profile_id,
            "device_id": device_id,
            "session_id": session_id,
            "scope_id": scope_id,
            "local_generation": local_generation,
            "permit_id": permit_id,
            "nonce": nonce,
            "expires_at_ms": expires_at_ms,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.checksum,
            "snapshot_generation": snapshot.snapshot_generation,
            "applied_watermark": {"known": selected_watermark},
            "snapshot_metadata": dict(snapshot_metadata),
            "snapshot_metadata_hash": snapshot_metadata_hash,
            "preflight": dict(preflight),
            "next_page_index": progress.next_page_index,
            "received_pages": progress.received_pages,
            "received_bytes": progress.received_bytes,
            "complete": progress.complete,
        }

    @staticmethod
    def _recovery_nonce(
        recovery_key: bytes,
        *,
        profile_id: str,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        permit_id: str,
        snapshot_id: str,
        snapshot_generation: int,
        applied_watermark: int,
    ) -> str:
        context = {
            "profile_id": profile_id,
            "device_id": device_id,
            "session_id": session_id,
            "scope_id": scope_id,
            "local_generation": local_generation,
            "permit_id": permit_id,
            "snapshot_id": snapshot_id,
            "snapshot_generation": snapshot_generation,
            "applied_watermark": applied_watermark,
        }
        message = b"inkcards.pull-open.recovery-nonce.v1\0" + _canonical(context)
        return hmac.new(recovery_key, message, hashlib.sha256).hexdigest()

    def _recover_open_pull(
        self,
        *,
        recovery_key: bytes,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        snapshot: Any,
        snapshot_metadata: Mapping[str, Any],
        snapshot_metadata_hash: str,
        selected_watermark: int,
        preflight: Mapping[str, Any],
        max_response_bytes: Optional[int],
        now_ms: int,
    ) -> Optional[Dict[str, Any]]:
        try:
            recoverable = self.store.find_recoverable_permit(
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.snapshot_generation,
                applied_watermark=selected_watermark,
                now_ms=now_ms,
            )
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        if recoverable is None:
            return None

        existing_permit, progress = recoverable
        raw_nonce = self._recovery_nonce(
            recovery_key,
            profile_id=existing_permit.profile_id,
            device_id=existing_permit.device_id,
            session_id=existing_permit.session_id,
            scope_id=existing_permit.scope_id,
            local_generation=existing_permit.local_generation,
            permit_id=existing_permit.permit_id,
            snapshot_id=existing_permit.snapshot_id,
            snapshot_generation=existing_permit.snapshot_generation,
            applied_watermark=existing_permit.applied_watermark,
        )
        candidate = self._pull_open_payload(
            profile_id=self.store.profile_id,
            device_id=device_id,
            session_id=session_id,
            scope_id=scope_id,
            local_generation=local_generation,
            permit_id=existing_permit.permit_id,
            nonce=raw_nonce,
            expires_at_ms=existing_permit.expires_at_ms,
            snapshot=snapshot,
            snapshot_metadata=snapshot_metadata,
            snapshot_metadata_hash=snapshot_metadata_hash,
            selected_watermark=selected_watermark,
            preflight=preflight,
            progress=progress,
        )
        if max_response_bytes is not None and len(_canonical(candidate)) > max_response_bytes:
            raise SnapshotError("response_too_large", status=413)

        nonce_hash = hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()
        try:
            permit, progress = self.store.recover_permit_nonce(
                expected_permit=existing_permit,
                expected_progress=progress,
                nonce_hash=nonce_hash,
                now_ms=self._now(),
            )
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        return self._pull_open_payload(
            profile_id=self.store.profile_id,
            device_id=device_id,
            session_id=session_id,
            scope_id=scope_id,
            local_generation=local_generation,
            permit_id=permit.permit_id,
            nonce=raw_nonce,
            expires_at_ms=permit.expires_at_ms,
            snapshot=snapshot,
            snapshot_metadata=snapshot_metadata,
            snapshot_metadata_hash=snapshot_metadata_hash,
            selected_watermark=selected_watermark,
            preflight=preflight,
            progress=progress,
        )

    def open_pull(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        profile_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        permit_id: Optional[str] = None,
        nonce: Optional[str] = None,
        applied_watermark: Optional[Any] = None,
        generation: Optional[int] = None,
        snapshot_generation: Optional[int] = None,
        scope_generation: Optional[int] = None,
        max_response_bytes: Optional[int] = None,
        recovery_key: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """实时查询 gate 后签发或恢复一个 pull permit。"""

        if max_response_bytes is not None and (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise SnapshotError("invalid_response_budget")
        if recovery_key is not None and (
            not isinstance(recovery_key, bytes) or len(recovery_key) != 32
        ):
            raise SnapshotError("invalid_recovery_key")
        self._check_profile(profile_id)
        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _safe_int(local_generation, "local_generation")
        gate = self.store.pull_gate(device_id, session_id)
        if not gate.allowed:
            raise SnapshotError(gate.reason or "pull_gate_blocked", status=409)
        try:
            snapshot = (
                self.store.get_snapshot(snapshot_id, scope_id=scope_id)
                if snapshot_id is not None
                else self.store.latest_snapshot(scope_id)
            )
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        requested_generation = self._generation(
            generation, snapshot_generation, scope_generation
        )
        if requested_generation is not None and requested_generation != snapshot.snapshot_generation:
            raise SnapshotError("generation_mismatch", status=409)
        selected_watermark = gate.applied_watermark or 0
        requested_watermark = (
            selected_watermark
            if applied_watermark is None
            else _watermark(applied_watermark)
        )
        if requested_watermark != selected_watermark:
            raise SnapshotError("applied_watermark_mismatch", status=409)
        if snapshot.applied_watermark < selected_watermark:
            raise SnapshotError("snapshot_watermark_behind", status=409)
        snapshot_metadata, snapshot_metadata_hash = self._snapshot_metadata(
            snapshot,
            profile_id=self.store.profile_id,
            scope_id=scope_id,
            snapshot_generation=snapshot.snapshot_generation,
            applied_watermark=selected_watermark,
        )
        now = self._now()
        preflight = _decode_object(snapshot.manifest_json)
        # source_scope 只供服务端刷新使用，不扩展设备的严格 preflight 协议。
        preflight.pop("source_scope", None)
        if permit_id is None and nonce is None and recovery_key is not None:
            recovered = self._recover_open_pull(
                recovery_key=recovery_key,
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                snapshot=snapshot,
                snapshot_metadata=snapshot_metadata,
                snapshot_metadata_hash=snapshot_metadata_hash,
                selected_watermark=selected_watermark,
                preflight=preflight,
                max_response_bytes=max_response_bytes,
                now_ms=now,
            )
            if recovered is not None:
                return recovered
        selected_permit, raw_nonce, resume_id = self._permit_values(permit_id, nonce)
        if resume_id is None and recovery_key is not None:
            raw_nonce = self._recovery_nonce(
                recovery_key,
                profile_id=self.store.profile_id,
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                permit_id=selected_permit,
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.snapshot_generation,
                applied_watermark=selected_watermark,
            )
        candidate: Optional[Dict[str, Any]] = None
        if max_response_bytes is not None:
            candidate_expires_at = now + self.permit_ttl_ms
            candidate_progress = PullProgressRecord(
                selected_permit, 0, 0, 0, False, -1, None, None
            )
            if resume_id is not None:
                try:
                    existing_permit = self.store.get_permit(selected_permit)
                    existing_progress = self.store.get_pull_progress(selected_permit)
                except StorageValidationError as error:
                    raise self._storage_error(error) from None
                candidate_expires_at = existing_permit.expires_at_ms
                candidate_progress = existing_progress
            candidate = self._pull_open_payload(
                profile_id=self.store.profile_id,
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                permit_id=selected_permit,
                nonce=raw_nonce,
                expires_at_ms=candidate_expires_at,
                snapshot=snapshot,
                snapshot_metadata=snapshot_metadata,
                snapshot_metadata_hash=snapshot_metadata_hash,
                selected_watermark=selected_watermark,
                preflight=preflight,
                progress=candidate_progress,
            )
            if len(_canonical(candidate)) > max_response_bytes:
                raise SnapshotError("response_too_large", status=413)
        try:
            record = self.store.issue_permit(
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                snapshot_id=snapshot.snapshot_id,
                snapshot_generation=snapshot.snapshot_generation,
                applied_watermark=selected_watermark,
                permit_id=selected_permit,
                nonce_hash=hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest(),
                expires_at_ms=now + self.permit_ttl_ms,
                now_ms=now,
                resume_permit_id=resume_id,
            )
            progress = self.store.get_pull_progress(record.permit_id)
        except StorageValidationError as error:
            if (
                error.code == "permit_already_issued"
                and recovery_key is not None
                and permit_id is None
                and nonce is None
            ):
                recovered = self._recover_open_pull(
                    recovery_key=recovery_key,
                    device_id=device_id,
                    session_id=session_id,
                    scope_id=scope_id,
                    local_generation=local_generation,
                    snapshot=snapshot,
                    snapshot_metadata=snapshot_metadata,
                    snapshot_metadata_hash=snapshot_metadata_hash,
                    selected_watermark=selected_watermark,
                    preflight=preflight,
                    max_response_bytes=max_response_bytes,
                    now_ms=self._now(),
                )
                if recovered is not None:
                    return recovered
            raise self._storage_error(error) from None
        result = self._pull_open_payload(
            profile_id=self.store.profile_id,
            device_id=device_id,
            session_id=session_id,
            scope_id=scope_id,
            local_generation=local_generation,
            permit_id=record.permit_id,
            nonce=raw_nonce,
            expires_at_ms=record.expires_at_ms,
            snapshot=snapshot,
            snapshot_metadata=snapshot_metadata,
            snapshot_metadata_hash=snapshot_metadata_hash,
            selected_watermark=selected_watermark,
            preflight=preflight,
            progress=progress,
        )
        if resume_id is not None and max_response_bytes is not None:
            if len(_canonical(result)) > max_response_bytes:
                raise SnapshotError("response_too_large", status=413)
        return result

    def page(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        permit_id: str,
        nonce: str,
        page_index: int,
        cursor: Optional[str],
        profile_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        generation: Optional[int] = None,
        snapshot_generation: Optional[int] = None,
        scope_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        """严格核对 context/nonce 后记录连续页；重复页返回相同 JSON。"""

        self._check_profile(profile_id)
        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _safe_int(local_generation, "local_generation")
        permit_id = _validate_text(permit_id, "permit_id", 256)
        nonce = self._nonce(nonce)
        page_index = _safe_int(page_index, "page_index")
        if cursor is not None:
            cursor = _validate_text(cursor, "cursor", 256)
        try:
            permit = self.store.get_permit(permit_id)
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        selected_snapshot = snapshot_id or permit.snapshot_id
        selected_generation = self._generation(
            generation, snapshot_generation, scope_generation
        )
        if selected_generation is not None and selected_generation != permit.snapshot_generation:
            raise SnapshotError("generation_mismatch", status=409)
        try:
            page_json, page_hash, page_size, page_cursor, is_last, _ = self.store.record_page(
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                permit_id=permit_id,
                nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                snapshot_id=selected_snapshot,
                snapshot_generation=permit.snapshot_generation,
                page_index=page_index,
                cursor=cursor,
                now_ms=self._now(),
            )
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        result = _decode_object(page_json)
        if result.get("page_hash") != page_hash or result.get("page_bytes") != page_size:
            raise SnapshotError("snapshot_corrupt", status=503, retryable=True)
        if result.get("cursor") != page_cursor or bool(result.get("complete")) != is_last:
            raise SnapshotError("snapshot_corrupt", status=503, retryable=True)
        return result

    def commit(
        self,
        *,
        device_id: str,
        session_id: str,
        scope_id: str,
        local_generation: int,
        permit_id: str,
        nonce: str,
        profile_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        generation: Optional[int] = None,
        snapshot_generation: Optional[int] = None,
        scope_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        """只有 SQLite 已持久记录全部页面后才消费一次性 permit。"""

        self._check_profile(profile_id)
        device_id = _validate_text(device_id, "device_id", 128)
        session_id = _validate_text(session_id, "session_id", 128)
        scope_id = _validate_text(scope_id, "scope_id", 128)
        local_generation = _safe_int(local_generation, "local_generation")
        permit_id = _validate_text(permit_id, "permit_id", 256)
        nonce = self._nonce(nonce)
        try:
            permit = self.store.get_permit(permit_id)
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        selected_snapshot = snapshot_id or permit.snapshot_id
        selected_generation = self._generation(
            generation, snapshot_generation, scope_generation
        )
        if selected_generation is not None and selected_generation != permit.snapshot_generation:
            raise SnapshotError("generation_mismatch", status=409)
        try:
            snapshot = self.store.get_snapshot(selected_snapshot, scope_id=scope_id)
            consumed = self.store.consume_permit(
                device_id=device_id,
                session_id=session_id,
                scope_id=scope_id,
                local_generation=local_generation,
                permit_id=permit_id,
                nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                snapshot_id=selected_snapshot,
                snapshot_generation=permit.snapshot_generation,
                snapshot_checksum=snapshot.checksum,
                now_ms=self._now(),
            )
        except StorageValidationError as error:
            raise self._storage_error(error) from None
        except CommitUnknownError:
            raise
        return {
            "protocol": "x4-2",
            "committed": True,
            "permit_id": consumed.permit_id,
            "snapshot_id": consumed.snapshot_id,
            "snapshot_hash": snapshot.checksum,
            "snapshot_generation": consumed.snapshot_generation,
            "applied_watermark": {"known": consumed.applied_watermark},
        }


__all__ = [
    "PublishedSnapshot",
    "SnapshotError",
    "SnapshotPublication",
    "SnapshotService",
]
