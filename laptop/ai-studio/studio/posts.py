"""Посты для Telegram / Instagram / Facebook: карусель карточек + текст поста на RU или UZ.

post.json:
{
  "id": "pirotexnika_post", "lang": "ru", "theme": "official", "format": "4x5",   # 4x5 (1080x1350), 1x1, 16x9, 9x16
  "source_line": "Источник: lex.uz · ЗРУ-1111",
  "facts": [...],                      # как в scenario.json, проверяются командой check
  "slides": [ {"type": "cover", ...}, ... ],
  "caption": "Текст поста с хэштегами",
  "cover_image": "frames/cover.png"    # необязательно: картинка из генератора (Nano Banana) на обложку
}
Типы слайдов: cover, number, compare, list, quote, outro (поля в README, раздел 14).
"""
import json

from PIL import Image, ImageDraw

from . import llm
from .config import load_text, load_yaml
from .infographic import Ctx, KIND, _font, icon, mix, wrap

SIZES = {"4x5": (1080, 1350), "1x1": (1080, 1080), "16x9": (1600, 900), "9x16": (1080, 1920)}
from .infographic import UI


def _chrome(d, cx, W, H, i, n, lang):
    ui = UI.get(lang, UI["ru"])
    if cx.t.get("header") == "bar":
        d.rectangle((0, 0, W, 110), fill=cx.c("header_bg")); d.rectangle((0, 110, W, 115), fill=cx.c("header_line"))
        cx.text(d, (70, 34), cx.brand, cx.body(38, 800), (255, 255, 255), bg=cx.c("header_bg"))
        cx.text(d, (W - 70 - d.textlength(f"{i}/{n}", font=cx.body(32, 600)), 40), f"{i}/{n}", cx.body(32, 600), (190, 205, 230), bg=cx.c("header_bg"))
    else:
        cx.text(d, (70, 50), cx.brand, _font("sans", 34, 900) if cx.t.get("head") != "serif" else cx.head(38, 700), cx.c("ink"))
        cx.text(d, (W - 70 - d.textlength(f"{i}/{n}", font=cx.body(32, 600)), 54), f"{i}/{n}", cx.body(32, 600), cx.c("muted"))
        if cx.t.get("header") == "double":
            d.line((70, 108, W - 70, 108), fill=cx.c("ink"), width=4); d.line((70, 117, W - 70, 117), fill=cx.c("ink"), width=1)
        elif cx.t.get("header") != "none":
            d.line((70, 110, W - 70, 110), fill=cx.c("rule"), width=2)
    if cx.t.get("side_bar"):
        d.rectangle((0, 0, 18, H), fill=cx.c("side_bar"))
    d.line((70, H - 110, W - 70, H - 110), fill=cx.c("rule"), width=2)
    for j, l in enumerate(wrap(d, cx.source, cx.body(26, 400), W - 140)[:2]):
        cx.text(d, (70, H - 92 + j * 32), l, cx.body(26, 400), cx.c("muted"))
    if i < n:
        sw = ui["swipe"] + "  →"
        f = cx.body(28, 700)
        cx.text(d, (W - 70 - d.textlength(sw, font=f), H - 160), sw, f, cx.c("accent"))


def _label(d, cx, x, y, s):
    if not s:
        return y
    d.rectangle((x, y + 6, x + 6, y + 32), fill=cx.c("accent"))
    cx.text(d, (x + 20, y), s.upper(), cx.body(28, 700), cx.c("accent"), spacing=3)
    return y + 70


def _lines(d, cx, x, y, text, f, col, mw, gap=1.12):
    for l in wrap(d, text, f, mw):
        cx.text(d, (x, y), l, f, col); y += int(f.size * gap)
    return y


def slide(img, d, cx, s, W, H, lang):
    x, mw = 70, W - 140
    y = _label(d, cx, x, 170, s.get("label", ""))
    t = s["type"]
    if t == "cover":
        if s.get("image"):
            ph = Image.open(s["image"]).convert("RGB")
            ih = int(H * 0.42)
            ph = ph.resize((W, int(ph.height * W / ph.width)))
            top = max(0, (ph.height - ih) // 2)
            img.paste(ph.crop((0, top, W, top + ih)), (0, H - 110 - ih - 10))
        size = s.get("size", 104 if W <= 1080 else 110)
        f = cx.head(size, 900)
        for k, ln in enumerate(s["lines"]):
            col = cx.c("accent") if k in s.get("accent", [len(s["lines"]) - 1]) else cx.c("ink")
            y = _lines(d, cx, x - 4, y, ln, f, col, mw)
        if s.get("sub"):
            y = _lines(d, cx, x, y + 30, s["sub"], cx.body(42, 500), cx.c("muted"), mw)
        if s.get("icon") and not s.get("image"):
            sz = min(360, H - 110 - y - 80)
            if sz > 140:
                icon(img, cx, s["icon"], W - 70 - sz, H - 160 - sz - 40, sz)
    elif t == "number":
        f = cx.num(170 if W <= 1080 else 190, 900)
        y = _lines(d, cx, x - 6, y, s["value"], f, cx.c("ink"), mw, 1.0)
        cx.text(d, (x, y + 10), s.get("unit", ""), cx.head(64, 700), cx.c("muted")); y += 110
        d.rectangle((x, y, x + 160, y + 10), fill=cx.c("accent")); y += 60
        _lines(d, cx, x, y, s.get("note", ""), cx.body(44, 500), cx.c("ink"), mw)
    elif t == "compare":
        cx.text(d, (x, y), s["before"].get("caption", "").upper(), cx.body(30, 700), cx.c("muted"), spacing=3)
        f = cx.num(120, 900)
        w = cx.text(d, (x - 4, y + 40), s["before"]["value"], f, cx.c("muted"))
        d.line((x - 8, y + 40 + f.size * 0.62, x + w + 8, y + 40 + f.size * 0.62), fill=cx.c("neg"), width=10)
        y += 230
        cx.text(d, (x, y), s["after"].get("caption", "").upper(), cx.body(30, 700), cx.c("neg"), spacing=3)
        y = _lines(d, cx, x - 6, y + 30, s["after"]["value"], cx.num(160, 900), cx.c("neg"), mw, 1.05)
        if s.get("sub"):
            y += 30
            f2 = cx.head(52, 800)
            for l in wrap(d, s["sub"], f2, mw):
                tw = d.textlength(l, font=f2)
                d.rectangle((x - 6, y + 8, x + tw + 10, y + f2.size + 22), fill=cx.c("mark"))
                cx.text(d, (x, y), l, f2, cx.c("ink")); y += int(f2.size * 1.35)
        if s.get("tag"):
            f3 = cx.head(46, 800); tw = d.textlength(s["tag"], font=f3)
            d.rectangle((x, y + 30, x + tw + 50, y + 110), fill=cx.c("ink"))
            d.text((x + 25, y + 42), s["tag"], font=f3, fill=cx.c("bg"))
    elif t == "list":
        if s.get("title"):
            y = _lines(d, cx, x - 4, y, s["title"], cx.head(76, 900), cx.c("ink"), mw) + 30
        for it in s["items"]:
            col = KIND(cx, it.get("kind"))
            ic = it.get("icon") or ("check" if it.get("kind") == "pos" else "cross" if it.get("kind") == "neg" else None)
            if ic:
                icon(img, cx, ic, x, y - 6, 74)
            yy = _lines(d, cx, x + 100, y, it["text"], cx.body(44, 600), cx.c("ink"), mw - 100, 1.15)
            if it.get("note"):
                yy = _lines(d, cx, x + 100, yy, it["note"], cx.body(34, 400), col, mw - 100)
            y = max(yy, y + 90) + 30
    elif t == "quote":
        d.rectangle((x, y, x + 10, y + 10), fill=cx.c("accent"))
        f = _font("serif", 50, 500)
        yy = _lines(d, cx, x + 40, y, "«" + s["quote"] + "»", f, cx.c("ink"), mw - 40, 1.3)
        d.rectangle((x, y, x + 8, yy), fill=cx.c("accent"))
        _lines(d, cx, x + 40, yy + 40, s.get("article", ""), cx.body(38, 700), cx.c("muted"), mw - 40)
    elif t == "outro":
        f = cx.head(96, 900)
        for k, ln in enumerate(s["lines"]):
            if k == len(s["lines"]) - 1:
                for l in wrap(d, ln, f, mw):
                    tw = d.textlength(l, font=f)
                    d.rectangle((x - 8, y + f.size * 0.3, x + tw + 12, y + f.size * 1.08), fill=cx.c("mark"))
                    cx.text(d, (x - 4, y), l, f, cx.c("ink")); y += int(f.size * 1.15)
            else:
                y = _lines(d, cx, x - 4, y, ln, f, cx.c("ink"), mw)
        if s.get("sub"):
            _lines(d, cx, x, y + 40, s["sub"], cx.body(44, 500), cx.c("muted"), mw)


def render(project, cfg, post_file="post.json", theme=None, fmt=None):
    post = json.loads((project.dir / post_file).read_text(encoding="utf-8"))
    th = load_yaml("info_themes.yaml")[theme or post.get("theme", "official")]
    W, H = SIZES[fmt or post.get("format", "4x5")]
    cx = Ctx(th, post.get("source_line", "lex.uz"), post.get("brand", cfg["render"].get("brand", "MODDA AI")), 1)
    lang = post.get("lang", "ru")
    n = len(post["slides"])
    out_dir = project.sub("out") / f"{post.get('id', 'post')}_{lang}_{theme or post.get('theme', 'official')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i, s in enumerate(post["slides"], 1):
        s = dict(s)
        if s["type"] == "cover" and post.get("cover_image"):
            s["image"] = str(project.dir / post["cover_image"])
        img = Image.new("RGB", (W, H), cx.c("bg"))
        d = ImageDraw.Draw(img)
        slide(img, d, cx, s, W, H, lang)
        _chrome(d, cx, W, H, i, n, lang)
        p = out_dir / f"{i:02d}.png"
        img.save(p)
        files.append(p)
    (out_dir / "caption.txt").write_text(post.get("caption", ""), encoding="utf-8")
    # превью карусели одним листом
    th_w = 360
    tiles = [Image.open(f).resize((th_w, int(H * th_w / W))) for f in files]
    sheet = Image.new("RGB", (th_w * len(tiles) + 10 * (len(tiles) - 1), tiles[0].height), (255, 255, 255))
    for k, tl in enumerate(tiles):
        sheet.paste(tl, (k * (th_w + 10), 0))
    sheet.save(out_dir / "preview.jpg", quality=90)
    return out_dir, files


def write(project, cfg, lang="ru", theme="official", fmt="4x5", topic="", law_url="", post_file=None):
    """LLM пишет post.json по law.txt (и по fake.txt, если есть)."""
    law = project.law()
    if not law:
        raise RuntimeError("Нет law.txt: сначала python -m studio law <проект> <ссылка>")
    fake = (project.dir / "fake.txt").read_text(encoding="utf-8") if (project.dir / "fake.txt").exists() else ""
    system = load_text("post_system.md").replace("{lang}", lang).replace("{brv}", str(cfg["brv"]))
    user = (f"Тема: {topic or 'главное в законе для людей'}\nЯзык: {lang}\nСсылка: {law_url}\n"
            + (f"Опровергаем фейк: «{fake}»\n" if fake else "") + f"=== ЗАКОН ===\n{law[:120000]}")
    post = llm.extract_json(llm.chat(system, user, cfg))
    post.update({"lang": lang, "theme": theme, "format": fmt})
    name = post_file or ("post.json" if lang == "ru" else f"post_{lang}.json")
    (project.dir / name).write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    return name, post


def as_scenario(post):
    """Для фактчека: тексты слайдов как реплики."""
    shots = []
    for i, s in enumerate(post["slides"]):
        parts = []
        for k in ("label", "title", "sub", "value", "unit", "note", "quote", "article", "tag"):
            if isinstance(s.get(k), str):
                parts.append(s[k])
        parts += s.get("lines", [])
        for side in ("before", "after"):
            if s.get(side):
                parts.append(s[side].get("value", ""))
        for it in s.get("items", []):
            parts += [it.get("text", ""), it.get("note", "")]
        shots.append({"id": f"slide{i + 1}", "text": " ".join(p for p in parts if p), "captions": []})
    shots.append({"id": "caption", "text": post.get("caption", ""), "captions": []})
    return {"facts": post.get("facts", []), "clips": {}, "shots": shots, "source": {}}
