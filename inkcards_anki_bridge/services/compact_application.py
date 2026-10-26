"""Store-backed one-event compact apply and recovery service.

The service is intended to run on Anki's collection worker. It never accepts an
HTTP request object and it never imports Anki at module import time.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from ..anki.adopted_restore import ADOPTED_SCHEMA, parse_adopted_source
from ..anki.application_context import SnapshotApplicationContext
from ..anki.compact_260903 import (
    DECK_COUNTER_FIELDS,
    LEGACY_POLICY_ID,
    AnkiCompactAdapter,
    CompactCapabilityReport,
    CompactNativeError,
    CompactNativeResult,
    PreparedCompactEvent,
    RecoveryDecision,
    _snapshot_application_context,
    _snapshot_payload,
    create_apply_authorization,
)
from ..anki.compact_projection import (
    CompactApplyAuthorization,
    CompactProjectionError,
    canonical_json,
    json_sha256,
    object_from_json,
    resolve_counter_baseline,
)
from ..storage.errors import StorageValidationError


class CompactApplicationError(RuntimeError):
    """Compact 服务拒绝前进时给 Collection worker 的稳定错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CompactOperationResult:
    status: str
    reason: Optional[str]
    event_id: Optional[str]
    sequence: Optional[int]
    intent_id: Optional[str]
    attempt_id: Optional[str]
    changes: Any = field(repr=False, compare=False)
    generation_before: int = 0
    generation_after: int = 0
    refresh_context: bool = False
    more_pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "intent_id": self.intent_id,
            "attempt_id": self.attempt_id,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "refresh_context": self.refresh_context,
            "more_pending": self.more_pending,
        }


def _record(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    names = (
        "profile_id", "device_id", "session_id", "sequence", "event_id", "snapshot_id",
        "card_id", "card_index", "rating", "answered_at_ms", "duration_ms", "payload_hash",
        "status", "recovery_required", "collection_identity", "collection_generation",
        "collection_generation_after", "policy_json", "before_json", "before_hash", "plan_json",
        "expected_after_json", "after_json", "after_hash", "intent_id", "attempt_id",
        "receipt_json", "receipt_hash", "reason",
    )
    return {name: getattr(record, name) for name in names if hasattr(record, name)}


def _event_payload(event: Any) -> dict[str, Any]:
    value = _record(event)
    required = (
        "profile_id", "device_id", "session_id", "sequence", "event_id", "snapshot_id",
        "card_id", "card_index", "rating", "answered_at_ms", "duration_ms", "payload_hash",
    )
    if any(key not in value for key in required):
        raise CompactApplicationError("compact_event_invalid", "compact event record is incomplete")
    return {key: value[key] for key in required}


def _parse_object(value: Any, code: str) -> dict[str, Any]:
    try:
        return object_from_json(value, code)
    except CompactProjectionError as exc:
        raise CompactApplicationError(exc.code, exc.message) from exc


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CompactApplicationService:
    """A single-event, generation-bound compact application coordinator."""

    def __init__(self, store: Any, adapter: Optional[AnkiCompactAdapter] = None, *, diagnostics: Any = None) -> None:
        self.store = store
        self.adapter = adapter or AnkiCompactAdapter()
        self.diagnostics = diagnostics

    def capability_report(self, collection: Any) -> CompactCapabilityReport:
        return self.adapter.capability_report(collection)

    def authorize_next(
        self,
        collection: Any,
        device_id: str,
        session_id: str,
        *,
        collection_identity: str,
        expected_generation: int,
        legacy_source: Optional[Mapping[str, Any]] = None,
    ) -> CompactApplyAuthorization:
        """Read-only preflight for the next not-applied event; creates no Store intent."""

        generation = self._check_generation(expected_generation)
        event, application = self._next_event(device_id, session_id)
        if event is None:
            raise CompactApplicationError("compact_no_pending_event", "no compact event is pending")
        if application["status"] != "not_applied":
            raise CompactApplicationError(
                "compact_recovery_required",
                "pending compact event already has an intent and must be recovered",
            )
        event_map = _event_payload(event)
        snapshot = self._snapshot(event_map["snapshot_id"])
        restored_source = self._legacy_source(event_map, snapshot)
        if legacy_source is not None and legacy_source != restored_source:
            raise CompactApplicationError("legacy_source_required", "恢复授权只能来自电脑端核验并导入的来源")
        legacy_source = restored_source
        predecessor = self._predecessor_after(event_map, device_id, session_id)
        counters, proof = self._counter_baseline_resolution(snapshot, collection_identity, generation, allow_gaps=True)
        try:
            return create_apply_authorization(
                collection,
                event_map,
                snapshot,
                collection_identity=collection_identity,
                collection_generation=generation,
                mode=("adopted_restore" if snapshot.adopted_source is not None else "legacy_restore") if legacy_source is not None else "device_policy",
                legacy_source=legacy_source,
                predecessor_after=predecessor,
                expected_counters=counters,
                baseline_proof_json=proof,
            )
        except CompactNativeError as exc:
            raise CompactApplicationError(exc.code, exc.message) from exc

    def apply_pending(
        self,
        collection: Any,
        device_id: str,
        session_id: str,
        *,
        collection_identity: str,
        expected_generation: int,
        authorization: CompactApplyAuthorization,
    ) -> CompactOperationResult:
        generation = self._check_generation(expected_generation)
        event, application = self._next_event(device_id, session_id)
        if event is None:
            return self._idle(generation)
        if application["status"] != "not_applied":
            raise CompactApplicationError("compact_recovery_required", "event is not a new not-applied row")
        event_map = _event_payload(event)
        if not isinstance(authorization, CompactApplyAuthorization):
            return self._result(
                "blocked", "compact_authorization_required", event_map, application, generation,
                generation, None, refresh=False, device_id=device_id, session_id=session_id,
            )
        intent_id, attempt_id = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            snapshot = self._snapshot(event_map["snapshot_id"])
            if authorization.mode in ("legacy_restore", "adopted_restore"):
                source = self._legacy_source(event_map, snapshot)
                if source is None or source["source_sha256"] != authorization.source_sha256:
                    raise CompactApplicationError("legacy_source_changed", "授权后恢复来源已改变")
            predecessor = self._predecessor_after(event_map, device_id, session_id)
            counters, proof = self._counter_baseline_resolution(snapshot, collection_identity, generation, allow_gaps=True)
            if proof != authorization.baseline_proof_json:
                raise CompactApplicationError("authorization_stale", "兼容性依据在授权后已变化")
            prepared = self.adapter.prepare_event(
                collection,
                event_map,
                snapshot,
                authorization,
                collection_identity=collection_identity,
                collection_generation=generation,
                intent_id=intent_id,
                attempt_id=attempt_id,
                predecessor_after=predecessor,
                expected_counters=counters,
            )
        except (CompactNativeError, CompactProjectionError, CompactApplicationError) as exc:
            if self.diagnostics is not None:
                self.diagnostics.record_exception(exc, "prepare")
            return self._result(
                "blocked", exc.code, event_map, application, generation, generation,
                None, refresh=False, device_id=device_id, session_id=session_id,
            )

        expected_after = self.adapter.expected_after_for_store(prepared)
        intent = self.store.begin_compact_apply(
            device_id,
            session_id,
            int(event_map["sequence"]),
            collection_identity=collection_identity,
            collection_generation=generation,
            policy_json=prepared.policy_json,
            before_json=self.adapter.state_for_store(prepared.before),
            plan_json=prepared.plan,
            expected_after_json=expected_after,
            intent_id=intent_id,
            attempt_id=attempt_id,
        )
        try:
            self._record_started(device_id, session_id, prepared)
        except Exception:
            # The intent remains recoverable, and no native mutation has
            # started. Keep the collection refresh flag false.
            return self._result(
                "unknown",
                "score_started_evidence_persistence_failed",
                event_map,
                _record(intent),
                generation,
                generation,
                None,
                refresh=False,
                device_id=device_id,
                session_id=session_id,
            )
        return self._apply_same_intent(collection, device_id, session_id, prepared, intent, generation)

    def recover_pending(
        self,
        collection: Any,
        device_id: str,
        session_id: str,
        *,
        collection_identity: str,
        expected_generation: int,
        legacy_source: Optional[Mapping[str, Any]] = None,
    ) -> CompactOperationResult:
        generation = self._check_generation(expected_generation)
        event, application = self._next_event(device_id, session_id)
        if event is None:
            return self._idle(generation)
        if application["status"] not in ("applying", "unknown"):
            raise CompactApplicationError("compact_not_recoverable", "next compact event has no recoverable intent")
        event_map = _event_payload(event)
        snapshot = self._snapshot(event_map["snapshot_id"])
        app = _record(application)
        if app["status"] == "applying" and not bool(app.get("recovery_required")):
            # 当前 Store 契约由重开流程标记 interrupted intent；不能伪造
            # recovery_required 或把仍存活的普通 applying 当成崩溃证据。
            return self._result(
                "blocked", "compact_recovery_restart_required", event_map, app,
                generation, generation, None, refresh=False, device_id=device_id, session_id=session_id,
            )
        if app.get("collection_identity") != collection_identity:
            return self._classify_blocked(
                device_id, session_id, app, "conflict", "collection_identity_changed", generation
            )
        try:
            prepared = self.adapter.restore_prepared_event(collection, event_map, application)
            decision = self.adapter.inspect_recovery(
                collection,
                event_map,
                snapshot,
                application,
                self.store.query_compact_score_evidence(device_id, session_id, int(event_map["sequence"])),
                collection_identity=collection_identity,
                collection_generation=generation,
            )
        except (CompactNativeError, CompactProjectionError) as exc:
            code = getattr(exc, "code", "compact_recovery_invalid")
            return self._classify_blocked(device_id, session_id, app, "conflict", code, generation)

        if decision.classification == "not_started":
            recovered = self._classify(
                device_id,
                session_id,
                app,
                "not_started",
                decision,
                before_json=self.adapter.state_for_store(decision.observed_before or {}),
                generation_after=None,
            )
            if str(_record(recovered).get("status")) != "applying":
                return self._result(
                    "unknown", "not_started_recovery_not_replayable", event_map, _record(recovered),
                    generation, generation, None, refresh=False, device_id=device_id, session_id=session_id,
                )
            return self._apply_same_intent(collection, device_id, session_id, prepared, recovered, generation)

        if decision.classification == "score_complete" and decision.score_proven:
            # Store 重开后禁止在分类前添加评分阶段行。把每次实际读回纳入
            # 同一恢复分类 proof；Store 的补齐阶段行引用该 proof 的摘要。
            readbacks: dict[str, Any] = {}

            def record_recovery_stage(stage: str, native_state: Mapping[str, Any]) -> None:
                readbacks[stage] = dict(native_state)

            repaired = self.adapter.repair_recovery(
                collection, prepared, decision, record_stage=record_recovery_stage
            )
            if repaired.status != "applied":
                return self._result(
                    repaired.status,
                    repaired.reason,
                    event_map,
                    app,
                    generation,
                    generation,
                    repaired.changes,
                    refresh=bool(repaired.mutated),
                    device_id=device_id,
                    session_id=session_id,
                )
            try:
                done = self._classify(
                    device_id, session_id, app, "score_complete", decision,
                    # 此字段是 Store 契约中的已验证历史前态；当前恢复观测另存
                    # observed_recovery_state，不能把评分后状态冒充历史 before。
                    before_json=self.adapter.state_for_store(prepared.before),
                    generation_after=generation + 1, prepared=prepared,
                    final_after=self.adapter.state_for_store(repaired.final_after or {}),
                    stage_readbacks=readbacks,
                    final_native_readback=repaired.final_after,
                )
                if str(_record(done).get("status")) != "applied":
                    return self._result(
                        "conflict", "recovery_receipt_not_applied", event_map, _record(done),
                        generation, generation, repaired.changes, refresh=bool(repaired.mutated),
                        device_id=device_id, session_id=session_id,
                    )
                self._advance_generation(generation, generation + 1)
            except Exception:
                return self._result(
                    "unknown", "store_recovery_receipt_failed", event_map, app, generation, generation,
                    repaired.changes, refresh=bool(repaired.mutated), device_id=device_id, session_id=session_id,
                )
            return self._result(
                "applied",
                "recovered_without_rescoring",
                event_map,
                _record(done),
                generation,
                generation + 1,
                repaired.changes,
                refresh=True,
                device_id=device_id,
                session_id=session_id,
            )

        classification = "conflict" if decision.classification == "conflict" else "ambiguous"
        return self._classify_blocked(
            device_id,
            session_id,
            app,
            classification,
            decision.reason,
            generation,
            decision=decision,
        )

    def _apply_same_intent(
        self,
        collection: Any,
        device_id: str,
        session_id: str,
        prepared: PreparedCompactEvent,
        intent: Any,
        generation: int,
    ) -> CompactOperationResult:
        def record_stage(stage: str, native_state: Mapping[str, Any]) -> None:
            self._record_stage(device_id, session_id, prepared, stage, native_state)

        try:
            native = self.adapter.apply_prepared(
                collection,
                prepared,
                intent=intent,
                record_stage=record_stage,
            )
        except CompactNativeError as exc:
            return self._result(
                exc.status,
                exc.code,
                prepared.event,
                _record(intent),
                generation,
                generation,
                exc.changes,
                refresh=exc.mutated,
                device_id=device_id,
                session_id=session_id,
            )
        if native.status != "applied":
            return self._result(
                native.status,
                native.reason,
                prepared.event,
                _record(intent),
                generation,
                generation,
                native.changes,
                refresh=bool(native.mutated),
                device_id=device_id,
                session_id=session_id,
            )
        if native.final_after is None:
            return self._result(
                "unknown",
                "native_final_readback_missing",
                prepared.event,
                _record(intent),
                generation,
                generation,
                native.changes,
                refresh=bool(native.mutated),
                device_id=device_id,
                session_id=session_id,
            )
        try:
            final = self.adapter.state_for_store(native.final_after)
            done = self.store.finish_compact_apply(
                prepared.intent_id,
                after_json=final,
                collection_generation_after=generation + 1,
            )
            self._advance_generation(generation, generation + 1)
        except Exception as exc:
            return self._result(
                "unknown",
                "store_receipt_failed_after_native_score",
                prepared.event,
                _record(intent),
                generation,
                generation,
                native.changes,
                refresh=True,
                device_id=device_id,
                session_id=session_id,
            )
        return self._result(
            "applied",
            None,
            prepared.event,
            _record(done),
            generation,
            generation + 1,
            native.changes,
            refresh=True,
            device_id=device_id,
            session_id=session_id,
        )

    def _record_stage(
        self,
        device_id: str,
        session_id: str,
        prepared: PreparedCompactEvent,
        stage: str,
        native_state: Mapping[str, Any],
    ) -> None:
        self.store.record_compact_score_evidence(
            device_id,
            session_id,
            int(prepared.event["sequence"]),
            stage=stage,
            evidence_json={
                "schema": "inkcards.compact.score-evidence.v1",
                "source": {
                    "score_applied": "anki.scheduler.answer_card.readback",
                    "due_corrected": "anki.collection.due_readback",
                    "score_verified": "anki.collection.final_readback",
                }[stage],
                "binding": {
                    # Store's full canonical plan digest is distinct from the
                    # marker's inner scheduler-plan digest.
                    "plan_hash": json_sha256(prepared.plan),
                    "marker_binding": dict(prepared.plan["marker_binding"]),
                },
                "observed": self.adapter.state_for_store(native_state),
                "native_observed": dict(native_state),
            },
        )

    def _record_started(self, device_id: str, session_id: str, prepared: PreparedCompactEvent) -> None:
        self.store.record_compact_score_evidence(
            device_id,
            session_id,
            int(prepared.event["sequence"]),
            stage="score_started",
            evidence_json={
                "schema": "inkcards.compact.score-started.v1",
                "source": "AnkiCompactAdapter.apply_prepared",
                "binding": {
                    "plan_hash": json_sha256(prepared.plan),
                    "marker_binding": dict(prepared.plan["marker_binding"]),
                },
                "proof_id": str(prepared.plan["plan_sha256"]),
            },
        )

    def _classify(
        self,
        device_id: str,
        session_id: str,
        app: Mapping[str, Any],
        classification: str,
        decision: RecoveryDecision,
        *,
        before_json: Any,
        generation_after: Optional[int],
        prepared: Optional[PreparedCompactEvent] = None,
        final_after: Optional[Mapping[str, Any]] = None,
        stage_readbacks: Optional[Mapping[str, Any]] = None,
        final_native_readback: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        plan = _parse_object(app.get("plan_json"), "plan_invalid")
        if classification == "score_complete":
            if prepared is None:
                raise CompactApplicationError("recovery_plan_missing", "score recovery requires its verified plan")
            if final_after is None:
                raise CompactApplicationError("recovery_readback_missing", "score recovery requires a final native readback")
            score_state = self.adapter.state_for_store(decision.score_after or {})
            expected_log_id = int(prepared.event["answered_at_ms"])
            matching_logs = [row for row in score_state["review_logs"] if int(row["id"]) == expected_log_id]
            if len(matching_logs) != 1:
                raise CompactApplicationError("recovery_score_readback_missing", "恢复缺少真实评分阶段读回")
            score_after = {
                "card": score_state["card"],
                "review_log_count": len(score_state["review_logs"]),
                "score_review_log": matching_logs[0],
                "counters": score_state["counters"],
            }
            final_after = dict(final_after)
        else:
            score_after = None
            final_after = None
        proof = {
            "profile_id": str(app.get("profile_id", "")),
            "device_id": device_id,
            "session_id": session_id,
            "sequence": int(app["sequence"]),
            "event_id": str(app["event_id"]),
            "attempt_id": str(app.get("attempt_id", "")),
            "collection_identity": str(app.get("collection_identity", "")),
            "collection_generation": int(app.get("collection_generation", -1)),
            "plan_hash": _sha_text(str(app.get("plan_json", ""))),
            "source": "anki.card+revlog+custom_data+deck_counters",
            "marker_id": str(plan.get("marker_sha256", "")),
            "proof_id": str(plan.get("plan_sha256", "")),
            "decision": decision.reason,
            "verification": dict(decision.verification),
            "observed_recovery_state": dict(decision.observed_before or {}),
            "score_readback": dict(decision.score_after or {}),
            "stage_readbacks": dict(stage_readbacks or {}),
            "final_native_readback": dict(final_native_readback or {}),
        }
        return self.store.classify_compact_recovery(
            str(app["intent_id"]),
            classification=classification,
            verification_json=proof,
            observed_before_json=before_json,
            score_after_json=score_after,
            final_after_json=final_after,
            collection_generation_after=generation_after,
        )

    def _classify_blocked(
        self,
        device_id: str,
        session_id: str,
        app: Mapping[str, Any],
        classification: str,
        reason: str,
        generation: int,
        *,
        decision: Optional[RecoveryDecision] = None,
    ) -> CompactOperationResult:
        verification = decision or RecoveryDecision(classification, reason, {})
        try:
            done = self._classify(
                device_id,
                session_id,
                app,
                classification,
                verification,
                before_json=app.get("before_json"),
                generation_after=None,
            )
            app = _record(done)
        except Exception:
            pass
        return self._result(
            "conflict" if classification == "conflict" else "unknown",
            reason,
            {"event_id": app.get("event_id"), "sequence": app.get("sequence")},
            app,
            generation,
            generation,
            None,
            refresh=False,
            device_id=device_id,
            session_id=session_id,
        )

    def _predecessor_after(self, event: Mapping[str, Any], device_id: str, session_id: str) -> Optional[dict[str, Any]]:
        # 通过既有 connect() 只读枚举同 profile/同卡的会话，再使用 Store
        # 类型化查询读取收据；不修改 schema 或任何 Store 数据。
        db = self.store.connect()
        try:
            db.execute("PRAGMA query_only=ON")
            scopes = db.execute(
                "SELECT DISTINCT device_id,session_id FROM compact_events WHERE profile_id=? AND card_id=?",
                (self.store.profile_id, str(event["card_id"])),
            ).fetchall()
        finally:
            db.close()
        candidates = []
        for scope in scopes:
            scope_device, scope_session = str(scope[0]), str(scope[1])
            events = self.store.query_compact_events(scope_device, scope_session)
            apps = {int(row.sequence): row for row in self.store.query_compact_applications(scope_device, scope_session)}
            for previous in events:
                if str(previous.card_id) != str(event["card_id"]):
                    continue
                if scope_device == device_id and scope_session == session_id and int(previous.sequence) >= int(event["sequence"]):
                    continue
                previous_app = apps.get(int(previous.sequence))
                if previous_app is None or str(previous_app.status) != "applied":
                    raise CompactApplicationError("compact_same_card_order", "此前同卡事件尚未应用")
                candidates.append((previous, previous_app))
        if not candidates:
            return None
        latest_generation = max(int(row[1].collection_generation_after) for row in candidates)
        latest = [row for row in candidates if int(row[1].collection_generation_after) == latest_generation]
        if len(latest) != 1:
            raise CompactApplicationError("compact_predecessor_ambiguous", "同卡已应用收据没有唯一的最新代数")
        previous, app = latest[0]
        after = self._validated_receipt_after(previous, app)
        context = getattr(self._snapshot(str(event['snapshot_id'])), 'application_context', None)
        if context is not None and context.source.collection_generation > latest_generation:
            # 新冻结基线接管外部变更后的卡态，旧收据仍证明 marker 所有权。
            after = {**after, 'receipt_generation_after':latest_generation,
                     'superseding_snapshot_generation':context.source.collection_generation}
        return after

    def _validated_receipt_after(self, previous: Any, app: Any) -> dict[str, Any]:
        """复用同卡路径的完整 hash、阶段与原生最终读回验证。"""
        if any(getattr(app, key, None) != getattr(previous, key, None) for key in (
            "profile_id", "device_id", "session_id", "sequence", "event_id", "payload_hash",
        )):
            raise CompactApplicationError("compact_predecessor_receipt_invalid", "应用与事件身份不符")
        before_raw = getattr(app, "before_json", None)
        if not before_raw or _sha_text(str(before_raw)) != str(getattr(app, "before_hash", "")):
            raise CompactApplicationError("compact_predecessor_receipt_invalid", "前序 before 摘要不符")
        plan = _parse_object(getattr(app, "plan_json", None), "compact_predecessor_receipt_invalid")
        if self.adapter.state_for_store(plan.get("before", {})) != _parse_object(before_raw, "compact_predecessor_receipt_invalid"):
            raise CompactApplicationError("compact_predecessor_receipt_invalid", "计划原始 before 与前序收据不符")
        after_raw = getattr(app, "after_json", None)
        receipt_raw = getattr(app, "receipt_json", None)
        if not after_raw or not receipt_raw:
            raise CompactApplicationError("compact_predecessor_receipt_missing", "preceding applied receipt is incomplete")
        if _sha_text(str(after_raw)) != str(getattr(app, "after_hash", "")):
            raise CompactApplicationError("compact_predecessor_receipt_invalid", "preceding after hash is invalid")
        if _sha_text(str(receipt_raw)) != str(getattr(app, "receipt_hash", "")):
            raise CompactApplicationError("compact_predecessor_receipt_invalid", "preceding receipt hash is invalid")
        receipt = _parse_object(receipt_raw, "compact_predecessor_receipt_invalid")
        if (
            receipt.get("event_id") != previous.event_id
            or receipt.get("payload_hash") != previous.payload_hash
            or receipt.get("after_hash") != getattr(app, "after_hash", None)
            or receipt.get("collection_identity") != getattr(app, "collection_identity", None)
            or receipt.get("collection_generation") != getattr(app, "collection_generation", None)
            or receipt.get("collection_generation_after") != getattr(app, "collection_generation_after", None)
            or receipt.get("before_hash") != getattr(app, "before_hash", None)
            or receipt.get("policy_hash") != _sha_text(str(app.policy_json))
        ):
            raise CompactApplicationError("compact_predecessor_receipt_invalid", "preceding receipt binding differs")
        stages = self.store.query_compact_score_evidence(previous.device_id, previous.session_id, int(previous.sequence))
        evidence_hashes = {row.stage: row.evidence_hash for row in stages}
        if receipt.get("evidence_hashes") != evidence_hashes or any(
            _sha_text(row.evidence_json) != row.evidence_hash for row in stages
        ):
            raise CompactApplicationError("compact_predecessor_evidence_invalid", "前序阶段证据摘要不符")
        stage_values = {row.stage: _parse_object(row.evidence_json, "compact_predecessor_evidence_invalid") for row in stages}
        required = set(plan.get("required_evidence", ()))
        if not {"score_applied", "due_corrected", "score_verified"}.issubset(required) or not required.issubset(stage_values):
            raise CompactApplicationError("compact_predecessor_evidence_invalid", "前序收据缺少必要评分阶段")
        for stage in required:
            value = stage_values[stage]
            binding = value.get("binding")
            if isinstance(binding, Mapping):
                if binding.get("plan_hash") != _sha_text(app.plan_json) or binding.get("marker_binding") != plan.get("marker_binding"):
                    raise CompactApplicationError("compact_predecessor_evidence_invalid", "评分阶段计划绑定不符")
            elif (
                not isinstance(value.get("recovery_classification_hash"), str)
                or value.get("recovery_classification_hash") != evidence_hashes.get("recovery_classification")
            ):
                raise CompactApplicationError("compact_predecessor_evidence_invalid", "恢复派生阶段没有分类依据")
        recovery = stage_values.get("recovery_classification", {}).get("verification")
        if isinstance(recovery, Mapping) and (
            recovery.get("plan_hash") != _sha_text(app.plan_json)
            or recovery.get("collection_generation") != app.collection_generation
            or recovery.get("collection_identity") != app.collection_identity
        ):
            raise CompactApplicationError("compact_predecessor_evidence_invalid", "恢复分类计划或代数绑定不符")
        raw_final = stage_values.get("score_verified", {}).get("native_observed")
        if not isinstance(raw_final, Mapping):
            raw_final = stage_values.get("recovery_classification", {}).get("verification", {}).get("final_native_readback")
        after = _parse_object(after_raw, "compact_predecessor_after_invalid")
        if not isinstance(raw_final, Mapping) or self.adapter.state_for_store(raw_final) != after:
            raise CompactApplicationError("compact_predecessor_native_receipt_missing", "前序收据缺少可验证的最终原生读回")
        native_card_hex = raw_final.get("native_card_hex")
        if not isinstance(native_card_hex, str) or not native_card_hex:
            raise CompactApplicationError("compact_predecessor_native_receipt_missing", "前序读回未绑定原始卡片位型")
        return {**after, "native_card_hex": native_card_hex,
                "native_revlogs_sha256": json_sha256(raw_final["native_revlogs"]) if isinstance(raw_final.get("native_revlogs"), list) else None}

    def _snapshot_counter_baseline(
        self, snapshot: Any, collection_identity: str, generation: int
    ) -> Optional[dict[str, Any]]:
        # 既有严格接口继续拒绝未证明的版本间隙。
        return self._counter_baseline_resolution(
            snapshot, collection_identity, generation
        )[0]

    def _counter_baseline_resolution(
        self,
        snapshot: Any,
        collection_identity: str,
        generation: int,
        *,
        allow_gaps: bool = False,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if (
            getattr(snapshot, "adopted_source", None) is not None
            or snapshot.application_context is None
        ):
            return None, None
        context = snapshot.application_context
        db = self.store.connect()
        try:
            db.execute("PRAGMA query_only=ON")
            scopes = db.execute(
                "SELECT DISTINCT device_id,session_id FROM compact_applications WHERE profile_id=? "
                "AND status='applied' AND collection_generation>=? AND collection_generation<?",
                (
                    self.store.profile_id,
                    context.source.collection_generation,
                    generation,
                ),
            ).fetchall()
        finally:
            db.close()
        edges = []
        for device, session in scopes:
            events = {
                row.sequence: row
                for row in self.store.query_compact_events(device, session)
            }
            for app in self.store.query_compact_applications(device, session):
                if (
                    app.status != "applied"
                    or not context.source.collection_generation
                    <= app.collection_generation
                    < generation
                ):
                    continue
                event = events.get(app.sequence)
                if event is None:
                    raise CompactApplicationError(
                        "snapshot_counter_receipt_path_invalid", "评分收据没有对应事件"
                    )
                after = self._validated_receipt_after(event, app)
                before = _parse_object(
                    app.before_json, "snapshot_counter_receipt_path_invalid"
                )
                edges.append(
                    {
                        "generation": app.collection_generation,
                        "after_generation": app.collection_generation_after,
                        "identity": app.collection_identity,
                        "before": before.get("counters"),
                        "after": after.get("counters"),
                        "receipt_hash": app.receipt_hash,
                    }
                )
        try:
            return resolve_counter_baseline(
                context, collection_identity, generation, edges, allow_gaps=allow_gaps
            )
        except CompactProjectionError as error:
            raise CompactApplicationError(error.code, error.message) from error

    def _snapshot(self, snapshot_id: str) -> Any:
        try:
            snapshot = self.store.get_snapshot(str(snapshot_id))
        except Exception as exc:
            raise CompactApplicationError("compact_snapshot_unavailable", "bound compact snapshot is unavailable") from exc
        if snapshot.application_context is not None:
            return snapshot
        source = self._read_legacy_source(snapshot_id)
        if source is None:
            return snapshot
        return self._validated_legacy_snapshot(snapshot, source)

    def _legacy_path(self, snapshot_id: str) -> Path:
        # 固定本地路径，无 HTTP 入参或来源路径穿越。
        return Path(self.store.root) / "legacy_restore" / (_sha_text(snapshot_id) + ".json")

    def _read_legacy_source(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self._legacy_path(snapshot_id)
        try:
            if not path.exists():
                return None
            if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("invalid local source file")
            source = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(source, Mapping):
                raise TypeError("source must be an object")
            return source
        except (OSError, ValueError, TypeError) as exc:
            raise CompactApplicationError("legacy_source_invalid", "本地恢复来源不可读取或格式无效") from exc

    def _validated_legacy_snapshot(self, snapshot: Any, source: Mapping[str, Any]) -> Any:
        try:
            if not isinstance(source, Mapping):
                raise TypeError("source must be an object")
            if source.get("schema") == ADOPTED_SCHEMA:
                context = parse_adopted_source(snapshot, source)
                restored = replace(snapshot, application_context=context,
                    restoration_source_sha256=json_sha256(source), adopted_source=dict(source))
                _snapshot_application_context(restored, _snapshot_payload(restored))
                return restored
            if source.get("schema") != "inkcards-legacy-source-v1":
                raise ValueError("source schema")
            if source.get("policy_id") != LEGACY_POLICY_ID or not isinstance(source.get("policy_approval"), str) or not source["policy_approval"]:
                raise ValueError("explicit ordinary policy approval required")
            if not isinstance(source.get("source_id"), str) or not source["source_id"]:
                raise ValueError("source identity")
            context = SnapshotApplicationContext.from_json(canonical_json(source["application_context"]))
            restored = replace(snapshot, application_context=context, restoration_source_sha256=json_sha256(source))
            _snapshot_application_context(restored, _snapshot_payload(restored))
            if context.source.unsupported or not isinstance(source.get("events"), list) or not source["events"]:
                raise ValueError("incomplete source")
            return restored
        except (KeyError, TypeError, ValueError, CompactNativeError, CompactProjectionError) as exc:
            raise CompactApplicationError("legacy_source_invalid", "恢复来源缺少完整历史上下文或原快照绑定") from exc

    def _legacy_source(self, event: Mapping[str, Any], snapshot: Any) -> dict[str, Any] | None:
        # 原始冻结上下文优先；恢复文件不能覆盖新快照。
        original = self.store.get_snapshot(snapshot.snapshot_id)
        if original.application_context is not None:
            return None
        source = self._read_legacy_source(snapshot.snapshot_id)
        if source is None:
            return None
        self._validated_legacy_snapshot(original, source)
        if snapshot.restoration_source_sha256 != json_sha256(source):
            raise CompactApplicationError("legacy_source_changed", "读取期间恢复来源已改变")
        binding = {key: event[key] for key in (
            "profile_id", "device_id", "session_id", "sequence", "event_id",
            "payload_hash", "snapshot_id", "card_id",
        )}
        matches = [item for item in source["events"] if item == binding]
        if len(matches) != 1:
            raise CompactApplicationError("legacy_source_binding_mismatch", "恢复来源未唯一绑定此事件原始输入")
        return {"source_id": source["source_id"], "source_sha256": json_sha256(source), **binding}

    def _validate_restoration_bindings(self, source: Mapping[str, Any], snapshot_id: str) -> None:
        # 每条来源绑定须对应 Store 中已收到的真实事件，不能据卡 ID 授权。
        if len({canonical_json(binding) for binding in source["events"]}) != len(source["events"]):
            raise CompactApplicationError("legacy_source_binding_mismatch", "恢复来源包含重复事件绑定")
        for binding in source["events"]:
            if not isinstance(binding, Mapping):
                raise CompactApplicationError("legacy_source_invalid", "来源事件绑定无效")
            events = self.store.query_compact_events(binding.get("device_id"), binding.get("session_id"))
            keys = ("profile_id", "device_id", "session_id", "sequence", "event_id", "payload_hash", "snapshot_id", "card_id")
            if set(binding) != set(keys) or not any(
                {key: getattr(event, key) for key in keys} == binding for event in events
            ) or binding["snapshot_id"] != snapshot_id:
                raise CompactApplicationError("legacy_source_binding_mismatch", "来源事件与 Store 原始记录不符")

    def approved_adopted_scope(self) -> tuple[str, str]:
        """本机 UI 只发现一个完整核验来源及其原会话，不接触 Collection 或凭据。"""
        directory = Path(self.store.root) / "legacy_restore"
        candidates = []
        for path in sorted(directory.glob("*.json")):
            try:
                if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("invalid source file")
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, Mapping):
                    raise ValueError("source object required")
                if value.get("schema") != ADOPTED_SCHEMA:
                    continue
                snapshot_id = value["snapshot"]["snapshot_id"]
                if path != self._legacy_path(snapshot_id):
                    raise ValueError("noncanonical imported source")
                snapshot = self.store.get_snapshot(snapshot_id)
                if snapshot.application_context is not None:
                    raise ValueError("cannot replace frozen source")
                self._validated_legacy_snapshot(snapshot, value)
                self._validate_restoration_bindings(value, snapshot_id)
                scopes = {(b["device_id"], b["session_id"]) for b in value["events"]}
                if len(scopes) != 1:
                    raise CompactApplicationError("adopted_source_ambiguous", "恢复来源未唯一确定一个原会话")
                device, session = next(iter(scopes))
                actual = self.store.query_compact_events(device, session)
                keys = ("profile_id", "device_id", "session_id", "sequence", "event_id", "payload_hash", "snapshot_id", "card_id")
                expected_bindings = {canonical_json(b) for b in value["events"]}
                actual_bindings = {canonical_json({k:getattr(e,k) for k in keys}) for e in actual}
                if actual_bindings != expected_bindings:
                    raise CompactApplicationError("legacy_source_binding_mismatch", "原会话包含未批准或缺失的事件")
                candidates.append((device, session))
            except (KeyError, TypeError, ValueError, OSError, StorageValidationError) as exc:
                raise CompactApplicationError("legacy_source_invalid", "已导入恢复来源无效") from exc
        if not candidates:
            raise CompactApplicationError("adopted_source_missing", "尚未导入已批准的采用规则来源")
        if len(candidates) != 1:
            raise CompactApplicationError("adopted_source_ambiguous", "存在多个已批准来源，不能自动选择")
        return candidates[0]

    def import_adopted_source(self, source: Mapping[str, Any], *, expected_sha256: str) -> str:
        """电脑端导入已经明确批准的采用规则；不捕获或写入 Collection。"""
        if not isinstance(source, Mapping) or source.get("schema") != ADOPTED_SCHEMA:
            raise CompactApplicationError("legacy_source_invalid", "必须提供明确采用规则来源")
        return self._import_restoration_source(source, expected_sha256=expected_sha256)

    def import_legacy_source(self, source: Mapping[str, Any], *, expected_sha256: str) -> str:
        """电脑端核验后的显式导入；摘要只校验一致性，不证明历史真实性。

        来源必须是已经人工核验的历史备份，不能由当前 Collection 捕获生成。
        本方法不读取/修改 Collection、事件、intent 或原快照。
        """
        if not isinstance(source, Mapping) or source.get("schema") != "inkcards-legacy-source-v1":
            raise CompactApplicationError("legacy_source_invalid", "严格历史入口只接受原历史上下文来源")
        return self._import_restoration_source(source, expected_sha256=expected_sha256)

    def _import_restoration_source(self, source: Mapping[str, Any], *, expected_sha256: str) -> str:
        """仅复用本地原子发布与原事件绑定；入口分别校验各自来源语义。"""
        if json_sha256(source) != expected_sha256:
            raise CompactApplicationError("legacy_source_invalid", "核验的来源摘要不符")
        try:
            snapshot_id = source["snapshot"]["snapshot_id"] if source.get("schema") == ADOPTED_SCHEMA else source["application_context"]["snapshot_id"]
            snapshot = self.store.get_snapshot(snapshot_id)
        except (KeyError, TypeError, StorageValidationError) as exc:
            raise CompactApplicationError("legacy_source_invalid", "来源未绑定原快照") from exc
        if snapshot.application_context is not None:
            raise CompactApplicationError("legacy_source_invalid", "不可覆盖已有冻结上下文")
        self._validated_legacy_snapshot(snapshot, source)
        self._validate_restoration_bindings(source, snapshot_id)
        path = self._legacy_path(snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = canonical_json(source)
        # 完整写入+fsync后原子建立；重复导入一致来源可复用，不覆盖已有来源。
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".source-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != text:
                    raise CompactApplicationError("legacy_source_changed", "已有不同恢复来源，禁止自动覆盖")
        finally:
            os.unlink(temporary)
        return expected_sha256

    def _next_event(self, device_id: str, session_id: str) -> tuple[Any, dict[str, Any]]:
        events = self.store.query_compact_events(device_id, session_id)
        applications = self.store.query_compact_applications(device_id, session_id)
        app_by_sequence = {int(item.sequence): item for item in applications}
        for event in events:
            app = app_by_sequence.get(int(event.sequence))
            if app is None:
                raise CompactApplicationError("compact_application_missing", "received compact row has no application state")
            state = _record(app)
            if state.get("status") != "applied":
                return event, state
            # applied 不能仅凭状态跳过；核验完整持久收据和原生读回。
            self._validated_receipt_after(event, app)
        return None, {}

    def _check_generation(self, expected_generation: int) -> int:
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
            raise CompactApplicationError("collection_generation_invalid", "runtime generation is invalid")
        try:
            actual = int(self.store.get_collection_generation())
        except Exception as exc:
            raise CompactApplicationError("collection_generation_unavailable", "Store generation is unavailable") from exc
        if actual != expected_generation:
            raise CompactApplicationError("generation_mismatch", "runtime and Store generations differ")
        return actual

    def _advance_generation(self, before: int, after: int) -> None:
        if after != before + 1:
            raise CompactApplicationError("generation_step_invalid", "compact score must advance generation exactly once")
        current = int(self.store.get_collection_generation())
        if current == before:
            self.store.set_collection_generation(after)
        elif current != after:
            raise CompactApplicationError("generation_mismatch", "Store generation changed during compact apply")

    def _result(
        self,
        status: str,
        reason: Optional[str],
        event: Mapping[str, Any],
        application: Mapping[str, Any],
        before: int,
        after: int,
        changes: Any,
        *,
        refresh: bool,
        device_id: str,
        session_id: str,
    ) -> CompactOperationResult:
        if changes is None:
            changes = self.adapter.empty_changes()
        try:
            pending = any(
                str(row.status) != "applied"
                for row in self.store.query_compact_applications(device_id, session_id)
            )
        except Exception:
            # 账本读取失败也要把已经取得的真实 OpChanges 返回宿主。
            pending = True
        # more_pending 是成功后可继续处理的通知；不让 unknown/blocked 被 manager 当作继续作业。
        pending = pending if status == "applied" else False
        return CompactOperationResult(
            status=status,
            reason=reason,
            event_id=str(event.get("event_id", "")) or None,
            sequence=int(event["sequence"]) if event.get("sequence") is not None else None,
            intent_id=str(application["intent_id"]) if application.get("intent_id") else None,
            attempt_id=str(application["attempt_id"]) if application.get("attempt_id") else None,
            changes=changes,
            generation_before=before,
            generation_after=after,
            refresh_context=refresh,
            more_pending=pending,
        )

    def _idle(self, generation: int) -> CompactOperationResult:
        return CompactOperationResult(
            "idle",
            None,
            None,
            None,
            None,
            None,
            self.adapter.empty_changes(),
            generation,
            generation,
            False,
            False,
        )


__all__ = ["CompactApplicationError", "CompactApplicationService", "CompactOperationResult"]
