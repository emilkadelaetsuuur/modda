import json
import unittest
from unittest.mock import patch

import auto_law


class AutoLawTest(unittest.TestCase):
    def setUp(self):
        self.rows = {row["key"]: row for row in auto_law.training_examples()}

    def test_listing_price_is_a_review_lead_with_dated_law(self):
        listing = "Ташкент\nКомнат 2\n55м²\nЦена: 70.000$\nhttps://example.com/?ref=abc"
        row = {"description": listing, "published_at": "2026-01-27"}
        norm = auto_law._PRICE_CURRENCY
        with patch("auto_law.lex_passage", return_value={"url": "https://lex.uz/ru/docs/6052633?ONDATE=12.03.2025#6054273",
                   "passage": norm.excerpt}) as verify:
            found, = auto_law.check_video(row)
            self.assertEqual((found["quote"], found["category"]), ("Цена: 70.000$", "advertising_review"))
            self.assertIn("статьи 2", found["reason"])
            self.assertEqual(verify.call_args.args[-1], "27.01.2026")
            for text in (listing.replace("Ташкент", "Алматы"), listing.replace("70.000$", "700 000 сум"),
                         "Осторожно, мошенники!\n" + listing):
                self.assertFalse(auto_law.check_video({**row, "description": text}))

    def test_training_captions_have_independent_inputs_and_expected_outcomes(self):
        with patch("auto_law.lex_passage") as verify:
            for row in self.rows.values():
                with self.subTest(key=row["key"]):
                    found = auto_law.check_video(row, training=True)
                    self.assertEqual(len(found), row["expected"])
                    for item in found:
                        source = (" ".join(part["text"] for part in row["speech"])
                                  if item["evidence_kind"] == "speech" else row["description"])
                        self.assertIn(item["quote"], source)
                        self.assertTrue(item["law_url"].startswith("https://lex.uz/"))
                        self.assertIn("ONDATE=", item["law_url"])
                        self.assertIn("14.02.2025", item["law_label"])
                        for field in ("rule_id", "category", "title", "where", "reason", "law_excerpt"):
                            self.assertTrue(item[field])
            verify.assert_not_called()

    def test_variants_match_verbatim_with_minimal_neighbouring_context(self):
        russian = ("Для инвесторов в Узбекистане. Инвестируйте в корпоративные облигации. "
                   "Ожидаемая доходность — 12.5% годовых.")
        uzbek = "Ўзбекистон аҳолиси учун! Облигацияларимизни сотиб олинг. Йилига 23 фоиз фойда оласиз."
        latin = "O'zbekistonliklarga taklif! Aksiyalarga mablag' kiriting. 20% sof daromad kafolatlanadi."
        for caption, anchor in ((russian, "6054890"), (uzbek, "6053616"), (latin, "6053616")):
            row = dict(self.rows["ad-ru"], description="Здравствуйте. " + caption + " Подробнее в профиле.")
            with self.subTest(caption=caption):
                hit, = auto_law.check_video(row, training=True)
                self.assertEqual(hit["quote"], caption)
                self.assertTrue(hit["law_url"].endswith("#" + anchor))
                self.assertEqual(hit["category"], "potential_violation")

    def test_search_terms_metadata_or_an_unrelated_product_cannot_trigger_a_finding(self):
        original = self.rows["ad-ru"]["description"]
        alternatives = (
            original.replace("Узбекистана", "Казахстана"),
            original.replace("Купите наши обыкновенные акции", "Мы изучили рынок акций"),
            original.replace("наши обыкновенные акции", "наш учебник об акциях"),
            original.replace("Гарантируем доход 30% в месяц", "Скидка на обслуживание 30%"),
            original.replace("Гарантируем доход 30% в месяц", "Доход в 2021 году составил 30%"),
            original.replace("акции", "товары по акции"),
            original.replace("30%", "отличный"),
            original.replace("Гарантируем", "Не гарантируем"),
            "Речь о предстоящем вкладе. " + original,
            "Автор написал: " + original,
            "Допустим, текст такой. " + original,
            original + " Эти слова — ложь.",
            "Ожидаемая доходность 20%. " + "Другая тема. " * 7 + "Для жителей Узбекистана. Купите акции.",
            "Купите облигации. У этого ролика тысячи просмотров.",
            "",
        )
        for caption in alternatives:
            with self.subTest(caption=caption), patch("auto_law.lex_passage") as verify:
                self.assertEqual(auto_law.check_video(dict(self.rows["ad-ru"], description=caption,
                                                           query=original, account=original)), [])
                verify.assert_not_called()

    def test_uzbek_controls_with_different_apostrophes_and_reported_speech(self):
        invitation = self.rows["ad-uz"]["description"].replace("‘", "ʻ")
        for caption in ("Misol: " + invitation, "Firibgarlar aytmoqda: " + invitation,
                        "«" + invitation + "»", invitation + " Bu yolg‘on.",
                        invitation.replace("olasiz", "olmaydi"), invitation + " Ishonmang."):
            with self.subTest(caption=caption):
                self.assertEqual(auto_law.check_video(dict(self.rows["ad-uz"], description=caption), training=True), [])

    def test_live_checks_text_and_scope_in_same_edition(self):
        norm = auto_law._AD_RU
        anchor, scope = auto_law._AD_SCOPE[norm]
        row = dict(self.rows["ad-ru"], kind="reel", published_at="2025-02-13T20:10:00Z")
        with patch("auto_law.lex_passage") as verify:
            verify.side_effect = [
                dict(url=norm.url, passage=norm.excerpt),
                dict(url=norm.url.split("#")[0] + "#" + anchor, passage=scope),
            ]
            hit, = auto_law.check_video(row)
            first, second = verify.call_args_list
            self.assertEqual(first.args, ("https://lex.uz/ru/docs/6052633#6054890", norm.excerpt, "14.02.2025"))
            self.assertEqual(second.args, (norm.url.split("#")[0] + "#" + anchor, scope))
            self.assertEqual(hit["law_excerpt"], norm.excerpt)
            self.assertEqual(hit["where"], "Текст публикации")
            self.assertEqual(hit["law_url"], norm.url)

    def test_changes_to_law_or_lex_outage_are_visible_to_the_caller(self):
        for answers in ([ValueError("Lex.uz временно недоступен.")],
                        [dict(passage=auto_law._AD_RU.excerpt + " Дополнение.")],
                        [dict(passage=auto_law._AD_RU.excerpt, url=auto_law._AD_RU.url),
                         dict(passage="Область действия изменилась.")]):
            with self.subTest(answers=answers), patch("auto_law.lex_passage", side_effect=answers):
                with self.assertRaises(ValueError):
                    auto_law.check_video(self.rows["ad-ru"])

    def test_unreliable_dates_and_editor_notes_are_never_evidence(self):
        with patch("auto_law.lex_passage") as verify:
            for bad in ("", None, "2025-02-30", "2025-02-14T00:00:00", "2022-09-08", "9999-12-31"):
                self.assertEqual(auto_law.check_video(dict(self.rows["ad-ru"], published_at=bad)), [])
            self.assertEqual(auto_law.check_video(dict(self.rows["ad-ru"], method="manual")), [])
            verify.assert_not_called()

    def test_training_archives_are_bound_to_training_sources_and_date(self):
        for extra in (dict(kind="reel"), dict(url="https://www.instagram.com/reel/a/"),
                      dict(published_at="2025-02-15")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                auto_law.check_video(dict(self.rows["ad-ru"], **extra), training=True)

    def test_spoken_quote_prefers_localized_transcript_and_honours_rebuttals(self):
        row = self.rows["spoken-ad"]
        for caption in ("", self.rows["ad-ru"]["description"]):
            combined = dict(row, description=caption, speech=json.dumps(row["speech"]))
            hit, = auto_law.check_video(combined, training=True)
            self.assertEqual(hit["evidence_kind"], "speech")
            self.assertEqual((hit["start"], hit["end"]), (2.7, 11.64))
            self.assertIn("00:02.70–00:11.64", hit["where"])
            self.assertTrue(auto_law.can_check(combined))
        self.assertFalse(auto_law.check_video(dict(row, description="Репортаж о том, как действуют мошенники."),
                                              training=True))
        reply = dict(start=12, end=17, text="Это неправда, не покупайте эти акции.")
        qualified = dict(row, description=self.rows["ad-ru"]["description"], speech=[*row["speech"], reply])
        self.assertFalse(auto_law.check_video(qualified, training=True))

    def test_missing_evidence_and_invalid_speech_ranges(self):
        for speech in ([], "not JSON", [{}], [dict(start=-1, end=2, text="...")],
                       [dict(start=False, end=0.5, text="...")], [dict(start=5, end=2, text="...")]):
            row = dict(self.rows["spoken-ad"], speech=speech, description="")
            self.assertFalse(auto_law.can_check(row))
            self.assertFalse(auto_law.check_video(row, training=True))
        self.assertFalse(auto_law.can_check(dict(self.rows["spoken-ad"], published_at="")))

    def test_labour_claim_is_distinct_from_a_suspected_advertising_violation(self):
        row = self.rows["leave"]
        hit, = auto_law.check_video(dict(row, description=row["description"].replace("15", "двадцать два")),
                                    training=True)
        self.assertEqual(hit["category"], "inaccurate_claim")
        self.assertIn("22", hit["reason"])
        self.assertIn("21", hit["reason"])
        self.assertIn("217", hit["law_label"])
        for caption in ("Миф: " + row["description"], row["description"].replace("календарных", "рабочих")):
            self.assertEqual(auto_law.check_video(dict(row, description=caption), training=True), [])


def _dated(norm, day, training):
    base, _, anchor = norm.url.partition("#")
    return f"{base.split('?')[0]}?ONDATE={day:%d.%m.%Y}#{anchor}", norm.excerpt


@patch("auto_law._reference", side_effect=_dated)
class RealPublicationTest(unittest.TestCase):
    """Patterns from live YouTube results: hedges elsewhere in a long video must not hide a claim."""

    def row(self, title="", description="", speech="", frames=()):
        return dict(platform="youtube", method="search", published_at="2026-11-12T08:00:00Z",
                    video_title=title, description=description, speech=[],
                    media_report={"speech": {"text": speech, "segments": []},
                                  "frames": [dict(start=2 * i, text=t) for i, t in enumerate(frames)]})

    def test_listing_survives_questions_and_negations_in_speech(self, _):
        description = ("Продажа дома.\n\nЛокация: Юнусабадский район.\n• Общая площадь дома: 450 м².\n\n"
                       "Цена: 599 999 $.\n\nКонтакт:\n97 403 65 22.")
        speech = "Это одна из лучших локаций, правда? Но есть одно но: дом не новый."
        hit, = auto_law.check_video(self.row("Продажа дома", description, speech))
        self.assertEqual((hit["quote"], hit["where"].split(",")[0]), ("Цена: 599 999 $.", "Описание YouTube"))
        bulleted = description.replace("Цена: 599 999 $.", "• Цена: 80 000 $.")
        hit, = auto_law.check_video(self.row("Продажа дома", bulleted, speech))
        self.assertEqual(hit["quote"], "Цена: 80 000 $.")

    def test_price_in_title_or_frame_uses_listing_details_from_the_whole_video(self, _):
        title = "4-комнатная квартира в Юнусабаде, Ташкент — ЖК NUR, 240 000 $"
        hit, = auto_law.check_video(self.row(title, "Продаётся квартира, 113 м², 6 этаж."))
        self.assertEqual((hit["quote"], hit["evidence_kind"]), ("240 000 $", "title"))
        frames = ["Голден Хаус\n\n2 КВ с балконом,\n\n73500$\n\n#99898 1270357"]
        hit, = auto_law.check_video(self.row("#новостройка #ташкент #квартира", "", "", frames))
        self.assertEqual((hit["quote"], hit["evidence_kind"], hit["start"]), ("73500$", "frame", 0))

    def test_ocr_noise_comparisons_and_foreign_listings_are_not_prices(self, _):
        context = "#новостройка #ташкент #квартира продаётся"
        for frame in ("Таргет $120 Контекст $90 SEO $70", "Цена 0000$ 998872181555", "$\n\n95-039-99-66",
                      "Душанбе $70160 Бишкек $112320 Ташкент $118000"):
            with self.subTest(frame=frame):
                self.assertEqual(auto_law.check_video(self.row(context, "", "", [frame])), [])
        kyrgyz = "2 комнатная квартира 66м2 цена 1250 $ за м2 Тел: 0501 007 463 #ош"
        self.assertEqual(auto_law.check_video(self.row(kyrgyz, "Ошский мк-р\nЦена: 1250$ за м²")), [])

    def test_conventional_units_are_reported_as_a_dollar_equivalent(self, _):
        description = "Продаётся 4-комнатная квартира в Юнусабаде, 113 м².\nЦена: 240 000 у.е.\n📞 +998 93 599-97-99"
        hit, = auto_law.check_video(self.row("Квартира в ЖК NUR", description))
        self.assertEqual((hit["quote"], hit["evidence_kind"]), ("Цена: 240 000 у.е.", "caption"))
        self.assertIn("условных единицах", hit["reason"])
        self.assertIn("статьи 2", hit["reason"])
        self.assertIn("у.е.", hit["title"])
        for frame, quote in (("ЮНУСАБАД 158 000 y.e.", "158 000 y.e."), ("— 240 000у.е. | м2", "240 000у.е.")):
            hit, = auto_law.check_video(self.row("Elegant Home — продажа квартир в Ташкенте", "", "", [frame]))
            self.assertEqual(hit["quote"], quote)
        dollars, = auto_law.check_video(self.row("Квартира в ЖК NUR", description.replace("у.е.", "$")))
        self.assertNotIn("условных", dollars["reason"])
        # A dollar sign in a later source outweighs "у.е." found first on a frame.
        both, = auto_law.check_video(self.row("4-комнатная квартира, Ташкент — 240 000 $", description, "",
                                              ["— 240 000у.е. | м2"]))
        self.assertEqual((both["quote"], both["evidence_kind"]), ("240 000 $", "title"))
        self.assertNotIn("условных", both["reason"])
        for text in ("Цена: 240 000 уе", "Цена: 5 у.ед. за услугу"):
            self.assertEqual(auto_law.check_video(self.row("Квартира в ЖК NUR", description.replace(
                "Цена: 240 000 у.е.", text))), [])

    def test_debunk_framing_still_excludes_the_whole_video(self, _):
        row = self.row("Квартира в Ташкенте за 100 $? Фейки и ИИ-объявления на OLX",
                       "Продаётся квартира, 50 м²\nЦена: 100 $", "превосходная квартира за 100 долларов")
        self.assertEqual(auto_law.check_video(row), [])

    def test_spoken_offer_is_found_despite_disclaimers_elsewhere(self, _):
        description = "Не является инвестиционной рекомендацией. Как выбирать акции? Разбираем по шагам."
        cues = [dict(start=40, end=44, text="Для жителей Узбекистана!"),
                dict(start=44, end=47, text="Купите наши обыкновенные акции."),
                dict(start=47, end=51, text="Гарантируем доход 30% в месяц.")]
        row = dict(self.row("Как выбирать акции в Узбекистане", description), speech=cues,
                   subtitle_preview=True, media_report={})
        hit, = auto_law.check_video(row)
        self.assertEqual((hit["rule_id"], hit["evidence_kind"], hit["start"]), ("ad-securities-return", "speech", 40))
        rebuttal = [*cues, dict(start=51, end=54, text="Так пишут мошенники, не верьте.")]
        self.assertEqual(auto_law.check_video(dict(row, speech=rebuttal)), [])
