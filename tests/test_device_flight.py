"""设备日志必须认证、持久化并严格排除任意正文或凭据字段。"""

import json
from unittest.mock import patch

from tests.test_device_routes import DeviceRouteTests


class DeviceFlightTests(DeviceRouteTests):
    def report(self):
        return {
            "schema": "inkcards-sync-flight-v1",
            "app_version": "0.2.70",
            "status": "running",
            "events": [
                {
                    "sequence": 1,
                    "stage": "descriptor_decode",
                    "ms": 1000,
                    "heap": 1900000,
                    "bytes": 32700,
                    "kind": "index",
                }
            ],
        }

    def send(self, report, headers=None, session="session-a"):
        return self.call(
            "POST",
            "/v2/diagnostics/report",
            {"device_id": "device-a", "session_id": session, "report": report},
            self.headers if headers is None else headers,
        )

    def test_authenticated_bounded_report_is_persisted(self):
        self.assertEqual(self.send(self.report())[0], 200)
        path = self.store.root / "diagnostics/device-flight.jsonl"
        self.assertEqual(json.loads(path.read_text().splitlines()[-1]), self.report())
        self.assertEqual(self.send(self.report(), headers={})[0], 401)
        self.assertEqual(self.send(self.report(), session="foreign-session")[0], 409)
        for _ in range(70):
            self.assertEqual(self.send(self.report())[0], 200)
        self.assertLessEqual(path.stat().st_size, 65536)
        self.assertLessEqual(len(path.read_text().splitlines()), 16)
        for changes in [
            {"card_text": "secret"},
            {"token": "secret"},
            {"stage": "secret"},
            {"heap": True},
        ]:
            value = self.report()
            value["events"][0].update(changes)
            self.assertEqual(self.send(value)[0], 422)
        self.assertNotIn("secret", path.read_text())

    def test_previous_diagnostic_version_and_file_phase_remain_supported(self):
        for version in ("0.2.68", "0.2.69"):
            old = self.report()
            old["app_version"] = version
            self.assertEqual(self.send(old)[0], 200)
        current = self.report()
        current["events"][0]["stage"] = "file_existing"
        self.assertEqual(self.send(current)[0], 200)
        current["events"][0]["stage"] = "descriptor_existing"
        self.assertEqual(self.send(current)[0], 200)

    def test_disk_failure_has_no_success_ack(self):
        with patch(
            "inkcards_anki_bridge.device.routes.save_report",
            side_effect=OSError("disk full"),
        ):
            status, body = self.send(self.report())
        self.assertEqual(status, 503)
        self.assertNotIn("received", body)
        self.assertFalse((self.store.root / "diagnostics/device-flight.jsonl").exists())

    def test_current_bounded_scan_and_cleanup_stages_are_accepted(self):
        current = self.report()
        current["app_version"] = "0.2.71"
        for stage in ("admit_candidate", "missing_files", "prepare",
                      "collect_repository", "collect_repository_done"):
            current["events"][0]["stage"] = stage
            self.assertEqual(self.send(current)[0], 200)
