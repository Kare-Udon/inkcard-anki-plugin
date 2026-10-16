from __future__ import annotations

import threading
import time
import unittest

from inkcards_anki_bridge.dispatcher import MainThreadDispatcher


class DispatcherTests(unittest.TestCase):
    def test_main_thread_dispatcher_requires_explicit_schedule(self) -> None:
        pending = []
        dispatcher = MainThreadDispatcher(lambda closure: pending.append(closure))
        result: list[str] = []

        worker = threading.Thread(
            target=lambda: result.append(dispatcher.submit(lambda: "scheduled", 1.0)),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while not pending and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(len(pending), 1)
        pending.pop(0)()
        worker.join(timeout=1.0)
        self.assertEqual(result, ["scheduled"])


if __name__ == "__main__":
    unittest.main()
