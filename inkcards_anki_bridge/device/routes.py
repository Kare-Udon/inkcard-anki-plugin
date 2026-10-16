"""x4-2 设备 route 表。

pairing、review 收件与状态查询始终可用。pull 只有在调用方显式注入同一 profile
的 :class:`SnapshotService` 后才注册；未注入时继续返回 capability_unavailable。
override 仍未开放。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ..domain.sync_gate import LUA_SAFE_INTEGER_MAX, ImmutableReviewEvent
from ..services.inbox import InboxService
from ..services.snapshots import SnapshotError, SnapshotService
from ..storage import (
    BusyStorageError,
    CommitUnknownError,
    ProfileStore,
    StorageError,
    StorageValidationError,
)
from .auth import AuthError, AuthPrincipal, DeviceAuthenticator
from .codec import CodecError, decode_object, encode_json, error_envelope


class RouteError(Exception):
    """route 层稳定错误，不携带请求原文。"""

    def __init__(self, code: str, *, status: int = 422, retryable: bool = False) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class RouteLimits:
    max_request_bytes: int = 64 * 1024
    max_response_bytes: int = 64 * 1024
    max_review_events: int = 100
    max_event_ids: int = 200

    def __post_init__(self) -> None:
        for value in (
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_review_events,
            self.max_event_ids,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("route limits must be positive integers")


@dataclass(frozen=True)
class RouteResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class DeviceRouter:
    """无框架 route dispatcher；所有持久收件都通过 InboxService。"""

    DEFERRED_PATHS = frozenset(
        {
            "/v2/pull/open",
            "/v2/pull/refresh",
            "/v2/pull/page",
            "/v2/pull/commit",
            "/v2/override/prepare",
            "/v2/override/confirm",
        }
    )

    def __init__(
        self,
        auth: DeviceAuthenticator,
        inbox: InboxService,
        *,
        snapshot_service: Optional[SnapshotService] = None,
        refresh_handler: Optional[Callable[..., Tuple[int, Mapping[str, Any]]]] = None,
        compact_job_requester: Optional[Callable[[str, str], Any]] = None,
        compact_job_status_reader: Optional[Callable[[str, str], Optional[Mapping[str, Any]]]] = None,
        compact_capability_reader: Optional[Callable[[], Mapping[str, Any]]] = None,
        limits: Optional[RouteLimits] = None,
    ) -> None:
        if not isinstance(auth, DeviceAuthenticator):
            raise TypeError("auth must be a DeviceAuthenticator")
        if not isinstance(inbox, InboxService):
            raise TypeError("inbox must be an InboxService")
        if inbox.store is not auth.store:
            raise ValueError("auth and inbox must use the same ProfileStore")
        if snapshot_service is not None:
            if not isinstance(snapshot_service, SnapshotService):
                raise TypeError("snapshot_service must be a SnapshotService")
            if snapshot_service.store is not auth.store:
                raise ValueError("snapshot service must use the same ProfileStore")
        self.auth = auth
        self.inbox = inbox
        self.snapshot_service = snapshot_service
        if refresh_handler is not None and (snapshot_service is None or not callable(refresh_handler)):
            raise ValueError("refresh handler requires a snapshot service")
        self.refresh_handler = refresh_handler
        if compact_job_requester is not None and not callable(compact_job_requester):
            raise TypeError("compact_job_requester must be callable")
        if compact_job_status_reader is not None and not callable(compact_job_status_reader):
            raise TypeError("compact_job_status_reader must be callable")
        self.compact_capability_reader = compact_capability_reader
        self.compact_job_requester = compact_job_requester
        self.compact_job_status_reader = compact_job_status_reader
        self.store: ProfileStore = auth.store
        self.limits = limits or RouteLimits()
        self._review_lock = threading.RLock()
        self.route_table = {
            ("POST", "/v2/pair"): self._pair,
            ("POST", "/v2/handshake"): self._handshake,
            ("POST", "/v2/reviews"): self._reviews,
            ("POST", "/v2/receipts"): self._receipts,
            ("POST", "/v2/sync/status"): self._status,
            ("POST", "/v3/reviews/compact"): self._compact_reviews,
            ("POST", "/v3/receipts/compact"): self._compact_receipts,
        }
        if snapshot_service is not None:
            self.route_table.update(
                {
                    ("POST", "/v2/pull/open"): self._pull_open,
                    ("POST", "/v2/pull/page"): self._pull_page,
                    ("POST", "/v2/pull/commit"): self._pull_commit,
                }
            )
        if refresh_handler is not None:
            self.route_table[("POST", "/v2/pull/refresh")] = self._pull_refresh

    def _response(self, status: int, payload: Mapping[str, Any]) -> RouteResponse:
        try:
            body = encode_json(payload, max_bytes=self.limits.max_response_bytes)
        except CodecError as error:
            # 正常响应超预算时只能返回最小、仍带 error envelope 的失败；不会截断 JSON。
            if error.code != "response_too_large":
                payload = error_envelope("invalid_response")
            else:
                payload = {"error": {"code": "response_too_large"}}
            try:
                body = encode_json(payload, max_bytes=self.limits.max_response_bytes)
            except CodecError:
                body = b'{"error":{"code":"response_too_large"}}'
            status = 413
        return RouteResponse(
            status,
            body,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "Connection": "close",
            },
        )

    def _error(self, code: str, status: int, retryable: bool = False) -> RouteResponse:
        return self._response(status, error_envelope(code, retryable=retryable))

    @staticmethod
    def _text(value: Any, *, code: str = "invalid_request", maximum: int = 256) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise RouteError(code)
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise RouteError(code)
        return value

    @staticmethod
    def _pull_recovery_key(headers: Mapping[str, Any]) -> bytes:
        """从已验证的 Bearer token 派生临时稳定密钥，不持久化或记录原 token。"""

        value = DeviceAuthenticator._header(headers, "Authorization")
        parts = value.split(" ") if isinstance(value, str) else []
        if (
            len(parts) != 2
            or parts[0].lower() != "bearer"
            or not parts[1]
            or any(char.isspace() for char in parts[1])
        ):
            raise AuthError("bearer_token_required")
        return hashlib.sha256(
            b"inkcards.pull-open.recovery-key.v1\0" + parts[1].encode("utf-8")
        ).digest()

    def _body(
        self,
        raw: bytes,
        *,
        required: Iterable[str],
        allowed: Iterable[str],
    ) -> Dict[str, Any]:
        return decode_object(
            raw,
            max_bytes=self.limits.max_request_bytes,
            required=required,
            allowed=allowed,
        )

    def dispatch(
        self,
        method: str,
        path: str,
        headers: Mapping[str, Any],
        body: bytes,
    ) -> RouteResponse:
        """执行一个请求；不会做 CORS 放行，也不会读取任意文件或转发 API。"""

        method_upper = method.upper() if isinstance(method, str) else ""
        if method_upper != "POST":
            return self._error("method_not_allowed", 405)
        route_key = (method_upper, path)
        if route_key not in self.route_table:
            if path in self.DEFERRED_PATHS:
                return self._error("capability_unavailable", 503, retryable=False)
            return self._error("not_found", 404)
        try:
            return self.route_table[route_key](headers, body)
        except CodecError as error:
            return self._error(error.code, error.status, error.retryable)
        except AuthError as error:
            return self._error(error.code, error.status, error.retryable)
        except RouteError as error:
            return self._error(error.code, error.status, error.retryable)
        except SnapshotError as error:
            return self._error(error.code, error.status, error.retryable)
        except CommitUnknownError as error:
            return self._error(error.code, 503, True)
        except BusyStorageError as error:
            return self._error(error.code, 503, True)
        except StorageValidationError as error:
            status = 409 if error.code in {"identity_mismatch", "sequence_conflict", "receipt_regression"} else 422
            return self._error(error.code, status)
        except StorageError as error:
            return self._error(error.code, 503, True)
        except Exception:
            return self._error("internal_error", 500)

    def _pair(self, _: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(raw, required={"code", "device_id"}, allowed={"code", "device_id", "name"})
        code = body["code"]
        device_id = self._text(body["device_id"], code="invalid_pair_request", maximum=128)
        if "name" in body:
            self._text(body["name"], code="invalid_pair_request", maximum=128)
        credential = self.auth.pair(code, device_id)
        return self._response(
            200,
            {
                "protocol": "x4-2",
                "profile_id": credential.profile_id,
                "device_id": credential.device_id,
                "token": credential.token,
            },
        )

    def _authenticated(
        self,
        headers: Mapping[str, Any],
        body: Mapping[str, Any],
        *,
        require_session: bool = True,
    ) -> Tuple[AuthPrincipal, str]:
        device_id = self._text(body.get("device_id"), code="device_id_required", maximum=128)
        raw_session = body.get("session_id")
        if raw_session is None and not require_session:
            session_id = ""
        else:
            session_id = self._text(raw_session, code="session_id_required", maximum=128)
        principal = self.auth.authenticate_headers(headers, device_id)
        if principal.profile_id != self.store.profile_id:
            raise AuthError("profile_mismatch", status=409)
        # require_session 是语义标记；storage facade 会在真正读写时核对持久 session。
        if require_session:
            return principal, session_id
        return principal, session_id

    def _handshake(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"protocol", "device_id"},
            allowed={"protocol", "device_id", "session_id", "next_sequence"},
        )
        if body["protocol"] != "x4-2":
            raise RouteError("unsupported_protocol", status=422)
        if "next_sequence" in body and (
            isinstance(body["next_sequence"], bool)
            or not isinstance(body["next_sequence"], int)
            or not 1 <= body["next_sequence"] <= LUA_SAFE_INTEGER_MAX
        ):
            raise RouteError("invalid_next_sequence")
        principal, session_id = self._authenticated(headers, body, require_session=False)
        if not session_id:
            session_id = self.auth.new_session_id()
        session = self.auth.open_session(principal, session_id)
        requested_next = body.get("next_sequence") or 1
        next_sequence = max(session.high_water + 1, requested_next)
        if next_sequence > LUA_SAFE_INTEGER_MAX:
            raise RouteError("invalid_next_sequence")
        return self._response(
            200,
            {
                "protocol": "x4-2",
                "profile_id": principal.profile_id,
                "device_id": principal.device_id,
                "session_id": session.session_id,
                "session": {
                    "id": session.session_id,
                    "high_water": session.high_water,
                    "next_sequence": next_sequence,
                    "resumed": session.high_water > 0,
                },
                "limits": {
                    "max_request_bytes": self.limits.max_request_bytes,
                    "max_response_bytes": self.limits.max_response_bytes,
                    "max_review_events": self.limits.max_review_events,
                },
                "capabilities": {
                    "reviews": True,
                    "receipts": True,
                    "sync_status": True,
                    "pull": self.snapshot_service is not None,
                    "pull_refresh": self.refresh_handler is not None,
                    "override": False,
                    "compact_application": self._compact_capabilities(),
                },
                "transport_security": "trusted_lan_http_only",
            },
        )

    def _pull_refresh(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"device_id", "session_id", "scope_id"},
            allowed={"device_id", "session_id", "scope_id", "base_snapshot_id", "refresh_token"},
        )
        principal, session_id = self._authenticated(headers, body)
        scope_id = self._text(body["scope_id"], code="invalid_scope_id", maximum=128)
        gate = self.store.pull_gate(principal.device_id, session_id)
        if not gate.allowed:
            raise RouteError(gate.reason or "pull_gate_blocked", status=409)
        base_snapshot_id = body.get("base_snapshot_id")
        if base_snapshot_id is not None:
            base_snapshot_id = self._text(base_snapshot_id, code="invalid_snapshot_id", maximum=256)
        refresh_token = body.get("refresh_token")
        if refresh_token is not None:
            refresh_token = self._text(refresh_token, code="invalid_refresh_token", maximum=128)
        handler = self.refresh_handler
        if handler is None:
            raise RouteError("capability_unavailable", status=503)
        status, result = handler(
            device_id=principal.device_id,
            session_id=session_id,
            scope_id=scope_id,
            base_snapshot_id=base_snapshot_id,
            refresh_token=refresh_token,
        )
        return self._response(status, result)

    def _pull_open(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"device_id", "session_id", "scope_id", "local_generation"},
            allowed={
                "device_id",
                "session_id",
                "scope_id",
                "local_generation",
                "profile_id",
                "snapshot_id",
                "permit_id",
                "nonce",
                "applied_watermark",
                "generation",
                "snapshot_generation",
                "scope_generation",
            },
        )
        principal, session_id = self._authenticated(headers, body)
        service = self.snapshot_service
        if service is None:
            raise RouteError("capability_unavailable", status=503)
        result = service.open_pull(
            device_id=principal.device_id,
            session_id=session_id,
            scope_id=body["scope_id"],
            local_generation=body["local_generation"],
            profile_id=body.get("profile_id"),
            snapshot_id=body.get("snapshot_id"),
            permit_id=body.get("permit_id"),
            nonce=body.get("nonce"),
            applied_watermark=body.get("applied_watermark"),
            generation=body.get("generation"),
            snapshot_generation=body.get("snapshot_generation"),
            scope_generation=body.get("scope_generation"),
            max_response_bytes=self.limits.max_response_bytes,
            recovery_key=self._pull_recovery_key(headers),
        )
        return self._response(200, result)

    def _pull_page(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={
                "device_id",
                "session_id",
                "scope_id",
                "local_generation",
                "permit_id",
                "nonce",
                "page_index",
                "cursor",
            },
            allowed={
                "device_id",
                "session_id",
                "scope_id",
                "local_generation",
                "permit_id",
                "nonce",
                "page_index",
                "cursor",
                "profile_id",
                "snapshot_id",
                "generation",
                "snapshot_generation",
                "scope_generation",
            },
        )
        principal, session_id = self._authenticated(headers, body)
        service = self.snapshot_service
        if service is None:
            raise RouteError("capability_unavailable", status=503)
        result = service.page(
            device_id=principal.device_id,
            session_id=session_id,
            scope_id=body["scope_id"],
            local_generation=body["local_generation"],
            permit_id=body["permit_id"],
            nonce=body["nonce"],
            page_index=body["page_index"],
            cursor=body["cursor"],
            profile_id=body.get("profile_id"),
            snapshot_id=body.get("snapshot_id"),
            generation=body.get("generation"),
            snapshot_generation=body.get("snapshot_generation"),
            scope_generation=body.get("scope_generation"),
        )
        return self._response(200, result)

    def _pull_commit(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={
                "device_id",
                "session_id",
                "scope_id",
                "local_generation",
                "permit_id",
                "nonce",
            },
            allowed={
                "device_id",
                "session_id",
                "scope_id",
                "local_generation",
                "permit_id",
                "nonce",
                "profile_id",
                "snapshot_id",
                "generation",
                "snapshot_generation",
                "scope_generation",
            },
        )
        principal, session_id = self._authenticated(headers, body)
        service = self.snapshot_service
        if service is None:
            raise RouteError("capability_unavailable", status=503)
        result = service.commit(
            device_id=principal.device_id,
            session_id=session_id,
            scope_id=body["scope_id"],
            local_generation=body["local_generation"],
            permit_id=body["permit_id"],
            nonce=body["nonce"],
            profile_id=body.get("profile_id"),
            snapshot_id=body.get("snapshot_id"),
            generation=body.get("generation"),
            snapshot_generation=body.get("snapshot_generation"),
            scope_generation=body.get("scope_generation"),
        )
        return self._response(200, result)

    @staticmethod
    def _validate_event(event: Any, session_id: str) -> Dict[str, Any]:
        if not isinstance(event, dict):
            raise RouteError("invalid_event")
        allowed = {"event_id", "sequence", "payload", "payload_hash"}
        if set(event) - allowed or not {"event_id", "sequence", "payload"}.issubset(event):
            raise RouteError("invalid_event")
        if not isinstance(event["event_id"], str) or not isinstance(event["sequence"], int) or isinstance(event["sequence"], bool):
            raise RouteError("invalid_event")
        if event["event_id"] != "%s:%s" % (session_id, event["sequence"]):
            raise RouteError("event_session_mismatch")
        if not isinstance(event["payload"], dict):
            raise RouteError("invalid_event")
        try:
            immutable = ImmutableReviewEvent.create(
                event["event_id"], event["sequence"], event["payload"]
            )
        except Exception:
            raise RouteError("invalid_event") from None
        if "payload_hash" in event and event["payload_hash"] != immutable.payload_hash:
            raise RouteError("invalid_event")
        normalized = dict(event)
        if "payload_hash" in normalized and not isinstance(normalized["payload_hash"], str):
            raise RouteError("invalid_event")
        return normalized

    def _preflight_conflicts(
        self, device_id: str, session_id: str, events: Sequence[Dict[str, Any]]
    ) -> None:
        event_ids = [event["event_id"] for event in events]
        existing = {
            receipt.event_id: receipt
            for receipt in self.inbox.query_receipts(
                device_id, session_id, event_ids=event_ids
            )
        }
        conflicts = []
        for event in events:
            current = existing.get(event["event_id"])
            if current is None:
                continue
            immutable = ImmutableReviewEvent.create(
                event["event_id"], event["sequence"], event["payload"]
            )
            if current.payload_hash != immutable.payload_hash:
                conflicts.append(event)
        if conflicts:
            # 仅持久化冲突证据，不把同一批中的新事件半提交为成功 ACK。
            for event in conflicts:
                self.inbox.receive_batch(device_id, session_id, [event])
            raise RouteError("receipt_conflict", status=409)

    def _reviews(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"device_id", "session_id", "events"},
            allowed={"device_id", "session_id", "events"},
        )
        principal, session_id = self._authenticated(headers, body)
        events = body["events"]
        if not isinstance(events, list) or not events or len(events) > self.limits.max_review_events:
            raise RouteError("review_batch_too_large", status=413)
        validated = [self._validate_event(event, session_id) for event in events]
        with self._review_lock:
            self._preflight_conflicts(principal.device_id, session_id, validated)
            batch = self.inbox.receive_batch(principal.device_id, session_id, validated)
        if any(receipt.status == "conflict" for receipt in batch.receipts):
            raise RouteError("receipt_conflict", status=409)
        return self._response(
            200,
            {
                "durable": bool(batch.durable),
                "committed_at_ms": batch.committed_at_ms,
                "receipts": [receipt.to_payload() for receipt in batch.receipts],
                "ack_semantics": "durably_received_not_necessarily_applied",
            },
        )

    def _compact_capabilities(self) -> Dict[str, Any]:
        if callable(self.compact_capability_reader):
            try:
                return dict(self.compact_capability_reader())
            except Exception:
                pass
        return {"protocol": "x4-3", "apply_enabled": False, "requires_frozen_context": True}

    def _compact_reviews(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"protocol", "device_id", "session_id", "snapshot_id", "base_sequence", "events"},
            allowed={"protocol", "device_id", "session_id", "snapshot_id", "base_sequence", "events"},
        )
        if body["protocol"] != "x4-3":
            raise RouteError("unsupported_protocol")
        principal, session_id = self._authenticated(headers, body)
        events = body["events"]
        if not isinstance(events, list) or not events or len(events) > self.limits.max_review_events:
            raise RouteError("review_batch_too_large", status=413)
        snapshot_id = self._text(body["snapshot_id"], code="snapshot_id_required", maximum=256)
        with self._review_lock:
            receipts, conflict = self.store.receive_compact_batch(
                principal.device_id, session_id, snapshot_id, body["base_sequence"], events
            )
        if conflict:
            raise RouteError("receipt_conflict", status=409)
        payload = {
            "protocol": "x4-3",
            "durable": True,
            "receipts": [receipt.to_payload() for receipt in receipts],
            "ack_semantics": "durably_received_not_applied",
        }
        if self.compact_job_requester is not None:
            # Store 已提交后才排队；worker 不会把 Collection 交给 HTTP 路由。
            payload["apply_job"] = self._request_compact_job(principal.device_id, session_id)
        return self._response(
            200,
            payload,
        )

    @staticmethod
    def _compact_job_view(job: Any) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        if isinstance(job, Mapping):
            value = job
        else:
            converter = getattr(job, "to_dict", None)
            if not callable(converter):
                return {"state": "unavailable", "reason": "invalid_job_status"}
            try:
                value = converter()
            except Exception:
                return {"state": "unavailable", "reason": "invalid_job_status"}
        if not isinstance(value, Mapping):
            return {"state": "unavailable", "reason": "invalid_job_status"}
        # 不向设备暴露 profile identity、runtime epoch 或其他宿主上下文。
        keys = (
            "job_id", "state", "result_status", "reason", "cancel_requested",
            "generation_before", "generation_after", "events_applied",
        )
        return {key: value[key] for key in keys if key in value}

    def _request_compact_job(self, device_id: str, session_id: str) -> Dict[str, Any]:
        requester = self.compact_job_requester
        if requester is None:
            return {"state": "unavailable", "reason": "compact_worker_unavailable"}
        try:
            view = self._compact_job_view(requester(device_id, session_id))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not code:
                code = "compact_job_unavailable"
            return {"state": "not_scheduled", "reason": code}
        return view or {"state": "not_scheduled", "reason": "compact_job_unavailable"}

    def _compact_receipts(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"protocol", "device_id", "session_id"},
            allowed={"protocol", "device_id", "session_id", "after_sequence"},
        )
        if body["protocol"] != "x4-3":
            raise RouteError("unsupported_protocol")
        principal, session_id = self._authenticated(headers, body)
        page_size = min(self.limits.max_event_ids, 100)
        receipts = self.store.query_compact_receipts(
            principal.device_id, session_id, after_sequence=body.get("after_sequence"),
            limit=page_size + 1,
        )
        has_more = len(receipts) > page_size
        receipts = receipts[:page_size]
        return self._response(
            200,
            {
                "protocol": "x4-3",
                "receipts": [receipt.to_payload() for receipt in receipts],
                "has_more": has_more,
                "application": "not_applied",
                "ack_semantics": "durably_received_not_applied",
            },
        )

    @staticmethod
    def _filter_applications(records: Sequence[Any], event_ids: Optional[Set[str]], after: Optional[int]) -> List[Dict[str, Any]]:
        result = []
        for record in records:
            if event_ids is not None and record.event_id not in event_ids:
                continue
            if after is not None and record.sequence <= after:
                continue
            result.append(record.to_payload())
        return result

    def _receipts(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"device_id", "session_id"},
            allowed={"device_id", "session_id", "event_ids", "after_sequence"},
        )
        principal, session_id = self._authenticated(headers, body)
        event_ids: Optional[List[str]] = None
        if "event_ids" in body:
            if not isinstance(body["event_ids"], list) or len(body["event_ids"]) > self.limits.max_event_ids:
                raise RouteError("invalid_event_ids")
            event_ids = [self._text(value, code="invalid_event_ids", maximum=256) for value in body["event_ids"]]
        after = body.get("after_sequence")
        if after is not None and (isinstance(after, bool) or not isinstance(after, int) or after < 1):
            raise RouteError("invalid_after_sequence")
        receipts = self.inbox.query_receipts(
            principal.device_id, session_id, event_ids=event_ids, after_sequence=after
        )
        applications = self.inbox.query_applications(principal.device_id, session_id)
        allowed_ids = set(event_ids) if event_ids else None
        return self._response(
            200,
            {
                "receipts": [receipt.to_payload() for receipt in receipts],
                "applications": self._filter_applications(applications, allowed_ids, after),
                "ack_semantics": "durably_received_not_necessarily_applied",
            },
        )

    @staticmethod
    def _counts(records: Sequence[Any], statuses: Sequence[str]) -> Dict[str, int]:
        counts = {status: 0 for status in statuses}
        counts.update(Counter(record.status for record in records))
        return counts

    @staticmethod
    def _watermark(applications: Sequence[Any]) -> Any:
        applied = {record.sequence for record in applications if record.status == "applied"}
        watermark = 0
        while watermark + 1 in applied:
            watermark += 1
        return {"known": watermark} if watermark else "unknown"

    def _status(self, headers: Mapping[str, Any], raw: bytes) -> RouteResponse:
        body = self._body(
            raw,
            required={"device_id", "session_id"},
            allowed={"device_id", "session_id"},
        )
        principal, session_id = self._authenticated(headers, body)
        receipts = self.inbox.query_receipts(principal.device_id, session_id)
        applications = self.inbox.query_applications(principal.device_id, session_id)
        compact_receipts = self.inbox.query_compact_receipts(principal.device_id, session_id)
        compact_applications = self.inbox.query_compact_applications(principal.device_id, session_id)
        compact_receipt_hashes = {record.event_id: record.payload_hash for record in compact_receipts}
        compact_application_hashes = {record.event_id: record.payload_hash for record in compact_applications}
        compact_state_mismatch = compact_receipt_hashes != compact_application_hashes
        compact_watermark_value = self.inbox.compact_applied_watermark(principal.device_id, session_id)
        compact_state_unverified = (
            bool(compact_applications)
            and all(record.status == "applied" for record in compact_applications)
            and compact_watermark_value is None
        )
        delivery_counts = self._counts(
            receipts,
            ("received", "pending_apply", "applying", "applied", "unknown", "conflict", "abandoned"),
        )
        application_counts = self._counts(
            applications, ("pending_apply", "applying", "applied", "unknown", "conflict", "abandoned")
        )
        compact_delivery_counts = self._counts(compact_receipts, ("received",))
        compact_application_counts = self._counts(
            compact_applications, ("not_applied", "applying", "applied", "unknown", "conflict")
        )
        if compact_state_mismatch or compact_state_unverified:
            phase = "blocked"
        elif delivery_counts["conflict"] or application_counts["conflict"] or compact_application_counts["conflict"]:
            phase = "blocked"
        elif delivery_counts["unknown"] or application_counts["unknown"] or compact_application_counts["unknown"]:
            phase = "reconcile_application"
        elif any(record.recovery_required for record in compact_applications):
            phase = "recover_application"
        elif delivery_counts["applying"] or application_counts["applying"] or compact_application_counts["applying"]:
            phase = "pending_apply"
        elif (delivery_counts["received"] or delivery_counts["pending_apply"]
              or application_counts["pending_apply"] or compact_application_counts["not_applied"]):
            phase = "pending_apply"
        elif receipts or compact_applications:
            phase = "applied"
        else:
            phase = "idle"
        reasons = [record.reason for record in receipts if record.reason]
        reasons.extend(record.reason for record in applications if record.reason)
        reasons.extend(record.reason for record in compact_applications if record.reason)
        if compact_state_mismatch:
            reasons.append("compact_application_receipt_mismatch")
        if compact_state_unverified:
            reasons.append("compact_application_receipt_invalid")
        compact_job = None
        if self.compact_job_status_reader is not None:
            try:
                compact_job = self._compact_job_view(
                    self.compact_job_status_reader(principal.device_id, session_id)
                )
            except Exception:
                compact_job = {"state": "unavailable", "reason": "compact_job_status_unavailable"}
        compact_pending = any(
            record.status in {"not_applied", "applying", "unknown"}
            for record in compact_applications
        )
        if (
            compact_job is None
            and compact_pending
            and not compact_state_mismatch
            and not compact_state_unverified
            and compact_application_counts["conflict"] == 0
            and self.compact_job_requester is not None
        ):
            # 重启后补排持久未完成事件；若历史作业仍可查询，绝不重复启动。
            compact_job = self._request_compact_job(principal.device_id, session_id)
        response_payload = {
            "profile_id": principal.profile_id,
            "device_id": principal.device_id,
            "session_id": session_id,
            "phase": phase,
            "delivery": {"counts": delivery_counts, "total": len(receipts)},
            "application": {
                "counts": application_counts,
                "total": len(applications),
                "applied_watermark": self._watermark(applications),
            },
            "compact_delivery": {
                "counts": compact_delivery_counts,
                "total": len(compact_receipts),
                "ack_semantics": "received_not_applied",
            },
            "compact_application": {
                "counts": compact_application_counts,
                "total": len(compact_applications),
                "recovery_required": sum(1 for record in compact_applications if record.recovery_required),
                "applied_watermark": (
                    {"known": compact_watermark_value}
                    if compact_watermark_value is not None else "unknown"
                ),
            },
            "last_error": reasons[-1] if reasons else None,
            "ready_scope_id": (
                self.store.latest_current_scope_id()
                if self.snapshot_service is not None
                else None
            ),
        }
        response_payload["compact_application_capability"] = self._compact_capabilities()
        if self.compact_job_status_reader is not None or self.compact_job_requester is not None:
            response_payload["compact_job"] = compact_job
        return self._response(
            200,
            response_payload,
        )


__all__ = ["DeviceRouter", "RouteError", "RouteLimits", "RouteResponse"]
