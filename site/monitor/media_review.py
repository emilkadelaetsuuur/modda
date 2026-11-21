"""Inspect the bytes of a video; keep the transcript and sampled frame times separate."""
from contextlib import closing
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

import auto_law

MAX_BYTES = 48 * 1024 * 1024
MAX_SECONDS = 600
MAX_UPLOAD_SECONDS = 180
FRAME_LANGS = "rus+uzb+eng"


def _run(cmd, timeout):
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError("Не удалось обработать медиафайл.")
    return result.stdout


def _api(method, path, headers, body=None):
    with closing(http.client.HTTPSConnection("api.voicelab.uz", timeout=30)) as client:
        client.request(method, path, body=body, headers=headers)
        response = client.getresponse()
        raw = response.read(4 * 1024 * 1024 + 1)
        if not 200 <= response.status < 300:
            raise ValueError(f"Сервис распознавания речи вернул HTTP {response.status}.")
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Ответ сервиса распознавания речи слишком большой.")
        return json.loads(raw) if raw else {}


def transcribe(audio, duration, language="auto"):
    key = os.environ.get("VOICELAB_API_KEY")
    if not key:
        raise ValueError("Ключ распознавания речи не настроен.")
    boundary = "lex" + uuid.uuid4().hex
    opts = {"language":language, "include_speakers":"false"}
    body = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode() for k,v in opts.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio.mp3"\r\n'
             'Content-Type: audio/mpeg\r\n\r\n').encode() + audio + f'\r\n--{boundary}--\r\n'.encode()
    auth = {"Authorization": "Bearer " + key}
    fingerprint = hashlib.sha256(audio).hexdigest() + language
    submitted = _api("POST", "/v1/stt", {**auth, "Content-Type":"multipart/form-data; boundary="+boundary,
                     "Idempotency-Key":str(uuid.uuid5(uuid.NAMESPACE_URL, "lex-voice-v1/"+fingerprint))}, body)
    job = str(submitted.get("id",""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job):
        raise ValueError("Сервис распознавания речи не вернул номер расшифровки.")
    path = "/v1/stt/transcriptions/" + job
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        reply = _api("GET", path, auth)
        if reply.get("status") == "failed":
            raise ValueError("Сервис распознавания речи не смог распознать запись.")
        if reply.get("status") == "completed":
            transcript = reply.get("transcript") or ""
            segments = []
            for cue in reply.get("segments") or []:
                start, end = cue.get("start_ms"), cue.get("end_ms")
                text = cue.get("text")
                if isinstance(text,str) and type(start) in (float,int) and type(end) in (float,int) and 0 <= start < end and start/1000 < duration:
                    segments.append({"start":start/1000, "end":min(duration,end/1000), "text":text.strip()})
            segments = auto_law._segments(segments, MAX_SECONDS)
            return {"text":transcript, "segments":segments, "language":reply.get("language",""),
                    "status":"ok" if transcript.strip() or segments else "empty"}
        time.sleep(1.5)
    raise ValueError("Сервис распознавания речи пока не завершил расшифровку.")


def inspect_file(path, language="auto", max_seconds=MAX_SECONDS, reserve=None):
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("Размер MP4 должен быть до 48 МиБ.")
    local = ["-protocol_whitelist","file","-f","mov","-enable_drefs","0","-use_absolute_path","0"]
    info = json.loads(_run(["ffprobe","-v","error",*local,"-show_entries","format=duration:stream=codec_type,width,height",
                           "-of","json",str(path)], 15))
    duration = float(info["format"].get("duration") or 0)
    streams = info.get("streams",[])
    if not math.isfinite(duration) or not 0 < duration <= max_seconds:
        raise ValueError(f"Для проверки нужен ролик длительностью до {max_seconds // 60} минут.")
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos or any(not 0 < s.get("width",0)*s.get("height",0) <= 3840*2160 for s in videos):
        raise ValueError("Нужен MP4 с видеодорожкой разрешением до 4K.")
    errors = []
    speech = dict(text="", segments=[], language="", status="no_audio")
    ff = ["ffmpeg","-nostdin","-v","error","-threads","2",*local,"-i",str(path)]
    if any(s.get("codec_type")=="audio" for s in streams):
        try:
            audio = _run([*ff,"-map","0:a:0","-vn","-t",str(duration),"-ac","1","-ar","16000","-f","mp3","pipe:1"], 50)
            if reserve:
                reserve(math.ceil(duration))
            speech = transcribe(audio, duration, language)
        except (ValueError, OSError, http.client.HTTPException, subprocess.SubprocessError) as exc:
            errors.append(str(exc) if isinstance(exc,ValueError) else "Сервис распознавания речи недоступен.")
            speech["status"] = "error"
    frames = []
    step = 2 if duration <= 60 else 5 if duration <= 180 else 15
    scanned = 0
    try:
        with tempfile.TemporaryDirectory(prefix="lex-ocr-") as scratch:
            root = Path(scratch)
            for sec in range(0,math.ceil(duration),step):
                image = root / f"frame-{sec:05d}.png"
                _run([*ff[:ff.index("-i")],"-ss",str(sec),*ff[ff.index("-i"):],"-map","0:v:0","-frames:v","1",
                      "-vf","scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease",
                      "-threads","1",str(image)], 15)
                if image.exists():
                    frames.append((sec,image))
            if not frames:
                raise ValueError("Из видео не удалось извлечь кадры.")
            listing = root / "frames.txt"
            listing.write_text("\n".join(str(f) for _, f in frames),encoding="utf8")
            output = _run(["tesseract",str(listing),"stdout","-l",FRAME_LANGS,"--psm","11"], 135).decode("utf8")
            pages = output.split("\f")
            if len(pages) < len(frames):
                raise ValueError("OCR вернул неполную выборку кадров.")
            scanned = len(frames)
            frames = [dict(start=sec, text=txt.strip()[:3500]) for (sec,_),txt in zip(frames,pages) if txt.strip()]
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        frames = []
        errors.append(str(exc) if isinstance(exc,ValueError) else "Распознавание кадров недоступно.")
    return dict(speech=speech,frames=frames,frames_scanned=scanned,frame_step=step,duration=round(duration,3),errors=errors)


def download_public(video_id, root):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Некорректный идентификатор YouTube.")
    cmd = ["yt-dlp","--ignore-config","--no-playlist","--no-progress","--no-warnings","--retries","0",
           "--fragment-retries","0","--socket-timeout","12","--max-filesize","48M",
           "--match-filter","duration <= 600 & !is_live","-f",
           "best[ext=mp4][vcodec!=none][acodec!=none][height<=720]/bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]",
           "--merge-output-format","mp4","-o",str(root/"source.%(ext)s"),"--","https://www.youtube.com/watch?v="+video_id]
    try:
        _run(cmd,75)
    except (ValueError,OSError,subprocess.SubprocessError):
        raise ValueError("YouTube не предоставил медиафайл для проверки звука и кадров.") from None
    source = root/"source.mp4"
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES:
        raise ValueError("Полный MP4 недоступен в пределах 10 минут и 48 МиБ.")
    return source
