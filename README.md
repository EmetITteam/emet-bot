# EMET Bot — AI Sales Coach & LMS

Telegram-бот + адмін-панель для компанії EMET (естетична медицина).
RAG-асистент з 8 режимами роботи, LMS-системою навчання, щоденним моніторингом якості.

## Швидкий старт

### Вимоги
- Docker + Docker Compose
- Акаунти: OpenAI API, Google Gemini API, Telegram Bot Token
- Сервісний акаунт Google Drive (для синхронізації документів)

### 1. Клонування
```bash
git clone https://github.com/EmetITteam/emet-bot.git
cd emet-bot
```

### 2. Налаштування .env
```bash
cp .env.example .env
# Заповнити: TELEGRAM_TOKEN, OPENAI_API_KEY, GEMINI_API_KEY, 
#            POSTGRES_*, ADMIN_ID, GOOGLE_SERVICE_ACCOUNT_JSON
```

### 3. Запуск
```bash
docker compose up -d
```
Бот стартує на polling, адмін-панель на http://localhost:5000

### 4. Перший запуск
1. Написати боту /start в Telegram
2. Зайти в адмін-панель (пароль: ADMIN_PASSWORD з .env)
3. Завантажити курси: Навчання → Upload xlsx
4. Синхронізувати базу знань: База знань → Sync

## Документація

| Документ | Опис |
|----------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Повна архітектура: RAG, LLM, БД, Docker |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | План розвитку, спринти, блокери |
| [RECOMMENDATIONS.md](RECOMMENDATIONS.md) | Рекомендації для розробника |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Деплой на сервер (покроково) |
| [docs/PROMPTS.md](docs/PROMPTS.md) | Опис 8 режимів бота з прикладами |

## Структура проекту

```
├── main.py              # Ядро бота (FSM, хендлери, RAG, dialog_state, knowledge_gaps detector)
├── admin_panel.py       # Flask адмін-панель (~26 маршрутів, /gaps, /quality з SD метриками)
├── classifier.py        # LLM classifier (19 інтентів, few-shot, product_canonical)
├── dialog_state.py      # NEW: per-turn state tracker (intent, product, comparison_target)
├── prompts.py           # Системні промпти
├── prompts_v2.py        # Модульні: BASE (anti-sycophancy + scope) + SOS / INFO / VERBATIM (STRICT-MODE) / COMBO / FEEDBACK
├── db.py                # PostgreSQL connection pool
├── sync_manager.py      # Google Drive → ChromaDB + scope tagging + retry + admin notify
├── quality_monitor.py   # LLM-judge + SD metrics + quality_history
├── scripts/             # Міграції, backup_indices.sh
├── tools/               # Імпорт курсів, шаблони, аналітика
├── tests/               # Тести + regression_fixtures.json + run_regression.py (eval harness)
├── courses/             # Excel-файли курсів
├── docs/                # Документація + scope_C_to_consider.md (відкладений backlog)
└── data/                # ChromaDB індекси (runtime, bind-mounted)
```

## Ключові можливості

- **8 режимів**: Sales Coach, База знань, LMS, Кейси, Операційні, Онбординг, Сертифікати, Комбо
- **3-zone RAG** (598 + 599 + 470 chunks) з product-locked retrieval (16 продуктів) і scope-метаданими
- **LLM classifier** з 19 інтентами + product detection + confidence
- **Triple LLM failover**: GPT-4o → Gemini → Claude (failover_depth tracked у logs)
- **DialogState** — скид chat_history при topic shift, comparison_target tracking
- **Anti-sycophancy** + auto-tracking виправлень → admin /gaps
- **LMS**: 13 курсів, 48 тем, тести з scoring
- **Quality Monitor**: LLM-judge на 10 діалогах/день + SD-метрики + еволюція по днях
- **Eval harness**: 15 fixture-кейсів регресій + async runner
- **Admin Panel**: дашборд, знання, курси, доступи (з role dropdown), /quality, /gaps
- **Auto-backup**: PG dump + ChromaDB tar (cron 02:00, 7-day rotation)

## Регресійні тести

Перед великими правками промптів/класифікатора:
```bash
# Швидкий smoke (~30 сек, лише classifier):
docker exec emet_bot_app python /app/tests/run_regression.py --no-generate

# Повний прогон (~3 хв, з LLM, ~$0.10):
docker exec emet_bot_app python /app/tests/run_regression.py
```

## Tech Stack

Python 3.11 | aiogram 3.x | Flask | PostgreSQL 16 | ChromaDB | LangChain | OpenAI | Google Gemini | Anthropic Claude | Docker Compose | GitHub Actions
