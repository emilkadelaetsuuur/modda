import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import deepfake_detector as d
import deepfake_watch as w

class Forensics(unittest.TestCase):
    def test_confirmation_requires_both_tracks(self):
        r=d.REFERENCE
        self.assertFalse(d.reference_match("",{"v":r["video_hash"]}))
        self.assertFalse(d.reference_match("",{"a":r["audio_hash"]}))
        self.assertTrue(d.reference_match("",{"v":r["video_hash"],"a":r["audio_hash"]}))
        self.assertTrue(d.reference_match(r["sha256"],{}))

    def test_model_signals_do_not_confirm(self):
        def check(scores):
            return d.assess([{"samples":[{"time":i*4,"score":s} for i,s in enumerate(scores)]}],False)[0]
        self.assertEqual(check([.999]),"unknown")
        self.assertEqual(check([.99,.999]),"review")
        self.assertEqual(check([]),"unknown")

    def test_reencode_requires_all_audio_and_visual_windows(self):
        ref={"frames":["abcdef"]*6,"audio":["012345"]*6}
        self.assertTrue(d.compare_signatures(ref,ref))
        self.assertFalse(d.compare_signatures(ref,dict(ref,audio=["ffffff"]*6)))
        self.assertFalse(d.compare_signatures(ref,dict(ref,frames=["000000"]*6)))

    def test_native_telegram_media(self):
        p=w.Posts()
        p.feed('<div data-post="KunUz/15"><video src="https://cdn4.telesco.pe/test.mp4"></video><div class="tgme_widget_message_text">A<br>B</div><time datetime="2026-11-10T00:00:00+00:00"></time></div><div data-post="KunUz/16">post</div>')
        self.assertEqual((p.rows[0]["key"],p.rows[0]["caption"],p.rows[0]["media"]),("kunuz/15","A B","https://cdn4.telesco.pe/test.mp4"))
        self.assertFalse(p.rows[1]["video"])
        self.assertFalse(w.safe_url("https://t.me.evil.test/xx"))
        self.assertFalse(w.safe_url("https://user@cdn4.telesco.pe/xx"))

    def test_source_failure_does_not_hide_telegram(self):
        with TemporaryDirectory() as tmp:
            def connect():
                return sqlite3.connect(Path(tmp)/"db")
            db=connect()
            w.initialize(db)
            now=datetime.now(timezone.utc).isoformat()
            row={"key":"kunuz/25","caption":"Видео из Ташкента","published":now,"views":900}
            foreign={"key":"kunuz/26","caption":"Видео из Москвы, Россия","published":now,"views":90000}
            with patch.object(w.public_sources,"youtube_discover",side_effect=ValueError()), patch.object(w,"feed",return_value=[row,foreign]):
                w.scan(connect,"")
            state=w.snapshot(db)
            self.assertIn("error",state["run"]["sources"]["YouTube"])
            self.assertEqual([i["key"] for i in state["items"]],["tg:kunuz/25"])
            self.assertEqual((state["items"][0]["status"],state["items"][0]["views"]),("queued",900))
            self.assertEqual(state["run"]["sources"]["@kunuz"]["other"],1)
            self.assertEqual(w.request_scan(db,True),state["run"]["id"])
            db.close()

    def test_view_counter(self):
        p=w.Posts()
        p.feed('<div data-post="kunuz/7"><video></video><span class="tgme_widget_message_views">9.83K</span></div>')
        self.assertEqual(p.rows[0]["views"],9830)
        self.assertEqual((w.views("983"),w.views("1.2M"),w.views("—")),(983,1200000,0))

    def test_popular_uzbek_videos_only(self):
        def video(i,title,views,seconds=40,account=""):
            return {"url":f"https://www.youtube.com/watch?v=abcdefghi{i:02d}","title":title,"description":"","views":views,
                    "duration":seconds,"account":account,"published_at":"2026-11-10T00:00:00Z"}
        rows=[video(1,"Toshkentda soxta video",500),video(2,"Mirziyoyev nutqi",90000),video(3,"Putin speech deepfake",900000),
              video(4,"Uzun film O‘zbekiston",80000,3600),video(5,"Funny cats",70000),video(6,"Health tips",60000,account="Kun.uz")]
        topic=w.watch_topics.DEEPFAKE_TOPICS["df_leaders"]
        with patch.object(w.public_sources,"youtube_discover",return_value=rows) as discover, \
             patch.object(w.public_sources,"youtube_details",return_value=rows):
            found,queries,stats=w.youtube_popular("key",topic,"2026-11-01T00:00:00+00:00")
        # Most watched first; the 500-view video is below MIN_VIEWS and skipped.
        self.assertEqual([p["title"] for p in found],["Mirziyoyev nutqi","Health tips"])
        self.assertEqual(stats,{"count":2,"examined":5,"other":2,"low":1})
        self.assertEqual(discover.call_args.kwargs["order"],"viewCount")
        self.assertEqual({c.kwargs["duration"] for c in discover.call_args_list},{"short","medium"})
        self.assertEqual(queries,list(topic.youtube))

    def test_unrelated_rows_are_hidden_after_upgrade(self):
        with TemporaryDirectory() as tmp:
            db=sqlite3.connect(Path(tmp)/"db")
            db.execute("""CREATE TABLE df_videos(key TEXT PRIMARY KEY, platform TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                published TEXT NOT NULL, added INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 0,
                claimed INTEGER NOT NULL DEFAULT 0, report TEXT NOT NULL DEFAULT '{}')""")
            for key,title in (("tg:kunuz/1","Toshkentda yong‘in"),("tg:kunuz/2","Eronda zilzila")):
                db.execute("INSERT INTO df_videos(key,platform,url,title,published,added) VALUES(?,?,?,?,?,1)",
                           (key,"Telegram","https://t.me/"+key[3:],title,"2026-11-10T00:00:00+00:00"))
            w.initialize(db)
            self.assertEqual([i["key"] for i in w.snapshot(db)["items"]],["tg:kunuz/1"])
            self.assertEqual(w.snapshot(db)["counts"],{"queued":1})
            db.close()
