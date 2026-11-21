"""Вежливый HTTP-клиент: пауза между запросами, повторы, кэш сырого HTML на диске."""

from __future__ import annotations

import gzip
import time
from pathlib import Path

import httpx

from . import BASE_URL

USER_AGENT = "lexuz-index-prototype/0.1 (+personal research; polite crawler, 1 req/s)"


class PoliteClient:
    def __init__(self, raw_dir: Path, delay: float = 1.0, timeout: float = 60.0, retries: int = 3):
        self.raw_dir = raw_dir
        self.delay = delay
        self.retries = retries
        self._last = 0.0
        self.requests_made = 0
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,uz;q=0.8,en;q=0.5"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    # --- низкоуровневые запросы -------------------------------------------------
    def _wait(self) -> None:
        pause = self.delay - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                resp = self.client.request(method, url, **kw)
                self._last = time.monotonic()
                self.requests_made += 1
            except httpx.HTTPError as e:  # таймаут, обрыв соединения
                self._last = time.monotonic()
                err = e
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else 5.0 * (attempt + 1)
                    err = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    time.sleep(min(wait, 120))
                    continue
                resp.raise_for_status()
                return resp
            time.sleep(3.0 * (attempt + 1))
        assert err is not None
        raise err

    def get_text(self, url: str, **kw) -> str:
        r = self.request("GET", url, **kw)
        r.encoding = "utf-8"
        return r.text

    def post_text(self, url: str, data: dict) -> str:
        r = self.request("POST", url, data=data)
        r.encoding = "utf-8"
        return r.text

    # --- документы с кэшем -------------------------------------------------------
    def raw_path(self, lang: str, doc_id: int) -> Path:
        return self.raw_dir / lang / f"{doc_id}.html.gz"

    def get_document(self, lang: str, doc_id: int, force: bool = False) -> tuple[str, bool]:
        """Возвращает (html, from_cache)."""
        path = self.raw_path(lang, doc_id)
        if path.exists() and not force:
            return gzip.decompress(path.read_bytes()).decode("utf-8"), True
        html = self.get_text(f"/{lang}/docs/{doc_id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(html.encode("utf-8"), compresslevel=6))
        return html, False


def read_cached(raw_dir: Path, lang: str, doc_id: int) -> str | None:
    path = raw_dir / lang / f"{doc_id}.html.gz"
    if not path.exists():
        return None
    return gzip.decompress(path.read_bytes()).decode("utf-8")
