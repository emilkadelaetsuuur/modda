"""Narrow tools exposed to the MODDA Claude session.

Full MODDA AI Studio pipeline for a site order of 15, 30 or 45 seconds: a script with characters and a
narrator (each line voiced by VoiceLab), word-timed captions, sfx and music, Flow frames (Nano Banana)
turned into silent 8-second Veo clips, each clip cut into one or two shots. Legal claims come only from
the order's passages: every fact carries a verbatim quote, and numbers are checked against the law."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata

HOME = Path(os.environ.get("MODDA_HOME", Path(os.environ["LOCALAPPDATA"]) / "ModdaBridge"))
STUDIO = Path(os.environ.get("MODDA_STUDIO", Path(os.environ["LOCALAPPDATA"]) / "ModdaStudio" / "modda-ai-studio"))
VENV = STUDIO / ".venv" / "Scripts" / "python.exe"
# The studio's own environment has faster-whisper and the VoiceLab client; run there when called from elsewhere.
if VENV.is_file() and not os.environ.get("MODDA_REEXEC") and Path(sys.executable).resolve() != VENV.resolve():
    sys.exit(subprocess.run([str(VENV), "-X", "utf8", __file__, *sys.argv[1:]], stdin=sys.stdin,
                            env={**os.environ, "MODDA_REEXEC": "1"}).returncode)
sys.path.insert(0, str(STUDIO))
from studio import prompts, scriptwriter  # noqa: E402
from studio import config as sconfig  # noqa: E402
from studio.config import load_yaml  # noqa: E402
from studio.project import Project  # noqa: E402

DURATIONS = {15: 3, 30: 5, 45: 7}          # seconds -> Flow clips (8 s each, a clip gives 1-2 shots)
FLOW_PROJECT = "https://flow.google.com/project/e22cbe59-4dfa-4968-bd25-c344231f96c8"  # character refs live here
SPEAKERS = ("narrator", "abror", "malika", "sardor", "bobo")
COLORS = ("w", "y", "r", "g")
SFX = ("boom", "ding", "buzz", "stamp", "pop", "tick", "cash", "sparkle", "whoosh")
AUDIENCE = {"kids_soft": "kids", "elder_clear": "elderly", "doc_infographic": "adults", "isometric": "adults",
            "line_art": "adults"}
# VoiceLab speaking rate in syllables (vowels) per second. Measured on ru: law1 5.11, b7578072 5.07
# (words/s varied 2.1-2.4 because legal words are long). uz is not measured yet and uses the same rate.
SYL_PER_SEC = {"ru": 5.0, "uz": 5.0}
SYL_PER_WORD = {"ru": 2.4, "uz": 2.5}


def write_json(path, obj):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf8")
    os.replace(temp, path)


def load(path): return json.loads(path.read_text(encoding="utf8"))
def plain(s): return " ".join(unicodedata.normalize("NFKC", s).replace("ʻ", "'").replace("’", "'").casefold().split())
def words(s): return len(re.findall(r"\w+", s))
def syllables(s): return sum(ch in "аеёиоуыэюяaeiou" for ch in s.casefold())


def field(obj, key, limit, low=1):
    v = obj.get(key)
    if not isinstance(v, str) or not low <= len(v.strip()) <= limit:
        raise ValueError(f"Проверьте поле {key} (от {low} до {limit} символов)")
    return v.strip()


def target(spec):
    d = spec.get("duration")
    if d in DURATIONS:
        return d
    n = spec.get("scenes", 2)
    return 15 if n <= 3 else 30 if n <= 5 else 45


def audience(spec): return AUDIENCE.get(spec["style"], "teens")
def cfg(root): return sconfig.load(root)


def voiced(c, lang):
    ids = c["tts"].get("voice_ids", {})
    return [s for s in SPEAKERS if ids.get(f"{s}_{lang}") or ids.get(s)]


def budget(spec, c):
    """Speech budget for the ordered length: what the narration must fill once lead, gaps and outro are taken."""
    d = target(spec)
    R = c["render"]
    sps, spw = SYL_PER_SEC.get(spec["lang"], 5.0), SYL_PER_WORD.get(spec["lang"], 2.4)
    lo, hi = max(3, round(d / 3.8)), min(18, round(d / 2.3))
    speech = d - R["lead"] - R["outro"] - R["gap"] * ((lo + hi) // 2 - 1)
    syl = [round(speech * 0.92 * sps), round(speech * 1.04 * sps)]
    return dict(seconds=d, clips=spec["scenes"], shots=[lo, hi], syllables=syl,
                words=[round(syl[0] / spw), round(syl[1] / spw)], syllables_per_second=sps,
                note="Length is measured in syllables (vowels): VoiceLab reads about "
                     f"{sps:g} syllables per second, so long legal words cost more time than short ones.")


def estimate(shots, spec, c):
    R = c["render"]
    sps = SYL_PER_SEC.get(spec["lang"], 5.0)
    speech = sum(syllables(s["text"]) / sps for s in shots)
    return R["lead"] + speech + R["gap"] * (len(shots) - 1) + R["outro"]


def example(lang):
    ex = STUDIO / "examples" / ("pirotexnika_uz" if lang == "uz" else "pirotexnika_ru") / "scenario.json"
    try:
        s = load(ex)
        return {"clips": s["clips"], "shots": s["shots"], "facts": s.get("facts", [])}
    except (OSError, ValueError, KeyError):
        return None


def brief(root, j):
    s = j["spec"]
    c = cfg(root)
    chars = load_yaml("characters.yaml")
    shots = load_yaml("shots.yaml")
    motions = {k: " ".join(v["prompt"].split())[:160] for k, v in load_yaml("motions.yaml").items() if isinstance(v, dict)}
    lang = s["lang"]
    b = budget(s, c)
    return dict(
        task=s, passages=[dict(index=n, **p) for n, p in enumerate(j["sources"], 1)], budget=b,
        audience=audience(s), format_guide=load_yaml("formats.yaml").get(s["format"]),
        style_guide={k: v for k, v in load_yaml("styles.yaml")[s["style"]].items() if k in ("name", "prompt", "for")},
        speakers={k: dict(name=chars.get(k, {}).get("name_" + lang) or chars.get(k, {}).get("name_ru", "Рассказчик"),
                          role=chars.get(k, {}).get("role", "спокойный рассказчик за кадром, даёт факты закона"))
                  for k in voiced(c, lang)},
        on_screen_characters={k: dict(name=v.get("name_" + lang) or v.get("name_ru"), role=v.get("role"))
                              for k, v in chars.items() if isinstance(v, dict)},
        shots_catalog={k: v.get("use", "") for k, v in shots.items() if isinstance(v, dict)},
        motions_catalog=motions, sfx=list(SFX), caption_colors={"w": "обычный", "y": "факт, число, статья",
                                                                "r": "запрет, нарушение", "g": "можно, решение"},
        example=example(lang),
        rules=(
            "Write the whole video script yourself and submit it with scenario before opening Flow. "
            f"Exactly {s['scenes']} clips c1..c{s['scenes']} (each is one paid 8-second Flow video). "
            f"{b['shots'][0]}-{b['shots'][1]} shots, spoken text in total {b['words'][0]}-{b['words'][1]} words "
            f"so the video lasts about {target(s)} seconds. "
            "Hybrid: the narrator calmly states the law; characters (Abror reacts emotionally, Malika asks as the viewer) "
            "make it lively. Hook in the first 2 seconds, one clear call to action at the end. "
            "Each line 2-5 seconds (at most 14 words); captions in chunks of 1-4 words with colours. "
            "Spell numbers in words in text (the voice reads text), digits are fine in captions. "
            "Every clip is used by 1-2 shots: the first at offset 0, the second at offset 4; neighbouring shots use "
            "different clips and different camera angles. A clip has one place, one clear action and concrete props; "
            "no readable text in frames. "
            "Legal statements only from passages: each fact cites a passage by index with an exact verbatim quote "
            "(source language), and every shot that states the law lists its fact ids. Numbers must appear in the "
            "passages. If the passages do not support the topic, call attention instead of inventing. "
            f"Text and captions in {'Uzbek (Latin script, oʻ gʻ with ʻ)' if lang == 'uz' else 'Russian'}."))


def scenario(root, j, args):
    spec = j["spec"]
    c = cfg(root)
    lang = spec["lang"]
    title = field(args, "title", 160)
    chars = load_yaml("characters.yaml")
    shots_lib = load_yaml("shots.yaml")
    motions = {k for k, v in load_yaml("motions.yaml").items() if isinstance(v, dict)}
    emotions = load_yaml("voices.yaml")["emotions"]
    speakers = voiced(c, lang)
    refs = j["sources"]
    n = spec["scenes"]
    raw_clips = args.get("clips")
    if not isinstance(raw_clips, list) or [x.get("id") for x in raw_clips if isinstance(x, dict)] != [f"c{i}" for i in range(1, n + 1)]:
        raise ValueError(f"Нужно ровно {n} клипов по порядку: " + ", ".join(f"c{i}" for i in range(1, n + 1)))
    clips = {}
    for x in raw_clips:
        cid = x["id"]
        shot, motion = x.get("shot"), x.get("motion")
        if shot not in shots_lib:
            raise ValueError(f"{cid}: ракурс {shot} не из shots_catalog")
        if motion not in motions:
            raise ValueError(f"{cid}: движение {motion} не из motions_catalog")
        who = x.get("characters") or []
        if not isinstance(who, list) or len(who) > 3 or any(ch not in chars for ch in who):
            raise ValueError(f"{cid}: персонажи только из on_screen_characters, не больше трёх")
        props = x.get("props") or []
        if not isinstance(props, list) or not 1 <= len(props) <= 5 or not all(isinstance(p, str) and 2 <= len(p) <= 80 for p in props):
            raise ValueError(f"{cid}: props — от 1 до 5 предметов")
        clips[cid] = dict(shot=shot, characters=who, scene=field(x, "scene", 700, 20), props=props,
                          action=field(x, "action", 300, 5), motion=motion)
    raw_facts = args.get("facts")
    if not isinstance(raw_facts, list) or not 1 <= len(raw_facts) <= 8:
        raise ValueError("Нужно от 1 до 8 фактов с цитатами")
    facts = []
    for k, x in enumerate(raw_facts, 1):
        i = x.get("source")
        quote = field(x, "quote", 2000, 12)
        if type(i) is not int or not 1 <= i <= len(refs) or plain(quote) not in plain(refs[i - 1]["text"]):
            raise ValueError(f"Факт f{k}: цитата должна дословно совпадать с выдержкой {i}")
        facts.append(dict(id=f"f{k}", claim=field(x, "claim", 240), quote=quote, article=refs[i - 1]["article"],
                          source=refs[i - 1]["url"]))
    fids = {f["id"] for f in facts}
    raw_shots = args.get("shots")
    lo, hi = budget(spec, c)["shots"]
    if not isinstance(raw_shots, list) or not lo <= len(raw_shots) <= hi + 2:
        raise ValueError(f"Нужно {lo}-{hi} шотов для ролика {target(spec)} с")
    shots, use = [], {k: 0 for k in clips}
    prev = None
    for k, x in enumerate(raw_shots, 1):
        sid = f"s{k:02d}"
        sp, emo = x.get("speaker"), x.get("emotion", "serious")
        if sp not in speakers:
            raise ValueError(f"{sid}: говорить могут только {', '.join(speakers)} (для них есть голоса VoiceLab)")
        if emo not in emotions:
            raise ValueError(f"{sid}: эмоция {emo} не из списка: {', '.join(emotions)}")
        text = field(x, "text", 170, 2)
        if words(text) > 16:
            raise ValueError(f"{sid}: реплика длиннее 16 слов, разбейте на два шота")
        caps = x.get("captions")
        if not isinstance(caps, list) or not 1 <= len(caps) <= 5 or not all(
                isinstance(p, list) and len(p) == 2 and isinstance(p[0], str) and 1 <= len(p[0]) <= 40 and p[1] in COLORS for p in caps):
            raise ValueError(f"{sid}: captions — 1-5 кусков [текст, цвет w|y|r|g], до 40 символов")
        cid = x.get("clip")
        if cid not in clips:
            raise ValueError(f"{sid}: клип {cid} не объявлен")
        off = x.get("offset", 0)
        if not isinstance(off, (int, float)) or not 0 <= off <= 6:
            raise ValueError(f"{sid}: offset от 0 до 6 секунд")
        if (cid, off) == prev:
            raise ValueError(f"{sid}: тот же кусок клипа, что у предыдущего шота")
        prev = (cid, off)
        sfx = x.get("sfx") or []
        fl = x.get("facts") or []
        if not isinstance(sfx, list) or any(e not in SFX for e in sfx) or len(sfx) > 2:
            raise ValueError(f"{sid}: звуки только из списка sfx, не больше двух")
        if not isinstance(fl, list) or any(f not in fids for f in fl):
            raise ValueError(f"{sid}: ссылка на неизвестный факт")
        use[cid] += 1
        shots.append(dict(id=sid, speaker=sp, emotion=emo, text=text, captions=caps,
                          visual={"clip": cid, "offset": float(off)}, sfx=sfx, facts=fl))
    unused = [k for k, v in use.items() if not v]
    if unused:
        raise ValueError("Клипы без шотов: " + ", ".join(unused) + ". Каждый оплаченный клип должен попасть в ролик.")
    if any(v > 3 for v in use.values()):
        raise ValueError("Один клип используется больше трёх раз")
    if not any(sh["facts"] for sh in shots) or fids - {f for sh in shots for f in sh["facts"]}:
        raise ValueError("Каждый факт должен прозвучать хотя бы в одном шоте (поле facts шота)")
    est, d = estimate(shots, spec, c), target(spec)
    if not 0.9 * d <= est <= 1.07 * d:
        total, syl, b = sum(words(sh["text"]) for sh in shots), sum(syllables(sh["text"]) for sh in shots), budget(spec, c)
        raise ValueError(f"Сценарий звучит ~{est:.1f} с ({syl} слогов, {total} слов), а заказан ролик {d} с: "
                         f"нужно {b['syllables'][0]}-{b['syllables'][1]} слогов (гласных), это около "
                         f"{b['words'][0]}-{b['words'][1]} слов. Длинные юридические слова сокращайте или переносите в субтитры.")
    src = refs[raw_facts[0]["source"] - 1]
    label = "MANBA: LEX.UZ" if lang == "uz" else "ИСТОЧНИК: LEX.UZ"
    s = dict(id="modda", title=title, lang=lang, style=spec["style"], format=spec["format"], aspect=spec["aspect"],
             audience=audience(spec), duration=d, clips=clips, shots=shots, facts=facts,
             source={"url": src["url"], "plate": [label, src["title"][:60], (src["article"] + " · " + src["as_of"])[:60]]})
    if (root / "scenario.json").exists() and load(root / "scenario.json") != s and (
            any((root / "clips").glob("*.mp4")) or any(root.glob("flow-attempt-*"))):
        raise ValueError("У проекта уже есть клипы для прежнего сценария.")
    law = "\n\n".join(r["text"] + "\n" + r["article"] + " " + r["title"] + " " + r["as_of"] for r in refs)
    errors = [msg for level, msg in scriptwriter.check(s, law, c) if level == "error"]
    if errors:
        raise ValueError(" · ".join(errors)[:430])
    project = Project(root)
    (root / "law.txt").write_text(law, encoding="utf8")
    write_json(root / "scenario.json", s)
    prompts.write_tasks(project, s)
    flow = []
    for cid in clips:
        ings = [r.name for r in prompts.refs(s, cid)]
        flow.append(dict(clip=cid, already_attached=(root / "clips" / (cid + ".mp4")).exists(), ingredients=ings,
                         frame_prompt=prompts.frame_prompt(s, cid), video_prompt=prompts.motion_prompt(s, cid)))
    write_json(root / "progress.json", {"phase": "flow", "message": f"Сценарий на ~{est:.0f} с прошёл проверку цитат. "
                                        f"Дальше кадры и {n} клипов."})
    return {"estimated_seconds": round(est, 1), "flow_project": FLOW_PROJECT, "aspect": spec["aspect"],
            "settings": "Image: Nano Banana, aspect as ordered, 1 output; attach the listed ingredients that exist in the "
                        "project library (they are the uploaded files of the same name). Video: Frames to video, start frame = "
                        "this clip's image, Veo 3.1 Lite, 8 s, 1 output, Return silent videos ON.",
            "clips": flow, "paid_video_generations": n, "retakes_allowed": 2}


def start_clip(root, args):
    clip = field(args, "clip", 3)
    if clip not in load(root / "scenario.json")["clips"]:
        raise ValueError("Неизвестная сцена.")
    made = sorted(root.glob("flow-attempt-*"))
    mine = [p for p in made if p.name == "flow-attempt-" + clip or p.name.startswith("flow-attempt-" + clip + "-")]
    n = len(load(root / "scenario.json")["clips"])
    if mine:
        reason = (args.get("retake_reason") or "").strip()
        if not reason:
            return {"authorized": False, "reason": "Попытка уже зарезервирована. Проверьте результат в Flow; "
                                                    "переснять можно только брак (текст, цифры, сломанный персонаж) с retake_reason."}
        if len(mine) >= 2 or len(made) >= n + 2:
            return {"authorized": False, "reason": "Лимит пересъёмок исчерпан. Используйте лучший дубль или attention."}
        name = f"flow-attempt-{clip}-{len(mine) + 1}"
    else:
        name = "flow-attempt-" + clip
        reason = ""
    try:
        with (root / name).open("x") as f:
            f.write(json.dumps({"t": time.time(), "retake": reason}, ensure_ascii=False))
    except FileExistsError:
        return {"authorized": False, "reason": "Попытка уже зарезервирована. Проверьте результат в Flow."}
    return {"authorized": True, "outputs": 1}


def attach(root, j, args):
    clip = field(args, "clip", 3)
    s = load(root / "scenario.json")
    if clip not in s["clips"]:
        raise ValueError("Неизвестная сцена.")
    dest = root / "clips" / (clip + ".mp4")
    retake = any(root.glob(f"flow-attempt-{clip}-*"))
    if dest.exists() and not (retake and args.get("replace")):
        return {"attached": clip}
    dirs = [Path(os.getenv("MODDA_DOWNLOAD_DIR", Path.home() / "Downloads")).resolve(), (Path.home() / "Desktop").resolve()]
    name = args.get("filename") or ""
    if name and (Path(name).name != name or not name.lower().endswith(".mp4")):
        raise ValueError("Передайте только имя MP4 из «Загрузок» или с Рабочего стола.")
    receipts = [load(r) for r in root.glob("receipt-*.json")]
    used, used_sha = {r["name"] for r in receipts}, {r["sha256"] for r in receipts}
    (root / "clips").mkdir(exist_ok=True)
    part = dest.with_suffix(".part")

    def candidates():
        # Без имени берём и незавершённую загрузку браузера (*.tmp / *.crdownload): Chromium держит в ней
        # готовый файл, пока открыто окно «Сохранить как», и удаляет её при отмене окна.
        paths = [d / name for d in dirs] if name else [f for d in dirs for p in ("*.mp4", "*.tmp", "*.crdownload")
                                                         for f in d.glob(p)]
        out = []
        for f in paths:
            try:
                st = f.stat()
                if (f.is_file() and f.resolve().parent in dirs and not f.is_symlink() and f.name not in used
                        and st.st_mtime >= j["started"] - 2 and 512 < st.st_size <= 180 * 1024 ** 2):
                    out.append((st.st_mtime, f))
            except OSError:
                continue
        return [f for _, f in sorted(out, key=lambda x: x[0], reverse=True)]

    def grab(f):
        # Копируем сразу (временный файл может исчезнуть) и проверяем уже копию.
        try:
            size = f.stat().st_size
            time.sleep(1.5)
            if f.stat().st_size != size:
                return None, "загрузка ещё идёт"
            shutil.copyfile(f, part)
        except OSError:
            return None, "файл исчез"
        probe = subprocess.run(["ffprobe", "-v", "error", "-f", "mov", "-protocol_whitelist", "file", "-show_entries",
                                "stream=codec_type,width,height:format=duration", "-of", "json", str(part)],
                               capture_output=True, timeout=20)
        meta = json.loads(probe.stdout or b"{}")
        vid = next((x for x in meta.get("streams", []) if x.get("codec_type") == "video"), {})
        if not vid or not 3 <= float(meta.get("format", {}).get("duration", 0)) <= 12:
            return None, "нужен видеоклип Flow длительностью 3-12 секунд"
        if (vid["width"] > vid["height"]) != (j["spec"]["aspect"] == "16:9"):
            return None, "ориентация клипа отличается от заказа"
        with part.open("rb") as file:
            sha = hashlib.file_digest(file, "sha256").hexdigest()
        if sha in used_sha:
            return None, "этот файл уже прикреплён к другой сцене"
        return sha, ""

    deadline = time.time() + (5 if name else 75)
    sha, why, f = None, "", None
    while time.time() < deadline and not sha:
        found = candidates()
        if name and len(found) > 1:
            raise ValueError("Несколько файлов с этим именем. Подходящие файлы: " + ", ".join(x.name for x in found[:8]))
        for f in found[:3]:
            sha, why = grab(f)
            if sha:
                break
        if not sha:
            time.sleep(1)
    if not sha:
        part.unlink(missing_ok=True)
        raise ValueError("Не нашёл скачанный клип этого задания"
                         + (f" ({why})" if why else "") + ". Скачайте клип (Download → 720p) и сразу снова вызовите attach.")
    part.replace(dest)
    write_json(root / ("receipt-" + clip + ".json"), {"name": f.name, "sha256": sha})
    done = sum((root / "clips" / (k + ".mp4")).is_file() for k in s["clips"])
    write_json(root / "progress.json", {"phase": "flow", "message": f"Клипы готовы: {done} из {len(s['clips'])}."})
    return {"attached": clip, "clips_ready": done, "clips_total": len(s["clips"])}


def main():
    root = Path.cwd()
    j = load(root / "brief.json")
    op = sys.argv[1]
    args = json.load(sys.stdin)
    if op == "brief":
        res = brief(root, j)
    elif op == "scenario":
        res = scenario(root, j, args)
    elif op == "start_clip":
        res = start_clip(root, args)
    elif op == "attach":
        res = attach(root, j, args)
    elif op == "finish":
        sc = load(root / "scenario.json")
        if not sc["clips"] or not all((root / "clips" / (k + ".mp4")).is_file() and (root / ("receipt-" + k + ".json")).exists()
                                      for k in sc["clips"]):
            raise ValueError("Ещё не все клипы зарегистрированы.")
        write_json(root / "ready.json", {"ready": True})
        res = {"render_queued": True}
    elif op == "attention":
        write_json(root / "attention.json", {"message": field(args, "message", 420)})
        res = {"recorded": True}
    else:
        raise ValueError("Неизвестная операция.")
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e)[:700]}, ensure_ascii=False))
        sys.exit(1)
