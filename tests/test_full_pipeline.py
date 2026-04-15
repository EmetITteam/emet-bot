"""
Повний E2E тест з реальним викликом RAG + LLM.
Перевіряє 5 критичних сценаріїв з дизлайків Александри:
1. Vitaran Whitening concentration (hallucination check)
2. IUSE SB vs ESSE (product substring bug)
3. Exoxe protocol (RAG empty chunks)
4. Petaran+Ellanse combo (verbatim — не перефразування)
5. "дорого" без продукту → ask clarification

Запуск у Docker.
"""
import asyncio
import os
import sys
import re
sys.path.insert(0, "/app")

from openai import AsyncOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from classifier import (classify, normalize_product, validate_answer,
                        INTENT_TO_COACH_SUBTYPE, VERBATIM_INTENTS)
from prompts_v2 import (PROMPT_COACH_BASE, PROMPT_COACH_SOS, PROMPT_COACH_FEEDBACK,
                        PROMPT_COACH_INFO, PROMPT_COACH_EVALUATE, PROMPT_COACH_VISIT,
                        PROMPT_COACH_SCRIPT, PROMPT_COACH_VERBATIM, PROMPT_EXTRACT)
from prompts import PROMPT_COMBO

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
emb = OpenAIEmbeddings(model="text-embedding-3-small", api_key=os.getenv("OPENAI_API_KEY"))
db_products = Chroma(persist_directory="/app/data/db_index_products_openai", embedding_function=emb)
db_competitors = Chroma(persist_directory="/app/data/db_index_competitors_openai", embedding_function=emb)

PROMPT_MAP = {
    "sos": PROMPT_COACH_SOS,
    "feedback": PROMPT_COACH_FEEDBACK,
    "evaluate": PROMPT_COACH_EVALUATE,
    "info": PROMPT_COACH_INFO,
    "visit": PROMPT_COACH_VISIT,
    "script": PROMPT_COACH_SCRIPT,
}


def product_locked_search(vdb, query, k, product_canonical):
    if not product_canonical:
        return vdb.similarity_search(query, k=k)
    try:
        docs = vdb.similarity_search(query, k=k, filter={"product_canonical": product_canonical})
        if len(docs) >= k // 2:
            return docs
        if product_canonical.startswith("HP Cell Vitaran"):
            generic = vdb.similarity_search(query, k=k // 2, filter={"product_canonical": "Vitaran"})
            return (docs + generic)[:k]
        return docs or vdb.similarity_search(query, k=k)
    except Exception:
        return vdb.similarity_search(query, k=k)


def format_context(docs):
    parts = []
    for d in docs:
        src = d.metadata.get("source", "?")
        pc = d.metadata.get("product_canonical", "?")
        parts.append(f"[📘 {src} | product={pc}]\n{d.page_content}")
    return "\n\n".join(parts)


async def extract_facts(context, query):
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini", timeout=30, temperature=0.0, max_tokens=800,
            messages=[
                {"role": "system", "content": PROMPT_EXTRACT},
                {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nЗАПИТ:\n{query}"}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return ""


async def process(query):
    """Повний pipeline: classify → RAG → extract → generate → validate."""
    # 1. Classify
    cls = await classify(client, query)
    intent = cls["intent"]
    product = normalize_product(cls["primary_product"], cls["product_variant"])
    subtype = INTENT_TO_COACH_SUBTYPE.get(intent, "info")
    verbatim = cls["needs_verbatim"]
    print(f"  CLASSIFIER: intent={intent} product={product} subtype={subtype} verbatim={verbatim}")

    # 2. Special cases
    if intent == "unclear_no_product":
        return "📝 Про який продукт йде мова?..."
    if intent in ("greeting", "out_of_scope"):
        return "(kb response)"

    # 3. Select prompt
    if subtype == "combo":
        system_prompt = PROMPT_COMBO
    elif verbatim and subtype == "info":
        system_prompt = PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_VERBATIM
    else:
        system_prompt = PROMPT_COACH_BASE + "\n\n" + PROMPT_MAP.get(subtype, PROMPT_COACH_INFO)

    # 4. RAG — для combo semantic search, для інших product-lock
    has_comp = bool(cls["competitor"])
    if subtype == "combo":
        docs_p = db_products.similarity_search(query, k=15)
    else:
        docs_p = product_locked_search(db_products, query, 12, product)
    docs_c = db_competitors.similarity_search(query, k=8) if has_comp else []
    context = format_context(docs_p + docs_c)

    # Лог product distribution у отриманих чанках
    from collections import Counter
    products_in_ctx = Counter(d.metadata.get("product_canonical", "?") for d in docs_p)
    print(f"  RAG products: {dict(products_in_ctx)}")

    # 5. Extract facts (except feedback)
    extracted = ""
    if subtype not in ("feedback",):
        extracted = await extract_facts(context, query)

    ctx = f"ВИТЯГНУТІ ФАКТИ:\n{extracted}\n\n{context}" if extracted else context

    # 6. Generate
    resp = await client.chat.completions.create(
        model="gpt-4o", timeout=30, temperature=0.3, max_tokens=1500,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"КОНТЕКСТ:\n{ctx}\n\nВОПРОС:\n{query}"}
        ]
    )
    answer = resp.choices[0].message.content.strip()
    answer = re.sub(r'\*\*(.+?)\*\*', r'*\1*', answer)

    # 7. Validate
    if verbatim or cls["confidence"] < 0.75:
        val = await validate_answer(client, query, answer, product)
        print(f"  VALIDATOR: valid={val['valid']} severity={val.get('severity')} issues={val.get('issues')}")

    return answer


TESTS = [
    ("CRITICAL: Vitaran Whitening concentration",
     "Витаран вайтенинг печет",
     ["10 мг/мл", "Whitening"],
     ["20 мг/мл"]),  # заборонено

    ("CRITICAL: IUSE SB не плутати з ESSE",
     "косметолог не хоче купувати IUSE SB тому що це моно препарат",
     ["IUSE", "Skin Booster"],
     ["ESSE"]),

    ("CRITICAL: Exoxe protocol (після reindex)",
     "який протокол Exoxe?",
     ["Exoxe", "процедур"],
     ["Уточніть"]),

    ("CRITICAL: Petaran+Ellanse combo verbatim",
     "комбо Petaran + Ellanse",
     ["3 місяці", "зони"],
     []),  # має цитувати нюанс "не в одну зону"

    ("CRITICAL: дорого без продукту → ask",
     "дорого",
     ["Про який продукт"],
     ["Vitaran", "20-30%"]),
]


async def main():
    results = []
    for name, query, must_have, must_not in TESTS:
        print(f"\n{'=' * 80}")
        print(f"TEST: {name}")
        print(f"QUERY: {query}")
        print("-" * 80)
        answer = await process(query)
        print(f"\nANSWER ({len(answer)} chars):\n{answer[:800]}")

        # Перевірки
        a_lower = answer.lower()
        failed_have = [kw for kw in must_have if kw.lower() not in a_lower]
        failed_not = [kw for kw in must_not if kw.lower() in a_lower]

        passed = not failed_have and not failed_not
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"\n{status}")
        if failed_have:
            print(f"  MISSING: {failed_have}")
        if failed_not:
            print(f"  FORBIDDEN FOUND: {failed_not}")
        results.append((name, passed))

    print(f"\n{'=' * 80}")
    passed = sum(1 for _, p in results if p)
    print(f"FINAL: {passed}/{len(results)} passed")
    for name, p in results:
        print(f"  {'✅' if p else '❌'} {name}")


if __name__ == "__main__":
    asyncio.run(main())
