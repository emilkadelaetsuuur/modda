"""Read public YouTube metadata and the Russian-language LexUZ document feed."""

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from xml.etree import ElementTree as ET


DEFAULT_CHANNEL = "UCvbxjabiN6Rmb42OguPFAdA"  # Gazeta.uz: Новости Узбекистана
LEX_RSS = "https://lex.uz/ru/rss"
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"
MAX_BYTES = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _utc_date(value: str, *, email_date: bool = False) -> str:
    """Convert source dates with explicit offsets; leave missing/invalid dates unknown."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        raw = value.strip()
        dt = parsedate_to_datetime(raw) if email_date else datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return ""
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return ""


def _get(url: str, source: str) -> bytes:
    request = Request(url, headers={"User-Agent": "LexMonitor/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(f"{source} вернул HTTP {exc.code}.") from None
    except (URLError, OSError, HTTPException):
        raise ValueError(f"Не удалось подключиться к {source}. Повторите позже.") from None
    if len(data) > MAX_BYTES:
        raise ValueError(f"Ответ {source} превышает лимит размера.")
    return data


def _xml(url: str, source: str) -> ET.Element:
    try:
        return ET.fromstring(_get(url, source))
    except ET.ParseError:
        raise ValueError(f"{source} прислал некорректную XML-ленту.") from None


def _video_id(url: str) -> str:
    hostnames = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
    message = "Укажите ссылку на видео или Shorts на YouTube."
    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname
    except (ValueError, AttributeError):
        raise ValueError(message) from None
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(message)
    path = parsed.path.strip("/").split("/")
    video_id = ""
    if host in hostnames:
        if path == ["watch"]:
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(path) == 2 and path[0] in {"shorts", "live", "embed"}:
            video_id = path[1]
    elif host == "youtu.be" and len(path) == 1:
        video_id = path[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError(message)
    return video_id


def youtube_video(url: str) -> dict:
    """Read a video's title and author from YouTube's public oEmbed endpoint."""
    canonical = "https://www.youtube.com/watch?v=" + _video_id(url)
    query = urlencode({"url": canonical, "format": "json"})
    try:
        item = json.loads(_get("https://www.youtube.com/oembed?" + query, "YouTube"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("YouTube прислал некорректный ответ.") from None
    if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
        raise ValueError("YouTube не вернул заголовок ролика.")
    author = item.get("author_name")
    return {
        "url": canonical,
        "title": item["title"].strip(),
        "text": "Канал: " + author.strip() if isinstance(author, str) and author.strip() else "",
        "platform": "youtube",
    }


def youtube_discover(api_key: str, query: str, published_after: str, *, short=False, duration=None, order="date") -> list[dict]:
    """Get one page of recent video search results viewable in Uzbekistan.

    order="viewCount" ranks the period's most watched videos first; duration is "short" (under 4 min) or "medium" (4-20 min)."""
    if not api_key or not api_key.isascii() or not api_key.isprintable() or any(c.isspace() for c in api_key):
        raise ValueError("Проверьте ключ YouTube Data API.")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("Поисковая тема YouTube должна содержать от 1 до 500 символов.")
    after = _utc_date(published_after)
    if not after:
        raise ValueError("Для поиска YouTube нужна дата с часовым поясом.")
    duration = duration or ("short" if short else None)
    if duration not in (None, "short", "medium", "long") or order not in ("date", "viewCount", "relevance"):
        raise ValueError("Неверные параметры поиска YouTube.")
    params = urlencode({
        "part": "snippet", "type": "video", "q": query.strip(), "order": order,
        "regionCode": "UZ", "publishedAfter": after, "maxResults": 50,
        "fields": "items(id/videoId,snippet(title,description,publishedAt,channelTitle))",
        **({"videoDuration": duration} if duration else {}),
    })
    result = _youtube_api(api_key, "search", params)
    rows = []
    for item in result["items"]:
        if not isinstance(item, dict):
            continue
        identifier, snippet = item.get("id"), item.get("snippet")
        if not isinstance(identifier, dict) or not isinstance(snippet, dict):
            continue
        video_id, title = identifier.get("videoId"), snippet.get("title")
        published = _utc_date(snippet.get("publishedAt"))
        if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                or not isinstance(title, str) or not title.strip() or not published):
            continue
        description = snippet.get("description")
        rows.append({
            "url": "https://www.youtube.com/watch?v=" + video_id, "platform": "youtube",
            "account": snippet.get("channelTitle", ""),
            "title": unescape(title).strip(), "text": unescape(description).strip() if isinstance(description, str) else "",
            "published_at": published,
        })
    return rows


def _youtube_api(api_key, resource, params):
    req = Request("https://www.googleapis.com/youtube/v3/" + resource + "?" + params, headers={"User-Agent": "LexMonitor/1.0"})
    req.add_unredirected_header("X-Goog-Api-Key", api_key)
    try:
        with build_opener(_NoRedirect()).open(req, timeout=10) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        with exc:
            if exc.code == 403 or exc.code == 429:
                raise ValueError(f"YouTube Data API вернул HTTP {exc.code}. Проверьте квоту поиска и доступ ключа.") from None
            raise ValueError(f"YouTube Data API отклонил запрос (HTTP {exc.code}).") from None
    except (URLError, OSError, HTTPException):
        raise ValueError("Не удалось подключиться к поиску YouTube. Повторите позже.") from None
    if len(data) > MAX_BYTES:
        raise ValueError("Ответ поиска YouTube превышает лимит размера.")
    try:
        result = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Поиск YouTube прислал некорректный ответ.") from None
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise ValueError("Поиск YouTube не вернул список видео.")
    return result


def youtube_details(api_key, video_ids):
    """Retrieve complete first-party metadata for public videos, including Shorts."""
    ids = list(dict.fromkeys(video_ids))
    if not ids:
        return []
    if len(ids) > 50 or not all(re.fullmatch(r"[A-Za-z0-9_-]{11}", i) for i in ids):
        raise ValueError("Неверные идентификаторы видео YouTube.")
    if not api_key:
        raise ValueError("Добавьте ключ YouTube Data API в настройках.")
    result = _youtube_api(api_key, "videos", urlencode({
        "part": "snippet,status,contentDetails,statistics", "id": ",".join(ids),
        "fields": "items(id,status/privacyStatus,contentDetails/duration,statistics/viewCount,"
                  "snippet(title,description,channelTitle,publishedAt,defaultAudioLanguage,defaultLanguage))",
    }))
    rows = []
    for item in result["items"]:
        snippet = item.get("snippet", {})
        if item.get("id") not in ids or item.get("status", {}).get("privacyStatus") != "public":
            continue
        views = str(item.get("statistics", {}).get("viewCount", ""))
        rows.append(dict(url="https://www.youtube.com/watch?v=" + item["id"], platform="youtube",
                         title=snippet.get("title", ""), account=snippet.get("channelTitle", ""),
                         audio_language=(snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "").split("-")[0].lower(),
                         description=snippet.get("description", ""), published_at=_utc_date(snippet.get("publishedAt")),
                         duration=_iso_seconds(item.get("contentDetails", {}).get("duration")),
                         views=int(views) if views.isdigit() else 0))
    return rows


def _iso_seconds(value):
    """Length of an ISO 8601 duration such as PT1M5S, or None when YouTube does not report it (live streams)."""
    matched = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not matched or value in ("P", "PT"):
        return None
    days, hours, minutes, secs = (int(part or 0) for part in matched.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + secs


def read_subtitles(value):
    """Read plain WebVTT/SRT dialogue, preserving cue times for a local review."""
    cues = []
    timing = r"(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)[.,](\d{3})"
    def seconds(parts):
        hours, minutes, secs, millis = parts
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs) + int(millis) / 1000
    for block in re.split(r"\n[ \t]*\n", value.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()):
        lines = block.splitlines()
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "REGION", "STYLE")):
            continue
        idx = 0 if "-->" in lines[0] else 1
        matched = re.fullmatch(timing + r"\s+-->\s+" + timing + r"(?:\s+.*)?", lines[idx]) if idx < len(lines) else None
        if not matched:
            raise ValueError("Проверьте формат файла SRT или WebVTT и интервалы субтитров.")
        phrase = unescape(re.sub(r"<[^>]*>", "", " ".join(lines[idx + 1:]))).strip()
        start, end = seconds(matched.groups()[:4]), seconds(matched.groups()[4:])
        if start >= end or end > 12 * 3600 or (cues and start < cues[-1]["end"]):
            raise ValueError("Нужны последовательные интервалы субтитров без наложений, в пределах 12 часов.")
        if phrase:
            cues.append(dict(start=start, end=end, text=phrase))
    if not cues or len(cues) > 1000 or sum(len(c["text"]) + 1 for c in cues) > 16000:
        raise ValueError("Файл должен содержать от 1 до 1000 реплик и до 16 000 символов речи.")
    return cues


def youtube_feed(channel_id: str) -> list[dict]:
    """Read the public Atom feed. Descriptions are creator-supplied metadata."""
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("Укажите ID канала YouTube в формате UC и 22 символа.")
    root = _xml("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id, "YouTube")
    if root.tag != ATOM + "feed":
        raise ValueError("YouTube не вернул ленту канала.")
    items = []
    for entry in root.findall(ATOM + "entry"):
        video_id = entry.findtext(YT + "videoId", "").strip()
        title = entry.findtext(ATOM + "title", "").strip()
        if title and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            items.append({
                "url": "https://www.youtube.com/watch?v=" + video_id,
                "title": title,
                "text": entry.findtext(f"{MEDIA}group/{MEDIA}description", "").strip(),
                "platform": "youtube",
                "published_at": _utc_date(entry.findtext(ATOM + "published", "")),
                "source_updated_at": _utc_date(entry.findtext(ATOM + "updated", "")),
            })
    return items


def lex_feed() -> list[dict]:
    """Return official document notices, including the feed's own summaries."""
    root = _xml(LEX_RSS, "LexUZ")
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("LexUZ не вернул ленту документов.")
    items = []
    for entry in root.findall("./channel/item"):
        link = entry.findtext("link", "").strip()
        title = entry.findtext("title", "").strip()
        if not link or not title:
            continue
        parsed = urlsplit(urljoin(LEX_RSS, link))
        if parsed.hostname != "lex.uz" or parsed.scheme not in {"http", "https"}:
            continue
        items.append({
            "url": parsed._replace(scheme="https").geturl(),
            "title": title,
            "text": entry.findtext("description", "").strip(),
            "platform": "lexuz",
            "published_at": _utc_date(entry.findtext("pubDate", ""), email_date=True),
            "source_updated_at": _utc_date(entry.findtext(ATOM + "updated", "") or
                                           entry.findtext("{http://purl.org/dc/terms/}modified", "")),
        })
    return items
