"""
test_knowledge_integrity.py — Knowledge base integrity check.

Verifies that ALL data from coach_openai is correctly distributed
into products_openai + competitors_openai with zero data loss.

Run after ANY index change:
  docker exec emet_bot_app python /app/tests/test_knowledge_integrity.py

Also runs automatically in sync_manager after each rebuild.
"""
import os, sys
# UTF-8 для Windows-терміналу. reconfigure безпечний при повторному виклику (на відміну від обгортки TextIOWrapper).
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from collections import Counter

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("ERROR: OPENAI_API_KEY not set")
    sys.exit(1)

COMP_PATTERNS = ["competitor", "competitir", "_master."]

# Baseline: verified brands and minimum expected chunks (from 2026-04-07 audit)
BRAND_CHECKS = {
    # (query, min_expected_in_context, must_contain_keywords)
    "Ellansé":           ("Тривалість Ellanse S та M?",        ["18", "24"]),
    "Exoxe":             ("Як зберігати Exoxe?",               ["кімнат"]),
    "Vitaran Whitening": ("Склад Vitaran Whitening",           ["глутатіон"]),
    "Neuramis":          ("Які є види Neuramis?",              ["Light", "Deep"]),
    "Neuronox":          ("Нейронокс зареєстрований?",         ["зареєстр"]),
    "Petaran":           ("Як розвести Петаран?",              ["флакон"]),
    "Pain Relief":       ("Pain Relief Vitaran технологія",    ["Pain Relief"]),
    "IUSE Hair":         ("Курс IUSE Hair?",                  ["процедур"]),
    "IUSE Collagen":     ("Як приймати IUSE Collagen?",        ["шот"]),
    "Skin Healer":       ("Лінійка Vitaran Skin Healer?",     ["Dual Serum"]),
    "Skinbooster":       ("Курс IUSE Skin Booster?",          ["3"]),
    "Tox Eye":           ("Склад Vitaran Tox Eye?",           ["PDRN"]),
    "ESSE":              ("Що таке лізати?",                   ["лізат"]),
    "Magnox":            ("Що таке Magnox 520?",               ["магній"]),
}

# Minimum chunk counts per index (after empty-chunk filter, 2026-04-14)
MIN_PRODUCTS = 580
MIN_COMPETITORS = 595
MIN_LMS = 120

# Файли в Coach що містять тільки заголовок без контенту — фільтр sync_manager їх відкидає
HEADING_ONLY_FILES = {
    "ELLANSE_ умови семинара.docx",
}


def run_integrity_check(verbose=True):
    """Run full integrity check. Returns (passed: bool, report: str)."""
    emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)
    errors = []
    warnings = []

    # ── 1. Check index sizes ─────────────────────────────────────────────
    indices = {}
    for name, path in [("coach", "data/db_index_coach_openai"),
                       ("products", "data/db_index_products_openai"),
                       ("competitors", "data/db_index_competitors_openai")]:
        if not os.path.exists(path):
            errors.append(f"Index {path} does not exist!")
            continue
        vdb = Chroma(persist_directory=path, embedding_function=emb)
        data = vdb._collection.get(limit=5000, include=["metadatas"])
        indices[name] = {
            "count": len(data["ids"]),
            "sources": Counter(m.get("source", "") for m in data["metadatas"]),
            "metadatas": data["metadatas"],
        }

    if "coach" not in indices:
        errors.append("Coach index missing — cannot verify")
        return False, "\n".join(errors)

    coach = indices["coach"]
    prod = indices.get("products", {"count": 0, "sources": Counter(), "metadatas": []})
    comp = indices.get("competitors", {"count": 0, "sources": Counter(), "metadatas": []})

    if verbose:
        print(f"Coach: {coach['count']} | Products: {prod['count']} | Competitors: {comp['count']}")

    # Check minimums
    if prod["count"] < MIN_PRODUCTS:
        errors.append(f"Products index too small: {prod['count']} < {MIN_PRODUCTS}")
    if comp["count"] < MIN_COMPETITORS:
        errors.append(f"Competitors index too small: {comp['count']} < {MIN_COMPETITORS}")

    # Check LMS
    lms_count = sum(1 for m in prod["metadatas"] if "[LMS]" in m.get("source", ""))
    if lms_count < MIN_LMS:
        errors.append(f"LMS chunks in products: {lms_count} < {MIN_LMS}")

    # ── 2. Check every coach source exists in products or competitors ────
    missing = []
    wrong_cat = []
    for src, cnt in coach["sources"].items():
        is_comp = any(p in src.lower() for p in COMP_PATTERNS)
        in_prod = prod["sources"].get(src, 0)
        in_comp = comp["sources"].get(src, 0)

        if not in_prod and not in_comp:
            # Файли що містять тільки заголовок (<80 chars контенту) фільтруються — це OK
            if src in HEADING_ONLY_FILES:
                continue
            missing.append(f"{src} ({cnt} chunks)")
        elif is_comp and in_prod > 0 and in_comp == 0:
            wrong_cat.append(f"{src} → should be COMP but in PROD")
        elif not is_comp and in_comp > 0 and in_prod == 0:
            wrong_cat.append(f"{src} → should be PROD but in COMP")

    if missing:
        errors.append(f"MISSING from both indices ({len(missing)}):")
        for m in missing:
            errors.append(f"  {m}")

    if wrong_cat:
        warnings.append(f"Wrong category ({len(wrong_cat)}):")
        for w in wrong_cat:
            warnings.append(f"  {w}")

    # ── 3. RAG search verification — check each brand returns data ───────
    if "products" in indices:
        vdb_p = Chroma(persist_directory="data/db_index_products_openai", embedding_function=emb)
        vdb_c = Chroma(persist_directory="data/db_index_competitors_openai", embedding_function=emb)

        rag_fails = []
        for brand, (query, keywords) in BRAND_CHECKS.items():
            docs_p = vdb_p.similarity_search(query, k=12)
            docs_c = vdb_c.similarity_search(query, k=8)
            all_text = " ".join(d.page_content for d in docs_p + docs_c).lower()

            missing_kw = [kw for kw in keywords if kw.lower() not in all_text]
            if missing_kw:
                rag_fails.append(f"{brand}: keywords {missing_kw} not found for query '{query}'")

        if rag_fails:
            errors.append(f"RAG search failures ({len(rag_fails)}):")
            for f in rag_fails:
                errors.append(f"  {f}")

    # ── 4. Build report ──────────────────────────────────────────────────
    lines = []
    lines.append("=" * 60)
    lines.append("KNOWLEDGE INTEGRITY CHECK")
    lines.append(f"Coach: {coach['count']} | Products: {prod['count']} (LMS: {lms_count}) | Competitors: {comp['count']}")
    lines.append(f"Sources: coach={len(coach['sources'])}, products={len(prod['sources'])}, competitors={len(comp['sources'])}")

    if errors:
        lines.append("")
        lines.append(f"ERRORS ({len(errors)}):")
        for e in errors:
            lines.append(f"  {e}")

    if warnings:
        lines.append("")
        lines.append(f"WARNINGS ({len(warnings)}):")
        for w in warnings:
            lines.append(f"  {w}")

    if not errors and not warnings:
        lines.append("")
        lines.append("RESULT: ALL OK — zero data loss, zero wrong categories, all brands verified")
    elif errors:
        lines.append("")
        lines.append(f"RESULT: FAILED — {len(errors)} errors")
    else:
        lines.append("")
        lines.append(f"RESULT: PASSED with {len(warnings)} warnings")

    lines.append("=" * 60)
    report = "\n".join(lines)

    if verbose:
        print(report)

    return len(errors) == 0, report


if __name__ == "__main__":
    passed, report = run_integrity_check()
    sys.exit(0 if passed else 1)
