import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

"""
Health Check — перевірка після деплою.

Запуск:
    python tests/health_check.py

Що перевіряє:
    1. .env існує і містить всі необхідні ключі
    2. Векторні індекси існують і не порожні
    3. З'єднання з PostgreSQL працює
    4. Telegram Bot token валідний (getMe)
    5. OpenAI API відповідає

Коли запускати:
    Після кожного деплою на сервер, щоб переконатись що всі компоненти живі.
    Запускати прямо на сервері або локально з правильним .env.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

REQUIRED_ENV = [
    "TELEGRAM_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ADMIN_ID",
    "DATABASE_URL",
]

REQUIRED_INDEXES = [
    "data/db_index_kb_openai",
    "data/db_index_coach_openai",
]

passed = 0
failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        msg = f"  ❌ {label}"
        if detail:
            msg += f"\n     → {detail}"
        print(msg)


# ------------------------------------------------------------------
# 1. ENV змінні
# ------------------------------------------------------------------
print(f"\n{'='*60}")
print("Health Check")
print(f"{'='*60}\n")

print("[ 1. ENV змінні ]")
for key in REQUIRED_ENV:
    val = os.getenv(key, "")
    check(key, bool(val), f"не знайдено в .env" if not val else "")

# ------------------------------------------------------------------
# 2. Векторні індекси
# ------------------------------------------------------------------
print("\n[ 2. Векторні індекси ]")
for index_path in REQUIRED_INDEXES:
    exists = os.path.isdir(index_path)
    if exists:
        # Перевіряємо що папка не порожня (є chroma.sqlite3)
        files = os.listdir(index_path)
        non_empty = len(files) > 0
        check(index_path, non_empty, "папка існує але порожня" if not non_empty else "")
    else:
        check(index_path, False, "папка не існує — потрібна пересборка індексу")

# ------------------------------------------------------------------
# 3. PostgreSQL
# ------------------------------------------------------------------
print("\n[ 3. PostgreSQL ]")
try:
    import db
    result = db.query("SELECT 1", fetchone=True)
    check("З'єднання з БД", result == (1,), f"неочікувана відповідь: {result}")

    tables = ["logs", "users", "courses", "allowed_emails"]
    for table in tables:
        try:
            count = db.query(f"SELECT COUNT(*) FROM {table}", fetchone=True)
            check(f"Таблиця {table} ({count[0]} рядків)", True)
        except Exception as e:
            check(f"Таблиця {table}", False, str(e))
except Exception as e:
    check("З'єднання з БД", False, str(e))

# ------------------------------------------------------------------
# 4. Telegram Bot
# ------------------------------------------------------------------
print("\n[ 4. Telegram Bot ]")
try:
    import httpx
    token = os.getenv("TELEGRAM_TOKEN", "")
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    data = resp.json()
    if data.get("ok"):
        username = data["result"].get("username", "?")
        check(f"Bot token (@{username})", True)
    else:
        check("Bot token", False, data.get("description", "невідома помилка"))
except Exception as e:
    check("Telegram API", False, str(e))

# ------------------------------------------------------------------
# 5. OpenAI API
# ------------------------------------------------------------------
print("\n[ 5. OpenAI API ]")
try:
    import httpx
    key = os.getenv("OPENAI_API_KEY", "")
    resp = httpx.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10
    )
    check("OpenAI API", resp.status_code == 200, f"HTTP {resp.status_code}" if resp.status_code != 200 else "")
except Exception as e:
    check("OpenAI API", False, str(e))

# ------------------------------------------------------------------
# Підсумок
# ------------------------------------------------------------------
total = passed + failed
print(f"\n{'='*60}")
print(f"Результат: {passed}/{total} перевірок пройшло")

if failed == 0:
    print("✅ ВСЕ ПРАЦЮЄ — деплой успішний")
elif failed <= 2:
    print("⚠️  Є незначні проблеми — перевір деталі вище")
else:
    print("❌ КРИТИЧНІ ПРОБЛЕМИ — бот може не працювати")

print(f"{'='*60}\n")

sys.exit(0 if failed == 0 else 1)
