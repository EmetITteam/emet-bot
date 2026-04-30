"""tools/smart_import.py — структурні імпортери для xlsx / pptx / docx.

Замінює "сирий текст → split 1200 chars" на handlers що поважають структуру файлу:
- xlsx: рядок = chunk (картка), не CSV-склейки
- pptx: слайд = chunk (заголовок + bullets разом)
- docx: split по H1/H2 заголовках, fallback на 1200 chars

Кожен chunk отримує rich metadata:
- source: ім'я файлу
- chunk_type: 'xlsx_row' | 'pptx_slide' | 'docx_section' | 'raw'
- product_canonical: спроба визначити продукт
- product_subline: для ESSE — лінія (Core/Sensitive/Plus)

Використання — імпортуй в sync_manager:
    from tools.smart_import import smart_extract_documents
    docs = smart_extract_documents(file_path, file_bytes, mime_type)
"""
from __future__ import annotations

import io
import re
from typing import Iterator
from langchain_core.documents import Document


# ============================================================
# XLSX — кожний рядок = одна картка
# ============================================================

def extract_xlsx_rows(file_bytes: bytes, source_name: str) -> Iterator[Document]:
    """xlsx → один Document на рядок. Зберігає всі колонки як structured text."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if ws.max_row < 2:
            continue
        # Headers — рядок 1, нормалізовані
        headers = []
        for c in range(1, ws.max_column + 1):
            h = ws.cell(row=1, column=c).value
            headers.append(str(h).strip() if h else f"col{c}")
        # Data rows
        for r in range(2, ws.max_row + 1):
            row_data = {}
            for c, h in enumerate(headers, 1):
                v = ws.cell(row=r, column=c).value
                if v is not None and str(v).strip():
                    row_data[h] = str(v).strip()
            if not row_data:
                continue
            # Перший непорожній field = "name" продукту (часто Найменування / Назва)
            name_keys = ["Найменування", "Назва", "Name", "Product", "Продукт"]
            name = next((row_data.get(k) for k in name_keys if k in row_data), None)
            if not name:
                # Беремо першу непорожню колонку
                name = next(iter(row_data.values()))
            # Формуємо markdown
            md_lines = [f"# {name[:120]}"]
            md_lines.append(f"_(з документа: {source_name}, аркуш: {sheet_name})_\n")
            for k, v in row_data.items():
                if k in name_keys:
                    continue
                md_lines.append(f"**{k}:** {v}")
            content = "\n".join(md_lines)
            # Detect product_canonical (source_name has priority over content)
            product_canonical = _detect_product_canonical_from_text(name + " " + content, source_name)
            product_subline = _detect_subline(sheet_name, content)
            yield Document(
                page_content=content,
                metadata={
                    "source": source_name,
                    "sheet": sheet_name,
                    "row": r,
                    "chunk_type": "xlsx_row",
                    "product_canonical": product_canonical,
                    "product_subline": product_subline,
                    "url": "xlsx_structured",
                    "folder": "products",
                }
            )


# ============================================================
# PPTX — кожний слайд = chunk (заголовок + bullets)
# ============================================================

def extract_pptx_slides(file_bytes: bytes, source_name: str) -> Iterator[Document]:
    """pptx → один Document на слайд. Зберігає заголовок + усі text-frames."""
    from pptx import Presentation
    prs = Presentation(io.BytesIO(file_bytes))
    for slide_num, slide in enumerate(prs.slides, 1):
        # Збираємо всі тексти зі слайду
        title = ""
        body_texts = []
        for shape in slide.shapes:
            if not hasattr(shape, "text") or not shape.text.strip():
                continue
            text = shape.text.strip()
            # Heuristic: title placeholder = найбільший шрифт або перший
            if shape.has_text_frame and (
                getattr(shape, "is_placeholder", False) and
                getattr(shape.placeholder_format, "idx", -1) == 0
            ):
                title = text
            else:
                body_texts.append(text)
        if not title and body_texts:
            title = body_texts.pop(0).split("\n")[0][:100]
        if not title and not body_texts:
            continue
        # Формуємо markdown
        md = f"# Слайд {slide_num}: {title or '(без заголовка)'}\n\n"
        md += f"_(з презентації: {source_name})_\n\n"
        for txt in body_texts:
            # Зберігаємо буллети — кожен рядок як bullet
            for line in txt.split("\n"):
                line = line.strip()
                if line:
                    md += f"- {line}\n"
        full_text = title + " " + " ".join(body_texts)
        yield Document(
            page_content=md,
            metadata={
                "source": source_name,
                "slide": slide_num,
                "chunk_type": "pptx_slide",
                "product_canonical": _detect_product_canonical_from_text(source_name + " " + full_text, source_name),
                "product_subline": _detect_subline("", full_text),
                "url": "pptx_structured",
                "folder": "products",
            }
        )


# ============================================================
# DOCX — split по H1/H2 заголовках, fallback на 1200 chars
# ============================================================

_HEADING_RE = re.compile(r"heading\s*[1-6]", re.IGNORECASE)
_STYLE_ID_RE = re.compile(r"^heading[1-6]$", re.IGNORECASE)


def _is_heading_style(para) -> bool:
    """Detects H1-H6 / Title across en/uk/ru locales.

    Word docx files authored in Ukrainian/Russian Word use localized style names
    ('Заголовок 1', 'Заголовок 2'); style.style_id stays locale-independent
    ('Heading1', 'Heading2'). Also accepts 'Title' / 'Назва' / 'Название'.
    """
    if not para.style:
        return False
    name = (para.style.name or "").lower()
    sid = (getattr(para.style, "style_id", "") or "").lower()
    if _HEADING_RE.search(name):
        return True
    if _STYLE_ID_RE.match(sid):
        return True
    if "title" in name or "назв" in name or "заголов" in name:
        return True
    return False


def _is_visual_heading(para, text: str) -> bool:
    """Fallback heuristic: many EMET docx files don't use heading styles, just bold/all-caps.

    Treat as heading if: short (<100 chars), bold OR mostly uppercase (>60% letters upper),
    doesn't end with sentence punctuation, has at least 1 letter.
    """
    if len(text) > 100 or len(text) < 2:
        return False
    if text[-1] in ".,;:?!»":
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    # All-caps check (>60% upper among letters)
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio >= 0.6:
        return True
    # Bold check: ALL runs in para are bold (not just first)
    try:
        runs = list(para.runs)
        if runs and all(r.bold for r in runs if r.text.strip()):
            return True
    except Exception:
        pass
    return False


def extract_docx_sections(file_bytes: bytes, source_name: str) -> Iterator[Document]:
    """docx → один Document на секцію (H1-H6/Title). Якщо немає заголовків — fallback на параграфи."""
    from docx import Document as DocxDocument
    doc = DocxDocument(io.BytesIO(file_bytes))
    sections = []  # list of (heading, list of paragraphs)
    current_heading = "Документ"
    current_paras = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if _is_heading_style(para) or _is_visual_heading(para, text):
            # Зберігаємо попередню секцію
            if current_paras:
                sections.append((current_heading, current_paras))
            current_heading = text[:120]
            current_paras = []
        else:
            current_paras.append(text)
    if current_paras:
        sections.append((current_heading, current_paras))
    # Якщо нема структури — fallback на 1200-char split
    if len(sections) <= 1 and current_paras:
        full_text = "\n".join(current_paras)
        # Розрізаємо по 1200 chars + 300 overlap (як було раніше)
        chunk_size = 1200
        overlap = 300
        for i in range(0, len(full_text), chunk_size - overlap):
            chunk_text = full_text[i:i + chunk_size]
            if not chunk_text.strip():
                continue
            yield Document(
                page_content=f"# {source_name}\n\n{chunk_text}",
                metadata={
                    "source": source_name,
                    "chunk_type": "docx_chunked",
                    "product_canonical": _detect_product_canonical_from_text(source_name + " " + chunk_text, source_name),
                    "product_subline": "",
                    "url": "docx_fallback",
                    "folder": "products",
                }
            )
        return
    # Нормальний шлях — секції за заголовками
    for heading, paras in sections:
        content_text = "\n\n".join(paras)
        if len(content_text.strip()) < 30:
            continue
        md = f"# {heading}\n_(з документа: {source_name})_\n\n{content_text}"
        yield Document(
            page_content=md,
            metadata={
                "source": source_name,
                "section": heading,
                "chunk_type": "docx_section",
                "product_canonical": _detect_product_canonical_from_text(source_name + " " + heading + " " + content_text, source_name),
                "product_subline": _detect_subline(heading, content_text),
                "url": "docx_structured",
                "folder": "products",
            }
        )


# ============================================================
# Detector helpers — re-export from product_detector for backward-compat
# (Single source of truth: tools/product_detector.py)
# ============================================================

from tools.product_detector import (
    detect_product_canonical as _shared_detect_canonical,
    detect_subline as _shared_detect_subline,
)


def _detect_product_canonical_from_text(text: str, source_name: str = "") -> str:
    """Backward-compat wrapper. Calls shared product_detector. Always returns str (not None)."""
    return _shared_detect_canonical(source_name=source_name, content=text) or ""


def _detect_subline(sheet_name: str, content: str) -> str:
    """Backward-compat wrapper. Calls shared product_detector."""
    return _shared_detect_subline(sheet_name=sheet_name, content=content)


# ============================================================
# Public dispatcher
# ============================================================

def smart_extract_documents(file_bytes: bytes, source_name: str, mime_type: str) -> list[Document]:
    """Dispatcher — викликає правильний extractor за mime/extension."""
    src_lower = source_name.lower()
    try:
        if mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or src_lower.endswith(".xlsx"):
            return list(extract_xlsx_rows(file_bytes, source_name))
        if mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation" or src_lower.endswith(".pptx"):
            return list(extract_pptx_slides(file_bytes, source_name))
        if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or src_lower.endswith(".docx"):
            return list(extract_docx_sections(file_bytes, source_name))
    except Exception as e:
        # Fail-soft: повертаємо пусто щоб старий fallback в sync_manager міг спробувати
        import logging
        logging.getLogger("emet_sync").warning("smart_extract failed for %s: %s", source_name, e)
        return []
    return []
