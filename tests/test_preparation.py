"""设备到来前准备、短轮询、旧结果失效和跨进程恢复。"""

import dataclasses
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from inkcards_anki_bridge.anki.operations import OperationResult, OperationState
from inkcards_anki_bridge.anki.preparation import PreparedSync
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
