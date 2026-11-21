"""Recognize speech locally from the media URL supplied with a public Instagram Reel."""

import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


MAX_BYTES = 48 * 1024 * 1024
MAX_SECONDS = 180
_ROOT = Path(__file__).resolve().parent
_CDN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:cdninstagram\.com|fbcdn\.net)", re.I)
_STATUS = {
    "ok": "Речь распознана локально.",
    "no_speech": "Разборчивой речи не обнаружено.",
    "uncertain": "Речь распознана с недостаточной уверенностью для автоматической проверки.",
    "language": "Язык аудиодорожки не удалось уверенно определить как русский или узбекский.",
    "duration": "Длительность аудиодорожки превышает три минуты.",
    "decode": "Аудиодорожка недоступна или не читается.",
    "runtime": "Локальная модель распознавания речи недоступна.",
    "download": "Не удалось получить видеозапись с медиасервера площадки.",
    "size": "Размер видео выходит за пределы локальной обработки.",
    "timeout": "Превышено время локальной обработки видео.",
}


def media_url(value):
    if not isinstance(value, str) or not value.isascii() or not 1 <= len(value) <= 8192:
        return ""
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        return ""
    try:
        parts = urlsplit(value)
        if (parts.scheme == "https" and _CDN.fullmatch(parts.netloc) and parts.path.startswith("/")
                and len(parts.path) > 1 and not parts.fragment):
            return value
    except ValueError:
        pass
    return ""


def _download(url, target):
    parts = urlsplit(url)
    connection = http.client.HTTPSConnection(parts.netloc, timeout=12)
    deadline = time.monotonic() + 35
    try:
        # Direct GET only; redirects, credentials and cookies are never forwarded.
        connection.request("GET", parts.path + ("?" + parts.query if parts.query else ""))
        response = connection.getresponse()
        if response.status != 200:
            return "download"
        declared = response.getheader("Content-Length")
        if declared is not None and (not declared.isdecimal() or len(declared) > 12
                                     or not 0 < int(declared) <= MAX_BYTES):
            return "size"
        total = 0
        with target.open("wb") as stream:
            while True:
                if time.monotonic() > deadline:
                    return "timeout"
                block = response.read1(128 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_BYTES:
                    return "size"
                stream.write(block)
        if not total or declared is not None and total != int(declared):
            return "download"
        return "ok"
    finally:
        connection.close()


def _asr_python():
    path = os.environ.get("LEX_ASR_PYTHON")
    config = _ROOT / "data" / "asr-python.txt"
    if not path and config.is_file():
        path = config.read_text(encoding="utf-8-sig").strip()
    return path or sys.executable


def _recognize_file(video):
    # Force the MP4 demuxer and local input. Decode at most one extra second to
    # distinguish a complete short recording from the beginning of a longer one.
    child_options = dict(stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        decoded = subprocess.run([
            "ffmpeg", "-nostdin", "-loglevel", "error", "-xerror", "-protocol_whitelist", "file",
            "-f", "mov", "-enable_drefs", "0", "-i", str(video), "-map", "0:a:0", "-vn", "-sn",
            "-af", "aresample=16000:async=1:first_pts=0", "-ac", "1", "-ar", "16000",
            "-t", str(MAX_SECONDS + 1), "-f", "s16le", "pipe:1",
        ], timeout=35, **child_options)
    except OSError:
        return [], "decode"
    if decoded.returncode or not decoded.stdout:
        return [], "decode"
    if len(decoded.stdout) > 32000 * MAX_SECONDS:
        return [], "duration"
    child_options.pop("stdin")
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([_asr_python(), str(Path(__file__).resolve()), "--pcm"],
                            input=decoded.stdout, env=env, timeout=210, **child_options)
    if result.returncode:
        return [], "runtime"
    payload = json.loads(result.stdout)
    return payload["speech"], payload["status"]


def enrich(row):
    """Return a timed transcript or a non-sensitive status; never persist the media URL."""
    url = media_url(row.get("media_url"))
    speech, status = [], "download"
    permalink = row.get("url")
    if url and isinstance(permalink, str) and re.fullmatch(
            r"https://(?:www\.)?instagram\.com/reels?/[A-Za-z0-9_-]+/?", permalink):
        try:
            with tempfile.TemporaryDirectory(prefix="lex-reel-") as scratch:
                video = Path(scratch) / "media.mp4"
                status = _download(url, video)
                if status == "ok":
                    speech, status = _recognize_file(video)
        except (TimeoutError, subprocess.TimeoutExpired):
            status = "timeout"
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            status = "runtime" if status == "ok" else "download"
    return {"speech": speech, "speech_status": _STATUS.get(status, _STATUS["runtime"])}


def _transcribe():
    import numpy as np
    from faster_whisper import WhisperModel

    configured = os.environ.get("LEX_ASR_MODEL")
    if configured:
        model = Path(configured)
    else:
        cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
        cache = Path(os.environ.get("HF_HUB_CACHE", cache))
        repo = cache / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
        model = repo / "snapshots" / (repo / "refs" / "main").read_text().strip()
    if not (model / "model.bin").is_file():
        return [], "runtime"
    engine = WhisperModel(str(model), device="cpu", compute_type="int8", cpu_threads=6,
                          local_files_only=True)
    raw = sys.stdin.buffer.read(MAX_SECONDS * 32000 + 1)
    if not raw or len(raw) % 2 or len(raw) > MAX_SECONDS * 32000:
        return [], "duration"
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    segments, info = engine.transcribe(samples, beam_size=3, vad_filter=True, word_timestamps=True,
                                       condition_on_previous_text=False, temperature=0)
    return _trusted_segments(segments, info, len(samples) / 16000)


def _trusted_segments(segments, info, duration):
    if info.duration_after_vad < 0.3:
        return [], "no_speech"
    if info.language not in {"ru", "uz"} or info.language_probability < 0.65:
        return [], "language"
    speech = []
    for segment in segments:
        words = segment.words or []
        if (not words or segment.avg_logprob < -0.8 or segment.no_speech_prob > 0.55
                or segment.compression_ratio > 2.4 or any(word.probability < 0.23 for word in words)):
            # A low-confidence sentence could be a negation or attribution. Do not
            # assemble a seemingly affirmative claim from the remaining sentences.
            return [], "uncertain"
        start = words[0].start
        end = min(words[-1].end, duration)
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
            return [], "uncertain"
        speech.append(dict(start=round(start, 3), end=round(end, 3), text=segment.text.strip()))
    return (speech, "ok") if speech else ([], "no_speech")


if __name__ == "__main__" and sys.argv[1:] == ["--pcm"]:
    speech, status = _transcribe()
    print(json.dumps({"speech": speech, "status": status}, ensure_ascii=True))
