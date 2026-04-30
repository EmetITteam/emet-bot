"""tests/rag_eval_runner.py — оффлайн RAG-evaluation для порівняння індексів.

Що робить:
1. Читає tests/rag_eval_queries.json (85 запитів стратифіковано)
2. Для кожного запиту — top-K retrieval з заданого ChromaDB
3. Метрики:
   - top1_product_match: чи перший chunk має expected_product
   - topK_product_recall: % chunks з expected_product
   - smart_chunk_pct: % chunks типу xlsx_row/pptx_slide/docx_section або manual_card
   - source_diversity: унікальні sources / K
   - keyword_recall: % expected_keywords знайдених у retrieved text
4. Зберігає JSON метрики (для compare before/after)

Запуск:
    python /app/tests/rag_eval_runner.py --index data/db_index_products_openai --output /tmp/metrics_before.json
    python /app/tests/rag_eval_runner.py --index data/db_index_products_openai_v2 --output /tmp/metrics_after.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings


SMART_CHUNK_TYPES = {"xlsx_row", "pptx_slide", "docx_section", "docx_chunked"}


def _smart_retrieve(vdb, query: str, k: int, case: dict) -> list:
    """Симулює get_context логіку: per-category routing + filters.

    - category=combo → пріоритет scope=protocol + general merge
    - category=comparison з 2 expected_products → balanced
    - category=esse + detected_subline → subline-narrowed
    - category=expected_product → product-locked
    - default → semantic search
    """
    category = case.get("category", "")
    expected = case.get("expected_product")

    # COMBO: scope=protocol filter
    if category == "combo":
        try:
            protocol_docs = vdb.similarity_search(query, k=k, filter={"scope": "protocol"})
        except Exception:
            protocol_docs = []
        general = vdb.similarity_search(query, k=k)
        seen, merged = set(), []
        for d in protocol_docs + general:
            key = (d.metadata.get("source", ""), d.page_content[:100])
            if key in seen:
                continue
            seen.add(key)
            merged.append(d)
        return merged[:k]

    # ESSE with subline: try subline-narrowed first
    if category == "esse" and expected == "ESSE":
        try:
            from tools.product_detector import detect_subline_from_query
            sub = detect_subline_from_query(query)
            if sub:
                sub_docs = vdb.similarity_search(query, k=k,
                    filter={"$and": [{"product_canonical": "ESSE"}, {"product_subline": sub}]})
                if len(sub_docs) >= max(3, k // 3):
                    if len(sub_docs) < k:
                        general = vdb.similarity_search(query, k=k - len(sub_docs),
                            filter={"product_canonical": "ESSE"})
                        seen = {(d.metadata.get("source", ""), d.page_content[:60]) for d in sub_docs}
                        for d in general:
                            key = (d.metadata.get("source", ""), d.page_content[:60])
                            if key not in seen:
                                sub_docs.append(d)
                                if len(sub_docs) >= k:
                                    break
                    return sub_docs[:k]
        except Exception:
            pass

    # Product-locked для конкретного expected продукту
    if expected:
        try:
            docs = vdb.similarity_search(query, k=k, filter={"product_canonical": expected})
            if len(docs) >= max(3, k // 4):
                return docs
        except Exception:
            pass

    # Default: semantic
    return vdb.similarity_search(query, k=k)


def evaluate_index(index_path: str, queries: list[dict], k: int = 16, smart_routing: bool = False) -> dict:
    if not os.path.isdir(index_path):
        raise FileNotFoundError(f"Index not found: {index_path}")

    emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=os.getenv("OPENAI_API_KEY"))
    vdb = Chroma(persist_directory=index_path, embedding_function=emb)
    total_chunks = vdb._collection.count()

    case_results = []

    # Per-category aggregators
    by_category = {}

    for case in queries:
        q = case["query"]
        expected = case.get("expected_product")
        keywords = case.get("expected_keywords", []) or []
        category = case.get("category", "other")

        # Retrieval — smart_routing симулює per-category get_context логіку
        if smart_routing:
            docs = _smart_retrieve(vdb, q, k, case)
        else:
            docs = vdb.similarity_search(q, k=k)

        # Metrics
        top1_match = 1 if (expected and docs and docs[0].metadata.get("product_canonical") == expected) else 0
        topK_match_count = sum(
            1 for d in docs
            if expected and d.metadata.get("product_canonical") == expected
        )
        topK_match_pct = topK_match_count / max(1, len(docs))

        smart_count = sum(
            1 for d in docs
            if d.metadata.get("chunk_type") in SMART_CHUNK_TYPES
            or d.metadata.get("url") == "manual_card"
            or d.metadata.get("url", "").startswith("xlsx_") or d.metadata.get("url", "").startswith("pptx_") or d.metadata.get("url", "").startswith("docx_")
        )
        smart_pct = smart_count / max(1, len(docs))

        manual_card_count = sum(1 for d in docs if d.metadata.get("url") == "manual_card")
        manual_pct = manual_card_count / max(1, len(docs))

        unique_sources = len({d.metadata.get("source", "") for d in docs})
        diversity = unique_sources / max(1, len(docs))

        # Keyword recall — % of expected keywords found in retrieved text (lowercase)
        if keywords:
            blob = " ".join(d.page_content.lower() for d in docs)
            kw_hits = sum(1 for kw in keywords if kw.lower() in blob)
            kw_recall = kw_hits / len(keywords)
        else:
            kw_recall = None  # no keywords → skip

        result = {
            "id": case["id"],
            "category": category,
            "query": q,
            "expected_product": expected,
            "top1_match": top1_match,
            "topK_match_pct": round(topK_match_pct, 3),
            "smart_pct": round(smart_pct, 3),
            "manual_pct": round(manual_pct, 3),
            "diversity": round(diversity, 3),
            "kw_recall": round(kw_recall, 3) if kw_recall is not None else None,
            "top1_chunk_canonical": docs[0].metadata.get("product_canonical") if docs else None,
            "top1_chunk_type": docs[0].metadata.get("chunk_type") if docs else None,
            "top1_source": docs[0].metadata.get("source", "")[:80] if docs else None,
        }
        case_results.append(result)

        by_category.setdefault(category, []).append(result)

    # Aggregate metrics
    def avg(field, items=None):
        items = items or case_results
        vals = [r[field] for r in items if r[field] is not None]
        return round(sum(vals) / max(1, len(vals)), 3)

    summary = {
        "index_path": index_path,
        "total_chunks_in_index": total_chunks,
        "queries_evaluated": len(case_results),
        "k": k,
        "metrics": {
            "top1_match_pct":   avg("top1_match"),
            "topK_match_pct":   avg("topK_match_pct"),
            "smart_pct":        avg("smart_pct"),
            "manual_pct":       avg("manual_pct"),
            "diversity":        avg("diversity"),
            "kw_recall":        avg("kw_recall"),
        },
        "by_category": {
            cat: {
                "n": len(items),
                "top1_match_pct":  avg("top1_match", items),
                "topK_match_pct":  avg("topK_match_pct", items),
                "smart_pct":       avg("smart_pct", items),
                "kw_recall":       avg("kw_recall", items),
            }
            for cat, items in by_category.items()
        },
        "cases": case_results,
    }
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True, help="Path to ChromaDB index")
    parser.add_argument("--queries", default="tests/rag_eval_queries.json")
    parser.add_argument("--output", required=True, help="Output JSON path for metrics")
    parser.add_argument("--k", type=int, default=16)
    parser.add_argument("--smart-routing", action="store_true",
                        help="Симулює per-category retrieval (combo→protocol filter, esse→subline, etc)")
    args = parser.parse_args()

    with open(args.queries, encoding="utf-8") as f:
        data = json.load(f)
    queries = data["cases"]

    print(f"Evaluating index: {args.index}")
    print(f"Queries: {len(queries)} (k={args.k})")
    print()

    summary = evaluate_index(args.index, queries, args.k, smart_routing=args.smart_routing)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    m = summary["metrics"]
    print(f"=== METRICS for {args.index} ===")
    print(f"Total chunks in index: {summary['total_chunks_in_index']}")
    print(f"Queries evaluated:     {summary['queries_evaluated']}")
    print(f"")
    print(f"top1_match_pct:  {m['top1_match_pct']:.1%}  (top-1 chunk has expected product)")
    print(f"topK_match_pct:  {m['topK_match_pct']:.1%}  (% of top-K with expected product)")
    print(f"smart_pct:       {m['smart_pct']:.1%}  (% of top-K from structured/manual extraction)")
    print(f"manual_pct:      {m['manual_pct']:.1%}  (% of top-K from manual_product_cards/)")
    print(f"diversity:       {m['diversity']:.1%}  (unique sources / K)")
    print(f"kw_recall:       {m['kw_recall']:.1%}  (% expected keywords in retrieved text)")
    print()
    print("By category:")
    for cat, c in sorted(summary["by_category"].items(), key=lambda x: -x[1]["n"]):
        print(f"  {cat:15} n={c['n']:2}  top1={c['top1_match_pct']:.1%}  topK={c['topK_match_pct']:.1%}  smart={c['smart_pct']:.1%}  kw={c['kw_recall']:.1%}")

    print(f"\nFull metrics saved → {args.output}")


if __name__ == "__main__":
    main()
