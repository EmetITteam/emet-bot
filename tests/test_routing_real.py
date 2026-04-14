"""
Тест роутингу на реальних запитах Александри + варіанти.
Перевіряє чи правильно спрацьовує детекція: заперечення / продукт / тип.
Запуск: python tests/test_routing_real.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Копіюємо keywords з main.py (щоб не тягнути весь імпорт)
_OBJECTION_KEYWORDS = [
    "дорого", "дорогой", "дорога", "дорогую", "дорогое", "дорогий", "дорогі", "дороге", "дорогуват",
    "цін", "ціна вис", "цена выс", "цена велик", "ціна велик",
    "не можу дозволити", "не можем позволить", "бюджет не", "не потягну", "не потяну",
    "задорого", "задороге", "занадто дорог", "слишком дорог", "дорогувато",
    "є дешевше", "есть дешевле", "дешевле", "дешевше", "можна дешевше", "можно дешевле",
    "не хоче", "не хочет", "не хочу купувати", "не хочет покупать",
    "не купує", "не покупает", "не бере", "не берет", "не замовля", "не заказыва",
    "відмовляєть", "відмовля", "отказывает", "отказыва", "відмов", "отказ",
    "не потрібно", "не нужно", "не треба", "не надо", "не актуально",
    "не цікаво", "не интересно", "не цікавить", "не интересует",
    "надає перевагу", "отдает предпочтение", "предпочитает", "віддає перевагу",
    "лояльн", "звик до", "привык к", "звикла до", "привыкла к",
    "вже працює з", "уже работает с", "вже використовує", "уже использует",
    "роками працює", "роками використовує", "роками использует", "годами работает", "годами использует",
    " роками", " годами",
    "довгий час", "давно працює", "давно использует", "давно купує", "давно покупает",
    "довіряє", "доверяет",
    "вже є постачальник", "уже есть поставщик",
    "моно препарат", "монопрепарат", "моно складу", "моно составу",
    "мульти склад", "мульти состав", "мультикомпонент", "многокомпонент",
    "багатокомпонент", "комплексн склад", "комплексный состав",
    "склад кращ", "состав лучше", "склад слабк", "состав слабк",
    "пече", "жжет", "болить", "больно", "болюч", "болезн",
    "неприємн", "неприятн", "дискомфорт",
    "побічк", "побочк", "побочн", "побічн", "побочные", "ускладненн", "осложнени",
    "набряк", "отек", "отёк", "почервонінн", "покрасн",
    "гематом", "синці", "синяки", "папул",
    "не вірю", "не верю", "сумнів", "сомнен", "сомнева",
    "не впевнен", "не уверен", "не знаю", "невпевнен",
    "підозр", "подозр", "странн", "дивн",
    "подумаю", "подумать", "подумае", "потім", "потом", "пізніше", "позже",
    "не зараз", "не сейчас", "пізніш", "позж",
    "небезпечн", "опасн", "ризик", "риск", "не сертиф", "не досліджен", "не исслед",
    "не бачу результат", "не вижу результат", "немає результат", "нет результат",
    "слабкий результат", "слабый результат", "нема ефекту", "нет эффекта",
    "не працює", "не работает", "не допомаг", "не помогает",
    "інші препарати даю", "другие препараты даю", "інші краще", "другие лучше",
    "більш виражен", "более выраж", "виражен ліфтинг", "выраж лифтинг",
    "сильний набряк", "сильный отек", "тривала реабілітаці", "длительная реабилитаци",
    "тривала постпроцедур", "длительная постпроцедур", "довга реабіліт", "долгая реабилит",
    "довго проходит", "долго проходит", "довго тримаєть", "долго держится",
    "інші препарати краще", "другие препараты лучше", "інші кращ", "другие лучш",
]

_EMET_PRODUCTS = [
    "ellansé", "ellanse", "elanse", "еланс", "елансе", "элансе", "эллансе",
    "neuramis", "нейрамис", "нейраміс",
    "vitaran skin", "вітаран скін", "скін хілер", "skin healer",
    "vitaran tox", "витаран токс", "вітаран токс", "tox eye", "токс ай",
    "vitaran whitening", "витаран вайтнінг", "вітаран вайтнінг", "вайтенінг", "вайтнінг",
    "hp cell", "хп сел", "hp сел",
    "vitaran", "вітаран", "витаран",
    "petaran", "петаран",
    "exoxe", "экзокс", "ексоксе",
    "esse", "эссе", "ессе",
    "iuse hair", "iuse хеір",
    "iuse skin", "iuse скінбустер", "скінбустер", "skinbooster",
    "iuse sb", "скін бустер", "скинбустер", "skin booster", "sb ha20",
    "iuse collagen", "iuse колаген",
    "iuse", "айюз", "июз",
    "neuronox", "нейронокс",
    "magnox", "магнокс",
]

_PRODUCT_CANONICAL = {
    "ellansé": "Ellansé", "ellanse": "Ellansé", "elanse": "Ellansé", "еланс": "Ellansé",
    "елансе": "Ellansé", "элансе": "Ellansé", "эллансе": "Ellansé",
    "neuramis": "Neuramis", "нейрамис": "Neuramis", "нейраміс": "Neuramis",
    "vitaran whitening": "HP Cell Vitaran Whitening", "вайтенінг": "HP Cell Vitaran Whitening",
    "вайтнінг": "HP Cell Vitaran Whitening",
    "hp cell": "HP Cell Vitaran",
    "vitaran": "Vitaran", "вітаран": "Vitaran", "витаран": "Vitaran",
    "petaran": "Petaran", "петаран": "Petaran",
    "exoxe": "EXOXE", "экзокс": "EXOXE", "ексоксе": "EXOXE",
    "esse": "ESSE", "эссе": "ESSE", "ессе": "ESSE",
    "iuse skin": "IUSE SKINBOOSTER HA 20",
    "скінбустер": "IUSE SKINBOOSTER HA 20", "skinbooster": "IUSE SKINBOOSTER HA 20",
    "iuse sb": "IUSE SKINBOOSTER HA 20", "скін бустер": "IUSE SKINBOOSTER HA 20",
    "скинбустер": "IUSE SKINBOOSTER HA 20", "skin booster": "IUSE SKINBOOSTER HA 20",
    "sb ha20": "IUSE SKINBOOSTER HA 20",
    "iuse hair": "IUSE HAIR REGROWTH",
    "iuse": "IUSE",
    "neuronox": "Neuronox", "magnox": "Magnox",
}


_SHORT_KEYS = {"esse", "эссе", "ессе", "iuse", "айюз", "sb"}

def _match_product(p, text):
    if p in _SHORT_KEYS:
        import re
        return bool(re.search(r'\b' + re.escape(p) + r'\b', text))
    return p in text


def detect(text):
    """Імітує routing з main.py."""
    t = text.lower().strip()
    has_objection = any(kw in t for kw in _OBJECTION_KEYWORDS)
    detected = next((p for p in _EMET_PRODUCTS if _match_product(p, t)), None)
    canonical = _PRODUCT_CANONICAL.get(detected, detected) if detected else None
    return {
        "objection": has_objection,
        "product": canonical,
        "expected_mode": "SOS" if has_objection else "INFO",
    }


# Реальні запити Александри + варіанти менеджерів
TEST_CASES = [
    # === Реальні з логів ===
    ("клієнт питає чому Ellanse такий дорогий колагеностимулятор", "SOS", "Ellansé"),
    ("косметолог не хоче купувати IUSE скін бустер тому що це моно препарат, надає перевагу препаратам с мулті складом", "SOS", "IUSE SKINBOOSTER HA 20"),
    ("косметолог не хоче купувати IUSE SB тому що це моно препарат, надає перевагу препаратам с мулті склад", "SOS", "IUSE SKINBOOSTER HA 20"),
    ("клієнт питає чому Vitaran вайтенінг сильно пече", "SOS", "HP Cell Vitaran Whitening"),
    ("Які переваги Ellansé для лікаря та пацієнта?", "INFO", "Ellansé"),
    ("дорого", "SOS", None),
    ("Vitaran I з Vitaran whitening", "INFO", "HP Cell Vitaran Whitening"),

    # === Варіанти "дорого" ===
    ("ellanse занадто дорого", "SOS", "Ellansé"),
    ("ціна на нейрамис висока", "SOS", "Neuramis"),
    ("задорого для моєї клініки", "SOS", None),
    ("не можу дозволити собі Ellanse", "SOS", "Ellansé"),
    ("бюджет не дозволяє", "SOS", None),
    ("є дешевше аналоги", "SOS", None),
    ("можно дешевле найти", "SOS", None),

    # === Варіанти відмови ===
    ("лікар не хоче брати Neuramis", "SOS", "Neuramis"),
    ("косметолог відмовляється від Vitaran", "SOS", "Vitaran"),
    ("не потрібно нам Ellanse", "SOS", "Ellansé"),
    ("не актуально для клініки", "SOS", None),
    ("лікарю не цікаво", "SOS", None),

    # === Лояльність до конкурента ===
    ("лікар працює з Ювідермом роками", "SOS", None),
    ("вже використовує Radiesse", "SOS", None),
    ("звикла до Sculptra, не хоче міняти", "SOS", None),
    ("надає перевагу Juvederm", "SOS", None),
    ("лояльна до Ресталайн", "SOS", None),

    # === Склад (моно/мульти) ===
    ("IUSE SB моно препарат, а хочу мульти склад", "SOS", "IUSE SKINBOOSTER HA 20"),
    ("у конкурента мультикомпонентний склад", "SOS", None),
    ("віддає перевагу багатокомпонентному", "SOS", None),

    # === Скарги / побічка ===
    ("після Vitaran сильно пече шкіру", "SOS", "Vitaran"),
    ("болить місце уколу після Ellanse", "SOS", "Ellansé"),
    ("у пацієнтки набряк після Neuramis", "SOS", "Neuramis"),
    ("почервоніння тримається 3 дні", "SOS", None),
    ("папули з'явились після HP Cell", "SOS", "HP Cell Vitaran"),
    ("гематома після ін'єкції", "SOS", None),
    ("пацієнт скаржиться на дискомфорт", "SOS", None),
    ("ускладнення після процедури", "SOS", None),

    # === Сумнів ===
    ("не вірю в ефективність Exoxe", "SOS", "EXOXE"),
    ("сумніваюсь в якості", "SOS", None),
    ("не впевнений що буде працювати", "SOS", None),

    # === Затягування ===
    ("лікар каже подумаю", "SOS", None),
    ("купить потім, не зараз", "SOS", None),
    ("пізніше розглянемо", "SOS", None),

    # === INFO запити (не повинні бути SOS) ===
    ("розкажи про Ellanse", "INFO", "Ellansé"),
    ("який склад Vitaran", "INFO", "Vitaran"),
    ("як зберігати Neuramis", "INFO", "Neuramis"),
    ("чим відрізняється Ellanse S від M", "INFO", "Ellansé"),
    ("Які переваги IUSE SB", "INFO", "IUSE SKINBOOSTER HA 20"),
    ("покажи протокол Exoxe", "INFO", "EXOXE"),

    # === Рекламації з реальних дизлайків Александри ===
    ("Petaran - не бачу результату, інші препарати дають більш виражений ліфтинг", "SOS", "Petaran"),
    ("у клієнтів тривала постпроцедурна реабілітація після процедури Petaran", "SOS", "Petaran"),
    ("Exoxe не працює як обіцяли", "SOS", "EXOXE"),
    ("нема ефекту від Neuramis", "SOS", "Neuramis"),
    ("інші препарати краще", "SOS", None),
]


def run_tests():
    passed = 0
    failed = []

    for i, (query, expected_mode, expected_product) in enumerate(TEST_CASES, 1):
        result = detect(query)
        mode_ok = result["expected_mode"] == expected_mode
        product_ok = result["product"] == expected_product
        ok = mode_ok and product_ok

        if ok:
            passed += 1
            status = "PASS"
        else:
            failed.append((query, expected_mode, expected_product, result))
            status = "FAIL"

        mode_mark = "OK" if mode_ok else f"WRONG (got {result['expected_mode']})"
        prod_mark = "OK" if product_ok else f"WRONG (got {result['product']})"
        print(f"[{i:2d}/{len(TEST_CASES)}] {status} | mode: {mode_mark} | product: {prod_mark}")
        print(f"     Q: {query[:80]}")
        if not ok:
            print(f"     Expected: mode={expected_mode}, product={expected_product}")
        print()

    print("=" * 80)
    print(f"RESULT: {passed}/{len(TEST_CASES)} passed")
    if failed:
        print(f"\nFAILURES ({len(failed)}):")
        for q, em, ep, r in failed:
            print(f"  - '{q[:70]}'")
            print(f"    expected mode={em} product={ep}")
            print(f"    got      mode={r['expected_mode']} product={r['product']}")


if __name__ == "__main__":
    run_tests()
