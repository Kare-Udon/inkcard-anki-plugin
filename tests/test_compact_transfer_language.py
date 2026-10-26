"""宿主语言不能改变隔离原生规划的 stock notetype 选择。"""

import tempfile
import unittest
from pathlib import Path

import anki.collection  # 必须先加载 Collection，避免 Anki 循环导入。
import anki.lang
from inkcards_anki_bridge.anki.compact_diagnostics import CompactDiagnostics
from inkcards_anki_bridge.anki.compact_projection import json_sha256
from inkcards_anki_bridge.logging_control import AddonLogger
from test_adopted_restore import adopted_fixture
from test_compact_service import DEVICE, SESSION, SyntheticFixture


class CompactTransferLanguageTests(unittest.TestCase):
    def test_native_projection_english_and_chinese(self):
        original_language = anki.lang.current_lang
        try:
            for language in ("en_US", "zh_CN"):
                with self.subTest(language=language):
                    anki.lang.set_lang("en_US")
                    with tempfile.TemporaryDirectory(
                        prefix="inkcards-native-service-language-", dir="/private/tmp"
                    ) as root:
                        fixture = SyntheticFixture(Path(root))
                        try:
                            source = adopted_fixture(
                                fixture, missing_persisted_lrt=True
                            )
                            fixture.service.import_adopted_source(
                                source, expected_sha256=json_sha256(source)
                            )
                            sink = CompactDiagnostics(
                                fixture.store.root,
                                logger=AddonLogger(
                                    fixture.store.root,
                                    policy={"level": "debug", "console": False},
                                ),
                            )
                            fixture.service.diagnostics = sink
                            anki.lang.set_lang(language)
                            result = fixture.apply_one()
                            sink.record_terminal(result.status, result.reason)
                            self.assertEqual(
                                result.status, "applied", sink.path.read_text()
                            )
                            self.assertIsNotNone(
                                fixture.store.query_compact_applications(
                                    DEVICE, SESSION
                                )[0].intent_id
                            )
                            self.assertEqual(anki.lang.current_lang, language)
                        finally:
                            fixture.close()
        finally:
            anki.lang.set_lang(original_language)
