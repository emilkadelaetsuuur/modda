"""Одна команда: ссылка на закон -> готовый ролик или пост.

python -m studio make <проект> --law <ссылка> --kind infographic|post|cartoon --lang ru|uz

infographic : LLM пишет сцены -> проверка формата и фактов -> озвучка VoiceLab -> MP4 16:9
post        : LLM пишет карусель -> проверка -> PNG-карточки + caption.txt
cartoon     : LLM пишет сценарий -> проверка -> кадры/видео (API или Flow по tasks/flow_playbook.md) -> озвучка -> MP4
Если для мультфильма нет кадров и видео, make останавливается и говорит, что сделать (мусор не собирает).
"""
import json
import shutil
import subprocess

from . import assemble, config, imagegen, infographic, lexuz, llm, posts, prompts, scriptwriter, videogen, voice

SCENE_FIELDS = {
    "title": ["lines"], "counter": ["value"], "timeline": ["points"], "bars": ["bars"], "compare": ["before", "after"],
    "columns": ["left", "right"], "statement": ["big"], "outro": ["lines"],
}
ICONS = {"firecracker", "rocket", "battery", "sparkler", "fountain", "doc", "check", "cross", "shield", "phone", "car", "traffic_light", "money", "house", "calendar", "gavel", "people", "school", "shop", "card", "warning", "clock"}


def validate_info(scn):
    errs = []
    shots = scn.get("shots") or []
    if len(shots) < 4:
        errs.append("меньше 4 сцен")
    for i, s in enumerate(shots):
        s.setdefault("id", f"s{i + 1:02d}")
        sc = s.get("scene") or {}
        t = sc.get("type")
        if t not in SCENE_FIELDS:
            errs.append(f"{s['id']}: неизвестный тип сцены {t}")
            continue
        for f in SCENE_FIELDS[t]:
            if not sc.get(f):
                errs.append(f"{s['id']} ({t}): нет поля {f}")
        if t == "counter":
            try:
                sc["value"] = int(str(sc["value"]).replace(" ", "").replace(",", ""))
            except Exception:  # noqa
                errs.append(f"{s['id']}: value должен быть числом")
        for key in ("icon",):
            if sc.get(key) and sc[key] not in ICONS:
                sc[key] = "doc"
        for side in ("left", "right"):
            for it in (sc.get(side) or {}).get("items", []):
                if it.get("icon") not in ICONS:
                    it["icon"] = "check" if side == "left" else "cross"
        if not s.get("text"):
            s.setdefault("dur", 4.0)
        else:
            s.setdefault("speaker", "narrator")
            s.setdefault("emotion", "serious")
    return errs


def write_infographic(project, cfg, law_url, lang, topic="", theme="official"):
    system = config.load_text("info_system.md").replace("{lang}", lang).replace("{brv}", str(cfg["brv"]))
    user = f"Тема: {topic or 'главное в законе для людей'}\nСсылка: {law_url}\n=== ЗАКОН ===\n{project.law()[:120000]}"
    for attempt in range(2):
        scn = llm.extract_json(llm.chat(system, user, cfg))
        scn.update({"kind": "infographic", "lang": lang, "theme": theme})
        errs = validate_info(scn)
        if not errs:
            break
        user += "\n\nПредыдущий ответ с ошибками формата, исправь: " + "; ".join(errs)
    project.save(scn)
    return scn, errs


def preflight(cfg, kind):
    """Что мешает собрать ролик. Возвращает список проблем."""
    p = []
    if not shutil.which("ffmpeg"):
        p.append("не установлен ffmpeg")
    if kind in ("cartoon", "infographic") and cfg["tts"]["provider"] == "voicelab" and not config.secret(cfg["tts"], False):
        p.append("нет VOICELAB_API_KEY в .env (озвучка)")
    if not config.secret(cfg["llm"], False) and cfg["llm"]["provider"] == "anthropic":
        p.append("нет ключа LLM (ANTHROPIC_API_KEY) для сценария")
    if kind == "cartoon":
        for sec in ("image", "video"):
            if cfg[sec]["mode"] == "api" and not config.secret(cfg[sec], False) and cfg[sec].get("backend") != "vertex":
                p.append(f"{sec}.mode=api, но нет GEMINI_API_KEY")
    return p


def run(project, cfg, law_url, kind="infographic", lang="ru", audience="adults", fmt="explainer", theme="official",
        style=None, aspect=None, topic="", post_format="4x5", rewrite=False, ignore_facts=False):
    probs = preflight(cfg, kind)
    if probs:
        raise SystemExit("Нельзя запускать:\n - " + "\n - ".join(probs))
    if law_url:
        doc = lexuz.get(law_url, cfg)
        project.law_path.write_text(doc["text"], encoding="utf-8")
    if not project.law():
        raise SystemExit("Нет текста закона: укажите --law <ссылка lex.uz>")

    if kind == "post":
        name = "post.json" if lang == "ru" else f"post_{lang}.json"
        if rewrite or not (project.dir / name).exists():
            posts.write(project, cfg, lang, theme, post_format, topic, law_url or "", name)
        post = json.loads((project.dir / name).read_text(encoding="utf-8"))
        rep = scriptwriter.check(posts.as_scenario(post), project.law(), cfg)
        if not scriptwriter.print_report([r for r in rep if r[0] != "warn"]) and not ignore_facts:
            raise SystemExit(f"Ошибки фактов в {name}: исправьте и запустите снова (или --ignore-facts)")
        out_dir, files = posts.render(project, cfg, name, theme, post_format)
        return {"post_dir": str(out_dir), "slides": [str(f) for f in files]}

    if kind == "infographic":
        if rewrite or not project.scenario_path.exists() or project.load().get("kind") != "infographic":
            scn, errs = write_infographic(project, cfg, law_url or "", lang, topic, theme)
            if errs:
                raise SystemExit("LLM дважды вернул неверный формат: " + "; ".join(errs))
        scn = project.load()
        if not scriptwriter.print_report([r for r in scriptwriter.check(scn, project.law(), cfg) if r[0] != "warn"]) and not ignore_facts:
            raise SystemExit("Ошибки фактов в scenario.json: исправьте и запустите снова (или --ignore-facts)")
        voice.run(project, cfg)
        out, T = infographic.render(project, cfg, theme)
        return {"video": str(out), "duration": round(T, 1)}

    # cartoon
    if rewrite or not project.scenario_path.exists() or project.load().get("kind") == "infographic":
        scriptwriter.write(project, cfg, law_url or "", lang, audience, fmt, style, topic)
    scn = project.load()
    if aspect:
        scn["aspect"] = aspect
        project.save(scn)
    if not scriptwriter.print_report(scriptwriter.check(scn, project.law(), cfg)) and not ignore_facts:
        raise SystemExit("Ошибки фактов в scenario.json: исправьте и запустите снова (или --ignore-facts)")
    pb = prompts.write_tasks(project, scn)
    imagegen.run(project, cfg)
    videogen.run(project, cfg)
    need = [s["visual"].get("clip") or s["visual"].get("image") for s in scn["shots"] if s.get("visual")]
    missing = sorted({c for c in need if c and not project.find_clip(scn, c) and not project.find_frame(scn, c)})
    if missing:
        raise SystemExit(f"Нет кадров/видео для: {', '.join(missing)}.\nСделайте их строго по {pb} "
                         f"(или включите image.mode/video.mode = api) и запустите make ещё раз.")
    voice.run(project, cfg)
    out, T = assemble.build(project, cfg)
    return {"video": str(out), "duration": round(T, 1)}


def doctor(cfg):
    """Проверка окружения: что есть, чего нет."""
    rows = []
    rows.append(("ffmpeg", bool(shutil.which("ffmpeg"))))
    for name, sec in (("LLM (сценарии, посты)", cfg["llm"]), ("VoiceLab (озвучка)", cfg["tts"]),
                      ("Gemini (кадры и видео по API)", cfg["image"])):
        rows.append((name, bool(config.secret(sec, False))))
    try:
        import faster_whisper  # noqa
        rows.append(("faster-whisper (тайминг субтитров)", True))
    except Exception:  # noqa
        rows.append(("faster-whisper (тайминг субтитров)", False))
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        rows.append(("ffmpeg loudnorm/zoompan", "loudnorm" in r.stdout and "zoompan" in r.stdout))
    except Exception:  # noqa
        pass
    rows.append((f"режим кадров: {cfg['image']['mode']}, режим видео: {cfg['video']['mode']}", True))
    return rows
