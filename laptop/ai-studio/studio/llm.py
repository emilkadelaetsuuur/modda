"""Один интерфейс к LLM: Claude API или любая локальная модель с OpenAI-совместимым API
(vLLM, Ollama, LM Studio, llama.cpp server). Переключение в config.yaml: llm.provider."""
import json
import re

import requests

from .config import secret


def chat(system: str, user: str, cfg: dict) -> str:
    c = cfg["llm"]
    if c["provider"] == "anthropic":
        key = secret(c)
        r = requests.post(
            c.get("base_url", "https://api.anthropic.com").rstrip("/") + "/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": c["model"], "max_tokens": c.get("max_tokens", 8000),
                  "temperature": c.get("temperature", 0.7), "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=600,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:500]}")
        return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

    if c["provider"] == "openai_compat":
        key = secret(c, required=False) or "local"
        r = requests.post(
            c["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": c["model"], "temperature": c.get("temperature", 0.7),
                  "max_tokens": c.get("max_tokens", 8000),
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=900,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"LLM {r.status_code}: {r.text[:500]}")
        return r.json()["choices"][0]["message"]["content"]

    raise ValueError(f"Неизвестный llm.provider: {c['provider']}")


def extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise
