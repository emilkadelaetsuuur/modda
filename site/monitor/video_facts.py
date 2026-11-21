"""Claims with a directly verifiable scope, quote, date and primary source."""
from datetime import date
from decimal import Decimal
import json
import re
from urllib.request import Request, urlopen

import auto_law

# Only an explicit official USD/UZS rate for a stated day, expressed to the kopeck/tiyin.
# Exchange-office rates, rounded numbers, predictions and unanchored quotes cannot enter this rule.
_RATE = re.compile(
    r"(?:Официальный курс (?:доллара США|USD) (?:ЦБ Узбекистана|Центрального банка Узбекистана) на "
    r"(?P<ru>\d{2}\.\d{2}\.\d{4})\s*(?:составляет|составил|[—–:-])\s*"
    r"(?P<r>[1-9]\d{0,2}(?:[ \u00a0]\d{3})+|[1-9]\d{2,5})(?P<rf>[.,]\d{2})\s*(?:сумов|сума|сум)"
    r"|O['ʻ‘’ʼ]zbekiston Markaziy bankining (?P<uz>\d{2}\.\d{2}\.\d{4}) (?:kungi|sanadagi) rasmiy "
    r"(?:AQSH dollari|dollar) kursi\s*[—–:-]?\s*"
    r"(?P<u>[1-9]\d{0,2}(?:[ \u00a0]\d{3})+|[1-9]\d{2,5})(?P<uf>[.,]\d{2})\s*so['ʻ‘’ʼ]m)"
    r"[.!]?", re.I)

def official_rate(day):
    url = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/" + day.isoformat() + "/"
    req = Request(url, headers={"User-Agent":"LexMonitor/1.0"})
    with urlopen(req, timeout=8) as response:
        values = json.loads(response.read(65_536))
    if not isinstance(values,list) or len(values)!=1:
        raise ValueError("Нет однозначной записи ЦБ.")
    v = values[0]
    if (v.get("Ccy")!="USD" or str(v.get("Nominal"))!="1"
            or v.get("Date")!=day.strftime("%d.%m.%Y") or not re.fullmatch(r"\d{1,6}(?:\.\d{1,4})?",str(v.get("Rate","")))):
        raise ValueError("ЦБ не подтвердил курс на точную дату утверждения.")
    return Decimal(str(v["Rate"])),url

def check(platform,title,description,published,media=None):
    row=dict(platform=platform.lower(),method="search",video_title=title,description=description,
             published_at=published,media_report=media or {})
    sources=[("Заголовок YouTube" if platform=="YouTube" else "Подпись Telegram",title,None),
             ("Описание YouTube" if platform=="YouTube" else "Подпись Telegram",description,None)]
    if platform=="Telegram":
        sources=sources[1:]
    coverage=["текст публикации"] if any(x[1] for x in sources) else []
    media=media or {}
    speech=media.get("speech",{})
    segments=speech.get("segments") or []
    if speech.get("text") or segments:
        coverage.append("речь")
        sources.extend(("Речь · VoiceLab",x["text"],x["start"]) for x in segments)
        if not segments:
            sources.append(("Речь · VoiceLab",speech["text"],None))
    if media.get("frames_scanned"):
        coverage.append("выборка кадров")
        sources.extend(("Надпись в кадре · OCR",x["text"],x["start"]) for x in media["frames"])
    findings,issues=[],[]
    try:
        for hit in auto_law.check_video(row):
            findings.append(dict(kind="discrepancy" if hit["category"]=="inaccurate_claim" else "legal_review",
                                 quote=hit["quote"],where=hit["where"],time=hit.get("start"),reason=hit["reason"],
                                 source=hit["law_url"],source_label=hit["law_label"],excerpt=hit["law_excerpt"]))
    except Exception:
        issues.append("Редакция нормы на дату публикации не подтверждена.")
    day_published=auto_law._day(published)
    context=" ".join([x[1] for x in sources]+[speech.get("text","")])
    seen=set()
    # Only a debunk or warning framing excludes the whole publication. Ordinary negations and
    # questions elsewhere in a long transcript say nothing about the stated rate itself.
    if day_published and not auto_law.framed(context):
        for where,txt,at in sources:
            for line in re.finditer(r"[^\r\n]+",txt):
                quote=line[0].strip()
                m=_RATE.fullmatch(quote)
                if not m or quote in seen or len(seen)>=3:
                    continue
                begin=line.start()+line[0].index(quote)
                if auto_law.qualified_near(txt,begin,begin+len(quote)):
                    continue
                seen.add(quote)
                dd,mm,yy=(m["ru"] or m["uz"]).split(".")
                try:
                    when=date(int(yy),int(mm),int(dd))
                except ValueError:
                    continue
                if when>day_published:
                    continue
                try:
                    rate,url=official_rate(when)
                except Exception:
                    issues.append("Официальный курс на указанную дату не подтверждён.")
                    continue
                claimed=Decimal(re.sub(r"\s+","",(m["r"] or m["u"])+(m["rf"] or m["uf"])).replace(",","."))
                findings.append(dict(kind="matches" if claimed==rate else "discrepancy",quote=quote,where=where,time=at,
                                     reason=f"В утверждении указано {claimed} UZS. В архиве ЦБ на {dd}.{mm}.{yy} — {rate} UZS за 1 USD.",
                                     source=url,source_label="Центральный банк Республики Узбекистан",excerpt=f"1 USD = {rate} UZS"))
    if media.get("errors"):
        issues.append("Часть речи или кадров недоступна для распознавания.")
    return dict(findings=findings,coverage=coverage,issues=list(dict.fromkeys(issues)))
