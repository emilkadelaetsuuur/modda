"""Automatic discovery of Uzbek public videos and bounded media inspection."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlencode
from urllib.request import build_opener, HTTPRedirectHandler, Request

import deepfake_detector as detector
import media_review
import risk_review
import autonomous_monitor
import video_facts
import watch_topics
import public_sources
import telegram_delivery
from telegram_source import _bot_api, TelegramRejected

PERIOD = 3 * 3600
# Public Uzbek news and fact-checking channels whose web preview shows native video.
CHANNELS = ("kunuz","qalampirlive","daryo","uzbekistan24","zamonuz","sputnikuz","factcheckeruz","kunuzru")
# Every post of a fact-checking channel is about a fake, so its latest posts are read on each topic.
FACTCHECK = ("factcheckeruz",)
# Only popular publications: fewer views than this are skipped (the fact-checking channel is exempt).
MIN_VIEWS = int(os.environ.get("LEX_MIN_VIEWS", "1000"))
YT_PER_RUN, TG_PER_CHANNEL, MAX_SECONDS = 6, 2, 600
FETCH_GAP = 0.35
REFERENCE_REPORT = Path(__file__).with_name("reference.json")
POST_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{2,31}/[1-9][0-9]{0,14}")
TG_HOST = re.compile(r"(?:[a-z0-9-]+\.)*(?:telesco\.pe|cdn-telegram\.org|telegram-cdn\.org|t\.me)")
LIMIT = 48 * 1024 * 1024

def safe_url(url):
    try:
        u = urlsplit(url)
        return u.scheme == "https" and u.username is None and u.port is None and TG_HOST.fullmatch(u.hostname or "")
    except ValueError:
        return False

class TelegramRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl):
            raise ValueError("Неизвестный адрес видео в Telegram.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def open_tg(url):
    if not safe_url(url):
        raise ValueError("Некорректный адрес видео.")
    return build_opener(TelegramRedirect()).open(Request(url,headers={"User-Agent":"Mozilla/5.0 LexMonitor/1.0"}), timeout=12)

def views(text):
    """Telegram's rounded view counter: 983, 9.83K, 1.2M."""
    matched = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([KkMm]?)", (text or "").strip())
    if not matched:
        return 0
    return round(float(matched[1].replace(",", ".")) * {"": 1, "k": 1000, "m": 1000000}[matched[2].lower()])

class Posts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.depth, self.counter = [], None, 0, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-post") and POST_ID.fullmatch(a["data-post"]):
            self.row = dict(key=a["data-post"].lower(), caption="", published="", media="", video=False, views=0)
            self.rows.append(self.row)
            self.depth = 0
        if self.row is None:
            return
        classes = a.get("class", "").split()
        self.counter = tag == "span" and "tgme_widget_message_views" in classes
        if tag == "video" or "tgme_widget_message_video_player" in classes:
            self.row["video"] = True
        if tag in ("video","source") and safe_url(a.get("src", "")):
            self.row["media"] = a["src"]
        if tag == "time" and a.get("datetime"):
            self.row["published"] = a["datetime"]
        if tag == "div" and (self.depth or "tgme_widget_message_text" in classes):
            self.depth += 1
        if tag == "br" and self.depth:
            self.row["caption"] += " "

    def handle_endtag(self, tag):
        self.counter = False
        if tag == "div" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.row is not None and self.counter:
            self.row["views"] = views(data)
        if self.row is not None and self.depth:
            self.row["caption"] += data

_paced, _last_fetch = threading.Lock(), [0.0]

def _pace():
    # One scan reads up to 16 channel searches. A short gap keeps the public preview from throttling us.
    with _paced:
        wait = _last_fetch[0] + FETCH_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.monotonic()

def feed(handle, query=""):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{2,31}(?:/[1-9][0-9]{0,14})?", handle):
        raise ValueError("Некорректный канал Telegram.")
    _pace()
    with open_tg("https://t.me/s/" + handle + ("?" + urlencode({"q":query}) if query else "")) as res:
        raw = res.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Слишком большая страница канала.")
    parser = Posts()
    parser.feed(raw.decode("utf8","replace"))
    if not parser.rows and not (query and b"tme_no_messages_found" in raw):
        raise ValueError("Лента канала недоступна.")
    return [p for p in parser.rows if p["key"].split("/")[0] == handle.lower().split("/")[0] and p["video"]]

def telegram_file(key, directory):
    row = next((p for p in feed(key) if p["key"] == key and p["media"]), None)
    if not row:
        raise ValueError("Telegram не предоставил файл этой публикации.")
    path = directory / "source.mp4"
    with open_tg(row["media"]) as res, path.open("wb") as f:
        if int(res.headers.get("Content-Length") or 0) > LIMIT:
            raise ValueError("Размер видео превышает 48 МиБ.")
        total, start = 0, time.monotonic()
        while chunk := res.read(128 * 1024):
            total += len(chunk)
            if total > LIMIT or time.monotonic() - start > 75:
                raise ValueError("Видео превысило лимит загрузки.")
            f.write(chunk)
    return path

def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS df_notices(key TEXT PRIMARY KEY,state TEXT NOT NULL,retry INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS df_runs(id INTEGER PRIMARY KEY, started INTEGER NOT NULL,
        status TEXT NOT NULL, origin TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS df_videos(key TEXT PRIMARY KEY, platform TEXT NOT NULL, url TEXT NOT NULL,
        title TEXT NOT NULL, published TEXT NOT NULL, added INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        attempt INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0, report TEXT NOT NULL DEFAULT '{}')""")

    columns = {r[1] for r in conn.execute("PRAGMA table_info(df_videos)")}
    for field in ("author","description"):
        if field not in columns:
            conn.execute(f"ALTER TABLE df_videos ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    # uz: 1 related to Uzbekistan, 0 not related, -1 not classified yet (rows saved before the filter).
    for field,default in (("uz",-1),("views",0)):
        if field not in columns:
            conn.execute(f"ALTER TABLE df_videos ADD COLUMN {field} INTEGER NOT NULL DEFAULT {default}")
    for key,platform,title,description,author in conn.execute(
            "SELECT key,platform,title,description,author FROM df_videos WHERE uz=-1").fetchall():
        related = watch_topics.uzbek_related(title+" "+description,platform,author if platform=="YouTube" else "")
        conn.execute("UPDATE df_videos SET uz=? WHERE key=?",(int(related),key))

    run_columns = {r[1] for r in conn.execute("PRAGMA table_info(df_runs)")}
    for field,default in (("topic_id","auto"),("custom_query","")):
        if field not in run_columns:
            conn.execute(f"ALTER TABLE df_runs ADD COLUMN {field} TEXT NOT NULL DEFAULT '{default}'")
    conn.execute("""CREATE TABLE IF NOT EXISTS df_run_items(run_id INTEGER,key TEXT,PRIMARY KEY(run_id,key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS df_claims(key TEXT PRIMARY KEY, result TEXT NOT NULL)""")

def request_scan(conn, manual=False, topic_id="auto", custom="", receipt=False):
    topic_id,custom = watch_topics.selection(topic_id,custom)
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE df_runs SET status='interrupted' WHERE status='running' AND started<?",(now-600,))
    last = conn.execute("SELECT id,started,status FROM df_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_auto = conn.execute("SELECT MAX(started) FROM df_runs WHERE origin='schedule'").fetchone()[0]
    cap = conn.execute("SELECT count(*) FROM df_runs WHERE origin='manual' AND started>?",(now-86400,)).fetchone()[0]
    limited = (manual and last and (now-last[1]<180 or cap>=8)) or (not manual and last_auto and now-last_auto<PERIOD)
    if last and (last[2] in ('queued','running') or limited):
        conn.commit()
        return (last[0],False) if receipt else last[0]
    run_id = conn.execute("INSERT INTO df_runs(started,status,origin,topic_id,custom_query) VALUES(?,'queued',?,?,?)",
                          (now,"manual" if manual else "schedule",topic_id,custom)).lastrowid
    conn.commit()
    return (run_id,True) if receipt else run_id

def rank(topic, text, count):
    """Download order: the most watched first; among equally popular ones signs of fraud or a false claim, then a
    mention of a fake; for claims, testable facts."""
    if topic.kind in ("risk","deepfake"):
        return count, risk_review.score(text), watch_topics.deepfake_hint(text)
    return count, watch_topics.priority(text)

def youtube_popular(api_key, topic, since):
    """The period's most watched Uzbek videos for the topic: Shorts and other videos up to 10 minutes.

    Each query costs two searches (200 quota units); the second query runs only when the first gives too few."""
    queries, seen, picked, examined, other, low, failure = [], set(), {}, 0, 0, 0, None
    for query in topic.youtube:
        try:
            ids = []
            for duration in ("short", "medium"):
                found = public_sources.youtube_discover(api_key, query, since, duration=duration, order="viewCount")
                ids += [public_sources._video_id(p["url"]) for p in found[:25]]
            queries.append(query)
            ids = [i for i in dict.fromkeys(ids) if i and i not in seen][:50]
            seen.update(ids)
            details = public_sources.youtube_details(api_key, ids) if ids else []
        except ValueError as exc:
            failure = exc
            break
        for p in details:
            key, text = public_sources._video_id(p["url"]), p["title"]+" "+p.get("description","")
            seconds = p.get("duration")
            if key in picked or (seconds is not None and not 0 < seconds <= MAX_SECONDS):
                continue
            examined += 1
            if p.get("views", 0) < MIN_VIEWS:
                low += 1
                continue
            if not watch_topics.relevant(topic, text):
                continue
            if not watch_topics.uzbek_related(text, "YouTube", p.get("account",""), p.get("audio_language","")):
                other += 1
                continue
            picked[key] = p
        if len(picked) >= YT_PER_RUN:
            break
    if failure and not queries:
        raise failure
    best = sorted(picked.values(), key=lambda p: rank(topic, p["title"]+" "+p.get("description",""), p.get("views",0)), reverse=True)
    return best[:YT_PER_RUN], queries, {"count": min(len(best), YT_PER_RUN), "examined": examined, "other": other, "low": low}

def scan(connect, api_key):
    with closing(connect()) as db:
        request_scan(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            task=db.execute("SELECT id,topic_id,custom_query,origin FROM df_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not task:
                return
            db.execute("UPDATE df_runs SET status='running' WHERE id=?",(task[0],))
            turn=db.execute("SELECT count(*) FROM df_runs WHERE origin=? AND topic_id='auto' AND id<?",
                            (task[3],task[0])).fetchone()[0]
    topic=watch_topics.resolve(task[1],task[2],turn)
    since=datetime.now(timezone.utc)-timedelta(days=30)
    stats,rows={},[]
    try:
        found,queries,stats["YouTube"]=youtube_popular(api_key,topic,since.isoformat())
        for p in found:
            key=public_sources._video_id(p["url"])
            rows.append(("yt:"+key,"YouTube",p["url"],p["title"],p["published_at"],p.get("account",""),p.get("description",""),p.get("views",0)))
    except Exception:
        queries=list(topic.youtube[:1])
        stats["YouTube"]={"error":"Поиск или получение полного описания YouTube временно недоступны."}
    for channel in CHANNELS:
        try:
            found={}
            for term in topic.telegram+(("",) if channel in FACTCHECK and topic.kind in ("risk","deepfake") else ()):
                for post in feed(channel,term):
                    if post["published"] and datetime.fromisoformat(post["published"])>=since:
                        found[post["key"]]=post
            relevant=[p for p in found.values() if watch_topics.relevant(topic,p["caption"])]
            related=[p for p in relevant if watch_topics.uzbek_related(p["caption"],"Telegram")]
            popular=[p for p in related if channel in FACTCHECK or p.get("views",0)>=MIN_VIEWS]
            posts=sorted(popular,key=lambda p:(*rank(topic,p["caption"],p.get("views",0)),p["published"]),reverse=True)[:TG_PER_CHANNEL]
            for p in posts:
                caption=p["caption"].strip()
                rows.append(("tg:"+p["key"],"Telegram","https://t.me/"+p["key"],caption[:350] or "Видео канала @"+channel,
                             p["published"],"@"+channel,caption,p.get("views",0)))
            stats["@"+channel]={"count":len(posts),"examined":len(found),"other":len(relevant)-len(related),"low":len(related)-len(popular)}
        except Exception:
            stats["@"+channel]={"error":"Тематический поиск в канале временно недоступен."}
    now, added = int(time.time()), 0
    with closing(connect()) as db, db:
        for row in rows:
            added += db.execute("""INSERT OR IGNORE INTO df_videos(key,platform,url,title,published,author,description,views,uz,added)
                                   VALUES(?,?,?,?,?,?,?,?,1,?)""",(*row,now)).rowcount
            first_full=not db.execute("SELECT 1 FROM df_run_items WHERE key=? LIMIT 1",(row[0],)).fetchone()
            db.execute("INSERT OR IGNORE INTO df_run_items VALUES(?,?)",(task[0],row[0]))
            previous=db.execute("SELECT title,description FROM df_videos WHERE key=?",(row[0],)).fetchone()
            db.execute("UPDATE df_videos SET uz=1,views=MAX(views,?) WHERE key=?",(row[7],row[0]))
            if previous!=(row[3],row[6]):
                db.execute("UPDATE df_videos SET title=?,author=?,description=? WHERE key=?",(row[3],row[5],row[6],row[0]))
            if first_full or previous!=(row[3],row[6]):
                db.execute("DELETE FROM df_claims WHERE key=?",(row[0],))
        detail={"sources":stats,"topic":" / ".join(queries),"label":topic.label,"kind":topic.kind,
                "telegram_queries":topic.telegram,"new":added,"completed":now}
        db.execute("UPDATE df_runs SET status=?,detail=? WHERE id=?",
                   ("finished" if any("count" in s for s in stats.values()) else "failed",json.dumps(detail),task[0]))

def save_facts(connect, row, media=None):
    with closing(connect()) as db:
        full=db.execute("SELECT 1 FROM df_run_items WHERE key=? LIMIT 1",(row[0],)).fetchone()
    if full:
        result=video_facts.check(*row[1:],media=media)
    else:
        result=dict(findings=[],coverage=[],issues=["Эта запись из предыдущего обхода с сокращённым описанием. Для сверки нужен полный контекст."])
    # Recognised speech and frame captions are kept as excerpts: the risk review quotes what was said in the video.
    speech=(media or {}).get("speech") or {}
    said=speech.get("text") or " ".join(x.get("text","") for x in speech.get("segments") or [])
    if said.strip():
        result["speech"]=" ".join(said.split())[:4000]
    shown=" \n".join(x.get("text","") for x in (media or {}).get("frames") or [] if x.get("text"))
    if shown.strip():
        result["ocr"]=shown[:1500]
    with closing(connect()) as db, db:
        if media is None:
            db.execute("INSERT OR IGNORE INTO df_claims VALUES(?,?)",(row[0],json.dumps(result)))
        else:
            db.execute("INSERT OR REPLACE INTO df_claims VALUES(?,?)",(row[0],json.dumps(result)))

def check_publication_text(connect):
    with closing(connect()) as db:
        rows=db.execute("""SELECT v.key,v.platform,v.title,v.description,v.published FROM df_videos v
            LEFT JOIN df_claims c ON v.key=c.key WHERE c.key IS NULL AND v.uz=1 ORDER BY v.views DESC,v.added DESC LIMIT 4""").fetchall()
    for row in rows:
        save_facts(connect,row)

def analyze(connect):
    now = int(time.time())
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE df_videos SET status='unavailable',report='{"reason":"Проверка была прервана."}'
                      WHERE status='checking' AND claimed<?""",(now-600,))
        # Alternate platforms when both have pending work. Retry download failures once.
        row = db.execute("""SELECT key,platform,title,description,published FROM df_videos
            WHERE uz=1 AND (status='queued' OR (status='unavailable' AND attempt<2 AND claimed<?))
            ORDER BY attempt, (platform=COALESCE((SELECT platform FROM df_videos WHERE claimed>0 ORDER BY claimed DESC LIMIT 1),'Telegram')),
            views DESC,added DESC,published DESC LIMIT 1""",(now-3600,)).fetchone()
        if not row:
            return
        db.execute("UPDATE df_videos SET status='checking',claimed=?,attempt=attempt+1 WHERE key=?",(now,row[0]))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            key = row[0][3:]
            path = media_review.download_public(key,directory) if row[1]=="YouTube" else telegram_file(key,directory)
            report = detector.inspect(path)
            status = report["state"]
            with closing(connect()) as db, db:
                db.execute("UPDATE df_videos SET status=?,report=? WHERE key=?",(status,json.dumps(report),row[0]))
            try:
                media=media_review.inspect_file(path,reserve=lambda seconds:autonomous_monitor.reserve_voice(connect,seconds))
            except Exception:
                media={"errors":["Речь и кадры недоступны."]}
            # An unavailable text service must not invalidate the media verdict already saved.
            try:
                save_facts(connect,row,media)
            except (KeyError,TypeError,ValueError):
                save_facts(connect,row,{"errors":["Неполный ответ распознавания."]})
            return
    except Exception as exc:
        status = "unavailable"
        report = {"reason":str(exc)[:180] if type(exc) is ValueError else "Файл недоступен для анализа звука и кадров."}
    with closing(connect()) as db, db:
        db.execute("UPDATE df_videos SET status=?,report=? WHERE key=?",(status,json.dumps(report),row[0]))

def notify(connect):
    token, chat = telegram_delivery._connection_settings()
    if not token or not telegram_delivery.valid_destination(chat):
        return
    now = int(time.time())
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT v.key,v.url,v.title,v.published,v.status,v.report FROM df_videos v
            LEFT JOIN df_notices n ON n.key=v.key WHERE v.status IN ('review','confirmed') AND v.uz=1
            AND (n.key IS NULL OR (n.state='retry' AND n.retry<=?)) LIMIT 1""",(now,)).fetchone()
        if not row:
            return
        db.execute("INSERT OR REPLACE INTO df_notices VALUES(?,'sending',?)",(row[0],now))
    report = json.loads(row[5])
    label = "Совпадение с документированным дипфейком" if row[4]=="confirmed" else "Сигнал для ручной проверки"
    message = "\n".join(filter(None,[label,row[2][:200],row[3],row[1],report.get("reason"),report.get("reference")]))
    state, retry = "sent", 0
    try:
        res = _bot_api(token,"sendMessage",{"chat_id":chat,"text":message,"link_preview_options":{"is_disabled":True}})
        if not isinstance(res, dict) or type(res.get("message_id")) is not int:
            state = "uncertain"
    except TelegramRejected as exc:
        state, retry = ("retry",now+max(180,exc.retry_after or 0)) if 400<=exc.status<500 else ("uncertain",0)
    except ValueError:
        state = "uncertain"
    with closing(connect()) as db, db:
        db.execute("UPDATE df_notices SET state=?,retry=? WHERE key=?",(state,retry,row[0]))

def found_by(conn, keys):
    """The first scan that brought each publication: its topic, queries and launch. The feed shows why a video was picked."""
    first = {}
    if not keys:
        return first
    marks = ",".join("?"*len(keys))
    for key,run,started,origin,topic_id,custom,detail in conn.execute(f"""SELECT i.key,r.id,r.started,r.origin,r.topic_id,r.custom_query,r.detail
            FROM df_run_items i JOIN df_runs r ON r.id=i.run_id WHERE i.key IN ({marks}) ORDER BY r.id""",keys):
        if key in first:
            first[key]["runs"] += 1
            continue
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            d = {}
        topic = watch_topics.DEEPFAKE_TOPICS.get(topic_id) or watch_topics.TOPICS.get(topic_id)
        first[key] = dict(run=run, started=started, origin=origin, label=d.get("label") or custom or (topic.label if topic else ""),
                          kind=d.get("kind") or (topic.kind if topic else ""), youtube=d.get("topic",""),
                          telegram=list(d.get("telegram_queries") or (topic.telegram if topic else ())), runs=1)
    return first

def selection_marks(title, description):
    """Words that passed the filters: the tie to Uzbekistan and a mention of a fake or AI (ranking only, never a verdict)."""
    folded = watch_topics._fold(title+" "+description)
    tie = watch_topics._UZ.search(watch_topics._SIGNATURE.sub(" ",folded))
    hint = watch_topics._HINT.search(folded)
    return dict(uz=tie[0].strip() if tie else "", hint=hint[0].strip() if hint else "")

def snapshot(conn):
    last = conn.execute("SELECT id,started,status,origin,detail,topic_id,custom_query FROM df_runs ORDER BY id DESC LIMIT 1").fetchone()
    # Only publications related to Uzbekistan are shown; older unrelated rows stay in the database for audit.
    rows=conn.execute("""SELECT v.key,v.platform,v.url,v.title,v.published,v.status,v.report,v.author,v.description,v.views,c.result,v.attempt
                         FROM df_videos v LEFT JOIN df_claims c ON v.key=c.key WHERE v.uz=1
                         ORDER BY v.key IN (SELECT key FROM df_run_items WHERE run_id=?) DESC,v.added DESC,v.published DESC LIMIT 100""",(last[0] if last else 0,)).fetchall()
    found = found_by(conn,[r[0] for r in rows])
    items=[]
    for k,p,u,t,d,s,r,a,desc,n,f,att in rows:
        facts=json.loads(f) if f else None
        risk=risk_review.assess(p,a,k,t,desc,facts)
        if facts:
            # The page needs whether speech and frames were read, not the full transcript every five seconds.
            facts["speech_read"],facts["ocr_read"]=bool(facts.pop("speech","")),bool(facts.pop("ocr",""))
        items.append(dict(key=k,platform=p,url=u,title=t,published=d,status=s,report=json.loads(r),author=a,
                          description=desc,views=n,facts=facts,attempts=att,risk=risk,
                          found=found.get(k),marks=selection_marks(t,desc)))
    levels={"high":0,"medium":0,"low":0}
    for i in items:
        levels[i["risk"]["level"]]+=1
    current=[r[0] for r in conn.execute("""SELECT i.key FROM df_run_items i JOIN df_videos v ON v.key=i.key
                                           WHERE i.run_id=? AND v.uz=1""",(last[0],))] if last else []
    reference = dict(detector.REFERENCE)
    try:
        proof = json.loads(REFERENCE_REPORT.read_text(encoding="utf8"))
        if proof.get("exact_match") and proof.get("sha256")==reference["sha256"]:
            reference["report"] = proof
    except (OSError,ValueError):
        pass
    return dict(items=items, current_keys=current, topics={k:v.label for k,v in watch_topics.TOPICS.items()},
                deepfake_topics={k:v.label for k,v in watch_topics.DEEPFAKE_TOPICS.items()}, channels=CHANNELS, interval=PERIOD, min_views=MIN_VIEWS, reference=reference,
                counts=dict(conn.execute("SELECT status,COUNT(*) FROM df_videos WHERE uz=1 GROUP BY status")), risk_counts=levels,
                run=dict(id=last[0],started=last[1],status=last[2],origin=last[3],topic_id=last[5],custom=last[6],**json.loads(last[4])) if last else None)
