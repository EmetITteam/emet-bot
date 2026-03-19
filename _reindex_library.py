"""
Переіндексація EMET_Knowledge_Library з chunk_size=800 (замість ~1500).
Запуск: python _reindex_library.py
"""
import os
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import OpenAIEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY  = os.getenv("GEMINI_API_KEY")
FILE_PATH   = "data/EMET_Knowledge_Library_1.md.txt"
SOURCE_NAME = "EMET_Knowledge_Library_1.md.txt"

COACH_DIRS = {
    "openai":  "data/db_index_coach_openai",
    "google":  "data/db_index_coach_google",
}

def reindex(embed_key: str, embeddings, chroma_dir: str):
    db = Chroma(persist_directory=chroma_dir, embedding_function=embeddings)

    # 1. Видаляємо старі чанки цього файлу
    existing = db.get(where={"source": SOURCE_NAME})
    old_ids = existing.get("ids", [])
    if old_ids:
        db.delete(ids=old_ids)
        print(f"  [{embed_key}] Видалено {len(old_ids)} старих чанків")
    else:
        print(f"  [{embed_key}] Старих чанків не знайдено")

    # 2. Читаємо файл
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    # 3. Чанкуємо з меншим розміром
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
        separators=["\n---\n", "\n\n", "\n", " "]
    )
    chunks = splitter.split_text(text)
    print(f"  [{embed_key}] Нових чанків: {len(chunks)}")

    # 4. Додаємо з метаданими
    docs_text = chunks
    metadatas = [{"source": SOURCE_NAME} for _ in chunks]
    ids = [f"library_{embed_key}_{i}" for i in range(len(chunks))]
    db.add_texts(texts=docs_text, metadatas=metadatas, ids=ids)
    print(f"  [{embed_key}] Додано в {chroma_dir}")

print("=== Переіндексація EMET_Knowledge_Library ===\n")

print("OpenAI embeddings...")
emb_openai = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
reindex("openai", emb_openai, COACH_DIRS["openai"])

print("\nGoogle embeddings...")
emb_google = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001", google_api_key=GEMINI_KEY)
reindex("google", emb_google, COACH_DIRS["google"])

print("\n✅ Готово! Тепер всі 20 заперечень проіндексовані.")
