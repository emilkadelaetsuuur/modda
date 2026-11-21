"""Анимированная инфографика 16:9 без нейросети: сцены из scenario.json -> покадровый рендер -> MP4 с голосом.

scenario.json (kind = "infographic"):
{
  "id": "pirotexnika_info", "kind": "infographic", "lang": "ru", "theme": "official",
  "source_line": "Источник: lex.uz · Закон ЗРУ-1111",
  "shots": [
     {"id": "s01", "speaker": "narrator", "text": "реплика для озвучки (можно пусто = сцена без голоса)",
      "dur": 4.0,   # длина сцены без голоса
      "scene": {"type": "title", ...},  "sfx": ["stamp"]}
  ]
}
Типы сцен и их поля описаны в README (раздел «Инфографика») и в examples/pirotexnika_info/scenario.json.
"""
import json
import math
import subprocess

from PIL import Image, ImageDraw, ImageFont

from . import music, sfx
from .config import ASSETS, load_yaml

W, H, FPS = 1920, 1080, 30
UI = {
    "ru": {"tag": "Правовое просвещение", "swipe": "Листайте", "src": "Источник"},
    "uz": {"tag": "Huquqiy maʼrifat", "swipe": "Varaqlang", "src": "Manba"},
    "en": {"tag": "Legal literacy", "swipe": "Swipe", "src": "Source"},
}
FD = ASSETS / "fonts"
_fc = {}


def _font(kind, size, w=700):
    k = (kind, size, w)
    if k in _fc:
        return _fc[k]
    if kind == "serif":
        f = ImageFont.truetype(str(FD / "Lora-Variable.ttf"), size); f.set_variation_by_axes([min(700, max(400, w))])
    elif kind == "mono":
        f = ImageFont.truetype(str(FD / "DejaVuSansMono-Bold.ttf"), size)
    elif kind == "body":
        f = ImageFont.truetype(str(FD / "SourceSans3-Variable.ttf"), size); f.set_variation_by_axes([w])
    else:  # sans / sans_light / sans_thin (Roboto: weight, width)
        f = ImageFont.truetype(str(FD / "Roboto-Variable.ttf"), size)
        wd = 80 if kind == "sans" else 100
        ww = {"sans": w, "sans_light": min(w, 500), "sans_thin": 200}.get(kind, w)
        f.set_variation_by_axes([ww, wd])
    _fc[k] = f
    return f


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def eo(x):
    x = clamp(x); return 1 - (1 - x) ** 3


def eio(x):
    x = clamp(x); return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def mix(c1, c2, a):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * a) for i in range(3))


class Ctx:
    def __init__(self, theme, source_line, brand, total, lang="ru"):
        self.lang = lang
        self.t = {k: (tuple(v) if isinstance(v, list) else v) for k, v in theme.items()}
        self.source = source_line
        self.brand = brand
        self.T = total

    # шрифты по теме
    def head(self, size, w=900):
        return _font(self.t.get("head", "sans"), size, w)

    def num(self, size, w=900):
        return _font(self.t.get("num", "sans"), size, w)

    def body(self, size, w=400):
        return _font("body", size, w)

    def c(self, k):
        return self.t[k]

    def text(self, d, xy, s, f, col, a=1.0, spacing=0, bg=None):
        col = mix(bg or self.c("bg"), col, a)
        if not spacing:
            d.text(xy, s, font=f, fill=col); return d.textlength(s, font=f)
        x, y = xy
        for ch in s:
            d.text((x, y), ch, font=f, fill=col); x += d.textlength(ch, font=f) + spacing
        return x - xy[0]


def rise(t, t0, dur=0.45, dist=36):
    k = eo((t - t0) / dur); return k, (1 - k) * dist


# ---------- иконки ----------
def _icon(name, size, cx):
    big = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    w = h = size * 2
    INK, NEG, POS, MARK, PAPER = cx.c("ink"), cx.c("neg"), cx.c("pos"), cx.c("mark"), cx.c("paper")
    lw = int(w * 0.035)
    if name == "firecracker":
        x0, x1, y0, y1 = w * 0.36, w * 0.64, h * 0.30, h * 0.92
        d.rounded_rectangle((x0, y0, x1, y1), radius=int(w * 0.04), outline=INK, width=lw, fill=NEG)
        for k in (0.42, 0.78):
            y = y0 + (y1 - y0) * k; d.line((x0, y, x1, y), fill=INK, width=lw)
        d.line((w * 0.5, y0, w * 0.5, h * 0.18), fill=INK, width=lw)
        d.arc((w * 0.5, h * 0.06, w * 0.7, h * 0.30), 180, 270, fill=INK, width=lw)
    elif name == "rocket":
        c = w / 2
        body = [(c - w * 0.12, h * 0.75), (c - w * 0.12, h * 0.32), (c, h * 0.08), (c + w * 0.12, h * 0.32), (c + w * 0.12, h * 0.75)]
        d.polygon(body, fill=NEG); d.line(body + [body[0]], fill=INK, width=lw, joint="curve")
        d.polygon([(c - w * 0.12, h * 0.58), (c - w * 0.26, h * 0.80), (c - w * 0.12, h * 0.75)], fill=INK)
        d.polygon([(c + w * 0.12, h * 0.58), (c + w * 0.26, h * 0.80), (c + w * 0.12, h * 0.75)], fill=INK)
        d.line((c, h * 0.75, c, h * 0.97), fill=INK, width=lw)
    elif name == "battery":
        d.rounded_rectangle((w * 0.12, h * 0.30, w * 0.88, h * 0.86), radius=int(w * 0.04), fill=NEG, outline=INK, width=lw)
        for i in range(3):
            for j in range(2):
                x, y, r = w * (0.27 + i * 0.23), h * (0.45 + j * 0.26), w * 0.075
                d.ellipse((x - r, y - r, x + r, y + r), fill=PAPER, outline=INK, width=lw)
    elif name == "sparkler":
        d.line((w * 0.30, h * 0.95, w * 0.58, h * 0.42), fill=INK, width=lw)
        x, y = w * 0.62, h * 0.33
        for i in range(12):
            a = i * math.pi / 6; r0, r1 = w * 0.08, w * (0.24 if i % 2 == 0 else 0.17)
            d.line((x + r0 * math.cos(a), y + r0 * math.sin(a), x + r1 * math.cos(a), y + r1 * math.sin(a)), fill=POS, width=lw)
    elif name == "fountain":
        pts = [(w * 0.32, h * 0.95), (w * 0.68, h * 0.95), (w * 0.60, h * 0.66), (w * 0.40, h * 0.66)]
        d.polygon(pts, fill=POS); d.line(pts + [pts[0]], fill=INK, width=lw)
        for a in (-55, -28, 0, 28, 55):
            r = math.radians(a - 90)
            d.line((w * 0.5, h * 0.62, w * 0.5 + w * 0.36 * math.cos(r), h * 0.62 + h * 0.5 * math.sin(r)), fill=POS, width=lw)
    elif name == "doc":
        d.rectangle((w * 0.16, h * 0.06, w * 0.84, h * 0.94), fill=PAPER, outline=INK, width=lw)
        for i in range(7):
            y = h * (0.20 + i * 0.085)
            d.line((w * 0.28, y, w * (0.72 if i % 3 != 2 else 0.55), y), fill=INK if i == 0 else cx.c("rule"), width=lw)
        x, y, r = w * 0.66, h * 0.80, w * 0.11
        d.ellipse((x - r, y - r, x + r, y + r), outline=cx.c("accent"), width=lw)
    elif name in ("check", "cross"):
        col = POS if name == "check" else NEG
        d.ellipse((w * 0.1, h * 0.1, w * 0.9, h * 0.9), outline=col, width=lw * 2)
        if name == "check":
            d.line([(w * 0.3, h * 0.52), (w * 0.45, h * 0.66), (w * 0.72, h * 0.36)], fill=col, width=lw * 2, joint="curve")
        else:
            d.line((w * 0.33, h * 0.33, w * 0.67, h * 0.67), fill=col, width=lw * 2); d.line((w * 0.33, h * 0.67, w * 0.67, h * 0.33), fill=col, width=lw * 2)
    elif name == "shield":
        pts = [(w * 0.5, h * 0.08), (w * 0.86, h * 0.22), (w * 0.8, h * 0.6), (w * 0.5, h * 0.92), (w * 0.2, h * 0.6), (w * 0.14, h * 0.22)]
        d.polygon(pts, fill=cx.c("pos")); d.line(pts + [pts[0]], fill=INK, width=lw)
    elif name == "phone":
        d.rounded_rectangle((w * 0.28, h * 0.06, w * 0.72, h * 0.94), radius=int(w * 0.06), outline=INK, width=lw)
        for i in range(4):
            y = h * (0.3 + i * 0.1); d.line((w * 0.36, y, w * (0.64 - 0.06 * (i % 2)), y), fill=INK, width=lw)
    elif name == "car":
        d.rounded_rectangle((w * 0.10, h * 0.48, w * 0.90, h * 0.74), radius=int(w * 0.05), fill=cx.c("accent"), outline=INK, width=lw)
        d.polygon([(w * 0.26, h * 0.48), (w * 0.36, h * 0.30), (w * 0.66, h * 0.30), (w * 0.76, h * 0.48)], fill=PAPER, outline=INK)
        d.line([(w * 0.26, h * 0.48), (w * 0.36, h * 0.30), (w * 0.66, h * 0.30), (w * 0.76, h * 0.48)], fill=INK, width=lw)
        d.line((w * 0.51, h * 0.30, w * 0.51, h * 0.48), fill=INK, width=lw)
        for x in (0.28, 0.72):
            d.ellipse((w * x - w * 0.09, h * 0.66, w * x + w * 0.09, h * 0.84), fill=INK)
            d.ellipse((w * x - w * 0.04, h * 0.71, w * x + w * 0.04, h * 0.79), fill=PAPER)
    elif name == "traffic_light":
        d.rounded_rectangle((w * 0.34, h * 0.06, w * 0.66, h * 0.80), radius=int(w * 0.06), fill=INK)
        for k, col in enumerate((NEG, MARK, POS)):
            cy = h * (0.18 + k * 0.22)
            d.ellipse((w * 0.5 - w * 0.09, cy - w * 0.09, w * 0.5 + w * 0.09, cy + w * 0.09), fill=col if k == 0 else mix(INK, col, 0.35))
        d.rectangle((w * 0.47, h * 0.80, w * 0.53, h * 0.97), fill=INK)
    elif name == "money":
        for k in range(3):
            o = k * w * 0.05
            d.rounded_rectangle((w * 0.12 + o, h * 0.30 - o, w * 0.82 + o, h * 0.70 - o), radius=int(w * 0.03), fill=POS if k == 2 else PAPER, outline=INK, width=lw)
        d.ellipse((w * 0.47 - w * 0.09, h * 0.40 - w * 0.09 - w * 0.1, w * 0.47 + w * 0.09 + w * 0.1, h * 0.40 + w * 0.09), outline=PAPER, width=lw)
    elif name == "house":
        d.polygon([(w * 0.12, h * 0.48), (w * 0.5, h * 0.14), (w * 0.88, h * 0.48)], fill=cx.c("accent"), outline=INK)
        d.line([(w * 0.12, h * 0.48), (w * 0.5, h * 0.14), (w * 0.88, h * 0.48)], fill=INK, width=lw)
        d.rectangle((w * 0.22, h * 0.48, w * 0.78, h * 0.90), fill=PAPER, outline=INK, width=lw)
        d.rectangle((w * 0.44, h * 0.64, w * 0.58, h * 0.90), fill=INK)
    elif name == "calendar":
        d.rounded_rectangle((w * 0.14, h * 0.18, w * 0.86, h * 0.90), radius=int(w * 0.04), fill=PAPER, outline=INK, width=lw)
        d.rectangle((w * 0.14, h * 0.18, w * 0.86, h * 0.36), fill=NEG)
        for x in (0.32, 0.68):
            d.line((w * x, h * 0.10, w * x, h * 0.26), fill=INK, width=lw * 2)
        for r_ in range(3):
            for c_ in range(4):
                x, y = w * (0.24 + c_ * 0.16), h * (0.46 + r_ * 0.13)
                d.rectangle((x, y, x + w * 0.08, y + h * 0.07), fill=INK if (r_, c_) != (1, 2) else NEG)
    elif name == "gavel":
        d.line((w * 0.30, h * 0.70, w * 0.62, h * 0.38), fill=INK, width=lw * 2)
        d.polygon([(w * 0.50, h * 0.18), (w * 0.80, h * 0.48), (w * 0.70, h * 0.58), (w * 0.40, h * 0.28)], fill=cx.c("accent"), outline=INK)
        d.rectangle((w * 0.12, h * 0.82, w * 0.62, h * 0.90), fill=INK)
    elif name == "people":
        for k, (x, sc) in enumerate(((0.30, 0.8), (0.70, 0.8), (0.50, 1.0))):
            r = w * 0.09 * sc
            cy = h * (0.30 if k == 2 else 0.36)
            col = cx.c("accent") if k == 2 else cx.c("muted")
            d.ellipse((w * x - r, cy - r, w * x + r, cy + r), fill=col, outline=INK, width=lw)
            d.pieslice((w * x - r * 2, cy + r * 1.3, w * x + r * 2, cy + r * 5.3), 180, 360, fill=col, outline=INK, width=lw)
    elif name == "school":
        d.polygon([(w * 0.10, h * 0.40), (w * 0.5, h * 0.14), (w * 0.90, h * 0.40)], fill=PAPER, outline=INK)
        d.line([(w * 0.10, h * 0.40), (w * 0.5, h * 0.14), (w * 0.90, h * 0.40), (w * 0.10, h * 0.40)], fill=INK, width=lw)
        d.rectangle((w * 0.16, h * 0.40, w * 0.84, h * 0.88), fill=cx.c("accent"), outline=INK, width=lw)
        for x in (0.24, 0.40, 0.56, 0.72):
            d.rectangle((w * x, h * 0.50, w * (x + 0.08), h * 0.64), fill=PAPER)
        d.rectangle((w * 0.44, h * 0.70, w * 0.56, h * 0.88), fill=INK)
    elif name == "shop":
        d.rectangle((w * 0.14, h * 0.40, w * 0.86, h * 0.88), fill=PAPER, outline=INK, width=lw)
        for k in range(5):
            d.rectangle((w * (0.10 + k * 0.16), h * 0.24, w * (0.26 + k * 0.16), h * 0.40), fill=NEG if k % 2 == 0 else PAPER, outline=INK, width=lw)
        d.rectangle((w * 0.24, h * 0.52, w * 0.48, h * 0.88), fill=INK)
        d.rectangle((w * 0.56, h * 0.52, w * 0.78, h * 0.70), fill=cx.c("muted"))
    elif name == "card":
        d.rounded_rectangle((w * 0.10, h * 0.26, w * 0.90, h * 0.76), radius=int(w * 0.05), fill=cx.c("accent"), outline=INK, width=lw)
        d.rectangle((w * 0.10, h * 0.36, w * 0.90, h * 0.46), fill=INK)
        d.rounded_rectangle((w * 0.18, h * 0.54, w * 0.34, h * 0.66), radius=int(w * 0.02), fill=MARK)
    elif name == "warning":
        d.polygon([(w * 0.5, h * 0.10), (w * 0.92, h * 0.86), (w * 0.08, h * 0.86)], fill=MARK, outline=INK)
        d.line([(w * 0.5, h * 0.10), (w * 0.92, h * 0.86), (w * 0.08, h * 0.86), (w * 0.5, h * 0.10)], fill=INK, width=lw)
        d.line((w * 0.5, h * 0.36, w * 0.5, h * 0.62), fill=INK, width=lw * 2)
        d.ellipse((w * 0.46, h * 0.70, w * 0.54, h * 0.78), fill=INK)
    elif name == "clock":
        d.ellipse((w * 0.12, h * 0.12, w * 0.88, h * 0.88), fill=PAPER, outline=INK, width=lw)
        d.line((w * 0.5, h * 0.5, w * 0.5, h * 0.24), fill=INK, width=lw)
        d.line((w * 0.5, h * 0.5, w * 0.68, h * 0.58), fill=NEG, width=lw)
    return big.resize((size, size), Image.LANCZOS)


_icons = {}


def icon(img, cx, name, x, y, size, a=1.0):
    k = (name, size, id(cx))
    if k not in _icons:
        _icons[k] = _icon(name, size, cx)
    sp = _icons[k]
    if a < 1:
        sp = sp.copy(); sp.putalpha(sp.split()[3].point(lambda v: int(v * a)))
    img.paste(sp, (int(x), int(y)), sp)


# ---------- рамка ----------
def chrome(img, d, cx, t, num, total_n):
    hdr = cx.t.get("header", "line")
    x0 = 120
    if hdr == "bar":
        d.rectangle((0, 0, W, 130), fill=cx.c("header_bg")); d.rectangle((0, 130, W, 136), fill=cx.c("header_line"))
        cx.text(d, (x0, 42), cx.brand, cx.body(40, 800), (255, 255, 255), bg=cx.c("header_bg"))
        cx.text(d, (x0 + 260, 50), UI.get(cx.lang, UI["ru"])["tag"], cx.body(30, 400), (190, 205, 230), bg=cx.c("header_bg"))
        n = f"{num:02d} / {total_n:02d}"
        cx.text(d, (W - 120 - d.textlength(n, font=cx.body(30, 600)), 50), n, cx.body(30, 600), (190, 205, 230), bg=cx.c("header_bg"))
    elif hdr != "none":
        f = _font("mono", 28) if cx.t.get("num") == "mono" else (cx.head(34, 700) if cx.t.get("head") == "serif" else _font("sans", 30, 900))
        cx.text(d, (x0, 60), cx.brand, f, cx.c("ink"), spacing=2 if cx.t.get("head") != "serif" else 0)
        n = f"{num:02d} / {total_n:02d}"
        fn = cx.body(28, 600)
        cx.text(d, (W - 120 - d.textlength(n, font=fn), 66), n, fn, cx.c("muted"))
        if hdr == "double":
            d.line((120, 118, W - 120, 118), fill=cx.c("ink"), width=4); d.line((120, 128, W - 120, 128), fill=cx.c("ink"), width=1)
        else:
            d.line((120, 118, W - 120, 118), fill=cx.c("rule"), width=2)
    else:
        cx.text(d, (x0, 64), cx.brand, cx.body(30, 700), cx.c("ink"))
    if cx.t.get("side_bar"):
        d.rectangle((0, 0, 24, H), fill=cx.c("side_bar"))
    d.line((120, 990, W - 120, 990), fill=cx.c("rule"), width=2)
    cx.text(d, (120, 1006), cx.source, cx.body(26, 400), cx.c("muted"))
    d.rectangle((0, H - 6, int(W * t / cx.T), H), fill=cx.c("accent"))


def grid(d, cx):
    if cx.t.get("grid"):
        g = mix(cx.c("bg"), cx.c("rule"), 0.45)
        for x in range(120, 1801, 140):
            d.line((x, 140, x, 970), fill=g, width=1)


def label(d, cx, x, y, s, col, t, t0):
    a, dy = rise(t, t0)
    if a <= 0:
        return
    if cx.t.get("num") == "mono":
        cx.text(d, (x, y + dy), s.upper(), _font("mono", 26), cx.c("muted"), a); return
    d.rectangle((x, y + dy + 6, x + 6, y + dy + 30), fill=mix(cx.c("bg"), col, a))
    cx.text(d, (x + 20, y + dy), s.upper(), cx.body(26, 700), col, a, spacing=3)


def wrap(d, s, f, mw):
    out, cur = [], ""
    for w in s.split():
        x = (cur + " " + w).strip()
        if d.textlength(x, font=f) <= mw or not cur:
            cur = x
        else:
            out.append(cur); cur = w
    out.append(cur)
    return out


def KIND(cx, k):
    return {"pos": cx.c("pos"), "neg": cx.c("neg"), "accent": cx.c("accent"), "muted": cx.c("muted")}.get(k, cx.c("ink"))


# ---------- сцены ----------
def sc_title(img, d, cx, t, s):
    label(d, cx, 120, 220, s.get("label", ""), cx.c("accent"), t, 0.1)
    size = s.get("size", 140)
    f = cx.head(size, 900)
    while size > 80 and max(d.textlength(l, font=f) for l in s["lines"]) > (1120 if s.get("icon") else 1680):
        size -= 6; f = cx.head(size, 900)
    y = 290
    for i, line in enumerate(s["lines"]):
        a, dy = rise(t, 0.35 + i * 0.5, 0.5, 50)
        col = cx.c("accent") if i in s.get("accent", [len(s["lines"]) - 1]) else cx.c("ink")
        cx.text(d, (114, y + dy), line, f, col, a)
        y += int(f.size * 1.15)
    if s.get("icon"):
        a, dy = rise(t, 0.2, 0.7, 60)
        icon(img, cx, s["icon"], 1340, 240 + dy, 440, a)


def sc_counter(img, d, cx, t, s):
    label(d, cx, 120, 220, s.get("label", ""), cx.c("accent"), t, 0.0)
    k = eio((t - 0.2) / 1.6)
    val = int(round(s["value"] * k / 1000) * 1000) if s["value"] > 10000 else int(round(s["value"] * k))
    txt = f"{val:,}".replace(",", " ")
    a, dy = rise(t, 0.1, 0.4, 30)
    f = cx.num(240, 900) if cx.t.get("num") != "sans_thin" else cx.num(260, 200)
    w = cx.text(d, (110, 290 + dy), txt, f, cx.c("ink"), a)
    cx.text(d, (130 + w, 290 + dy + f.size * 0.55), s.get("unit", ""), cx.head(80, 700), cx.c("muted"), a)
    y = 680
    d.line((120, y, 1800, y), fill=cx.c("rule"), width=3)
    d.rectangle((120, y - 6, 120 + int(1680 * k * s.get("bar", 0.6)), y + 6), fill=cx.c("accent"))
    a2, dy2 = rise(t, 1.9, 0.4)
    cx.text(d, (120, 730 + dy2), s.get("note", ""), cx.body(44, 500), cx.c("muted"), a2)


def sc_timeline(img, d, cx, t, s):
    label(d, cx, 120, 220, s.get("label", ""), cx.c("ink"), t, 0.0)
    a, dy = rise(t, 0.15, 0.5, 40)
    cx.text(d, (112, 270 + dy), s.get("title", ""), cx.head(170, 900), cx.c("ink"), a)
    a, dy = rise(t, 0.4, 0.5)
    cx.text(d, (120, 480 + dy), s.get("subtitle", ""), cx.body(50, 500), cx.c("muted"), a)
    if s.get("icon"):
        a, dy = rise(t, 0.2, 0.6, 50)
        icon(img, cx, s["icon"], 1480, 170 + dy, 300, a)
    pts = s["points"]
    y, x0, x1 = 740, 160, 1760
    k = eio((t - 0.8) / 2.0)
    d.line((x0, y, x1, y), fill=cx.c("rule"), width=4)
    d.line((x0, y, x0 + (x1 - x0) * k, y), fill=cx.c("ink"), width=6)
    n = len(pts)
    for i, p in enumerate(pts):
        at = i / (n - 1) if n > 1 else 0
        x = x0 + (x1 - x0) * at
        on = k >= at - 0.001
        pop = eo((k - at) * 6 + 0.35) if on else 0
        col = cx.c("accent") if (i == n - 1 and s.get("highlight_last", True)) else (cx.c("ink") if i == 0 else cx.c("muted"))
        r = 14 + 6 * pop
        d.ellipse((x - r, y - r, x + r, y + r), fill=col if on else cx.c("bg"), outline=col if on else cx.c("rule"), width=4)
        if on:
            for j, (txt, f, cc) in enumerate([(p["date"], cx.head(46, 800), col), (p.get("text", ""), cx.body(32, 400), cx.c("muted"))]):
                tw = d.textlength(txt, font=f)
                xx = min(max(x - tw / 2, 120), W - 120 - tw)
                cx.text(d, (xx, y + 40 + j * 60), txt, f, cc, pop)


def sc_bars(img, d, cx, t, s):
    label(d, cx, 120, 220, s.get("label", ""), cx.c("ink"), t, 0.0)
    a, dy = rise(t, 0.1, 0.5)
    cx.text(d, (114, 270 + dy), s.get("title", ""), cx.head(100, 900), cx.c("ink"), a)
    a, dy = rise(t, 0.3, 0.5)
    cx.text(d, (120, 400 + dy), s.get("subtitle", ""), cx.body(42, 400), cx.c("muted"), a)
    bars = s["bars"]
    base, n = 850, len(bars)
    bw, gap, x0 = 140, 28, 1800 - (len(bars) * 168 - 28)
    vmax = max(b["value"] for b in bars)
    for i, b in enumerate(bars):
        k = eo((t - 0.4 - i * 0.18) / 0.6)
        h = 440 * b["value"] / vmax * k
        x = x0 + i * (bw + gap)
        col = KIND(cx, b.get("kind", "ink"))
        d.rectangle((x, base - h, x + bw, base), fill=col)
        if k > 0.3:
            f = cx.num(54, 900)
            cx.text(d, (x + (bw - d.textlength(b["label"], font=f)) / 2, base - h + 16), b["label"], f, cx.c("paper"), clamp((k - 0.3) / 0.4), bg=col)
    d.line((x0 - 20, base, x0 + n * (bw + gap) - gap + 20, base), fill=cx.c("ink"), width=4)
    k = eo((t - 1.7) / 0.5)
    for g in s.get("groups", []):
        if k <= 0:
            break
        xa, xb = x0 + g["from"] * (bw + gap), x0 + g["to"] * (bw + gap) + bw
        col = KIND(cx, g.get("kind"))
        d.line((xa, base + 30, xa + (xb - xa) * k, base + 30), fill=col, width=4)
        cx.text(d, (xa, base + 46), g["text"], cx.head(38, 800), col, k)
    a, dy = rise(t, 2.2, 0.5)
    y = 560
    for ln in s.get("note", []):
        cx.text(d, (120, y + dy), ln, cx.body(48, 600), cx.c("ink"), a); y += 60
    if s.get("note_accent"):
        cx.text(d, (120, y + 40 + dy), s["note_accent"], cx.head(60, 900), cx.c("neg"), a)


def sc_compare(img, d, cx, t, s):
    label(d, cx, 120, 220, s.get("label", ""), cx.c("accent"), t, 0.0)
    a, dy = rise(t, 0.1, 0.5)
    cx.text(d, (114, 270 + dy), s.get("title", ""), cx.head(100, 900), cx.c("ink"), a)
    a, dy = rise(t, 0.4, 0.5)
    cx.text(d, (120, 460 + dy), s["before"].get("caption", "Было").upper(), cx.body(30, 700), cx.c("muted"), a, spacing=3)
    f = cx.num(120, 900)
    w = cx.text(d, (114, 500 + dy), s["before"]["value"], f, cx.c("muted"), a)
    k = eio((t - 1.0) / 0.4)
    if k > 0:
        d.line((110, 500 + f.size * 0.62 + dy, 110 + (w + 10) * k, 500 + f.size * 0.62 + dy), fill=cx.c("neg"), width=10)
    a, dy = rise(t, 1.3, 0.5, 50)
    cx.text(d, (820, 460 + dy), s["after"].get("caption", "Стало").upper(), cx.body(30, 700), cx.c("neg"), a, spacing=3)
    cx.text(d, (814, 480 + dy), s["after"]["value"], cx.num(180, 900), cx.c("neg"), a)
    if s.get("sub"):
        a, dy = rise(t, 1.9, 0.5)
        hl = eio((t - 2.2) / 0.5)
        f = cx.head(60, 800)
        tw = d.textlength(s["sub"], font=f)
        if hl > 0:
            d.rectangle((812, 742 + dy, 812 + (tw + 16) * hl, 812 + dy), fill=cx.c("mark"))
        cx.text(d, (820, 736 + dy), s["sub"], f, cx.c("ink"), a)
    if s.get("tag"):
        a, dy = rise(t, 2.8, 0.5)
        if a > 0:
            f = cx.head(46, 800)
            tw = d.textlength(s["tag"], font=f)
            d.rectangle((820, 860 + dy, 860 + tw + 20, 930 + dy), fill=mix(cx.c("bg"), cx.c("ink"), a))
            d.text((846, 866 + dy), s["tag"], font=f, fill=mix(cx.c("ink"), cx.c("bg"), a))


def sc_columns(img, d, cx, t, s):
    a, dy = rise(t, 0.0, 0.5)
    cx.text(d, (114, 180 + dy), s.get("title", ""), cx.head(90, 900), cx.c("ink"), a)
    k = eio((t - 0.2) / 0.6)
    d.line((W / 2, 340, W / 2, 340 + 600 * k), fill=cx.c("rule"), width=3)
    for side, col_ in enumerate((s["left"], s["right"])):
        x0 = 120 if side == 0 else W / 2 + 80
        col = KIND(cx, col_.get("kind"))
        a, dy = rise(t, 0.3 + side * 0.25, 0.5)
        w = cx.text(d, (x0, 360 + dy), col_["title"], cx.head(64, 900), col, a)
        cx.text(d, (x0 + w + 30, 384 + dy), col_.get("sub", ""), cx.body(38, 600), cx.c("muted"), a)
        for i, it in enumerate(col_["items"]):
            a, dy = rise(t, 0.7 + side * 0.25 + i * 0.18, 0.5)
            ix = x0 + i * 270
            icon(img, cx, it["icon"], ix, 500 + dy, 200, a)
            f = cx.body(34, 600)
            for j, l in enumerate(wrap(d, it["text"], f, 240)):
                cx.text(d, (ix + 100 - d.textlength(l, font=f) / 2, 720 + j * 42 + dy), l, f, cx.c("ink"), a)


def sc_statement(img, d, cx, t, s):
    label(d, cx, 120, 220, s.get("label", ""), cx.c("ink"), t, 0.0)
    y = 280
    for i, ln in enumerate(s.get("lead", [])):
        a, dy = rise(t, 0.1 + i * 0.15, 0.5)
        cx.text(d, (114, y + dy), ln, cx.body(62, 600), cx.c("ink"), a); y += 78
    f = cx.head(140, 900)
    hl = eio((t - 1.4) / 0.6)
    y += 40
    for j, ln in enumerate(s.get("big", [])):
        a, dy = rise(t, 0.9 + j * 0.15, 0.5, 50)
        tw = d.textlength(ln, font=f)
        if hl > 0:
            d.rectangle((108, y + f.size * 0.28 + dy, 108 + (tw + 16) * hl, y + f.size * 1.05 + dy), fill=cx.c("mark"))
        cx.text(d, (114, y + dy), ln, f, cx.c("ink"), a); y += int(f.size * 1.12)
    if s.get("icon"):
        a, dy = rise(t, 0.4, 0.6, 50)
        icon(img, cx, s["icon"], 1400, 300 + dy, 380, a)


def sc_outro(img, d, cx, t, s):
    f = cx.head(130, 900)
    y = 280
    for i, ln in enumerate(s["lines"]):
        a, dy = rise(t, 0.0 + i * 0.3, 0.5)
        if i == len(s["lines"]) - 1:
            hl = eio((t - 0.6) / 0.5); tw = d.textlength(ln, font=f)
            if hl > 0:
                d.rectangle((108, y + f.size * 0.3 + dy, 108 + (tw + 20) * hl, y + f.size * 1.08 + dy), fill=cx.c("mark"))
        cx.text(d, (114, y + dy), ln, f, cx.c("ink"), a); y += int(f.size * 1.2)
    a, dy = rise(t, 0.8, 0.5)
    cx.text(d, (120, y + 40 + dy), s.get("sub", ""), cx.body(56, 500), cx.c("muted"), a)
    a, dy = rise(t, 1.0, 0.5)
    fb = _font("sans", 72, 900)
    bw_ = d.textlength(cx.brand, font=fb) + 4 * len(cx.brand)
    cx.text(d, (W - 120 - bw_, 760 + dy), cx.brand, fb, cx.c("ink"), a, spacing=4)
    if a > 0:
        d.rectangle((W - 120 - bw_, 860 + dy, W - 120 - bw_ + bw_ * a, 868 + dy), fill=cx.c("accent"))


SCENES = {"title": sc_title, "counter": sc_counter, "timeline": sc_timeline, "bars": sc_bars,
          "compare": sc_compare, "columns": sc_columns, "statement": sc_statement, "outro": sc_outro}


# ---------- тайминг, кадры, звук ----------
def plan(scn, manifest):
    rows, t = [], 0.0
    for s in scn["shots"]:
        lead = s.get("lead", 0.3 if t > 0 else 0.4)
        vd = manifest.get(s["id"], {}).get("dur") if s.get("text") or s.get("audio") else None
        dur = max(s.get("dur", 3.0), (vd + lead + 0.35) if vd else 0)
        rows.append({"id": s["id"], "start": round(t, 3), "end": round(t + dur, 3), "voice_at": round(t + lead, 3) if vd else None})
        t += dur
    return rows, t


def render(project, cfg, theme_name=None, out_name=None):
    scn = project.load()
    themes = load_yaml("info_themes.yaml")
    theme = themes[theme_name or scn.get("theme", "editorial")]
    mpath = project.sub("voice") / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    rows, T = plan(scn, manifest)
    brand = scn.get("brand", cfg["render"].get("brand", "MODDA AI"))
    cx = Ctx(theme, scn.get("source_line", "Источник: lex.uz"), brand, T, scn.get("lang", "ru"))
    n_sc = len(scn["shots"])
    work = project.sub("work")
    silent = work / "info_video.mp4"
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                          "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(silent)],
                         stdin=subprocess.PIPE)
    for i in range(int(T * FPS)):
        t = i / FPS
        img = Image.new("RGB", (W, H), cx.c("bg"))
        d = ImageDraw.Draw(img)
        grid(d, cx)
        for k, (s, r) in enumerate(zip(scn["shots"], rows)):
            if r["start"] <= t < r["end"] or (k == n_sc - 1 and t >= r["start"]):
                lt = t - r["start"]
                SCENES[s["scene"]["type"]](img, d, cx, lt, s["scene"])
                chrome(img, d, cx, t, min(k + 1, n_sc), n_sc)
                if lt < 0.18 and k > 0:
                    d.rectangle((0, 0, int(W * (1 - eo(lt / 0.18))), H), fill=cx.c("ink"))
                break
        p.stdin.write(img.tobytes())
    p.stdin.close(); p.wait()

    # звук
    ai, af, vl = [], "", []

    def addin(f):
        ai.extend(["-i", str(f)]); return len(ai) // 2

    for s, r in zip(scn["shots"], rows):
        if r["voice_at"] is None:
            continue
        k = addin(project.sub("voice") / f"{s['id']}.wav")
        ms = int(r["voice_at"] * 1000)
        af += f"[{k}:a]adelay={ms}|{ms}[v{k}];"; vl.append(f"[v{k}]")
    mus = scn.get("music", {})
    mfile = project.dir / mus["file"] if mus.get("file") else music.synth(work / "info_music.wav", T + 1, mus.get("preset", "calm"))
    m = addin(mfile)
    if vl:
        af += "".join(vl) + f"amix=inputs={len(vl)}:normalize=0,pan=stereo|c0=c0|c1=c0,aresample=44100,asplit[vm][vs];"
        af += f"[{m}:a]atrim=0:{T:.3f},volume={mus.get('volume', 0.24)}[mm];[mm][vs]sidechaincompress=threshold=0.03:ratio=5:attack=20:release=350[md];"
        mixin = ["[vm]", "[md]"]
    else:
        af += f"[{m}:a]atrim=0:{T:.3f},volume={mus.get('volume', 0.3)}[md];"; mixin = ["[md]"]
    fx = []
    wp = sfx.path("whoosh")
    for s, r in zip(scn["shots"][1:], rows[1:]):
        k = addin(wp); ms = int(max(0, r["start"] - 0.15) * 1000)
        af += f"[{k}:a]volume=0.15,aresample=44100,adelay={ms}|{ms}[f{k}];"; fx.append(f"[f{k}]")
    for s, r in zip(scn["shots"], rows):
        for e in s.get("sfx", []):
            e = {"name": e} if isinstance(e, str) else e
            k = addin(sfx.path(e["name"])); ms = int((r["start"] + e.get("at", 0.8)) * 1000)
            af += f"[{k}:a]volume={e.get('vol', 0.4)},aresample=44100,adelay={ms}|{ms}[f{k}];"; fx.append(f"[f{k}]")
    allin = mixin + fx
    af += ("".join(allin) + f"amix=inputs={len(allin)}:normalize=0,atrim=0:{T:.3f},afade=t=out:st={T - 0.6:.2f}:d=0.6,"
           f"loudnorm=I=-14:TP=-1:LRA=9,aresample=48000[aout]")
    out = project.sub("out") / (out_name or f"{scn.get('id', 'info')}_{theme_name or scn.get('theme', 'editorial')}_16x9.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(silent), *ai, "-filter_complex", af, "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], check=True)
    return out, T


def stills(project, cfg, theme_name=None):
    """Кадр конца каждой сцены: быстро проверить вёрстку темы, не рендеря видео."""
    scn = project.load()
    theme = load_yaml("info_themes.yaml")[theme_name or scn.get("theme", "editorial")]
    cx = Ctx(theme, scn.get("source_line", "Источник: lex.uz"), scn.get("brand", "MODDA AI"), 30, scn.get("lang", "ru"))
    tiles = []
    for k, s in enumerate(scn["shots"]):
        img = Image.new("RGB", (W, H), cx.c("bg")); d = ImageDraw.Draw(img)
        grid(d, cx)
        SCENES[s["scene"]["type"]](img, d, cx, 6.0, s["scene"])
        chrome(img, d, cx, 30 * (k + 1) / len(scn["shots"]), k + 1, len(scn["shots"]))
        tiles.append(img.resize((640, 360), Image.LANCZOS))
    cols = 3
    rows_n = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (640 * cols, 360 * rows_n), (255, 255, 255))
    for i, tl in enumerate(tiles):
        sheet.paste(tl, ((i % cols) * 640, (i // cols) * 360))
    out = project.sub("out") / f"{scn.get('id')}_{theme_name or scn.get('theme')}_sheet.jpg"
    sheet.save(out, quality=90)
    return out
