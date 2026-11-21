"""Поиск по индексу на русском и узбекском (кириллица, латиница, новый алфавит 2026).

Каждое слово запроса превращается в два варианта:
  • русский  — слово с отрезанным окончанием («трудового» -> трудов*),
  • узбекский — единый ключ без окончаний («шартномасини», «shartnomasini», «şartnomasini» -> şartnoma*).
Русские статьи проиндексированы русскими ключами, узбекские — узбекскими, поэтому один запрос
«(русский OR узбекский)» корректно ищет в обоих языках. Подсветка и фрагменты строятся в Python —
так их можно показать в любой записи узбекского текста.
"""

from __future__ import annotations

import html
import re
import sqlite3
import time
from dataclasses import dataclass, field

from . import uzbek
from .store import norm

# --- русская морфология ----------------------------------------------------------------------
_RU_ENDINGS = sorted(
    """
    иями ями ами ией иях ях ах ием ем ом иям ям ам ов ев ей ой ий ый ая яя ое ее ые ие ых их ым им ую юю
    ого его ому ему ыми ими ею ою ия ья ье ью ию ии
    ение ения ению ением ении ений ениям ениями ениях
    ание ания анию анием ании аний
    ость ости остью остей остям остями остях
    ться тся ать ять ить еть уть ет ют ут ит ят ал ял ил ел ла ло ли ны на но ен ан
    а я о е ы и у ю ь й
    """.split(),
    key=len,
    reverse=True,
)
RU_STOPWORDS = set("""
в во на по и или а но с со к ко о об обо от до за из у для при про через под над без не ни же ли бы
""".split())


def ru_stem(word: str) -> str:
    w = norm(word).lower()
    if len(w) <= 3 or not re.search(r"[а-я]", w):
        return w
    min_stem = 3 if len(w) <= 5 else 4   # «лицу» -> лиц*, «детей» -> дет*, «увольнение» -> увольн*
    for end in _RU_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= min_stem:
            return w[: -len(end)]
    return w


# --- сокращения кодексов и номера статей -------------------------------------------------------
RU_CODES = {
    "ук": "Уголовный кодекс%", "упк": "Уголовно-процессуальный кодекс%", "уик": "Уголовно-исполнительный кодекс%",
    "гк": "Гражданский кодекс%", "гпк": "Гражданский процессуальный кодекс%", "тк": "Трудовой кодекс%",
    "нк": "Налоговый кодекс%", "ск": "Семейный кодекс%", "жк": "Жилищный кодекс%", "зк": "Земельный кодекс%",
    "бк": "Бюджетный кодекс%", "вк": "Водный кодекс%", "тамк": "Таможенный кодекс%", "грк": "Градостроительный кодекс%",
    "эпк": "Экономический процессуальный кодекс%",
    "коап": "Кодекс Республики Узбекистан об административной ответственности%",
    "кас": "Кодекс Республики Узбекистан об административном судопроизводстве%",
    **dict.fromkeys(["конституция", "конституции", "конституцию", "конституцией"], "Конституция Республики%"),
}
# узбекские сокращения — по ключу, т.е. «жк», «JK», «jk» одинаковы
UZ_CODES = {
    "jk": "%Жиноят кодекси%", "jpk": "%Жиноят-процессуал кодекси%", "jik": "%Жиноят-ижроия кодекси%",
    "fk": "%Фуқаролик кодекси%", "fpk": "%Фуқаролик процессуал кодекси%", "mk": "%Меҳнат кодекси%",
    "sk": "%Солиқ кодекси%", "ok": "%Оила кодекси%", "ujk": "%Уй-жой кодекси%", "yk": "%Ер кодекси%",
    "bk": "%Божхона кодекси%", "ipk": "%Иқтисодий процессуал кодекси%", "hk": "%Ҳаво кодекси%",
    "mjtk": "%Маъмурий жавобгарлик тўғрисидаги кодекс%", "mjk": "%Маъмурий жавобгарлик тўғрисидаги кодекс%",
}
UZ_CONSTITUTION = "%Республикаси Конституцияси%"
RU_ARTICLE_WORDS = {"ст", "статья", "статьи", "статье", "статью", "статьей"}
UZ_ARTICLE_WORDS = {"m", "modda", "moddasi", "moddaga", "moddada", "moddaning", "moddani"}

_PHRASE = re.compile(r'"([^"]+)"')
_NUM = re.compile(r"^\d+$")


@dataclass
class Query:
    """Разобранный запрос: FTS-термины, фильтры по кодексам, номера статей, данные для подсветки."""
    terms: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    nums: list[str] = field(default_factory=list)
    hl: dict = field(default_factory=lambda: {"ru": (set(), set()), "uz": (set(), set())})  # lang -> (prefixes, exact)

    def add_hl(self, lang: str, value: str, prefix: bool) -> None:
        (self.hl[lang][0] if prefix else self.hl[lang][1]).add(value)


def _q(s: str) -> str:
    return '"' + s.replace('"', "") + '"'


def parse_query(query: str, lang: str | None = None) -> Query:
    q = Query()
    use_ru, use_uz = lang in (None, "", "ru"), lang in (None, "", "uz")

    # 1) фразы в кавычках — точная последовательность слов
    for phrase in _PHRASE.findall(query):
        words = uzbek.WORD.findall(phrase)
        if not words:
            continue
        alts = []
        if use_ru:
            alts.append(_q(" ".join(norm(w).lower() for w in words)))
        if use_uz:
            alts.append(_q(" ".join(uzbek.to_key(w) for w in words)))
        q.terms.append("(" + " OR ".join(alts) + ")")
        for w in words:
            q.add_hl("ru", norm(w).lower(), False)
            q.add_hl("uz", uzbek.to_key(w), False)

    # 2) отдельные слова
    words = uzbek.WORD.findall(_PHRASE.sub(" ", query))
    q.nums = [w for w in words if _NUM.match(w)][:3]
    content = []
    for w in words:
        low, key = norm(w).lower(), uzbek.to_key(w)
        code = []
        if use_ru and low in RU_CODES:
            code.append(RU_CODES[low])
        if use_uz and key in UZ_CODES:
            code.append(UZ_CODES[key])
        if use_uz and key.startswith("konstitutsiya") and not key.startswith("konstitutsiyav"):
            code.append(UZ_CONSTITUTION)
        if code:
            q.codes += code
            continue
        if q.nums and (low in RU_ARTICLE_WORDS or key in UZ_ARTICLE_WORDS):
            continue                       # «ст 169 ук», «169-modda» — слово «статья» выражено номером
        content.append(w)
    if not content and not q.terms and not q.nums:
        content, q.codes = words, []       # запрос только «ук» или «конституция» — ищем как слово
    meaningful = [w for w in content if norm(w).lower() not in RU_STOPWORDS
                  and uzbek.to_key(w) not in uzbek.STOPWORDS]
    for w in meaningful or content:
        if _NUM.match(w):
            q.terms.append(_q(w))          # номера ищем точно: 15 ≠ 150
            q.add_hl("ru", w, False)
            q.add_hl("uz", w, False)
            continue
        alts = []
        ru = ru_stem(w)
        uz = uzbek.stem(uzbek.to_key(w))
        if use_ru and re.search(r"[а-яё]", w.lower()):
            alts.append(_q(ru) + ("*" if len(ru) >= 3 else ""))
            q.add_hl("ru", ru, len(ru) >= 3)
        if use_uz and uz:
            alts.append(_q(uz) + ("*" if len(uz) >= 3 else ""))
            q.add_hl("uz", uz, len(uz) >= 3)
        if alts:
            q.terms.append(alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")")
    return q


def _all(terms: list[str]) -> str:
    return " AND ".join(terms)


def _most(terms: list[str]) -> str | None:
    """«Все слова, кроме одного» — мягче AND, точнее OR."""
    if len(terms) < 3:
        return None
    return " OR ".join("(" + _all([t for j, t in enumerate(terms) if j != i]) + ")" for i in range(len(terms)))


# --- подсветка и фрагменты -----------------------------------------------------------------------
_SUP = dict.fromkeys(map(ord, "⁰¹²³⁴⁵⁶⁷⁸⁹"), None)


def _is_hit(word: str, lang: str, q: Query) -> bool:
    prefixes, exact = q.hl["uz" if lang == "uz" else "ru"]
    k = uzbek.to_key(word) if lang == "uz" else norm(word).lower().translate(_SUP)
    return k in exact or any(k.startswith(p) for p in prefixes)


def mark(text: str, lang: str, q: Query) -> str:
    """Экранирует текст и оборачивает найденные слова в <mark>."""
    out, pos = [], 0
    for m in uzbek.WORD.finditer(text or ""):
        if _is_hit(m.group(0), lang, q):
            out.append(html.escape(text[pos:m.start()]))
            out.append("<mark>" + html.escape(m.group(0)) + "</mark>")
            pos = m.end()
    out.append(html.escape((text or "")[pos:]))
    return "".join(out)


def snippet(text: str, lang: str, q: Query, width: int = 300) -> str:
    """Кусок текста вокруг самого плотного скопления найденных слов."""
    text = text or ""
    hits = [m.start() for m in uzbek.WORD.finditer(text) if _is_hit(m.group(0), lang, q)]
    if len(text) <= width:
        return mark(text, lang, q)
    if hits:
        best = max(hits, key=lambda s: sum(1 for h in hits if s - 40 <= h <= s + width - 40))
        start = max(0, best - 60)
    else:
        start = 0
    if start:
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 30 else start
    end = min(len(text), start + width)
    if end < len(text):
        sp = text.rfind(" ", start, end)
        end = sp if sp > start + width // 2 else end
    return ("… " if start else "") + mark(text[start:end], lang, q) + (" …" if end < len(text) else "")


def show(text: str | None, lang: str, script: str) -> str:
    """Текст для показа: узбекский — в выбранной записи (cyr / lat / new)."""
    return uzbek.translit(text or "", script) if lang == "uz" else (text or "")


# --- поиск ---------------------------------------------------------------------------------------
# веса bm25 по колонкам: title, heading, path, body, notes
_BM25 = "bm25(units_fts, 3.0, 6.0, 1.5, 1.0, 0.2)"
_FROM = ("FROM units_fts JOIN units u ON u.id = units_fts.rowid"
         " JOIN docs d ON d.id = u.doc_id AND d.lang = u.lang")


def search(con: sqlite3.Connection, query: str, *, limit: int = 20, offset: int = 0,
           only_active: bool = True, category: str | None = None, lang: str | None = None,
           script: str = "lat") -> dict:
    t0 = time.perf_counter()
    q = parse_query(query, lang)
    if not q.terms:
        return {"query": query, "total": 0, "results": [], "ms": 0.0, "mode": "empty"}

    where, params = ["units_fts MATCH ?"], []
    if q.codes:
        where.append("(" + " OR ".join(["d.title LIKE ?"] * len(q.codes)) + ")")
        params += q.codes
    if only_active:
        where.append("(d.status IS NULL OR d.status = 'Y')")
    if category:
        where.append("d.category = ?")
        params.append(category)
    if lang:
        where.append("u.lang = ?")
        params.append(lang)
    sql_where = " AND ".join(where)

    # «статья 276 …» / «276-модда» — сама статья выше, чем 276¹, 276² и упоминания в тексте
    order, order_params = "score", []
    if q.nums:
        order = "CASE WHEN " + " OR ".join(["u.heading LIKE ?"] * (2 * len(q.nums))) + " THEN 0 ELSE 1 END, score"
        for n in q.nums:
            order_params += [f"Статья {n}.%", f"{n}-модда%"]

    def run(match: str):
        total = con.execute(f"SELECT COUNT(*) {_FROM} WHERE {sql_where}", [match, *params]).fetchone()[0]
        rows = con.execute(
            f"""SELECT u.id AS rid, u.doc_id, u.lang, u.anchor, u.kind, u.ord, u.heading, u.path, u.body,
                       d.title, d.status, d.category, d.edition, d.number, d.adopted, {_BM25} AS score
                {_FROM} WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
            [match, *params, *order_params, limit, offset]).fetchall()
        return total, rows

    mode, match = "all", _all(q.terms)
    total, rows = run(match)
    if total == 0 and (relaxed := _most(q.terms)):
        mode, match = "most", relaxed      # нет статьи со всеми словами — все, кроме одного
        total, rows = run(match)
    if total == 0 and len(q.terms) > 1:
        mode, match = "any", " OR ".join(q.terms)
        total, rows = run(match)

    results = []
    for r in rows:
        lg = r["lang"]
        results.append({
            "rid": r["rid"], "doc_id": r["doc_id"], "lang": lg, "anchor": r["anchor"], "kind": r["kind"],
            "title": show(r["title"], lg, script),
            "heading": mark(show(r["heading"], lg, script), lg, q),
            "path": show(r["path"], lg, script),
            # фрагмент вырезаем из исходного текста и транслитерируем только его, а не всю статью
            "snippet": show(snippet(r["body"], lg, q), lg, script),
            "status": r["status"], "category": r["category"], "edition": r["edition"],
            "number": show(r["number"], lg, script), "adopted": r["adopted"],
            "url": f"https://lex.uz/{lg}/docs/{r['doc_id']}#{r['anchor']}",
        })
    return {"query": query, "match": match, "mode": mode, "total": total, "results": results,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def get_unit(con: sqlite3.Connection, rid: int, query: str | None = None, script: str = "lat") -> dict | None:
    """Полный текст одной статьи с подсветкой и ссылками на соседние статьи документа."""
    r = con.execute("SELECT * FROM units WHERE id = ?", (rid,)).fetchone()
    if not r:
        return None
    lg = r["lang"]
    q = parse_query(query or "", lg)
    d = con.execute("SELECT title, status, edition, adopted, effective, number FROM docs WHERE id=? AND lang=?",
                    (r["doc_id"], lg)).fetchone()
    nb = {n["ord"]: n["id"] for n in con.execute(
        "SELECT id, ord FROM units WHERE doc_id=? AND lang=? AND ord IN (?, ?)",
        (r["doc_id"], lg, r["ord"] - 1, r["ord"] + 1))}
    out = {
        "rid": rid, "doc_id": r["doc_id"], "lang": lg, "anchor": r["anchor"], "kind": r["kind"],
        "heading": mark(show(r["heading"], lg, script), lg, q),
        "path": show(r["path"], lg, script),
        "paragraphs": [mark(p, lg, q) for p in show(r["body"], lg, script).split("\n")],
        "notes": html.escape(show(r["notes"], lg, script)),
        "prev": nb.get(r["ord"] - 1), "next": nb.get(r["ord"] + 1),
        "url": f"https://lex.uz/{lg}/docs/{r['doc_id']}#{r['anchor']}",
    }
    if d:
        out.update({k: show(d[k], lg, script) if k in ("title", "number") else d[k] for k in d.keys()})
    return out
