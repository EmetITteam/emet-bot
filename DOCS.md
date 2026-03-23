# EMET Bot — Технічна документація

> Версія: 2.1 | Дата: 2026-03-23 | Мова: Python 3.11

---

## 1. Трудовитрати розробки

| Модуль | Годин |
|---|---|
| Архітектура, БД-схема, Docker-інфра | 20 |
| Telegram-бот: FSM, хендлери, 8 режимів | 70 |
| RAG pipeline (dual-index, OpenAI + Google) | 25 |
| Intent routing + product/objection/competitor detection | 15 |
| Адмін-панель (Flask, повний CRUD, дашборд) | 50 |
| Google Drive auto-sync (атомарний swap, race condition fix) | 25 |
| LMS: курси, теми, тести, прогрес | 25 |
| Промпти + Sales Coach SOS-логіка + ітерації якості | 30 |
| Моніторинг: cost tracking, backup, дайджести, feedback | 15 |
| Безпека: brute-force → PG, credentials → env, chat history → PG | 10 |
| Налагодження, деплой, виправлення багів | 45 |
| Тести (test_coach.py, tests/test_routing.py) | 8 |
| Документація | 5 |
| **Разом** | **~343 год** |

---

## 2. Огляд системи

**EMET Bot** — Telegram-асистент для команди продажів компанії EMET (естетична медицина).

Ключові можливості:
- Відповіді на запитання з корпоративної бази знань (HR, регламенти, CRM)
- AI Sales Coach: скрипти, аргументи, відпрацювання заперечень лікарів
- SOS-режим: миттєва готова фраза під час візиту
- LMS: курси з препаратів, тести, фіксація прогресу
- Розбір кейсів (аналіз реальних діалогів менеджера)
- Сертифікати: пошук і завантаження PDF-документів
- Голосові повідомлення і фото (розпізнавання + відповідь)

---

## 3. Архітектура

```
Telegram ──► aiogram 3.25 (main.py)
                │
                ├─► RAG: ChromaDB + OpenAI/Google embeddings
                │         data/db_index_{kb,coach,certs}_{openai,google}/
                │
                ├─► LLM Failover: gpt-4o-mini → gpt-4o → gemini-2.0-flash
                │
                ├─► PostgreSQL (db.py, ThreadedConnectionPool 1–20)
                │         11 таблиць: logs, users, courses, topics, questions,
                │         answer_options, user_progress, onboarding_items,
                │         onboarding_progress, sync_state, audit_log,
                │         allowed_emails, deleted_chunks, chat_histories
                │
                ├─► Google Drive (sync_manager.py)
                │         Авто-синхронізація: Drive → ChromaDB (атомарний swap)
                │
                └─► Flask Admin Panel (admin_panel.py, порт 5000)


Docker Compose:
  emet_bot_app      ← python main.py
  emet_admin_panel  ← python admin_panel.py
  emet_postgres     ← postgres:16-alpine
```

### Моделі AI

| Призначення | Модель | Fallback |
|---|---|---|
| KB / операційні / кейси | gpt-4o-mini | gemini-2.0-flash |
| Sales Coach / Combo | gpt-4o | gemini-2.0-flash |
| Intent routing | gpt-4o-mini | "kb" (hardcode) |
| Embeddings (основні) | text-embedding-3-small | — |
| Embeddings (резерв) | gemini-embedding-001 | — |

---

## 4. Структура файлів

```
/opt/emet-bot/
├── main.py              # Точка входу. Всі хендлери, RAG-логіка, FSM
├── admin_panel.py       # Flask веб-панель адміністратора
├── sync_manager.py      # Авто-синхронізація Google Drive → ChromaDB
├── prompts.py           # Всі системні промпти LLM (редагувати тут)
├── db.py                # PostgreSQL connection pool
├── analytics.py         # Генерація Excel-звітів
├── dashboard.py         # HTML-дашборд статистики
├── test_coach.py        # Авто-тест якості Sales Coach (3 запити, GPT-суддя)
├── backup_indices.sh    # Cron-скрипт бекапу ChromaDB індексів
│
├── .env                 # Секрети (не в git)
├── docker-compose.yml   # Конфігурація контейнерів
├── Dockerfile           # python:3.11-slim, WORKDIR /app
├── requirements.txt     # Python залежності
│
└── data/
    ├── db_index_kb_openai/      # ChromaDB: KB + OpenAI embeddings
    ├── db_index_kb_google/      # ChromaDB: KB + Gemini embeddings
    ├── db_index_coach_openai/   # ChromaDB: Coach + OpenAI embeddings
    ├── db_index_coach_google/   # ChromaDB: Coach + Gemini embeddings
    ├── db_index_certs_openai/   # ChromaDB: Certs + OpenAI embeddings
    ├── db_index_certs_google/   # ChromaDB: Certs + Gemini embeddings
    └── EMET_Sales_Arguments_KB.txt  # Ручна база аргументів продажів
```

---

## 5. Конфігурація (.env)

| Змінна | Опис | Обов'язкова |
|---|---|---|
| `TELEGRAM_TOKEN` | Bot token від @BotFather | ✅ |
| `OPENAI_API_KEY` | OpenAI API ключ | ✅ |
| `GEMINI_API_KEY` | Google Gemini API ключ | ✅ |
| `ANTHROPIC_API_KEY` | Claude API ключ (резерв) | ❌ |
| `ADMIN_ID` | Telegram ID адміністратора | ✅ |
| `POSTGRES_DB` | Ім'я БД | ✅ |
| `POSTGRES_USER` | Користувач БД | ✅ |
| `POSTGRES_PASSWORD` | Пароль БД | ✅ |
| `DATABASE_URL` | postgresql://user:pass@host:5432/db | ✅ |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service Account JSON (base64 або raw) | ✅ |
| `ADMIN_PASSWORD` | Пароль адмін-панелі | ✅ |
| `FLASK_SECRET` | Flask session secret | ✅ |
| `AUTO_SYNC_ENABLED` | `true`/`false` — авто-синк з Drive | ❌ |
| `SYNC_INTERVAL_SEC` | Інтервал синку в секундах (default: 3600) | ❌ |
| `COURSE_FOLDER_ID` | ID папки Google Drive з курсами | ❌ |
| `DAILY_BUDGET_LIMIT` | Ліміт витрат OpenAI/день в USD (default: 10) | ❌ |
| `TZ` | Часовий пояс (default: Europe/Kiev) | ❌ |
| `HTTPS_ENABLED` | `true` якщо за SSL-проксі | ❌ |

---

## 6. Схема бази даних PostgreSQL

### `logs` — логи запитів
```sql
id, date, user_id, username, mode, ai_engine,
question, answer, found_in_db, model, tokens_in, tokens_out
```

### `users` — користувачі бота
```sql
user_id TEXT PK, username, first_name,
role TEXT DEFAULT 'manager',  -- admin/manager/operator/director
level TEXT DEFAULT 'junior',
registered_at, last_active, is_active INTEGER DEFAULT 1
```

### `courses` / `topics` / `questions` / `answer_options` — LMS
```
courses → topics (course_id) → questions (topic_id) → answer_options (question_id)
```

### `user_progress` — прогрес тестів
```sql
user_id, course_id, topic_id, passed, score, attempts, last_date
UNIQUE(user_id, topic_id)
```

### `sync_state` — стан синхронізації Drive
```sql
file_id TEXT PK, file_name, modified_time, indexed_at,
folder_label,  -- 'kb' | 'coach' | 'certs'
uploaded_by, deleted_by, deleted_at
```

### `deleted_chunks` — кошик (30 днів на відновлення)
```sql
id, file_name, file_id, index_path, chunks_json,
deleted_by, deleted_at, restore_deadline
```

### `audit_log` — аудит дій в адмін-панелі
```sql
id, user_id, action, details, created_at
```

### `allowed_emails` — white-list email для авторизації
```sql
id, email UNIQUE, role, full_name, activated_by_user_id, activated_at
```

### `onboarding_items` / `onboarding_progress` — онбординг
```sql
onboarding_items: id, day, order_num, title, description, item_type
onboarding_progress: user_id, item_id, completed, completed_at
UNIQUE(user_id, item_id)
```

### `chat_histories` — історія діалогів (виживає після рестарту)
```sql
user_id TEXT PK, history_json TEXT, updated_at TIMESTAMP
```

### `feedback` — оцінки відповідей (👍/👎)
```sql
id SERIAL PK, log_id INTEGER, user_id TEXT,
rating INTEGER,  -- 1 = позитивна, -1 = негативна
mode TEXT, created_at TIMESTAMP
```

### `admin_login_attempts` — brute-force захист адмінки (PostgreSQL)
```sql
ip TEXT PK, count INTEGER DEFAULT 0, locked_until TIMESTAMP
-- блокування на 5 хв після 5 невдалих спроб, виживає після рестарту
```

---

## 7. Режими бота та маршрутизація

### Режими (FSM States)

| Режим | State | Опис |
|---|---|---|
| 📋 KB | `mode_kb` | HR, регламенти, внутрішні процедури |
| 💼 Coach | `mode_coach` | Скрипти продажів, аргументи, заперечення |
| 🎓 LMS | `mode_learning` | Курси з препаратів, тести |
| 🔍 Кейси | `mode_cases` | Розбір реальних діалогів |
| ⚙️ Операційні | `mode_operational` | Відрядження, повернення товару, семінари |
| 🌱 Онбординг | `mode_onboarding` | Чек-лист для нових менеджерів |
| 📜 Сертифікати | `mode_certs` | Пошук PDF-сертифікатів з Drive |

### Логіка маршрутизації (порядок перевірок)

```
1. Combo keywords?  → mode = "combo"
2. Operational keywords? → mode = "operational"  (відрядження, etc.)
3. Script/follow-up keywords + є chat_history? → mode = "coach"
4. Інакше → detect_intent(LLM) паралельно з prepare_search_query
```

### Під-режими Coach

| Тригер | Формат відповіді |
|---|---|
| Продукт + заперечення + НЕ скрипт | **SOS**: готова фраза + 2-3 тези. Заборонено починати з тривалості дії |
| Запит на скрипт/діалог | **Script**: діалог Лікар/Менеджер |
| Follow-up (інші аргументи, etc.) | **Short list**: 3-5 нових аргументів, max 8 рядків |
| Загальне питання про продукт | **Full Coach**: 7-секційний розбір |

---

## 8. RAG Pipeline

### Індекси

```
Документи з Google Drive
    ↓
sync_manager.py: extract_text() → chunk (800/1500 chars) → embed
    ↓
ChromaDB (persist на диску):
  data/db_index_kb_openai/      ← chunk_size=800,  OpenAI embeddings
  data/db_index_kb_google/      ← chunk_size=800,  Gemini embeddings
  data/db_index_coach_openai/   ← chunk_size=1500, OpenAI embeddings
  data/db_index_coach_google/   ← chunk_size=1500, Gemini embeddings
```

### Search query збагачення

Перед RAG-пошуком `main.py` збагачує search_query:
- Знайдено продукт → `"{canonical} {query}"`
- Заперечення + конкурент → `"порівняння конкурент {product} {competitor} аргументи"`
- Запит на скрипт → `"скрипт аргументи заперечення діалог {product}"`

### Failover

```
OpenAI (спроба 1) → OpenAI (спроба 2 якщо пустий контекст)
    → при помилці: Gemini fallback
```

---

## 9. Google Drive Sync

### Папки Drive

| Label | Folder ID | Призначення |
|---|---|---|
| `kb` | `1RBXHGXOIc2kkSAw-LqzLaRqEE3Ix7L-m` | HR-регламенти |
| `coach` | `1KPPBurEoCV_wWzY5HxEtv_TrMI4qXfPa` | Продукти / продажі |
| `certs` | `1ma-6CNO2FeHaicbRag7RvStkf5Rp1MyJ` | Сертифікати (тільки sync_state) |

### Алгоритм синку

```
1. list_files_with_meta(Drive) → порівняти з sync_state в PG
2. Якщо є зміни:
   а. Побудувати індекс у tmp_dir (_building)
   б. Атомарний swap: старий → _old, новий → prod
   в. Видалити _old
3. Оновити sync_state в PG
4. Скинути VDB-синглтони в main.py (наступний запит завантажить новий індекс)
```

**Захист від race condition:** `threading.Lock` — якщо синк вже виконується, новий запуск повертає `error=sync_in_progress` без блокування.

### Ручний синк

Адмін-панель → кнопка "Синхронізувати" → POST `/sync` → `sync_manager.run_sync()` в окремому потоці.

---

## 10. Адмін-панель

**URL:** `https://your-domain/` (через SSL-проксі → `localhost:5000`)
**Авторизація:** пароль з `ADMIN_PASSWORD` у `.env`

### Розділи

| Розділ | Функціонал |
|---|---|
| Дашборд | Статистика запитів, витрати токенів по моделях, графіки |
| Користувачі | CRUD: додати/видалити/змінити роль, блокування |
| База знань | Завантаження документів (KB / Coach / Both), перегляд індексу, видалення |
| Курси LMS | Перегляд курсів, тем, тестових питань |
| Онбординг | Редагування чек-листа за днями |
| Синхронізація | Ручний запуск синку з Google Drive |
| Логи | Перегляд і фільтрація запитів |
| Аудит | Лог дій адміністраторів |

### Завантаження документів

При завантаженні через адмін-панель можна вибрати категорію:
- **KB** → `data/db_index_kb_openai` + `data/db_index_kb_google`
- **Coach** → `data/db_index_coach_openai` + `data/db_index_coach_google`
- **Both** → всі 4 індекси

---

## 11. Фонові задачі (main.py)

| Задача | Розклад | Дія |
|---|---|---|
| `auto_sync_task` | кожні `SYNC_INTERVAL_SEC` (default 1 год) | Синк Drive → RAG |
| `daily_cost_task` | кожну годину + о 23:00 | Перевірка бюджету + звіт адміну |
| `weekly_digest_task` | щопонеділка о 09:00 | Звіт по активності до всіх менеджерів |
| `ttl_cleanup_task` | щодня о 03:00 | Видалення логів > 90 днів, кошика > 30 днів |

---

## 12. Бекап

### ChromaDB індекси

**Скрипт:** `/opt/emet-bot/backup_indices.sh`
**Cron:** `0 2 * * * /opt/emet-bot/backup_indices.sh >> /var/log/emet_backup.log 2>&1`
**Зберігання:** `/opt/emet-bot/backups/indices_YYYYMMDD_HHMM.tar.gz`
**Ротація:** 7 останніх архівів
**Алерт:** Telegram-повідомлення адміну після кожного бекапу

### PostgreSQL

```bash
# Ручний бекап
docker exec emet_postgres pg_dump -U emet emet_bot > backup_$(date +%Y%m%d).sql

# Відновлення
docker exec -i emet_postgres psql -U emet emet_bot < backup_20260323.sql
```

---

## 13. Деплой

### Перший запуск (на чистому сервері)

```bash
# 1. Клонувати репозиторій
git clone <repo> /opt/emet-bot
cd /opt/emet-bot

# 2. Створити .env (скопіювати шаблон, заповнити всі ключі)
cp .env.example .env
nano .env

# 3. Додати Google credentials
echo "GOOGLE_SERVICE_ACCOUNT_JSON=$(base64 -w0 credentials.json)" >> .env

# 4. Запустити
docker compose up -d --build

# 5. Перевірити
docker compose logs -f emet-bot
```

### Оновлення (деплой нової версії)

```bash
cd /opt/emet-bot

# Зберегти локальні зміни (порт 5000:5000 в docker-compose.yml)
git stash

# Отримати зміни
git pull

# Відновити локальні зміни
git stash pop

# Перебудувати і перезапустити
docker compose up -d --build

# Перевірити логи
docker compose logs -f emet-bot
```

### Корисні команди

```bash
# Статус контейнерів
docker compose ps

# Логи бота в реальному часі
docker logs -f emet_bot_app

# Зайти всередину контейнера
docker exec -it emet_bot_app bash

# Перезапустити тільки бота (без rebuild)
docker compose restart emet-bot

# Тест якості Sales Coach (запускати в контейнері)
docker exec emet_bot_app python test_coach.py

# Тест Google Drive авторизації
docker exec emet_bot_app python -c "import sync_manager; d,_ = sync_manager.get_services(); print('OK')"

# Ручна синхронізація Drive
docker exec emet_bot_app python -c "import sync_manager; r = sync_manager.run_sync(); print(r)"

# Перевірити бекапи
ls -lh /opt/emet-bot/backups/
```

---

## 14. Відновлення після збоїв

### Бот не відповідає

```bash
docker compose ps                    # перевірити статус
docker logs emet_bot_app --tail=50   # переглянути помилки
docker compose restart emet-bot      # перезапустити
```

### PostgreSQL недоступний

```bash
docker compose restart postgres
# Якщо не допомогло — перевірити volume
docker volume inspect aesthetic_bot_postgres_data
```

### Зіпсований ChromaDB індекс (бот повертає порожні відповіді)

```bash
# Варіант 1: відновити з бекапу
cd /opt/emet-bot
tar -xzf backups/indices_YYYYMMDD_HHMM.tar.gz -C data/

# Варіант 2: перебудувати з Drive
docker exec emet_bot_app python -c "
import sync_manager
import shutil, os
for path in ['data/db_index_kb_openai', 'data/db_index_kb_google',
             'data/db_index_coach_openai', 'data/db_index_coach_google']:
    shutil.rmtree(path, ignore_errors=True)
r = sync_manager.run_sync()
print(r)
"
```

### Бот забув розмову після рестарту

Починаючи з версії 2.0 — не трапляється. Історія зберігається в таблиці `chat_histories` PostgreSQL. Якщо все ж стався збій:
```bash
# Перевірити що таблиця є
docker exec emet_postgres psql -U emet emet_bot -c "SELECT COUNT(*) FROM chat_histories;"
```

### Синхронізація Drive не працює

```bash
# 1. Перевірити авторизацію
docker exec emet_bot_app python -c "import sync_manager; d,_ = sync_manager.get_services(); print('Auth OK')"

# 2. Перевірити що GOOGLE_SERVICE_ACCOUNT_JSON заповнений
docker exec emet_bot_app python -c "import os; print(len(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON','')))"
# Має бути > 0

# 3. Запустити синк вручну і подивитись помилку
docker exec emet_bot_app python -c "import sync_manager; print(sync_manager.run_sync())"
```

### Перевищено бюджет OpenAI

Бот автоматично надсилає алерт адміну при перевищенні `DAILY_BUDGET_LIMIT` (default $10).
Для збільшення ліміту:
```bash
# В .env на сервері:
DAILY_BUDGET_LIMIT=20
docker compose restart emet-bot
```

---

## 15. Моніторинг

### Витрати токенів

- **Реалтайм:** адмін-панель → Дашборд → секція "Витрати"
- **Щоденний звіт:** Telegram о 23:00 адміну
- **Алерт:** миттєво якщо `today_cost >= DAILY_BUDGET_LIMIT`

### Бекапи

- **Лог:** `/var/log/emet_backup.log`
- **Telegram-алерт:** щодня о 02:00 після успішного бекапу

### Щотижневий дайджест

Щопонеділка о 09:00 → адміну + всім активним менеджерам:
- Кількість активних користувачів
- Всього запитів за тиждень
- % знайдено в базі
- Прогрес навчання (тести)
- Розбивка по режимах

---

## 16. Управління користувачами

### Команди в Telegram (тільки для адміна)

```
/adduser <telegram_id> [role]    — додати користувача
/removeuser <telegram_id>        — деактивувати
/users                           — список активних
/stats                           — статистика за сьогодні
/sync                            — ручна синхронізація Drive
/export                          — вивантажити логи Excel
```

### Ролі

| Роль | Доступ |
|---|---|
| `admin` | Всі функції + адмін-команди |
| `manager` | Всі режими бота |
| `operator` | Обмежений доступ |
| `director` | Перегляд аналітики |

### Авторизація нових користувачів

1. Користувач пише боту → бот просить email
2. Якщо email є в `allowed_emails` → активується з відповідною роллю
3. Якщо немає → відмова / запит до адміна

---

## 17. Локальна розробка

```bash
# Клонувати і встановити залежності
git clone <repo>
cd aesthetic_bot
pip install -r requirements.txt

# Налаштувати .env для локального PostgreSQL
DATABASE_URL=postgresql://emet:emet2026@localhost:5432/emet_bot

# Запустити тільки PostgreSQL через Docker
docker run -d --name emet_pg -e POSTGRES_DB=emet_bot \
  -e POSTGRES_USER=emet -e POSTGRES_PASSWORD=emet2026 \
  -p 5432:5432 postgres:16-alpine

# Запустити бота локально
python main.py

# Запустити адмін-панель локально
python admin_panel.py
```

---

## 18. Відомі обмеження

| Обмеження | Статус |
|---|---|
| Адмін-панель: одна точка входу, без 2FA | Відкрито |
| Rate limit на login адмін-панелі: немає | Відкрито |
| ChromaDB: немає версіонування індексів | Відкрито (є бекап) |
| Сертифікати: PDF скановані, RAG не використовується | By design (SQL-пошук по іменах) |
| Chat history: зберігається тільки для coach/kb/combo/cases/operational | By design |
