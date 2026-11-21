"""Узбекский язык: три записи и единый ключ поиска.

На lex.uz узбекские тексты хранятся кириллицей. Пользователи пишут запросы:
  • кириллицей                — «меҳнат шартномаси»
  • латиницей (алфавит 1995)  — «mehnat shartnomasi», «oʻzbek», «o'zbek», «o‘zbek», «sogʻliq»
  • новым алфавитом (ЗРУ-1184 от 21.10.2026, вступает в силу 22.04.2027:
    28 букв и апостроф; oʻ→ö, gʻ→ğ, sh→ş, ch→ç) — «mehnat şartnomasi», «özbek», «soğliq»
Все три приводятся к одному ключу (строчные буквы нового алфавита без апострофов),
поэтому любой запрос находит одни и те же статьи. Текст показываем в той записи,
которую выбрал пользователь.
"""

from __future__ import annotations

import re
from functools import lru_cache

_CYR = re.compile(r"[а-яёўқғҳА-ЯЁЎҚҒҲ]")
_CYR_RUN = re.compile(r"[а-яёўқғҳА-ЯЁЎҚҒҲ]+")
_VOWELS = set("аеёиоуэюяў")
_APOS = "ʻʼ'‘’`´ʹ"
_APOS_RE = re.compile(f"[{_APOS}]")
_SUPERSCRIPTS = dict.fromkeys(map(ord, "⁰¹²³⁴⁵⁶⁷⁸⁹"), None)

# кириллица -> латиница 1995 года (официальная до 2027): ʻ — U+02BB, тутуқ белгиси ʼ — U+02BC
_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "x", "ш": "sh", "щ": "sh", "ъ": "ʼ", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ё": "yo",
    "ў": "oʻ", "қ": "q", "ғ": "gʻ", "ҳ": "h", "ч": "ch",
}
# новый алфавит 2026 года: одна буква вместо сочетаний
_NEW = {**_LAT, "ш": "ş", "щ": "ş", "ч": "ç", "ў": "ö", "ғ": "ğ"}
_TABLES = {"lat": _LAT, "new": _NEW}


def _translit_word(word: str, table: dict) -> str:
    out = []
    lower = word.lower()
    all_caps = len(word) > 1 and word.isupper()
    for i, ch in enumerate(lower):
        prev = lower[i - 1] if i else ""
        if ch == "е":
            piece = "ye" if (i == 0 or prev in _VOWELS or prev in "ъь") else "e"
        elif ch == "ҳ" and prev == "с" and table is _LAT:
            piece = "ʼh"                  # асҳоб -> asʼhob, чтобы не читалось как «sh»
        elif ch == "ц":
            piece = "ts" if (i > 0 and prev in _VOWELS) else "s"
        else:
            piece = table.get(ch, ch)
        if word[i].isupper() and piece:
            piece = piece.upper() if all_caps else piece[0].upper() + piece[1:]
        out.append(piece)
    return "".join(out)


@lru_cache(maxsize=262144)
def _translit_cached(word: str, script: str) -> str:
    # слова в законах постоянно повторяются — кэш ускоряет выдачу в несколько раз
    return _translit_word(word, _TABLES[script])


def translit(text: str, script: str) -> str:
    """Узбекская кириллица -> 'lat' (латиница 1995) или 'new' (алфавит 2026). 'cyr' — без изменений."""
    if script not in _TABLES or not text:
        return text
    return _CYR_RUN.sub(lambda m: _translit_cached(m.group(0), script), text)


@lru_cache(maxsize=262144)
def to_key(word: str) -> str:
    """Ключ поиска для одного слова в любой записи."""
    w = word.lower().translate(_SUPERSCRIPTS)
    if _CYR.search(w):
        w = _CYR_RUN.sub(lambda m: _translit_cached(m.group(0), "new"), w)
    else:
        w = re.sub(f"([og])[{_APOS}]", lambda m: "ö" if m.group(1) == "o" else "ğ", w)
        w = w.replace("ō", "ö").replace("ḡ", "ğ").replace("ǵ", "ğ").replace("õ", "ö")
        w = w.replace("sh", "ş").replace("ch", "ç")   # «s'h» с апострофом остаётся s+h
    return _APOS_RE.sub("", w)


WORD = re.compile(r"[^\W_]+(?:[ʻʼ'‘’`][^\W_]+)*")


def key_text(text: str) -> str:
    """Весь текст -> ключи слов (для индекса). Пунктуация и цифры остаются на месте."""
    return WORD.sub(lambda m: to_key(m.group(0)), text)


# --- упрощённая морфология: отрезаем падеж, принадлежность и множественное число ---------------
_CASE = ["gaça", "dagi", "ning", "dan", "tan", "dek", "day", "da", "ta", "ga", "ka", "qa", "ni"]
_POSS = ["imiz", "ingiz", "lari", "si", "im", "ing", "i"]
_PLUR = ["lar"]
_MIN_STEM = 3

STOPWORDS = {
    "va", "bilan", "uçun", "ham", "yoki", "bu", "u", "bir", "deb", "esa", "lekin", "agar", "kabi",
    "haqida", "töğrisida", "qanday", "nima", "qaysi", "qança", "uning", "ular",
}


def _strip(w: str, suffixes: list[str]) -> str:
    for s in suffixes:
        if w.endswith(s) and len(w) - len(s) >= _MIN_STEM:
            return w[: -len(s)]
    return w


def stem(key: str) -> str:
    """«fuqarolarning» -> fuqaro, «şartnomasi» -> şartnoma, «vazirligi» -> vazirli (k/g чередуются)."""
    if len(key) <= 4 or key.isdigit():
        return key
    w = _strip(key, _CASE)
    w2 = _strip(w, _POSS)
    vowel_suffix = w2 != w and (w[len(w2):][:1] in "aeiou")
    w = _strip(w2, _PLUR)
    if vowel_suffix and w == w2 and w[-1:] in ("g", "ğ") and len(w) > 4:
        w = w[:-1]                       # vazirlik+i -> vazirligi: ищем по «vazirli*»
    return w
