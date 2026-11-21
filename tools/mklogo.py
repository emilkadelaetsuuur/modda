import sys
from PIL import Image
import numpy as np
src, out = sys.argv[1], sys.argv[2]
_src = Image.open(src).convert("RGBA"); _bg = Image.new("RGBA", _src.size, (255, 255, 255, 255)); _bg.alpha_composite(_src)
im = np.asarray(_bg.convert("RGB")).astype(np.float32)
# white -> transparent ("colour to alpha"): alpha from the darkest channel's distance to white
a = (255 - im.min(axis=2)) / 255.0
a[a < 0.04] = 0
rgb = np.where(a[..., None] > 0, (im - (1 - a[..., None]) * 255) / np.maximum(a[..., None], 1e-6), 0)
rgb = np.clip(rgb, 0, 255)
solid = a > 0.5
rows, cols = np.nonzero(solid.sum(axis=1) >= 4)[0], np.nonzero(solid.sum(axis=0) >= 4)[0]
ys, xs = rows, cols
pad = 6
y0, y1, x0, x1 = max(ys.min() - pad, 0), ys.max() + pad + 1, max(xs.min() - pad, 0), xs.max() + pad + 1
rgba = np.dstack([rgb, a * 255])[y0:y1, x0:x1].astype(np.uint8)
dark = Image.fromarray(rgba, "RGBA")
# light variant: near-neutral (black) pixels become white, the orange "o" stays
r, g, b, al = [np.asarray(c, dtype=np.float32) for c in dark.split()]
neutral = (np.abs(r - g) < 40) & (np.abs(g - b) < 40)
lr, lg, lb = [np.where(neutral, 255, c) for c in (r, g, b)]
light = Image.fromarray(np.dstack([lr, lg, lb, al]).astype(np.uint8), "RGBA")
for name, img in (("modda-logo", dark), ("modda-logo-light", light)):
    h = 96  # 3x of a 32 px display height
    w = round(img.width * h / img.height)
    img.resize((w, h), Image.LANCZOS).save(f"{out}/{name}.png", optimize=True)
    print(name, w, h)
