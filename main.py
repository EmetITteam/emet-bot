import asyncio
import io
import os
import json
import db
import base64
import random
import time
import logging
from collections import deque
from datetime import datetime
import sync_manager
from openai import AsyncOpenAI, RateLimitError as OpenAIRateLimitError
from google import genai
from googleapiclient.http import MediaIoBaseDownload
import anthropic
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
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

# --- БРЕНД-СЛОВНИК ДЛЯ ПОШУКУ СЕРТИФІКАТІВ ---
BRAND_SYNONYMS = {
    "витаран": "Vitaran", "вітаран": "Vitaran", "vitaran": "Vitaran",
    "еллансе": "Ellanse", "елансе": "Ellanse", "элансе": "Ellanse", "эллансе": "Ellanse", "ellanse": "Ellanse",
    "нейраміс": "Neuramis", "нейрамис": "Neuramis", "neuramis": "Neuramis",
    "петаран": "Petaran", "petaran": "Petaran",
    "нейронокс": "Neuronox", "neuronox": "Neuronox",
    "ессе": "ESSE", "эссе": "ESSE", "esse": "ESSE",
    "іюз": "IUSE", "июз": "IUSE", "iuse": "IUSE",
    "ексокс": "EXOXE", "екзокс": "EXOXE", "экзокс": "EXOXE", "exoxe": "EXOXE",
    "скін бустер": "SkinBooster", "skin booster": "SkinBooster", "skinbooster": "SkinBooster",
    "magnox": "Magnox", "магнокс": "Magnox",
}

def detect_brand(query: str) -> str:
    """Витягує канонічну назву бренду з запиту користувача."""
    q = query.lower()
    for keyword, brand in BRAND_SYNONYMS.items():
        if keyword in q:
            return brand
    return ""

# --- ROLES & WHITELIST (з БД) ---
# Ролі: admin, manager, operator
# Якщо в таблиці users немає жодного запису → відкритий доступ (режим розробки)
# В продакшені: /adduser <id> [role] через адмін-команду

ROLES = {"admin", "manager", "operator", "director"}

# TTL-кэш для is_allowed и get_user_role — избегаем DB-запроса на каждое сообщение
_USER_CACHE_TTL = 60  # секунд
_user_cache: dict[int, tuple[bool, str, float]] = {}  # user_id → (allowed, role, expires_at)

def _get_user_cache(user_id: int):
    """Возвращает (allowed, role) из кэша или None если устарел."""
    entry = _user_cache.get(user_id)
    if entry and time.monotonic() < entry[2]:
        return entry[0], entry[1]
    return None

def _set_user_cache(user_id: int, allowed: bool, role: str):
    _user_cache[user_id] = (allowed, role, time.monotonic() + _USER_CACHE_TTL)

def invalidate_user_cache(user_id: int):
    """Сбросить кэш пользователя — вызывать при изменении роли/доступа."""
    _user_cache.pop(user_id, None)

def _load_user_from_db(user_id: int) -> tuple[bool, str]:
    try:
        row = db.query(
            "SELECT "
            "  (SELECT COUNT(*) FROM users WHERE is_active=1) AS total,"
            "  COALESCE((SELECT is_active FROM users WHERE user_id=%s), -1) AS user_active,"
            "  COALESCE((SELECT role FROM users WHERE user_id=%s AND is_active=1), 'guest') AS role",
            (str(user_id), str(user_id)), fetchone=True
        )
        total, user_active, role = row
        allowed = True if total == 0 else user_active == 1
        return allowed, role
    except Exception:
        return True, "guest"

def is_allowed(user_id: int) -> bool:
    """Перевіряє доступ: якщо є хоча б один користувач в БД — лише вони мають доступ."""
    cached = _get_user_cache(user_id)
    if cached:
        return cached[0]
    allowed, role = _load_user_from_db(user_id)
    _set_user_cache(user_id, allowed, role)
    return allowed

def get_user_role(user_id: int) -> str:
    """Повертає роль користувача ('admin'/'manager'/'operator'), або 'guest' якщо не знайдено."""
    cached = _get_user_cache(user_id)
    if cached:
        return cached[1]
    allowed, role = _load_user_from_db(user_id)
    _set_user_cache(user_id, allowed, role)
    return role

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or get_user_role(user_id) == "admin"

# --- RATE LIMITER (sliding window, in-memory) ---
_rate_buckets: dict[int, deque] = {}
RATE_LIMIT_MAX = 10     # max requests per window per user
RATE_LIMIT_WINDOW = 60  # seconds

def check_rate_limit(user_id: int) -> bool:
    """Повертає True якщо запит дозволений, False — якщо ліміт вичерпано."""
    now = time.monotonic()
    if user_id not in _rate_buckets:
        _rate_buckets[user_id] = deque()
    dq = _rate_buckets[user_id]
    while dq and now - dq[0] > RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_MAX:
        return False
    dq.append(now)
    return True

_openai_quota_exceeded = False  # флаг: OpenAI закончился баланс → не дёргать повторно до рестарта

client_openai = AsyncOpenAI(api_key=OPENAI_KEY)
client_google = genai.Client(api_key=GEMINI_KEY)
client_claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

MODEL_OPENAI = "gpt-4o-mini"
MODEL_OPENAI_COACH = "gpt-4o"
MODEL_GOOGLE = "gemini-2.0-flash"
MODEL_CLAUDE = "claude-sonnet-4-6"

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("emet_bot")

# --- RAG & LLM CONSTANTS ---
RAG_K_PRODUCTS         = 12    # chunks from products index (our data)
RAG_K_COMPETITORS      = 8     # chunks from competitors index (comparisons + products like ESSE)
RAG_K_DEFAULT          = 15    # chunks for kb/cases/operational
RAG_K_COMBO            = 15    # chunks for combo with category filter
LLM_TIMEOUT            = 60    # seconds timeout for all LLM API calls
LLM_INTENT_MAX_TOKENS  = 10    # detect_intent classifier output
LLM_QUERY_MAX_TOKENS   = 60    # prepare_search_query output
LLM_CLAUDE_MAX_TOKENS  = 4096  # Claude fallback response
STREAM_UPDATE_INTERVAL = 0.8   # seconds between streaming message edits
CHAT_HISTORY_COACH     = 6     # max messages in coach history (3 exchanges)
CHAT_HISTORY_DEFAULT   = 4     # max messages for other modes (2 exchanges)

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
    mode_certs = State()       # Режим 7: Сертифікати та документи
    lms_test_active = State()
    waiting_for_json = State()
    voice_confirm = State()  # Подтверждение распознанного голоса/фото

# --- 3. СИСТЕМНЫЕ ПРОМПТЫ ---
from prompts import SYSTEM_PROMPTS


# --- 4. ИНТЕРФЕЙС (МЕНЮ) ---
def get_main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 HR і регламенти", callback_data="set_kb")
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

            # Теми курсу
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
            cur.execute("ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS folder_label TEXT DEFAULT ''")
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

            # Історія діалогів — виживає після рестарту контейнера
            cur.execute('''CREATE TABLE IF NOT EXISTS chat_histories
                (user_id TEXT PRIMARY KEY,
                 history_json TEXT NOT NULL DEFAULT '[]',
                 updated_at TIMESTAMP DEFAULT NOW())''')

            # Оцінки відповідей (👍/👎)
            cur.execute('''CREATE TABLE IF NOT EXISTS feedback
                (id SERIAL PRIMARY KEY,
                 log_id INTEGER,
                 user_id TEXT,
                 rating INTEGER,
                 mode TEXT,
                 created_at TIMESTAMP DEFAULT NOW())''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_log ON feedback(log_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_mode ON feedback(mode)")

            # Спроби входу в адмін-панель (brute-force захист, виживає після рестарту)
            cur.execute('''CREATE TABLE IF NOT EXISTS admin_login_attempts
                (ip TEXT PRIMARY KEY,
                 count INTEGER DEFAULT 0,
                 locked_until TIMESTAMP)''')


def _load_chat_history_db(user_id: int) -> list:
    """Завантажує останню історію діалогу з БД (fallback при рестарті)."""
    try:
        row = db.query("SELECT history_json FROM chat_histories WHERE user_id=%s", (str(user_id),), fetchone=True)
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return []


def _save_chat_history_db(user_id: int, history: list):
    """Зберігає поточну історію діалогу в БД."""
    try:
        db.execute(
            "INSERT INTO chat_histories (user_id, history_json, updated_at) VALUES (%s,%s,NOW()) "
            "ON CONFLICT (user_id) DO UPDATE SET history_json=EXCLUDED.history_json, updated_at=NOW()",
            (str(user_id), json.dumps(history, ensure_ascii=False))
        )
    except Exception as e:
        logger.error("history DB save error: %s", e)


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
            logger.info("Demo course 'Vitaran' loaded: %d topics, %d questions", len(topics_data), total_q)


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
    logger.info("Onboarding checklist loaded: %d items", len(items))


# --- 5b. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД ---
def log_to_db(user_id, username, mode, ai_engine, question, answer, has_source, model=None, tokens_in=0, tokens_out=0) -> int:
    """Записує лог запиту і повертає log_id (для прив'язки feedback)."""
    try:
        return db.execute_returning(
            "INSERT INTO logs (date, user_id, username, mode, ai_engine, question, answer, found_in_db, model, tokens_in, tokens_out) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, username,
             mode, ai_engine, question, answer, 1 if has_source else 0, model, tokens_in, tokens_out)
        )
    except Exception as e:
        logger.error("log DB write error: %s", e)
        return 0


def save_course(title, json_data):
    try:
        db.execute(
            "INSERT INTO courses (title, data, created_at) VALUES (%s, %s, %s)",
            (title, json_data, datetime.now().isoformat())
        )
    except Exception as e:
        logger.error("course save error: %s", e)


def get_courses():
    return db.query("SELECT id, title, description FROM courses WHERE visible IS NOT FALSE ORDER BY id")


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
        logger.error("audit write error: %s", e)


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
_INJECTION_PATTERNS = re.compile(
    r"(?i)(ignore\s+(all\s+)?previous\s+instructions|"
    r"you\s+are\s+now\s+|system\s*:\s*|"
    r"disregard\s+(all\s+)?prior|"
    r"forget\s+(everything|all|your\s+instructions))",
)

def _extract_docs(docs):
    context_text = ""
    sources = {}
    grouped_docs = {}

    for doc in docs:
        name = doc.metadata.get("source", "Невідомий документ")
        url = doc.metadata.get("url", "")
        file_id = doc.metadata.get("file_id", "")
        content = doc.page_content
        # Sanitize: strip known prompt injection patterns from indexed documents
        if _INJECTION_PATTERNS.search(content):
            content = _INJECTION_PATTERNS.sub("[FILTERED]", content)
            logger.warning("Prompt injection pattern detected in doc '%s'", name[:60])
        if name not in grouped_docs:
            grouped_docs[name] = {"url": url, "file_id": file_id, "content": []}
        grouped_docs[name]["content"].append(content)

    for i, (name, data) in enumerate(grouped_docs.items(), 1):
        doc_id = f"REF{i}"
        full_content = "\n".join(data["content"])
        # Mark competitor docs so LLM doesn't confuse their data with our products
        _name_l = name.lower()
        is_competitor = "competitor" in _name_l or "competitir" in _name_l or "_master." in _name_l or "competitors_" in _name_l
        label = f"⚠️ КОНКУРЕНТ (чужі дані, НЕ наш продукт)" if is_competitor else ""
        lms_label = "📘 НАВЧАЛЬНИЙ КУРС EMET" if "[LMS]" in name else ""
        tag = lms_label or label
        header = f"=== ИСТОЧНИК: {doc_id} | {tag} | {name} ===" if tag else f"=== ИСТОЧНИК: {doc_id} | {name} ==="
        context_text += f"{header}\n{full_content}\n\n"
        sources[doc_id] = {"name": name, "url": data["url"], "file_id": data.get("file_id", "")}

    return context_text, sources


_vdb_kb_openai          = None
_vdb_products_openai    = None
_vdb_competitors_openai = None
# Legacy fallback — used if new indices don't exist yet
_vdb_coach_openai       = None
_vdb_kb_google          = None
_vdb_products_google    = None
_vdb_competitors_google = None
_vdb_coach_google       = None  # legacy fallback

# Embedding синглтони — створюються один раз, а не на кожен запит
_emb_openai = None
_emb_google = None

def _get_emb_openai():
    global _emb_openai
    if _emb_openai is None:
        _emb_openai = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
    return _emb_openai

def _get_emb_google():
    global _emb_google
    if _emb_google is None:
        _emb_google = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001", google_api_key=GEMINI_KEY)
    return _emb_google

# Drive сервіс для скачування сертифікатів (реюзаємо авторизацію із sync_manager)
_drive_service = None

def get_drive_service():
    global _drive_service
    if _drive_service is None:
        _drive_service, _ = sync_manager.get_services()
    return _drive_service


_vdb_mtimes = {}  # Track index directory modification times for auto-refresh

def _get_vdb(name, provider="openai"):
    """Lazy-init a ChromaDB instance by logical name. Auto-refreshes if index files changed."""
    global _vdb_kb_openai, _vdb_products_openai, _vdb_competitors_openai, _vdb_coach_openai
    global _vdb_kb_google, _vdb_products_google, _vdb_competitors_google, _vdb_coach_google

    _PATHS = {
        "products":    "data/db_index_products_openai",
        "competitors": "data/db_index_competitors_openai",
        "coach":       "data/db_index_coach_openai",       # legacy fallback
        "kb":          "data/db_index_kb_openai",
    }
    _PATHS_G = {
        "products":    "data/db_index_products_google",
        "competitors": "data/db_index_competitors_google",
        "coach":       "data/db_index_coach_google",
        "kb":          "data/db_index_kb_google",
    }

    paths = _PATHS if provider == "openai" else _PATHS_G
    emb = _get_emb_openai() if provider == "openai" else _get_emb_google()

    # Check if split indices exist; if not, fall back to legacy coach
    if name in ("products", "competitors") and not os.path.exists(paths[name]):
        logger.info("Split index %s not found, falling back to legacy coach", paths[name])
        name = "coach"

    path = paths[name]
    cache_key = f"_vdb_{name}_{provider}"

    # Auto-refresh: if index directory was modified, drop cached instance
    try:
        current_mtime = os.path.getmtime(path) if os.path.exists(path) else 0
        prev_mtime = _vdb_mtimes.get(cache_key, 0)
        if current_mtime != prev_mtime and prev_mtime != 0:
            # Index was rebuilt on disk — drop cache so next call creates fresh instance
            globals()[cache_key] = None
            logger.info("VDB cache refreshed for %s (mtime changed)", path)
        _vdb_mtimes[cache_key] = current_mtime
    except Exception:
        pass

    cached = globals().get(cache_key)
    if cached is not None:
        return cached

    vdb = Chroma(persist_directory=path, embedding_function=emb)
    globals()[cache_key] = vdb
    _vdb_mtimes[cache_key] = os.path.getmtime(path) if os.path.exists(path) else 0
    return vdb


RAG_SCORE_THRESHOLD    = 1.5    # permissive threshold — log scores for tuning, don't discard aggressively

# Product name normalization for RAG search queries
_QUERY_NORMALIZE = {
    "токс ай": "Tox Eye", "витаран токс": "Vitaran Tox Eye",
    "вайтнінг": "Whitening", "вайтинг": "Whitening",
    "скінбустер": "IUSE SKINBOOSTER HA 20", "скін бустер": "IUSE SKINBOOSTER HA 20",
    "скін хілер": "Vitaran Skin Healer",
    "ессе": "ESSE Esse", "эссе": "ESSE Esse",
    "елансе": "Ellanse Ellansé", "еланс": "Ellanse Ellansé",
    "нейраміс": "Neuramis", "нейрамис": "Neuramis",
    "нейронокс": "Neuronox", "петаран": "Petaran PLLA",
    "ексоксе": "Exoxe EXOXE", "экзокс": "Exoxe EXOXE",
    "хп сел": "HP Cell Vitaran", "hp сел": "HP Cell Vitaran",
    "айюз хеір": "IUSE HAIR REGROWTH", "iuse хеір": "IUSE HAIR REGROWTH",
    "лізат": "лізати Esse пробіотики", "пребіотик": "пребіотики Esse",
}

def _normalize_query(query: str) -> str:
    """Enrich query with canonical product names for better RAG matching."""
    q_lower = query.lower()
    additions = []
    for trigger, canonical in _QUERY_NORMALIZE.items():
        if trigger in q_lower:
            additions.append(canonical)
    if additions:
        return query + " " + " ".join(additions)
    return query


def _search_with_score(vdb, query, k, threshold=RAG_SCORE_THRESHOLD):
    """Search with score logging and optional filtering."""
    try:
        results = vdb.similarity_search_with_score(query, k=k)
        if results:
            scores = [score for _, score in results]
            logger.debug("RAG scores: min=%.3f max=%.3f avg=%.3f query=%s",
                        min(scores), max(scores), sum(scores)/len(scores), query[:50])
        return [doc for doc, score in results if score < threshold]
    except Exception:
        return vdb.similarity_search(query, k=k)


def get_context(query, mode="kb", provider="openai", has_competitor=False):
    """3-zone RAG search: products / products+competitors / kb."""
    normalized_query = _normalize_query(query)

    if mode in ("kb", "cases", "operational"):
        vdb = _get_vdb("kb", provider)
        return _extract_docs(_search_with_score(vdb, normalized_query, RAG_K_DEFAULT))

    if mode == "combo":
        vdb = _get_vdb("products", provider)
        docs = _search_with_score(vdb, normalized_query, RAG_K_COMBO)
        if not docs:
            docs = vdb.similarity_search(normalized_query, k=RAG_K_COMBO)
        return _extract_docs(docs)

    # Coach mode — 3-zone logic
    vdb_products = _get_vdb("products", provider)
    vdb_competitors = _get_vdb("competitors", provider)

    if has_competitor:
        docs_ours = _search_with_score(vdb_products, normalized_query, RAG_K_PRODUCTS)
        docs_comp = _search_with_score(vdb_competitors, normalized_query, RAG_K_COMPETITORS)
        return _extract_docs(docs_ours + docs_comp)
    else:
        docs_products = _search_with_score(vdb_products, normalized_query, RAG_K_PRODUCTS)
        docs_comp = _search_with_score(vdb_competitors, normalized_query, RAG_K_COMPETITORS)
        return _extract_docs(docs_products + docs_comp)


async def detect_intent(query: str) -> str:
    try:
        response = await client_openai.chat.completions.create(
            model="gpt-4o-mini",
            timeout=15,
            messages=[{"role": "system", "content": (
                "Ты — маршрутизатор запросов EMET. Классифицируй запрос ОДНИМ словом.\n"
                "Работает на любом языке (UA/RU).\n\n"
                "ПРАВИЛА:\n"
                "- 'kb' — регламенты компании, HR, отпуска, CRM, структура, зарплаты, ИТ-доступы, "
                "правила работы, внутренние документы и процедуры EMET.\n"
                "- 'coach' — продажи препаратов EMET: Vitaran, Ellanse, Petaran, Neuramis, Exoxe, "
                "Neuronox, IUSE, Esse (эссе, ессе), Magnox, PDRN, PCL, филлеры, ботулотоксин; их состав, показания, "
                "применение, дозировки, отличия от конкурентов; скрипты, возражения (дорого, есть аналоги, "
                "работаю с другим), переговоры с врачами, аргументы продаж. "
                "ВАЖНО: 'дай диалог', 'распиши диалог', 'другие аргументы', 'что ещё сказать', "
                "'варианты ответов на возражение', 'детально распиши' — это ВСЕГДА coach.\n"
                "ВАЖНО: приглашение клиента/врача на семинар, работа с отказом от встречи/визита — это coach.\n"
                "- 'cases' — ТОЛЬКО когда человек вставляет ГОТОВЫЙ ТЕКСТ реального диалога/переписки "
                "и просит разобрать ошибки. Просьба 'дай диалог' или 'распиши варианты' = НЕ cases.\n"
                "- 'operational' — командировки (відрядження), возврат товара, SLA, "
                "оформление расходов, документы для поездок, возмещение затрат (НЕ приглашение клиентов).\n"
                "- 'certs' — сертификаты, регистрационные удостоверения, разрешительные документы.\n\n"
                "Отвечай только одним словом: kb, coach, cases, operational или certs."
            )},
            {"role": "user", "content": query}],
            temperature=0.0, max_tokens=LLM_INTENT_MAX_TOKENS
        )
        result = response.choices[0].message.content.strip().lower()
        return result if result in ("kb", "coach", "cases", "operational", "certs") else "kb"
    except Exception:
        return "kb"


async def prepare_search_query(user_query: str) -> str:
    try:
        response = await client_openai.chat.completions.create(
            model="gpt-4o-mini",
            timeout=15,
            messages=[{
                "role": "system",
                "content": "Переведи запрос пользователя на украинский и русский языки, добавь 2-3 синонима. "
                           "Для тем про увольнение/звільнення добавь: обходной лист, розрахунок, припинення трудового договору, безпека при роботі з кандидатами. "
                           "Выдай всё одной строкой через пробел. Это нужно для поиска по базе."
            },
            {"role": "user", "content": user_query}],
            temperature=0.0, max_tokens=LLM_QUERY_MAX_TOKENS
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return user_query


# --- 7. ЯДРО RAG (выделено в отдельную функцию для переиспользования) ---
MAX_QUERY_LEN = 5000

async def process_text_query(text: str, message: types.Message, state: FSMContext):
    """Основная RAG-логика. Принимает text явно (для голоса/фото/текста)."""
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Забагато запитів. Зачекайте хвилину і спробуйте знову.")
        return
    if len(text) > MAX_QUERY_LEN:
        await message.answer(f"⚠️ Запит завеликий (максимум {MAX_QUERY_LEN} символів). Скоротіть, будь ласка.")
        return

    t = text.lower().strip()

    greetings = ["привет", "здравствуйте", "добрый день", "привіт", "добрий день"]
    if t in greetings:
        return await message.answer("Вітаю! Я готовий до роботи. Задайте ваше запитання.")

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
    # Якщо in-memory порожня (рестарт контейнера) — відновлюємо з БД
    if not chat_history:
        chat_history = _load_chat_history_db(message.from_user.id)

    # Ключевые слова запросов на скрипт/диалог (один список — используется в двух местах ниже)
    _SCRIPT_KEYWORDS = [
        "дай диалог", "дай діалог", "дай скрипт", "скрипт з лікарем",
        "діалог з лікарем", "диалог с врачом", "розіграй діалог",
        "зіграй діалог", "покажи діалог", "покажи диалог",
        "конкретный диалог", "конкретний діалог",
        "пример диалога", "приклад діалогу",
        # Семінар по [продукту] = запит на скрипт запрошення
        "семінар по", "семинар по",
    ]
    _t_lower_early = text.lower().strip()
    _is_script_early = any(kw in _t_lower_early for kw in _SCRIPT_KEYWORDS)

    # Авторозмітка по контенту запиту (перемикання між режимами)
    # state_data вже отримано вище (рядок ~770), повторний виклик не потрібен
    _COMBO_KEYWORDS = ["комбо", "комбін", "combo", "поєднати", "поєднання", "сочетать", "сочетание", "совместить",
                       "протокол для", "протоколи для", "протоколы для", "які протоколи", "какие протоколы"]
    _is_combo_query = any(kw in _t_lower_early for kw in _COMBO_KEYWORDS)

    # Operational: відрядження та операційні питання — перехоплюємо до detect_intent щоб не впасти в combo
    _OPERATIONAL_EARLY_KEYWORDS = [
        "відрядження", "командировк", "возврат товар", "повернення товар",
        "відшкодуванн", "возмещени", "оформити витрат", "оформить расход",
        "семінар", "семинар", "документи для поїздки", "документы для поездки",
    ]
    _is_operational_early = any(kw in _t_lower_early for kw in _OPERATIONAL_EARLY_KEYWORDS)
    # Якщо запит про семінар/дзвінок/візит, але клієнт чинить опір — це робота з запереченням (coach), не операційне
    _CLIENT_RESISTANCE_KW = [
        "не хоче", "не хочет", "не хочу", "відмовляєть", "не йде", "не идет",
        "не хочет идти", "не хоче йти", "против", "проти", "не згоден", "не согласен",
    ]
    if _is_operational_early and any(kw in _t_lower_early for kw in _CLIENT_RESISTANCE_KW):
        _is_operational_early = False

    # Follow-up coach: продовження тренінгу — "інші аргументи", "розпиши детально", "варіанти відповідей"
    _FOLLOWUP_COACH_KEYWORDS = [
        "інші аргументи", "другие аргументы", "ще аргументи", "що ще сказати", "что ещё сказать",
        "розпиши детально", "распиши подробно", "детально розпиши", "більше варіантів", "больше вариантов",
        "розпиши діалог", "распиши диалог", "варіанти відповідей", "варианты ответов на",
        "детальніше", "подробнее", "ещё варианты", "ще варіанти",
        "дай діалог", "дай конкретный диалог", "дай конкретний діалог",
        "покажи діалог", "покажи диалог", "приклад діалогу", "пример диалога",
        "як відповісти", "как ответить", "що сказати", "что сказать",
        # Запит "розкажи про X" під час активної сесії — залишаємо в coach
        "розкажи про", "расскажи про", "розкажи більше про", "розкажи детально про",
        "що таке", "что такое", "чим відрізняєть", "чем отличается",
    ]
    _is_coach_followup = any(kw in _t_lower_early for kw in _FOLLOWUP_COACH_KEYWORDS)
    # Короткі підтвердження/афірмації після активної coach-сесії → залишаємо в coach
    # Приклади: "Хочу", "Так", "Да нужно", "Ок продовжи" — без цього бот просить уточнення
    _AFFIRMATION_KEYWORDS = [
        "хочу", "так", "да", "ок", "добре", "хорошо", "ага", "угу",
        "потрібно", "нужно", "давай", "продовж", "продолжай", "далі", "більше", "ще", "ещё",
        "є", "есть", "зрозуміло", "понятно", "дякую", "спасибо", "окей", "okay",
    ]
    _t_clean_aff = _t_lower_early.rstrip("!?.")
    _is_short_affirmation = (
        len(_t_lower_early.split()) <= 4
        and chat_history
        and any(_t_clean_aff == kw or _t_clean_aff.startswith(kw + " ") or _t_clean_aff.endswith(" " + kw) for kw in _AFFIRMATION_KEYWORDS)
    )
    # Риторичні/розмовні питання в середині активної сесії — це продовження діалогу, а не KB-запит
    # Приклад: "Почему ты сразу мне так не написал?" — 8 слів, але явне продовження coach-сесії
    _RHETORICAL_MID_SESSION = [
        "почему ты", "зачем ты", "как так", "чому ти", "чому так", "чому не",
        "почему не", "як же так", "что значит", "що означає", "навіщо ти",
        "а чому", "а как", "а чому", "а навіщо",
    ]
    _is_mid_session_rhetorical = (
        chat_history
        and len(_t_lower_early.split()) <= 10
        and any(_t_lower_early.startswith(kw) for kw in _RHETORICAL_MID_SESSION)
    )
    if _is_short_affirmation or _is_mid_session_rhetorical:
        _is_coach_followup = True

    # combo_mode з кнопки — тільки для першого повідомлення, потім скидаємо
    _combo_from_button = state_data.get("combo_mode", False)
    if _combo_from_button:
        await state.update_data(combo_mode=False)

    # Ранній детектор назв препаратів EMET — якщо є назва продукту → завжди coach, без LLM
    # Охоплює: "що таке нейрамис", "розкажи про эссе", "витаран vs конкуренти" і т.д.
    _EMET_PRODUCT_NAMES_EARLY = [
        "ellanse", "elanse", "еланс", "елансе", "элансе", "эллансе",
        "neuramis", "нейрамис", "нейраміс",
        "vitaran", "вітаран", "витаран",
        "petaran", "петаран",
        "полімолочна кислота", "полимолочная кислота", "poly plla",
        "exoxe", "ексоксе", "экзокс",
        "esse", "эссе", "ессе",
        "iuse", "айюз",
        "magnox", "магнокс",
        "neuronox", "нейронокс",
    ]
    # Word-boundary check for short keys to avoid substring collisions (esse in "message" etc)
    _SHORT_KEYS = {"esse", "эссе", "ессе", "iuse", "айюз"}
    def _match_product(p, text):
        if p in _SHORT_KEYS:
            import re
            return bool(re.search(r'\b' + re.escape(p) + r'\b', text))
        return p in text
    _has_emet_product_early = any(_match_product(p, _t_lower_early) for p in _EMET_PRODUCT_NAMES_EARLY)

    _search_query_ready = None  # буде заповнено паралельно якщо пройдемо через else
    if _combo_from_button or _is_combo_query:
        mode_key = "combo"
    elif _has_emet_product_early:
        # Назва EMET-препарату → завжди coach (навіть якщо є "семінар", "документи" тощо)
        mode_key = "coach"
    elif _is_operational_early:
        mode_key = "operational"
    elif (_is_script_early or _is_coach_followup) and chat_history:
        mode_key = "coach"
    else:
        # detect_intent і prepare_search_query запускаємо паралельно — економія ~300ms
        mode_key, _search_query_ready = await asyncio.gather(
            detect_intent(text),
            prepare_search_query(text),
        )

    state_map = {
        "coach":       UserState.mode_coach,
        "cases":       UserState.mode_cases,
        "operational": UserState.mode_operational,
        "certs":       UserState.mode_certs,
    }
    new_state = state_map.get(mode_key, UserState.mode_kb)
    # Clear chat history when mode auto-switches to prevent context leaks
    prev_state = await state.get_state()
    if prev_state and new_state != prev_state:
        chat_history = []
        await state.update_data(chat_history=[])
    await state.set_state(new_state)

    # --- Certs: прямий SQL-пошук по назвах файлів (без RAG) ---
    if mode_key == "certs":
        brand = detect_brand(text)
        if brand:
            rows = db.query_dict(
                "SELECT file_name, file_id FROM sync_state WHERE folder_label = 'certs' AND LOWER(file_name) LIKE %s ORDER BY file_name",
                (f"%%{brand.lower()}%%",)
            )
        else:
            rows = db.query_dict(
                "SELECT file_name, file_id FROM sync_state WHERE folder_label = 'certs' ORDER BY file_name LIMIT 30"
            )
        if not rows:
            label = f"*{brand}*" if brand else "вашим запитом"
            await message.answer(f"Документів по {label} не знайдено в базі сертифікатів.")
        else:
            cert_files = [{"file_id": r["file_id"], "name": r["file_name"]} for r in rows if r.get("file_id")]
            await state.update_data(cert_files=cert_files)
            builder = InlineKeyboardBuilder()
            for idx, cf in enumerate(cert_files[:8]):
                short_name = cf["name"][:40] + "…" if len(cf["name"]) > 40 else cf["name"]
                builder.button(text=f"📥 {short_name}", callback_data=f"cert_dl_{idx}")
            builder.button(text="🏠 Головне меню", callback_data="go_home")
            builder.adjust(1)
            label = brand if brand else "запитом"
            await message.answer(
                f"Знайдено {len(cert_files)} документ(ів) по *{label}*:",
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
        return

    # Якщо detect_intent + prepare_search_query вже виконались паралельно — беремо готовий результат
    search_query = _search_query_ready if _search_query_ready is not None else await prepare_search_query(text)

    # --- Python-level детекция продукта + возражения (чтобы LLM не переспрашивал) ---
    # IMPORTANT: longer keys MUST come before shorter ones (e.g. "iuse hair" before "iuse")
    _EMET_PRODUCTS = [
        "ellanse", "elanse", "еланс", "елансе", "элансе", "эллансе", "ellanсе",
        "neuramis", "нейрамис", "нейраміс",
        "vitaran skin", "вітаран скін", "скін хілер", "skin healer",
        "vitaran tox", "витаран токс", "вітаран токс", "tox eye", "токс ай",
        "vitaran whitening", "витаран вайтнінг", "вітаран вайтнінг",
        "hp cell", "хп сел", "hp сел",
        "vitaran", "вітаран", "витаран",
        "petaran", "петаран",
        "полімолочна кислота", "полимолочная кислота", "poly plla", "поли-l-молочна", "поли-l-молочная",
        "exoxe", "экзокс", "экзосомы", "ексоксе",
        "esse", "эссе", "ессе",
        "iuse hair", "iuse хеір", "iuse хеир", "айюз хеір",
        "iuse skin", "iuse скінбустер", "скінбустер", "skinbooster",
        "iuse collagen", "iuse колаген", "айюз колаген",
        "iuse", "айюз", "июз",
        "neuronox", "нейронокс",
        "magnox", "магнокс",
    ]
    _PRODUCT_CANONICAL = {
        "ellanse": "Ellansé", "elanse": "Ellansé", "еланс": "Ellansé",
        "елансе": "Ellansé", "элансе": "Ellansé", "эллансе": "Ellansé", "ellanсе": "Ellansé",
        "neuramis": "Neuramis", "нейрамис": "Neuramis", "нейраміс": "Neuramis",
        "vitaran skin": "Vitaran Skin Healer", "вітаран скін": "Vitaran Skin Healer",
        "скін хілер": "Vitaran Skin Healer", "skin healer": "Vitaran Skin Healer",
        "vitaran tox": "HP Cell Vitaran Tox Eye", "витаран токс": "HP Cell Vitaran Tox Eye",
        "вітаран токс": "HP Cell Vitaran Tox Eye", "tox eye": "HP Cell Vitaran Tox Eye",
        "токс ай": "HP Cell Vitaran Tox Eye",
        "vitaran whitening": "HP Cell Vitaran Whitening", "витаран вайтнінг": "HP Cell Vitaran Whitening",
        "вітаран вайтнінг": "HP Cell Vitaran Whitening",
        "hp cell": "HP Cell Vitaran", "хп сел": "HP Cell Vitaran", "hp сел": "HP Cell Vitaran",
        "vitaran": "Vitaran", "вітаран": "Vitaran", "витаран": "Vitaran",
        "petaran": "Petaran", "петаран": "Petaran",
        "полімолочна кислота": "Petaran", "полимолочная кислота": "Petaran",
        "poly plla": "Petaran", "поли-l-молочна": "Petaran", "поли-l-молочная": "Petaran",
        "exoxe": "EXOXE", "экзокс": "EXOXE", "экзосомы": "EXOXE", "ексоксе": "EXOXE",
        "esse": "ESSE", "эссе": "ESSE", "ессе": "ESSE",
        "iuse hair": "IUSE HAIR REGROWTH", "iuse хеір": "IUSE HAIR REGROWTH",
        "iuse хеир": "IUSE HAIR REGROWTH", "айюз хеір": "IUSE HAIR REGROWTH",
        "iuse skin": "IUSE SKINBOOSTER HA 20", "iuse скінбустер": "IUSE SKINBOOSTER HA 20",
        "скінбустер": "IUSE SKINBOOSTER HA 20", "skinbooster": "IUSE SKINBOOSTER HA 20",
        "iuse collagen": "IUSE Collagen", "iuse колаген": "IUSE Collagen", "айюз колаген": "IUSE Collagen",
        "iuse": "IUSE", "айюз": "IUSE", "июз": "IUSE",
        "neuronox": "Neuronox", "нейронокс": "Neuronox",
        "magnox": "Magnox", "магнокс": "Magnox",
    }
    _OBJECTION_KEYWORDS = [
        "дорого", "дорогой", "дорога", "дорогую", "дорогое",
        "не вірю", "не верю", "подумаю", "подумать",
        "є дешевше", "есть дешевле", "дешевле",
        "не впевнений", "не уверен",
        "не потрібно", "не нужно",
    ]
    # _SCRIPT_KEYWORDS визначений вище і переюзається тут
    t_lower = t
    _detected_product = next((p for p in _EMET_PRODUCTS if _match_product(p, t_lower)), None)
    _all_detected_products = [p for p in _EMET_PRODUCTS if p in t_lower]
    _has_objection = any(kw in t_lower for kw in _OBJECTION_KEYWORDS)
    _is_script_request = any(kw in t_lower for kw in _SCRIPT_KEYWORDS)

    # Если coach и нет продукта в тексте — ищем продукт И возражение в истории
    # Триггер: запит на скрипт, follow-up, або просто немає продукту в поточному повідомленні
    _history_objection = None
    if mode_key == "coach" and (_is_script_request or _is_coach_followup or not _detected_product) and chat_history:
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
    _canonical_from_competitor = False  # прапор: canonical встановлено через детекцію конкурента, а не EMET-продукту
    _all_canonicals = list(dict.fromkeys(
        _PRODUCT_CANONICAL.get(p, p) for p in _all_detected_products
    ))

    # Детекція конкурентів — збагачує контекст для порівняльних аргументів
    _COMPETITORS = ["radiesse", "радіесс", "sculptra", "скульптра", "juvederm", "ювідерм",
                    "teosyal", "теосял", "restylane", "рестилайн", "rejuran", "реджуран",
                    "aesthefill", "естефіл", "plinest", "плінест",
                    "nucleofill", "нуклеофіл", "mastelli", "мастеллі", "cellular matrix",
                    "benev", "jalupro", "regenyal", "juvelook", "profhilo", "профіло"]
    _detected_competitor = next((c for c in _COMPETITORS if c in t_lower), None)
    if not _detected_competitor and chat_history:
        for _m in reversed([m for m in chat_history if m["role"] == "user"][-3:]):
            _detected_competitor = next((c for c in _COMPETITORS if c in _m["content"].lower()), None)
            if _detected_competitor:
                break

    # Детектор конкурентного запиту — "конкуренти X", "X vs Y", "чим відрізняється X"
    _COMPETITOR_QUERY_KW = [
        "конкурент", "competitor", "відрізняєть", "відмінність", "порівняй", "порівняння",
        " vs ", "проти ", "чим кращ", "кращ за", "краще ніж", "аналог"
    ]
    _is_competitor_query = any(kw in t_lower for kw in _COMPETITOR_QUERY_KW)

    # Таблиця конкурентів по препарату — для розширення search_query
    # Задокументовані конкуренти (всі мають окремі розділи "Конкурентний аналіз" в ChromaDB)
    _PRODUCT_COMPETITOR_HINTS = {
        "Vitaran":  "Rejuran KIARA TWAC Pluryal Plinest Nucleofill PDRN порівняння конкурентний аналіз концентрація сировина",
        "Ellansé":  "Radiesse Sculptra Juvederm Juvelook Aesthefill полімолочна кислота PCL полікапролактон неоколагенез колаген відмінність порівняння аргументи",
        "Petaran":  "Sculptra AestheFill Juvelook PLLA полімолочна кислота порівняння аргументи конкуренти",
        "Neuramis": "Juvederm Teosyal Restylane філери HA порівняння аргументи",
        "EXOXE":    "PRP плазмотерапія PDRN Rejuran екзосоми порівняння",
        "IUSE Collagen": "скінбустер бустер колаген порівняння",
        "ESSE":     "космецевтика пробіотик лінійка показання",
        "Magnox":   "магній колаген показання",
    }

    # Маппінг конкурент → EMET-продукт для запитів типу "Розкажи про лінійку Plinest/Реджуран"
    # Якщо EMET-продукт не згаданий, але конкурент є → заповнюємо _canonical для правильного RAG
    _COMPETITOR_TO_CANONICAL = {
        "plinest": "Vitaran",       "плінест": "Vitaran",
        "rejuran": "Vitaran",       "реджуран": "Vitaran",
        "kiara": "Vitaran",         "twac": "Vitaran",
        "nucleofill": "Vitaran",    "нуклеофіл": "Vitaran",
        "sculptra": "Petaran",      "скульптра": "Petaran",
        "aesthefill": "Petaran",    "естефіл": "Petaran",
        "juvelook": "Petaran",
        "radiesse": "Ellansé",      "радіесс": "Ellansé",
        "juvederm": "Neuramis",     "ювідерм": "Neuramis",
        "teosyal": "Neuramis",      "теосял": "Neuramis",
        "restylane": "Neuramis",    "рестилайн": "Neuramis",
    }
    if not _canonical and _detected_competitor and mode_key == "coach":
        _mapped = _COMPETITOR_TO_CANONICAL.get(_detected_competitor)
        if _mapped:
            _canonical = _mapped
            _canonical_from_competitor = True

    # Збагачуємо search_query продуктом — щоб RAG шукав по потрібному препарату, а не random
    if _canonical and mode_key == "coach":
        _INFO_FOLLOWUP_KW = ["що таке", "что такое", "розкажи", "расскажи", "чим відрізняєть", "чем отличается"]
        _is_seminar_req = any(kw in t_lower for kw in ["семінар", "семинар"])
        if _is_script_request and _is_seminar_req:
            search_query = f"скрипт запрошення семінар захід {_canonical}"
        elif _is_script_request or (_is_coach_followup and not any(kw in t_lower for kw in _INFO_FOLLOWUP_KW)):
            search_query = f"скрипт аргументи заперечення діалог {_canonical}"
        elif len(_all_canonicals) > 1 and not _is_competitor_query:
            # Кілька EMET-продуктів в одному запиті — об'єднуємо назви + підказки для RAG
            _multi_hints = " ".join(
                _PRODUCT_COMPETITOR_HINTS.get(c, "")
                for c in _all_canonicals if c in _PRODUCT_COMPETITOR_HINTS
            )
            search_query = (" ".join(_all_canonicals) + " лінійка відмінність " + _multi_hints).strip()
        elif _canonical_from_competitor:
            # Запит про конкурентний продукт без згадки EMET — беремо всі конкурентні матеріали EMET-продукту
            _comp_hint = _PRODUCT_COMPETITOR_HINTS.get(_canonical, "конкуренти порівняння аргументи")
            search_query = f"{_detected_competitor} {_canonical} {_comp_hint}"
        elif _is_competitor_query or (_has_objection and _detected_competitor):
            # Конкурентний запит — явно вказуємо назви конкурентів для RAG
            _comp_hint = _PRODUCT_COMPETITOR_HINTS.get(_canonical, "конкуренти порівняння аргументи")
            search_query = f"{_canonical} {_comp_hint}"
        elif _has_objection:
            search_query = f"заперечення {_canonical} {search_query or text}"
        elif _canonical.lower() not in (search_query or "").lower():
            search_query = f"{_canonical} {search_query or text}"

    # Ellansé конкурентний запит — явно вказуємо PCL як перший аргумент
    _is_ellanse_competitor = (
        _canonical == "Ellansé"
        and (_is_competitor_query or _detected_competitor or any(
            kw in t_lower for kw in ["полімолочн", "полимолочн", "juvelook", "radiesse", "sculptra", "відрізняєть", "отличается"]
        ))
    )

    if mode_key == "coach" and _detected_product and _has_objection and not _is_script_request and not _is_coach_followup:
        # Заперечення + продукт → SOS-формат (тільки якщо НЕ запит на скрипт/діалог)
        llm_user_text = (
            f"[СИСТЕМА: продукт — {_canonical}. "
            f"Дай SOS-відповідь: коротка готова фраза менеджера + 2-3 тезиси. "
            f"⛔ НЕ починай з аргументу тривалості дії. Перший аргумент — фінансова вигода лікаря або унікальний механізм.]\n\n"
            f"ПИТАННЯ:\n{text}"
        )
    elif mode_key == "coach" and _is_ellanse_competitor:
        # Ellansé vs конкурент — підказуємо LLM що PCL є головним диференціатором
        llm_user_text = (
            f"[СИСТЕМА: продукт — Ellansé (PCL — полікапролактон). "
            f"Порівнюючи з конкурентом, ПЕРШИЙ аргумент — механізм PCL: негайний об'єм (CMC-гель) + неоколагенез I типу (PCL-мікросфери). "
            f"Якщо конкурент — полімолочна кислота (Juvelook, Aesthefill, PLLA): підкресли що Ellansé = об'єм + ліфтинг одночасно, а не лише біостимуляція. "
            f"Якщо Radiesse: Ellansé дає програмований термін дії (S/M), без ризику міграції кальцію.]\n\n"
            f"ПИТАННЯ:\n{text}"
        )
    elif mode_key == "coach" and _is_script_request and _canonical:
        # Запит на скрипт/діалог
        _is_seminar_script = any(kw in t_lower for kw in ["семінар", "семинар"])
        if _is_seminar_script:
            llm_user_text = (
                f"[СИСТЕМА: продукт — {_canonical}. "
                f"Дай SOS-скрипт запрошення лікаря на семінар по {_canonical}: "
                f"відкриваюче питання + 3-4 гілки (час/зайнятість, 'і так знаю', незручно, 'не потрібно') + ультра-версія 10 сек. "
                f"Формат: ⚡ SOS-скрипт з 💬 Крок 1 → 💬 Крок 2 → 🎯 Суть → ультра-версія.]\n\n"
                f"ПИТАННЯ:\n{text}"
            )
        elif _history_objection:
            llm_user_text = (
                f"[СИСТЕМА: продукт — {_canonical}. "
                f"Дай скрипт-діалог менеджера з лікарем. "
                f"Контекст: заперечення «{_history_objection}» — діалог має відпрацьовувати саме його.]\n\n"
                f"ПИТАННЯ:\n{text}"
            )
        else:
            # Ellansé + конкурент → підкреслюємо доповнення портфеля (PCL ≠ PLLA/CaHA — різні механізми)
            # Для інших продуктів (Neuramis vs Juvederm, Vitaran vs Rejuran) — задача "переключити", не "доповнити"
            _competitor_ctx = (
                f" Лікар вже працює з {_detected_competitor.title()} — підкресли що Ellansé ДОПОВНЮЄ портфель (PCL vs {_detected_competitor.title()}), не замінює. Не критикуй {_detected_competitor.title()}."
                if (_canonical == "Ellansé" and _detected_competitor) else ""
            )
            llm_user_text = (
                f"[СИСТЕМА: продукт — {_canonical}. Дай скрипт-діалог менеджера з лікарем.{_competitor_ctx}]\n\n"
                f"ПИТАННЯ:\n{text}"
            )
    elif mode_key == "coach" and _canonical_from_competitor and not _is_script_request and not _has_objection:
        # Запит про конкурентний продукт без EMET-продукту в тексті ("Розкажи про лінійку Plinest/Реджуран")
        # Даємо нейтральний фактаж + закриваємо на диференціацію EMET
        llm_user_text = (
            f"[СИСТЕМА: запит — інформація про конкурента '{_detected_competitor}' (наш конкурент). "
            f"ФОРМАТ відповіді: "
            f"1) Нейтральний фактаж з контексту: склад, механізм, для кого. "
            f"БЕЗ позитивних оцінок ('чудові результати', 'ефективний', 'популярний') — не продаємо конкурента. "
            f"Якщо є лише часткові дані — скажи 'У нашому конкурентному аналізі є такі дані: ...' і дай те що є. "
            f"2) Плавний перехід: 'А ось чим наш {_canonical} відрізняється...' + 2-3 ключові переваги з контексту.]\n\n"
            f"ПИТАННЯ:\n{text}"
        )
    elif mode_key == "coach" and _is_coach_followup:
        # Продовження тренінгу — "інші аргументи", "розпиши детально"
        # Визначаємо що вже відкинув лікар (щоб не повторювати)
        _t_lower_fu = text.lower()
        _avoid_hint = ""
        for _neg_marker in ["не интересует", "не цікавить", "не важно", "не важливо", "не актуально", "не актуальн"]:
            if _neg_marker in _t_lower_fu:
                _avoid_hint = "Уникай аргументів, які менеджер чи лікар вже відкинув або назвав неактуальними. "
                break
        _prod_ctx = f"продукт — {_canonical}. " if _canonical else ""
        _obj_ctx = f"Заперечення в контексті: «{_history_objection}». " if _history_objection else ""
        # Якщо коротка афірмація ("Хочу", "Так", "Да нужно") — просимо продовжити те що пропонував
        if _is_short_affirmation:
            llm_user_text = (
                f"[СИСТЕМА: {_prod_ctx}{_obj_ctx}"
                f"Користувач відповів коротко: «{text}» — це підтвердження запиту з попереднього повідомлення. "
                f"Виконай саме те, що ти пропонував або на що натякав в останній відповіді. "
                f"Якщо не зрозуміло що саме — дай детальний розбір або наступний рівень деталізації по темі.]\n\n"
                f"ПИТАННЯ:\n{text}"
            )
        else:
            llm_user_text = (
                f"[СИСТЕМА: {_prod_ctx}{_obj_ctx}"
                f"Дай 3-5 НОВИХ конкретних аргументів або готових фраз менеджера. "
                f"{_avoid_hint}"
                f"ФОРМАТ: тільки готові фрази/аргументи — БЕЗ нумерованих секцій (1️⃣2️⃣3️⃣), БЕЗ повного 7-секційного розбору. "
                f"Максимум 8 рядків. НЕ починай з нуля.]\n\n"
                f"ПИТАННЯ:\n{text}"
            )
    else:
        llm_user_text = text

    ai_used = "OpenAI"
    context, sources = "", {}
    answer = None
    _tokens_in = 0
    _tokens_out = 0
    _model_used = None
    _context_was_empty = False
    _t_start = time.monotonic()

    # Отправляем placeholder — пользователь сразу видит что бот думает
    sent_msg = await message.answer("⏳")

    global _openai_quota_exceeded
    _openai_attempts = 0
    while _openai_attempts < 2 and answer is None and not _openai_quota_exceeded:
        _openai_attempts += 1
        try:
            loop = asyncio.get_running_loop()
            _model = MODEL_OPENAI_COACH if mode_key in ("coach", "combo") else MODEL_OPENAI
            context, sources = await loop.run_in_executor(None, get_context, search_query, mode_key, "openai", bool(_detected_competitor))
            if not context.strip():
                _context_was_empty = True
            stream = await client_openai.chat.completions.create(
                model=_model,
                timeout=LLM_TIMEOUT,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPTS[mode_key]},
                    *chat_history,
                    {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\n⚠️ НАГАДУВАННЯ: 1) Цифри ТІЛЬКИ з контексту 2) Дані ⚠️КОНКУРЕНТ = чужі, не наші 3) Пріоритет: 📘LMS > інші 4) Перед 'немає інформації' — перечитай ВЕСЬ контекст\n\nВОПРОС:\n{llm_user_text}"}
                ],
                stream=True,
                stream_options={"include_usage": True},
            )
            chunks = []
            last_edit = asyncio.get_running_loop().time()
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    chunks.append(delta)
                    now = asyncio.get_running_loop().time()
                    if now - last_edit >= STREAM_UPDATE_INTERVAL:
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
            if _openai_quota_exceeded:
                # Баланс поповнено і OpenAI знову відповів — сповіщаємо адміна
                _openai_quota_exceeded = False
                if ADMIN_ID:
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            "✅ *OpenAI баланс поповнено!*\n\nБот повернувся на OpenAI.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
            else:
                _openai_quota_exceeded = False
        except Exception as e_openai:
            err_str = str(e_openai)
            logger.warning("OpenAI attempt %d failed: %s", _openai_attempts, e_openai)
            # Якщо закінчився баланс — ретрай безглуздий, одразу на Gemini + сповіщаємо адміна
            _is_quota_error = (
                isinstance(e_openai, OpenAIRateLimitError)
                or "insufficient_quota" in err_str
                or "exceeded your current quota" in err_str
            )
            if _is_quota_error:
                _openai_quota_exceeded = True
                logger.warning("OpenAI quota exceeded — falling back to Gemini")
                if ADMIN_ID:
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            "⚠️ *OpenAI баланс вичерпано!*\n\n"
                            "Бот автоматично переключився на Gemini.\n"
                            "Поповни баланс: https://platform.openai.com/account/billing",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                break  # не ретраїти, одразу fallback
            elif _openai_attempts < 2:
                await asyncio.sleep(1)

    if answer is None:
        ai_used = "Google"
        try:
            loop = asyncio.get_running_loop()
            context, sources = await loop.run_in_executor(None, get_context, search_query, mode_key, "google", bool(_detected_competitor))
            # Structured chat for Gemini — separate system, history, and user turns
            gemini_contents = [{"role": "user", "parts": [{"text": SYSTEM_PROMPTS[mode_key]}]},
                               {"role": "model", "parts": [{"text": "Зрозуміло, працюю за інструкціями."}]}]
            for msg in chat_history:
                g_role = "user" if msg["role"] == "user" else "model"
                gemini_contents.append({"role": g_role, "parts": [{"text": msg["content"]}]})
            gemini_contents.append({"role": "user", "parts": [{"text": f"КОНТЕКСТ:\n{context}\n\n⚠️ НАГАДУВАННЯ: 1) Цифри ТІЛЬКИ з контексту 2) Дані ⚠️КОНКУРЕНТ = чужі, не наші 3) Пріоритет: 📘LMS > інші 4) Перед 'немає інформації' — перечитай ВЕСЬ контекст\n\nВОПРОС:\n{llm_user_text}"}]})
            res = await loop.run_in_executor(
                None,
                lambda: client_google.models.generate_content(model=MODEL_GOOGLE, contents=gemini_contents,
                                                               config={"timeout": 60})
            )
            answer = res.text
            if hasattr(res, "usage_metadata") and res.usage_metadata:
                _tokens_in = getattr(res.usage_metadata, "prompt_token_count", 0) or 0
                _tokens_out = getattr(res.usage_metadata, "candidates_token_count", 0) or 0
            _model_used = MODEL_GOOGLE
        except Exception as e_google:
            logger.error("Google Gemini unavailable: %s", e_google)
            if ADMIN_ID:
                try:
                    await bot.send_message(ADMIN_ID,
                        f"⚠️ *Gemini недоступний!*\n\n`{type(e_google).__name__}: {str(e_google)[:200]}`\n\nБот переключився на Claude.",
                        parse_mode="Markdown")
                except Exception:
                    pass

    if answer is None and client_claude:
        ai_used = "Claude"
        try:
            loop = asyncio.get_running_loop()
            context, sources = await loop.run_in_executor(None, get_context, search_query, mode_key, "openai", bool(_detected_competitor))
            claude_msg = await client_claude.messages.create(
                model=MODEL_CLAUDE,
                max_tokens=LLM_CLAUDE_MAX_TOKENS,
                timeout=LLM_TIMEOUT,
                system=SYSTEM_PROMPTS[mode_key],
                messages=[
                    *chat_history,
                    {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\n⚠️ НАГАДУВАННЯ: 1) Цифри ТІЛЬКИ з контексту 2) Дані ⚠️КОНКУРЕНТ = чужі, не наші 3) Пріоритет: 📘LMS > інші 4) Перед 'немає інформації' — перечитай ВЕСЬ контекст\n\nВОПРОС:\n{llm_user_text}"}
                ]
            )
            answer = claude_msg.content[0].text
            _tokens_in = claude_msg.usage.input_tokens
            _tokens_out = claude_msg.usage.output_tokens
            _model_used = MODEL_CLAUDE
        except Exception as e_claude:
            logger.error("Claude unavailable: %s", e_claude)
            if ADMIN_ID:
                try:
                    await bot.send_message(ADMIN_ID,
                        f"🚨 *Claude недоступний!*\n\n`{type(e_claude).__name__}: {str(e_claude)[:200]}`\n\nВсі LLM недоступні — бот не відповів на запит.",
                        parse_mode="Markdown")
                except Exception:
                    pass

    if answer is None:
        await sent_msg.edit_text("Вибачте, сервери ШІ зараз перевантажені. Спробуйте через хвилину.")
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
    # Fix **bold** → *bold* (Telegram doesn't render double asterisks)
    answer = re.sub(r'\*\*(.+?)\*\*', r'*\1*', answer)

    if used_links:
        final_links = list(set(used_links))
        answer += "\n\n*Ознайомитись з документами:*\n" + "\n".join(final_links)

    _uname = message.from_user.username or f"id{message.from_user.id}"
    _has_source = bool(sources) and not _context_was_empty
    _log_id = log_to_db(message.from_user.id, _uname, mode_key, ai_used, text, answer, _has_source, _model_used, _tokens_in, _tokens_out)

    # Зберігаємо історію діалогу для всіх режимів
    # coach: 3 обміни (6 повідомлень), решта: 2 обміни (4 повідомлення)
    if mode_key in ("coach", "combo", "kb", "cases", "operational", "certs"):
        clean_answer = re.sub(r'\[?REF\d+\]?', '', answer).strip()
        chat_history.append({"role": "user", "content": text})
        chat_history.append({"role": "assistant", "content": clean_answer})
        limit = CHAT_HISTORY_COACH if mode_key == "coach" else CHAT_HISTORY_DEFAULT
        chat_history = chat_history[-limit:]
        await state.update_data(chat_history=chat_history)
        _save_chat_history_db(message.from_user.id, chat_history)

    if _context_was_empty and ADMIN_ID:
        mode_label = {"kb": "База знань", "coach": "Sales Coach", "combo": "Комбо", "cases": "Кейси", "operational": "Операційні"}.get(mode_key, mode_key)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"*Пропуск в RAG [{mode_label}]!*\n@{message.from_user.username} спросил:\n_{text}_",
                parse_mode="Markdown"
            )
        except Exception as admin_err:
            logger.error("admin notification error: %s", admin_err)

    _latency = time.monotonic() - _t_start
    logger.info(
        "query uid=%s mode=%s model=%s latency=%.1fs ctx_empty=%s tokens_in=%d tokens_out=%d",
        message.from_user.id, mode_key, _model_used, _latency, _context_was_empty, _tokens_in, _tokens_out
    )
    if _context_was_empty:
        logger.warning("context_was_empty uid=%s mode=%s query=%r", message.from_user.id, mode_key, text[:100])

    await send_paginated(message, state, answer, sent_msg=sent_msg)

    # Кнопки оцінки відповіді (coach і kb)
    if mode_key in ("coach", "kb") and _log_id:
        fb_builder = InlineKeyboardBuilder()
        fb_builder.button(text="👍", callback_data=f"fb_up_{_log_id}")
        fb_builder.button(text="👎", callback_data=f"fb_dn_{_log_id}")
        fb_builder.adjust(2)
        await message.answer("Відповідь була корисною?", reply_markup=fb_builder.as_markup())

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

    # Check if any option is too long for inline button (Telegram limit ~64 chars visible)
    max_btn_len = 50
    long_options = any(len(opt["text"]) > max_btn_len for opt in options)

    builder = InlineKeyboardBuilder()
    if long_options:
        # Long options: show lettered list in text (A/B/C/D), buttons = letters
        letters = "ABCDEFGH"
        option_lines = []
        for i, opt in enumerate(options):
            letter = letters[i] if i < len(letters) else str(i + 1)
            option_lines.append(f"{letter}) {opt['text']}")
            builder.button(
                text=f"{letter}",
                callback_data=f"lms_answer_{opt['id']}_{int(opt['is_correct'])}"
            )
        options_text = "\n\n".join(option_lines)
        question_text = f"*Питання {index + 1}/{total}*\n\n{q['text']}\n\n{options_text}"
    else:
        # Short options: show directly on buttons
        for opt in options:
            builder.button(
                text=opt["text"],
                callback_data=f"lms_answer_{opt['id']}_{int(opt['is_correct'])}"
            )
        question_text = f"*Питання {index + 1}/{total}*\n\n{q['text']}"

    builder.adjust(1)
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
            timeout=30,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.4,
        )
        analysis = response.choices[0].message.content.strip()
        await message.answer(f"📊 *Аналіз результатів*\n\n{analysis}", parse_mode="Markdown")
    except Exception as e:
        # Якщо GPT недоступний — мовчки пропускаємо
        logger.error("test_analysis error: %s", e)


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
        "*📋 HR і регламенти*\n"
        "Корпоративні правила, відпустки, структура компанії, документи.\n\n"
        "*💼 Sales Коуч*\n"
        "Препарати, склади, порівняння, скрипти продажів.\n"
        "Режими: вільний діалог, 🆘 SOS-підготовка, робота із запереченнями, сезонні скрипти.\n\n"
        "*🎓 Навчання і тести*\n"
        "Курси з продуктів з уроками та тестами. Є звичайні та 🏆 сертифікаційні тести.\n"
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
    if role not in ("admin", "director") and message.from_user.id != ADMIN_ID:
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

    # Один batch-запит для тестів всіх менеджерів
    manager_ids = [str(uid) for uid, _, _ in managers]
    placeholders = ",".join(["%s"] * len(manager_ids))
    test_stats = {
        r[0]: r for r in db.query(
            f"SELECT user_id, COUNT(*), SUM(passed), COALESCE(AVG(score),0) "
            f"FROM user_progress WHERE user_id IN ({placeholders}) GROUP BY user_id",
            tuple(manager_ids)
        )
    }
    onb_stats = {
        r[0]: r[1] for r in db.query(
            f"SELECT user_id, COALESCE(SUM(completed),0) "
            f"FROM onboarding_progress WHERE user_id IN ({placeholders}) GROUP BY user_id",
            tuple(manager_ids)
        )
    }

    lines = ["👥 *Прогрес команди менеджерів*\n"]
    for uid, fname, uname in managers:
        name = fname or uname or uid
        uid_s = str(uid)

        t = test_stats.get(uid_s)
        tests_done   = int(t[1] or 0) if t else 0
        tests_passed = int(t[2] or 0) if t else 0
        avg_score    = float(t[3] or 0) if t else 0.0

        onb_done = int(onb_stats.get(uid_s) or 0)
        onb_pct  = round(onb_done / total_onb * 100) if total_onb else 0

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
    builder.button(text="✅ Вірно, шукати", callback_data="voice_confirm_yes")
    builder.button(text="✏️ Уточнити запит", callback_data="voice_confirm_no")
    builder.adjust(2)

    msg = "Ваш запит:\n" + f"*{text}*" + "\n\nВсе вірно?"
    try:
        await message.answer(msg, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        await message.answer("Ваш запит:\n" + text + "\n\nВсе вірно?", reply_markup=builder.as_markup())


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
    await callback.message.answer("Напишіть ваше запитання текстом.")


# --- ГОЛОСОВЫЕ СООБЩЕНИЯ ---
@dp.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ заборонено. Зверніться до адміністратора EMET.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        file = await bot.get_file(message.voice.file_id)
        voice_bytes = await bot.download_file(file.file_path)

        transcript = await client_openai.audio.transcriptions.create(
            model="whisper-1",
            timeout=60,
            file=("voice.ogg", voice_bytes.read(), "audio/ogg"),
        )
        text = transcript.text.strip()

        if not text:
            return await message.answer("Не вдалося розпізнати голосове повідомлення. Спробуйте ще раз.")

        await _ask_voice_confirm(message, state, text)

    except Exception as e:
        logger.error("voice recognition error: %s", e)
        await message.answer("Не вдалося обробити голосове повідомлення.")


# --- ФОТО / СКРИНШОТЫ ---
@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        await message.answer("⛔ Доступ заборонено. Зверніться до адміністратора EMET.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        photo_bytes = await bot.download_file(file.file_path)

        img_b64 = base64.b64encode(photo_bytes.read()).decode()
        caption = message.caption or "Опиши що на скріншоті і сформулюй запитання для пошуку в базі знань компанії."

        response = await client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Користувач надіслав скріншот. Його запитання/контекст: '{caption}'.\n"
                            "Опиши що ти бачиш на зображенні і сформулюй один конкретний текстовий запит "
                            "для пошуку відповіді в корпоративній базі знань. Тільки запит, без зайвих слів."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                    }
                ]
            }],
            max_tokens=150,
            timeout=30,
        )

        extracted_query = response.choices[0].message.content.strip()
        await _ask_voice_confirm(message, state, extracted_query)

    except Exception as e:
        logger.error("photo processing error: %s", e)
        await message.answer("Не вдалося обробити зображення. Спробуйте описати запитання текстом.")


# --- ПЕРЕКЛЮЧЕНИЕ РЕЖИМОВ ---
@dp.callback_query(F.data == "set_kb")
async def mode_kb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_kb)
    await callback.message.answer("📋 Режим: *База знань*. Задайте запитання по регламентах компанії.", parse_mode="Markdown")
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
    _save_chat_history_db(callback.from_user.id, [])
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
    _save_chat_history_db(callback.from_user.id, [])
    await callback.message.answer("💬 Вільний діалог. Опишіть запит клієнта або заперечення.")
    await callback.answer()


@dp.callback_query(F.data == "coach_sos")
async def coach_sos(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    _save_chat_history_db(callback.from_user.id, [])
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
    _save_chat_history_db(callback.from_user.id, [])
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
    _save_chat_history_db(callback.from_user.id, [])
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


CERTS_BRANDS = [
    ("Vitaran",     "Vitaran"),
    ("Ellanse",     "Ellanse"),
    ("Neuramis",    "Neuramis"),
    ("Petaran",     "Petaran"),
    ("Neuronox",    "Neuronox"),
    ("ESSE",        "ESSE"),
    ("IUSE",        "IUSE"),
    ("EXOXE",       "EXOXE"),
    ("SkinBooster", "SkinBooster"),
    ("Magnox",      "Magnox"),
]

def build_certs_brand_menu():
    builder = InlineKeyboardBuilder()
    for label, brand in CERTS_BRANDS:
        builder.button(text=f"💊 {label}", callback_data=f"certs_brand_{brand}")
    builder.button(text="🏠 Головне меню", callback_data="go_home")
    builder.adjust(2)
    return builder.as_markup()


@dp.callback_query(F.data == "coach_certs")
async def coach_certs(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_certs)
    await state.update_data(chat_history=[], cert_files=[])
    _save_chat_history_db(callback.from_user.id, [])
    await callback.message.answer(
        "📜 *Сертифікати та реєстраційні документи*\n\nОберіть препарат:",
        parse_mode="Markdown",
        reply_markup=build_certs_brand_menu()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("certs_brand_"))
async def certs_by_brand(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_certs)
    brand = callback.data.replace("certs_brand_", "")
    await callback.answer()
    rows = db.query_dict(
        "SELECT file_name, file_id FROM sync_state WHERE folder_label = 'certs' AND LOWER(file_name) LIKE %s ORDER BY file_name",
        (f"%%{brand.lower()}%%",)
    )
    if not rows:
        await callback.message.answer(
            f"Документів по *{brand}* не знайдено.",
            parse_mode="Markdown",
            reply_markup=build_certs_brand_menu()
        )
        return
    cert_files = [{"file_id": r["file_id"], "name": r["file_name"]} for r in rows if r.get("file_id")]
    await state.update_data(cert_files=cert_files)
    builder = InlineKeyboardBuilder()
    for idx, cf in enumerate(cert_files[:8]):
        short_name = cf["name"][:40] + "…" if len(cf["name"]) > 40 else cf["name"]
        builder.button(text=f"📥 {short_name}", callback_data=f"cert_dl_{idx}")
    builder.button(text="◀️ Назад", callback_data="coach_certs")
    builder.button(text="🏠 Головне меню", callback_data="go_home")
    builder.adjust(1)
    await callback.message.answer(
        f"📜 *{brand}* — знайдено {len(cert_files)} документ(ів):",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("cert_dl_"))
async def download_cert(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("⏳ Завантажую...")
    idx = int(callback.data.replace("cert_dl_", ""))
    data = await state.get_data()
    cert_files = data.get("cert_files", [])
    if idx >= len(cert_files):
        await callback.message.answer("Файл не знайдено. Спробуйте зробити новий запит.")
        return
    cf = cert_files[idx]
    try:
        loop = asyncio.get_running_loop()

        def _download():
            drive = get_drive_service()
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=cf["file_id"]))
            done = False
            while not done:
                _, done = dl.next_chunk()
            buf.seek(0)
            return buf

        buf = await loop.run_in_executor(None, _download)
        await callback.message.answer_document(
            types.BufferedInputFile(buf.read(), filename=cf["name"]),
            caption=f"📄 {cf['name']}"
        )
    except Exception as e:
        await callback.message.answer(f"Помилка завантаження: {e}")


@dp.callback_query(F.data == "coach_seasonal")
async def coach_seasonal(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_coach)
    await state.update_data(chat_history=[])
    _save_chat_history_db(callback.from_user.id, [])
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
    await callback.message.answer("Головне меню:", reply_markup=get_main_menu())
    await callback.answer()


# --- FEEDBACK (👍/👎) ---
@dp.callback_query(F.data.startswith("fb_"))
async def handle_feedback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer()
        return
    _, direction, log_id_str = parts
    rating = 1 if direction == "up" else -1
    try:
        log_id = int(log_id_str)
        # Перевіряємо чи вже оцінював (один голос на лог)
        existing = db.query("SELECT id FROM feedback WHERE log_id=%s AND user_id=%s", (log_id, str(callback.from_user.id)), fetchone=True)
        if existing:
            await callback.answer("Ви вже оцінили цю відповідь.")
            return
        # Отримуємо mode з logs
        log_row = db.query("SELECT mode FROM logs WHERE id=%s", (log_id,), fetchone=True)
        mode = log_row[0] if log_row else "unknown"
        db.execute(
            "INSERT INTO feedback (log_id, user_id, rating, mode) VALUES (%s,%s,%s,%s)",
            (log_id, str(callback.from_user.id), rating, mode)
        )
        await callback.message.edit_text("Дякую за оцінку!" if rating == 1 else "Зрозуміло, працюємо над покращенням!")
    except Exception as e:
        logger.error("feedback handler error: %s", e)
    await callback.answer()


# --- LMS: СПИСОК КУРСОВ ---
@dp.callback_query(F.data == "set_learn")
async def show_courses(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserState.mode_learning)
    courses = get_courses()

    if not courses:
        await callback.message.answer("🎓 Курсів поки немає. Зверніться до адміністратора.")
        await callback.answer()
        return

    COURSE_ICONS = {
        "vitaran":   "🧬",
        "ellans":    "⏳",
        "exohe":     "🔬",
        "exosom":    "🔬",
        "нейронокс": "💉",
        "neuronox":  "💉",
        "petaran":   "🌿",
        "neuramis":  "💎",
        "juvederm":  "💠",
        "radiess":   "✨",
        "sculptra":  "🕊",
        "pluryal":   "🫧",
        "nucleofil": "🧪",
    }

    def _course_icon(title: str) -> str:
        t = title.lower()
        for kw, icon in COURSE_ICONS.items():
            if kw in t:
                return icon
        return "📖"

    def _course_label(title: str) -> str:
        """Strip common repetitive suffixes for cleaner button text."""
        for suffix in [" — базовий курс продажів", " — базовий курс", " — курс продажів", " — курс"]:
            if title.lower().endswith(suffix.lower()):
                return title[: len(title) - len(suffix)].strip()
        return title

    builder = InlineKeyboardBuilder()
    for c_id, title, _ in courses:
        icon = _course_icon(title)
        label = _course_label(title)
        builder.button(text=f"{icon}  {label}", callback_data=f"lms_course_{c_id}")
    builder.button(text="⬅️ Головне меню", callback_data="go_home")
    builder.adjust(1)

    await callback.message.answer(
        "*🎓 Навчання EMET*\n\nОберіть курс:",
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

    # Один batch-запит замість N окремих get_user_progress
    topic_ids = [t[0] for t in topics]
    progress_map = {}
    if topic_ids:
        placeholders = ",".join(["%s"] * len(topic_ids))
        rows = db.query(
            f"SELECT topic_id, passed, score, attempts FROM user_progress "
            f"WHERE user_id=%s AND topic_id IN ({placeholders})",
            (str(user_id), *topic_ids)
        )
        progress_map = {r[0]: (r[1], r[2], r[3]) for r in rows}

    builder = InlineKeyboardBuilder()
    for t_id, order_num, title, is_cert, max_att in topics:
        progress = progress_map.get(t_id)
        # Trim long titles for Telegram button limit
        short_title = title[:35] + "…" if len(title) > 38 else title
        icon = "🏆" if is_cert else "⬜"
        if progress and progress[0]:
            icon = "✅"
            label = f"{icon} {order_num}. {short_title} ({progress[1]}%)"
        elif progress and is_cert and max_att and progress[2] >= max_att:
            label = f"🔒 {order_num}. {short_title} — вичерпано"
        elif progress:
            label = f"🔄 {order_num}. {short_title} (розпочато)"
        else:
            label = f"{icon} {order_num}. {short_title}"
        builder.button(text=label, callback_data=f"lms_topic_{t_id}")

    builder.button(text="⬅️ До курсів", callback_data="set_learn")
    builder.adjust(1)

    await callback.message.answer(
        "*Теми курсу:*\n\n✅ — пройдено  🔄 — розпочато  ⬜ — не розпочато",
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
            text=f"✅ Складено ({progress[1]}%) — пройти знову",
            callback_data=f"lms_starttest_{topic_id}"
        )
    else:
        builder.button(text="📝 Пройти тест", callback_data=f"lms_starttest_{topic_id}")

    builder.button(text="⬅️ До тем", callback_data=f"lms_course_{course_id}")
    builder.adjust(1)

    TG_LIMIT = 4096
    lesson_text = f"*{title}*{cert_label}\n\n{content}{attempts_info}"

    if len(lesson_text) <= TG_LIMIT:
        try:
            await callback.message.answer(lesson_text, parse_mode="Markdown", reply_markup=builder.as_markup())
        except TelegramBadRequest:
            await callback.message.answer(lesson_text, reply_markup=builder.as_markup())
    else:
        # Split: send content chunks first, keyboard only on last chunk
        header = f"*{title}*{cert_label}\n\n"
        chunks = []
        remaining = content
        first = True
        while remaining:
            prefix = header if first else ""
            suffix = attempts_info if not remaining[TG_LIMIT - len(prefix):] else ""
            chunk = prefix + remaining[: TG_LIMIT - len(prefix) - len(suffix)] + suffix
            chunks.append(chunk)
            remaining = remaining[TG_LIMIT - len(prefix) - len(suffix):]
            first = False

        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            markup = builder.as_markup() if is_last else None
            try:
                await callback.message.answer(chunk, parse_mode="Markdown", reply_markup=markup)
            except TelegramBadRequest:
                await callback.message.answer(chunk, reply_markup=markup)

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
    wrong_answers = data.get('lms_wrong_answers', [])

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


# --- LMS: перехоплення тексту під час тесту ---
@dp.message(StateFilter(UserState.lms_test_active))
async def handle_text_in_test(message: types.Message):
    if message.text:
        await message.answer("Оберіть варіант відповіді з кнопок вище.")


# --- АДМІН: ЗАВАНТАЖЕННЯ КУРСУ ЧЕРЕЗ JSON ---
@dp.callback_query(F.data == "admin_upload")
async def ask_json(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ У вас немає прав адміністратора.", show_alert=True)
    await callback.message.answer("Надішліть JSON-файл з навчальним курсом.")
    await state.set_state(UserState.waiting_for_json)


@dp.message(UserState.waiting_for_json, F.document)
async def process_json(message: types.Message, state: FSMContext):
    file = await bot.get_file(message.document.file_id)
    content = await bot.download_file(file.file_path)
    json_str = content.read().decode('utf-8')
    try:
        data = json.loads(json_str)
        save_course(data.get('title', 'Без назви'), json_str)
        await message.answer(f"✅ Курс '{data.get('title')}' завантажено!")
        await state.set_state(UserState.mode_kb)
    except Exception as e:
        await message.answer(f"❌ Помилка валідації JSON: {e}")


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
        f"Тем опрацьовано: *{len(test_rows)}*\n"
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
    UserState.mode_cases, UserState.mode_operational, UserState.mode_onboarding,
    UserState.mode_certs, None
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


# Ціни моделей ($/1M токенів) — синхронізовано з admin_panel.py PRICES
_MODEL_PRICES = {
    "gpt-4o":            {"in": 2.50,  "out": 10.00},
    "gpt-4o-mini":       {"in": 0.15,  "out":  0.60},
    "gemini-2.0-flash":  {"in": 0.10,  "out":  0.40},
    "claude-sonnet-4-6": {"in": 3.00,  "out": 15.00},
}
DAILY_BUDGET_LIMIT = float(os.getenv("DAILY_BUDGET_LIMIT", "10.0"))  # USD


def _calc_row_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    m = (model or "").lower()
    for key, p in _MODEL_PRICES.items():
        if key in m:
            return (tokens_in * p["in"] + tokens_out * p["out"]) / 1_000_000
    return 0.0


async def daily_cost_task():
    """Щодня о 23:00 надсилає адміну звіт витрат. Якщо > DAILY_BUDGET_LIMIT — алерт одразу."""
    from datetime import date, timedelta
    while True:
        now = datetime.now()
        # Перевірка бюджету раз на годину протягом дня
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        await asyncio.sleep((next_hour - now).total_seconds())

        try:
            today = date.today().isoformat()
            rows = db.query_dict(
                "SELECT model, tokens_in, tokens_out FROM logs WHERE date >= %s",
                (today + " 00:00:00",)
            )
            today_cost = sum(_calc_row_cost(r.get("model",""), r.get("tokens_in") or 0, r.get("tokens_out") or 0) for r in rows)
            today_calls = len(rows)

            # Алерт якщо перевищено денний бюджет
            if today_cost >= DAILY_BUDGET_LIMIT:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *EMET: перевищено денний бюджет!*\n\n"
                    f"Витрачено сьогодні: *${today_cost:.3f}* з ліміту ${DAILY_BUDGET_LIMIT:.2f}\n"
                    f"Запитів: {today_calls}\n\n"
                    f"Перегляд: /admin → Дашборд",
                    parse_mode="Markdown"
                )

            # Щоденний звіт о 23:00
            if datetime.now().hour == 23:
                # Розбивка по моделях за сьогодні
                model_cost: dict = {}
                for r in rows:
                    m = r.get("model") or "unknown"
                    model_cost[m] = model_cost.get(m, 0.0) + _calc_row_cost(m, r.get("tokens_in") or 0, r.get("tokens_out") or 0)

                lines = "\n".join(
                    f"  • `{m}`: ${c:.4f}" for m, c in sorted(model_cost.items(), key=lambda x: -x[1])
                ) or "  —"

                status = "🟢" if today_cost < DAILY_BUDGET_LIMIT * 0.7 else ("🟡" if today_cost < DAILY_BUDGET_LIMIT else "🔴")
                await bot.send_message(
                    ADMIN_ID,
                    f"{status} *EMET: денний звіт витрат*\n\n"
                    f"📅 {today}\n"
                    f"💬 Запитів: *{today_calls}*\n"
                    f"💰 Витрачено: *${today_cost:.4f}* / ліміт ${DAILY_BUDGET_LIMIT:.2f}\n\n"
                    f"По моделях:\n{lines}",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error("cost_digest error: %s", e)


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
            logger.info("TTL cleanup: deleted logs and trash older than 90 days")
        except Exception as e:
            logger.error("ttl_cleanup error: %s", e)


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
            logger.error("weekly_digest error: %s", e)


async def daily_quality_task():
    """Щодня о 08:00 Kyiv time відправляє звіт якості ТІЛЬКИ адміну."""
    from datetime import timedelta, timezone
    import zoneinfo
    try:
        tz_kyiv = zoneinfo.ZoneInfo("Europe/Kyiv")
    except Exception:
        try:
            tz_kyiv = zoneinfo.ZoneInfo("Europe/Kiev")
        except Exception:
            tz_kyiv = timezone(timedelta(hours=3))  # UTC+3 fallback
    while True:
        now = datetime.now(tz_kyiv)
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now.hour >= 8:
            # Already past 8AM today — check if we missed today's report
            marker = "data/.quality_sent_" + now.strftime("%Y%m%d")
            if not os.path.exists(marker):
                # Send now (missed today's report due to restart)
                logger.info("Quality task: missed today's 08:00, sending now")
                wait_secs = 5
            else:
                target += timedelta(days=1)
                wait_secs = (target - now).total_seconds()
        else:
            wait_secs = (target - now).total_seconds()
        if wait_secs > 10:
            logger.info("Quality task: next run at %s (in %.0f min)", target.strftime("%H:%M"), wait_secs / 60)
        await asyncio.sleep(max(wait_secs, 5))

        try:
            from quality_monitor import run_monitor_safe
            report, findings = run_monitor_safe()
            # Always mark as sent (even if no dialogs) to prevent infinite loop
            marker = "data/.quality_sent_" + datetime.now(tz_kyiv).strftime("%Y%m%d")
            if report and ADMIN_ID:
                if len(report) > 4000:
                    parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
                else:
                    parts = [report]
                for part in parts:
                    await bot.send_message(ADMIN_ID, part, parse_mode="Markdown")
                logger.info("Quality report sent to admin: %d findings", len(findings) if findings else 0)
            elif ADMIN_ID:
                await bot.send_message(ADMIN_ID, "Quality Monitor: за останні 24 год діалогів не знайдено.")
                logger.info("Quality report: no dialogs found")
            with open(marker, "w") as mf:
                mf.write("sent")
        except Exception as e:
            logger.error("Quality monitor error: %s", e)
            # Still mark to prevent loop
            try:
                marker = "data/.quality_sent_" + datetime.now(tz_kyiv).strftime("%Y%m%d")
                with open(marker, "w") as mf:
                    mf.write(f"error: {e}")
            except Exception:
                pass


async def auto_sync_task():
    """Фоновая задача: синхронизация Drive → RAG + курсы каждые SYNC_INTERVAL_SEC секунд."""
    # Первый запуск — через 60 сек после старта (бот уже принимает запросы)
    await asyncio.sleep(60)
    while True:
        logger.info("Auto-sync: starting Google Drive sync...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, sync_manager.run_sync)

        # Если RAG обновился — сбрасываем VDB-синглтоны, чтобы следующий запрос загрузил новый индекс
        if result["rag_updated"]:
            global _vdb_kb_openai, _vdb_products_openai, _vdb_competitors_openai, _vdb_coach_openai
            global _vdb_kb_google, _vdb_products_google, _vdb_competitors_google, _vdb_coach_google
            _vdb_kb_openai = _vdb_products_openai = _vdb_competitors_openai = _vdb_coach_openai = None
            _vdb_kb_google = _vdb_products_google = _vdb_competitors_google = _vdb_coach_google = None
            cat_labels = {"kb": "📚 База знань", "coach": "💼 Коуч", "certs": "📜 Сертифікати"}
            by_cat = result.get("rag_by_category", {})
            lines = [f"{cat_labels.get(k, k)}: {v} файл(ів)" for k, v in by_cat.items()]
            summary = "\n".join(lines) if lines else f"{len(result['rag_updated'])} файл(ів)"
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"✅ База знань оновлена\n\n{summary}\n\nВсього змін: {len(result['rag_updated'])}"
                )
            except Exception:
                pass

        if result["courses_updated"]:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"✅ Курси оновлено: {', '.join(result['courses_updated'])}"
                )
            except Exception:
                pass

        if result["error"]:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Помилка синхронізації: {result['error']}")
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
        logger.info("EMET bot started. Auto-sync every %d min.", sync_manager.SYNC_INTERVAL_SEC // 60)
    else:
        logger.info("EMET bot started. Auto-sync disabled (AUTO_SYNC_ENABLED=false).")
    asyncio.create_task(weekly_digest_task())
    asyncio.create_task(ttl_cleanup_task())
    asyncio.create_task(daily_cost_task())
    asyncio.create_task(daily_quality_task())

    # Повідомлення адміну про запуск — розрізняємо деплой від несподіваного рестарту
    try:
        marker_path = "data/deploy_marker.txt"
        if os.path.exists(marker_path):
            with open(marker_path) as f:
                deploy_time = f.read().strip()
            os.remove(marker_path)
            await bot.send_message(
                ADMIN_ID,
                f"✅ *Деплой завершено успішно*\n"
                f"Бот оновлено та перезапущено.\n"
                f"_Час деплою: {deploy_time}_",
                parse_mode="Markdown"
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ *Бот перезапустився*\n"
                "Причина невідома — можливий краш або ручний рестарт.\n"
                "Перевір логи: `docker logs emet_bot_app --tail 50`",
                parse_mode="Markdown"
            )
    except Exception:
        pass

    await dp.start_polling(bot)


@dp.errors()
async def handle_error(event: types.ErrorEvent) -> bool:
    """Глобальний обробник необроблених помилок — сповіщає адміна."""
    err = event.exception
    update = event.update
    context = ""
    if update.message:
        context = f"user={update.message.from_user.id}, text={update.message.text[:80] if update.message.text else '—'}"
    elif update.callback_query:
        context = f"user={update.callback_query.from_user.id}, data={update.callback_query.data}"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ *Помилка в боті*\n`{type(err).__name__}: {str(err)[:200]}`\n_{context}_",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    logger.error("[ERROR] %s: %s | %s", type(err).__name__, err, context)
    return True  # True = помилка оброблена, aiogram не ре-рейзить


if __name__ == "__main__":
    asyncio.run(main())