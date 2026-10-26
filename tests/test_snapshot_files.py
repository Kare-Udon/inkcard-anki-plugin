from __future__ import annotations

import dataclasses
import hashlib
import http.client
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

from inkcards_anki_bridge.anki.application_context import CapturedApplicationContext
from inkcards_anki_bridge.anki.models import (
    CardSnapshot,
    DayTable,
    DeckSnapshot,
    DueKind,
    PresetSnapshot,
    SchedulerDue,
    SnapshotManifest,
    TypedSnapshot,
)
from inkcards_anki_bridge.device.auth import DeviceAuthenticator
from inkcards_anki_bridge.device.routes import DeviceRouter, RouteLimits
from inkcards_anki_bridge.device.server import DeviceServer
from inkcards_anki_bridge.domain.sync_gate import ImmutableReviewEvent
from inkcards_anki_bridge.services.inbox import InboxService
from inkcards_anki_bridge.services.snapshot_files import SnapshotFilesService
from inkcards_anki_bridge.services.snapshots import SnapshotService
from inkcards_anki_bridge.storage import ProfileStore


def make_snapshot(cards: List[Dict[str, Any]]) -> TypedSnapshot:
    card_rows = tuple(
        CardSnapshot(
            card_id=card["card_id"],
            note_id=card["note_id"],
            deck_id="20",
            ordinal=card["ordinal"],
            card_type="new",
            queue=0,
            due=SchedulerDue(DueKind.NEW_POSITION, str(card["card_id"])),
            question=card["question"],
            answer=card["answer"],
            schedule_revision=card.get("schedule_revision", "r1"),
        )
        for card in cards
    )
    ids = tuple(card.card_id for card in card_rows)
    return TypedSnapshot(
        manifest=SnapshotManifest(
            schema="inkcards.typed-snapshot.v1",
            profile_identity="profile-a",
            profile_generation=0,
            runtime_epoch="epoch-a",
            anki_version="26.9.3",
            anki_build="29bb700b",
            scope_card_ids=ids,
            scope_deck_ids=("20",),
            card_count=len(cards),
            included_card_count=len(cards),
        ),
        decks=(
            DeckSnapshot(
                "20", "Deck", preset_id="preset-a", new_limit=20, review_limit=200
            ),
        ),
        presets=(
            PresetSnapshot(
                "preset-a",
                name="Default",
                fsrs_params={"version": 6, "params": [1.0, 2.0]},
                desired_retention=0.9,
                learning_steps=(1, 10),
                relearning_steps=(10,),
                new_limit=20,
                review_limit=200,
                max_interval_days=36500,
                learning_steps_unit="minutes",
            ),
        ),
        day_table=DayTable(
            review_day=100,
            day_learning_day=100,
            day_cutoff_ms=1_700_000_000_000,
            first_day_start_ms=1_700_000_000_000,
            future_cutoffs_ms=tuple(1_700_000_000_000 + 86_400_000 * i for i in range(1, 91)),
        ),
        cards=card_rows,
        application_context=CapturedApplicationContext(
            profile_identity="profile-a",
            collection_generation=0,
            runtime_epoch="epoch-a",
            anki_version="26.9.3",
            anki_build="29bb700b",
        ),
    )


def descriptors(service: SnapshotFilesService, opened: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for page_index, _ in enumerate(opened["manifest"]["descriptor_pages"]):
        raw = service.manifest_page(
            device_id="device-a",
            session_id="session-a",
            transfer_id=opened["transfer_id"],
            page_index=page_index,
        )
        result.extend(json.loads(raw.decode("utf-8"))["files"])
    return result


class SnapshotFilesHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="inkcards-artifacts-", dir="/private/tmp")
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: 1_800_000_000_000)
        self.inbox = InboxService(self.store)
        self.snapshots = SnapshotService(self.store, clock=lambda: 1_800_000_000_000)
        self.files = SnapshotFilesService(self.store, clock=lambda: 1_800_000_000_001)
        self.router = DeviceRouter(
            self.auth,
            self.inbox,
            snapshot_service=self.snapshots,
            snapshot_files_service=self.files,
            limits=RouteLimits(max_request_bytes=1024, max_response_bytes=65536),
        )
        self.server: DeviceServer | None = None
        try:
            self.server = DeviceServer(self.router)
            self.server.start()
        except PermissionError as error:
            # 受限沙箱不能 bind loopback 时仍通过同一 route dispatcher 验证 wire 行为。
            if error.errno != 1:
                raise
            self.server = None
        code = self.auth.issue_pairing_code()
        credential = self.auth.pair(code, "device-a")
        self.headers = {"Authorization": "Bearer " + credential.token}
        status, _, _ = self.request(
            "POST", "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-a", "session_id": "session-a"},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.published = self.snapshots.publish(
            make_snapshot([
                {"card_id": "101", "note_id": "201", "ordinal": 0, "question": "Q1", "answer": "A1"},
                {"card_id": "102", "note_id": "202", "ordinal": 1, "question": "Q2", "answer": "A2"},
            ]),
            scope_id="deck:language",
            generation=0,
            applied_watermark=0,
        )

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.stop()
        self.store.close()
        self.tempdir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: Any,
        headers: Dict[str, str] | None = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.server is None:
            response = self.router.dispatch(method, path, headers or {}, body)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            try:
                payload_value = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload_value = response.body
            return response.status, payload_value, response_headers
        connection = http.client.HTTPConnection(self.server.host, self.server.port, timeout=3)
        values = {"Content-Type": "application/json"}
        values.update(headers or {})
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        try:
            payload_value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload_value = raw
        return status, payload_value, response_headers

    def open_artifacts(self, known_file_ids: List[str] | None = None) -> Dict[str, Any]:
        status, opened, _ = self.request(
            "POST",
            "/v2/artifacts/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
                "known_file_ids": known_file_ids or [],
            },
            self.headers,
        )
        self.assertEqual(status, 200, opened)
        return opened

    def test_real_http_download_commit_receipt_and_identity_checks(self) -> None:
        stale_ids = [hashlib.sha256(("stale-%d" % i).encode()).hexdigest() for i in range(25)]
        opened = self.open_artifacts(stale_ids)
        root = opened["manifest"]
        self.assertEqual(root["schema"], "inkcards.files.v1")
        self.assertEqual(root["snapshot_id"], self.published.snapshot_id)
        self.assertEqual(root["known_watermark"], "0")
        self.assertNotIn("applied_watermark", root["metadata"])
        self.assertEqual(root["metadata"]["catalog"], [{
            "deck_id": "20", "name": "Deck", "parent_id": None,
            "card_count": 2, "direct_card_count": 2, "has_children": False,
        }])
        self.assertEqual(root["metadata"].get("queue_index"), 1)
        self.assertEqual(root["metadata"]["config"]["params"], [1.0, 2.0])
        self.assertEqual(root["metadata"]["config"]["learning"], [60, 600])
        page_descriptor = root["descriptor_pages"][0]
        self.assertEqual(page_descriptor["kind"], "index")
        self.assertRegex(page_descriptor["name"], r"^p-[0-9a-f]{32}\.json$")
        self.assertGreater(page_descriptor["record_count"], 0)
        status, page, headers = self.request(
            "POST", "/v2/artifacts/manifest",
            {"device_id": "device-a", "session_id": "session-a", "transfer_id": opened["transfer_id"], "page_index": 0},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertIsInstance(page, dict)
        files = page["files"]
        self.assertEqual(page_descriptor["record_count"], len(files))
        self.assertTrue(files)
        self.assertTrue(all(isinstance(d.get("queue"), list) for d in files if d["kind"] == "index"))
        for descriptor in files:
            prefix = {"text": "t", "schedule": "s", "index": "i"}[descriptor["kind"]]
            self.assertRegex(descriptor["name"], rf"^{prefix}-[0-9a-f]{{32}}\.bin$")
            status, package, headers = self.request(
                "POST", "/v2/artifacts/file",
                {"device_id": "device-a", "session_id": "session-a", "transfer_id": opened["transfer_id"], "file_id": descriptor["id"]},
                self.headers,
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/octet-stream")
            self.assertTrue(package.startswith(b"ICPK1\n"))
            self.assertEqual(hashlib.sha256(package).hexdigest(), descriptor["id"])

        commit_key = str(uuid.uuid4())
        commit_body = {
            "device_id": "device-a",
            "session_id": "session-a",
            "transfer_id": opened["transfer_id"],
            "commit_key": commit_key,
        }
        status, receipt, _ = self.request("POST", "/v2/artifacts/commit", commit_body, self.headers)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(receipt["known_watermark"], "0")
        event = ImmutableReviewEvent.create(
            "session-a:1", 1,
            {"card_id": "101", "rating": 3, "answered_at_ms": 1_700_000_000_001},
        )
        self.store.receive_batch("device-a", "session-a", [event])
        self.assertEqual(self.request("POST", "/v2/artifacts/commit", commit_body, self.headers)[1], receipt)
        status, observed, _ = self.request(
            "POST", "/v2/artifacts/status",
            {"device_id": "device-a", "session_id": "session-a", "transfer_id": opened["transfer_id"], "commit_key": commit_key},
            self.headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(observed, receipt)
        status, conflict, _ = self.request(
            "POST", "/v2/artifacts/status",
            {"device_id": "device-a", "session_id": "session-a", "transfer_id": opened["transfer_id"], "commit_key": str(uuid.uuid4())},
            self.headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "artifact_transfer_conflict")
        status, wrong_identity, _ = self.request(
            "POST", "/v2/artifacts/status",
            {"device_id": "device-b", "session_id": "session-a", "transfer_id": opened["transfer_id"], "commit_key": commit_key},
            self.headers,
        )
        self.assertEqual(status, 401)

        old_route_body = json.dumps({"device_id": "device-a", "session_id": "session-a", "padding": "x" * 1500}).encode()
        if self.server is None:
            response = self.router.dispatch("POST", "/v2/sync/status", self.headers, old_route_body)
            self.assertEqual(response.status, 413)
        else:
            connection = http.client.HTTPConnection(self.server.host, self.server.port, timeout=3)
            connection.request("POST", "/v2/sync/status", body=old_route_body, headers=self.headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
            connection.close()

    def test_received_not_applied_gate_blocks_artifact_open_and_commit(self) -> None:
        opened = self.open_artifacts()
        event = ImmutableReviewEvent.create(
            "session-a:1", 1,
            {"card_id": "101", "rating": 3, "answered_at_ms": 1_700_000_000_001},
        )
        self.store.receive_batch("device-a", "session-a", [event])
        status, blocked, _ = self.request(
            "POST", "/v2/artifacts/commit",
            {
                "device_id": "device-a", "session_id": "session-a",
                "transfer_id": opened["transfer_id"], "commit_key": str(uuid.uuid4()),
            },
            self.headers,
        )
        self.assertEqual(status, 409)
        self.assertTrue(blocked["error"]["code"].startswith("pull_gate_"))
        status, blocked, _ = self.request(
            "POST", "/v2/artifacts/open",
            {
                "device_id": "device-a", "session_id": "session-a",
                "scope_id": "deck:language", "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
            },
            self.headers,
        )
        self.assertEqual(status, 409)
        self.assertTrue(blocked["error"]["code"].startswith("pull_gate_"))

    def test_artifact_open_accepts_explicit_applied_watermark(self) -> None:
        status, opened, _ = self.request(
            "POST",
            "/v2/artifacts/open",
            {
                "device_id": "device-a",
                "session_id": "session-a",
                "scope_id": "deck:language",
                "local_generation": 0,
                "snapshot_id": self.published.snapshot_id,
                "applied_watermark": {"known": 0},
            },
            self.headers,
        )
        self.assertEqual(status, 200, opened)
        self.assertEqual(opened["manifest"]["known_watermark"], "0")

    def test_artifact_open_scopes_known_watermark_to_requesting_device_session(self) -> None:
        seed = self.snapshots.publish(
            {
                "snapshot_schema": "candidate.v1",
                "manifest": {},
                "decks": [],
                "presets": [],
                "day_table": {},
                "complete": True,
                "unsupported": [],
                "cards": [{"card_id": "101"}, {"card_id": "102"}],
            },
            scope_id="deck:language",
            generation=0,
            applied_watermark=0,
        )
        rows = [
            [1, 3 + sequence % 2, 1_700_000_000_000 + sequence * 1_000, 120]
            for sequence in range(1, 5)
        ]
        self.store.receive_compact_batch(
            "device-a", "session-a", seed.snapshot_id, 1, rows
        )
        for sequence in range(1, 5):
            before = {"card_id": "101", "revision": sequence - 1}
            after = {"card_id": "101", "revision": sequence}
            plan = {
                "required_evidence": ["score_applied"],
                "expected_score_after": after,
            }
            application = self.store.begin_compact_apply(
                "device-a",
                "session-a",
                sequence,
                collection_identity="synthetic-collection",
                collection_generation=self.store.get_collection_generation(),
                policy_json={"algorithm": "FSRS", "randomization": "seeded"},
                before_json=before,
                plan_json=plan,
                expected_after_json=after,
                intent_id="watermark-intent-%d" % sequence,
                attempt_id="watermark-attempt-%d" % sequence,
            )
            self.store.record_compact_score_evidence(
                "device-a",
                "session-a",
                sequence,
                stage="score_applied",
                evidence_json={"source": "synthetic-artifact-watermark", "sequence": sequence},
            )
            self.store.finish_compact_apply(
                application.intent_id,
                after_json=after,
                collection_generation_after=sequence,
            )

        gate_a = self.store.pull_gate("device-a", "session-a")
        self.assertTrue(gate_a.allowed)
        self.assertEqual(gate_a.applied_watermark, 4)

        credential_b = self.auth.pair(self.auth.issue_pairing_code(), "device-b")
        headers_b = {"Authorization": "Bearer " + credential_b.token}
        status, handshake, _ = self.request(
            "POST",
            "/v2/handshake",
            {"protocol": "x4-2", "device_id": "device-b", "session_id": "session-b"},
            headers_b,
        )
        self.assertEqual(status, 200, handshake)
        gate_b = self.store.pull_gate("device-b", "session-b")
        self.assertTrue(gate_b.allowed)
        self.assertEqual(gate_b.applied_watermark, 0)
        self.assertIsNone(self.store.compact_applied_watermark("device-b", "session-b"))

        typed = make_snapshot([
            {"card_id": "101", "note_id": "201", "ordinal": 0, "question": "Q1", "answer": "A1"},
            {"card_id": "102", "note_id": "202", "ordinal": 1, "question": "Q2", "answer": "A2"},
        ])
        typed = dataclasses.replace(
            typed,
            manifest=dataclasses.replace(typed.manifest, profile_generation=4),
            application_context=dataclasses.replace(
                typed.application_context, collection_generation=4
            ),
        )
        self.published = self.snapshots.publish(
            typed, scope_id="deck:language", generation=4, applied_watermark=4
        )

        refresh_sessions: List[str] = []

        def refresh_handler(**request: Any) -> Tuple[int, Dict[str, Any]]:
            refresh_sessions.append(request["session_id"])
            # 作用域缓存向两个 session 返回同一份已足够新的快照。
            return 200, self.published.to_payload()

        refresh_router = DeviceRouter(
            self.auth,
            self.inbox,
            snapshot_service=self.snapshots,
            snapshot_files_service=self.files,
            refresh_handler=refresh_handler,
        )

        def refresh_for(device_id: str, session_id: str, headers: Dict[str, str]):
            raw = json.dumps(
                {"device_id": device_id, "session_id": session_id, "scope_id": "deck:language"},
                separators=(",", ":"),
            ).encode("utf-8")
            response = refresh_router.dispatch("POST", "/v2/pull/refresh", headers, raw)
            return response.status, json.loads(response.body.decode("utf-8"))

        def open_for(device_id: str, session_id: str, headers: Dict[str, str], watermark: int):
            return self.request(
                "POST",
                "/v2/artifacts/open",
                {
                    "device_id": device_id,
                    "session_id": session_id,
                    "scope_id": "deck:language",
                    "local_generation": 0,
                    "snapshot_id": self.published.snapshot_id,
                    "applied_watermark": {"known": watermark},
                },
                headers,
            )

        def commit_and_check(
            device_id: str,
            session_id: str,
            headers: Dict[str, str],
            opened: Dict[str, Any],
        ):
            status, page, _ = self.request(
                "POST",
                "/v2/artifacts/manifest",
                {
                    "device_id": device_id,
                    "session_id": session_id,
                    "transfer_id": opened["transfer_id"],
                    "page_index": 0,
                },
                headers,
            )
            self.assertEqual(status, 200, page)
            for descriptor in page["files"]:
                status, package, _ = self.request(
                    "POST",
                    "/v2/artifacts/file",
                    {
                        "device_id": device_id,
                        "session_id": session_id,
                        "transfer_id": opened["transfer_id"],
                        "file_id": descriptor["id"],
                    },
                    headers,
                )
                self.assertEqual(status, 200, package)
            commit_key = str(uuid.uuid4())
            status, receipt, _ = self.request(
                "POST",
                "/v2/artifacts/commit",
                {
                    "device_id": device_id,
                    "session_id": session_id,
                    "transfer_id": opened["transfer_id"],
                    "commit_key": commit_key,
                },
                headers,
            )
            self.assertEqual(status, 200, receipt)
            status, status_receipt, _ = self.request(
                "POST",
                "/v2/artifacts/status",
                {
                    "device_id": device_id,
                    "session_id": session_id,
                    "transfer_id": opened["transfer_id"],
                    "commit_key": commit_key,
                },
                headers,
            )
            self.assertEqual(status, 200, status_receipt)
            self.assertEqual(status_receipt, receipt)
            return receipt

        status, refreshed_a = refresh_for("device-a", "session-a", self.headers)
        self.assertEqual(status, 200, refreshed_a)
        self.assertEqual(refreshed_a["applied_watermark"], {"known": 4})
        status, opened_a, _ = open_for("device-a", "session-a", self.headers, 4)
        self.assertEqual(status, 200, opened_a)
        self.assertEqual(opened_a["manifest"]["known_watermark"], "4")
        receipt_a = commit_and_check("device-a", "session-a", self.headers, opened_a)
        self.assertEqual(receipt_a["known_watermark"], "4")

        status, refreshed_b = refresh_for("device-b", "session-b", headers_b)
        self.assertEqual(status, 200, refreshed_b)
        self.assertEqual(refreshed_b["snapshot_id"], refreshed_a["snapshot_id"])
        self.assertEqual(refreshed_b["applied_watermark"], {"known": 4})
        status, opened_b, _ = open_for("device-b", "session-b", headers_b, 0)
        self.assertEqual(status, 200, opened_b)
        self.assertEqual(opened_b["manifest"]["known_watermark"], "0")
        self.assertEqual(self.files._manifest(self.published.snapshot_id)["known_watermark"], "4")
        self.assertEqual(refresh_sessions, ["session-a", "session-b"])
        receipt_b = commit_and_check("device-b", "session-b", headers_b, opened_b)
        self.assertEqual(receipt_b["known_watermark"], "0")

    def test_stable_slots_reuse_text_and_schedule_but_reindex_sort(self) -> None:
        def open_snapshot(snapshot: TypedSnapshot) -> Dict[str, Any]:
            published = self.snapshots.publish(
                snapshot, scope_id="deck:language", generation=0, applied_watermark=0
            )
            return self.files.open_transfer(
                device_id="device-a", session_id="session-a",
                snapshot_id=published.snapshot_id, scope_id="deck:language",
            )

        first = self.open_artifacts()
        first_desc = descriptors(self.files, first)
        base_cards = [
            {"card_id": "101", "note_id": "201", "ordinal": 0, "question": "Q1", "answer": "A1", "schedule_revision": "r1"},
            {"card_id": "102", "note_id": "202", "ordinal": 1, "question": "Q2", "answer": "A2", "schedule_revision": "r1"},
        ]
        schedule_update = [dict(card) for card in base_cards]
        schedule_update[0]["schedule_revision"] = "r2"
        second = open_snapshot(make_snapshot(schedule_update))
        second_desc = descriptors(self.files, second)
        first_by_kind = {kind: {d["id"] for d in first_desc if d["kind"] == kind} for kind in ("text", "schedule", "index")}
        second_by_kind = {kind: {d["id"] for d in second_desc if d["kind"] == kind} for kind in ("text", "schedule", "index")}
        self.assertEqual(first_by_kind["text"], second_by_kind["text"])
        self.assertNotEqual(first_by_kind["schedule"], second_by_kind["schedule"])

        text_update = [dict(card) for card in schedule_update]
        text_update[0]["question"] = "edited"
        third = open_snapshot(make_snapshot(text_update))
        third_desc = descriptors(self.files, third)
        third_by_kind = {kind: {d["id"] for d in third_desc if d["kind"] == kind} for kind in ("text", "schedule", "index")}
        self.assertNotEqual(second_by_kind["text"], third_by_kind["text"])
        self.assertEqual(second_by_kind["schedule"], third_by_kind["schedule"])

        sorted_update = list(reversed(text_update))
        fourth = open_snapshot(make_snapshot(sorted_update))
        fourth_desc = descriptors(self.files, fourth)
        fourth_by_kind = {kind: {d["id"] for d in fourth_desc if d["kind"] == kind} for kind in ("text", "schedule", "index")}
        self.assertEqual(third_by_kind["text"], fourth_by_kind["text"])
        self.assertEqual(third_by_kind["schedule"], fourth_by_kind["schedule"])
        self.assertNotEqual(third_by_kind["index"], fourth_by_kind["index"])


if __name__ == "__main__":
    unittest.main()
