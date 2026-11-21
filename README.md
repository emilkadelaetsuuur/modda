# Modda

Сверка публикаций с законодательством Узбекистана и короткие ролики о законах по нормам Lex.uz.
Демо: https://nasa.cartify.ink

В репозитории код в том виде, в каком он работает на сервере и ноутбуке на 21.11.2026.
Ключи, базы данных, модели и рабочие папки заданий сюда не входят (см. «Чего нет в репозитории»).

## Состав

| Папка | Что это | Где работает |
|---|---|---|
| `site/monitor` | Проверка видео (лента на «/»), мониторинг публикаций, живой поиск, проверка загруженного ролика. Python, без фреймворков. Тесты в `tests/` | Сервер, docker `nasa-lex-monitor-v6`, порт 18881, код смонтирован в `/srv` |
| `site/studio` | Видеостудия `/studio/`: заказ ролика 15/30/45 с, очередь, статус ноутбука-монтажёра, выдача MP4 | Сервер, docker `nasa-modda-studio`, порт 18883 |
| `site/lex-search` | Поиск по базе Lex.uz `/qidiruv/`, ролики и опровержения по найденным нормам (`lexuz/rolik.py`, `lexuz/refute.py`), unit-файлы systemd в `deploy/` | Сервер, `lex-search.service`, порт 18390 |
| `laptop/bridge` | Мост сайт → ноутбук: `worker.py` забирает заказы, `tools.mjs` (MCP-сервер) и `job_tools.py` ведут задание: сценарий, клипы, сборка. Команда `/modda` для сессии приёма заказов | Ноутбук, `%LOCALAPPDATA%\ModdaBridge` |
| `laptop/ai-studio` | MODDA AI Studio: сценарий, кадры и клипы, озвучка, субтитры по словам, звуки, музыка, монтаж. `examples/` — готовые проекты-образцы | Ноутбук, `%LOCALAPPDATA%\ModdaStudio\modda-ai-studio`, своё `.venv` |
| `deploy/nginx` | Конфигурация nginx сайта nasa.cartify.ink | Сервер |
| `brand` | Логотип: исходник и версии для светлого и тёмного фона (раздаются из `/brand/`) | Сервер, `/var/www/nasa.cartify.ink/brand` |
| `tools` | `mklogo.py` — обрезка и прозрачность логотипа; `sitepatch.py` — правки ребрендинга 21.11.2026 (логотип, без «//», без названий сторонних сервисов, лента по страницам, сбор от популярного) | Разово |
| `docs/business` | Финансовая модель и бизнес-часть v7 | — |

## Как связаны части

1. Посетитель заказывает ролик на `/studio/`. Сервер студии ставит задание в очередь.
2. `laptop/bridge/bridge/worker.py` опрашивает сервер и берёт задание, только если на ноутбуке запущен приём заказов (`/loop 2m /modda`, папка `ModdaBridge\desktop`).
3. Сессия приёма пишет сценарий по нормам Lex.uz (каждый факт — дословная цитата), делает кадры и клипы и прикрепляет их через `attach`.
4. `job_tools.py` проверяет длину (по слогам) и цитаты, AI Studio озвучивает и собирает MP4, `worker.py` загружает ролик на сайт.

Лента на «/» (`site/monitor/deepfake_watch.py`) раз в 3 часа ищет популярные ролики об Узбекистане в YouTube и Telegram: первыми берутся самые просматриваемые, меньше `LEX_MIN_VIEWS` (по умолчанию 1000) пропускается. На странице лента разбита по 10 записей.

## Тесты

```
cd site/monitor
python -m unittest discover -s tests
```

На сервере то же внутри образа: `docker run --rm -v <папка monitor>:/srv:ro -w /srv --network none --entrypoint python lex-forensics-runtime:2 -m unittest discover -s tests` (100 тестов).

## Чего нет в репозитории

- Ключи и токены: `.env` студии и AI Studio, `/opt/lex-monitor/modda/private/service.env`, `ModdaBridge/private/*.key`, `site/studio/law-search.private.json`. Образцы настроек: `laptop/ai-studio/.env.example`, `laptop/ai-studio/config.example.yaml`.
- Базы и данные: `studio.sqlite`, `monitor.sqlite`, `lex-search/data/lex.db` (≈200 МБ, собирается `lex.py`), модели детектора в `/opt/lex-monitor/models`.
- Рабочие папки: задания ноутбука (`ModdaBridge/jobs`), проекты и рендеры AI Studio, `node_modules`, `.venv`.
