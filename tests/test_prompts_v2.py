"""
Тест модульних промптів v2.
Запускати всередині Docker: docker exec emet_bot_app python /app/tests/test_prompts_v2.py
"""
import asyncio
import os
import sys
import time
sys.path.insert(0, "/app")

from openai import AsyncOpenAI
from prompts_v2 import (
    PROMPT_EXTRACT, PROMPT_COACH_BASE, PROMPT_COACH_SOS,
    PROMPT_COACH_EVALUATE, PROMPT_COACH_FEEDBACK,
    PROMPT_COACH_INFO, PROMPT_COACH_SCRIPT, PROMPT_COACH_VISIT
)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_COACH = "gpt-4o"
MODEL_EXTRACT = "gpt-4o-mini"

# --- Тестовий RAG-контекст (реальні дані з бази) ---
SAMPLE_CONTEXT = """
[Джерело: НАВЧАЛЬНИЙ КУРС EMET | Ellanse]
Ellanse - ін'єкційний філер на основі полікапролактону (PCL).
Ellanse S - тривалість дії 18 місяців. Ellanse M - тривалість дії 24 місяці.
Складається з CMC-гелю (карбоксиметилцелюлоза) + PCL-мікросфери 25-50 мкм.
Механізм: негайний об'єм (CMC-гель) + неоколагенез I типу (PCL-мікросфери поступово розчиняються, стимулюючи власний колаген).
Маржинальність для лікаря: середній чек процедури 8000-15000 грн, собівартість шприца ~3500 грн.
CE-сертифікація, клас III медичний виріб.
Показання: корекція носогубних складок, контурна пластика обличчя, волюмізація вилиць та підборіддя.

[Джерело: КОНКУРЕНТ | Sculptra vs Ellanse]
Sculptra (Galderma) - полімолочна кислота (PLLA). Потребує 2-3 сеанси з інтервалом 4-6 тижнів.
Результат видно через 4-6 тижнів після першого сеансу. Тривалість до 25 місяців.
Ризик вузликів при неправильному розведенні.
Ellanse: результат одразу + біостимуляція. Одна процедура. PCL ≠ PLLA - різні механізми.
Ellianse перевага: об'єм + ліфтинг одночасно, а не лише біостимуляція як у Sculptra.

[Джерело: НАВЧАЛЬНИЙ КУРС EMET | Neuramis]
Neuramis - лінійка філерів на основі гіалуронової кислоти (HA).
Neuramis Volume - для глибокої волюмізації, 1 мл. Neuramis Deep - для середніх зморшок.
Виробник: Medytox (Південна Корея). CE-сертифікація.
Маржинальність: середній чек 4000-7000 грн, собівартість ~1800 грн.
Конкурент: Juvederm (Allergan) - середній чек 6000-12000 грн, собівартість ~4500 грн.
Перевага Neuramis: вища маржа при порівнянній якості + повна лінійка.

[Джерело: НАВЧАЛЬНИЙ КУРС EMET | HP Cell Vitaran]
HP Cell Vitaran i - полінуклеотидний препарат для біоревіталізації.
Концентрація PN: 20 мг/мл (найвища серед аналогів).
Pain Relief Technology - pH 7.6-7.8, зниження болю на 32%, папул на 76%, еритеми на 33%.
Ionization-Adjusted PN - дисоціація Na+, осмолярність 280-320 мОсм/кг.
Зберігання: кімнатна температура (не потребує холодильника).
"""

# --- Тестові запити по кожному типу ---
TEST_CASES = [
    # === Тип A: SOS (заперечення) ===
    {
        "name": "SOS-1: Ціна Ellanse",
        "type": "A",
        "query": "Лікар каже що Ellanse занадто дорогий, є дешевші філери",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_SOS,
        "check": ["марж", "killer", "крок"],  # "марж" = маржа/маржу/маржі/маржинальність
        "extract": True,
    },
    {
        "name": "SOS-2: Конкурент Sculptra",
        "query": "Лікар каже що вже працює зі Скульптрою і не хоче міняти",
        "type": "A",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_SOS,
        "check": ["sculptra", "полікапролактон", "killer"],  # PCL або полікапролактон
        "extract": True,
    },
    # === Тип B: Оцінка менеджера ===
    {
        "name": "EVALUATE-1: Менеджер порівнює Ellanse",
        "type": "B",
        "query": "Я відповіла лікарю: 'Ellanse тримається довше за Sculptra і дає миттєвий результат'. Оціни мою відповідь",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_EVALUATE,
        "check": ["/10", "добре", "покращити"],
        "extract": False,
    },
    {
        "name": "EVALUATE-2: Менеджер продає Neuramis",
        "query": "Я сказала лікарю: 'Neuramis дешевше за Juvederm і якість така сама'. Як я відповіла?",
        "type": "B",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_EVALUATE,
        "check": ["/10", "дешев"],
        "extract": False,
    },
    # === Тип C: Визнання помилки ===
    {
        "name": "FEEDBACK-1: Виправлення терміну",
        "type": "C",
        "query": "Ти помилився, Ellanse S це 18 місяців, а не 12",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_FEEDBACK,
        "check": ["18"],
        "extract": False,
        "anti_check": ["SOS", "Крок 1", "Суть"],
    },
    {
        "name": "FEEDBACK-2: Моя відповідь краща",
        "type": "C",
        "query": "Я відповіла краще ніж ти запропонував, лікар одразу погодився",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_FEEDBACK,
        "check": ["рацію"],  # "визнай" може бути "дякую" або "маєте рацію"
        "extract": False,
        "anti_check": ["SOS", "Крок 1"],
    },
    # === Тип D: Інформація ===
    {
        "name": "INFO-1: Що таке Vitaran",
        "type": "D",
        "query": "Розкажи про HP Cell Vitaran i — склад, механізм, для кого",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_INFO,
        "check": ["20 мг/мл", "Pain Relief", "полінуклеотид"],
        "extract": False,
    },
    {
        "name": "INFO-2: Порівняння Ellanse S vs M",
        "type": "D",
        "query": "Чим відрізняється Ellanse S від Ellanse M?",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_INFO,
        "check": ["18", "24"],
        "extract": False,
    },
    # === Тип E: Підготовка до візиту ===
    {
        "name": "VISIT-1: Дерматолог з Juvederm",
        "type": "E",
        "query": "Готуюсь до візиту: дерматолог, приватна клініка, працює з Juvederm, цікавиться біостимуляцією",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_VISIT,
        "check": ["брифінг", "Juvederm", "питання"],
        "extract": True,
    },
    {
        "name": "VISIT-2: Косметолог перший візит",
        "type": "E",
        "query": "Перший візит до косметолога, вона працює зі Скульптрою, клініка преміум-сегменту",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_VISIT,
        "check": ["Sculptra", "продукт"],
        "extract": True,
    },
    # === Звичайні запити: склад, зберігання, порівняння ===
    {
        "name": "REGULAR-1: Склад Ellanse",
        "type": "D",
        "query": "Який склад Ellanse?",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_INFO,
        "check": ["CMC", "PCL", "полікапролактон"],
        "extract": False,
    },
    {
        "name": "REGULAR-2: Зберігання Vitaran",
        "type": "D",
        "query": "Як зберігати Vitaran?",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_INFO,
        "check": ["кімнатн"],
        "extract": False,
    },
    # === Скрипт ===
    {
        "name": "SCRIPT-1: Скрипт продажу Neuramis",
        "type": "SCRIPT",
        "query": "Дай скрипт як запропонувати Neuramis лікарю який працює з Juvederm",
        "prompt": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_SCRIPT,
        "check": ["маржа", "менеджер"],
        "extract": True,
    },
]


async def run_extract(context, query):
    """Step 1: extract facts."""
    t0 = time.time()
    resp = await client.chat.completions.create(
        model=MODEL_EXTRACT,
        timeout=15,
        messages=[
            {"role": "system", "content": PROMPT_EXTRACT},
            {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nЗАПИТ МЕНЕДЖЕРА:\n{query}"}
        ],
        temperature=0.0,
        max_tokens=800
    )
    text = resp.choices[0].message.content.strip()
    return text, time.time() - t0


async def run_llm(prompt, context, query):
    """Step 2: main LLM call."""
    t0 = time.time()
    resp = await client.chat.completions.create(
        model=MODEL_COACH,
        timeout=30,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nВОПРОС:\n{query}"}
        ],
        temperature=0.3,
        max_tokens=1500
    )
    text = resp.choices[0].message.content.strip()
    tokens_in = resp.usage.prompt_tokens
    tokens_out = resp.usage.completion_tokens
    return text, tokens_in, tokens_out, time.time() - t0


async def main():
    print("=" * 80)
    print("TEST MODULAR PROMPTS v2 — real LLM calls")
    print("=" * 80)

    total = len(TEST_CASES)
    passed = 0
    failed_names = []

    for i, tc in enumerate(TEST_CASES, 1):
        name = tc["name"]
        print(f"\n{'─' * 70}")
        print(f"[{i}/{total}] {name} (Type {tc['type']})")
        print(f"Query: {tc['query'][:80]}...")

        context = SAMPLE_CONTEXT
        extract_text = ""

        # Step 1: extract facts
        if tc.get("extract"):
            extract_text, ext_time = await run_extract(context, tc["query"])
            print(f"  Extract ({ext_time:.1f}s): {extract_text[:120]}...")
            context = f"ВИТЯГНУТІ ФАКТИ:\n{extract_text}\n\n{SAMPLE_CONTEXT}"

        # Step 2: LLM call
        answer_raw, tok_in, tok_out, llm_time = await run_llm(tc["prompt"], context, tc["query"])

        # Post-processing як в production (main.py:1807)
        import re
        answer = re.sub(r'\*\*(.+?)\*\*', r'*\1*', answer_raw)
        _had_double_stars = answer != answer_raw

        # Check quality
        answer_lower = answer.lower()
        checks_passed = []
        checks_failed = []
        for kw in tc.get("check", []):
            if kw.lower() in answer_lower:
                checks_passed.append(kw)
            else:
                checks_failed.append(kw)

        anti_failed = []
        for kw in tc.get("anti_check", []):
            if kw.lower() in answer_lower:
                anti_failed.append(kw)

        # Double stars — post-processing фіксить, але логуємо як warning
        ok = len(checks_failed) == 0 and len(anti_failed) == 0

        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed_names.append(name)

        print(f"  LLM ({llm_time:.1f}s, {tok_in}+{tok_out} tok): {len(answer)} chars")
        print(f"  Checks OK: {checks_passed}")
        if checks_failed:
            print(f"  Checks MISSING: {checks_failed}")
        if anti_failed:
            print(f"  Anti-checks FOUND (bad): {anti_failed}")
        if _had_double_stars:
            print(f"  ⚠️ Raw had ** (fixed by post-processing regex)")
        print(f"  [{status}]")
        print(f"\n  ANSWER (first 500 chars):\n  {answer[:500]}")

    print(f"\n{'=' * 80}")
    print(f"RESULTS: {passed}/{total} passed")
    if failed_names:
        print(f"FAILED: {', '.join(failed_names)}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
