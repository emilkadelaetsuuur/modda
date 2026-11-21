"""Выдержки из базы в формате MODDA: {title, article, url, as_of, text}.

Их берёт генератор роликов MODDA («база команды») и мост опровержений. Если в запросе есть ссылка
на lex.uz и номер статьи, первой идёт именно эта статья — на нужном языке, даже когда ссылка ведёт
на документ другого языка (у узбекской и русской версий на lex.uz разные номера документов).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import uzbek
from .search import search

LEX_LINK = re.compile(r"https?://(?:www\.)?lex\.uz/(?:(ru|uz|oz|en)/)?(?:docs|acts)/(\d+)[^\s#]*(?:#(\d+))?", re.I)
LANG_MARK = re.compile(r"\blang[:=](ru|uz)\b", re.I)
_ART_IN_TEXT = re.compile(r"(?:стать[яиюей]|ст\.)\s*(\d+)|(\d+)\s*-\s*(?:modda|модда)", re.I)
_ART_IN_HEADING = re.compile(r"^\s*(?:Статья\s+(\d+)|(\d+)\s*-\s*модда)", re.I)
MAX_TEXT = 16000


def article_number(heading: str | None) -> str | None:
    m = _ART_IN_HEADING.match(heading or "")
    return (m.group(1) or m.group(2)) if m else None


def detect_lang(text: str) -> str:
    """Язык запроса: узбекские буквы или латиница → uz, иначе ru."""
    cyr = len(re.findall(r"[а-яё]", text, re.I))
    return "uz" if re.search(r"[ўқғҳ]", text, re.I) or len(re.findall(r"[a-z]", text, re.I)) > cyr else "ru"


def _unit(con, doc_id: int, lang: str, num: str | None = None, anchor: str | None = None):
    if anchor:
        row = con.execute("SELECT * FROM units WHERE doc_id=? AND lang=? AND anchor=?", (doc_id, lang, anchor)).fetchone()
        if row:
            return row
    if num:
        pattern = f"Статья {num}.%" if lang == "ru" else f"{num}-модда%"
        return con.execute("SELECT * FROM units WHERE doc_id=? AND lang=? AND heading LIKE ? ORDER BY ord LIMIT 1",
                           (doc_id, lang, pattern)).fetchone()
    return None


def counterpart(con, doc_id: int, lang: str) -> int | None:
    """Тот же документ на другом языке: совпадают дата принятия и цифры номера (ЗРУ-776 ↔ ЎРҚ-776)."""
    other = "uz" if lang == "ru" else "ru"
    d = con.execute("SELECT adopted, number, units FROM docs WHERE id=? AND lang=?", (doc_id, lang)).fetchone()
    if not d or not d["adopted"]:
        return None
    digits = re.sub(r"\D", "", d["number"] or "")
    rows = [r for r in con.execute("SELECT id, number, units FROM docs WHERE lang=? AND adopted=? AND indexed_at IS NOT NULL",
                                   (other, d["adopted"]))
            if re.sub(r"\D", "", r["number"] or "") == digits]
    if not rows:
        return None
    return min(rows, key=lambda r: abs((r["units"] or 0) - (d["units"] or 0)))["id"]


def _iso(day: str | None) -> str | None:
    m = re.fullmatch(r"(\d\d)\.(\d\d)\.(\d{4})", day or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else None


def to_passage(con, unit, script: str = "lat") -> dict | None:
    d = con.execute("SELECT title, edition, adopted FROM docs WHERE id=? AND lang=?", (unit["doc_id"], unit["lang"])).fetchone()
    show = (lambda s: uzbek.translit(s or "", script)) if unit["lang"] == "uz" else (lambda s: s or "")
    text = show(unit["body"]).strip()
    if len(text) > MAX_TEXT:
        cut = text.rfind(". ", 0, MAX_TEXT - 1)
        text = text[: cut + 1 if cut > MAX_TEXT // 2 else MAX_TEXT - 1] + "…"
    as_of = _iso(d["edition"]) or _iso(d["adopted"]) or date.today().isoformat()
    if len(text) < 20 or as_of > date.today().isoformat():
        return None
    return {
        "title": show(d["title"])[:450],
        "article": (show(unit["heading"]) or show(d["title"]))[:300],
        "url": f"https://lex.uz/{unit['lang']}/docs/{unit['doc_id']}#{unit['anchor']}",
        "as_of": as_of,
        "text": text,
        "lang": unit["lang"],
    }


def resolve_link(con, url: str, lang: str | None = None, num: str | None = None):
    """Ссылка lex.uz (+ номер статьи) → статья из базы на языке lang (по умолчанию — на языке ссылки)."""
    m = LEX_LINK.search(url)
    if not m:
        return None
    link_lang = "uz" if (m.group(1) or "ru").lower() in ("uz", "oz") else "ru"
    doc_id, anchor = int(m.group(2)), m.group(3)
    unit = _unit(con, doc_id, link_lang, num, anchor)
    if unit is None or not lang or lang == link_lang:
        return unit
    other = counterpart(con, doc_id, link_lang)
    n = article_number(unit["heading"]) or num
    return _unit(con, other, lang, n) if other and n else None


def find(con: sqlite3.Connection, query: str, limit: int = 5, lang: str | None = None, script: str = "lat") -> list[dict]:
    query = query or ""
    mark = LANG_MARK.search(query)
    lang = lang or (mark.group(1).lower() if mark else None)
    rest = LANG_MARK.sub(" ", LEX_LINK.sub(" ", query))
    lang = lang or detect_lang(rest)
    nums = [a or b for a, b in _ART_IN_TEXT.findall(query)]
    units, seen = [], set()
    for link in LEX_LINK.finditer(query):
        unit = resolve_link(con, link.group(0), lang, nums[0] if nums else None)
        if unit is not None and unit["id"] not in seen:
            seen.add(unit["id"])
            units.append(unit)
    # статья найдена по ссылке — добавлять случайные совпадения не нужно, они только запутают сценарий
    if not units and rest.strip():
        res = search(con, rest, limit=limit * 2, lang=lang, script=script)
        if res["mode"] != "all":
            # узбекский запрос кириллицей без ў/қ/ғ/ҳ похож на русский — пробуем другой язык
            alt = search(con, rest, limit=limit * 2, lang="ru" if lang == "uz" else "uz", script=script)
            if alt["total"] and (res["total"] == 0 or alt["mode"] == "all"):
                res = alt
        for r in res["results"]:
            if r["rid"] not in seen:
                seen.add(r["rid"])
                units.append(con.execute("SELECT * FROM units WHERE id=?", (r["rid"],)).fetchone())
    out = []
    for u in units:
        p = to_passage(con, u, script)
        if p:
            out.append(p)
        if len(out) >= limit:
            break
    return out
