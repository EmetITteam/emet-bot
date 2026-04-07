"""
migrate_split_indices.py — Split coach_openai into products + competitors indices.
Run inside container: docker exec -it emet_bot_app python /app/migrate_split_indices.py
"""
import os, sys
sys.stdout = __import__('io').TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("ERROR: OPENAI_API_KEY not set")
    sys.exit(1)

SRC = "data/db_index_coach_openai"
DST_PRODUCTS    = "data/db_index_products_openai"
DST_COMPETITORS = "data/db_index_competitors_openai"

# Patterns that identify COMPETITOR documents
COMPETITOR_PATTERNS = ["competitor", "_master", "competitors_"]

emb = OpenAIEmbeddings(model="text-embedding-3-small", openai_api_key=OPENAI_KEY)

print("Loading source index:", SRC)
src_vdb = Chroma(persist_directory=SRC, embedding_function=emb)
col = src_vdb._collection
data = col.get(limit=5000, include=["metadatas", "documents"])

print(f"Total chunks in source: {len(data['ids'])}")

products_docs = []
competitors_docs = []

for cid, meta, content in zip(data["ids"], data["metadatas"], data["documents"]):
    source_name = meta.get("source", "").lower()
    is_competitor = any(p in source_name for p in COMPETITOR_PATTERNS)

    doc = Document(page_content=content, metadata=meta)

    if is_competitor:
        competitors_docs.append(doc)
    else:
        products_docs.append(doc)

print(f"\nClassification:")
print(f"  Products:    {len(products_docs)} chunks")
print(f"  Competitors: {len(competitors_docs)} chunks")
print()

# Build products index
if products_docs:
    print(f"Building {DST_PRODUCTS} ({len(products_docs)} chunks)...")
    # Remove old index if exists
    if os.path.exists(DST_PRODUCTS):
        import shutil
        shutil.rmtree(DST_PRODUCTS)
    vdb_p = Chroma(persist_directory=DST_PRODUCTS, embedding_function=emb)
    # Batch add to avoid rate limits
    BATCH = 50
    for i in range(0, len(products_docs), BATCH):
        batch = products_docs[i:i+BATCH]
        vdb_p.add_documents(batch)
        print(f"  Added {min(i+BATCH, len(products_docs))}/{len(products_docs)}")
    print(f"  Done: {vdb_p._collection.count()} chunks")
    print()

# Build competitors index
if competitors_docs:
    print(f"Building {DST_COMPETITORS} ({len(competitors_docs)} chunks)...")
    if os.path.exists(DST_COMPETITORS):
        import shutil
        shutil.rmtree(DST_COMPETITORS)
    vdb_c = Chroma(persist_directory=DST_COMPETITORS, embedding_function=emb)
    for i in range(0, len(competitors_docs), BATCH):
        batch = competitors_docs[i:i+BATCH]
        vdb_c.add_documents(batch)
        print(f"  Added {min(i+BATCH, len(competitors_docs))}/{len(competitors_docs)}")
    print(f"  Done: {vdb_c._collection.count()} chunks")
    print()

print("=" * 50)
print("MIGRATION COMPLETE")
print(f"  Products:    {DST_PRODUCTS} = {len(products_docs)} chunks")
print(f"  Competitors: {DST_COMPETITORS} = {len(competitors_docs)} chunks")
print(f"  Legacy:      {SRC} = {len(data['ids'])} chunks (kept as fallback)")
print("=" * 50)
