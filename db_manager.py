import sqlite3
import datetime
import os

DB_PATH = 'data/emet_lms.db'

def init_db():
    if not os.path.exists('data'):
        os.makedirs('data')
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Профили и прогресс сотрудников
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        current_mode TEXT DEFAULT 'kb',
        current_course_id INTEGER DEFAULT 0,
        current_step_index INTEGER DEFAULT 0
    )''')

    # 2. База курсов и тестов (хранение JSON структуры)
    cursor.execute('''CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        data TEXT, 
        created_at TEXT
    )''')

    # 3. Результаты тестов для админки
    cursor.execute('''CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        course_id INTEGER,
        score TEXT,
        completed_at TEXT
    )''')

    # 4. Логирование для анализа запросов менеджеров
    cursor.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        mode TEXT,
        question TEXT,
        answer TEXT,
        timestamp TEXT
    )''')
    
    conn.commit()
    conn.close()

def save_course(title, json_data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO courses (title, data, created_at) VALUES (?, ?, ?)",
                   (title, json_data, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def log_interaction(user_id, mode, question, answer):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO activity_logs (user_id, mode, question, answer, timestamp) VALUES (?, ?, ?, ?, ?)",
                   (user_id, mode, question, answer, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()