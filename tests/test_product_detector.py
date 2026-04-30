"""tests/test_product_detector.py — sanity tests для product_detector.

Запуск:
    python /app/tests/test_product_detector.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.product_detector import detect_product_canonical, detect_scope, detect_subline


CANONICAL_CASES = [
    # (source_name, content_snippet, expected_canonical)
    # ── Filename priority ──
    ("Neuronox_Competitors_MASTER.docx", "порівняння з Neuramis ботокс", "Neuronox"),
    ("ЕХОХЕ.docx", "екзосоми мезенхімальних", "EXOXE"),
    ("_EXOXE_Competitors_MASTER.docx", "конкуренти", "EXOXE"),
    ("Neuramis_Competitors_MASTER.docx", "filler гіалуронова", "Neuramis"),
    ("Magnox_520_Competitors_MASTER.docx", "магній", "Magnox"),
    ("ВПВ Skin Booster (1).xlsx", "переваги", "IUSE SKINBOOSTER HA 20"),
    ("iuse_skinbooster_Competiors_Master.docx", "", "IUSE SKINBOOSTER HA 20"),
    ("iuse_collagen_Competitors_Master.docx", "", "IUSE Collagen"),
    ("IUSE_HAIR_Competitors_MASTER.docx", "", "IUSE HAIR REGROWTH"),
    ("ESSE_асортимент (роздріб).xlsx", "пробіотики", "ESSE"),
    ("ESSE_Competitors_MASTER.docx", "", "ESSE"),
    ("Petaran_Competitirs-MASTER.docx", "", "Petaran"),
    ("Ellanse__Competitors_MASTER.docx", "", "Ellansé"),
    ("VITARAN DUAL SERUM презентація.pptx", "", "Vitaran Skin Healer"),
    ("_HP CELL VITARAN Whitening 08_02_23.docx", "", "HP Cell Vitaran Whitening"),
    ("_HP CELL VITARAN Tox 08_02_23.docx", "", "HP Cell Vitaran Tox Eye"),
    ("HP Cell Vitaran.docx", "vitaran i 2*1 мл", "HP Cell Vitaran i"),
    # ── Content-only fallback ──
    ("Документ.docx", "ELLANSE — це біостимулятор", "Ellansé"),
    ("Слайд.pptx", "Petaran це філер", "Petaran"),
    # ── Negative ──
    ("readme.txt", "просто загальний текст", None),
]

SCOPE_CASES = [
    ("Комбіновані протоколи.docx", "схема процедур з Petaran", "protocol"),
    ("vitaran_combo_protokol.docx", "розведення", "protocol"),
    ("PDRN.docx", "пдрн молекула — це фрагменти", "ingredient"),
    ("ESSE_асортимент.xlsx", "лінійка esse core", "line"),
    ("Neuramis_Light.docx", "filler гіалуронова кислота 20 мг/мл", "product"),
]

SUBLINE_CASES = [
    ("Лінійка ЕSSE CORE", "продукти", "Esse Core"),  # Ukrainian Е!
    ("Лінійка ESSE SENSITIVE", "", "Esse Sensitive"),
    ("Лінійка ESSE PLUS", "", "Esse Plus"),
    ("Консилери", "", "Esse Concealers"),
    ("Сонцезахисні креми", "", "Esse Sun Protection"),
    ("Набори", "", "Esse Sets"),
    ("", "ESSE Professional treatment", "Esse Professional"),
]


def run():
    failures = []
    for src, content, expected in CANONICAL_CASES:
        got = detect_product_canonical(src, content)
        if got != expected:
            failures.append(f"  CANONICAL: {src!r} + {content!r} → got {got!r}, expected {expected!r}")
    for src, content, expected in SCOPE_CASES:
        got = detect_scope(src, content)
        if got != expected:
            failures.append(f"  SCOPE: {src!r} + {content!r} → got {got!r}, expected {expected!r}")
    for sheet, content, expected in SUBLINE_CASES:
        got = detect_subline(sheet, content)
        if got != expected:
            failures.append(f"  SUBLINE: {sheet!r} + {content!r} → got {got!r}, expected {expected!r}")

    total = len(CANONICAL_CASES) + len(SCOPE_CASES) + len(SUBLINE_CASES)
    if failures:
        print(f"FAIL: {len(failures)}/{total} cases")
        for f in failures:
            print(f)
        return 1
    print(f"OK: {total}/{total} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
