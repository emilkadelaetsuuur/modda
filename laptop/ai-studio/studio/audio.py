"""Мелкие аудио-утилиты: конвертация, чтение, обрезка тишины, длительность."""
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 48000


def to_wav(src, dst, start=None, end=None, sr=SR):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(src), "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, check=True)
    return Path(dst)


def read(path):
    with wave.open(str(path), "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    return x, sr


def write(path, x, sr=SR):
    x = np.clip(x, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return Path(path)


def trim_silence(path, thr_db=-40, pad=0.03, fade=0.01):
    """Срезает тишину в начале и конце: так склейка попадает точно в первое слово."""
    x, sr = read(path)
    if len(x) == 0:
        return 0.0
    win = int(0.01 * sr)
    frames = len(x) // win
    if frames == 0:
        return len(x) / sr
    rms = np.sqrt((x[:frames * win].reshape(frames, win) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(rms)
    loud = np.where(db > max(thr_db, db.max() - 45))[0]
    if len(loud) == 0:
        return len(x) / sr
    a = max(0, loud[0] * win - int(pad * sr))
    b = min(len(x), (loud[-1] + 1) * win + int(pad * sr))
    y = x[a:b].copy()
    f = int(fade * sr)
    if len(y) > 2 * f:
        y[:f] *= np.linspace(0, 1, f)
        y[-f:] *= np.linspace(1, 0, f)
    write(path, y, sr)
    return len(y) / sr


def duration(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()
