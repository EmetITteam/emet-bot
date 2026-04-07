# Деплой EMET Bot

## Сервер

- **Хост:** 49.12.81.83 (Hetzner)
- **OS:** Ubuntu/Debian
- **SSH:** порт 33222, ключ `~/.ssh/id_rsa`
- **Доступ:** `ssh -i ~/.ssh/id_rsa -p 33222 emet@49.12.81.83`

## Автоматичний деплой (CI/CD)

При кожному `git push` в `main`:
1. GitHub Actions запускає `py_compile` для всіх .py файлів
2. При успіху — SSH на сервер:
   - `cd /opt/emet-bot && git pull origin main`
   - `docker compose restart emet-bot`

**Файл:** `.github/workflows/deploy.yml`

## Ручний деплой

### Оновити бота (тільки код):
```bash
ssh -i ~/.ssh/id_rsa -p 33222 emet@49.12.81.83
cd /opt/emet-bot && git pull origin main
docker compose restart emet-bot
```

### Оновити адмін-панель (потрібен rebuild):
```bash
ssh -i ~/.ssh/id_rsa -p 33222 emet@49.12.81.83
cd /opt/emet-bot && git pull origin main
docker rm -f emet_admin_panel
docker compose up -d --build emet-admin
```

### Оновити обидва:
```bash
ssh -i ~/.ssh/id_rsa -p 33222 emet@49.12.81.83
cd /opt/emet-bot && git pull origin main
docker rm -f emet_admin_panel
docker compose up -d --build emet-admin
docker compose restart emet-bot
```

## Діагностика

### Перевірити стан контейнерів:
```bash
docker ps --format '{{.Names}} {{.Status}}'
```

### Логи бота:
```bash
docker logs emet_bot_app --tail=50
docker logs emet_bot_app -f  # follow
```

### Логи адмін-панелі:
```bash
docker logs emet_admin_panel --tail=50
```

### Підключення до PostgreSQL:
```bash
docker exec emet_postgres psql -U emet -d emet_bot
```

### Перевірка ChromaDB:
```bash
docker exec emet_bot_app python -c "
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
import os; from dotenv import load_dotenv; load_dotenv()
emb = OpenAIEmbeddings(model='text-embedding-3-small', openai_api_key=os.getenv('OPENAI_API_KEY'))
for idx in ['data/db_index_products_openai', 'data/db_index_competitors_openai', 'data/db_index_kb_openai']:
    vdb = Chroma(persist_directory=idx, embedding_function=emb)
    print(f'{idx}: {vdb._collection.count()} chunks')
"
```

## Типові проблеми

### Бот не відповідає
```bash
docker logs emet_bot_app --tail=20  # перевірити помилки
docker compose restart emet-bot     # перезапустити
```

### Контейнер з конфліктом імені
```bash
docker rm -f emet_admin_panel       # видалити старий
docker compose up -d emet-admin     # створити новий
```

### DNS помилка (api.telegram.org)
Тимчасова проблема мережі — бот автоматично перезапуститься (restart: always).

### Бот стартує але не відповідає на /start
Перевірити що TELEGRAM_TOKEN в .env правильний і бот не запущений десь ще.

## Змінні оточення (.env)

| Змінна | Опис | Обов'язкова |
|--------|------|-------------|
| TELEGRAM_TOKEN | Токен бота від @BotFather | Так |
| OPENAI_API_KEY | OpenAI API ключ | Так |
| GEMINI_API_KEY | Google Gemini API ключ | Так |
| ANTHROPIC_API_KEY | Anthropic Claude ключ | Ні (fallback) |
| ADMIN_ID | Telegram ID адміністратора | Так |
| ADMIN_PASSWORD | Пароль адмін-панелі | Так |
| POSTGRES_DB | Назва БД | Так (emet_bot) |
| POSTGRES_USER | Юзер БД | Так (emet) |
| POSTGRES_PASSWORD | Пароль БД | Так |
| GOOGLE_SERVICE_ACCOUNT_JSON | JSON сервісного акаунту | Для Google Drive sync |
| FLASK_SECRET | Секрет Flask сесій | Ні (є дефолт) |
| AUTO_SYNC_ENABLED | true/false — автосинхронізація | Ні (дефолт: true) |
