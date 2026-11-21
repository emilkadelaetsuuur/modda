"""MODDA studio desk. Run behind the existing HTTPS vhost.

The studio is open for demos: anyone can view the queue, search the law base and order a video within a daily
demo limit, because each Flow scene is a paid generation. The owner (laptop shortcut ticket or private code) has no
demo limit and alone can import passages into the team base."""
import hashlib
import hmac
import json
import os
from contextlib import closing
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

import catalog

DATA=Path(os.environ.get("MODDA_DATA","data"))
ORIGIN=os.environ.get("MODDA_ORIGIN","https://nasa.cartify.ink")
CAP=180*1024*1024
DEMO_DAILY=int(os.environ.get("MODDA_DEMO_DAILY","6"))
DEMO_SCENES=int(os.environ.get("MODDA_DEMO_SCENES","3"))
# A finished video of 15, 30 or 45 seconds: Flow clips are 8 s, each clip gives one or two shots of the montage.
DURATIONS={15:3,30:5,45:7}
DEMO_SECONDS=int(os.environ.get("MODDA_DEMO_SECONDS","45"))
QUEUE=8
PHASES={"script":"Подготовка сценария","flow":"Генерация сцен","render":"Монтаж на ноутбуке","upload":"Загрузка MP4"}
# Progress and attention texts come from the laptop and are shown to visitors: no service or model names.
VENDOR_WORDS=((re.compile(r"\b(?:в|во) (?:Google )?Flow\b"),"в генераторе сцен"),(re.compile(r"\b(?:Google )?Flow(?:’da)?\b"),"генератор сцен"),
    (re.compile(r"\bVeo(?: \d(?:\.\d)?)?(?: ?-? ?(?:Lite|Fast|Quality))?\b"),"видеомодель"),(re.compile(r"\bNano Banana(?: Pro)?\b"),"генератор кадров"),
    (re.compile(r"\bClaude(?: Desktop| Code)?\b"),"ассистент"),(re.compile(r"\bVoiceLab\b"),"озвучка"))
def public_text(s):
    for pattern,word in VENDOR_WORDS:s=pattern.sub(word,s)
    return s

def db():
    c=sqlite3.connect(DATA/"studio.sqlite",timeout=12)
    c.row_factory=sqlite3.Row
    return c

def init():
    DATA.mkdir(parents=True,exist_ok=True)
    (DATA/"videos").mkdir(exist_ok=True)
    with closing(db()) as c,c:
        c.execute("PRAGMA journal_mode=WAL")
        catalog.init(c)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tickets(digest TEXT PRIMARY KEY,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,created INTEGER NOT NULL,
        updated INTEGER NOT NULL,spec TEXT NOT NULL,sources TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
        phase TEXT NOT NULL DEFAULT '',message TEXT NOT NULL DEFAULT '',lease TEXT NOT NULL DEFAULT '',
        bytes INTEGER NOT NULL DEFAULT 0,sha TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS heartbeat(id INTEGER PRIMARY KEY, seen INTEGER NOT NULL, info TEXT NOT NULL);
        """)
def digest(s):return hashlib.sha256(s.encode()).hexdigest()
def same(a,b):return isinstance(a,str) and bool(b) and hmac.compare_digest(a.encode(),b.encode())
def public_job(r):
    return dict(id=r["id"],created=r["created"],updated=r["updated"],status=r["status"],phase=r["phase"],
                message=r["message"],bytes=r["bytes"],spec=json.loads(r["spec"]),sources=json.loads(r["sources"]),
                video="/studio/video/"+r["id"] if r["status"]=="done" else None)
def demo_used(c):
    return c.execute("SELECT count(*) FROM jobs WHERE created>? AND json_extract(spec,'$.by')='demo'",(int(time.time())-86400,)).fetchone()[0]
def make_job(c,d,fixture=False,by="owner"):
    rid=d.get("request_id","")
    if not isinstance(rid,str) or not re.fullmatch("[a-f0-9-]{24,48}",rid):raise ValueError("Нужен идентификатор запроса.")
    old=c.execute("SELECT * FROM jobs WHERE request_id=?",(rid,)).fetchone()
    if old:return public_job(old)
    prompt=d.get("prompt")
    if not isinstance(prompt,str) or not 3<=len(prompt.strip())<=1500:raise ValueError("Введите задание от 3 до 1500 символов.")
    spec={"prompt":prompt.strip(),"kind":"fixture" if fixture else "flow"}
    for key,choices in (("format",catalog.FORMATS),("style",catalog.STYLES),("lang",("ru","uz")),("aspect",("9:16","16:9")),("corpus",("demo","team"))):
        val=d.get(key)
        if not isinstance(val,str) or val not in choices:raise ValueError("Проверьте параметры ролика.")
        spec[key]=val
    if "duration" in d:
        if type(d["duration"]) is not int or d["duration"] not in DURATIONS:raise ValueError("Выберите длительность: 15, 30 или 45 секунд.")
        spec["duration"]=d["duration"];spec["scenes"]=DURATIONS[d["duration"]]
    else:  # older callers (refutations, the search's order button) still send a number of scenes
        if type(d.get("scenes")) is not int or not 1<=d["scenes"]<=7:raise ValueError("Выберите от 1 до 7 сцен.")
        spec["scenes"]=d["scenes"];spec["duration"]=15 if d["scenes"]<=3 else 30 if d["scenes"]<=5 else 45
        if spec["format"]!="refute":spec["scenes"]=DURATIONS[spec["duration"]]  # a page opened before the 15/30/45 update still sends scenes
    if by=="demo":
        if spec["duration"]>DEMO_SECONDS:raise ValueError(f"В демо-режиме — ролики до {DEMO_SECONDS} секунд.")
        if demo_used(c)>=DEMO_DAILY:raise ValueError(f"Дневной лимит демо исчерпан: {DEMO_DAILY} роликов за сутки. Каждая сцена — платная генерация.")
        spec["by"]="demo"
    sources=catalog.find(c,prompt,spec["corpus"])
    if not sources:raise ValueError("В выбранной базе нет подходящей нормы. Уточните тему или добавьте источник.")
    if c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','active')").fetchone()[0]>=QUEUE:raise ValueError("Дождитесь освобождения очереди.")
    key=secrets.token_hex(12);now=int(time.time())
    c.execute("INSERT INTO jobs(id,request_id,created,updated,spec,sources) VALUES(?,?,?,?,?,?)",(key,rid,now,now,json.dumps(spec,ensure_ascii=False),json.dumps(sources,ensure_ascii=False)))
    return public_job(c.execute("SELECT * FROM jobs WHERE id=?",(key,)).fetchone())

class Desk(BaseHTTPRequestHandler):
    server_version="MODDA"
    def log_message(self,fmt,*args):pass
    def reply(self,status,body,kind="application/json; charset=utf-8",extra=()):
        data=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header("Content-Type",kind);self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store");self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for a,b in extra:self.send_header(a,b)
        self.end_headers()
        if self.command!="HEAD":self.wfile.write(data)
    def body(self,limit=48000):
        n=int(self.headers.get("Content-Length","0"))
        if not 1<=n<=limit:raise ValueError("Неверный размер запроса.")
        j=json.loads(self.rfile.read(n))
        if not isinstance(j,dict):raise ValueError("Ожидается JSON-объект.")
        return j
    def owner(self,c):
        s=SimpleCookie();s.load(self.headers.get("Cookie",""))
        token=s.get("modda_session")
        return token and c.execute("SELECT 1 FROM sessions WHERE digest=? AND expires>?",(digest(token.value),int(time.time()))).fetchone()
    def worker(self):
        return same(self.headers.get("Authorization",""),"Bearer "+os.environ["MODDA_WORKER_KEY"])
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        path=urlsplit(self.path).path
        if path in ("/studio","/studio/","/studio/studio.js","/studio/studio.css"):
            name=path.rsplit("/",1)[-1] if path.endswith((".js",".css")) else "studio.html"
            mime={"studio.js":"application/javascript","studio.css":"text/css","studio.html":"text/html"}[name]
            return self.reply(200,Path(__file__).with_name(name).read_bytes(),mime+"; charset=utf-8")
        with closing(db()) as c:
            if path=="/studio/api/status":
                hb=c.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
                return self.reply(200,dict(formats=catalog.FORMATS,styles=catalog.STYLES,viewer="owner" if self.owner(c) else "demo",
                    demo=dict(daily=DEMO_DAILY,scenes=DEMO_SCENES,seconds=DEMO_SECONDS,used=demo_used(c)),queue=QUEUE,
                    durations={str(k):v for k,v in DURATIONS.items()},
                    counts=dict(c.execute("SELECT corpus,count(*) FROM passages GROUP BY corpus")),
                    external=catalog.external_configured(),worker=dict(hb) if hb else None,
                    jobs=[public_job(r) for r in c.execute("SELECT * FROM jobs ORDER BY created DESC,rowid DESC LIMIT 30")]))
            if re.fullmatch(r"/studio/video/[a-f0-9]{24}",path):
                key=path.split("/")[-1]
                r=c.execute("SELECT bytes FROM jobs WHERE id=? AND status='done'",(key,)).fetchone()
                file=DATA/"videos"/(key+".mp4")
                if not r or not file.is_file():return self.reply(404,{})
                n=r[0];start=0;end=n-1;status=200
                requested=self.headers.get("Range")
                if requested:
                    m=re.fullmatch(r"bytes=(\d*)-(\d*)",requested)
                    if not m or not any(m.groups()):return self.reply(416,{},extra=[("Content-Range",f"bytes */{n}")])
                    start=int(m[1]) if m[1] else max(0,n-int(m[2]));end=min(n-1,int(m[2])) if m[1] and m[2] else n-1
                    if start>end:return self.reply(416,{},extra=[("Content-Range",f"bytes */{n}")])
                    status=206
                self.send_response(status);self.send_header("Content-Type","video/mp4");self.send_header("Accept-Ranges","bytes")
                if status==206:self.send_header("Content-Range",f"bytes {start}-{end}/{n}")
                self.send_header("Content-Length",str(end-start+1));self.send_header("Cache-Control","private, no-store");self.end_headers()
                if self.command!="HEAD":
                    with file.open("rb") as f:
                        f.seek(start);left=end-start+1
                        while left:
                            buf=f.read(min(262144,left))
                            if not buf:break
                            self.wfile.write(buf);left-=len(buf)
                return
        self.reply(404,{})
    def do_POST(self):
        try:self.post()
        except (ValueError,UnicodeError,KeyError,TypeError) as e:self.reply(400,{"error":str(e)[:250]})
        except sqlite3.Error:self.reply(503,{"error":"Хранилище временно недоступно."})
        except (BrokenPipeError,ConnectionResetError):pass
    def post(self):
        path=urlsplit(self.path).path
        if not path.startswith("/studio/api/"):return self.reply(404,{})
        op=path.removeprefix("/studio/api/")
        private=op.startswith("worker/")
        if private and not self.worker():return self.reply(403,{})
        if not private and self.headers.get("Origin")!=ORIGIN:return self.reply(403,{})
        if op=="worker/upload":return self.upload()
        if self.headers.get_content_type()!="application/json":return self.reply(415,{})
        data=self.body(8_000_000 if op=="import" else 48000);now=int(time.time())
        with closing(db()) as c,c:
            if op=="worker/pair":
                ticket=secrets.token_urlsafe(32);c.execute("DELETE FROM tickets WHERE expires<?",(now,))
                c.execute("INSERT INTO tickets VALUES(?,?)",(digest(ticket),now+90));c.commit()
                return self.reply(200,{"ticket":ticket})
            if op=="login":
                t=data.get("ticket","");valid=False
                if isinstance(t,str) and t:valid=c.execute("DELETE FROM tickets WHERE digest=? AND expires>?",(digest(t),now)).rowcount==1
                if not valid and not same(data.get("code"),os.environ["MODDA_OWNER_KEY"]):return self.reply(403,{"error":"Код не подошёл или ссылка истекла."})
                token=secrets.token_urlsafe(32);c.execute("DELETE FROM sessions WHERE expires<?",(now,))
                c.execute("INSERT INTO sessions VALUES(?,?)",(digest(token),now+43200));c.commit()
                return self.reply(200,{},extra=[("Set-Cookie","modda_session="+token+"; HttpOnly; Secure; SameSite=Strict; Path=/studio; Max-Age=43200")])
            owner=private or self.owner(c)
            if op=="search":return self.reply(200,{"sources":catalog.find(c,str(data.get("prompt",""))[:1500],data.get("corpus"))})
            if op=="import":
                if not owner:return self.reply(403,{"error":"Загрузка в базу команды доступна только с ноутбука владельца."})
                if not isinstance(data.get("passages"),list) or not 1<=len(data["passages"])<=2000:raise ValueError("Нужно от 1 до 2000 выдержек.")
                n=catalog.ingest(c,"team",data["passages"]);c.commit();return self.reply(200,{"imported":n})
            if op in ("jobs","worker/fixture"):
                c.execute("BEGIN IMMEDIATE")
                job=make_job(c,data,op=="worker/fixture",by="owner" if owner else "demo");c.commit();return self.reply(201,job)
            if op=="worker/poll":
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,?,?)",(now,str(data.get("info",""))[:160]))
                c.execute("""UPDATE jobs SET status='attention',message='Ноутбук прекратил обновлять задание. Проверьте результат на ноутбуке перед повторным запуском.'
                          WHERE status='active' AND updated<?""",(now-300,))
                r=None
                if data.get("claim") is True and not c.execute("SELECT 1 FROM jobs WHERE status='active'").fetchone():
                    r=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
                job=None
                if r:
                    lease=secrets.token_urlsafe(32)
                    c.execute("UPDATE jobs SET lease=?,status='active',updated=?,phase='script' WHERE id=?",(lease,now,r["id"]))
                    job=dict(public_job(r),lease=lease)
                c.commit();return self.reply(200,{"job":job})
            if op=="worker/update":
                phase=data.get("phase","")
                if phase not in (*PHASES,"attention"):raise ValueError("Неизвестный этап.")
                n=c.execute("""UPDATE jobs SET phase=?,updated=?,message=?,status=? WHERE id=? AND lease=? AND status='active'""",
                  (phase,now,public_text(str(data.get("message","")))[:450],"attention" if phase=="attention" else "active",data.get("id"),data.get("lease"))).rowcount
                c.commit();return self.reply(200 if n else 409,{})
        self.reply(404,{})
    def upload(self):
        key=self.headers.get("X-Job","");lease=self.headers.get("X-Lease","")
        if not re.fullmatch("[a-f0-9]{24}",key) or not lease:return self.reply(400,{})
        n=int(self.headers.get("Content-Length","0"));expected=self.headers.get("X-SHA256","")
        if not 0<n<=CAP or not re.fullmatch("[a-f0-9]{64}",expected):return self.reply(400,{})
        with closing(db()) as c:
            r=c.execute("SELECT * FROM jobs WHERE id=? AND lease=?",(key,lease)).fetchone()
            if not r:return self.reply(403,{})
            if r["status"]=="done" and same(r["sha"],expected):return self.reply(200,{"ok":True})
            if r["status"]!="active":return self.reply(409,{})
        tmp=None
        try:
            with tempfile.NamedTemporaryFile(dir=DATA/"videos",suffix=".part",delete=False) as f:
                tmp=Path(f.name);left=n;sha=hashlib.sha256()
                while left:
                    b=self.rfile.read(min(262144,left))
                    if not b:raise ValueError("Загрузка прервана.")
                    f.write(b);sha.update(b);left-=len(b)
            if not same(sha.hexdigest(),expected):raise ValueError("Контрольная сумма файла не совпала.")
            p=subprocess.run(["ffprobe","-v","error","-protocol_whitelist","file","-f","mov","-show_entries","stream=codec_type:format=duration","-of","json",str(tmp)],capture_output=True,timeout=15)
            meta=json.loads(p.stdout or b"{}");types={s["codec_type"] for s in meta.get("streams",[])}
            if p.returncode or not {"audio","video"}<=types or not 0<float(meta.get("format",{}).get("duration",0))<=900:
                raise ValueError("Нужен читаемый MP4 с видео и звуком.")
            with closing(db()) as c,c:
                c.execute("BEGIN IMMEDIATE")
                if not c.execute("SELECT 1 FROM jobs WHERE id=? AND lease=? AND status='active'",(key,lease)).fetchone():return self.reply(409,{})
                os.replace(tmp,DATA/"videos"/(key+".mp4"));tmp=None
                c.execute("UPDATE jobs SET status='done',phase='',message='',updated=?,bytes=?,sha=? WHERE id=?",(int(time.time()),n,expected,key))
            return self.reply(200,{"ok":True})
        finally:
            if tmp:tmp.unlink(missing_ok=True)

if __name__=="__main__":
    if not all(len(os.environ.get(x,""))>=32 for x in ("MODDA_OWNER_KEY","MODDA_WORKER_KEY")):raise SystemExit("Set private operator and worker credentials")
    init()
    ThreadingHTTPServer((os.environ.get("MODDA_BIND","127.0.0.1"),int(os.environ.get("PORT","18883"))),Desk).serve_forever()
