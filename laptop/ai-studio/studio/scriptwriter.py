"""Сценарий из закона: промпт -> LLM -> scenario.json -> фактчек."""
import difflib
import json
import re

from . import llm
from .config import load_text, load_yaml

LANG_NAME = {"ru": "русский", "uz": "узбекский (латиница, oʻzbek tili)", "en": "английский"}


# ---------- сборка промпта ----------
def build_prompts(cfg, law_text, law_url, lang="ru", audience="teens", fmt="explainer",
                  style=None, topic="", fake_text=""):
    aud = load_yaml("audiences.yaml")[audience]
    form = load_yaml("formats.yaml")[fmt]
    chars = load_yaml("characters.yaml")
    shots = load_yaml("shots.yaml")
    motions = {k: v for k, v in load_yaml("motions.yaml").items() if k != "suffix"}
    style = style or aud["style"]
    char_keys = ["narrator"] + aud.get("characters", [])
    cb = ["- narrator: рассказчик за кадром, в кадре не появляется"]
    for k in aud.get("characters", []):
        c = chars[k]
        cb.append(f"- {k} ({c['name_ru']} / {c['name_uz']}): {c['role']}")
    cb.append("Можно добавить: " + ", ".join(k for k in chars if k not in char_keys))
    n_min, n_max = aud["shots"]
    system = load_text("script_system.md")
    repl = {
        "{wps}": str(aud["wps"]), "{lang_name}": LANG_NAME.get(lang, lang), "{lang}": lang,
        "{audience}": audience, "{audience_label}": aud["label"], "{audience_rules}": aud["rules"],
        "{format}": fmt, "{format_label}": form["label"], "{format_structure}": form["structure"],
        "{format_rules}": form.get("rules", ""), "{duration}": str(aud["duration"]),
        "{shots_min}": str(n_min), "{shots_max}": str(n_max),
        "{clips_hint}": f"{max(4, n_min // 2)}-{max(6, n_max // 2 + 1)}",
        "{characters_block}": "\n".join(cb),
        "{shots_list}": "\n".join(f"- {k}: {v['use']}" for k, v in shots.items()),
        "{motions_list}": ", ".join(motions),
        "{style}": style, "{law_url}": law_url,
    }
    for k, v in repl.items():
        system = system.replace(k, v)
    fake_block = ""
    if fake_text:
        fake_block = ("ФЕЙК, КОТОРЫЙ НАДО ОПРОВЕРГНУТЬ (пересказ поста из соцсетей, автора не называть):\n"
                      f"«{fake_text.strip()}»")
    user = load_text("script_user.md")
    for k, v in {"{topic}": topic or "", "{fake_block}": fake_block, "{brv}": f"{cfg['brv']:,}".replace(",", " "),
                 "{law_url}": law_url, "{law_text}": law_text[:120000]}.items():
        user = user.replace(k, v)
    return system, user


def write(project, cfg, law_url, lang="ru", audience="teens", fmt="explainer", style=None,
          topic="", fake_text="", llm_check=True):
    law_text = project.law()
    if not law_text:
        raise RuntimeError("Нет law.txt. Сначала: python -m studio law <проект> <ссылка lex.uz>")
    system, user = build_prompts(cfg, law_text, law_url, lang, audience, fmt, style, topic, fake_text)
    (project.sub("tasks") / "script_prompt_system.md").write_text(system, encoding="utf-8")
    (project.sub("tasks") / "script_prompt_user.md").write_text(user, encoding="utf-8")
    raw = llm.chat(system, user, cfg)
    (project.sub("work") / "script_raw.txt").write_text(raw, encoding="utf-8")
    scn = llm.extract_json(raw)
    scn.setdefault("lang", lang)
    scn.setdefault("audience", audience)
    scn.setdefault("format", fmt)
    scn.setdefault("style", style or load_yaml("audiences.yaml")[audience]["style"])
    project.save(scn)
    report = check(scn, law_text, cfg)
    if llm_check:
        report += llm_factcheck(scn, law_text, cfg)
    return scn, report


# ---------- фактчек ----------
def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[ʻʼ‘’`´']", "'", s)
    s = re.sub(r"[«»\"“”„]", "", s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = s.replace("­", "")
    return re.sub(r"\s+", " ", s).strip()


def _find_quote(quote, law_n):
    q = norm(quote)
    if q and q in law_n:
        return 1.0
    # нечёткий поиск: лучшее окно той же длины
    best, L = 0.0, len(q)
    if not L:
        return 0.0
    step = max(1, L // 4)
    for i in range(0, max(1, len(law_n) - L + 1), step):
        r = difflib.SequenceMatcher(None, q, law_n[i:i + L]).quick_ratio()
        if r > best - 0.02:
            r = difflib.SequenceMatcher(None, q, law_n[i:i + L]).ratio()
            best = max(best, r)
    return best


NUM = re.compile(r"\d+(?:[.,]\d+)?")


def check(scn, law_text, cfg=None):
    """Локальные проверки без LLM. Возвращает список (уровень, сообщение)."""
    rep = []
    law_n = norm(law_text)
    shots_lib = load_yaml("shots.yaml")
    motions = load_yaml("motions.yaml")
    chars = load_yaml("characters.yaml")
    facts = {f["id"]: f for f in scn.get("facts", [])}
    for f in facts.values():
        score = _find_quote(f.get("quote", ""), law_n) if law_n else 0
        if not law_n:
            rep.append(("warn", f"{f['id']}: нет law.txt, цитату не проверить"))
        elif score >= 0.999:
            rep.append(("ok", f"{f['id']}: цитата найдена в законе ({f.get('article', '')})"))
        elif score >= 0.85:
            rep.append(("warn", f"{f['id']}: цитата найдена неточно ({score:.0%}), проверьте вручную: {f['quote'][:80]}"))
        else:
            rep.append(("error", f"{f['id']}: цитаты НЕТ в законе: {f.get('quote', '')[:100]}"))
    allowed_nums = set(NUM.findall(" ".join(f.get("claim", "") for f in facts.values())))
    allowed_nums |= set(NUM.findall(law_text))
    allowed_nums |= set(NUM.findall(" ".join(scn.get("source", {}).get("plate", []))))
    if cfg:  # пересчёт БРВ из facts в млн сум: 15 БРВ -> 6 млн
        brv = cfg["brv"]
        allowed_nums |= {str(brv), str(brv // 1000), "000"}  # 1 БРВ = 412 000 сум
        for n in list(NUM.findall(" ".join(f.get("claim", "") for f in facts.values()))):
            k = float(n.replace(",", "."))
            allowed_nums |= {str(round(brv * k / 1e6)), str(int(brv * k / 1e6)), f"{brv * k:,.0f}".replace(",", " ")}
    allowed_nums |= set(NUM.findall(scn.get("fake", "")))  # в опровержении можно назвать цифру из фейка
    clips = scn.get("clips", {})
    for cid, c in clips.items():
        if c.get("shot") and c["shot"] not in shots_lib:
            rep.append(("warn", f"клип {cid}: неизвестный ракурс {c['shot']}"))
        if c.get("motion") and c["motion"] not in motions:
            rep.append(("warn", f"клип {cid}: неизвестное движение {c['motion']}"))
        if not c.get("props") and not c.get("frame_prompt"):
            rep.append(("warn", f"клип {cid}: нет props, предметы закона могут не попасть в кадр"))
        for ch in c.get("characters", []):
            if ch not in chars:
                rep.append(("warn", f"клип {cid}: неизвестный персонаж {ch}"))
    prev = None
    for s in scn.get("shots", []):
        sid = s.get("id", "?")
        v = s.get("visual", {})
        ref = v.get("clip") or v.get("image")
        if ref and ref not in clips and not v.get("file"):
            rep.append(("error", f"{sid}: ссылка на несуществующий клип {ref}"))
        if ref and ref == prev and not v.get("offset"):
            rep.append(("warn", f"{sid}: тот же клип, что у предыдущего шота, без offset"))
        prev = ref
        txt = s.get("text", "")
        if "—" in txt or "–" in txt:
            rep.append(("warn", f"{sid}: длинное тире в тексте, лучше запятая"))
        caps = " ".join(c[0] for c in s.get("captions", []))
        for n in set(NUM.findall(txt + " " + caps)):
            if n not in allowed_nums:
                rep.append(("error", f"{sid}: число {n} не найдено ни в законе, ни в facts"))
        words = len(re.findall(r"\w+", txt))
        if words > 14:
            rep.append(("warn", f"{sid}: длинная реплика ({words} слов), шот будет дольше 5 секунд"))
        for fid in s.get("facts", []):
            if fid not in facts:
                rep.append(("warn", f"{sid}: ссылка на неизвестный факт {fid}"))
    if not any(l == "error" for l, _ in rep):
        rep.append(("ok", "структура и числа в порядке"))
    return rep


def llm_factcheck(scn, law_text, cfg):
    system = load_text("factcheck_system.md").replace("{brv}", str(cfg["brv"]))
    user = (f"=== ЗАКОН ===\n{law_text[:120000]}\n=== СЦЕНАРИЙ ===\n"
            + json.dumps({"facts": scn.get("facts"), "shots": [{k: s.get(k) for k in ("id", "text", "captions")}
                                                                for s in scn.get("shots", [])]},
                         ensure_ascii=False, indent=1))
    try:
        res = llm.extract_json(llm.chat(system, user, cfg))
    except Exception as e:  # noqa
        return [("warn", f"LLM-фактчек не выполнен: {e}")]
    if res.get("ok"):
        return [("ok", "LLM-фактчек: ошибок не найдено")]
    return [("error", f"LLM-фактчек {i.get('shot')}: {i.get('problem')} | закон: {i.get('law_says', '')[:120]} "
                      f"| исправить: {i.get('fix')}") for i in res.get("issues", [])]


def print_report(rep):
    icon = {"ok": "  OK ", "warn": "  !  ", "error": " XXX "}
    for lvl, msg in rep:
        print(icon[lvl], msg)
    return not any(l == "error" for l, _ in rep)


# ---------- вердикт по посту (вход для бота и детектора) ----------
def verdict(post_text, law_text, cfg):
    from .config import load_text as _lt
    user = f"=== ПОСТ ===\n{post_text.strip()}\n=== ЗАКОН ===\n{law_text[:120000]}"
    res = llm.extract_json(llm.chat(_lt("verdict_system.md"), user, cfg))
    law_n = norm(law_text)
    for c in res.get("claims", []):
        q = c.get("law_quote", "")
        c["quote_found"] = bool(q) and _find_quote(q, law_n) >= 0.85
    return res
