"""Legal passage importer and search contract shared by the studio and worker."""
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import os

FORMATS={"explainer":"Закон простыми словами","refute":"Проверка спорного утверждения","myth_fact":"Миф или факт","quick_rule":"Правило за 15 секунд","dialog_scene":"Диалог героев","quiz":"Викторина"}
STYLES={"bright":"Яркая анимация","stickman":"Рисованные человечки","cartoon3d":"3D-мультфильм","kids_soft":"Мягкая иллюстрация","elder_clear":"Контрастная графика","doc_infographic":"Инфографика","flat_motion":"Плоская анимация","isometric":"Изометрия","kinetic_type":"Кинетическая типографика","line_art":"Линейный рисунок","paper_cut":"Бумажная аппликация"}
STOP=set("создай сделай сделайте расскажи покажи короткий короткое короткое ролик видео узбекистане узбекистан узбекистана закон законодательство пожалуйста про для или как что это один ролика секунд мне языке русском узбекском простой простыми словами мультик модда факты факт тему пример объясни".split())

def init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS passages(id INTEGER PRIMARY KEY, corpus TEXT NOT NULL,title TEXT NOT NULL,
      article TEXT NOT NULL,url TEXT NOT NULL,as_of TEXT NOT NULL,text TEXT NOT NULL,UNIQUE(corpus,url,article,as_of))""")
    c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passage_search USING fts5(title,article,text,tokenize='unicode61')")

def validate(x):
    if not isinstance(x,dict): raise ValueError("Требуется объект источника.")
    o={}
    for k,limit in (("title",450),("article",300),("url",1500),("as_of",10),("text",16000)):
        v=x.get(k)
        if not isinstance(v,str) or not v.strip() or len(v)>limit: raise ValueError("Проверьте поле источника "+k)
        o[k]=v.strip()
    u=urlsplit(o["url"])
    if u.scheme!="https" or u.netloc.lower() not in ("lex.uz","www.lex.uz") or not re.fullmatch(r"/(?:(?:ru|uz|oz|en)/)?(?:docs|acts)/-?\d+/?",u.path):
        raise ValueError("Нужна прямая HTTPS-ссылка на документ Lex.uz.")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d",o["as_of"]) or date.fromisoformat(o["as_of"])>date.today():
        raise ValueError("Укажите проверенную дату источника в формате ГГГГ-ММ-ДД.")
    if len(o["text"])<20: raise ValueError("Слишком короткая выдержка из закона.")
    return o

def ingest(c,corpus,rows):
    docs=[validate(x) for x in rows]
    for d in docs:
        row=c.execute("""INSERT INTO passages(corpus,title,article,url,as_of,text) VALUES(?,?,?,?,?,?)
        ON CONFLICT(corpus,url,article,as_of) DO UPDATE SET title=excluded.title,text=excluded.text RETURNING id""",
        (corpus,*(d[k] for k in ("title","article","url","as_of","text")))).fetchone()[0]
        c.execute("DELETE FROM passage_search WHERE rowid=?",(row,))
        c.execute("INSERT INTO passage_search(rowid,title,article,text) VALUES(?,?,?,?)",(row,d["title"],d["article"],d["text"]))
    return len(docs)

CONNECTION = Path(__file__).with_name("law-search.private.json")


class DirectRequest(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def external_configured():
    return bool(os.getenv("MODDA_LAW_SEARCH_URL")) or CONNECTION.is_file()


def external_settings():
    if endpoint := os.getenv("MODDA_LAW_SEARCH_URL"):
        return endpoint, os.getenv("MODDA_LAW_SEARCH_TOKEN", "")
    try:
        config = json.loads(CONNECTION.read_text(encoding="utf8"))
        endpoint, token = config["url"], config.get("token", "")
        if not isinstance(endpoint, str) or not isinstance(token, str):
            raise ValueError()
        return endpoint, token
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("Проверьте файл подключения базы команды на сервере.") from None


def find(c,query,corpus):
    if corpus not in ("demo","team"): raise ValueError("Неизвестная база.")
    if corpus=="team" and external_configured():
        endpoint, token = external_settings()
        parsed = urlsplit(endpoint)
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.scheme not in ("http","https") or (token and parsed.scheme!="https"):
            raise ValueError("Проверьте URL поискового API. Для Bearer-токена требуется HTTPS.")
        headers={"Accept":"application/json"}
        if token: headers["Authorization"]="Bearer "+token
        req=Request(endpoint+("&" if "?" in endpoint else "?")+urlencode({"q":query,"limit":5}),headers=headers)
        try:
            open_request = build_opener(DirectRequest()).open if token else urlopen
            with open_request(req,timeout=12) as response:
                payload = response.read(512001)
            if len(payload)>512000:raise ValueError()
            data=json.loads(payload)
            if not isinstance(data.get("results"),list): raise ValueError()
            return [validate(item) for item in data["results"][:5]]
        except Exception:
            raise ValueError("Поисковый API базы команды недоступен или вернул неверный формат.") from None
    numbered = re.search(r"\b(?:ст(?:атья|атьи|атью|атье|атьёй|атьей|\.)|модда)\s*(\d{1,4})\b|\b(\d{1,4})\s*[-–]\s*modda\b", query, re.I)
    article_num = next((v for v in numbered.groups() if v), None) if numbered else None
    terms=[]
    for word in re.findall(r"[^\W\d_]{3,}",query.casefold(),re.UNICODE):
        if word in STOP: continue
        word=word[:6] if len(word)>6 else word
        if word not in terms: terms.append(word)
    if not terms and not article_num:return []
    match=" OR ".join('"'+w+'"*' for w in terms[:40])
    if article_num:
        # Restrict the candidates first, then rank using the rest of the subject when provided.
        exact = 'article:"' + article_num + '"'
        exact_hits = c.execute("""SELECT p.title,p.article,p.url,p.as_of,p.text FROM passage_search
            JOIN passages p ON p.id=passage_search.rowid WHERE passage_search MATCH ? AND p.corpus=?
            ORDER BY bm25(passage_search,3,5,1),p.as_of DESC LIMIT 5""",
            (exact + (" AND (" + match + ")" if match else ""),corpus)).fetchall()
        if not exact_hits and match:
            exact_hits = c.execute("""SELECT p.title,p.article,p.url,p.as_of,p.text FROM passage_search
                JOIN passages p ON p.id=passage_search.rowid WHERE passage_search MATCH ? AND p.corpus=?
                ORDER BY p.as_of DESC,p.id LIMIT 5""",(exact,corpus)).fetchall()
        return [dict(zip(("title","article","url","as_of","text"),r)) for r in exact_hits]
    rows=c.execute("""SELECT p.title,p.article,p.url,p.as_of,p.text FROM passage_search
        JOIN passages p ON p.id=passage_search.rowid WHERE passage_search MATCH ? AND p.corpus=?
        ORDER BY bm25(passage_search,1,4,2),p.as_of DESC LIMIT 5""",(match,corpus))
    return [dict(zip(("title","article","url","as_of","text"),r)) for r in rows]
