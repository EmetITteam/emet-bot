"""Test if bot uses Alexandra's exact formulations from filled cards."""
import asyncio, os, sys
sys.path.insert(0, '/app')
from openai import AsyncOpenAI, OpenAI
import classifier as clf
from main import get_context, MODEL_OPENAI_COACH
from prompts_v2 import PROMPT_COACH_BASE, PROMPT_COACH_INFO, PROMPT_COACH_SOS

# Phrases that SHOULD appear (Alexandra's formulations)
EXPECTED_FROM_ALEXANDRA = {
    "Ellansé S": [
        "архітектуру обличчя",  # part of killer phrase
        "власним колагеном",
        "1 процедура замість 2-3",  # objection arg
    ],
    "Petaran": [
        # Killer phrase patterns (will check after seeing actual card)
    ],
    "Ellansé M": [
        "архітектуру обличчя",
    ],
}

QUERIES = [
    ("coach", "Killer phrase для Ellansé S"),
    ("coach", "ТОП-3 заперечення лікарів про Ellansé"),
    ("coach", "Чому переходити на Ellansé коли є Sculptra"),
    ("coach", "Дай аргумент проти 'Ellansé дорого'"),
    ("coach", "Killer phrase для Petaran"),
    ("coach", "З якими EMET-препаратами комбінувати Ellansé S"),
    ("coach", "Профіль ідеального пацієнта для Ellansé S"),
]


async def main():
    cli = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    sync_cli = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    for mode, q in QUERIES:
        print(f"\n{'='*100}")
        print(f"Q: {q}")
        cr = await clf.classify(cli, q, chat_history=[])
        print(f"   intent={cr.get('intent')} | product={cr.get('primary_product')}")

        # Normalize product
        from main import normalize_product
        prod_canon = normalize_product(cr.get('primary_product'), cr.get('product_variant'))

        ctx, srcs = get_context(
            q, mode='coach', provider='openai',
            intent=cr.get('intent'),
            comparison_target=[],
            has_competitor=False,
            product_canonical=prod_canon,
        )

        # Check if Alexandra's KARTKA cards in sources
        kartka_count = sum(1 for v in srcs.values() if 'KARTKA' in v.get('name', ''))
        sales_count = sum(1 for v in srcs.values()
                          if 'sales' in v.get('name', '').lower()
                          or 'sales_director' in v.get('name', '').lower())
        print(f"   sources: {len(srcs)} total | kartka cards: {kartka_count} | sales-section: {sales_count}")

        # LLM call
        sysprompt = PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_INFO
        if 'дорого' in q.lower() or 'sos' in mode:
            sysprompt = PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_SOS

        resp = sync_cli.chat.completions.create(
            model=MODEL_OPENAI_COACH,
            messages=[
                {'role': 'system', 'content': sysprompt},
                {'role': 'user', 'content': f'КОНТЕКСТ:\n{ctx}\n\nВОПРОС:\n{q}'},
            ],
            temperature=0.3, max_tokens=1200,
        )
        ans = resp.choices[0].message.content
        print(f"\n--- ANSWER ({len(ans)} chars) ---")
        print(ans[:1000])


if __name__ == '__main__':
    asyncio.run(main())
