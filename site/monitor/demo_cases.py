"""Local Instagram review cards. Transactions are owned by the caller."""

from datetime import date, datetime, timedelta, timezone
import re
from typing import NamedTuple
from urllib.parse import parse_qsl, urlsplit

from telegram_source import public_post_url


LAW_URL = "https://lex.uz/uz/acts/7565069?ONDATE=11.06.2025+00#7567520"
EXPLANATION_URL = "https://gov.uz/ru/vnadzor/news/view/89203"
LAW_LABEL = "Постановление Кабинета Министров № 352 от 07.06.2025, приложение 1, п. 2 (ред. на 11.06.2025)"
LAW_EXCERPT = "сунъий сув объектларидан олинадиган сувдан фойдаланиш ва сув истеъмоли қоидаларини бузиш"
COMPARISON = (
    "В утверждении обещают выплату за съёмку мойки машины водопроводной водой. "
    "Пункт 2 приложения 1 в пересказе по-русски ограничивает действие порядка правонарушениями "
    "по части 2 статьи 74 КоАО, включая нарушения правил использования воды из искусственных "
    "водных объектов. В утверждении опущено это условие. Юристу следует сверить контекст "
    "публикации и редакцию нормы на дату заявления."
)
TRAINING_KEY = "training:water-352"
LEAVE_LAW_URL = "https://lex.uz/ru/acts/6257291?ONDATE=14.02.2025+00#6263237"
LEAVE_LAW_LABEL = "Трудовой кодекс Республики Узбекистан, статья 217 (ред. на 14.02.2025)"
LEAVE_LAW_EXCERPT = ("Продолжительность ежегодного основного минимального трудового отпуска "
                     "составляет двадцать один календарный день.")
LEAVE_QUOTE = ("Минимальная продолжительность ежегодного основного трудового отпуска "
               "в Узбекистане составляет 15 календарных дней.")
LEAVE_TOPIC = "Минимальный ежегодный отпуск"
TRAINING_SAMPLES = (
    ("Утверждение о выплате",
     "По постановлению № 352 за видео мойки машины питьевой водой из крана положено денежное вознаграждение."),
    ("Опровержение",
     "По постановлению № 352 за видео мойки машины питьевой водой из крана денежное вознаграждение не положено."),
    ("Другая тема", "В субботу в городском парке пройдёт открытая тренировка."),
    ("Продолжительность отпуска", LEAVE_QUOTE),
    ("Верный общий минимум", LEAVE_QUOTE.replace("15 календарных дней", "21 календарный день")),
)

# Match the entire affirmative caption. Quoted, qualified, questioned or rebutted
# claims need manual reading before attaching any excerpt to an actual author.
_DECREE = r"по постановлению (?:Кабинета Министров )?№\s*352"
_RECORDING = (r"за (?:видео|видеозапись) (?:мойки|мытья) (?:машины|автомобиля) "
              r"(?:питьевой водой из крана|водопроводной водой|водой из-под крана)")
_REWARD = r"(?:положено|полагается|выплатят) (?:денежное )?вознаграждение"
WATER_CLAIM = re.compile(
    rf"(?:{_DECREE} {_RECORDING} {_REWARD}|{_RECORDING} {_DECREE} {_REWARD}|"
    rf"{_DECREE} {_REWARD} {_RECORDING})[.!]?", re.IGNORECASE,
)
_DAYS = r"(?P<count>[0-9]{1,3}|[а-яё]+(?: [а-яё]+)?) календарн(?:ый день|ых дня|ых дней)"
LEAVE_CLAIM = re.compile(
    r"минимальная продолжительность ежегодного основного трудового отпуска в Узбекистане составляет "
    + _DAYS + r"[.!]?", re.IGNORECASE,
)
_DAY_WORDS = (
    "ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать "
    "тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать"
).split()
_NUMBER = {word: number for number, word in enumerate(_DAY_WORDS)}
_NUMBER.update(zip("двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто".split(),
                   range(20, 100, 10)))
TASHKENT = timezone(timedelta(hours=5))


class Norm(NamedTuple):
    url: str
    label: str
    excerpt: str
    effective_from: date


WATER = Norm(LAW_URL, LAW_LABEL, LAW_EXCERPT, date(2025, 6, 11))
LEAVE = Norm(LEAVE_LAW_URL, LEAVE_LAW_LABEL, LEAVE_LAW_EXCERPT, date(2023, 4, 30))
EMPTY_EVIDENCE = ("", "", "", "", "")

EDIT_FIELDS = {
    "quote": ("Цитата", 4000),
    "timecode": ("Таймкод", 16),
    "law_url": ("Ссылка на норму", 2048),
    "law_label": ("Реквизиты нормы", 400),
    "law_excerpt": ("Фрагмент нормы", 4000),
    "reason": ("Основание проверки", 4000),
    "assignee": ("Исполнитель", 160),
}


def initialize(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demo_cases (
            id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            mode TEXT NOT NULL CHECK (mode IN ('training', 'live')),
            platform TEXT NOT NULL DEFAULT 'instagram',
            url TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '', discovered_at TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL, topic TEXT NOT NULL,
            quote TEXT NOT NULL DEFAULT '', timecode TEXT NOT NULL DEFAULT '',
            law_url TEXT NOT NULL DEFAULT '', law_label TEXT NOT NULL DEFAULT '',
            law_excerpt TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'assigned', 'closed')),
            assignee TEXT NOT NULL DEFAULT ''
        )
    """)


def _card(cursor, row):
    card = dict(zip((col[0] for col in cursor.description), row))
    training = card.pop("source_key") == TRAINING_KEY
    card["media_url"] = "/demo-media/water.mp4" if training else ""
    card["source_url"] = EXPLANATION_URL if card["law_url"] == LAW_URL else ""
    return card


def list_cases(conn):
    cursor = conn.execute("SELECT * FROM demo_cases ORDER BY id DESC")
    return [_card(cursor, row) for row in cursor]


def get_case(conn, case_id):
    if isinstance(case_id, str) and re.fullmatch(r"[0-9]{1,19}", case_id):
        case_id = int(case_id)
    if type(case_id) is not int or not 1 <= case_id <= 2**63 - 1:
        raise ValueError("Некорректный номер карточки.")
    cursor = conn.execute("SELECT * FROM demo_cases WHERE id=?", (case_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Карточка не найдена.")
    return _card(cursor, row)


def _day_count(value):
    if value.isascii() and value.isdigit():
        return int(value)
    words = value.casefold().split()
    if len(words) == 1:
        return _NUMBER.get(words[0])
    tens, units = (_NUMBER.get(word, -1) for word in words)
    if tens >= 20 and 1 <= units <= 9:
        return tens + units
    return None


def _comparison(description):
    if len(description) > EDIT_FIELDS["quote"][1]:
        return None
    caption = " ".join(description.split())
    if WATER_CLAIM.fullmatch(caption):
        return WATER, COMPARISON
    claim = LEAVE_CLAIM.fullmatch(caption)
    if claim:
        source = re.search(r"составляет " + _DAYS + r"\.$", LEAVE.excerpt)
        claimed = _day_count(claim["count"])
        minimum = _day_count(source["count"]) if source else None
        if minimum is not None and claimed is not None and minimum != claimed:
            reason = (f"В публикации указан общий минимум в календарных днях — {claimed}. "
                      f"В статье 217 для того же вида отпуска указан минимум {minimum}. "
                      "Юристу следует сверить контекст и применимость общей нормы.")
            return LEAVE, reason
    return None


def replay(conn):
    """Run the same narrow comparisons over fictional captions with dated legal snapshots."""
    matched = added = 0
    case_id = None
    for _, caption in TRAINING_SAMPLES:
        comparison = _comparison(caption)
        if not comparison:
            continue
        norm, reason = comparison
        if norm == WATER:
            key, topic, timecode = TRAINING_KEY, "Водопользование", "00:03"
            description = "Учебная реконструкция. Норма на 11.06.2025. " + caption
        else:
            key, topic, timecode = "training:leave-217", LEAVE_TOPIC, ""
            description = "Учебная текстовая публикация. Дата правового сценария — 14.02.2025. " + caption
        matched += 1
        inserted = conn.execute("""
            INSERT INTO demo_cases (source_key, mode, topic, description, timecode,
                                    quote, law_url, law_label, law_excerpt, reason)
            VALUES (?, 'training', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO NOTHING
        """, (key, topic, description, timecode, caption, norm.url, norm.label, norm.excerpt, reason))
        added += inserted.rowcount
        if key == TRAINING_KEY:
            case_id, = conn.execute("SELECT id FROM demo_cases WHERE source_key=?", (key,)).fetchone()
    return dict(scanned=len(TRAINING_SAMPLES), matched=matched, added=added, case_id=case_id)


def _live_evidence(description, published_at, verify, cache):
    comparison = _comparison(description)
    if not comparison or not published_at or verify is None:
        return EMPTY_EVIDENCE
    norm, reason = comparison
    try:
        stamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return EMPTY_EVIDENCE
        local_day = stamp.astimezone(TASHKENT).date()
        if not norm.effective_from <= local_day <= datetime.now(TASHKENT).date():
            return EMPTY_EVIDENCE
    except (ValueError, OverflowError):
        return EMPTY_EVIDENCE

    day = local_day.strftime("%d.%m.%Y")
    if (norm, day) not in cache:
        link = urlsplit(norm.url)
        try:
            reference = verify(link._replace(query="").geturl(), norm.excerpt, day)
            resolved = reference["url"]
            _check_law_url(resolved)
            edition = urlsplit(resolved)
            version = dict(parse_qsl(edition.query)).get("ONDATE", "").split(" ")[0]
            if (not version or datetime.strptime(version, "%d.%m.%Y").date() > local_day
                    or edition.fragment != link.fragment or edition.path.split("/")[-1] != link.path.split("/")[-1]
                    or norm.excerpt not in " ".join(reference["passage"].split())):
                raise ValueError
            label = norm.label.partition(" (ред.")[0] + f" (ред. от {version}, на дату публикации {day})"
            cache[norm, day] = resolved, label
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            cache[norm, day] = None
    verified = cache[norm, day]
    if not verified:
        return EMPTY_EVIDENCE
    return (description, *verified, norm.excerpt, reason)


def add_live(conn, rows, topic, verify=None):
    """Verify eligible claims before writing. Keep unverified publications for human review."""
    prepared = []
    cache = {}
    seen = set()
    discovered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        url = row.get("url")
        source = public_post_url(url) if isinstance(url, str) else None
        if not source or source[0] != "instagram":
            continue
        shortcode = source[1].rstrip("/").rsplit("/", 1)[1]
        source_key = "instagram:" + shortcode
        if source_key in seen or conn.execute("SELECT 1 FROM demo_cases WHERE source_key=?", (source_key,)).fetchone():
            continue
        seen.add(source_key)
        description = (row.get("description") or "").strip()
        published_at = (row.get("published_at") or "")[:80]
        evidence = _live_evidence(description, published_at, verify, cache)
        prepared.append((source_key, source[1], (row.get("account") or "")[:160],
                         published_at, discovered_at, description[:16000], topic[:400], *evidence))
    return conn.executemany("""
        INSERT INTO demo_cases (source_key, mode, url, account, published_at, discovered_at,
                                description, topic, quote, law_url, law_label, law_excerpt, reason)
        VALUES (?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO NOTHING
    """, prepared).rowcount


def _check_law_url(value):
    message = "Укажите HTTPS-ссылку на документ Lex.uz, при необходимости с датой редакции и якорем пункта."
    try:
        if "\\" in value or any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError
        url = urlsplit(value)
        if (url.scheme != "https" or url.netloc.lower() not in {"lex.uz", "www.lex.uz"}
                or not re.fullmatch(r"/(?:(?:uz|ru|en)/)?(?:docs|acts|mact)/-?[1-9][0-9]*/?", url.path)
                or (url.fragment and not re.fullmatch(r"-?[0-9]+", url.fragment))):
            raise ValueError
        if url.query:
            ((key, date),) = parse_qsl(url.query, strict_parsing=True, keep_blank_values=True)
            if key.upper() != "ONDATE" or not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}(?: [0-9]{2})?", date):
                raise ValueError
            datetime.strptime(date[:10], "%d.%m.%Y")
    except ValueError:
        raise ValueError(message) from None


def review(conn, obj):
    """Save a partial review, requiring evidence and an assignee for internal handoff."""
    if not isinstance(obj, dict):
        raise ValueError("Нужна карточка проверки.")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    card = get_case(conn, obj.get("id"))
    case_id = card["id"]
    status = obj.get("status", card["status"])
    if status not in ("new", "assigned", "closed"):
        raise ValueError("Неизвестный статус проверки.")
    if card["status"] == "closed":
        if status == "closed":
            return card
        raise ValueError("Проверка уже закрыта.")
    if card["status"] == "assigned" and status == "new":
        raise ValueError("Карточка уже передана исполнителю. Обновите список.")

    for field, (label, limit) in EDIT_FIELDS.items():
        if field in obj:
            value = obj[field]
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"Поле «{label}» должно содержать текст до {limit} символов.")
            card[field] = value.strip()
    if card["timecode"] and not re.fullmatch(r"[0-9]{1,2}:(?:[0-5][0-9]:)?[0-5][0-9]", card["timecode"]):
        raise ValueError("Введите таймкод в формате ММ:СС или Ч:ММ:СС.")
    if card["law_url"]:
        _check_law_url(card["law_url"])
    if status == "assigned":
        missing = [label for field, (label, _) in EDIT_FIELDS.items() if field != "timecode" and not card[field]]
        if missing:
            raise ValueError("Для передачи юристу заполните поля: " + ", ".join(missing) + ".")
    conn.execute("""
        UPDATE demo_cases SET quote=?, timecode=?, law_url=?, law_label=?, law_excerpt=?,
                              reason=?, assignee=?, status=? WHERE id=?
    """, (*(card[field] for field in EDIT_FIELDS), status, case_id))
    return get_case(conn, case_id)
