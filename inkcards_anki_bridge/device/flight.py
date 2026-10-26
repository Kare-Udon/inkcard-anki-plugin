"""接收有界阶段记录；只允许固定诊断字段，不接受卡面、路径或凭据。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

METHODS = {
    "begin",
    "put_descriptor",
    "admit_candidate",
    "missing_files",
    "put_file",
    "prepare",
    "publish",
    "warm_identity_step",
}
STAGES = (
    METHODS
    | {name + "_done" for name in METHODS}
    | {
        "collect_repository",
        "collect_repository_done",
        "sync_start",
        "sync_finish",
        "callback_fault",
        "descriptor_hash",
        "descriptor_existing",
        "descriptor_decode",
        "descriptor_validate",
        "descriptor_write",
        "descriptor_progress",
        "file_hash",
        "file_existing",
        "file_scan",
        "file_write",
        "file_progress",
        "wire_decode",
        "wire_decode_done",
        "network_start",
        "network_ready",
    }
)
CODES = {
    "time_budget",
    "memory_error",
    "lua_error",
    "flight_record_failed",
    "handshake",
    "status",
    "refresh",
    "open",
    "descriptor",
    "file",
    "commit",
    "commit_status",
    "upload",
    "receipts",
    "diagnostics",
    "pair",
    "pair_confirm",
    "application_wait",
}


def validate_report(value):
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "app_version",
        "status",
        "events",
    }:
        raise ValueError("flight_shape")
    if (
        value["schema"] != "inkcards-sync-flight-v1"
        or value["app_version"] not in {"0.2.68", "0.2.69", "0.2.70", "0.2.71"}
        or value["status"] not in {"running", "complete", "failed", "cancelled"}
    ):
        raise ValueError("flight_version")
    events = value["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 16:
        raise ValueError("flight_events")
    previous = -1
    for event in events:
        if (
            not isinstance(event, dict)
            or not {"sequence", "stage", "ms", "heap"} <= set(event)
            or set(event)
            - {"sequence", "stage", "ms", "heap", "bytes", "kind", "line", "code"}
        ):
            raise ValueError("flight_event")
        if not isinstance(event["stage"], str) or event["stage"] not in STAGES:
            raise ValueError("flight_stage")
        for field, maximum in {
            "sequence": 2147483647,
            "ms": 2147483647,
            "heap": 2147483647,
            "bytes": 262144,
            "line": 10000000,
        }.items():
            if field in event and (
                type(event[field]) is not int or not 0 <= event[field] <= maximum
            ):
                raise ValueError("flight_number")
        if event["sequence"] <= previous:
            raise ValueError("flight_sequence")
        previous = event["sequence"]
        if "kind" in event and (
            not isinstance(event["kind"], str)
            or event["kind"] not in {"text", "schedule", "index", "media"}
        ):
            raise ValueError("flight_kind")
        if "code" in event and (
            not isinstance(event["code"], str) or event["code"] not in CODES
        ):
            raise ValueError("flight_code")
    return value


def save_report(root: Path, report):
    line = (
        json.dumps(
            validate_report(report), ensure_ascii=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    if len(line) > 8192:
        raise ValueError("flight_bytes")
    directory = root / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "device-flight.jsonl"
    history = (
        path.read_bytes().splitlines(keepends=True)[-15:]
        if path.exists() and path.stat().st_size <= 65536
        else []
    )
    history.append(line)
    while sum(map(len, history)) > 65536:
        history.pop(0)
    fd, temporary = tempfile.mkstemp(
        prefix="device-flight-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(b"".join(history))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
