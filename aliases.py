"""aliases.py — alias dictionary і query expansion для UA↔EN транслітерацій.

Корінь проблеми: classifier і embedding не розпізнають українську транслітерацію
типу «Інтенсіті серум» як «Intensity Serum» / «Рефайнінг клінсер» як «Refining Cleanser».
Recall впадає до 30% для ESSE-запитів.

Цей модуль:
- ESSE_PRODUCTS — повний список 37 канонічних назв з RAG
- ALIAS_MAP — українські→англійські варіанти + поширені синоніми
- expand_query(query) — додає EN canonical форми до UA query перед embedding
- detect_products_in_text(text) — повертає список канонічних назв знайдених у тексті
- get_known_products_for_classifier() — flat список для classifier prompt
"""

# Канонічні назви всіх EMET-продуктів (ін'єкційні + космецевтика + нутрієнти)
# Використовується для detect_products_in_text — щоб бот зрозумів що це наш продукт
EMET_PRODUCTS = [
    # Ін'єкційні
    "Ellansé S", "Ellansé M", "Ellansé",
    "Petaran PLLA", "Petaran",
    "Neuramis", "Neuronox",
    "EXOXE",
    "HP Cell Vitaran i", "HP Cell Vitaran iII",
    "HP Cell Vitaran Whitening", "Vitaran Whitening",
    "HP Cell Vitaran Tox Eye", "Vitaran Tox Eye",
    "Vitaran",  # generic для будь-якого варіанту
    "IUSE Skinbooster HA20", "IUSE Skinbooster",
    # Космецевтика
    "Vitaran Skin Healer", "Vitaran Dual Serum",
    "Vitaran Azulene Serum", "Vitaran Sleeping Cream",
    # Нутрієнти
    "IUSE Collagen Marine Beauty", "IUSE Collagen",
    "IUSE Hair Regrowth", "IUSE HAIR",
    "Magnox 520", "Magnox",
]

# Повний асортимент ESSE з RAG (станом на 24.04.2026, 37 продуктів)
ESSE_PRODUCTS = [
    "Bakuchiol Serum", "Bakuchiol Serum R18",
    "Biome Mist", "Biome Mist T6",
    "Cocoa Exfoliator",
    "Cream Cleanser", "Cream Cleanser C8",
    "Cream Mask", "Cream Mask K6",
    "Deep Moisturiser", "Deep Moisturiser M6",
    "Esse Core", "Esse Plus", "Esse Professional", "Esse Sensitive",
    "Gel Cleanser C5",
    "Hand Cream", "Hand Cream B7",
    "Hyaluronic Serum R6",
    "Hydrating Mist", "Hydrating Mist T5",
    "Light Moisturiser", "Light Moisturiser M5",
    "Lip Conditioner B6", "Lip Cream R5",
    "Microderm Exfoliator", "Microderm Exfoliator E6",
    "Omega Deep Moisturiser", "Omega Light Moisturiser", "Omega Rich Moisturiser",
    "Refining Cleanser C6",
    "Repair Oil R7",
    "Resurrect Serum",
    "Rich Moisturiser", "Rich Moisturiser M7",
    "Ultra Moisturiser", "Ultra Moisturiser M8",
]

# UA→EN транслітерація: коли менеджер пише українською, додаємо EN варіант для embedding
# Ключ — lowercase нормалізований UA-фрагмент, значення — EN canonical (повна або часткова назва)
ALIAS_MAP = {
    # ESSE серуми
    "інтенсіті серум": "Intensity Serum Esse",
    "інтенсіті": "Intensity Serum Esse",
    "intensity серум": "Intensity Serum Esse",
    "пробіотик серум": "Probiotic Serum Esse",
    "пробіотичний серум": "Probiotic Serum Esse",
    "сенсітів серум": "Sensitive Serum Esse",
    "сенситив серум": "Sensitive Serum Esse",
    "бакучіол": "Bakuchiol Serum Esse",
    "ресуррект": "Resurrect Serum Esse",
    "ресуррект серум": "Resurrect Serum Esse",
    "гіалуронік серум": "Hyaluronic Serum R6 Esse",
    "гіалуроновий серум": "Hyaluronic Serum R6 Esse",
    # ESSE очищувачі
    "рефайнінг клінсер": "Refining Cleanser C6 Esse",
    "рефайнінг": "Refining Cleanser C6 Esse",
    "гель клінсер": "Gel Cleanser C5 Esse",
    "гель-клінсер": "Gel Cleanser C5 Esse",
    "крем клінсер": "Cream Cleanser C8 Esse",
    "крем-клінсер": "Cream Cleanser C8 Esse",
    # ESSE маски / ексфоліатори
    "клей маска": "Clay Mask Esse",
    "глиняна маска": "Clay Mask Esse",
    "крем маска": "Cream Mask K6 Esse",
    "крем-маска": "Cream Mask K6 Esse",
    "какао ексфоліатор": "Cocoa Exfoliator Esse",
    "мікродерм": "Microderm Exfoliator Esse",
    "мікродерм ексфоліатор": "Microderm Exfoliator E6 Esse",
    # ESSE зволожувачі
    "лайт мойсчур": "Light Moisturiser M5 Esse",
    "діп мойсчур": "Deep Moisturiser M6 Esse",
    "річ мойсчур": "Rich Moisturiser M7 Esse",
    "ультра мойсчур": "Ultra Moisturiser M8 Esse",
    "омега мойсчур": "Omega Moisturiser Esse",
    # ESSE міст / олії
    "байоме міст": "Biome Mist T6 Esse",
    "пробіотик міст": "Biome Mist T6 Esse",
    "гідрейтінг міст": "Hydrating Mist T5 Esse",
    "репейр оіл": "Repair Oil R7 Esse",
    "репер оіл": "Repair Oil R7 Esse",
    # ESSE губи / руки
    "ліп кондишн": "Lip Conditioner B6 Esse",
    "ліп крем": "Lip Cream R5 Esse",
    "хенд крем": "Hand Cream B7 Esse",
    # ESSE лінії
    "сенситів": "Esse Sensitive line",
    "сенситив": "Esse Sensitive line",
    "сенсітів": "Esse Sensitive line",
    "плас": "Esse Plus line",
    "професійна лінія": "Esse Professional line",
    "професіональна": "Esse Professional line",
    "кор": "Esse Core line",

    # Vitaran варіанти (UA транслітерація)
    "вайтенінг": "Vitaran Whitening",
    "вайтенинг": "Vitaran Whitening",
    "вайтнінг": "Vitaran Whitening",
    "тохтай": "Vitaran Tox Eye",
    "токс ай": "Vitaran Tox Eye",
    "токсай": "Vitaran Tox Eye",
    "вітаран ай": "Vitaran i",
    "вітаран ай ту": "Vitaran iII",
    "вітаран два": "Vitaran iII",
    "скін хілер": "Vitaran Skin Healer",
    "дуал серум": "Vitaran Dual Serum",
    "дуал-серум": "Vitaran Dual Serum",

    # Інші продукти
    "елансе": "Ellansé",
    "эллансе": "Ellansé",
    "петаран": "Petaran PLLA",
    "ексоксе": "EXOXE",
    "экзосом": "EXOXE",
    "нейрамис": "Neuramis",
    "нейраміс": "Neuramis",
    "нейронокс": "Neuronox",
    "скінбустер": "IUSE Skinbooster HA20",
    "скін бустер": "IUSE Skinbooster HA20",
    "хеар регроус": "IUSE Hair Regrowth",
    "колаген марін": "IUSE Collagen Marine Beauty",
    "магнокс": "Magnox 520",
}


def _normalize(text: str) -> str:
    """NFKC нормалізація + lowercase + видалення zero-width chars + collapse whitespace."""
    import unicodedata, re
    if not text:
        return ""
    # NFKC: канонічна декомпозиція (e + ̈ → ё) + lowercase
    text = unicodedata.normalize("NFKC", text)
    # Видалити zero-width / control chars (крім \n \t)
    text = "".join(c for c in text if not (c.isspace() and c not in (" ", "\n", "\t", "\r")) or not c.isspace() or c == " ")
    text = re.sub(r"[​-‏﻿]", "", text)
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def expand_query(query: str) -> str:
    """Додає EN canonical форми до query перед embedding.
    Приклад: «склад інтенсіті серум» → «склад інтенсіті серум Intensity Serum Esse»
    Це покращує recall для UA-only запитів про продукти з EN-only назвами в RAG."""
    if not query:
        return query
    norm = _normalize(query)
    additions = []
    for ua_alias, en_canonical in ALIAS_MAP.items():
        if ua_alias in norm and en_canonical.lower() not in norm:
            additions.append(en_canonical)
    if additions:
        # Додаємо унікальні в кінець, не дублюємо
        seen = set()
        unique = [a for a in additions if not (a in seen or seen.add(a))]
        return query + " " + " ".join(unique)
    return query


def _short_form(product_name: str) -> str:
    """Відрізає code-suffix (C6, M5, R18, T5...) щоб 'Refining Cleanser C6' → 'Refining Cleanser'."""
    import re
    return re.sub(r"\s+[A-Z]\d{1,3}\s*$", "", product_name).strip()


def detect_products_in_text(text: str) -> list[str]:
    """Повертає список канонічних назв продуктів знайдених у тексті.
    Перевіряє ESSE_PRODUCTS (full+short forms) + ALIAS_MAP. Useful для:
    - short-query handler (≤3 слова + product → coach)
    - smart KB→Coach fallback (raw query has product → fallback to products)"""
    if not text:
        return []
    norm = _normalize(text)
    found = []
    # EMET products — основний asortyment (Vitaran, Petaran, Ellansé, Neuramis тощо)
    for prod in EMET_PRODUCTS:
        if prod.lower() in norm and prod not in found:
            found.append(prod)
    # ESSE products — пробуємо повну і скорочену форми (без code suffix)
    for prod in ESSE_PRODUCTS:
        if prod.lower() in norm:
            if prod not in found:
                found.append(prod)
            continue
        short = _short_form(prod)
        if short != prod and short.lower() in norm and short not in found and prod not in found:
            found.append(prod)
    # ALIAS_MAP — UA-форми мапяться на EN canonical
    for ua, en in ALIAS_MAP.items():
        if ua in norm and en not in found:
            found.append(en)
    return found


def get_known_products_for_classifier() -> str:
    """Flat список продуктів для inject у classifier prompt.
    Допомагає classifier'у зрозуміти що «Refining cleanser» це наш ESSE-продукт,
    а не out_of_scope."""
    sections = []
    sections.append("ESSE асортимент (37 продуктів):")
    sections.append(", ".join(ESSE_PRODUCTS))
    sections.append("\nВсі ці назви → primary_product = ESSE\n")
    sections.append("Українська транслітерація → English canonical (для розпізнавання):")
    common_aliases = [
        "інтенсіті → Intensity Serum (ESSE)",
        "пробіотик серум → Probiotic Serum (ESSE)",
        "рефайнінг клінсер → Refining Cleanser (ESSE)",
        "сенситів → Esse Sensitive line",
        "вайтенінг → Vitaran Whitening",
        "тохтай / токс ай → Vitaran Tox Eye",
        "скін хілер → Vitaran Skin Healer",
        "дуал серум → Vitaran Dual Serum",
        "ексоксе → EXOXE",
        "магнокс → Magnox 520",
    ]
    sections.append("\n".join(f"  - {a}" for a in common_aliases))
    return "\n".join(sections)


# Quick self-test
if __name__ == "__main__":
    print("=== expand_query tests ===")
    for q in [
        "склад інтенсіті серум",
        "Refining cleanser для жирної шкіри",
        "як працює тохтай",
        "звичайний запит без продукту",
    ]:
        print(f"\nQ: {q}")
        print(f"→ {expand_query(q)}")
    print("\n=== detect_products_in_text tests ===")
    for q in [
        "Refining cleanser",
        "Інтенсіті серум",
        "Vitaran Whitening концентрація",
        "запит без жодного продукту",
    ]:
        print(f"\nQ: {q}")
        print(f"→ {detect_products_in_text(q)}")
