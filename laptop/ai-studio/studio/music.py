"""Фоновый бит без лицензий: синтезируем сами (kick, clap, хэты, бас, аккорды, колокольчик).
Можно вместо этого положить свой трек: scenario.music.file."""
import wave

import numpy as np

PRESETS = {
    # bpm, прогрессия (MIDI корней + аккорды), громкость слоёв
    "upbeat": dict(bpm=124, prog=[(57, [57, 60, 64]), (53, [53, 57, 60]), (48, [48, 52, 55]), (55, [55, 59, 62])],
                   kick=0.9, hat=0.25, clap=0.5, bass=0.18, stab=0.06, bell=0.05),
    "tense": dict(bpm=110, prog=[(57, [57, 60, 64]), (57, [57, 60, 64]), (53, [53, 57, 60]), (52, [52, 56, 59])],
                  kick=0.9, hat=0.3, clap=0.35, bass=0.2, stab=0.05, bell=0.03),
    "calm": dict(bpm=88, prog=[(48, [48, 52, 55]), (55, [55, 59, 62]), (57, [57, 60, 64]), (53, [53, 57, 60])],
                 kick=0.45, hat=0.1, clap=0.0, bass=0.12, stab=0.07, bell=0.06),
    "kids": dict(bpm=118, prog=[(48, [48, 52, 55]), (53, [53, 57, 60]), (55, [55, 59, 62]), (48, [48, 52, 55])],
                 kick=0.6, hat=0.2, clap=0.4, bass=0.14, stab=0.06, bell=0.08),
}


def synth(out_path, dur=32.0, preset="upbeat", sr=44100, seed=1):
    p = PRESETS.get(preset, PRESETS["upbeat"])
    beat = 60 / p["bpm"]
    n = int(sr * dur)
    out = np.zeros(n)

    def env(L, a=0.005, d=0.2):
        x = np.arange(L) / sr
        return np.minimum(1, x / a) * np.exp(-x / d)

    def add(sig, start):
        s = int(start * sr)
        if s >= n:
            return
        e = min(n, s + len(sig))
        out[s:e] += sig[:e - s]

    f = lambda m: 440 * 2 ** ((m - 69) / 12)  # noqa
    kl = int(0.35 * sr)
    kx = np.arange(kl) / sr
    kick = np.sin(2 * np.pi * (50 + 90 * np.exp(-kx * 30)) * kx) * np.exp(-kx * 9)
    rng = np.random.default_rng(seed)
    hl = int(0.06 * sr)
    hat = np.diff(np.concatenate([[0], rng.standard_normal(hl) * np.exp(-np.arange(hl) / sr * 70)]))
    cl = int(0.2 * sr)
    clap = rng.standard_normal(cl) * np.exp(-np.arange(cl) / sr * 22) * 0.6
    bars = int(dur / (beat * 4)) + 1
    for b in range(bars):
        root, ch = p["prog"][b % 4]
        t0 = b * 4 * beat
        for k in range(4):
            add(kick * p["kick"], t0 + k * beat)
            if k in (1, 3) and p["clap"]:
                add(clap * p["clap"], t0 + k * beat)
            for h in range(2):
                add(hat * p["hat"], t0 + k * beat + h * beat / 2 + beat / 4)
                L = int(beat / 2 * sr * 0.9)
                x = np.arange(L) / sr
                add(np.sign(np.sin(2 * np.pi * f(root - 12) * x)) * p["bass"] * env(L, 0.005, 0.15), t0 + k * beat + h * beat / 2)
        for k in (0.5, 1.5, 2.5, 3.5):
            L = int(0.25 * sr)
            x = np.arange(L) / sr
            st = sum(np.sin(2 * np.pi * f(m + 12) * x) + 0.4 * np.sin(2 * np.pi * f(m + 24) * x) for m in ch)
            add(st * p["stab"] * env(L, 0.003, 0.09), t0 + k * beat)
        for k, m in enumerate([ch[2] + 12, ch[1] + 12, ch[0] + 24, ch[1] + 12]):
            L = int(0.4 * sr)
            x = np.arange(L) / sr
            add((np.sin(2 * np.pi * f(m) * x) + 0.3 * np.sin(2 * np.pi * f(m) * 2.01 * x)) * p["bell"] * env(L, 0.002, 0.25), t0 + k * beat)
    out = np.tanh(out * 1.3) / 1.1
    fade = int(1.5 * sr)
    out[-fade:] *= np.linspace(1, 0, fade)
    st = (np.stack([out, out], 1) * 32767 * 0.8).astype(np.int16)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(st.tobytes())
    return out_path
