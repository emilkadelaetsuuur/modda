"""Offline contract tests for the bounded Instagram discovery request."""

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from demo_instagram import InstagramDiscoveryError, discover_instagram


class InstagramDiscoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("demo_instagram.http.client.HTTPSConnection")
        self.addCleanup(patcher.stop)
        self.connect = patcher.start()
        self.connection = self.connect.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.respond([])

    def respond(self, rows):
        self.response.read.return_value = json.dumps(rows).encode()

    def test_keyword_sends_one_bounded_authenticated_request(self):
        self.assertEqual(discover_instagram("secret", "закон Узбекистана", "reels"), [])
        self.connect.assert_called_once_with("api.apify.com", timeout=105)
        self.connection.request.assert_called_once()
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args[0], "POST")
        url = urlsplit(args[1])
        self.assertEqual(url.path, "/v2/actors/apify~instagram-hashtag-scraper/run-sync-get-dataset-items")
        params = parse_qs(url.query)
        self.assertEqual(params["limit"], ["5"])
        self.assertEqual(params["timeout"], ["90"])
        self.assertEqual(params["maxTotalChargeUsd"], ["0.05"])
        self.assertEqual(params["restartOnError"], ["false"])
        self.assertNotIn("secret", args[1] + kwargs["body"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(json.loads(kwargs["body"]), {
            "hashtags": ["закон Узбекистана"], "resultsLimit": 5, "keywordSearch": True, "resultsType": "reels",
        })
        self.connection.close.assert_called_once()

    def test_hashtag_and_multiple_result_schemas(self):
        self.respond([
            {"url": "https://instagram.com/p/First_1/?igsh=s", "ownerUsername": "source",
             "timestamp": "2025-11-07T20:56:47.000Z", "caption": "О законе", "productType": "clips",
             "latestComments": [{"ownerUsername": "commenter"}]},
            {"shortCode": "First_1", "caption": "copy"},
            {"media": {"code": "Second-2", "caption": {"text": "Подпись"}, "user": {"username": "other"},
                       "taken_at": 1704067200, "product_type": "feed"}},
            {"shortCode": "Third", "childPosts": [{"ownerUsername": "child", "timestamp": "2026-01-01"}]},
        ])
        rows = discover_instagram("x", "#қонун", "posts")
        payload = json.loads(self.connection.request.call_args.kwargs["body"])
        self.assertEqual(payload["hashtags"], ["қонун"])
        self.assertFalse(payload["keywordSearch"])
        self.assertEqual(rows, [
            {"url": "https://www.instagram.com/reel/First_1/", "account": "source",
             "description": "О законе", "published_at": "2025-11-07T20:56:47+00:00"},
            {"url": "https://www.instagram.com/p/Second-2/", "account": "other",
             "description": "Подпись", "published_at": "2024-01-01T00:00:00+00:00"},
            {"url": "https://www.instagram.com/p/Third/", "account": "", "description": "", "published_at": ""},
        ])

    def test_counts_up_to_five_and_request_format_supplies_reel_path(self):
        self.respond([{"shortCode": f"Clip{i}"} for i in range(8)])
        rows = discover_instagram("x", "поиск", "reels")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["url"], "https://www.instagram.com/reel/Clip0/")

    def test_failure_rows_and_unsafe_urls_are_not_empty_searches(self):
        for rows in ([{"error": "no_items"}], [{"error": "upstream"}], {"error": {}},
                     [{"url": "https://www.instagram.com@other.invalid/p/NotAnInstaLink/"}],
                     [{"url": "https://www.instagram.com/explore/tags/tag/"}]):
            with self.subTest(rows=rows):
                self.respond(rows)
                with self.assertRaises(InstagramDiscoveryError):
                    discover_instagram("x", "поиск", "posts")

    def test_http_failure_and_socket_timeout_do_not_retry_or_echo_secrets(self):
        self.response.status = 403
        self.response.read.return_value = b'secret'
        with self.assertRaisesRegex(InstagramDiscoveryError, "доступ") as error:
            discover_instagram("secret", "topic", "posts")
        self.assertNotIn("secret", str(error.exception))
        self.connection.request.assert_called_once()
        self.connection.request.side_effect = TimeoutError("secret")
        with self.assertRaisesRegex(InstagramDiscoveryError, "ожидания") as error:
            discover_instagram("secret", "topic", "posts")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.connection.request.call_count, 2)

    def test_invalid_arguments_stay_local(self):
        for args in (("", "q", "posts"), ("x", "", "reels"), ("x", "#two tags", "reels"), ("x", "q", "both")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                discover_instagram(*args)
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
