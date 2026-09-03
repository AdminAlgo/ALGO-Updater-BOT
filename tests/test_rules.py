import unittest
from datetime import datetime, timedelta, timezone

from src.config import Company, Config, LowHoursThresholds, Secrets
from src.eld.base import ConnectionState, DriverSnapshot, HosTimers
from src.rules import (
    DRIVER_GROUP,
    KIND_CYCLE,
    KIND_SHIFT_VIOLATION,
    TEAM_GROUP,
    evaluate_driver,
)
from src.state import AlertState


def _config(**overrides):
    base = dict(
        poll_interval_seconds=120,
        team_group_chat_id="team-chat",
        low_hours_thresholds_minutes=LowHoursThresholds(driver_group=[], team_group=[]),
        disconnect_realert_minutes=60,
        disconnect_stale_minutes=30,
        shift_limit_hours=14,
        connection_required_statuses=["Driving", "On Duty", "Yard Move"],
        companies=[],
        secrets=Secrets(
            factor_api_base_url="https://x", leader_api_base_url="https://x",
            telegram_bot_token="123:test",
        ),
        attach_log_image=False,
        disconnect_alerts_enabled=False,
        on_duty_alert_hours=0,
    )
    base.update(overrides)
    return Config(**base)


def _company(**overrides):
    base = dict(
        name="Acme", provider="factor", driver_group_chat_id="driver-chat",
        monitor_all_drivers=True,
    )
    base.update(overrides)
    return Company(**base)


def _snapshot(duty_code="DS_D", shift_seconds=0, cycle_seconds=20 * 3600):
    # cycle_seconds defaults well above the [10, 5]-hour Cycle thresholds so
    # tests that aren't about the Cycle rule don't spuriously trigger it.
    return DriverSnapshot(
        driver_id="d1", name="Jane Driver", username=None,
        duty_status_code=duty_code,
        hos=HosTimers(drive_seconds=3600, shift_seconds=shift_seconds,
                     break_seconds=3600, cycle_seconds=cycle_seconds),
        connection=ConnectionState(has_vehicle=False),
    )


class CycleAlertTests(unittest.TestCase):
    """A1: 70-hour Cycle running low."""

    def test_fires_at_10_and_5_hour_thresholds_once_each(self):
        config = _config()
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)

        # shift_seconds must stay away from 0 here or the shift-violation
        # rule fires too and pollutes the alert count.
        alerts = evaluate_driver(_snapshot(shift_seconds=3600, cycle_seconds=9 * 3600),
                                 company=company, config=config, state=state, now=now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, KIND_CYCLE)
        self.assertEqual(alerts[0].threshold, 10)
        self.assertEqual(alerts[0].audience, DRIVER_GROUP)
        alerts[0].commit(state, now)

        # Still under 10h but hasn't dropped to 5h — no repeat.
        alerts = evaluate_driver(_snapshot(shift_seconds=3600, cycle_seconds=8 * 3600),
                                 company=company, config=config, state=state, now=now)
        self.assertEqual(alerts, [])

        # Crosses 5h — a second, distinct (and more urgent) alert.
        alerts = evaluate_driver(_snapshot(shift_seconds=3600, cycle_seconds=4 * 3600),
                                 company=company, config=config, state=state, now=now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].threshold, 5)

    def test_resets_on_recovery_and_on_off_duty(self):
        config = _config()
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)

        # cycle_hours=4 crosses BOTH thresholds; only the most urgent (5) fires.
        alerts = evaluate_driver(_snapshot(shift_seconds=3600, cycle_seconds=4 * 3600),
                                 company=company, config=config, state=state, now=now)
        alerts[0].commit(state, now)
        self.assertTrue(state.cycle_already_fired("d1", DRIVER_GROUP, 5))

        # 34-hour restart brings Cycle back up above every threshold.
        evaluate_driver(_snapshot(shift_seconds=3600, cycle_seconds=70 * 3600),
                        company=company, config=config, state=state, now=now)
        self.assertFalse(state.cycle_already_fired("d1", DRIVER_GROUP, 5))

    def test_dispatch_also_notified_when_flag_enabled(self):
        config = _config(cycle_also_notify_dispatch=True)
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)

        alerts = evaluate_driver(_snapshot(shift_seconds=3600, cycle_seconds=4 * 3600),
                                 company=company, config=config, state=state, now=now)
        self.assertEqual({a.audience for a in alerts}, {DRIVER_GROUP, TEAM_GROUP})

    def test_never_fires_off_duty(self):
        config = _config()
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)

        alerts = evaluate_driver(
            _snapshot(duty_code="DS_OFF", cycle_seconds=1 * 3600),
            company=company, config=config, state=state, now=now,
        )
        self.assertEqual(alerts, [])


class ShiftViolationRoutingTests(unittest.TestCase):
    """FIX-1: violation alert goes to the driver's group, not dispatch."""

    def test_default_goes_to_driver_group_only(self):
        config = _config()
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)

        alerts = evaluate_driver(_snapshot(), company=company, config=config,
                                 state=state, now=now)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, KIND_SHIFT_VIOLATION)
        self.assertEqual(alerts[0].audience, DRIVER_GROUP)
        self.assertEqual(alerts[0].chat_id, "driver-chat")

    def test_dispatch_also_notified_when_flag_enabled(self):
        config = _config(shift_violation_also_notify_dispatch=True)
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)

        alerts = evaluate_driver(_snapshot(), company=company, config=config,
                                 state=state, now=now)

        audiences = {a.audience for a in alerts}
        self.assertEqual(audiences, {DRIVER_GROUP, TEAM_GROUP})


class ShiftViolationResendTests(unittest.TestCase):
    """FIX-2: violation alert repeats every N minutes while still violating."""

    def test_resends_every_30_minutes_then_stops_off_duty(self):
        config = _config(shift_violation_resend_minutes=30, shift_violation_max_resends=5)
        company = _company()
        state = AlertState()
        start = datetime.now(timezone.utc)

        sent_at_minutes = []
        # Poll every 5 minutes for 95 minutes, as the real 2-min scheduler would.
        for minute in range(0, 96, 5):
            now = start + timedelta(minutes=minute)
            alerts = evaluate_driver(_snapshot(), company=company, config=config,
                                     state=state, now=now)
            for alert in alerts:
                alert.commit(state, now)
                sent_at_minutes.append(minute)

        self.assertEqual(sent_at_minutes, [0, 30, 60, 90])

        # Driver goes Off Duty — no more alerts, and the episode resets.
        off_duty_snap = _snapshot(duty_code="DS_OFF", shift_seconds=0)
        now = start + timedelta(minutes=100)
        alerts = evaluate_driver(off_duty_snap, company=company, config=config,
                                 state=state, now=now)
        self.assertEqual(alerts, [])
        self.assertFalse(state.shift_violation_active("d1"))
        self.assertEqual(state.shift_violation_resend_count("d1"), 0)

    def test_stops_at_max_resends(self):
        config = _config(shift_violation_resend_minutes=30, shift_violation_max_resends=2)
        company = _company()
        state = AlertState()
        start = datetime.now(timezone.utc)

        sent_at_minutes = []
        for minute in range(0, 151, 5):
            now = start + timedelta(minutes=minute)
            alerts = evaluate_driver(_snapshot(), company=company, config=config,
                                     state=state, now=now)
            for alert in alerts:
                alert.commit(state, now)
                sent_at_minutes.append(minute)

        # 1 initial + 2 resends = 3 messages total, then it stops (safety cap).
        self.assertEqual(sent_at_minutes, [0, 30, 60])


class NoActiveShiftGuardTests(unittest.TestCase):
    """A freshly-clocked-on driver (shift=0, drive=0, break full) is a data
    artifact, not a violation — must not crash or fire any HOS alert."""

    def test_guard_clears_state_without_error(self):
        config = _config()
        company = _company()
        state = AlertState()
        now = datetime.now(timezone.utc)
        snap = DriverSnapshot(
            driver_id="d1", name="Jane Driver", username=None,
            duty_status_code="DS_D",
            hos=HosTimers(drive_seconds=0, shift_seconds=0,
                         break_seconds=8 * 3600, cycle_seconds=3600),
            connection=ConnectionState(has_vehicle=False),
        )

        alerts = evaluate_driver(snap, company=company, config=config,
                                 state=state, now=now)

        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
