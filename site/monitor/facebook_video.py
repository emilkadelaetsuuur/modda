"""Search public Facebook videos with one bounded Apify keyword search."""

import http.client
import json
import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urlsplit

from demo_instagram import MAX_CHARGE_USD, MAX_RESPONSE_BYTES, RESULT_LIMIT, RUN_TIMEOUT


_ACTOR_PATH = "/v2/actors/apify~facebook-video-search-scraper/run-sync-get-dataset-items"
_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
_ID = re.compile(r"[1-9][0-9]*")
_VIDEO_PATH = re.compile(r"/(?:reel|[A-Za-z0-9_-][A-Za-z0-9._-]*/videos)/([1-9][0-9]*)")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


class FacebookDiscoveryError(RuntimeError):
    """The search provider could not deliver its results."""


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _media_link(value):
    """Return the Facebook video ID and a public media permalink, without trackers."""
    if not isinstance(value, str) or len(value) > 2048 or not value.isprintable() or "\\" in value:
        return None
    try:
        link = urlsplit(value)
    except ValueError:
        return None
    if link.scheme != "https" or link.netloc.lower() not in _HOSTS:
        return None
    path = link.path.rstrip("/")
    match = _VIDEO_PATH.fullmatch(path)
    if match:
        return match[1], f"https://www.facebook.com{path}"
    field = {"/video/embed": "video_id", "/watch": "v"}.get(path)
    if field:
        ids = parse_qs(link.query, keep_blank_values=True).get(field, [])
        if len(ids) == 1 and _ID.fullmatch(ids[0]):
            return ids[0], f"https://www.facebook.com/watch?v={ids[0]}"
    return None


def _published_day(value):
    # Search rows may give "May 23 · 210K views" or "3 days ago". Neither
    # identifies a calendar day unambiguously. Keep a complete date at day precision.
    label = _text(value).partition("·")[0].strip()
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", label):
            return date.fromisoformat(label).isoformat()
        match = re.fullmatch(r"([A-Za-z]{3}) ([0-9]{1,2}), ([0-9]{4})", label)
        if match:
            month = _MONTHS.index(match[1].lower()) + 1
            return date(int(match[3]), month, int(match[2])).isoformat()
    except ValueError:
        pass
    return ""


def _results(rows):
    if not isinstance(rows, list):
        raise FacebookDiscoveryError("Поисковый сервис вернул неожиданный формат ответа.")
    results = {}
    for row in rows[:RESULT_LIMIT]:
        if not isinstance(row, dict):
            raise FacebookDiscoveryError("Поисковый сервис вернул повреждённую запись.")
        if row.get("error") or row.get("errorDescription"):
            raise FacebookDiscoveryError("Поисковый сервис сообщил об ошибке поиска видео в Facebook.")
        # facebookUrl, inputUrl and url describe the search itself, never the video.
        media = _media_link(row.get("videoUrl"))
        if not media:
            continue
        video_id, url = media
        video = row.get("video")
        if isinstance(video, dict) and video.get("id") is not None and str(video["id"]) != video_id:
            continue
        owner = row.get("video_owner_profile")
        results.setdefault(video_id, {
            "url": url,
            "account": _text(owner.get("name")) if isinstance(owner, dict) else "",
            "published_at": _published_day(row.get("relative_time_string")),
            "description": _text(row.get("save_description")) or _text(row.get("title")),
        })
    return list(results.values())


def discover_facebook_videos(token: str, query: str) -> list[dict]:
    """Look up a phrase or hashtag without accessing a user's Facebook account."""
    token = _text(token)
    if not re.fullmatch(r"[!-~]+", token):
        raise ValueError("Укажите действующий токен Apify.")
    query = _text(query)
    if not query or len(query) > 180 or not query.isprintable():
        raise ValueError("Введите поисковую фразу длиной до 180 символов в одну строку.")
    payload = {"searchTerm": query, "resultsLimit": RESULT_LIMIT}
    options = urlencode({
        "maxTotalChargeUsd": MAX_CHARGE_USD, "timeout": RUN_TIMEOUT,
        "limit": RESULT_LIMIT, "format": "json", "restartOnError": "false",
        "fields": "videoUrl,video,video_owner_profile,save_description,title,relative_time_string,error,errorDescription",
    })
    connection = http.client.HTTPSConnection("api.apify.com", timeout=RUN_TIMEOUT + 15)
    try:
        connection.request("POST", f"{_ACTOR_PATH}?{options}", body=json.dumps(payload),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        response = connection.getresponse()
        status = response.status
        if status in (401, 403):
            raise FacebookDiscoveryError("Поисковый сервис отклонил доступ. Проверьте токен и права на запуск.")
        if status in (402, 429):
            raise FacebookDiscoveryError("Достигнут лимит поискового сервиса. Проверьте баланс и ограничения запусков.")
        if status in (408, 504):
            raise FacebookDiscoveryError("Поисковый сервис не успел завершить поиск за отведённое время.")
        if not 200 <= status < 300:
            raise FacebookDiscoveryError(f"Поисковый сервис отклонил запрос (HTTP {status}).")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except TimeoutError:
        raise FacebookDiscoveryError("Истекло время ожидания ответа Apify.") from None
    except (OSError, http.client.HTTPException):
        raise FacebookDiscoveryError("Соединение с поисковым сервисом прервалось.") from None
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FacebookDiscoveryError("Ответ поискового сервиса превышает допустимый размер.")
    try:
        rows = json.loads(raw)
    except (ValueError, RecursionError):
        raise FacebookDiscoveryError("Поисковый сервис вернул нечитаемый ответ.") from None
    return _results(rows)
