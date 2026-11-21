"""Текст закона с lex.uz.

Пока нет официального API: парсим публичную страницу документа.
Когда LEX.UZ даст API, впишите lexuz.api_url в config.yaml (функция fetch_api).
"""
import re

import requests

UA = {"User-Agent": "Mozilla/5.0 (ModdaAIStudio/1.0; +https://lex.uz)"}


def doc_id(url_or_id: str) -> str:
    m = re.search(r"docs/(-?\d+)", str(url_or_id))
    if m:
        return m.group(1)
    if re.fullmatch(r"-?\d+", str(url_or_id).strip()):
        return str(url_or_id).strip()
    raise ValueError(f"Не понял номер документа lex.uz: {url_or_id}")


def page_url(url_or_id, lang="ru"):
    did = doc_id(url_or_id)
    prefix = "" if lang in ("", "uz") else f"/{lang}"
    return f"https://lex.uz{prefix}/docs/{did}"


def html_to_text(html: str) -> dict:
    from bs4 import BeautifulSoup

    s = BeautifulSoup(html, "html.parser")
    title = s.title.get_text(" ", strip=True) if s.title else ""
    root = s.select_one("#divCont") or s.select_one(".docBody") or s.body or s
    for junk in root.select(".INDEXES_ON_REF, .lx_elem2, .lx_elem3, script, style, .lx_no_select"):
        junk.decompose()
    blocks = root.select(".lx_elem")
    if blocks:
        lines = [b.get_text(" ", strip=True) for b in blocks]
    else:
        lines = root.get_text("\n", strip=True).splitlines()
    lines = [re.sub(r"\s+", " ", l).strip() for l in lines]
    lines = [l for l in lines if l]
    status = ""
    st = s.select_one(".docBody")
    if st:
        first = st.get_text(" ", strip=True)[:80]
        if "кучга кирган эмас" in first or "не вступил" in first:
            status = "not_in_force_yet"
    return {"title": title, "text": "\n".join(lines), "status": status}


def fetch(url_or_id, lang="ru", timeout=30) -> dict:
    url = page_url(url_or_id, lang)
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    out = html_to_text(r.text)
    out["url"] = url
    return out


def fetch_api(url_or_id, cfg) -> dict:
    """Заготовка под официальный LEX.UZ API: формат ответа уточнить, когда выдадут доступ."""
    from .config import secret

    sec = cfg["lexuz"]
    key = secret(sec)
    r = requests.get(f"{sec['api_url'].rstrip('/')}/documents/{doc_id(url_or_id)}",
                     headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    js = r.json()
    return {"title": js.get("title", ""), "text": js.get("text", ""), "url": page_url(url_or_id), "status": ""}


def get(url_or_id, cfg):
    if cfg["lexuz"].get("api_url"):
        return fetch_api(url_or_id, cfg)
    return fetch(url_or_id, cfg["lexuz"].get("lang", "ru"))
