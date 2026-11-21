"""Read Brand24 notification email over TLS and checkpoint imported alerts in SQLite."""

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import imaplib
import json
import re
import ssl

from brand24_source import ALERT_SENDER, parse_brand24_alert


MAX_BYTES = 2 * 1024 * 1024
BATCH_SIZE = 25
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_UID = re.compile(rb"\bUID (\d+)\b", re.I)


def _quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _folder_name(folder):
    # IMAP4rev1 mailbox names use modified UTF-7 before UTF8=ACCEPT is enabled.
    def encode(match):
        value = match.group()
        if value == "&":
            return "&-"
        token = base64.b64encode(value.encode("utf-16-be")).decode("ascii")
        return "&" + token.rstrip("=").replace("/", ",") + "-"

    return _quoted(re.sub(r"[^\x20-\x7e]+|&", encode, folder))


def _ok(reply):
    status, data = reply
    if status != "OK":
        raise ValueError("Почтовый сервер не выполнил запрос Brand24.")
    return data


def _uid_number(value):
    if not isinstance(value, bytes) or not re.fullmatch(rb"[1-9][0-9]{0,9}", value):
        raise ValueError("Некорректный идентификатор в ответе IMAP.")
    number = int(value)
    if number > 4294967295:
        raise ValueError("Некорректный идентификатор в ответе IMAP.")
    return number


def _search(mail, start, end, since):
    terms = ["UID", f"{start}:{end}", "FROM", ALERT_SENDER, "SMALLER", str(MAX_BYTES + 1)]
    if since:
        terms.extend(("SINCE", since))
    data = _ok(mail.uid("SEARCH", None, *terms))
    if len(data) != 1 or not isinstance(data[0], bytes):
        raise ValueError("Почтовый сервер не вернул список писем Brand24.")
    return sorted({uid for word in data[0].split() if start <= (uid := _uid_number(word)) <= end})


def _message(mail, uid):
    data = _ok(mail.uid("FETCH", str(uid), f"(UID BODY.PEEK[]<0.{MAX_BYTES + 1}>)"))
    # An expunged UID yields OK without a FETCH response.
    if data == [None] or not data:
        return None
    for index, part in enumerate(data):
        if not isinstance(part, tuple) or len(part) != 2:
            continue
        fields, raw = part
        if not re.search(rb"\bBODY\[\](?:<0>)?\s+\{[0-9]+\}$", fields, re.I):
            continue
        # Servers can return UID either before the literal or in its closing line.
        tail = data[index + 1] if index + 1 < len(data) else None
        if isinstance(tail, bytes):
            fields += b" " + tail
        match = _UID.search(fields)
        if match and int(match[1]) == uid and isinstance(raw, bytes):
            return raw if len(raw) <= MAX_BYTES else None
    raise ValueError("Почтовый сервер вернул неполное письмо Brand24.")


def _checkpoint(conn, key, uid, rows, save_row):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute("""
            INSERT INTO config(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            WHERE CAST(config.value AS INTEGER) < CAST(excluded.value AS INTEGER)
        """, (key, str(uid)))
        if not updated.rowcount:
            return 0
        for row in rows:
            save_row(conn, row)
    return len(rows)


def _poll(mail, conn, key, end, save_row):
    # Keep the original date window across restarts while draining the first batch.
    date = datetime.now(timezone.utc).date() - timedelta(days=14)
    since = f"{date.day:02}-{_MONTHS[date.month - 1]}-{date.year}"
    with conn:
        conn.execute("INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)",
                     (key + ":initial", f"{end} {since}"))
    initial, since = conn.execute("SELECT value FROM config WHERE key=?", (key + ":initial",)).fetchone()[0].split()
    initial = int(initial)
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    last = int(row[0]) if row else 0
    budget = BATCH_SIZE
    count = 0
    for upper, date_filter in ((min(initial, end), since), (end, None)):
        if last >= upper:
            continue
        matches = _search(mail, last + 1, upper, date_filter)
        for uid in matches[:budget]:
            raw = _message(mail, uid)
            rows = parse_brand24_alert(raw) if raw else []
            count += _checkpoint(conn, key, uid, rows, save_row)
        if len(matches) > budget:
            break
        if not matches or matches[-1] < upper:
            _checkpoint(conn, key, upper, [], save_row)
        last = upper
        budget -= len(matches)
        if not budget:
            break
    return count


def collect_brand24_mail(conn_factory, save_row, *, host, user, password, folder="INBOX", port=993):
    """Collect one bounded batch, returning the number of mention rows processed."""
    if any(not isinstance(v, str) or not v or any(ord(c) < 32 or ord(c) == 127 for c in v)
           for v in (host, user, password, folder)):
        raise ValueError("Проверьте настройки почтового ящика Brand24.")
    try:
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Проверьте номер порта IMAP Brand24.") from None

    mailbox = _folder_name(folder)
    try:
        with imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=12) as mail:
            try:
                _ok(mail.login(_quoted(user), password))
            except imaplib.IMAP4.error:
                raise ValueError("Почтовый сервер отклонил вход. Проверьте логин и пароль приложения.") from None
            _ok(mail.select(mailbox, readonly=True))
            validity = _uid_number(mail.response("UIDVALIDITY")[1][0])
            next_uid = mail.response("UIDNEXT")[1][0]
            if next_uid is None:
                data = _ok(mail.status(mailbox, "(UIDNEXT)"))
                match = re.search(rb"\bUIDNEXT (\d+)\)", data[0] or b"", re.I)
                next_uid = match[1] if match else None
            end = _uid_number(next_uid) - 1
            scope = [host.lower(), port, user, "INBOX" if folder.upper() == "INBOX" else folder, validity]
            key = "brand24_imap_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()
            with closing(conn_factory()) as conn:
                return _poll(mail, conn, key, end, save_row)
    except (OSError, imaplib.IMAP4.error, UnicodeError):
        raise ValueError("Не удалось прочитать почту уведомлений по IMAP с TLS. Проверьте сервер и папку.") from None
