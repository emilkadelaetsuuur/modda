"""How likely a publication is fraud, a false claim about the law, dubious treatment or an unreliable source.

Rules only, no model. Every reason quotes the words that triggered it, so the card can say "because ...".
The score orders and labels the feed; it is a warning for a person to check, never a legal conclusion."""
from collections import Counter
import re
import unicodedata

# Uzbek news outlets and the fact-checking channel the scanner reads: their own reporting is not anonymous.
MEDIA = {"kunuz", "kunuzru", "daryo", "qalampirlive", "uzbekistan24", "zamonuz", "sputnikuz", "factcheckeruz",
         "gazetauz", "podrobnouz", "uzreport", "uzanews", "dunyouz", "xabaruz", "darakchi"}
_YT_MEDIA = re.compile(r"kun\.?uz|daryo|qalampir|gazeta\.?uz|podrobno|sputnik|uzreport|\buza\b|dunyo\.?uz|xabar\.?uz|zamon\.?uz"
                       r"|uzbekistan ?24|o'zbekiston 24|mtrk|yoshlar tv|sevimli tv|milliy tv|factchecker")

# Norms checked in the Modda Lex.uz base (current editions). The card links the exact article.
NORMS = {
    "fraud": ("УК РУз, ст. 168. Мошенничество", "https://lex.uz/ru/docs/111457#222582",
              "Завладение чужим имуществом или правом на него путём обмана или злоупотребления доверием."),
    "finance_ad": ("Закон «О рекламе», ст. 43. Реклама финансовых услуг", "https://lex.uz/ru/docs/6052633#6054895",
                   "Реклама финансовых услуг не должна злоупотреблять доверием и неопытностью и должна раскрывать существенные условия договора."),
    "folk_ad": ("Закон «О рекламе», ст. 45. Реклама товаров народной медицины", "https://lex.uz/ru/docs/6052633#6054905",
                "Нельзя ссылаться на случаи излечения и создавать впечатление, что к врачу обращаться не нужно."),
    "folk_ao": ("КоАО РУз, ст. 59⁵. Нарушение законодательства о народной медицине", "https://lex.uz/ru/docs/97661#7206248",
                "Штраф за лечение и публичную рекламу лечения без права заниматься народной медициной."),
    "license": ("Закон «Об охране здоровья граждан», ст. 11. Лицензирование", "https://lex.uz/ru/docs/41329#1070222",
                "Медицинская деятельность разрешена юридическим лицам только при наличии лицензии."),
    "false_info": ("КоАО РУз, ст. 202². Распространение ложной информации", "https://lex.uz/ru/docs/97661#5190976",
                   "Штраф за распространение ложной информации, в том числе в интернете."),
    "bad_ad": ("КоАО РУз, ст. 178¹. Нарушение законодательства о рекламе", "https://lex.uz/ru/docs/97661#1687371",
               "Штраф за недостоверную рекламу."),
    "ad_id": ("Закон «О рекламе», ст. 18. Идентификация рекламы", "https://lex.uz/ru/docs/6052633#6054315",
              "Реклама должна быть отделена от другой информации так, чтобы её можно было распознать как рекламу."),
}

CATEGORIES = {
    "fraud": dict(title="Мошенничество", noun="мошенничества", high=5, medium=2,
                  advice="Не переводите деньги, не вводите данные карты и коды из SMS, не вступайте в «закрытые группы». "
                         "Законная инвестиция не гарантирует доход и раскрывает условия договора."),
    "health": dict(title="Сомнительное лечение", noun="недостоверной рекламы лечения", high=4, medium=2,
                   advice="Не заменяйте лечение у врача. Медицинская деятельность разрешена только по лицензии: "
                          "проверьте клинику на license.gov.uz."),
    "legal": dict(title="Утверждения о законах", noun="ложного утверждения о законе", high=4, medium=2,
                  advice="Сверьте утверждение с текстом закона — поиск по базе Lex.uz ниже. Пока документ не найден, "
                         "считайте это слухом и не пересылайте."),
    "source": dict(title="Ненадёжный источник", noun="недостоверной информации", high=5, medium=3,
                   advice="Найдите то же сообщение у госоргана или крупного СМИ, прежде чем верить и пересылать."),
}
ORDER = ("fraud", "health", "legal", "source")

# Text is folded before matching: lower case, one apostrophe, Uzbek Cyrillic letters as Russian ones
# (ў→у, қ→к, ғ→г, ҳ→х), Turkish-style Latin as Uzbek Latin (ö→o', ş→sh), stress marks removed.
_MAP = {"‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'", "´": "'", "ö": "o'", "ğ": "g'", "ş": "sh", "ç": "ch",
        "ў": "у", "қ": "к", "ғ": "г", "ҳ": "х", "ё": "е"}


def fold(text):
    """The folded text and, for each folded character, its index in the original."""
    text = unicodedata.normalize("NFKC", text or "")
    out, pos = [], []
    for i, ch in enumerate(text):
        if unicodedata.category(ch) == "Mn":
            continue
        low = ch.lower()
        for c in _MAP.get(low, low):
            out.append(c)
            pos.append(i)
    return "".join(out), pos, text


def _rx(pattern):
    return re.compile(pattern)


# (category, weight, key, reason, pattern)
SIGNALS = [
    ("fraud", 2, "easy_income", "обещают доход без усилий или вложений",
     _rx(r"passiv daromad|пассив\w* (?:даромад|доход)|ishlamasdan|ишламасдан|investitsiyasiz|инвестициясиз|без вложений|"
         r"не выходя из дома|uydan chiqmasdan|уйдан чикмасдан|oson pul|осон пул|л[её]гки\w* деньг|быстр\w* заработ|"
         r"kafolatlangan daromad|кафолатланган даромад|гарантированн\w* доход|har kuni daromad|хар куни даромад|"
         r"kuniga \d[\d ]*(?:\$|dollar|ming|so'm|сум)|\d+ ?% (?:в день|в месяц|в неделю|kuniga|oyiga|кунига|ойига)")),
    ("fraud", 1, "make_money", "предлагают «заработать» в интернете",
     _rx(r"pul ishlash|pul topish|пул ишлаш|пул топиш|заработ\w* в интернете|internetda pul|интернетда пул")),
    ("fraud", 2, "tasks", "платят за лайки и задания — в таких схемах потом просят взнос за «вывод» денег",
     _rx(r"topshiriq\w* bajar|топшириг?\w* бажар|layk\w* (?:bos|qo'y)|лайк\w* (?:за деньги|ставить)|задани\w* за деньги|"
         r"pul yechib olish|pul yechish|пул ечиш|вывод (?:денег|средств)|минимальн\w* вывод")),
    ("fraud", 2, "referral", "дают реферальные ссылки и денежные бонусы за регистрацию",
     _rx(r"referral|referal|реферал|ref_?code|promo ?kod|промокод|\+\s?\d[\d ]*\$|maksimal bonus|бонус\w* за регистрац")),
    ("fraud", 1, "crypto", "зазывают на крипто-, форекс- или биржевые сервисы",
     _rx(r"kripto|крипто|bitcoin|биткоин|binance|bybit|bitget|\bmexc\b|\bokx\b|forex|форекс|трейдинг|treyding|\bbirja|\bбиржа")),
    ("fraud", 1, "short_link", "ставят сокращённые ссылки, которые скрывают, куда ведут",
     _rx(r"bit\.ly|tinyurl|cutt\.ly|clck\.ru|goo\.gl|shorturl|linktr\.ee|canva\.site|taplink")),
    ("fraud", 2, "private", "уводят общение в личку или закрытую группу",
     _rx(r"yopiq (?:jamoa|guruh|kanal|klub)|епик (?:жамоа|гурух|канал)|закрыт\w* (?:клуб|канал|групп|сообществ)|в личк|"
         r"пишите в (?:лс|личные|директ|whatsapp|телеграм)|lichkaga|lichga yoz|direktga|директга|whatsapp|ватсап")),
    ("fraud", 1, "comment_bait", "просят написать в комментариях, чтобы с вами связались",
     _rx(r"izohda yozing|izohga yozing|изохда езинг|напишите в комментари|пишите в комментари")),
    ("fraud", 3, "prepay", "просят перевести деньги или внести предоплату",
     _rx(r"предоплат|переведите|скиньте на карт|oldindan to'lov|олдиндан тулов|kartaga (?:o'tkaz|tashla|pul)|"
         r"картага (?:утказ|ташла|пул)|pul o'tkazing|пул утказинг|депозит внес|depozit qo'y")),
    ("fraud", 2, "payout", "обещают выплаты «всем» — так часто маскируют фишинговые ссылки",
     _rx(r"выплат\w* (?:всем|каждому)|компенсаци\w* (?:всем|каждому)|hammaga (?:pul|nafaqa|kompensatsiya|to'lov)|"
         r"хаммага (?:пул|нафака|компенсация|тулов)|har bir fuqaroga \d|хар бир фукарога \d|davlat pul beradi|давлат пул беради")),
    ("fraud", 2, "prize", "разыгрывают «призы» в обмен на действия",
     _rx(r"розыгрыш|вы выиграли|siz yutdingiz|сиз ютдингиз|yutuqli o'yin|ютукли уйин|sovg'a qilamiz|совга киламиз|"
         r"bepul (?:iphone|telefon|pul)")),
    ("fraud", 2, "guarantee", "гарантируют результат или доход без риска",
     _rx(r"без риска|risksiz|рисксиз|гарантир\w* (?:доход|прибыл|результат|выплат)|100 ?% (?:foyda|прибыл|kafolat|natija|доход)|"
         r"kafolatla(?!nmay|nmag)\w*|кафолатла(?!нмай|нмаг)\w*|xavfsiz daromad")),
    ("fraud", 1, "urgency", "давят срочностью",
     _rx(r"только сегодня|успейте|осталось \d+ мест|shoshiling|шошилинг|faqat bugun|факат бугун|ulgurib qoling|"
         r"улгуриб колинг|joylar cheklangan|жойлар чекланган")),

    ("health", 1, "folk", "предлагают лечение «народными» методами",
     _rx(r"табиб|tabib|халк табобат|xalq tabobat|народн\w* медицин|zuluk|зулук|пиявк|асалари захри|ari zahri|"
         r"гирудотерап|hijoma|хижома|хиджам")),
    ("health", 2, "cure_all", "обещают излечение без врача, операции или лекарств",
     _rx(r"operatsiyasiz|операциясиз|без операци|dorisiz|дорисиз|без лекарств|100 ?% (?:davo|shifo|излеч|вылеч)|"
         r"butunlay davola|бутунлай даво|навсегда избав|bir umrga|бир умрга")),

    ("legal", 2, "law_claim", "говорят о новом законе, налоге, штрафе или пенсии, но не ссылаются на документ",
     _rx(r"yangi qonun|янги конун|новый закон|новые правила|qonun kuchga|конун кучга|kuchga kiradi|кучга киради|"
         r"вступа\w* в силу|soliq to'lay|солик тулай|налог\w* (?:на переводы|для мигрант|для всех)|"
         r"jarima (?:solinadi|joriy)|жарима (?:солинади|жорий)|штраф\w* (?:для всех|введут|будут)|taqiqlanadi|такикланади|"
         r"pensiya tizim|пенсия тизим|пенсионн\w* (?:реформ|систем)|nafaqa (?:oshadi|o'zgaradi)|нафака (?:ошади|узгаради)|"
         r"пенси\w* (?:повыс|измен)")),

    ("source", 1, "spread", "призывают распространять",
     _rx(r"tarqating|таркатинг|ulashing|улашинг|распространите|перешлите|поделитесь со всеми|hammaga yuboring|хаммага юборинг")),
    ("source", 1, "rumor", "ссылаются на слухи, а не на источник",
     _rx(r"говорят,|по слухам|ходят слухи|инсайд|aytishlaricha|айтишларича|mish-mish|миш-миш|eshitishimcha|эшитишимча|"
         r"noma'lum manba|номаълум манба")),
    ("source", 1, "rate_news", "курс подаётся как сенсация без ссылки на Центробанк",
     _rx(r"(?:курс|kurs)\w*.{0,40}(?:ошди|oshdi|тушди|tushdi|кутарил|ko'taril|рекорд|rekord|арзонла|arzonla|подорож|рухнул)")),
]

_CLICKBAIT = _rx(r"срочно|\bшок\b|сенсаци|тез куринг|tez ko'ring|диккат(?! билан)|дикат(?! билан)|diqqat(?! bilan)|хуш хабар|xush xabar|хаммани хайрат|"
                 r"hammani hayrat|ана холос|ana xolos|об-ха|все в шоке|hamma hayron|ана сизга|ana sizga")
_EMOJI = re.compile(r"[😱🔥💥⚡‼❗🤯😨😳⚠🚨]")
_TREAT = _rx(r"davola|даволаш|даволан|лечени|лечим|лечит|shifo|шифо|\bdavo\b|\bдаво\b|xizmat|хизмат|услуг")
_PHONE = re.compile(r"\+?998[\s-]?\(?\d{2}\)?[\s-]?\d{3}")
_DOCTOR = _rx(r"shifokorga (?:murojaat|boring)|шифокорга (?:мурожаат|боринг)|ginekologga|stomatologga|к врачу|врачу|"
              r"обратитесь к врач|vrachga|врачга")
_OFFICIAL = _rx(r"lex\.uz|gov\.uz|cbu\.uz|my\.gov|soliq\.uz|qaror\w* (?:№|n\b)|карор\w* №|постановлени\w* (?:№|кабинет|президент)|"
                r"указ\w* (?:№|президент)|farmon|фармон|\bпк-\d|\bpq-\d|\bуп-\d|\bзру-\d|o'rq-\d|урк-\d|президент карор")
_CENTRAL_BANK = _rx(r"cbu|markaziy bank|марказий банк|центральн\w* банк|\bцб\b")
_AD_LABEL = _rx(r"реклама|reklama|#ad\b|homiy|хомий|на правах рекламы")
_COMMERCE = _rx(r"sotiladi|сотилади|xarid qiling|харид килинг|xizmat\w* (?:ko'rsatiladi|taklif)|хизмат\w* (?:курсатилади|таклиф)|"
                r"клиника|klinika|chegirma|чегирма|скидк|buyurtma|буюртма")
# A report about a scheme, an arrest or a warning: the post describes fraud rather than committing it.
_NEWS = _rx(r"ushland|ушланд|qo'lga olin|кулга олин|задержан|jinoyat ishi|жиноят иши|уголовное дело|javobgarlikka tortil|"
            r"жавобгарликка тортил|qamoqqa olin|камокка олин|ogohlantir|огохлантир|предупрежда|aldanmang|алданманг|"
            r"ehtiyot bo'ling|эхтиет булинг|осторожно|не ведитесь|faktcheking|фактчек|factchecker")

# Distinct diseases. A list of many serious ones next to "treatment" is a typical miracle-cure advert.
DISEASES = [(n, _rx(p)) for n, p in (
    ("инфаркт", r"инфаркт|infarkt"), ("эпилепсия", r"эпилеп|epilep|тутканок|tutqanoq"), ("простата", r"простат|prostat"),
    ("диабет", r"диабет|diabet|kandli|qandli"), ("рак", r"саратон|saraton|онколог|onkolog|\bрак\b"),
    ("бесплодие", r"бепушт|bepusht|бесплод|фарзандсиз|farzandsiz"), ("гепатит", r"гепатит|gepatit"),
    ("печень", r"жигар|jigar|печен"), ("почки", r"буйрак|buyrak|почк"), ("давление", r"гипертон|qon bosim|кон босим|давлени"),
    ("грыжа", r"грыж|grij|гриж"), ("остеохондроз", r"остеохондроз|osteoxondroz"), ("ревматизм", r"ревматизм|revmatizm"),
    ("артрит", r"артрит|artrit"), ("варикоз", r"варикоз|varikoz"), ("гайморит", r"гайморит|gaymorit"),
    ("гастрит", r"гастрит|gastrit|ошкозон|oshqozon"), ("псориаз", r"псориаз|psoriaz"), ("астма", r"астма|astma"),
    ("миома", r"миома|mioma"), ("инсульт", r"инсульт|insult|фалаж|falaj"), ("сердце", r"юрак|yurak|сердц"),
    ("лёгкие", r"упка|o'pka|л[её]гки[хе]"), ("щитовидная железа", r"щитовид|qalqonsimon|калконсимон"))]

_LAW_SEARCH = [(_rx(r"пенси|pensiya|нафака|nafaqa"), "пенсия"), (_rx(r"солик|soliq|налог"), "налог"),
               (_rx(r"жарима|jarima|штраф"), "штраф"), (_rx(r"мигрант|migrant|жунатма|jo'natma|o'tkazma|утказма|перевод"), "трудовая миграция")]


def _stop(text, i):
    """A sentence or list boundary. A dot inside a link or a number (bit.ly, 6.3) is not one."""
    ch = text[i]
    return ch in "!?\n▪•|" or (ch == "." and (i + 1 == len(text) or text[i + 1].isspace()))


def _quote(text, start, end, room=80):
    a = start
    while a > 0 and start - a < room and not _stop(text, a - 1):
        a -= 1
    b = end
    while b < len(text) and b - end < room and not _stop(text, b):
        b += 1
    quote = " ".join(text[a:b].split()).strip(" ,;:—-")
    if a > 0 and not _stop(text, a - 1):
        quote = "…" + quote
    if b < len(text) and not _stop(text, b):
        quote += "…"
    return quote


_NEG_WORD = re.compile(r"\w*?(?:maydi|magan|mas|mang|майди|маган|мас|манг)\b")
_NEG_AFTER = re.compile(r"[^.!?\n]{0,30}?\b(?:emas|эмас)\b")
_NEG_BEFORE = re.compile(r"(?:\bне|\bнет|\bни)\s+\S*\s*$")


def _negated(folded, m):
    """"Income is not guaranteed" must not count as a promise: Uzbek -maydi/-mas/emas and Russian «не»."""
    end = m.end()
    while end < len(folded) and (folded[end].isalnum() or folded[end] == "'"):
        end += 1
    return bool(_NEG_WORD.fullmatch(folded, m.start(), end) or _NEG_AFTER.match(folded, end)
                or _NEG_BEFORE.search(folded[max(0, m.start() - 16):m.start()]))


def _first(pattern, sources):
    """The first source where the pattern matches and is not negated: where, quote."""
    for where, (folded, pos, original) in sources:
        for m in pattern.finditer(folded):
            if not _negated(folded, m):
                return where, _quote(original, pos[m.start()], pos[m.end() - 1] + 1)
    return None


def _publisher(platform, author, key):
    handle = (author or "").lstrip("@").lower()
    if platform == "Telegram":
        if not handle and key.startswith("tg:"):
            handle = key[3:].split("/")[0]
        return handle, handle in MEDIA
    return author or "", bool(author) and bool(_YT_MEDIA.search(fold(author)[0]))


def assess(platform, author, key, title, description, facts=None):
    facts = facts or {}
    # A Telegram "title" is the start of the caption, so the caption alone is the text there.
    caption = platform == "Telegram" and description and description.strip().startswith((title or "").strip()[:80])
    texts = [] if caption else [("заголовок", title)]
    if description and (caption or description.strip() != (title or "").strip()):
        texts.append(("описание" if platform == "YouTube" else "подпись к видео", description))
    if facts.get("speech"):
        texts.append(("речь в ролике", facts["speech"]))
    if facts.get("ocr"):
        texts.append(("надпись в кадре", facts["ocr"]))
    sources = [(where, fold(t)) for where, t in texts if t]
    whole = " \n ".join(s[1][0] for s in sources)
    original = " \n ".join(t for _, t in texts if t)
    handle, media = _publisher(platform, author, key)
    factcheck = handle == "factcheckeruz"
    news = media and bool(_NEWS.search(whole))
    official = bool(_OFFICIAL.search(whole))
    hits, good = [], []

    def add(cat, weight, reason, found=None, norm=None, kind=""):
        where, quote = found or ("", "")
        hits.append(dict(cat=cat, weight=weight, reason=reason, where=where, quote=quote, norm=norm, kind=kind))

    for cat, weight, kind, reason, pattern in SIGNALS:
        found = _first(pattern, sources)
        if not found:
            continue
        if kind == "law_claim" and (media or official):
            continue
        if kind == "rate_news" and _CENTRAL_BANK.search(whole):
            continue
        norm = {"fraud": "fraud", "legal": "false_info", "source": "false_info"}.get(cat)
        if kind in ("easy_income", "guarantee", "referral", "crypto"):
            norm = "finance_ad"
        if cat == "health":
            norm = "folk_ad"
        add(cat, weight, reason, found, norm, kind)

    treat = _TREAT.search(whole)
    folk = next((h for h in hits if h["kind"] == "folk"), None)
    if folk and treat:
        folk["weight"] = 2
    names = [n for n, p in DISEASES if p.search(whole)]
    if treat and len(names) >= 3:
        found = _first(_TREAT, sources)
        add("health", 3 if len(names) >= 6 else 2,
            f"обещают лечить сразу {len(names)} болезней: {', '.join(names[:6])}…", found, "folk_ao", "diseases")
    if treat and any(h["cat"] == "health" for h in hits) and _PHONE.search(original):
        add("health", 1, "записывают на лечение по телефону", _first(re.compile(r"\+?998"), sources), "license", "phone")
    if any(h["cat"] == "health" for h in hits) and _DOCTOR.search(whole):
        add("health", -2, "при этом советуют обратиться к врачу", _first(_DOCTOR, sources), None, "doctor")

    # Clickbait packaging: capitals, alarm emoji and stock phrases.
    letters = [c for c in title or "" if c.isalpha()]
    if len(letters) >= 15 and sum(c.isupper() for c in letters) / len(letters) >= 0.7:
        add("source", 1, "заголовок написан капсом", ("заголовок", " ".join(title.split())[:160]), None, "caps")
    emoji = _EMOJI.findall(original)
    if len(emoji) >= 3:
        add("source", 1, "ставят много тревожных эмодзи: " + " ".join(dict.fromkeys(emoji)), None, None, "emoji")
    bait = {m[0] for m in _CLICKBAIT.finditer(whole)}
    if bait:
        add("source", 2 if len(bait) >= 3 else 1, "используют кликбейт-фразы", _first(_CLICKBAIT, sources), "false_info", "clickbait")
    tags = re.findall(r"#\w+", original)
    words = Counter(w for w in re.findall(r"\w{4,}", fold(description or "")[0]))
    if len(tags) >= 12 or (words and words.most_common(1)[0][1] >= 10):
        add("source", 1, "описание набито ключевыми словами и хештегами — так собирают просмотры", None, None, "stuffing")
    if media and _COMMERCE.search(whole) and _PHONE.search(original) and not _AD_LABEL.search(whole):
        add("source", 1, "похоже на рекламу без пометки «Реклама»", _first(_COMMERCE, sources), "ad_id", "hidden_ad")

    claim = next((h for h in hits if h["kind"] == "law_claim"), None)
    if claim:
        claim["weight"] += any(h["kind"] in ("clickbait", "caps") for h in hits) + (not media)
        if claim["weight"] > 2:
            claim["reason"] += " и подают это как сенсацию"
    content = sum(h["weight"] for h in hits if h["cat"] != "source" and h["weight"] > 0)
    if not media and (content or any(h["cat"] == "source" for h in hits)):
        who = f"«{author}»" if author else "без имени"
        add("source", 1, f"автор не СМИ и не госорган: канал {who}", None, None, "anon")

    for kind_name, weight, label in (("discrepancy", 4, "утверждение расходится с первоисточником"),
                                     ("legal_review", 2, "условие требует оценки юриста")):
        for f in facts.get("findings") or []:
            if f.get("kind") == kind_name:
                hits.append(dict(cat="legal", weight=weight, reason=label + ": " + f.get("reason", ""), where=f.get("where", ""),
                                 quote=f.get("quote", ""), norm=None, kind=kind_name,
                                 source=dict(label=f.get("source_label", ""), url=f.get("source", ""))))
    for f in facts.get("findings") or []:
        if f.get("kind") == "matches":
            good.append("утверждение совпало с первоисточником: " + f.get("source_label", ""))

    # A news report or a warning describes a scheme; its words about money are not an offer.
    if news or factcheck:
        for h in hits:
            if h["cat"] in ("fraud", "health"):
                h["weight"] = 0
    if media:
        # A news outlet mentioning crypto or treatment in a report is not an offer; only strong signals stay.
        for h in hits:
            if h["cat"] in ("fraud", "health") and h["weight"] == 1:
                h["weight"] = 0
    if media:
        good.append(f"публикует известное СМИ @{handle}" if platform == "Telegram" else f"канал известного СМИ «{author}»")
    if official:
        good.append("есть ссылка на официальный документ или госресурс")
    if any(h["kind"] == "doctor" for h in hits):
        good.append("советуют обратиться к врачу")

    categories = {}
    for cat in ORDER:
        score = max(0, sum(h["weight"] for h in hits if h["cat"] == cat))
        c = CATEGORIES[cat]
        level = "high" if score >= c["high"] else "medium" if score >= c["medium"] else "low"
        categories[cat] = dict(score=score, level=level, title=c["title"])
    rank = {"high": 2, "medium": 1, "low": 0}
    # The most specific category leads at the same level: a clickbait law claim is a claim about the law.
    lead = max(ORDER, key=lambda c: (rank[categories[c]["level"]], -ORDER.index(c)))
    level = categories[lead]["level"]
    noun = CATEGORIES[lead]["noun"]
    if factcheck:
        level, headline = "low", "Разбор фактчекеров: публикация проверяет чужое утверждение"
    elif news and level != "high":
        level, headline = "low", "Новость о мошенниках или предупреждение — сама публикация денег не просит"
    elif level == "high":
        headline = f"Высокая вероятность {noun}"
    elif level == "medium":
        headline = f"Есть признаки {noun}"
    else:
        headline = "Признаков мошенничества и ложных утверждений не найдено"

    strong = [h for h in hits if h["weight"] > 0 and categories[h["cat"]]["level"] != "low"]
    weak = [h for h in hits if h["weight"] > 0 and categories[h["cat"]]["level"] == "low"]
    strong.sort(key=lambda h: (h["cat"] != lead, -h["weight"]))
    norms, seen = [], set()
    for h in strong:
        if h["norm"] and h["norm"] not in seen:
            seen.add(h["norm"])
            label, url, excerpt = NORMS[h["norm"]]
            norms.append(dict(label=label, url=url, excerpt=excerpt))
    if any(h["kind"] == "diseases" for h in strong) and "license" not in seen:
        label, url, excerpt = NORMS["license"]
        norms.append(dict(label=label, url=url, excerpt=excerpt))
    if any(h["kind"] == "hidden_ad" for h in weak + strong) and "ad_id" not in seen:
        label, url, excerpt = NORMS["ad_id"]
        norms.append(dict(label=label, url=url, excerpt=excerpt))
    search = ""
    if any(h["kind"] == "law_claim" for h in hits):
        search = next((q for p, q in _LAW_SEARCH if p.search(whole)), "вступает в силу")
    note = ""
    if factcheck:
        note = "Factchecker.uz разбирает утверждение из соцсетей. Вывод редакции — в статье по ссылке в посте."
    elif news:
        note = "Публикация сообщает о схеме, задержании или предупреждает читателей. Слова о деньгах в ней — описание схемы, а не предложение."
    clean = lambda h: {k: v for k, v in h.items() if k not in ("norm",)}
    return dict(level=level, lead=lead if level != "low" else "", headline=headline, note=note,
                score=sum(c["score"] for c in categories.values()),
                because=[clean(h) for h in strong], also=[clean(h) for h in weak], good=good,
                categories=categories, norms=norms, search=search,
                advice=CATEGORIES[lead]["advice"] if level != "low" else "",
                kind="factcheck" if factcheck else "news" if news else "")


def score(text):
    """Download order only: a stronger risk is checked first."""
    return assess("YouTube", "", "", text, "")["score"]
