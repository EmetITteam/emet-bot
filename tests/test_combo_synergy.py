"""
Тест combo synergy — чи бот синтезує "що дає ця комбінація".
Запуск у Docker: docker exec emet_bot_app python /app/tests/test_combo_synergy.py
"""
import asyncio, os, sys, re
sys.path.insert(0, "/app")
from openai import AsyncOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from main import get_combo_synergy_context, _search_with_product_filter, _get_vdb
from classifier import classify, normalize_product
from prompts import PROMPT_COMBO

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
emb = OpenAIEmbeddings(model="text-embedding-3-small", api_key=os.getenv("OPENAI_API_KEY"))
db = Chroma(persist_directory="/app/data/db_index_products_openai", embedding_function=emb)

TESTS = [
    ("комбо Petaran + Ellanse", "Petaran", "Ellansé", ["ліфтинг", "колаген"]),
    ("комбо Vitaran + Exoxe", "Vitaran", "EXOXE", ["регенера", "екзосом"]),
    ("комбо з Ellanse", "Ellansé", None, ["Ellans"]),
]

async def main():
    print("=" * 80)
    print("COMBO SYNERGY TEST")
    print("=" * 80)

    for query, prod_a, prod_b, check_words in TESTS:
        print(f"\nQ: {query}")

        # 1. Classify
        cls = await classify(client, query)
        product = normalize_product(cls["primary_product"], cls["product_variant"])
        print(f"  CLS: intent={cls['intent']} product={product} secondary={cls.get('secondary_product')}")

        # 2. Get synergy context
        synergy = get_combo_synergy_context(prod_a, prod_b)
        print(f"  SYNERGY context: {len(synergy)} chars")
        if synergy:
            print(f"  First 300: {synergy[:300]}")

        # 3. Get combo context
        docs = db.similarity_search(query, k=15)
        context = "\n\n".join(d.page_content[:400] for d in docs)

        # 4. Full context
        full_ctx = f"ЕФЕКТИ ПРОДУКТІВ ДЛЯ СИНЕРГІЇ:\n{synergy}\n\n{context}" if synergy else context

        # 5. Generate
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o", timeout=60, temperature=0.3, max_tokens=2000,
                messages=[
                    {"role": "system", "content": PROMPT_COMBO},
                    {"role": "user", "content": f"КОНТЕКСТ:\n{full_ctx}\n\nВОПРОС:\n{query}"}
                ]
            )
            answer = resp.choices[0].message.content.strip()
            answer = re.sub(r'\*\*(.+?)\*\*', r'*\1*', answer)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        # 6. Check
        has_synergy = "комбінація" in answer.lower() or "разом" in answer.lower() or "синерг" in answer.lower()
        found = sum(1 for w in check_words if w.lower() in answer.lower())

        print(f"  HAS SYNERGY SECTION: {has_synergy}")
        print(f"  CHECK WORDS: {found}/{len(check_words)} ({check_words})")
        print(f"\n  ANSWER ({len(answer)} chars):")
        print("  " + answer[:1000].replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    asyncio.run(main())
