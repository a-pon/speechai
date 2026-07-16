# SpeechAI — контекст для агента

## Проект

Анализ аудиозаписей первичных стоматологических консультаций: транскрипция + оценка врача по 6 этапам (шкала 1–5) + веб-просмотр.

**Заказчик / домен:** премиальная стоматологическая клиника (A3 Beauté).

## Текущая фаза: MVP 1.1

- Ручная загрузка MP3 через сайт (блок на главной — только отладка)
- Простая админка с управлением пользователями
- Вход врачей по ссылке из 1С
- Навигация: список `/` → карточка `/record/{id}` с кнопкой «← назад»
- Результат: диалог (врач/пациент, таймкоды) + структурированный отчёт об оценке
- Дизайн UI — максимально простой, без полировки
- Режим `MOCK_AI=true` для демо без Yandex API

## Стек

| Компонент | MVP | Позже |
|-----------|-----|-------|
| Язык | Python 3.12+ | — |
| API | FastAPI, Uvicorn | — |
| БД | SQLite | PostgreSQL |
| Файлы | `data/audio/` локально | S3 / MinIO |
| Фоновые задачи | `BackgroundTasks` | Celery + Redis |
| STT | Yandex SpeechKit v3 async | — |
| LLM | YandexGPT | — |
| Фронт | MPA: `index.html` + `consultation.html` + JS | React SPA, поиск/фильтры |
| Ingestion | Upload only | Poll REST API |

## Структура репозитория

```
speechAI/
├── AGENTS.md              # этот файл
├── docs/PLAN.md           # план (обновляется)
├── config/
│   └── evaluation_prompt.txt
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   ├── models/
│   │   ├── services/
│   │   └── static/        # MVP UI
│   └── requirements.txt
├── data/                  # sqlite + audio (gitignore)
└── .env.example
```

## Оценка качества

6 этапов, баллы 1–5, общий балл — среднее арифметическое. Полный промпт: `config/evaluation_prompt.txt`.

Этапы: контакт → анамнез → диагностика → презентация лечения → возражения → завершение.

## Yandex Cloud

- SpeechKit: async `RecognizeFile`, speaker labeling (до 2 спикеров на моно)
- YandexGPT: Foundation Models API, ответ — текст отчёта (MVP) или JSON (позже)
- Env: `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `MOCK_AI`, `DATABASE_URL`

## Правила для агента

1. **MVP first** — не добавлять Celery/Postgres/React без явной просьбы
2. **Минимальный diff** — простой UI, без дизайн-систем
3. **Док-файлы** — `AGENTS.md`, `docs/PLAN.md`, `README.md` поддерживать в актуальном состоянии и предупреждать пользователя при правках
4. **Коммиты** — только по запросу пользователя
5. **Язык UI** — русский
