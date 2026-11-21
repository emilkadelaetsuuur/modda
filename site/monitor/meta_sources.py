"""Read a recent batch of posts through Meta Graph API with Facebook Login."""

from datetime import datetime, timezone
import http.client
import json
import os
import re
from urllib.parse import urlencode, urlsplit


GRAPH_HOST = "graph.facebook.com"
DEFAULT_VERSION = "v27.0"
BATCH_SIZE = 25
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_SETTINGS = {
    "instagram": ("IG_USER_ID", "IG_ACCESS_TOKEN"),
    "facebook": ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"),
}
_LINK_HOSTS = {
    "instagram": {"instagram.com", "www.instagram.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"},
}


def _settings(platform):
    if not isinstance(platform, str) or platform not in _SETTINGS:
        raise ValueError("Выберите платформу instagram или facebook.")
    id_name, token_name = _SETTINGS[platform]
    account_id = os.getenv(id_name, "").strip()
    token = os.getenv(token_name, "").strip()
    version = os.getenv("META_GRAPH_VERSION", DEFAULT_VERSION).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,30}", account_id):
        raise ValueError(f"Укажите числовой идентификатор в {id_name}.")
    if not re.fullmatch(r"[!-~]{1,8192}", token):
        raise ValueError(f"Проверьте настройку токена {token_name}.")
    if not re.fullmatch(r"v[1-9][0-9]?\.[0-9]{1,2}", version):
        raise ValueError("Укажите версию META_GRAPH_VERSION в формате v27.0.")

    discovery = ""
    if platform == "instagram":
        discovery = os.getenv("IG_DISCOVERY_USERNAME", "").strip()
        if discovery and not re.fullmatch(r"[A-Za-z0-9._]{1,30}", discovery):
            raise ValueError("В IG_DISCOVERY_USERNAME укажите имя профиля без @ и ссылки.")
    return account_id, token, version, discovery


def meta_status(platform: str) -> bool:
    """Check local settings only. An expired token still needs an actual request."""
    try:
        _settings(platform)
    except ValueError:
        return False
    return True


def _read_graph(path, token):
    # HTTPSConnection never follows redirects. The bearer token is sent only here.
    connection = http.client.HTTPSConnection(GRAPH_HOST, timeout=12)
    try:
        connection.request("GET", path, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        response = connection.getresponse()
        status = response.status
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException):
        raise ValueError("Не удалось связаться с Meta Graph API. Повторите позже.") from None
    finally:
        connection.close()

    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ответ Meta превышает допустимый размер.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if not 200 <= status < 300 or error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        if status == 429 or code in (4, 17, 32, 613):
            raise ValueError("Достигнут лимит запросов Meta. Повторите позже.")
        if status in (401, 403) or code in (10, 190, 200):
            raise ValueError("Meta отклонила доступ. Проверьте токен, права и роль аккаунта.")
        raise ValueError("Meta отклонила запрос. Проверьте доступность источника и версию API.")
    if not isinstance(data, dict):
        raise ValueError("Meta вернула нечитаемый ответ.")
    return data


def _post_url(value, platform):
    if not isinstance(value, str) or not value or re.search(r"[\s\x00-\x1f\\]", value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() not in _LINK_HOSTS[platform]:
        return None
    return value


def _source_rows(platform, fields, limit):
    account_id, token, version, discovery = _settings(platform)
    path = f"/{version}/{account_id}"
    if discovery:
        nested = f"business_discovery.username({discovery}){{media.limit({limit}){{{fields}}}}}"
        params = {"fields": nested}
    else:
        path += "/posts" if platform == "facebook" else "/media"
        params = {"fields": fields, "limit": limit}

    result = _read_graph(f"{path}?{urlencode(params)}", token)
    if discovery:
        profile = result.get("business_discovery")
        if not isinstance(profile, dict):
            raise ValueError("Meta не предоставила данные указанного профессионального профиля.")
        result = profile.get("media")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("Meta не вернула список публикаций.")
    return result["data"][:limit]


def meta_probe(platform: str) -> str:
    """Read a single media/post ID to check access, including on empty feeds."""
    _source_rows(platform, "id", 1)
    source = "Facebook" if platform == "facebook" else "Instagram"
    return f"Доступ к публикациям {source} подтверждён."


def _published_at(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        pass
    return None


def meta_feed(platform: str) -> list[dict]:
    """Return posts from the latest API batch (at most 25 items)."""
    if platform == "facebook":
        text_key, url_key, time_key = "message", "permalink_url", "created_time"
    else:
        text_key, url_key, time_key = "caption", "permalink", "timestamp"
    rows = _source_rows(platform, ",".join((text_key, url_key, time_key)), BATCH_SIZE)
    posts = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = _post_url(item.get(url_key), platform)
        body = item.get(text_key)
        if url:
            body = body.strip() if isinstance(body, str) else ""
            title = body.splitlines()[0][:120] if body else "Публикация без подписи"
            post = {"url": url, "title": title, "text": body, "platform": platform}
            published_at = _published_at(item.get(time_key))
            if published_at:
                post["published_at"] = published_at
            posts.append(post)
    return posts
