"""Тайминг субтитров: когда каждый кусок реплики появляется на экране.
faster-whisper даёт время каждого слова; куски субтитров привязываем по доле символов,
поэтому мелкие ошибки распознавания ("суммов" вместо "сумов") не ломают синхрон.
Без whisper: равномерно по символам."""
import numpy as np

from . import audio

_model = None


def _whisper(cfg):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        a = cfg["align"]
        _model = WhisperModel(a["whisper_model"], device=a["device"], compute_type=a["compute_type"])
    return _model


def words(wav_path, lang, cfg, tmp_dir, hint=""):
    try:
        model = _whisper(cfg)
    except Exception as e:  # noqa
        print(f"  (whisper недоступен: {e}; субтитры по символам)")
        return []
    tmp = tmp_dir / (wav_path.stem + "_16k.wav")
    audio.to_wav(wav_path, tmp, sr=16000)
    x, _ = audio.read(tmp)
    segs, _ = model.transcribe(x, language=lang if lang in ("ru", "uz", "en") else None, word_timestamps=True,
                               vad_filter=False, beam_size=1, temperature=0.0,
                               condition_on_previous_text=False, initial_prompt=hint or None)
    out = []
    for s in segs:
        for w in s.words or []:
            out.append((w.word.strip(), float(w.start), float(w.end)))
    return out


import re as _re


def _toks(s):
    return [_re.sub(r"[^\w'ʻʼ]", "", w.lower().replace("ё", "е")) for w in s.split() if _re.sub(r"\W", "", w)]


def _same(a, b):
    return a == b or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4])


def chunk_word_starts(chunks, text):
    """Номер слова реплики, с которого начинается каждый кусок субтитров.
    Цифры в субтитрах ("15-25") не совпадают со словами речи ("пятнадцати..."),
    поэтому ищем буквенные слова куска в тексте реплики."""
    T = _toks(text)
    starts, pos, last = [], 0, -1
    for i, c in enumerate(chunks):
        toks = _toks(c[0])
        found = []
        p = pos
        for tk in toks:
            if not tk or any(ch.isdigit() for ch in tk):
                found.append(None)
                continue
            for j in range(p, len(T)):
                if _same(tk, T[j]):
                    found.append(j)
                    p = j + 1
                    break
            else:
                found.append(None)
        if i == 0:
            st = 0
        elif found and found[0] is not None:
            st = found[0]
        else:
            st = last + 1 if last >= 0 else pos
        starts.append(min(st, max(0, len(T) - 1)))
        hits = [f for f in found if f is not None]
        last = max(hits) if hits else st
        pos = max(pos, last + 1)
    return starts, len(T)


def chunk_times(chunks, wl, dur, text=""):
    """chunks: [[text, color, (t0, t1)?], ...] -> [(text, color, t0, t1)] в секундах от начала реплики."""
    if all(len(c) >= 4 for c in chunks):
        return [(c[0], c[1], float(c[2]), float(c[3])) for c in chunks]
    if text:
        ws, nT = chunk_word_starts(chunks, text)
        if wl and len(wl) == nT:
            tf_idx = lambda k: wl[k][1]  # noqa
        elif wl:
            P = np.linspace(0, 1, len(wl) + 1)
            T = np.array([w[1] for w in wl] + [max(wl[-1][2], dur * 0.98)])
            tf_idx = lambda k: float(np.interp(k / max(1, nT), P, T))  # noqa
        else:
            tf_idx = lambda k: k / max(1, nT) * dur * 0.97  # noqa
        b = [0.0] + [tf_idx(k) for k in ws[1:]] + [dur]
    else:
        lens = [len(c[0]) + 1 for c in chunks]
        fr = np.cumsum([0] + lens) / float(sum(lens))
        b = [f * dur for f in fr]
        b[-1] = dur
    res = []
    for i, c in enumerate(chunks):
        t0, t1 = b[i], b[i + 1]
        res.append((c[0], c[1] if len(c) > 1 else "w", round(t0, 3), round(max(t1, t0 + 0.25), 3)))
    return res


def auto_chunks(text, max_words=3):
    w = text.split()
    return [[" ".join(w[i:i + max_words]), "w"] for i in range(0, len(w), max_words)]
