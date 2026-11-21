"""SQLite-хранилище.

docs       — документы и их реквизиты;
units      — статьи (исходный текст для показа);
units_fts  — полнотекстовый индекс FTS5 по ключам слов (без хранения текста, rowid = units.id).
             Для русского текста ключ — слово с «ё→е», для узбекского — единый латинский ключ
             (см. uzbek.py), поэтому запрос кириллицей, латиницей и новым алфавитом находит одно и то же.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import uzbek
from .listing import ListedDoc
from .parse import ParsedDoc

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER NOT NULL,
    lang        TEXT    NOT NULL,
    title       TEXT,
    category    TEXT,     -- набор, через который документ найден (Кодекс, Закон, …)
    doc_type    TEXT,     -- вид акта из списка («Закон Республики Узбекистан»)
    number      TEXT,
    badge       TEXT,
    status      TEXT,     -- Y действующий / R утратил силу / N не действующий / NULL неизвестно
    adopted     TEXT,
    effective   TEXT,
    edition     TEXT,     -- дата текущей редакции
    editions    TEXT,     -- JSON-список дат всех редакций
    okoz        TEXT,
    tsz         TEXT,
    publication TEXT,
    units       INTEGER,
    chars       INTEGER,
    fetched_at  TEXT,
    indexed_at  TEXT,
    PRIMARY KEY (id, lang)
);
CREATE TABLE IF NOT EXISTS units (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL,
    lang    TEXT    NOT NULL,
    ord     INTEGER NOT NULL,
    anchor  TEXT,
    kind    TEXT,
    heading TEXT,
    path    TEXT,
    body    TEXT,
    notes   TEXT
);
CREATE INDEX IF NOT EXISTS units_doc ON units (doc_id, lang, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    title, heading, path, body, notes,
    content = '', contentless_delete = 1,
    -- надстрочные цифры — разделители: «276⁴» индексируется как «276»;
    -- диакритику не снимаем: в новом алфавите ş≠s, ç≠c, ö≠o, ğ≠g
    tokenize = "unicode61 remove_diacritics 0 separators '⁰¹²³⁴⁵⁶⁷⁸⁹'"
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def reset_index(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS units_fts")
    con.execute("DROP TABLE IF EXISTS units")
    con.executescript(SCHEMA)


def norm(text: str) -> str:
    """Русский текст: ё → е (и в индексе, и в запросе)."""
    return text.replace("ё", "е").replace("Ё", "Е")


def key_text(text: str, lang: str) -> str:
    return uzbek.key_text(text) if lang == "uz" else norm(text)


def upsert_listing(con: sqlite3.Connection, lang: str, item: ListedDoc, category: str | None) -> None:
    con.execute(
        """INSERT INTO docs (id, lang, title, category, doc_type, number, badge, status)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id, lang) DO UPDATE SET
             title=COALESCE(docs.title, excluded.title),
             category=COALESCE(excluded.category, docs.category),
             doc_type=COALESCE(excluded.doc_type, docs.doc_type),
             number=COALESCE(excluded.number, docs.number),
             badge=COALESCE(excluded.badge, docs.badge),
             status=COALESCE(excluded.status, docs.status)""",
        (item.doc_id, lang, item.title, category, item.doc_type, item.number, item.badge, item.status),
    )


def save_parsed(con: sqlite3.Connection, doc: ParsedDoc, fetched: bool, category: str | None = None) -> dict:
    """Записывает документ и пересобирает его статьи в индексе."""
    old = con.execute("SELECT edition FROM docs WHERE id=? AND lang=?", (doc.doc_id, doc.lang)).fetchone()
    chars = sum(len(u.text) for u in doc.units)
    ts = now()
    con.execute(
        """INSERT INTO docs (id, lang, title, category, adopted, effective, edition, editions, okoz, tsz,
                             publication, units, chars, fetched_at, indexed_at, number)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id, lang) DO UPDATE SET
             title=excluded.title, category=COALESCE(excluded.category, docs.category),
             number=COALESCE(docs.number, excluded.number),
             adopted=excluded.adopted, effective=excluded.effective, edition=excluded.edition,
             editions=excluded.editions, okoz=excluded.okoz, tsz=excluded.tsz,
             publication=excluded.publication, units=excluded.units, chars=excluded.chars,
             fetched_at=COALESCE(excluded.fetched_at, docs.fetched_at), indexed_at=excluded.indexed_at""",
        (doc.doc_id, doc.lang, doc.title, category, doc.adopted, doc.effective, doc.edition,
         json.dumps(doc.editions, ensure_ascii=False), doc.okoz, doc.tsz, doc.publication,
         len(doc.units), chars, ts if fetched else None, ts, doc.number),
    )
    old_ids = [r[0] for r in con.execute("SELECT id FROM units WHERE doc_id=? AND lang=?", (doc.doc_id, doc.lang))]
    con.executemany("DELETE FROM units_fts WHERE rowid=?", [(i,) for i in old_ids])
    con.execute("DELETE FROM units WHERE doc_id=? AND lang=?", (doc.doc_id, doc.lang))

    title_key = key_text(doc.title, doc.lang)
    for i, u in enumerate(doc.units):
        notes = "\n".join(u.notes)
        cur = con.execute(
            "INSERT INTO units (doc_id, lang, ord, anchor, kind, heading, path, body, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (doc.doc_id, doc.lang, i, u.anchor, u.kind, u.heading, u.path, u.text, notes),
        )
        con.execute(
            "INSERT INTO units_fts (rowid, title, heading, path, body, notes) VALUES (?,?,?,?,?,?)",
            (cur.lastrowid, title_key, key_text(u.heading, doc.lang), key_text(u.path, doc.lang),
             key_text(u.text, doc.lang), key_text(notes, doc.lang)),
        )
    return {"old_edition": old["edition"] if old else None, "new_edition": doc.edition}


def stats(con: sqlite3.Connection, lang: str | None = None) -> dict:
    where = "indexed_at IS NOT NULL" + (" AND lang = ?" if lang else "")
    args = [lang] if lang else []
    row = con.execute(
        "SELECT COUNT(*) AS docs, COALESCE(SUM(units),0) AS units, COALESCE(SUM(chars),0) AS chars,"
        f" SUM(status='Y') AS active, MAX(indexed_at) AS last_indexed FROM docs WHERE {where}", args
    ).fetchone()
    cats = con.execute(
        f"SELECT COALESCE(category,'—') AS category, COUNT(*) AS n FROM docs WHERE {where}"
        " GROUP BY 1 ORDER BY 2 DESC", args
    ).fetchall()
    langs = con.execute(
        "SELECT lang, COUNT(*) AS n FROM docs WHERE indexed_at IS NOT NULL GROUP BY lang"
    ).fetchall()
    return {**dict(row), "categories": {r["category"]: r["n"] for r in cats},
            "langs": {r["lang"]: r["n"] for r in langs}}
