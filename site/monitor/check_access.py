"""Test real read access without printing credentials or consuming Telegram updates."""

import argparse
from datetime import datetime, timedelta, timezone
import os

from app import DEFAULT_SEARCH_QUERY, load_settings
from meta_sources import meta_probe
from public_sources import youtube_discover
from telegram_source import telegram_probe


def check(source):
    if source == "youtube":
        key = os.getenv("YOUTUBE_API_KEY", "").strip()
        if not key:
            raise ValueError("Для тематического поиска нужен YOUTUBE_API_KEY. Лента канала работает без ключа.")
        since = datetime.now(timezone.utc) - timedelta(days=7)
        query = os.getenv("YOUTUBE_SEARCH_QUERY") or DEFAULT_SEARCH_QUERY
        rows = youtube_discover(key, query, since.isoformat(timespec="seconds"))
        return f"Тематический поиск доступен. В ответе {len(rows)} видео."
    if source == "telegram":
        return telegram_probe()
    return meta_probe(source)


def main():
    sources = ("youtube", "telegram", "instagram", "facebook")
    parser = argparse.ArgumentParser(description="Проверить доступ к источнику. Проверка YouTube использует один поисковый запрос.")
    parser.add_argument("source", nargs="?", choices=(*sources, "all"), default="all")
    choice = parser.parse_args().source
    load_settings()
    failed = False
    for source in sources if choice == "all" else (choice,):
        try:
            print(f"{source} — {check(source)}")
        except ValueError as exc:
            print(f"{source} — {exc}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
