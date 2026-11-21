"""Find up to five Instagram publications with one Apify Actor run."""

from datetime import datetime, timezone
import http.client
import json
import re
from urllib.parse import urlencode, urlsplit

from video_speech import media_url


RESULT_LIMIT = 5
MAX_CHARGE_USD = "0.05"
RUN_TIMEOUT = 90
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ACTOR_PATH = "/v2/actors/apify~instagram-hashtag-scraper/run-sync-get-dataset-items"
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]+")
_POST_PATH = re.compile(r"/(?:[A-Za-z0-9._]+/)?(p|reel|reels)/([A-Za-z0-9_-]+)/?")


class InstagramDiscoveryError(RuntimeError):
    """The provider could not return a usable search response."""


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _link_parts(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme != "https" or url.netloc.lower() not in {"www.instagram.com", "instagram.com"}:
        return None
    return _POST_PATH.fullmatch(url.path)


def _published(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        stamp = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        pass
    return ""


def _publication(row, format):
    # The normal Actor output is flat. Read a wrapped media record as one post;
    # comments, tagged users, and carousel children have their own metadata.
    for key in ("media", "node"):
        if isinstance(row.get(key), dict):
            row = row[key]
            break
    link = _link_parts(row.get("url"))
    code = link[2] if link else _text(row.get("shortCode") or row.get("code") or row.get("shortcode"))
    if not _SHORTCODE.fullmatch(code):
        return None
    product = row.get("productType") or row.get("product_type")
    reel = product == "clips" or (link and link[1] in ("reel", "reels"))
    if not product and format == "reels":
        reel = True
    path = "reel" if reel else "p"
    caption = row.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    user = row.get("user") or row.get("owner")
    username = _text(row.get("ownerUsername"))
    if not username and isinstance(user, dict):
        username = _text(user.get("username"))
    post = {
        "url": f"https://www.instagram.com/{path}/{code}/",
        "account": username,
        "published_at": _published(row.get("timestamp") or row.get("taken_at")),
        "description": _text(caption),
    }
    downloadable = media_url(row.get("videoUrl"))
    if reel and downloadable:
        post["media_url"] = downloadable
    return post


def _results(rows, format):
    if not isinstance(rows, list):
        raise InstagramDiscoveryError("Поисковый сервис вернул неожиданный формат ответа.")
    found = {}
    for row in rows[:RESULT_LIMIT]:
        if not isinstance(row, dict):
            raise InstagramDiscoveryError("Поисковый сервис вернул повреждённую запись.")
        if row.get("error") == "no_items":
            raise InstagramDiscoveryError("Поисковый сервис сообщает, что данные по запросу пусты или недоступны.")
        if row.get("error") or row.get("requestErrorMessages"):
            raise InstagramDiscoveryError("Поисковому сервису не удалось прочитать данные Instagram.")
        post = _publication(row, format)
        if not post:
            raise InstagramDiscoveryError("Поисковый сервис вернул запись без ссылки или кода публикации Instagram.")
        code = post["url"].split("/")[-2]
        found.setdefault(code, post)
    return list(found.values())


def discover_instagram(token: str, query: str, format: str) -> list[dict]:
    """Search one phrase or explicit #hashtag. Unavailable metadata is empty."""
    token = _text(token)
    if not re.fullmatch(r"[!-~]+", token):
        raise ValueError("Укажите действующий токен Apify.")
    query = _text(query)
    if len(query) > 180 or not query.isprintable():
        raise ValueError("Введите поисковую фразу длиной до 180 символов в одну строку.")
    hashtag = query.startswith("#")
    if not query or (hashtag and not re.fullmatch(r"#\w+", query)):
        raise ValueError("Укажите поисковую фразу или один хештег в формате #слово.")
    if format not in ("posts", "reels"):
        raise ValueError("Укажите формат posts или reels.")
    payload = {
        "hashtags": [query[1:] if hashtag else query],
        "keywordSearch": not hashtag,
        "resultsType": format,
        "resultsLimit": RESULT_LIMIT,
    }
    options = urlencode({
        "maxTotalChargeUsd": MAX_CHARGE_USD, "timeout": RUN_TIMEOUT,
        "limit": RESULT_LIMIT, "format": "json", "restartOnError": "false",
        "fields": "url,shortCode,shortcode,code,caption,ownerUsername,owner,user,timestamp,taken_at,"
                  "productType,product_type,videoUrl,media,node,error,requestErrorMessages",
    })
    connection = http.client.HTTPSConnection("api.apify.com", timeout=RUN_TIMEOUT + 15)
    try:
        connection.request("POST", f"{_ACTOR_PATH}?{options}", body=json.dumps(payload),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        response = connection.getresponse()
        status = response.status
        if status in (401, 403):
            raise InstagramDiscoveryError("Поисковый сервис отклонил доступ. Проверьте токен и права на запуск.")
        if status in (402, 429):
            raise InstagramDiscoveryError("Достигнут лимит поискового сервиса. Проверьте баланс и ограничения запусков.")
        if status in (408, 504):
            raise InstagramDiscoveryError("Поисковый сервис не успел завершить поиск за отведённое время.")
        if not 200 <= status < 300:
            raise InstagramDiscoveryError(f"Поисковый сервис отклонил запрос (HTTP {status}).")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except TimeoutError:
        raise InstagramDiscoveryError("Истекло время ожидания ответа Apify.") from None
    except (OSError, http.client.HTTPException):
        raise InstagramDiscoveryError("Соединение с поисковым сервисом прервалось.") from None
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise InstagramDiscoveryError("Ответ поискового сервиса превышает допустимый размер.")
    try:
        rows = json.loads(raw)
    except (ValueError, RecursionError):
        raise InstagramDiscoveryError("Поисковый сервис вернул нечитаемый ответ.") from None
    return _results(rows, format)
