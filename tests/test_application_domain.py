from __future__ import annotations

import copy
import unittest

from inkcards_anki_bridge.anki.application_260801 import (
    ApplicationBlockedError,
    ApplicationConflictError,
    capability_report,
    capture_card_observation,
    check_before_gate,
    map_rating,
    normalize_event,
    stable_recovery_fingerprint,
)


def _state(state: str = "review", *, revision: str = "r-1") -> dict:
    value = {
        "card_id": "101",
        "schedule_revision": revision,
        "state": state,
        "step": 0,
        "memory": {"stability": 4.0, "difficulty": 6.0},
        "interval_days": 5,
        "due_kind": "review_days",
        "due_value": 20,
        "due_collection_day": 30,
        "short_interval": False,
    }
    if state in ("learning", "relearning"):
        value.update(
            {
                "due_kind": "step_seconds",
                "due_value": 60,
                "due_at_ms": 1_704_067_260_000,
                "due_collection_day": None,
            }
        )
    if state == "new":
        value.update(
            {
                "memory": None,
                "interval_days": None,
                "due_kind": None,
                "due_value": None,
                "due_collection_day": None,
            }
        )
    return value


def _event(**overrides: object) -> dict:
    payload = {
        "card_id": "101",
        "rating": 3,
        "answered_at_ms": 1_704_067_200_000,
        "duration_ms": 1_500,
        "scheduler_version": "x4-2",
        "fsrs_version": "FSRS-6",
        "config_revision": "cfg-1",
        "before": _state(revision="r-1"),
        "after": _state(revision="r-2"),
        "day_context": {"collection_day": 30, "timezone": "UTC"},
    }
    payload.update(overrides)
    return payload


class ApplicationDomainTests(unittest.TestCase):
    def test_rating_mapping_is_explicit_and_rejects_wire_values(self) -> None:
        self.assertEqual([map_rating(value) for value in (1, 2, 3, 4)], [0, 1, 2, 3])
        for value in (0, 5, True, "3", None):
            with self.assertRaises(ApplicationConflictError):
                map_rating(value)

    def test_each_rating_keeps_event_time_and_duration_explicit(self) -> None:
        for rating, expected_enum in ((1, 0), (2, 1), (3, 2), (4, 3)):
            payload = _event(rating=rating, answered_at_ms=1_704_067_201_234, duration_ms=2_750)
            normalized = normalize_event("evt:1", 1, payload)
            with self.subTest(rating=rating):
                self.assertEqual(normalized.rating_anki, expected_enum)
                self.assertEqual(normalized.answered_at_ms, 1_704_067_201_234)
                self.assertEqual(normalized.duration_ms, 2_750)

    def test_fixed_bundle_keeps_write_capability_closed(self) -> None:
        report = capability_report("26.08.1", "39e4b0b4")
        self.assertTrue(report.exact)
        self.assertFalse(report.write_enabled)
        self.assertIn("historical_scheduler", report.disabled_reasons)
        with self.assertRaises(ApplicationBlockedError):
            report.require_write()

    def test_version_or_build_mismatch_is_fail_closed(self) -> None:
        for version, build in (("26.9.0", "39e4b0b4"), ("26.08.1", "other")):
            report = capability_report(version, build)
            self.assertFalse(report.exact)
            self.assertFalse(report.write_enabled)
            self.assertTrue(report.disabled_reasons)

    def test_event_requires_before_after_and_fsrs_memory(self) -> None:
        normalized = normalize_event("evt:1", 1, _event())
        self.assertEqual(normalized.card_id, "101")
        self.assertEqual(normalized.rating_anki, 2)
        self.assertEqual(normalized.duration_ms, 1_500)

        missing_memory = _event(before=_state())
        del missing_memory["before"]["memory"]
        with self.assertRaises(ApplicationConflictError):
            normalize_event("evt:1", 1, missing_memory)

        missing_before = _event()
        del missing_before["before"]
        with self.assertRaises(ApplicationConflictError):
            normalize_event("evt:1", 1, missing_before)

    def test_before_gate_rejects_profile_generation_revision_and_deleted_card(self) -> None:
        event = normalize_event("evt:1", 1, _event())
        observation = {
            "profile_principal": "profile-a",
            "generation": 4,
            "collection_identity": "collection-a",
            "card_id": "101",
            "stable_revision": "before-revision",
            "card_exists": True,
        }
        accepted = check_before_gate(
            event,
            observation,
            profile_principal="profile-a",
            generation=4,
            collection_identity="collection-a",
            expected_revision="before-revision",
        )
        self.assertTrue(accepted.ok)

        cases = (
            {"profile_principal": "other"},
            {"generation": 5},
            {"collection_identity": "other"},
            {"stable_revision": "changed"},
            {"card_exists": False},
        )
        for change in cases:
            current = dict(observation)
            current.update(change)
            with self.subTest(change=change):
                result = check_before_gate(
                    event,
                    current,
                    profile_principal="profile-a",
                    generation=4,
                    collection_identity="collection-a",
                    expected_revision="before-revision",
                )
                self.assertFalse(result.ok)
                self.assertTrue(result.reason)

    def test_before_gate_classifies_undo_pause_reset_preset_and_branch(self) -> None:
        event = normalize_event("evt:1", 1, _event())
        base = {
            "profile_principal": "profile-a",
            "generation": 4,
            "collection_identity": "collection-a",
            "card_id": "101",
            "stable_revision": "before-revision",
            "card_exists": True,
        }
        flags = (
            ("undo_detected", "undo_detected"),
            ("reset_detected", "card_reset"),
            ("paused_or_buried", "card_paused_or_buried"),
            ("preset_changed", "deck_preset_changed"),
            ("branch_detected", "same_card_branch"),
        )
        for flag, reason in flags:
            observation = dict(base)
            observation[flag] = True
            result = check_before_gate(
                event,
                observation,
                profile_principal="profile-a",
                generation=4,
                collection_identity="collection-a",
                expected_revision="before-revision",
            )
            with self.subTest(flag=flag):
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)

    def test_deleted_card_and_missing_card_memory_fail_closed_at_collection_gate(self) -> None:
        class MissingCardCollection:
            def get_card(self, _card_id: int) -> object:
                raise KeyError("deleted")

        with self.assertRaises(ApplicationConflictError):
            capture_card_observation(MissingCardCollection(), "101")

        class MissingMemoryCard:
            id = 101
            queue = 2
            type = 2
            due = 10
            ivl = 5
            reps = 1
            lapses = 0

        class MissingMemoryCollection:
            def get_card(self, _card_id: int) -> MissingMemoryCard:
                return MissingMemoryCard()

            def get_review_logs(self, _card_id: int) -> list:
                return []

        with self.assertRaises(ApplicationConflictError):
            capture_card_observation(MissingMemoryCollection(), "101")

    def test_all_scheduler_states_are_structurally_preserved(self) -> None:
        for state in ("learning", "relearning", "review", "new"):
            payload = _event(before=_state(state, revision="r-1"), after=_state(state, revision="r-2"))
            with self.subTest(state=state):
                self.assertEqual(normalize_event("evt:1", 1, payload).before["state"], state)

    def test_stable_fingerprint_excludes_elapsed_and_dynamic_due(self) -> None:
        snapshot = {
            "profile_principal": "profile-a",
            "collection_identity": "collection-a",
            "card_id": "101",
            "card": {
                "queue": 1,
                "state": "learning",
                "reps": 2,
                "lapses": 1,
                "due": 1_704_067_260,
            },
            "review_logs": [],
            "diagnostic": {
                "collection_mod": 10,
                "scheduler_current_state": {"elapsed_secs": 1},
            },
        }
        later = copy.deepcopy(snapshot)
        later["card"]["due"] = 1_704_067_261
        later["diagnostic"]["collection_mod"] = 11
        later["diagnostic"]["scheduler_current_state"]["elapsed_secs"] = 2
        self.assertEqual(
            stable_recovery_fingerprint(snapshot), stable_recovery_fingerprint(later)
        )


if __name__ == "__main__":
    unittest.main()
