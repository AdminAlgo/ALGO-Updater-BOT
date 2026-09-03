import unittest

from src.eld.base import ConnectionState, DriverSnapshot, HosTimers
from src.messages import (
    cycle_text,
    disconnect_status_phrase,
    disconnect_text,
    low_hours_text,
    on_duty_text,
    shift_violation_text,
)


def _snapshot(name="Jane Driver", duty_code="DS_D", drive_seconds=3600,
             shift_seconds=3600, break_seconds=3600, cycle_seconds=3600):
    return DriverSnapshot(
        driver_id="d1", name=name, username=None,
        duty_status_code=duty_code,
        hos=HosTimers(drive_seconds=drive_seconds, shift_seconds=shift_seconds,
                     break_seconds=break_seconds, cycle_seconds=cycle_seconds),
        connection=ConnectionState(has_vehicle=False),
    )


class LowHoursTextTests(unittest.TestCase):
    def test_greeting_and_name(self):
        text = low_hours_text(_snapshot(name="Jane Driver"))
        self.assertTrue(text.startswith("Hello.\nDear Jane Driver\n\n"))
        self.assertIn("Thank you.", text)

    def test_empty_name_falls_back_to_driver(self):
        text = low_hours_text(_snapshot(name=""))
        self.assertIn("Dear Driver\n\n", text)

    def test_picks_the_most_urgent_timer(self):
        # Break is tightest here — should be named, not Drive or Shift.
        text = low_hours_text(_snapshot(drive_seconds=7200, shift_seconds=7200,
                                        break_seconds=1800))
        self.assertIn("on your Break", text)
        self.assertIn("30 minutes", text)


class CycleTextTests(unittest.TestCase):
    def test_reports_actual_cycle_time_remaining(self):
        text = cycle_text(_snapshot(cycle_seconds=5 * 3600 + 30 * 60))
        self.assertIn("You have only 5 hours and 30 minutes left on your "
                     "70-hour Cycle.", text)
        self.assertIn("Please plan your reset.", text)


class ShiftViolationTextTests(unittest.TestCase):
    def test_uses_configured_hour_limit(self):
        text = shift_violation_text(_snapshot(), shift_limit_hours=14)
        self.assertIn("Your 14-hour Shift limit is finished (00:00).", text)
        self.assertIn("Please go Off Duty", text)


class OnDutyTextTests(unittest.TestCase):
    def test_uses_configured_hours(self):
        text = on_duty_text(_snapshot(), hours=2)
        self.assertIn("more than 2 hours", text)


class DisconnectTextTests(unittest.TestCase):
    def test_status_phrase_matches_duty_status(self):
        cases = {
            "DS_D": "while you are driving",
            "DS_ON": "while you are On Duty",
            "DS_YM": "while you are in Yard Move",
            "DS_PC": "while you are in Personal Conveyance",
        }
        for code, expected_phrase in cases.items():
            snap = _snapshot(duty_code=code)
            phrase = disconnect_status_phrase(snap)
            self.assertEqual(phrase, expected_phrase)
            text = disconnect_text(snap, phrase)
            self.assertIn(f"DISCONNECTED {expected_phrase}.", text)

    def test_unmapped_status_has_no_phrase(self):
        # Off Duty / Sleeper never reach this rule in practice (the `active`
        # gate excludes them), but the guard must still refuse to guess.
        snap = _snapshot(duty_code="DS_OFF")
        self.assertIsNone(disconnect_status_phrase(snap))


if __name__ == "__main__":
    unittest.main()
