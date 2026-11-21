"""Ролики о законах из нашей базы lex.uz: форма заказа (под паролем выгрузки) и заказы для моста.

Веб-сервис (пользователь lexsearch) только проверяет заказ и кладёт его файлом в data/rolik-orders/.
Задание в MODDA создаёт мост lex-refute (root, ключ владельца студии) — он же собирает готовый пакет
в папке выгрузки, как для опровержений. Каждая сцена — платная генерация во Flow, поэтому форма закрыта паролем.
"""

from __future__ import annotations

import html
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from . import passages, uzbek

# формат MODDA → (узбекское название, русское название, что попросить у сценариста)
FORMATS = {
    "explainer": ("Qonun oddiy tilda", "Закон простыми словами",
                  "Объясни простыми словами, что устанавливает эта статья: кого она касается, что можно и чего нельзя, "
                  "какие сроки и последствия."),
    "quick_rule": ("15 soniyada qoida", "Правило за 15 секунд",
                   "Одно главное правило из этой статьи — коротко и так, чтобы запомнилось."),
    "myth_fact": ("Afsona yoki haqiqat", "Миф или факт",
                  "Частое заблуждение об этой норме и что на самом деле говорит закон."),
    "dialog_scene": ("Qahramonlar suhbati", "Диалог героев",
                     "Сценка: один герой попадает в ситуацию, другой объясняет, что говорит эта статья."),
    "quiz": ("Viktorina", "Викторина",
             "Вопрос зрителю по этой статье, варианты ответа и правильный ответ со ссылкой на норму."),
}
# стиль MODDA → (возраст зрителей, подпись uz, подпись ru). До 25 лет — человечки и мультфильмы, 25+ — инфографика.
STYLES = {
    "stickman": ("u25", "25 yoshgacha · chizma odamchalar", "До 25 лет · рисованные человечки"),
    "cartoon3d": ("u25", "25 yoshgacha · 3D multfilm", "До 25 лет · 3D-мультфильм"),
    "doc_infographic": ("25p", "25 yosh va katta · infografika", "25 лет и старше · инфографика"),
}
AUDIENCE = {"u25": "Зрители: до 25 лет — живо и просто, без канцелярита.",
            "25p": "Зрители: 25 лет и старше — спокойно и точно, с цифрами и сроками из статьи."}
MAX_SCENES = 7
DURATIONS = {3: 15, 5: 30, 7: 45}  # Flow scenes -> finished video seconds (MODDA studio)


def orders_dir(data_dir: Path) -> Path:
    return data_dir / "rolik-orders"


def _e(s) -> str:
    return html.escape(str(s or ""))


def _unit_info(con, rid: int):
    """Статья по id из поиска → (статья, документ) или None."""
    unit = con.execute("SELECT * FROM units WHERE id=?", (rid,)).fetchone()
    if unit is None:
        return None
    doc = con.execute("SELECT title, edition, adopted FROM docs WHERE id=? AND lang=?", (unit["doc_id"], unit["lang"])).fetchone()
    return unit, doc


def law_for_unit(con, doc_id: int, lang: str, anchor: str) -> dict | None:
    """Статья на обоих языках в формате MODDA: {uz: {...}, ru: {...}, label, monitor_url}."""
    unit = passages._unit(con, doc_id, lang, None, anchor)
    if unit is None:
        return None
    url = f"https://lex.uz/{lang}/docs/{doc_id}#{anchor}"
    other = "ru" if lang == "uz" else "uz"
    other_unit = passages.resolve_link(con, url, other)
    law = {lang: passages.to_passage(con, unit), other: passages.to_passage(con, other_unit) if other_unit else None}
    main = law[lang]
    if main is None:
        return None
    law.update(label=f"{main['article']} — {main['title']}", monitor_url=url)
    return law


def prompt_for(case: dict) -> str:
    p = case["law"][case["lang"]]
    head = FORMATS[case["format"]][2]
    tail = f"\nНорма: {p['article']}\nДокумент: {p['title'][:300]}\nИсточник: {p['url']} lang:{case['lang']}"
    return (head + "\n" + AUDIENCE[STYLES[case["style"]][0]] + tail)[:1500]


def form_page(con, rid: int, error: str = "") -> tuple[int, bytes]:
    info = _unit_info(con, rid)
    if info is None:
        return 404, _page("Modda · ролик", '<p class="err">Modda topilmadi · Статья не найдена.</p>')
    unit, doc = info
    law = law_for_unit(con, unit["doc_id"], unit["lang"], unit["anchor"])
    if law is None:
        return 404, _page("Modda · ролик", '<p class="err">Bu modda uchun matn yoʻq · У этой статьи нет текста.</p>')
    main = law[unit["lang"]]
    langs = "".join(
        f'<label class="opt{" off" if not law.get(lg) else ""}"><input type="radio" name="lang" value="{lg}"'
        f'{" checked" if lg == ("uz" if law.get("uz") else "ru") else ""}{" disabled" if not law.get(lg) else ""}>'
        f'<b>{name}</b>{"" if law.get(lg) else " <small>bazada yoʻq · нет в базе</small>"}</label>'
        for lg, name in (("uz", "Oʻzbekcha · Узбекский"), ("ru", "Ruscha · Русский")))
    formats = "".join(f'<label class="opt"><input type="radio" name="format" value="{k}"{" checked" if k == "explainer" else ""}>'
                      f'<b>{uz}</b> · {ru}</label>' for k, (uz, ru, _) in FORMATS.items())
    styles = "".join(f'<label class="opt"><input type="radio" name="style" value="{k}"{" checked" if k == "stickman" else ""}>'
                     f'<b>{uz}</b> · {ru}</label>' for k, (_, uz, ru) in STYLES.items())
    scenes = "".join(f'<option value="{n}"{" selected" if n == 5 else ""}>{s} soniya · {s} секунд ({n} sahna · сцен)</option>'
                     for n, s in DURATIONS.items())
    text = main["text"]
    body = f"""
<h1>Video tayyorlash · Снять ролик</h1>
{f'<p class="err">{_e(error)}</p>' if error else ''}
<section class="art">
  <p class="doc">{_e(main['title'])}</p>
  <h2>{_e(main['article'])}</h2>
  <p class="text">{_e(text[:700])}{'…' if len(text) > 700 else ''}</p>
  <p class="links">{' · '.join(f'<a href="{_e(x["url"])}" target="_blank" rel="noopener">lex.uz {lg}</a>' for lg in ("uz", "ru") if (x := law.get(lg)))}
    · tahrir · редакция {_e(main['as_of'])}</p>
</section>
<form method="post" action="rolik">
  <input type="hidden" name="rid" value="{rid}">
  <fieldset><legend>Format · Формат</legend>{formats}</fieldset>
  <fieldset><legend>Tomoshabinlar va uslub · Зрители и стиль</legend>{styles}</fieldset>
  <fieldset><legend>Video tili · Язык ролика</legend>{langs}</fieldset>
  <fieldset><legend>Davomiyligi · Длительность</legend>
    <label class="opt"><select name="scenes">{scenes}</select>
    <span>Ovoz, subtitr va musiqa bilan tayyor video. Har bir sahna — pullik generatsiya · Готовый ролик с озвучкой, субтитрами и музыкой. Каждая сцена — платная генерация</span></label>
  </fieldset>
  <button type="submit">Navbatga qoʻyish · Поставить в очередь</button>
  <p class="note">Videoni noutbuk tayyorlaydi; tayyor paket <a href="./">yuklab olish sahifasida</a> paydo boʻladi.
  Eʼlon qilishdan oldin yurist tekshirsin.<br>
  Ролик снимет ноутбук; готовый пакет появится на <a href="./">странице выгрузки</a>. Перед публикацией нужна проверка юриста.</p>
</form>"""
    return 200, _page("Modda · снять ролик", body)


def save_order(con, data_dir: Path, raw: bytes) -> tuple[bool, str, int | None]:
    """Проверяет заказ из формы и кладёт его в data/rolik-orders. → (успех, сообщение, rid)."""
    f = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
    try:
        rid = int(f.get("rid", ""))
        scenes = int(f.get("scenes", ""))
    except ValueError:
        return False, "Проверьте поля формы.", None
    fmt, style, lang = f.get("format"), f.get("style"), f.get("lang")
    if fmt not in FORMATS or style not in STYLES or lang not in ("uz", "ru") or scenes not in DURATIONS:
        return False, "Проверьте поля формы.", rid
    info = _unit_info(con, rid)
    if info is None:
        return False, "Статья не найдена.", rid
    unit = info[0]
    law = law_for_unit(con, unit["doc_id"], unit["lang"], unit["anchor"])
    if law is None or not law.get(lang):
        return False, "Этой статьи нет в базе на выбранном языке.", rid
    order = {"id": uuid.uuid4().hex, "rid": rid, "doc_id": unit["doc_id"], "doc_lang": unit["lang"], "anchor": unit["anchor"],
             "format": fmt, "style": style, "lang": lang, "scenes": scenes, "ordered_at": int(time.time())}
    d = orders_dir(data_dir)
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o755)
    tmp = d / f".{order['id']}.tmp"
    tmp.write_text(json.dumps(order, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(d / f"{order['ordered_at']}-{order['id']}.json")
    return True, "", rid


def rule_id(order: dict, number: str | None) -> str:
    """Имя дела и пакета: qonun-<документ>-<статья>-<формат>-<возраст>, только [a-z0-9-]."""
    art = number or order["anchor"]
    return f"qonun-{order['doc_id']}-{art}-{order['format'].replace('_', '-')}-{STYLES[order['style']][0]}"


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(title)}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--muted:#5f6673;--line:#e3e6eb;--accent:#1f6feb;--bad:#c62828;--ok:#2e7d32}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1115;--card:#171a21;--fg:#e8eaee;--muted:#9aa3b2;--line:#2a2f3a;--accent:#6aa5ff;--bad:#ef5350;--ok:#66bb6a}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 60px}} h1{{font-size:24px;margin:0 0 16px}} h2{{font-size:18px;margin:4px 0 8px}}
a{{color:var(--accent)}} .art{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:18px}}
.doc,.links{{color:var(--muted);margin:0}} .text{{border-left:3px solid var(--ok);padding-left:10px;white-space:pre-line}}
fieldset{{border:1px solid var(--line);border-radius:12px;background:var(--card);margin:0 0 14px;padding:10px 14px 12px}}
legend{{font-weight:600;padding:0 6px}} .opt{{display:flex;gap:8px;align-items:baseline;padding:5px 0}} .opt.off{{color:var(--muted)}}
select{{font:inherit;padding:4px 8px}} button{{font:inherit;font-weight:600;background:var(--accent);color:#fff;border:0;border-radius:10px;padding:10px 18px;cursor:pointer}}
button:focus-visible,input:focus-visible,select:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.note{{color:var(--muted);font-size:14px}} .err{{color:var(--bad);font-weight:600}}
</style></head><body><main>{body}</main></body></html>""".encode("utf-8")
