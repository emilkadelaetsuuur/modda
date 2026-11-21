"""Extract a numbered article from the anchored article/paragraph layout on LexUZ."""

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_NUMBER = r"[0-9]+(?:[.^-][0-9]+)?"
_ARTICLE = re.compile(
    rf"^(?:Статья\s+(?P<ru>{_NUMBER})\s*\.|(?P<uz>{_NUMBER})\s*[-‑–]\s*(?:модда|modda)\s*\.)",
    re.IGNORECASE,
)
_MAX_DOCUMENT = 16 * 1024 * 1024
_DAY = r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}"
_EDITION_ERROR = "Не удалось подтвердить историю редакций на Lex.uz. Выберите редакцию в календаре Lex.uz и укажите ссылку с ONDATE."


def _revision(value):
    match = re.fullmatch(rf"({_DAY})(?:[ +]([0-9]{{2}}))?", value)
    if not match:
        raise ValueError
    day, sequence = match.groups()
    order = (datetime.strptime(day, "%d.%m.%Y").date(), int(sequence or "0"))
    return order, day + (f"+{sequence}" if sequence else "")


def _document_url(value: str) -> tuple[str, str]:
    try:
        url = urlsplit(value.strip())
        if url.scheme not in {"https", "http"} or url.netloc.lower() not in {"lex.uz", "www.lex.uz"}:
            raise ValueError
        if not re.fullmatch(r"/(?:(?:ru|uz|en)/)?(?:docs|acts|mact)/-?[0-9]+/?", url.path):
            raise ValueError
        options = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        date = ""
        if options:
            if len(options) != 1 or options[0][0].upper() != "ONDATE":
                raise ValueError
            _, date = _revision(options[0][1])
        base = "https://lex.uz" + url.path.rstrip("/")
        return (f"{base}?ONDATE={date}" if date else base), date
    except (ValueError, AttributeError):
        raise ValueError("Укажите ссылку на документ lex.uz с необязательной датой ONDATE=ДД.ММ.ГГГГ.") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect may silently open a different document or edition.
        return None


def _load(url: str) -> str:
    req = Request(url, headers={"User-Agent": "LexMonitor/1.0"})
    try:
        with build_opener(_NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(_MAX_DOCUMENT + 1)
    except HTTPError as exc:
        raise ValueError(f"LexUZ не открыл запрошенный документ или редакцию (HTTP {exc.code}).") from None
    except (URLError, OSError, HTTPException):
        raise ValueError("Не удалось получить документ LexUZ. Повторите позже.") from None
    if len(raw) > _MAX_DOCUMENT:
        raise ValueError("Документ LexUZ превышает лимит загрузки.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Не удалось прочитать кодировку документа LexUZ.") from None


class _RevisionMenu(HTMLParser):
    """Read the edition menu, effective date and repeal notice on the Russian document page."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.menu_depth = 0
        self.capture = None
        self.parts = []
        self.entries = []
        self.last_label = ""
        self.effective_dates = set()
        self.status = ""

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        self.depth += 1
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        kind = ""
        if attrs.get("id") == "lx_var_t":
            self.menu_depth = self.depth
        elif self.menu_depth and self.depth == self.menu_depth + 1:
            if "lx_date_selected" in classes:
                kind = "selected"
            elif "lx_date_link" in classes:
                kind = "link"
            else:
                raise ValueError(_EDITION_ERROR)
        elif attrs.get("id") == "lx_lact_num_top":
            kind = "status"
        elif "docHeader__item-label" in classes:
            kind = "label"
        elif "docHeader__item-value" in classes and self.last_label == "Дата вступления в силу":
            kind = "effective"
        if kind:
            self.capture = (self.depth, kind, attrs.get("onclick", ""))
            self.parts = []

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self.capture and self.capture[0] == self.depth:
            _, kind, action = self.capture
            text = " ".join("".join(self.parts).split())
            if kind in ("link", "selected"):
                self.entries.append((kind, action, text))
            elif kind == "status":
                self.status = text
            elif kind == "label":
                self.last_label = text
            elif kind == "effective":
                self.effective_dates.add(text)
                self.last_label = ""
            self.capture = None
        if self.menu_depth == self.depth:
            self.menu_depth = 0
        self.depth = max(0, self.depth - 1)


def _edition_for_date(base, requested):
    if not isinstance(requested, str) or not re.fullmatch(_DAY, requested):
        raise ValueError("Укажите дату публикации в формате ДД.ММ.ГГГГ.")
    try:
        as_of = _revision(requested)[0][0]
    except ValueError:
        raise ValueError("Некорректная дата публикации.") from None
    document_id = base.rsplit("/", 1)[1]
    index_path = f"/ru/docs/{document_id}"
    menu = _RevisionMenu()
    try:
        menu.feed(_load("https://lex.uz" + index_path))
        menu.close()
        if len(menu.effective_dates) != 1 or not menu.entries:
            raise ValueError
        effective = _revision(next(iter(menu.effective_dates)))[0][0]
        end = None
        if "утратил силу" in menu.status.lower():
            notice = re.search(rf"утратил силу\s+({_DAY})", menu.status, re.IGNORECASE)
            if not notice:
                raise ValueError
            end = _revision(notice[1])[0][0]
        editions = {}
        selected = set()
        for kind, action, label in menu.entries:
            order, date = _revision(label)
            if kind == "link":
                # Use only this document's edition links. Never interpret the page's JavaScript.
                link = re.fullmatch(r"lxOpenUrl\('([^']+)'\);?", action)
                if not link or not link[1].startswith(index_path + "?ONDATE="):
                    raise ValueError
                link_order, date = _revision(link[1].split("=", 1)[1])
                if link_order != order:
                    raise ValueError
            else:
                selected.add(order)
            editions[order] = date
        if len(selected) != 1:
            raise ValueError
    except ValueError:
        raise ValueError(_EDITION_ERROR) from None
    if as_of < effective:
        raise ValueError("На дату публикации документ ещё не вступил в силу.")
    if end and as_of >= end:
        raise ValueError("На дату публикации документ уже утратил силу.")
    eligible = [order for order in editions if order[0] <= as_of]
    if not eligible:
        raise ValueError(_EDITION_ERROR)
    return f"{base}?ONDATE={editions[max(eligible)]}"


class _Document(HTMLParser):
    """Collect only ID-bearing text children of actual LexUZ law blocks."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.block_depth = 0
        self.content_depth = 0
        self.block_kind = ""
        self.anchor = ""
        self.parts = []
        self.blocks = []
        self.title_parts = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag == "div":
            self.depth += 1
            attrs = dict(attrs)
            classes = (attrs.get("class") or "").split()
            if not self.block_depth and ("lx_elem" in classes or any(
                    c.startswith(("ACT_", "CLAUSE_", "TEXT_HEADER_")) for c in classes)):
                self.block_depth = self.depth
                self.block_kind = next((c for c in classes if c.isupper()), "")
            elif self.block_depth and self.depth == self.block_depth + 1:
                anchor = attrs.get("id") or ""
                if re.fullmatch(r"-?[0-9]+", anchor) and attrs.get("name") == anchor:
                    self.content_depth = self.depth
                    self.anchor = anchor
                    self.parts = []
        elif self.content_depth:
            if tag in {"br", "p", "td", "li"}:
                self.parts.append(" ")
            elif tag == "sup":
                self.parts.append("^")

    def handle_data(self, data):
        if self.content_depth:
            self.parts.append(data)
        elif self.in_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag != "div":
            return
        if self.content_depth and self.depth == self.content_depth:
            text = " ".join("".join(self.parts).split())
            if text:
                self.blocks.append((self.block_kind, self.anchor, text))
            self.content_depth = 0
        if self.block_depth and self.depth == self.block_depth:
            self.block_depth = 0
        self.depth = max(0, self.depth - 1)


def lex_article(url: str, number: str) -> dict:
    """Read the page's article verbatim, with its real anchor and retrieval metadata."""
    url, date = _document_url(url)
    number = str(number).strip()
    if not re.fullmatch(_NUMBER, number):
        raise ValueError("Укажите номер статьи, например 129.")
    page = _Document()
    page.feed(_load(url))
    page.close()

    matches = []
    for index, (kind, anchor, heading) in enumerate(page.blocks):
        if kind == "CLAUSE_DEFAULT":
            match = _ARTICLE.match(heading)
            if match and number in match.groups():
                matches.append((index, anchor, heading))
    if not matches:
        raise ValueError(f"Статья {number} не найдена в распознанных заголовках этого документа LexUZ.")
    if len(matches) > 1:
        raise ValueError(f"В документе несколько статей {number}. Уточните документ.")
    index, anchor, heading = matches[0]
    paragraphs = []
    for kind, _, text in page.blocks[index + 1:]:
        if kind in {"CLAUSE_DEFAULT", "TEXT_HEADER_DEFAULT", "ACT_TITLE", "ACT_FORM"}:
            break
        if kind != "ACT_TEXT":
            raise ValueError("У статьи сложная структура. Сверьте полный текст на LexUZ.")
        paragraphs.append(text)
    if not paragraphs:
        raise ValueError("Не удалось выделить тело статьи на LexUZ.")
    version = f"Запрошенная дата редакции LexUZ: {date}" if date else "Версия на странице при загрузке."
    checked = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    document = " ".join("".join(page.title_parts).split())
    return {
        "url": f"{url}#{anchor}",
        "title": f"{heading} · {document}" if document else heading,
        "text": "\n".join(paragraphs) + f"\n\n{version}\nЗагружено: {checked}",
        "platform": "lexuz",
        "kind": "article",
    }


def lex_passage(value, excerpt="", edition_date=""):
    """Verify a verbatim passage in a numbered LexUZ document or its specific anchor."""
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith("https://"):
        raise ValueError("Укажите HTTPS-ссылку на документ Lex.uz.")
    base, edition = _document_url(value)
    anchor = urlsplit(value).fragment
    if anchor and not re.fullmatch(r"-?[0-9]+", anchor):
        raise ValueError("Укажите числовой якорь пункта после # в ссылке Lex.uz.")
    quote = " ".join(excerpt.split())
    if len(quote) > 8000 or (not anchor and len(quote) < 12):
        raise ValueError("Откройте конкретный пункт по ссылке с # или вставьте дословную цитату от 12 символов.")
    if edition_date and not edition:
        base = _edition_for_date(base, edition_date)
    page = _Document()
    page.feed(_load(base))
    page.close()
    matches = [(key, block) for kind, key, block in page.blocks
               if (not anchor or anchor == key) and (not quote or quote in block)]
    if len(matches) != 1:
        raise ValueError("По указанной ссылке не удалось однозначно подтвердить цитату. Проверьте пункт, редакцию и язык Lex.uz.")
    key, passage = matches[0]
    return {"url": f"{base}#{key}", "passage": passage,
            "title": " ".join("".join(page.title_parts).split()),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
