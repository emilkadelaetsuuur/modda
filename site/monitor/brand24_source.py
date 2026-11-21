"""Convert Brand24 notification emails to review records without opening their links."""

from datetime import datetime, timezone
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlsplit

from telegram_source import public_post_url


ALERT_SENDER = "mail@alerts.brand24.com"
_HOME = "https://app.brand24.com/"
_URL = re.compile(r"""https?://[^\s<>"'«»]+""", re.I)
_BRAND_HREF = re.compile(r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
                         r"brand24\.com(?:[/?#][^\\\s\x00-\x1f\x7f]*)?", re.ASCII | re.I)
_FOOTER = re.compile(
    r"(?:^|\s)(?:unsubscribe\b|follow (?:us|brand24)\b|connect with (?:us|brand24)\b|"
    r"manage (?:your )?(?:subscription|preferences|notifications)\b|"
    r"you(?:'re| are) receiving this (?:email|message)\b|"
    r"отписаться\b|подписывайтесь на нас\b|©\s*(?:\d{4}|Brand24)|"
    r"(?:begin forwarded|original) message\b)", re.I)
_FOOTER_CLASS = re.compile(r"footer|social[-_ ]?(?:icons?|links?|follow)|unsubscribe|contentinfo", re.I)
_PRIVATE = re.compile(
    r"https?://|www\.|\b[\w.+-]+@[\w.-]+\.\w+|"
    r"\b(?:bearer|api[-_ ]?key|access[-_ ]?token|password|secret|token)\s*[:= ]\s*\S|"
    r"[A-Za-z0-9_/-]{40,}", re.I)


def _short_label(value, limit):
    value = str(value)
    if len(value) > 4096:
        return ""
    label = " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split())
    if _PRIVATE.search(label):
        return ""
    return label if len(label) <= limit else label[:limit - 1].rstrip() + "…"


def _dashboard(candidate):
    if len(candidate) > 2048:
        return None
    try:
        url = urlsplit(candidate)
    except ValueError:
        return None
    if url.scheme != "https" or url.netloc.lower() != "app.brand24.com":
        return None
    if re.fullmatch(r"/(?:panel/(?:results|analysis)/[1-9][0-9]{0,19}|"
                    r"projects?/[1-9][0-9]{0,19}(?:/mentions)?)/?", url.path):
        return _HOME[:-1] + url.path
    if url.path.rstrip("/") in {"/panel/results", "/panel/analysis"}:
        values = parse_qs(url.query, keep_blank_values=True).get("sid", [])
        if len(values) == 1 and re.fullmatch(r"[1-9][0-9]{0,19}", values[0]):
            return _HOME[:-1] + url.path + "?sid=" + values[0]
    return None


def _plain_links(body):
    footer = False
    for line in body.splitlines():
        marker = _FOOTER.search(line)
        for match in _URL.finditer(line):
            candidate = match[0].rstrip(".,;!?:)]}…")
            ignored = footer or line.lstrip().startswith(">") or (marker and marker.start() < match.start())
            yield candidate, "", bool(ignored)
        footer = footer or bool(marker)


def _publication_time(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
                        r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
                        value) or value.endswith("-00:00"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


class _HtmlLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.footer = False
        self.ignored_tag = None
        self.ignored_depth = 0
        self.anchor = None
        self.anchor_ignored = False
        self.label = []
        self.article_depth = 0
        self.article_start = 0
        self.article_dates = set()
        self.dates_by_url = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        role = " ".join(attrs.get(key) or "" for key in ("class", "id", "role"))
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                   "meta", "param", "source", "track", "wbr"}:
            if tag == "br" and self.anchor is not None:
                self.label.append(" ")
            return
        if self.ignored_tag:
            self.ignored_depth += tag == self.ignored_tag
        elif (tag in {"footer", "script", "style", "head", "template", "blockquote", "nav"}
              or _FOOTER_CLASS.search(role) or "hidden" in attrs
              or re.search(r"display\s*:\s*none", attrs.get("style") or "", re.I)):
            self.ignored_tag = tag
            self.ignored_depth = 1
        if tag == "a":
            self.anchor = attrs.get("href") or ""
            self.anchor_ignored = self.footer or bool(self.ignored_tag)
            self.label = []
        if tag == "article":
            if self.article_depth == 0:
                self.article_start = len(self.links)
                self.article_dates = set()
            else:
                self.article_dates.add(None)
            self.article_depth += 1
        if (tag == "time" and self.article_depth and not self.footer and not self.ignored_tag
                and "datePublished" in (attrs.get("itemprop") or "").split()):
            self.article_dates.add(_publication_time(attrs.get("datetime") or ""))

    def handle_data(self, data):
        if self.anchor is not None:
            if self.anchor_ignored or not self.ignored_tag:
                self.label.append(data)
        else:
            for candidate, _, ignored in _plain_links(data):
                self.links.append((candidate, "", self.footer or bool(self.ignored_tag) or ignored))
            if not self.ignored_tag and _FOOTER.search(data):
                self.footer = True

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            label = "".join(self.label)
            shown = label.strip()
            if shown.lower().startswith("https://") and (visible := public_post_url(shown)):
                target = public_post_url(self.anchor)
                if len(self.anchor) <= 2048 and _BRAND_HREF.fullmatch(self.anchor):
                    self.anchor = shown
                elif target != visible:
                    self.links.extend([(shown, "", True), (self.anchor, "", True)])
                    self.anchor = ""
            if not self.ignored_tag and _FOOTER.search(label):
                self.footer = True
            self.links.append((self.anchor, label, self.anchor_ignored or self.footer or bool(self.ignored_tag)))
            self.anchor = None
            self.label = []
        if tag == "article" and self.article_depth:
            self.article_depth -= 1
            if self.article_depth == 0 and self.article_dates:
                sources = {public_post_url(link) for link, _, ignored in self.links[self.article_start:]
                           if not ignored}
                sources.discard(None)
                if len(sources) == 1:
                    _, url = sources.pop()
                    self.dates_by_url.setdefault(url, set()).update(self.article_dates)
        if tag == self.ignored_tag:
            self.ignored_depth -= 1
            if self.ignored_depth == 0:
                self.ignored_tag = None


def _body_parts(part):
    """Visit alternative representations of the body, skipping files and forwarded mail."""
    if part.get_content_disposition() == "attachment" or part.get_filename() is not None:
        return
    content_type = part.get_content_type()
    if content_type in {"text/html", "text/plain"}:
        yield part
    elif part.get_content_maintype() == "multipart" and part.is_multipart():
        children = list(part.iter_parts())
        if content_type == "multipart/related":
            start = part.get_param("start")
            if start:
                children = [child for child in children if child.get("Content-ID") == start]
            children = children[:1]
        for child in children:
            bodies = list(_body_parts(child))
            if bodies:
                yield from bodies
                if content_type != "multipart/alternative":
                    return


def _from_brand24(message):
    senders = message.get_all("From", [])
    if len(senders) != 1 or senders[0].defects:
        return False
    if any(group.display_name is not None for group in senders[0].groups):
        return False
    addresses = senders[0].addresses
    return len(addresses) == 1 and addresses[0].addr_spec.lower() == ALERT_SENDER


def parse_brand24_alert(raw: bytes) -> list[dict]:
    """Return direct mentions, one dashboard review record, or [] for an invalid sender."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if not _from_brand24(message):
            return []
        parts = list(_body_parts(message))
    except (MessageError, ValueError, IndexError, RecursionError):
        return []

    direct = {}
    plain = {}
    ignored_urls = set()
    dashboards = set()
    dates_by_url = {}
    for part in parts:
        try:
            body = part.get_content()
        except (LookupError, ValueError, UnicodeError):
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        is_html = part.get_content_type() == "text/html"
        if is_html:
            parser = _HtmlLinks()
            try:
                parser.feed(body)
                parser.close()
            except (ValueError, AssertionError):
                continue
            links = parser.links
            for url, dates in parser.dates_by_url.items():
                dates_by_url.setdefault(url, set()).update(dates)
        else:
            links = _plain_links(body)
        for candidate, label, ignored in links:
            source = public_post_url(candidate)
            if source and ignored:
                ignored_urls.add(source[1])
            elif source:
                platform, url = source
                records = direct if is_html else plain
                snippet = _short_label(label, 200)
                if url not in records or (snippet and not records[url]["text"]):
                    records[url] = {"platform": platform, "kind": "brand24_mention", "url": url,
                                    "title": "Упоминание из Brand24", "text": snippet}
            elif not ignored and (dashboard := _dashboard(candidate)):
                dashboards.add(dashboard)
    for url, row in plain.items():
        if url not in ignored_urls:
            direct.setdefault(url, row)
    if direct:
        for url, dates in dates_by_url.items():
            if url in direct and len(dates) == 1 and None not in dates:
                direct[url]["published_at"] = next(iter(dates))
        return list(direct.values())

    message_ids = [value.strip() for name, value in message.raw_items() if name.lower() == "message-id"]
    key = raw
    if (len(message_ids) == 1 and message_ids[0].isascii()
            and re.fullmatch(r"<[^\s<>@]+@[^\s<>@]+>", message_ids[0])):
        key = message_ids[0].encode("ascii")
    return [{"platform": "brand24", "kind": "brand24_alert",
             "url": dashboards.pop() if len(dashboards) == 1 else _HOME,
             "title": _short_label(message.get("Subject", ""), 160) or "Уведомление Brand24",
             "text": "Проверьте источник публикации в кабинете Brand24.",
             "_identity": sha256(key).hexdigest()}]
