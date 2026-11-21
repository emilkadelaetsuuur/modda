const $=id=>document.getElementById(id);let model,formReady=false,lastJobs='',requestId=null,previousSpec='';
// Audience rule: stickman and cartoons for viewers under 25, infographics for 25 and older.
const AUDIENCE={u25:{style:'stickman',styles:['stickman','cartoon3d','bright','kids_soft','flat_motion','paper_cut','kinetic_type']},
 '25p':{style:'doc_infographic',styles:['doc_infographic','elder_clear','isometric','line_art']}};
const IDEAS=[['Сколько дней отпуска положено','Сколько дней ежегодного отпуска положено работнику в Узбекистане','demo'],
 ['Можно ли уволить беременную','Можно ли уволить беременную женщину по инициативе работодателя','demo'],
 ['Испытательный срок','Какой испытательный срок можно установить при приёме на работу','demo'],
 ['Увольнение по своему желанию','Как уволиться по собственному желанию и за сколько дней предупредить','team'],
 ['Возврат товара с браком','Как вернуть товар с недостатками и что должен сделать магазин','team'],
 ['Наказание за мошенничество','Какое наказание грозит за мошенничество в Узбекистане','team']];
const NAMES={queued:'В очереди',active:'В работе',done:'Готово',attention:'Нужно внимание'},STEPS=[['script','Сценарий'],['flow','Сцены'],['render','Монтаж'],['upload','Загрузка']];
function node(t,s,cls){const e=document.createElement(t);if(s!==undefined)e.textContent=s;if(cls)e.className=cls;return e}
function plural(n,a,b,c){const m=n%10,h=n%100;return n+' '+(m===1&&h!==11?a:m>=2&&m<=4&&(h<12||h>14)?b:c)}
async function api(name,data){const r=await fetch('/studio/api/'+name,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});let b={};try{b=await r.json()}catch{}if(!r.ok){const e=new Error(b.error||'Запрос не выполнен');e.status=r.status;throw e}return b}
function sources(rows,host){host.replaceChildren();for(const s of rows){const x=node('div',undefined,'source'),a=node('a',s.article+' · '+s.title);a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';x.append(a,node('div','Редакция на дату: '+s.as_of,'meta'),node('p',s.text.length>420?s.text.slice(0,417)+'…':s.text));host.append(x)}}
function note(text,kind){const n=$('result');n.textContent=text;n.className='note'+(kind?' '+kind:'')}
function audience(){return document.querySelector('input[name=aud]:checked').value}
function fillStyles(){const a=AUDIENCE[audience()],sel=$('style'),keep=sel.value;sel.replaceChildren();for(const [aud,label] of [[audience(),'Подходит аудитории'],[audience()==='u25'?'25p':'u25','Другие стили']]){const g=document.createElement('optgroup');g.label=label;for(const k of AUDIENCE[aud].styles)if(model.styles[k])g.append(new Option(model.styles[k],k));sel.append(g)}const rest=Object.keys(model.styles).filter(k=>!AUDIENCE.u25.styles.includes(k)&&!AUDIENCE['25p'].styles.includes(k));if(rest.length){const g=sel.lastElementChild;for(const k of rest)g.append(new Option(model.styles[k],k))}sel.value=keep&&a.styles.includes(keep)?keep:a.style}
function fillDurations(s){const sel=$('duration'),keep=Number(sel.value)||30,max=s.viewer==='owner'?45:s.demo.seconds;sel.replaceChildren();for(const [sec,clips] of Object.entries(s.durations)){const o=new Option(sec+' секунд · '+plural(clips,'сцена','сцены','сцен'),sec);o.disabled=Number(sec)>max;sel.add(o)}sel.value=String(keep<=max?keep:Math.min(max,15))}
function setup(s){
 for(const [k,v] of Object.entries(s.formats))$('format').add(new Option(v,k));$('format').value=s.formats.explainer?'explainer':Object.keys(s.formats)[0];
 fillStyles();fillDurations(s);
 $('corpus').value=s.external||s.counts.team?'team':'demo';if(!s.external&&!s.counts.team)$('corpus').options[0].disabled=true;
 for(const [label,text,corpus] of IDEAS){const b=node('button',label,'idea');b.type='button';b.onclick=()=>{$('prompt').value=text;if(!$('corpus').querySelector('option[value='+corpus+']').disabled)$('corpus').value=corpus;$('prompt').focus();note('')};$('ideas').append(b)}
 for(const r of document.querySelectorAll('input[name=aud]'))r.onchange=fillStyles;
 formReady=true}
function track(j){const ul=node('ol',undefined,'track'),at=STEPS.findIndex(x=>x[0]===j.phase);STEPS.forEach(([,label],n)=>{const li=node('li',label);if(j.status==='done'||(at>=0&&n<at))li.className='past';else if(j.status==='active'&&n===at)li.className='now';ul.append(li)});return ul}
function when(t){return new Date(t*1000).toLocaleString('ru-RU',{timeZone:'Asia/Tashkent',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}
// Refutation orders carry a long brief for the scriptwriter; the card shows the claim itself.
function title(p){const claim=p.match(/Утверждение: «([^»]+)»/);let t=claim?'Опровержение: «'+claim[1].replace(/\.$/,'')+'»':p.split('\n')[0].replace(/\s*Источник: \S+.*$/,'');return t.length>200?t.slice(0,197)+'…':t}
function card(j){
 const box=node('article',undefined,'job'),top=node('div',undefined,'top');box.dataset.id=j.id;
 top.append(node('span',NAMES[j.status]||j.status,'tag '+j.status),node('span',when(j.created)+(j.spec.kind==='fixture'?' · технический тест':j.spec.by==='demo'?' · демо':''),'meta'));
 box.append(top,node('h3',title(j.spec.prompt)),node('div',[model.formats[j.spec.format],model.styles[j.spec.style],j.spec.lang.toUpperCase(),j.spec.aspect,(j.spec.duration?j.spec.duration+' с · ':'')+plural(j.spec.scenes||1,'сцена','сцены','сцен')].filter(Boolean).join(' · '),'meta'));
 if(j.status!=='done')box.append(track(j));
 if(j.message)box.append(node('p',j.message,'msg'));
 if(j.video){const v=node('video');v.controls=true;v.preload='metadata';v.playsInline=true;v.src=j.video;const act=node('div',undefined,'actions'),a=node('a','Скачать MP4','btn ghost sm');a.href=j.video;a.download='modda-'+j.id+'.mp4';act.append(a);box.append(v,act)}
 const det=node('details'),list=node('div',undefined,'sources');det.dataset.k='src';sources(j.sources,list);det.append(node('summary','Статьи закона в основе ('+j.sources.length+')'),list);box.append(det);
 if(title(j.spec.prompt)!==j.spec.prompt){const brief=node('details');brief.dataset.k='brief';brief.append(node('summary','Задание сценаристу'),node('p',j.spec.prompt,'small pre'));box.append(brief)}
 return box}
const ORDER={active:0,queued:1,done:2,attention:3};
function drawJobs(){
 const f=$('filter').value,rows=model.jobs.filter(j=>f==='all'||(f==='work'?['queued','active'].includes(j.status):j.status===f)).sort((a,b)=>(ORDER[a.status]??4)-(ORDER[b.status]??4)||b.created-a.created);
 const sig=JSON.stringify([f,rows.map(j=>[j.id,j.status,j.phase,j.message,j.bytes])]);if(sig===lastJobs)return;lastJobs=sig;
 const host=$('jobs'),open=new Set([...host.querySelectorAll('details[open]')].map(d=>d.closest('.job').dataset.id+':'+d.dataset.k)),playing=new Map([...host.querySelectorAll('video')].map(v=>[v.closest('.job').dataset.id,v]));
 host.replaceChildren();if(!rows.length){host.append(node('p',model.jobs.length?'Под этот фильтр роликов нет.':'Пока нет роликов. Задайте тему выше — ролик появится здесь.','empty'));return}
 for(const j of rows){const old=playing.get(j.id),c=card(j);if(old&&j.video){c.querySelector('video').replaceWith(old)}for(const d of c.querySelectorAll('details'))d.open=open.has(j.id+':'+d.dataset.k);host.append(c)}}
function draw(s){model=s;if(!formReady)setup(s);
 const alive=s.worker&&Date.now()/1000-s.worker.seen<55,idle=alive&&/^ожидает/.test(s.worker.info||''),w=$('worker');w.textContent=!alive?'Не на связи — задания ждут в очереди':idle?'Ноутбук включён, но приём заказов не запущен — задания ждут в очереди':'На связи — ролики собираются';w.className='state'+(!alive?'':idle?' wait':' on');
 const busy=s.jobs.filter(j=>['queued','active'].includes(j.status)).length;
 if(s.viewer==='owner'){$('limits').textContent='Вы вошли с ноутбука-владельца: дневной лимит демо не действует. В очереди: '+busy+' из '+s.queue+'.'}
 else{const left=Math.max(0,s.demo.daily-s.demo.used),l=$('limits');l.replaceChildren(document.createTextNode('Сегодня можно создать ещё '+plural(left,'ролик','ролика','роликов')+' из '+s.demo.daily+', длиной до '+s.demo.seconds+' секунд. В очереди: '+busy+' из '+s.queue+'.'));const m=node('div',undefined,'meter'),bar=node('span');bar.style.width=Math.min(100,100*s.demo.used/s.demo.daily)+'%';m.append(bar);l.append(m);$('submit').disabled=left===0}
 $('importPanel').hidden=s.viewer!=='owner';
 $('corpusInfo').textContent=(s.external?'Вся база lex.uz доступна через поиск. ':'')+'Демо-набор: '+plural(s.counts.demo||0,'выдержка','выдержки','выдержек')+' из Трудового кодекса и закона о рекламе'+(s.counts.team?'; база команды: '+s.counts.team:'')+'.';
 drawJobs()}
async function refresh(){try{draw(await api('status'));$('error').textContent=''}catch(e){$('error').textContent=e.message||'Студия недоступна. Повторим запрос.'}}
function spec(){return Object.fromEntries(['prompt','format','style','lang','aspect','corpus','duration'].map(k=>[k,k==='duration'?Number($(k).value):$(k).value.trim()]))}
$('search').onclick=async()=>{const v=spec();if(v.prompt.length<3){note('Напишите тему — хотя бы несколько слов.','bad');$('prompt').focus();return}$('search').disabled=true;try{const s=await api('search',v);sources(s.sources,$('sources'));note(s.sources.length?'Найдено статей: '+s.sources.length+'. По ним будет написан сценарий.':'В этой базе подходящих статей нет. Уточните тему или выберите другую базу.',s.sources.length?'ok':'bad')}catch(e){note(e.message,'bad')}finally{$('search').disabled=false}};
$('create').onsubmit=async e=>{e.preventDefault();const v=spec();if(v.prompt.length<3){note('Напишите тему — хотя бы несколько слов.','bad');$('prompt').focus();return}$('submit').disabled=true;if(JSON.stringify(v)!==previousSpec||!requestId){previousSpec=JSON.stringify(v);requestId=crypto.randomUUID()}try{const j=await api('jobs',{...v,request_id:requestId});requestId=null;sources(j.sources,$('sources'));note('Готово: ролик поставлен в очередь. Статус — в разделе «Очередь и готовые ролики» ниже.','ok');$('filter').value='all';lastJobs='';await refresh()}catch(e){note(e.message,'bad')}finally{if(model)$('submit').disabled=model.viewer!=='owner'&&model.demo.used>=model.demo.daily}};
$('filter').onchange=()=>model&&drawJobs();
$('import').onclick=async()=>{try{const f=$('file').files[0];if(!f||f.size>7900000)throw Error('Выберите JSONL-файл до 7,9 МБ.');const t=(await f.text()).trim();const passages=t.startsWith('[')?JSON.parse(t):t.split(/\r?\n/).filter(x=>x.trim()).map(x=>JSON.parse(x));const res=await api('import',{passages});$('importStatus').textContent='Загружено: '+res.imported;$('corpus').value='team';await refresh()}catch(e){$('importStatus').textContent=e.message}};
// The laptop shortcut opens the studio with a one-time ticket: it signs the owner in without a typed code.
(async()=>{const t=new URLSearchParams(location.hash.slice(1)).get('ticket');if(t){history.replaceState(null,'',location.pathname);try{await api('login',{ticket:t})}catch(e){}}await refresh();setInterval(()=>{if(!document.hidden)refresh()},5000)})();
