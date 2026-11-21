"""PNG-слои поверх видео: субтитры (два размера для эффекта "выпрыгивания"), бейдж бренда, плашка источника."""
from PIL import Image, ImageDraw, ImageFont

from .config import ASSETS

FB = str(ASSETS / "fonts" / "FiraSans-Black.ttf")
FS = str(ASSETS / "fonts" / "FiraSans-SemiBold.ttf")
COLORS = {"y": (255, 214, 40), "r": (255, 70, 70), "g": (90, 235, 120), "w": (255, 255, 255),
          "b": (90, 190, 255)}
STROKE = (20, 14, 40)


def _wrap(d, txt, f, mw):
    out, cur = [], ""
    for w in txt.split():
        x = (cur + " " + w).strip()
        if d.textlength(x, font=f) <= mw or not cur:
            cur = x
        else:
            out.append(cur)
            cur = w
    out.append(cur)
    return out


def caption(path, txt, color, W, H, scale=1.0, base_scale=1.0, y_center=None):
    if y_center is None:
        y_center = 0.80 if W > H else 0.615
    col = COLORS.get(color, COLORS["w"])
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = color != "w"
    sz = int((104 if accent else 92) * scale * base_scale * min(W, H) / 1080)
    f = ImageFont.truetype(FB, sz)
    lines = _wrap(d, txt.upper() if accent else txt, f, int(W * 0.89))
    y0 = H * y_center - len(lines) * sz * 0.55
    for i, l in enumerate(lines):
        tw = d.textlength(l, font=f)
        d.text(((W - tw) / 2, y0 + i * sz * 1.1), l, font=f, fill=col,
               stroke_width=max(8, int(sz * 0.12)), stroke_fill=STROKE)
    img.save(path)
    return path


def brand(path, W, H, text="MODDA AI", color=(15, 139, 110)):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FB, 34)
    w = d.textlength(text, font=f)
    d.rounded_rectangle((50, 70, 50 + w + 50, 126), radius=28, fill=tuple(color) + (235,))
    d.text((75, 78), text, font=f, fill=(255, 255, 255))
    img.save(path)
    return path


def plate(path, lines, W, H, color=(15, 139, 110)):
    """lines[0] = заголовок (ИСТОЧНИК: LEX.UZ), остальные = закон, статья."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = lines[1:]
    top = int(H * 0.72)
    bottom = top + 90 + 58 * max(1, len(body))
    d.rounded_rectangle((60, top, W - 60, bottom), radius=30, fill=(255, 255, 255, 245))
    d.rounded_rectangle((60, top, 86, bottom), radius=12, fill=tuple(color) + (255,))
    d.text((115, top + 25), lines[0] if lines else "ИСТОЧНИК: LEX.UZ", font=ImageFont.truetype(FB, 44), fill=tuple(color))
    fs = ImageFont.truetype(FS, 42)
    for i, l in enumerate(body):
        while d.textlength(l, font=fs) > W - 200 and fs.size > 26:
            fs = ImageFont.truetype(FS, fs.size - 2)
        d.text((115, top + 90 + i * 58), l, font=fs, fill=(31, 35, 64))
    img.save(path)
    return path
