from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import public_sources
import auto_law
import autonomous_monitor as monitor
import telegram_delivery
import video_search


class AutonomousMonitorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "monitor.sqlite"
        self.row = {"url":"https://www.youtube.com/watch?v=TestVideo01", "description":"Проверочная подпись",
                    "published_at":"2025-02-14", "account":"example"}
        self.finding = dict(rule_id="test", title="Проверка", category="inaccurate_claim",
                            quote=self.row["description"], reason="Проверочный вывод",
                            law_url="https://lex.uz/ru/docs/123?ONDATE=14.02.2025#456",
                            law_excerpt="Проверочная норма", law_label="Проверочная статья", where="Подпись")
        with closing(self.connect()) as conn, conn:
            # Simulate upgrading a database which already contains a real search result.
            video_search.initialize(conn)
            video_search.store_item(conn, self.row, "запрос", "search")
            monitor.initialize(conn)
            telegram_delivery.initialize(conn)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def state(self, *args):
        with closing(self.connect()) as conn:
            return monitor.snapshot(conn, *args)

    def deliveries(self):
        with closing(self.connect()) as conn:
            return list(conn.execute("SELECT alert_id FROM telegram_deliveries"))

    def test_scan_uses_full_youtube_details_and_deduplicates_shorts(self):
        url = "https://www.youtube.com/watch?v=SomeShort01"
        row = {**self.row, "url":url, "title":"Название"}
        with patch("public_sources.youtube_discover", return_value=[dict(url=url,text="Обрезанный сниппет")]) as search, patch(
                "public_sources.youtube_details", return_value=[row]), patch(
                "auto_law.check_video", side_effect=lambda item, **kw: [self.finding] if item["url"] == url else []):
            monitor.search_youtube(self.connect, "key")
            monitor.search_youtube(self.connect, "key")
        search.assert_called_once()
        with closing(self.connect()) as conn, conn:
            self.assertEqual(video_search.store_item(conn, {**row,"url":"https://www.youtube.com/shorts/SomeShort01"}, "тема", "search"), 0)
            saved = monitor.public_scan_status(conn)
        self.assertEqual((saved["scanned"], saved["verified"]), (1,1))
        item, = saved["items"]
        self.assertEqual((item["status"],item["url"],item["video_title"]), ("matched",url,row["title"]))
        self.assertEqual(len(self.deliveries()), 1)

    def test_show_uses_real_search_once_and_preserves_schedule(self):
        url = "https://www.youtube.com/watch?v=NewShowId01"
        with patch.object(monitor.time, "time", return_value=1760036001), patch(
                "public_sources.youtube_discover", return_value=[dict(url=url)]), patch(
                "public_sources.youtube_details", return_value=[dict(self.row, url=url)]):
            monitor.tick(self.connect, "key", True)
            before = self.state("key",True)["discovery"]
        with patch.object(monitor.time, "time", return_value=1760036011), patch(
                "public_sources.youtube_discover", return_value=[dict(url=url),dict(url=self.row["url"])]) as search, patch(
                "public_sources.youtube_details", return_value=[dict(self.row,url=url),self.row]):
            with closing(self.connect()) as c:
                run, created = monitor.queue_show(c,"key")
                self.assertTrue(created)
                self.assertEqual(monitor.queue_show(c,"key"),(run,False))
                self.assertEqual(monitor.live_show_status(c,run)["run"]["status"],"queued")
            monitor.tick(self.connect,"key",True)
            monitor.tick(self.connect,"key",True)
            search.assert_called_once()
            with closing(self.connect()) as c:
                result = monitor.live_show_status(c,run)["run"]
                self.assertEqual((result["status"],result["origin"],result["found"]),("ready","show",2))
                self.assertEqual([i["new"] for i in result["items"]],[False,False])
                self.assertEqual(monitor.public_scan_status(c)["verified"],1)
            current = self.state("key",True)["discovery"]
            self.assertEqual((current["calls_today"],current["next_at"]),(before["calls_today"],before["next_at"]))

    def edit(self, **changes):
        with closing(self.connect()) as conn, conn:
            video_search.store_item(conn, {**self.row, **changes}, "запрос", "search")

    def test_demo_offline_and_separate(self):
        with patch("auto_law.lex_passage", side_effect=AssertionError("unexpected network")) as fetch:
            monitor.demo_run(self.path)
            monitor.demo_run(self.connect)
        state = self.state()
        fixtures = auto_law.training_examples()
        self.assertEqual(state["demo"]["checked_count"], len(fixtures))
        self.assertEqual(state["demo"]["result_count"], sum(row["expected"] for row in fixtures))
        self.assertTrue(all(item["is_demo"] and not item["url"] for item in state["demo"]["items"]))
        self.assertEqual(state["live"]["items"], [])
        self.assertEqual(self.deliveries(), [])
        fetch.assert_not_called()

    def test_local_backlog_without_search_and_manual_exclusion(self):
        with closing(self.connect()) as conn, conn:
            video_search.save_manual(conn, {"url":"https://www.facebook.com/reel/12345",
                                            "description":"Заметка редактора"})
        with patch("auto_law.check_video", return_value=[self.finding]) as check, patch(
                "public_sources.youtube_discover") as discovery:
            monitor.tick(self.connect, "configured-token", False)
            monitor.tick(self.path, "", True)
            monitor.tick(self.path, "configured-token", "0")
        self.assertEqual(check.call_count, 1)
        self.assertFalse(check.call_args.kwargs["training"])
        discovery.assert_not_called()
        item, = self.state()["live"]["items"]
        self.assertEqual((item["url"], item["quote"], item["is_demo"]),
                         (self.row["url"], self.row["description"], False))
        self.assertEqual(self.deliveries(), [(item["id"],)])
        self.assertEqual(self.state("", True)["discovery"]["status"], "no_token")

    def test_missing_source_or_valid_date_is_separate_from_checked_and_can_recover(self):
        undated = {**self.row, "url":"https://www.youtube.com/watch?v=MissingDate", "published_at":"неизвестно"}
        silent = {**self.row, "url":"https://www.youtube.com/watch?v=SilentVid01", "description":"  "}
        with closing(self.connect()) as conn, conn:
            for row in (undated, silent):
                video_search.store_item(conn, row, "запрос", "search")
        with patch("auto_law.check_video", return_value=[]) as check:
            monitor.tick(self.connect)
            state = self.state()["live"]
            self.assertEqual((state["checked_count"], state["insufficient_count"]), (1, 2))
            self.assertEqual(check.call_count, 1)
            with closing(self.connect()) as conn, conn:
                video_search.store_item(conn, {**undated, "published_at":"2025-03-12"}, "запрос", "search")
                video_search.store_item(conn, {**silent, "speech":[{"start":2, "end":4, "text":"Речь автора"}],
                                               "speech_status":"ok"}, "запрос", "search")
            monitor.tick(self.connect)
            state = self.state()["live"]
            self.assertEqual((state["checked_count"], state["insufficient_count"]), (3, 0))

    def test_unverified_sources_retry_and_recover_without_duplicate(self):
        with patch.object(monitor.time, "time", return_value=1000) as clock, patch(
                "auto_law.check_video", side_effect=[ValueError("Lex.uz недоступен"),
                                                     [{**self.finding, "law_excerpt":""}], [self.finding]]) as check:
            monitor.tick(self.connect)
            monitor.tick(self.connect)
            self.assertEqual(check.call_count, 1)
            self.assertIn("Lex.uz", self.state()["live"]["error"])
            self.assertEqual(self.deliveries(), [])
            clock.return_value += monitor.RETRY_SECONDS
            monitor.tick(self.connect)
            self.assertEqual(self.state()["live"]["result_count"], 0)
            clock.return_value += monitor.RETRY_SECONDS * 2
            monitor.tick(self.connect)
            monitor.tick(self.connect)
        self.assertEqual(self.state()["live"]["status"], "ready")
        self.assertEqual(len(self.deliveries()), 1)

    def test_shared_claim_and_caption_edit_invalidate_running_check(self):
        started, release = threading.Event(), threading.Event()

        def slow_check(row, training):
            if row["description"] == self.row["description"]:
                started.set()
                self.assertTrue(release.wait(timeout=5))
                return [self.finding]
            return []

        with patch("auto_law.check_video", side_effect=slow_check) as check, ThreadPoolExecutor(1) as pool:
            first = pool.submit(monitor.tick, self.connect)
            try:
                self.assertTrue(started.wait(timeout=5))
                monitor.tick(self.connect)
                self.assertEqual(check.call_count, 1)
                self.edit(description="Исправленная подпись")
                monitor.tick(self.connect)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(self.state()["live"]["items"], [])
        self.assertEqual(self.deliveries(), [])

    def test_failed_outbox_write_rolls_back_alert_and_reclaims_job_after_lease(self):
        with patch.object(monitor.time, "time", return_value=1000) as clock, patch(
                "auto_law.check_video", return_value=[self.finding]):
            with patch("telegram_delivery.enqueue", side_effect=sqlite3.OperationalError("interrupted")):
                with self.assertRaises(sqlite3.OperationalError):
                    monitor.tick(self.connect)
            self.assertEqual(self.state()["live"]["items"], [])
            self.assertEqual(self.deliveries(), [])
            clock.return_value += monitor.LEASE_SECONDS
            monitor.tick(self.connect)
        self.assertEqual(self.state()["live"]["result_count"], 1)
        self.assertEqual(len(self.deliveries()), 1)

    def test_edited_evidence_hides_old_notice_and_keeps_its_identity(self):
        with patch("auto_law.check_video", return_value=[self.finding]):
            monitor.tick(self.connect)
        first, = self.state()["live"]["items"]
        new_quote = "Новый текст публикации"
        self.edit(description=new_quote)
        self.assertEqual(self.state()["live"]["items"], [])
        with patch("auto_law.check_video", return_value=[{**self.finding, "quote":new_quote}]):
            monitor.tick(self.connect)
        current, = self.state()["live"]["items"]
        self.assertEqual((current["id"], current["quote"]), (first["id"], new_quote))
        self.assertEqual(self.deliveries(), [(first["id"],)])
        self.edit(description="Снятая претензия")
        with patch("auto_law.check_video", return_value=[]):
            monitor.tick(self.connect)
        self.assertEqual(self.state()["live"]["items"], [])

    def test_failed_search_consumes_budget_and_concurrent_process_cannot_repeat_it(self):
        started, release = threading.Event(), threading.Event()

        def discover(token, query, since):
            started.set()
            self.assertTrue(release.wait(timeout=5))
            raise RuntimeError("Ошибка источника secret-token")

        with patch("public_sources.youtube_discover", side_effect=discover) as search, ThreadPoolExecutor(1) as pool:
            first = pool.submit(monitor.tick, self.connect, "secret-token", True)
            try:
                self.assertTrue(started.wait(timeout=5))
                monitor.tick(self.connect, "secret-token", True)
            finally:
                release.set()
            first.result(timeout=5)
        search.assert_called_once()
        self.assertEqual(search.call_args.args[1], monitor.QUERIES[0])
        state = self.state("secret-token", True)["discovery"]
        self.assertEqual((state["calls_today"], state["status"]), (1, "error"))
        self.assertNotIn("secret-token", state["error"])

    def test_daily_limit_and_query_rotation(self):
        with patch.object(monitor.time, "time", return_value=86400 * 9000) as clock, patch.object(
                monitor, "DISCOVERY_INTERVAL", 60), patch("public_sources.youtube_discover", return_value=[]) as search:
            for _ in range(5):
                monitor.tick(self.connect, "token", True)
                clock.return_value += 61
            state = self.state("token", True)["discovery"]
            self.assertEqual(state["calls_today"], state["daily_limit"])
            self.assertEqual([call.args[1] for call in search.call_args_list], list(monitor.QUERIES[:4]))
            clock.return_value += 86400
            monitor.tick(self.connect, "token", True)
            self.assertEqual(self.state("token", True)["discovery"]["calls_today"], 1)

    def test_speech_update_rechecks_unchanged_caption_and_keeps_timecode(self):
        with patch("auto_law.check_video", return_value=[]):
            monitor.tick(self.connect)
        segments = [{"start":2.1,"end":4.2,"text":"Пример  проверяемой"},
                    {"start":4.2,"end":7.4,"text":"фразы в записи."}]
        self.edit(speech=segments, speech_status="ok")
        speech_match = {**self.finding, "quote":"Пример проверяемой фразы в записи.",
                        "evidence_kind":"speech", "where":"Речь · 00:02–00:08", "start":2.1, "end":7.4}
        with patch("auto_law.check_video", return_value=[speech_match]) as check:
            monitor.tick(self.connect)
        self.assertEqual(check.call_args.args[0]["speech"], segments)
        item, = self.state()["live"]["items"]
        self.assertEqual((item["evidence_kind"], item["start"], item["end"]), ("speech", 2.1, 7.4))

    def test_old_meta_backlog_and_alerts_are_paused(self):
        with closing(self.connect()) as conn, conn:
            row = {**self.row,"url":"https://www.instagram.com/reel/MetaCase/"}
            video_search.store_item(conn, row, "", "public_feed")
            conn.execute("INSERT INTO auto_jobs(mode,source_key) VALUES ('live','ig:MetaCase')")
            monitor._save_alert(conn, "live", "ig:MetaCase", {**self.finding, "url":row["url"]}, 1)
            monitor.initialize(conn)
        with patch("auto_law.check_video", return_value=[]) as check:
            monitor.tick(self.connect)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[0]["platform"], "youtube")
        self.assertEqual(self.state()["live"]["result_count"], 0)
        with closing(self.connect()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM auto_jobs WHERE source_key='ig:MetaCase'").fetchone(), ("skipped",))

    def test_preview_source_and_subtitles_at_four_minutes(self):
        text = auto_law.demo_cases.LEAVE_QUOTE
        source = "1\n00:04:02,000 --> 00:04:07,500\n" + text
        row = {**self.row,"platform":"youtube","title":"Ежегодный отпуск"}
        norm = auto_law.demo_cases.LEAVE
        with patch("public_sources.youtube_details", return_value=[row]), patch("auto_law._reference", return_value=(norm.url,norm.excerpt)):
            data = monitor.preview_youtube("key", self.row["url"], source)
        hit, = data["findings"]
        self.assertEqual((hit["start"],hit["end"],hit["quote"]), (242,247.5,text))
        self.assertIn("загруженного файла", hit["where"])
        with patch("public_sources.youtube_details", return_value=[{**row, "title":"Опровержение"}]):
            self.assertEqual(monitor.preview_youtube("key", self.row["url"], source)["findings"], [])
        self.assertEqual(self.deliveries(), [])
        with self.assertRaises(ValueError):
            public_sources.read_subtitles("WEBVTT\n\nнет таймкода")


if __name__ == "__main__":
    unittest.main()
