"""Озвучка по шотам: каждая реплика отдельным файлом voice/<shot_id>.wav.
Отдельные файлы = точная синхронизация: склейка ставится ровно на начало реплики.

Источник голоса для шота (по приоритету):
  1. shot.audio = {"file": "...", "from": 0.0, "to": 2.7}  готовая запись (своя или старая)
  2. tts.provider = voicelab  (VoiceLab / Aisha API)
  3. tts.provider = mock      (синтетический "бубнёж" нужной длины, для проверки монтажа без ключа)
"""
import hashlib
import json

import numpy as np

from . import audio
from .config import load_yaml
from .tts_voicelab import VoiceLab, apply_emotion_text, clean_text


def _voice_for(cfg, speaker, lang):
    v = load_yaml("voices.yaml")["speakers"]
    base = dict((v.get(speaker) or v["narrator"]).get(lang) or {})
    vid = cfg["tts"].get("voice_ids", {}).get(f"{speaker}_{lang}") or cfg["tts"].get("voice_ids", {}).get(speaker)
    if vid:
        base["voice_id"] = vid
    return base


def _mock(text, path, wps=2.6):
    words = max(1, len(text.split()))
    dur = words / wps + 0.15
    sr = audio.SR
    t = np.arange(int(dur * sr)) / sr
    f0 = 140 + 25 * np.sin(2 * np.pi * 0.7 * t)
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6))
    syl = 0.5 + 0.5 * np.sin(2 * np.pi * (wps * 1.6) * t - np.pi / 2)
    audio.write(path, 0.25 * sig * syl ** 2)


def run(project, cfg, only=None, force=False):
    scn = project.load()
    lang = scn.get("lang", "ru")
    aud = load_yaml("audiences.yaml").get(scn.get("audience", "teens"), {})
    emotions = load_yaml("voices.yaml")["emotions"]
    provider = cfg["tts"]["provider"]
    vdir = project.sub("voice")
    man_path = vdir / "manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    vl = None
    for s in scn["shots"]:
        sid = s["id"]
        if only and sid not in only:
            continue
        if not (s.get("text") or s.get("audio")):
            continue  # сцена без голоса (инфографика)
        out = vdir / f"{sid}.wav"
        emo = emotions.get(s.get("emotion", "serious"), emotions["serious"])
        text = apply_emotion_text(clean_text(s.get("tts_text") or s["text"]), emo.get("text", "none"))
        voice = _voice_for(cfg, s.get("speaker", "narrator"), lang)
        key = hashlib.md5(json.dumps([provider, text, voice, emo, s.get("audio")], ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()[:12]
        if out.exists() and manifest.get(sid, {}).get("hash") == key and not force:
            continue
        trim = cfg["tts"].get("trim_silence", True)
        if s.get("audio"):
            a = s["audio"]
            audio.to_wav(project.dir / a["file"], out, a.get("from"), a.get("to"))
            trim = a.get("trim", False)
            src = "file"
        elif provider == "voicelab":
            vl = vl or VoiceLab(cfg)
            data = vl.params(text, lang, voice, emo, aud.get("tts_speed", 1.0))
            raw = vl.synth(data, lang)
            tmp = project.sub("work") / f"{sid}_raw.bin"
            tmp.write_bytes(raw)
            audio.to_wav(tmp, out)
            src = f"voicelab {data.get('voice_id') or data.get('model', '')} {data.get('mood', '')}".strip()
        elif provider == "mock":
            _mock(text, out, aud.get("wps", 2.6))
            src = "mock"
        else:
            raise ValueError(f"Нет audio у шота {sid}, а tts.provider={provider}")
        dur = audio.trim_silence(out) if trim else audio.duration(out)
        manifest[sid] = {"file": out.name, "dur": round(dur, 3), "hash": key, "src": src, "text": text}
        print(f"{sid}: {dur:5.2f} c  [{src}]  {text}")
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest
