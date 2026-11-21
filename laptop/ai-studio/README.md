# MODDA AI Studio

Программа делает ролики о законах Узбекистана. Источник фактов: официальные тексты на lex.uz. Программа пишет сценарий с проверкой фактов, генерирует кадры и анимацию, озвучивает через VoiceLab, накладывает субтитры, музыку и звуки и собирает готовый MP4.

Три вида роликов:

| Вид | Как делается | Формат | Пример |
|---|---|---|---|
| Мультфильм с персонажем | нейросеть рисует кадры и анимирует их | 9:16 или 16:9 | `examples/pirotexnika_ru`, `examples/pirotexnika_uz` |
| Моушн-ролик (5 стилей) | тот же конвейер, но в стиле моушн-дизайна | 9:16 или 16:9 | `assets/refs/motion/` |
| Инфографика (6 тем) | рендер кодом, без нейросети, текст и цифры точные | 16:9 | `examples/pirotexnika_info` (RU), `examples/pirotexnika_info_uz` (UZ) |
| Посты-карусели + текст поста | карточки кодом в тех же 6 темах, текст поста с хэштегами | 4:5, 1:1, 16:9, 9:16 | `examples/pirotexnika_post` (RU и UZ) |

Всё работает на русском и узбекском (латиница). Для интеграции в бот или сайт есть HTTP API (раздел 15).

## Быстрый старт: одна команда

```bash
python -m studio doctor                                                                          # всё ли настроено
python -m studio make projects/x --law https://lex.uz/docs/XXXX --kind infographic --lang uz      # инфографика 16:9
python -m studio make projects/x --law https://lex.uz/docs/XXXX --kind post --lang uz             # пост-карусель + текст
python -m studio make projects/x --law https://lex.uz/docs/XXXX --kind cartoon --lang ru          # мультфильм (кадры во Flow)
```

Как именно сделаны наши ролики, по шагам и с промптами: **RECIPE.md**. Через API: `POST /api/make` с теми же параметрами (раздел 15).

Если чего-то не хватает (ключа, кадров, фактов в законе), программа останавливается и пишет, что сделать. Заглушек в готовом ролике не будет.

```
lex.uz ──> сценарий (LLM + фактчек) ──> кадры (Nano Banana) ──> клипы (Veo / Flow / ffmpeg)
                                   └──> озвучка (VoiceLab) ──> тайминг слов (whisper) ──> сборка (ffmpeg) ──> MP4
```

---

## 1. Установка

Нужны Python 3.10+ и ffmpeg.

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env          # впишите ключи
```

Ключи в `.env`:

| Ключ | Для чего | Обязателен |
|---|---|---|
| `VOICELAB_API_KEY` | озвучка | да |
| `ANTHROPIC_API_KEY` | сценарий и фактчек | да, если LLM = Claude |
| `GEMINI_API_KEY` | кадры и видео по API | нет (можно делать во Flow) |

Проверка: `python -m studio templates` покажет все шаблоны.

---

## 2. Быстрая проверка на готовом примере

Пример про пиротехнику уже содержит клипы и запись голоса, ключи не нужны:

```bash
python -m studio check examples/pirotexnika_ru     # фактчек: цитаты найдены в законе
python -m studio voice examples/pirotexnika_ru     # нарезка голоса по шотам
python -m studio build examples/pirotexnika_ru     # сборка, ~1 минута
```

Готовый ролик: `examples/pirotexnika_ru/out/`. Там же `*_sheet.jpg` (кадр из каждого шота) и `timeline.json`.

Узбекская версия с озвучкой VoiceLab:

```bash
python -m studio voice examples/pirotexnika_uz     # нужен VOICELAB_API_KEY
python -m studio build examples/pirotexnika_uz
```

Без ключа можно проверить монтаж на "заглушке" голоса: `python -m studio voice examples/pirotexnika_uz --provider mock`.

---

## 3. Новый ролик по любому закону

### Шаг 1. Создать проект и скачать закон

```bash
python -m studio init projects/osago --law https://lex.uz/ru/docs/XXXXXXX --lang ru --audience teens --format explainer
```

- `--lang`: `ru`, `uz`
- `--audience`: `kids` (7-11), `teens` (12-17), `adults`, `elderly` (55+)
- `--format`: `explainer`, `refute`, `myth_fact`, `quick_rule`, `dialog_scene`, `quiz`
- `--topic "..."`: на чём сделать акцент (необязательно)

Текст закона сохранится в `law.txt`. Если страница не скачалась: `python -m studio law projects/osago --file закон.txt`.

### Шаг 2. Сценарий

```bash
python -m studio script projects/osago
```

LLM пишет `scenario.json`, затем сразу идут две проверки:
1. каждая цитата из `facts` ищется в тексте закона (не нашлась = ошибка);
2. каждое число в репликах и субтитрах должно быть в законе или в `facts`;
3. второй LLM работает как юрист-фактчекер и ищет искажения.

Ошибки печатаются с пометкой `XXX`. Исправьте `scenario.json` руками или запустите `script` ещё раз. Повторная проверка: `python -m studio check projects/osago --llm`.

Промпт, который ушёл в LLM, лежит в `tasks/script_prompt_system.md`: его можно скопировать в чат с Claude и сделать сценарий вручную.

### Шаг 3. Кадры и клипы

Три режима, выбираются в `config.yaml` (`image.mode`, `video.mode`) или флагом `--mode`.

**Вариант А: Google Flow (без API, как делали пиротехнику)**

```bash
python -m studio prompts projects/osago
```

Откройте `projects/osago/tasks/flow_playbook.md`: пошаговая инструкция с промптами, настройками и проверками для каждого клипа. Короткий список промптов: `flow_tasks.md`, для автоматизации: `flow_tasks.json`. Коротко во Flow:
1. Nano Banana, 9:16, вставить FRAME PROMPT, скачать как `frames/c1.png`.
2. «Кадры в видео», Veo 3.1, 9:16, 8 с, 720p, без звука, первый кадр из шага 1, вставить VIDEO PROMPT, скачать как `clips/c1.mp4`.

Чтобы это сделал Claude in Chrome сам, дайте ему текст из `prompts/flow_agent.md` (там же подсказки по интерфейсу Flow).

**Вариант Б: по API**

```bash
python -m studio frames projects/osago --mode api     # Nano Banana через Gemini API
python -m studio clips  projects/osago --mode api     # Veo через Gemini API или Vertex
```

Модели задаются в `config.yaml`. Preview-версии Veo 3.1 в Gemini API отключены 22.10.2026, поэтому id модели проверяйте в документации Google и меняйте в конфиге.

**Вариант В: без нейросети для видео**

```bash
python -m studio clips projects/osago --mode still
```

Кадр оживляется ffmpeg-ом: наезд, панорама, тряска по типу движения из `motions.yaml`. Подходит для черновика или если закончились бонусы. Если клипа нет, а кадр есть, сборка сделает так же автоматически.

### Шаг 4. Озвучка

```bash
python -m studio voice projects/osago
```

Каждая реплика озвучивается отдельным файлом `voice/s01.wav`, тишина по краям обрезается. Повторный запуск пропускает реплики, которые не менялись (кэш по тексту и настройкам). Перегенерировать одну: `--only s03 --force`.

### Шаг 5. Сборка

```bash
python -m studio build projects/osago
```

Или всё одной командой: `python -m studio all projects/osago`.

---

## 4. Опровержение фейка

```bash
# только вердикт по посту
python -m studio verify projects/fake1 --law https://lex.uz/ru/docs/XXXX --post "С 1 января штраф за петарды 50 млн!"

# вердикт + сценарий-опровержение (+ --build сразу собрать)
python -m studio refute projects/fake1 --law https://lex.uz/ru/docs/XXXX --fake "С 1 января штраф за петарды 50 млн!" --lang uz --audience adults
```

`verdict.json`: вердикт (`true`, `misleading`, `fake`, `unverifiable`), цитаты из закона и короткий текст на RU и UZ для карточки в боте. Если пост правдивый, ролик-опровержение не делается.

---

## 5. VoiceLab: как устроена озвучка

API: `POST https://back.aisha.group/api/v1/tts/post/`, заголовок `X-Api-Key`, поля `transcript`, `language`, `model`, `mood`, `speed`, `voice_id`. Ответ: JSON с `audio_path` (WAV), программа скачивает файл сама. Если сервис ответил «в очереди», программа ждёт статус.

Что важно знать:
- **Эмоции (`mood`) работают только у встроенного узбекского голоса Gulnoza**: `Neutral`, `Cheerful`, `Happy`, `Sad`. Скорость (`speed`) тоже только для узбекского.
- **Для русского** и для мужского голоса Аброра нужен свой голос: создайте его в кабинете VoiceLab (клон) и впишите `voice_id` в `config.yaml`:
  ```yaml
  tts:
    voice_ids:
      abror: 123
      narrator_ru: 456
  ```
- Лимит 1000 символов на запрос (реплики у нас по 4-12 слов, хватает).
- Эмоции из сценария переводятся в параметры VoiceLab в `prompts/voices.yaml`. Если `mood` недоступен, эмоцию передаёт текст: восклицательный знак, вопрос, пауза.
- Своя запись вместо TTS: в шоте укажите `"audio": {"file": "voice_src/my.mp3", "from": 0.0, "to": 2.7}`.

---

## 6. Шаблоны (папка `prompts/`)

| Файл | Что внутри | Как расширить |
|---|---|---|
| `styles.yaml` | 11 стилей генерации: `bright` (утверждённый), `stickman`, `cartoon3d`, `kids_soft`, `elder_clear`, `doc_infographic` и 5 моушн-стилей (раздел 12) | добавьте ключ с `prompt`, `lock`, `negative` |
| `info_themes.yaml` | 6 тем инфографики (раздел 11) | скопируйте тему и поменяйте цвета |
| `characters.yaml` | Аброр, Малика, Сардор, Бобо, Нодира (юрист), инспектор, массовка | `sheet` вставляется в каждый кадр дословно, так персонаж не меняется |
| `shots.yaml` | 18 ракурсов: хук снизу, крупный план с наклоном, вид сверху, через плечо, дрон, облёт группы, POV телефона, фейк/правда сплит и др. | `camera` + подходящее `motion` |
| `motions.yaml` | 15 движений камеры для Veo + как имитировать их в ffmpeg (`still`) | `prompt` с `{action}` |
| `audiences.yaml` | правила текста, темп, размер субтитров, музыка под каждый возраст | |
| `formats.yaml` | скелеты роликов: познавательный, опровержение, мифы, правило за 15 с, сценка, тест | |
| `voices.yaml` | голоса персонажей и эмоции для VoiceLab | |
| `script_system.md` | главный промпт сценариста (правила, схема JSON) | |
| `factcheck_system.md`, `verdict_system.md` | промпты юриста-фактчекера | |
| `flow_agent.md` | промпт для Claude in Chrome, чтобы он сам генерировал во Flow | |

Референсы стиля и Аброра: `assets/refs/`. Звуки: `assets/sfx/` (boom, whoosh, fireworks_show из ролика про петарды, остальные синтезируются командой `python -m studio sfx`).

---

## 7. Формат scenario.json (коротко)

```json
{
  "id": "pirotexnika_ru", "lang": "ru", "audience": "teens", "style": "bright",
  "source": {"url": "https://lex.uz/...", "plate": ["ИСТОЧНИК: LEX.UZ", "Закон ЗРУ-1111", "ст. 185¹ КоАО"]},
  "facts": [{"id": "f4", "claim": "Штраф 15-25 БРВ", "quote": "дословно из закона", "article": "ст. 185¹"}],
  "clips": {"c1": {"shot": "hook_extreme_low", "characters": ["crowd"], "scene": "...", "action": "...", "motion": "handheld_blast"}},
  "shots": [
    {"id": "s01", "speaker": "narrator", "emotion": "serious", "text": "Одна петарда, и у соседей горит балкон!",
     "captions": [["Одна петарда,", "y"], ["и у соседей", "w"], ["горит балкон!", "r"]],
     "visual": {"clip": "c1", "offset": 0.0}, "sfx": ["boom"], "facts": []}
  ]
}
```

- Цвета субтитров: `y` жёлтый (факт, цифра), `r` красный (запрет, штраф, фейк), `g` зелёный (можно, правильно), `w` белый.
- `visual`: `{"clip": "c1", "offset": 4.0}` кусок видео с 4-й секунды; `{"image": "c3", "fx": "slam"}` кадр с ударом и тряской; `{"file": "my.mp4"}` своё видео.
- Звуки: `boom`, `fireworks_show`, `ding`, `buzz`, `stamp`, `pop`, `tick`, `cash`, `sparkle`. `whoosh` ставится на каждую склейку сам.
- Необязательно: `timing` (lead, gap, outro), `music` (`preset`: upbeat, tense, calm, kids или `file`), `plate_at`, `clips_dir`, `frames_dir`.

## 8. Правила монтажа, которые зашиты в сборку

- Одна реплика = один шот. Склейка стоит ровно на начале реплики, поэтому голос не отстаёт от картинки.
- 0.35 с перед первой репликой (там удар), 0.08 с между репликами, 1.6 с хвоста с плашкой источника.
- Каждый шот: лёгкий наезд камеры, +12% насыщенности; если клип короче шота, он чуть замедляется, затем держится последний кадр.
- Субтитры по кускам 1-4 слова, «выпрыгивают» (2 кадра крупнее), время каждого куска по словам из whisper.
- Звук клипов Veo выключен (модель любит добавлять английскую речь).
- Музыка синтезируется (без проблем с авторскими правами), приглушается под голос, итоговая громкость -14 LUFS.

## 9. Локальная LLM

```yaml
llm:
  provider: openai_compat
  base_url: http://localhost:11434/v1     # Ollama, vLLM, LM Studio
  model: qwen3-32b-instruct
```

Все промпты и проверки работают так же. Для узбекского текста качество локальной модели проверьте на 2-3 законах до демо.

## 10. Если что-то не так

| Проблема | Что делать |
|---|---|
| `Нет ключа: задайте переменную окружения ...` | впишите ключ в `.env` |
| VoiceLab 402 | закончился баланс |
| VoiceLab 401 | ключ не подходит к `voice_id` |
| Veo / Flow отказ по политике | упростить сцену (без ударов рукой, огня в лицо) или взять `"visual": {"image": ..., "fx": "slam"}` |
| В ролике заглушка «нет клипа» | нет `clips/<id>.mp4` и `frames/<id>.png` |
| Субтитры сдвинуты | `pip install faster-whisper` или задайте время кусков вручную: `["текст", "y", 0.0, 1.0]` |
| lex.uz не скачался | `law --file закон.txt` |

---

## 11. Инфографика (16:9, без нейросети)

Самый строгий формат для взрослых и госзаказчика. Кадры рисуются кодом, поэтому текст, цифры и номера статей всегда точные. Сцены анимированы: тексты выезжают, сумма «накручивается», старый штраф зачёркивается, шкала времени рисуется, столбики растут.

```bash
python -m studio infographic examples/pirotexnika_info --theme official            # ролик в одной теме
python -m studio infographic examples/pirotexnika_info --all-themes                 # во всех 6 темах
python -m studio infographic examples/pirotexnika_info --theme minimal --stills     # только лист кадров, за секунды
```

Готовые файлы: `out/<id>_<тема>_16x9.mp4`. Сборка ролика 30 секунд занимает около 20 секунд.

**Темы** (`prompts/info_themes.yaml`, примеры кадров в `assets/refs/infographic/`):

| Тема | Как выглядит | Кому |
|---|---|---|
| `official` | белый, тёмно-синяя шапка, золото, заголовки с засечками | госорганы, официальные каналы |
| `dark_business` | почти чёрный фон, моноширинные цифры, оранжевый акцент | деловые и финансовые темы |
| `magazine` | кремовый фон, красная полоса, засечки | соцсети, широкая аудитория |
| `newspaper` | бумага, двойная линейка, колонки | пожилая аудитория, СМИ |
| `minimal` | много воздуха, тонкие цифры, синий акцент | презентации, молодая аудитория |
| `editorial` | светлая бумага, красный и зелёный | универсальная |

Новая тема: скопируйте блок в `info_themes.yaml` и поменяйте цвета и шрифты (`head`: sans, serif; `num`: sans, serif, mono, sans_thin; `header`: line, bar, double, none).

**Сценарий инфографики.** В `scenario.json` поле `"kind": "infographic"`, у каждого шота есть `scene`. Реплика `text` озвучивается как обычно (VoiceLab или готовый файл `audio`). Шот без `text` становится сценой без голоса длиной `dur` секунд.

| `scene.type` | Что показывает | Поля |
|---|---|---|
| `title` | крупный заголовок + иконка | `label`, `lines`, `accent` (номера строк цветом), `icon` |
| `counter` | число накручивается от нуля | `label`, `value`, `unit`, `note` |
| `timeline` | шкала времени с датами | `label`, `title`, `subtitle`, `icon`, `points[{date, text}]` |
| `bars` | столбики, подписи групп | `label`, `title`, `subtitle`, `bars[{label, value, kind}]`, `groups[{from, to, text, kind}]`, `note`, `note_accent` |
| `compare` | было (зачёркивается) / стало | `label`, `title`, `before{caption, value}`, `after{caption, value}`, `sub`, `tag` |
| `columns` | можно / нельзя с иконками | `title`, `left{title, kind, sub, items[{icon, text}]}`, `right{...}` |
| `statement` | главная мысль с маркером | `label`, `lead[]`, `big[]`, `icon` |
| `outro` | финал и бренд | `lines[]`, `sub` |

`kind`: `pos` (зелёный, можно), `neg` (красный, запрещено), `accent`, `muted`. Иконки (22): `firecracker`, `rocket`, `battery`, `sparkler`, `fountain`, `doc`, `check`, `cross`, `shield`, `phone`, `car`, `traffic_light`, `money`, `house`, `calendar`, `gavel`, `people`, `school`, `shop`, `card`, `warning`, `clock`. Звуки на сцене: `"sfx": [{"name": "stamp", "at": 1.0}]`.

Чтобы LLM написал такой сценарий, добавьте в запрос к `script` фразу: «формат инфографики, используй типы сцен из README раздел 11». Фактчек (`check`) работает так же, как для обычных роликов.

---

## 12. Моушн-стили

Пять стилей профессионального моушн-дизайна для обычного конвейера (кадры + анимация). Примеры кадров: `assets/refs/motion/`, промпты: `prompts/styles.yaml`.

| `style` | Что это | Когда брать |
|---|---|---|
| `flat_motion` | плоская иллюстрация, простые формы, приглушённые цвета | объяснение ситуации, бытовые сцены |
| `isometric` | изометрическая схема: дом, машина, документ на одной площадке | кто участвует и что будет, процессы |
| `kinetic_type` | крупные слова и цветные плашки | короткие ролики-правила, хуки (текст лучше делать через инфографику) |
| `line_art` | тонкая чёрная линия на белом, красный и зелёный акценты | опровержения фейков: «ложь / правда» |
| `paper_cut` | слои бумаги с мягкими тенями, тёплые цвета | детские и семейные ролики |

В сценарии: `"style": "line_art"`. Референс стиля подставляется в генерацию кадров автоматически.

---

## 13. Горизонтальные ролики (16:9)

Добавьте в `scenario.json` поле `"aspect": "16:9"`. Тогда кадры и видео генерируются горизонтальными (промпты меняются сами), сборка идёт в 1920×1080, субтитры опускаются в нижнюю часть кадра, плашка источника встаёт ниже. Без поля ролик вертикальный 9:16.

---

## 14. Посты для соцсетей (карусель + текст)

Карточки для Telegram, Instagram, Facebook рисуются кодом в тех же 6 темах, что и инфографика. Рядом сохраняется текст поста `caption.txt` с хэштегами.

```bash
python -m studio post examples/pirotexnika_post --lang ru --theme official          # готовый post.json
python -m studio post examples/pirotexnika_post --lang uz --theme magazine          # узбекская версия (post_uz.json)
python -m studio post projects/osago --lang uz --law https://lex.uz/docs/XXXX       # LLM пишет пост по закону сам
python -m studio post examples/pirotexnika_post --all-themes --format 1x1           # все темы, квадрат
```

Результат: `out/<id>_<язык>_<тема>/01.png ... 06.png`, `caption.txt`, `preview.jpg` (все карточки в ряд).

Форматы: `4x5` (1080×1350, Instagram, по умолчанию), `1x1` (1080×1080), `16x9` (1600×900, Telegram), `9x16` (сторис).

Типы слайдов в `post.json`:

| `type` | Поля |
|---|---|
| `cover` | `label`, `lines[]`, `accent[]`, `sub`, `icon` (или картинка из генератора: `"cover_image": "frames/cover.png"` в корне post.json) |
| `number` | `label`, `value`, `unit`, `note` |
| `compare` | `label`, `before{caption, value}`, `after{caption, value}`, `sub`, `tag` |
| `list` | `label`, `title`, `items[{text, kind, note, icon}]` |
| `quote` | `label`, `quote` (дословно из закона), `article` |
| `outro` | `lines[]`, `sub` |

Перед рендером факты поста проверяются так же, как в роликах: цитаты из `facts` ищутся в законе, числа сверяются. Промпт для LLM: `prompts/post_system.md`. Картинку на обложку можно сделать генератором кадров (`frames --mode api` или Flow) в любом стиле из `styles.yaml`.

---

## 15. Интеграция: API для бота и сайта

**Как библиотека Python:**

```python
from studio.api import Studio
st = Studio()
st.verify("projects/x", "https://lex.uz/docs/7987564", "текст поста")            # вердикт: true / misleading / fake
st.post("projects/x", "https://lex.uz/docs/7987564", lang="uz", theme="official")  # карточки + caption
st.video("projects/x", "https://lex.uz/docs/7987564", lang="uz", audience="teens") # мультфильм / моушн
st.infographic("projects/x", theme="official")                                   # инфографика 16:9
st.refute("projects/x", "https://lex.uz/docs/7987564", "текст фейка", lang="uz")  # вердикт + пост + ролик
```

**Как HTTP-сервис:**

```bash
python -m studio serve --port 8080 --root projects
```

| Метод | Тело запроса (JSON) | Ответ |
|---|---|---|
| `POST /api/make` | `project`, `law_url`, `kind` (infographic, post, cartoon), `lang`, `theme`, `style`, `aspect` | `{"job": id}` |
| `POST /api/verify` | `project`, `law_url`, `text` | вердикт сразу |
| `POST /api/post` | `project`, `law_url`, `lang`, `theme`, `fmt`, `topic` | `{"job": id}` |
| `POST /api/video` | `project`, `law_url`, `lang`, `audience`, `format`, `style`, `aspect` | `{"job": id}` |
| `POST /api/infographic` | `project`, `theme` | `{"job": id}` |
| `POST /api/refute` | `project`, `law_url`, `fake`, `lang` | `{"job": id}` |
| `GET /api/jobs/<id>` | | `queued`, `running`, `done` (с путями и ссылками `*_url` на файлы) или `error` |
| `GET /files/<проект>/out/...` | | скачать картинку, видео или caption.txt |

Пример:

```bash
curl -X POST localhost:8080/api/post -d '{"project":"demo","lang":"uz","theme":"magazine"}'
# {"job": "888bcdcc71", "status_url": "/api/jobs/888bcdcc71"}
curl localhost:8080/api/jobs/888bcdcc71
# {"status": "done", "result": {"slides": [...], "slides_urls": ["/files/demo/out/.../01.png", ...], "caption": "..."}}
```

Сервер без внешних зависимостей (стандартная библиотека Python). Для продакшена поставьте его за nginx и закройте доступ ключом.
