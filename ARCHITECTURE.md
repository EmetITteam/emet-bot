# EMET Bot — Архітектура проекту

## Огляд
Telegram-бот + адмін-панель для компанії EMET (естетична медицина).  
RAG-асистент з 8 режимами, LMS-системою, щоденним моніторингом якості.

---

## Діаграма системи

```
┌─────────────────────────────────────────────────────────────────────┐
│                         ЗОВНІШНІ СЕРВІСИ                            │
│                                                                     │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ OpenAI   │  │  Google   │  │Anthropic │  │   Google Drive    │  │
│  │ GPT-4o   │  │  Gemini   │  │  Claude  │  │  (документи)      │  │
│  │ Embed    │  │  Embed    │  │          │  │                   │  │
│  └────┬─────┘  └────┬──────┘  └────┬─────┘  └────────┬──────────┘  │
│       │              │              │                  │            │
└───────┼──────────────┼──────────────┼──────────────────┼────────────┘
        │              │              │                  │
        ▼              ▼              ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         DOCKER COMPOSE                              │
│                                                                     │
│  ┌────────────────────────────────────────────────┐                 │
│  │              emet_bot_app                       │                 │
│  │  main.py (Telegram polling)                     │                 │
│  │                                                 │                 │
│  │  ┌─────────┐ ┌──────────┐ ┌──────────────────┐ │                 │
│  │  │   FSM   │ │   RAG    │ │   LLM Failover   │ │                 │
│  │  │11 станів│ │ 3 zones  │ │ GPT→Gemini→Claude│ │                 │
│  │  └─────────┘ └──────────┘ └──────────────────┘ │                 │
│  │  ┌──────────────────────┐ ┌──────────────────┐ │                 │
│  │  │  Quality Monitor     │ │   Sync Manager   │ │                 │
│  │  │  (daily 08:00)       │ │   (hourly)       │ │                 │
│  │  └──────────────────────┘ └──────────────────┘ │                 │
│  └─────────────────────┬──────────────────────────┘                 │
│                        │ ./data (shared volume)                     │
│  ┌─────────────────────┴──────────────────────────┐                 │
│  │             emet_admin_panel                     │                 │
│  │  admin_panel.py (Flask :5000)                    │                 │
│  │                                                  │                 │
│  │  ┌──────────┐ ┌─────────┐ ┌──────────────────┐  │                 │
│  │  │Дашборд   │ │  LMS    │ │  Quality Page    │  │                 │
│  │  │База знань│ │ Upload  │ │  /quality        │  │                 │
│  │  │Доступи   │ │ Index   │ │                  │  │                 │
│  │  └──────────┘ └─────────┘ └──────────────────┘  │                 │
│  └─────────────────────┬───────────────────────────┘                 │
│                        │                                             │
│  ┌─────────────────────┴───────────────────────────┐                 │
│  │             emet_postgres                        │                 │
│  │  PostgreSQL 16 (16 таблиць)                      │                 │
│  │  logs, users, courses, topics, questions,        │                 │
│  │  user_progress, sync_state, feedback...          │                 │
│  └──────────────────────────────────────────────────┘                 │
│                                                                      │
│  ┌──────────────────────────────────────────────────┐                │
│  │              ./data/ (shared volume)              │                │
│  │  db_index_products_openai/   (598 chunks)         │                │
│  │  db_index_competitors_openai/ (599 chunks)        │                │
│  │  db_index_kb_openai/          (~470 chunks)       │                │
│  │  db_index_coach_openai/       (1112 source)       │                │
│  └──────────────────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────┐         ┌──────────────────┐
│   Telegram API   │         │   GitHub Actions  │
│  @emetlms_bot    │         │  push → deploy    │
└──────────────────┘         └──────────────────┘
```

---

## Обгрунтування технічних рішень

| Рішення | Чому | Альтернативи що розглядались |
|---------|------|------------------------------|
| **3-zone RAG** (products/competitors/merge) | 62% бази були конкуренти → GPT плутав наші дані з чужими | Один індекс з маркуванням (не спрацювало — GPT ігнорував мітки) |
| **aiogram 3.x** (не python-telegram-bot) | Async, FSM вбудований, middleware, streaming | python-telegram-bot (синхронний, менш гнучкий) |
| **ChromaDB** (не Pinecone/Weaviate) | Локальний, безкоштовний, працює в Docker | Pinecone (платний), FAISS (немає metadata filtering) |
| **Flask** (не FastAPI) | Простота, inline HTML templates, швидкий start | FastAPI (краще для API, але overhead для admin UI) |
| **GPT-4o primary** | Найкраща якість для coach/sales промптів | GPT-4o-mini (дешевше, але гірше для складних скриптів) |
| **PostgreSQL** (не SQLite) | Конкурентний доступ (бот + адмінка), надійність | SQLite (single-writer lock, не витримує 2 контейнери) |
| **Dual embeddings** (OpenAI + Google) | Failover при збої одного провайдера | Один провайдер (ризик простою) |

---

## Структура файлів

```
aesthetic_bot/
│
├── main.py                    # Ядро бота (3267 рядків)
│                              # - FSM-машина станів (11 станів)
│                              # - 60+ Telegram хендлерів
│                              # - RAG pipeline (3-zone search)
│                              # - LLM failover: GPT-4o → Gemini → Claude
│                              # - Фонові задачі (sync, digest, quality monitor)
│
├── admin_panel.py             # Flask адмін-панель (2208 рядків)
│                              # - 23 API маршрути
│                              # - Дашборд, база знань, LMS, доступи, дайджести
│                              # - Quality monitor UI (/quality)
│
├── prompts.py                 # Системні промпти (480 рядків)
│                              # - PROMPT_COACH (Sales Coach — основний)
│                              # - PROMPT_KB (База знань / HR)
│                              # - PROMPT_CASES (Розбір кейсів)
│                              # - PROMPT_OPERATIONAL (Операційні)
│                              # - PROMPT_COMBO (Комбо-протоколи)
│
├── db.py                      # PostgreSQL connection pool (120 рядків)
├── sync_manager.py            # Google Drive → ChromaDB sync + scope metadata (~600 рядків)
├── quality_monitor.py         # Щоденний аналіз якості + LLM judge + SD метрики (~520 рядків)
├── dialog_state.py            # NEW (24.04): мінімальний context tracker для скиду chat_history
│
├── tools/                     # Утиліти (запускаються вручну або через адмінку)
│   ├── import_course.py       # Імпорт курсу з Excel → PostgreSQL
│   ├── make_course_template.py# Генератор Excel-шаблону курсу
│   └── analytics.py           # Експорт аналітики в Excel
│
├── scripts/                   # Одноразові / міграційні скрипти
│   ├── migrate_split_indices.py  # Міграція: coach → products + competitors
│   ├── _import_ellanse.py     # Імпорт Ellanse (одноразовий)
│   ├── _reindex_library.py    # Переіндексація бібліотеки знань
│   └── analyze_chains.py      # Аналіз ланцюжків діалогів
│
├── tests/                     # Тестування
│   ├── regression_fixtures.json # NEW: 15 known-bad cases для регресій
│   ├── run_regression.py        # NEW: async runner — classifier+RAG+LLM проти fixtures
│   ├── test_routing.py        # Тести маршрутизації запитів
│   ├── rag_smoke.py           # Smoke-тести RAG
│   ├── health_check.py        # Health check бота
│   ├── golden_dataset.json    # Еталонний датасет для тестів
│   └── ...
│
├── courses/                   # Excel-файли курсів (не в git)
│   ├── course_template.xlsx   # Порожній шаблон
│   ├── Тест Еllanse.xlsx     # Курс Ellanse
│   └── ...                    # 10+ курсів
│
├── audit/                     # Експорти аудитів (не в git)
│
├── data/                      # Runtime дані (не в git)
│   ├── db_index_products_openai/    # ChromaDB: наші продукти
│   ├── db_index_competitors_openai/ # ChromaDB: конкуренти
│   ├── db_index_kb_openai/          # ChromaDB: база знань
│   ├── db_index_*_google/           # Google embeddings (резерв)
│   └── bot_usage.db                 # Legacy SQLite (deprecated)
│
├── mini_app/                  # Telegram Mini App (пілот, не в git)
│
├── .github/workflows/deploy.yml  # CI/CD: push → lint → SSH deploy
├── docker-compose.yml         # 3 сервіси: postgres, bot, admin
├── Dockerfile                 # Python 3.11-slim
├── requirements.txt           # 26 залежностей
├── .env                       # Секрети (не в git)
└── .env.example               # Шаблон .env
```

---

## Архітектура RAG (3-zone search)

```
                     Запит менеджера
                           │
                    ┌──────▼──────┐
                    │  detect_intent()  │  → coach / kb / cases / operational
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Детекція    │  _detected_product = "vitaran"
                    │  продукту +  │  _detected_competitor = "juvederm"
                    │  конкурента  │
                    └──┬────┬──┬──┘
                       │    │  │
          ─────────────┘    │  └──────────────
          │                 │                 │
          ▼                 ▼                 ▼
    ┌──────────┐    ┌─────────────┐    ┌───────────┐
    │ PRODUCTS │    │  PRODUCTS   │    │COMPETITORS│
    │  ONLY    │    │     +       │    │   ONLY    │
    │  k=12    │    │ COMPETITORS │    │   k=5     │
    │          │    │ k=12 + k=5  │    │           │
    └────┬─────┘    └──────┬──────┘    └─────┬─────┘
         │                 │                 │
         └────────┬────────┘                 │
                  │                          │
           ┌──────▼──────┐           ┌───────▼──────┐
           │ _extract_docs│           │ _extract_docs│
           │  📘 LMS      │           │  ⚠️ КОНКУРЕНТ│
           │  📄 Product  │           │              │
           └──────┬──────┘           └──────┬───────┘
                  │                          │
                  └────────┬─────────────────┘
                           │
                    ┌──────▼──────┐
                    │   GPT-4o    │  system: PROMPT_COACH
                    │  + context  │  + chat_history
                    │  + question │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Telegram   │  streaming response
                    │  відповідь  │  + feedback buttons
                    └─────────────┘
```

**Тригери зон:**
| Зона | Коли | K |
|------|------|---|
| Products only | Запит про наш продукт без конкурента | 12 |
| Products + Competitors | Наш + конкурент ("vs", "порівняй") | 12+5 |
| KB | Режим base знань / HR / регламенти | 15 |

---

## LLM Failover

```
GPT-4o (primary)  →  Gemini 2.0 Flash  →  Claude Sonnet
     │                     │                    │
  timeout/error        timeout/error         timeout/error
  quota exceeded       unavailable           unavailable
     │                     │                    │
     └──→ alert admin  ───→└──→ alert admin ───→└──→ "Сервери перевантажені"
```

| Модель | Для чого | Max tokens |
|--------|----------|------------|
| gpt-4o | Coach, Combo | stream |
| gpt-4o-mini | KB, Cases, Operational, Intent routing | stream |
| gemini-2.0-flash | Fallback #1 (structured chat API) | 4096 |
| claude-sonnet | Fallback #2 | 4096 |

---

## База даних PostgreSQL (16 таблиць)

**Основні:**
- `logs` — всі запити/відповіді (user_id, mode, model, tokens, found_in_db)
- `users` — профілі (user_id, role, level, is_active)
- `chat_histories` — JSON-історія діалогу per user
- `feedback` — 👍/👎 оцінки відповідей

**LMS:**
- `courses` → `topics` → `questions` → `answer_options`
- `user_progress` — passed, score, attempts per user per topic

**Знання:**
- `sync_state` — стан синхронізації Google Drive файлів
- `deleted_chunks` — кошик видалених документів (30 днів)

**Доступи:**
- `allowed_emails` — білий список + ролі
- `admin_login_attempts` — brute-force захист
- `audit_log` — лог дій адміністратора

**Якість і моніторинг (24.04 update):**
- `quality_history` — щоденні агрегати LLM-judge (helpful/factual/format/role) + SD метрики (correction_rate, mode_mismatch, margin_at_risk, pct_openai/gemini/claude)
- `knowledge_gaps` — запити де менеджер виправив бота (open/filled/dismissed) + поле med_dept_response
- `logs.failover_depth` — нова колонка: 0=OpenAI, 1=Gemini, 2=Claude (observability LLM failover)

---

## ChromaDB індекси

| Індекс | Чанків | Вміст | Chunk size |
|--------|--------|-------|------------|
| products_openai | 598 | LMS курси + Sales docs (16 product_canonical) | 1200/300 |
| competitors_openai | 599 | Competitors_MASTER файли | 1200/300 |
| kb_openai | 470 | Регламенти, HR, SOP | 800/150 |
| coach_openai | 1112 | Source — split → products + competitors | 1200/300 |
| *_google | mirror | Google embeddings (failover) | same |

**Metadata на чанках:**
- `source` — назва файлу
- `url` — посилання Google Drive
- `file_id` — ID файлу
- `category` — "general" / "combo"
- `folder` — "coach" / "kb" / "products"
- `product_canonical` — нормалізована назва продукту (ESSE / Petaran / Ellansé / Vitaran / ...) — для product-locked retrieval
- `scope` — **NEW (24.04):** `line` / `product` / `ingredient` / `protocol` — щоб бот не приписував характеристики лінії конкретному продукту (антигалюцинація)

**Розподіл scope (станом на 24.04):** product 1040 / protocol 75 / ingredient 54 / line 28

---

## Docker Compose

```yaml
services:
  postgres:     # PostgreSQL 16 Alpine
    port: 5432 (internal)
    volume: postgres_data

  emet-bot:     # Python 3.11 → main.py
    volume: ./data:/app/data
    depends_on: postgres (healthy)
    healthcheck: pgrep -f 'python main.py'

  emet-admin:   # Python 3.11 → admin_panel.py
    port: 5000
    volume: ./data:/app/data (shared with bot!)
    depends_on: postgres (healthy)
```

**Важливо:** `./data` змонтований в обох контейнерах — ChromaDB індекси спільні.

---

## CI/CD

```
Developer (VS Code) → git push → GitHub Actions:
  1. py_compile lint (all .py files)
  2. SSH deploy:
     - git pull on server
     - docker compose restart emet-bot
     - docker compose up -d --build emet-admin
```

---

## Фонові задачі (main.py)

| Задача | Розклад | Що робить |
|--------|---------|-----------|
| daily_quality_task | 08:00 (Kyiv) | Аналіз діалогів → звіт ADMIN_ID + LLM-judge + heavy-correctors alert (>2 виправлень/день) |
| daily_cost_task | 22:00 (Kyiv) | Денний звіт витрат → ADMIN_ID. Quiet hours 22:00-09:00 (критичні >2× ліміту прориваються) |
| weekly_digest_task | Пн 09:00 | Дайджест всім менеджерам |
| auto_sync_task | кожні 60 хв | Sync Google Drive → ChromaDB + scope tagging + retry + admin notify on fail |
| ttl_cleanup_task | 03:00 | Видалення логів >90 днів |
| backup_indices.sh (cron) | 02:00 | PG dump + ChromaDB tar, 7-day rotation |

---

## Адмін-панель (~26 маршрутів)

| Секція | Маршрути | Опис |
|--------|----------|------|
| 📊 Дашборд | / | KPI, графіки, витрати |
| 📚 База знань | /knowledge, /knowledge/upload, /delete, /restore, /sync | CRUD документів |
| 👥 Користувачі | /users | Список + ролі |
| 🎓 Навчання | /learning, /upload, /delete, /template, /index_courses | LMS + RAG індексація |
| 🔑 Доступи | /access, /access/add, /access/activate, /access/role, /access/delete, /upload | Email whitelist + inline role-dropdown (NEW 22.04) |
| 🔍 Якість | /quality, /quality/run | Quality monitor + історія 30 днів + **бізнес-метрики SD** (NEW 23.04) |
| 🆘 Пробіли знань | /gaps, /gaps/update/<id> | **NEW 23.04**: трекінг виправлень менеджерів — медвідділ закриває з відповіддю |
| 📨 Дайджест | /digest, /digest/send | Тижневий звіт |

---

## Quality Layer (24.04 update)

Новий шар механізмів які захищають бота від галюцинацій / sycophancy / context drift.
Усі активні в production.

```
                Запит менеджера
                      │
                      ▼
            ┌─────────────────────┐
            │   Classifier (LLM)   │ → intent, product, confidence
            └──────────┬──────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ Differential diagnosis      │  conf<0.5 + ambiguous → clarify (no LLM call)
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ Knowledge gap detector       │  intent=correction → log to knowledge_gaps
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ DialogState compute_state    │  topic shift → reset chat_history
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ RAG product-locked search    │  filter by product_canonical
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ _extract_docs з scope-мітками│  [SCOPE=LINE] / [SCOPE=PRODUCT] / [SCOPE=PROTOCOL]
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ Prompt assembly:             │
        │ BASE (anti-sycophancy + scope│
        │       discipline + ESSE rules│
        │ + SOS/INFO/VERBATIM/COMBO     │  + STRICT-MODE для verbatim інтентів
        │ + PRICE-COMPARATIVE block     │  для objection_price з comparison_target
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ LLM (failover_depth логується)│
        └──────────────┬──────────────┘
                       │
                       ▼
        ┌─────────────────────────────┐
        │ log_to_db + scope context    │
        └─────────────────────────────┘
```

**Ключові правила в BASE prompt:**
- `#0 anti-sycophancy` — не погоджуватись автоматично з виправленням, перевіряти через RAG
- `#0 scope discipline` — чанк з SCOPE=LINE не приписувати конкретному продукту
- `#0 ESSE exception` — космецевтика, дозволено overview лінії якщо немає product chunk
- `STRICT-MODE` — для composition/concentration/certs заборонено hedge-слова («приблизно», «припускаю»)
- `PRAVILO 3 PRICE-COMPARATIVE` — 5-тезисна структура для «X дорожче за Y але дія однакова»
- `Side effects → med dept redirect` — не пояснювати побічки, перенаправляти

---

## Eval Harness (regression testing)

`tests/regression_fixtures.json` (15 кейсів) + `tests/run_regression.py` (async runner).

Прогоняє кожен кейс через **реальний** classifier + RAG + LLM і перевіряє:
- expected_intent / expected_product / min_confidence
- must_contain[] / must_not_contain[]

**Запуск:**
```bash
docker exec emet_bot_app python /app/tests/run_regression.py --no-generate   # 30 сек, лише classifier
docker exec emet_bot_app python /app/tests/run_regression.py                 # 3 хв, з LLM (~$0.10)
docker exec emet_bot_app python /app/tests/run_regression.py --case <id>     # один кейс
docker exec emet_bot_app python /app/tests/run_regression.py --category esse # фільтр
```

**Workflow:** перед кожним великим коммітом у промпти/класифікатор → `--no-generate`, перевіряти що PASS-count не впав.

Категорії в fixtures: `esse`, `side_effect`, `combo`, `price`, `classifier`, `scope`, `clarify`.

---

## Безпека

- Telegram auth: user_id + email whitelist
- Admin panel: password + brute-force protection (5 спроб → 5 хв lock)
- Session: httpOnly, SameSite=Lax, configurable HTTPS
- Headers: CSP, X-Frame-Options=DENY, X-Content-Type-Options
- Secrets: .env (gitignored)
- Audit log: всі дії адміна
- Prompt injection: regex filter в _extract_docs()

---

## Продукти EMET (категоризація в промпті)

| Категорія | Продукти | CE |
|-----------|----------|-----|
| 💉 Ін'єкційні | EXOXE, Ellansé S/M, Neuramis, Neuronox, HP Cell Vitaran (i/iII/Whitening/Tox Eye), Petaran, IUSE Skinbooster HA 20 | ✅ |
| 🧴 Космецевтика | Vitaran Skin Healer (Dual Serum, Azulene Serum, Sleeping Cream), ESSE | ❌ |
| 💊 Нутрієнти | IUSE Collagen Marine Beauty, IUSE HAIR REGROWTH, Magnox 520 | ❌ |

---

## Ключові метрики

| Показник | Значення |
|----------|----------|
| Рядків коду | ~9,500 |
| Python файлів | 17 (основних) + 7 (тести) |
| Telegram хендлерів | 60+ |
| API маршрутів адмінки | 23 |
| PostgreSQL таблиць | 16 |
| ChromaDB індексів | 6 (3 OpenAI + 3 Google) |
| Системних промптів | 5 |
| LMS курсів | 14 |
| LMS тем | 44 |
| RAG чанків (products) | ~620 |
| RAG чанків (competitors) | ~611 |
| Git комітів | 101+ |
