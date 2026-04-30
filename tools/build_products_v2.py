"""tools/build_products_v2.py — параллельна побудова products_openai_v2 + competitors_v2 з нуля.

Що робить (в ізольованих _v2 директоріях, не торкаючи production):
1. Скачує всі файли з coach folder (Google Drive)
2. Використовує smart_import для xlsx/pptx/docx → structured chunks (без re-split)
3. Для PDF/Google Docs/etc — старий шлях extract_text + 1200-char splitter
4. Записує об'єднаний документ-список як coach_openai_v2 (intermediate)
5. Викликає _split_coach_to_products_competitors_v2 → products_v2, competitors_v2
6. Друкує статистику для верифікації перед атомарним swap'ом

Запуск (з locked sync — БЕЗПЕЧНО):
    docker exec emet_bot_app python /app/tools/build_products_v2.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

import sync_manager as sm


COACH_V2 = "data/db_index_coach_openai_v2"
PRODUCTS_V2 = "data/db_index_products_openai_v2"
COMPETITORS_V2 = "data/db_index_competitors_openai_v2"


def build_coach_v2():
    """Step 1: Build coach_openai_v2 з Drive using smart_import."""
    print("=" * 70)
    print("STEP 1: Build coach_openai_v2 from Drive (with smart_import)")
    print("=" * 70)

    drive, _ = sm.get_services()
    coach_folder = sm.RAG_FOLDERS["coach_openai"]["folder_id"]
    files = sm.list_files_with_meta(drive, coach_folder)
    print(f"Drive files in coach folder: {len(files)}")

    bundle = sm._files_to_documents(drive, files, folder_label="coach")
    pre_chunked = bundle["pre_chunked"]
    raw_docs = bundle["raw"]
    print(f"  smart pre-chunked: {len(pre_chunked)}")
    print(f"  raw (will be split): {len(raw_docs)}")

    # Split raw_docs using same chunk_size as production
    cfg = sm.RAG_FOLDERS["coach_openai"]
    chunks = list(pre_chunked)
    if raw_docs:
        split_chunks = RecursiveCharacterTextSplitter(
            chunk_size=cfg["chunk_size"], chunk_overlap=cfg["overlap"]
        ).split_documents(raw_docs)
        chunks.extend(split_chunks)
        print(f"  split_chunks (from raw): {len(split_chunks)}")

    print(f"\nTotal chunks → {COACH_V2}: {len(chunks)}")

    # Type breakdown
    type_dist = Counter(d.metadata.get("chunk_type", "raw_split") for d in chunks)
    print(f"Chunk types: {dict(type_dist.most_common())}")

    # Build coach_v2 ChromaDB
    shutil.rmtree(COACH_V2, ignore_errors=True)
    emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=sm.OPENAI_KEY)
    sm._batch_to_chroma(chunks, emb, COACH_V2,
                         rate_limit_sleep=10, rate_limit_keywords=["429", "RateLimitError"])

    vdb = Chroma(persist_directory=COACH_V2, embedding_function=emb)
    cnt = vdb._collection.count()
    print(f"✅ coach_openai_v2 built: {cnt} chunks\n")
    return cnt


def split_coach_v2_to_products_competitors():
    """Step 2: Split coach_v2 → products_v2 + competitors_v2 (analog of _split_coach_to_products_competitors)."""
    print("=" * 70)
    print("STEP 2: Split coach_v2 → products_v2 + competitors_v2")
    print("=" * 70)

    emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=sm.OPENAI_KEY)
    src = Chroma(persist_directory=COACH_V2, embedding_function=emb)
    data = src._collection.get(limit=50000, include=["metadatas", "documents"])

    if not data["ids"]:
        print("  coach_v2 is empty, abort")
        return

    products, competitors = [], []
    for meta, content in zip(data["metadatas"], data["documents"]):
        src_name = (meta.get("source", "") or "").lower()
        is_comp = any(p in src_name for p in sm.COMP_PATTERNS)
        doc = Document(page_content=content, metadata=meta)
        (competitors if is_comp else products).append(doc)
    print(f"  by competitor pattern → products: {len(products)}, competitors: {len(competitors)}")

    # Add LMS topics to products
    try:
        import db as _db
        splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=300)
        topics = _db.query_dict(
            "SELECT t.title, t.content, c.title as ct "
            "FROM topics t JOIN courses c ON c.id=t.course_id "
            "WHERE t.content IS NOT NULL AND length(trim(t.content)) > 50 "
            "ORDER BY c.id, t.order_num"
        )
        lms_added = 0
        for t in topics:
            text = f"# {t['ct']}\n## {t['title']}\n\n{t['content']}"
            fn = f"[LMS] {t['ct']} -- {t['title']}"
            doc = Document(page_content=text, metadata={"source": fn, "url": "lms_course", "folder": "products"})
            split = splitter.split_documents([doc])
            products.extend(split)
            lms_added += len(split)
        print(f"  LMS topics added to products: {lms_added}")
    except Exception as e:
        print(f"  LMS add error: {e}")

    # Add manual product cards
    cards_dir = "data/manual_product_cards"
    if os.path.isdir(cards_dir):
        card_count = 0
        for filename in sorted(os.listdir(cards_dir)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(cards_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            meta = {"source": f"[KARTKA] {filename}", "url": "manual_card", "folder": "products"}
            if content.startswith("---\n"):
                end = content.find("\n---\n", 4)
                if end > 0:
                    yaml_block = content[4:end]
                    body = content[end + 5:]
                    for line in yaml_block.splitlines():
                        if ":" in line:
                            k, _, v = line.partition(":")
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k in ("product_canonical", "section", "product_name", "source", "product_subline"):
                                meta[k] = v
                    content = body
            doc = Document(page_content=content, metadata=meta)
            products.append(doc)
            card_count += 1
        print(f"  manual cards added: {card_count}")

    # Filter empty/heading-only — smart-aware (структурні чанки мають нижчий поріг)
    SMART_CHUNK_TYPES = {"xlsx_row", "pptx_slide", "docx_section"}

    def _has_real_content(d):
        text = d.page_content
        has_body = False
        for line in text.split("\n"):
            s = line.strip()
            if s and not s.startswith("#"):
                has_body = True
                break
        if not has_body:
            return False
        chunk_type = d.metadata.get("chunk_type", "") or ""
        url = d.metadata.get("url", "") or ""
        if chunk_type in SMART_CHUNK_TYPES or url == "manual_card":
            return len(text.strip()) >= 80
        return len(text.strip()) >= 250

    before_p, before_c = len(products), len(competitors)
    products = [d for d in products if _has_real_content(d)]
    competitors = [d for d in competitors if _has_real_content(d)]
    print(f"  filtered empty/heading-only: products {before_p}→{len(products)}, competitors {before_c}→{len(competitors)}")

    # Re-detect canonical + scope (single source of truth)
    from tools.product_detector import detect_product_canonical, detect_scope
    for d in products + competitors:
        src = d.metadata.get("source", "") or ""
        content = d.page_content or ""
        if not d.metadata.get("product_canonical"):
            canonical = detect_product_canonical(source_name=src, content=content)
            if canonical:
                d.metadata["product_canonical"] = canonical
        if not d.metadata.get("scope"):
            d.metadata["scope"] = detect_scope(source_name=src, content=content)

    # Stats
    prod_dist = Counter(d.metadata.get("product_canonical") or "(none)" for d in products)
    scope_dist = Counter(d.metadata.get("scope", "?") for d in products + competitors)
    chunk_type_dist = Counter(d.metadata.get("chunk_type", "(raw_split)") for d in products)
    print(f"\n  products by canonical:")
    for prod, n in prod_dist.most_common(20):
        print(f"    {prod}: {n}")
    print(f"  scope distribution: {dict(scope_dist.most_common())}")
    print(f"  chunk type distribution (products): {dict(chunk_type_dist.most_common())}")

    # Build products_v2 + competitors_v2
    print("\n" + "-" * 70)
    print("Embedding products_v2 + competitors_v2...")
    print("-" * 70)
    counts = {}
    for path, docs, label in [(PRODUCTS_V2, products, "products_v2"),
                                (COMPETITORS_V2, competitors, "competitors_v2")]:
        shutil.rmtree(path, ignore_errors=True)
        vdb = Chroma(persist_directory=path, embedding_function=emb)
        BATCH = 50
        for i in range(0, len(docs), BATCH):
            sm._batch_to_chroma_simple(docs[i:i + BATCH], emb, vdb)
        cnt = vdb._collection.count()
        counts[label] = cnt
        print(f"  ✅ {label}: {cnt} chunks")
    return counts


def main():
    t0 = time.time()
    coach_count = build_coach_v2()
    if coach_count == 0:
        print("❌ coach_v2 empty, aborting")
        return
    counts = split_coach_v2_to_products_competitors()
    elapsed = time.time() - t0
    print("\n" + "=" * 70)
    print(f"✅ DONE in {elapsed:.0f}s")
    print(f"   coach_v2:       {coach_count} chunks → {COACH_V2}")
    if counts:
        print(f"   products_v2:    {counts.get('products_v2', 0)} chunks → {PRODUCTS_V2}")
        print(f"   competitors_v2: {counts.get('competitors_v2', 0)} chunks → {COMPETITORS_V2}")
    print("=" * 70)


if __name__ == "__main__":
    main()
