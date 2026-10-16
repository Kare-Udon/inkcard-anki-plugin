from __future__ import annotations

import json
import unittest

from inkcards_anki_bridge.protocol import PROTOCOL_VERSION, ProtocolError, parse_request


def request_bytes(request_id: str = "request-1", params: str = "{}") -> bytes:
    return (
        '{"protocol_version":"'
        + PROTOCOL_VERSION
        + '","request_id":'
        + json.dumps(request_id, ensure_ascii=False)
        + ',"method":"hello","params":'
        + params
        + "}"
    ).encode("utf-8")


class ProtocolStrictnessTests(unittest.TestCase):
    def test_request_id_rejects_all_unicode_cc_controls(self) -> None:
        # C0、DEL、C1 和 NEL 都属于 Unicode General_Category=Cc。
        for control in ("\x00", "\x1f", "\x7f", "\x80", "\x85", "\x9f"):
            with self.subTest(codepoint=f"U+{ord(control):04X}"):
                with self.assertRaises(ProtocolError) as context:
                    parse_request(request_bytes(request_id=f"id-{control}"), "hello")
                self.assertEqual(context.exception.code, "invalid_request_id")

    def test_request_id_allows_non_control_unicode(self) -> None:
        request = parse_request(request_bytes(request_id="卡片-🙂"), "hello")
        self.assertEqual(request.request_id, "卡片-🙂")

    def test_non_finite_json_constants_are_rejected_at_any_nesting_level(self) -> None:
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                with self.assertRaises(ProtocolError) as context:
                    parse_request(request_bytes(params=f'{{"nested":[{{"value":{literal}}}]}}'), "hello")
                self.assertEqual(context.exception.code, "malformed_json")

    def test_duplicate_keys_are_rejected_at_top_level_and_nested_level(self) -> None:
        duplicate_top_level = (
            '{"protocol_version":"'
            + PROTOCOL_VERSION
            + '","request_id":"x","request_id":"y","method":"hello","params":{}}'
        ).encode("utf-8")
        duplicate_nested = request_bytes(params='{"nested":{"key":1,"key":2}}')
        for raw in (duplicate_top_level, duplicate_nested):
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolError) as context:
                    parse_request(raw, "hello")
                self.assertEqual(context.exception.code, "duplicate_json_key")


if __name__ == "__main__":
    unittest.main()
