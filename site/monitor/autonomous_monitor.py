"""Durable local queue for checking public video statements against dated legal text."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import media_review
import public_sources
import auto_law
import telegram_delivery
import video_search


DISCOVERY_INTERVAL = 6 * 3600
DAILY_LIMIT = 4
SHOW_LIMIT = 6
SHOW_INTERVAL = 120
SHOW_BATCH = 5
LEASE_SECONDS = 5 * 60
RETRY_SECONDS = 15 * 60
LIVE_BATCH = 20
QUERIES = (
    "Ташкент квартиры продажа", "Toshkent kvartira sotiladi", "акции Узбекистан доходность",
    "O‘zbekiston aksiya daromad", "минимальный отпуск Узбекистан", "O‘zbekiston yillik mehnat ta'tili",
)
REQUIRED = ("rule_id", "category", "title", "quote", "reason", "law_url", "law_excerpt", "law_label", "where")


def initialize(conn):
    """Install the queue, including pending work for videos collected before installation."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    video_search.initialize(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS auto_jobs (
        mode TEXT NOT NULL, source_key TEXT NOT NULL, training_json TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', attempted_at INTEGER NOT NULL DEFAULT 0,
        next_at INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        owner TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', PRIMARY KEY (mode, source_key))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS auto_pending ON auto_jobs(mode, status, next_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS auto_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, source_key TEXT NOT NULL,
        rule_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, first_payload TEXT NOT NULL, payload TEXT NOT NULL,
        UNIQUE(mode, source_key, rule_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS auto_searches (
        id INTEGER PRIMARY KEY, started INTEGER NOT NULL, platform TEXT NOT NULL, query TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running', error TEXT NOT NULL DEFAULT '', found INTEGER NOT NULL DEFAULT 0,
        last_progress INTEGER NOT NULL DEFAULT 0)""")
    if "last_progress" not in {c[1] for c in conn.execute("PRAGMA table_info(auto_searches)")}:
        conn.execute("ALTER TABLE auto_searches ADD COLUMN last_progress INTEGER NOT NULL DEFAULT 0")
    columns = {c[1] for c in conn.execute("PRAGMA table_info(auto_searches)")}
    for column, declaration in (("origin", "TEXT NOT NULL DEFAULT 'schedule'"),
                                ("receipt", "TEXT NOT NULL DEFAULT ''"), ("sent_at", "INTEGER NOT NULL DEFAULT 0"),
                                ("received_at", "INTEGER NOT NULL DEFAULT 0"), ("listed", "INTEGER NOT NULL DEFAULT 0")):
        if column not in columns:
            conn.execute(f"ALTER TABLE auto_searches ADD COLUMN {column} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS auto_search_dates ON auto_searches(started)")

    eligible = "{prefix}platform = 'youtube' AND {prefix}method IN ('search','feed','public_feed')"
    incoming = eligible.format(prefix="NEW.")
    conn.execute("DROP TRIGGER IF EXISTS auto_new_video")
    conn.execute(f"""CREATE TRIGGER auto_new_video AFTER INSERT ON video_candidates
        WHEN {incoming} BEGIN
          INSERT OR IGNORE INTO auto_jobs(mode,source_key) VALUES ('youtube',NEW.identity);
        END""")
    conn.execute("DROP TRIGGER IF EXISTS auto_changed_video")
    conn.execute(f"""CREATE TRIGGER auto_changed_video
        AFTER UPDATE OF video_title,description,published_at,url,method,platform,account,speech ON video_candidates
        WHEN (NEW.platform='youtube' OR OLD.platform='youtube') AND (NEW.video_title IS NOT OLD.video_title OR NEW.description IS NOT OLD.description OR NEW.published_at IS NOT OLD.published_at
          OR NEW.url IS NOT OLD.url OR NEW.method IS NOT OLD.method OR NEW.platform IS NOT OLD.platform
          OR NEW.account IS NOT OLD.account OR NEW.speech IS NOT OLD.speech) BEGIN
          UPDATE auto_alerts SET active=0 WHERE mode='live' AND source_key=OLD.identity;
          INSERT INTO auto_jobs(mode,source_key,status)
            VALUES ('youtube', NEW.identity, CASE WHEN {incoming} THEN 'pending' ELSE 'skipped' END)
            ON CONFLICT(mode,source_key) DO UPDATE SET status=excluded.status, owner='', attempted_at=0,
                failures=0, next_at=0, error='';
        END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS auto_removed_video AFTER DELETE ON video_candidates BEGIN
          UPDATE auto_alerts SET active=0 WHERE mode='live' AND source_key=OLD.identity;
          DELETE FROM auto_jobs WHERE mode IN ('live','youtube') AND source_key=OLD.identity;
        END""")
    conn.execute(f"""INSERT OR IGNORE INTO auto_jobs(mode,source_key)
        SELECT 'youtube',identity FROM video_candidates WHERE {eligible.format(prefix="")}""")
    conn.execute("""CREATE TABLE IF NOT EXISTS media_checks (
        identity TEXT PRIMARY KEY, status TEXT NOT NULL, retry_at INTEGER NOT NULL, report TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '')""")
    conn.execute("CREATE TABLE IF NOT EXISTS voice_usage(day TEXT PRIMARY KEY, seconds INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS auto_versions (name TEXT PRIMARY KEY)")
    migrate = conn.execute("INSERT OR IGNORE INTO auto_versions VALUES ('dated-caption-or-speech')")
    if migrate.rowcount:
        # Older installations cached all checked rows as ready, including missing inputs.
        conn.execute("UPDATE auto_jobs SET status='pending',owner='',next_at=0 WHERE status='ready'")
        conn.execute("UPDATE auto_alerts SET active=0")
    # Each rule change re-runs the rules over the cached captions and media once:
    # hedges-near-quote — a negation or question anywhere in the speech no longer drops the video,
    #   prices in titles and frames are matched;
    # conventional-units — prices in "у.е." count as a dollar equivalent;
    # dollar-before-units — an explicit dollar price is quoted in preference to "у.е.".
    for version in ("hedges-near-quote", "conventional-units", "dollar-before-units"):
        if conn.execute("INSERT OR IGNORE INTO auto_versions VALUES (?)", (version,)).rowcount:
            conn.execute("UPDATE auto_jobs SET status='pending',owner='',next_at=0 WHERE status='ready'")
    conn.execute("""UPDATE auto_jobs SET status='skipped',owner='' WHERE mode='live'
                    AND status!='skipped'""")
    conn.execute("UPDATE auto_alerts SET active=0 WHERE mode='live' AND source_key NOT LIKE 'yt:%' AND active=1")
    conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY,value TEXT NOT NULL)")


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds") if stamp else ""


def _connection(connect):
    return connect() if callable(connect) else sqlite3.connect(connect, timeout=10)


def _day_start(stamp):
    return stamp - stamp % 86400


def _reserve(conn, stamp):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        last, total = conn.execute("SELECT max(started), count(*) FROM auto_searches WHERE platform='youtube' AND origin='schedule'").fetchone()
        if last is not None and stamp < last + DISCOVERY_INTERVAL:
            return None
        calls, = conn.execute("SELECT count(*) FROM auto_searches WHERE platform='youtube' AND origin='schedule' AND started>=?",
                              (_day_start(stamp),)).fetchone()
        if calls >= DAILY_LIMIT:
            return None
        query = QUERIES[total % len(QUERIES)]
        attempt = conn.execute("INSERT INTO auto_searches(started,platform,query) VALUES (?,'youtube',?)",
                               (stamp, query)).lastrowid
        return attempt, query


def _discover(conn, api_key):
    stamp = int(time.time())
    reservation = _reserve(conn, stamp)
    if not reservation:
        return
    _fetch_batch(conn, api_key, reservation, 20, True)


def _fetch_batch(conn, api_key, reservation, limit, scheduled):
    attempt, query = reservation
    stamp = int(time.time())
    try:
        with conn:
            conn.execute("UPDATE auto_searches SET sent_at=? WHERE id=?", (stamp, attempt))
        hits = public_sources.youtube_discover(api_key, query, _iso(stamp - 30 * 86400))
        ids = list(dict.fromkeys(public_sources._video_id(hit["url"]) for hit in hits))[:limit]
        with conn:
            conn.execute("UPDATE auto_searches SET received_at=?, listed=? WHERE id=?", (int(time.time()),len(ids),attempt))
        rows = public_sources.youtube_details(api_key, ids)
        receipt = dict(scanned=len(ids), verified=len(rows), checked_at=_iso(stamp), query=query, sources=[], errors=[])
        with conn:
            for row in rows:
                added = video_search.store_item(conn, row, query, "search")
                _, url, identity = video_search._video_url(row["url"])
                receipt["sources"].append({"identity": identity, "url": url, "new":bool(added)})
            payload = json.dumps(receipt, ensure_ascii=False)
            if scheduled:
                conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES ('youtube_monitor_search',?)",(payload,))
            conn.execute("UPDATE auto_searches SET status='ready',found=?,last_progress=?,receipt=? WHERE id=?",
                         (len(rows),int(time.time()),payload,attempt))
    except Exception as exc:
        reason = str(exc).replace(api_key, "…")[:500] or "Поиск YouTube не завершён."
        with conn:
            conn.execute("UPDATE auto_searches SET status='error',error=?,last_progress=? WHERE id=?",
                         (reason,int(time.time()),attempt))


def queue_show(conn, api_key):
    if not api_key:
        raise ValueError("Ключ YouTube не настроен.")
    now = int(time.time())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT id,started,status FROM auto_searches WHERE origin='show' ORDER BY id DESC LIMIT 1").fetchone()
        if last and (now-last[1] < SHOW_INTERVAL or last[2] == "queued" or
                     (last[2] == "running" and now-last[1] < 2*LEASE_SECONDS)):
            return last[0], False
        used, = conn.execute("SELECT count(*) FROM auto_searches WHERE origin='show' AND started>=?", (_day_start(now),)).fetchone()
        if used >= SHOW_LIMIT:
            raise ValueError("Сегодня использованы все живые показы. Последние результаты доступны ниже.")
        count, = conn.execute("SELECT count(*) FROM auto_searches WHERE platform='youtube'").fetchone()
        query = QUERIES[count % len(QUERIES)]
        run = conn.execute("""INSERT INTO auto_searches(started,platform,origin,query,status)
                              VALUES (?,'youtube','show',?,'queued')""",(now,query)).lastrowid
        return run, True


def _take_show(conn):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT id,query FROM auto_searches WHERE origin='show' AND status='queued' ORDER BY id LIMIT 1").fetchone()
        if job:
            conn.execute("UPDATE auto_searches SET status='running',last_progress=? WHERE id=?", (int(time.time()),job[0]))
        return job


def _job_mode(mode):
    return "youtube" if mode == "live" else mode


def _claim(conn, mode):
    stamp = int(time.time())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("""SELECT source_key, training_json FROM auto_jobs WHERE mode=?
            AND (mode='demo' OR source_key LIKE 'yt:%') AND status IN ('pending','error','running') AND next_at <= ?
            ORDER BY attempted_at,source_key LIMIT 1""", (_job_mode(mode), stamp)).fetchone()
        if job is None:
            return None
        key, fixture = job
        if mode == "demo":
            row = json.loads(fixture)
        else:
            cursor = conn.execute("SELECT * FROM video_candidates WHERE identity=?", (key,))
            row = dict(zip((c[0] for c in cursor.description), cursor.fetchone()))
            try:
                row["speech"] = json.loads(row["speech"] or "[]")
            except (ValueError, TypeError):
                row["speech"] = []
            media = conn.execute("SELECT report FROM media_checks WHERE identity=? AND status IN ('ready','partial')", (key,)).fetchone()
            if media:
                row["media_report"] = json.loads(media[0])
        owner = uuid.uuid4().hex
        conn.execute("""UPDATE auto_jobs SET status='running',owner=?,attempted_at=?,next_at=?
            WHERE mode=? AND source_key=?""", (owner, stamp, stamp + LEASE_SECONDS, _job_mode(mode), key))
        return key, row, owner


def _alert(row, result, demo):
    if any(not isinstance(result.get(field), str) or not result[field].strip() for field in REQUIRED):
        raise ValueError("Не хватает доказательства или проверенной нормы Lex.uz.")
    ref = urlsplit(result["law_url"])
    kind = result.get("evidence_kind", "caption")
    media = row.get("media_report", {})
    if kind == "speech":
        segments = media.get("speech", {}).get("segments") or row.get("speech", [])
        source = " ".join(s["text"] for s in segments if isinstance(s, dict) and isinstance(s.get("text"), str))
    elif kind == "frame":
        source = " ".join(f["text"] for f in media.get("frames", []) if f["start"] == result.get("start"))
    elif kind == "voice_text":
        source = media.get("speech", {}).get("text", "")
    elif kind == "title":
        source = row["video_title"]
    else:
        source = row["description"]
    if (ref.scheme != "https" or ref.netloc.lower() not in {"lex.uz", "www.lex.uz"} or not ref.fragment
            or kind not in {"caption", "speech", "title", "frame", "voice_text"} or " ".join(result["quote"].split()) not in " ".join(source.split())):
        raise ValueError("Не подтверждена дословная цитата публикации или ссылка на норму Lex.uz.")
    return {**{field:result[field] for field in REQUIRED}, "platform":row["platform"],
            "url":row["url"], "original":row["url"], "published_at":row["published_at"],
            "account":row.get("account", ""), "evidence_kind":kind, "is_demo":demo,
            "demo_media_url":row.get("demo_media_url", "") if demo else "",
            **{field:result[field] for field in ("start", "end") if field in result}}


def _save_alert(conn, mode, key, alert, stamp):
    payload = json.dumps(alert, ensure_ascii=False)
    identity = (mode, key, alert["rule_id"])
    created = conn.execute("""INSERT INTO auto_alerts
        (mode,source_key,rule_id,created_at,updated_at,first_payload,payload)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(mode,source_key,rule_id) DO NOTHING""",
        (*identity, stamp, stamp, payload, payload))
    if not created.rowcount:
        conn.execute("""UPDATE auto_alerts SET active=1, updated_at=?,payload=?
            WHERE mode=? AND source_key=? AND rule_id=?""", (stamp, payload, *identity))
    elif mode == "live":
        telegram_delivery.enqueue(conn, created.lastrowid, alert)


def _work(conn, mode, limit):
    for _ in range(limit):
        job = _claim(conn, mode)
        if not job:
            break
        key, row, owner = job
        demo = mode == "demo"
        try:
            if not auto_law.can_check(row):
                with conn:
                    conn.execute("""UPDATE auto_jobs SET status='insufficient',owner='',error='',next_at=0,failures=0
                        WHERE mode=? AND source_key=? AND owner=?""", (_job_mode(mode), key, owner))
                continue
            alerts = [_alert(row, result, demo) for result in auto_law.check_video(row, training=demo)]
        except Exception as exc:
            with conn:
                conn.execute("""UPDATE auto_jobs SET status='error',owner='',error=?,failures=failures+1,
                    next_at=? + min(?, ? * (1 << min(failures,5))) WHERE mode=? AND source_key=? AND owner=?""",
                    (str(exc)[:500] or "Ошибка сверки с Lex.uz.", int(time.time()), DISCOVERY_INTERVAL,
                     RETRY_SECONDS, _job_mode(mode), key, owner))
            continue
        with conn:
            # A caption edit or reclaimed lease invalidates this worker's result.
            completed = conn.execute("""UPDATE auto_jobs SET status='ready',owner='',error='',next_at=0,failures=0
                WHERE mode=? AND source_key=? AND owner=?""", (_job_mode(mode), key, owner))
            if completed.rowcount:
                for alert in alerts:
                    _save_alert(conn, mode, key, alert, int(time.time()))


def tick(connect, api_key="", enabled=False):
    with closing(_connection(connect)) as conn:
        if api_key:
            show = _take_show(conn)
            if show:
                _fetch_batch(conn, api_key, show, SHOW_BATCH, False)
        if enabled is True and api_key:
            _discover(conn, api_key)
        _work(conn, "live", LIVE_BATCH)


def reserve_voice(connect, seconds):
    day = datetime.now(timezone.utc).date().isoformat()
    with closing(_connection(connect)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        limit = int(os.environ.get("LEX_STT_DAILY_MINUTES","30")) * 60
        used = conn.execute("SELECT seconds FROM voice_usage WHERE day=?", (day,)).fetchone()
        if (used[0] if used else 0) + seconds > limit:
            raise ValueError("Исчерпан суточный лимит минут распознавания речи.")
        conn.execute("""INSERT INTO voice_usage(day,seconds) VALUES (?,?) ON CONFLICT(day)
                        DO UPDATE SET seconds=seconds+excluded.seconds""", (day, seconds))


def media_tick(connect):
    stamp = int(time.time())
    with closing(_connection(connect)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            selected = conn.execute("""SELECT v.identity,v.audio_language FROM video_candidates v
               LEFT JOIN media_checks m ON m.identity=v.identity
               WHERE v.platform='youtube' AND v.method='search' AND (m.identity IS NULL OR m.retry_at BETWEEN 1 AND ?)
               ORDER BY (m.identity IS NULL) DESC, v.identity IN (
                   SELECT json_extract(s.value,'$.identity') FROM auto_searches a,json_each(a.receipt,'$.sources') s
                   WHERE a.origin='show' AND a.receipt!='' AND a.started>=?
               ) DESC, v.published_at DESC LIMIT 1""", (stamp,stamp-600)).fetchone()
            if not selected:
                return
            key, language = selected
            conn.execute("""INSERT INTO media_checks(identity,status,retry_at) VALUES (?,'running',?)
                ON CONFLICT(identity) DO UPDATE SET status='running',retry_at=excluded.retry_at""",(key,stamp+1800))
        report = {}
        try:
            with tempfile.TemporaryDirectory(prefix="lex-youtube-") as tmp:
                file = media_review.download_public(key.removeprefix("yt:"), Path(tmp))
                report = media_review.inspect_file(file, language=language if language in {"ru","uz"} else "auto",
                                                reserve=lambda n: reserve_voice(connect,n))
            status = "partial" if report["errors"] else "ready"
            error = " ".join(report["errors"])
        except (OSError,ValueError,subprocess.SubprocessError, KeyError) as exc:
            status = "unavailable"
            error = str(exc) if isinstance(exc,ValueError) else "Не удалось обработать видео."
        with conn:
            conn.execute("UPDATE media_checks SET status=?,retry_at=?,report=?,error=? WHERE identity=?",
                         (status,0 if status=="ready" else stamp+86400,json.dumps(report,ensure_ascii=False),error,key))
            if report:
                conn.execute("UPDATE auto_alerts SET active=0 WHERE mode='live' AND source_key=?",(key,))
                conn.execute("""UPDATE auto_jobs SET status='pending',owner='',next_at=0,attempted_at=0
                             WHERE mode='youtube' AND source_key=?""",(key,))
        _work(conn,"live",LIVE_BATCH)


def search_youtube(connect, api_key):
    if not api_key:
        raise ValueError("Добавьте ключ YouTube Data API в настройках.")
    tick(connect, api_key, True)
    with closing(_connection(connect)) as conn:
        state = snapshot(conn, api_key, True)["discovery"]
        if state["status"] == "error":
            raise ValueError(state["error"])
        if state["status"] == "running":
            raise ValueError("Поиск YouTube уже идёт. Обновите ленту через несколько секунд.")


def preview_youtube(api_key, url, subtitle_file="", connect=None):
    video_id = public_sources._video_id(url)
    cues = public_sources.read_subtitles(subtitle_file) if subtitle_file else []
    rows = public_sources.youtube_details(api_key, [video_id])
    if not rows:
        raise ValueError("YouTube не вернул публичное видео по этой ссылке.")
    row = rows[0]
    row.update(video_title=row["title"], method="preview", speech=cues, subtitle_preview=bool(cues))
    media_status = ""
    if connect and not cues:
        with closing(_connection(connect)) as conn:
            check = conn.execute("SELECT status,report,error FROM media_checks WHERE identity=?", ("yt:"+video_id,)).fetchone()
        if check:
            media_status = check[2] or check[0]
            if check[0] in {"ready","partial"}:
                row["media_report"] = json.loads(check[1])
    findings = [_alert(row, finding, False) for finding in auto_law.check_video(row)]
    return dict(video_title=row["title"], url=row["url"], published_at=row["published_at"],
                account=row["account"], has_subtitles=bool(cues), media_report=row.get("media_report"), media_status=media_status, findings=findings)


def public_scan_status(conn):
    saved = conn.execute("SELECT value FROM config WHERE key='youtube_monitor_search'").fetchone()
    if not saved:
        return None
    return _report_items(conn, json.loads(saved[0]))


def _report_items(conn, report):
    report["items"] = []
    for source in report.pop("sources"):
        candidate = conn.execute("""SELECT v.account,v.published_at,v.video_title,j.status,j.error
            FROM video_candidates v JOIN auto_jobs j ON j.source_key=v.identity AND j.mode='youtube'
            WHERE v.identity=?""", (source["identity"],)).fetchone()
        if candidate is None:
            continue
        account, published_at, video_title, status, error = candidate
        alert = conn.execute("""SELECT payload FROM auto_alerts WHERE source_key=? AND mode='live'
            AND active=1 ORDER BY id DESC LIMIT 1""", (source["identity"],)).fetchone()
        item = {"url": source["url"], "new":source.get("new"), "video_title": video_title, "account": account,
                "published_at": published_at, "quote": "", "law_url": "", "law_label": "", "reason": error,
                "status": {"ready": "checked", "insufficient": "insufficient", "error": "error"}.get(status, "pending")}
        if alert:
            item.update(json.loads(alert[0]), status="matched")
        check = conn.execute("SELECT status,report,error FROM media_checks WHERE identity=?", (source["identity"],)).fetchone()
        if check:
            media = json.loads(check[1])
            item["media"] = dict(status=check[0], detail=check[2], frames=media.get("frames_scanned",0),
                                 voice=media.get("speech",{}).get("status",""), step=media.get("frame_step",0))
        report["items"].append(item)
    return report


def live_show_status(conn, run_id=None, api_key="", scheduled=False):
    cond, args = ("AND id=?", (run_id,)) if run_id else ("", ())
    cur = conn.execute("""SELECT id,origin,started,status,query,sent_at,received_at,listed,found,last_progress,error,receipt
                          FROM auto_searches WHERE platform='youtube' """ + cond + " ORDER BY id DESC LIMIT 1", args)
    row = cur.fetchone()
    run = None
    if row:
        run = dict(zip((c[0] for c in cur.description), row))
        raw = run.pop("receipt")
        if not raw and run["origin"]=="schedule" and run["status"]=="ready":
            saved = conn.execute("SELECT value FROM config WHERE key='youtube_monitor_search'").fetchone()
            if saved:
                old = json.loads(saved[0])
                if old["query"]==run["query"] and old["checked_at"]==_iso(run["started"]):
                    raw = saved[0]
        run["items"] = _report_items(conn,json.loads(raw))["items"] if raw else []
        if run["status"]=="running" and time.time()-max(run["started"],run["sent_at"],run["received_at"])>2*LEASE_SECONDS:
            run.update(status="error",error="Поиск прервался до сохранения результатов.")
    example = conn.execute("""SELECT v.url,v.video_title FROM video_candidates v JOIN media_checks m USING(identity)
                              WHERE m.status='ready' AND json_extract(m.report,'$.speech.status')='ok'
                              AND json_extract(m.report,'$.frames_scanned')>0 ORDER BY v.published_at DESC LIMIT 1""").fetchone()
    last, = conn.execute("SELECT max(started) FROM auto_searches WHERE origin='show'").fetchone()
    used, = conn.execute("SELECT count(*) FROM auto_searches WHERE origin='show' AND started>=?",(_day_start(int(time.time())),)).fetchone()
    plan = discovery_status(conn,api_key,scheduled)
    last_scheduled = conn.execute("SELECT id FROM auto_searches WHERE platform='youtube' AND origin='schedule' ORDER BY id DESC LIMIT 1").fetchone()
    background = dict(enabled=plan["enabled"], next_at=plan["next_at"], previous_at=plan["checked_at"],
                      previous_id=last_scheduled[0] if last_scheduled else None)
    return dict(run=run, background=background, example={"url":example[0],"title":example[1]} if example else None,
                next_show_at=max((last or 0)+SHOW_INTERVAL, _day_start(int(time.time()))+86400 if used>=SHOW_LIMIT else 0))


def demo_run(connect):
    """Check the built-in examples against their matching offline legal references."""
    fixtures = auto_law.training_examples()
    with closing(_connection(connect)) as conn:
        with conn:
            for row in fixtures:
                key, payload = row["key"], json.dumps(row, ensure_ascii=False, sort_keys=True)
                queued = conn.execute("""INSERT INTO auto_jobs(mode,source_key,training_json) VALUES ('demo',?,?)
                    ON CONFLICT(mode,source_key) DO UPDATE SET training_json=excluded.training_json,
                      status='pending', owner='',error='',next_at=0,failures=0,attempted_at=0
                    WHERE auto_jobs.status!='running' OR auto_jobs.training_json!=excluded.training_json""",
                    (key, payload))
                if queued.rowcount:
                    conn.execute("UPDATE auto_alerts SET active=0 WHERE mode='demo' AND source_key=?", (key,))
        _work(conn, "demo", len(fixtures))


def _queue_status(conn, mode):
    counts = dict(conn.execute("SELECT status,count(*) FROM auto_jobs WHERE mode=? GROUP BY status", (_job_mode(mode),)))
    last, = conn.execute("SELECT max(attempted_at) FROM auto_jobs WHERE mode=?", (_job_mode(mode),)).fetchone()
    error = conn.execute("""SELECT error FROM auto_jobs WHERE mode=? AND status='error'
        ORDER BY attempted_at DESC LIMIT 1""", (_job_mode(mode),)).fetchone()
    items = [dict(json.loads(payload), id=number, created_at=_iso(created), updated_at=_iso(updated))
             for number, created, updated, payload in conn.execute("""SELECT id,created_at,updated_at,payload
                 FROM auto_alerts WHERE mode=? AND active=1 ORDER BY created_at DESC,id DESC LIMIT 100""", (mode,))]
    total, = conn.execute("SELECT count(*) FROM auto_alerts WHERE mode=? AND active=1", (mode,)).fetchone()
    status = ("error" if error else "running" if counts.get("running") else
              "ready" if counts.get("ready") or counts.get("insufficient") else "idle")
    return dict(items=items, result_count=total, status=status, checked_at=_iso(last), error=error[0] if error else "",
                checked_count=counts.get("ready",0), pending_count=counts.get("pending",0),
                error_count=counts.get("error",0), insufficient_count=counts.get("insufficient",0))


def snapshot(conn, api_key="", scheduled=False):
    return {"demo":_queue_status(conn, "demo"), "live":_queue_status(conn, "live"),
            "discovery":discovery_status(conn, api_key, scheduled)}


def discovery_status(conn, api_key, scheduled):
    stamp = int(time.time())
    calls, = conn.execute("SELECT count(*) FROM auto_searches WHERE platform='youtube' AND origin='schedule' AND started>=?", (_day_start(stamp),)).fetchone()
    last = conn.execute("""SELECT started, platform, query, status, error, found, last_progress
        FROM auto_searches WHERE platform='youtube' AND origin='schedule' ORDER BY id DESC LIMIT 1""").fetchone()
    started, platform, query, status, error, found, progress = last or (0, "", "", "idle", "", 0, 0)
    enabled = scheduled is True and bool(api_key)
    if status == "running" and stamp >= max(started, progress) + 2 * LEASE_SECONDS:
        status, error = "error", "Предыдущий поиск прервался. Следующая попытка пройдёт по расписанию."
    next_at = max(started + DISCOVERY_INTERVAL if started else stamp,
                  _day_start(stamp) + 86400 if calls >= DAILY_LIMIT else stamp)
    return dict(enabled=enabled, scheduled=scheduled is True, token_present=bool(api_key),
                             status=status if enabled else ("no_token" if scheduled else "disabled"),
                             checked_at=_iso(started), error=error, next_at=_iso(next_at) if enabled else "",
                             platform=platform, query=query, calls_today=calls, found=found,
                             daily_limit=DAILY_LIMIT, interval_hours=DISCOVERY_INTERVAL // 3600)
