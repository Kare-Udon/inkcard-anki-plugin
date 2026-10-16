from __future__ import annotations

import unittest
from inkcards_anki_bridge.runtime import BridgeRuntime


class BridgeRuntimeTests(unittest.TestCase):
    """旧模块名只验证为新 add-on runtime 的兼容别名。"""

    def test_legacy_name_does_not_restore_old_v1_bridge(self) -> None:
        from inkcards_anki_bridge.runtime import AddonRuntime

        self.assertIs(BridgeRuntime, AddonRuntime)


if __name__ == "__main__":
    unittest.main()
