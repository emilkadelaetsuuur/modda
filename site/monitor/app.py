"""Local monitoring desk for the Lex Hackathon."""

import argparse
import tempfile
import subprocess
import media_review
import auto_law
import deepfake_watch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import webbrowser

from access_settings import settings_request
from brand24_mail import collect_brand24_mail
import autonomous_monitor
import demo_cases
import telegram_delivery
import video_search
from demo_instagram import InstagramDiscoveryError, discover_instagram
from law_source import lex_article, lex_passage
from meta_sources import meta_feed, meta_status
from public_sources import DEFAULT_CHANNEL, lex_feed, youtube_discover, youtube_feed, youtube_video
from telegram_source import public_post_url, telegram_updates


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "monitor.sqlite"
PLATFORMS = ("youtube", "youtube_search", "lexuz", "instagram", "facebook", "telegram", "brand24")
SEARCH_INTERVAL = 3600
DEFAULT_SEARCH_QUERY = "закон Узбекистана|O‘zbekiston qonuni|Ўзбекистон қонуни"
FILTERS = {"social": "platform != 'lexuz'", "laws": "platform = 'lexuz'",
           "submissions": "kind IN ('submission', 'brand24_mention', 'brand24_alert')", "articles": "kind = 'article'"}
CONFIG = {"YOUTUBE_CHANNEL_ID", "YOUTUBE_API_KEY", "YOUTUBE_SEARCH_QUERY",
          "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_IDS",
          "IG_USER_ID", "IG_ACCESS_TOKEN", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN",
          "IG_DISCOVERY_USERNAME", "META_GRAPH_VERSION", "BRAND24_IMAP_HOST", "BRAND24_IMAP_USER",
          "BRAND24_IMAP_PASSWORD", "BRAND24_IMAP_FOLDER", "APIFY_TOKEN", "AUTO_META_DISCOVERY", "AUTO_YOUTUBE_DISCOVERY",
          "TELEGRAM_ALERT_CHAT_ID"}
lock = threading.Lock()
instagram_search_lock = threading.Lock()
youtube_search_lock = threading.Lock()
media_upload_lock = threading.Lock()
status = {k: {"state": "wait", "message": "Ожидает обновления", "at": ""} for k in PLATFORMS}
next_run = {k: 0.0 for k in PLATFORMS}
inherited = {k: os.environ[k] for k in CONFIG if k in os.environ}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_settings():
    path = ROOT / "settings.env"
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in CONFIG and value.strip():
                values[key] = value.strip()
    changed = False
    values.update(inherited)
    for key in CONFIG:
        if os.getenv(key) != values.get(key):
            changed = True
            if key in values:
                os.environ[key] = values[key]
            else:
                os.environ.pop(key, None)
    return changed


def connect():
    return sqlite3.connect(DB, timeout=10)


def init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn, conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                platform TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, seen TEXT NOT NULL,
                published_at TEXT, source_updated_at TEXT);
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        for name in ("published_at", "source_updated_at"):
            if name not in columns:
                conn.execute(f"ALTER TABLE items ADD COLUMN {name} TEXT")
        demo_cases.initialize(conn)
        video_search.initialize(conn)
        autonomous_monitor.initialize(conn)
        telegram_delivery.initialize(conn)
        deepfake_watch.initialize(conn)


def monitor_options():
    return os.getenv("YOUTUBE_API_KEY", ""), os.getenv("AUTO_YOUTUBE_DISCOVERY", "1") == "1"


def monitor_state():
    with closing(connect()) as conn:
        data = autonomous_monitor.snapshot(conn, *monitor_options())
        data["public_search"] = autonomous_monitor.public_scan_status(conn)
        data["telegram"] = telegram_delivery.snapshot(conn)
        return data


def save(conn, row, default_kind="feed"):
    kind = row.get("kind", default_kind)
    identity_parts = [kind, row["url"]]
    if kind == "submission":
        identity_parts.append(row["text"].strip())
    elif kind == "brand24_alert":
        identity_parts = [kind, row["_identity"]]
    identity = hashlib.sha256(json.dumps(identity_parts, ensure_ascii=False).encode()).hexdigest()
    conn.execute("""
        INSERT INTO items(identity, platform, kind, url, title, text, seen, published_at, source_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identity) DO UPDATE SET title=excluded.title, text=excluded.text,
            published_at=COALESCE(excluded.published_at, items.published_at),
            source_updated_at=COALESCE(excluded.source_updated_at, items.source_updated_at)
        WHERE items.title != excluded.title OR items.text != excluded.text
           OR (excluded.published_at IS NOT NULL AND excluded.published_at IS NOT items.published_at)
           OR (excluded.source_updated_at IS NOT NULL AND excluded.source_updated_at IS NOT items.source_updated_at)
    """, (identity, row["platform"], kind, row["url"], row["title"], row["text"], now(),
          row.get("published_at") or None, row.get("source_updated_at") or None))


def configured(source):
    if source == "brand24":
        return all(os.getenv(key) for key in ("BRAND24_IMAP_HOST", "BRAND24_IMAP_USER", "BRAND24_IMAP_PASSWORD"))
    if source == "youtube_search":
        return bool(os.getenv("YOUTUBE_API_KEY"))
    if source == "telegram":
        return bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    return source not in ("instagram", "facebook") or meta_status(source)


class _SearchCooldown(Exception):
    def __init__(self, until):
        self.until = until


def reserve_search():
    """Claim the next request before contacting Google, including failed requests."""
    stamp = int(time.time())
    with closing(connect()) as conn, conn:
        claim = conn.execute("""
            INSERT INTO config(key, value) VALUES ('youtube_search_last_attempt', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            WHERE CAST(config.value AS INTEGER) <= ?
        """, (str(stamp), stamp - SEARCH_INTERVAL))
        if not claim.rowcount:
            last, = conn.execute("SELECT value FROM config WHERE key='youtube_search_last_attempt'").fetchone()
            raise _SearchCooldown(int(last) + SEARCH_INTERVAL)


def fetch(source):
    if source == "brand24":
        return collect_brand24_mail(connect, save, host=os.environ["BRAND24_IMAP_HOST"],
                                    user=os.environ["BRAND24_IMAP_USER"], password=os.environ["BRAND24_IMAP_PASSWORD"],
                                    folder=os.getenv("BRAND24_IMAP_FOLDER") or "INBOX")
    if source == "telegram":
        bot_id = os.getenv("TELEGRAM_BOT_TOKEN", "").partition(":")[0]
        key = "telegram_offset_" + bot_id
        with closing(connect()) as conn:
            cursor = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            updates = telegram_updates(int(cursor[0]) if cursor else 0)
            with conn:
                for _, row in updates:
                    if row:
                        save(conn, row)
                if updates:
                    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)",
                                 (key, str(max(i for i, _ in updates) + 1)))
            return sum(row is not None for _, row in updates)
    if source == "youtube":
        rows = youtube_feed(os.getenv("YOUTUBE_CHANNEL_ID") or DEFAULT_CHANNEL)
    elif source == "youtube_search":
        key = os.getenv("YOUTUBE_API_KEY")
        if not key:
            raise ValueError("Поиску нужен YOUTUBE_API_KEY в settings.env.")
        query = os.getenv("YOUTUBE_SEARCH_QUERY") or DEFAULT_SEARCH_QUERY
        after = datetime.now(timezone.utc) - timedelta(days=7)
        reserve_search()
        rows = youtube_discover(key, query, after.isoformat(timespec="seconds"))
    elif source == "lexuz":
        rows = lex_feed()
    else:
        rows = meta_feed(source)
    with closing(connect()) as conn, conn:
        for row in reversed(rows):
            save(conn, row)
    return len(rows)


def collect_one(source):
    delay = {"telegram": 20, "youtube_search": SEARCH_INTERVAL}.get(source, 300)
    try:
        count = fetch(source)
        state, message = "ok", f"Получено за обновление · {count}"
    except _SearchCooldown as wait:
        delay = max(1, wait.until - time.time())
        next_time = datetime.fromtimestamp(wait.until, timezone.utc).strftime("%H:%M UTC")
        state, message = "wait", f"Следующий тематический поиск доступен в {next_time}."
    except ValueError as exc:
        state, message = "error", str(exc)
    except Exception:
        state, message = "error", "Сбой обработки. Проверьте настройки и повторите обновление."
    with lock:
        status[source] = {"state": state, "message": message, "at": now()}
        next_run[source] = time.monotonic() + delay


def schedule(pool, force=False, sources=PLATFORMS):
    with lock:
        for source in sources:
            if not configured(source):
                message = "Нужен YOUTUBE_API_KEY в settings.env" if source == "youtube_search" else "Нужен токен в settings.env"
                if source == "brand24":
                    message = "Подключите почту для уведомлений в настройках доступа."
                status[source] = {"state": "off", "message": message, "at": ""}
            elif (status[source]["state"] != "busy"
                  and (time.monotonic() >= next_run[source] or status[source]["state"] == "off"
                       or (force and source != "youtube_search"))):
                status[source] = {"state": "busy", "message": "Обновляется…", "at": status[source]["at"]}
                pool.submit(collect_one, source)


def get_state(query, sources=PLATFORMS):
    view = query.get("view", ["social"])[0]
    if view not in FILTERS:
        raise ValueError("Неизвестный раздел.")
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        raise ValueError("Неверный номер страницы.") from None
    size = 15
    where = FILTERS[view]
    with closing(connect()) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT count(*) FROM items WHERE " + where).fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(page, pages))
        rows = conn.execute(
            "SELECT platform,kind,url,title,text,seen,published_at,source_updated_at FROM items WHERE " + where +
            " ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size))
        items = [dict(row) for row in rows]
    with lock:
        return dict(items=items, sources={key: status[key] for key in sources}, total=total,
                    page=page, pages=pages, demo_available=True, monitor_available=True)


def submit(url, note):
    source = public_post_url(url)
    if not source:
        raise ValueError("Вставьте ссылку на публикацию YouTube, Instagram, Facebook, Threads или Telegram либо на карточку из Библиотеки рекламы Meta.")
    platform, url = source
    if len(note) > 4000:
        raise ValueError("Сократите цитату до 4000 символов.")
    row = {"url": url, "platform": platform, "kind": "submission",
           "title": "Обращение по публикации", "text": note}
    metadata = None
    if platform == "youtube":
        try:
            metadata = youtube_video(url)
            row.update(url=metadata["url"], title=metadata["title"])
        except ValueError:
            pass
    with closing(connect()) as conn, conn:
        if metadata:
            save(conn, metadata, "metadata")
        save(conn, row)
    return "Ссылка и цитата сохранены." if note else "Ссылка сохранена. Цитата пока не указана."


class Desk(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def parse_request(self):
        if not super().parse_request():
            return False
        host = self.headers.get("Host", "").lower()
        port = self.server.server_port
        public = os.getenv("LEX_PUBLIC_ORIGIN","")
        if host not in {f"localhost:{port}", f"127.0.0.1:{port}", urlsplit(public).netloc if public else f"localhost:{port}"}:
            self.send_error(403)
            return False
        return True

    def reply(self, code, payload, mime="application/json; charset=utf-8", headers=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self.path == "/setup":
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def demo_video(self, filename):
        path = ROOT / "demo_media" / filename
        if not path.is_file():
            return self.reply(404, {"error": "Учебный видеофайл не найден."})
        data = path.read_bytes()
        headers = {"Accept-Ranges": "bytes"}
        byte_range = self.headers.get("Range")
        if byte_range is None:
            return self.reply(200, data, "video/mp4", headers)
        match = re.fullmatch(r"bytes=([0-9]{0,14})-([0-9]{0,14})", byte_range)
        if match and any(match.groups()):
            first, last = match.groups()
            start = int(first) if first else max(0, len(data) - int(last))
            end = min(int(last), len(data) - 1) if first and last else len(data) - 1
            if start <= end:
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                return self.reply(206, data[start:end+1], "video/mp4", headers)
        headers["Content-Range"] = f"bytes */{len(data)}"
        return self.reply(416, b"", "video/mp4", headers)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path)
        public = bool(os.getenv("LEX_PUBLIC_ORIGIN"))
        if public and path.path not in {"/", "/monitor", "/api/monitor", "/api/health", "/api/monitor/media-example",
                                       "/demo-media/water.mp4", "/demo-media/securities.mp4", "/api/monitor/media-evidence", "/live", "/api/live-search", "/deepfakes", "/api/deepfakes"}:
            return self.reply(404, {"error":"Раздел недоступен."})
        if path.path == "/deepfakes" or (os.getenv("LEX_DEEPFAKE_WORKER") == "1" and path.path in {"/","/live"} and not path.query):
            return self.reply(200,(ROOT/"deepfakes.html").read_bytes(),"text/html; charset=utf-8")
        if path.path == "/api/deepfakes":
            with closing(connect()) as conn:
                return self.reply(200,deepfake_watch.snapshot(conn))
        if path.path == "/live":
            return self.reply(200,(ROOT/"live.html").read_bytes(),"text/html; charset=utf-8")
        if path.path == "/api/live-search":
            raw = parse_qs(path.query).get("run", [""])[0]
            if raw and not re.fullmatch(r"[1-9][0-9]{0,14}",raw):
                return self.reply(400,{"error":"Неверный номер запуска."})
            with closing(connect()) as conn:
                state = autonomous_monitor.live_show_status(conn,int(raw) if raw else None,*monitor_options())
            if raw and not state["run"]:
                return self.reply(404,{"error":"Запуск не найден."})
            return self.reply(200,state)
        if path.path == "/api/monitor/media-evidence":
            video_id = parse_qs(path.query).get("id",[""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}",video_id):
                return self.reply(400, {"error":"Некорректная ссылка на видео."})
            with closing(connect()) as conn:
                found = conn.execute("SELECT report FROM media_checks WHERE identity=? AND status IN ('ready','partial')",
                                     ("yt:"+video_id,)).fetchone()
            return self.reply(200, {"report":json.loads(found[0])}) if found else self.reply(404, {"error":"Проверка медиа ещё не готова."})
        if path.path == "/api/monitor/media-example":
            sample = ROOT / "data" / "media-example.json"
            if not sample.is_file():
                return self.reply(503, {"error":"Пример распознавания ещё не готов."})
            return self.reply(200, sample.read_bytes())
        if path.path == "/api/health":
            self.reply(200, {"service": "lex-hackathon"})
        elif public and path.path in {"/","/monitor"}:
            page = (ROOT / "monitor.html").read_bytes().replace(b"<body>",b'<body class="public-desk">')
            self.reply(200,page,"text/html; charset=utf-8")
        elif path.path == "/":
            self.reply(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path.path == "/demo":
            self.reply(200, (ROOT / "demo.html").read_bytes(), "text/html; charset=utf-8")
        elif path.path == "/videos":
            self.reply(200, (ROOT / "video_search.html").read_bytes(), "text/html; charset=utf-8")
        elif path.path == "/monitor":
            self.reply(200, (ROOT / "monitor.html").read_bytes(), "text/html; charset=utf-8")
        elif path.path == "/api/monitor":
            self.reply(200, monitor_state())
        elif path.path == "/api/video-search":
            with closing(connect()) as conn:
                self.reply(200, {"items": video_search.list_items(conn), "presets": video_search.PRESETS,
                                 "examples": video_search.EXAMPLES, "has_token": bool(os.getenv("APIFY_TOKEN"))})
        elif path.path == "/api/video-search/links":
            try:
                self.reply(200, video_search.external_links(parse_qs(path.query).get("query", [""])[0]))
            except ValueError as exc:
                self.reply(400, {"error": str(exc)})
        elif path.path in {"/demo-media/water.mp4", "/demo-media/securities.mp4"}:
            self.demo_video(path.path.rsplit("/", 1)[-1])
        elif path.path == "/api/demo":
            with closing(connect()) as conn:
                last = conn.execute("SELECT value FROM config WHERE key='demo_last_search'").fetchone()
                self.reply(200, {"cases": demo_cases.list_cases(conn), "has_token": bool(os.getenv("APIFY_TOKEN")),
                                 "last_search": last[0] if last else ""})
        elif self.path == "/setup":
            self.reply(200, (ROOT / "setup.html").read_bytes(), "text/html; charset=utf-8")
        elif path.path == "/api/state":
            try:
                self.reply(200, get_state(parse_qs(path.query), self.server.sources))
            except ValueError as exc:
                self.reply(400, {"error": str(exc)})
        else:
            self.reply(404, {"error": "Страница не найдена."})

    def reject_post(self):
        # Drain a small local request so Windows can deliver the rejection before closing the socket.
        length = self.headers.get("Content-Length", "")
        if length.isdecimal() and len(length) <= 5 and 0 < int(length) <= 48000:
            self.connection.settimeout(1)
            try:
                self.rfile.read(int(length))
            except OSError:
                pass
        return self.reply(403, {"error": "Откройте форму в локальном кабинете."})

    def media_upload(self):
        if self.headers.get("Origin") != (os.getenv("LEX_PUBLIC_ORIGIN") or "http://" + self.headers["Host"]):
            return self.reject_post()
        if self.headers.get_content_type() != "video/mp4":
            return self.reply(415, {"error":"Нужен файл MP4."})
        if not media_upload_lock.acquire(blocking=False):
            return self.reply(429, {"error":"Уже проверяется видео. Повторите через минуту."})
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= media_review.MAX_BYTES:
                return self.reply(413, {"error":"Нужен MP4 до 48 МиБ."})
            language = self.headers.get("X-Lex-Language", "auto")
            if language not in {"ru","uz","auto"}:
                raise ValueError("Выберите язык речи: русский, узбекский или авто.")
            day = self.headers.get("X-Lex-Published","")
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",day) or not auto_law._day(day):
                raise ValueError("Укажите дату публикации ролика.")
            with tempfile.TemporaryDirectory(prefix="lex-upload-") as tmp:
                path = Path(tmp) / "upload.mp4"
                remaining = length
                self.connection.settimeout(35)
                with path.open("wb") as file:
                    while remaining:
                        chunk = self.rfile.read(min(remaining,128*1024))
                        if not chunk:
                            raise ValueError("Файл передан не полностью.")
                        file.write(chunk)
                        remaining -= len(chunk)
                report = media_review.inspect_file(path, language=language, max_seconds=media_review.MAX_UPLOAD_SECONDS,
                                        reserve=lambda n:autonomous_monitor.reserve_voice(connect,n))
            row = dict(method="upload",platform="file",description="",published_at=day,media_report=report)
            return self.reply(200, {"report":report,"findings":auto_law.check_video(row)})
        except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
            return self.reply(400,{"error":str(exc) if isinstance(exc,ValueError) else "Не удалось прочитать или проверить MP4."})
        finally:
            media_upload_lock.release()

    def do_POST(self):
        if self.headers.get("X-Lex-Request") != "1":
            return self.reject_post()
        if os.getenv("LEX_PUBLIC_ORIGIN") and self.path not in {
                "/api/monitor/youtube-search","/api/monitor/youtube-preview","/api/monitor/media-upload","/api/monitor/demo","/api/monitor/live-search","/api/monitor/deepfakes"}:
            return self.reply(404, {"error":"Раздел недоступен."})
        if self.path == "/api/monitor/media-upload":
            return self.media_upload()
        if self.path == "/api/settings":
            return settings_request(self, ROOT / "settings.env")
        is_video = self.path.startswith("/api/video-search/")
        is_demo = is_video or self.path.startswith(("/api/demo/", "/api/monitor/"))
        if is_demo and (self.headers.get_all("Origin", []) != [os.getenv("LEX_PUBLIC_ORIGIN") or "http://" + self.headers["Host"]]
                        or self.headers.get_content_type() != "application/json"):
            return self.reject_post()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 48000:
                raise ValueError("Неверный размер запроса.")
            obj = json.loads(self.rfile.read(length))
            if is_demo and isinstance(obj, dict) and type(obj.get("id")) is int:
                obj["id"] = str(obj["id"])
            if not isinstance(obj, dict) or any(not isinstance(v, str) for v in obj.values()):
                raise ValueError("Неверный формат полей.")
            if is_video:
                return self.video_action(obj)
            if self.path == "/api/monitor/deepfakes":
                if os.getenv("LEX_DEEPFAKE_WORKER") != "1":
                    return self.reply(503,{"error":"Фоновый анализ выключен."})
                with closing(connect()) as conn:
                    _,created = deepfake_watch.request_scan(conn,manual=True,topic_id=obj.get("topic","auto"),custom=obj.get("custom",""),receipt=True)
                    return self.reply(202 if created else 200,dict(deepfake_watch.snapshot(conn),created=created))
            if self.path == "/api/monitor/live-search":
                with closing(connect()) as conn:
                    run_id, created = autonomous_monitor.queue_show(conn, monitor_options()[0])
                    data = autonomous_monitor.live_show_status(conn,run_id,*monitor_options())
                return self.reply(202 if created else 200, dict(data,created=created))
            if self.path == "/api/monitor/demo":
                autonomous_monitor.demo_run(connect)
                return self.reply(200, monitor_state())
            if self.path == "/api/monitor/youtube-preview":
                return self.reply(200, autonomous_monitor.preview_youtube(
                    monitor_options()[0], obj.get("url", ""), obj.get("subtitles", ""), connect))
            if self.path in {"/api/monitor/public-search", "/api/monitor/youtube-search"}:
                if not youtube_search_lock.acquire(blocking=False):
                    return self.reply(409, {"error": "Поиск уже идёт. Результат появится в кабинете."})
                try:
                    autonomous_monitor.search_youtube(connect, monitor_options()[0])
                    return self.reply(200, monitor_state())
                except ValueError as exc:
                    return self.reply(409, {"error": str(exc)})
                finally:
                    youtube_search_lock.release()
            if is_demo:
                return self.demo_action(obj)
            elif self.path == "/api/collect":
                schedule(self.server.pool, force=True, sources=self.server.sources)
                message = "Запрошено обновление подключённых источников."
            elif self.path == "/api/submit":
                message = submit(obj.get("url", "").strip(), obj.get("note", "").strip())
            elif self.path == "/api/article":
                row = lex_article(obj.get("url", "").strip(), obj.get("number", "").strip())
                with closing(connect()) as conn, conn:
                    save(conn, row, "article")
                message = "Текст статьи сохранён со ссылкой на редакцию страницы."
            else:
                return self.reply(404, {"error": "Неизвестная операция."})
            self.reply(200, {"message": message})
        except (ValueError, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except InstagramDiscoveryError as exc:
            self.reply(502, {"error": str(exc)})

    def video_action(self, obj):
        if self.path == "/api/video-search/find":
            platform = obj.get("platform", "")
            query = video_search.search_query(obj.get("query", ""))
            token = os.getenv("APIFY_TOKEN", "")
            if not token:
                return self.reply(409, {"error": "Для автоматического поиска подключите поисковый сервис в настройках. Ссылки на поиск и библиотеку Meta доступны сразу."})
            if not instagram_search_lock.acquire(blocking=False):
                return self.reply(409, {"error": "Предыдущий поиск ещё выполняется."})
            try:
                try:
                    rows, errors = video_search.discover(token, query, platform)
                except RuntimeError as exc:
                    return self.reply(502, {"error": str(exc)})
                with closing(connect()) as conn, conn:
                    added = sum(video_search.store_item(conn, row, query, "search") for row in rows)
                message = f"Видео в выдаче: {len(rows)}. Добавлено новых: {added}."
                return self.reply(200, {"message": " ".join([message, *errors]), "found":len(rows), "added":added})
            finally:
                instagram_search_lock.release()
        with closing(connect()) as conn, conn:
            if self.path == "/api/video-search/save":
                added = video_search.save_manual(conn, obj)
                message = "Видео добавлено на проверку." if added else "Эта ссылка уже есть в списке."
            elif self.path == "/api/video-search/review":
                video_search.review(conn, obj)
                message = "Источник и результат проверки сохранены."
            else:
                return self.reply(404, {"error":"Неизвестная операция."})
        self.reply(200, {"message":message})

    def demo_action(self, obj):
        if self.path == "/api/demo/search":
            token = os.getenv("APIFY_TOKEN", "")
            if not token:
                return self.reply(409, {"error": "Для живого поиска введите токен поиска в настройках."})
            if not instagram_search_lock.acquire(blocking=False):
                return self.reply(409, {"error": "Поиск уже выполняется. Дождитесь результатов."})
            try:
                query = obj.get("query", "").strip()
                rows = discover_instagram(token, query, obj.get("format", ""))
                with closing(connect()) as conn, conn:
                    added = demo_cases.add_live(conn, rows, query, verify=lex_passage)
                    conn.execute("INSERT OR REPLACE INTO config VALUES ('demo_last_search', ?)", (now(),))
                return self.reply(200, {"message": f"Найдено публикаций: {len(rows)}. Новых карточек: {added}.",
                                        "found": len(rows), "added": added})
            finally:
                instagram_search_lock.release()
        with closing(connect()) as conn, conn:
            if self.path in {"/api/demo/replay", "/api/demo/restart"}:
                if self.path == "/api/demo/restart":
                    conn.execute("DELETE FROM demo_cases WHERE mode='training'")
                result = demo_cases.replay(conn)
                result["message"] = (f"Проверено учебных публикаций: {result['scanned']}. "
                                     f"На сверку отобрано: {result['matched']}. Новых карточек: {result['added']}.")
            elif self.path in {"/api/demo/law", "/api/demo/review"}:
                case = demo_cases.get_case(conn, obj.get("id", ""))
                published = case.get("published_at")
                edition = ""
                if published:
                    stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    edition = stamp.astimezone(timezone(timedelta(hours=5))).strftime("%d.%m.%Y")
                if self.path == "/api/demo/law":
                    result = lex_passage(obj.get("url", ""), obj.get("excerpt", ""), edition)
                else:
                    if case["mode"] == "training" and any(key in obj for key in demo_cases.EDIT_FIELDS
                                                         if key != "assignee"):
                        raise ValueError("Учебный фрагмент и эталонная норма фиксированы.")
                    if case["mode"] == "live" and obj.get("status", case["status"]) == "assigned":
                        quote = obj.get("law_excerpt", case["law_excerpt"]).strip()
                        if not quote:
                            raise ValueError("Перед передачей приложите цитату из нормы Lex.uz.")
                        confirmed = lex_passage(obj.get("law_url", case["law_url"]), quote, edition)
                        obj["law_url"] = confirmed["url"]
                        obj["law_excerpt"] = quote
                    result = {"case": demo_cases.review(conn, obj), "message": "Решение сохранено в локальном кабинете."}
            else:
                return self.reply(404, {"error": "Неизвестная операция."})
        self.reply(200, result)


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--port", type=int, default=18864)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--brand24-only", action="store_true",
                        help="Run only the Brand24 collector alongside an existing Lex Monitor.")
    mode.add_argument("--demo-only", action="store_true", help="Run video monitoring without the general feed collectors.")
    args = parser.parse_args()
    load_settings()
    init()
    address = f"http://127.0.0.1:{args.port}"
    start_url = address + ("/monitor" if args.demo_only else "")
    try:
        server = ThreadingHTTPServer((os.getenv("LEX_BIND","127.0.0.1"), args.port), Desk)
    except OSError:
        if args.open:
            try:
                with urlopen(address + "/api/health", timeout=2) as response:
                    if json.load(response).get("service") == "lex-hackathon":
                        webbrowser.open(start_url)
                        return
            except (OSError, ValueError):
                pass
        raise
    stop = threading.Event()
    server.sources = () if args.demo_only else ("brand24",) if args.brand24_only else PLATFORMS
    with server, ThreadPoolExecutor(max_workers=max(1, len(server.sources))) as pool:
        server.pool = pool
        def ticker():
            while not stop.is_set():
                try:
                    changed = load_settings()
                except (OSError, UnicodeError):
                    changed = False
                schedule(pool, force=changed, sources=server.sources)
                stop.wait(2)
        threading.Thread(target=ticker, daemon=True).start()
        if not args.brand24_only:
            def monitor_loop(action, delay):
                while not stop.is_set():
                    try:
                        action()
                    except Exception as exc:
                        print(f"{action.__name__}: {type(exc).__name__}", flush=True)
                    stop.wait(delay)

            def scan_videos():
                autonomous_monitor.tick(connect, *monitor_options())

            def send_alerts():
                telegram_delivery.dispatch(connect)

            loops = [(scan_videos,15), (send_alerts,10)]
            if os.getenv("LEX_DEEPFAKE_WORKER") == "1":
                loops = [(lambda:deepfake_watch.scan(connect,monitor_options()[0]), 5),
                         (lambda:deepfake_watch.analyze(connect), 2),
                         (lambda:deepfake_watch.check_publication_text(connect), 9),
                         (lambda:deepfake_watch.notify(connect), 10),
                         (lambda:autonomous_monitor.tick(connect,monitor_options()[0],False),15),
                         (send_alerts,10)]
            if os.getenv("LEX_MEDIA_WORKER") == "1":
                loops.append((lambda:autonomous_monitor.media_tick(connect),5))
            for action, delay in loops:
                threading.Thread(target=monitor_loop, args=(action, delay), daemon=True).start()
        print(start_url, flush=True)
        if args.open:
            webbrowser.open(start_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()


if __name__ == "__main__":
    run()
