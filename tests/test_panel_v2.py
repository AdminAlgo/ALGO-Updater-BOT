"""Panel v2 — every page renders, the main forms work, and the new rules
(off-duty check-in, pause, company-wide alert types/language) behave."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src import messages
from src.activity_log import ActivityLog
from src.alert_texts import AlertTextStore
from src.eld.base import ConnectionState, DriverSnapshot, HosTimers
from src.registry import GroupRegistry
from src.roster_cache import RosterCache
from src.roster_seen import RosterSeen
from src.rules import KIND_OFF_DUTY_CHECKIN, evaluate_driver
from src.state import AlertState
from src.telegram_sender import SendResult
from src.templates_store import TemplateStore

CONFIG_YAML = """
poll_interval_seconds: 120
team_group_chat_id: "-100123"
disconnect_realert_minutes: 60
disconnect_stale_minutes: 30
shift_limit_hours: 14
connection_required_statuses: ["Driving", "On Duty", "Yard Move"]
low_hours_thresholds_minutes:
  driver_group: [120, 60, 30]
  team_group: []
companies:
  - name: "ACME"
    provider: "factor"
    driver_group_chat_id: "-100123"
    usdot: 1234567
    monitor_all_drivers: true
    company_key_env: "ACME_KEY"
"""

ENV = {
    "FACTOR_API_BASE_URL": "https://x", "LEADER_API_BASE_URL": "https://x",
    "TELEGRAM_BOT_TOKEN": "123456789:test", "FACTOR_API_KEY": "pk", "LEADER_API_KEY": "pk",
    "ACME_KEY": "ck", "DASHBOARD_SECRET_KEY": "s" * 32, "DASHBOARD_ADMIN_PASSWORD": "pw",
    "DASHBOARD_ADMIN_USERNAME": "boss",
}


def _snap(did, name, duty="DS_D", drive=3600, shift=5 * 3600, brk=2 * 3600, cycle=30 * 3600,
          status="IN_MOTION", truck="51", loc="0.9mi ENE of Hicks Station, TX"):
    return DriverSnapshot(
        driver_id=did, name=name, username=None, duty_status_code=duty,
        hos=HosTimers(drive_seconds=drive, shift_seconds=shift, break_seconds=brk,
                      cycle_seconds=cycle),
        connection=ConnectionState(has_vehicle=True, vehicle_status=status, vehicle_number=truck,
                                   last_telemetry=datetime.now(timezone.utc) - timedelta(seconds=53)),
        raw={"vehicle": {"calc_location": loc, "speed": 61}},
    )


class _Sender:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send_alert(self, alert):
        self.sent.append(alert)
        return SendResult(True, alert.chat_id, alert.kind)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in list(ENV) + ["CONFIG_PATH"]}
        os.environ.update(ENV)
        os.environ.pop("CONFIG_PATH", None)
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "config.yaml").write_text(CONFIG_YAML, "utf-8")
        (d / ".env").write_text("", "utf-8")
        from src.config import load_config
        from src.dashboard.app import create_app
        from src.dashboard.runtime import RuntimeContext

        self.registry = GroupRegistry(d / "driver_groups.json")
        self.registry.record_group("-100111", "#51 Alice Driver", "2026-01-01")
        self.registry.record_group("-100222", "Dispatch Alice", "2026-01-01")
        self.registry.register("d1", "-100111", "#51 Alice Driver", "name", "Alice Driver")
        self.cache = RosterCache(lambda: load_config(str(d / "config.yaml"), str(d / ".env")),
                                 seen=RosterSeen(d / "seen.json"))
        self.cache.prime("ACME", [
            _snap("d1", "Alice Driver", drive=20 * 60, status="OFFLINE"),
            _snap("d2", "Bob Trucker", duty="DS_OFF"),
            _snap("d3", "Carla Haul", duty="DS_ON"),
        ])
        self.sender = _Sender()
        store = TemplateStore(d / "templates.json")
        store.seed_defaults()
        self.runtime = RuntimeContext(
            config_path=str(d / "config.yaml"), env_path=str(d / ".env"),
            registry=self.registry, state=AlertState(), activity_log=ActivityLog(None),
            roster_cache=self.cache, sender=self.sender, template_store=store,
            send_log_path=str(d / "send_log.jsonl"),
            alert_texts=AlertTextStore(d / "alert_texts.json"),
        )
        app = create_app(self.runtime)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        messages.set_overrides({})
        self.tmp.cleanup()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _login(self):
        return self.client.post("/login", data={"username": "boss", "password": "pw"})

    def test_login_needs_username_and_password(self):
        r = self.client.post("/login", data={"username": "admin", "password": "pw"})
        self.assertIn(b"Wrong username or password", r.data)
        self.assertEqual(self._login().status_code, 302)

    def test_every_page_renders(self):
        self._login()
        for path in ("/", "/drivers", "/drivers?filter=unlinked", "/drivers?sort=time",
                     "/companies", "/watchlists", "/watchlists?min=120", "/groups",
                     "/warnings", "/stats", "/stats?period=all_time", "/settings",
                     "/drivers/export.csv", "/watchlists/low.csv", "/watchlists/resting.csv"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
        page = self.client.get("/drivers").data.decode()
        self.assertIn("Hicks Station", page)          # location like the ELD platform
        self.assertIn("53 seconds ago", page)
        self.assertIn("00:20", page)                  # drive clock HH:MM
        self.assertIn("No group", page)               # unlinked drivers in red

    def test_edit_by_group_name_sets_dispatch_language_and_kinds(self):
        self._login()
        r = self.client.post("/drivers/d1/edit", data={
            "driver_name": "Alice Driver", "chat_id": "#51 Alice Driver",
            "dispatch_chat_id": "Dispatch Alice (-100222)", "language": "ru",
            "updates_on": "1", "kinds": ["low_hours", "cycle", "shift_violation"],
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.registry.chat_for("d1"), "-100111")
        self.assertEqual(self.registry.dispatch_chat_for("d1"), "-100222")
        self.assertEqual(self.registry.explicit_language("d1"), "ru")
        self.assertEqual(set(self.registry.disabled_kinds_for("d1")),
                         {"disconnect", "on_duty", "off_duty_checkin"})

    def test_unknown_group_name_is_refused(self):
        self._login()
        self.client.post("/drivers/d2/edit", data={"driver_name": "Bob", "chat_id": "Nope"})
        self.assertIsNone(self.registry.chat_for("d2"))

    def test_pause_and_company_alert_types(self):
        self._login()
        self.client.post("/drivers/d1/pause", data={"paused": "1"})
        self.assertTrue(self.registry.is_paused("d1"))
        self.client.post("/companies/ACME/alerts", data={
            "kinds": ["low_hours", "cycle", "shift_violation", "on_duty", "off_duty_checkin"],
            "language": "uz"})
        company = self.runtime.current_config().companies[0]
        self.assertEqual(company.disabled_kinds, ["disconnect"])
        self.assertEqual(company.language, "uz")

    def test_broadcast_preview_then_send(self):
        self._login()
        tid = self.runtime.template_store.all()[0]["id"]
        r = self.client.post("/warnings/preview", data={"template_id": tid, "audience": "all"})
        self.assertIn(b"Send now to 1 driver", r.data)
        self.client.post("/warnings/send", data={"template_id": tid, "audience": "all"})
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn("Alice Driver", self.sender.sent[0].text)

    def test_settings_save(self):
        self._login()
        self.client.post("/settings", data={
            "off_duty_checkin_days": "3, 5", "low_hours": "120,60,30", "cycle": "10,5",
            "on_duty_alert_hours": "2", "disconnect_alerts_enabled": "1",
            "disconnect_stale_minutes": "30", "disconnect_realert_minutes": "60",
            "shift_violation_resend_minutes": "30", "watchlist_low_minutes": "45"})
        cfg = self.runtime.current_config()
        self.assertEqual(cfg.off_duty_checkin_days, [3, 5])
        self.assertEqual(cfg.watchlist_low_minutes, 45)

    def test_alert_text_override_is_used_and_validated(self):
        self._login()
        self.client.post("/warnings/alert-texts/off_duty_checkin",
                         data={"text_en": "Hi {name}, off {days}. OK?"})
        text = messages.off_duty_checkin_text(_snap("x", "Ann"), 2)
        self.assertEqual(text, "Hi Ann, off 2 days. OK?")
        r = self.client.post("/warnings/alert-texts/cycle",
                             data={"text_en": "Hi {nme}"}, follow_redirects=True)
        self.assertIn(b"unknown placeholder", r.data)


def _cfg(**kw):
    base = dict(connection_required_statuses=["Driving", "On Duty", "Yard Move"],
                low_hours_thresholds_minutes=SimpleNamespace(driver_group=[120, 60, 30], team_group=[]),
                cycle_alert_thresholds_hours=[10, 5], shift_limit_hours=14,
                disconnect_stale_minutes=30, disconnect_realert_minutes=60,
                on_duty_alert_hours=0, off_duty_checkin_days=[2, 3], team_group_chat_id="-1",
                attach_log_image=False)
    base.update(kw)
    return SimpleNamespace(**base)


class OffDutyCheckinTests(unittest.TestCase):
    def setUp(self):
        self.reg = GroupRegistry()
        self.reg.register("d1", "-100", "t", "name", "Ann Lee")
        self.state = AlertState()
        self.company = SimpleNamespace(name="ACME", driver_group_chat_id="-9",
                                       disabled_kinds=[], language="ru")
        self.t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def _eval(self, when, duty="DS_OFF", **cfg):
        snap = _snap("d1", "Ann Lee", duty=duty, drive=11 * 3600, shift=14 * 3600, brk=8 * 3600)
        alerts = evaluate_driver(snap, company=self.company, config=_cfg(**cfg), state=self.state,
                                 now=when, registry=self.reg)
        for a in alerts:
            a.commit(self.state, when)
        return [a for a in alerts if a.kind == KIND_OFF_DUTY_CHECKIN]

    def test_sends_at_2_then_3_days_in_company_language(self):
        self.assertEqual(self._eval(self.t0), [])                       # clock starts
        self.assertEqual(self._eval(self.t0 + timedelta(days=1)), [])
        two = self._eval(self.t0 + timedelta(days=2, minutes=1))
        self.assertEqual(len(two), 1)
        self.assertIn("2 дня", two[0].text)                             # company default = ru
        self.assertEqual(self._eval(self.t0 + timedelta(days=2, hours=5)), [])  # once only
        self.assertEqual(len(self._eval(self.t0 + timedelta(days=3, minutes=1))), 1)

    def test_back_on_duty_resets(self):
        self._eval(self.t0)
        self._eval(self.t0 + timedelta(days=1), duty="DS_D")
        self.assertEqual(self._eval(self.t0 + timedelta(days=2, minutes=1)), [])

    def test_pause_and_company_kind_switch_it_off(self):
        self._eval(self.t0)
        self.reg.set_paused("d1", True)
        self.assertEqual(self._eval(self.t0 + timedelta(days=2, minutes=1)), [])
        self.reg.set_paused("d1", False)
        self.company.disabled_kinds = ["off_duty_checkin"]
        self.assertEqual(self._eval(self.t0 + timedelta(days=2, minutes=2)), [])


class RosterSeenTests(unittest.TestCase):
    def test_baseline_then_new(self):
        seen = RosterSeen()
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        seen.observe("ACME", [_snap("a", "A")], now=t0)
        seen.observe("ACME", [_snap("b", "B")], now=t0 + timedelta(hours=2))
        now = t0 + timedelta(hours=3)
        self.assertFalse(seen.is_new("a", now))
        self.assertTrue(seen.is_new("b", now))
        self.assertFalse(seen.is_new("b", t0 + timedelta(days=9)))


class TranslationTests(unittest.TestCase):
    def test_every_kind_has_all_four_languages(self):
        for kind, table in messages.TEMPLATES.items():
            self.assertEqual(set(table), set(messages.LANGUAGES), kind)
            for lang, text in table.items():
                self.assertEqual(messages.template_problems(kind, text), [], (kind, lang))


if __name__ == "__main__":
    unittest.main()
