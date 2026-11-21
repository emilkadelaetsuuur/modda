from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import deepfake_watch as watch
import video_facts as facts
import watch_topics


class Claims(unittest.TestCase):
    phrase="Официальный курс доллара США ЦБ Узбекистана на 14.02.2025 составляет 13 003,95 сумов."
    def check(self,caption,media=None):
        return facts.check("YouTube","Курс валют",caption,"2025-02-14T08:00:00Z",media)

    @patch.object(facts,"official_rate",return_value=(Decimal("13003.95"),"https://cbu.uz/"))
    def test_exact_matched_disputed_and_attribution(self,lookup):
        self.assertEqual(self.check(self.phrase)["findings"][0]["kind"],"matches")
        q=self.phrase.replace("003,95","004,95")
        r=self.check("",{"speech":{"segments":[{"text":q,"start":3.5,"end":7.5}]}})
        self.assertEqual(r["findings"][0]["kind"],"discrepancy")
        self.assertEqual(r["findings"][0]["time"],3.5)
        self.assertEqual(self.check("Опровержение.\n"+q)["findings"],[])
        self.assertEqual(self.check(self.phrase.replace("14.02.2025","15.02.2025"))["findings"],[])
        self.assertEqual(self.check(self.phrase.replace("003,95","000"))["findings"],[])

    @patch.object(facts,"official_rate",side_effect=ValueError())
    def test_unavailable_source_never_becomes_false(self,_):
        result=self.check(self.phrase)
        self.assertEqual(result["findings"],[])
        self.assertTrue(result["issues"])

    def test_presets_and_query_apply_to_both_platforms(self):
        self.assertRaises(ValueError,watch_topics.selection,"custom","https://example.org")
        with TemporaryDirectory() as root:
            def connect(): return sqlite3.connect(Path(root)/"test.db")
            with closing(connect()) as db:
                watch.initialize(db)
                watch.request_scan(db,True,"custom","курс доллара")
            item={"url":"https://www.youtube.com/watch?v=7fKf1234567","published_at":"2025-02-14T08:00:00Z",
                  "title":"Курс","description":self.phrase,"views":5000}
            with patch.object(watch.public_sources,"youtube_discover",return_value=[item]) as discover, \
                 patch.object(watch.public_sources,"youtube_details",return_value=[item]), \
                 patch.object(watch,"feed",return_value=[]) as feed:
                watch.scan(connect,"key")
            self.assertEqual(discover.call_args.args[1],"курс доллара")
            self.assertEqual([call.args for call in feed.call_args_list],
                             [args for c in watch.CHANNELS for args in [(c,"курс доллара")]+([(c,"")] if c in watch.FACTCHECK else [])])
            with closing(connect()) as db:
                data=watch.snapshot(db)
                self.assertEqual(data["run"]["label"],"курс доллара")
                self.assertEqual(data["current_keys"],["yt:7fKf1234567"])
                self.assertEqual(watch.request_scan(db,True,"housing"),data["run"]["id"])

    def test_priority_prefers_testable_claims(self):
        self.assertFalse(watch_topics.relevant(watch_topics.TOPICS["currency"],"Привлечено 115 миллиардов долларов"))
        self.assertTrue(watch_topics.relevant(watch_topics.TOPICS["currency"],"Dollarning rasmiy kursi"))
        self.assertGreater(watch_topics.priority("Официальный курс ЦБ составляет 12 001,95 сумов"),watch_topics.priority("Интервью"))

    def test_auto_rotates_and_custom_does_not_leak_into_schedule(self):
        with TemporaryDirectory() as root:
            with closing(sqlite3.connect(Path(root)/"test.db")) as db:
                watch.initialize(db)
                k=watch.request_scan(db,True,"custom","курс доллара")
                with db: db.execute("UPDATE df_runs SET status='finished' WHERE id=?",(k,))
                scheduled=watch.request_scan(db)
                self.assertEqual(db.execute("SELECT topic_id,origin FROM df_runs WHERE id=?",(scheduled,)).fetchone(),("auto","schedule"))
        self.assertNotEqual(watch_topics.resolve("auto","",0),watch_topics.resolve("auto","",1))
        self.assertEqual(watch_topics.resolve("auto","",0).kind,"risk")
        self.assertEqual(watch_topics.resolve("custom","курс доллара",0).youtube,("курс доллара Узбекистан","курс доллара"))
        self.assertEqual(watch_topics.resolve("custom","Toshkent kvartira",0).youtube,("Toshkent kvartira",))

    def test_uzbekistan_filter(self):
        related=watch_topics.uzbek_related
        self.assertTrue(related("Toshkentda soxta video tarqaldi","Telegram"))
        self.assertTrue(related("Курс сума вырос","Telegram"))
        self.assertFalse(related("Sun’iy intellekt bilan yaratilgan video","Telegram"))
        self.assertTrue(related("Sun’iy intellekt bilan yaratilgan video","YouTube"))
        self.assertFalse(related("Rossiyada deepfake tarqaldi","YouTube"))
        self.assertFalse(related("Сумма кредита","Telegram"))
        self.assertFalse(related("Қазақстанда жаңалық","YouTube"))
        self.assertTrue(related("Health tips","YouTube","Kun.uz"))
        self.assertFalse(related("Trump speech","YouTube","Kun.uz"))
        self.assertTrue(related("Shukurullo Isroilov bilan qanday rol o‘ynagan?","YouTube"))
        # A channel signature or link is not the subject of the post.
        self.assertFalse(related("Isroilda yangi saylov 👉 https://kun.uz/uz/1 ⚡️ @Uzbekistan24 kanaliga a’zo bo‘ling","Telegram"))
        self.assertFalse(related("🇮🇩Indoneziyada havo sifati yomonlashdi","YouTube"))
