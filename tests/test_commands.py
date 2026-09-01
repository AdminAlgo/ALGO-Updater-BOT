import unittest

from src.commands import _handle_command, _parse_assign_args, _roster_search, process_updates
from src.registry import Candidate, GroupRegistry

ROSTER = [
    Candidate("drv_100001", "Ali Niezov", "alin", "1833", "WWH INC"),
    Candidate("drv_100002", "Ali Niezed", "alie", "2044", "WWH INC"),
    Candidate("drv_200003", "Bek Toshev", None, "77", "GOWIN"),
]


class FakeSender:
    """Records send_message calls; serves a fixed list of updates once."""

    def __init__(self, updates=None):
        self.dry_run = False
        self._updates = list(updates or [])
        self.sent = []  # (chat_id, text)

    def send_message(self, chat_id, text, kind="", entities=None):
        self.sent.append((str(chat_id), text))

    def get_updates(self, offset=0, timeout=0):
        u, self._updates = self._updates, []
        return u

    def last(self):
        return self.sent[-1][1] if self.sent else ""


def _pm(text, user_id=999):
    return {"chat": {"id": user_id, "type": "private"}, "from": {"id": user_id}, "text": text}


def _grp(text, chat_id=-1001, title="Some Group", user_id=999):
    return {"chat": {"id": chat_id, "type": "supergroup", "title": title},
            "from": {"id": user_id}, "text": text}


class AssignArgParsingTests(unittest.TestCase):
    def test_pipe_separates_id(self):
        q, cid, err = _parse_assign_args("/assign Ali Niezov | -1001999", {"type": "private"})
        self.assertEqual((q, cid, err), ("Ali Niezov", "-1001999", None))

    def test_trailing_id(self):
        q, cid, err = _parse_assign_args("/assign Bek Toshev -1002888", {"type": "private"})
        self.assertEqual((q, cid, err), ("Bek Toshev", "-1002888", None))

    def test_group_context_supplies_id(self):
        q, cid, err = _parse_assign_args("/assign Ali Niezov",
                                         {"type": "supergroup", "id": -1007})
        self.assertEqual((q, cid, err), ("Ali Niezov", "-1007", None))

    def test_pm_without_id_is_an_error(self):
        q, cid, err = _parse_assign_args("/assign Ali Niezov", {"type": "private"})
        self.assertIsNone(cid)
        self.assertIn("group id", err)


class RosterSearchTests(unittest.TestCase):
    def test_substring(self):
        self.assertEqual({c.driver_id for c in _roster_search("niez", ROSTER)},
                         {"drv_100001", "drv_100002"})

    def test_truck(self):
        self.assertEqual([c.driver_id for c in _roster_search("77", ROSTER)], ["drv_200003"])

    def test_full_name_unique(self):
        self.assertEqual([c.driver_id for c in _roster_search("bek toshev", ROSTER)],
                         ["drv_200003"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.reg = GroupRegistry()
        self.snd = FakeSender()

    def _run(self, text, chat):
        cmd = text.split()[0].split("@")[0].lower()
        return _handle_command(cmd, text, chat, self.snd, self.reg, ROSTER, is_admin=True)

    def test_faq_is_available_to_everyone(self):
        for is_admin in (True, False):
            snd = FakeSender()
            handled = _handle_command("/faq", "/faq", {"id": 1, "type": "private"},
                                      snd, self.reg, ROSTER, is_admin=is_admin)
            self.assertTrue(handled)
            body = snd.last()
            self.assertIn("LOW HOURS", body)
            self.assertIn("14-HOUR SHIFT LIMIT", body)
            self.assertLess(len(body), 4096)

    def test_non_admin_is_refused(self):
        cmd = "/groups"
        handled = _handle_command(cmd, "/groups", {"id": 1, "type": "private"},
                                  self.snd, self.reg, ROSTER, is_admin=False)
        self.assertTrue(handled)
        self.assertIn("administrators", self.snd.last())

    def test_assign_from_pm(self):
        self.reg.record_group("-1005", "Ali N group", "t0")
        self._run("/assign Ali Niezov | -1005", {"id": 999, "type": "private"})
        self.assertEqual(self.reg.chat_for("drv_100001"), "-1005")
        self.assertIn("Ali Niezov", self.snd.last())

    def test_assign_ambiguous_query_is_rejected(self):
        self._run("/assign Ali | -1005", {"id": 999, "type": "private"})
        self.assertIsNone(self.reg.chat_for("drv_100001"))
        self.assertIsNone(self.reg.chat_for("drv_100002"))
        self.assertIn("name one exactly", self.snd.last())

    def test_whois_reports_assignment(self):
        self.reg.register("drv_200003", "-1009", "Bek grp", "manual", "Bek Toshev")
        self._run("/whois -1009", {"id": 999, "type": "private"})
        self.assertIn("Bek Toshev", self.snd.last())

    def test_unassign_blocks_rediscovery(self):
        self.reg.register("drv_100001", "-1005", "t", "manual", "Ali Niezov")
        self._run("/unassign Ali Niezov", {"id": 999, "type": "private"})
        self.assertFalse(self.reg.is_registered("drv_100001"))
        self.assertTrue(self.reg.is_blocked("drv_100001"))

    def test_groups_and_unassigned_reports(self):
        self.reg.record_group("-1005", "Unclaimed Group", "t0")
        self._run("/groups", {"id": 999, "type": "private"})
        self.assertIn("Unclaimed Group", self.snd.last())
        self._run("/unassigned", {"id": 999, "type": "private"})
        self.assertIn("-1005", self.snd.last())


class DiscoveryTests(unittest.TestCase):
    def test_unique_title_auto_registers(self):
        snd = FakeSender([
            {"update_id": 5, "message": _grp("hi", chat_id=-1001, title="#1833 Ali Niezov | WWH")}
        ])
        reg = GroupRegistry()
        newly = process_updates(snd, reg, ROSTER)
        self.assertEqual(reg.chat_for("drv_100001"), "-1001")
        self.assertEqual([n[0] for n in newly], ["Ali Niezov"])

    def test_ambiguous_title_goes_to_pending_not_registered(self):
        # "Ali Niez" substring hits both Ali Niezov and Ali Niezed.
        snd = FakeSender([
            {"update_id": 6, "message": _grp("hi", chat_id=-1002, title="Ali Niezov and Ali Niezed")}
        ])
        reg = GroupRegistry()
        process_updates(snd, reg, ROSTER)
        self.assertIsNone(reg.chat_for("drv_100001"))
        self.assertIn("-1002", reg.pending())
        self.assertIn("-1002", [g["chat_id"] for g in reg.unassigned_groups()])

    def test_bot_removed_forgets_group(self):
        reg = GroupRegistry()
        reg.record_group("-1003", "Old Group", "t0")
        left = {"update_id": 7, "my_chat_member": {
            "chat": {"id": -1003, "type": "supergroup", "title": "Old Group"},
            "from": {"id": 1},
            "new_chat_member": {"user": {"id": 42, "is_bot": True}, "status": "left"},
        }}
        process_updates(FakeSender([left]), reg, ROSTER)
        self.assertNotIn("-1003", reg.known_groups())

    def test_offset_advances_even_with_no_actionable_updates(self):
        snd = FakeSender([{"update_id": 99, "message": _pm("hello there")}])
        reg = GroupRegistry()
        process_updates(snd, reg, ROSTER)
        self.assertEqual(reg.offset, 100)


if __name__ == "__main__":
    unittest.main()
