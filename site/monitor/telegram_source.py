"""Read public channel posts and user-submitted links through the Telegram Bot API."""

from datetime import datetime, timezone
import json
import os
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect())
_url_text = re.compile(
    r"""(?<![\w./@-])(?:https?://|(?=(?:www\.|m\.|web\.)?(?:t\.me|telegram\.me|instagram\.com|threads\.(?:com|net)|youtube\.com|youtu\.be|facebook\.com)/))[^\s<>"'«»]+""",
    re.IGNORECASE,
)
_username = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,31}")
_telegram_reserved = {"share", "proxy", "socks", "login", "joinchat", "boost", "invoice",
                      "contact", "addstickers", "addemoji", "setlanguage", "giftcode"}
_facebook_id = r"(?:[1-9]\d*|pfbid[A-Za-z0-9]+)"


def public_post_url(candidate):
    """Return (platform, canonical post URL) only for recognized public post shapes."""
    if len(candidate) > 2048 or "\\" in candidate or any(char.isspace() or ord(char) < 32 for char in candidate):
        return None
    try:
        url = urlsplit(candidate)
        host = (url.hostname or "").lower().removeprefix("www.")
        if url.scheme not in {"http", "https"} or url.username is not None or url.port is not None:
            return None
    except ValueError:
        return None

    path = url.path.rstrip("/")
    query = parse_qs(url.query)
    if host in {"t.me", "telegram.me"}:
        match = re.fullmatch(r"/(?:s/)?([^/]+)/([1-9]\d*)", path)
        if match and _username.fullmatch(match[1]) and match[1].lower() not in _telegram_reserved:
            return "telegram", f"https://t.me/{match[1].lower()}/{match[2]}"
    elif host == "instagram.com":
        if re.fullmatch(r"/(?:p|reel|tv)/[A-Za-z0-9_-]+", path):
            return "instagram", f"https://www.instagram.com{path}/"
    elif host in {"threads.com", "threads.net"}:
        match = re.fullmatch(r"/@([A-Za-z0-9._]{1,30})/post/([A-Za-z0-9_-]+)/?", url.path)
        if match:
            return "threads", f"https://www.threads.com/@{match[1].lower()}/post/{match[2]}"
    elif host in {"youtube.com", "m.youtube.com", "youtu.be"}:
        video_id = None
        if host == "youtu.be":
            video_id = path.lstrip("/")
        elif path == "/watch":
            video_id = query.get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})", path)
            if match:
                video_id = match[1]
        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return "youtube", f"https://www.youtube.com/watch?v={video_id}"
    elif host == "fb.watch" and re.fullmatch(r"/[A-Za-z0-9_-]+", path):
        return "facebook", f"https://fb.watch{path}/"
    elif host in {"facebook.com", "m.facebook.com", "web.facebook.com"}:
        if host == "facebook.com" and url.path in {"/ads/library", "/ads/library/"}:
            ids = parse_qs(url.query, keep_blank_values=True).get("id", [])
            if len(ids) == 1 and re.fullmatch(r"[1-9][0-9]*", ids[0]):
                return "meta_ad_library", f"https://www.facebook.com/ads/library/?id={ids[0]}"
        if re.fullmatch(rf"/(?:[\w.-]+/(?:posts|videos)|groups/[\w.-]+/(?:posts|permalink)|reel)/{_facebook_id}", path, re.ASCII):
            return "facebook", f"https://www.facebook.com{path}"
        if re.fullmatch(r"/share/(?:p|r|v)/[A-Za-z0-9_-]+", path):
            return "facebook", f"https://www.facebook.com{path}/"
        fields = {"/story.php": ("story_fbid", "id"), "/permalink.php": ("story_fbid", "id"),
                  "/watch": ("v",), "/photo": ("fbid",), "/photo.php": ("fbid",)}.get(path)
        if fields:
            values = {field: query.get(field, [""])[0] for field in fields}
            if all(re.fullmatch(_facebook_id, value) for value in values.values()):
                return "facebook", f"https://www.facebook.com{path}?{urlencode(values)}"
    return None


def _submission(message, body, token):
    # A private message supplies a link and an optional note, never the remote post body.
    for match in _url_text.finditer(body):
        candidate = match[0].rstrip(".,;!?:)]}…")
        if "://" not in candidate:
            candidate = "https://" + candidate
        source = public_post_url(candidate)
        if source and token not in candidate:
            note = (body[:match.start()] + body[match.end():]).strip()
            return {"platform": source[0], "url": source[1], "kind": "submission",
                    "title": "Ссылка из личного сообщения", "text": note}
    entities = message.get("entities") or message.get("caption_entities") or []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("type") != "text_link":
            continue
        candidate = entity.get("url")
        if isinstance(candidate, str) and token not in candidate:
            source = public_post_url(candidate)
            if source:
                return {"platform": source[0], "url": source[1], "kind": "submission",
                        "title": "Ссылка из личного сообщения", "text": body}
    return None


def _entry(update, channel_ids, token):
    channel = update.get("channel_post") or update.get("edited_channel_post")
    message = channel or update.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
        return None
    chat = message["chat"]
    content = message.get("text") or message.get("caption") or ""
    body = content.replace(token, "[скрыто]").strip() if isinstance(content, str) else ""
    if channel and chat.get("type") == "channel":
        if channel_ids is not None and chat.get("id") not in channel_ids:
            return None
        username, mid = chat.get("username"), message.get("message_id")
        if not isinstance(username, str) or not _username.fullmatch(username):
            return None
        if type(mid) is not int or mid < 1 or username.lower() in _telegram_reserved:
            return None
        title = str(chat.get("title") or username).replace(token, "[скрыто]")
        post = {"platform": "telegram", "url": f"https://t.me/{username.lower()}/{mid}",
                "title": title, "text": body, "kind": "channel"}
        for field, destination in (("date", "published_at"), ("edit_date", "source_updated_at")):
            value = message.get(field)
            if type(value) is int and value > 0:
                try:
                    post[destination] = datetime.fromtimestamp(value, timezone.utc).isoformat()
                except (OSError, ValueError, OverflowError):
                    pass
        return post
    if not channel and chat.get("type") == "private":
        return _submission(message, body, token)
    return None


def _settings():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{1,128}", token):
        raise ValueError("Проверьте TELEGRAM_BOT_TOKEN.")
    allowed = os.getenv("TELEGRAM_CHANNEL_IDS", "").strip()
    channel_ids = None
    if allowed:
        try:
            channel_ids = {int(value.strip()) for value in allowed.split(",")}
        except ValueError:
            raise ValueError("TELEGRAM_CHANNEL_IDS должен содержать числовые ID через запятую.") from None
    return token, channel_ids


class TelegramRejected(ValueError):
    def __init__(self, status, message, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _bot_api(token, method, params=None):
    headers = {"Accept": "application/json"}
    data = None
    if params is not None:
        data = json.dumps(params).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"https://api.telegram.org/bot{token}/{method}", data=data, headers=headers)
    try:
        with _opener.open(request, timeout=8) as response:
            payload = json.load(response)
    except HTTPError as error:
        code = error.code
        retry_after = 0
        try:
            if code == 429:
                detail = json.loads(error.read(8192))
                retry_after = int(detail["parameters"]["retry_after"])
        except (ValueError, TypeError, KeyError, OSError):
            pass
        finally:
            error.close()
        if code in (401, 404):
            raise TelegramRejected(code, "Telegram отклонил токен. Проверьте настройки бота.") from None
        raise TelegramRejected(code, f"Telegram Bot API вернул HTTP {code}.", retry_after) from None
    except (HTTPException, URLError, OSError, ValueError, RecursionError):
        raise ValueError("Не удалось получить корректный ответ Telegram Bot API.") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True or "result" not in payload:
        raise ValueError("Telegram Bot API отклонил запрос или вернул неполный ответ.")
    return payload["result"]


def telegram_probe() -> str:
    """Read bot identity and webhook status without consuming updates."""
    token, _ = _settings()
    identity = _bot_api(token, "getMe")
    if not isinstance(identity, dict) or identity.get("is_bot") is not True:
        raise ValueError("Telegram не подтвердил учётную запись бота.")
    webhook = _bot_api(token, "getWebhookInfo")
    if not isinstance(webhook, dict) or not isinstance(webhook.get("url"), str):
        raise ValueError("Telegram не сообщил состояние webhook.")
    if webhook["url"]:
        raise ValueError("У бота включён webhook. Для получения публикаций через getUpdates нужен бот без webhook.")
    username = identity.get("username")
    masked = ""
    if isinstance(username, str) and _username.fullmatch(username):
        masked = f" @{username[0]}…{username[-2:]}"
    return f"Telegram подтвердил бота{masked}. Webhook не настроен."


def telegram_updates(offset: int) -> list[tuple[int, dict | None]]:
    """Return updates at/above offset. Persist max(update_id)+1 after processing."""
    if type(offset) is not int or offset < 0:
        raise ValueError("Некорректный offset Telegram.")
    token, channel_ids = _settings()
    params = {"offset": offset, "limit": 100, "timeout": 1,
              "allowed_updates": ["channel_post", "edited_channel_post", "message"]}
    updates = _bot_api(token, "getUpdates", params)
    if not isinstance(updates, list):
        raise ValueError("Telegram не вернул список обновлений.")

    entries = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        uid = update.get("update_id")
        if type(uid) is not int or uid < offset or uid in entries:
            continue
        entry = _entry(update, channel_ids, token)
        if entry:
            entry["telegram_update_id"] = uid
        entries[uid] = entry
    return sorted(entries.items())
