"""Local discovery queue for public videos with potential impersonation claims."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from demo_instagram import InstagramDiscoveryError, discover_instagram
from facebook_video import FacebookDiscoveryError, discover_facebook_videos
from telegram_source import public_post_url


PRESETS = [
    {"label": "Реклама Raida", "query": "Raida halol savdo"},
    {"label": "Предложение из известного ролика", "query": "Law firm Justice"},
    {"label": "Инвестиции", "query": "O‘zbekneftgaz sarmoya"},
    {"label": "Возврат денег", "query": "возврат денег от брокера Узбекистан"},
    {"label": "Обещание заработка", "query": "halol savdo daromad"},
]
RAIDA_SOURCE = "https://qalampir.uz/uz/news/kiberkhavfsizlik-markazi-qalampir-uz-ra%D2%B3bari-va-ortik-khuzhayev-ishtirokidagi-videolarga-ishonmaslikka-chak-irdi-94068"
JUSTICE_SOURCE = "https://www.factchecker.uz/post/o-zbekiston-bosh-vaziri-abdulla-aripov-bayonoti"
INVESTMENT_SOURCE = "https://www.factchecker.uz/post/investitsiya-platformalarida-firibgarlikning-asosiy-turlari-va-himoyalanish-usullari"
EXAMPLES = [
    {
        "title": "Из интервью Qalampir.uz сделали рекламу Raida",
        "platform": "Instagram и Facebook", "date": "2023-12-02",
        "description": "Редакция узнала своё интервью декабря 2018 года. В распространявшейся версии подменили голоса и добавили обещание заработка. В исходном разговоре собеседники не обсуждали Raida.",
        "evidence_url": RAIDA_SOURCE, "video_url": "https://t.me/qalampirlive/87154",
        "original_url": "https://archive.qalampir.uz/news/jaxongir-ortiqxojaev-bilan-eksklyuziv-videointervyuni-tomosha-qiling-25810",
    },
    {
        "title": "Предложение «Law firm Justice» от имени Абдуллы Арипова",
        "platform": "Facebook и Instagram", "date": "2024-01-15",
        "description": "Factchecker.uz описывает распространённое в обеих соцсетях видео. Авторы разбора указывают, что изображение обработали с помощью ИИ, а аудиозапись с обещанием быстрого дохода подделали.",
        "evidence_url": JUSTICE_SOURCE,
        "video_url": "https://youtu.be/TYvW1LVJ7VM",
    },
    {
        "title": "Инвестиционная реклама от имени крупных компаний Узбекистана",
        "platform": "Facebook", "date": "2024-08-20",
        "description": "Корреспондент Factchecker.uz проверил схему с предложением вложить деньги. В расследовании есть видеопример и описано использование изменённых голосов известных людей.",
        "evidence_url": INVESTMENT_SOURCE,
        "video_url": "https://video.wixstatic.com/video/75765c_e71c3bd6d05348cea43782d3788b93de/480p/mp4/file.mp4",
    },
]


def search_query(value):
    value = value.strip()
    if not 1 <= len(value) <= 180 or not value.isprintable():
        raise ValueError("Введите фразу длиной до 180 символов в одну строку.")
    return value


def external_links(query):
    query = search_query(query)
    return {
        "instagram_native": "https://www.instagram.com/explore/search/keyword/?" + urlencode({"q":query}),
        "facebook_native": "https://www.facebook.com/search/videos/?" + urlencode({"q":query}),
        "instagram": "https://www.google.com/search?" + urlencode({"q": "site:instagram.com/reel/ " + query}),
        "facebook": "https://www.google.com/search?" + urlencode({"q": "(site:facebook.com/reel/ OR site:facebook.com inurl:videos) " + query}),
        "ads": "https://www.facebook.com/ads/library/?" + urlencode({
            "active_status": "active", "ad_type": "all", "country": "UZ", "media_type": "video",
            "q": query, "search_type": "keyword_unordered",
        }),
    }


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS video_candidates (
        id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, platform TEXT NOT NULL,
        url TEXT NOT NULL, account TEXT NOT NULL, description TEXT NOT NULL, published_at TEXT NOT NULL,
        discovered_at TEXT NOT NULL, query TEXT NOT NULL, method TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'candidate', evidence_url TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '')""")
    columns = {column[1] for column in conn.execute("PRAGMA table_info(video_candidates)")}
    for column in ("speech", "speech_status", "review_snapshot", "video_title", "audio_language"):
        if column not in columns:
            conn.execute(f"ALTER TABLE video_candidates ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    conn.execute("""UPDATE video_candidates SET review_snapshot=json_object(
        'account',account,'description',description,'published_at',published_at,'method',method,'query',query)
        WHERE status='reviewed' AND review_snapshot=''""")


def list_items(conn):
    cursor = conn.execute("""SELECT id, platform, url, account, description, published_at,
        discovered_at, query, method, status, evidence_url, reason, review_snapshot FROM video_candidates ORDER BY id DESC""")
    columns = [column[0] for column in cursor.description]
    items = [dict(zip(columns, row)) for row in cursor]
    for item in items:
        saved_review = item.pop("review_snapshot")
        if saved_review and item["status"] == "reviewed":
            item.update(json.loads(saved_review))
        item["lead"], item["reference_url"] = caption_lead(item)
    return items


def caption_lead(item):
    if item["method"] == "manual":
        return "Ссылку добавил редактор.", ""
    caption = item["description"].casefold()
    if any(term in caption for term in ("опровержен", "осторожно", "разбор фейка", "ogohlantir", "raddiya", "ehtiyot bo")):
        return "В подписи есть признаки предупреждения или разбора. Проверьте, в каком контексте показывают запись.", ""
    if "raida" in caption and any(term in caption for term in ("halol", "savdo", "daromad", "заработ", "доход")):
        return "В подписи есть Raida и предложение торговли или заработка. Сравните ролик с разбором редакции, снявшей интервью.", RAIDA_SOURCE
    if "law firm justice" in caption:
        return "В подписи встречается название из разбора Factchecker.uz. Сравните фразу и фрагмент записи.", JUSTICE_SOURCE
    if any(term in caption for term in ("neftgaz", "neftigaz", "нефтегаз")) and any(
            term in caption for term in ("инвест", "sarmoya", "даромад", "daromad")):
        return "В подписи одновременно упоминаются нефтегазовая компания и инвестиции или доход. У Factchecker.uz есть разбор похожего предложения.", INVESTMENT_SOURCE
    return "Видео вернул поиск площадки по вашей фразе. Подлинность изображения и голоса предстоит проверить.", ""


def _video_url(value):
    result = public_post_url(value)
    if result is None:
        raise ValueError("Нужна ссылка на видео YouTube, Instagram, Facebook или объявление Meta.")
    platform, url = result
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    code = path.rsplit("/", 1)[-1]
    if platform == "youtube":
        return platform, url, "yt:" + parse_qs(parts.query)["v"][0]
    if platform == "instagram" and path.startswith(("/reel/", "/tv/")):
        return platform, url, "ig:" + code
    if platform == "meta_ad_library":
        return platform, url, "ad:" + parse_qs(parts.query)["id"][0]
    if platform == "facebook":
        if path == "/watch":
            return platform, url, "fb:" + parse_qs(parts.query)["v"][0]
        if path.startswith("/reel/") or "/videos/" in path:
            return platform, url, "fb:" + code
        if parts.hostname == "fb.watch" or path.startswith(("/share/r/", "/share/v/")):
            return platform, url, url
    raise ValueError("Откройте сам ролик и скопируйте его ссылку.")


def store_item(conn, row, query, method):
    platform, url, identity = _video_url(row["url"])
    discovered = datetime.now(timezone.utc).isoformat(timespec="seconds")
    speech = json.dumps(row["speech"], ensure_ascii=False) if row.get("speech") else ""
    speech_status = row.get("speech_status", "")
    data = (identity, platform, url, row.get("account", "")[:250], row.get("description", "")[:16000],
            row.get("published_at", "")[:80], discovered, query, method, speech, speech_status, row.get("title", "")[:500], row.get("audio_language","")[:12])
    added = conn.execute("""INSERT INTO video_candidates
        (identity, platform, url, account, description, published_at, discovered_at, query, method,speech,speech_status,video_title,audio_language)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO NOTHING""", data).rowcount
    if not added and method in {"search", "feed", "public_feed"}:
        conn.execute("""UPDATE video_candidates SET
            account=coalesce(nullif(?,''),account),
            description=?, video_title=?, audio_language=?,
            published_at=coalesce(nullif(?,''),published_at), method=?, query=?,
            speech=CASE WHEN ?!='' THEN ? WHEN description!=? THEN '' ELSE speech END,
            speech_status=CASE WHEN ?!='' THEN ? WHEN description!=? THEN '' ELSE speech_status END
            WHERE identity=?""",
            (data[3], data[4], data[-2], data[-1], data[5], method, query, speech_status, speech, data[4],
             speech_status, speech_status, data[4], identity))
    return added


def discover(token, query, platform):
    query = search_query(query)
    if platform not in {"instagram", "facebook", "both"}:
        raise ValueError("Выберите Instagram, Facebook или обе платформы.")
    sources = ["instagram", "facebook"] if platform == "both" else [platform]
    rows = []
    errors = []
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        pending = {source: (pool.submit(discover_instagram, token, query, "reels") if source == "instagram"
                            else pool.submit(discover_facebook_videos, token, query)) for source in sources}
        for source, job in pending.items():
            try:
                for row in job.result():
                    try:
                        platform_found, _, _ = _video_url(row["url"])
                    except ValueError:
                        continue
                    if platform_found == source:
                        rows.append(row)
            except (InstagramDiscoveryError, FacebookDiscoveryError, ValueError) as exc:
                errors.append(f"{source.capitalize()}: {exc}")
    if len(errors) == len(sources):
        raise RuntimeError(" ".join(errors))
    return rows, errors


def save_manual(conn, obj):
    query = obj.get("query", "").strip()
    note = obj.get("description", "").strip()
    if len(note) > 4000:
        raise ValueError("Сократите описание до 4 000 символов.")
    return store_item(conn, {"url":obj.get("url", "").strip(), "description":note},
                      search_query(query) if query else "", "manual")


def review(conn, obj):
    number = obj.get("id", "")
    if obj.get("status") != "reviewed" or not re.fullmatch(r"[1-9][0-9]{0,17}", number):
        raise ValueError("Выберите видео из списка.")
    link = obj.get("evidence_url", "").strip()
    parts = urlsplit(link)
    if (not 1 <= len(link) <= 2048 or not parts.hostname or parts.scheme != "https" or parts.username is not None
            or parts.password is not None or any(c.isspace() or ord(c) < 32 for c in link) or "\\" in link):
        raise ValueError("Укажите HTTPS-ссылку на оригинальную запись или опубликованный разбор.")
    reason = obj.get("reason", "").strip()
    if not 1 <= len(reason) <= 4000:
        raise ValueError("Запишите, что удалось установить при сверке со ссылкой.")
    changed = conn.execute("""UPDATE video_candidates SET status='reviewed', evidence_url=?, reason=?,
                               review_snapshot=CASE WHEN review_snapshot='' THEN json_object(
                               'account',account,'description',description,'published_at',published_at,
                               'method',method,'query',query) ELSE review_snapshot END
                               WHERE id=?""", (link, reason, int(number))).rowcount
    if not changed:
        raise ValueError("Такая карточка не найдена.")
