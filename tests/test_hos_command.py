"""/hos — the answer to "where is the update".

Threshold alerts are silent by design until a timer actually drops, which makes
a working bot and a broken bot look identical from inside a Telegram group.
/hos reports from the same snapshot the rules are evaluating, so the difference
is visible on demand.
"""

import unittest

from src.commands import _handle_command
from src.eld.base import ConnectionState, DriverSnapshot, HosTimers
from src.registry import Candidate, GroupRegistry

ROSTER = [
    Candidate("d1", "Jane Driver", "janed", "8888", "SOLEH EXPRESS INC"),
    Candidate("d2", "Bek Toshev", None, "77", "GOWIN"),
]

THRESHOLDS = [120, 60, 30]


def _snap(driver_id="d1", name="Jane Driver", drive=6 * 3600, shift=9 * 3600,
          brk=3 * 3600, cycle=41 * 3600, duty="DS_D", truck="8888"):
    return DriverSnapshot(
        driver_id=driver_id, name=name, username=None, duty_status_code=duty,
        hos=HosTimers(drive_seconds=drive, shift_seconds=shift,
                      break_seconds=brk, cycle_seconds=cycle),
        connection=ConnectionState(has_vehicle=True, vehicle_status="IN_MOTION",
                                   vehicle_number=truck),
    )


SNAPSHOTS = [("SOLEH EXPRESS INC", _snap()),
             ("GOWIN", _snap(driver_id="d2", name="Bek Toshev", truck="77"))]


class FakeSender:
    def __init__(self):
        self.dry_run = False
        self.sent = []

    def send_message(self, chat_id, text, kind="", entities=None):
        self.sent.append((str(chat_id), text))

    def last(self):
        return self.sent[-1][1] if self.sent else ""


def _grp(chat_id=-1001, title="#8888 Jane Driver"):
    return {"id": chat_id, "type": "supergroup", "title": title}


_PM = {"id": 999, "type": "private"}


class HosInGroupTests(unittest.TestCase):
    def setUp(self):
        self.snd = FakeSender()
        self.reg = GroupRegistry()

    def _run(self, text, chat, is_admin=True, snapshots=SNAPSHOTS):
        cmd = text.split()[0]
        return _handle_command(cmd, text, chat, self.snd, self.reg, ROSTER,
                               is_admin=is_admin, snapshots=snapshots,
                               low_hours_thresholds=THRESHOLDS)

    def test_reports_the_linked_driver(self):
        self.reg.register("d1", "-1001", "#8888 Jane Driver", "manual", "Jane Driver")
        self.assertTrue(self._run("/hos", _grp()))
        out = self.snd.last()
        self.assertIn("Jane Driver", out)
        self.assertIn("6h 00m", out)        # drive remaining
        self.assertIn("Driving", out)       # duty status label
        self.assertIn("truck 8888", out)

    def test_unlinked_group_says_so_instead_of_staying_silent(self):
        self.assertTrue(self._run("/hos", _grp()))
        out = self.snd.last()
        self.assertIn("No driver is linked", out)
        self.assertIn("/add", out)

    def test_tells_you_when_the_next_warning_would_fire(self):
        self.reg.register("d1", "-1001", "g", "manual", "Jane Driver")
        self._run("/hos", _grp())
        # tightest timer is the 3h break => next threshold down is 120m
        self.assertIn("120m left", self.snd.last())

    def test_says_when_every_threshold_is_already_crossed(self):
        self.reg.register("d1", "-1001", "g", "manual", "Jane Driver")
        snaps = [("SOLEH EXPRESS INC", _snap(drive=20 * 60))]
        self._run("/hos", _grp(), snapshots=snaps)
        self.assertIn("inside every", self.snd.last())

    def test_no_active_shift_is_explained_not_reported_as_trouble(self):
        # shift=0 + drive=0 + a full break timer is the "ELD has no shift yet"
        # artifact that rules.evaluate_driver skips outright.
        self.reg.register("d1", "-1001", "g", "manual", "Jane Driver")
        snaps = [("SOLEH EXPRESS INC", _snap(drive=0, shift=0, brk=8 * 3600))]
        self._run("/hos", _grp(), snapshots=snaps)
        self.assertIn("No active shift", self.snd.last())

    def test_linked_driver_missing_from_this_cycle_is_not_an_error(self):
        self.reg.register("d9", "-1001", "g", "manual", "Ghost Driver")
        self._run("/hos", _grp())
        self.assertIn("No live hours yet", self.snd.last())

    def test_warns_when_the_driver_has_no_group_at_all(self):
        self._run("/hos Jane", _PM)
        self.assertIn("CANNOT be alerted", self.snd.last())


class HosLookupTests(unittest.TestCase):
    def setUp(self):
        self.snd = FakeSender()
        self.reg = GroupRegistry()

    def _run(self, text, chat=_PM, is_admin=True):
        return _handle_command(text.split()[0], text, chat, self.snd, self.reg,
                               ROSTER, is_admin=is_admin, snapshots=SNAPSHOTS,
                               low_hours_thresholds=THRESHOLDS)

    def test_lookup_by_name(self):
        self.assertTrue(self._run("/hos Bek Toshev"))
        self.assertIn("Bek Toshev", self.snd.last())

    def test_lookup_by_truck_number(self):
        self._run("/hos 8888")
        self.assertIn("Jane Driver", self.snd.last())

    def test_unknown_name_points_at_roster(self):
        self._run("/hos Nobody")
        self.assertIn("/roster", self.snd.last())

    def test_pm_without_a_name_asks_for_one(self):
        self._run("/hos")
        self.assertIn("Which driver", self.snd.last())

    def test_non_admin_cannot_look_up_other_drivers(self):
        self._run("/hos Bek Toshev", is_admin=False)
        self.assertIn("plain /hos", self.snd.last())

    def test_non_admin_can_still_ask_about_their_own_group(self):
        self.reg.register("d1", "-1001", "g", "manual", "Jane Driver")
        handled = _handle_command("/hos", "/hos", _grp(), self.snd, self.reg,
                                  ROSTER, is_admin=False, snapshots=SNAPSHOTS,
                                  low_hours_thresholds=THRESHOLDS)
        self.assertTrue(handled)
        self.assertIn("Jane Driver", self.snd.last())

    def test_without_snapshots_it_admits_it_rather_than_guessing(self):
        handled = _handle_command("/hos", "/hos", _grp(), self.snd, self.reg,
                                  ROSTER, is_admin=True, snapshots=None)
        self.assertTrue(handled)
        self.assertIn("aren't available", self.snd.last())

    def test_status_still_means_coverage(self):
        # /status was already an alias for /coverage — /hos must not steal it.
        self._run("/status")
        self.assertIn("coverage", self.snd.last().lower())


if __name__ == "__main__":
    unittest.main()
