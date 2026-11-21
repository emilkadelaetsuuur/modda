import unittest
from unittest.mock import call, patch

from law_source import lex_passage


class LexPassageTest(unittest.TestCase):
    page = """
        <title>Проверочный документ</title>
        <div class="ACT_TEXT"><div id="23" name="23">2. Первый <a href="#">фрагмент</a> нормы.</div></div>
        <div class="lx_elem ACT_TEXT"><div id="24" name="24">3. Второй фрагмент нормы.</div></div>
    """
    calendar = """
        <div id="lx_lact_num_top">от 06.01.2021 г.</div>
        <div id="lx_var_t" class="dropdown-menu lx_date_ddm">
          <div class="dropdown-menu__item lx_date_selected stopProp">12.01.2026</div>
          <div class="dropdown-menu__item lx_date_link" onclick="lxOpenUrl('/ru/docs/100?ONDATE=21.10.2025 01')">21.10.2025 01</div>
          <div class="dropdown-menu__item lx_date_link" onclick="lxOpenUrl('/ru/docs/100?ONDATE=21.10.2025')">21.10.2025</div>
          <div class="dropdown-menu__item lx_date_link" onclick="lxOpenUrl('/ru/docs/100?ONDATE=06.01.2021')">06.01.2021</div>
        </div>
        <div id="other"><div class="lx_date_link" onclick="lxOpenUrl('/ru/docs/100?ONDATE2=13.11.2025&amp;action=compare')">13.11.2025</div></div>
        <div class="docHeader__item-label">Дата вступления в силу</div>
        <div class="docHeader__item-value d-flex">07.01.2021</div>
    """

    @patch("law_source._load")
    def test_exact_anchor_and_edition(self, download):
        download.side_effect = [self.calendar, self.page]
        ref = lex_passage("https://www.lex.uz/uz/acts/100#23", "Первый фрагмент", "20.11.2026")
        self.assertEqual(download.call_args_list, [call("https://lex.uz/ru/docs/100"),
                                                   call("https://lex.uz/uz/acts/100?ONDATE=12.01.2026")])
        self.assertEqual(ref["passage"], "2. Первый фрагмент нормы.")
        self.assertEqual(ref["url"], "https://lex.uz/uz/acts/100?ONDATE=12.01.2026#23")
        self.assertTrue(ref["checked_at"])

    @patch("law_source._load")
    def test_uses_highest_same_day_revision_even_between_revision_dates(self, download):
        for date in ("21.10.2025", "20.11.2025"):
            with self.subTest(date=date):
                download.side_effect = [self.calendar, self.page]
                ref = lex_passage("https://lex.uz/uz/mact/100#23", "", date)
                self.assertEqual(ref["url"], "https://lex.uz/uz/mact/100?ONDATE=21.10.2025+01#23")

    @patch("law_source._load")
    def test_explicit_revision_takes_precedence_and_retains_its_suffix(self, download):
        download.return_value = self.page
        for original, expected in (("+00", "+00"), ("%2B01", "+01"), ("%2002", "+02"), ("", "")):
            with self.subTest(original=original):
                download.reset_mock()
                base = "https://lex.uz/acts/100?ONDATE=21.10.2025"
                ref = lex_passage(base + original + "#23", "", "20.11.2026")
                download.assert_called_once_with(base + expected)
                self.assertEqual(ref["url"], base + expected + "#23")

    @patch("law_source._load")
    def test_refuses_unverifiable_calendar_or_inapplicable_act(self, download):
        repeal = self.calendar.replace("от 06.01.2021 г.", "<span>Акт утратил силу&nbsp;13.11.2025</span>")
        for page, date, error in ((self.calendar, "06.01.2021", "не вступил"),
                                  (repeal, "13.11.2025", "утратил силу"),
                                  (self.page, "20.11.2026", "ONDATE"),
                                  (self.calendar.replace("?ONDATE=21.10.2025 01", "?ONDATE=21.10.2025 02"),
                                   "21.11.2025", "ONDATE")):
            with self.subTest(date=date, error=error):
                download.reset_mock()
                download.return_value = page
                with self.assertRaisesRegex(ValueError, error):
                    lex_passage("https://lex.uz/acts/100#23", "", date)
                download.assert_called_once_with("https://lex.uz/ru/docs/100")

    @patch("law_source._load")
    def test_rejects_wrong_or_ambiguous_quotes(self, download):
        download.return_value = self.page
        for url, phrase in [("https://lex.uz/docs/100#24", "Первый фрагмент"),
                            ("https://lex.uz/docs/100", "фрагмент нормы.")]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                lex_passage(url, phrase)
        resolved = lex_passage("https://lex.uz/docs/100", "Второй фрагмент нормы")
        self.assertTrue(resolved["url"].endswith("#24"))

    @patch("law_source._load")
    def test_rejects_external_url_before_fetch(self, download):
        with self.assertRaises(ValueError):
            lex_passage("https://example.org/docs/100#23")
        download.assert_not_called()
