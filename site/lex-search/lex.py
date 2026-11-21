"""lex.uz → локальная база + быстрый поиск по статьям.

Примеры:
  python lex.py crawl constitution codes     # Конституция и действующие кодексы
  python lex.py crawl laws                   # действующие законы (~1600, около часа)
  python lex.py crawl --ids 6257291 111457   # отдельные документы по номеру
  python lex.py search "испытательный срок при приеме на работу"
  python lex.py serve                        # веб-интерфейс на http://localhost:8000
  python lex.py stats
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from lexuz import listing as lst
from lexuz import store
from lexuz.fetch import PoliteClient, read_cached
from lexuz.parse import parse_document
from lexuz.search import search

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "lex.db"
RAW_DIR = DATA / "raw"

# язык текста -> (префикс интерфейса в URL, код языка в поиске lex.uz)
# Узбекские тексты на lex.uz хранятся кириллицей (код 3); латиница (код 4) почти пуста —
# латинскую и новую (2026) запись мы получаем транслитерацией.
LANGS = {"ru": ("ru", 1), "uz": ("uz", 3)}


def _preset_url(name: str, lang: str) -> tuple[str, str]:
    url, category = lst.PRESETS[name]
    ui, code = LANGS[lang]
    return url.replace("/ru/", f"/{ui}/").replace("lang=1", f"lang={code}"), category


def _fmt_eta(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}ч {m:02d}м" if h else f"{m}м {s:02d}с"


def index_one(con, client: PoliteClient, lang: str, doc_id: int, category: str | None, force: bool) -> str:
    html, cached = client.get_document(LANGS[lang][0], doc_id, force=force)
    doc = parse_document(html, doc_id, lang)
    store.save_parsed(con, doc, fetched=not cached, category=category)
    con.commit()
    src = "кэш" if cached else "сеть"
    return f"{doc.title[:70]} — {len(doc.units)} статей/фрагментов, ред. {doc.edition or '—'} ({src})"


_AMENDING_RU = ("о внесении изменени", "о внесении дополнени", "о признании утратившим", "о признании утратившими")
# в узбекских названиях суть в конце: «…га ўзгартиш ва қўшимчалар киритиш тўғрисида»
_AMENDING_UZ = ("киритиш тўғрисида", "киритиш ҳақида", "ўз кучини йўқотган деб топиш",
                "kiritish toʻgʻrisida", "kiritish to‘g‘risida", "kiritish haqida")
_KARAKALPAK = set("әөүңыӘӨҮҢ")


def _wanted(item: lst.ListedDoc, args) -> bool:
    if item.status in ("R", "N") and not args.include_repealed:
        return False
    title = item.title.lower()
    # изменяющие акты уже «влиты» в действующие редакции кодексов и законов — для поиска это шум
    if not args.include_amending and (title.startswith(_AMENDING_RU) or any(p in title for p in _AMENDING_UZ)):
        return False
    if args.lang == "uz" and _KARAKALPAK & set(item.title):
        return False  # тексты на каракалпакском языке — отдельный язык, в прототип не берём
    return True


def cmd_crawl(args) -> None:
    con = store.connect(DB_PATH)
    client = PoliteClient(RAW_DIR, delay=args.delay)
    jobs: list[tuple[int, str | None]] = []
    try:
        sources = []
        for name in args.presets:
            if name not in lst.PRESETS:
                sys.exit(f"Неизвестный набор «{name}». Есть: {', '.join(lst.PRESETS)}")
            url, category = _preset_url(name, args.lang)
            sources.append((name, url, category))
        if args.listing:
            sources.append(("свой список", args.listing, args.category))
        for name, url, category in sources:
            print(f"Список «{name}»: https://lex.uz{url}" if url.startswith("/") else f"Список: {url}")
            skipped = 0
            for item in lst.iter_listing(client, url, max_pages=args.max_pages):
                store.upsert_listing(con, args.lang, item, category)
                if _wanted(item, args):
                    jobs.append((item.doc_id, category))
                else:
                    skipped += 1
            con.commit()
            print(f"  пропущено (утратившие силу / изменяющие акты): {skipped}")
        jobs += [(i, args.category) for i in args.ids]
        if args.limit:
            jobs = jobs[: args.limit]

        seen, ordered = set(), []
        for j in jobs:
            if j[0] not in seen:
                seen.add(j[0])
                ordered.append(j)
        print(f"Документов к загрузке: {len(ordered)} (пауза между запросами {args.delay} с)")
        t0, done, failed = time.monotonic(), 0, []
        for n, (doc_id, category) in enumerate(ordered, 1):
            try:
                info = index_one(con, client, args.lang, doc_id, category, args.force)
                done += 1
            except Exception as e:  # один битый документ не должен останавливать обход
                failed.append(doc_id)
                info = f"ОШИБКА: {e!r}"
            elapsed = time.monotonic() - t0
            eta = elapsed / n * (len(ordered) - n)
            print(f"[{n}/{len(ordered)}] {doc_id}: {info} | осталось ~{_fmt_eta(eta)}", flush=True)
        print(f"Готово: {done} документов, ошибок: {len(failed)} {failed[:20]}, HTTP-запросов: {client.requests_made}")
    finally:
        client.close()
        con.close()


def cmd_reindex(args) -> None:
    """Пересобирает индекс из сохранённого HTML без обращения к сайту (после правок парсера)."""
    con = store.connect(DB_PATH)
    store.reset_index(con)  # схема индекса могла поменяться — пересоздаём таблицу целиком
    rows = con.execute("SELECT id, lang, category FROM docs WHERE indexed_at IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        html = read_cached(RAW_DIR, LANGS[r["lang"]][0], r["id"])
        if html is None:
            continue
        store.save_parsed(con, parse_document(html, r["id"], r["lang"]), fetched=False, category=r["category"])
        n += 1
    con.commit()
    con.execute("INSERT INTO units_fts(units_fts) VALUES('optimize')")
    con.commit()
    print(f"Переиндексировано документов: {n}")


def cmd_search(args) -> None:
    import html
    con = store.connect(DB_PATH)
    res = search(con, " ".join(args.query), limit=args.limit, only_active=not args.all, category=args.category,
                 lang=args.lang, script=args.script)
    strip = lambda s: html.unescape((s or "").replace("<mark>", "[").replace("</mark>", "]"))
    note = {"most": " (нет статей со всеми словами — показаны совпадения по всем, кроме одного)",
            "any": " (нет статей со всеми словами — показаны частичные совпадения)"}.get(res["mode"], "")
    print(f"Найдено: {res['total']} за {res['ms']} мс{note}\n")
    for i, r in enumerate(res["results"], 1):
        print(f"{i}. [{r['lang']}] {r['title']} — {strip(r['heading'])}")
        if r["path"]:
            print(f"   {r['path']}")
        print(f"   {strip(r['snippet'])[:400]}")
        print(f"   {r['url']}\n")


def cmd_stats(args) -> None:
    con = store.connect(DB_PATH)
    s = store.stats(con)
    size = DB_PATH.stat().st_size / 2**20 if DB_PATH.exists() else 0
    raw = sum(p.stat().st_size for p in RAW_DIR.rglob("*.gz")) / 2**20 if RAW_DIR.exists() else 0
    print(f"Документов в индексе: {s['docs']} (действующих: {s['active'] or 0}), по языкам: {s['langs']}")
    print(f"Статей/фрагментов: {s['units']}, символов текста: {s['chars']:,}".replace(",", " "))
    print(f"По наборам: {s['categories']}")
    print(f"База: {size:.1f} МБ, сырой HTML (gzip): {raw:.1f} МБ, последнее обновление: {s['last_indexed']}")


def cmd_refute(args) -> None:
    """Опровержения: находки Modda Monitor → ролики MODDA → пакеты в data/eksport (запускается таймером на сервере)."""
    from lexuz import refute
    if args.retry:
        for line in refute.retry(DATA, args.retry):
            print(line)
        return
    con = store.connect(DB_PATH)
    for line in refute.run(con, DATA, dry_run=args.dry_run) or ["без изменений"]:
        print(line)


def cmd_eksport_password(args) -> None:
    from lexuz import eksport
    password = eksport.set_password(DATA / "eksport.auth")
    out = Path(args.out)
    out.write_text(f"Raddiyalar / Опровержения: https://nasa.cartify.ink/qidiruv/eksport/\nlogin: modda\nparol: {password}\n",
                   encoding="utf-8")
    out.chmod(0o600)
    print(f"Пароль записан в {out}")


def cmd_serve(args) -> None:
    from lexuz.web import serve
    serve(Path(args.db) if args.db else DB_PATH, host=args.host, port=args.port)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Локальная база и поиск по lex.uz")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="скачать и проиндексировать документы")
    c.add_argument("presets", nargs="*", help=f"готовые наборы: {', '.join(lst.PRESETS)}")
    c.add_argument("--listing", help="URL страницы поиска lex.uz (свой фильтр)")
    c.add_argument("--ids", nargs="*", type=int, default=[], help="номера документов (из URL /docs/<id>)")
    c.add_argument("--category", help="подпись набора для --listing/--ids")
    c.add_argument("--lang", choices=LANGS, default="ru", help="язык текста: ru — русский, uz — узбекский")
    c.add_argument("--max-pages", type=int, help="ограничить число страниц списка")
    c.add_argument("--limit", type=int, help="ограничить число документов")
    c.add_argument("--include-repealed", action="store_true", help="качать и утратившие силу")
    c.add_argument("--include-amending", action="store_true", help="качать и акты «О внесении изменений…»")
    c.add_argument("--force", action="store_true", help="перекачать, даже если есть в кэше")
    c.add_argument("--delay", type=float, default=1.0, help="пауза между запросами, сек (по умолчанию 1)")
    c.set_defaults(func=cmd_crawl)

    r = sub.add_parser("reindex", help="пересобрать индекс из сохранённого HTML")
    r.set_defaults(func=cmd_reindex)

    s = sub.add_parser("search", help="поиск из командной строки")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--all", action="store_true", help="включая утратившие силу")
    s.add_argument("--category")
    s.add_argument("--lang", choices=LANGS, help="только документы на этом языке")
    s.add_argument("--script", choices=["cyr", "lat", "new"], default="lat",
                   help="запись узбекского текста: кириллица, латиница, новый алфавит 2026")
    s.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="что лежит в базе")
    st.set_defaults(func=cmd_stats)

    rf = sub.add_parser("refute", help="опровержения: находки Modda Monitor → ролики MODDA → папка выгрузки")
    rf.add_argument("--dry-run", action="store_true", help="показать задания, ничего не создавая")
    rf.add_argument("--retry", metavar="ПРАВИЛО[:ЯЗЫК]",
                    help="перезапустить дело после «внимания» или ошибки (сначала проверьте, что клипов во Flow не было)")
    rf.set_defaults(func=cmd_refute)

    ep = sub.add_parser("eksport-password", help="задать новый пароль страницы выгрузки")
    ep.add_argument("--out", required=True, help="куда записать пароль (файл с доступом только владельцу)")
    ep.set_defaults(func=cmd_eksport_password)

    w = sub.add_parser("serve", help="веб-интерфейс поиска")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--db", help="другой файл базы (по умолчанию data/lex.db)")
    w.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
