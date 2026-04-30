"""tools/product_detector.py — єдина точка істини для product_canonical/scope detection.

Використовується трьома місцями:
- tools/smart_import.py (xlsx/pptx/docx structured extraction)
- sync_manager.py (_split_coach_to_products_competitors metadata enrichment)
- tools/build_products_v2.py (parallel index builder)

Ідея: filename має пріоритет над content (для Competitors_MASTER docs).
Канонічна назва = одна з 16 наших ліній продуктів. None якщо не визначено.
"""
from __future__ import annotations

# ============================================================
# product_canonical detector
# ============================================================

def detect_product_canonical(source_name: str = "", content: str = "") -> str | None:
    """Маппінг (source, content) → канонічна назва EMET-продукту.

    Filename має пріоритет — для competitor docs ("Neuronox_Competitors_MASTER.docx")
    атрибуція має йти по нашому продукту, навіть якщо в content порівнюють з конкурентом.

    Returns: canonical str або None.
    """
    src = (source_name or "").lower()
    text = (content or "").lower()

    # ── STEP 1: Strong filename signals ──
    if "neuronox" in src or "нейронокс" in src:
        return "Neuronox"
    if any(k in src for k in ["exoxe", "ехохе", "ексоксе", "_exoxe_", "exoxe_"]):
        return "EXOXE"
    if "neuramis" in src or "нейрамис" in src or "нейраміс" in src:
        return "Neuramis"
    if "magnox" in src or "магнокс" in src:
        return "Magnox"
    if any(k in src for k in ["whitening", "вайтенинг", "вайтенінг"]):
        return "HP Cell Vitaran Whitening"
    if any(k in src for k in ["tox eye", "тохтай", "токс ай", "vitaran tox", "_tox_", "vitaran_tox", "tox&face"]):
        return "HP Cell Vitaran Tox Eye"
    if any(k in src for k in ["skin healer", "скін хілер", "dual serum", "vitaran exosome",
                               "azulene", "sleeping cream", "wrapping serum"]):
        return "Vitaran Skin Healer"
    if any(k in src for k in ["petaran", "петаран"]):
        return "Petaran"
    if any(k in src for k in ["ellans", "ellanse", "елансе"]):
        return "Ellansé"
    if "iuse hair" in src or "iuse_hair" in src:
        return "IUSE HAIR REGROWTH"
    if "iuse collagen" in src or "iuse_collagen" in src:
        return "IUSE Collagen"
    if "iuse skin" in src or "skinbooster" in src or "skin booster" in src or "iuse_sb" in src or "впв skin" in src:
        return "IUSE SKINBOOSTER HA 20"
    if "esse" in src or "ессе" in src:
        return "ESSE"
    if any(k in src for k in ["vitaran iii", "vitaran_iii", "vitaran ii", "vitaran_ii"]):
        return "HP Cell Vitaran i"
    if any(k in src for k in ["vitaran i ", "vitaran_i", "hp cell vitaran"]):
        return "HP Cell Vitaran i"

    # ── STEP 2: Content matching (для файлів без явних маркерів в назві) ──
    combined = src + " " + text[:500]
    if any(k in combined for k in ["whitening", "вайтенинг", "вайтенінг"]):
        return "HP Cell Vitaran Whitening"
    if any(k in combined for k in ["tox eye", "тохтай", "токс ай"]):
        return "HP Cell Vitaran Tox Eye"
    if any(k in combined for k in ["skin healer", "vitaran exosome", "dual serum",
                                    "azulene", "sleeping cream", "wrapping serum"]):
        return "Vitaran Skin Healer"
    if any(k in combined for k in ["vitaran iii", "vitaran_iii", "vitaran ii", "vitaran_ii"]):
        return "HP Cell Vitaran i"
    if any(k in combined for k in ["vitaran i ", "vitaran i\n", "vitaran_i", "hp cell vitaran"]):
        return "HP Cell Vitaran i"
    if "vitaran" in combined or "вітаран" in combined or "витаран" in combined:
        return "Vitaran"
    if any(k in combined for k in ["ellans", "елансе", "ellanse"]):
        return "Ellansé"
    if any(k in combined for k in ["petaran", "петаран", "poly plla", "полі-l-молочна"]):
        return "Petaran"
    if any(k in combined for k in ["exoxe", "ехохе", "ексоксе", "экзосом"]):
        return "EXOXE"
    if "neuronox" in combined or "нейронокс" in combined:
        return "Neuronox"
    if "neuramis" in combined or "нейрамис" in combined or "нейраміс" in combined:
        return "Neuramis"
    if "iuse skin" in combined or "скінбустер" in combined or "skinbooster" in combined or "skin booster" in combined:
        return "IUSE SKINBOOSTER HA 20"
    if "iuse hair" in combined:
        return "IUSE HAIR REGROWTH"
    if "iuse collagen" in combined:
        return "IUSE Collagen"
    if "esse" in combined or "ессе" in combined:
        return "ESSE"
    if "magnox" in combined or "магнокс" in combined:
        return "Magnox"
    return None


# ============================================================
# scope detector (line / product / ingredient / protocol)
# ============================================================

def detect_scope(source_name: str = "", content: str = "") -> str:
    """Визначає рівень специфічності чанка."""
    src = (source_name or "").lower()
    text = (content or "").lower()

    # protocol — за source або тексту
    if any(k in src for k in ["комбін", "протокол", "combo", "protokol"]):
        return "protocol"
    if any(k in text[:200] for k in ["протокол", "розведення", "схема процедур", "техніка"]):
        return "protocol"

    # ingredient — пояснення про компонент окремо
    # Skip ingredient classification якщо source filename вже містить продукт.
    src_has_product = any(p in src for p in ["petaran", "петаран", "ellans", "елансе", "vitaran",
                                                "вітаран", "neuramis", "нейрамис", "iuse", "exoxe",
                                                "neuronox", "magnox", "esse"])
    if not src_has_product:
        if any(k in text[:300] for k in [" plla ", "поліl-молочна", "поликапролак", "пдрн", " pdrn ",
                                           " pcl ", "поликапролактон", "гіалуронова кислот", "hyaluronic"]):
            if not any(p in text[:100] for p in ["petaran", "петаран", "ellans", "елансе", "vitaran",
                                                    "вітаран", "neuramis", "нейрамис", "iuse"]):
                return "ingredient"

    # line — згадка >=2 продуктів того самого бренду / lineup keywords
    line_markers = [
        ("esse", ["sensitive", "sensitive plus", "core", "professional", "лінійка esse", "лінія esse",
                  "пробіотична космецевтика", "лінійки", "асортимент"]),
        ("vitaran", ["лінійка vitaran", "лінія vitaran", "all variants", "усі варіанти"]),
        ("iuse", ["лінійка iuse", "лінія iuse", "skinbooster і hair", "колаген і hair"]),
    ]
    for brand, markers in line_markers:
        if brand in text[:400] and any(m in text[:400] for m in markers):
            return "line"

    return "product"


# ============================================================
# product_subline (для ESSE)
# ============================================================

def detect_subline_from_query(query: str) -> str:
    """Для ESSE-запитів — визначає підлінію по ключових словах у питанні менеджера.

    Призначена для retrieval-time filtering: якщо запит явно про куперозну/sensitive
    лінію, можна звузити пошук до chunks з відповідним product_subline.

    Returns: канонічна назва підлінії або "" якщо не визначено.
    """
    q = (query or "").lower()
    # Replace cyrillic e/Е → ASCII (для consistency з detect_subline)
    q = q.replace("е", "e").replace("Е", "E")

    # Sensitive markers
    if any(k in q for k in ["sensitive", "сeнсітів", "сeнситив", "купeроз", "розацeа", "чутлив", "атопічн", "атопич"]):
        return "Esse Sensitive"
    # Sun protection
    if any(k in q for k in ["spf", "сонцeзахис", "сонцезахис", "foundation", "тональн"]):
        return "Esse Sun Protection"
    # Concealers
    if any(k in q for k in ["консилeр", "consealer", "concealer", "коректор"]):
        return "Esse Concealers"
    # Plus
    if any(k in q for k in ["esse plus", "esse+", "плюс лінія", "anti-aging", "антиейдж", "антивікового"]):
        return "Esse Plus"
    # Sets / kits
    if any(k in q for k in ["набор esse", "набір esse", "kit esse"]):
        return "Esse Sets"
    # Professional
    if "профeсійн" in q or "professional" in q or "профeсіонал" in q:
        return "Esse Professional"
    # Acne / oily / cleansing → Core (default for ESSE if none of above matched)
    return ""


def detect_subline(sheet_name: str = "", content: str = "") -> str:
    """Для ESSE — визначає лінію (Core/Sensitive/Plus/тощо).

    Note: cyrillic Е/е (U+0415/U+0435) часто міксується з ASCII E/e в назвах
    ('Лінійка ЕSSE CORE'). Нормалізуємо: cyrillic→ASCII перед matching.
    """
    combined = (sheet_name + " " + content[:300]).lower()
    # cyrillic e/Е → ASCII e/E (e.g. "еsse" → "esse")
    combined = combined.replace("е", "e").replace("Е", "E")

    if "sensitive" in combined or "сeнсітів" in combined or "сeнситив" in combined:
        return "Esse Sensitive"
    if "esse plus" in combined or "esse+" in combined or "лінійка esse plus" in combined:
        return "Esse Plus"
    if "professional" in combined or "профeсійн" in combined:
        return "Esse Professional"
    if "esse core" in combined or "лінійка esse core" in combined:
        return "Esse Core"
    if "консилeр" in combined or "concealer" in combined:
        return "Esse Concealers"
    if "сонцeзахис" in combined or "foundation" in combined or "тональн" in combined:
        return "Esse Sun Protection"
    if "набор" in combined or "набір" in combined or "set" in combined:
        return "Esse Sets"
    return ""
