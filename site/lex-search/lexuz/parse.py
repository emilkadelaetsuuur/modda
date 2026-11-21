"""Разбор страницы документа lex.uz в метаданные и поисковые единицы (статьи).

Текст документа лежит в <div id="divCont"> как последовательность блоков
<div class="<ТИП> lx_elem"> … <div id="<якорь>">текст</div></div>, где ТИП:
  TEXT_HEADER_DEFAULT — заголовок раздела / главы / параграфа,
  CLAUSE_DEFAULT      — заголовок статьи («Статья 15. …»),
  ACT_TEXT и прочие   — абзацы текста.
Между ними встречаются скрытые служебные блоки: COMMENT (комментарии LexUz),
INDEXES_ON_REF (классификаторы ОКОЗ/ТСЗ), PUBLICATION_ORIGIN (источник опубликования).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import lxml.html

MAX_CHUNK_CHARS = 3000  # для документов без статей (постановления, положения)

# «30.04.2023. Конституция …» или «ЗРУ-647-сон 18.11.2020. О Национальной гвардии …»
_TITLE_DATE = re.compile(r"^\s*(?:(?P<num>\S+)\s+)?(?P<date>\d\d\.\d\d\.\d{4})\.\s*(?P<name>.+)$", re.S)
# Русские заголовки начинаются со слова («Глава I.»), узбекские — с номера («I боб.», «БИРИНЧИ БЎЛИМ.»),
# поэтому ищем ключевое слово в начале строки, а не только первым словом.
_HEADING_LEVELS = [
    (re.compile(r"\b(подраздел|кичик бўлим)\b", re.I), 1),
    (re.compile(r"\b(раздел|бўлим|section)\b", re.I), 0),
    (re.compile(r"\b(глава|боб|chapter)\b", re.I), 2),
    (re.compile(r"(§|\bпараграф\b)", re.I), 3),
    (re.compile(r"\b(приложение|илова)\b", re.I), 0),
]


@dataclass
class Unit:
    kind: str            # article | intro | text
    anchor: str          # id элемента на странице -> ссылка #anchor
    heading: str
    path: str            # «Раздел … / Глава …»
    paragraphs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass
class ParsedDoc:
    doc_id: int
    lang: str
    title: str
    adopted: str | None
    effective: str | None
    edition: str | None            # дата текущей редакции
    editions: list[str]
    okoz: str | None
    tsz: str | None
    publication: str | None
    units: list[Unit]
    number: str | None = None


_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍‎‏﻿"), None)
_DATE = re.compile(r"\d\d\.\d\d\.\d{4}")


def _clean(s: str) -> str:
    return " ".join(s.translate(_INVISIBLE).split())


_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _text(el) -> str:
    """Текст элемента; <br> превращаем в пробел, чтобы не склеивать слова,
    а <sup> — в надстрочные цифры: «статья 276<sup>4</sup>» -> «статья 276⁴», а не «2764»."""
    for br in el.iter("br"):
        br.tail = " " + (br.tail or "")
    for sup in el.iter("sup"):
        sup.text = (sup.text or "").translate(_SUPERSCRIPT)
    return _clean(el.text_content())


def _heading_level(text: str) -> int:
    head = text[:40]
    for rx, level in _HEADING_LEVELS:
        if rx.search(head):
            return level
    return 4


def parse_document(html: str, doc_id: int, lang: str) -> ParsedDoc:
    root = lxml.html.document_fromstring(html)

    raw_title = _clean(root.findtext(".//title") or "")
    adopted, title, number = None, raw_title, None
    m = _TITLE_DATE.match(raw_title)
    if m:
        adopted, title = m.group("date"), _clean(m.group("name"))
        if m.group("num"):
            number = re.sub(r"-(сон|son)$", "", m.group("num"), flags=re.I)

    effective = None
    for item in root.xpath('//div[contains(@class,"docHeader__item")][div[contains(@class,"docHeader__item-value")]]'):
        label = _clean(" ".join(item.xpath('./div[contains(@class,"docHeader__item-label")]//text()')))
        value = _clean(" ".join(item.xpath('./div[contains(@class,"docHeader__item-value")]//text()')))
        if label.lower().startswith(("дата вступления", "kuchga kirish", "кучга кириш", "entry into force")):
            effective = value or None

    edition_nodes = root.xpath('//*[@id="lx_var_t"]//div[contains(@class,"dropdown-menu__item")]')
    editions = [m.group(0) for e in edition_nodes if (m := _DATE.search(e.text_content()))]
    selected = root.xpath('//*[@id="lx_var_t"]//div[contains(@class,"lx_date_selected")]')
    sm = _DATE.search(selected[0].text_content()) if selected else None
    edition = sm.group(0) if sm else (editions[0] if editions else None)

    def _classifier(prefix: str) -> str | None:
        vals = root.xpath(f'//*[@id="{prefix}{doc_id}"]//span[@class="iorVal"]/text()')
        return _clean(" ; ".join(vals)).strip("[] ") or None

    okoz, tsz = _classifier("onLBC"), _classifier("onLS")
    pub = root.xpath('//*[@id="divCont"]/div[contains(@class,"PUBLICATION_ORIGIN")]')
    publication = _clean(pub[0].text_content()) if pub else None

    units: list[Unit] = []
    headings: dict[int, str] = {}
    current: Unit | None = None
    seen_article = False

    def path() -> str:
        return " / ".join(headings[k] for k in sorted(headings))

    def start(kind: str, anchor: str, heading: str) -> Unit:
        u = Unit(kind=kind, anchor=anchor, heading=heading, path=path())
        units.append(u)
        return u

    cont = root.xpath('//*[@id="divCont"]')
    blocks = list(cont[0]) if cont else []
    for block in blocks:
        classes = (block.get("class") or "").split()
        if "lx_elem" in classes:
            inner = block.xpath("./div[@id]")
            if not inner:
                continue
            anchor = inner[0].get("id")
            text = _text(inner[0])
            if not text:
                continue
            kind = classes[0]
            if kind == "TEXT_HEADER_DEFAULT":
                level = _heading_level(text)
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = text
                current = None
            elif kind == "CLAUSE_DEFAULT":
                seen_article = True
                current = start("article", anchor, text)
            else:
                too_long = current is not None and current.kind != "article" and len(current.text) > MAX_CHUNK_CHARS
                if current is None or too_long:
                    heading = headings[max(headings)] if headings else title
                    current = start("text" if (seen_article or headings) else "intro", anchor, heading)
                current.paragraphs.append(text)
        elif "COMMENT" in classes or "TEXT_CENTER" in classes:
            note = " ".join(_text(d) for d in block.xpath("./div[@id]"))
            if note and current is not None:
                current.notes.append(note)
    # заголовки статей без текста (например «Статья 12. Исключена») тоже оставляем
    return ParsedDoc(doc_id, lang, title, adopted, effective, edition, editions,
                     okoz, tsz, publication, units, number)
