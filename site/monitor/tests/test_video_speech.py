from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from demo_instagram import _publication
import video_speech as speech


class VideoSpeechTest(unittest.TestCase):
    url = "https://scontent-a.cdninstagram.com/v/clip.mp4?sig=temporary"
    row = {"url": "https://www.instagram.com/reel/abc/", "media_url": url}

    def test_only_public_instagram_reels_retain_provider_media(self):
        self.assertEqual(speech.media_url(self.url), self.url)
        self.assertEqual(speech.media_url("https://video.xx.fbcdn.net/x"), "https://video.xx.fbcdn.net/x")
        for url in ("https://cdninstagram.com/x", "https://a.cdninstagram.com.local/x",
                    "https://cdninstagram.com@example.com/x", "https://a.cdninstagram.com:8080/x",
                    "http://a.fbcdn.net/x", "https://a.fbcdn.net/x#frag", "https://a.fbcdn.net/a\r\nX:y",
                    "https://x.invalid/a", "file:///secret.mp4", "https://a.fbcdn.net\\@evil.com/x"):
            self.assertEqual(speech.media_url(url), "")
        source = dict(shortCode="abc", productType="clips", videoUrl=self.url)
        self.assertEqual(_publication(source, "reels")["media_url"], self.url)
        self.assertNotIn("media_url", _publication(dict(source, productType="feed"), "posts"))
        self.assertNotIn("media_url", _publication(dict(source, videoUrl="https://elsewhere/video.mp4"), "reels"))
        with patch("video_speech._download") as fetch:
            for link in ("https://www.facebook.com/reel/123/", "https://www.instagram.com/p/abc/", ""):
                self.assertFalse(speech.enrich(dict(self.row, url=link))["speech"])
            fetch.assert_not_called()

    def test_download_rejects_redirects_payload_overruns_and_truncation(self):
        for status, declared, chunks, expected in (
            (302, None, [], "download"), (200, "60000000", [], "size"),
            (200, "3", [b"ab", b""], "download"), (200, None, [b"abcdef"], "size"),
            (200, "3", [b"abc", b""], "ok"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                destination = Path(tmp) / "video"
                with patch("video_speech.http.client.HTTPSConnection") as client, patch("video_speech.MAX_BYTES", 5):
                    connection = client.return_value
                    response = connection.getresponse.return_value
                    response.status = status
                    response.getheader.return_value = declared
                    response.read1.side_effect = chunks
                    self.assertEqual(speech._download(self.url, destination), expected)
                    connection.request.assert_called_once_with("GET", "/v/clip.mp4?sig=temporary")
                    connection.close.assert_called_once()

    def test_short_local_audio_reaches_offline_worker_without_sensitive_url(self):
        runs = [subprocess.CompletedProcess([], 0, b"pcm" * 1200),
                subprocess.CompletedProcess([], 0, b'{"speech":[],"status":"no_speech"}')]
        with patch("video_speech.subprocess.run", side_effect=runs) as runner:
            self.assertEqual(speech._recognize_file(Path("recording.mp4")), ([], "no_speech"))
            decode, asr = runner.call_args_list
            self.assertIn("recording.mp4", decode.args[0])
            self.assertIn("file", decode.args[0])
            self.assertIn("mov", decode.args[0])
            self.assertEqual(asr.kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(asr.kwargs["input"], b"pcm" * 1200)
        with patch("video_speech.MAX_SECONDS", 1), patch("video_speech.subprocess.run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 0, b"x" * 32002)
            self.assertEqual(speech._recognize_file("recording.mp4"), ([], "duration"))
            runner.assert_called_once()

    def test_timeout_removes_partial_download_and_hides_signed_url(self):
        paths = []

        def fail(url, target):
            paths.append(target)
            target.write_bytes(b"partial media")
            raise TimeoutError(url)

        with patch("video_speech._download", side_effect=fail):
            result = speech.enrich(self.row)
        self.assertFalse(result["speech"])
        self.assertNotIn("sig", result["speech_status"])
        self.assertFalse(paths[0].exists())

    def test_reject_whole_transcript_when_a_later_sentence_is_uncertain(self):
        # Exercise the recognizer postprocessing without model weights or network.
        confident = Mock(avg_logprob=-0.2, no_speech_prob=0.02, compression_ratio=1.2,
                         words=[Mock(probability=0.9, start=0, end=0.7)], text="Купите акции.")
        doubtful = Mock(avg_logprob=-2.1, no_speech_prob=0.1, compression_ratio=1,
                        words=[Mock(probability=0.3, start=0.7, end=0.9)], text="Это неправда.")
        info = Mock(duration_after_vad=1, language="ru", language_probability=0.95)
        self.assertEqual(speech._trusted_segments([confident, doubtful], info, 1), ([], "uncertain"))
