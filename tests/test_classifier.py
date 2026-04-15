"""
Тест classifier на 50 реальних запитах з логів Александри.
Запуск у Docker: docker exec emet_bot_app python /app/tests/test_classifier.py
"""
import asyncio
import os
import sys
sys.path.insert(0, "/app")

from openai import AsyncOpenAI
from classifier import classify

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ═══════════════════════════════════════════════════════════════════════
# Реальні запити з логів — з expected classification
# Формат: (query, expected_intent, expected_primary_product, expected_variant)
# ═══════════════════════════════════════════════════════════════════════

TEST_CASES = [
    # === Критичні дизлайки Александри ===
    ("дорого", "unclear_no_product", None, None),
    ("Концентрація PDRN 20 мг/мл у Vitaran Whitening - не 20 мг., а 10!", "correction", "Vitaran", "Whitening"),
    ("Витаран вайтенинг печет", "clinical_side_effect", "Vitaran", "Whitening"),
    ("клієнт питає чому Vitaran вайтенінг сильно пече", "clinical_side_effect", "Vitaran", "Whitening"),
    ("Які переваги Ellansé для лікаря та пацієнта?", "info_about_product", "Ellansé", None),
    ("клієнт питає чому Ellanse такий дорогий колагеностимулятор", "objection_price", "Ellansé", None),
    ("дай повний протокол - скільки потрібно всього процедур?", "info_protocol", None, None),  # без продукту в цьому запиті
    ("косметолог не хоче купувати IUSE скін бустер тому що це моно препарат, надає перевагу препаратам с мулті складом", "objection_no_need", "IUSE SKINBOOSTER HA 20", None),
    ("косметолог не хоче купувати IUSE SB тому що це моно препарат, надає перевагу препаратам с мулті складом", "objection_no_need", "IUSE SKINBOOSTER HA 20", None),
    ("у клієнтів тривала постпроцедурна реабілітація після процедури Petaran, на Sculptra немає такого", "clinical_long_recovery", "Petaran", None),
    ("Petaran - не бачу результату, інші препарати дають більш виражений ліфтинг", "clinical_no_result", "Petaran", None),

    # === Склад / виробник ===
    ("состав вайтенинг и токс", "info_composition", "Vitaran", "Whitening"),
    ("состав витаран токс и ай", "info_composition", "Vitaran", "Tox"),
    ("Состав витаран вайтенинг", "info_composition", "Vitaran", "Whitening"),
    ("Состав витаран ай", "info_composition", "Vitaran", "i"),
    ("Состав витаран токс", "info_composition", "Vitaran", "Tox"),
    ("Кто производитель витаран", "info_about_product", "Vitaran", None),
    ("производитель эсс", "info_about_product", "ESSE", None),

    # === Конкуренти ===
    ("Конкуренты витаран", "info_comparison", "Vitaran", None),
    ("Конкуренты петаран", "info_comparison", "Petaran", None),
    ("Отличие петаран от джувелук", "info_comparison", "Petaran", None),
    ("Клиент работает с ювелук и не хочет брать петаран", "objection_competitor", "Petaran", None),
    ("Клиент не хочет работать с витаран , работает с реджуран", "objection_competitor", "Vitaran", None),

    # === Робота менеджера ===
    ("я відповіла краще ніж ти", "correction", None, None),
    ("фахівець не хоче працювати з IUSE skin booster, бо вважає що монокомпонентні препарати вже не в тренді. менеджер відповів що гіалуронова кислота це основа косметології і в деяких випадках є необхідним", "evaluate_my_answer", "IUSE SKINBOOSTER HA 20", None),
    ("інтервал Ellanse і Petaran 6 міс. - не 3 міс.", "correction", "Ellansé", None),

    # === Комбо ===
    ("комбо з Petaran", "combo_with_product", "Petaran", None),
    ("комбо з Ellanse", "combo_with_product", "Ellansé", None),
    ("Vitaran I з Vitaran whitening", "combo_with_product", "Vitaran", None),  # два варіанти одного — спроба валідна

    # === Заперечення ===
    ("Ellanse занадто дорого", "objection_price", "Ellansé", None),
    ("не хоче купувати Neuramis", "objection_no_need", "Neuramis", None),
    ("лікар працює з Juvederm роками", "objection_competitor", None, None),
    ("не вірю в ефективність Exoxe", "objection_doubt", "EXOXE", None),
    ("Neuramis багато на сірому ринку", "objection_grey_market", "Neuramis", None),

    # === Скарги ===
    ("після Ellanse болить", "clinical_side_effect", "Ellansé", None),
    ("у пацієнтки папули після Vitaran i", "clinical_side_effect", "Vitaran", "i"),
    ("не бачу ліфтингу від Petaran", "clinical_no_result", "Petaran", None),

    # === Інформація ===
    ("який склад Ellanse?", "info_composition", "Ellansé", None),
    ("як зберігати Vitaran?", "info_storage", "Vitaran", None),
    ("розкажи про EXOXE", "info_about_product", "EXOXE", None),
    ("скільки процедур курс Petaran?", "info_protocol", "Petaran", None),
    ("чим відрізняється Ellanse S від M?", "info_comparison", "Ellansé", None),
    ("для кого IUSE Hair?", "info_indications", "IUSE HAIR REGROWTH", None),

    # === Скрипти / візит ===
    ("дай скрипт продажу Neuramis", "script_request", "Neuramis", None),
    ("готуюсь до візиту дерматолог працює з Juvederm", "visit_prep", None, None),
    ("підготуй до зустрічі з косметологом", "visit_prep", None, None),

    # === Протипоказання ===
    ("чи можна Vitaran при вагітності?", "clinical_contraindication", "Vitaran", None),
    ("поєднується Ellanse з лазером?", "clinical_contraindication", "Ellansé", None),

    # === Спецвипадки ===
    ("привіт", "greeting", None, None),
    ("як справи?", "greeting", None, None),
    ("яка погода сьогодні", "out_of_scope", None, None),
]


async def run():
    print(f"Testing classifier on {len(TEST_CASES)} real queries...\n")
    correct_intent = 0
    correct_product = 0
    mismatches = []

    for i, (query, exp_intent, exp_product, exp_variant) in enumerate(TEST_CASES, 1):
        result = await classify(client, query, chat_history=None)

        intent_ok = result["intent"] == exp_intent
        # Product матч: точний або обидва None
        product_ok = result["primary_product"] == exp_product

        status = []
        if intent_ok:
            correct_intent += 1
        else:
            status.append(f"INTENT: got '{result['intent']}' expected '{exp_intent}'")

        if product_ok:
            correct_product += 1
        else:
            status.append(f"PRODUCT: got '{result['primary_product']}' expected '{exp_product}'")

        mark = "PASS" if (intent_ok and product_ok) else "FAIL"
        print(f"[{i:2d}/{len(TEST_CASES)}] {mark} | intent={result['intent']:25s} | product={result['primary_product']} | conf={result['confidence']:.2f}")
        print(f"     Q: {query[:90]}")
        if status:
            for s in status:
                print(f"     ! {s}")
            mismatches.append((query, exp_intent, exp_product, result))
        print()

    print("=" * 80)
    print(f"INTENT accuracy: {correct_intent}/{len(TEST_CASES)} = {100*correct_intent/len(TEST_CASES):.1f}%")
    print(f"PRODUCT accuracy: {correct_product}/{len(TEST_CASES)} = {100*correct_product/len(TEST_CASES):.1f}%")
    print("=" * 80)

    if mismatches:
        print("\nMISMATCHES:")
        for q, ei, ep, r in mismatches:
            print(f"- Q: {q[:80]}")
            print(f"  Expected: intent={ei}, product={ep}")
            print(f"  Got:      intent={r['intent']}, product={r['primary_product']}, conf={r['confidence']:.2f}")


if __name__ == "__main__":
    asyncio.run(run())
