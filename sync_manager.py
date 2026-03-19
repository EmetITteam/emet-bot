"""
sync_manager.py — Автосинхронизация Google Drive для ЭМЕТ-бота

Что делает:
- RAG sync: сравнивает modifiedTime файлов в Drive с sync_state в SQLite.
  При изменениях — пересобирает оба ChromaDB-индекса в фоне (temp dir → атомарный swap).
- Course sync: парсит Google Sheets из COURSE_FOLDER_ID → обновляет курсы в SQLite.

Формат курса (Google Spreadsheet):
  Имя файла = название курса
  Лист "теми":    столбцы: # | Назва теми | Зміст
  Лист "тести":   столбцы: Тема # | Питання | Варіант A | B | C | D | Правильна (A/B/C/D)

Конфигурация (.env):
  COURSE_FOLDER_ID   — ID папки Google Drive с курсами (если не задан — курсы не синхронизируются)
  SYNC_INTERVAL_SEC  — интервал проверки в секундах (по умолчанию 3600)
"""

import os
import io
import time
import shutil
import db
import pandas as pd
from datetime import datetime

from pypdf import PdfReader
from docx import Document as DocxDocument
from pptx import Presentation
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv

from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
COURSE_FOLDER_ID = os.getenv("COURSE_FOLDER_ID", "")
SYNC_INTERVAL_SEC = int(os.getenv("SYNC_INTERVAL_SEC", "3600"))

SERVICE_ACCOUNT_FILE = "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# Те же папки что в knowledge_base.py и build_openai_db.py
RAG_FOLDER_IDS = [
    "1RBXHGXOIc2kkSAw-LqzLaRqEE3Ix7L-m",
    "1aGlC06ewPnElN1FEYjMTpDat9bbHlc2w",
]

# ─── Авторизация ──────────────────────────────────────────────────────────────

def get_services():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets


# ─── Инициализация таблиц ─────────────────────────────────────────────────────

def init_sync_tables():
    """Проверка что таблицы sync_state и courses уже созданы в init_db() (main.py).
    Оставлена для обратной совместимости — вызывается из main.py."""
    pass  # Таблицы созданы в init_db() через db.get_connection()


# ─── Список файлов Drive с метаданными ───────────────────────────────────────

def list_files_with_meta(drive, folder_id):
    """Рекурсивно возвращает список файлов с полями id, name, mimeType, modifiedTime, webViewLink."""
    result = []
    page_token = None
    while True:
        try:
            resp = drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
            for item in resp.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    result.extend(list_files_with_meta(drive, item["id"]))
                else:
                    result.append(item)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        except Exception as e:
            print(f"Ошибка доступа к папке {folder_id}: {e}")
            break
    return result


# ─── Извлечение текста из файлов (все поддерживаемые форматы) ────────────────

def extract_text(drive, file):
    mime = file["mimeType"]
    fid = file["id"]
    text = ""
    try:
        if mime == "application/pdf":
            buf = _download_bytes(drive, fid)
            reader = PdfReader(buf)
            text = "".join(p.extract_text() or "" for p in reader.pages)

        elif mime == "application/vnd.google-apps.document":
            text = drive.files().export_media(fileId=fid, mimeType="text/plain").execute().decode("utf-8")

        elif mime == "application/vnd.google-apps.spreadsheet":
            text = drive.files().export_media(fileId=fid, mimeType="text/csv").execute().decode("utf-8")

        elif mime == "application/vnd.google-apps.presentation":
            text = drive.files().export_media(fileId=fid, mimeType="text/plain").execute().decode("utf-8")

        elif mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            buf = _download_bytes(drive, fid)
            text = pd.read_excel(buf).to_csv(index=False)

        elif mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
            buf = _download_bytes(drive, fid)
            prs = Presentation(buf)
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text += shape.text + "\n"

        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            buf = _download_bytes(drive, fid)
            doc = DocxDocument(buf)
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

        elif mime in ("text/plain", "text/csv"):
            text = drive.files().get_media(fileId=fid).execute().decode("utf-8", errors="replace")

    except Exception as e:
        print(f"Ошибка извлечения текста '{file['name']}': {e}")
    return text


def _download_bytes(drive, file_id) -> io.BytesIO:
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


# ─── Построение индексов ──────────────────────────────────────────────────────

def _build_openai_index(drive, files, target_dir):
    docs = _files_to_documents(drive, files)
    if not docs:
        return
    chunks = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=200).split_documents(docs)
    print(f"OpenAI: {len(chunks)} чанков → {target_dir}")
    emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
    _batch_to_chroma(chunks, emb, target_dir, rate_limit_sleep=10, rate_limit_keywords=["429", "RateLimitError"])


def _build_google_index(drive, files, target_dir):
    docs = _files_to_documents(drive, files)
    if not docs:
        return
    chunks = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=200).split_documents(docs)
    print(f"Google: {len(chunks)} чанков → {target_dir}")
    emb = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001", google_api_key=GEMINI_KEY)
    _batch_to_chroma(chunks, emb, target_dir, rate_limit_sleep=30, rate_limit_keywords=["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"])


def _files_to_documents(drive, files):
    documents = []
    for f in files:
        text = extract_text(drive, f)
        if text:
            documents.append(Document(
                page_content=text,
                metadata={"source": f["name"], "url": f.get("webViewLink", "")}
            ))
    return documents


def _batch_to_chroma(chunks, embeddings, persist_dir, rate_limit_sleep, rate_limit_keywords):
    db = None
    for i in range(0, len(chunks), 50):
        batch = chunks[i:i + 50]
        success = False
        while not success:
            try:
                if db is None:
                    db = Chroma.from_documents(batch, embedding=embeddings, persist_directory=persist_dir)
                else:
                    db.add_documents(batch)
                success = True
                time.sleep(0.5)
            except Exception as e:
                err = str(e)
                if any(k in err for k in rate_limit_keywords):
                    print(f"Rate limit / недоступность. Сплю {rate_limit_sleep} сек...")
                    time.sleep(rate_limit_sleep)
                else:
                    raise


# ─── RAG синхронизация ────────────────────────────────────────────────────────

def sync_rag_indexes():
    """
    Проверяет изменения в RAG-папках.
    При наличии изменений: пересобирает оба индекса в temp-директориях, затем атомарный swap.
    Возвращает список имён изменённых файлов (пустой = изменений нет).
    """
    drive, _ = get_services()

    all_files = []
    for folder_id in RAG_FOLDER_IDS:
        all_files.extend(list_files_with_meta(drive, folder_id))

    # Проверяем что изменилось
    indexed = {r[0]: r[1] for r in db.query("SELECT file_id, modified_time FROM sync_state")}

    changed = [f for f in all_files if indexed.get(f["id"]) != f["modifiedTime"]]

    if not changed:
        print(f"RAG sync: изменений нет (проверено {len(all_files)} файлов)")
        return []

    print(f"RAG sync: {len(changed)} изменений. Начинаю пересборку обоих индексов...")

    tmp_openai = "data/db_index_openai_building"
    tmp_google = "data/db_index_google_building"
    shutil.rmtree(tmp_openai, ignore_errors=True)
    shutil.rmtree(tmp_google, ignore_errors=True)

    try:
        _build_openai_index(drive, all_files, tmp_openai)
        _build_google_index(drive, all_files, tmp_google)
    except Exception as e:
        print(f"Ошибка пересборки индексов: {e}")
        shutil.rmtree(tmp_openai, ignore_errors=True)
        shutil.rmtree(tmp_google, ignore_errors=True)
        return []

    # Атомарный swap: live → old → new → live
    for live, tmp in [("data/db_index_openai", tmp_openai), ("data/db_index_google", tmp_google)]:
        old = live + "_old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(live):
            os.rename(live, old)
        os.rename(tmp, live)
        shutil.rmtree(old, ignore_errors=True)

    # Обновляем sync_state для всех файлов (не только изменённых)
    db.executemany(
        "INSERT INTO sync_state (file_id, file_name, modified_time, indexed_at) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (file_id) DO UPDATE SET file_name=EXCLUDED.file_name, "
        "modified_time=EXCLUDED.modified_time, indexed_at=EXCLUDED.indexed_at",
        [(f["id"], f["name"], f["modifiedTime"], datetime.now().isoformat()) for f in all_files]
    )

    changed_names = [f["name"] for f in changed]
    print(f"RAG sync завершён. Изменённые файлы: {changed_names}")
    return changed_names


# ─── Синхронизация курсов из Google Sheets ───────────────────────────────────

def _parse_course_spreadsheet(sheets_svc, spreadsheet_id, course_title):
    """
    Читает листы "теми" и "тести" из спредшита.
    Возвращает структуру курса: {title, topics: [{title, content, questions}]}
    """
    def read_sheet(sheet_name):
        try:
            result = sheets_svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1:Z1000"
            ).execute()
            return result.get("values", [])
        except Exception as e:
            print(f"Лист '{sheet_name}' не найден в '{course_title}': {e}")
            return []

    # Парсим темы: # | Назва теми | Зміст
    topics = {}
    for row in read_sheet("теми")[1:]:  # пропускаем заголовок
        if len(row) < 3:
            continue
        try:
            order = int(str(row[0]).strip())
            topics[order] = {
                "title": str(row[1]).strip(),
                "content": str(row[2]).strip(),
                "questions": []
            }
        except ValueError:
            continue

    # Парсим тесты: Тема # | Питання | A | B | C | D | Правильна
    letter_to_idx = {"A": 0, "B": 1, "C": 2, "D": 3}
    for row in read_sheet("тести")[1:]:
        if len(row) < 7:
            continue
        try:
            topic_num = int(str(row[0]).strip())
        except ValueError:
            continue
        if topic_num not in topics:
            continue

        q_text = str(row[1]).strip()
        opts_raw = [str(row[i]).strip() if i < len(row) else "" for i in range(2, 6)]
        correct_letter = str(row[6]).strip().upper() if len(row) > 6 else ""
        correct_idx = letter_to_idx.get(correct_letter, -1)

        options = [(text, i == correct_idx) for i, text in enumerate(opts_raw) if text]
        if q_text and options:
            topics[topic_num]["questions"].append({"text": q_text, "options": options})

    return {"title": course_title, "topics": [topics[k] for k in sorted(topics.keys())]}


def _upsert_course(course_data, drive_file_id, drive_modified):
    """Удаляет старую версию курса и вставляет новую."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM courses WHERE drive_file_id=%s", (drive_file_id,))
            existing = cur.fetchone()

            if existing:
                old_id = existing[0]
                cur.execute("SELECT id FROM topics WHERE course_id=%s", (old_id,))
                topic_ids = [r[0] for r in cur.fetchall()]
                for tid in topic_ids:
                    cur.execute("SELECT id FROM questions WHERE topic_id=%s", (tid,))
                    q_ids = [r[0] for r in cur.fetchall()]
                    for qid in q_ids:
                        cur.execute("DELETE FROM answer_options WHERE question_id=%s", (qid,))
                    cur.execute("DELETE FROM questions WHERE topic_id=%s", (tid,))
                cur.execute("DELETE FROM topics WHERE course_id=%s", (old_id,))
                cur.execute("DELETE FROM courses WHERE id=%s", (old_id,))

            cur.execute(
                "INSERT INTO courses (title, description, drive_file_id, drive_modified, created_at) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (course_data["title"], "", drive_file_id, drive_modified, datetime.now().isoformat())
            )
            course_id = cur.fetchone()[0]

            for order, topic in enumerate(course_data["topics"], 1):
                cur.execute(
                    "INSERT INTO topics (course_id, order_num, title, content) VALUES (%s,%s,%s,%s) RETURNING id",
                    (course_id, order, topic["title"], topic["content"])
                )
                topic_id = cur.fetchone()[0]
                for q in topic["questions"]:
                    cur.execute(
                        "INSERT INTO questions (topic_id, text) VALUES (%s,%s) RETURNING id",
                        (topic_id, q["text"])
                    )
                    q_id = cur.fetchone()[0]
                    for opt_text, is_correct in q["options"]:
                        cur.execute(
                            "INSERT INTO answer_options (question_id, text, is_correct) VALUES (%s,%s,%s)",
                            (q_id, opt_text, int(is_correct))
                        )


def sync_courses():
    """
    Проверяет Google Sheets в COURSE_FOLDER_ID на изменения.
    Обновляет курсы в SQLite.
    Возвращает список обновлённых курсов.
    """
    if not COURSE_FOLDER_ID:
        return []

    drive, sheets_svc = get_services()

    spreadsheets = [
        f for f in list_files_with_meta(drive, COURSE_FOLDER_ID)
        if f["mimeType"] == "application/vnd.google-apps.spreadsheet"
    ]

    existing = {
        r[0]: r[1]
        for r in db.query("SELECT drive_file_id, drive_modified FROM courses WHERE drive_file_id IS NOT NULL")
    }

    updated = []
    for f in spreadsheets:
        if existing.get(f["id"]) == f["modifiedTime"]:
            continue  # Не изменился

        try:
            course_data = _parse_course_spreadsheet(sheets_svc, f["id"], f["name"])
            if not course_data["topics"]:
                print(f"Курс '{f['name']}': лист 'теми' пустой или не найден, пропускаю")
                continue
            _upsert_course(course_data, f["id"], f["modifiedTime"])
            updated.append(f["name"])
            total_q = sum(len(t["questions"]) for t in course_data["topics"])
            print(f"Курс обновлён: '{f['name']}' ({len(course_data['topics'])} тем, {total_q} вопросов)")
        except Exception as e:
            print(f"Ошибка парсинга курса '{f['name']}': {e}")

    return updated


# ─── Главная функция синхронизации ───────────────────────────────────────────

def run_sync():
    """
    Запускает полную синхронизацию: RAG-индексы + курсы.
    Возвращает {'rag_updated': [...], 'courses_updated': [...], 'error': None | str}
    """
    result = {"rag_updated": [], "courses_updated": [], "error": None}
    try:
        result["rag_updated"] = sync_rag_indexes()
        result["courses_updated"] = sync_courses()
    except Exception as e:
        result["error"] = str(e)
        print(f"Ошибка sync: {e}")
    return result