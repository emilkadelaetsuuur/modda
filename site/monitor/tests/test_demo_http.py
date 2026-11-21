from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app


class DemoHttpTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = patch.object(app, "DB", Path(temp.name) / "monitor.sqlite")
        db.start()
        self.addCleanup(db.stop)
        app.init()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Desk)
        self.server.sources = ()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, payload=None, origin=None):
        port = self.server.server_port
        headers = {"X-Lex-Request": "1", "Origin": origin or f"http://127.0.0.1:{port}",
                   "Content-Type": "application/json"}
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as client:
            client.request("GET" if payload is None else "POST", path,
                           body=None if payload is None else json.dumps(payload), headers=headers)
            response = client.getresponse()
            return response.status, json.load(response)

    def test_offline_replay_handoff_and_reset(self):
        with patch.dict("os.environ", {"APIFY_TOKEN": ""}):
            status, summary = self.request("/api/demo/replay", {})
            self.assertEqual(status, 200)
            self.assertGreater(summary["scanned"], summary["matched"])
            case_id = summary["case_id"]
            status, answer = self.request("/api/demo/review", {"id": case_id, "status": "assigned",
                                                              "assignee": "Дежурный юрист"})
            self.assertEqual(status, 200)
            self.assertEqual(answer["case"]["status"], "assigned")
            self.assertEqual(self.request("/api/demo/replay", {})[1]["added"], 0)
            self.assertEqual(self.request("/api/demo/restart", {}, "https://example.org")[0], 403)
            self.assertEqual(self.request("/api/demo/restart", {})[0], 200)
            result = self.request("/api/demo")[1]
            self.assertFalse(result["has_token"])
            self.assertTrue(all(row["status"] == "new" for row in result["cases"]))
            self.assertEqual(self.request("/api/demo/search", {"query":"q","format":"reels"})[0], 409)

    def test_live_case_requires_verified_excerpt(self):
        incoming = {"url":"https://www.instagram.com/p/testDemoQ1/", "description":"Проверочный материал",
                    "account":"", "published_at":"2026-11-19T20:01:00+00:00"}
        with closing(app.connect()) as conn, conn:
            app.demo_cases.add_live(conn, [incoming], "пример")
            case_id = app.demo_cases.list_cases(conn)[0]["id"]
        fields = {"id":case_id, "status":"assigned", "assignee":"Дежурный юрист", "quote":"Проверочный материал",
                  "law_url":"https://lex.uz/docs/123#456", "law_label":"Проверочный пункт",
                  "law_excerpt":"Точная цитата", "reason":"Сверить утверждение"}
        with patch.object(app, "lex_passage", side_effect=ValueError("Текст не подтверждён.")) as fetch:
            self.assertEqual(self.request("/api/demo/review", fields)[0], 400)
            fetch.assert_called_once_with(fields["law_url"], fields["law_excerpt"], "20.11.2026")
        self.assertEqual(self.request("/api/demo")[1]["cases"][0]["status"], "new")
        with patch.object(app, "lex_passage", return_value={"url":"https://lex.uz/docs/123?ONDATE=10.11.2026#456"}):
            status, answer = self.request("/api/demo/review", fields)
            self.assertEqual(status, 200)
            self.assertEqual(answer["case"]["status"], "assigned")
        self.request("/api/demo/restart", {})
        self.assertTrue(any(c["mode"] == "live" and c["status"] == "assigned"
                            for c in self.request("/api/demo")[1]["cases"]))
