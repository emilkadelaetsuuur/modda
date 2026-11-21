"""VoiceLab (Aisha AI) TTS.

Два API, выбор по tts.base_url:
- новый https://api.voicelab.uz (docs.voicelab.uz): ключ в "Authorization: Bearer",
  POST /v1/tts json {text, language, voice_id, speed} -> аудио сразу или {"id"};
  GET /v1/tts/generations/{id} -> status completed + audio_url. Голоса: GET /v1/voices?language=ru,
  их id вписываются в config.yaml tts.voice_ids (<персонаж>_<язык>).
- старый https://back.aisha.group (ниже).

Документация старого API: https://aisha.group/uz/api-documentation/text-to-speech
POST {base}/api/v1/tts/post/   multipart: transcript, language, model, mood, speed, voice_id
  201 -> {"audio_path": "/media/tts_audios/<id>.wav", ...}
  202 -> {"id": ..., "status": "PENDING"}  (если передан webhook_notification_url)
GET  {base}/api/v1/tts/status/{id}/  -> PENDING | SUCCESS (audio_path) | FAILED
Ошибки: 400 неверные данные, 401 нужен ключ для своего голоса, 402 нет баланса, 503 сервис недоступен.
"""
import hashlib
import json
import re
import time

import requests

from .config import secret

MAX_CHARS = 1000


class VoiceLabError(RuntimeError):
    pass


def clean_text(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", "", text)          # теги эмоций в стиле ElevenLabs не читаем
    text = re.sub(r"\s*—\s*", ", ", text).replace("–", "-")
    return re.sub(r"\s+", " ", text).strip()


def apply_emotion_text(text, how):
    if how == "exclaim":
        text = re.sub(r"[.]+$", "", text)
        if not text.endswith(("!", "?")):
            text += "!"
    elif how == "question" and not text.endswith("?"):
        text = text.rstrip(".!") + "?"
    elif how == "pause":
        parts = text.split(" ", 1)
        if len(parts) == 2 and not parts[0].endswith((",", ".", "!", "?", ":")):
            text = parts[0] + ", " + parts[1]
    return text


class VoiceLab:
    def __init__(self, cfg):
        t = cfg["tts"]
        self.base = t["base_url"].rstrip("/")
        self.key = secret(t)
        self.timeout = t.get("timeout", 120)
        self.new_api = "api.voicelab.uz" in self.base

    def _headers(self, lang):
        if self.new_api:
            return {"Authorization": f"Bearer {self.key}"}
        return {"X-Api-Key": self.key, "Accept-Language": lang if lang in ("uz", "ru", "en") else "uz"}

    def params(self, text, lang, voice: dict, emotion: dict, speed_mul=1.0):
        """Собирает поля запроса по правилам API (mood и model только для uz Gulnoza, speed только uz)."""
        if self.new_api:
            vid = voice.get("voice_id")
            if not vid:
                raise VoiceLabError(f"Новый API VoiceLab: нет voice_id для языка {lang}. "
                                    "Впишите голоса из GET /v1/voices в config.yaml tts.voice_ids")
            sp = float(voice.get("speed", 1.0)) * float(emotion.get("speed_mul", 1.0)) * speed_mul
            return {"text": text, "language": lang, "voice_id": str(vid), "speed": round(min(1.25, max(0.7, sp)), 2)}
        data = {"transcript": text, "language": lang}
        vid = voice.get("voice_id")
        if vid:
            data["voice_id"] = str(vid)
        elif lang == "uz":
            data["model"] = voice.get("model", "Gulnoza")
            data["mood"] = emotion.get("mood") or voice.get("mood", "Neutral")
        if lang == "uz":
            sp = float(voice.get("speed", 1.0)) * float(emotion.get("speed_mul", 1.0)) * speed_mul
            data["speed"] = f"{min(1.25, max(0.7, sp)):.2f}"  # шире 0.7-1.25 звучит неестественно (API 0.5-2.0)
        return data

    def synth(self, data: dict, lang: str) -> bytes:
        if self.new_api:
            return self._synth_new(data, lang)
        if len(data["transcript"]) > MAX_CHARS:
            raise VoiceLabError(f"Текст длиннее {MAX_CHARS} символов, разбейте реплику")
        url = f"{self.base}/api/v1/tts/post/"
        for attempt in range(3):
            r = requests.post(url, headers=self._headers(lang), files={k: (None, v) for k, v in data.items()},
                              timeout=self.timeout)
            if r.status_code == 503 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if r.status_code == 402:
            raise VoiceLabError("VoiceLab: недостаточно баланса (402)")
        if r.status_code == 401:
            raise VoiceLabError("VoiceLab: нужен ключ или нет доступа к этому voice_id (401)")
        if r.status_code not in (200, 201, 202):
            raise VoiceLabError(f"VoiceLab {r.status_code}: {r.text[:300]}")
        js = r.json()
        path = js.get("audio_path") or js.get("audio_url")
        if not path and (r.status_code == 202 or js.get("status") == "PENDING"):
            path = self._wait(js.get("id"), lang)
        if not path:
            raise VoiceLabError(f"VoiceLab: в ответе нет audio_path: {js}")
        audio_url = path if path.startswith("http") else self.base + path
        a = requests.get(audio_url, headers=self._headers(lang), timeout=self.timeout)
        a.raise_for_status()
        return a.content

    def _synth_new(self, data: dict, lang: str) -> bytes:
        if len(data["text"]) > MAX_CHARS:
            raise VoiceLabError(f"Текст длиннее {MAX_CHARS} символов, разбейте реплику")
        key = hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        headers = {**self._headers(lang), "Idempotency-Key": "modda-" + key[:40]}
        for attempt in range(3):
            r = requests.post(f"{self.base}/v1/tts", headers=headers, json=data, timeout=self.timeout)
            if r.status_code in (429, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if r.status_code == 402:
            raise VoiceLabError("VoiceLab: недостаточно баланса (402)")
        if r.status_code in (401, 403):
            raise VoiceLabError(f"VoiceLab: ключ не подошёл ({r.status_code}): {r.text[:200]}")
        if r.status_code not in (200, 201, 202):
            raise VoiceLabError(f"VoiceLab {r.status_code}: {r.text[:300]}")
        if r.headers.get("content-type", "").startswith("audio"):
            return r.content
        gid = r.json().get("id")
        if not gid:
            raise VoiceLabError(f"VoiceLab: в ответе нет id генерации: {r.text[:300]}")
        t0 = time.time()
        while time.time() - t0 < 300:
            g = requests.get(f"{self.base}/v1/tts/generations/{gid}", headers=self._headers(lang), timeout=30)
            g.raise_for_status()
            js = g.json()
            if js.get("status") == "completed" and js.get("audio_url"):
                a = requests.get(js["audio_url"], timeout=self.timeout)
                a.raise_for_status()
                return a.content
            if js.get("status") == "failed":
                raise VoiceLabError(f"VoiceLab: генерация {gid} не удалась: {str(js)[:300]}")
            time.sleep(1.5)
        raise VoiceLabError(f"VoiceLab: генерация {gid} не завершилась за 300 c")

    def _wait(self, task_id, lang, limit=300):
        t0 = time.time()
        while time.time() - t0 < limit:
            r = requests.get(f"{self.base}/api/v1/tts/status/{task_id}/", headers=self._headers(lang), timeout=30)
            r.raise_for_status()
            js = r.json()
            st = str(js.get("status", "")).upper()
            if st == "SUCCESS":
                return js.get("audio_path") or js.get("audio_url")
            if st == "FAILED":
                raise VoiceLabError(f"VoiceLab: задача {task_id} FAILED: {js}")
            time.sleep(2)
        raise VoiceLabError(f"VoiceLab: задача {task_id} не завершилась за {limit} c")
