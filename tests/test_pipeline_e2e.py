"""
End-to-end тест нового pipeline на реальних запитах Александри.
Запуск у Docker: docker exec emet_bot_app python /app/tests/test_pipeline_e2e.py
"""
import asyncio
import os
import sys
sys.path.insert(0, "/app")

from openai import AsyncOpenAI
from classifier import classify, normalize_product, INTENT_TO_COACH_SUBTYPE, VERBATIM_INTENTS
from prompts_v2 import (
    PROMPT_COACH_BASE, PROMPT_COACH_SOS, PROMPT_COACH_EVALUATE,
    PROMPT_COACH_FEEDBACK, PROMPT_COACH_INFO, PROMPT_COACH_SCRIPT,
    PROMPT_COACH_VISIT, PROMPT_COACH_VERBATIM, PROMPT_EXTRACT
)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Реальні запити Александри (дизлайки + нові складні)
QUERIES = [
    "дорого",  # unclear_no_product → ask
    "Витаран вайтенинг печет",  # clinical_side_effect + verbatim лотерея
    "клієнт питає чому Ellanse такий дорогий колагеностимулятор",  # objection_price
    "косметолог не хоче купувати IUSE SB тому що це моно препарат",  # objection_no_need
    "Petaran - не бачу результату, інші препарати дають більш виражений ліфтинг",  # clinical_no_result
    "у клієнтів тривала постпроцедурна реабілітація після процедури Petaran, на Sculptra немає такого",  # clinical_long_recovery
    "комбо з Petaran",  # combo_with_product
    "Концентрація PDRN 20 мг/мл у Vitaran Whitening - не 20 мг., а 10!",  # correction
    "дай повний протокол - скільки потрібно всього процедур?",  # info_protocol (needs_verbatim)
    "Які переваги Ellansé для лікаря та пацієнта?",  # info_about_product
    "фахівець не хоче працювати з IUSE skin booster, бо вважає що монокомпонентні препарати вже не в тренді. менеджер відповів що гіалуронова кислота це основа косметології",  # evaluate_my_answer
    "інтервал Ellanse і Petaran 6 міс. - не 3 міс.",  # correction
    "склад Vitaran Whitening",  # info_composition (verbatim)
    "поєднується Ellanse з лазером?",  # clinical_contraindication (verbatim)
]


async def run():
    print("=" * 80)
    print("NEW PIPELINE E2E TEST")
    print("=" * 80)
    for i, q in enumerate(QUERIES, 1):
        r = await classify(client, q)
        subtype = INTENT_TO_COACH_SUBTYPE.get(r["intent"], "info")
        prod = normalize_product(r["primary_product"], r["product_variant"])
        verbatim = "YES" if r["needs_verbatim"] else "no"
        print(f"\n[{i:2d}/{len(QUERIES)}]")
        print(f"  Q: {q[:90]}")
        print(f"  intent:    {r['intent']}")
        print(f"  subtype:   {subtype}")
        print(f"  product:   {prod}  (variant: {r['product_variant']})")
        print(f"  competitor: {r['competitor']}")
        print(f"  verbatim:  {verbatim}")
        print(f"  confidence: {r['confidence']:.2f}")


if __name__ == "__main__":
    asyncio.run(run())
