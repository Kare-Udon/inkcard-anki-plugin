"""固定码与持久配对事务的回归验收。"""
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from inkcards_anki_bridge.device.auth import AuthError, DeviceAuthenticator
from inkcards_anki_bridge.storage import ProfileStore, CommitUnknownError


class DeviceManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ProfileStore(self.root, profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def attempt(self, code, device="reader", token=None):
        return dict(code=code, device_id=device, pairing_id=str(uuid.uuid4()), token=token or uuid.uuid4().hex * 2)

    def bind(self, slot, device="reader"):
        attempt = self.attempt(slot["code"], device)
        self.auth.prepare_pairing(**attempt)
        self.auth.confirm_pairing(device, attempt["pairing_id"], attempt["token"])
        return attempt

    def test_same_device_rebind_preserves_session_and_old_token_until_confirm(self):
        slot = self.auth.add_device("阅读器")
        old = self.bind(slot)
        session = self.store.open_session("reader", "session-a")
        with self.store._transaction() as db:
            db.execute("UPDATE sessions SET high_water=7 WHERE device_id='reader'")
        self.auth.allow_rebind(slot["slot_id"])
        new = self.attempt(slot["code"])
        self.auth.prepare_pairing(**new)
        self.auth.authenticate("reader", old["token"])
        self.auth.confirm_pairing("reader", new["pairing_id"], new["token"])
        with self.assertRaises(AuthError):
            self.auth.authenticate("reader", old["token"])
        self.auth.authenticate("reader", new["token"])
        self.assertEqual(self.store.open_session("reader", session.session_id).high_water, 7)

    def test_fixed_code_and_idempotent_pending_and_confirm_survive_restart(self):
        slot = self.auth.add_device("阅读器")
        self.assertRegex(slot["code"], r"^[0-9]{12}$")
        attempt = self.attempt(slot["code"])
        first = self.auth.prepare_pairing(**attempt)
        self.store.close()
        self.store = ProfileStore(self.root, profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store, clock=lambda: 10**14)
        self.assertEqual(first, self.auth.prepare_pairing(**attempt))
        active = self.auth.confirm_pairing("reader", attempt["pairing_id"], attempt["token"])
        self.assertEqual(active, self.auth.confirm_pairing("reader", attempt["pairing_id"], attempt["token"]))
        self.assertEqual(self.auth.prepare_pairing(**attempt)["status"], "active")
        self.assertNotIn("code", self.auth.list_devices()[0])
        self.assertNotIn("token", str(active))
        with closing(sqlite3.connect(self.store.db_path)) as db:
            with db:
                dump = "\n".join(db.iterdump())
        self.assertNotIn(slot["code"], dump)
        self.assertNotIn(attempt["token"], dump)

    def test_occupancy_identity_rebind_reset_remove(self):
        slot = self.auth.add_device("A")
        a = self.attempt(slot["code"])
        self.auth.prepare_pairing(**a)
        with self.assertRaisesRegex(AuthError, "pairing_in_progress"):
            self.auth.prepare_pairing(**self.attempt(slot["code"], "other"))
        self.auth.confirm_pairing("reader", a["pairing_id"], a["token"])
        self.store.open_session("reader", "old")
        with self.assertRaisesRegex(AuthError, "rebind_required|device_already_bound"):
            self.auth.prepare_pairing(**self.attempt(slot["code"], "other"))
        second = self.auth.add_device("B")
        with self.assertRaisesRegex(AuthError, "device_identity_conflict"):
            self.auth.prepare_pairing(**self.attempt(second["code"]))
        reset = self.auth.reset_pairing_code(slot["slot_id"])
        self.auth.authenticate("reader", a["token"])
        with self.assertRaisesRegex(AuthError, "rebind_required|device_already_bound"):
            self.auth.prepare_pairing(**self.attempt(reset["code"], "other"))
        self.auth.allow_rebind(slot["slot_id"])
        b = self.bind(reset, "other")
        with self.assertRaises(AuthError):
            self.auth.authenticate("reader", a["token"])
        self.assertIsNotNone(self.store._read(lambda db: db.execute("SELECT closed_at_ms FROM sessions WHERE device_id='reader'").fetchone()[0]))
        self.auth.remove_device(slot["slot_id"])
        self.auth.remove_device(slot["slot_id"])
        with self.assertRaises(AuthError):
            self.auth.authenticate("other", b["token"])
        self.assertEqual(len(self.auth.list_devices()), 1)
        self.assertEqual(len(self.auth.list_devices(include_revoked=True)), 2)

    def test_failed_confirm_and_reset_roll_back_without_invalidating_active(self):
        slot = self.auth.add_device("A")
        old = self.bind(slot)
        self.auth.allow_rebind(slot["slot_id"])
        new = self.attempt(slot["code"])
        self.auth.prepare_pairing(**new)
        with patch.object(self.store, "_commit", side_effect=OSError("private")):
            with self.assertRaises(CommitUnknownError):
                self.auth.confirm_pairing("reader", new["pairing_id"], new["token"])
        self.auth.authenticate("reader", old["token"])
        self.auth.confirm_pairing("reader", new["pairing_id"], new["token"])

    def test_concurrent_prepare_has_one_winner(self):
        slot = self.auth.add_device("A")
        def run(attempt):
            try:
                return DeviceAuthenticator(self.store).prepare_pairing(**attempt)["status"]
            except AuthError as exc:
                return exc.code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, [self.attempt(slot["code"]), self.attempt(slot["code"])]))
        self.assertCountEqual(results, ["pending", "pairing_in_progress"])

    def test_prepare_is_not_authorization_and_mismatch_cannot_confirm(self):
        slot = self.auth.add_device("A")
        pending = self.attempt(slot["code"])
        self.auth.prepare_pairing(**pending)
        with self.assertRaisesRegex(AuthError, "invalid_token"):
            self.auth.authenticate("reader", pending["token"])
        for device, pairing_id, token in [
            ("other", pending["pairing_id"], pending["token"]),
            ("reader", str(uuid.uuid4()), pending["token"]),
            ("reader", pending["pairing_id"], "a" * 64),
        ]:
            with self.assertRaisesRegex(AuthError, "pairing_transaction_mismatch"):
                self.auth.confirm_pairing(device, pairing_id, token)
        changed = dict(pending, token="b" * 64)
        with self.assertRaisesRegex(AuthError, "pairing_transaction_mismatch"):
            self.auth.prepare_pairing(**changed)
        self.auth.confirm_pairing("reader", pending["pairing_id"], pending["token"])

    def test_reset_cancels_pending_without_allowing_rebind_or_revoking_active(self):
        slot = self.auth.add_device("A")
        old = self.bind(slot)
        self.auth.allow_rebind(slot["slot_id"])
        pending = self.attempt(slot["code"])
        self.auth.prepare_pairing(**pending)
        reset = self.auth.reset_pairing_code(slot["slot_id"])
        with self.assertRaisesRegex(AuthError, "invalid_pairing_code"):
            self.auth.prepare_pairing(**pending)
        with self.assertRaisesRegex(AuthError, "pairing_transaction_mismatch"):
            self.auth.confirm_pairing("reader", pending["pairing_id"], pending["token"])
        self.auth.authenticate("reader", old["token"])
        fresh = self.attempt(reset["code"])
        self.auth.prepare_pairing(**fresh)
        self.auth.remove_device(slot["slot_id"])
        with self.assertRaises(AuthError):
            self.auth.confirm_pairing("reader", fresh["pairing_id"], fresh["token"])
        with self.assertRaises(AuthError):
            self.auth.authenticate("reader", old["token"])

    def test_prepare_and_management_write_failures_are_recoverable(self):
        slot = self.auth.add_device("A")
        pending = self.attempt(slot["code"])
        for action in [
            lambda: self.auth.prepare_pairing(**pending),
            lambda: self.auth.reset_pairing_code(slot["slot_id"]),
            lambda: self.auth.remove_device(slot["slot_id"]),
            lambda: self.auth.rename_device(slot["slot_id"], "B"),
            lambda: self.auth.add_device("B"),
        ]:
            with patch.object(self.store, "_commit", side_effect=OSError("SECRET")):
                with self.assertRaises(CommitUnknownError):
                    action()
            self.assertEqual(self.auth.list_devices()[0]["name"], "A")
            self.assertEqual(len(self.auth.list_devices()), 1)
        self.bind(slot)
        with patch.object(self.store, "_commit", side_effect=OSError("SECRET")):
            with self.assertRaises(CommitUnknownError):
                self.auth.allow_rebind(slot["slot_id"])
        self.assertEqual(self.auth.list_devices()[0]["status"], "bound")

    def test_commit_completed_but_response_lost_can_be_retried(self):
        slot = self.auth.add_device("A")
        attempt = self.attempt(slot["code"])
        original = self.store._commit
        def commit_then_fail(db):
            original(db)
            raise OSError("模拟提交响应丢失")
        with patch.object(self.store, "_commit", side_effect=commit_then_fail):
            with self.assertRaises(CommitUnknownError):
                self.auth.prepare_pairing(**attempt)
        self.assertEqual(self.auth.prepare_pairing(**attempt)["status"], "pending")
        with patch.object(self.store, "_commit", side_effect=commit_then_fail):
            with self.assertRaises(CommitUnknownError):
                self.auth.confirm_pairing("reader", attempt["pairing_id"], attempt["token"])
        self.assertEqual(self.auth.prepare_pairing(**attempt)["status"], "active")
        self.assertEqual(self.auth.confirm_pairing("reader", attempt["pairing_id"], attempt["token"])["status"], "active")

    def test_v7_migration_preserves_credentials_sessions_receipts_and_history(self):
        from inkcards_anki_bridge.domain.sync_gate import ImmutableReviewEvent
        self.store.register_device("legacy", "legacy-token")
        self.store.open_session("legacy", "history")
        self.store.receive_batch("legacy", "history", [ImmutableReviewEvent.create("history:1", 1, {"card_id": "c", "rating": 3})])
        self.store.register_device("removed", "revoked-token")
        self.store.revoke_device("removed")
        receipts = self.store.query_receipts("legacy", "history")
        with self.store._transaction() as db:
            db.execute("DROP TABLE pairing_transactions")
            db.execute("DROP TABLE device_slots")
            db.execute("DELETE FROM schema_migrations WHERE version>=8")
            db.execute("PRAGMA user_version=7")
        self.store.close()
        self.store = ProfileStore(self.root, profile_id="profile-a")
        self.auth = DeviceAuthenticator(self.store)
        slots = {item["device_id"]: item for item in self.auth.list_devices(include_revoked=True)}
        self.assertEqual(slots["legacy"]["status"], "bound")
        self.assertEqual(slots["removed"]["status"], "revoked")
        self.assertEqual(self.store.query_receipts("legacy", "history"), receipts)
        self.auth.authenticate("legacy", "legacy-token")
        self.assertEqual(self.store.open_session("legacy", "history").high_water, 1)
        self.assertIsNone(self.store._read(lambda db: db.execute("SELECT code_hash FROM device_slots WHERE device_id='legacy'").fetchone()[0]))
        reset = self.auth.reset_pairing_code(slots["legacy"]["slot_id"])
        with self.assertRaisesRegex(AuthError, "rebind_required"):
            self.auth.prepare_pairing(**self.attempt(reset["code"], "legacy"))
        self.auth.allow_rebind(slots["legacy"]["slot_id"])
        self.bind(reset, "legacy")
        self.assertEqual(self.store.query_receipts("legacy", "history"), receipts)
        self.auth.remove_device(slots["legacy"]["slot_id"])
        self.assertEqual(self.store._read(lambda db: db.execute("SELECT COUNT(*) FROM deliveries WHERE device_id='legacy'").fetchone()[0]), 1)
