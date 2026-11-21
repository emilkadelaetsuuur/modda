"""Offline contract tests for Apify's public Facebook video search."""

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from facebook_video import FacebookDiscoveryError, discover_facebook_videos


class FacebookVideoTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("facebook_video.http.client.HTTPSConnection")
        self.addCleanup(patcher.stop)
        self.connect = patcher.start()
        self.connection = self.connect.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.respond([])

    def respond(self, rows):
        self.response.read.return_value = json.dumps(rows).encode()

    def test_one_keyword_search_with_cost_result_and_time_limits(self):
        self.assertEqual(discover_facebook_videos("test-key", "закон Узбекистана"), [])
        self.connect.assert_called_once_with("api.apify.com", timeout=105)
        self.connection.request.assert_called_once()
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args[0], "POST")
        url = urlsplit(args[1])
        self.assertEqual(url.path, "/v2/actors/apify~facebook-video-search-scraper/run-sync-get-dataset-items")
        params = parse_qs(url.query)
        self.assertEqual(params["limit"], ["5"])
        self.assertEqual(params["timeout"], ["90"])
        self.assertEqual(params["maxTotalChargeUsd"], ["0.05"])
        self.assertEqual(params["restartOnError"], ["false"])
        self.assertEqual(json.loads(kwargs["body"]), {"searchTerm": "закон Узбекистана", "resultsLimit": 5})
        self.assertNotIn("test-key", args[1] + kwargs["body"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.connection.close.assert_called_once()

    def test_documented_media_shapes_dates_and_deduplication(self):
        search_url = "https://www.facebook.com/watch/search?q=calendar"
        self.respond([
            {"facebookUrl": search_url, "videoUrl": "https://www.facebook.com/news.uz/videos/1234/?ref=home",
             "video": {"id": "1234"}, "video_owner_profile": {"name": "Редакция"},
             "save_description": "Изменения в законе", "title": "Новости", "relative_time_string": "Feb 29, 2024 · 39 views"},
            {"videoUrl": "https://facebook.com/reel/1234/", "title": "Duplicate"},
            {"videoUrl": "https://m.facebook.com/reel/5678/", "save_description": "", "title": "Правила",
             "relative_time_string": "May 23 · 210K views", "video_owner_profile": {"__typename": "InstagramUserV2"}},
            {"videoUrl": "https://www.facebook.com/video/embed?video_id=8910", "relative_time_string": "3 days ago"},
        ])
        rows = discover_facebook_videos("x", "#қонун")
        self.assertEqual(rows, [
            {"url": "https://www.facebook.com/news.uz/videos/1234", "account": "Редакция",
             "description": "Изменения в законе", "published_at": "2024-02-29"},
            {"url": "https://www.facebook.com/reel/5678", "account": "", "description": "Правила", "published_at": ""},
            {"url": "https://www.facebook.com/watch?v=8910", "account": "", "description": "", "published_at": ""},
        ])

    def test_non_video_and_untrusted_locations_are_skipped(self):
        search = "https://www.facebook.com/watch/search?q=calendar"
        for candidate in (search, "https://www.facebook.com/groups/123/posts/456/",
                          "https://www.facebook.com/news/posts/456",
                          "https://www.facebook.com.evil.test/reel/123", "https://www.facebook.com@evil.test/reel/123",
                          "https://www.facebook.com/watch?v=1&v=2", "https://www.facebook.com/../videos/123",
                          "https://www.facebook.com/groups/123", "https://www.facebook.com/profile.php?id=456"):
            with self.subTest(candidate=candidate):
                self.respond([{"facebookUrl": search, "video": {"id": "456"}, "videoUrl": candidate}])
                self.assertEqual(discover_facebook_videos("x", "фраза"), [])
        self.respond([{"url": "https://www.facebook.com/reel/123", "inputUrl": search},
                      {"videoUrl": "https://www.facebook.com/watch?v=123", "video": {"id": "456"}}])
        self.assertEqual(discover_facebook_videos("x", "фраза"), [])

    def test_five_rows_max_online_and_offline_no_date_guess(self):
        self.respond([{"videoUrl": f"https://facebook.com/watch?v={i+1}",
                       "relative_time_string": "Feb 29, 2023"} for i in range(8)])
        rows = discover_facebook_videos("x", "фраза")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["published_at"] == "" for row in rows))

    def test_bad_input_stays_local(self):
        for args in (("", "фраза"), ("x", ""), ("x", "z" * 181), ("x", "a\nb"), ("bad\nkey", "test")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                discover_facebook_videos(*args)
        self.connect.assert_not_called()

    def test_provider_failures_are_distinct_from_empty_no_retry_or_key_in_error(self):
        for status in (401, 402, 403, 429, 500, 504):
            with self.subTest(status=status):
                self.response.status = status
                self.connection.request.reset_mock()
                self.response.read.return_value = b"not-for-display"
                with self.assertRaises(FacebookDiscoveryError) as caught:
                    discover_facebook_videos("not-for-display", "topic")
                self.assertNotIn("not-for-display", str(caught.exception))
                self.connection.request.assert_called_once()
        self.response.status = 200
        for rows in ({"items": []}, [{"error": "not-for-display"}], ["not-for-display"]):
            with self.subTest(rows=rows):
                self.respond(rows)
                with self.assertRaises(FacebookDiscoveryError) as caught:
                    discover_facebook_videos("not-for-display", "topic")
                self.assertNotIn("not-for-display", str(caught.exception))
        self.connection.request.side_effect = TimeoutError("not-for-display")
        with self.assertRaisesRegex(FacebookDiscoveryError, "ожидания") as caught:
            discover_facebook_videos("not-for-display", "topic")
        self.assertNotIn("not-for-display", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
