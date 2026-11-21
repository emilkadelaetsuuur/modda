"""Обход списков результатов поиска lex.uz.

Страницы поиска сделаны на ASP.NET WebForms: первая страница отдаётся по GET,
следующие — через POST-«postback» с __VIEWSTATE и __EVENTTARGET ссылки пагинатора.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import urljoin

import lxml.html

from . import BASE_URL
from .fetch import PoliteClient

# Готовые наборы для команды crawl. lang=1 — русский интерфейс поиска.
PRESETS = {
    "constitution": ("/ru/search/nat?lang=1&form_id=4131", "Конституция"),
    "codes": ("/ru/search/nat?lang=1&sort_id=3975&form_id=3964", "Кодекс"),
    "laws": ("/ru/search/nat?lang=1&sort_id=3975&form_id=3968", "Закон"),
    "president": ("/ru/search/nat?lang=1&sort_id=3985", "Акт Президента"),
    "government": ("/ru/search/nat?lang=1&sort_id=3988", "Акт Правительства"),
}

STATUS_BY_CLASS = {"status_code_y": "Y", "status_code_r": "R", "status_code_n": "N"}
STATUS_LABEL = {"Y": "Действующий", "R": "Утратил силу", "N": "Не действующий", None: "—"}

_DOC_HREF = re.compile(r"/(ru|uz|en|oz)?/?docs/(\d+)$")
_POSTBACK = re.compile(r"__doPostBack\('([^']+)'")
_FOUND = re.compile(r"найдено\s+([\d\s ]+)\s+документ", re.I)
_BADGE = re.compile(r"^(?P<type>.*?),?\s*от\s+(?P<date>\d\d\.\d\d\.\d{4})\s*г?\.?(?:\s*№\s*(?P<num>.+))?$")


@dataclass
class ListedDoc:
    doc_id: int
    title: str
    status: str | None
    badge: str
    doc_type: str | None = None
    date: str | None = None
    number: str | None = None


@dataclass
class ListingPage:
    docs: list[ListedDoc]
    total: int | None
    form_action: str | None
    form_fields: dict = field(default_factory=dict)
    pager: dict = field(default_factory=dict)  # подпись ссылки -> __EVENTTARGET


def parse_listing(html: str, page_url: str) -> ListingPage:
    root = lxml.html.document_fromstring(html)
    docs: list[ListedDoc] = []
    for row in root.xpath('//tr[contains(@class,"dd-table__main-item")]'):
        links = row.xpath('.//div[contains(@class,"dd-table__main-left-desc")]//a[@href]')
        if not links:
            continue
        m = _DOC_HREF.search(links[0].get("href", ""))
        if not m:
            continue
        status = None
        for cls in row.xpath('.//span[contains(@class,"lx_act_state")]//i/@class'):
            for token in cls.split():
                status = STATUS_BY_CLASS.get(token, status)
        badge = " ".join("".join(row.xpath('.//span[contains(@class,"badge")]//text()')).split())
        item = ListedDoc(
            doc_id=int(m.group(2)),
            title=" ".join(links[0].text_content().split()),
            status=status,
            badge=badge,
        )
        bm = _BADGE.match(badge)
        if bm:
            item.doc_type = bm.group("type").strip() or None
            item.date = bm.group("date")
            item.number = (bm.group("num") or "").strip() or None
        docs.append(item)

    total = None
    fm = _FOUND.search(html)
    if fm:
        total = int(re.sub(r"\D", "", fm.group(1)))

    form_action, fields = None, {}
    forms = root.xpath('//form[@id="Form1"]')
    if forms:
        form = forms[0]
        form_action = urljoin(urljoin(BASE_URL, page_url), form.get("action", ""))
        for inp in form.xpath(".//input[@name]"):
            if inp.get("type") in ("checkbox", "radio") and inp.get("checked") is None:
                continue
            if inp.get("type") in ("submit", "button", "image"):
                continue
            fields[inp.get("name")] = inp.get("value") or ""
        for sel in form.xpath(".//select[@name]"):
            chosen = sel.xpath(".//option[@selected]") or sel.xpath(".//option")
            if chosen:
                fields[sel.get("name")] = chosen[0].get("value") or ""

    pager = {}
    for a in root.xpath('//table[@id="ucFoundActsControl_rptPaging"]//a[@href] | //div[contains(@class,"pgn")]//a[@href]'):
        pm = _POSTBACK.search(a.get("href", "").replace("&#39;", "'"))
        label = " ".join(a.text_content().split())
        if pm and label:
            pager.setdefault(label, pm.group(1))
    return ListingPage(docs, total, form_action, fields, pager)


def iter_listing(client: PoliteClient, url: str, max_pages: int | None = None,
                 log=print) -> Iterator[ListedDoc]:
    """Проходит по всем страницам списка и отдаёт найденные документы."""
    html = client.get_text(url)
    page_no = 1
    seen: set[int] = set()
    while True:
        page = parse_listing(html, url)
        new = [d for d in page.docs if d.doc_id not in seen]
        if page_no == 1 and page.total is not None:
            log(f"  в списке найдено документов: {page.total}")
        log(f"  страница {page_no}: {len(page.docs)} документов")
        for d in new:
            seen.add(d.doc_id)
            yield d
        if not new or (max_pages and page_no >= max_pages):
            return
        target = page.pager.get(str(page_no + 1))
        if target is None:
            # пагинатор показывает «окно» номеров; дальше — через «...» / «Следующий»
            for label in ("...", "…", "Следующий", "Следующая", "»", ">"):
                if label in page.pager:
                    target = page.pager[label]
                    break
        if target is None or not page.form_action:
            return
        data = dict(page.form_fields)
        data["__EVENTTARGET"] = target
        data["__EVENTARGUMENT"] = ""
        html = client.post_text(page.form_action, data)
        page_no += 1
