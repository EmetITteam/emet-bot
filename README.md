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
├── main.py              # Ядро бота (FSM, хендлери, RAG)
├── admin_panel.py       # Flask адмін-панель (23 маршрути)
├── prompts.py           # 5 системних промптів
├── db.py                # PostgreSQL connection pool
├── sync_manager.py      # Google Drive → ChromaDB
├── quality_monitor.py   # Щоденний аналіз якості
├── scripts/             # Міграції та одноразові скрипти
├── tools/               # Імпорт курсів, шаблони, аналітика
├── tests/               # Автотести
├── courses/             # Excel-файли курсів
├── docs/                # Документація
└── data/                # ChromaDB індекси (runtime)
```

## Ключові можливості

- **8 режимів**: Sales Coach, База знань, LMS, Кейси, Операційні, Онбординг, Сертифікати, Комбо
- **3-zone RAG**: Products / Competitors / Merge — залежно від запиту
- **Triple LLM failover**: GPT-4o → Gemini → Claude
- **LMS**: 14 курсів, 44 теми, тести з scoring
- **Quality Monitor**: щоденний звіт якості тільки адміну
- **Admin Panel**: дашборд, управління знаннями, курсами, доступами

## Tech Stack

Python 3.11 | aiogram 3.x | Flask | PostgreSQL 16 | ChromaDB | LangChain | OpenAI | Google Gemini | Anthropic Claude | Docker Compose | GitHub Actions
