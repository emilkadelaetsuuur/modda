from contextlib import closing
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import app
import auto_law
import telegram_delivery as delivery
from telegram_source import TelegramRejected


class TelegramDeliveryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = patch.object(app, "DB", Path(folder.name) / "test.sqlite")
        db.start()
        self.addCleanup(db.stop)
        app.init()
        config = patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "1:local", "TELEGRAM_ALERT_CHAT_ID": "-123"})
        config.start()
        self.addCleanup(config.stop)
        fixture = auto_law.training_examples()[0]
        alert = auto_law.check_video(fixture, training=True)[0]
        self.alert = {**alert, "is_demo":False, "platform":"instagram", "published_at":"2025-02-14",
                      "url":"https://www.instagram.com/reel/sample1/"}

    def record(self):
        with closing(app.connect()) as conn, conn:
            payload = json.dumps(self.alert)
            key = conn.execute("""INSERT INTO auto_alerts
                (mode,source_key,rule_id,created_at,updated_at,first_payload,payload)
                VALUES ('live','ig:sample1','example',1,1,?,?)""", (payload, payload)).lastrowid
            delivery.enqueue(conn, key, self.alert)
            delivery.enqueue(conn, key, self.alert)
            return key

    @patch.object(delivery, "_bot_api", return_value={"message_id": 21})
    def test_delivery_once_using_current_evidence(self, send):
        key = self.record()
        with closing(app.connect()) as conn, conn:
            new = dict(self.alert, quote="Актуальная фраза")
            conn.execute("UPDATE auto_alerts SET payload=? WHERE id=?", (json.dumps(new), key))
        delivery.dispatch(app.connect)
        delivery.dispatch(app.connect)
        send.assert_called_once()
        token, method, params = send.call_args.args
        self.assertEqual((method, params["chat_id"]), ("sendMessage", "-123"))
        for value in (new["quote"], new["url"], new["law_url"], new["published_at"]):
            self.assertIn(value, params["text"])
        self.assertNotIn(token, params["text"])
        with closing(app.connect()) as conn:
            self.assertEqual(delivery.snapshot(conn)["sent"], 1)

    @patch.object(delivery, "_bot_api")
    def test_no_target_no_demo_and_no_withdrawn_alert(self, send):
        key = self.record()
        with patch.dict("os.environ", {"TELEGRAM_ALERT_CHAT_ID": ""}):
            delivery.dispatch(app.connect)
        with closing(app.connect()) as conn, conn:
            conn.execute("UPDATE auto_alerts SET active=0 WHERE id=?", (key,))
            delivery.enqueue(conn, key+1, {**self.alert, "is_demo":True})
            self.assertEqual(conn.execute("SELECT count(*) FROM telegram_deliveries").fetchone()[0], 1)
        delivery.dispatch(app.connect)
        send.assert_not_called()

    def test_explicit_rejection_can_retry_unknown_delivery_cannot_duplicate(self):
        self.record()
        with patch.object(delivery, "_bot_api", side_effect=TelegramRejected(429, "Лимит", 300)) as send:
            delivery.dispatch(app.connect)
            delivery.dispatch(app.connect)
            send.assert_called_once()
        with closing(app.connect()) as conn, conn:
            self.assertEqual(delivery.snapshot(conn)["pending"], 1)
            conn.execute("UPDATE telegram_deliveries SET next_attempt=0")
        with patch.object(delivery, "_bot_api", side_effect=ValueError("lost response")) as send:
            delivery.dispatch(app.connect)
            delivery.dispatch(app.connect)
            send.assert_called_once()
        with closing(app.connect()) as conn:
            self.assertEqual(delivery.snapshot(conn)["uncertain"], 1)
