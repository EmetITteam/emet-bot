#!/usr/bin/env python3
"""
Regression test: same patterns as in today's real session (2026-03-24).
Tests early routing logic WITHOUT calling OpenAI/Gemini — pure keyword/rule checks.
Run: docker exec -it emet_bot_app python3 /app/test_routing_v2.py
"""

# ── Replicate routing constants from main.py ────────────────────────────────

_SCRIPT_KEYWORDS = [
    "дай диалог", "дай діалог", "дай скрипт", "скрипт з лікарем",
    "діалог з лікарем", "диалог с врачом", "розіграй діалог",
    "зіграй діалог", "покажи діалог", "покажи диалог",
    "конкретный диалог", "конкретний діалог",
    "пример диалога", "приклад діалогу",
]

_COMBO_KEYWORDS = [
    "комбо", "комбін", "combo", "поєднати", "поєднання", "сочетать",
    "сочетание", "совместить", "протокол для", "протоколи для",
    "протоколы для", "які протоколи", "какие протоколы",
]

_OPERATIONAL_EARLY_KEYWORDS = [
    "відрядження", "командировк", "возврат товар", "повернення товар",
    "відшкодуванн", "возмещени", "оформити витрат", "оформить расход",
    "семінар", "семинар", "документи для поїздки", "документы для поездки",
]

_CLIENT_RESISTANCE_KW = [
    "не хоче", "не хочет", "не хочу", "відмовляєть", "відмовляєт",
    "не йде", "не идет", "не хочет идти", "не хоче йти",
    "против", "проти", "не згоден", "не согласен",
]

_FOLLOWUP_COACH_KEYWORDS = [
    "інші аргументи", "другие аргументы", "ще аргументи", "що ще сказати", "что ещё сказать",
    "розпиши детально", "распиши подробно", "детально розпиши", "більше варіантів", "больше вариантов",
    "розпиши діалог", "распиши диалог", "варіанти відповідей", "варианты ответов на",
    "детальніше", "подробнее", "ещё варианты", "ще варіанти",
    "дай діалог", "дай конкретный диалог", "дай конкретний діалог",
    "покажи діалог", "покажи диалог", "приклад діалогу", "пример диалога",
    "як відповісти", "как ответить", "що сказати", "что сказать",
    "розкажи про", "расскажи про", "розкажи більше про", "розкажи детально про",
    "що таке", "что такое", "чим відрізняєть", "чем отличается",
]

_AFFIRMATION_KEYWORDS = [
    "хочу", "так", "да", "ок", "добре", "хорошо", "ага", "угу",
    "потрібно", "нужно", "давай", "продовж", "продолжай", "далі", "більше", "ще", "ещё",
]


def simulate_early_routing(text: str, has_history: bool = False) -> dict:
    t = text.lower().strip()

    is_script_early = any(kw in t for kw in _SCRIPT_KEYWORDS)
    is_combo = any(kw in t for kw in _COMBO_KEYWORDS)
    is_operational = any(kw in t for kw in _OPERATIONAL_EARLY_KEYWORDS)
    has_resistance = any(kw in t for kw in _CLIENT_RESISTANCE_KW)

    # Fix: семінар + опір клієнта → не operational
    if is_operational and has_resistance:
        is_operational = False

    is_followup = any(kw in t for kw in _FOLLOWUP_COACH_KEYWORDS)

    # Fix: short affirmation with history
    is_affirmation = (
        len(t.split()) <= 3
        and has_history
        and any(t == kw or t.startswith(kw + " ") for kw in _AFFIRMATION_KEYWORDS)
    )
    if is_affirmation:
        is_followup = True

    # Routing decision (mirrors main.py logic)
    if is_combo:
        mode = "combo"
        path = "early:combo"
    elif is_operational:
        mode = "operational"
        path = "early:operational"
    elif (is_script_early or is_followup) and has_history:
        mode = "coach"
        path = "early:script/followup"
    else:
        mode = "→ detect_intent (LLM)"
        path = "llm"

    return {
        "mode": mode, "path": path,
        "flags": {
            "script": is_script_early, "combo": is_combo,
            "operational": is_operational, "resistance": has_resistance,
            "followup": is_followup, "affirmation": is_affirmation,
        }
    }


# ── Test cases (same patterns as real session 2026-03-24) ──────────────────

TESTS = [
    # --- BLOCK A: queries that go through LLM detect_intent ---
    # (these are fine, just verifying they still reach the right mode)
    {
        "id": "A1", "desc": "Objection: Ellanse дорого",
        "text": "Эллансе дорого", "history": False,
        "expect": "→ detect_intent (LLM)",
        "note": "LLM should say coach. Was: coach (correct, but wrong sub-mode). Acceptable."
    },
    {
        "id": "A2", "desc": "Product comparison: Витаран vs конкуренти",
        "text": "Витаран хуже плинеста",  "history": False,
        "expect": "→ detect_intent (LLM)",
        "note": "Should route to coach via LLM."
    },
    # --- BLOCK B: SHORT AFFIRMATIONS (was broken) ---
    {
        "id": "B1", "desc": "Short: 'Хочу' after active session",
        "text": "Хочу", "history": True,
        "expect": "coach",
        "note": "FIXED: was → lost context, bot asked to clarify."
    },
    {
        "id": "B2", "desc": "Short: 'Да нужно' after active session",
        "text": "Да нужно", "history": True,
        "expect": "coach",
        "note": "FIXED: short affirmation with history."
    },
    {
        "id": "B3", "desc": "Short: 'Ещё' continuation",
        "text": "Ещё", "history": True,
        "expect": "coach",
        "note": "FIXED: single-word continuation."
    },
    {
        "id": "B4", "desc": "Short: 'Продовж' after coach answer",
        "text": "Продовж", "history": True,
        "expect": "coach",
        "note": "FIXED: Ukrainian affirmation."
    },
    {
        "id": "B5", "desc": "Short 'Хочу' WITHOUT history — should go to LLM",
        "text": "Хочу", "history": False,
        "expect": "→ detect_intent (LLM)",
        "note": "No context = cant assume coach."
    },
    # --- BLOCK C: SEMINAR + CLIENT RESISTANCE (was broken) ---
    {
        "id": "C1", "desc": "Seminar invite, client refuses (full phrase)",
        "text": "Звонок клиенту первый раз пригласить на семинар петаран, она не хочет идти",
        "history": True, "expect": "→ detect_intent (LLM)",
        "note": "FIXED: was operational. Now LLM gets it → should say coach."
    },
    {
        "id": "C2", "desc": "Seminar invite, client refuses (short)",
        "text": "Визит клиенту пригласить на семинар, не хочет",
        "history": True, "expect": "→ detect_intent (LLM)",
        "note": "FIXED: семінар keyword cleared by resistance."
    },
    {
        "id": "C3", "desc": "Seminar — admin/expense (no resistance) — should stay operational",
        "text": "Как оформить семинар командировочные",
        "history": False, "expect": "operational",
        "note": "Genuine operational query, no client resistance."
    },
    {
        "id": "C4", "desc": "Seminar — purely operational (travel docs)",
        "text": "документи для семінару відрядження",
        "history": False, "expect": "operational",
        "note": "Pure admin query — stays operational."
    },
    # --- BLOCK D: РАССКАЖИ ПРО + product (was broken) ---
    {
        "id": "D1", "desc": "'Расскажи про эссе' WITH active session",
        "text": "Расскажи про эссе", "history": True,
        "expect": "coach",
        "note": "FIXED: was → KB, found nothing. Now stays in coach via followup keyword."
    },
    {
        "id": "D2", "desc": "'Розкажи про Нерамис' with history",
        "text": "Розкажи про Нерамис", "history": True,
        "expect": "coach",
        "note": "FIXED: stays in coach session."
    },
    {
        "id": "D3", "desc": "'Що таке PCL' with history",
        "text": "Що таке PCL", "history": True,
        "expect": "coach",
        "note": "FIXED: in-session product question stays coach."
    },
    {
        "id": "D4", "desc": "'Расскажи про эссе' WITHOUT history",
        "text": "Расскажи про эссе", "history": False,
        "expect": "→ detect_intent (LLM)",
        "note": "No history = LLM decides. Should route to coach via improved prompt."
    },
    # --- BLOCK E: FOLLOW-UP KEYWORDS (should still work) ---
    {
        "id": "E1", "desc": "Follow-up: 'другие аргументы'",
        "text": "другие аргументы", "history": True,
        "expect": "coach",
        "note": "Regression: classic followup should still work."
    },
    {
        "id": "E2", "desc": "Follow-up: 'чем отличается'",
        "text": "Чем отличается от конкурентов", "history": True,
        "expect": "coach",
        "note": "Regression: competitor comparison in session."
    },
    {
        "id": "E3", "desc": "Combo query",
        "text": "какие протоколы комбо для Эллансе", "history": True,
        "expect": "combo",
        "note": "Regression: combo still wins over followup."
    },
]


def run_tests():
    passed = 0
    failed = 0
    print("\n" + "=" * 72)
    print("  ROUTING REGRESSION TEST v2 — after fixes 2026-03-24")
    print("=" * 72)

    results_by_block = {}
    for t in TESTS:
        block = t["id"][0]
        res = simulate_early_routing(t["text"], t["history"])
        ok = res["mode"] == t["expect"]
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        results_by_block.setdefault(block, []).append((t, res, ok))

    BLOCK_NAMES = {
        "A": "LLM-detect (baseline)",
        "B": "Short affirmations fix",
        "C": "Seminar + resistance fix",
        "D": "Розкажи про + product fix",
        "E": "Regression: existing followups",
    }

    for block, items in results_by_block.items():
        block_ok = all(ok for _, _, ok in items)
        block_icon = "OK" if block_ok else "!!"
        print(f"\n  [{block_icon}] Block {block}: {BLOCK_NAMES.get(block, '')}")
        print(f"  {'-' * 60}")
        for t, res, ok in items:
            icon = "[PASS]" if ok else "[FAIL]"
            print(f"  {icon} {t['id']}: {t['desc']}")
            if not ok:
                print(f"         Expected: {t['expect']}")
                print(f"         Got:      {res['mode']}  (path: {res['path']})")
                print(f"         Flags:    {res['flags']}")
            print(f"         Note: {t['note']}")

    print(f"\n{'=' * 72}")
    print(f"  RESULT: {passed}/{len(TESTS)} passed  |  {failed} failed")
    print(f"{'=' * 72}\n")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(run_tests())
