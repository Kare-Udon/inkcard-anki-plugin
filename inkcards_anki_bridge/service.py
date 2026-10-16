"""认证之后的窄 RPC 分派；不保存也不直接读取 collection。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .dispatcher import DispatchTimeout, Dispatcher
from .protocol import (
    BRIDGE_VERSION,
    CAPABILITIES,
    DISPATCH_TIMEOUT_SECONDS,
    MAX_CARD_IDS,
    PROTOCOL_VERSION,
    SUPPORTED_ANKI_VERSION,
    ProtocolError,
)


SNAPSHOT_RESULT_KEYS = frozenset(
    {
        "snapshot_schema",
        "profile_principal",
        "anki_version",
        "scheduler",
        "scope",
        "cards",
        "complete",
        "limitations",
    }
)


class SnapshotExporter(Protocol):
    def export(self, card_ids: list[str]) -> dict[str, Any]:
        """在 dispatcher 已选定的执行上下文中读取快照。"""


@dataclass(frozen=True)
class BridgeIdentity:
    instance_id: str
    profile_principal: str
    token: str
    anki_version: str
    python_version: str
    created_at_ms: int


class BridgeService:
    """HTTP worker 只能通过这里调用 dispatcher。"""

    def __init__(
        self,
        identity: BridgeIdentity,
        dispatcher: Dispatcher,
        snapshot_exporter: SnapshotExporter,
        *,
        profile_ready: Callable[[], bool] | None = None,
    ) -> None:
        self.identity = identity
        self._dispatcher = dispatcher
        self._snapshot_exporter = snapshot_exporter
        self._profile_ready = profile_ready or (lambda: True)

    def handle(self, method: str, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "hello":
            self._require_empty_params(params)
            return self._hello()
        if method == "capabilities":
            self._require_empty_params(params)
            return self._capabilities()
        if method == "export_scheduling_snapshot":
            return self._export_scheduling_snapshot(params)
        if method == "apply_events":
            raise ProtocolError(
                "capability_disabled",
                "event application is disabled in M1",
                status=403,
            )
        if method == "status":
            self._require_empty_params(params)
            return self._status()
        if method == "watermark":
            self._require_empty_params(params)
            return self._watermark()
        raise ProtocolError("unknown_method", "method is not part of bridge v1")

    @staticmethod
    def _require_empty_params(params: dict[str, Any]) -> None:
        if params:
            raise ProtocolError("invalid_params", "this endpoint accepts an empty params object", status=422)

    def _hello(self) -> dict[str, Any]:
        return {
            "bridge_version": BRIDGE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "instance_id": self.identity.instance_id,
            "profile_principal": self.identity.profile_principal,
            "anki_version": self.identity.anki_version,
            "python_version": self.identity.python_version,
            "supported_anki_versions": [SUPPORTED_ANKI_VERSION],
            "capabilities": dict(CAPABILITIES),
            "limits": self._limits(),
        }

    def _capabilities(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "bridge_version": BRIDGE_VERSION,
            "capabilities": dict(CAPABILITIES),
            "write_enabled": False,
            "disabled_reason": "M1 is read-only; event application is deliberately closed",
            "limits": self._limits(),
        }

    def _limits(self) -> dict[str, Any]:
        from .protocol import (
            MAX_REQUEST_BYTES,
            MAX_REQUEST_ID_BYTES,
            MAX_RESPONSE_BYTES,
            REQUEST_SOCKET_TIMEOUT_SECONDS,
        )

        return {
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_request_id_bytes": MAX_REQUEST_ID_BYTES,
            "max_card_ids": MAX_CARD_IDS,
            "dispatch_timeout_ms": int(DISPATCH_TIMEOUT_SECONDS * 1000),
            "request_socket_timeout_ms": int(REQUEST_SOCKET_TIMEOUT_SECONDS * 1000),
        }

    def _export_scheduling_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        card_ids = params.get("card_ids")
        if not isinstance(card_ids, list) or not card_ids:
            raise ProtocolError(
                "invalid_card_ids",
                "card_ids must be a non-empty array",
                status=422,
            )
        if len(card_ids) > MAX_CARD_IDS:
            raise ProtocolError("too_many_card_ids", "card_ids exceeds the configured limit", status=422)
        if any(not isinstance(card_id, str) for card_id in card_ids):
            raise ProtocolError("invalid_card_ids", "card_ids must contain strings", status=422)
        if len(set(card_ids)) != len(card_ids):
            raise ProtocolError("duplicate_card_id", "card_ids must be unique", status=422)
        for card_id in card_ids:
            if not card_id.isdecimal() or int(card_id) <= 0:
                raise ProtocolError("invalid_card_id", "card ids must be positive decimal strings", status=422)

        try:
            snapshot = self._dispatcher.submit(
                lambda: self._snapshot_exporter.export(list(card_ids)),
                DISPATCH_TIMEOUT_SECONDS,
            )
        except DispatchTimeout:
            raise ProtocolError(
                "dispatcher_timeout",
                "Anki execution context did not complete in time",
                status=503,
                retryable=True,
            ) from None
        except ProtocolError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "snapshot_export_failed")
            message = getattr(exc, "message", "snapshot export failed")
            status = int(getattr(exc, "status", 503))
            retryable = bool(getattr(exc, "retryable", True))
            raise ProtocolError(code, message, status=status, retryable=retryable) from exc

        if not isinstance(snapshot, dict):
            raise ProtocolError("invalid_snapshot", "snapshot exporter returned a non-object", status=503)
        if set(snapshot) != SNAPSHOT_RESULT_KEYS:
            raise ProtocolError(
                "invalid_snapshot",
                "snapshot exporter returned an unexpected result schema",
                status=503,
            )
        if snapshot["complete"] is not False:
            raise ProtocolError(
                "invalid_snapshot",
                "M1 snapshots must remain incomplete",
                status=503,
            )
        if snapshot["profile_principal"] != self.identity.profile_principal:
            raise ProtocolError(
                "profile_changed",
                "snapshot profile does not match the bridge instance",
                status=409,
                retryable=False,
            )
        if not isinstance(snapshot["cards"], list) or not isinstance(snapshot["limitations"], list):
            raise ProtocolError("invalid_snapshot", "snapshot cards/limitations have invalid types", status=503)
        snapshot = dict(snapshot)
        # instance_id 由当前 BridgeIdentity 注入，adapter 不得自行猜测或提供。
        snapshot["instance_id"] = self.identity.instance_id
        return snapshot

    def _status(self) -> dict[str, Any]:
        return {
            "instance_id": self.identity.instance_id,
            "profile_principal": self.identity.profile_principal,
            "state": "ready" if self._profile_ready() else "profile_not_ready",
            "profile_ready": bool(self._profile_ready()),
            "write_enabled": False,
            "transport": {"host": "127.0.0.1", "authenticated": True},
        }

    def _watermark(self) -> dict[str, Any]:
        return {
            "known": False,
            "value": None,
            "applied_events": False,
            "reason": "write_capability_disabled",
        }


def now_ms() -> int:
    return int(time.time() * 1000)
