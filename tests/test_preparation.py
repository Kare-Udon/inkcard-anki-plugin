"""设备到来前准备、短轮询、旧结果失效和跨进程恢复。"""

import dataclasses
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from inkcards_anki_bridge.anki.operations import (
    OperationResult,
    OperationState,
    QueryOpAdapter,
)
from inkcards_anki_bridge.anki.preparation import (
    PreparedSync,
    SourceSignature,
    media_signature,
)
from inkcards_anki_bridge.anki.snapshot_jobs import SnapshotJobState
from inkcards_anki_bridge.runtime import AddonRuntime
from inkcards_anki_bridge.services.snapshot_files import ArtifactError
from inkcards_anki_bridge.services.snapshots import SnapshotError
from tests.test_pull_refresh_runtime import FakeServer
from tests.test_snapshot_files import make_snapshot


class Scanner:
    def submit(self, context, operation, on_complete):
        self.complete = on_complete

    def finish(self, signature):
        self.complete(
            OperationResult(OperationState.COMPLETED, value=signature, executed=True)
        )


@pytest.fixture
def scene(tmp_path):
    with patch("inkcards_anki_bridge.runtime.DeviceServer", FakeServer):
        runtime = AddonRuntime(profile_id="profile-a", root=tmp_path, port=0).start()
    runtime.store.register_device("device-a", "token-a")
    runtime.store.open_session("device-a", "session-a")
    jobs = []

    def request(**kwargs):
        job = SimpleNamespace(state=SnapshotJobState.STARTED, result=None, error=None)
        job.poll_state = lambda: job.state
        jobs.append(job)
        return job

    runtime._snapshot_jobs = SimpleNamespace(
        request_snapshot=request, stop=lambda: None
    )
    scanner = Scanner()
    prepare = PreparedSync(runtime, scanner)
    runtime._preparation = prepare
    runtime.router.snapshot_files_service.require_prepared = True
    yield runtime, scanner, prepare, jobs
    runtime.stop()


def complete(runtime, prepare, job):
    typed = make_snapshot(
        [
            {
                "card_id": "101",
                "note_id": "201",
                "ordinal": 0,
                "question": "Q",
                "answer": "A",
            }
        ]
    )
    generation = runtime.collection_generation
    typed = dataclasses.replace(
        typed,
        manifest=dataclasses.replace(typed.manifest, profile_generation=generation),
        application_context=dataclasses.replace(
            typed.application_context, collection_generation=generation
        ),
    )
    job.result = runtime.publish_snapshot(
        typed,
        scope_id=runtime.default_sync_scope_id(),
        generation=generation,
        source_scope={"kind": "exclude", "ids": []},
    )
    job.state = SnapshotJobState.PUBLISHED
    prepare.tick()
    deadline = time.monotonic() + 3
    while prepare._packing and time.monotonic() < deadline:
        time.sleep(0.005)
    assert prepare.status()["state"] == "ready", prepare.status()


def args(runtime):
    return {
        "device_id": "device-a",
        "session_id": "session-a",
        "scope_id": runtime.default_sync_scope_id(),
    }


def test_pull_decision_checks_media_and_scope_without_waiting_for_pack(scene, tmp_path):
    runtime, scanner, prepare, jobs = scene
    media = tmp_path / "media"
    media.mkdir()
    signature = SourceSignature("source", str(media), media_signature(media), 2**31)
    prepare.tick()
    scanner.finish(signature)
    complete(runtime, prepare, jobs[-1])
    base = jobs[-1].result.snapshot_id
    with patch.object(
        runtime.store, "snapshot_has_only_own_reviews", return_value=True
    ) as proof:
        prepare.invalidate()
        assert prepare.can_skip_pull("device-a", "session-a", base)
        proof.assert_called_once_with(
            "device-a", "session-a", base, runtime.default_sync_scope_id()
        )
        assert not prepare.can_skip_pull("device-a", "session-a", "unknown-base")
        (media / "added.png").write_bytes(b"media")
        assert not prepare.can_skip_pull("device-a", "session-a", base)


def test_background_prepares_without_device_and_sync_never_builds(scene):
    runtime, scanner, prepare, jobs = scene
    prepare.tick()
    scanner.finish("source-1")
    assert len(jobs) == 1  # 设备还没有发送任何请求。
    status, body = runtime.refresh_pull(**args(runtime))
    assert status == 202 and body["state"] == "pending"
    complete(runtime, prepare, jobs[-1])
    service = runtime.router.snapshot_files_service
    with patch.object(
        service, "_prepare_snapshot", side_effect=AssertionError("HTTP 禁止打包")
    ):
        started = time.monotonic()
        status, ready = runtime.refresh_pull(**args(runtime))
        assert time.monotonic() - started < 0.25
        assert status == 200 and ready["state"] == "ready"
        opened = service.open_transfer(
            device_id="device-a",
            session_id="session-a",
            snapshot_id=ready["snapshot_id"],
            scope_id=ready["scope_id"],
        )
        assert opened["manifest"]["card_count"] == 1
    assert len(jobs) == 1
    with pytest.raises(SnapshotError) as error:
        runtime.refresh_pull(
            **{**args(runtime), "device_id": "other"},
            refresh_token=body["refresh_token"],
        )
    assert error.value.code == "snapshot_refresh_token_invalid"


def test_stale_publication_is_retried_without_marking_refresh_failed(scene):
    runtime, scanner, prepare, jobs = scene
    prepare.tick()
    scanner.finish("before")
    jobs[-1].state = SnapshotJobState.STALE_RESULT_DROPPED
    prepare.tick()
    assert runtime.refresh_pull(**args(runtime))[0] == 202
    prepare.tick()
    scanner.finish("after")
    assert len(jobs) == 2


def test_on_demand_does_not_touch_collection_until_requested_and_idle(scene):
    runtime, scanner, prepare, jobs = scene
    prepare._on_demand = True
    busy = True
    prepare._host_available = lambda: not busy
    prepare.invalidate()
    prepare.tick()
    assert not hasattr(scanner, "complete")
    status, body = runtime.refresh_pull(**args(runtime))
    assert status == 202 and body["state"] == "pending"
    prepare.tick()
    assert not hasattr(scanner, "complete")
    busy = False
    prepare.tick()
    scanner.finish("after")
    assert len(jobs) == 1
    complete(runtime, prepare, jobs[-1])
    assert runtime.refresh_pull(**args(runtime), refresh_token=body["refresh_token"])[0] == 200
    prepare.invalidate()
    prepare.tick()
    assert len(jobs) == 1


@pytest.mark.parametrize("writeback_timing", ["before_scan", "during_scan"])
def test_rating_writeback_keeps_refresh_pending_until_new_snapshot(
    scene, writeback_timing
):
    runtime, _, prepare, jobs = scene
    queued = []
    prepare.operations = QueryOpAdapter(
        submitter=queued.append, context_provider=runtime.snapshot_context
    )
    prepare.clock = lambda: 100

    def commit_rating_writeback():
        runtime.store.set_collection_generation(1)
        runtime.refresh_committed_collection_generation(1)

    def scan(*_):
        if writeback_timing == "during_scan" and runtime.collection_generation == 0:
            commit_rating_writeback()
        return "signature"

    with patch("inkcards_anki_bridge.anki.preparation.source_signature", scan):
        prepare.tick()
        status, pending = runtime.refresh_pull(**args(runtime))
        assert (status, pending["state"]) == (202, "pending")
        refresh = {**args(runtime), "refresh_token": pending["refresh_token"]}
        if writeback_timing == "before_scan":
            commit_rating_writeback()
        discarded = queued.pop(0)(object())
        assert discarded.state is OperationState.STALE_RESULT_DROPPED
        assert discarded.executed is (writeback_timing == "during_scan")
        assert not jobs
        status, pending = runtime.refresh_pull(**refresh)
        assert (status, pending["state"]) == (202, "pending")

        # 计时不前进，下一轮仍应立即扫描新版本。
        prepare.tick()
        assert queued.pop(0)(object()).state is OperationState.COMPLETED

    assert len(jobs) == 1
    complete(runtime, prepare, jobs[0])
    status, ready = runtime.refresh_pull(**refresh)
    assert (status, ready["state"], ready["snapshot_generation"]) == (200, "ready", 1)


@pytest.mark.parametrize("writeback_during_failure", [False, True])
def test_source_scan_exception_still_fails_refresh(scene, writeback_during_failure):
    runtime, _, prepare, _ = scene
    queued = []
    prepare.operations = QueryOpAdapter(
        submitter=queued.append, context_provider=runtime.snapshot_context
    )

    def broken_scan(*_):
        if writeback_during_failure:
            runtime.store.set_collection_generation(1)
            runtime.refresh_committed_collection_generation(1)
        raise RuntimeError("source read failed")

    with patch("inkcards_anki_bridge.anki.preparation.source_signature", broken_scan):
        prepare.tick()
        assert isinstance(queued.pop(0)(object()).error, RuntimeError)

    with pytest.raises(SnapshotError) as error:
        runtime.refresh_pull(**args(runtime))
    assert (error.value.status, error.value.code) == (409, "snapshot_prepare_failed")


def test_restart_validates_and_reuses_prepared_version(scene):
    runtime, scanner, prepare, jobs = scene
    prepare.tick()
    scanner.finish("source-1")
    complete(runtime, prepare, jobs[-1])
    prepare.close()
    restarted = PreparedSync(runtime, scanner)
    runtime._preparation = restarted
    status, _ = runtime.refresh_pull(**args(runtime))
    assert status == 202  # 扫描确认前不能宣称就绪。
    restarted.tick()
    scanner.finish("source-1")
    status, ready = runtime.refresh_pull(**args(runtime))
    assert status == 200
    assert ready["snapshot_id"] == jobs[-1].result.snapshot_id
    assert len(jobs) == 1


def test_media_change_advances_generation_and_rebuilds_before_ready(scene):
    runtime, scanner, prepare, jobs = scene
    prepare.tick()
    scanner.finish("source-1")
    complete(runtime, prepare, jobs[-1])
    prepare._next_scan = 0
    prepare.tick()
    scanner.finish("source-2")
    assert runtime.collection_generation == 1
    assert len(jobs) == 2
    assert runtime.refresh_pull(**args(runtime))[0] == 202
    complete(runtime, prepare, jobs[-1])
    assert runtime.refresh_pull(**args(runtime))[0] == 200
    # 卡片编辑钩子及时使已准备版本失效。
    runtime.note_external_collection_change()
    assert runtime.refresh_pull(**args(runtime))[0] == 202


def test_artifacts_open_refuses_unprepared_snapshot_without_building(scene):
    runtime, scanner, prepare, _jobs = scene
    prepare.tick()
    scanner.finish("source-1")
    typed = make_snapshot(
        [
            {
                "card_id": "101",
                "note_id": "201",
                "ordinal": 0,
                "question": "Q",
                "answer": "A",
            }
        ]
    )
    publication = runtime.publish_snapshot(
        typed,
        scope_id=runtime.default_sync_scope_id(),
        source_scope={"kind": "exclude", "ids": []},
    )
    service = runtime.router.snapshot_files_service
    with (
        patch.object(
            service, "_prepare_snapshot", side_effect=AssertionError("HTTP 禁止打包")
        ),
        pytest.raises(ArtifactError, match="snapshot_not_prepared"),
    ):
        service.open_transfer(
            device_id="device-a",
            session_id="session-a",
            snapshot_id=publication.snapshot_id,
            scope_id=publication.scope_id,
        )


def test_failed_timer_setup_closes_started_runtime(tmp_path):
    from unittest.mock import Mock

    from inkcards_anki_bridge.plugin import start_in_anki
    from tests.test_plugin_runtime import fake_aqt

    server = SimpleNamespace(
        server_address=("127.0.0.1", 39123), start=Mock(), stop=Mock()
    )
    with (
        patch("inkcards_anki_bridge.runtime.DeviceServer", return_value=server),
        patch(
            "inkcards_anki_bridge.plugin._attach_preparation",
            side_effect=RuntimeError("timer setup failed"),
        ),
        pytest.raises(RuntimeError, match="timer setup failed"),
    ):
        start_in_anki(
            aqt_module=fake_aqt(tmp_path / "profile"),
            user_files_root=tmp_path / "addon",
        )
    server.stop.assert_called_once()
