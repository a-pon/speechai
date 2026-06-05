# SpeechAI

MVP для анализа стоматологических консультаций: загрузка MP3, транскрипция, оценка врача по 6 этапам и просмотр результата на простом сайте.

## Что входит

- Веб-интерфейс на FastAPI
- Вход в админку для `admin` и `doctor`
- Загрузка аудиозаписей
- Список консультаций и карточка записи
- Локальное хранилище SQLite и аудиофайлов
- Docker-образ для Render и локального запуска

## Пользователи

| Логин | Пароль | Права |
|-------|--------|-------|
| `admin` | `adm1n` | Все записи, загрузка, удаление |
| `doctor` | `doctor` | Только свои записи, загрузка и удаление своих записей |

У пользователя `doctor` поле врача в форме загрузки заполняется автоматически значением `doctor`.

## Локальный запуск без Docker

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd ..
cp .env.example .env

cd backend
uvicorn app.main:app --reload --port 8000
```

Откройте `http://localhost:8000`.

## Docker: быстрый старт

```bash
cp .env.example .env
mkdir -p volumes/data
docker compose up -d
```

После старта сайт будет доступен на `http://localhost:8000`.

### Остановить контейнер

```bash
docker compose down
```

### Посмотреть логи

```bash
docker compose logs -f
```

### Обновить образ

```bash
docker compose pull
docker compose up -d
```

## Переменные окружения

Минимум для Docker:

```env
MOCK_AI=true
SESSION_SECRET=change-me
DATABASE_URL=sqlite:////data/speechai.db
AUDIO_DIR=/data/audio
```

Если включаете реальный режим:

```env
MOCK_AI=false
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...
```

## Docker Compose для сервера

На сервере нужны только:

- `docker-compose.yml`
- `.env`
- `volumes/`

Пример запуска:

```bash
docker compose pull
docker compose up -d
```

### Рекомендуемая структура на сервере

```text
speechai/
  docker-compose.yml
  .env
  volumes/
```

## Render: рекомендуемый вариант

Самый простой путь для публичного демо:

- код лежит в GitHub
- Render подключается к репозиторию
- Render сам собирает Docker-образ из `Dockerfile`
- при push в `main` Render автоматически делает redeploy

Что нужно для сохранения данных:

- подключить persistent disk
- смонтировать его в `/data`
- оставить SQLite и аудиофайлы внутри этого диска

### Почему это удобно

- не нужен отдельный registry
- не нужен отдельный сервер с `docker compose`
- Render сам хранит URL, логи и деплои
- деплой полностью привязан к GitHub-репозиторию

### Что именно сделать в Render

1. Создать аккаунт в Render и подключить GitHub.
2. Нажать `New +` -> `Web Service`.
3. Выбрать репозиторий `speechai`.
4. Указать ветку `main`.
5. Выбрать `Docker` runtime.
6. Включить `Auto-Deploy` для `main`.
7. Добавить environment variables.
8. Добавить persistent disk и смонтировать его в `/data`.
9. Дождаться первого деплоя.

Ничего вручную загружать в Render не нужно: он сам клонирует код из GitHub и строит контейнер по `Dockerfile`.

### Переменные для Render

Минимум:

```env
MOCK_AI=true
SESSION_SECRET=any-long-random-string
DATABASE_URL=sqlite:////data/speechai.db
AUDIO_DIR=/data/audio
```

Если нужен реальный режим:

```env
MOCK_AI=false
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...
```

### Важно про диск

Для SQLite и загруженных аудиофайлов нужен persistent disk. Без него данные будут пропадать после redeploy или рестарта.

### Что Render делает сам

- забирает код из GitHub
- собирает Docker-образ по `Dockerfile`
- запускает контейнер
- выдаёт публичный `onrender.com` URL

## Серверный деплой через `docker compose`

Это больше не основной путь, но `docker compose` можно оставить для локальной проверки или собственного VPS.

## Команды

| Действие | Команда |
|----------|---------|
| Локальный запуск | `cd backend && uvicorn app.main:app --reload --port 8000` |
| Docker старт | `docker compose up -d` |
| Docker обновление | `docker compose pull && docker compose up -d` |
| Логи | `docker compose logs -f` |
| Остановка | `docker compose down` |

## Документы

- [AGENTS.md](AGENTS.md)
- [docs/PLAN.md](docs/PLAN.md)
