"""tests/run_regression.py — регресійний прогон fixture-кейсів через реальний pipeline.

Запуск (на сервері в Docker):
    docker exec emet_bot_app python /app/tests/run_regression.py
    docker exec emet_bot_app python /app/tests/run_regression.py --category esse
    docker exec emet_bot_app python /app/tests/run_regression.py --case audit23-petaran-mono-protocol

Що робить:
1. Завантажує tests/regression_fixtures.json
2. Для кожного кейсу:
   - Прогоняє запит через справжній classifier (LLM call)
   - Перевіряє expected_intent / expected_product / min_confidence
   - Опціонально — генерує відповідь через RAG+LLM і перевіряє must_contain / must_not_contain
3. Виводить підсумок: PASS/FAIL по кожному, фінальний звіт

Exit code: 0 якщо всі PASS, 1 якщо хоч один FAIL.
Бере ~2-3 хв на 15 кейсів (15 classifier calls + 15 LLM calls = ~$0.10 на прогін).
"""
import asyncio
import argparse
import json
import os
import re
import sys

# UTF-8 для Windows-консолі
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

sys.path.insert(0, "/app")
os.chdir("/app")

from openai import AsyncOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from classifier import classify, normalize_product, INTENT_TO_COACH_SUBTYPE
from prompts_v2 import (
    PROMPT_COACH_BASE, PROMPT_COACH_INFO, PROMPT_COACH_SOS,
    PROMPT_COACH_VERBATIM, PROMPT_EXTRACT,
)
from prompts import PROMPT_COMBO

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_KEY, timeout=60)
emb = OpenAIEmbeddings(model="text-embedding-3-small", api_key=OPENAI_KEY)
db_products = Chroma(persist_directory="/app/data/db_index_products_openai", embedding_function=emb)
db_competitors = Chroma(persist_directory="/app/data/db_index_competitors_openai", embedding_function=emb)


def load_fixtures(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


async def run_case(case, generate_answer=True):
    """Прогоняє один кейс. Повертає dict з полями: id, status (pass/fail), errors[]."""
    errors = []
    query = case["query"]
    cls = await classify(client, query)
    intent = cls.get("intent", "")
    product = normalize_product(cls.get("primary_product"), cls.get("product_variant"))
    confidence = cls.get("confidence", 0.0)

    # Check 1: intent
    if case.get("expected_intent") and intent != case["expected_intent"]:
        errors.append(f"intent mismatch: got '{intent}', expected '{case['expected_intent']}'")

    # Check 2: product
    expected_prod = case.get("expected_product")
    if expected_prod is not None:
        # ESSE / Petaran / Vitaran etc — порівнюємо case-insensitive substring
        if not product or expected_prod.lower() not in (product or "").lower():
            errors.append(f"product mismatch: got '{product}', expected to contain '{expected_prod}'")
    elif expected_prod is None and product:
        # Ожидаемо null — отримали значення
        # Не fail (м'яке правило) — просто warning
        pass

    # Check 3: confidence
    if confidence < case.get("min_confidence", 0.0):
        errors.append(f"low confidence: got {confidence:.2f}, expected ≥ {case['min_confidence']}")

    # Check 4: must_contain / must_not_contain (потребує генерації відповіді)
    if generate_answer and (case.get("must_contain") or case.get("must_not_contain")):
        try:
            answer = await generate_response(query, cls, intent, product)
            answer_lc = answer.lower()
            for needle in case.get("must_contain", []):
                if needle.lower() not in answer_lc:
                    errors.append(f"missing required phrase: '{needle}'")
            for needle in case.get("must_not_contain", []):
                if needle.lower() in answer_lc:
                    errors.append(f"forbidden phrase present: '{needle}'")
        except Exception as e:
            errors.append(f"generation error: {e}")

    return {
        "id": case["id"],
        "category": case.get("category", "?"),
        "query": query[:80],
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "classifier": {"intent": intent, "product": product, "confidence": confidence},
    }


async def generate_response(query, cls, intent, product):
    """Будує відповідь так само як main.py — RAG+prompt+LLM."""
    subtype = INTENT_TO_COACH_SUBTYPE.get(intent, "info")
    needs_verbatim = cls.get("needs_verbatim", False)

    # RAG — product-locked якщо є продукт
    if product:
        docs = db_products.similarity_search(query, k=8, filter={"product_canonical": product})
    else:
        docs = db_products.similarity_search(query, k=8)
    has_competitor = bool(cls.get("competitor"))
    if has_competitor:
        docs += db_competitors.similarity_search(query, k=4)
    # Передаємо scope метадані в контекст (як у main._extract_docs)
    context = ""
    for d in docs[:10]:
        scope = d.metadata.get("scope", "?")
        scope_label = ""
        if scope == "line":
            scope_label = "🌐 SCOPE=LINE (характеристика лінії в цілому)"
        elif scope == "ingredient":
            scope_label = "🧪 SCOPE=INGREDIENT"
        elif scope == "protocol":
            scope_label = "📋 SCOPE=PROTOCOL"
        header = f"=== {scope_label} | {d.metadata.get('source','?')} ===" if scope_label else f"=== {d.metadata.get('source','?')} ==="
        context += f"{header}\n{d.page_content[:400]}\n\n"

    # Prompt assembly
    if intent == "correction":
        # При correction — використовуємо BASE з anti-sycophancy правилом
        sys_prompt = PROMPT_COACH_BASE + PROMPT_COACH_INFO
    elif needs_verbatim and subtype in ("info", "combo"):
        sys_prompt = PROMPT_COACH_BASE + PROMPT_COACH_VERBATIM
    elif subtype == "sos":
        sys_prompt = PROMPT_COACH_BASE + PROMPT_COACH_SOS
    elif subtype == "combo":
        sys_prompt = PROMPT_COACH_BASE + PROMPT_COMBO
    else:
        sys_prompt = PROMPT_COACH_BASE + PROMPT_COACH_INFO

    resp = await client.chat.completions.create(
        model="gpt-4o", timeout=60, temperature=0.0, max_tokens=700,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nВОПРОС:\n{query}"}
        ]
    )
    text = resp.choices[0].message.content.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return text


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", help="фільтр по категорії (esse, side_effect, combo, price, classifier, scope, clarify)")
    parser.add_argument("--case", help="запустити лише один case по id")
    parser.add_argument("--no-generate", action="store_true", help="не генерувати відповідь, лише classifier check")
    parser.add_argument("--fixtures", default="/app/tests/regression_fixtures.json")
    args = parser.parse_args()

    cases = load_fixtures(args.fixtures)
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if not cases:
        print("No cases match filter")
        sys.exit(1)

    print(f"Running {len(cases)} regression cases...")
    print("=" * 80)

    results = []
    for i, case in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] {case['id']} ({case.get('category','?')})")
        print(f"  Q: {case['query'][:80]}")
        result = await run_case(case, generate_answer=not args.no_generate)
        results.append(result)
        cls = result["classifier"]
        print(f"  CLS: intent={cls['intent']} product={cls['product']} conf={cls['confidence']:.2f}")
        if result["status"] == "PASS":
            print(f"  ✅ PASS")
        else:
            print(f"  ❌ FAIL ({len(result['errors'])} errors):")
            for err in result["errors"]:
                print(f"     • {err}")

    print()
    print("=" * 80)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = len(results) - passed
    print(f"RESULT: {passed}/{len(results)} PASS, {failed} FAIL")
    if failed:
        print("\nFAILED cases:")
        for r in results:
            if r["status"] == "FAIL":
                print(f"  {r['id']} ({r['category']}) — {len(r['errors'])} errors")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
