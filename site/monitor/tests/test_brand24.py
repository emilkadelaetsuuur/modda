"""Offline Brand24 regression tests. Run with python -m unittest discover -s tests."""

from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import app
import brand24_mail
from brand24_source import ALERT_SENDER, parse_brand24_alert


POST = "https://www.instagram.com/p/Example123/"
SECOND_POST = "https://t.me/example_channel/42"
DASHBOARD = "https://app.brand24.com/panel/results?sid=123"


def alert(message_id, plain, html=None, sender=ALERT_SENDER):
    message = EmailMessage()
    message["From"] = sender
    message["Message-ID"] = f"<{message_id}@example.invalid>"
    message["Date"] = "Fri, 20 Nov 2026 10:00:00 +0000"
    message["Subject"] = "Brand24 notification"
    message.set_content(plain)
    if html:
        message.add_alternative(html, subtype="html")
    return message


class AlertParserTests(unittest.TestCase):
    def test_direct_links_deduplicate_and_only_explicit_date_is_kept(self):
        html = f"""
            <article><a href="{POST}?utm_source=email">Instagram mention</a>
              <time itemprop="datePublished" datetime="2026-11-18T09:30:00+05:00"></time>
            </article>
            <a href="{SECOND_POST}">Telegram mention</a>
            <a href="{DASHBOARD}">Open project</a>
            <footer><a href="https://t.me/brand_footer/8">Follow us</a></footer>
        """
        message = alert("multi", f"{POST}\n{SECOND_POST}\n{DASHBOARD}", html)
        message.add_attachment(b"https://t.me/attachment_only/5", maintype="text",
                               subtype="plain", filename="other.txt")
        rows = parse_brand24_alert(message.as_bytes())
        self.assertCountEqual([row["url"] for row in rows], [POST, SECOND_POST])
        self.assertTrue(all(row["kind"] == "brand24_mention" for row in rows))
        dates = {row["url"]: row.get("published_at") for row in rows}
        self.assertEqual(dates, {POST: "2026-11-18T04:30:00+00:00", SECOND_POST: None})

    def test_timestamp_for_two_links_is_ambiguous(self):
        html = f"""<article><a href="{POST}">First</a><a href="{SECOND_POST}">Second</a>
                   <time itemprop="datePublished" datetime="2026-11-18T09:30:00Z"></time>
                   </article>"""
        rows = parse_brand24_alert(alert("two-posts", POST, html).as_bytes())
        self.assertCountEqual([row["url"] for row in rows], [POST, SECOND_POST])
        self.assertTrue(all(row.get("published_at") is None for row in rows))

    def test_sender_address_in_display_name_is_insufficient(self):
        impostor = alert("other", POST, sender=f'"{ALERT_SENDER}" <other@example.invalid>')
        self.assertEqual(parse_brand24_alert(impostor.as_bytes()), [])


class MailImportTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = patch.object(app, "DB", Path(folder.name) / "test.sqlite")
        db.start()
        self.addCleanup(db.stop)
        app.init()
        ssl_client = patch.object(brand24_mail.imaplib, "IMAP4_SSL")
        self.mail = ssl_client.start().return_value.__enter__.return_value
        self.addCleanup(ssl_client.stop)
        self.messages = {}
        self.validity = 10
        self.fetches = []
        self.searches = []
        self.mail.login.return_value = ("OK", [b"Logged in"])
        self.mail.select.return_value = ("OK", [b"4"])
        self.mail.response.side_effect = self.response
        self.mail.uid.side_effect = self.uid

    def response(self, field):
        values = {"UIDNEXT": max(self.messages, default=0) + 1, "UIDVALIDITY": self.validity}
        return field, [str(values[field]).encode()]

    def uid(self, command, value, *terms):
        if command == "SEARCH":
            self.searches.append(terms)
            self.assertEqual(terms[terms.index("FROM") + 1], ALERT_SENDER)
            self.assertEqual(terms[terms.index("SMALLER") + 1], str(brand24_mail.MAX_BYTES + 1))
            lower, upper = map(int, terms[terms.index("UID") + 1].split(":"))
            found = [str(uid) for uid in sorted(self.messages) if lower <= uid <= upper]
            return "OK", [" ".join(found).encode()]
        self.assertEqual(command, "FETCH")
        self.assertIn("BODY.PEEK[]<0.", terms[0])
        uid = int(value)
        self.fetches.append(uid)
        raw = self.messages[uid]
        # IMAP servers may send UID before or after the response literal.
        if uid % 2:
            return "OK", [(b"1 (UID %d BODY[]<0> {%d}" % (uid, len(raw)), raw), b")"]
        return "OK", [(b"2 (BODY[]<0> {%d}" % len(raw), raw), b" UID %d)" % uid]

    def collect(self, save=app.save):
        return brand24_mail.collect_brand24_mail(app.connect, save, host="mail.example.invalid",
                                                 user="test", password="fixture")

    def items(self):
        with closing(app.connect()) as conn:
            return conn.execute("SELECT kind, url, published_at FROM items ORDER BY id").fetchall()

    def test_checkpoint_rollback_then_resume_and_replay(self):
        first = alert("one", DASHBOARD + "&utm_source=mail").as_bytes()
        self.messages = {1: first, 2: alert("two", f"{POST}\n{SECOND_POST}").as_bytes(),
                         3: first, 4: alert("different", DASHBOARD).as_bytes()}

        def fail_on_second_link(conn, row):
            app.save(conn, row)
            if row["url"] == SECOND_POST:
                raise RuntimeError("simulated write failure")

        with self.assertRaisesRegex(RuntimeError, "simulated write failure"):
            self.collect(fail_on_second_link)
        review = ("brand24_alert", DASHBOARD, None)
        self.assertEqual(self.items(), [review])
        self.assertEqual(self.fetches, [1, 2])
        self.assertIn("SINCE", self.searches[0])
        self.mail.select.assert_called_with('"INBOX"', readonly=True)
        with patch.object(brand24_mail, "BATCH_SIZE", 1):
            self.assertEqual(self.collect(), 2)
            self.assertEqual(self.fetches, [1, 2, 2])
        self.collect()
        self.assertEqual(self.items(), [review, ("brand24_mention", POST, None),
                                        ("brand24_mention", SECOND_POST, None), review])
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.fetches, [1, 2, 2, 3, 4])
        self.messages[9] = alert("new", SECOND_POST).as_bytes()
        self.assertEqual(self.collect(), 1)
        self.assertNotIn("SINCE", self.searches[-1])
        self.validity += 1
        self.messages = {1: alert("new-mailbox", DASHBOARD).as_bytes()}
        self.assertEqual(self.collect(), 1)
        self.assertIn("SINCE", self.searches[-1])
        self.assertEqual(self.items().count(review), 3)


if __name__ == "__main__":
    unittest.main()
