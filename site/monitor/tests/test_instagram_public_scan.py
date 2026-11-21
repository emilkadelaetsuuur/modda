import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import instagram_public_scan as scan


def message(number, links):
    body = "".join(f'<a href="{url}">Непроверенный текст из Telegram</a>' for url in links)
    return f'<div data-post="{scan.CHANNEL}/{number}"><div class="tgme_widget_message_text">{body}</div></div>'


def profile(media):
    context = {"username": scan.ACCOUNT, "type": "Profile", "graphql_media": [
        {"shortcode_media": {"shortcode": code, "is_video": video}} for code, video in media
    ]}
    return '<script>initialize(' + json.dumps({"contextJSON": json.dumps({"context": context})}) + ')</script>'


class PublicScanTests(unittest.TestCase):
    def test_filters_channel_links_and_uses_only_instagram_for_facts(self):
        reel = "https://www.instagram.com/reel/TestCode/"
        page = message(1, [reel, reel + "?tracking=1", "https://www.instagram.com@outside.test/reel/TestCode/",
                           "https://example.com/reel/TestCode/", "https://www.instagram.com/p/TestCode/"])
        page += message(2, ["https://instagram.com/account/reel/TestCode/?ref=abc"])
        page += '<div data-post="another_channel/2"><div class="tgme_widget_message_text">'
        page += '<a href="https://www.instagram.com/reel/OtherCode/">link</a></div></div>'

        def get(url):
            if url == scan.DISCOVERY_URL:
                return page
            if url == scan.PROFILE_URL:
                return profile([])
            if url == reel:
                return ('<meta property="og:url" content="https://www.instagram.com/account/reel/TestCode/">'
                        '<meta property="og:description" content="23 likes, 2 comments - account on February 28, 2026: ...">')
            self.assertEqual(parse_qs(urlsplit(url).query), {"url": [reel]})
            return json.dumps({"provider_name": "Instagram", "title": "Подпись из Instagram", "author_name": "account"})

        with patch.object(scan, "_get", side_effect=get):
            report = scan.search_public_reels()
        self.assertEqual(report, {
            "discovery_url": scan.DISCOVERY_URL, "scanned": 1, "verified": 1, "errors": [],
            "rows": [{"url": reel, "account": "account", "description": "Подпись из Instagram",
                      "published_at": "2026-02-28", "source_url": f"https://t.me/{scan.CHANNEL}/2"}],
        })

    def test_bad_candidate_does_not_hide_remaining_rows_and_missing_dates_stay_empty(self):
        reels = [f"https://www.instagram.com/reel/Test{i:03d}/" for i in range(11)]

        def get(url):
            if url == scan.DISCOVERY_URL:
                return message(2, reels)
            if url == scan.PROFILE_URL:
                return profile([])
            if "/oembed/" in url:
                original = parse_qs(urlsplit(url).query)["url"][0]
                return json.dumps({"provider_name": "Instagram", "author_name": "account",
                                   "title": "" if original == reels[-1] else "caption"})
            # A different original or an unrelated account's date cannot supply the candidate's date.
            return '<meta property="og:url" content="https://www.instagram.com/reel/Elsewhere/"><meta property="og:description" content="account on May 1, 2025: ...">'

        with patch.object(scan, "_get", side_effect=get):
            report = scan.search_public_reels()
        self.assertEqual(report["scanned"], 8)
        self.assertEqual(report["verified"], 7)
        self.assertEqual(len(report["errors"]), 1)
        self.assertTrue(all(row["published_at"] == "" for row in report["rows"]))

    def test_profile_grid_adds_originals_and_preserves_three_channel_slots(self):
        channel_codes = ["ChannelA", "ChannelB", "ChannelC"]
        public_media = [("Bad/Post", True), ("PhotoNotReel", False), ("ChannelB", True)]
        public_media += [(f"Profile{i}", True) for i in range(6)]

        def get(url):
            if url == scan.DISCOVERY_URL:
                return message(3, [f"https://instagram.com/reel/{code}" for code in channel_codes])
            if url == scan.PROFILE_URL:
                return profile(public_media)
            if "/oembed/" in url:
                return json.dumps({"provider_name": "Instagram", "author_name": scan.ACCOUNT, "title": "Из Meta"})
            return "<html></html>"

        with patch.object(scan, "_get", side_effect=get):
            report = scan.search_public_reels()
        self.assertEqual(report["scanned"], 8)
        self.assertEqual(report["verified"], 8)
        self.assertEqual(report["errors"], [])
        self.assertEqual([r["source_url"] for r in report["rows"]].count(scan.PROFILE_URL), 5)

    def test_discovery_failure_is_visible_and_redirects_are_not_followed(self):
        with patch.object(scan.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 302
            report = scan.search_public_reels()
            self.assertEqual(report["scanned"], 0)
            self.assertIn("HTTP 302", report["errors"][0])
            self.assertEqual(connection.call_count, 2)


if __name__ == "__main__":
    unittest.main()
