"""Laptop worker: personal native Claude Code, browser downloads, MODDA montage and delivery."""
import argparse
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from urllib.parse import urlsplit
import webbrowser

HOME=Path(os.getenv("MODDA_HOME",Path(os.environ["LOCALAPPDATA"])/"ModdaBridge"))
ENGINE=HOME/"engine"; CODE=HOME/"bridge"; PRIVATE=HOME/"private"
# Montage runs in MODDA AI Studio: VoiceLab voices per character, word-timed captions (faster-whisper), sfx, music.
STUDIO=Path(os.getenv("MODDA_STUDIO",Path(os.environ["LOCALAPPDATA"])/"ModdaStudio"/"modda-ai-studio"))
STUDIO_PY=STUDIO/".venv"/"Scripts"/"python.exe"
if STUDIO_PY.is_file():ENGINE=STUDIO
BASE="https://nasa.cartify.ink/studio"
def key():return (PRIVATE/"worker.key").read_text(encoding="ascii").strip()
def post(op,data):
    req=Request(BASE+"/api/worker/"+op,data=json.dumps(data).encode(),headers={"Authorization":"Bearer "+key(),"Content-Type":"application/json"})
    with urlopen(req,timeout=25) as r:return json.load(r)
def write(path,data):
    t=path.with_suffix(".writing");t.write_text(json.dumps(data,ensure_ascii=False),encoding="utf8");t.replace(path)
def read(path):return json.loads(path.read_text(encoding="utf8"))
def update(job,phase,message):
    return post("update",dict(id=job["id"],lease=job["lease"],phase=phase,message=message))
def exe():
    for path in [HOME/"node_modules/@anthropic-ai/claude-code/bin/claude.exe",HOME/"node_modules/@anthropic-ai/claude-code-win32-x64/claude.exe"]:
        if path.is_file():return str(path)
    return shutil.which("claude") or "claude"
def env():
    values={k:v for k,v in os.environ.items() if not (k.startswith(("ANTHROPIC_","CLAUDE_CODE_OAUTH","MODDA_WORKER","MODDA_OWNER")) or k in ("GEMINI_API_KEY","GOOGLE_API_KEY"))}
    values.update(MODDA_HOME=str(HOME),MODDA_PYTHON=sys.executable,PYTHONUTF8="1",PYTHONIOENCODING="utf-8",HF_HUB_OFFLINE="1")
    return values
def assemble(root,checkpoint):
    for command in ("check","voice","build"):
        checkpoint("render","MODDA: "+{"check":"проверка сценария","voice":"подготовка звуковых дорожек","build":"сборка MP4"}[command])
        logfile=root/("modda-"+command+".log")
        with logfile.open("wb") as out:
            e=env()
            if ENGINE==STUDIO:py=str(STUDIO_PY)
            else:py=sys.executable;e["PYTHONPATH"]=os.pathsep.join((str(ENGINE),str(HOME/"deps")))
            proc=subprocess.run([py,"-X","utf8","-m","studio",command,str(root)],cwd=ENGINE,env=e,stdout=out,stderr=subprocess.STDOUT,timeout=840)
        log=logfile.read_text(encoding="utf8",errors="replace")
        if proc.returncode or "ставлю заглушку" in log:
            raise ValueError("MODDA: этап "+command+" не пройден. Подробности в "+logfile.name)
    name=read(root/"scenario.json")["id"]
    file=root/"out"/(name+".mp4")
    if not file.is_file():raise ValueError("MODDA не вернула MP4.")
    p=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",str(file)],capture_output=True,text=True)
    try:checkpoint("render",f"MODDA: ролик собран, {float(p.stdout):.1f} с — озвучка, субтитры, звуки и музыка.")
    except ValueError:pass
    return file
def upload(job,file):
    with file.open("rb") as f:checksum=hashlib.file_digest(f,"sha256").hexdigest()
    target=urlsplit(BASE)
    for attempt in range(3):
        try:
            with closing(http.client.HTTPSConnection(target.hostname,timeout=350)) as conn,file.open("rb") as fp:
                conn.request("POST",target.path+"/api/worker/upload",fp,headers={"Authorization":"Bearer "+key(),"X-Job":job["id"],"X-Lease":job["lease"],"X-SHA256":checksum,"Content-Length":str(file.stat().st_size),"Content-Type":"video/mp4"})
                res=conn.getresponse();res.read()
                if res.status==200:return
                if res.status<500:raise ValueError("Сайт отклонил файл: HTTP "+str(res.status))
                raise OSError("Сервер временно недоступен")
        except OSError:
            if attempt==2:raise
            time.sleep(3+attempt*4)
def claude(root,j,checkpoint):
    proc=None
    config=root/".modda-mcp.json"
    write(config,{"mcpServers":{"modda":{"command":shutil.which("node") or "node","args":[str(CODE/"tools.mjs")],"env":{"MODDA_JOB_DIR":str(root),"MODDA_HOME":str(HOME),"MODDA_PYTHON":sys.executable}}}})
    instructions=("Personal MODDA video job. First call mcp__modda__brief. Follow its source and budget constraints. "
      "Use mcp__modda__scenario, native Claude in Chrome to operate Google Flow, then mcp__modda__attach per MP4 and mcp__modda__finish. "
      "Only use tabs for Google Flow. Do not visit other sites, access accounts or change subscriptions. "
      "If the extension, Google login, browser permission, or budget blocks the task, record the exact blocker via mcp__modda__attention. "
      "Use only one Flow generation for each requested scene. Do not retry a paid generation automatically.")
    cmd=[exe(),"--chrome","--strict-mcp-config","--mcp-config",str(config),"--tools","","--allowedTools",
         "mcp__modda__brief,mcp__modda__scenario,mcp__modda__start_clip,mcp__modda__attach,mcp__modda__finish,mcp__modda__attention","--",instructions]
    # This is the operator's native, interactive client. It can display first-use permissions.
    try:
        proc=subprocess.Popen(cmd,cwd=CODE,env=env(),creationflags=subprocess.CREATE_NEW_CONSOLE)
        start=time.monotonic()
        while time.monotonic()-start<1800:
            if (root/"attention.json").is_file():raise ValueError(read(root/"attention.json")["message"])
            if (root/"ready.json").is_file():return
            if proc.poll() is not None:raise ValueError("Сессия сборки закрылась до получения клипов. Проверьте вход и разрешение браузера.")
            if (root/"progress.json").is_file():
                p=read(root/"progress.json");checkpoint(p["phase"],p["message"])
            elif time.monotonic()-start>45:checkpoint("script","Проверьте окно сборки на ноутбуке: возможен запрос доверия или разрешения.")
            time.sleep(3)
        raise ValueError("Сессия не завершила задание за 30 минут. Проверьте уже созданные клипы.")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:proc.kill()
def desktop_job(root, job, checkpoint):
    pending = HOME / "desktop-pending.json"
    write(pending, {"id": job["id"], "expires": time.time() + 5400})
    checkpoint("script", "Ожидает приём заказов на ноутбуке. В папке MODDA запустите /modda или цикл /loop 2m /modda.")
    try:
        # A 45-second order is 7 frames and 7 clips in Flow with a save dialog per clip: allow 90 minutes.
        for _ in range(1800):
            if (root / "attention.json").exists():
                raise ValueError(read(root / "attention.json")["message"])
            if (root / "ready.json").exists():
                return
            if (root / "progress.json").exists():
                status = read(root / "progress.json")
                checkpoint(status["phase"], status["message"])
            time.sleep(3)
        raise ValueError("Приём заказов не завершил задание за 90 минут. Перед новым запуском проверьте созданные клипы.")
    finally:
        pending.unlink(missing_ok=True)

def handle(job):
    root=HOME/"jobs"/job["id"];root.mkdir(parents=True,exist_ok=True)
    write(root/"brief.json",{**{k:job[k] for k in ("id","spec","sources")},"started":time.time()})
    state=["script","Ноутбук принял задание"];stop=threading.Event()
    def beat():
        while not stop.wait(12):
            try:post("poll",{"claim":False,"info":"выполняет задание"});update(job,*state)
            except Exception:pass
    def checkpoint(phase,message):
        if state!=[phase,message]:
            state[:]=[phase,message]
            try:update(job,phase,message)
            except OSError:pass
    th=threading.Thread(target=beat,daemon=True);th.start()
    try:
        if job["spec"]["kind"]=="fixture":
            source=ENGINE/"examples/pirotexnika_ru"
            for name in ("scenario.json","law.txt","voice_src","clips","frames"):
                obj=source/name
                if obj.is_dir():shutil.copytree(obj,root/name,dirs_exist_ok=True)
                else:shutil.copy2(obj,root/name)
        elif (HOME/"use-desktop").exists():desktop_job(root,job,checkpoint)
        else:claude(root,job,checkpoint)
        file=assemble(root,checkpoint)
        checkpoint("upload","Ноутбук отправляет проверенный MP4.")
        upload(job,file)
    except Exception as e:
        msg=str(e)[:420]
        write(root/"worker-error.json",{"message":msg})
        try:update(job,"attention",msg)
        except Exception:pass
    finally:stop.set();th.join(2)
def main():
    parser=argparse.ArgumentParser();parser.add_argument("mode",choices=["run","open","fixture","desktop","chrome"]);a=parser.parse_args()
    if a.mode=="desktop":
        (HOME/"use-desktop").touch()
        # Desktop's auto-open command requires CLI login. The native folder picker also
        # works for operators who only signed in to the Desktop app.
        auth = subprocess.run([exe(),"auth","status"], cwd=CODE, env=env(), capture_output=True)
        if auth.returncode == 0:
            subprocess.Popen([exe(),"--desktop"], cwd=HOME/"desktop", env=env(), creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            os.startfile(str(HOME/"desktop"/"Начать здесь.txt"))

        return
    if a.mode=="chrome":
        (HOME/"use-desktop").unlink(missing_ok=True)
        subprocess.Popen([exe(), "--chrome", "--", "/chrome"], cwd=CODE, env=env(), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    if a.mode=="open":
        ticket=post("pair",{})["ticket"];webbrowser.open(BASE+"/#ticket="+ticket);return
    if a.mode=="fixture":
        j=post("fixture",dict(request_id=uuid.uuid4().hex,prompt="Пиротехника в Узбекистане · проверка возврата архивного ролика",format="explainer",style="bright",lang="ru",aspect="9:16",scenes=1,corpus="demo"))
        print(j["id"]);return
    import msvcrt
    lock=(HOME/"worker.lock").open("a+b");lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:return
    while True:
        try:
            claim = True
            info = "готов принимать задания"
            if (HOME/"use-desktop").exists():
                pulse = HOME/"desktop-online"
                claim = pulse.exists() and time.time()-pulse.stat().st_mtime < 190
                info = "приём заказов на связи" if claim else "ожидает команду /modda на ноутбуке"
            j=post("poll",{"claim":claim,"info":info})["job"]
            if j:handle(j)
        except Exception as e:
            (HOME/"connection.log").write_text(str(e)[:350],encoding="utf8")
        time.sleep(5)
if __name__=="__main__":main()
