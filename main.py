import asyncio
import os
import json
import db
import base64
import random
from datetime import datetime
import sync_manager
from openai import AsyncOpenAI
from google import genai
import anthropic
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import re

# RAG импорты для обеих систем
from langchain_openai import OpenAIEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_chroma import Chroma

# --- 1. НАСТРОЙКИ СИСТЕМЫ ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# --- ROLES & WHITELIST (з БД) ---
# Ролі: admin, manager, operator
# Якщо в таблиці users немає жодного запису → відкритий доступ (режим розробки)
# В продакшені: /adduser <id> [role] через адмін-команду

ROLES = {"admin", "manager", "operator", "director"}

def is_allowed(user_id: int) -> bool:
    """Перевіряє доступ: якщо є хоча б один користувач в БД — лише вони мають доступ."""
    try:
        total = db.query("SELECT COUNT(*) FROM users WHERE is_active=1", fetchone=True)
        if not total or total[0] == 0:
            return True  # відкритий доступ — таблиця порожня
        row = db.query("SELECT is_active FROM users WHERE user_id=%s", (str(user_id),), fetchone=True)
        return bool(row and row[0] == 1)
    except Exception:
        return True  # при помилці БД — не блокуємо

def get_user_role(user_id: int) -> str:
    """Повертає роль користувача ('admin'/'manager'/'operator'), або 'guest' якщо не знайдено."""
    try:
        row = db.query("SELECT role FROM users WHERE user_id=%s AND is_active=1", (str(user_id),), fetchone=True)
        return row[0] if row else "guest"
    except Exception:
        return "guest"

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or get_user_role(user_id) == "admin"

client_openai = AsyncOpenAI(api_key=OPENAI_KEY)
client_google = genai.Client(api_key=GEMINI_KEY)
client_claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

MODEL_OPENAI = "gpt-4o-mini"
MODEL_OPENAI_COACH = "gpt-4o"
MODEL_GOOGLE = "gemini-2.0-flash"
MODEL_CLAUDE = "claude-sonnet-4-6"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- 2. FSM ---
class UserState(StatesGroup):
    waiting_email = State()   # Очікування робочої пошти при першому вході
    mode_kb = State()
    mode_coach = State()
    mode_learning = State()
    mode_cases = State()       # Режим 4: Разбор кейсов
    mode_operational = State() # Режим 5: Операционные вопросы
    mode_onboarding = State()  # Режим 6: Онбординг
    lms_test_active = State()
    waiting_for_json = State()
    voice_confirm = State()  # Подтверждение распознанного голоса/фото

# --- 3. СИСТЕМНЫЕ ПРОМПТЫ ---
from prompts import SYSTEM_PROMPTS


# --- 4. ИНТЕРФЕЙС (МЕНЮ) ---
def get_main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 HR і регламенти", callback_data="set_kb")
    builder.button(text="💼 Sales Коуч", callback_data="set_coach")
    builder.button(text="🎓 Навчання і тести", callback_data="set_learn")
    builder.button(text="🔍 Розбір кейсів", callback_data="set_cases")
    builder.button(text="⚙️ Операційні питання", callback_data="set_operational")
    builder.button(text="🌱 Онбординг", callback_data="set_onboarding")
    builder.button(text="👤 Мій профіль", callback_data="show_profile")
    builder.adjust(1)
    return builder.as_markup()

# --- 5. БАЗА ДАННЫХ ---
def init_db():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            # Логи запросов
            cur.execute('''CREATE TABLE IF NOT EXISTS logs
                (id SERIAL PRIMARY KEY, date TEXT, user_id TEXT, username TEXT, mode TEXT, ai_engine TEXT,
                 question TEXT, answer TEXT, found_in_db INTEGER, model TEXT, tokens_in INTEGER, tokens_out INTEGER)''')

            # Курсы
            cur.execute('''CREATE TABLE IF NOT EXISTS courses
                (id SERIAL PRIMARY KEY,
                 title TEXT, description TEXT, data TEXT, created_at TEXT,
                 drive_file_id TEXT DEFAULT NULL, drive_modified TEXT DEFAULT NULL)''')

            # Темы курса
            cur.execute('''CREATE TABLE IF NOT EXISTS topics
                (id SERIAL PRIMARY KEY,
                 course_id INTEGER, order_num INTEGER, title TEXT, content TEXT,
                 is_certification INTEGER DEFAULT 0,
                 max_attempts INTEGER DEFAULT 0,
                 FOREIGN KEY(course_id) REFERENCES courses(id))''')
            # Міграція: додаємо колонки якщо їх ще немає (для існуючих БД)
            cur.execute("ALTER TABLE topics ADD COLUMN IF NOT EXISTS is_certification INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE topics ADD COLUMN IF NOT EXISTS max_attempts INTEGER DEFAULT 0")

            # Вопросы теста
            cur.execute('''CREATE TABLE IF NOT EXISTS questions
                (id SERIAL PRIMARY KEY,
                 topic_id INTEGER, text TEXT,
                 FOREIGN KEY(topic_id) REFERENCES topics(id))''')

            # Варианты ответов
            cur.execute('''CREATE TABLE IF NOT EXISTS answer_options
                (id SERIAL PRIMARY KEY,
                 question_id INTEGER, text TEXT, is_correct INTEGER,
                 FOREIGN KEY(question_id) REFERENCES questions(id))''')

            # Прогресс пользователей
            cur.execute('''CREATE TABLE IF NOT EXISTS user_progress
                (id SERIAL PRIMARY KEY,
                 user_id TEXT, course_id INTEGER, topic_id INTEGER,
                 passed INTEGER DEFAULT 0, score INTEGER DEFAULT 0,
                 attempts INTEGER DEFAULT 0, last_date TEXT,
                 UNIQUE(user_id, topic_id))''')

            # --- Пользователи ---
            cur.execute('''CREATE TABLE IF NOT EXISTS users
                (user_id TEXT PRIMARY KEY,
                 username TEXT,
                 first_name TEXT,
                 role TEXT DEFAULT 'manager',
                 level TEXT DEFAULT 'junior',
                 registered_at TEXT,
                 last_active TEXT,
                 is_active INTEGER DEFAULT 1)''')

            # --- Онбординг: шаблонные пункты чеклиста ---
            cur.execute('''CREATE TABLE IF NOT EXISTS onboarding_items
                (id SERIAL PRIMARY KEY,
                 day INTEGER,
                 order_num INTEGER,
                 title TEXT,
                 description TEXT,
                 item_type TEXT DEFAULT 'task')''')

            # --- Онбординг: прогресс по пользователям ---
            cur.execute('''CREATE TABLE IF NOT EXISTS onboarding_progress
                (id SERIAL PRIMARY KEY,
                 user_id TEXT,
                 item_id INTEGER,
                 completed INTEGER DEFAULT 0,
                 completed_at TEXT,
                 UNIQUE(user_id, item_id))''')

            # Sync state
            cur.execute('''CREATE TABLE IF NOT EXISTS sync_state
                (file_id TEXT PRIMARY KEY, file_name TEXT, modified_time TEXT, indexed_at TEXT,
                 uploaded_by TEXT, deleted_by TEXT, deleted_at TEXT)''')
            cur.execute("ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS uploaded_by TEXT")
            cur.execute("ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS deleted_by TEXT")
            cur.execute("ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS deleted_at TEXT")

            # Кошик — видалені файли (30 днів на відновлення)
            cur.execute('''CREATE TABLE IF NOT EXISTS deleted_chunks
                (id SERIAL PRIMARY KEY,
                 file_name TEXT,
                 file_id TEXT,
                 index_path TEXT,
                 chunks_json TEXT,
                 deleted_by TEXT,
                 deleted_at TIMESTAMP DEFAULT NOW(),
                 restore_deadline TIMESTAMP)''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deleted_chunks_deadline ON deleted_chunks(restore_deadline)")

            # Аудит-лог
            cur.execute('''CREATE TABLE IF NOT EXISTS audit_log
                (id SERIAL PRIMARY KEY,
                 user_id TEXT,
                 action TEXT,
                 details TEXT,
                 created_at TIMESTAMP DEFAULT NOW())''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)")

            # Дозволені email (для авторизації)
            cur.execute('''CREATE TABLE IF NOT EXISTS allowed_emails
                (id SERIAL PRIMARY KEY,
                 email TEXT UNIQUE,
                 role TEXT DEFAULT 'manager',
                 full_name TEXT,
                 activated_by_user_id TEXT,
                 activated_at TIMESTAMP,
                 added_at TIMESTAMP DEFAULT NOW())''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_allowed_emails_email ON allowed_emails(email)")


def seed_vitaran_course():
    """Загружает демо-курс по Vitaran если его ещё нет в БД"""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM courses WHERE title=%s",
                ("Vitaran — базовий курс продажів",)
            )
            if cur.fetchone():
                return

            cur.execute(
                "INSERT INTO courses (title, description, created_at) VALUES (%s, %s, %s) RETURNING id",
                (
                    "Vitaran — базовий курс продажів",
                    "Склад, показання, клінічне застосування та техніки продажу препарату Vitaran",
                    datetime.now().isoformat()
                )
            )
            course_id = cur.fetchone()[0]

            topics_data = [
                {
                    "title": "Що таке Vitaran?",
                    "content": (
                        "*Vitaran* — ін'єкційний препарат на основі *PDRN* (Polydeoxyribonucleotide) з концентрацією *2%*.\n\n"
                        "PDRN — натуральний полімер ДНК лосося, що стимулює регенерацію клітин через A2A-рецептори аденозину.\n\n"
                        "*Ключові ефекти:*\n"
                        "- Потужна регенерація тканин\n"
                        "- Відновлення шкіри після постакне та рубців\n"
                        "- Протизапальна дія\n"
                        "- Стимуляція синтезу власного колагену\n\n"
                        "*Відмінність від Petaran:*\n"
                        "- Vitaran 2% — вища концентрація, потужна регенерація\n"
                        "- Petaran 1% + ГК — регенерація + зволоження одночасно"
                    ),
                    "questions": [
                        {
                            "text": "Яка концентрація PDRN у препараті Vitaran?",
                            "options": [("1%", False), ("2%", True), ("3%", False), ("0,5%", False)]
                        },
                        {
                            "text": "З якої сировини виготовляється PDRN?",
                            "options": [
                                ("Стовбурові клітини людини", False),
                                ("ДНК лосося", True),
                                ("Гіалуронова кислота", False),
                                ("Рослинні пептиди", False)
                            ]
                        },
                        {
                            "text": "Чим принципово відрізняється Vitaran від Petaran?",
                            "options": [
                                ("Тільки ціною", False),
                                ("Vitaran — сильніша регенерація; Petaran = регенерація + зволоження", True),
                                ("Вони ідентичні за складом", False),
                                ("Vitaran для обличчя, Petaran — лише для тіла", False)
                            ]
                        }
                    ]
                },
                {
                    "title": "Показання та клінічне застосування",
                    "content": (
                        "*Основні показання до Vitaran:*\n"
                        "- Постакне та атрофічні рубці\n"
                        "- Розтяжки (стрії)\n"
                        "- Фотопошкодження та фотостаріння шкіри\n"
                        "- Відновлення після агресивних процедур (лазер, пілінг)\n"
                        "- Атрофічна в'яла шкіра\n\n"
                        "*Стандартний протокол:*\n"
                        "- Курс: *4–6 процедур*\n"
                        "- Інтервал між процедурами: *1–2 тижні*\n"
                        "- Зони: обличчя, шия, декольте, тіло\n"
                        "- Техніки: мезотерапія, мікропапули, лінійна техніка\n\n"
                        "*Важливо:* процедуру призначає та проводить виключно лікар-косметолог."
                    ),
                    "questions": [
                        {
                            "text": "Скільки процедур включає стандартний курс Vitaran?",
                            "options": [
                                ("1–2 процедури", False),
                                ("4–6 процедур", True),
                                ("10–12 процедур", False),
                                ("Кількість не обмежена", False)
                            ]
                        },
                        {
                            "text": "Яке основне показання до застосування Vitaran?",
                            "options": [
                                ("Збільшення об'єму губ", False),
                                ("Постакне та атрофічні рубці", True),
                                ("Корекція носогубних складок", False),
                                ("Підняття брів", False)
                            ]
                        }
                    ]
                },
                {
                    "title": "Продажі через цінність",
                    "content": (
                        "*Формула: ВЛАСТИВІСТЬ → ПЕРЕВАГА → ВИГОДА*\n\n"
                        "\"PDRN 2% стимулює власну регенерацію (властивість) → результат тримається довше аптечних засобів (перевага) → пацієнт повертається на повторний курс, середній чек клініки зростає (вигода для лікаря)\"\n\n"
                        "*Заперечення: 'Дорого'*\n"
                        "→ 'Аптечний аналог засвоюється на 30%, ін'єкційний PDRN — на 95%. Що насправді дорожче?'\n\n"
                        "*Комбінації для максимального чека:*\n"
                        "- Vitaran (ін'єкція) + ESSE (домашній догляд) + IUSE Collagen (добавка)\n"
                        "- Системний підхід = середній чек зростає в *3–5 разів*\n\n"
                        "*Болі лікаря, які закриває Vitaran:*\n"
                        "- Страх ускладнень → PDRN природного походження, мінімальний ризик\n"
                        "- Низький чек → протокол підвищує середній чек клініки\n"
                        "- Пацієнти не повертаються → курс 4–6 процедур = гарантовані повторні візити"
                    ),
                    "questions": [
                        {
                            "text": "Як правильно відповісти на заперечення 'занадто дорого'?",
                            "options": [
                                ("Запропонувати знижку", False),
                                ("Порівняти засвоєння 30% vs 95% і показати реальну економіку", True),
                                ("Пояснити що це преміум-клас і все", False),
                                ("Перейти до обговорення іншого продукту", False)
                            ]
                        },
                        {
                            "text": "Яка комбінація продуктів максимізує середній чек клініки?",
                            "options": [
                                ("Тільки Vitaran", False),
                                ("Vitaran + ESSE + IUSE Collagen", True),
                                ("Vitaran + Petaran", False),
                                ("Будь-яка комбінація рівноцінна", False)
                            ]
                        },
                        {
                            "text": "Що купує лікар з точки зору продажу через цінність?",
                            "options": [
                                ("Ін'єкційний препарат за ціною", False),
                                ("Бізнес-рішення: повторні візити, вищий чек, репутація клініки", True),
                                ("Засіб для регенерації шкіри", False),
                                ("Право на знижку для пацієнтів", False)
                            ]
                        }
                    ]
                }
            ]

            for order, topic_data in enumerate(topics_data, 1):
                cur.execute(
                    "INSERT INTO topics (course_id, order_num, title, content) VALUES (%s, %s, %s, %s) RETURNING id",
                    (course_id, order, topic_data["title"], topic_data["content"])
                )
                topic_id = cur.fetchone()[0]

                for q_data in topic_data["questions"]:
                    cur.execute(
                        "INSERT INTO questions (topic_id, text) VALUES (%s, %s) RETURNING id",
                        (topic_id, q_data["text"])
                    )
                    q_id = cur.fetchone()[0]
                    for opt_text, is_correct in q_data["options"]:
                        cur.execute(
                            "INSERT INTO answer_options (question_id, text, is_correct) VALUES (%s, %s, %s)",
                            (q_id, opt_text, int(is_correct))
                        )

            total_q = sum(len(t["questions"]) for t in topics_data)
            print(f"Демо-курс 'Vitaran' завантажено: {len(topics_data)} теми, {total_q} питань")


def seed_onboarding():
    """Загружает тестовый чеклист онбординга (первая рабочая неделя)."""
    row = db.query("SELECT COUNT(*) FROM onboarding_items", fetchone=True)
    if row[0] > 0:
        return  # Уже загружено

    items = [
        # День 1: Документы и доступы
        (1, 1, "Підписати трудовий договір та NDA", "Отримай документи в HR і підпиши до кінця дня.", "document"),
        (1, 2, "Отримати корпоративний email @emet.ua", "Звернись до IT-відділу (контакт у базі знань).", "task"),
        (1, 3, "Встановити Telegram і приєднатись до робочих чатів", "Запроси керівника додати тебе до чатів відділу.", "task"),
        (1, 4, "Ознайомитись із правилами компанії", "Запитай у Sales Coach: 'Правила компанії EMET'", "document"),
        (1, 5, "Познайомитись із командою представництва", "Зустріч з командою — організовує керівник.", "meeting"),

        # День 2: Продукти
        (2, 1, "Вивчити лінійку ін'єкційних препаратів EMET", "Запитай Sales Coach: 'Розкажи про препарати EMET'", "task"),
        (2, 2, "Пройти урок про Vitaran у розділі Навчання", "Відкрий 🎓 Навчання → Vitaran — базовий курс", "test"),
        (2, 3, "Зрозуміти різницю PDRN vs PCL vs філери", "Запитай Coach: 'Порівняй Vitaran, Ellansé і Neuramis'", "task"),
        (2, 4, "Ознайомитись із косметичною лінійкою", "Продукти: ESSE (пробіотична), IUSE Collagen, Magnox 520", "task"),

        # День 3: Продажі
        (3, 1, "Вивчити формулу продажу ВЛАСТИВІСТЬ → ПЕРЕВАГА → ВИГОДА", "Запитай Coach: 'Як продавати через цінність?'", "task"),
        (3, 2, "Відпрацювати заперечення 'Дорого'", "Запитай Coach: 'Відпрацюй заперечення дорого для Ellansé'", "task"),
        (3, 3, "Відпрацювати заперечення 'Є постачальник'", "Запитай Coach: 'Є постачальник — як відповісти?'", "task"),
        (3, 4, "Пройти рольову гру з лікарем-скептиком", "Запитай Coach: 'Зіграй лікаря-скептика, я продаю Vitaran'", "task"),

        # День 4: Операційні питання
        (4, 1, "Дізнатись порядок оформлення відрядження", "Запитай базу знань: 'Як оформити відрядження?'", "document"),
        (4, 2, "Зрозуміти порядок оформлення повернень", "Запитай базу знань: 'Порядок повернення препаратів'", "document"),
        (4, 3, "Ознайомитись зі стандартами семінарів", "Запитай базу знань: 'Стандарти проведення семінару EMET'", "document"),
        (4, 4, "Підготувати план першої зустрічі з лікарем", "Запитай Coach: 'Підготуй скрипт першої зустрічі з косметологом'", "task"),

        # День 5: Підсумки
        (5, 1, "Пройти тест по Vitaran (мін. 70%)", "Відкрий 🎓 Навчання → Vitaran → Пройти тест", "test"),
        (5, 2, "Зробити SOS-тренування з Coach", "Запитай Coach: 'SOS: мені зустрічатись з лікарем через 10 хвилин'", "task"),
        (5, 3, "Заповнити чеклист першого тижня та надіслати керівнику", "Після виконання всіх пунктів — повідом керівника.", "task"),
    ]

    db.executemany(
        "INSERT INTO onboarding_items (day, order_num, title, description, item_type) VALUES (%s,%s,%s,%s,%s)",
        items
    )
    print(f"Онбординг-чеклист завантажено: {len(items)} пунктів")


# --- 5b. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД ---
def log_to_db(user_id, username, mode, ai_engine, question, answer, has_source, model=None, tokens_in=0, tokens_out=0):
    try:
        db.execute(
            "INSERT INTO logs (date, user_id, username, mode, ai_engine, question, answer, found_in_db, model, tokens_in, tokens_out) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, username,
             mode, ai_engine, question, answer, 1 if has_source else 0, model, tokens_in, tokens_out)
        )
    except Exception as e:
        print(f"Ошибка записи в БД: {e}")


def save_course(title, json_data):
    try:
        db.execute(
            "INSERT INTO courses (title, data, created_at) VALUES (%s, %s, %s)",
            (title, json_data, datetime.now().isoformat())
        )
    except Exception as e:
        print(f"Ошибка сохранения курса: {e}")


def get_courses():
    return db.query("SELECT id, title, description FROM courses ORDER BY id")


def get_topics(course_id):
    return db.query(
        "SELECT id, order_num, title, is_certification, max_attempts FROM topics WHERE course_id=%s ORDER BY order_num",
        (course_id,)
    )


def get_topic_content(topic_id):
    return db.query(
        "SELECT title, content, is_certification, max_attempts FROM topics WHERE id=%s",
        (topic_id,), fetchone=True
    )


def get_questions(topic_id):
    questions = db.query("SELECT id, text FROM questions WHERE topic_id=%s", (topic_id,))
    result = []
    for q_id, q_text in questions:
        options = db.query("SELECT id, text, is_correct FROM answer_options WHERE question_id=%s", (q_id,))
        result.append({
            "id": q_id,
            "text": q_text,
            "options": [{"id": o[0], "text": o[1], "is_correct": o[2]} for o in options]
        })
    return result


def get_user_progress(user_id, topic_id):
    return db.query("SELECT passed, score, attempts FROM user_progress WHERE user_id=%s AND topic_id=%s", (str(user_id), topic_id), fetchone=True)


def save_user_progress(user_id, course_id, topic_id, passed, score):
    db.execute("""
        INSERT INTO user_progress (user_id, course_id, topic_id, passed, score, attempts, last_date)
        VALUES (%s, %s, %s, %s, %s, 1, %s)
        ON CONFLICT(user_id, topic_id) DO UPDATE SET
            passed = GREATEST(user_progress.passed, EXCLUDED.passed),
            score = GREATEST(user_progress.score, EXCLUDED.score),
            attempts = user_progress.attempts + 1,
            last_date = EXCLUDED.last_date
    """, (str(user_id), course_id, topic_id, int(passed), score, datetime.now().isoformat()))


# --- 5c. ФУНКЦИИ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ И ОНБОРДИНГА ---

def upsert_user(user_id, username, first_name):
    db.execute("""
        INSERT INTO users (user_id, username, first_name, registered_at, last_active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(user_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name,
            last_active = EXCLUDED.last_active
    """, (str(user_id), username, first_name,
          datetime.now().isoformat(), datetime.now().isoformat()))


def get_user(user_id):
    return db.query("SELECT user_id, username, first_name, role, level, registered_at FROM users WHERE user_id=%s", (str(user_id),), fetchone=True)


def audit_action(user_id, action: str, details: str = None):
    """Записує дію в аудит-лог (не кидає виключень)."""
    try:
        db.execute(
            "INSERT INTO audit_log (user_id, action, details) VALUES (%s, %s, %s)",
            (str(user_id), action, details)
        )
    except Exception as e:
        print(f"[audit] помилка запису: {e}")


def get_onboarding_items():
    return db.query("SELECT id, day, order_num, title, description, item_type FROM onboarding_items ORDER BY day, order_num")


def get_onboarding_progress(user_id):
    rows = db.query("SELECT item_id, completed FROM onboarding_progress WHERE user_id=%s", (str(user_id),))
    return {r[0]: r[1] for r in rows}


def toggle_onboarding_item(user_id, item_id):
    existing = db.query("SELECT completed FROM onboarding_progress WHERE user_id=%s AND item_id=%s", (str(user_id), item_id), fetchone=True)
    if existing:
        new_val = 1 - existing[0]
        db.execute(
            "UPDATE onboarding_progress SET completed=%s, completed_at=%s WHERE user_id=%s AND item_id=%s",
            (new_val, datetime.now().isoformat() if new_val else None, str(user_id), item_id)
        )
    else:
        db.execute(
            "INSERT INTO onboarding_progress (user_id, item_id, completed, completed_at) VALUES (%s,%s,1,%s)",
            (str(user_id), item_id, datetime.now().isoformat())
        )


def get_user_level(user_id):
    """Рассчитывает уровень пользователя на основе результатов тестов."""
    rows = db.query("SELECT passed, score FROM user_progress WHERE user_id=%s", (str(user_id),))
    if not rows:
        return "🌱 Новачок", 0, 0
    total = len(rows)
    passed = sum(1 for r in rows if r[0])
    avg_score = round(sum(r[1] for r in rows) / total)
    pct_passed = round(passed / total * 100)
    if pct_passed > 80 and avg_score > 85:
        level = "⭐️ Senior"
    elif pct_passed >= 50 and avg_score >= 70:
        level = "💼 Middle"
    elif passed > 0:
        level = "📈 Junior"
    else:
        level = "🌱 Новачок"
    return level, pct_passed, avg_score


# --- 6. МОДУЛИ ВЕКТОРНОГО ПОИСКА ---
def _extract_docs(docs):
    context_text = ""
    sources = {}
    grouped_docs = {}

    for doc in docs:
        name = doc.metadata.get("source", "Неизвестный документ")
        url = doc.metadata.get("url", "")
        content = doc.page_content
        if name not in grouped_docs:
            grouped_docs[name] = {"url": url, "content": []}
        grouped_docs[name]["content"].append(content)

    for i, (name, data) in enumerate(grouped_docs.items(), 1):
        doc_id = f"REF{i}"
        full_content = "\n".join(data["content"])
        context_text += f"=== ИСТОЧНИК: {doc_id} | НАЗВАНИЕ: {name} ===\n{full_content}\n\n"
        sources[doc_id] = {"name": name, "url": data["url"]}

    return context_text, sources


_vdb_kb_openai    = None
_vdb_coach_openai = None
_vdb_kb_google    = None
_vdb_coach_google = None


def get_context_openai(query, mode="kb"):
    global _vdb_kb_openai, _vdb_coach_openai
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
    if mode in ("coach", "combo"):
        if _vdb_coach_openai is None:
            _vdb_coach_openai = Chroma(persist_directory="data/db_index_coach_openai", embedding_function=embeddings)
        if mode == "combo":
            return _extract_docs(_vdb_coach_openai.similarity_search(query, k=15, filter={"category": "combo"}))
        return _extract_docs(_vdb_coach_openai.similarity_search(query, k=20))
    else:
        # kb, cases, operational — все используют kb-индекс
        if _vdb_kb_openai is None:
            _vdb_kb_openai = Chroma(persist_directory="data/db_index_kb_openai", embedding_function=embeddings)
        return _extract_docs(_vdb_kb_openai.similarity_search(query, k=15))


def get_context_google(query, mode="kb"):
    global _vdb_kb_google, _vdb_coach_google
    embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001", google_api_key=GEMINI_KEY)
    if mode in ("coach", "combo"):
        if _vdb_coach_google is None:
            _vdb_coach_google = Chroma(persist_directory="data/db_index_coach_google", embedding_function=embeddings)
        if mode == "combo":
            return _extract_docs(_vdb_coach_google.similarity_search(query, k=15, filter={"category": "combo"}))
        return _extract_docs(_vdb_coach_google.similarity_search(query, k=20))
    else:
        if _vdb_kb_google is None:
            _vdb_kb_google = Chroma(persist_directory="data/db_index_kb_google", embedding_function=embeddings)
        return _extract_docs(_vdb_kb_google.similarity_search(query, k=15))


async def detect_intent(query: str) -> str:
    try:
        response = await client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": (
                "Ты — маршрутизатор запросов EMET. Классифицируй запрос ОДНИМ словом.\n"
                "Работает на любом языке (UA/RU).\n\n"
                "ПРАВИЛА:\n"
                "- 'kb' — регламенты компании, HR, отпуска, CRM, структура, зарплаты, ИТ-доступы, "
                "правила работы, внутренние документы и процедуры EMET.\n"
                "- 'coach' — всё что касается ПРОДУКТОВ EMET: препараты (Vitaran, Ellanse, Petaran, "
                "Neuramis, Exoxe, Neuronox, IUSE, Esse, Magnox, PDRN, PCL, филлеры, ботулотоксин), "
                "их состав, показания, применение, дозировки, отличия; А ТАКЖЕ продажи, скрипты, "
                "возражения, переговоры с врачами, косметология.\n"
                "- 'cases' — разбор конкретного диалога/ситуации с клиентом, анализ ошибок встречи.\n"
                "- 'operational' — командировки, возврат товара, семинары, SLA, оформление расходов.\n\n"
                "Отвечай только одним словом: kb, coach, cases или operational."
            )},
            {"role": "user", "content": query}],
            temperature=0.0, max_tokens=10
        )
        result = response.choices[0].message.content.strip().lower()
        return result if result in ("kb", "coach", "cases", "operational") else "kb"
    except Exception:
        return "kb"


async def prepare_search_query(user_query: str) -> str:
    try:
        response = await client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "system",
                "content": "Переведи запрос пользователя на украинский и русский языки, добавь 2-3 синонима. "
                           "Выдай всё одной строкой через пробел. Это нужно для поиска по базе."
            },
            {"role": "user", "content": user_query}],
            temperature=0.0, max_tokens=60
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return user_query


# --- 7. ЯДРО RAG (выделено в отдельную функцию для переиспользования) ---
async def process_text_query(text: str, message: types.Message, state: FSMContext):
    """Основная RAG-логика. Принимает text явно (для голоса/фото/текста)."""
    t = text.lower().strip()

    greetings = ["привет", "здравствуйте", "добрый день", "привіт", "добрий день"]
    if t in greetings:
        return await message.answer("Здравствуйте! Я готов к работе. Задайте ваш вопрос.")

    # Перехват вопросов о возможностях бота
    self_query_keywords = [
        "что ты умеешь", "что умеешь", "что ты можешь", "что можешь",
        "расскажи о себе", "кто ты", "что такое этот бот", "как ты работаешь",
        "як ти працюєш", "що ти вмієш", "що ти можеш", "розкажи про себе",
        "хто ти", "функционал", "функціонал", "возможности бота", "можливості бота",
    ]
    if any(kw in t for kw in self_query_keywords):
        return await message.answer(
            "Привіт! Я — AI-асистент команди ЕМЕТ 👋\n\n"
            "Допомагаю менеджерам працювати швидше та продавати впевненіше:\n\n"
            "*💼 Коуч з продажів*\n"
            "Скрипти, відповіді на заперечення, порівняння з конкурентами, питання які продають — просто опиши ситуацію.\n\n"
            "*🆘 SOS прямо під час візиту*\n"
            "Лікар каже «дорого» або «є кращий аналог» — пишеш мені, я даю готову фразу за секунди.\n\n"
            "*🔍 База знань*\n"
            "Регламенти, структура компанії, CRM, відпустки — відповідаю на будь-яке внутрішнє запитання.\n\n"
            "*🎓 Навчання*\n"
            "Курси з препаратів з тестами та фіксацією результатів.\n\n"
            "*🎤🖼 Голос і фото*\n"
            "Надсилай голосове або скриншот — розпізнаю і відповім.\n\n"
            "_Питай будь-що — сам визначу що тобі потрібно._",
            parse_mode="Markdown"
        )

    await bot.send_chat_action(message.chat.id, "typing")

    # Читаем историю ДО detect_intent — нужна для форс-роутинга скрипт-запросов
    state_data = await state.get_data()
    chat_history = state_data.get("chat_history", [])

    # Ключевые слова запросов на скрипт/диалог
    _SCRIPT_KEYWORDS_EARLY = [
        "дай диалог", "дай діалог", "дай скрипт", "скрипт з лікарем",
        "діалог з лікарем", "диалог с врачом", "розіграй діалог",
        "зіграй діалог", "покажи діалог", "покажи диалог",
    ]
    _t_lower_early = text.lower().strip()
    _is_script_early = any(kw in _t_lower_early for kw in _SCRIPT_KEYWORDS_EARLY)

    # Авторозмітка по контенту запиту (перемикання між режимами)
    state_data_early = await state.get_data()
    if state_data_early.get("combo_mode"):
        mode_key = "combo"
    elif _is_script_early and chat_history:
        mode_key = "coach"
    else:
        detected_mode = await detect_intent(text)
        mode_key = detected_mode

    state_map = {
        "coach": UserState.mode_coach,
        "cases": UserState.mode_cases,
        "operational": UserState.mode_operational,
    }
    await state.set_state(state_map.get(mode_key, UserState.mode_kb))

    search_query = await prepare_search_query(text)

    # --- Python-level детекция продукта + возражения (чтобы LLM не переспрашивал) ---
    _EMET_PRODUCTS = [
        "ellanse", "elanse", "еланс", "елансе", "элансе", "эллансе", "ellanсе",
        "neuramis", "нейрамис", "нейраміс",
        "vitaran", "вітаран", "витаран",
        "petaran", "петаран",
        "exoxe", "экзокс", "экзосомы", "ексоксе",
        "esse", "эссе", "ессе",
        "vitaran skin", "вітаран скін",
        "iuse collagen", "iuse", "айюз", "июз",
        "magnox", "магнокс",
    ]
    # Канонические имена продуктов для подстановки в системное сообщение
    _PRODUCT_CANONICAL = {
        "ellanse": "Ellansé", "elanse": "Ellansé", "еланс": "Ellansé",
        "елансе": "Ellansé", "элансе": "Ellansé", "эллансе": "Ellansé", "ellanсе": "Ellansé",
        "neuramis": "Neuramis", "нейрамис": "Neuramis", "нейраміс": "Neuramis",
        "vitaran": "Vitaran", "вітаран": "Vitaran", "витаран": "Vitaran",
        "petaran": "Petaran", "петаран": "Petaran",
        "exoxe": "EXOXE", "экзокс": "EXOXE", "экзосомы": "EXOXE", "ексоксе": "EXOXE",
        "esse": "ESSE", "эссе": "ESSE", "ессе": "ESSE",
        "vitaran skin": "Vitaran Skin Healer", "вітаран скін": "Vitaran Skin Healer",
        "iuse collagen": "IUSE Collagen", "iuse": "IUSE Collagen",
        "айюз": "IUSE Collagen", "июз": "IUSE Collagen",
        "magnox": "Magnox", "магнокс": "Magnox",
    }
    _OBJECTION_KEYWORDS = [
        "дорого", "дорогой", "дорога", "дорогую", "дорогое",
        "не вірю", "не верю", "подумаю", "подумать",
        "є дешевше", "есть дешевле", "дешевле",
        "не впевнений", "не уверен",
        "не потрібно", "не нужно",
    ]
    _SCRIPT_KEYWORDS = [
        "дай диалог", "дай діалог", "дай скрипт", "скрипт з лікарем",
        "діалог з лікарем", "диалог с врачом", "розіграй діалог",
        "зіграй діалог", "покажи діалог", "покажи диалог",
    ]
    t_lower = t
    _detected_product = next((p for p in _EMET_PRODUCTS if p in t_lower), None)
    _has_objection = any(kw in t_lower for kw in _OBJECTION_KEYWORDS)
    _is_script_request = any(kw in t_lower for kw in _SCRIPT_KEYWORDS)

    # Если запрос на скрипт/диалог — ищем продукт И возражение в USER-сообщениях истории
    _history_objection = None
    if mode_key == "coach" and _is_script_request and chat_history:
        user_msgs = [m for m in chat_history if m["role"] == "user"]
        for msg in reversed(user_msgs):
            msg_lower = msg["content"].lower()
            # Ищем продукт если ещё не найден
            if not _detected_product:
                found = next((p for p in _EMET_PRODUCTS if p in msg_lower), None)
                if found:
                    _detected_product = found
            # Ищем возражение если ещё не найдено
            if not _history_objection and any(kw in msg_lower for kw in _OBJECTION_KEYWORDS):
                # Сохраняем весь текст сообщения с возражением как контекст
                _history_objection = msg["content"].strip()
            # Если уже нашли и продукт и возражение — выходим
            if _detected_product and _history_objection:
                break

    if _detected_product:
        _canonical = _PRODUCT_CANONICAL.get(_detected_product, _detected_product)
    else:
        _canonical = None

    if mode_key == "coach" and _detected_product and _has_objection:
        # Заперечення + продукт → Формат А
        llm_user_text = f"[СИСТЕМА: продукт вказано — {_canonical}. Використовуй ФОРМАТ А, НЕ питай уточнення]\n\nВОПРОС:\n{text}"
    elif mode_key == "coach" and _is_script_request and _canonical:
        # Запит на скрипт/діалог → Формат В з конкретним продуктом + можливим запереченням з історії
        if _history_objection:
            llm_user_text = (
                f"[СИСТЕМА: дай ФОРМАТ В — скрипт-діалог менеджера з лікарем про {_canonical}. "
                f"Контекст: менеджер вже стикнувся з запереченням «{_history_objection}» — "
                f"діалог має відпрацьовувати саме це заперечення]\n\nВОПРОС:\n{text}"
            )
        else:
            llm_user_text = f"[СИСТЕМА: дай ФОРМАТ В — скрипт-діалог менеджера з лікарем про {_canonical}]\n\nВОПРОС:\n{text}"
    else:
        llm_user_text = text

    ai_used = "OpenAI"
    context, sources = "", {}
    answer = None
    _tokens_in = 0
    _tokens_out = 0
    _model_used = None
    _context_was_empty = False

    # Отправляем placeholder — пользователь сразу видит что бот думает
    sent_msg = await message.answer("⏳")

    try:
        loop = asyncio.get_running_loop()
        context, sources = await loop.run_in_executor(None, get_context_openai, search_query, mode_key)
        if not context.strip():
            _context_was_empty = True
        _model = MODEL_OPENAI_COACH if mode_key in ("coach", "combo") else MODEL_OPENAI
        stream = await client_openai.chat.completions.create(
            model=_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPTS[mode_key]},
                *chat_history,
                {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nВОПРОС:\n{llm_user_text}"}
            ],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = []
        last_edit = asyncio.get_event_loop().time()
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                chunks.append(delta)
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 0.8:
                    try:
                        await sent_msg.edit_text("".join(chunks))
                    except Exception:
                        pass
                    last_edit = now
            if chunk.usage:
                _tokens_in = chunk.usage.prompt_tokens
                _tokens_out = chunk.usage.completion_tokens
        answer = "".join(chunks)
        _model_used = _model
    except Exception as e_openai:
        print(f"OpenAI недоступен: {e_openai}")

    if answer is None:
        ai_used = "Google"
        try:
            loop = asyncio.get_running_loop()
            context, sources = await loop.run_in_executor(None, get_context_google, search_query, mode_key)
            # Для Google строим историю как текст
            history_text = ""
            for msg in chat_history:
                role = "Менеджер" if msg["role"] == "user" else "Коуч"
                history_text += f"{role}: {msg['content']}\n"
            prompt = f"{SYSTEM_PROMPTS[mode_key]}\n\n{history_text}КОНТЕКСТ:\n{context}\n\nВОПРОС:\n{llm_user_text}"
            res = client_google.models.generate_content(model=MODEL_GOOGLE, contents=prompt)
            answer = res.text
            if hasattr(res, "usage_metadata") and res.usage_metadata:
                _tokens_in = getattr(res.usage_metadata, "prompt_token_count", 0) or 0
                _tokens_out = getattr(res.usage_metadata, "candidates_token_count", 0) or 0
            _model_used = MODEL_GOOGLE
        except Exception as e_google:
            print(f"Google недоступен: {e_google}")

    if answer is None and client_claude:
        ai_used = "Claude"
        try:
            loop = asyncio.get_running_loop()
            context, sources = await loop.run_in_executor(None, get_context_openai, search_query, mode_key)
            claude_msg = await client_claude.messages.create(
                model=MODEL_CLAUDE,
                max_tokens=1024,
                system=SYSTEM_PROMPTS[mode_key],
                messages=[
                    *chat_history,
                    {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nВОПРОС:\n{llm_user_text}"}
                ]
            )
            answer = claude_msg.content[0].text
            _tokens_in = claude_msg.usage.input_tokens
            _tokens_out = claude_msg.usage.output_tokens
            _model_used = MODEL_CLAUDE
        except Exception as e_claude:
            print(f"Claude недоступен: {e_claude}")

    if answer is None:
        await sent_msg.edit_text("Извините, серверы ИИ сейчас перегружены. Попробуйте через минуту.")
        return

    # Собираем ссылки на источники
    used_links = []
    if sources:
        for doc_id, data in sources.items():
            if doc_id in answer:
                used_links.append(f"📄 [{data['name']}]({data['url']})")
        if not used_links and "нужные документы" in answer.lower():
            for doc_id, data in sources.items():
                used_links.append(f"📄 [{data['name']}]({data['url']})")

    # Убираем метки REF перед отправкой
    answer = re.sub(r'\[?REF\d+\]?', '', answer).strip()
    answer = answer.replace("  ", " ")

    if used_links:
        final_links = list(set(used_links))
        answer += "\n\n*Ознакомиться с документами:*\n" + "\n".join(final_links)

    _uname = message.from_user.username or f"id{message.from_user.id}"
    log_to_db(message.from_user.id, _uname, mode_key, ai_used, text, answer, bool(used_links), _model_used, _tokens_in, _tokens_out)

    # Сохраняем историю диалога (только coach-режим, т.к. он многоходовой)
    if mode_key == "coach":
        clean_answer = re.sub(r'\[?REF\d+\]?', '', answer).strip()
        chat_history.append({"role": "user", "content": text})
        chat_history.append({"role": "assistant", "content": clean_answer})
        chat_history = chat_history[-6:]  # храним последние 3 обмена
        await state.update_data(chat_history=chat_history)

    if _context_was_empty and mode_key == "kb":
        try:
            await bot.send_message(
                ADMIN_ID,
                f"*Пропуск в базе знаний!*\n@{message.from_user.username} спросил:\n_{text}_"
            )
        except Exception as admin_err:
            print(f"Ошибка уведомления админа: {admin_err}")

    await send_paginated(message, state, answer, sent_msg=sent_msg)

    # Після відповіді в режимі Coach — показуємо меню Coach знову
    if mode_key == "coach":
        builder = InlineKeyboardBuilder()
        builder.button(text="💬 Новий запит", callback_data="coach_free")
        builder.button(text="🆘 SOS", callback_data="coach_sos")
        builder.button(text="🗣 Заперечення", callback_data="coach_objections")
        builder.button(text="🔗 Комбо", callback_data="coach_combo")
        builder.button(text="📜 Сертифікати", callback_data="coach_certs")
        builder.button(text="🗓 Сезонні скрипти", callback_data="coach_seasonal")
        builder.button(text="🏠 Головне меню", callback_data="go_home")
        builder.adjust(2, 2, 2, 1)
        await message.answer("Що далі?", reply_markup=builder.as_markup())


# --- 8. LMS ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def send_question(message: types.Message, questions: list, index: int):
    """Отправляет вопрос теста с вариантами ответов."""
    q = questions[index]
    total = len(questions)

    options = list(q["options"])
    random.shuffle(options)

    builder = InlineKeyboardBuilder()
    for opt in options:
        builder.button(
            text=opt["text"],
            callback_data=f"lms_answer_{opt['id']}_{int(opt['is_correct'])}"
        )
    builder.adjust(1)

    question_text = f"*Вопрос {index + 1}/{total}*\n\n{q['text']}"
    try:
        await message.answer(question_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        await message.answer(question_text, reply_markup=builder.as_markup())


# --- 8b. ПАГИНАЦИЯ ДЛИННЫХ ОТВЕТОВ ---
_PAGE_LIMIT = 3800  # безопасный порог ниже лимита Telegram 4096


def split_message(text: str, limit: int = _PAGE_LIMIT) -> list[str]:
    """Разбивает текст на страницы по абзацам, не разрывая слова."""
    if len(text) <= limit:
        return [text]
    pages = []
    while len(text) > limit:
        split_at = text.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        pages.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        pages.append(text)
    return pages


async def send_paginated(message: types.Message, state: FSMContext, text: str, sent_msg=None):
    """Отправляет ответ: если длинный — по страницам с кнопкой 'Далі →'.
    Если передан sent_msg — редактирует его вместо отправки нового сообщения."""
    pages = split_message(text)

    async def _send_first(content, **kwargs):
        if sent_msg:
            try:
                await sent_msg.edit_text(content, **kwargs)
                return
            except Exception:
                pass
        await message.answer(content, **kwargs)

    if len(pages) == 1:
        try:
            await _send_first(pages[0], parse_mode="Markdown", disable_web_page_preview=True)
        except Exception:
            await _send_first(pages[0], disable_web_page_preview=True)
        return

    await state.update_data(pages=pages, page_idx=0)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📖 Далі → (1/{len(pages)})", callback_data="page_next")
    try:
        await _send_first(pages[0], parse_mode="Markdown", disable_web_page_preview=True,
                          reply_markup=builder.as_markup())
    except Exception:
        await _send_first(pages[0], disable_web_page_preview=True,
                          reply_markup=builder.as_markup())


# --- 8c. AI-АНАЛІЗ РЕЗУЛЬТАТІВ ТЕСТУ ---
async def send_test_analysis(message: types.Message, wrong_answers: list, score_pct: int, passed: bool):
    """Генерує персоналізований аналіз результатів тесту через GPT."""
    try:
        wrong_text = "\n".join(
            f"- Питання: {w['question']}\n  Правильна відповідь: {w['correct']}"
            for w in wrong_answers
        )
        prompt = (
            f"Учень пройшов тест з результатом {score_pct}% ({'склав' if passed else 'не склав'}).\n"
            f"Неправильні відповіді:\n{wrong_text}\n\n"
            "Дай коротку персоналізовану оцінку українською мовою у такому форматі:\n"
            "**💪 Сильні сторони** (1-2 речення — що добре засвоєно)\n"
            "**⚠️ Слабкі сторони** (конкретні теми/поняття з неправильних відповідей)\n"
            "**📌 Рекомендації** (2-3 конкретних кроки для покращення)\n\n"
            "Відповідь — лише аналіз, без зайвих вступів. Максимум 200 слів."
        )
        response = await client_openai.chat.completions.create(
            model=MODEL_OPENAI,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.4,
        )
        analysis = response.choices[0].message.content.strip()
        await message.answer(f"📊 *Аналіз результатів*\n\n{analysis}", parse_mode="Markdown")
    except Exception as e:
        # Якщо GPT недоступний — мовчки пропускаємо
        print(f"[test_analysis] помилка: {e}")


# --- 9. ТЕЛЕГРАМ ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    # Перевірка: чи вже авторизований
    existing = db.query("SELECT is_active FROM users WHERE user_id=%s", (str(user_id),), fetchone=True)
    if existing and existing[0] == 1:
        # Вже авторизований — показуємо меню
        upsert_user(user_id, message.from_user.username, message.from_user.first_name)
        name = message.from_user.first_name or message.from_user.username or "колего"
        await state.set_state(UserState.mode_kb)
        await message.answer(
            f"Вітаю, {name}! 👋 Я — AI-асистент EMET.\n\nОберіть розділ:",
            reply_markup=get_main_menu()
        )
        return

    # Перевірка: чи є взагалі дозволені email в системі
    email_count = db.query("SELECT COUNT(*) FROM allowed_emails", fetchone=True)
    if not email_count or email_count[0] == 0:
        # Список email ще не завантажено — відкритий доступ (режим розробки)
        upsert_user(user_id, message.from_user.username, message.from_user.first_name)
        name = message.from_user.first_name or message.from_user.username or "колего"
        await state.set_state(UserState.mode_kb)
        await message.answer(
            f"Вітаю, {name}! 👋 Я — AI-асистент EMET.\n\nОберіть розділ:",
            reply_markup=get_main_menu()
        )
        return

    # Новий користувач — просимо email
    await state.set_state(UserState.waiting_email)
    await message.answer(
        "👋 Вітаю в AI-асистенті *EMET*!\n\n"
        "Для отримання доступу введіть вашу *корпоративну електронну пошту*:",
        parse_mode="Markdown"
    )


@dp.message(StateFilter(UserState.waiting_email))
async def handle_email_input(message: types.Message, state: FSMContext):
    if not message.text:
        return
    email = message.text.strip().lower()

    # Базова перевірка формату email
    import re
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        await message.answer("❌ Невірний формат пошти. Введіть email у форматі *name@company.ua*", parse_mode="Markdown")
        return

    # Перевірка в allowed_emails
    row = db.query(
        "SELECT id, role, full_name, activated_by_user_id FROM allowed_emails WHERE email=%s",
        (email,), fetchone=True
    )
    if not row:
        await message.answer(
            "⛔ Ця пошта не знайдена в системі.\n\n"
            "Зверніться до керівника або адміністратора для отримання доступу."
        )
        return

    email_id, role, full_name, already_activated_by = row

    # Перевірка: чи email вже використовується іншим акаунтом
    if already_activated_by and already_activated_by != str(message.from_user.id):
        await message.answer(
            "⚠️ Ця пошта вже прив'язана до іншого акаунту.\n"
            "Зверніться до адміністратора."
        )
        return

    # Авторизація — створюємо/оновлюємо користувача
    user_id = message.from_user.id
    db.execute("""
        INSERT INTO users (user_id, username, first_name, role, registered_at, last_active, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username, first_name=EXCLUDED.first_name,
            role=EXCLUDED.role, is_active=1, last_active=EXCLUDED.last_active
    """, (str(user_id), message.from_user.username, message.from_user.first_name,
          role, datetime.now().isoformat(), datetime.now().isoformat()))

    # Прив'язуємо email до telegram-акаунту
    db.execute(
        "UPDATE allowed_emails SET activated_by_user_id=%s, activated_at=NOW() WHERE id=%s",
        (str(user_id), email_id)
    )
    audit_action(user_id, "email_auth", f"email={email} role={role}")

    name = full_name or message.from_user.first_name or message.from_user.username or "колего"
    await state.set_state(UserState.mode_kb)
    await message.answer(
        f"✅ Доступ надано, {name}!\n\n"
        f"Ваша роль: *{role}*\n\n"
        "Оберіть розділ:",
        parse_mode="Markdown",
        reply_markup=get_main_menu()
    )


@dp.callback_query(F.data == "page_next")
async def page_next_handler(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pages = data.get("pages", [])
    page_idx = data.get("page_idx", 0) + 1

    if page_idx >= len(pages):
        await callback.answer("Це остання сторінка")
        return

    await state.update_data(page_idx=page_idx)
    is_last = page_idx == len(pages) - 1

    await callback.message.delete_reply_markup()

    markup = None
    if not is_last:
        builder = InlineKeyboardBuilder()
        builder.button(text=f"📖 Далі → ({page_idx + 1}/{len(pages)})", callback_data="page_next")
        markup = builder.as_markup()

    try:
        await callback.message.answer(pages[page_idx], parse_mode="Markdown",
                                      disable_web_page_preview=True, reply_markup=markup)
    except Exception:
        await callback.message.answer(pages[page_idx], disable_web_page_preview=True,
                                      reply_markup=markup)
    await callback.answer()


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "*AI-асистент EMET*\n\n"
        "*🔍 HR і регламенти*\n"
        "Корпоративні правила, відпустки, структура компанії, документи.\n\n"
        "*💼 Sales Коуч*\n"
        "Препарати, склади, порівняння, скрипти продажів.\n"
        "Режими: вільний діалог, 🆘 SOS-підготовка, робота із запереченнями, сезонні скрипти.\n\n"
        "*🎓 Навчання і тести*\n"
        "Курси по продуктах з уроками та тестами. Є звичайні та 🏆 сертифікаційні тести.\n"
        "Після тесту — AI-аналіз: сильні/слабкі сторони та рекомендації.\n\n"
        "*🔍 Розбір кейсів*\n"
        "Аналіз реальних ситуацій з клієнтами та лікарями.\n\n"
        "*⚙️ Операційні питання*\n"
        "Логістика, замовлення, документообіг, склад.\n\n"
        "*🌱 Онбординг*\n"
        "Чеклист першого тижня для нових співробітників.\n\n"
        "*🎤 Голос і фото*\n"
        "Надішли голосове повідомлення або скріншот — розпізнаю і відповім.\n\n"
        "Натисніть /start щоб відкрити меню."
    )
    await message.answer(help_text, parse_mode="Markdown")


# --- АДМІН: УПРАВЛІННЯ КОРИСТУВАЧАМИ ---
@dp.message(Command("adduser"))
async def cmd_adduser(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Тільки адміністратор.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("Використання: /adduser <user_id> [role]\nРолі: admin, director, operator, manager")
    try:
        uid = int(parts[1])
    except ValueError:
        return await message.answer("❌ user_id має бути числом.")
    role = parts[2].lower() if len(parts) >= 3 else "manager"
    if role not in ROLES:
        return await message.answer(f"❌ Невідома роль. Доступні: {', '.join(ROLES)}")
    db.execute("""
        INSERT INTO users (user_id, role, registered_at, last_active, is_active)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT(user_id) DO UPDATE SET role=EXCLUDED.role, is_active=1, last_active=EXCLUDED.last_active
    """, (str(uid), role, datetime.now().isoformat(), datetime.now().isoformat()))
    audit_action(message.from_user.id, "adduser", f"uid={uid} role={role}")
    await message.answer(f"✅ Користувач `{uid}` доданий з роллю *{role}*.", parse_mode="Markdown")


@dp.message(Command("removeuser"))
async def cmd_removeuser(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Тільки адміністратор.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("Використання: /removeuser <user_id>")
    try:
        uid = int(parts[1])
    except ValueError:
        return await message.answer("❌ user_id має бути числом.")
    db.execute("UPDATE users SET is_active=0 WHERE user_id=%s", (str(uid),))
    audit_action(message.from_user.id, "removeuser", f"uid={uid}")
    await message.answer(f"✅ Доступ для `{uid}` відкликано.", parse_mode="Markdown")


@dp.message(Command("listusers"))
async def cmd_listusers(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Тільки адміністратор.")
    rows = db.query("SELECT user_id, username, first_name, role, is_active FROM users ORDER BY registered_at DESC")
    if not rows:
        return await message.answer("Список користувачів порожній.")
    lines = []
    for uid, uname, fname, role, active in rows:
        status = "✅" if active else "🚫"
        name = fname or uname or uid
        lines.append(f"{status} `{uid}` — {name} [{role}]")
    await message.answer("*Користувачі бота:*\n\n" + "\n".join(lines), parse_mode="Markdown")


@dp.message(Command("team"))
async def cmd_team(message: types.Message):
    role = get_user_role(message.from_user.id)
    if role not in ("admin", "director") and not is_admin(message.from_user.id):
        return await message.answer("⛔ Команда доступна тільки для директора та адміністратора.")

    managers = db.query(
        "SELECT user_id, first_name, username FROM users WHERE role='manager' AND is_active=1",
    )
    if not managers:
        return await message.answer("👥 Менеджерів у системі ще немає.")

    # Кількість тем/онбординг для розрахунку %
    total_topics = db.query("SELECT COUNT(*) FROM topics", fetchone=True)
    total_topics = total_topics[0] if total_topics else 1
    total_onb = db.query("SELECT COUNT(*) FROM onboarding_items", fetchone=True)
    total_onb = total_onb[0] if total_onb else 1

    lines = ["👥 *Прогрес команди менеджерів*\n"]
    for uid, fname, uname in managers:
        name = fname or uname or uid

        # Тести
        test_row = db.query(
            "SELECT COUNT(*), SUM(passed), COALESCE(AVG(score),0) FROM user_progress WHERE user_id=%s",
            (str(uid),), fetchone=True
        )
        tests_done = test_row[0] or 0
        tests_passed = int(test_row[1] or 0)
        avg_score = float(test_row[2] or 0)

        # Онбординг
        onb_row = db.query(
            "SELECT SUM(completed) FROM onboarding_progress WHERE user_id=%s",
            (str(uid),), fetchone=True
        )
        onb_done = int(onb_row[0] or 0) if onb_row else 0
        onb_pct = round(onb_done / total_onb * 100) if total_onb else 0

        lines.append(
            f"👤 *{name}*\n"
            f"  🎓 Тести: {tests_passed}/{tests_done} складено, ср. бал {avg_score:.0f}%\n"
            f"  🌱 Онбординг: {onb_done}/{total_onb} ({onb_pct}%)\n"
        )

    await message.answer("\n".join(lines), parse_mode="Markdown")


# --- ПОДТВЕРЖДЕНИЕ ГОЛОСА / ФОТО ---
async def _ask_voice_confirm(message: types.Message, state: FSMContext, text: str):
    """Показывает распознанный запрос и просит подтвердить перед поиском."""
    await state.update_data(pending_query=text)
    await state.set_state(UserState.voice_confirm)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Верно, искать", callback_data="voice_confirm_yes")
    builder.button(text="✏️ Задать иначе", callback_data="voice_confirm_no")
    builder.adjust(2)

    msg = "Ваш запрос:\n" + f"*{text}*" + "\n\nВсё верно?"
    try:
        await message.answer(msg, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        await message.answer("Ваш запрос:\n" + text + "\n\nВсё верно?", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "voice_confirm_yes", StateFilter(UserState.voice_confirm))
async def voice_confirmed(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("pending_query", "")
    await callback.message.delete_reply_markup()
    await callback.answer()
    await process_text_query(text, callback.message, state)


@dp.callback_query(F.data == "voice_confirm_no", StateFilter(UserState.voice_confirm))
async def voice_rejected(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete_reply_markup()
    await callback.answer()
    await state.set_state(UserState.mode_kb)
    await callback.message.answer("Напишите ваш вопрос текстом.")


# --- ГОЛОСОВЫЕ СООБЩЕНИЯ ---
@dp.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        file = await bot.get_file(message.voice.file_id)
        voice_bytes = await bot.download_file(file.file_path)

        transcript = await client_openai.audio.transcriptions.create(
            model="whisper-1",
            file=("voice.ogg", voice_bytes.read(), "audio/ogg"),
        )
        text = transcript.text.strip()

        if not text:
            return await message.answer("Не удалось распознать голосовое сообщение. Попробуйте ещё раз.")

        await _ask_voice_confirm(message, state, text)

    except Exception as e:
        print(f"Ошибка распознавания голоса: {e}")
        await message.answer("Не удалось обработать голосовое сообщение.")


# --- ФОТО / СКРИНШОТЫ ---
@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = await bot.download_file(file.file_path)

        img_b64 = base64.b64encode(photo_bytes.read()).decode()
        caption = message.caption or "Опиши что на скриншоте и сформулируй вопрос для поиска в базе знаний компании."

        response = await client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Пользователь прислал скриншот. Его вопрос/контекст: '{caption}'.\n"
                            "Опиши что ты видишь на изображении и сформулируй один конкретный текстовый запрос "
                            "для поиска ответа в корпоративной базе знаний. Только запрос, без лишних слов."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                    }
                ]
            }],
            max_tokens=150
        )

        extracted_query = response.choices[0].message.content.strip()
        await _ask_voice_confirm(message, state, extracted_query)

    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        await message.answer("Не удалось обработать изображение. Попробуйте описать вопрос текстом.")


# --- ПЕРЕКЛЮЧЕНИЕ РЕЖИМОВ ---
@dp.callback_query(F.data == "set_kb")
async def mode_kb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_kb)
    await callback.message.answer("🔍 Режим: *База знаний*. Задайте вопрос по регламентам компании.", parse_mode="Markdown")
    await callback.answer()


def get_coach_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Вільний діалог", callback_data="coach_free")
    builder.button(text="🆘 SOS: швидка підготовка", callback_data="coach_sos")
    builder.button(text="🗣 Робота з запереченнями", callback_data="coach_objections")
    builder.button(text="🔗 Комбо-протоколи", callback_data="coach_combo")
    builder.button(text="📜 Сертифікати", callback_data="coach_certs")
    builder.button(text="🗓 Сезонні скрипти", callback_data="coach_seasonal")
    builder.button(text="🏠 Головне меню", callback_data="go_home")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data == "set_coach")
async def mode_coach(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    await callback.message.answer(
        "💼 *Sales Коуч EMET*\n\nОберіть режим або напишіть своє питання:",
        parse_mode="Markdown",
        reply_markup=get_coach_menu()
    )
    await callback.answer()


@dp.callback_query(F.data == "coach_free")
async def coach_free(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    await callback.message.answer("💬 Вільний діалог. Опишіть запит клієнта або заперечення.")
    await callback.answer()


@dp.callback_query(F.data == "coach_sos")
async def coach_sos(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    await callback.message.answer(
        "🆘 *SOS — швидка підготовка*\n\n"
        "Напишіть ситуацію, наприклад:\n"
        "• _«Зустріч з лікарем через 10 хвилин»_\n"
        "• _«Клієнт питає чому Vitaran дорожче конкурентів»_\n"
        "• _«Лікар незадоволений попередньою партією»_\n\n"
        "Отримаєте короткий скрипт і ключові аргументи.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "coach_objections")
async def coach_objections(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    await callback.message.answer(
        "🗣 *Робота з запереченнями*\n\n"
        "Напишіть заперечення клієнта, наприклад:\n"
        "• _«Дорого»_\n"
        "• _«Вже працюємо з іншим постачальником»_\n"
        "• _«Потрібно подумати»_\n\n"
        "Отримаєте готові відповіді та техніки.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "coach_combo")
async def coach_combo(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[], combo_mode=True)
    await callback.message.answer(
        "🔗 *Комбо-протоколи*\n\n"
        "Напишіть препарат або завдання, наприклад:\n"
        "• _«Комбо з Vitaran»_\n"
        "• _«Що поєднати з Ellansé для омолодження»_\n"
        "• _«Протокол для постакне»_\n\n"
        "Отримаєте рекомендований комбо-протокол з аргументами.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "coach_certs")
async def coach_certs(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    await callback.message.answer(
        "📜 *Сертифікати*\n\n"
        "Напишіть назву препарату або компанії, наприклад:\n"
        "• _«Сертифікат Vitaran»_\n"
        "• _«Документи на Ellansé»_\n"
        "• _«Реєстрація препаратів ЕМЕТ»_\n\n"
        "Отримаєте інформацію з бази знань.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "coach_seasonal")
async def coach_seasonal(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    await callback.message.answer(
        "🗓 *Сезонні скрипти*\n\n"
        "Напишіть тему або сезон, наприклад:\n"
        "• _«Весняне оновлення — реклама біоревіталізації»_\n"
        "• _«Літо — захист та відновлення»_\n"
        "• _«Кінець року — акції та знижки»_\n\n"
        "Отримаєте готовий сезонний скрипт.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "go_home")
async def go_home(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_kb)
    await callback.message.answer("Главное меню:", reply_markup=get_main_menu())
    await callback.answer()


# --- LMS: СПИСОК КУРСОВ ---
@dp.callback_query(F.data == "set_learn")
async def show_courses(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_learning)
    courses = get_courses()

    if not courses:
        await callback.message.answer("🎓 Курсов пока нет. Обратитесь к администратору.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for c_id, title, _ in courses:
        builder.button(text=f"📚 {title}", callback_data=f"lms_course_{c_id}")
    builder.button(text="⬅️ Главное меню", callback_data="go_home")
    builder.adjust(1)

    await callback.message.answer(
        "*🎓 Обучение ЭМЕТ*\n\nВыберите курс:",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


# --- LMS: ТЕМЫ КУРСА ---
@dp.callback_query(F.data.startswith("lms_course_"))
async def show_topics(callback: types.CallbackQuery, state: FSMContext):
    course_id = int(callback.data.split("_")[-1])
    await state.update_data(lms_course_id=course_id)

    topics = get_topics(course_id)
    user_id = callback.from_user.id

    builder = InlineKeyboardBuilder()
    for t_id, order_num, title, is_cert, max_att in topics:
        progress = get_user_progress(user_id, t_id)
        icon = "🏆" if is_cert else "⬜"
        if progress and progress[0]:
            icon = "✅"
            label = f"{icon} {order_num}. {title} ({progress[1]}%)"
        elif progress and is_cert and max_att and progress[2] >= max_att:
            label = f"🔒 {order_num}. {title} — вичерпано спроби"
        elif progress:
            label = f"🔄 {order_num}. {title}"
        else:
            label = f"{icon} {order_num}. {title}"
        builder.button(text=label, callback_data=f"lms_topic_{t_id}")

    builder.button(text="⬅️ К курсам", callback_data="set_learn")
    builder.adjust(1)

    await callback.message.answer(
        "*Темы курса:*\n\n✅ — пройдено  🔄 — начато  ⬜ — не начато",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


# --- LMS: ПРОСМОТР УРОКА ---
@dp.callback_query(F.data.startswith("lms_topic_"))
async def show_lesson(callback: types.CallbackQuery, state: FSMContext):
    topic_id = int(callback.data.split("_")[-1])
    await state.update_data(lms_topic_id=topic_id)

    topic = get_topic_content(topic_id)
    if not topic:
        await callback.answer("Тема не найдена", show_alert=True)
        return

    title, content, is_cert, max_att = topic
    progress = get_user_progress(callback.from_user.id, topic_id)
    attempts_used = progress[2] if progress else 0

    data = await state.get_data()
    course_id = data.get('lms_course_id', 0)

    builder = InlineKeyboardBuilder()
    cert_label = " 🏆 *Сертифікаційний тест*" if is_cert else ""
    attempts_info = ""

    if is_cert and max_att:
        left = max(0, max_att - attempts_used)
        attempts_info = f"\n\n⚠️ Залишилось спроб: *{left}/{max_att}*"
        if left == 0:
            builder.button(text="🔒 Спроби вичерпано", callback_data="cert_no_attempts")
        elif progress and progress[0]:
            builder.button(text=f"🏆 Складено ({progress[1]}%)", callback_data="cert_already_passed")
        else:
            builder.button(text=f"📝 Скласти тест ({left} спроби залишилось)", callback_data=f"lms_starttest_{topic_id}")
    elif progress and progress[0]:
        builder.button(
            text=f"✅ Пройдено ({progress[1]}%) — пройти знову",
            callback_data=f"lms_starttest_{topic_id}"
        )
    else:
        builder.button(text="📝 Пройти тест", callback_data=f"lms_starttest_{topic_id}")

    builder.button(text="⬅️ До тем", callback_data=f"lms_course_{course_id}")
    builder.adjust(1)

    lesson_text = f"*{title}*{cert_label}\n\n{content}{attempts_info}"
    try:
        await callback.message.answer(lesson_text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        await callback.message.answer(lesson_text, reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "cert_no_attempts")
async def cert_no_attempts(callback: types.CallbackQuery):
    await callback.answer("🔒 Спроби на цей сертифікаційний тест вичерпано. Зверніться до адміністратора.", show_alert=True)


@dp.callback_query(F.data == "cert_already_passed")
async def cert_already_passed(callback: types.CallbackQuery):
    await callback.answer("🏆 Ви вже склали цей тест! Результат зафіксовано.", show_alert=True)


# --- LMS: НАЧАТЬ ТЕСТ ---
@dp.callback_query(F.data.startswith("lms_starttest_"))
async def start_test(callback: types.CallbackQuery, state: FSMContext):
    topic_id = int(callback.data.split("_")[-1])
    questions = get_questions(topic_id)

    if not questions:
        await callback.answer("Питання для цієї теми не знайдено", show_alert=True)
        return

    # Перевірка ліміту спроб для сертифікаційного тесту
    topic = get_topic_content(topic_id)
    if topic:
        _, _, is_cert, max_att = topic
        if is_cert and max_att:
            progress = get_user_progress(callback.from_user.id, topic_id)
            attempts_used = progress[2] if progress else 0
            if attempts_used >= max_att:
                await callback.answer(
                    f"🔒 Спроби вичерпано ({attempts_used}/{max_att}). Зверніться до адміністратора.",
                    show_alert=True
                )
                return
            if progress and progress[0]:
                await callback.answer("🏆 Ви вже склали цей сертифікаційний тест!", show_alert=True)
                return

    data = await state.get_data()
    course_id = data.get('lms_course_id', 0)
    await state.update_data(
        lms_questions=questions,
        lms_q_index=0,
        lms_correct=0,
        lms_wrong_answers=[],
        lms_topic_id=topic_id,
        lms_course_id=course_id
    )
    await state.set_state(UserState.lms_test_active)
    await callback.answer()

    cert_note = " 🏆 *Сертифікаційний тест*\n" if (topic and topic[2]) else ""
    await callback.message.answer(
        f"{cert_note}*Тест*: {len(questions)} питань. Для заліку потрібно *70%* і вище.\n\nПочинаємо!",
        parse_mode="Markdown"
    )
    await send_question(callback.message, questions, 0)


# --- LMS: ОТВЕТ НА ВОПРОС ---
@dp.callback_query(F.data.startswith("lms_answer_"), StateFilter(UserState.lms_test_active))
async def handle_answer(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    is_correct = int(parts[-1])

    data = await state.get_data()
    questions = data['lms_questions']
    q_index = data['lms_q_index']
    correct_count = data.get('lms_correct', 0)
    wrong_answers = data.get('lms_wrong_answers', [])  # список неверных ответов

    await callback.answer()

    current_q = questions[q_index]
    if is_correct:
        correct_count += 1
        feedback = "✅ *Вірно!*"
    else:
        correct_text = next(
            (opt["text"] for opt in current_q["options"] if opt["is_correct"]),
            "—"
        )
        feedback = f"❌ *Невірно.*\nПравильна відповідь: _{correct_text}_"
        wrong_answers.append({
            "question": current_q["text"],
            "correct": correct_text
        })

    try:
        await callback.message.answer(feedback, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(feedback)

    next_index = q_index + 1
    total = len(questions)
    await state.update_data(lms_q_index=next_index, lms_correct=correct_count, lms_wrong_answers=wrong_answers)

    if next_index >= total:
        score_pct = round(correct_count / total * 100)
        passed = score_pct >= 70

        topic_id = data.get('lms_topic_id')
        course_id = data.get('lms_course_id')
        save_user_progress(callback.from_user.id, course_id, topic_id, passed, score_pct)

        result_icon = "🎉" if passed else "📚"
        result_text = (
            f"{result_icon} *Результат тесту*\n\n"
            f"Правильних відповідей: *{correct_count}/{total}*\n"
            f"Результат: *{score_pct}%*\n\n"
        )
        result_text += (
            "✅ Тест складено! Можете переходити до наступної теми."
            if passed else
            "❌ Потрібно мінімум *70%* для заліку. Спробуйте ще раз — матеріал завжди доступний."
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="📋 До тем курсу", callback_data=f"lms_course_{course_id}")
        builder.adjust(1)

        await state.set_state(UserState.mode_learning)
        try:
            await callback.message.answer(result_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        except Exception:
            await callback.message.answer(result_text, reply_markup=builder.as_markup())

        # AI-аналіз результатів (async, не блокує)
        if wrong_answers:
            asyncio.create_task(send_test_analysis(callback.message, wrong_answers, score_pct, passed))
    else:
        await send_question(callback.message, questions, next_index)


# --- LMS: перехват текста во время теста ---
@dp.message(StateFilter(UserState.lms_test_active))
async def handle_text_in_test(message: types.Message):
    if message.text:
        await message.answer("Выберите вариант ответа из кнопок выше.")


# --- АДМИН: ЗАГРУЗКА КУРСА ЧЕРЕЗ JSON ---
@dp.callback_query(F.data == "admin_upload")
async def ask_json(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("У вас нет прав администратора.", show_alert=True)
    await callback.message.answer("Отправьте JSON-файл с обучающим курсом.")
    await state.set_state(UserState.waiting_for_json)


@dp.message(UserState.waiting_for_json, F.document)
async def process_json(message: types.Message, state: FSMContext):
    file = await bot.get_file(message.document.file_id)
    content = await bot.download_file(file.file_path)
    json_str = content.read().decode('utf-8')
    try:
        data = json.loads(json_str)
        save_course(data.get('title', 'Без названия'), json_str)
        await message.answer(f"✅ Курс '{data.get('title')}' загружен!")
        await state.set_state(UserState.mode_kb)
    except Exception as e:
        await message.answer(f"❌ Ошибка валидации JSON: {e}")


# --- РЕЖИМ 4: РАЗБОР КЕЙСОВ ---
@dp.callback_query(F.data == "set_cases")
async def mode_cases(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_cases)
    await callback.message.answer(
        "🔍 *Режим: Розбір кейсів*\n\n"
        "Опиши ситуацію або встав текст реального діалогу з лікарем — я вкажу на помилки і дам виправлений варіант.",
        parse_mode="Markdown"
    )
    await callback.answer()


# --- РЕЖИМ 5: ОПЕРАЦИОННЫЕ ВОПРОСЫ ---
@dp.callback_query(F.data == "set_operational")
async def mode_operational(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_operational)
    await callback.message.answer(
        "⚙️ *Режим: Операційні питання*\n\n"
        "Тут — відрядження, повернення, семінари, SLA, юридичні обмеження.\n"
        "Задай питання або вибери тему:",
        parse_mode="Markdown",
        reply_markup=_get_operational_menu()
    )
    await callback.answer()


def _get_operational_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="✈️ Відрядження", callback_data="op_topic_trip")
    builder.button(text="↩️ Повернення товару", callback_data="op_topic_return")
    builder.button(text="📋 Семінари та заходи", callback_data="op_topic_events")
    builder.button(text="⏱ SLA та терміни", callback_data="op_topic_sla")
    builder.button(text="⬅️ Головне меню", callback_data="go_home")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data.startswith("op_topic_"))
async def op_topic_shortcut(callback: types.CallbackQuery, state: FSMContext):
    topic_map = {
        "op_topic_trip": "Як оформити відрядження? Які документи потрібні і терміни відшкодування?",
        "op_topic_return": "Як оформити повернення препарату? Процедура, терміни, відповідальна особа.",
        "op_topic_events": "Стандарти проведення семінару EMET. Що підготувати, вимоги до закупівель.",
        "op_topic_sla": "SLA EMET — терміни виконання різних типів запитів.",
    }
    query = topic_map.get(callback.data, "")
    await state.set_state(UserState.mode_operational)
    await callback.answer()
    if query:
        await process_text_query(query, callback.message, state)


# --- РЕЖИМ 6: ОНБОРДИНГ ---
@dp.callback_query(F.data == "set_onboarding")
async def mode_onboarding(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_onboarding)
    user_id = callback.from_user.id
    items = get_onboarding_items()
    progress = get_onboarding_progress(user_id)

    total = len(items)
    done = sum(1 for it in items if progress.get(it[0]))
    pct = round(done / total * 100) if total else 0

    builder = InlineKeyboardBuilder()
    for day in range(1, 6):
        day_items = [it for it in items if it[1] == day]
        day_done = sum(1 for it in day_items if progress.get(it[0]))
        status = "✅" if day_done == len(day_items) else ("🔄" if day_done > 0 else "⬜")
        builder.button(
            text=f"{status} День {day} ({day_done}/{len(day_items)})",
            callback_data=f"onb_day_{day}"
        )
    builder.button(text="⬅️ Головне меню", callback_data="go_home")
    builder.adjust(1)

    await callback.message.answer(
        f"🌱 *Онбординг — перший тиждень*\n\n"
        f"Прогрес: *{done}/{total}* пунктів виконано ({pct}%)\n\n"
        f"✅ — день завершено  🔄 — в процесі  ⬜ — не розпочато\n\n"
        f"Вибери день:",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("onb_day_"))
async def onboarding_day(callback: types.CallbackQuery, state: FSMContext):
    day = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    items = [it for it in get_onboarding_items() if it[1] == day]
    progress = get_onboarding_progress(user_id)

    type_icons = {"task": "📌", "document": "📄", "test": "📝", "meeting": "🤝"}
    day_names = {1: "День 1 — Документи і доступи", 2: "День 2 — Продукти EMET",
                 3: "День 3 — Техніки продажу", 4: "День 4 — Операційні питання",
                 5: "День 5 — Підсумки та тести"}

    builder = InlineKeyboardBuilder()
    for it in items:
        item_id, _, _, title, _, item_type = it
        done = progress.get(item_id, 0)
        icon = "✅" if done else type_icons.get(item_type, "⬜")
        builder.button(
            text=f"{icon} {title}",
            callback_data=f"onb_item_{item_id}"
        )
    builder.button(text="⬅️ До списку днів", callback_data="set_onboarding")
    builder.adjust(1)

    done_count = sum(1 for it in items if progress.get(it[0]))
    await callback.message.answer(
        f"*{day_names.get(day, f'День {day}')}*\n\n"
        f"Виконано: *{done_count}/{len(items)}*\n\n"
        f"Натисни на пункт щоб відмітити як виконаний / скасувати.\n"
        f"📌 задача  📄 документ  📝 тест  🤝 зустріч",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("onb_item_"))
async def onboarding_item_toggle(callback: types.CallbackQuery, state: FSMContext):
    item_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    toggle_onboarding_item(user_id, item_id)

    # Получаем описание пункта
    item = db.query("SELECT day, title, description FROM onboarding_items WHERE id=%s", (item_id,), fetchone=True)

    progress = get_onboarding_progress(user_id)
    is_done = progress.get(item_id, 0)
    status = "✅ Відмічено як виконане" if is_done else "↩️ Відмітку знято"

    builder = InlineKeyboardBuilder()
    builder.button(text=f"⬅️ Назад до Дня {item[0]}", callback_data=f"onb_day_{item[0]}")
    builder.adjust(1)

    await callback.message.answer(
        f"{status}\n\n*{item[1]}*\n\n💡 _{item[2]}_",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer(status)


# --- ПРОФИЛЬ ---
@dp.callback_query(F.data == "show_profile")
async def show_profile_callback(callback: types.CallbackQuery, state: FSMContext):
    await _send_profile(callback.from_user, callback.message)
    await callback.answer()


@dp.message(Command("profile"))
async def cmd_profile(message: types.Message, state: FSMContext):
    await _send_profile(message.from_user, message)


async def _send_profile(user, message):
    user_id = user.id
    level, pct_passed, avg_score = get_user_level(user_id)

    # Прогресс онбординга
    items = get_onboarding_items()
    progress = get_onboarding_progress(user_id)
    onb_done = sum(1 for it in items if progress.get(it[0]))
    onb_total = len(items)

    # Результаты тестов
    test_rows = db.query(
        "SELECT t.title, up.score, up.passed, up.attempts FROM user_progress up "
        "JOIN topics t ON t.id = up.topic_id WHERE up.user_id=%s ORDER BY up.last_date DESC",
        (str(user_id),)
    )

    name = user.first_name or user.username or f"id{user_id}"
    username_str = f"@{user.username}" if user.username else f"id{user_id}"

    text = (
        f"👤 *Профіль менеджера*\n\n"
        f"*Ім'я:* {name} ({username_str})\n"
        f"*Рівень:* {level}\n\n"
        f"📊 *Статистика тестів:*\n"
        f"Пройдено тем: *{len(test_rows)}*\n"
    )
    if test_rows:
        text += f"Середній бал: *{avg_score}%*\n"
        text += f"Тестів зараховано: *{pct_passed}%*\n\n"
        text += "*Останні результати:*\n"
        for title, score, passed, attempts in test_rows[:5]:
            icon = "✅" if passed else "❌"
            text += f"{icon} {title[:30]} — {score}%\n"
    else:
        text += "\nТестів ще не пройдено. Спробуй *🎓 Навчання*!\n"

    text += (
        f"\n🌱 *Онбординг:* {onb_done}/{onb_total} пунктів виконано\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🌱 Перейти до онбордингу", callback_data="set_onboarding")
    builder.button(text="🎓 Перейти до навчання", callback_data="set_learn")
    builder.button(text="⬅️ Головне меню", callback_data="go_home")
    builder.adjust(1)

    try:
        await message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        await message.answer(text, reply_markup=builder.as_markup())


# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
@dp.message(StateFilter(
    UserState.mode_kb, UserState.mode_coach, UserState.mode_learning,
    UserState.mode_cases, UserState.mode_operational, UserState.mode_onboarding, None
))
async def handle_message(message: types.Message, state: FSMContext):
    if not message.text:
        return
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ заборонено. Зверніться до адміністратора EMET.")
        return
    await process_text_query(message.text, message, state)


# --- 10. НАСТРОЙКА КОМАНД TELEGRAM ---
async def set_bot_commands(bot: Bot):
    commands = [
        types.BotCommand(command="start", description="Головне меню"),
        types.BotCommand(command="help", description="Довідка по боту"),
        types.BotCommand(command="profile", description="Мій профіль та прогрес"),
        types.BotCommand(command="team", description="[director] Прогрес команди менеджерів"),
        types.BotCommand(command="adduser", description="[admin] Додати користувача"),
        types.BotCommand(command="removeuser", description="[admin] Відкликати доступ"),
        types.BotCommand(command="listusers", description="[admin] Список користувачів"),
    ]
    await bot.set_my_commands(commands)


async def ttl_cleanup_task():
    """Щодня о 03:00 видаляє старі логи (>90 днів) та аудит-записи (>90 днів)."""
    from datetime import date, timedelta
    while True:
        now = datetime.now()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            cutoff = (date.today() - timedelta(days=90)).isoformat()
            db.execute("DELETE FROM logs WHERE date < %s", (cutoff + " 00:00:00",))
            db.execute("DELETE FROM audit_log WHERE created_at < NOW() - INTERVAL '90 days'")
            db.execute("DELETE FROM deleted_chunks WHERE restore_deadline < NOW()")
            print(f"[TTL] Очищено логи та кошик старіше 30/90 днів")
        except Exception as e:
            print(f"[TTL] помилка: {e}")


async def weekly_digest_task():
    """Щопонеділка о 09:00 надсилає дайджест адміну та всім активним менеджерам."""
    from datetime import date, timedelta
    while True:
        now = datetime.now()
        days_until_monday = (7 - now.weekday()) % 7 or 7
        if now.weekday() == 0 and now.hour < 9:
            days_until_monday = 0
        next_monday = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
        await asyncio.sleep(max((next_monday - now).total_seconds(), 60))

        try:
            week_ago = (date.today() - timedelta(days=7)).isoformat()
            today_s  = date.today().isoformat()

            rows = db.query_dict(
                "SELECT * FROM logs WHERE date >= %s AND date <= %s",
                (week_ago + " 00:00:00", today_s + " 23:59:59")
            )
            total       = len(rows)
            uniq_users  = len(set(r["user_id"] for r in rows))
            found_count = sum(1 for r in rows if r.get("found_in_db") == 1)
            found_pct   = round(found_count / total * 100) if total else 0

            by_mode = {}
            for r in rows:
                m = r.get("mode") or "unknown"
                by_mode[m] = by_mode.get(m, 0) + 1
            mode_lines = "\n".join(
                f"  • {m}: {c}" for m, c in sorted(by_mode.items(), key=lambda x: x[1], reverse=True)
            )

            # Прогрес навчання за тиждень
            test_rows = db.query(
                "SELECT COUNT(*) as total, SUM(passed) as passed FROM user_progress "
                "WHERE last_date >= %s", (week_ago,)
            )
            tests_total  = test_rows[0][0] if test_rows else 0
            tests_passed = int(test_rows[0][1] or 0) if test_rows else 0
            tests_pct    = round(tests_passed / tests_total * 100) if tests_total else 0

            text = (
                f"📊 *Щотижневий дайджест EMET Bot*\n"
                f"_{week_ago} — {today_s}_\n\n"
                f"👥 Активних користувачів: *{uniq_users}*\n"
                f"💬 Всього запитів: *{total}*\n"
                f"🎯 Знайдено в базі: *{found_pct}%* ({found_count}/{total})\n\n"
                f"🎓 *Навчання за тиждень:*\n"
                f"  • Спроб тестів: *{tests_total}*\n"
                f"  • Складено: *{tests_passed}* ({tests_pct}%)\n\n"
                f"📋 *По режимах:*\n{mode_lines if mode_lines else '  —'}\n\n"
                f"_Сформовано: {datetime.now().strftime('%d.%m.%Y %H:%M')}_"
            )

            # Відправляємо адміну
            await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")

            # Відправляємо всім активним менеджерам і адмінам (крім ADMIN_ID — вже отримав)
            recipients = db.query(
                "SELECT user_id FROM users WHERE role IN ('admin','manager') AND is_active=1 AND user_id != %s",
                (str(ADMIN_ID),)
            )
            for (uid,) in recipients:
                try:
                    await bot.send_message(int(uid), text, parse_mode="Markdown")
                except Exception:
                    pass

        except Exception as e:
            print(f"weekly_digest error: {e}")


async def auto_sync_task():
    """Фоновая задача: синхронизация Drive → RAG + курсы каждые SYNC_INTERVAL_SEC секунд."""
    # Первый запуск — через 60 сек после старта (бот уже принимает запросы)
    await asyncio.sleep(60)
    while True:
        print("Auto-sync: запуск синхронизации Google Drive...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, sync_manager.run_sync)

        # Если RAG обновился — сбрасываем синглтоны, чтобы следующий запрос загрузил новый индекс
        if result["rag_updated"]:
            global _vdb_kb_openai, _vdb_coach_openai, _vdb_kb_google, _vdb_coach_google
            _vdb_kb_openai = None
            _vdb_coach_openai = None
            _vdb_kb_google = None
            _vdb_coach_google = None
            files_str = ", ".join(result["rag_updated"][:5])
            if len(result["rag_updated"]) > 5:
                files_str += f" и ещё {len(result['rag_updated']) - 5}"
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"RAG-индекс обновлён.\nИзменения: {files_str}"
                )
            except Exception:
                pass

        if result["courses_updated"]:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"Курсы обновлены: {', '.join(result['courses_updated'])}"
                )
            except Exception:
                pass

        if result["error"]:
            try:
                await bot.send_message(ADMIN_ID, f"Ошибка синхронизации: {result['error']}")
            except Exception:
                pass

        await asyncio.sleep(sync_manager.SYNC_INTERVAL_SEC)


async def main():
    init_db()
    sync_manager.init_sync_tables()
    seed_vitaran_course()
    seed_onboarding()
    await set_bot_commands(bot)
    if os.getenv("AUTO_SYNC_ENABLED", "false").lower() == "true":
        asyncio.create_task(auto_sync_task())
        print(f"Бот Эмет запущен. Автосинхронизация каждые {sync_manager.SYNC_INTERVAL_SEC // 60} мин.")
    else:
        print("Бот Эмет запущен. Автосинхронизация отключена (AUTO_SYNC_ENABLED=false).")
    asyncio.create_task(weekly_digest_task())
    asyncio.create_task(ttl_cleanup_task())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())