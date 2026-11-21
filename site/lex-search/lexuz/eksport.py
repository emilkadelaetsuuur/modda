"""Папка выгрузки опровержений: страница со списком пакетов, файлы и zip. Доступ по паролю (HTTP Basic)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from pathlib import Path

from . import rolik
from .refute import FILES, NAME

ITERATIONS = 60000
TYPES = {".mp4": "video/mp4", ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".zip": "application/zip"}
STATUS = {"queued": ("Navbatda · В очереди", "wait"), "attention": ("Eʼtibor kerak · Нужно внимание", "warn"),
          "error": ("Xato · Ошибка", "warn"), "waiting": ("Kutilmoqda · Ждёт лимита", "wait"),
          "new": ("Yangi · Новое", "wait"), "exported": ("Tayyor · Готово", "ok")}


def set_password(auth_file: Path, user: str = "modda") -> str:
    password = secrets.token_urlsafe(12)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS).hex()
    auth_file.write_text(json.dumps({"user": user, "salt": salt.hex(), "iter": ITERATIONS, "hash": digest}), encoding="utf-8")
    auth_file.chmod(0o600)
    return password


class Guard:
    """Проверка пароля; удачные заголовки кэшируются, чтобы видео с перемоткой не пересчитывало хэш на каждый кусок."""

    def __init__(self, auth_file: Path):
        self.file, self.ok, self.mtime = auth_file, set(), None

    def check(self, header: str) -> bool | None:
        try:
            st = self.file.stat()
            conf = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # пароль не задан — выгрузка выключена
        if st.st_mtime != self.mtime:
            self.ok, self.mtime = set(), st.st_mtime
        if header in self.ok:
            return True
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeError):
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(conf["salt"]), conf["iter"]).hex()
        good = hmac.compare_digest(user.encode(), conf["user"].encode()) & hmac.compare_digest(calc, conf["hash"])
        if good:
            self.ok.add(header)
        return good


def resolve(export_dir: Path, rel: str) -> Path | None:
    """eksport/<пакет>/<файл> или eksport/<пакет>.zip → путь к файлу; всё прочее — None."""
    m = re.fullmatch(r"([^/]+)\.zip", rel)
    if m and NAME.match(m.group(1)):
        p = export_dir / rel
        return p if p.is_file() else None
    m = re.fullmatch(r"([^/]+)/([^/]+)", rel)
    if m and NAME.match(m.group(1)) and m.group(2) in FILES:
        p = export_dir / m.group(1) / m.group(2)
        return p if p.is_file() else None
    return None


def content_type(path: Path) -> str:
    return TYPES.get(path.suffix, "application/octet-stream")


def _e(s) -> str:
    return html.escape(str(s or ""))


def page(export_dir: Path) -> bytes:
    packages = []
    for case_file in sorted(export_dir.glob("*/case.json"), reverse=True):
        if NAME.match(case_file.parent.name):
            try:
                packages.append(json.loads(case_file.read_text(encoding="utf-8")))
            except ValueError:
                pass
    try:
        status = json.loads((export_dir / "_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = []
    pending = [s for s in status if s.get("status") != "exported"]

    cards = []
    for p in packages:
        n, law = p["name"], p.get("law", {})
        main = law.get(p["lang"]) or law.get("uz") or law.get("ru") or {}
        links = " · ".join(f'<a href="{_e(x["url"])}" target="_blank" rel="noopener">lex.uz {lg}</a>'
                           for lg in ("uz", "ru") if (x := law.get(lg)))
        files = " ".join(f'<a class="chip" href="{n}/{f}" download>{f}</a>' for f in FILES if (export_dir / n / f).exists())
        if p.get("kind") == "law":
            fmt = rolik.FORMATS.get(p.get("format"), ("", "", ""))
            aud = rolik.STYLES.get(p.get("style"), ("", "", ""))
            cards.append(f"""
<article class="card">
  <video controls preload="metadata" src="{n}/video.mp4"></video>
  <div class="body">
    <div class="tags"><span class="badge good">QONUN · ЗАКОН</span><span class="badge">{_e(fmt[0])} · {_e(fmt[1])}</span>
      <span class="badge">{_e(aud[2])}</span><span class="badge">{_e(p['lang'])}</span></div>
    <p class="claim">{_e(main.get('article'))}</p>
    <p class="where">{_e(main.get('title'))}</p>
    <p class="law">{_e(main.get('text', '')[:420])}{'…' if len(main.get('text', '')) > 420 else ''}</p>
    <p class="links">{links}</p>
    <p class="files"><a class="chip main" href="{n}.zip" download>⬇ {n}.zip</a> {files}</p>
  </div>
</article>""")
            continue
        src = p["claim"].get("url")
        cards.append(f"""
<article class="card">
  <video controls preload="metadata" src="{n}/video.mp4"></video>
  <div class="body">
    <div class="tags"><span class="badge bad">NOTOʻGʻRI · НЕВЕРНО</span><span class="badge">{_e(p['lang'])}</span>
      {'<span class="badge demo">oʻquv misoli · учебный пример</span>' if p.get('demo') else ''}</div>
    <p class="claim">«{_e(p['claim'].get('quote'))}»</p>
    <p class="where">{_e(p['claim'].get('platform'))} · {_e((p['claim'].get('published_at') or '')[:10])} · {_e(p['claim'].get('where'))}
      {f' · <a href="{_e(src)}" target="_blank" rel="noopener">manba · источник</a>' if src else ''}</p>
    <p class="law"><b>{_e(main.get('article'))}</b><br>{_e(main.get('text', '')[:420])}{'…' if len(main.get('text', '')) > 420 else ''}</p>
    <p class="why">{_e(p.get('reason'))}</p>
    <p class="links">{links}</p>
    <p class="files"><a class="chip main" href="{n}.zip" download>⬇ {n}.zip</a> {files}</p>
  </div>
</article>""")
    def what(s) -> str:
        if s.get("kind") == "law":
            fmt = rolik.FORMATS.get(s.get("format"), ("", "ролик о законе", ""))
            return f"{_e(fmt[0])} · {_e(fmt[1])}"
        return f"«{_e((s.get('quote') or '')[:140])}»"
    rows = "".join(
        f'<li><span class="badge {STATUS.get(s["status"], ("", "wait"))[1]}">{_e(STATUS.get(s["status"], (s["status"],))[0])}</span> '
        f'<b>{_e(s.get("article"))}</b> · {_e(s.get("lang"))} — {what(s)}'
        f'<br><small>{_e(s.get("message"))}</small></li>' for s in pending)
    body = f"""<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Raddiyalar · Опровержения</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--muted:#5f6673;--line:#e3e6eb;--accent:#1f6feb;--bad:#c62828;--ok:#2e7d32;--warn:#b26a00}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1115;--card:#171a21;--fg:#e8eaee;--muted:#9aa3b2;--line:#2a2f3a;--accent:#6aa5ff;--bad:#ef5350;--ok:#66bb6a;--warn:#ffb74d}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 60px}} h1{{margin:0 0 4px;font-size:24px}} .sub{{color:var(--muted);margin:0 0 20px}}
a{{color:var(--accent)}} h2{{font-size:16px;margin:28px 0 10px;color:var(--muted);font-weight:600}}
.card{{display:grid;grid-template-columns:260px 1fr;gap:18px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px}}
video{{width:100%;aspect-ratio:9/16;background:#000;border-radius:10px;object-fit:contain}}
.claim{{font-size:18px;font-weight:600;margin:8px 0 4px}} .where,.why{{color:var(--muted);margin:4px 0}} .law{{border-left:3px solid var(--ok);padding-left:10px;margin:12px 0}}
.badge{{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);margin-right:6px;color:var(--muted)}}
.badge.bad{{color:#fff;background:var(--bad);border-color:var(--bad);font-weight:700}} .badge.ok{{color:var(--ok);border-color:var(--ok)}}
.badge.warn{{color:var(--warn);border-color:var(--warn)}} .badge.good{{color:#fff;background:var(--ok);border-color:var(--ok);font-weight:700}} .badge.demo{{color:var(--warn);border-color:var(--warn)}}
.chip{{display:inline-block;padding:4px 10px;border:1px solid var(--line);border-radius:8px;text-decoration:none;font-size:13px;margin:2px 4px 2px 0}}
.chip.main{{background:var(--accent);color:#fff;border-color:var(--accent)}} ul{{padding:0;list-style:none}} li{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}}
.empty{{color:var(--muted)}} @media (max-width:700px){{.card{{grid-template-columns:1fr}} video{{max-height:70vh}}}}
</style></head><body><main>
<h1>Raddiyalar va qonun videolari · Опровержения и ролики о законах</h1>
<p class="sub">Modda Monitor topgan notoʻgʻri daʼvolarga raddiyalar va qonun bazasidan buyurtma qilingan videolar. Eʼlon qilishdan oldin yurist tekshirsin.<br>
Опровержения фейков, найденных Modda Monitor, и ролики о законах, заказанные из <a href="../">поиска по законам</a>
(кнопка «Снять ролик» у статьи). Перед публикацией нужна проверка юриста.</p>
{f'<h2>Jarayonda · В работе</h2><ul>{rows}</ul>' if pending else ''}
<h2>Tayyor paketlar · Готовые пакеты ({len(packages)})</h2>
{''.join(cards) or '<p class="empty">Hozircha yoʻq · Пока нет готовых пакетов.</p>'}
</main></body></html>"""
    return body.encode("utf-8")
