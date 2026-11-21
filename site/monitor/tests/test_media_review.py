import unittest
from unittest.mock import patch
import auto_law
import autonomous_monitor
import media_review


class MediaReviewTest(unittest.TestCase):
    def setUp(self):
        self.quote = auto_law.demo_cases.LEAVE_QUOTE
        self.row = dict(method="search",platform="youtube",description="",video_title="",speech=[],
                        published_at="2025-02-14",url="https://www.youtube.com/watch?v=TestVideo01",
                        media_report={"speech":{"text":"","segments":[]},"frames":[]})
        norm = auto_law.demo_cases.LEAVE
        ref = patch("auto_law._reference",return_value=(norm.url,norm.excerpt))
        ref.start()
        self.addCleanup(ref.stop)

    def test_quote_must_come_from_that_frame(self):
        self.row["media_report"]["frames"] = [dict(start=6,text=self.quote)]
        hit, = auto_law.check_video(self.row)
        self.assertEqual((hit["evidence_kind"],hit["start"]),("frame",6))
        autonomous_monitor._alert(self.row,hit,False)
        with self.assertRaises(ValueError):
            autonomous_monitor._alert(self.row,dict(hit,start=7),False)

    def test_untimed_voice_and_surrounding_disclaimer(self):
        self.row["media_report"]["speech"]["text"] = self.quote
        hit, = auto_law.check_video(self.row)
        self.assertEqual(hit["evidence_kind"],"voice_text")
        self.assertNotIn("start",hit)
        self.row["media_report"]["frames"] = [dict(start=2,text="Вымышленное объявление")]
        self.assertEqual(auto_law.check_video(self.row),[])

    def test_voice_timestamps_are_from_api(self):
        response = {"status":"completed","language":"ru","transcript":self.quote,
                    "segments":[dict(text=self.quote,start_ms=2500,end_ms=4810)]}
        with patch.dict("os.environ",VOICELAB_API_KEY="test"), patch("media_review._api",side_effect=[{"id":"test"},response]):
            voice = media_review.transcribe(b"audio",6)
        self.row["media_report"]["speech"] = voice
        hit, = auto_law.check_video(self.row)
        self.assertEqual((hit["evidence_kind"],hit["start"],hit["end"]),("speech",2.5,4.81))
