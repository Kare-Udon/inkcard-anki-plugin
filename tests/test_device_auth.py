from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from inkcards_anki_bridge.device.auth import (
    AuthError,
    DeviceAuthenticator,
)
from inkcards_anki_bridge.storage import ProfileStore


class FakeClock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeEntropy:
    def __init__(self) -> None:
        self.counter = 0

    def randbelow(self, upper: int) -> int:
        self.counter += 1
        return 123456 % upper

    def token_bytes(self, length: int) -> bytes:
        self.counter += 1
        return bytes(((self.counter + index) % 256 for index in range(length)))


class DeviceAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.entropy = FakeEntropy()
        self.store = ProfileStore(Path(self.tempdir.name), profile_id="profile-a")
        self.auth = DeviceAuthenticator(
            self.store,
            clock=self.clock,
            secrets_source=self.entropy,
            pairing_ttl_ms=100,
            max_pairing_attempts=3,
            pairing_attempt_window_ms=50,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_pairing_uses_one_time_six_digit_code_and_persists_only_token_hash(self) -> None:
        code = self.auth.issue_pairing_code()
        self.assertRegex(code, r"^\d{6}$")
        credential = self.auth.pair(code, "device-a")
        self.assertEqual(len(credential.token), 64)
        self.assertNotIn(credential.token, repr(credential))
        self.assertNotIn(credential.token, repr(self.auth))

        with sqlite3.connect(str(self.store.db_path)) as db:
            row = db.execute(
                "SELECT token_hash FROM devices WHERE profile_id=? AND device_id=?",
                ("profile-a", "device-a"),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], hashlib.sha256(credential.token.encode()).hexdigest())
        self.assertNotEqual(row[0], credential.token)

        with self.assertRaisesRegex(AuthError, "invalid_or_expired_pairing_code"):
            self.auth.pair(code, "device-b")

    def test_expiry_wrong_attempts_and_rate_limit_are_bounded(self) -> None:
        code = self.auth.issue_pairing_code()
        self.clock.now += 101
        with self.assertRaisesRegex(AuthError, "invalid_or_expired_pairing_code"):
            self.auth.pair(code, "device-a")

        self.clock.now = 2_000_000
        for _ in range(3):
            with self.assertRaisesRegex(AuthError, "invalid_or_expired_pairing_code"):
                self.auth.pair("000000", "device-a")
        with self.assertRaisesRegex(AuthError, "pairing_rate_limited"):
            self.auth.pair("000000", "device-a")
        self.clock.now += 51
        code = self.auth.issue_pairing_code()
        self.assertEqual(self.auth.pair(code, "device-a").device_id, "device-a")

    def test_wrong_revoked_cross_profile_and_device_mismatch_are_not_accepted(self) -> None:
        code = self.auth.issue_pairing_code()
        credential = self.auth.pair(code, "device-a")
        with self.assertRaisesRegex(AuthError, "invalid_token"):
            self.auth.authenticate("device-a", "wrong-token")
        principal = self.auth.authenticate("device-a", credential.token)
        self.assertEqual((principal.profile_id, principal.device_id), ("profile-a", "device-a"))

        with self.assertRaisesRegex(AuthError, "invalid_token"):
            self.auth.authenticate("device-b", credential.token)
        self.store.revoke_device("device-a", now_ms=self.clock.now)
        with self.assertRaisesRegex(AuthError, "invalid_token"):
            self.auth.authenticate("device-a", credential.token)

        other_dir = tempfile.TemporaryDirectory()
        try:
            other_store = ProfileStore(Path(other_dir.name), profile_id="profile-b")
            other_auth = DeviceAuthenticator(other_store, clock=self.clock, secrets_source=self.entropy)
            with self.assertRaisesRegex(AuthError, "invalid_token"):
                other_auth.authenticate("device-a", credential.token)
            other_store.close()
        finally:
            other_dir.cleanup()

    def test_runtime_session_does_not_return_or_store_long_term_token(self) -> None:
        code = self.auth.issue_pairing_code()
        credential = self.auth.pair(code, "device-a")
        principal = self.auth.authenticate("device-a", credential.token)
        session = self.auth.open_session(principal, "session-a")
        self.assertEqual(session.session_id, "session-a")
        self.assertNotIn(credential.token, repr(session))
        self.assertNotIn(credential.token, repr(self.auth.runtime_sessions()))


if __name__ == "__main__":
    unittest.main()
