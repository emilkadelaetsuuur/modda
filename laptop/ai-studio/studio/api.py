"""Интеграция MODDA AI Studio в другие сервисы (бот, сайт, детектор).

1) Как библиотека Python:
    from studio.api import Studio
    st = Studio()                                    # читает config.yaml и .env
    st.verify("projects/x", law_url, post_text)     # вердикт по посту
    st.video("projects/x", law_url=..., lang="uz", audience="teens")     # мультфильм/моушн 9:16
    st.infographic("projects/x", theme="official")  # инфографика 16:9 (нужен scenario.json kind=infographic)
    st.post("projects/x", lang="uz", theme="official")                  # карусель + текст поста

2) Как HTTP-сервис:  python -m studio serve --port 8080
    POST /api/make         {"project": "...", "law_url": "...", "kind": "infographic|post|cartoon", "lang": "uz", "theme": "official"}
    POST /api/verify       {"project": "...", "law_url": "...", "text": "пост"}             -> вердикт сразу
    POST /api/post         {"project": "...", "law_url": "...", "lang": "uz", "theme": "official", "topic": "..."}
    POST /api/video        {"project": "...", "law_url": "...", "lang": "ru", "audience": "teens", "format": "explainer"}
    POST /api/infographic  {"project": "...", "theme": "official"}
    POST /api/refute       {"project": "...", "law_url": "...", "fake": "текст фейка", "lang": "uz"}
    GET  /api/jobs/<id>    статус задачи: queued | running | done | error, в done есть пути к файлам
    GET  /files/<путь>     скачать готовый файл (только внутри папки projects_root)
Тяжёлые задачи (post, video, infographic, refute) возвращают {"job": id} и идут в фоне.
"""
import argparse
import json
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import assemble, config, imagegen, infographic, lexuz, posts, scriptwriter, videogen, voice
from .project import Project


class Studio:
    def __init__(self, config_path=None):
        self.config_path = config_path

    def _cfg(self, project_dir):
        return config.load(project_dir, self.config_path)

    def _law(self, p, cfg, law_url):
        if law_url and (not p.law_path.exists() or law_url != self._meta(p).get("law_url")):
            doc = lexuz.get(law_url, cfg)
            p.law_path.write_text(doc["text"], encoding="utf-8")
            self._save_meta(p, law_url=doc["url"], law_title=doc["title"])

    @staticmethod
    def _meta(p):
        f = p.dir / "project.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def _save_meta(self, p, **kw):
        m = self._meta(p); m.update(kw)
        (p.dir / "project.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

    def verify(self, project_dir, law_url, text):
        p, cfg = Project(project_dir), self._cfg(project_dir)
        self._law(p, cfg, law_url)
        res = scriptwriter.verdict(text, p.law(), cfg)
        (p.dir / "verdict.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        return res

    def post(self, project_dir, law_url=None, lang="ru", theme="official", fmt="4x5", topic="", rewrite=False):
        p, cfg = Project(project_dir), self._cfg(project_dir)
        self._law(p, cfg, law_url)
        name = "post.json" if lang == "ru" else f"post_{lang}.json"
        if rewrite or not (p.dir / name).exists():
            posts.write(p, cfg, lang, theme, fmt, topic, law_url or "", name)
        post = json.loads((p.dir / name).read_text(encoding="utf-8"))
        report = scriptwriter.check(posts.as_scenario(post), p.law(), cfg)
        errors = [m for l, m in report if l == "error"]
        out_dir, files = posts.render(p, cfg, name, theme, fmt)
        return {"slides": [str(f) for f in files], "caption": post.get("caption", ""), "preview": str(out_dir / "preview.jpg"),
                "fact_errors": errors}

    def video(self, project_dir, law_url=None, lang="ru", audience="teens", fmt="explainer", style=None, topic="",
              fake="", aspect=None, clips_mode=None):
        p, cfg = Project(project_dir), self._cfg(project_dir)
        self._law(p, cfg, law_url)
        if not p.scenario_path.exists():
            scriptwriter.write(p, cfg, law_url or "", lang, audience, fmt, style, topic, fake)
        scn = p.load()
        if aspect:
            scn["aspect"] = aspect; p.save(scn)
        errors = [m for l, m in scriptwriter.check(scn, p.law(), cfg) if l == "error"]
        if errors:
            return {"status": "fact_errors", "fact_errors": errors, "scenario": str(p.scenario_path)}
        imagegen.run(p, cfg)
        videogen.run(p, cfg, mode=clips_mode)
        voice.run(p, cfg)
        out, T = assemble.build(p, cfg)
        return {"video": str(out), "duration": round(T, 2), "scenario": str(p.scenario_path)}

    def infographic(self, project_dir, theme=None):
        p, cfg = Project(project_dir), self._cfg(project_dir)
        voice.run(p, cfg)
        out, T = infographic.render(p, cfg, theme)
        return {"video": str(out), "duration": round(T, 2)}

    def make(self, project_dir, law_url=None, kind="infographic", lang="ru", audience="adults", fmt="explainer",
             theme="official", style=None, aspect=None, topic="", post_format="4x5", rewrite=False):
        """То же, что python -m studio make: закон -> инфографика | пост | мультфильм."""
        from . import make as mk
        p, cfg = Project(project_dir), self._cfg(project_dir)
        try:
            return mk.run(p, cfg, law_url, kind, lang, audience, fmt, theme, style, aspect, topic, post_format, rewrite)
        except SystemExit as e:  # понятная остановка: нет ключа, ошибки фактов, нет кадров
            return {"status": "stopped", "reason": str(e), "flow_playbook": str(p.sub("tasks") / "flow_playbook.md")}

    def refute(self, project_dir, law_url, fake, lang="ru", audience="adults", make_post=True):
        p = Project(project_dir)
        (p.dir / "fake.txt").write_text(fake, encoding="utf-8")
        res = {"verdict": self.verify(project_dir, law_url, fake)}
        if res["verdict"].get("verdict") == "true":
            return res
        if make_post:
            res["post"] = self.post(project_dir, law_url, lang, topic=res["verdict"].get("refute_topic", ""))
        res["video"] = self.video(project_dir, law_url, lang, audience, "refute", topic=res["verdict"].get("refute_topic", ""), fake=fake)
        return res


# ---------------- HTTP ----------------
JOBS = {}


def _urls(obj, root):
    """Пути к файлам внутри root дополняем ссылками /files/..., чтобы бот или сайт мог их скачать."""
    if isinstance(obj, dict):
        out = {k: _urls(v, root) for k, v in obj.items()}
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith(str(root)):
                out[k + "_url"] = "/files/" + str(Path(v).relative_to(root))
            if isinstance(v, list) and v and all(isinstance(x, str) and x.startswith(str(root)) for x in v):
                out[k + "_urls"] = ["/files/" + str(Path(x).relative_to(root)) for x in v]
        return out
    return obj


def _run_job(jid, fn, kw, root=None):
    JOBS[jid]["status"] = "running"
    try:
        res = fn(**kw)
        JOBS[jid].update(status="done", result=_urls(res, root) if root else res)
    except Exception as e:  # noqa
        JOBS[jid].update(status="error", error=str(e), trace=traceback.format_exc()[-2000:])


def make_handler(st: Studio, root: Path):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj=None, raw=None, ctype="application/json; charset=utf-8"):
            body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _project(self, data):
            name = Path(data.get("project") or uuid.uuid4().hex[:8]).name  # только имя, без выхода за root
            return str(root / name)

        def do_GET(self):
            if self.path.startswith("/api/jobs/"):
                j = JOBS.get(self.path.rsplit("/", 1)[-1])
                return self._send(200 if j else 404, j or {"error": "no such job"})
            if self.path.startswith("/files/"):
                f = (root / self.path[len("/files/"):]).resolve()
                if root.resolve() not in f.parents or not f.is_file():
                    return self._send(404, {"error": "not found"})
                ct = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg", ".txt": "text/plain; charset=utf-8",
                      ".json": "application/json"}.get(f.suffix, "application/octet-stream")
                return self._send(200, raw=f.read_bytes(), ctype=ct)
            if self.path in ("/", "/api"):
                return self._send(200, {"service": "MODDA AI Studio", "endpoints": ["/api/make", "/api/verify", "/api/post", "/api/video",
                                                                                  "/api/infographic", "/api/refute", "/api/jobs/<id>", "/files/<path>"]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            proj = self._project(data)
            route = self.path.rstrip("/")
            if route == "/api/verify":
                try:
                    return self._send(200, st.verify(proj, data.get("law_url"), data["text"]))
                except Exception as e:  # noqa
                    return self._send(500, {"error": str(e)})
            jobs = {
                "/api/make": (st.make, ["law_url", "kind", "lang", "audience", "fmt", "theme", "style", "aspect", "topic",
                                        "post_format", "rewrite"]),
                "/api/post": (st.post, ["law_url", "lang", "theme", "fmt", "topic", "rewrite"]),
                "/api/video": (st.video, ["law_url", "lang", "audience", "fmt", "style", "topic", "fake", "aspect", "clips_mode"]),
                "/api/infographic": (st.infographic, ["theme"]),
                "/api/refute": (st.refute, ["law_url", "fake", "lang", "audience", "make_post"]),
            }
            if route not in jobs:
                return self._send(404, {"error": "not found"})
            fn, keys = jobs[route]
            if route == "/api/video" and "format" in data:
                data["fmt"] = data["format"]
            kw = {"project_dir": proj, **{k: data[k] for k in keys if k in data}}
            jid = uuid.uuid4().hex[:10]
            JOBS[jid] = {"status": "queued", "route": route, "project": Path(proj).name}
            threading.Thread(target=_run_job, args=(jid, fn, kw, root), daemon=True).start()
            self._send(202, {"job": jid, "status_url": f"/api/jobs/{jid}"})

        def log_message(self, fmt, *args):
            print("[api]", fmt % args)

    return H


def serve(port=8080, root="projects", config_path=None, host="0.0.0.0"):
    rootp = Path(root).resolve()
    rootp.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), make_handler(Studio(config_path), rootp))
    print(f"MODDA AI Studio API: http://{host}:{port}  (проекты в {rootp})")
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--root", default="projects")
    a = ap.parse_args()
    serve(a.port, a.root)
