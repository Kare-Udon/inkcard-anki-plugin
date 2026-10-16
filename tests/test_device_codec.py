from __future__ import annotations

import json
import unittest

from inkcards_anki_bridge.device.codec import (
    CodecError,
    decode_object,
    encode_json,
    error_envelope,
)


class DeviceCodecTests(unittest.TestCase):
    def test_duplicate_key_is_rejected_at_every_object_level(self) -> None:
        with self.assertRaisesRegex(CodecError, "duplicate_key"):
            decode_object(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(CodecError, "duplicate_key"):
            decode_object(b'{"outer":{"a":1,"a":2}}')

    def test_non_finite_numbers_are_rejected(self) -> None:
        for raw in (b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(CodecError, "non_finite_number"):
                    decode_object(raw)

    def test_non_object_bad_utf8_and_unknown_fields_fail_closed(self) -> None:
        for raw in (b"[]", b"null", b"1"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(CodecError, "top_level_object_required"):
                    decode_object(raw)
        with self.assertRaisesRegex(CodecError, "invalid_utf8"):
            decode_object(b"{\xff")
        with self.assertRaisesRegex(CodecError, "unknown_field"):
            decode_object(b'{"known":1,"extra":2}', allowed={"known"})
        with self.assertRaisesRegex(CodecError, "missing_field"):
            decode_object(b'{"known":1}', required={"required"})

    def test_request_byte_budget_is_checked_before_decode(self) -> None:
        with self.assertRaisesRegex(CodecError, "request_too_large"):
            decode_object(b'{"text":"12345"}', max_bytes=10)

    def test_response_budget_counts_encoded_utf8_bytes(self) -> None:
        payload = {"text": "😀"}
        encoded = encode_json(payload)
        self.assertEqual(json.loads(encoded.decode("utf-8")), payload)
        self.assertGreater(len(encoded), len('{"text":"😀"}'))
        with self.assertRaisesRegex(CodecError, "response_too_large"):
            encode_json(payload, max_bytes=len(encoded) - 1)

    def test_error_envelope_is_stable_and_does_not_echo_input(self) -> None:
        envelope = error_envelope("invalid_token")
        self.assertEqual(
            envelope,
            {
                "error": {
                    "code": "invalid_token",
                    "message": "invalid_token",
                    "retryable": False,
                }
            },
        )
        self.assertNotIn("secret", repr(error_envelope("invalid_token")))


if __name__ == "__main__":
    unittest.main()
