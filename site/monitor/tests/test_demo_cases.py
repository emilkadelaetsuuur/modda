"""Exercise the local case workflow without calling Instagram or Lex.uz."""

import sqlite3
import unittest
from unittest.mock import Mock

import demo_cases as cases


CLAIM = cases.TRAINING_SAMPLES[0][1]
POST = "https://www.instagram.com/reel/TestCode_1/"
EVIDENCE = {
    "quote": "Цитата, которую специалист сверил в ролике.",
    "law_url": "https://lex.uz/ru/docs/-123#456",
    "law_label": "Реквизиты проверенного документа и номер пункта",
    "law_excerpt": "Выбранный специалистом фрагмент нормы.",
    "reason": "В цитате пропущено условие из указанного пункта.",
    "assignee": "Дежурный юрист",
}


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        with self.conn:
            cases.initialize(self.conn)

    def add(self, description="", verify=None, **fields):
        row = {"url": POST, "description": description, "account": "", "published_at": "", **fields}
        with self.conn:
            return cases.add_live(self.conn, [row], "Водопользование", verify)

    def review(self, case, **changes):
        with self.conn:
            return cases.review(self.conn, {"id": case["id"], **changes})

    def test_replay_scans_control_captions_and_preserves_review_on_restart(self):
        self.assertEqual(cases.list_cases(self.conn), [])
        with self.conn:
            run = cases.replay(self.conn)
        leave, card = cases.list_cases(self.conn)
        self.assertEqual(run, {"scanned": 5, "matched": 2, "added": 2, "case_id": card["id"]})
        self.assertEqual(leave["law_url"], cases.LEAVE_LAW_URL)
        self.assertEqual(leave["law_excerpt"], cases.LEAVE_LAW_EXCERPT)
        self.assertEqual(leave["quote"], cases.LEAVE_QUOTE)
        self.assertIn("15", leave["reason"])
        self.assertIn("21", leave["reason"])
        self.assertIn("14.02.2025", leave["description"])
        self.assertTrue(all(not leave[k] for k in ("url", "account", "timecode", "media_url", "published_at", "discovered_at")))
        self.assertEqual(card["mode"], "training")
        self.assertEqual(card["quote"], CLAIM)
        self.assertEqual(card["law_url"], cases.LAW_URL)
        self.assertEqual(card["law_excerpt"], cases.LAW_EXCERPT)
        self.assertEqual(card["media_url"], "/demo-media/water.mp4")
        self.assertEqual(card["timecode"], "00:03")
        self.assertTrue(all(not card[key] for key in ("url", "account", "published_at", "discovered_at")))
        assigned = self.review(card, status="assigned", assignee="Юрист смены")
        with self.conn:
            cases.initialize(self.conn)
            self.assertEqual(cases.replay(self.conn), dict(run, added=0))
        self.assertEqual(cases.list_cases(self.conn), [leave, assigned])

    def test_only_unambiguous_live_caption_gets_source_and_verbatim_quote(self):
        for index, caption in enumerate((
            cases.TRAINING_SAMPLES[1][1], cases.TRAINING_SAMPLES[2][1],
            CLAIM[:-1] + "?", "Миф: " + CLAIM, "«" + CLAIM + "»", CLAIM + " Это неверно.",
            "Постановление № 352, мойка машины, питьевая вода.",
        )):
            self.assertEqual(self.add(caption, url=f"https://instagram.com/p/Negative_{index}/"), 1)
        positive = CLAIM.replace("№ 352", "№352").replace("за видео", "за  видеозапись")
        verify = Mock(return_value={"url": cases.LAW_URL, "passage": cases.LAW_EXCERPT})
        self.assertEqual(self.add(positive, verify=verify, published_at="2025-06-11T01:00:00Z"), 1)
        card, *others = cases.list_cases(self.conn)
        self.assertEqual(card["quote"], positive)
        self.assertEqual(card["law_url"], cases.LAW_URL)
        self.assertEqual(card["status"], "new")
        self.assertTrue(card["discovered_at"])
        self.assertTrue(all(not card[key] for key in ("timecode", "media_url")))
        for other in others:
            self.assertTrue(all(not other[key] for key in ("quote", "law_url", "reason", "timecode")))
            self.assertEqual(other["mode"], "live")

    def test_live_dedup_across_post_routes_preserves_saved_fields(self):
        self.add("Первая версия.", account="@test", published_at="2025-06-15T08:00:00Z")
        card, = cases.list_cases(self.conn)
        assigned = self.review(card, status="assigned", timecode="1:02:03", **EVIDENCE)
        verify = Mock()
        self.assertEqual(self.add(CLAIM, url="https://instagram.com/p/TestCode_1/?utm_source=x",
                                  published_at="2025-06-15T08:00:00Z", verify=verify), 0)
        verify.assert_not_called()
        self.assertEqual(cases.list_cases(self.conn), [assigned])
        self.assertEqual(assigned["url"], POST)
        self.assertEqual(assigned["account"], "@test")
        self.assertEqual(assigned["published_at"], "2025-06-15T08:00:00Z")

    def test_another_word_order_uses_the_actual_source_wording(self):
        caption = ("За видеозапись мытья автомобиля водопроводной водой по постановлению "
                   "Кабинета Министров №352 выплатят вознаграждение.")
        verify = Mock(return_value={"url": cases.LAW_URL, "passage": cases.LAW_EXCERPT})
        self.add(caption, verify=verify, published_at="2025-06-11T01:00:00Z")
        card, = cases.list_cases(self.conn)
        self.assertEqual(card["quote"], caption)
        self.assertEqual(card["law_url"], cases.LAW_URL)

    def test_leave_compares_extracted_count_and_verifies_local_date_once_before_writes(self):
        response = {"url": cases.LEAVE_LAW_URL, "passage": cases.LEAVE_LAW_EXCERPT}
        def verify(url, excerpt, day):
            self.assertFalse(self.conn.in_transaction)
            return response
        verifier = Mock(side_effect=verify)
        captions = [cases.LEAVE_QUOTE, cases.LEAVE_QUOTE.replace("15", "тридцать два").replace("дней", "дня")]
        rows = [{"description": caption, "published_at": "2025-06-14T21:00:00Z",
                 "url": f"https://www.instagram.com/p/Leave_{i}/"} for i, caption in enumerate(captions)]
        with self.conn:
            self.assertEqual(cases.add_live(self.conn, rows, "Отпуск", verifier), 2)
        verifier.assert_called_once_with("https://lex.uz/ru/acts/6257291#6263237",
                                         cases.LEAVE_LAW_EXCERPT, "15.06.2025")
        last, first = cases.list_cases(self.conn)
        self.assertIn("32", last["reason"])
        self.assertIn("21", last["reason"])
        self.assertEqual(first["law_url"], cases.LEAVE_LAW_URL)
        self.assertIn("15.06.2025", first["law_label"])
        self.assertEqual(first["quote"], cases.LEAVE_QUOTE)

    def test_no_automatic_leave_finding_outside_rule_scope(self):
        quote = cases.LEAVE_QUOTE
        items = [
            (quote.replace("15 календарных дней", "21 календарный день"), "2026-01-05T11:00:00Z"),
            (quote.replace("15 календарных дней", "двадцать один календарный день"), "2026-01-05T11:00:00Z"),
            ("Ранее " + quote, "2025-03-01T00:00:00Z"),
            ("По моему мнению, " + quote, "2025-03-01T00:00:00Z"), ("«" + quote + "»", "2025-03-01T00:00:00Z"),
            (quote.replace("в Узбекистане", "для школьников"), "2025-03-01T00:00:00Z"),
            (quote.replace("календарных", "рабочих"), "2025-03-01T00:00:00Z"),
            (quote.replace("составляет", "не составляет"), "2025-03-01T00:00:00Z"),
            (quote.replace("составляет", "будет составлять"), "2025-03-01T00:00:00Z"),
            (quote[:-1] + "?", "2025-03-01T00:00:00Z"), (quote, "2023-04-29T15:00:00Z"),
            (quote, ""), (quote, "bad date"), (quote, "2025-06-14T21:00:00"),
            (CLAIM, ""), (CLAIM, "2025-06-09T09:00:00Z"),
        ]
        verify = Mock()
        for i, (caption, stamp) in enumerate(items):
            self.add(caption, url=f"https://instagram.com/p/No_{i}/", published_at=stamp, verify=verify)
        verify.assert_not_called()
        self.assertTrue(all(not card["reason"] and not card["law_url"] for card in cases.list_cases(self.conn)))

    def test_missing_changed_or_wrong_edition_is_a_manual_candidate(self):
        responses = (
            Mock(side_effect=ValueError("Lex.uz недоступен")),
            Mock(return_value={"url": cases.LEAVE_LAW_URL, "passage": "Изменившаяся норма."}),
            Mock(return_value={"url": cases.LEAVE_LAW_URL.replace("14.02.2025", "01.01.2026"),
                               "passage": cases.LEAVE_LAW_EXCERPT}),
            Mock(return_value={"url": cases.LEAVE_LAW_URL.replace("?ONDATE=14.02.2025+00", ""),
                               "passage": cases.LEAVE_LAW_EXCERPT}),
        )
        for i, verifier in enumerate(responses):
            self.add(cases.LEAVE_QUOTE, url=f"https://instagram.com/p/Manual_{i}/",
                     published_at="2025-03-01T00:00:00Z", verify=verifier)
        for card in cases.list_cases(self.conn):
            self.assertEqual(card["status"], "new")
            self.assertEqual(card["quote"], "")
            self.assertEqual(card["law_url"], "")

    def test_evidence_draft_assignment_reassignment_and_close(self):
        self.add()
        card, = cases.list_cases(self.conn)
        self.assertEqual(cases.get_case(self.conn, str(card["id"])), card)
        with self.assertRaisesRegex(ValueError, "Цитата"):
            self.review(card, status="assigned", assignee="Юрист")
        draft = self.review(card, id=str(card["id"]), **{k: v for k, v in EVIDENCE.items() if k != "assignee"})
        self.assertEqual(draft["status"], "new")
        assigned = self.review(draft, status="assigned", assignee="Юрист")
        self.assertEqual(self.review(assigned, status="assigned", assignee="Юрист"), assigned)
        reassigned = self.review(assigned, status="assigned", assignee="Другой юрист")
        self.assertEqual(reassigned["quote"], EVIDENCE["quote"])
        with self.assertRaisesRegex(ValueError, "передана"):
            self.review(draft, status="new")
        closed = self.review(reassigned, status="closed")
        self.assertEqual(self.review(reassigned, status="closed"), closed)
        with self.assertRaisesRegex(ValueError, "закрыта"):
            self.review(reassigned, status="assigned")

    def test_empty_candidate_can_be_dismissed_without_assignment(self):
        self.add("Ссылка по широкому поисковому запросу.")
        card, = cases.list_cases(self.conn)
        self.assertEqual(self.review(card, status="closed")["status"], "closed")

    def test_invalid_fields_are_rejected_before_writing(self):
        self.add("Текст из источника.")
        card, = cases.list_cases(self.conn)
        for invalid in (
            {"timecode": "01:60"}, {"timecode": "1:99:10"}, {"timecode": "-1:05"},
            {"law_url": "https://lex.uz@elsewhere.test/docs/123"},
            {"law_url": "https://lex.uz.evil.test/docs/123"}, {"law_url": "http://lex.uz/docs/123"},
            {"law_url": "https://lex.uz/"}, {"law_url": "https://lex.uz/docs/123?ONDATE=31.02.2025"},
            {"law_url": "https://lex.uz/docs/123?redirect=https://example.org"},
            {"reason": "x" * 4001}, {"quote": ["текст"]}, {"status": "made_up"},
            {"id": -1}, {"id": True}, {"id": 2**65}, {"id": card["id"] + 1},
        ):
            with self.subTest(fields=list(invalid)), self.assertRaises(ValueError):
                self.review(card, **invalid)
        self.assertEqual(cases.list_cases(self.conn), [card])

    def test_only_instagram_post_shapes_are_accepted(self):
        for url in ("https://instagram.com/name/", "https://t.me/channel/100",
                    "https://example.org/p/test", "javascript:alert(1)", None):
            self.assertEqual(self.add(CLAIM, url=url), 0)
        self.assertEqual(cases.list_cases(self.conn), [])

    def test_caller_can_roll_back_replay_and_live_import_together(self):
        with self.assertRaises(ValueError), self.conn:
            cases.replay(self.conn)
            cases.add_live(self.conn, [{"url": POST, "description": CLAIM}], "Вода")
            cases.review(self.conn, {"id": 0})
        self.assertEqual(cases.list_cases(self.conn), [])


if __name__ == "__main__":
    unittest.main()
