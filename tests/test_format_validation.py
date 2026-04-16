"""
Тест формату відповідей SOS — перевіряє що бот слідує правилам.
Запуск у Docker: docker exec emet_bot_app python /app/tests/test_format_validation.py
"""
import asyncio
import os
import sys
import re
sys.path.insert(0, "/app")

from openai import AsyncOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from classifier import classify, normalize_product, INTENT_TO_COACH_SUBTYPE
from prompts_v2 import (PROMPT_COACH_BASE, PROMPT_COACH_SOS, PROMPT_COACH_INFO,
                        PROMPT_COACH_FEEDBACK, PROMPT_COACH_EVALUATE, PROMPT_COACH_SCRIPT,
                        PROMPT_COACH_VISIT, PROMPT_COACH_VERBATIM, PROMPT_EXTRACT)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
emb = OpenAIEmbeddings(model="text-embedding-3-small", api_key=os.getenv("OPENAI_API_KEY"))
db_products = Chroma(persist_directory="/app/data/db_index_products_openai", embedding_function=emb)
db_competitors = Chroma(persist_directory="/app/data/db_index_competitors_openai", embedding_function=emb)

PROMPT_MAP = {
    "sos": PROMPT_COACH_SOS, "info": PROMPT_COACH_INFO,
    "feedback": PROMPT_COACH_FEEDBACK, "evaluate": PROMPT_COACH_EVALUATE,
    "script": PROMPT_COACH_SCRIPT, "visit": PROMPT_COACH_VISIT,
}


def product_locked_search(vdb, query, k, product):
    if not product:
        return vdb.similarity_search(query, k=k)
    try:
        docs = vdb.similarity_search(query, k=k, filter={"product_canonical": product})
        if len(docs) >= k // 2:
            return docs
        if product.startswith("HP Cell Vitaran"):
            gen = vdb.similarity_search(query, k=k // 2, filter={"product_canonical": "Vitaran"})
            return (docs + gen)[:k]
        return docs or vdb.similarity_search(query, k=k)
    except Exception:
        return vdb.similarity_search(query, k=k)


def format_context(docs):
    return "\n\n".join(
        f"[📘 {d.metadata.get('source','?')}]\n{d.page_content}" for d in docs
    )


def postprocess(answer):
    """Симулює production post-processing (main.py)."""
    answer = re.sub(r'\[?REF\d+\]?', '', answer).strip()
    answer = answer.replace("  ", " ")
    answer = re.sub(r'\*\*(.+?)\*\*', r'*\1*', answer)
    answer = re.sub(
        r'^\s*(?:💬\s*)?\*?Коротка готова фраза менеджера[^\n]*[:\*]?\s*\n+«[^»]*»\s*\n+',
        '', answer, flags=re.MULTILINE
    )
    answer = re.sub(
        r'^\s*(?:💬\s*)?\*?Готова фраза менеджера[^\n]*[:\*]?\s*\n+«[^»]*»\s*\n+',
        '', answer, flags=re.MULTILINE
    )
    return answer.strip()


async def extract_facts(ctx, q):
    try:
        r = await client.chat.completions.create(
            model="gpt-4o-mini", timeout=30, temperature=0.0, max_tokens=800,
            messages=[{"role": "system", "content": PROMPT_EXTRACT},
                      {"role": "user", "content": f"КОНТЕКСТ:\n{ctx}\n\nЗАПИТ:\n{q}"}]
        )
        return r.choices[0].message.content.strip()
    except: return ""


async def process(query):
    """Повна імітація pipeline."""
    cls = await classify(client, query)
    intent = cls["intent"]
    product = normalize_product(cls["primary_product"], cls["product_variant"])
    subtype = INTENT_TO_COACH_SUBTYPE.get(intent, "info")

    if intent == "unclear_no_product":
        return "📝 Про який продукт йде мова?...", cls, subtype

    # Спец-case для combo
    if subtype == "combo":
        from prompts import PROMPT_COMBO
        system_prompt = PROMPT_COMBO
    elif cls["needs_verbatim"] and subtype == "info":
        system_prompt = PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_VERBATIM
    else:
        system_prompt = PROMPT_COACH_BASE + "\n\n" + PROMPT_MAP.get(subtype, PROMPT_COACH_INFO)

    docs = product_locked_search(db_products, query, 12, product) if subtype != "combo" \
        else db_products.similarity_search(query, k=15)
    docs_c = db_competitors.similarity_search(query, k=8) if cls["competitor"] else []
    context = format_context(docs + docs_c)

    extracted = await extract_facts(context, query) if subtype != "feedback" else ""
    ctx = f"ВИТЯГНУТІ ФАКТИ:\n{extracted}\n\n{context}" if extracted else context

    user_msg = f"[Продукт: {product}]\n\nПИТАННЯ:\n{query}" if product else query
    r = await client.chat.completions.create(
        model="gpt-4o", timeout=60, temperature=0.3, max_tokens=1500,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": f"КОНТЕКСТ:\n{ctx}\n\nВОПРОС:\n{user_msg}"}]
    )
    raw = r.choices[0].message.content.strip()
    answer = postprocess(raw)
    return answer, cls, subtype


# ═══════════════════════════════════════════════════════════════════════
# ТЕСТИ
# ═══════════════════════════════════════════════════════════════════════

TESTS = [
    # === SOS 'дорого' — з продуктом і без ===
    {
        "name": "SOS: Vitaran дорого",
        "query": "Vitaran дорого",
        "expected_intent": "objection_price",
        "must_have": ["Крок 1", "Killer phrase", "Наступний крок"],
        "must_not_have": [
            "Коротка готова фраза менеджера",
            "mass-market",
            "аптечний аналог",
            "тестова процедура",
            "привезти зразки",
            "Rejuran",  # конкурент не згаданий у запиті
            "Plinest",
            "Nucleofill",
        ],
        # patient value — хоча б 2 з 3 аргументів мають бути
        "structure_any_2_of": [
            "тривалість",
            "реабілітац",
            "миттєв",
            "ефект",
        ],
    },
    {
        "name": "SOS: Ellanse дорого",
        "query": "Ellanse дорогий",
        "expected_intent": "objection_price",
        "must_have": ["Killer phrase"],
        "must_not_have": ["Коротка готова фраза менеджера", "mass-market", "привезти зразки"],
    },
    {
        "name": "SOS: дорого без продукту",
        "query": "дорого",
        "expected_intent": "unclear_no_product",
        "must_have": ["Про який продукт"],
        "must_not_have": ["20 мг/мл", "Rejuran", "Vitaran"],
    },
    # === Clinical side effect ===
    {
        "name": "Clinical: Vitaran Whitening пече",
        "query": "Vitaran Whitening пече",
        "expected_intent": "clinical_side_effect",
        "must_have": ["Чому", "глутатіон", "транексамова"],
        "must_not_have": ["20 мг/мл", "mass-market", "привезти зразки для тестування"],
    },
    # === Competitor ===
    {
        "name": "Competitor: лояльна до Sculptra",
        "query": "лікар лояльна до Sculptra, не хоче міняти",
        "expected_intent": "objection_competitor",
        "must_have": ["Killer phrase"],
        "must_not_have": ["Коротка готова фраза менеджера", "mass-market"],
    },
    # === Info ===
    {
        "name": "Info: склад Ellanse",
        "query": "який склад Ellanse?",
        "expected_intent": "info_composition",
        # Бот може писати VERBATIM з документа — "полікапролакто" / "ПКЛ" / "PCL"
        "must_have_any": ["полікапролакто", "ПКЛ", "PCL"],
        "must_have_any_2": ["карбоксиметилцелюлоз", "CMC", "КМЦ"],
    },
    # === Combo ===
    {
        "name": "Combo: Petaran + Ellanse",
        "query": "комбо Petaran + Ellanse",
        "expected_intent": "combo_with_product",
        "must_have": ["зон"],
        "must_not_have": ["[інформація відсутня]"],
    },
]


async def main():
    print("=" * 80)
    print("FORMAT VALIDATION TEST")
    print("=" * 80)

    passed = 0
    failed = []

    for i, tc in enumerate(TESTS, 1):
        print(f"\n{'─' * 80}")
        print(f"[{i}/{len(TESTS)}] {tc['name']}")
        print(f"Q: {tc['query']}")

        try:
            answer, cls, subtype = await process(tc["query"])
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            failed.append((tc["name"], f"ERROR: {e}"))
            continue

        print(f"  CLS: intent={cls['intent']} product={normalize_product(cls['primary_product'], cls['product_variant'])} subtype={subtype}")

        issues = []
        a_lower = answer.lower()

        # Intent check
        if cls["intent"] != tc["expected_intent"]:
            issues.append(f"intent expected={tc['expected_intent']}, got={cls['intent']}")

        # Must have (усі)
        for kw in tc.get("must_have", []):
            if kw.lower() not in a_lower:
                issues.append(f"MISSING: {kw}")

        # Must not have (жоден)
        for kw in tc.get("must_not_have", []):
            if kw.lower() in a_lower:
                issues.append(f"FORBIDDEN FOUND: {kw}")

        # Must have ANY (хоча б один)
        for kw_list_name in ["must_have_any", "must_have_any_2", "must_have_any_3"]:
            kws = tc.get(kw_list_name, [])
            if kws and not any(kw.lower() in a_lower for kw in kws):
                issues.append(f"{kw_list_name.upper()}: none of {kws} found")

        # Structure — ANY 2
        any2 = tc.get("structure_any_2_of", [])
        if any2:
            found = sum(1 for kw in any2 if kw.lower() in a_lower)
            if found < 2:
                issues.append(f"STRUCTURE: only {found}/min 2 from {any2}")

        # Structure — усі
        for kw in tc.get("must_have_structure", []):
            if kw.lower() not in a_lower:
                issues.append(f"STRUCTURE MISSING: {kw}")

        if not issues:
            passed += 1
            print(f"  ✅ PASS")
        else:
            failed.append((tc["name"], ", ".join(issues)))
            print(f"  ❌ FAIL:")
            for iss in issues:
                print(f"     • {iss}")

        print(f"\n  ANSWER (first 600 chars):")
        print("  " + answer[:600].replace("\n", "\n  "))

    print(f"\n{'=' * 80}")
    print(f"RESULT: {passed}/{len(TESTS)} passed")
    if failed:
        print(f"\nFAILED ({len(failed)}):")
        for name, reason in failed:
            print(f"  ❌ {name}")
            print(f"     {reason}")


if __name__ == "__main__":
    asyncio.run(main())
