"""Загрузка настроек: config.yaml (рядом с проектом или в корне) + переменные окружения + .env."""
import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
ASSETS = ROOT / "assets"

DEFAULT = {
    "llm": {
        "provider": "anthropic",          # anthropic | openai_compat
        "model": "claude-sonnet-5-5",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
        "max_tokens": 8000,
        "temperature": 0.7,
    },
    "lexuz": {
        "lang": "ru",                     # ru | uz | oz (латиница)
        "api_url": "",                    # официальный LEX.UZ API, когда дадут доступ
        "api_key_env": "LEXUZ_API_KEY",
    },
    "image": {
        "mode": "manual",                 # api | manual
        "model": "gemini-2.5-flash-image",
        "api_key_env": "GEMINI_API_KEY",
        "aspect_ratio": "9:16",
    },
    "video": {
        "mode": "manual",                 # api | manual | still
        "backend": "gemini",              # gemini | vertex
        "model": "veo-3.1-generate-001",
        "api_key_env": "GEMINI_API_KEY",
        "project": "",
        "location": "us-central1",
        "resolution": "720p",
        "duration": 8,
        "generate_audio": False,
        "poll_seconds": 10,
    },
    "tts": {
        "provider": "voicelab",           # voicelab | file | mock
        "base_url": "https://back.aisha.group",
        "api_key_env": "VOICELAB_API_KEY",
        "timeout": 120,
        "voice_ids": {},                  # speaker -> voice_id, перекрывает voices.yaml
        "trim_silence": True,
    },
    "align": {"whisper_model": "small", "device": "cpu", "compute_type": "int8"},
    "render": {
        "width": 1080, "height": 1920, "fps": 30, "crf": 18, "preset": "veryfast",
        "lead": 0.35, "gap": 0.08, "outro": 1.6,
        "saturation": 1.12, "contrast": 1.04, "push_in": 0.07,
        "brand": "MODDA AI", "brand_color": [15, 139, 110],
        "music_volume": 0.30, "whoosh_volume": 0.28, "loudness": -14,
    },
    "brv": 412000,
}


def _merge(a, b):
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(project_dir=None, path=None):
    _load_dotenv(ROOT / ".env")
    cfg = DEFAULT
    candidates = [Path(path)] if path else []
    candidates += [ROOT / "config.yaml"]
    if project_dir:
        candidates += [Path(project_dir) / "config.yaml"]
        _load_dotenv(Path(project_dir) / ".env")
    for c in candidates:
        if c and c.exists():
            cfg = _merge(cfg, yaml.safe_load(c.read_text(encoding="utf-8")) or {})
    return cfg


def secret(section: dict, required=True):
    env = section.get("api_key_env", "")
    val = os.environ.get(env, "")
    if required and not val:
        raise RuntimeError(f"Нет ключа: задайте переменную окружения {env} (или впишите в .env)")
    return val


def load_yaml(name):
    return yaml.safe_load((PROMPTS / name).read_text(encoding="utf-8"))


def load_text(name):
    return (PROMPTS / name).read_text(encoding="utf-8")
