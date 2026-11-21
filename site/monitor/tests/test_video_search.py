import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import video_search


class VideoSearchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        video_search.initialize(self.conn)

    def test_same_video_across_links_preserves_dated_evidence(self):
        url = "https://www.facebook.com/watch?v=12345"
        self.assertEqual(video_search.save_manual(self.conn, {"url":url}), 1)
        found = {"url":"https://www.facebook.com/reel/12345", "account":"Example",
                 "description":"Raida halol savdo", "published_at":"2026-11-18"}
        self.assertEqual(video_search.store_item(self.conn, found, "Raida", "search"), 0)
        first, = video_search.list_items(self.conn)
        self.assertEqual(first["reference_url"], video_search.RAIDA_SOURCE)
        self.assertEqual(first["published_at"], "2026-11-18")
        self.assertEqual(first["status"], "candidate")
        video_search.review(self.conn, {"id":str(first["id"]), "status":"reviewed", "reason":"Сверили фрагмент.",
                                        "evidence_url":video_search.RAIDA_SOURCE})
        video_search.store_item(self.conn, {**found,"description":"Изменённое описание"}, "другой запрос", "search")
        stored, = video_search.list_items(self.conn)
        self.assertEqual(stored["description"], first["description"])
        self.assertEqual(stored["reason"], "Сверили фрагмент.")

    def test_free_routes_and_rebuttal(self):
        links = video_search.external_links("halol savdo")
        params = parse_qs(urlsplit(links["ads"]).query)
        self.assertEqual((params["q"],params["country"],params["media_type"]), (["halol savdo"],["UZ"],["video"]))
        self.assertEqual(urlsplit(links["facebook_native"]).path, "/search/videos/")
        self.assertEqual(urlsplit(links["instagram_native"]).hostname, "www.instagram.com")
        lead, ref = video_search.caption_lead({"method":"search","description":"Опровержение рекламы Raida halol savdo"})
        self.assertIn("предупреждения", lead)
        self.assertFalse(ref)
        with self.assertRaises(ValueError):
            video_search.save_manual(self.conn, {"url":"https://www.facebook.com/page/posts/12345"})

    def test_combined_search_preserves_successful_platform(self):
        row = {"url":"https://www.instagram.com/reel/Video123/", "description":"", "published_at":"", "account":""}
        with patch.object(video_search, "discover_instagram", return_value=[row]) as ig, patch.object(
                video_search, "discover_facebook_videos", side_effect=video_search.FacebookDiscoveryError("Нет ответа")):
            rows, errors = video_search.discover("mock", "Raida", "both")
        self.assertEqual(rows, [row])
        self.assertEqual(errors, ["Facebook: Нет ответа"])
        ig.assert_called_once_with("mock", "Raida", "reels")
