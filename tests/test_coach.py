#!/usr/bin/env python3
"""
test_coach.py — автоматичний тест якості Sales Coach (3 послідовних запити)
Запуск на сервері: docker exec emet_bot_app python test_coach.py
Запуск локально:  python test_coach.py
"""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_KEY:
    print("ERROR: OPENAI_API_KEY not set"); sys.exit(1)

client = OpenAI(api_key=OPENAI_KEY)

# --- RAG ---
emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
try:
    db_coach = Chroma(persist_directory="data/db_index_coach_openai", embedding_function=emb)
except Exception as e:
    print(f"ERROR loading ChromaDB: {e}"); sys.exit(1)

# --- Промпт коуча (модульна система v2) ---
from prompts_v2 import PROMPT_COACH_BASE, PROMPT_COACH_SOS
COACH_SYSTEM = PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_SOS

# --- Тестовий сценарій: 3 послідовних питання від одного менеджера ---
TEST_QUESTIONS = [
    {
        "q": "Эллансе дорого",
        "expected_format": "SOS",
        "must_have": ["питання лікарю", "фінансовий або механічний аргумент"],
        "must_not_have": ["до 2 років", "24 місяці", "довготривалий", "менше повторних"],
    },
    {
        "q": "врача не интересует долгосрочность действия, есть другие аргументы?",
        "expected_format": "short list (3-5 аргументів)",
        "must_have": ["конкретний аргумент без тривалості", "готова фраза"],
        "must_not_have": ["до 2 років", "24 місяці", "довготривалий ефект"],
    },
    {
        "q": "дай конкретный диалог с примерами по возражению дорого но акцент не на длительности",
        "expected_format": "dialog script",
        "must_have": ["Лікар:", "Менеджер:", "конкретна фраза без тривалості"],
        "must_not_have": ["до 2 років", "Опиши ситуацію"],
    },
]

# --- Суддя-промпт ---
JUDGE_SYSTEM = """Ти — експерт-оцінювач якості AI sales coach для фармацевтичних представників.
Оцінюй відповідь по 5 критеріям, кожен 0-2 бали. Відповідай ТІЛЬКИ JSON."""

JUDGE_TEMPLATE = """Питання менеджера: {question}
Очікуваний формат: {expected_format}

Відповідь бота:
{answer}

Оціни по критеріям (0-2 кожен):

1. ПРОДУКТ_КОНТЕКСТ: Відповідь про правильний продукт (Ellansé) і відповідає режиму coach?
   2=правильний продукт і контекст | 1=частково | 0=неправильний продукт або режим

2. БЕЗ_ТРИВАЛОСТІ: Тривалість дії НЕ є першим або головним аргументом?
   2=тривалість відсутня або згадується останньою | 1=є але не домінує | 0=тривалість — основний аргумент

3. КОНКРЕТНІСТЬ: Є конкретні аргументи (фінансові, механізм дії, репутація) що реально можна сказати лікарю?
   2=3+ конкретних аргументи з фактами | 1=1-2 або загальні фрази | 0=порожні фрази типу "якість і результат"

4. ФОРМАТ: Формат відповіді відповідає запиту?
   2=точний формат (SOS→коротко+питання, діалог→скрипт Лікар/Менеджер) | 1=частково | 0=неправильний формат

5. ПРАКТИЧНІСТЬ: Менеджер може взяти цю відповідь і використати ПРЯМО ЗАРАЗ під час зустрічі?
   2=готово до використання без адаптації | 1=потребує доопрацювання | 0=теоретично, не практично

Поверни ТІЛЬКИ JSON без коментарів:
{{
  "scores": {{
    "ПРОДУКТ_КОНТЕКСТ": <0-2>,
    "БЕЗ_ТРИВАЛОСТІ": <0-2>,
    "КОНКРЕТНІСТЬ": <0-2>,
    "ФОРМАТ": <0-2>,
    "ПРАКТИЧНІСТЬ": <0-2>
  }},
  "total": <сума 0-10>,
  "verdict": "<одне речення — головне що добре або погано>",
  "fix": "<одне речення — що конкретно виправити якщо total < 8>"
}}"""


def get_rag_context(query: str, k: int = 6) -> str:
    try:
        docs = db_coach.similarity_search(query, k=k)
        return "\n\n---\n\n".join(d.page_content[:800] for d in docs)
    except Exception as e:
        return f"[RAG error: {e}]"


def ask_coach(question: str, chat_history: list, search_query: str) -> str:
    context = get_rag_context(search_query)
    messages = [
        {"role": "system", "content": COACH_SYSTEM},
        *chat_history,
        {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nВОПРОС:\n{question}"}
    ]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.3,
        max_tokens=800,
    )
    return resp.choices[0].message.content


def evaluate(question: str, answer: str, expected_format: str) -> dict:
    prompt = JUDGE_TEMPLATE.format(
        question=question,
        expected_format=expected_format,
        answer=answer,
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {"total": 0, "verdict": "parse error", "fix": "", "scores": {}}


def build_search_query(question: str, q_index: int, chat_history: list) -> str:
    """Імітує логіку main.py для збагачення search_query."""
    q_lower = question.lower()
    has_product = any(p in q_lower for p in ["ellanse", "еланс", "елансе", "элансе", "эллансе"])
    canonical = "Ellansé"

    if not has_product and chat_history:
        canonical = "Ellansé"  # відомо з сценарію

    script_kws = ["конкретный диалог", "конкретний діалог", "дай діалог", "дай диалог", "скрипт"]
    followup_kws = ["другие аргументы", "інші аргументи", "не интересует", "не цікавить"]

    if any(k in q_lower for k in script_kws):
        return f"скрипт діалог аргументи заперечення дорого {canonical}"
    elif any(k in q_lower for k in followup_kws):
        return f"аргументи заперечення {canonical} без тривалості фінансові"
    else:
        return f"заперечення дорого {canonical} аргументи"


def print_bar(score: int, max_score: int = 10) -> str:
    filled = round(score / max_score * 20)
    bar = "█" * filled + "░" * (20 - filled)
    return f"[{bar}] {score}/{max_score}"


def main():
    print("\n" + "=" * 65)
    print("  EMET COACH QUALITY TEST — 3 послідовних запити")
    print("=" * 65)

    chat_history = []
    results = []
    total_score = 0

    for i, test in enumerate(TEST_QUESTIONS):
        q = test["q"]
        print(f"\n{'─'*65}")
        print(f"ПИТАННЯ {i+1}/3: {q}")
        print('─' * 65)

        search_q = build_search_query(q, i, chat_history)
        answer = ask_coach(q, chat_history, search_q)

        print(f"\nВІДПОВІДЬ БОТА:\n{answer}\n")

        eval_result = evaluate(q, answer, test["expected_format"])
        scores = eval_result.get("scores", {})
        q_total = eval_result.get("total", sum(scores.values()) if scores else 0)
        total_score += q_total

        print(f"ОЦІНКА: {print_bar(q_total)}")
        for criterion, val in scores.items():
            mark = "✅" if val == 2 else ("⚠️" if val == 1 else "❌")
            print(f"  {mark} {criterion}: {val}/2")
        print(f"  Вердикт: {eval_result.get('verdict', '')}")
        if q_total < 8:
            print(f"  Що виправити: {eval_result.get('fix', '')}")

        chat_history.append({"role": "user", "content": q})
        chat_history.append({"role": "assistant", "content": answer})
        results.append({"q": q, "answer": answer, "eval": eval_result, "score": q_total})

    # --- Загальний висновок ---
    avg = total_score / len(TEST_QUESTIONS)
    print(f"\n{'='*65}")
    print(f"  ЗАГАЛЬНИЙ РЕЗУЛЬТАТ: {print_bar(round(avg))}")
    print(f"  Середня оцінка: {avg:.1f}/10")
    if avg >= 8:
        print("  СТАТУС: ✅ ЯКІСТЬ OK — бот готовий до використання")
    elif avg >= 6:
        print("  СТАТУС: ⚠️  ПОТРЕБУЄ ДООПРАЦЮВАННЯ")
    else:
        print("  СТАТУС: ❌ КРИТИЧНО — потрібні правки промпту або бази")

    print("\n  ДЕТАЛІ:")
    for r in results:
        mark = "✅" if r["score"] >= 8 else ("⚠️" if r["score"] >= 6 else "❌")
        print(f"  {mark} [{r['score']}/10] {r['q'][:60]}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
