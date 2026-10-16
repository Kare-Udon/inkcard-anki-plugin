from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock

from inkcards_anki_bridge.runtime import AddonRuntime


class CompactRuntimeGenerationTests(unittest.TestCase):
    def make_runtime(self, *, cached_generation: int, store_generation: int) -> AddonRuntime:
        runtime = AddonRuntime.__new__(AddonRuntime)
        runtime._lock = threading.RLock()
        runtime._state = "running"
        runtime._collection_generation = cached_generation
        runtime._store = Mock()
        runtime._store.get_collection_generation.return_value = store_generation
        return runtime

    def test_external_generation_update_persists_store_then_updates_cache(self) -> None:
        runtime = self.make_runtime(cached_generation=7, store_generation=7)

        runtime.update_collection_generation(8)

        self.assertEqual(runtime.collection_generation, 8)
        runtime.store.set_collection_generation.assert_called_once_with(8)

    def test_external_native_change_invalidates_generation_without_rewriting_receipts(self) -> None:
        runtime = self.make_runtime(cached_generation=7, store_generation=8)
        self.assertEqual(runtime.note_external_collection_change(), 9)
        self.assertEqual(runtime.collection_generation, 9)
        runtime.store.set_collection_generation.assert_called_once_with(9)

    def test_compact_capability_requires_a_live_supported_host(self) -> None:
        runtime = self.make_runtime(cached_generation=0, store_generation=0)
        runtime._compact_jobs = None
        runtime.set_compact_capability({'write_enabled': True})
        self.assertFalse(runtime.compact_capabilities()['apply_enabled'])
        runtime._compact_jobs = object()
        self.assertTrue(runtime.compact_capabilities()['apply_enabled'])
        self.assertEqual(runtime.compact_capabilities()['ratings'], [1, 2, 3, 4])
        runtime.set_compact_capability({'write_enabled': False})
        self.assertFalse(runtime.compact_capabilities()['apply_enabled'])

    def test_compact_result_generation_refreshes_cache_without_second_store_write(self) -> None:
        runtime = self.make_runtime(cached_generation=7, store_generation=8)

        runtime.refresh_committed_collection_generation(8)

        self.assertEqual(runtime.collection_generation, 8)
        runtime.store.set_collection_generation.assert_not_called()

    def test_generation_refresh_fails_closed_on_unexpected_store_value(self) -> None:
        runtime = self.make_runtime(cached_generation=7, store_generation=9)

        with self.assertRaisesRegex(RuntimeError, "Store generation changed"):
            runtime.refresh_committed_collection_generation(8)

        self.assertEqual(runtime.collection_generation, 7)
        runtime.store.set_collection_generation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
