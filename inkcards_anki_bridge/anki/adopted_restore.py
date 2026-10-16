"""明确采用批准规则的来源；绝不将采用时输入标作原快照冻结 context。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .application_context import CapturedApplicationContext
from .compact_projection import json_sha256

ADOPTED_SCHEMA = "inkcards-adopted-source-v1"
ADOPTED_POLICY_ID = "inkcards-compact-adopted-ordinary-v1"
ADOPTED_POLICY = {
    "fuzz": "fixed_ordinary",
    "load_balancing": "disabled",
    "counters": "preserve_current",
    "event_inputs": "original",
}
BINDING_KEYS = (
    "profile_id",
    "device_id",
    "session_id",
    "sequence",
    "event_id",
    "payload_hash",
    "snapshot_id",
    "card_id",
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class AdoptedApplicationContext:
    """原快照绑定与采用时捕获分开；source 不是原时点冻结输入。"""

    profile_id: str
    snapshot_id: str
    snapshot_checksum: str
    snapshot_generation: int
    source: CapturedApplicationContext

    @property
    def source_sha256(self) -> str:
        return self.source.source_sha256


def parse_adopted_source(
    snapshot: Any, value: Mapping[str, Any]
) -> AdoptedApplicationContext:
    """校验结构与原始绑定；信任来自本地审阅与明确批准，不来自摘要自证。"""
    if (
        value.get("schema") != ADOPTED_SCHEMA
        or value.get("policy_id") != ADOPTED_POLICY_ID
    ):
        raise ValueError("adopted schema or policy")
    if not isinstance(value.get("source_id"), str) or not value["source_id"]:
        raise ValueError("source identity")
    if value.get("policy") != ADOPTED_POLICY:
        raise ValueError("unsupported adopted policy")
    approval = value.get("approval")
    if not isinstance(approval, Mapping) or (
        approval.get("authority") != "direct_user"
        or approval.get("decision") != "approve"
        or not isinstance(approval.get("record_id"), str)
        or not approval["record_id"]
        or approval.get("policy") != ADOPTED_POLICY
    ):
        raise ValueError("explicit adopted approval required")
    binding = value.get("snapshot")
    expected = {
        "profile_id": snapshot.profile_id,
        "snapshot_id": snapshot.snapshot_id,
        "checksum": snapshot.checksum,
        "snapshot_generation": snapshot.snapshot_generation,
    }
    if (
        binding != expected
        or hashlib.sha256(snapshot.payload_json.encode()).hexdigest()
        != snapshot.checksum
    ):
        raise ValueError("original snapshot binding")
    source = CapturedApplicationContext.from_dict(value["adopted_context"])
    if (
        source.profile_identity != snapshot.profile_id
        or source.unsupported
        or source.collection_generation is None
    ):
        raise ValueError("adopted context identity or completeness")
    if (
        source.anki_version != "26.9.3"
        or source.anki_build != "29bb700b"
        or source.timezone != "Asia/Shanghai"
        or source.fsrs_enabled is not True
    ):
        raise ValueError("adopted source requires the verified native build and clock")
    if any(
        field is None
        for field in (
            source.scheduler_today,
            source.day_cutoff_seconds,
            source.collection_created_at_seconds,
            source.fsrs_short_term_with_steps_enabled,
        )
    ):
        raise ValueError("incomplete adopted clock")
    basis = value.get("basis")
    if not isinstance(basis, list) or not basis:
        raise ValueError("reviewed basis required")
    for item in basis:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("source_id"), str)
            or not item["source_id"]
            or not _HEX64.fullmatch(str(item.get("sha256", "")))
        ):
            raise ValueError("invalid basis binding")
    if not any(item.get("role") == "historical_backup" for item in basis):
        raise ValueError("historical backup basis required")
    events, cards = value.get("events"), value.get("cards")
    if not isinstance(events, list) or not events or not isinstance(cards, Mapping):
        raise ValueError("event and adopted prestate required")
    if any(
        not isinstance(event, Mapping) or set(event) != set(BINDING_KEYS)
        for event in events
    ):
        raise ValueError("invalid event binding")
    # 本模式每卡只采用一次原前态，不能把后续事件偷偷纳入同一批准。
    ids = [event["card_id"] for event in events]
    if len(set(ids)) != len(ids) or set(cards) != set(ids):
        raise ValueError("one approved event per adopted card required")
    for event in events:
        if (
            event["profile_id"] != snapshot.profile_id
            or event["snapshot_id"] != snapshot.snapshot_id
            or not _HEX64.fullmatch(str(event["payload_hash"]))
        ):
            raise ValueError("event original snapshot binding")
        card = cards[event["card_id"]]
        if not isinstance(card, Mapping) or set(card) != {
            "card",
            "native_card_hex",
            "native_revlogs",
        }:
            raise ValueError("complete adopted raw prestate required")
        before = card["card"]
        if (
            not isinstance(before, Mapping)
            or before.get("card_id") != event["card_id"]
            or before.get("type") != 2
            or before.get("queue") != 2
        ):
            raise ValueError("only ordinary review adoption is supported")
        native = card["native_card_hex"]
        if (
            not isinstance(native, str)
            or not native
            or len(native) % 2
            or re.fullmatch(r"[0-9a-f]+", native) is None
        ):
            raise ValueError("invalid native card binding")
        logs = card["native_revlogs"]
        if not isinstance(logs, list) or any(
            not isinstance(row, list)
            or len(row) != 9
            or any(type(n) is not int for n in row)
            or str(row[1]) != event["card_id"]
            for row in logs
        ):
            raise ValueError("invalid native history binding")
    return AdoptedApplicationContext(
        snapshot.profile_id,
        snapshot.snapshot_id,
        snapshot.checksum,
        snapshot.snapshot_generation,
        source,
    )


def verify_adopted_prestate(
    collection: Any, snapshot: Any, event: Mapping[str, Any]
) -> None:
    """授权/准备均逐字段检查采用时前态；评分后的恢复改用既有持久计划。"""
    from .compact_260903 import CompactNativeError, _capture_state

    source = snapshot.adopted_source
    before = source["cards"][str(event["card_id"])]
    current = _capture_state(collection, int(event["card_id"]), ())
    if any(current[key] != before[key] for key in before):
        raise CompactNativeError(
            "adopted_prestate_conflict", "卡片或完整历史与已批准采用前态不符"
        )
    if snapshot.restoration_source_sha256 != json_sha256(source):
        raise CompactNativeError("legacy_source_changed", "采用来源摘要不符")
