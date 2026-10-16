from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from inkcards_anki_bridge.rendezvous import RendezvousRecord, RendezvousStore


def record(instance_id: str) -> RendezvousRecord:
    return RendezvousRecord(
        host="127.0.0.1",
        port=43127,
        token=f"token-{instance_id}",
        instance_id=instance_id,
        profile_principal="Profile A",
        anki_version="26.8.1",
        python_version="3.13.14",
        bridge_version="0.1.0-m1",
        created_at_ms=1,
    )


class PausingClearStore(RendezvousStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.unlink_reached = threading.Event()
        self.release_unlink = threading.Event()

    def _unlink_record(self) -> None:
        self.unlink_reached.set()
        if not self.release_unlink.wait(timeout=2.0):
            raise AssertionError("test did not release the clear critical section")
        super()._unlink_record()


class RendezvousConcurrencyTests(unittest.TestCase):
    def test_clear_and_publish_serialize_the_check_replace_unlink_interleaving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rendezvous.json"
            store = PausingClearStore(path)
            store.publish(record("old"))

            clear_errors: list[BaseException] = []
            publish_errors: list[BaseException] = []
            publish_started = threading.Event()
            publish_done = threading.Event()

            def clear_old() -> None:
                try:
                    store.clear_if_instance("old")
                except BaseException as exc:
                    clear_errors.append(exc)

            def publish_new() -> None:
                publish_started.set()
                try:
                    store.publish(record("new"))
                except BaseException as exc:
                    publish_errors.append(exc)
                finally:
                    publish_done.set()

            clear_thread = threading.Thread(target=clear_old, daemon=True)
            clear_thread.start()
            self.assertTrue(store.unlink_reached.wait(timeout=1.0))

            publish_thread = threading.Thread(target=publish_new, daemon=True)
            publish_thread.start()
            self.assertTrue(publish_started.wait(timeout=1.0))
            # clear 持有稳定 lock 时，publish 必须仍在临界区外等待。
            self.assertFalse(publish_done.wait(timeout=0.1))

            store.release_unlink.set()
            clear_thread.join(timeout=2.0)
            publish_thread.join(timeout=2.0)
            self.assertFalse(clear_thread.is_alive())
            self.assertFalse(publish_thread.is_alive())
            self.assertEqual(clear_errors, [])
            self.assertEqual(publish_errors, [])
            self.assertEqual(store.read()["instance_id"], "new")

            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(store.lock_path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
