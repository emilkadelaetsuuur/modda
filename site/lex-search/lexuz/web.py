"""Веб-интерфейс поиска: стандартная библиотека Python, без зависимостей."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import os

from . import eksport, i18n, passages, rolik, store
from .search import get_unit, search

PAGE = Path(__file__).with_name("ui.html")
SCRIPTS = {"cyr", "lat", "new"}
ORIGIN = os.getenv("LEX_ORIGIN", "https://nasa.cartify.ink")  # откуда принимаем формы заказа роликов


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    local = threading.local()
    export_dir = db_path.parent / "eksport"
    guard = eksport.Guard(db_path.parent / "eksport.auth")

    def db() -> sqlite3.Connection:
        con = getattr(local, "con", None)
        if con is None:
            con = local.con = store.connect(db_path)
        return con

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # тише в консоли
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _file(self, path: Path) -> None:
            """Отдаёт файл целиком или по Range (перемотка видео)."""
            size = path.stat().st_size
            start, end, code = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and any(m.groups()):
                start = int(m[1]) if m[1] else max(0, size - int(m[2]))
                end = min(size - 1, int(m[2])) if m[1] and m[2] else size - 1
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", eksport.content_type(path))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if path.suffix in (".zip", ".md", ".txt", ".json") or self.headers.get("X-Download"):
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _authorized(self) -> bool:
            ok = guard.check(self.headers.get("Authorization", ""))
            if ok is None:
                self._send(404, b"not found", "text/plain")
                return False
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Raddiyalar", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def _export(self, rel: str, query: dict) -> None:
            if not self._authorized():
                return
            if rel == "":
                return self._send(200, eksport.page(export_dir), "text/html; charset=utf-8")
            if rel == "rolik":
                try:
                    code, body = rolik.form_page(db(), int(query.get("rid", "")))
                except ValueError:
                    code, body = 400, b"rid?"
                return self._send(code, body, "text/html; charset=utf-8")
            path = eksport.resolve(export_dir, unquote(rel))
            if path is None:
                return self._send(404, b"not found", "text/plain")
            self._file(path)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/eksport":
                self.send_response(301)
                self.send_header("Location", "eksport/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path.startswith("/eksport/"):
                try:
                    return self._export(url.path[len("/eksport/"):], q)
                except (BrokenPipeError, ConnectionResetError):
                    return
            lang = q.get("lang") if q.get("lang") in ("ru", "uz") else None
            script = q.get("script") if q.get("script") in SCRIPTS else "lat"
            try:
                if url.path == "/":
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/search":
                    opts = dict(only_active=q.get("active", "1") == "1", category=q.get("cat") or None, script=script)
                    res = search(db(), q.get("q", ""), limit=min(int(q.get("limit", 20)), 100),
                                 offset=int(q.get("offset", 0)), lang=lang, **opts)
                    if lang and (res["total"] == 0 or res["mode"] != "all") and not int(q.get("offset", 0)):
                        # на выбранном языке пусто или только частичные совпадения (узбекский запрос
                        # в русских текстах и наоборот) — подскажем, сколько найдётся на другом
                        other = "ru" if lang == "uz" else "uz"
                        alt = search(db(), q.get("q", ""), limit=1, lang=other, **opts)
                        if alt["total"] and (res["total"] == 0 or alt["mode"] == "all"):
                            res["elsewhere"] = {"lang": other, "total": alt["total"]}
                    self._json(res)
                elif url.path == "/api/modda":
                    # поисковый API «базы команды» для генератора роликов MODDA
                    found = passages.find(db(), q.get("q", ""), limit=min(int(q.get("limit", 5)), 10),
                                          lang=lang, script=script)
                    self._json({"results": found})
                elif url.path == "/api/unit":
                    unit = get_unit(db(), int(q["rid"]), q.get("q"), script)
                    self._json(unit or {"error": "not found"}, 200 if unit else 404)
                elif url.path == "/api/stats":
                    self._json(store.stats(db(), lang))
                elif url.path == "/api/i18n":
                    self._json(i18n.strings(q.get("ui", "uz-lat")))
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # покажем ошибку в интерфейсе, а не уроним сервер
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            """Заказ ролика о законе: только с этой же страницы (Origin) и под паролем выгрузки."""
            if urlparse(self.path).path != "/eksport/rolik":
                return self._send(404, b"not found", "text/plain")
            if not self._authorized():
                return
            origin = self.headers.get("Origin", "")
            if origin not in (ORIGIN, f"http://{self.headers.get('Host', '')}"):
                return self._send(403, b"forbidden", "text/plain")
            n = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 < n <= 4096:
                return self._send(400, b"bad request", "text/plain")
            ok, error, rid = rolik.save_order(db(), db_path.parent, self.rfile.read(n))
            if not ok:
                code, body = rolik.form_page(db(), rid, error) if rid is not None else (400, error.encode())
                return self._send(400 if code == 200 else code, body, "text/html; charset=utf-8")
            self.send_response(303)
            self.send_header("Location", "./")
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Поиск открыт: http://{host}:{port}  (Ctrl+C — остановить)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
