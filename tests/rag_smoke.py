import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

"""
RAG Smoke Test — перевірка якості пошуку після пересборки індексу.

Запуск:
    python tests/rag_smoke.py

Що перевіряє:
    - Чи знаходить ChromaDB потрібні документи для кожного контрольного питання
    - Чи є очікувані ключові слова в знайденому контексті
    - Підраховує загальний Recall Score (частка тестів що пройшли)

Коли запускати:
    Після кожної пересборки індексу (build_openai_db.py або knowledge_base.py).
    Якщо score < 0.75 — НЕ деплоїти, розібратись з проблемою.
"""

import sys
import os
import json

# Підключаємо корінь проекту
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

OPENAI_KEY = os.getenv("OPENAI_API_KEY")

DATASET_PATH = os.path.join(os.path.dirname(__file__), "golden_dataset.json")

DB_PATHS = {
    "kb":    "data/db_index_kb_openai",
    "coach": "data/db_index_coach_openai",
    "combo": "data/db_index_coach_openai",
}
K_VALUES = {
    "kb":    25,
    "coach": 20,
    "combo": 15,
}

_vdb_cache = {}


def get_vdb(mode: str):
    path = DB_PATHS[mode]
    if path not in _vdb_cache:
        embeddings = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
        _vdb_cache[path] = Chroma(persist_directory=path, embedding_function=embeddings)
    return _vdb_cache[path]


def get_context(question: str, mode: str) -> str:
    vdb = get_vdb(mode)
    k = K_VALUES[mode]
    if mode == "combo":
        docs = vdb.similarity_search(question, k=k, filter={"category": "combo"})
    else:
        docs = vdb.similarity_search(question, k=k)
    return "\n".join(d.page_content for d in docs)


def run():
    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    # Пропускаємо незаповнені шаблонні записи
    dataset = [d for d in dataset if "ЗАМЕНИТЕ" not in d["question"]]

    if not dataset:
        print("❌ golden_dataset.json порожній або не заповнений. Додай реальні питання.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"RAG Smoke Test — {len(dataset)} питань")
    print(f"{'='*60}\n")

    passed = 0
    failed = 0
    failed_cases = []

    for item in dataset:
        question  = item["question"]
        mode      = item["mode"]
        keywords  = [kw.lower() for kw in item["expected_keywords"]]
        desc      = item["description"]
        item_id   = item["id"]

        try:
            context = get_context(question, mode).lower()
        except Exception as e:
            print(f"  [{item_id}] ❌ ПОМИЛКА пошуку: {e}")
            failed += 1
            failed_cases.append((item_id, desc, f"Exception: {e}"))
            continue

        found     = [kw for kw in keywords if kw in context]
        missing   = [kw for kw in keywords if kw not in context]
        ok        = len(missing) == 0

        if ok:
            print(f"  [{item_id}] ✅ {desc}")
            passed += 1
        else:
            print(f"  [{item_id}] ❌ {desc}")
            print(f"       Не знайдено: {missing}")
            failed += 1
            failed_cases.append((item_id, desc, f"missing: {missing}"))

    total = passed + failed
    score = passed / total if total else 0

    print(f"\n{'='*60}")
    print(f"Результат: {passed}/{total} тестів пройшло  |  Score: {score:.0%}")

    if score >= 0.85:
        print("✅ ВІДМІННО — індекс готовий до деплою")
    elif score >= 0.75:
        print("⚠️  ПРИЙНЯТНО — можна деплоїти, але є питання для розбору")
    else:
        print("❌ ПОГАНО — НЕ ДЕПЛОЇТИ. Розберіться з проблемними запитами:")
        for item_id, desc, reason in failed_cases:
            print(f"   • [{item_id}] {desc} → {reason}")

    print(f"{'='*60}\n")

    # Exit code 1 якщо score нижче порогу — зупиняє CI/CD pipeline
    sys.exit(0 if score >= 0.75 else 1)


if __name__ == "__main__":
    run()
