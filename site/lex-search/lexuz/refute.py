"""Мост опровержений: ложное утверждение из Modda Monitor → ролик MODDA → пакет в папке выгрузки.

Каждый запуск (systemd-таймер раз в 2 минуты):
  1. читает активные находки Modda Monitor нужных категорий (по умолчанию inaccurate_claim — ложные утверждения о законе);
  2. для каждого правила берёт из нашей базы ту же статью на узбекском и русском;
  3. ставит в очередь MODDA один ролик-опровержение на язык (формат «Проверка спорного утверждения»);
     ролик снимает ноутбук: сессия Claude с /loop 2m /modda делает клипы в Google Flow и монтаж;
  4. когда MODDA получила MP4, собирает пакет: видео, объяснение на двух языках, тексты для публикации, case.json, zip.

Базы Modda Monitor и MODDA только читаются; задания создаются через API студии (вход по ключу владельца).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import passages, rolik

MONITOR_DB = Path(os.getenv("LEX_MONITOR_DB", "/opt/lex-monitor/data/monitor.sqlite"))
STUDIO_DB = Path(os.getenv("MODDA_STUDIO_DB", "/opt/lex-monitor/modda/data/studio.sqlite"))
STUDIO_API = os.getenv("MODDA_API", "http://127.0.0.1:18883/studio/api")
ORIGIN = os.getenv("MODDA_ORIGIN", "https://nasa.cartify.ink")
CATEGORIES = set(filter(None, os.getenv("REFUTE_CATEGORIES", "inaccurate_claim").split(",")))
LANGS = [x for x in os.getenv("REFUTE_LANGS", "uz").split(",") if x in ("uz", "ru")]
SCENES = int(os.getenv("REFUTE_SCENES", "2"))           # одна сцена = одна платная генерация во Flow
STYLE = os.getenv("REFUTE_STYLE", "stickman")             # «Рисованные человечки», как в исходном шаблоне MODDA
ASPECT = os.getenv("REFUTE_ASPECT", "9:16")
DAILY_LIMIT = int(os.getenv("REFUTE_DAILY_LIMIT", "3"))  # не больше N новых роликов в сутки
LAW_DAILY_LIMIT = int(os.getenv("LAW_DAILY_LIMIT", "5"))  # роликов о законах по заказам из поиска в сутки
RETRY_SECONDS = 1800
FILES = ("video.mp4", "README.md", "caption.uz.txt", "caption.ru.txt", "case.json")
NAME = re.compile(r"^\d{4}-\d\d-\d\d_[a-z0-9-]+_(uz|ru)$")


# --- состояние --------------------------------------------------------------------------------------
def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def _locked(data_dir: Path):
    """Один проход за раз: таймер и ручной --retry не должны перезаписать refute.json друг другу."""
    import fcntl  # только на сервере (Linux)
    with open(data_dir / "refute.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


# --- Modda Monitor ------------------------------------------------------------------------------------
def load_findings() -> dict[str, list[dict]]:
    """Активные находки Modda Monitor нужных категорий, сгруппированные по правилу (одно опровержение на правило)."""
    groups: dict[str, list[dict]] = {}
    with _ro(MONITOR_DB) as con:
        for r in con.execute("SELECT id, rule_id, created_at, payload FROM auto_alerts WHERE active=1 ORDER BY created_at"):
            p = json.loads(r["payload"])
            if p.get("category") in CATEGORIES:
                groups.setdefault(r["rule_id"], []).append({**p, "alert_id": r["id"], "found_at": r["created_at"]})
    return groups


def law_for(con, finding: dict) -> dict:
    """Статья, на которую ссылается Modda Monitor, из нашей базы — на узбекском и русском."""
    m = re.search(r"стать[яиюей]\s*(\d+)", finding.get("law_label", ""), re.I)
    num = m.group(1) if m else None
    law = {"label": finding.get("law_label", ""), "monitor_url": finding.get("law_url", "")}
    for lg in ("uz", "ru"):
        unit = passages.resolve_link(con, finding.get("law_url", ""), lg, num)
        law[lg] = passages.to_passage(con, unit, "lat") if unit is not None else None
    return law


def _source_line(f: dict) -> str:
    bits = [f.get("platform", ""), (f.get("published_at") or "")[:10], f.get("where", ""), f.get("url") or f.get("original") or ""]
    return ", ".join(b for b in bits if b)


def prompt_for(finding: dict, law: dict, lang: str) -> str:
    """Задание для MODDA. Ссылка на статью и метка языка — чтобы поиск базы вернул ровно эту статью."""
    p = law[lang]
    head = "Опровержение ложного утверждения: сначала само утверждение со штампом «НЕВЕРНО», затем что говорит закон."
    tail = f"\nНорма: {law['label']}\nИсточник: {p['url']} lang:{lang}"
    quote = finding.get("quote", "")[:400]
    reason = finding.get("reason", "")
    body = f"\nУтверждение: «{quote}»\nГде: {_source_line(finding)}\nПочему неверно: {reason}"
    room = 1500 - len(head) - len(tail)
    return head + (body if len(body) <= room else body[: room - 1] + "…") + tail


# --- MODDA ------------------------------------------------------------------------------------------
def _studio(op: str, data: dict, token: str | None = None):
    headers = {"Content-Type": "application/json", "Origin": ORIGIN}
    if token:
        headers["Cookie"] = "modda_session=" + token
    req = Request(f"{STUDIO_API}/{op}", data=json.dumps(data, ensure_ascii=False).encode(), headers=headers)
    try:
        with urlopen(req, timeout=60) as r:
            return json.load(r), r.headers
    except HTTPError as e:
        try:
            msg = json.load(e).get("error") or str(e)
        except ValueError:
            msg = str(e)
        raise RuntimeError(f"MODDA {op}: {msg}") from None


def studio_login() -> str:
    _, headers = _studio("login", {"code": os.environ["MODDA_OWNER_KEY"]})
    cookie = SimpleCookie()
    cookie.load(headers.get("Set-Cookie", ""))
    return cookie["modda_session"].value


def studio_job(job_id: str):
    with _ro(STUDIO_DB) as con:
        return con.execute("SELECT id, status, phase, message, updated, bytes, sha FROM jobs WHERE id=?", (job_id,)).fetchone()


# --- пакет выгрузки ---------------------------------------------------------------------------------
def _first_sentence(text: str, limit: int = 240) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip(), re.S)
    s = (m.group(1) if m else text).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _readme(case: dict, finding: dict, law: dict, job) -> str:
    uz, ru = law.get("uz"), law.get("ru")
    where = _source_line(finding) or "—"
    demo = finding.get("is_demo")
    lines = [f"# Raddiya / Опровержение — {(uz or ru or {}).get('article', law['label'])}", ""]
    if demo:
        lines += ["> **Oʻquv misoli / Учебный пример.** Modda Monitor’ning namunaviy holati — haqiqiy holat sifatida eʼlon qilmang. "
                  "Это демонстрационный случай Modda Monitor, не публикуйте его как реальный.", ""]
    lines += ["> Avtomatik tayyorlangan. Eʼlon qilishdan oldin yurist tekshirsin. / Подготовлено автоматически, "
              "перед публикацией нужна проверка юриста.", ""]
    lines += ["## Oʻzbekcha", "",
              f"**Daʼvo:** «{finding.get('quote', '')}»", "",
              f"**Qayerda:** {where}", "",
              f"**Nima uchun notoʻgʻri:** daʼvo qonunga zid. {uz['article'] if uz else law['label']} quyidagicha belgilaydi:", ""]
    if uz:
        lines += ["> " + uz["text"].replace("\n", "\n> "), "",
                  f"**Manba:** [{uz['title']} — {uz['article']}]({uz['url']}), tahrir: {uz['as_of']}", ""]
    lines += ["**Video:** `video.mp4`", "", "## Русский", "",
              f"**Утверждение:** «{finding.get('quote', '')}»", "",
              f"**Где:** {where}", "",
              f"**Почему это неверно:** {finding.get('reason', '')}", ""]
    if ru:
        lines += ["**Что говорит закон:**", "", "> " + ru["text"].replace("\n", "\n> "), "",
                  f"**Источник:** [{ru['title']} — {ru['article']}]({ru['url']}), редакция от {ru['as_of']}", ""]
    lines += [f"**Норма по данным Modda Monitor:** {law['label']} — {law['monitor_url']}", "",
              "**Видео:** `video.mp4`", "", "---",
              f"Modda Monitor: находка {finding.get('found_at', '')} · правило `{case['rule_id']}` · "
              f"MODDA: ролик `{case['job_id']}` · пакет собран {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    return "\n".join(lines)


def _captions(finding: dict, law: dict) -> dict[str, str]:
    out = {}
    for lg, (claim, fact, src, tags) in {
        "uz": ("Daʼvo", "Qonunda", "Manba", "#raddiya #lexuz #huquq"),
        "ru": ("Утверждение", "По закону", "Источник", "#опровержение #lexuz #право"),
    }.items():
        p = law.get(lg)
        if not p:
            continue
        out[lg] = (f"{claim}: «{finding.get('quote', '')}» — NOTOʻGʻRI / НЕВЕРНО.\n"
                   f"{fact} ({p['article']}): «{_first_sentence(p['text'])}»\n"
                   f"{src}: {p['url']}\n{tags}\n")
    return out


def build_package(export_dir: Path, case: dict, job) -> str:
    day = datetime.fromtimestamp(job["updated"], timezone.utc).strftime("%Y-%m-%d")
    name = f"{day}_{case['rule_id']}_{case['lang']}"
    finding, law = case["findings"][0], case["law"]
    texts = {"README.md": _readme(case, finding, law, job)}
    texts.update({f"caption.{lg}.txt": t for lg, t in _captions(finding, law).items()})
    info = {
        "name": name, "kind": "refute", "lang": case["lang"], "rule_id": case["rule_id"], "demo": bool(finding.get("is_demo")),
        "claim": {"quote": finding.get("quote"), "title": finding.get("title"), "where": finding.get("where"),
                  "platform": finding.get("platform"), "url": finding.get("url") or finding.get("original") or None,
                  "account": finding.get("account"), "published_at": finding.get("published_at"),
                  "category": finding.get("category")},
        "verdict": "false", "reason": finding.get("reason"),
        "law": law, "instances": [{k: f.get(k) for k in ("alert_id", "url", "platform", "published_at", "quote", "where")}
                                   for f in case["findings"]],
        "video": {"file": "video.mp4", "modda_job": case["job_id"], "bytes": job["bytes"], "sha256": job["sha"]},
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _package(export_dir, name, case["job_id"], texts, info)


# --- ролики о законах (заказы из поиска, см. rolik.py) ---------------------------------------------------
def _law_readme(case: dict, job) -> str:
    law, (fmt_uz, fmt_ru, _) = case["law"], rolik.FORMATS[case["format"]]
    _, aud_uz, aud_ru = rolik.STYLES[case["style"]]
    main = law.get(case["lang"]) or {}
    lines = [f"# Qonun · Закон — {main.get('article', law['label'])}", "",
             "> Avtomatik tayyorlangan. Eʼlon qilishdan oldin yurist tekshirsin. / Подготовлено автоматически, "
             "перед публикацией нужна проверка юриста.", "",
             f"**Format / Формат:** {fmt_uz} · {fmt_ru}", "",
             f"**Tomoshabinlar / Зрители:** {aud_uz} · {aud_ru}", "",
             f"**Video tili / Язык ролика:** {case['lang']}", ""]
    for lg, head, src, ed in (("uz", "## Oʻzbekcha", "Manba", "tahrir"), ("ru", "## Русский", "Источник", "редакция от")):
        p = law.get(lg)
        if p:
            lines += [head, "", f"**{p['article']}** — {p['title']}", "", "> " + p["text"].replace("\n", "\n> "), "",
                      f"**{src}:** {p['url']}, {ed} {p['as_of']}", ""]
    lines += ["**Video / Видео:** `video.mp4`", "", "---",
              f"Buyurtma · заказ {datetime.fromtimestamp(case['order']['ordered_at'], timezone.utc):%Y-%m-%d %H:%M} UTC · "
              f"MODDA: ролик `{case['job_id']}` · пакет собран {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    return "\n".join(lines)


def _law_captions(case: dict) -> dict[str, str]:
    out = {}
    fmt = rolik.FORMATS[case["format"]]
    for lg, label, src, tags in (("uz", fmt[0], "Manba", "#qonun #lexuz #huquq"), ("ru", fmt[1], "Источник", "#закон #lexuz #право")):
        p = case["law"].get(lg)
        if p:
            out[lg] = f"{label}: {p['article']}\n«{_first_sentence(p['text'])}»\n{src}: {p['url']}\n{tags}\n"
    return out


def build_law_package(export_dir: Path, case: dict, job) -> str:
    day = datetime.fromtimestamp(job["updated"], timezone.utc).strftime("%Y-%m-%d")
    name = f"{day}_{case['rule_id']}_{case['lang']}"
    texts = {"README.md": _law_readme(case, job)}
    texts.update({f"caption.{lg}.txt": t for lg, t in _law_captions(case).items()})
    info = {"name": name, "kind": "law", "lang": case["lang"], "rule_id": case["rule_id"], "format": case["format"],
            "style": case["style"], "audience": rolik.STYLES[case["style"]][0], "scenes": case["scenes"], "law": case["law"],
            "video": {"file": "video.mp4", "modda_job": case["job_id"], "bytes": job["bytes"], "sha256": job["sha"]},
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return _package(export_dir, name, case["job_id"], texts, info)


def _package(export_dir: Path, name: str, job_id: str, texts: dict[str, str], info: dict) -> str:
    """Папка пакета (ролик + тексты + case.json) и zip рядом; собирается во временной папке и подменяется целиком."""
    target, tmp = export_dir / name, export_dir / (name + ".building")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    shutil.copyfile(STUDIO_DB.parent / "videos" / f"{job_id}.mp4", tmp / "video.mp4")
    for fname, text in texts.items():
        (tmp / fname).write_text(text, encoding="utf-8")
    _write_json(tmp / "case.json", info)
    for f in tmp.iterdir():
        os.chmod(f, 0o644)
    shutil.rmtree(target, ignore_errors=True)
    tmp.replace(target)
    os.chmod(target, 0o755)
    zpath = export_dir / (name + ".zip")
    with zipfile.ZipFile(zpath.with_suffix(".zip.tmp"), "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            if (target / f).exists():
                z.write(target / f, f"{name}/{f}", compress_type=zipfile.ZIP_STORED if f.endswith(".mp4") else zipfile.ZIP_DEFLATED)
    os.chmod(zpath.with_suffix(".zip.tmp"), 0o644)
    zpath.with_suffix(".zip.tmp").replace(zpath)
    return name


# --- один проход --------------------------------------------------------------------------------------
def retry(data_dir: Path, target: str) -> list[str]:
    """Ручной перезапуск дела после «внимания» или ошибки: target = правило или правило:язык.

    Новое задание создаст следующий проход таймера. Запускать только после того, как человек убедился,
    что клипы во Flow для прежнего задания не делались (иначе это повторная платная генерация).
    """
    rule, _, lang = target.partition(":")
    with _locked(data_dir):
        state_path = data_dir / "refute.json"
        state = _load_json(state_path, {"cases": {}})
        now, log = int(time.time()), []
        hits = [c for c in state["cases"].values() if c["rule_id"] == rule and (not lang or c["lang"] == lang)]
        for case in hits:
            key = f"{case['rule_id']}:{case['lang']}"
            if case.get("status") not in ("attention", "error"):
                log.append(f"{key}: статус {case.get('status')}, перезапуск не нужен")
                continue
            if case.get("job_id"):
                case.setdefault("previous_jobs", []).append(
                    {"job_id": case["job_id"], "created": case.get("created", now), "message": case.get("message", "")})
            for k in ("job_id", "retry_at", "phase"):
                case.pop(k, None)
            case.update(status="new", request_id=uuid.uuid4().hex, updated=now,
                        message="Перезапуск: новое задание MODDA появится в течение 2 минут")
            log.append(f"{key}: {case['message']}")
        if not hits:
            log.append(f"дел по правилу «{target}» нет")
        _write_json(state_path, state)
    return log


def run(lex_con, data_dir: Path, dry_run: bool = False) -> list[str]:
    with _locked(data_dir):
        return _run(lex_con, data_dir, dry_run)


def _run(lex_con, data_dir: Path, dry_run: bool) -> list[str]:
    state_path = data_dir / "refute.json"
    export_dir = data_dir / "eksport"
    export_dir.mkdir(exist_ok=True)
    os.chmod(export_dir, 0o755)
    state = _load_json(state_path, {"cases": {}})
    cases, log, now = state["cases"], [], int(time.time())
    token = None

    # 1–3: новые находки → задания MODDA
    # лимит считает и прежние попытки, перезапущенные через --retry: каждая могла стоить генераций во Flow
    created_today = _created_today(cases, now, "refute")
    for rule_id, findings in load_findings().items():
        for lang in LANGS:
            key = f"{rule_id}:{lang}"
            case = cases.get(key)
            if case and (case.get("job_id") or now < case.get("retry_at", 0)):
                if case.get("job_id"):
                    case["findings"] = findings  # новые экземпляры того же фейка попадут в case.json
                continue
            law = law_for(lex_con, findings[0])
            case = cases[key] = {"rule_id": rule_id, "lang": lang, "created": now, "findings": findings, "law": law,
                                 "status": "new", "request_id": (case or {}).get("request_id") or uuid.uuid4().hex,
                                 "previous_jobs": (case or {}).get("previous_jobs", [])}
            if not law.get(lang):
                case.update(status="error", message=f"Статья «{law['label']}» не найдена в базе на языке {lang}",
                            retry_at=now + RETRY_SECONDS)
                log.append(f"{key}: {case['message']}")
                continue
            if created_today >= DAILY_LIMIT:
                case.update(status="waiting", message=f"Дневной лимит {DAILY_LIMIT} роликов исчерпан", retry_at=now + 3600)
                continue
            spec = dict(request_id=case["request_id"], prompt=prompt_for(findings[0], law, lang), format="refute",
                        style=STYLE, lang=lang, aspect=ASPECT, scenes=SCENES, corpus="team")
            if dry_run:
                log.append(f"{key}: создал бы задание MODDA:\n{spec['prompt']}")
                cases.pop(key)
                continue
            try:
                token = token or studio_login()
                job, _ = _studio("jobs", spec, token)
                case.update(job_id=job["id"], status="queued", message="В очереди MODDA — ролик снимет ноутбук")
                created_today += 1
                log.append(f"{key}: создан ролик MODDA {job['id']}")
            except (RuntimeError, OSError, KeyError) as e:
                case.update(status="error", message=str(e)[:300], retry_at=now + RETRY_SECONDS)
                log.append(f"{key}: ошибка: {case['message']}")

    # 3б: заказы роликов о законах из поиска (data/rolik-orders) → задания MODDA
    token = _law_orders(lex_con, data_dir, cases, now, log, dry_run, token)

    # 4: готовые ролики → пакеты выгрузки
    for key, case in cases.items():
        if not case.get("job_id") or case.get("status") == "exported":
            continue
        job = studio_job(case["job_id"])
        if job is None:
            case.update(status="error", message="Задание пропало из MODDA")
        elif job["status"] == "done":
            build = build_law_package if case.get("kind") == "law" else build_package
            case.update(status="exported", export=build(export_dir, case, job), message="")
            log.append(f"{key}: пакет {case['export']}")
        else:
            case.update(status="attention" if job["status"] == "attention" else "queued",
                        message=job["message"] or {"queued": "В очереди MODDA", "active": "Ноутбук снимает ролик"}.get(job["status"], ""),
                        phase=job["phase"])
        case["updated"] = now

    if not dry_run:
        _write_json(state_path, state)
        _write_json(export_dir / "_status.json", [
            {k: c.get(k) for k in ("rule_id", "lang", "status", "message", "phase", "job_id", "export", "created", "format")}
            | {"kind": c.get("kind", "refute"), "article": (c["law"].get(c["lang"]) or {}).get("article"),
               "quote": c["findings"][0].get("quote") if c.get("findings") else None}
            for c in cases.values()])
    return log


def _created_today(cases: dict, now: int, kind: str) -> int:
    mine = [c for c in cases.values() if c.get("kind", "refute") == kind]
    return (sum(1 for c in mine if c.get("job_id") and now - c.get("created", 0) < 86400)
            + sum(1 for c in mine for p in c.get("previous_jobs", []) if now - p.get("created", 0) < 86400))


def _law_orders(lex_con, data_dir: Path, cases: dict, now: int, log: list, dry_run: bool, token):
    """Заказы из формы «Снять ролик» → дела kind=law → задания MODDA (свой дневной лимит LAW_DAILY_LIMIT)."""
    for f in sorted(rolik.orders_dir(data_dir).glob("*.json")):
        try:
            order = json.loads(f.read_text(encoding="utf-8"))
            unit = passages._unit(lex_con, order["doc_id"], order["doc_lang"], None, order["anchor"])
            law = rolik.law_for_unit(lex_con, order["doc_id"], order["doc_lang"], order["anchor"])
            rid = rolik.rule_id(order, passages.article_number(unit["heading"]) if unit else None)
        except (OSError, ValueError, KeyError) as e:
            log.append(f"заказ {f.name} не прочитан: {e}")
            if not dry_run:
                f.unlink(missing_ok=True)
            continue
        key = f"{rid}:{order['lang']}"
        old = cases.get(key)
        if old and old.get("status") not in ("error", "attention", "exported"):
            log.append(f"{key}: такой ролик уже в работе ({old.get('status')}), повторный заказ пропущен")
        elif law is None or not law.get(order["lang"]):
            log.append(f"{key}: статьи нет в базе на языке {order['lang']}, заказ пропущен")
        else:
            previous = (old or {}).get("previous_jobs", [])
            if old and old.get("job_id"):
                previous = previous + [{"job_id": old["job_id"], "created": old.get("created", now), "message": old.get("message", "")}]
            cases[key] = {"kind": "law", "rule_id": rid, "lang": order["lang"], "format": order["format"],
                          "style": order["style"], "scenes": order["scenes"], "order": order, "law": law,
                          "created": now, "status": "new", "request_id": uuid.uuid4().hex, "previous_jobs": previous,
                          "message": "Buyurtma qabul qilindi · Заказ принят"}
            log.append(f"{key}: заказ принят ({order['format']}, {order['style']}, {order['scenes']} сц.)")
        if not dry_run:
            f.unlink(missing_ok=True)

    made = _created_today(cases, now, "law")
    for key, case in cases.items():
        if case.get("kind") != "law" or case.get("job_id") or case.get("status") not in ("new", "waiting", "error") \
                or now < case.get("retry_at", 0):
            continue
        if made >= LAW_DAILY_LIMIT:
            case.update(status="waiting", message=f"Дневной лимит {LAW_DAILY_LIMIT} роликов о законах исчерпан", retry_at=now + 3600)
            continue
        spec = dict(request_id=case["request_id"], prompt=rolik.prompt_for(case), format=case["format"], style=case["style"],
                    lang=case["lang"], aspect="9:16", scenes=case["scenes"], corpus="team")
        if dry_run:
            log.append(f"{key}: создал бы задание MODDA:\n{spec['prompt']}")
            continue
        try:
            token = token or studio_login()
            job, _ = _studio("jobs", spec, token)
            case.update(job_id=job["id"], created=now, status="queued", message="В очереди MODDA — ролик снимет ноутбук")
            made += 1
            log.append(f"{key}: создан ролик MODDA {job['id']}")
        except (RuntimeError, OSError, KeyError) as e:
            case.update(status="error", message=str(e)[:300], retry_at=now + RETRY_SECONDS)
            log.append(f"{key}: ошибка: {case['message']}")
    return token
