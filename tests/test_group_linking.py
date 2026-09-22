"""Zero-touch group linking: phonetic matching, sender linking, bulk linking.

The bug these cover: driver-group-only routing shipped against a registry that
matched group titles by raw substring, so a title with the name order flipped,
a middle name dropped, punctuation, or Cyrillic never linked — and an unlinked
driver silently gets no alerts at all.
"""

import unittest

from src.commands import process_updates
from src.registry import Candidate, GroupRegistry, best_match, match_sender, token_key

ROSTER = [
    Candidate("d1", "AZIZ KARIMOV", None, "118", "WWH INC"),
    Candidate("d2", "RUSTAM YULDASHEV", None, "402", "ZOHA LLC"),
    Candidate("d3", "ABIB ALI MOHAMED", None, None, "MNK EXPRESS INC"),
    Candidate("d4", "SHERZOD KHASANOV", None, None, "GOWIN"),
    Candidate("d5", "AZIZ TOSHEV", None, "900", "WWH INC"),
]


class TokenKeyTests(unittest.TestCase):
    def test_cyrillic_and_latin_collapse_to_one_key(self):
        self.assertEqual(token_key("Каримов"), token_key("Karimov"))

    def test_transliteration_spelling_variants_collapse(self):
        self.assertEqual(token_key("Khasanov"), token_key("Hasanov"))
        self.assertEqual(token_key("Yuldashev"), token_key("Iuldashev"))

    def test_doubled_letters_collapse(self):
        self.assertEqual(token_key("Abbos"), token_key("Abos"))

    def test_leading_zeros_on_a_truck_number_are_ignored(self):
        self.assertEqual(token_key("0118"), token_key("118"))


class BestMatchTests(unittest.TestCase):
    def _link(self, title):
        return best_match(title, ROSTER)

    def test_flipped_name_order_links(self):
        m = self._link("Karimov Aziz")
        self.assertEqual((m.driver_id, m.confidence), ("d1", "high"))

    def test_punctuation_glued_name_links(self):
        self.assertEqual(self._link("Aziz.Karimov").driver_id, "d1")

    def test_cyrillic_title_links_to_latin_roster_name(self):
        self.assertEqual(self._link("Юлдашев Рустам").driver_id, "d2")

    def test_dropped_middle_name_links(self):
        self.assertEqual(self._link("Abib Mohamed ELD").driver_id, "d3")

    def test_spelling_variant_links(self):
        self.assertEqual(self._link("Serzod Hasanov").driver_id, "d4")

    def test_first_name_plus_truck_links(self):
        m = self._link("WWH | 118 | Aziz")
        self.assertEqual((m.driver_id, m.matched_on), ("d1", "name+truck"))

    def test_truck_number_alone_links(self):
        self.assertEqual(self._link("Truck 402").driver_id, "d2")

    # --- the refusals matter more than the matches --- #
    def test_shared_first_name_alone_is_refused(self):
        m = self._link("Aziz")
        self.assertIsNone(m.driver_id)

    def test_two_drivers_in_one_title_is_refused(self):
        m = self._link("Aziz Karimov and Aziz Toshev")
        self.assertIsNone(m.driver_id)
        self.assertIn("ambiguous", m.reason)

    def test_meaningless_title_is_refused(self):
        self.assertIsNone(self._link("ELD group").driver_id)

    def test_empty_title_is_refused(self):
        self.assertIsNone(self._link("").driver_id)

    def test_rare_single_name_word_is_a_suggestion_not_a_link(self):
        m = self._link("Yuldashev")
        self.assertEqual((m.driver_id, m.confidence), ("d2", "medium"))
        self.assertFalse(m.linkable)


class MatchSenderTests(unittest.TestCase):
    def test_driver_writing_identifies_the_group(self):
        m = match_sender("Rustam Yuldashev", ROSTER)
        self.assertEqual((m.driver_id, m.matched_on), ("d2", "sender"))

    def test_cyrillic_telegram_name_works(self):
        self.assertEqual(match_sender("Рустам Юлдашев", ROSTER).driver_id, "d2")

    def test_one_word_sender_name_is_refused(self):
        self.assertIsNone(match_sender("Rustam", ROSTER).driver_id)

    def test_non_driver_sender_is_refused(self):
        self.assertIsNone(match_sender("Dispatch Office", ROSTER).driver_id)


class FakeSender:
    def __init__(self, updates=None):
        self.dry_run = False
        self._updates = list(updates or [])
        self.sent = []

    def send_message(self, chat_id, text, kind="", entities=None):
        self.sent.append((str(chat_id), text))

    def get_updates(self, offset=0, timeout=0):
        u, self._updates = self._updates, []
        return u


def _msg(update_id, chat_id, title, first="", last="", text="ok"):
    return {"update_id": update_id, "message": {
        "chat": {"id": chat_id, "type": "supergroup", "title": title},
        "from": {"id": 777, "first_name": first, "last_name": last},
        "text": text,
    }}


class AutoLinkTests(unittest.TestCase):
    def test_flipped_title_auto_links_on_any_message(self):
        reg = GroupRegistry()
        process_updates(FakeSender([_msg(1, -1001, "Каримов Азиз")]), reg, ROSTER)
        self.assertEqual(reg.chat_for("d1"), "-1001")

    def test_useless_title_links_from_the_driver_who_writes(self):
        reg = GroupRegistry()
        snd = FakeSender([_msg(2, -1002, "ELD", first="Rustam", last="Yuldashev")])
        process_updates(snd, reg, ROSTER)
        self.assertEqual(reg.chat_for("d2"), "-1002")

    def test_dispatcher_writing_in_an_unknown_group_links_nothing(self):
        reg = GroupRegistry()
        snd = FakeSender([_msg(3, -1003, "Work chat", first="Dispatch", last="Office")])
        process_updates(snd, reg, ROSTER)
        self.assertEqual(reg.all(), {})
        self.assertIn("-1003", reg.pending())

    def test_ambiguous_title_is_queued_not_linked(self):
        reg = GroupRegistry()
        snd = FakeSender([_msg(4, -1004, "Aziz Karimov and Aziz Toshev")])
        process_updates(snd, reg, ROSTER)
        self.assertEqual(reg.all(), {})
        self.assertIn("ambiguous", reg.pending()["-1004"]["reason"])

    def test_medium_confidence_title_becomes_a_one_click_suggestion(self):
        reg = GroupRegistry()
        process_updates(FakeSender([_msg(5, -1005, "Yuldashev")]), reg, ROSTER)
        self.assertIsNone(reg.chat_for("d2"))
        self.assertEqual([s["driver_id"] for s in reg.suggestions()], ["d2"])

    def test_a_suggestion_for_an_already_linked_driver_is_dropped(self):
        reg = GroupRegistry()
        process_updates(FakeSender([_msg(6, -1006, "Yuldashev")]), reg, ROSTER)
        reg.register("d2", "-2000", "real group", "manual", "RUSTAM YULDASHEV")
        self.assertEqual(reg.suggestions(), [])

    def test_removed_driver_is_not_relinked_automatically(self):
        reg = GroupRegistry()
        reg.block("d1")
        process_updates(FakeSender([_msg(7, -1007, "Каримов Азиз")]), reg, ROSTER)
        self.assertIsNone(reg.chat_for("d1"))


if __name__ == "__main__":
    unittest.main()
