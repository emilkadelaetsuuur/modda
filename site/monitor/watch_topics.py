"""Queries target the underlying claim, so a publisher need not use the word 'fake'."""
from dataclasses import dataclass
import re
import unicodedata

@dataclass(frozen=True)
class Topic:
    label: str
    youtube: tuple
    telegram: tuple
    kind: str = "claim"

TOPICS = {
    "investments": Topic("Инвестиции и обещания дохода",
                         ("инвестиции Узбекистан доход", "O‘zbekiston investitsiya daromad"), ("investitsiya","инвестиции")),
    "currency": Topic("Официальный курс доллара", ("курс доллара Узбекистан","O‘zbekiston dollar kursi"), ("dollar","доллар")),
    "labour": Topic("Трудовые права и отпуск", ("трудовой отпуск Узбекистан","O‘zbekiston mehnat ta’tili"), ("ta’til","отпуск")),
    "housing": Topic("Цены в рекламе жилья", ("Toshkent kvartira sotiladi","продажа квартир Ташкент"), ("kvartira","квартира")),
    "impersonation": Topic("Видеообращения и подмена голоса", ("Узбекистан видеообращение","O‘zbekiston videomurojaat"),
                           ("murojaat","обращение")),
}

# The automatic search looks for popular videos where fraud, false claims about the law and unreliable
# sources usually appear in Uzbekistan. risk_review scores each publication; the media check is secondary.
# The name stays for stored runs and callers; rows of earlier runs keep their own label in df_runs.detail.
DEEPFAKE_TOPICS = {
    # Telegram terms cover Uzbek Latin, Uzbek Cyrillic and Russian, because each channel writes in one of them.
    "df_scam": Topic("Мошеннические схемы и «лёгкий заработок»", ("pul ishlash internetda", "passiv daromad"),
                     ("firibgar", "фирибгар", "мошенни", "aldov"), "risk"),
    "df_money": Topic("Инвестиции и обещания дохода", ("investitsiya daromad", "kripto daromad"),
                      ("investitsiya", "инвестиция", "инвестиции", "daromad"), "risk"),
    "df_laws": Topic("Новые законы, штрафы и налоги", ("yangi qonun kuchga kiradi", "янги қонун жарима"),
                     ("yangi qonun", "янги қонун", "jarima", "штраф"), "risk"),
    "df_payouts": Topic("Пенсии, пособия и выплаты", ("pensiya yangilik", "nafaqa oshadi"),
                        ("pensiya", "пенсия", "nafaqa", "нафақа"), "risk"),
    "df_health": Topic("Лечение и «чудо-лекарства»", ("davolash", "shifokor"), ("davolash", "даволаш", "лечение", "shifokor"), "risk"),
    "df_leaders": Topic("Обращения от имени руководства", ("Mirziyoyev", "Мирзиёев"), ("murojaat", "мурожаат", "обращение"), "risk"),
}

def selection(key, custom):
    if key not in (*DEEPFAKE_TOPICS, *TOPICS, "auto", "custom"):
        raise ValueError("Выберите тему из списка.")
    if key != "custom":
        return key, ""
    custom = " ".join(unicodedata.normalize("NFKC", custom).split())
    if not (3<=len(custom)<=80 and re.fullmatch(r"[\w\s.,’‘ʻʼ'():–—-]+",custom)
            and sum(c.isalpha() for c in custom)>=2):
        raise ValueError("Введите тему длиной от 3 до 80 символов, без ссылки.")
    return key, custom

def resolve(key, custom, turn):
    if key=="auto":
        return tuple(DEEPFAKE_TOPICS.values())[turn%len(DEEPFAKE_TOPICS)]
    if key=="custom":
        # The country in the query keeps YouTube on Uzbek videos. The bare topic is a fallback for thin results.
        if _UZ.search(_fold(custom)):
            return Topic(custom,(custom,),(custom,),"risk")
        country = "Узбекистан" if re.search(r"[а-яё]",custom,re.I) else "O‘zbekiston"
        return Topic(custom,(custom+" "+country,custom),(custom,),"risk")
    return DEEPFAKE_TOPICS.get(key) or TOPICS[key]


_NUMERIC = re.compile(r"\b\d+(?:[ .,]\d+)*\s*(?:%|процент|сум|so[’ʻ‘ʼ']m|foiz|дн|дней|kun|UZS)",re.I)
_PRIMARY = re.compile(r"по закону|стать[еяи] \d|минимальн\w* отпуск|постановлени|курс.{0,30}(?:цб|марказий банк)|официальный курс|rasmiy kurs|markaziy bank|mehnat kodeksi|qarorga|qonunga",re.I)
_PROMISE = re.compile(r"гарантир\w*|без риска|выплатят всем|обещаем доход|обращение от имени|kafolatla\w*|xavfsiz daromad",re.I)

def priority(text):
    # Only determines download order. Verification uses the actual quote and its source.
    return 2*bool(_PRIMARY.search(text))+bool(_NUMERIC.search(text))+bool(_PROMISE.search(text))

_EXCHANGE = re.compile(r"курс|kurs|валют|valyuta|обмен|ayirboshl|uzs|сўм|сумов|so[’ʻ‘ʼ']m",re.I)

def relevant(topic, text):
    if topic == TOPICS["currency"]:
        return bool(_EXCHANGE.search(text))
    return True

_HINT = re.compile(r"deepfake|дипфейк|dipfeyk|фейк|feyk|soxta|сохта|поддел|сгенерир|нейросет|sun'iy intellekt|сунъий интеллект"
                   r"|\bии\b|\bai\b|ovoz.{0,20}(?:o'xshat|taqlid)|голос.{0,20}(?:подмен|клон)|aldov|мошенни|firibgar", re.I)

def deepfake_hint(text):
    """1 when the text itself mentions a fake, AI generation or fraud. Used to rank, never as a verdict."""
    return int(bool(_HINT.search(_fold(text))))


def _fold(text):
    """Lower-case text with one apostrophe and the Latin spelling used by some Uzbek channels (ö, ğ, ş, ç)."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[‘’ʻʼ`´']", "'", text)
    return text.replace("ö", "o'").replace("ğ", "g'").replace("ş", "sh").replace("ç", "ch")

# Explicit ties to Uzbekistan: the country, regions and large cities, the currency, state bodies and leaders.
# Place names match as word prefixes, so Uzbek case endings (Toshkentda, Andijonning) still count.
_UZ = re.compile(r"🇺🇿|\b(?:o'?zbekiston|ўзбекистон|узбекистан|uzbekistan|o'?zbek|ўзбек|узбек|uzbek"
                 r"|toshkent|tashkent|ташкент|тошкент|samarqand|samarkand|самарканд|самарқанд|buxoro|bukhara|бухар|бухоро"
                 r"|andijon|andijan|андижан|андижон|farg'ona|fergana|ферган|фарғона|namangan|наманган"
                 r"|qashqadaryo|кашкадар|қашқадарё|surxondaryo|сурхандар|сурхондарё|xorazm|хорезм|хоразм"
                 r"|navoiy|навои|jizzax|джизак|жиззах|sirdaryo|сырдар|сирдарё|qoraqalpog'|karakalpak|каракалпак|қорақалпоғ"
                 r"|nukus|нукус|termiz|термез|urganch|ургенч|qarshi|карши|қарши|chirchiq|чирчик|guliston|гулистан"
                 r"|qo'qon|коканд|қўқон|marg'ilon|маргилан|марғилон|angren|ангрен|olmaliq|алмалык|олмалиқ|shahrisabz|шахрисабз"
                 r"|chimyon|чимён|чимган|chorvoq|чарвак|чорвоқ|zomin|зомин|chilonzor|чиланзар|чилонзор|yunusobod|юнусабад|юнусобод"
                 r"|mirziyoyev|мирзиёев|aripov|арипов|oliy majlis|олий мажлис|markaziy bank|марказий банк"
                 r"|viloyat|вилоят|hokim|ҳоким|хоким|iiv\b|иив\b|dxx\b|дхх\b|mygov|uzcard|humo\b|bojxona|божхона"
                 r"|soliq qo'mita|солиқ қўмита|ichki ishlar|ички ишлар|mahalliy|маҳаллий"
                 r"|so'm\b|сўм\b|сум\b|сумов|сума\b|uzs\b|mahalla|махалл)", re.I)
# A foreign subject without any Uzbek tie: world news posted by Uzbek channels is not our scope.
_FOREIGN = re.compile(r"\b(?:rossiya|россия|росси|ukraina|украин|eron(?!ov)|иран|isroil(?!ov)|исроил(?!ов)|израил|aqsh|сша|amerika|америк"
                      r"|xitoy|китай|turkiya|турци|qozog'iston|казахстан|qirg'iziston|кыргызстан|киргиз|tojikiston|таджикистан"
                      r"|afg'oniston|афганистан|turkmaniston|туркмен|ozarbayjon|азербайджан|armaniston|армени|gruziya|грузи(?:я|и|ю|ей)\b"
                      r"|hindiston|инди(?:я|и|ю|ей)\b|pokiston|пакистан|yaponiya|япони|koreya|коре(?:я|и|ю|йск)|germaniya|германи|fransiya|франци"
                      r"|britaniya|британи|angliya|англи(?:я|и|ю|ей)\b|italiya|итали|ispaniya|испани|polsha|польш|belarus|беларус|yevropa|европ"
                      r"|saudiya|саудов|misr|египт|suriya|сири(?:я|и|ю|ей|йск)|iroq|ирак|g'azo|сектор\w* газ|falastin|палестин|indoneziya|индонези"
                      r"|moskva|москв|london|лондон|parij|париж|pekin|пекин|tehron|тегеран|kiyev|киев|istanbul|стамбул"
                      r"|рубл|rubl|путин|putin|зеленск|zelensk|tramp|trump|трамп|baydin|biden|байден|netanyax|netanyahu|нетаньях"
                      r"|erdo'g'an|erdogan|эрдоган|tokayev|токаев|lukashenko|лукашенко|makron|macron|макрон)", re.I)
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
# Channel signatures, links and @mentions say who published, not what the post is about.
_SIGNATURE = re.compile(r"https?://\S+|t\.me/\S+|@\w+|\b[\w-]+(?:\.[\w-]+)*\.uz\b")
_UZ_DOMAIN = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.uz\b")
# Uzbek-language text: the letter ў, o'/g' or frequent Uzbek words.
_UZ_LANGUAGE = re.compile(r"ў|\b\w*(?:o'|g')\w+|\b(?:va|bilan|uchun|haqida|qanday|nima|emas|kerak|bugun|yangi|narxi|kursi|qilish"
                          r"|ва|билан|учун|ҳақида|қандай|керак|бугун|янги)\b", re.I)
# Kazakh and Kyrgyz Cyrillic letters that Uzbek does not use.
_OTHER_TURKIC = re.compile(r"[әіңөүұһ]")

def uzbek_related(text, platform, publisher="", language=""):
    """Whether a publication is about Uzbekistan or addressed to its audience.

    An explicit tie (the country, a region or city, the sum, state bodies) always counts, and a foreign subject
    without one excludes the publication. Telegram channels post world news in Uzbek, so a Telegram post needs
    the explicit tie. A YouTube video in Uzbek or from an Uzbek publisher (a .uz site, Uzbek audio track) counts too."""
    folded = _fold(text)
    subject = _SIGNATURE.sub(" ", folded)
    if _UZ.search(subject):
        return True
    if _FOREIGN.search(subject) or _FLAG.search(subject) or platform != "YouTube":
        return False
    if language == "uz" or _UZ.search(_fold(publisher)) or _UZ_DOMAIN.search(_fold(publisher)) or _UZ_DOMAIN.search(folded):
        return True
    return bool(_UZ_LANGUAGE.search(subject)) and not _OTHER_TURKIC.search(subject)
