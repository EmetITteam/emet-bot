"""tools/dry_run_smart_import.py — preview structured import без перебудови ChromaDB.

Що робить:
1. Бере список файлів з coach folder (Google Drive)
2. Для кожного xlsx/pptx/docx — викликає smart_extract_documents()
3. Друкує: chunk count, перший chunk preview, розподіл product_canonical/subline
4. Не торкається ChromaDB

Запуск (на сервері в контейнері):
    docker exec emet_bot_app python /app/tools/dry_run_smart_import.py
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import Counter
from sync_manager import (
    get_services,
    list_files_with_meta,
    _download_bytes,
    RAG_FOLDERS,
    _SMART_MIMES,
    _SMART_EXTS,
)
from tools.smart_import import smart_extract_documents


def main():
    coach_folder = RAG_FOLDERS["coach_openai"]["folder_id"]
    drive, _ = get_services()
    files = list_files_with_meta(drive, coach_folder)
    print(f"=== DRY-RUN PREVIEW: smart_import on coach folder ({coach_folder}) ===")
    print(f"Total files: {len(files)}\n")

    smart_files = [
        f for f in files
        if f.get("mimeType") in _SMART_MIMES or f["name"].lower().endswith(_SMART_EXTS)
    ]
    other_files = [f for f in files if f not in smart_files]
    print(f"Smart-extractable (xlsx/pptx/docx): {len(smart_files)}")
    print(f"Other (PDF/Google docs/etc): {len(other_files)}")

    if other_files:
        print("\nOther files (raw extract path, не міняється):")
        for f in other_files[:20]:
            print(f"  - {f['name']} ({f.get('mimeType', '?')[:40]})")

    total_chunks = 0
    canonical_dist = Counter()
    subline_dist = Counter()
    chunk_type_dist = Counter()
    per_file_stats = []

    print("\n" + "=" * 70)
    print("SMART IMPORT PER-FILE BREAKDOWN")
    print("=" * 70)

    for i, f in enumerate(smart_files, 1):
        name = f["name"]
        mime = f["mimeType"]
        try:
            buf = _download_bytes(drive, f["id"])
            file_bytes = buf.getvalue()
            docs = smart_extract_documents(file_bytes, name, mime)
        except Exception as e:
            print(f"\n[{i}/{len(smart_files)}] ❌ {name}: {e}")
            continue

        n = len(docs)
        total_chunks += n
        per_file_stats.append((name, n))
        for d in docs:
            canonical_dist[d.metadata.get("product_canonical") or "(none)"] += 1
            subline_dist[d.metadata.get("product_subline") or "(none)"] += 1
            chunk_type_dist[d.metadata.get("chunk_type") or "(none)"] += 1

        print(f"\n[{i}/{len(smart_files)}] {name}")
        print(f"  chunks: {n}, type: {docs[0].metadata.get('chunk_type') if docs else '?'}")
        if docs:
            first = docs[0]
            print(f"  first chunk meta: canonical={first.metadata.get('product_canonical')!r}, subline={first.metadata.get('product_subline')!r}")
            preview = first.page_content[:300].replace("\n", " | ")
            print(f"  first chunk preview: {preview}...")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total smart files processed: {len(smart_files)}")
    print(f"Total chunks generated:      {total_chunks}")
    print(f"\nChunk types:")
    for ct, n in chunk_type_dist.most_common():
        print(f"  {ct}: {n}")
    print(f"\nProduct canonical distribution (top 20):")
    for prod, n in canonical_dist.most_common(20):
        print(f"  {prod}: {n}")
    print(f"\nProduct subline distribution:")
    for sub, n in subline_dist.most_common():
        print(f"  {sub}: {n}")
    print(f"\nTop 10 files by chunk count:")
    for name, n in sorted(per_file_stats, key=lambda x: -x[1])[:10]:
        print(f"  {n:4d}  {name}")


if __name__ == "__main__":
    main()
