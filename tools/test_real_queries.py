"""tools/test_real_queries.py — симулює реальні запити менеджера до бота end-to-end.

Для кожного питання прогонює:
1. classifier (intent + product detection)
2. get_context (RAG retrieval з мета-фільтрами + per-category routing)
3. assemble system prompt by mode
4. виклик OpenAI gpt-4o з реальним промптом + контекстом
5. логує: query, mode, intent, product, top sources, відповідь

Зберігає JSON для review. Запуск:
    docker exec emet_bot_app python /app/tools/test_real_queries.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from openai import OpenAI, AsyncOpenAI

import classifier as clf
import main as bot_main
from main import (
    get_context, MODEL_OPENAI_COACH, MODEL_OPENAI,
    INTENT_TO_COACH_SUBTYPE, _normalize_query,
)
from prompts import PROMPT_KB
from prompts_v2 import (
    PROMPT_COACH_BASE, PROMPT_COACH_INFO, PROMPT_COACH_SOS,
    PROMPT_COACH_SCRIPT, PROMPT_COACH_VISIT,
)


# ── Test cases — 40 запитів стратифіковано по 4 режимам ──

NO_MODE_QUERIES = [
    "В каком продукте есть ниацинамид",
    "ESSE при куперозі",
    "что подойдет для чувствительной кожи из эссе",
    "производитель экзокс",
    "какие фактеры роста в Vitaran Skin Healer",
    "склад Refining Cleanser",
    "як зберігати EXOXE",
    "Magnox для чого",
    "сертифікати Vitaran",
    "купероз який бренд краще",
]

COACH_QUERIES = [
    "Petaran vs Ellansé",
    "конкуренти петаран",
    "клієнт звик до Rejuran",
    "Витаран дорого",
    "Petaran не дає ліфтинг ефект",
    "Vitaran пече при введенні",
    "Яка тривалість Ellansé S",
    "склад Vitaran Whitening",
    "техніка введення IUSE Skinbooster",
    "Vitaran під час вагітності",
]

COMBO_QUERIES = [
    "комбо Petaran + Ellanse",
    "Vitaran і Neuramis разом",
    "Petaran і Exoxe в одну процедуру",
    "протокол постакне",
    "комбо для гіперпігментації",
    "Neuronox і Vitaran",
    "Ellansé і Neuramis",
    "Magnox і Vitaran",
    "комбо для контуру обличчя",
    "комбо для шиї",
]

SOS_QUERIES = [
    "лікар каже що Реджуран краще",
    "Эллансе занадто дорого",
    "Neuramis на сірому ринку дешевше",
    "Vitaran не має CE сертифікації",
    "не хочу міняти Juvederm на Neuramis",
    "Petaran дає фіброз",
    "Exoxe — небезпечні екзосоми з амніотичної рідини",
    "клієнт працює з Sculptra",
    "IUSE Hair неефективний",
    "лікар лояльний до Radiesse",
]

ALL_TESTS = (
    [{"mode": "kb", "query": q, "category": "no_mode"} for q in NO_MODE_QUERIES] +
    [{"mode": "coach", "query": q, "category": "coach"} for q in COACH_QUERIES] +
    [{"mode": "combo", "query": q, "category": "combo"} for q in COMBO_QUERIES] +
    [{"mode": "coach", "query": q, "category": "sos_objection"} for q in SOS_QUERIES]
)


# ── Prompt assembly (повторює логіку main.py minimally) ──

def assemble_system_prompt(mode: str, classifier_result: dict, product: str | None) -> str:
    """Збирає system prompt для заданого режиму + intent."""
    if mode == "kb":
        return PROMPT_KB

    if mode == "combo":
        # main.py використовує PROMPT_COACH_BASE + PROMPT_COMBO; для тесту достатньо BASE
        return PROMPT_COACH_BASE

    # coach mode
    intent = (classifier_result or {}).get("intent", "info_about_product")
    subtype = INTENT_TO_COACH_SUBTYPE.get(intent, "info")
    sub_map = {
        "feedback": PROMPT_COACH_INFO,
        "sos": PROMPT_COACH_SOS,
        "script": PROMPT_COACH_SCRIPT,
        "visit": PROMPT_COACH_VISIT,
        "info": PROMPT_COACH_INFO,
    }
    subprompt = sub_map.get(subtype, PROMPT_COACH_INFO)
    base = PROMPT_COACH_BASE
    pieces = [base, subprompt]
    if product:
        pieces.append(f"\n## ПОТОЧНИЙ ПРОДУКТ\nКористувач питає про: {product}\n")
    return "\n\n".join(pieces)


# ── Test runner ──

async def run_one(case: dict, oai_client, async_oai) -> dict:
    """Прогонює одне питання end-to-end."""
    query = case["query"]
    mode = case["mode"]
    t0 = time.time()

    # 1. Classifier (async)
    cr = {}
    intent = product = variant = comp = None
    confidence = 0
    try:
        cr = await clf.classify(async_oai, query, chat_history=[])
        if not isinstance(cr, dict):
            print(f"   classifier returned non-dict: type={type(cr)}, val={cr!r}")
            cr = {}
        intent = cr.get("intent", "") or ""
        product = cr.get("primary_product")
        variant = cr.get("product_variant")
        comp = cr.get("competitor")
        confidence = cr.get("confidence", 0)
    except Exception as e:
        print(f"   classifier exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    # Normalize product for RAG
    product_canonical_for_rag = None
    try:
        if product:
            product_canonical_for_rag = bot_main.normalize_product(product, variant)
    except Exception:
        product_canonical_for_rag = product

    has_comp = bool(comp)
    if not has_comp and intent == "info_comparison":
        has_comp = True
    if not has_comp and any(m in query.lower() for m in ["конкурент", "альтернатив", "аналог"]):
        has_comp = True

    comparison_target = []
    intent_for_rag = intent
    try:
        from dialog_state import _detect_comparison
        ct = _detect_comparison(query, product)
        if ct:
            comparison_target = list(ct)
    except Exception:
        pass

    # 2. RAG retrieval
    try:
        context, sources = get_context(
            query, mode=mode, provider="openai",
            has_competitor=has_comp,
            product_canonical=product_canonical_for_rag,
            rag_k_override=None,
            comparison_target=comparison_target,
            intent=intent_for_rag,
        )
        # Top sources for review
        top_srcs = []
        if isinstance(sources, dict):
            for ref_id, meta in list(sources.items())[:5]:
                top_srcs.append({
                    "ref": ref_id,
                    "source": meta.get("source", "")[:80],
                    "product_canonical": meta.get("product_canonical", ""),
                    "subline": meta.get("product_subline", ""),
                    "scope": meta.get("scope", ""),
                    "chunk_type": meta.get("chunk_type", ""),
                })
    except Exception as e:
        context = ""
        top_srcs = []
        sources = {}

    # 3. Assemble prompt
    system_prompt = assemble_system_prompt(mode, cr, product_canonical_for_rag)

    # 4. LLM call (gpt-4o)
    llm_user = query if not product_canonical_for_rag else f"[Продукт: {product_canonical_for_rag}]\n\nПИТАННЯ:\n{query}"
    full_user = f"{llm_user}\n\n=== КОНТЕКСТ ===\n{context}"
    answer = ""
    try:
        model_id = MODEL_OPENAI_COACH if mode in ("coach", "combo") else MODEL_OPENAI
        resp = oai_client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_user},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        answer = resp.choices[0].message.content
    except Exception as e:
        answer = f"[LLM ERROR] {e}"

    elapsed = time.time() - t0
    return {
        "category": case["category"],
        "mode": mode,
        "query": query,
        "classifier": {
            "intent": intent,
            "product": product,
            "variant": variant,
            "competitor": comp,
            "confidence": confidence,
        },
        "rag": {
            "product_canonical_for_rag": product_canonical_for_rag,
            "has_competitor": has_comp,
            "comparison_target": comparison_target,
            "context_chars": len(context),
            "top_sources": top_srcs,
        },
        "answer": answer,
        "elapsed_sec": round(elapsed, 1),
    }


async def main_async():
    output_path = os.environ.get("TEST_OUTPUT", "/app/data/test_real_queries.json")
    print(f"=== Running {len(ALL_TESTS)} test queries ===")
    oai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    async_oai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    results = []
    for i, case in enumerate(ALL_TESTS, 1):
        print(f"\n[{i}/{len(ALL_TESTS)}] {case['mode']:5} | {case['category']:14} | {case['query'][:60]}")
        try:
            r = await run_one(case, oai_client, async_oai)
            results.append(r)
            cls = r["classifier"]
            print(f"   intent={cls['intent']} product={cls['product']} (conf={cls['confidence']:.2f})")
            print(f"   ctx={r['rag']['context_chars']} chars | top sources={[s['source'][:30] for s in r['rag']['top_sources'][:3]]}")
            print(f"   answer (first 120ch): {(r['answer'] or '')[:120]}")
        except Exception as e:
            print(f"   ❌ FAIL: {e}")
            import traceback; traceback.print_exc()
            results.append({**case, "error": str(e)})

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n\n✅ Done. Saved {len(results)} results → {output_path}")


if __name__ == "__main__":
    asyncio.run(main_async())
