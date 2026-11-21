"""Compare statements in a video's published caption with dated passages on Lex.uz."""

from datetime import date, datetime
import json
import re
from urllib.parse import parse_qs, urlsplit

import demo_cases
from law_source import lex_passage
from video_speech import MAX_SECONDS


# Each archived link was resolved for the fictional publication date using Lex.uz's
# edition calendar. Production always resolves the edition again for the real post.
_SAMPLE_DATE = date(2025, 2, 14)
_PRICE_CURRENCY = demo_cases.Norm(
    "https://lex.uz/ru/docs/6052633#6054273", "Закон «О рекламе», статья 16, часть 3",
    "указывать цены (тарифы) в иностранной валюте;", date(2022, 9, 9))
_UZ_LOCATION = re.compile(
    r"(?:ташкент|toshkent|tashkent|чиланзар|chilonzor|себзор|юнусабад|yunusobod|сергели|sergeli|"
    r"мирзо.?улугбек|mirzo.?ulug.?bek|яшнабад|yashnobod|мирабад|mirobod|яккасарай|yakkasaroy|"
    r"шайхантаур|shayxontohur|алмазар|olmazor|бектемир|bektemir|учтепа|uchtepa|"
    r"самарканд|samarqand|samarkand|бухар|buxoro|bukhara|наманган|namangan|андижан|andijon|"
    r"ферган|farg.?ona|fergana|нукус|nukus|навои|navoiy|джизак|jizzax|ургенч|urganch|"
    r"узбекистан|uzbekistan|o.?zbekiston|ўзбекистон)", re.I)
_FLAT_DETAILS = re.compile(
    r"(?:комнат|\bэтаж|квартир|\bqavat|\bxona|\bжк\b|\bkvartira|новострой|\bдом(?:а|ов)?\b|\bкоттедж|"
    r"\bофис|помещени|недвижим|ko.?chmas\s*mulk|kuchmasmulk|\buy-?joy|uyjoy|\bhovli)", re.I)
_FLAT_AREA = re.compile(r"\d\s*(?:[мm]\s*[²2]|кв\.?\s*м|kv\.?\s*m|кв\b|сот(?:ок|ки|ка)\b)", re.I)
# A listing names the object's size, offers it for sale or gives a contact number.
_LISTING = re.compile(
    r"(?:прода[её]тся|продам|продаю|продаж|купить|sotiladi|sotuv|sotaman|сотилади|сотув|\bsale\b|"
    r"\+?998[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})", re.I)
_AMOUNT = r"\d(?:[\d.,\u00a0 ]*\d)?"
# "у.е." (conventional units) in Uzbek listings stand for US dollars. OCR may read the letters as
# Latin "y.e.". The first dot is required: a bare "уе" is an ordinary syllable.
_UE = r"[уy]\.[ \t]?[еe]\.?(?!\w)"
# A price field in a description: the price alone on its line, optionally after a list bullet.
_PRICE_LINE = re.compile(
    rf"^[ \t]*(?:[•·▪●◦*–—-]+[ \t]*)?(?P<quote>(?:(?:цена|стоимость|narx[i]?|нархи)\s*[:—-]?\s*)?"
    rf"(?:\$\s*{_AMOUNT}|{_AMOUNT}\s*(?:\$|USD|доллар(?:ов|а)?|{_UE}))(?:\s*за\s*(?:м\s*[²2]|кв\.?\s*м)\.?)?"
    rf"[ \t]*[.!]?)[ \t]*$", re.I | re.M)
# Titles, on-screen text and speech carry the price inline, next to other listing details.
# Digits and currency must share a line: OCR splits a phone number from a stray "$" above it.
_NUMBER = r"[1-9]\d{0,2}(?:[ \u00a0.,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|[1-9]\d*(?:[.,]\d+)?"
_PRICE_INLINE = re.compile(
    rf"(?<![\w$])(?:\$[ \t\u00a0]*(?:{_NUMBER})(?:[ \t]*(?:[kк]\b|тыс\.?))?"
    rf"|(?:{_NUMBER})[ \t\u00a0]*(?:\$|USD\b|доллар(?:ов|а|ы)?\b|dollar\w*|{_UE}))", re.I)
# A price table across several cities is market analytics, not an offer of one object.
_FOREIGN = re.compile(r"(?:душанбе|астан[аеуы]\b|алмат|бишкек|стамбул|istanbul|дубай|dubai|москв|moscow|"
                      r"тбилиси|\bбаку\b|\bош\b|ошск)", re.I)
_AD_LABEL = "Закон Республики Узбекистан «О рекламе», статья 42, часть 2, абзац 4"
_AD_RU = demo_cases.Norm(
    "https://lex.uz/ru/docs/6052633?ONDATE=05.11.2024#6054890", _AD_LABEL,
    "сообщать информацию о размерах ожидаемого дохода и о будущих прибылях (доходах);",
    date(2022, 9, 9),
)
_AD_UZ = demo_cases.Norm(
    "https://lex.uz/ru/docs/6052631?ONDATE=05.11.2024#6053616", _AD_LABEL,
    "кутилаётган даромад миқдорлари ва истиқболдаги фойда (даромад) тўғрисидаги ахборотни маълум қилиш;",
    date(2022, 9, 9),
)
# The cited bullet relies on the preceding paragraph for its meaning. Check both
# in the same edition before reporting a possible breach of the prohibition.
_AD_SCOPE = {
    _AD_RU: ("6054887", "В рекламе ценных бумаг и услуг (за исключением банковских вкладов), "
             "связанных с привлечением средств населения, или лиц, оказывающих эти услуги, запрещается:"),
    _AD_UZ: ("6053613", "Қимматли қоғозларнинг ва аҳоли маблағларини жалб этиш билан боғлиқ хизматларнинг "
             "(бундан банк омонатлари мустасно) ёки ушбу хизматларни кўрсатувчи шахсларнинг рекламасида "
             "қуйидагилар тақиқланади:"),
}

_UZ_CYR = str.maketrans(dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшъэюяўқғҳ",
    "a b v g d e yo j z i y k l m n o p r s t u f x ts ch sh ' e yu ya o' q g' h".split(),
)))
_APOSTROPHES = str.maketrans({ch: "'" for ch in "‘’ʻʼ`´"})

# Keep news, warnings, attributed quotations, negations, comparisons with past
# performance and hypotheticals out of this affirmative-statement detector.
_RU_CONTEXT = re.compile(
    r"\b(?:не|нет|нельзя|якобы|если|пример\w*|допустим|раньше|ранее|прошл\w*|"
    r"мошен\w*|фейк\w*|опровер\w*|опроверж\w*|предупреж\w*|осторожно|запрещ\w*|"
    r"новост(?!рой)\w*|сообща(?:ет|ют|лось|ли)|сообщил\w*|цит\w*|разбор|расследован\w*|"
    r"обман\w*|говорят|утвержда\w*|заявил\w*|выдум\w*|миф|провер(?:яем|или)|неправда|ложь|"
    r"рекламодатель|пост автора|в ролике|объявление гласит|написа(?:ли|л|но)|"
    r"на самом деле|в действительности|в сети распространя\w*|пишут|фактчек\w*|"
    r"вклад\w*|депозит\w*)\b"
)
_UZ_CONTEXT = re.compile(
    r"\b(?:agar|masalan|misol\w*|go'yoki|yolg'on\w*|rad et\w*|raddiya|"
    r"firibgar\w*|ehtiyot\w*|ogohlantir\w*|xabari\w*|xabar ber\w*|yangilik\w*|"
    r"deyish\w*|deyilgan|deyiladi|dedi|iqtibos|misol tariqasida|taqiqlan\w*|"
    r"o'tgan|avval\w*|kafolatlanmaydi|"
    r"\w+ma(?:ymiz|ydi|ng|nglar|slik)|emas|yo'q|omonat\w*|depozit\w*)\b"
)
# Words that make the whole publication a warning, debunk or list of myths. Unlike the
# hedges above they rarely occur in an ordinary listing or offer, so they apply video-wide.
# Ordinary negations, questions and quotes are checked only next to the quoted claim:
# real speech and descriptions contain them almost everywhere.
_FRAMING_RU = re.compile(
    r"\b(?:мошен\w*|фейк\w*|опроверж\w*|опроверг\w*|неправда|ложь|миф\w*|разоблач\w*|фактчек\w*|"
    r"якобы|осторожно|не верьте|не покупайте|не ведитесь)\b")
_FRAMING_UZ = re.compile(r"\b(?:firibgar\w*|yolg'on\w*|raddiya|ishonmang|aldanmang|soxta\w*)\b")
_QUOTED = re.compile(r"""[«»“”„"?]|(?<!\w)'(?=\w)""")
_SENTENCES = re.compile(r"(?:[^.!?\n]|\.(?=\d))+[.!?]*")
_RATE_RU = r"\d+(?:[.,]\d+)?\s*(?:%|процент(?:а|ов)?\b|сум(?:ов|а)?\b)"
_RATE_UZ = r"\d+(?:[.,]\d+)?\s*(?:%|foiz(?:lik)?\b|so'm\b)"
_RETURN_RU = rf"(?:доход(?:ность)?|прибыль)\s*(?:[—–:-]\s*)?(?:(?:в размере|от|до)\s+)?{_RATE_RU}"

_RU_OFFER = re.compile(
    r"\b(?:купите|покупайте|приобретайте|приобретите|закажите|предлагаем (?:вам )?купить|"
    r"вложите (?:средства |деньги )?в|инвестируйте в)\s+"
    r"(?:(?:наши|наши новые|эти|обыкновенные|простые|корпоративные|государственные)\s+){0,2}"
    r"(?:акции|облигации|ценные бумаги)\b"
)
_RU_AUDIENCE = re.compile(
    r"\b(?:(?:жител(?:и|ям|ей)|резидент(?:ы|ам|ов)|инвестор(?:ы|ам|ов)) "
    r"(?:Узбекистана|в Узбекистане)|(?:доступн[а-я]*|предложение|продажа) в Узбекистане)\b", re.I
)
_RU_RETURN = re.compile(
    rf"\b(?:(?:получите|получайте|гарантируем|обещаем)\s+(?:вам\s+)?"
    rf"(?:{_RETURN_RU}|{_RATE_RU}\s+(?:годовых|дохода|прибыли))|"
    rf"(?:ожидаем(?:ая|ый)|гарантированн(?:ая|ый))\s+{_RETURN_RU}|"
    rf"(?:доход(?:ность)?|прибыль)\s+составит\s+(?:от\s+|до\s+)?{_RATE_RU})"
)
_UZ_OFFER = re.compile(
    r"\b(?:aksiya(?:lar)?(?:imiz)?(?:ni)?|obligatsiya(?:lar)?(?:imiz)?(?:ni)?|"
    r"qimmatli qog'oz(?:lar)?(?:imiz)?(?:ni)?)\s+(?:hozir\s+)?(?:sotib|xarid)\s+"
    r"(?:oling|qiling)\b|\b(?:aksiyalar|obligatsiyalar)ga\s+(?:sarmoya|mablag')\s+kiriting\b"
)
_UZ_AUDIENCE = re.compile(
    r"\bo'zbekiston(?:dagi)?\s+(?:aholi(?:si(?:ga)?|ga)|fuqarolari(?:ga)?|"
    r"rezidentlari(?:ga)?|investorlar(?:i|ga|iga)?|aholisi uchun)\b|\bo'zbekistonliklar(?:ga)?\b"
)
_UZ_RETURN = re.compile(
    rf"(?:{_RATE_UZ}\s+(?:sof\s+)?(?:daromad|foyda)(?:ni)?\s+"
    rf"(?:olasiz|oling|kafolatlaymiz|kafolatlanadi|beramiz|va'da qilamiz)\b|"
    rf"(?:kutilayotgan|kafolatlangan)\s+(?:daromad|foyda)\s*[:—–-]?\s*{_RATE_UZ})"
)


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            day = date.fromisoformat(value)
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
            day = stamp.astimezone(demo_cases.TASHKENT).date()
        return day if day <= datetime.now(demo_cases.TASHKENT).date() else None
    except (ValueError, OverflowError):
        return None


def _plain(text):
    # Punctuation inside a tracking link is unrelated to the author's statement.
    context = re.sub(r"https?://\S+", " ", text, flags=re.I)
    return " ".join(context.casefold().translate(_APOSTROPHES).split())


def _qualified(caption, punctuation=True):
    # OCR turns frame edges into stray quotes and question marks, so frames pass punctuation=False.
    plain = _plain(caption)
    return ((punctuation and _QUOTED.search(plain)) or _RU_CONTEXT.search(plain)
            or _UZ_CONTEXT.search(plain.translate(_UZ_CYR)))


def framed(text):
    """True when the publication as a whole debunks, warns about or lists myths."""
    plain = _plain(text)
    return bool(_FRAMING_RU.search(plain) or _FRAMING_UZ.search(plain.translate(_UZ_CYR)))


def _near(source, begin, finish):
    """The quoted span together with one sentence on each side of it."""
    spans = [(m.start(), m.end()) for m in _SENTENCES.finditer(source)]
    inside = [i for i, (start, end) in enumerate(spans) if start < finish and end > begin]
    if inside:
        begin = min(begin, spans[max(inside[0] - 1, 0)][0])
        finish = max(finish, spans[min(inside[-1] + 1, len(spans) - 1)][1])
    return source[begin:finish]


def qualified_near(source, begin, finish, punctuation=True):
    return bool(_qualified(_near(source, begin, finish), punctuation))


def _ad_claim(caption):
    plain = " ".join(caption.casefold().translate(_APOSTROPHES).split())
    uz = plain.translate(_UZ_CYR)
    sentences = list(_SENTENCES.finditer(caption))
    for norm, text, audience, offer, returns in (
        (_AD_RU, plain, _RU_AUDIENCE, _RU_OFFER, _RU_RETURN),
        (_AD_UZ, uz, _UZ_AUDIENCE, _UZ_OFFER, _UZ_RETURN),
    ):
        if not (offer.search(text) and returns.search(text) and audience.search(text)):
            continue
        # Report the shortest set of neighbouring sentences that contains the
        # purchase invitation, local audience and promise. Quote the source verbatim.
        for count in range(1, 5):
            for first, last in zip(sentences, sentences[count - 1:]):
                if last.end() - first.start() > 550:
                    continue
                quote = caption[first.start():last.end()].strip()
                window = " ".join(quote.casefold().translate(_APOSTROPHES).split())
                if norm == _AD_UZ:
                    window = window.translate(_UZ_CYR)
                if offer.search(window) and returns.search(window) and audience.search(window):
                    return norm, quote
    return None


def _reference(norm, day, training):
    archived = urlsplit(norm.url)
    if training:
        if day != _SAMPLE_DATE:
            raise ValueError("Учебная копия нормы проверена для публикаций от 14.02.2025.")
        return norm.url, norm.excerpt
    # Remove the fixture edition. An explicit ONDATE overrides edition_date on Lex.uz.
    day_label = day.strftime("%d.%m.%Y")
    ref = lex_passage(archived._replace(query="").geturl(), norm.excerpt, day_label)
    if ref["passage"] != norm.excerpt:
        raise ValueError("Формулировка нормы изменилась. Требуется сверка редакции на Lex.uz.")
    if norm in _AD_SCOPE:
        anchor, scope = _AD_SCOPE[norm]
        same_edition = urlsplit(ref["url"])._replace(fragment=anchor).geturl()
        if lex_passage(same_edition, scope)["passage"] != scope:
            raise ValueError("Область действия нормы изменилась. Проверьте статью 42 на Lex.uz.")
    return ref["url"], ref["passage"]


def _segments(value, max_seconds=MAX_SECONDS):
    if isinstance(value, str) and len(value) <= 100_000:
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return []
    if not isinstance(value, list) or len(value) > 1000:
        return []
    previous_end = length = 0
    for part in value:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str) or not part["text"].strip():
            return []
        start, end = part.get("start"), part.get("end")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not previous_end <= start < end <= max_seconds):
            return []
        length += len(part["text"]) + 1
        if length > 16000:
            return []
        previous_end = end
    return value


def _inputs(row):
    caption = row.get("description") or ""
    if row.get("method") == "manual" or not isinstance(caption, str) or len(caption) > 16000:
        return "", []
    limit = 12 * 3600 if row.get("subtitle_preview") or row.get("media_report") else MAX_SECONDS
    cues = row.get("media_report", {}).get("speech", {}).get("segments") or row.get("speech")
    return caption.strip(), _segments(cues, limit)


def _video_title(row):
    return (row.get("video_title") or "").strip() if row.get("platform") == "youtube" and row.get("method") != "manual" else ""


def can_check(row):
    return _day(row.get("published_at")) is not None and (any(_inputs(row)) or bool(_video_title(row)) or bool(row.get("media_report", {}).get("frames")) or bool(row.get("media_report", {}).get("speech", {}).get("text")))


def _housing_price(source, kind, context):
    """A dollar price in an Uzbek property listing. The listing signs may sit anywhere in the
    publication: the title names the flat, the description gives the area, a frame shows the price."""
    if not (_UZ_LOCATION.search(context) and _FLAT_DETAILS.search(context)
            and (_FLAT_AREA.search(context) or _LISTING.search(context))):
        return None
    if kind == "caption":
        # Descriptions also hold market comparisons. Only a dedicated price line is a listing price.
        return _strongest(price["quote"].strip() for price in _PRICE_LINE.finditer(source))
    if kind in ("frame", "title") and _FOREIGN.search(source):
        return None
    # OCR reads "S", list markers and digits of a phone number as "$". On frames, require an
    # amount a property can cost; in a title or speech a rent of a few hundred dollars counts.
    minimum = 1000 if kind == "frame" else 100
    return _strongest(
        price[0].strip() for price in _PRICE_INLINE.finditer(source)
        if _dollars(price[0]) >= minimum
        and (kind in ("frame", "title") or not _FOREIGN.search(_near(source, price.start(), price.end()))))


def _strongest(prices):
    """The first dollar price, else the first price in "у.е.": a dollar sign is direct evidence,
    "у.е." only stand for dollars."""
    prices = list(prices)
    return next((price for price in prices if not re.search(_UE, price, re.I)), prices[0] if prices else None)


def _dollars(price):
    number = re.search(_NUMBER, price)[0]
    value = int(re.sub(r"\D", "", re.sub(r"[.,]\d{1,2}$", "", number)))
    return value * 1000 if re.search(r"\d[ \t]*(?:[kк]\b|тыс)", price, re.I) else value


def _match(caption, kind="caption", context=None):
    price = _housing_price(caption, kind, caption if context is None else context)
    if price:
        if re.search(_UE, price, re.I):
            title = "В объявлении о недвижимости цена указана в у.е."
            currency = ("Цена указана в условных единицах (у.е.). В объявлениях о жилье у.е. обычно означают "
                        "доллары США, поэтому такая цена может приравниваться к цене в иностранной валюте. "
                        "Статья 16 запрещает указывать в рекламе цены в иностранной валюте.")
        else:
            title = "В объявлении о недвижимости указана цена в долларах"
            currency = ("В объявлении о недвижимости указана цена в иностранной валюте. Статья 16 запрещает "
                        "такое указание в рекламе.")
        return _PRICE_CURRENCY, dict(rule_id="housing-ad-currency", category="advertising_review",
            title=title, quote=price,
            reason=currency + " Для оценки этого ролика нужно установить, продавал ли "
                   "собственник жильё частным образом или посредник продвигал объект. Пункты 5 и 9 "
                   "статьи 2 предусматривают исключения для объявлений вне предпринимательской деятельности "
                   "и информации продавца об ассортименте или характеристиках товара.")
    ad = _ad_claim(caption)
    if ad:
        norm, quote = ad
        rule_id, category = "ad-securities-return", "potential_violation"
        title = "Возможное нарушение в рекламе ценных бумаг"
        reason = ("В тексте жителям или инвесторам Узбекистана предлагают приобрести ценные бумаги "
                  "и называют размер будущего дохода. Статья 42 запрещает такие сведения в рекламе "
                  "ценных бумаг. Проверьте, является ли публикация рекламой и кому она адресована.")
    else:
        quote = caption.strip()
        comparison = demo_cases._comparison(quote)
        if not comparison or comparison[0] != demo_cases.LEAVE:
            for sentence in _SENTENCES.finditer(caption):
                quote = sentence[0].strip()
                comparison = demo_cases._comparison(quote)
                if comparison and comparison[0] == demo_cases.LEAVE:
                    break
            else:
                return None
        norm, reason = comparison
        rule_id, category = "labour-minimum-leave", "inaccurate_claim"
        title = "Возможная ошибка в утверждении о минимальном отпуске"
    return norm, dict(rule_id=rule_id, category=category, title=title, quote=quote, reason=reason)


def _clock(seconds):
    milliseconds = round(seconds * 1000)
    whole, remainder = divmod(milliseconds, 1000)
    stamp = f"{whole // 60:02d}:{whole % 60:02d}"
    if remainder:
        stamp += f".{remainder // 10:02d}" if remainder % 10 == 0 else f".{remainder:03d}"
    return stamp


def _time_range(segments, begin, finish):
    offset = 0
    times = []
    for segment in segments:
        stop = offset + len(segment["text"].strip())
        if offset < finish and stop > begin:
            times.append(segment)
        offset = stop + 1
    return times[0]["start"], times[-1]["end"]


def check_video(row, *, training=False):
    """Check the whole available context. Prefer a timed quote when both sources match."""
    if training and (row.get("kind") != "training" or row.get("url")):
        raise ValueError("Архивные цитаты используются только для учебных материалов.")
    day = _day(row.get("published_at"))
    caption, segments = _inputs(row)
    # Segment boundaries also delimit search windows when ASR supplies no punctuation.
    speech = "\n".join(segment["text"].strip() for segment in segments)
    title = _video_title(row)
    media = row.get("media_report", {})
    untimed = media.get("speech", {}).get("text", "")
    frames = media.get("frames", [])
    context = " ".join([title, caption, speech, untimed] + [f["text"] for f in frames])
    if day is None or framed(context) or re.search(r"вымышленное объявление|учебный ролик", context, re.I):
        return []
    findings = {}
    sources = [(speech, "speech", None), (untimed, "voice_text", None)]
    sources.extend((frame["text"], "frame", frame["start"]) for frame in frames)
    sources.extend([(caption, "caption", None), (title, "title", None)])
    for source, kind, frame_at in sources:
        matched = _match(source, kind, context) if source else None
        if matched is None:
            continue
        norm, hit = matched
        earlier = findings.get(hit["rule_id"])
        if (earlier and _strongest([earlier["quote"], hit["quote"]]) == earlier["quote"]) or day < norm.effective_from:
            continue
        begin = source.index(hit["quote"])
        finish = begin + len(hit["quote"])
        # A negation, question or attribution next to the claim makes it reported speech, not a claim.
        if qualified_near(source, begin, finish, punctuation=kind != "frame"):
            continue
        url, excerpt = _reference(norm, day, training)
        edition = parse_qs(urlsplit(url).query)["ONDATE"][0].split()[0]
        label = norm.label.partition(" (ред.")[0] + f" (ред. от {edition}, на дату публикации {day:%d.%m.%Y})"
        if kind == "speech":
            start, end = _time_range(segments, begin, finish)
            where = f"Речь в ролике, {_clock(start)}–{_clock(end)} · автоматическая расшифровка"
            if media:
                where = f"Речь · {_clock(start)}–{_clock(end)}"
            if row.get("subtitle_preview"):
                where = f"Субтитры из загруженного файла, {_clock(start)}–{_clock(end)}"
            hit.update(start=start, end=end, quote=hit["quote"].replace("\n", " "))
        elif kind == "frame":
            where = f"Надпись на кадре · {_clock(frame_at)} · OCR"
            hit.update(start=frame_at)
        elif kind == "voice_text":
            where = "Речь, без таймкода"
        else:
            where = "Учебная подпись к видео" if training else "Текст публикации"
            if row.get("platform") == "youtube":
                where = "Заголовок YouTube" if kind == "title" else "Описание YouTube"
            if begin or finish < len(source):
                where += f", символы {begin + 1}–{finish}"
        hit.update(evidence_kind=kind, where=where, law_url=url, law_excerpt=excerpt, law_label=label)
        findings[hit["rule_id"]] = hit
    return list(findings.values())


def training_examples():
    """Fictional captions with no social-media permalink. The day identifies the legal scenario."""
    ru = ("Для жителей Узбекистана! Купите наши обыкновенные акции. "
          "Гарантируем доход 30% в месяц.")
    uz = ("O‘zbekiston aholisi uchun taklif! Aksiyalarimizni sotib oling. "
          "Har oy 25 foiz daromad olasiz.")
    samples = [
        ("ad-ru", "Предложение акций", "instagram", ru, 1),
        ("ad-uz", "Предложение акций на узбекском", "facebook", uz, 1),
        ("leave", demo_cases.LEAVE_TOPIC, "instagram", demo_cases.LEAVE_QUOTE, 1),
        ("warning", "Предупреждение о рекламе", "facebook", "Осторожно, мошенники пишут: «" + ru + "»", 0),
        ("no-guarantee", "Отрицание обещания", "instagram", uz.replace("olasiz", "kafolatlanmaydi"), 0),
        ("correct-leave", "Верная продолжительность отпуска", "facebook", demo_cases.TRAINING_SAMPLES[-1][1], 0),
    ]
    examples = [dict(key=key, kind="training", platform=platform, url="", published_at=_SAMPLE_DATE.isoformat(),
                 title=title, description=caption, expected=expected)
            for key, title, platform, caption, expected in samples]
    # Actual model output from the synthetic audio in the bundled educational video.
    examples.append(dict(key="spoken-ad", kind="training", platform="instagram", url="",
                         demo_media_url="/demo-media/securities.mp4", published_at=_SAMPLE_DATE.isoformat(),
                         title="Обещание дохода в речи", description="Объявление с озвучкой.", expected=1,
                         speech=[dict(start=2.70, end=4.72, text="Для жителей Узбекистана"),
                                 dict(start=4.72, end=7.72, text="Купите наши обыкновенные акции"),
                                 dict(start=7.72, end=11.64, text="Гарантируем доход 30% в месяц")]))
    return examples
