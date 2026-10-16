"""许可寿命调查：仅临时 Store 与合成时钟，不访问活动 Collection。"""

import unittest

import test_snapshots as snapshot_fixtures
from inkcards_anki_bridge.services.snapshots import SnapshotError, SnapshotService


class PermitLifetimeInvestigationTests(unittest.TestCase):
    # 仅复用隔离 fixture，不继承原测试集。
    setUp = snapshot_fixtures.SnapshotServiceTests.setUp
    tearDown = snapshot_fixtures.SnapshotServiceTests.tearDown

    def test_absolute_expiry_survives_resume_and_recovery(self):
        now = [1000]
        service = SnapshotService(
            self.store, permit_ttl_ms=300_000, clock=lambda: now[0]
        )
        service.publish(snapshot_fixtures.make_snapshot(), scope_id="scope-a")
        context = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "scope-a",
            "local_generation": 0,
        }
        opened = service.open_pull(**context, recovery_key=b"s" * 32)
        self.assertEqual(opened["expires_at_ms"], 301000)
        now[0] = 101000
        resumed = service.open_pull(
            **context, permit_id=opened["permit_id"], nonce=opened["nonce"]
        )
        self.assertEqual(resumed["expires_at_ms"], 301000)
        now[0] = 201000
        recovered = service.open_pull(**context, recovery_key=b"s" * 32)
        self.assertEqual(recovered["permit_id"], opened["permit_id"])
        self.assertEqual(recovered["expires_at_ms"], 301000)

    def test_page_ack_does_not_extend_commit_deadline(self):
        now = [1000]
        service = SnapshotService(
            self.store, permit_ttl_ms=300_000, clock=lambda: now[0]
        )
        service.publish(snapshot_fixtures.make_snapshot(), scope_id="scope-a")
        context = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "scope-a",
            "local_generation": 0,
        }
        opened = service.open_pull(**context)
        credentials = {"permit_id": opened["permit_id"], "nonce": opened["nonce"]}
        now[0] = 300999
        page = service.page(**context, **credentials, page_index=0, cursor=None)
        self.assertTrue(page["complete"])
        self.assertEqual(
            self.store.get_permit(opened["permit_id"]).expires_at_ms, 301000
        )
        now[0] = 301000
        with self.assertRaises(SnapshotError) as caught:
            service.commit(**context, **credentials)
        self.assertEqual(caught.exception.code, "permit_expired")
        with self.assertRaises(SnapshotError) as caught:
            service.page(**context, **credentials, page_index=0, cursor=None)
        self.assertEqual(caught.exception.code, "permit_expired")

    def test_active_lease_still_requires_nonce_and_snapshot_generation(self):
        now = [1000]
        service = SnapshotService(
            self.store, permit_ttl_ms=300_000, clock=lambda: now[0]
        )
        service.publish(snapshot_fixtures.make_snapshot(), scope_id="scope-a")
        context = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "scope-a",
            "local_generation": 0,
        }
        opened = service.open_pull(**context)
        with self.assertRaises(SnapshotError) as caught:
            service.page(
                **context,
                permit_id=opened["permit_id"],
                nonce="bad-nonce",
                page_index=0,
                cursor=None,
            )
        self.assertEqual(caught.exception.code, "permit_invalid")
        with self.assertRaises(SnapshotError) as caught:
            service.page(
                **context,
                permit_id=opened["permit_id"],
                nonce=opened["nonce"],
                snapshot_generation=999,
                page_index=0,
                cursor=None,
            )
        self.assertEqual(caught.exception.code, "generation_mismatch")
        self.assertEqual(
            self.store.get_pull_progress(opened["permit_id"]).received_pages, 0
        )

    def test_expired_resume_rejected_and_fresh_open_restarts_progress(self):
        now = [1000]
        service = SnapshotService(
            self.store, permit_ttl_ms=300_000, clock=lambda: now[0]
        )
        service.publish(snapshot_fixtures.make_snapshot(), scope_id="scope-a")
        context = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "scope-a",
            "local_generation": 0,
        }
        opened = service.open_pull(**context, recovery_key=b"s" * 32)
        credentials = {"permit_id": opened["permit_id"], "nonce": opened["nonce"]}
        service.page(**context, **credentials, page_index=0, cursor=None)
        now[0] = 301000
        with self.assertRaises(SnapshotError) as caught:
            service.open_pull(**context, **credentials, recovery_key=b"s" * 32)
        self.assertEqual(caught.exception.code, "permit_invalid")
        fresh = service.open_pull(**context, recovery_key=b"s" * 32)
        self.assertNotEqual(fresh["permit_id"], opened["permit_id"])
        self.assertEqual(fresh["expires_at_ms"], 601000)
        self.assertEqual(
            self.store.get_pull_progress(fresh["permit_id"]).received_pages, 0
        )


class DefaultPermitLifetimeTests(unittest.TestCase):
    """默认固定 30 分钟窗口；恢复及已签发许可不自动续期。"""

    setUp = snapshot_fixtures.SnapshotServiceTests.setUp
    tearDown = snapshot_fixtures.SnapshotServiceTests.tearDown

    def fixture(self, **options):
        self.now = [1000]
        self.service = SnapshotService(self.store, clock=lambda: self.now[0], **options)
        self.service.publish(snapshot_fixtures.make_snapshot(), scope_id="scope-a")
        self.context = {
            "device_id": "device-a",
            "session_id": "session-a",
            "scope_id": "scope-a",
            "local_generation": 0,
        }
        self.opened = self.service.open_pull(**self.context, recovery_key=b"s" * 32)
        self.credentials = {
            "permit_id": self.opened["permit_id"],
            "nonce": self.opened["nonce"],
        }

    def test_default_page_and_commit_succeed_after_five_minutes(self):
        self.fixture()
        self.now[0] += 300_001
        self.assertTrue(
            self.service.page(
                **self.context, **self.credentials, page_index=0, cursor=None
            )["complete"]
        )
        self.assertTrue(
            self.service.commit(**self.context, **self.credentials)["committed"]
        )
        with self.assertRaises(SnapshotError) as caught:
            self.service.commit(**self.context, **self.credentials)
        self.assertEqual(caught.exception.code, "permit_already_used")

    def test_default_thirty_minute_boundary_rejects_page_and_commit(self):
        self.fixture()
        self.assertEqual(self.opened["expires_at_ms"], 1_801_000)
        self.now[0] += 1_799_999
        self.assertTrue(
            self.service.page(
                **self.context, **self.credentials, page_index=0, cursor=None
            )["complete"]
        )
        self.now[0] += 1
        for operation, extra in (
            (self.service.page, {"page_index": 0, "cursor": None}),
            (self.service.commit, {}),
        ):
            with self.assertRaises(SnapshotError) as caught:
                operation(**self.context, **self.credentials, **extra)
            self.assertEqual(caught.exception.code, "permit_expired")

    def test_default_resume_and_recovery_preserve_original_expiry(self):
        self.fixture()
        self.now[0] += 300_001
        resumed = self.service.open_pull(**self.context, **self.credentials)
        recovered = self.service.open_pull(**self.context, recovery_key=b"s" * 32)
        for result in (resumed, recovered):
            self.assertEqual(result["permit_id"], self.opened["permit_id"])
            self.assertEqual(result["expires_at_ms"], 1_801_000)

    def test_reload_does_not_extend_existing_short_permit(self):
        self.fixture(permit_ttl_ms=100)
        self.service = SnapshotService(self.store, clock=lambda: self.now[0])
        self.now[0] += 99
        resumed = self.service.open_pull(**self.context, **self.credentials)
        self.assertEqual(resumed["expires_at_ms"], 1100)
        self.now[0] += 1
        with self.assertRaises(SnapshotError) as caught:
            self.service.page(
                **self.context, **self.credentials, page_index=0, cursor=None
            )
        self.assertEqual(caught.exception.code, "permit_expired")

    def test_long_default_window_preserves_context_guards(self):
        self.fixture()
        self.now[0] += 300_001
        for changes, code in (
            ({"nonce": "wrong"}, "permit_invalid"),
            ({"device_id": "other-device"}, "identity_mismatch"),
            ({"snapshot_generation": 999}, "generation_mismatch"),
        ):
            request = dict(self.context, **self.credentials, page_index=0, cursor=None)
            request.update(changes)
            with self.assertRaises(SnapshotError) as caught:
                self.service.page(**request)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(SnapshotError) as caught:
            self.service.open_pull(
                **self.context, **self.credentials, applied_watermark={"known": 1}
            )
        self.assertEqual(caught.exception.code, "applied_watermark_mismatch")
        self.assertEqual(
            self.store.get_pull_progress(self.opened["permit_id"]).received_pages, 0
        )
