"""设备名额与两阶段授权事务；所有切换只在一个 SQLite 事务内完成。"""
from __future__ import annotations

from .errors import StorageValidationError


class PairingStateError(StorageValidationError):
    """安全、稳定的设备管理状态错误。"""

    def __init__(self, code: str):
        super().__init__(code, code)


class DeviceManagementStore:
    """由 ProfileStore 复用其 profile、连接和提交故障边界。"""

    def add_device_slot(self, slot_id, name, code_hash, now):
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM device_slots WHERE profile_id=? AND code_hash=?", (self.profile_id, code_hash)).fetchone():
                raise PairingStateError("pairing_code_collision")
            db.execute("INSERT INTO device_slots(profile_id,slot_id,name,status,code_hash,created_at_ms) VALUES(?,?,?,'pending',?,?)", (self.profile_id, slot_id, name, code_hash, now))

    def list_device_slots(self, include_revoked=False):
        def query(db):
            rows = db.execute(
                "SELECT s.slot_id,s.name,s.status,s.device_id,d.last_seen_at_ms,s.created_at_ms "
                "FROM device_slots s LEFT JOIN devices d ON d.profile_id=s.profile_id AND d.device_id=s.device_id "
                "WHERE s.profile_id=? AND (? OR s.status!='revoked') ORDER BY s.created_at_ms,s.slot_id",
                (self.profile_id, bool(include_revoked)),
            ).fetchall()
            return [dict(row) for row in rows]
        return self._read(query)

    def _device_slot(self, db, slot_id):
        row = db.execute("SELECT * FROM device_slots WHERE profile_id=? AND slot_id=?", (self.profile_id, slot_id)).fetchone()
        if row is None:
            raise PairingStateError("unknown_device_slot")
        return row

    def manage_device_slot(self, slot_id, action, *, now, name=None, code_hash=None):
        with self._transaction() as db:
            slot = self._device_slot(db, slot_id)
            if action == "remove" and slot["status"] == "revoked":
                return
            if slot["status"] == "revoked":
                raise PairingStateError("device_revoked")
            if action == "rename":
                db.execute("UPDATE device_slots SET name=? WHERE profile_id=? AND slot_id=?", (name, self.profile_id, slot_id))
                return
            if action == "reset":
                if db.execute("SELECT 1 FROM device_slots WHERE profile_id=? AND code_hash=?", (self.profile_id, code_hash)).fetchone():
                    raise PairingStateError("pairing_code_collision")
                db.execute("UPDATE device_slots SET code_hash=? WHERE profile_id=? AND slot_id=?", (code_hash, self.profile_id, slot_id))
            elif action == "allow_rebind":
                db.execute("UPDATE device_slots SET status=? WHERE profile_id=? AND slot_id=?", ("rebind_ready" if slot["device_id"] else "pending", self.profile_id, slot_id))
            elif action == "remove":
                db.execute("UPDATE device_slots SET status='revoked',code_hash=NULL WHERE profile_id=? AND slot_id=?", (self.profile_id, slot_id))
                self._revoke_slot_identity(db, slot["device_id"], now)
            else:
                raise ValueError("unknown device management action")
            db.execute("DELETE FROM pairing_transactions WHERE profile_id=? AND slot_id=?", (self.profile_id, slot_id))
            return slot["device_id"] if action == "remove" else None

    def _revoke_slot_identity(self, db, device_id, now):
        if device_id is not None:
            db.execute("UPDATE devices SET revoked_at_ms=? WHERE profile_id=? AND device_id=?", (now, self.profile_id, device_id))
            db.execute("UPDATE sessions SET closed_at_ms=? WHERE profile_id=? AND device_id=? AND closed_at_ms IS NULL", (now, self.profile_id, device_id))

    def prepare_device_pairing(self, code_hash, device_id, pairing_id, token_hash, now):
        with self._transaction() as db:
            slot = db.execute("SELECT * FROM device_slots WHERE profile_id=? AND code_hash=?", (self.profile_id, code_hash)).fetchone()
            if slot is None:
                raise PairingStateError("invalid_pairing_code")
            if slot["status"] == "revoked":
                raise PairingStateError("device_revoked")
            existing = db.execute("SELECT * FROM pairing_transactions WHERE profile_id=? AND pairing_id=?", (self.profile_id, pairing_id)).fetchone()
            if existing is not None:
                if (existing["slot_id"], existing["device_id"], existing["token_hash"]) != (slot["slot_id"], device_id, token_hash):
                    raise PairingStateError("pairing_transaction_mismatch")
                if existing["status"] == "active":
                    active = db.execute("SELECT 1 FROM devices WHERE profile_id=? AND device_id=? AND token_hash=? AND revoked_at_ms IS NULL", (self.profile_id, device_id, token_hash)).fetchone()
                    if active is None:
                        raise PairingStateError("device_revoked")
                return existing["status"]
            occupying = db.execute("SELECT status FROM pairing_transactions WHERE profile_id=? AND slot_id=?", (self.profile_id, slot["slot_id"])).fetchone()
            if occupying is not None and occupying["status"] == "pending":
                raise PairingStateError("pairing_in_progress")
            if slot["status"] == "bound":
                raise PairingStateError("rebind_required" if device_id == slot["device_id"] else "device_already_bound")
            other = db.execute("SELECT 1 FROM device_slots WHERE profile_id=? AND device_id=? AND slot_id!=? AND status!='revoked'", (self.profile_id, device_id, slot["slot_id"])).fetchone()
            pending_identity = db.execute("SELECT 1 FROM pairing_transactions WHERE profile_id=? AND device_id=? AND slot_id!=?", (self.profile_id, device_id, slot["slot_id"])).fetchone()
            legacy = db.execute("SELECT 1 FROM devices WHERE profile_id=? AND device_id=? AND revoked_at_ms IS NULL", (self.profile_id, device_id)).fetchone()
            if other or pending_identity or (legacy and slot["device_id"] != device_id):
                raise PairingStateError("device_identity_conflict")
            reused_token = db.execute("SELECT 1 FROM devices WHERE profile_id=? AND token_hash=? UNION ALL SELECT 1 FROM pairing_transactions WHERE profile_id=? AND token_hash=?", (self.profile_id, token_hash, self.profile_id, token_hash)).fetchone()
            if reused_token:
                raise PairingStateError("pairing_transaction_mismatch")
            db.execute("INSERT INTO pairing_transactions(profile_id,pairing_id,slot_id,device_id,token_hash,status,created_at_ms) VALUES(?,?,?,?,?,'pending',?)", (self.profile_id, pairing_id, slot["slot_id"], device_id, token_hash, now))
            return "pending"

    def confirm_device_pairing(self, device_id, pairing_id, token_hash, now):
        with self._transaction() as db:
            attempt = db.execute("SELECT * FROM pairing_transactions WHERE profile_id=? AND pairing_id=?", (self.profile_id, pairing_id)).fetchone()
            if attempt is None or (attempt["device_id"], attempt["token_hash"]) != (device_id, token_hash):
                raise PairingStateError("pairing_transaction_mismatch")
            slot = self._device_slot(db, attempt["slot_id"])
            if slot["status"] == "revoked":
                raise PairingStateError("device_revoked")
            if attempt["status"] == "active":
                active = db.execute("SELECT 1 FROM devices WHERE profile_id=? AND device_id=? AND token_hash=? AND revoked_at_ms IS NULL", (self.profile_id, device_id, token_hash)).fetchone()
                if active is None:
                    raise PairingStateError("device_revoked")
                return
            # 事务内重新核验身份，避免另一名额在 prepare 后先确认相同身份。
            other = db.execute("SELECT 1 FROM device_slots WHERE profile_id=? AND device_id=? AND slot_id!=? AND status!='revoked'", (self.profile_id, device_id, slot["slot_id"])).fetchone()
            if other:
                raise PairingStateError("device_identity_conflict")
            if slot["device_id"] and slot["device_id"] != device_id:
                self._revoke_slot_identity(db, slot["device_id"], now)
            db.execute(
                "INSERT INTO devices(profile_id,device_id,token_hash,created_at_ms,last_seen_at_ms) VALUES(?,?,?,?,?) "
                "ON CONFLICT(profile_id,device_id) DO UPDATE SET token_hash=excluded.token_hash,last_seen_at_ms=excluded.last_seen_at_ms,revoked_at_ms=NULL",
                (self.profile_id, device_id, token_hash, now, now),
            )
            db.execute("UPDATE device_slots SET device_id=?,status='bound' WHERE profile_id=? AND slot_id=?", (device_id, self.profile_id, slot["slot_id"]))
            db.execute("UPDATE pairing_transactions SET status='active' WHERE profile_id=? AND pairing_id=?", (self.profile_id, pairing_id))
            return slot["device_id"] if slot["device_id"] != device_id else None
