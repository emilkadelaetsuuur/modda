"""MODDA AI Studio: python -m studio <команда> <папка проекта> [опции]"""
import argparse
import json
import sys

from . import assemble, config, imagegen, infographic, lexuz, posts, prompts, scriptwriter, sfx, videogen, voice
from .project import Project


def cmd_init(a, cfg):
    p = Project(a.project)
    if p.scenario_path.exists():
        print("scenario.json уже есть, не трогаю")
    meta = {"law_url": a.law or "", "lang": a.lang, "audience": a.audience, "format": a.format,
            "style": a.style, "topic": a.topic or ""}
    (p.dir / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Проект создан: {p.dir}")
    if a.law:
        cmd_law(argparse.Namespace(project=a.project, url=a.law, file=None), cfg)


def _meta(p):
    f = p.dir / "project.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def cmd_law(a, cfg):
    p = Project(a.project)
    if a.file:
        text = open(a.file, encoding="utf-8").read()
        p.law_path.write_text(text, encoding="utf-8")
        print(f"law.txt из файла: {len(text)} символов")
        return
    url = a.url or _meta(p).get("law_url")
    doc = lexuz.get(url, cfg)
    p.law_path.write_text(doc["text"], encoding="utf-8")
    meta = _meta(p)
    meta.update({"law_url": doc["url"], "law_title": doc["title"], "law_status": doc["status"]})
    (p.dir / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{doc['title'][:120]}\nlaw.txt: {len(doc['text'])} символов {('(ещё не вступил в силу)' if doc['status'] else '')}")


def cmd_script(a, cfg):
    p = Project(a.project)
    m = _meta(p)
    fake = open(a.fake_file, encoding="utf-8").read() if a.fake_file else (a.fake or "")
    scn, rep = scriptwriter.write(p, cfg, m.get("law_url", ""), a.lang or m.get("lang", "ru"),
                                  a.audience or m.get("audience", "teens"), a.format or m.get("format", "explainer"),
                                  a.style or m.get("style"), a.topic or m.get("topic", ""), fake,
                                  llm_check=not a.no_llm_check)
    print(f"Сценарий: {scn.get('title')} | шотов {len(scn.get('shots', []))} | клипов {len(scn.get('clips', {}))}")
    ok = scriptwriter.print_report(rep)
    prompts.write_tasks(p, scn)
    if not ok:
        print("\nЕсть ошибки фактов. Исправьте scenario.json вручную или перегенерируйте (script ещё раз).")


def cmd_check(a, cfg):
    p = Project(a.project)
    scn = p.load()
    rep = scriptwriter.check(scn, p.law(), cfg)
    if a.llm:
        rep += scriptwriter.llm_factcheck(scn, p.law(), cfg)
    sys.exit(0 if scriptwriter.print_report(rep) else 1)


def cmd_prompts(a, cfg):
    p = Project(a.project)
    print("Промпты:", prompts.write_tasks(p, p.load()))


def cmd_frames(a, cfg):
    if a.mode:
        cfg["image"]["mode"] = a.mode
    imagegen.run(Project(a.project), cfg, a.only, a.force)


def cmd_clips(a, cfg):
    videogen.run(Project(a.project), cfg, a.only, a.force, a.mode)


def cmd_voice(a, cfg):
    if a.provider:
        cfg["tts"]["provider"] = a.provider
    voice.run(Project(a.project), cfg, a.only, a.force)


def cmd_build(a, cfg):
    p = Project(a.project)
    if a.draft:
        cfg["render"]["draft"] = True
    out, T = assemble.build(p, cfg, a.out, a.reuse_video)
    print(f"\nГОТОВО: {out}  ({T:.1f} c)")


def cmd_all(a, cfg):
    p = Project(a.project)
    if not p.law_path.exists() and _meta(p).get("law_url"):
        cmd_law(argparse.Namespace(project=a.project, url=None, file=None), cfg)
    if not p.scenario_path.exists():
        cmd_script(argparse.Namespace(project=a.project, lang=None, audience=None, format=None, style=None,
                                      topic=None, fake=None, fake_file=None, no_llm_check=False), cfg)
    scn = p.load()
    if not scriptwriter.print_report(scriptwriter.check(scn, p.law(), cfg)) and not a.ignore_facts:
        sys.exit("Остановлено: ошибки фактов (флаг --ignore-facts, чтобы собрать всё равно)")
    imagegen.run(p, cfg)
    videogen.run(p, cfg)
    voice.run(p, cfg)
    out, T = assemble.build(p, cfg)
    print(f"\nГОТОВО: {out}  ({T:.1f} c)")


def cmd_verify(a, cfg):
    p = Project(a.project)
    if a.law or not p.law_path.exists():
        cmd_law(argparse.Namespace(project=a.project, url=a.law, file=None), cfg)
    post = open(a.post_file, encoding="utf-8").read() if a.post_file else a.post
    res = scriptwriter.verdict(post, p.law(), cfg)
    (p.dir / "verdict.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ВЕРДИКТ: {res.get('verdict', '?').upper()}")
    for c in res.get("claims", []):
        ok = "цитата найдена" if c.get("quote_found") else "ЦИТАТА НЕ НАЙДЕНА, проверить руками"
        print(f" - [{c.get('status')}] {c.get('claim')}\n   закон ({c.get('article')}): {c.get('law_quote')}  ({ok})")
    print("\nRU:", res.get("short_ru"), "\nUZ:", res.get("short_uz"))


def cmd_refute(a, cfg):
    """Фейк -> вердикт -> сценарий-опровержение -> (если --build) ролик."""
    p = Project(a.project)
    meta = {"law_url": a.law, "lang": a.lang, "audience": a.audience, "format": "refute", "style": a.style}
    (p.dir / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd_law(argparse.Namespace(project=a.project, url=a.law, file=None), cfg)
    fake = open(a.fake_file, encoding="utf-8").read() if a.fake_file else a.fake
    (p.dir / "fake.txt").write_text(fake, encoding="utf-8")
    res = scriptwriter.verdict(fake, p.law(), cfg)
    (p.dir / "verdict.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ВЕРДИКТ: {res.get('verdict', '?').upper()} | {res.get('short_ru')}")
    if res.get("verdict") == "true":
        print("Пост соответствует закону, опровергать нечего. Можно сделать познавательный ролик: script --format explainer")
        return
    cmd_script(argparse.Namespace(project=a.project, lang=a.lang, audience=a.audience, format="refute",
                                  style=a.style, topic=res.get("refute_topic", ""), fake=fake, fake_file=None,
                                  no_llm_check=False), cfg)
    if a.build:
        cmd_all(argparse.Namespace(project=a.project, ignore_facts=False), cfg)


def cmd_infographic(a, cfg):
    p = Project(a.project)
    themes = list(config.load_yaml("info_themes.yaml")) if a.all_themes else [a.theme]
    if not a.stills:
        voice.run(p, cfg)
    for th in themes:
        if a.stills:
            print("Кадры:", infographic.stills(p, cfg, th))
        else:
            out, T = infographic.render(p, cfg, th)
            print(f"ГОТОВО: {out}  ({T:.1f} c)")


def cmd_post(a, cfg):
    p = Project(a.project)
    name = a.file or ("post.json" if a.lang == "ru" else f"post_{a.lang}.json")
    if a.rewrite or not (p.dir / name).exists():
        if a.law:
            cmd_law(argparse.Namespace(project=a.project, url=a.law, file=None), cfg)
        name, _ = posts.write(p, cfg, a.lang, a.theme or "official", a.format or "4x5", a.topic or "", a.law or "", name)
        print(f"LLM написал {name}")
    post = json.loads((p.dir / name).read_text(encoding="utf-8"))
    rep = [r for r in scriptwriter.check(posts.as_scenario(post), p.law(), cfg) if r[0] != "warn" or "цитата" in r[1]]
    scriptwriter.print_report(rep)
    themes = list(config.load_yaml("info_themes.yaml")) if a.all_themes else [a.theme]
    for th in themes:
        out_dir, files = posts.render(p, cfg, name, th, a.format)
        print(f"ГОТОВО: {len(files)} карточек + caption.txt + preview.jpg -> {out_dir}")


def cmd_serve(a, cfg):
    from .api import serve
    serve(a.port, a.root, getattr(a, "config", None))


def cmd_make(a, cfg):
    from . import make
    if a.clips:
        cfg["video"]["mode"] = a.clips
    if a.frames:
        cfg["image"]["mode"] = a.frames
    res = make.run(Project(a.project), cfg, a.law, a.kind, a.lang, a.audience, a.format, a.theme, a.style, a.aspect,
                   a.topic or "", a.post_format, a.rewrite, a.ignore_facts)
    print("\nГОТОВО:", json.dumps(res, ensure_ascii=False, indent=1))


def cmd_doctor(a, cfg):
    from . import make
    for name, ok in make.doctor(cfg):
        print(("  OK   " if ok else "  НЕТ  ") + name)


def cmd_templates(a, cfg):
    for name in ("styles", "audiences", "formats", "characters", "shots", "motions", "info_themes"):
        d = config.load_yaml(f"{name}.yaml")
        keys = [k for k in d if k != "suffix"]
        print(f"{name:11s}: {', '.join(keys)}")


def cmd_sfx(a, cfg):
    for pth in sfx.ensure_all():
        print(pth)


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa
        pass
    ap = argparse.ArgumentParser(prog="studio", description="MODDA AI Studio: ролики о законах из lex.uz")
    ap.add_argument("--config", help="путь к config.yaml")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sp.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        if name not in ("templates", "sfx"):
            s.add_argument("project")
        return s

    s = add("init", cmd_init, "создать проект")
    s.add_argument("--law", help="ссылка lex.uz")
    s.add_argument("--lang", default="ru", choices=["ru", "uz", "en"])
    s.add_argument("--audience", default="teens", choices=["kids", "teens", "adults", "elderly"])
    s.add_argument("--format", default="explainer")
    s.add_argument("--style")
    s.add_argument("--topic")
    s = add("law", cmd_law, "скачать текст закона")
    s.add_argument("url", nargs="?")
    s.add_argument("--file", help="взять текст из файла")
    s = add("script", cmd_script, "написать сценарий (LLM) + фактчек")
    for o in ("--lang", "--audience", "--format", "--style", "--topic", "--fake", "--fake-file"):
        s.add_argument(o)
    s.add_argument("--no-llm-check", action="store_true")
    s = add("check", cmd_check, "фактчек сценария")
    s.add_argument("--llm", action="store_true", help="плюс проверка LLM-юристом")
    add("prompts", cmd_prompts, "промпты кадров и видео в tasks/flow_tasks.md")
    s = add("frames", cmd_frames, "кадры (api | manual)")
    s.add_argument("--mode", choices=["api", "manual"])
    s.add_argument("--only", nargs="*")
    s.add_argument("--force", action="store_true")
    s = add("clips", cmd_clips, "видео-клипы (api | manual | still)")
    s.add_argument("--mode", choices=["api", "manual", "still"])
    s.add_argument("--only", nargs="*")
    s.add_argument("--force", action="store_true")
    s = add("voice", cmd_voice, "озвучка VoiceLab по шотам")
    s.add_argument("--provider", choices=["voicelab", "mock"])
    s.add_argument("--only", nargs="*")
    s.add_argument("--force", action="store_true")
    s = add("build", cmd_build, "собрать ролик")
    s.add_argument("--out")
    s.add_argument("--reuse-video", action="store_true", help="не пересобирать картинку, только звук")
    s.add_argument("--draft", action="store_true", help="черновик: вместо недостающих клипов заглушки")
    s = add("all", cmd_all, "всё подряд")
    s.add_argument("--ignore-facts", action="store_true")
    s = add("verify", cmd_verify, "проверить пост из соцсетей по закону (вердикт)")
    s.add_argument("--law", help="ссылка lex.uz")
    s.add_argument("--post", help="текст поста")
    s.add_argument("--post-file")
    s = add("refute", cmd_refute, "фейк -> вердикт -> ролик-опровержение")
    s.add_argument("--law", required=True)
    s.add_argument("--fake", help="текст фейка")
    s.add_argument("--fake-file")
    s.add_argument("--lang", default="ru", choices=["ru", "uz", "en"])
    s.add_argument("--audience", default="adults", choices=["kids", "teens", "adults", "elderly"])
    s.add_argument("--style")
    s.add_argument("--build", action="store_true", help="сразу собрать ролик")
    s = add("infographic", cmd_infographic, "анимированная инфографика 16:9 (без нейросети)")
    s.add_argument("--theme", help="editorial | dark_business | official | magazine | newspaper | minimal")
    s.add_argument("--all-themes", action="store_true", help="собрать во всех темах")
    s.add_argument("--stills", action="store_true", help="только лист с кадрами сцен, без видео")
    s = add("post", cmd_post, "пост-карусель для соцсетей (карточки + текст) на RU или UZ")
    s.add_argument("--lang", default="ru", choices=["ru", "uz", "en"])
    s.add_argument("--theme", help="тема как у инфографики: official, magazine, minimal ...")
    s.add_argument("--format", choices=["4x5", "1x1", "16x9", "9x16"], help="4x5 Instagram (по умолчанию), 1x1, 16x9 Telegram, 9x16 сторис")
    s.add_argument("--file", help="post.json (по умолчанию post.json или post_<lang>.json)")
    s.add_argument("--law", help="ссылка lex.uz, если поста ещё нет")
    s.add_argument("--topic")
    s.add_argument("--rewrite", action="store_true", help="заново написать пост через LLM")
    s.add_argument("--all-themes", action="store_true")
    sv = sp.add_parser("serve", help="HTTP API для интеграции (бот, сайт)")
    sv.set_defaults(fn=cmd_serve)
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--root", default="projects", help="папка для проектов")
    s = add("make", cmd_make, "ОДНА КОМАНДА: закон -> инфографика | пост | мультфильм")
    s.add_argument("--law", help="ссылка lex.uz")
    s.add_argument("--kind", default="infographic", choices=["infographic", "post", "cartoon"])
    s.add_argument("--lang", default="ru", choices=["ru", "uz", "en"])
    s.add_argument("--audience", default="adults", choices=["kids", "teens", "adults", "elderly"])
    s.add_argument("--format", default="explainer", help="для cartoon: explainer, refute, myth_fact, quick_rule, dialog_scene, quiz")
    s.add_argument("--theme", default="official", help="для infographic и post")
    s.add_argument("--style", help="для cartoon: bright, flat_motion, line_art, paper_cut, isometric ...")
    s.add_argument("--aspect", choices=["9:16", "16:9"])
    s.add_argument("--post-format", default="4x5", choices=["4x5", "1x1", "16x9", "9x16"])
    s.add_argument("--topic")
    s.add_argument("--frames", choices=["api", "manual"], help="кадры для cartoon")
    s.add_argument("--clips", choices=["api", "manual", "still"], help="видео для cartoon")
    s.add_argument("--rewrite", action="store_true", help="переписать сценарий заново")
    s.add_argument("--ignore-facts", action="store_true")
    dc = sp.add_parser("doctor", help="проверить ключи и окружение")
    dc.set_defaults(fn=cmd_doctor)
    add("templates", cmd_templates, "показать все шаблоны")
    add("sfx", cmd_sfx, "сгенерировать звуковые эффекты")

    a = ap.parse_args(argv)
    cfg = config.load(getattr(a, "project", None), a.config)
    a.fn(a, cfg)


if __name__ == "__main__":
    main()
