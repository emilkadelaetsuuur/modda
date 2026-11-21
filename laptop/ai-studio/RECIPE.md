# RECIPE: как сделаны наши ролики (повторять точь-в-точь)

Здесь три рецепта, ровно так, как мы делали ролики на хакатоне. Каждый шаг: что делать и какой промпт брать.
Перед стартом: `python -m studio doctor` (должно быть OK у ffmpeg, LLM, VoiceLab).

---

## Рецепт 1. Инфографика 16:9 (самый надёжный, без Flow)

Результат как `renders/videos/02-07_Infografika_*.mp4`.

**Шаг 1.** Одна команда:
```bash
python -m studio make projects/osago --law https://lex.uz/docs/XXXX --kind infographic --lang uz --theme official
```
Что происходит внутри:
1. скачивается закон с lex.uz;
2. LLM пишет сцены по промпту `prompts/info_system.md` (образец: `examples/pirotexnika_info/scenario.json`);
3. формат сцен проверяется, при ошибке LLM переписывает один раз;
4. цитаты фактов ищутся в законе, числа сверяются (ошибка = стоп);
5. VoiceLab озвучивает реплики, рендер собирает MP4.

**Шаг 2.** Проверить вёрстку за секунды: `python -m studio infographic projects/osago --stills` (лист кадров в `out/`).

**Шаг 3.** Если что-то не нравится: поправить тексты в `projects/osago/scenario.json` и запустить шаг 1 ещё раз (сценарий не перепишется, пока не добавить `--rewrite`).

Темы: official, dark_business, magazine, newspaper, minimal, editorial. Все сразу: `python -m studio infographic projects/osago --all-themes`.

---

## Рецепт 2. Пост-карусель + текст поста

```bash
python -m studio make projects/osago --law https://lex.uz/docs/XXXX --kind post --lang uz --theme official
```
Промпт LLM: `prompts/post_system.md`, образец: `examples/pirotexnika_post/post.json` и `post_uz.json`.
Результат: `out/<id>_<язык>_<тема>/01.png ... 06.png`, `caption.txt`, `preview.jpg`.

---

## Рецепт 3. Мультфильм с персонажем (как ролик про петарды)

Результат как `renders/videos/01_Multfilm_Pirotexnika_RU.mp4`. Это ручной рецепт: кадры и видео делаются в Google Flow, лучшие дубли выбирает человек.

**Шаг 1. Сценарий.**
```bash
python -m studio make projects/osago --law https://lex.uz/docs/XXXX --kind cartoon --lang ru --audience teens --style bright
```
LLM пишет сценарий по `prompts/script_system.md` (там же образец утверждённых клипов). Правила, которые дали хороший результат:
- гибрид: рассказчик спокойно даёт факты, Аброр эмоционально реагирует;
- хук в первые 2 секунды, 9-12 реплик по 2-5 секунд;
- субтитры кусками по 1-4 слова: жёлтый = факт, красный = запрет, зелёный = можно;
- 5-6 клипов по 8 секунд, каждый клип используется в 1-2 шотах, разные ракурсы подряд.

Команда остановится и скажет, каких кадров нет. Это нормально: дальше Flow.

Сравните свой `scenario.json` с образцом `examples/pirotexnika_ru/scenario.json`. Если сцены вялые (нет конкретной локации, одного ясного действия), поправьте поля `scene` и `action` руками.

**Шаг 2. Кадры и видео во Flow.** Откройте `projects/osago/tasks/flow_playbook.md` и делайте строго по нему (или дайте агенту `prompts/flow_agent.md` + этот файл).

Пример, как выглядят промпты одного клипа (c2, Аброр кричит), это ровно то, что мы вставляли во Flow:

Кадр (Nano Banana, 9:16, ингредиенты: REF abror 1, REF abror 2, REF style bright):
```
Vibrant high-quality 2D cartoon illustration, bold clean black outlines, crisp sharp details, bright saturated colors (electric blue, hot orange, magenta, lime green), cel shading with glossy highlights, high contrast, dynamic cinematic composition, premium animated explainer look. Stickman-style characters: big round white heads, small black dot eyes, expressive eyebrows, thin black stick limbs, white cartoon gloves, colorful clothes. Extreme close-up of the character's face filling the frame, slight dutch tilt, eyes wide, eyebrows very expressive, mouth open mid-shout, bokeh lights in the background. Abror's face fills the frame at night, fireworks bokeh behind him, he is outraged. Abror: adult stickman man with a big round white head, small black dot eyes, very expressive thick eyebrows, a short neat black beard, wearing a black Uzbek Chust doppi: a square four-sided black skullcap with exactly four large white pepper-shaped (chili pod) motifs, one on each side, and a thin white zigzag band at the bottom; navy blue jacket over a light blue shirt, white cartoon gloves, thin black stick legs, dark shoes. Vertical 9:16 composition, the subject fills the frame, safe empty area in the lower third for subtitles. No text, no letters, no numbers, no logos, no watermark.
```

Видео (Кадры в видео, Veo 3.1 Lite, 9:16, 8 с, без звука, первый кадр = кадр c2):
```
Quick snap zoom in on the face. Abror shouts angrily and points straight at the camera. Eyebrows jump, slight camera bounce on every accent, background bokeh drifts. Keep the exact same vibrant bold-outline 2D cartoon style, same characters, same colors, crisp lines, no style change. Vertical 9:16. No dialogue, no voices, no singing, no narration, no text, no letters, no subtitles, no logos.
```

Как должен выглядеть результат: `examples/pirotexnika_ru/frames/` (кадры) и `examples/pirotexnika_ru/clips/` (видео).

Что мы делали руками и что важно повторить:
- Аброр одинаковый во всех кадрах: всегда прикладывать REF abror 1 и 2;
- в кадрах нет текста (подписи накладывает программа);
- звук клипов не нужен: Veo добавляет английскую речь, программа его всё равно глушит;
- если Flow отказал (у нас отказал кадр с ударом рукой по столу), берём картинку и оживляем: `"visual": {"image": "c3", "fx": "slam"}`.

**Шаг 3. Голос.**
```bash
python -m studio voice projects/osago
```
Мы озвучивали два разных голоса: спокойный рассказчик и эмоциональный Аброр. В VoiceLab для этого нужны два своих голоса (клоны), их `voice_id` вписать в `config.yaml` → `tts.voice_ids` (`narrator_ru`, `abror_ru`, `abror`). Без этого все реплики прочитает один встроенный узбекский голос Gulnoza.

**Шаг 4. Сборка.**
```bash
python -m studio build projects/osago
```
Склейки ставятся точно на начало реплик, субтитры по словам (whisper), музыка приглушается под голос, звуки на склейках, -14 LUFS. Если какого-то клипа нет, сборка остановится. Черновик с заглушками: `build --draft`.

**Шаг 5. Проверка.** Открыть `out/<id>_sheet.jpg` (кадр из каждого шота) и посмотреть ролик целиком.
