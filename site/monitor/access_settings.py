"""Write-only local credentials for the monitoring desk."""

import json
import os
import re
import tempfile
import threading
from telegram_delivery import valid_destination


FIELDS = {"YOUTUBE_API_KEY", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN", "IG_USER_ID",
          "IG_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "BRAND24_IMAP_HOST", "BRAND24_IMAP_USER",
          "BRAND24_IMAP_PASSWORD", "BRAND24_IMAP_FOLDER", "APIFY_TOKEN", "AUTO_META_DISCOVERY", "AUTO_YOUTUBE_DISCOVERY",
          "TELEGRAM_ALERT_CHAT_ID"}
_lock = threading.Lock()


def save_settings(path, fields):
    if not isinstance(fields, dict) or fields.keys() - FIELDS:
        raise ValueError("Неизвестные поля настроек.")
    updates = {}
    for key, value in fields.items():
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError("Проверьте значение параметра.")
        if not value:
            continue
        if key.startswith("BRAND24_IMAP_"):
            if not value.isprintable() or value.strip() != value:
                raise ValueError("Почтовая настройка должна быть одной строкой без крайних пробелов.")
        elif any(not 33 <= ord(char) <= 126 for char in value):
            raise ValueError("Значение должно быть одной строкой без пробелов.")
        if key == "BRAND24_IMAP_HOST" and (len(value) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in value.split("."))):
            raise ValueError("Укажите имя IMAP-сервера без https:// и порта.")
        if key in {"FB_PAGE_ID", "IG_USER_ID"} and not value.isdigit():
            raise ValueError("ID аккаунта или Страницы должен состоять из цифр.")
        if key in {"AUTO_META_DISCOVERY", "AUTO_YOUTUBE_DISCOVERY"} and value not in {"0", "1"}:
            raise ValueError("Выберите режим автоматического поиска.")
        if key == "TELEGRAM_ALERT_CHAT_ID" and value != "off" and not valid_destination(value):
            raise ValueError("Укажите числовой ID чата или @имя канала Telegram.")
        updates[key] = value
    if not updates:
        raise ValueError("Заполните хотя бы одно поле.")

    with _lock:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        previous = dict(line.split("=", 1) for line in lines if "=" in line)
        if "BRAND24_IMAP_PASSWORD" not in updates and any(
                key in updates and updates[key] != previous.get(key)
                for key in ("BRAND24_IMAP_HOST", "BRAND24_IMAP_USER")):
            raise ValueError("Для другого сервера или почтового ящика введите пароль приложения заново.")
        kept = []
        for line in lines:
            key, sep, _ = line.strip().partition("=")
            if not sep or key not in updates:
                kept.append(line)
        kept.extend(f"{key}={value}" for key, value in updates.items())
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix="settings.", suffix=".tmp", delete=False) as file:
                tmp = file.name
                file.write("\n".join(kept) + "\n")
            os.replace(tmp, path)
        finally:
            if tmp is not None and os.path.exists(tmp):
                os.unlink(tmp)


def settings_request(handler, path):
    host = handler.headers.get_all("Host", [])
    origin = handler.headers.get_all("Origin", [])
    port = handler.server.server_port
    if (len(host) != 1 or host[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}
            or origin != [f"http://{host[0]}"]):
        return handler.reply(403, {"error": "Откройте форму в локальном кабинете."})
    if handler.headers.get_content_type() != "application/json":
        return handler.reply(415, {"error": "Ожидается JSON."})
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 56000:
            raise ValueError
        fields = json.loads(handler.rfile.read(length))
    except (ValueError, RecursionError):
        return handler.reply(400, {"error": "Некорректный запрос настроек."})
    try:
        save_settings(path, fields)
    except UnicodeError:
        return handler.reply(500, {"error": "Проверьте кодировку settings.env."})
    except ValueError as exc:
        return handler.reply(400, {"error": str(exc)})
    except OSError:
        return handler.reply(500, {"error": "Не удалось записать локальные настройки."})
    return handler.reply(200, {"message": "Настройки сохранены."})
