"""Discover reels on a public Instagram profile and in links from its Tashkent channel."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
import http.client
import json
import re
from urllib.parse import urlencode, urlsplit


CHANNEL = "kvartira_sotamiza"
ACCOUNT = "kvartira_sotamiz"
DISCOVERY_URL = f"https://t.me/s/{CHANNEL}?q=instagram.com"
PROFILE_URL = f"https://www.instagram.com/{ACCOUNT}/embed/"
MAX_CANDIDATES = 8
MAX_BYTES = 2 * 1024 * 1024
MONTHS = "January February March April May June July August September October November December".split()
REEL_PATH = re.compile(r"/(?:[\w.]{1,30}/)?reels?/([A-Za-z0-9_-]{5,32})/?", re.ASCII)


def _reel_url(link):
    if not isinstance(link, str) or len(link) > 2048 or "\\" in link or any(ord(c) <= 32 for c in link):
        return ""
    try:
        parts = urlsplit(link)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.netloc.lower() not in {"www.instagram.com", "instagram.com"}:
        return ""
    match = REEL_PATH.fullmatch(parts.path)
    return f"https://www.instagram.com/reel/{match[1]}/" if match else ""


class _ChannelLinks(HTMLParser):
    """Collect unique reel links from the text of public messages in the configured channel."""

    def __init__(self):
        super().__init__()
        self.links = {}
        self.depth = 0
        self.message_depth = self.text_depth = 0
        self.source = ""

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "div":
            self.depth += 1
            post = fields.get("data-post", "")
            if re.fullmatch(rf"{CHANNEL}/[1-9][0-9]*", post or ""):
                self.message_depth = self.depth
                self.source = f"https://t.me/{post}"
            if self.message_depth and "tgme_widget_message_text" in (fields.get("class") or "").split():
                self.text_depth = self.depth
        if tag == "a" and self.text_depth:
            url = _reel_url(fields.get("href"))
            if url:
                self.links.pop(url, None)
                self.links[url] = self.source

    def handle_endtag(self, tag):
        if tag == "div":
            if self.depth == self.text_depth:
                self.text_depth = 0
            if self.depth == self.message_depth:
                self.message_depth = 0
                self.source = ""
            self.depth = max(0, self.depth - 1)


class _PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.properties = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            fields = dict(attrs)
            name = fields.get("property")
            if name in ("og:url", "og:description"):
                self.properties[name] = fields.get("content") or ""


def _get(url):
    # Only these two fixed web services are contacted. Redirects and embedded URLs are never followed.
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in {"t.me", "www.instagram.com"}:
        raise ValueError("Недопустимый адрес источника.")
    path = parts.path + ("?" + parts.query if parts.query else "")
    connection = http.client.HTTPSConnection(parts.netloc, timeout=7)
    try:
        connection.request("GET", path, headers={"Accept-Language": "en-US,en;q=0.8", "User-Agent": "LexMonitor/1.0"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Ответ источника слишком большой.")
        return body.decode("utf-8")
    finally:
        connection.close()


def _published_at(url, account):
    parser = _PageMetadata()
    parser.feed(_get(url))
    if _reel_url(parser.properties.get("og:url")) != url:
        return ""
    # Meta may prepend engagement counts. Only read the date next to the account, before the caption.
    prefix = parser.properties.get("og:description", "").partition(":")[0]
    match = re.fullmatch(rf"(?:[^\n]* - )?{re.escape(account)} on ([A-Za-z]+) (\d{{1,2}}), (\d{{4}})", prefix, re.I)
    if not match or match[1].title() not in MONTHS:
        return ""
    try:
        return date(int(match[3]), MONTHS.index(match[1].title()) + 1, int(match[2])).isoformat()
    except ValueError:
        return ""


_READ_ERRORS = (OSError, http.client.HTTPException, ValueError, RecursionError)


def _channel_links():
    parser = _ChannelLinks()
    parser.feed(_get(DISCOVERY_URL))
    return list(reversed(parser.links.items()))


def _profile_links():
    # The HTML used by Instagram's public profile embed includes the data for its visible grid.
    page = _get(PROFILE_URL)
    for match in re.finditer(r'"contextJSON"\s*:\s*', page):
        encoded, _ = json.JSONDecoder().raw_decode(page, match.end())
        if not isinstance(encoded, str):
            continue
        payload = json.loads(encoded)
        context = payload.get("context") if isinstance(payload, dict) else None
        if not isinstance(context, dict) or context.get("type") != "Profile" or context.get("username") != ACCOUNT:
            continue
        media = context.get("graphql_media")
        if not isinstance(media, list):
            break
        links = []
        for item in media:
            node = item.get("shortcode_media") if isinstance(item, dict) else None
            if not isinstance(node, dict) or node.get("is_video") is not True:
                continue
            code = node.get("shortcode")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{5,32}", code):
                links.append((f"https://www.instagram.com/reel/{code}/", PROFILE_URL))
        return links
    raise ValueError("Витрина Instagram недоступна.")


def _verify(candidate):
    url, source = candidate
    try:
        metadata = json.loads(_get("https://www.instagram.com/api/v1/oembed/?" + urlencode({"url": url})))
        if not isinstance(metadata, dict) or metadata.get("provider_name") != "Instagram":
            raise ValueError("Instagram не подтвердил публикацию.")
        account = metadata.get("author_name")
        caption = metadata.get("title")
        if not isinstance(account, str) or not re.fullmatch(r"[\w.]{1,30}", account, re.ASCII):
            raise ValueError("Автор не указан в Instagram.")
        if source == PROFILE_URL and account.lower() != ACCOUNT:
            raise ValueError("Автор не совпадает с профилем Instagram.")
        if not isinstance(caption, str) or not caption.strip():
            raise ValueError("Подпись недоступна в Instagram.")
    except _READ_ERRORS as error:
        return None, f"{url} — {error}"
    row = {"url": url, "account": account, "description": caption.strip(), "published_at": "", "source_url": source}
    try:
        row["published_at"] = _published_at(url, account)
    except _READ_ERRORS as error:
        return row, f"{url} — не удалось проверить дату: {error}"
    return row, ""


def search_public_reels():
    """Check at most eight distinct public reel URLs, returning captions provided by Instagram itself."""
    result = {"discovery_url": DISCOVERY_URL, "scanned": 0, "verified": 0, "rows": [], "errors": []}
    sources = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = {DISCOVERY_URL: pool.submit(_channel_links), PROFILE_URL: pool.submit(_profile_links)}
        for url, task in tasks.items():
            try:
                sources[url] = task.result()
            except _READ_ERRORS as error:
                sources[url] = []
                result["errors"].append(f"Не удалось прочитать {url} — {error}")
    channel = sources[DISCOVERY_URL]
    profile = sources[PROFILE_URL]
    # Reserve three positions for cross-posted links; fill the rest with the newest profile videos.
    found = {}
    for url, source in channel[:3] + profile[:5] + channel[3:] + profile[5:]:
        found.setdefault(url, source)
        if len(found) == MAX_CANDIDATES:
            break
    candidates = list(found.items())
    result["scanned"] = len(candidates)
    if not candidates:
        return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, error in pool.map(_verify, candidates):
            if row:
                result["rows"].append(row)
            if error:
                result["errors"].append(error)
    result["verified"] = len(result["rows"])
    return result
