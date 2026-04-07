"""
test_rag_courses.py — автотест RAG після індексації курсів LMS.
Запуск: docker exec -it emet_bot_app python /app/test_rag_courses.py

Перевіряє:
  1. Чи знайшлися релевантні чанки в ChromaDB (coach_openai)
  2. Яку відповідь дає LLM по кожному питанню
  3. Чи є ключові факти у відповіді (мінімальна автоперевірка)
"""
import os, sys, textwrap
sys.stdout = __import__('io').TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("ERROR: OPENAI_API_KEY not set")
    sys.exit(1)

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from openai import OpenAI

# ── Налаштування ──────────────────────────────────────────────────────────────

COACH_DB  = "data/db_index_coach_openai"
RAG_K     = 8
LLM_MODEL = "gpt-4o-mini"

QUESTIONS = [
    {
        "id": 1,
        "product": "Petaran",
        "question": "З яких трьох активних компонентів складається Petaran POLY PLLA і яку спільну дію вони дають?",
        "must_contain": ["PLLA", "PN", "HA"],      # ключові слова у відповіді
        "hint": "PLLA + полінуклеотиди (PN) + гіалуронова кислота (HA), реституція тканин",
    },
    {
        "id": 2,
        "product": "Нейронокс",
        "question": "Яке місце займає Нейронокс серед офіційно зареєстрованих ботулотоксинів в Україні і чому це важливо для лікаря?",
        "must_contain": ["зареєстр", "п'ят"],
        "hint": "1 з 5 офіційно зареєстрованих препаратів в Україні",
    },
    {
        "id": 3,
        "product": "IUSE Skin Booster HA20",
        "question": "Скільки процедур потрібно на курс IUSE Skin Booster HA20, з яким інтервалом і коли робити підтримуючий курс?",
        "must_contain": ["3", "місяц"],
        "hint": "3 процедури з інтервалом 1 місяць, підтримуючий через 6 місяців",
    },
    {
        "id": 4,
        "product": "Exoxe",
        "question": "Звідки беруться екзосоми в препараті Exoxe і скільки факторів росту він містить?",
        "must_contain": ["амніот", "фактор"],
        "hint": "МСК амніотичної рідини людини, >70 факторів росту",
    },
    {
        "id": 5,
        "product": "IUSE Collagen Marine Beauty",
        "question": "Яка молекулярна маса пептидів колагену в IUSE Collagen Marine Beauty і як вони потрапляють до фібробластів?",
        "must_contain": ["1500", "фібробласт"],
        "hint": "1500 Da, проникають через бар'єр кишківника як сигнальні молекули",
    },
]

SYSTEM_PROMPT = """Ти — Sales Коуч компанії EMET (естетична медицина).
Відповідай ТІЛЬКИ на основі наданого контексту. Якщо інформації немає — так і скажи.
Відповідай лаконічно, по суті, мовою питання."""

# ── Ініціалізація ─────────────────────────────────────────────────────────────

print("Initializing embeddings and ChromaDB...")
emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)

if not os.path.exists(COACH_DB):
    print(f"ERROR: ChromaDB not found at '{COACH_DB}'. Run indexation first.")
    sys.exit(1)

vdb    = Chroma(persist_directory=COACH_DB, embedding_function=emb)
client = OpenAI(api_key=OPENAI_KEY)

total_docs = vdb._collection.count()
print(f"ChromaDB coach_openai: {total_docs} chunks total\n")

# ── Тести ─────────────────────────────────────────────────────────────────────

SEP = "=" * 70
results = []

for q in QUESTIONS:
    print(SEP)
    print(f"[{q['id']}/5] {q['product']}")
    print(f"Q: {q['question']}")
    print()

    # 1. RAG search
    docs = vdb.similarity_search(q["question"], k=RAG_K)
    lms_docs  = [d for d in docs if "lms" in (d.metadata.get("url") or "").lower()
                                 or "[LMS]" in (d.metadata.get("source") or "")]
    other_docs = [d for d in docs if d not in lms_docs]

    print(f"  RAG hits: {len(docs)} total | {len(lms_docs)} LMS-курси | {len(other_docs)} інші джерела")
    for d in docs[:3]:
        src = d.metadata.get("source", "?")
        preview = d.page_content[:120].replace("\n", " ")
        print(f"    [{src[:50]}] {preview}...")
    print()

    # 2. LLM answer
    context = "\n\n---\n\n".join(d.page_content for d in docs)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": f"Контекст:\n{context}\n\nПитання: {q['question']}"},
            ],
            temperature=0,
            max_tokens=400,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception as e:
        answer = f"[LLM ERROR: {e}]"

    print("  ВІДПОВІДЬ БОТА:")
    for line in textwrap.wrap(answer, 66):
        print(f"    {line}")
    print()

    # 3. Auto-check
    answer_lower = answer.lower()
    hits   = [kw for kw in q["must_contain"] if kw.lower() in answer_lower]
    misses = [kw for kw in q["must_contain"] if kw.lower() not in answer_lower]
    ok = len(misses) == 0

    status = "PASS" if ok else "FAIL"
    print(f"  ПЕРЕВІРКА: {status}")
    if hits:
        print(f"    OK  : {hits}")
    if misses:
        print(f"    MISS: {misses}  (очікувалось: {q['hint']})")

    results.append({"id": q["id"], "product": q["product"], "ok": ok, "misses": misses})
    print()

# ── Підсумок ──────────────────────────────────────────────────────────────────

print(SEP)
print("РЕЗУЛЬТАТИ:")
passed = sum(1 for r in results if r["ok"])
for r in results:
    icon = "PASS" if r["ok"] else "FAIL"
    miss_str = f"  <- немає: {r['misses']}" if not r["ok"] else ""
    print(f"  [{icon}] #{r['id']} {r['product']}{miss_str}")

print(f"\nПідсумок: {passed}/{len(results)} питань пройшло автоперевірку")
if passed == len(results):
    print("RAG по курсах працює коректно!")
else:
    print("Деякі відповіді неповні — можливо, треба перезапустити індексацію або уточнити контент в курсах.")
print(SEP)
