"""Rebrand nasa.cartify.ink: new logo, no "//" chips, no vendor names in visible text, paged feed, popular-first crawl.

usage: python3 sitepatch.py MONITOR_DIR STUDIO_DIR SEARCH_DIR [--write]
Every edit must match exactly; nothing is written unless all edits succeed (or were already applied)."""
import re
import sys
from pathlib import Path

MON, STU, SEA = (Path(a) for a in sys.argv[1:4])
WRITE = "--write" in sys.argv
V = "?v=2611"
files, log, crlf = {}, [], set()


def text(p):
    if p not in files:
        with open(p, encoding="utf8", newline="") as f:
            raw = f.read()
        if "\r\n" in raw:
            crlf.add(p)
        files[p] = raw.replace("\r\n", "\n")
    return files[p]


def sub(p, old, new, n=1):
    t = text(p)
    c = t.count(old)
    if c == 0 and (not new or new in t):
        log.append(f"already {p.name}: {old[:50]!r}")
        return
    if c != n:
        raise SystemExit(f"{p}: expected {n} x {old[:80]!r}, found {c}")
    files[p] = t.replace(old, new)


def resub(p, pattern, new, n=1):
    t = text(p)
    out, c = re.subn(pattern, new, t)
    if c != n:
        raise SystemExit(f"{p}: expected {n} x /{pattern}/, found {c}")
    files[p] = out


# 1. Logo and chips -------------------------------------------------------------------------------------------
LOGO = f'<img class="logo" src="/brand/modda-logo.png{V}" alt="Modda" width="131" height="26"></a>'
MARK = '<span class="mark" aria-hidden="true">§</span>Modda</a>'
CHIP = '.chip::before{content:"// "}.chip::after{content:" //"}'
pages = [MON / f"{n}.html" for n in ("deepfakes", "demo", "index", "live", "monitor", "setup", "video_search")]
for p in pages + [STU / "studio.html"]:
    sub(p, MARK, LOGO, n=text(p).count(MARK) or 2)
for p in pages + [STU / "studio.css"]:
    sub(p, CHIP, "")
    if ".logo{" not in text(p):
        resub(p, r"\.mark\{width:32px[^}]*\}", ".logo{height:22px}")
        resub(p, r"\.mark\{[^}]*\}", ".logo{display:block;height:26px;width:auto}")

sub(MON / "app.py", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self';",
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self';")

ui = SEA / "ui.html"
sub(ui, '<div class="brand"><b id="t-title"></b>',
    '<div class="brand"><a class="mlogo" href="/" aria-label="Modda"><picture>'
    f'<source srcset="/brand/modda-logo-light.png{V}" media="(prefers-color-scheme: dark)">'
    f'<img src="/brand/modda-logo.png{V}" alt="Modda" width="101" height="20"></picture></a><b id="t-title"></b>')
sub(ui, ".brand b { font-size: 18px; letter-spacing: -.01em; }",
    ".brand b { font-size: 18px; letter-spacing: -.01em; }\n.mlogo { display: inline-flex; }\n"
    ".mlogo img { display: block; height: 20px; width: auto; }")

# 2. Vendor names in visible text ------------------------------------------------------------------------------
sh = STU / "studio.html"
sub(sh, "рисует сцены в Google Flow, озвучивает", "рисует сцены, озвучивает")
sub(sh, "<strong>Сцены в Flow</strong>", "<strong>Сцены</strong>")
sub(sh, "Ролики собирает ноутбук с Claude Desktop и Google Flow.", "Ролики собирает ноутбук студии.")
sub(sh, "платная генерация в Google Flow (15 с", "платная генерация (15 с")
sj = STU / "studio.js"
sub(sj, "['flow','Google Flow']", "['flow','Сцены']")
sub(sj, "plural(clips,'сцена','сцены','сцен')+' в Flow',sec)", "plural(clips,'сцена','сцены','сцен'),sec)")
sub(sj, "'Ноутбук включён, но приём заказов в Claude Desktop не запущен", "'Ноутбук включён, но приём заказов не запущен")
ss = STU / "server.py"
sub(ss, '"flow":"Генерация в Google Flow"', '"flow":"Генерация сцен"')
sub(ss, "Каждая сцена — платная генерация в Flow.", "Каждая сцена — платная генерация.")
sub(ss, "Проверьте результат в Flow перед повторным запуском.", "Проверьте результат на ноутбуке перед повторным запуском.")
PUBLIC = '''
# Progress and attention texts come from the laptop and are shown to visitors: no service or model names.
VENDOR_WORDS=((re.compile(r"\\b(?:в|во) (?:Google )?Flow\\b"),"в генераторе сцен"),(re.compile(r"\\b(?:Google )?Flow(?:’da)?\\b"),"генератор сцен"),
    (re.compile(r"\\bVeo(?: \\d(?:\\.\\d)?)?(?: ?-? ?(?:Lite|Fast|Quality))?\\b"),"видеомодель"),(re.compile(r"\\bNano Banana(?: Pro)?\\b"),"генератор кадров"),
    (re.compile(r"\\bClaude(?: Desktop| Code)?\\b"),"ассистент"),(re.compile(r"\\bVoiceLab\\b"),"озвучка"))
def public_text(s):
    for pattern,word in VENDOR_WORDS:s=pattern.sub(word,s)
    return s
'''
if "def public_text" not in text(ss):
    cut = text(ss).index("\ndef ")
    files[ss] = files[ss][:cut] + PUBLIC + files[ss][cut:]
sub(ss, 'str(data.get("message",""))[:450]', 'public_text(str(data.get("message","")))[:450]')

ro = SEA / "rolik.py"
sub(ro, "Har bir sahna — Flow’da pullik generatsiya · ", "Har bir sahna — pullik generatsiya · ")
sub(ro, "Каждая сцена — платная генерация во Flow</span>", "Каждая сцена — платная генерация</span>")

mh = MON / "monitor.html"
sub(mh, "Звуковую дорожку распознаёт VoiceLab, надписи на выбранных кадрах — OCR.",
    "Речь распознаётся по звуковой дорожке, надписи — по выбранным кадрам.")
sub(mh, '"VoiceLab · " + (', '"Речь · " + (')
sub(mh, 'el("p","Речь · VoiceLab")', 'el("p","Речь")')
al = MON / "auto_law.py"
sub(al, 'f"Речь · VoiceLab · {_clock(start)}–{_clock(end)}"', 'f"Речь · {_clock(start)}–{_clock(end)}"')
sub(al, '"Речь · VoiceLab, без таймкода"', '"Речь, без таймкода"')
sub(MON / "autonomous_monitor.py", "Исчерпан суточный лимит минут VoiceLab.", "Исчерпан суточный лимит минут распознавания речи.")
mr = MON / "media_review.py"
for old, new in (('f"VoiceLab вернул HTTP', 'f"Сервис распознавания речи вернул HTTP'),
                 ("Ответ VoiceLab слишком большой.", "Ответ сервиса распознавания речи слишком большой."),
                 ("Серверный ключ VoiceLab не настроен.", "Ключ распознавания речи не настроен."),
                 ("VoiceLab не вернул номер расшифровки.", "Сервис распознавания речи не вернул номер расшифровки."),
                 ("VoiceLab не смог распознать запись.", "Сервис распознавания речи не смог распознать запись."),
                 ("VoiceLab пока не завершил расшифровку.", "Сервис распознавания речи пока не завершил расшифровку."),
                 ('"VoiceLab недоступен."', '"Сервис распознавания речи недоступен."')):
    sub(mr, old, new)
ap = MON / "app.py"
sub(ap, "Подключите почту для уведомлений Brand24 в настройках доступа.", "Подключите почту для уведомлений в настройках доступа.")
sub(ap, "Для автоматического поиска подключите Apify.", "Для автоматического поиска подключите поисковый сервис в настройках.")
sub(ap, "Для живого поиска введите токен Apify в настройках.", "Для живого поиска введите токен поиска в настройках.")
sub(MON / "brand24_mail.py", "Не удалось прочитать почту Brand24", "Не удалось прочитать почту уведомлений")
for p in (MON / "demo_instagram.py", MON / "facebook_video.py"):
    for old, new in (("Поисковику Apify", "Поисковому сервису"), ("Ответ Apify", "Ответ поискового сервиса"),
                     ("Соединение с Apify", "Соединение с поисковым сервисом"), ("лимит Apify", "лимит поискового сервиса")):
        if old in text(p):
            sub(p, old, new, n=text(p).count(old))
    t = text(p)
    files[p] = re.sub(r'(f?)"Apify ', r'\1"Поисковый сервис ', t)
ix = MON / "index.html"
sub(ix, 'brand24:"Brand24"}', 'brand24:"Почтовые уведомления"}')
sub(ix, 'brand24:"Brand24 · почта"}', 'brand24:"Почта · уведомления"}')
sub(ix, '"Из уведомления Brand24"', '"Из почтового уведомления"')
dm = MON / "demo.html"
sub(dm, "Лимит Apify — $0,05 за запуск.", "Лимит — $0,05 за запуск.")
sub(dm, "нужен токен Apify.", "нужен токен поиска.")
vs = MON / "video_search.html"
sub(vs, ">Настроить Apify<", ">Настроить доступ<")
sub(vs, '(state.hasToken ? "Apify подключён." : "Для поиска через Apify нужен токен.")',
    '(state.hasToken ? "Поиск подключён." : "Для поиска нужен токен.")')

# 3. Paged feed on / (deepfakes.html) ---------------------------------------------------------------------------
df = MON / "deepfakes.html"
sub(df, "#filter{width:auto;min-width:220px;border-radius:999px;padding-inline:18px}",
    "#filter,#sort{width:auto;min-width:200px;border-radius:999px;padding-inline:18px}\n"
    ".feedtools{display:flex;flex-wrap:wrap;gap:8px}#lenta{scroll-margin-top:100px}\n"
    ".pager{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:6px;margin:20px 0 4px}\n"
    ".pager button{min-width:42px;min-height:42px;padding:0 12px;border:1px solid var(--line-2);border-radius:999px;background:#fff;color:var(--ink);font-size:14px}\n"
    ".pager button:hover:not(:disabled){background:var(--tray)}\n"
    ".pager button[aria-current=page]{background:var(--ink);border-color:var(--ink);color:#fff}\n"
    ".pager button:disabled{opacity:.4;cursor:default}.pager .gap{padding:0 2px;color:var(--muted)}\n"
    ".pager .count{flex-basis:100%;text-align:center;font-size:13px;color:var(--muted)}")
sub(df, "aside{order:-1}.item{padding:16px}#filter{width:100%}",
    "aside{order:-1}.item{padding:16px}#filter,#sort{width:100%;min-width:0}.feedtools{width:100%;flex-wrap:nowrap}")
sub(df, '<section><span class="chip">Лента</span>', '<section id="lenta"><span class="chip">Лента</span>')
sub(df, '<select id="filter" aria-label="Фильтр ленты"><option value="all">Все записи, сначала опасные</option>',
    '<div class="feedtools"><select id="sort" aria-label="Порядок ленты"><option value="views">Сначала популярные</option>'
    '<option value="risk">Сначала опасные</option><option value="new">Сначала новые</option></select>'
    '<select id="filter" aria-label="Фильтр ленты"><option value="all">Все записи</option>')
sub(df, '</select></div><div id="feed"><p class="empty">Загружаем ленту…</p></div>',
    '</select></div></div><div id="feed"><p class="empty">Загружаем ленту…</p></div>'
    '<nav id="pager" class="pager" aria-label="Страницы ленты" hidden></nav>')
sub(df, 'let ownRun=0,state, busy=false, lastFeed="",lastRef="",visited=false;',
    'let ownRun=0,state, busy=false, lastFeed="",lastRef="",visited=false,page=1;const PAGE=10;')
sub(df, """function renderFeed(){const filter=$("filter").value,cats=["fraud","health","legal","source"];
 const rows=state.items.filter(i=>{const r=i.risk;return filter==="all"||filter===r.level||filter===i.platform||(filter==="current"&&state.current_keys.includes(i.key))||(cats.includes(filter)&&r.categories[filter].level!=="low")||(filter==="news"&&!!r.kind)}).map((i,n)=>[i,n]).sort((a,b)=>LV[a[0].risk.level]-LV[b[0].risk.level]||b[0].risk.score-a[0].risk.score||a[1]-b[1]).map(x=>x[0]);
 const sig=JSON.stringify([filter,rows]);if(sig===lastFeed)return;lastFeed=sig;""",
    """const seen=i=>i.views||0,when=i=>Date.parse(i.published)||0,
 ORDER={views:(a,b)=>seen(b)-seen(a)||LV[a.risk.level]-LV[b.risk.level],risk:(a,b)=>LV[a.risk.level]-LV[b.risk.level]||b.risk.score-a.risk.score||seen(b)-seen(a),new:(a,b)=>when(b)-when(a)};
function feedRows(){const filter=$("filter").value,cats=["fraud","health","legal","source"],by=ORDER[$("sort").value]||ORDER.views;
 return state.items.filter(i=>{const r=i.risk;return filter==="all"||filter===r.level||filter===i.platform||(filter==="current"&&state.current_keys.includes(i.key))||(cats.includes(filter)&&r.categories[filter].level!=="low")||(filter==="news"&&!!r.kind)}).map((i,n)=>[i,n]).sort((a,b)=>by(a[0],b[0])||a[1]-b[1]).map(x=>x[0])}
function pager(pages,total){const p=$("pager");p.replaceChildren();p.hidden=pages<2;if(pages<2)return;
 const btn=(text,n,label)=>{const b=el("button",text);b.type="button";if(label)b.setAttribute("aria-label",label);else if(n===page)b.setAttribute("aria-current","page");b.disabled=n<1||n>pages;b.onclick=()=>go(n);return b};
 p.append(btn("←",page-1,"Предыдущая страница"));let prev=0;
 for(const n of [...new Set([1,page-1,page,page+1,pages])].filter(n=>n>=1&&n<=pages).sort((a,b)=>a-b)){if(n-prev>1)p.append(el("span","…","gap"));p.append(btn(String(n),n));prev=n}
 p.append(btn("→",page+1,"Следующая страница"),el("span","Страница "+page+" из "+pages+" · "+plural(total,"запись","записи","записей")+" по "+PAGE+" на странице","count"))}
function go(n){page=n;renderFeed();$("lenta").scrollIntoView({block:"start"})}
function renderFeed(){const filter=$("filter").value,all=feedRows(),pages=Math.max(1,Math.ceil(all.length/PAGE));page=Math.min(Math.max(1,page),pages);
 const rows=all.slice((page-1)*PAGE,page*PAGE);
 const sig=JSON.stringify([filter,$("sort").value,page,all.length,rows]);if(sig===lastFeed)return;lastFeed=sig;pager(pages,all.length);""")
sub(df, """if(!visited && location.hash){const target=document.getElementById(decodeURIComponent(location.hash.slice(1)));if(target){target.scrollIntoView();visited=true}}""",
    """if(!visited && location.hash){const id=decodeURIComponent(location.hash.slice(1));let target=document.getElementById(id);
 if(!target&&id.startsWith("video-")){const n=feedRows().findIndex(i=>"video-"+i.key===id);if(n>=0){page=Math.floor(n/PAGE)+1;renderFeed();target=document.getElementById(id)}}
 if(target){target.scrollIntoView();visited=true}}""")
sub(df, 'if(s.created){ownRun=s.run.id;$("filter").value="current"}', 'if(s.created){ownRun=s.run.id;$("filter").value="current";page=1}')
sub(df, '$("filter").onchange=()=>state&&renderFeed();',
    '$("filter").onchange=$("sort").onchange=()=>{page=1;if(state)renderFeed()};')
sub(df, 'row("Порядок отбора","выше — публикации с конкретными суммами, обещаниями и ссылками на нормы");',
    'row("Порядок отбора","сначала самые просматриваемые публикации за 30 дней; при равной популярности выше — с конкретными суммами, обещаниями и ссылками на нормы");')
sub(df, 'row("Порядок отбора","сначала скачиваются публикации с признаками риска — обещаниями денег, «лечения», новых законов, кликбейтом. Оценка выше считается заново по всему тексту");',
    'row("Порядок отбора","сначала самые просматриваемые публикации за 30 дней, затем — с признаками риска: обещаниями денег, «лечения», новых законов, кликбейтом. Оценка выше считается заново по всему тексту");')
sub(df, '" — из подходящих видео сначала берутся самые просматриваемые"', '" — в первую очередь берутся самые просматриваемые подходящие видео"')
sub(df, 'r.kind==="risk"?"Порядок отбора: сначала публикации с признаками риска, затем просмотры за 30 дней.":r.kind==="deepfake"?"Порядок отбора: упоминание подделки или ИИ, затем просмотры за 30 дней.":"Выше в отборе — конкретные суммы, обещания и ссылки на нормы."',
    '"Порядок отбора: сначала самые просматриваемые за 30 дней (от "+(s.min_views||1000).toLocaleString("ru-RU")+" просмотров), затем "+(r.kind==="risk"?"признаки риска.":r.kind==="deepfake"?"упоминание подделки или ИИ.":"конкретные суммы, обещания и ссылки на нормы.")')
sub(df, '"отобрано "+v.count+" из "+v.examined+(v.other?", не об Узбекистане: "+v.other:"")',
    '"отобрано "+v.count+" из "+v.examined+(v.low?", мало просмотров: "+v.low:"")+(v.other?", не об Узбекистане: "+v.other:"")')

# 4. Popular-first crawl (deepfake_watch.py) --------------------------------------------------------------------
dw = MON / "deepfake_watch.py"
sub(dw, "FACTCHECK = (\"factcheckeruz\",)",
    "FACTCHECK = (\"factcheckeruz\",)\n# Only popular publications: fewer views than this are skipped (the fact-checking channel is exempt).\n"
    "MIN_VIEWS = int(os.environ.get(\"LEX_MIN_VIEWS\", \"1000\"))")
sub(dw, '''    """Download order: for risk searches signs of fraud or a false claim first, then a mention of a fake, then
    popularity; for claims, testable facts."""
    if topic.kind in ("risk","deepfake"):
        return risk_review.score(text), watch_topics.deepfake_hint(text), count
    return watch_topics.priority(text), count''',
    '''    """Download order: the most watched first; among equally popular ones signs of fraud or a false claim, then a
    mention of a fake; for claims, testable facts."""
    if topic.kind in ("risk","deepfake"):
        return count, risk_review.score(text), watch_topics.deepfake_hint(text)
    return count, watch_topics.priority(text)''')
sub(dw, "queries, seen, picked, examined, other, failure = [], set(), {}, 0, 0, None",
    "queries, seen, picked, examined, other, low, failure = [], set(), {}, 0, 0, 0, None")
sub(dw, """            examined += 1
            if not watch_topics.relevant(topic, text):""",
    """            examined += 1
            if p.get("views", 0) < MIN_VIEWS:
                low += 1
                continue
            if not watch_topics.relevant(topic, text):""")
sub(dw, '{"count": min(len(best), YT_PER_RUN), "examined": examined, "other": other}',
    '{"count": min(len(best), YT_PER_RUN), "examined": examined, "other": other, "low": low}')
sub(dw, """            posts=sorted(related,key=lambda p:(*rank(topic,p["caption"],p.get("views",0)),p["published"]),reverse=True)[:TG_PER_CHANNEL]""",
    """            popular=[p for p in related if channel in FACTCHECK or p.get("views",0)>=MIN_VIEWS]
            posts=sorted(popular,key=lambda p:(*rank(topic,p["caption"],p.get("views",0)),p["published"]),reverse=True)[:TG_PER_CHANNEL]""")
sub(dw, 'stats["@"+channel]={"count":len(posts),"examined":len(found),"other":len(relevant)-len(related)}',
    'stats["@"+channel]={"count":len(posts),"examined":len(found),"other":len(relevant)-len(related),"low":len(related)-len(popular)}')
sub(dw, "LEFT JOIN df_claims c ON v.key=c.key WHERE c.key IS NULL AND v.uz=1 ORDER BY v.added DESC LIMIT 4",
    "LEFT JOIN df_claims c ON v.key=c.key WHERE c.key IS NULL AND v.uz=1 ORDER BY v.views DESC,v.added DESC LIMIT 4")
sub(dw, """            added DESC,published DESC LIMIT 1\"\"\",(now-3600,)).fetchone()""",
    """            views DESC,added DESC,published DESC LIMIT 1\"\"\",(now-3600,)).fetchone()""")
sub(dw, "v.added DESC,v.published DESC LIMIT 60", "v.added DESC,v.published DESC LIMIT 100")
sub(dw, "channels=CHANNELS, interval=PERIOD,", "channels=CHANNELS, interval=PERIOD, min_views=MIN_VIEWS,")

for p, t in files.items():
    if WRITE:
        with open(p, "w", encoding="utf8", newline="") as f:
            f.write(t.replace("\n", "\r\n") if p in crlf else t)
print(("written" if WRITE else "dry run ok"), len(files), "files")
print("\n".join(log))
