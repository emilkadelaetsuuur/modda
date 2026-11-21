"""Сборка промптов кадров и движения из шаблонов: стиль + ракурс + персонажи + сцена."""
from .config import ASSETS, load_yaml


def _lib():
    return (load_yaml("styles.yaml"), load_yaml("shots.yaml"), load_yaml("motions.yaml"),
            load_yaml("characters.yaml"))


def frame_prompt(scn, cid):
    styles, shots, _, chars = _lib()
    c = scn["clips"][cid]
    if c.get("frame_prompt"):  # ручной промпт целиком
        return c["frame_prompt"]
    st = styles[scn.get("style", "bright")]
    parts = [st["prompt"]]
    if c.get("shot") in shots:
        parts.append(shots[c["shot"]]["camera"])
    parts.append(c.get("scene", ""))
    if c.get("props"):
        parts.append("Key objects clearly visible and recognizable: " + ", ".join(c["props"]) + ".")
    for ch in c.get("characters", []):
        if ch in chars:
            parts.append(chars[ch]["sheet"])
    vert = scn.get("aspect", "9:16") == "9:16"
    parts.append(("Vertical 9:16 composition" if vert else "Horizontal 16:9 widescreen composition")
                 + ", the subject fills the frame, safe empty area in the lower third for subtitles. "
                 "No text, no letters, no numbers, no logos, no watermark.")
    return " ".join(p.strip() for p in parts if p)


def motion_prompt(scn, cid):
    styles, _, motions, _ = _lib()
    c = scn["clips"][cid]
    if c.get("motion_prompt"):
        return c["motion_prompt"]
    st = styles[scn.get("style", "bright")]
    m = motions.get(c.get("motion") or "push_in_slow", motions["push_in_slow"])
    act = (c.get("action") or "the characters act naturally").strip().rstrip(".")
    act = act[:1].upper() + act[1:]
    body = " ".join(m["prompt"].replace("{action}", act).split()).replace("..", ".")
    suffix = motions["suffix"]
    if scn.get("aspect", "9:16") == "16:9":
        suffix = suffix.replace("Vertical 9:16.", "Horizontal 16:9.")
    return " ".join([body, st["lock"], suffix])


def negative(scn):
    return load_yaml("styles.yaml")[scn.get("style", "bright")]["negative"]


def refs(scn, cid):
    styles, _, _, chars = _lib()
    out = []
    for ch in scn["clips"][cid].get("characters", []):
        out += chars.get(ch, {}).get("refs", [])
    out += styles[scn.get("style", "bright")].get("refs", [])
    paths = []
    for r in out:
        p = ASSETS / "refs" / r
        if p.exists() and p not in paths:
            paths.append(p)
    return paths[:3]


def _checks(scn, cid):
    chars = load_yaml("characters.yaml")
    out = ["в кадре НЕТ текста, букв, цифр и логотипов",
           "стиль как на референсе стиля (" + scn.get("style", "bright") + "): те же линии и цвета",
           "формат " + scn.get("aspect", "9:16")]
    for ch in scn["clips"][cid].get("characters", []):
        if chars.get(ch, {}).get("check"):
            out.append(f"{chars[ch]['name_ru']}: {chars[ch]['check']}")
    return out


def write_tasks(project, scn):
    """tasks/flow_playbook.md (пошаговая инструкция для человека или агента в Google Flow),
    tasks/flow_tasks.json (то же для автоматизации), tasks/flow_tasks.md (короткий список промптов)."""
    import json
    aspect = scn.get("aspect", "9:16")
    chars = load_yaml("characters.yaml")
    used_chars = []
    for c in scn["clips"].values():
        for ch in c.get("characters", []):
            if ch not in used_chars and chars.get(ch, {}).get("refs"):
                used_chars.append(ch)
    style_refs = [r.name for r in refs({"clips": {"x": {"characters": []}}, "style": scn.get("style", "bright")}, "x")]
    need_video = [cid for cid in scn["clips"]
                  if any(sh.get("visual", {}).get("clip") == cid for sh in scn["shots"])]
    L = [f"# Google Flow: {scn.get('title', scn.get('id'))}", "",
         "## Правила (нарушать нельзя)",
         "1. Промпты копировать ДОСЛОВНО и ЦЕЛИКОМ: не переводить, не сокращать, не дописывать, не придумывать свои.",
         "2. Видео делать ТОЛЬКО режимом «Кадры в видео» (Frames to video) из своего кадра. Текст-в-видео не использовать.",
         "3. Порядок: шаг 0 (референсы), шаг 1 (все кадры), шаг 2 (проверка кадров), шаг 3 (все видео). Не перескакивать.",
         f"4. Формат везде {aspect}. Картинки: Nano Banana. Видео: Veo 3.1 Lite (или Fast), 8 секунд, 720p, «Вернуть видео без звука».",
         "5. Каждый файл сразу сохранять под указанным именем. Имя = id клипа.",
         "6. Кадр не прошёл проверку: перегенерировать с ТЕМ ЖЕ промптом (до 3 раз). Промпт не менять.", "",
         "## Шаг 0. Референсы (один раз на аккаунт Flow)",
         "Загрузить в библиотеку Flow (кнопка добавления изображения → Загрузить) и дать понятные имена:"]
    for ch in used_chars:
        for k, r in enumerate(chars[ch]["refs"], 1):
            L.append(f"- `assets/refs/{r}` → имя «REF {ch} {k}»")
    for r in style_refs:
        L.append(f"- `assets/refs/{r}` → имя «REF style {scn.get('style', 'bright')}»")
    L += ["Если эти референсы уже есть в библиотеке, шаг пропустить.", "", "## Шаг 1. Кадры"]
    data = {"aspect": aspect, "style": scn.get("style"), "clips": []}
    for i, cid in enumerate(scn["clips"], 1):
        c = scn["clips"][cid]
        ings = [f"REF {ch} {k}" for ch in c.get("characters", []) if ch in used_chars
                for k in range(1, len(chars[ch]["refs"]) + 1)]
        ings.append(f"REF style {scn.get('style', 'bright')}")
        have = project.find_frame(scn, cid)
        L += [f"### 1.{i}. Кадр {cid}" + ("  (уже есть, пропустить)" if have else ""), "",
              f"- Режим: Изображение · Nano Banana · {aspect}",
              f"- Ингредиенты: {', '.join(ings)}",
              "- Промпт (вставить целиком):", "", "```", frame_prompt(scn, cid), "```", "",
              "- Проверка: " + "; ".join(_checks(scn, cid)),
              f"- Скачать (1K) → `frames/{cid}.png`", ""]
        data["clips"].append({"id": cid, "frame_prompt": frame_prompt(scn, cid), "motion_prompt": motion_prompt(scn, cid),
                              "ingredients": ings, "checks": _checks(scn, cid), "needs_video": cid in need_video,
                              "frame_file": f"frames/{cid}.png", "video_file": f"clips/{cid}.mp4",
                              "negative": negative(scn)})
    L += ["## Шаг 2. Проверка кадров", "",
          "Открыть все кадры рядом. Один и тот же персонаж должен выглядеть одинаково во всех кадрах (лицо, одежда, головной убор).",
          "Кадр, где персонаж отличается или есть текст, перегенерировать по шагу 1.", "", "## Шаг 3. Видео"]
    for i, cid in enumerate(need_video, 1):
        have = project.find_clip(scn, cid)
        L += [f"### 3.{i}. Видео {cid}" + ("  (уже есть, пропустить)" if have else ""), "",
              f"- Режим: Кадры в видео · Veo 3.1 Lite · {aspect} · 8 c · без звука",
              f"- Первый кадр: `{cid}` из шага 1 (выбрать в библиотеке, проверить, что прикрепился)",
              "- Промпт (вставить целиком):", "", "```", motion_prompt(scn, cid), "```", "",
              "- Проверка: движение плавное, стиль и персонаж не изменились, нет текста",
              f"- Скачать (720p) → `clips/{cid}.mp4`", ""]
    only_img = [cid for cid in scn["clips"] if cid not in need_video]
    if only_img:
        L += [f"Видео для {', '.join(only_img)} НЕ делать: эти кадры монтаж оживит сам.", ""]
    L += ["## Шаг 4. Сборка", "", "```", f"python -m studio voice {project.dir.name}", f"python -m studio build {project.dir.name}", "```", "",
          "## Если Flow отказал по контент-политике",
          "Бонусы вернутся. Перегенерировать один раз. Если снова отказ: для кадра упростить сцену можно ТОЛЬКО в scenario.json "
          "(поле scene), затем `python -m studio prompts <проект>` и взять новый промпт. Для видео: удалить клип из списка, "
          "монтаж оживит кадр сам."]
    (project.sub("tasks") / "flow_playbook.md").write_text("\n".join(L), encoding="utf-8")
    (project.sub("tasks") / "flow_tasks.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # короткий список
    S = [f"# Промпты: {scn.get('title', scn.get('id'))}", ""]
    for cl in data["clips"]:
        S += [f"## {cl['id']}", "FRAME:", "```", cl["frame_prompt"], "```", "VIDEO:" if cl["needs_video"] else "VIDEO: не нужно",
              *(["```", cl["motion_prompt"], "```"] if cl["needs_video"] else []), ""]
    (project.sub("tasks") / "flow_tasks.md").write_text("\n".join(S), encoding="utf-8")
    return project.sub("tasks") / "flow_playbook.md"
