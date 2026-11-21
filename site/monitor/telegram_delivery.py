"""Deliver live, evidence-backed review notices to the team's chosen Telegram chat."""

from contextlib import closing
import json
import os
import re
import time

from telegram_source import TelegramRejected, _bot_api


def valid_destination(value):
    return bool(re.fullmatch(r"-?[1-9][0-9]{0,15}|@[A-Za-z][A-Za-z0-9_]{4,31}", value))


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS telegram_deliveries (
        alert_id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
        next_attempt INTEGER NOT NULL DEFAULT 0, destination TEXT NOT NULL DEFAULT '',
        message_id INTEGER, error TEXT NOT NULL DEFAULT '')""")


def enqueue(conn, alert_id, alert):
    if alert.get("is_demo") or not all(alert.get(k) for k in ("url", "published_at", "quote", "law_url")):
        return
    conn.execute("INSERT OR IGNORE INTO telegram_deliveries(alert_id) VALUES (?)", (alert_id,))


def _body(alert_id, alert):
    category = {"potential_violation": "Возможное нарушение",
                "advertising_review": "Нужно определить, является ли публикация рекламой",
                "inaccurate_claim": "Расхождение с правовой нормой"}.get(alert["category"], "На проверку")
    return "\n".join([
        f"Modda · {category} · №{alert_id}", alert["title"][:220],
        f'{alert["platform"].capitalize()} · {alert["published_at"]}',
        f'{alert["where"]} · «{alert["quote"][:450]}»',
        alert["reason"][:650], alert["law_label"][:180], alert["law_url"], alert["url"],
    ])


def _connection_settings():
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()


def snapshot(conn):
    token, chat = _connection_settings()
    counts = dict(conn.execute("""SELECT d.state,COUNT(*) FROM telegram_deliveries d
        JOIN auto_alerts a ON a.id=d.alert_id WHERE a.active=1 GROUP BY d.state"""))
    issue = conn.execute("""SELECT d.error FROM telegram_deliveries d JOIN auto_alerts a
        ON a.id=d.alert_id WHERE a.active=1 AND d.error!='' ORDER BY d.alert_id DESC LIMIT 1""").fetchone()
    return dict(configured=bool(token and valid_destination(chat)), token_present=bool(token),
                chat_configured=valid_destination(chat), pending=counts.get("pending", 0) + counts.get("sending", 0),
                sent=counts.get("sent", 0), uncertain=counts.get("uncertain", 0), error=issue[0] if issue else "")


def dispatch(connect):
    token, chat = _connection_settings()
    if not token or not valid_destination(chat):
        return
    now = int(time.time())
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # A send might have reached Telegram just before the process exited.
        conn.execute("""UPDATE telegram_deliveries SET state='uncertain',
            error='После прерывания проверьте доставку в Telegram.'
            WHERE state='sending' AND next_attempt<=?""", (now,))
        row = conn.execute("""SELECT d.alert_id,a.payload FROM telegram_deliveries d
            JOIN auto_alerts a ON a.id=d.alert_id WHERE a.active=1 AND a.mode='live'
            AND d.state='pending' AND d.next_attempt<=? ORDER BY d.alert_id LIMIT 1""", (now,)).fetchone()
        if not row:
            return
        conn.execute("""UPDATE telegram_deliveries SET state='sending',destination=?,next_attempt=?
            WHERE alert_id=?""", (chat, now + 90, row[0]))
    state, message_id, retry_at, error = "sent", None, 0, ""
    try:
        result = _bot_api(token, "sendMessage", {
            "chat_id": chat, "text": _body(row[0], json.loads(row[1])), "link_preview_options": {"is_disabled": True}})
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise ValueError("Нет подтверждения доставки.")
        message_id = result["message_id"]
    except TelegramRejected as exc:
        if 400 <= exc.status < 500:
            state, retry_at = "pending", now + max(60, exc.retry_after or 180)
            error = str(exc) + (" Проверьте получателя и право бота писать в чат." if exc.status in (400, 403) else "")
        else:
            state, error = "uncertain", "Telegram не подтвердил доставку. Проверьте чат по номеру находки."
    except ValueError:
        state, error = "uncertain", "Ответ Telegram не получен. Проверьте чат по номеру находки."
    with closing(connect()) as conn, conn:
        conn.execute("""UPDATE telegram_deliveries SET state=?,message_id=?,next_attempt=?,error=?
            WHERE alert_id=? AND state='sending' AND destination=?""",
            (state, message_id, retry_at, error, row[0], chat))
