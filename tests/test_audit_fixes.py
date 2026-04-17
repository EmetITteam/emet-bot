"""
Тест фіксів за результатами аудиту 16.04 (51 запит).
Запуск у Docker: docker exec emet_bot_app python /app/tests/test_audit_fixes.py
"""
import asyncio, os, sys, re
sys.path.insert(0, "/app")
from openai import AsyncOpenAI
from classifier import classify, normalize_product, INTENT_TO_COACH_SUBTYPE

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

TESTS = [
    # Fix 1: Contraindication safety
    {
        "name": "Pregnancy safety",
        "query": "можно ли использовать Vitaran во время беременности",
        "check_intent": "clinical_contraindication",
        "answer_must_have": ["протипоказан"],
        "answer_must_not": ["безпечн", "альтернатива для вагітних"],
    },
    # Fix 2: mass-market ban in all modes
    {
        "name": "INFO without mass-market",
        "query": "Чому Vitaran менш болючий за Rejuran Healer?",
        "check_intent": "clinical_why",
        "answer_must_not": ["mass-market", "мас-маркет"],
    },
    # Fix 3: source_question routing
    {
        "name": "Source question routing",
        "query": "из какого документа инфо?",
        "check_intent": "source_question",
    },
    {
        "name": "Source question 2",
        "query": "из какого документа ты взял эту информацию?",
        "check_intent": "source_question",
    },
    {
        "name": "Source question 3",
        "query": "с какого документа инфо",
        "check_intent": "source_question",
    },
    # Fix 4: clinical_why short answer
    {
        "name": "Clinical why — short",
        "query": "почему Vitaran безболезненный",
        "check_intent": "clinical_why",
        "answer_must_have": ["Pain Relief"],
    },
    # Existing tests — regression
    {
        "name": "Vitaran дорого — patient value",
        "query": "Vitaran дорого",
        "check_intent": "objection_price",
        "answer_must_not": ["Rejuran", "mass-market", "Коротка готова фраза менеджера"],
    },
    {
        "name": "Whitening пече — clinical",
        "query": "Vitaran Whitening пече",
        "check_intent": "clinical_side_effect",
        "answer_must_have": ["глутатіон", "транексамова"],
    },
    {
        "name": "Combo Petaran+Ellanse",
        "query": "комбо Petaran + Ellanse",
        "check_intent": "combo_with_product",
        "answer_must_have": ["зон"],
        "answer_must_not": ["[інформація відсутня]"],
    },
    {
        "name": "Grey market Neuramis",
        "query": "на сірому ринку Neuramis дешевше",
        "check_intent": "objection_grey_market",
        "answer_must_have": ["юридичн"],
    },
    {
        "name": "Дорого без продукту",
        "query": "дорого",
        "check_intent": "unclear_no_product",
    },
    {
        "name": "Evaluate answer",
        "query": "менеджер відповів що гіалуронова кислота це основа косметології, оціни",
        "check_intent": "evaluate_my_answer",
        "answer_must_have": ["/10"],
    },
]


async def main():
    print("=" * 80)
    print(f"AUDIT FIXES TEST — {len(TESTS)} scenarios")
    print("=" * 80)

    passed = 0
    failed = []

    for i, tc in enumerate(TESTS, 1):
        r = await classify(client, tc["query"])
        intent = r["intent"]
        product = normalize_product(r["primary_product"], r["product_variant"])

        issues = []

        # Intent check
        if tc.get("check_intent") and intent != tc["check_intent"]:
            issues.append(f"INTENT: expected={tc['check_intent']}, got={intent}")

        status = "PASS" if not issues else "FAIL"
        if not issues:
            passed += 1
        else:
            failed.append((tc["name"], issues))

        print(f"[{i:2d}/{len(TESTS)}] {status} | {tc['name']}")
        print(f"  Q: {tc['query'][:80]}")
        print(f"  intent={intent} product={product} conf={r['confidence']:.2f}")
        if issues:
            for iss in issues:
                print(f"  ! {iss}")
        print()

    print("=" * 80)
    print(f"CLASSIFIER ROUTING: {passed}/{len(TESTS)} passed")
    if failed:
        print("FAILED:")
        for name, issues in failed:
            print(f"  - {name}: {', '.join(issues)}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
