"""Звуковые эффекты. В assets/sfx лежат boom, whoosh, fireworks_show (из ролика про пиротехнику).
Остальные синтезируются при первом запуске: ding, buzz, stamp, pop, tick, cash, sparkle.
Чтобы заменить звук, положите свой файл с тем же именем (mp3 или wav) в assets/sfx."""
import numpy as np

from . import audio
from .config import ASSETS

SFX = ASSETS / "sfx"
SR = 48000


def _t(d):
    return np.arange(int(d * SR)) / SR


def _env(t, a, d):
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / d)


def _gen(name):
    rng = np.random.default_rng(7)
    if name == "ding":
        t = _t(1.2)
        return sum(np.sin(2 * np.pi * f * t) * g for f, g in [(1318, 1), (1976, .5), (2637, .25)]) * _env(t, .002, .35) * .4
    if name == "buzz":
        t = _t(0.55)
        return np.sign(np.sin(2 * np.pi * 110 * t)) * 0.3 * (np.sin(2 * np.pi * 8 * t) > -0.2) * _env(t, .01, .4)
    if name == "stamp":
        t = _t(0.6)
        return (np.sin(2 * np.pi * (60 + 120 * np.exp(-t * 40)) * t) + rng.standard_normal(len(t)) * np.exp(-t * 30) * .5) * _env(t, .001, .15) * .8
    if name == "pop":
        t = _t(0.15)
        return np.sin(2 * np.pi * (900 - 2500 * t) * t) * _env(t, .001, .03) * .5
    if name == "tick":
        out = np.zeros(int(2.0 * SR))
        for k in range(4):
            t = _t(0.05)
            s = int(k * 0.5 * SR)
            out[s:s + len(t)] += np.sin(2 * np.pi * 2400 * t) * _env(t, .0005, .01) * .5
        return out
    if name == "cash":
        t = _t(0.9)
        bell = sum(np.sin(2 * np.pi * f * t) for f in (2093, 2637, 3136)) * _env(t, .002, .25) * .2
        ch = rng.standard_normal(len(t)) * np.exp(-t * 25) * .25
        return bell + ch
    if name == "sparkle":
        out = np.zeros(int(1.5 * SR))
        for k in range(14):
            t = _t(0.25)
            s = int(rng.uniform(0, 1.2) * SR)
            f = rng.uniform(2500, 5000)
            out[s:s + len(t)] += np.sin(2 * np.pi * f * t) * _env(t, .001, .05) * .15
        return out
    if name == "whoosh":
        t = _t(0.6)
        n = rng.standard_normal(len(t))
        k = np.exp(-((t - .3) / .12) ** 2)
        return np.convolve(n, np.ones(30) / 30, "same") * k * .6
    if name == "boom":
        t = _t(2.0)
        return (np.sin(2 * np.pi * (40 + 80 * np.exp(-t * 8)) * t) + np.convolve(rng.standard_normal(len(t)), np.ones(60) / 60, "same") * 3) * _env(t, .002, .5) * .7
    raise KeyError(name)


def path(name):
    for ext in (".mp3", ".wav"):
        p = SFX / f"{name}{ext}"
        if p.exists():
            return p
    p = SFX / f"{name}.wav"
    audio.write(p, _gen(name), SR)
    return p


def ensure_all():
    return [path(n) for n in ("boom", "whoosh", "ding", "buzz", "stamp", "pop", "tick", "cash", "sparkle")]
