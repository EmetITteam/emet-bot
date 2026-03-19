import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

"""
Routing Unit Tests — перевірка логіки визначення режиму бота.

Запуск:
    python tests/test_routing.py

Що перевіряє:
    - Combo keywords: чи правильно детектуються запити на комбо-протоколи
    - Script keywords: чи правильно детектуються запити на діалог/скрипт
    - Negative cases: чи НЕ спрацьовують keywords на нерелевантних запитах

Коли запускати:
    Після будь-яких змін в main.py (нові keywords, зміна логіки routing).
    НЕ потребує API-ключів, не витрачає токени.
"""

import sys

# --------------------------------------------------------------------------
# Копія констант з main.py (тримаємо в синхронізації вручну при змінах)
# --------------------------------------------------------------------------
COMBO_KEYWORDS = [
    "комбо", "комбін", "combo", "поєднати", "поєднання",
    "сочетать", "сочетание", "совместить",
    "протокол для", "протоколи для", "протоколы для",
    "які протоколи", "какие протоколы",
]

SCRIPT_KEYWORDS = [
    "дай диалог", "дай діалог", "дай скрипт", "скрипт з лікарем",
    "діалог з лікарем", "диалог с врачом", "розіграй діалог",
    "зіграй діалог", "покажи діалог", "покажи диалог",
]


def is_combo(text: str) -> bool:
    t = text.lower().strip()
    return any(kw in t for kw in COMBO_KEYWORDS)


def is_script(text: str) -> bool:
    t = text.lower().strip()
    return any(kw in t for kw in SCRIPT_KEYWORDS)


# --------------------------------------------------------------------------
# Тест-кейси
# --------------------------------------------------------------------------
COMBO_SHOULD_MATCH = [
    ("З чим комбінують Vitaran?",                       "комбін — пряме слово"),
    ("Комбо протоколи для постакне",                    "комбо — пряме слово"),
    ("combo protocols for skin",                        "combo EN"),
    ("як поєднати Petaran з Vitaran?",                  "поєднати"),
    ("поєднання препаратів при птозі",                  "поєднання"),
    ("сочетать Ellanse с биоревитализацией",            "сочетать RU"),
    ("какие протоколы для лифтинга?",                   "какие протоколы"),
    ("які протоколи для омолодження?",                  "які протоколи"),
    ("протоколи для покращення якості шкіри",           "протоколи для"),
    ("протоколы для коррекции носогубок",               "протоколы для"),
]

COMBO_SHOULD_NOT_MATCH = [
    ("Розкажи про Vitaran",                             "загальне питання про препарат"),
    ("Що входить до складу Petaran?",                   "склад препарату"),
    ("Клієнт каже що дорого",                           "заперечення"),
    ("Як оформити відпустку?",                          "HR питання"),
    ("Дай скрипт з лікарем",                            "скрипт — не комбо"),
    ("Навіщо потрібні сертифікати?",                    "сертифікати"),
    ("Які показання у Neuramis?",                       "показання — не комбо"),
]

SCRIPT_SHOULD_MATCH = [
    ("Дай діалог з лікарем про Vitaran",                "дай діалог"),
    ("дай скрипт продажу",                              "дай скрипт"),
    ("покажи диалог с врачом",                          "покажи диалог RU"),
    ("розіграй діалог — лікар каже дорого",             "розіграй діалог"),
]

SCRIPT_SHOULD_NOT_MATCH = [
    ("Розкажи про Vitaran",                             "загальне питання"),
    ("З чим комбінують Petaran?",                       "комбо питання"),
    ("Як оформити лікарняний?",                         "HR питання"),
]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def run_group(label, cases, fn, expected):
    passed = failed = 0
    failures = []
    for text, desc in cases:
        result = fn(text)
        ok = (result == expected)
        if ok:
            passed += 1
        else:
            failed += 1
            got = "MATCH" if result else "NO MATCH"
            exp = "MATCH" if expected else "NO MATCH"
            failures.append(f"    ❌  «{text}»\n       ({desc})\n       очікувалось {exp}, отримано {got}")
    status = "✅" if failed == 0 else "❌"
    print(f"  {status} {label}: {passed}/{passed+failed}")
    for f in failures:
        print(f)
    return failed


def run():
    print(f"\n{'='*60}")
    print("Routing Unit Tests")
    print(f"{'='*60}\n")

    total_failed = 0

    print("[ COMBO KEYWORDS ]")
    total_failed += run_group("Мають спрацювати   ", COMBO_SHOULD_MATCH,     is_combo, True)
    total_failed += run_group("НЕ мають спрацювати", COMBO_SHOULD_NOT_MATCH, is_combo, False)

    print("\n[ SCRIPT KEYWORDS ]")
    total_failed += run_group("Мають спрацювати   ", SCRIPT_SHOULD_MATCH,    is_script, True)
    total_failed += run_group("НЕ мають спрацювати", SCRIPT_SHOULD_NOT_MATCH, is_script, False)

    total = (len(COMBO_SHOULD_MATCH) + len(COMBO_SHOULD_NOT_MATCH) +
             len(SCRIPT_SHOULD_MATCH) + len(SCRIPT_SHOULD_NOT_MATCH))
    passed = total - total_failed

    print(f"\n{'='*60}")
    print(f"Результат: {passed}/{total} тестів пройшло")
    if total_failed == 0:
        print("✅ ВСІ ТЕСТИ ПРОЙШЛИ")
    else:
        print(f"❌ {total_failed} тестів не пройшло — перевір keywords в main.py")
    print(f"{'='*60}\n")

    sys.exit(0 if total_failed == 0 else 1)


if __name__ == "__main__":
    run()
