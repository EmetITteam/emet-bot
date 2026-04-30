"""tools/augment_cards_from_rag.py — доповнення порожніх полів карток з RAG.

ПРИНЦИП: НЕ вигадування. Тільки витяг з реальних chunks бази.
Кожне додане значення супроводжується тегом джерела (з якого файлу).

Запуск (на сервері в Docker):
    docker exec emet_bot_app python /app/tools/augment_cards_from_rag.py
    docker exec emet_bot_app python /app/tools/augment_cards_from_rag.py --product "Vitaran Dual"
    docker exec emet_bot_app python /app/tools/augment_cards_from_rag.py --dry-run

Локально для smoke-тесту нема сенсу — потрібен доступ до RAG в Docker.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from openai import AsyncOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings


CARDS_DIR = Path("/app/data/manual_product_cards")
PRODUCTS_INDEX = "/app/data/db_index_products_openai"
COACH_INDEX = "/app/data/db_index_coach_openai"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_KEY, timeout=60)
emb = OpenAIEmbeddings(model="text-embedding-3-small", api_key=OPENAI_KEY)
db_products = Chroma(persist_directory=PRODUCTS_INDEX, embedding_function=emb)
db_coach = Chroma(persist_directory=COACH_INDEX, embedding_function=emb)


# Поля які можуть бути доповнені.
# (field_key, markdown_marker, query_template, min_length_to_consider_complete)
# Якщо контент після маркера МЕНШЕ min_length — вважаємо "incomplete", шукаємо більше.
AUGMENTABLE_FIELDS = [
    ("composition", "## Склад", "повний склад всі активні речовини концентрації мг ppm % ніацинамід пантенол аденозин пдрн екзосоми", 80),
    ("indications", "## Показання", "показання застосування для якої шкіри проблеми", 50),
    ("contraindications_absolute", "Абсолютні:", "абсолютні протипоказання заборонено", 40),
    ("contraindications_relative", "Відносні:", "відносні протипоказання обережно", 30),
    ("pregnancy_lactation", "Вагітність / лактація:", "вагітність лактація грудне вигодовування", 10),
    ("zones_allowed", "✅ Дозволені зони:", "дозволені зони введення обличчя шия", 30),
    ("zones_forbidden", "⛔ ЗАБОРОНЕНІ зони:", "заборонені зони не вводити", 15),
    ("injection_depth", "Глибина введення:", "глибина введення шар дерми", 15),
    ("technique", "Техніка:", "техніка введення канюля голка ретроградно", 20),
    ("dosage", "Дозування за процедуру:", "дозування за процедуру об'єм мл", 15),
    ("course_count", "Кількість процедур у курсі:", "кількість процедур курс схема", 10),
    ("interval_repeat", "Інтервал між процедурами:", "інтервал між процедурами тижні місяці", 10),
    ("compatibility_emet", "З EMET-препаратами:", "сумісність комбо комбінація з препаратами EMET", 30),
    ("compatibility_devices", "З апаратами (HIFU/RF/лазер):", "сумісність HIFU RF лазер мікронідлінг", 30),
    ("recovery", "Down-time:", "реабілітація down-time відновлення", 15),
    ("post_procedure_care", "Догляд після процедури:", "догляд після процедури рекомендації", 30),
    ("side_effects_common", "Поширені побічні (норма):", "поширені побічні реакції набряк", 30),
    ("storage_temperature", "## Зберігання", "температура зберігання умови зберігання", 10),
    ("mechanism_of_action", "## Механізм дії", "механізм дії як працює на тканинному рівні", 100),
    ("duration_effect", "**Тривалість ефекту:**", "тривалість ефекту місяці результат", 5),
    ("onset", "**Коли видно результат:**", "коли видно результат початок дії", 10),
]


EXTRACT_PROMPT = """Ти експерт-аналітик. Витягни ТОЧНУ ІНФОРМАЦІЮ про конкретне поле продукту з контексту.

⛔ КРИТИЧНІ ПРАВИЛА:
1. Витягуй ТІЛЬКИ те що ЯВНО написано в КОНТЕКСТІ нижче.
2. ЗАБОРОНЕНО додавати інформацію якої там нема.
3. ЗАБОРОНЕНО узагальнювати з власних знань.
4. Якщо в контексті НЕМАЄ інформації про це поле для цього продукту — поверни порожній рядок "".
5. Цифри / концентрації / назви компонентів — ТОЧНО як в документі.
6. Якщо є — обов'язково вкажи з якого файлу (з контексту видно source).

Поверни JSON:
{
  "value": "витягнуте значення (або '' якщо нема)",
  "source": "назва файлу з якого взято (або '' якщо нема)"
}"""


def parse_card(content: str) -> dict:
    """Парсить .md картку → dict з YAML frontmatter + body."""
    if not content.startswith("---\n"):
        return {"frontmatter": {}, "body": content}
    end = content.find("\n---\n", 4)
    if end < 0:
        return {"frontmatter": {}, "body": content}
    yaml_block = content[4:end]
    body = content[end + 5:]
    fm = {}
    for line in yaml_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip("'\"")
    return {"frontmatter": fm, "body": body}


def find_incomplete_fields(body: str) -> list[tuple[str, str, int]]:
    """Повертає список (field_key, current_content, length) полів які потребують augmentation.
    Включає як ПОРОЖНІ так і НЕПОВНІ (контент менший за threshold)."""
    incomplete = []
    for key, marker, _query, min_len in AUGMENTABLE_FIELDS:
        if marker not in body:
            incomplete.append((key, "", 0))
            continue
        idx = body.find(marker)
        after = body[idx + len(marker):]
        next_break = re.search(r"\n##|\n\*\*[^*]+:", after)
        if next_break:
            content_after = after[:next_break.start()].strip()
        else:
            content_after = after.strip()
        if len(content_after) < min_len:
            incomplete.append((key, content_after, len(content_after)))
    return incomplete


def find_empty_fields(body: str) -> list[str]:
    """Backward compat — повертає тільки keys."""
    return [k for k, _, _ in find_incomplete_fields(body)]


def detect_product_type(product_name: str) -> str:
    """cosmetic / injection / nutrient — для умного фільтра chunks."""
    n = product_name.lower()
    # Космецевтика — сироватки, креми, маски для домашнього застосування
    cosmetic_markers = ["serum", "сироватка", "cream", "крем", "mask", "маска",
                        "cleanser", "клінсер", "moisturiser", "skin healer",
                        "dual serum", "sleeping", "azulene", "exosome", "ексосом",
                        "wrapping", "capsule", "esse"]
    if any(m in n for m in cosmetic_markers):
        return "cosmetic"
    # Нутрієнти — таблетки, шоти, порошки
    if any(m in n for m in ["collagen", "magnox", "hair regrowth", "коллаген", "магнокс"]):
        return "nutrient"
    # За замовчуванням — інʼєкційний (філери, ботокси, мезо)
    return "injection"


def chunk_matches_product_type(chunk_text: str, product_type: str) -> bool:
    """Відсікає chunks які явно НЕ підходять для продукту цього типу."""
    t = chunk_text.lower()
    # Маркери інʼєкційного контенту
    injection_markers = ["ін'єкц", "иньекц", "канюля", "голка", "введення",
                         "субдермально", "внутрішньошкірно", "процедуру", "курс процедур",
                         "інтервал між процедурами", "лікар-косметолог", "доктор вводить"]
    # Маркери космецевтичного / домашнього застосування
    cosmetic_markers = ["наносити на шкіру", "наносіть", "ранок", "ввечері",
                        "після очищення", "перед сном", "масажними рухами",
                        "рекомендується наносити", "сироватка з"]

    has_injection = any(m in t for m in injection_markers)
    has_cosmetic = any(m in t for m in cosmetic_markers)

    if product_type == "cosmetic":
        # Для космецевтики — відкидаємо явні chunks про інʼєкції (без cosmetic-markers)
        if has_injection and not has_cosmetic:
            return False
    elif product_type == "injection":
        # Для інʼєкційних — відкидаємо явно домашню косметику
        if has_cosmetic and not has_injection:
            return False
    return True


async def extract_field(product_name: str, product_canonical: str, field_key: str, query_template: str, current_content: str = "") -> dict:
    """Запит у RAG → LLM extract → {value, source}.
    Якщо current_content передано — LLM розуміє що треба ДОПОВНИТИ, не дублювати."""
    product_type = detect_product_type(product_name)
    # Будуємо query
    query = f"{product_name} {query_template}"
    # Search ширше — і products, і coach (де можуть бути PPTX, докси)
    docs_p = db_products.similarity_search(query, k=20)
    docs_c = db_coach.similarity_search(query, k=20)
    # Об'єднуємо, де можливо filter по продукту
    seen = set()
    relevant_chunks = []
    for d in docs_p + docs_c:
        cid = d.metadata.get("source", "?") + "::" + d.page_content[:80]
        if cid in seen:
            continue
        seen.add(cid)
        content_lower = d.page_content.lower()
        # Шукаємо КЛЮЧОВІ слова продукту (унікальні, не загальні бренд)
        # Для Vitaran Dual Serum — потрібно "dual" або "exosome", не просто "vitaran"
        product_keywords_specific = []
        n_low = product_name.lower()
        if "dual" in n_low: product_keywords_specific.append("dual")
        if "azulene" in n_low: product_keywords_specific.append("azulene")
        if "sleeping" in n_low: product_keywords_specific.append("sleeping")
        if "skin healer" in n_low: product_keywords_specific.append("skin healer")
        if "whitening" in n_low: product_keywords_specific.append("whitening")
        if "tox eye" in n_low: product_keywords_specific.append("tox")
        if "collagen" in n_low: product_keywords_specific.append("collagen")
        if "hair" in n_low: product_keywords_specific.append("hair")
        if "skinbooster" in n_low: product_keywords_specific.append("skinbooster")
        if "magnox" in n_low: product_keywords_specific.append("magnox")
        if "neuronox" in n_low: product_keywords_specific.append("neuronox")
        if "neuramis" in n_low: product_keywords_specific.append("neuramis")
        if "ellansé" in n_low or "ellanse" in n_low: product_keywords_specific.append("ellans")
        if "petaran" in n_low: product_keywords_specific.append("petaran")
        if "exoxe" in n_low: product_keywords_specific.append("exoxe")

        # Якщо є специфічні keywords — chunk МАЄ містити хоча б один
        if product_keywords_specific:
            if not any(kw in content_lower for kw in product_keywords_specific):
                continue
        else:
            # Fallback на загальні слова якщо специфічних нема
            product_words = re.findall(r"\w{4,}", product_name.lower())
            if not any(w in content_lower for w in product_words[:3]):
                continue

        # Type-aware фільтр: cosmetic картка не бере injection chunks і навпаки
        if not chunk_matches_product_type(d.page_content, product_type):
            continue

        src = d.metadata.get("source", "?")
        # Не використовуємо chunks з самих manual_cards (щоб не йти по колу)
        if src.startswith("[KARTKA]") or "manual_card" in src.lower():
            continue
        relevant_chunks.append({"source": src, "content": d.page_content[:600]})
        if len(relevant_chunks) >= 8:
            break
    if not relevant_chunks:
        return {"value": "", "source": ""}
    # Build context with explicit source labels
    context = "\n\n".join(
        f"[ДЖЕРЕЛО: {c['source']}]\n{c['content']}" for c in relevant_chunks
    )
    current_block = ""
    if current_content:
        current_block = (
            f"\n\nПОТОЧНЕ ЗНАЧЕННЯ ПОЛЯ (вже в картці, треба ДОПОВНИТИ а не дублювати):\n"
            f"{current_content[:500]}\n\n"
            f"⚠️ Поверни ПОВНЕ оновлене значення (поточне + знайдені доповнення з контексту).\n"
            f"⚠️ Якщо нічого нового не знайдено в контексті — поверни поточне значення без змін."
        )
    user_msg = (
        f"ПРОДУКТ: {product_name} (канонічний: {product_canonical})\n"
        f"ШУКАЮ ПОЛЕ: {field_key}{current_block}\n\n"
        f"КОНТЕКСТ:\n{context}"
    )
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            timeout=60,
            temperature=0.0,
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        return {
            "value": (data.get("value") or "").strip(),
            "source": (data.get("source") or "").strip(),
        }
    except Exception as e:
        return {"value": "", "source": "", "error": str(e)[:100]}


def insert_into_card(body: str, field_key: str, marker: str, value: str, source: str) -> str:
    """Додає value у картку після відповідного маркера, або додає новий блок."""
    if not value:
        return body
    # Формуємо рядок з джерелом
    augmented = f"{value} _(джерело: {source})_" if source else value
    if marker in body:
        # Маркер є але порожньо — підставимо значення після нього
        idx = body.find(marker)
        # Знайти кінець блоку (до наступного ## або **<>:** або до кінця)
        after_marker_pos = idx + len(marker)
        after = body[after_marker_pos:]
        next_break = re.search(r"\n##|\n\*\*[^*]+:", after)
        end_pos = after_marker_pos + (next_break.start() if next_break else len(after))
        # Заміна того що між маркером і кінцем блоку
        body = body[:after_marker_pos] + f" {augmented}\n" + body[end_pos:]
    else:
        # Маркер відсутній — додаємо в кінець (після останньої секції)
        if marker.startswith("##"):
            # Це heading — додаємо новим блоком
            body = body.rstrip() + f"\n\n{marker}\n{augmented}\n"
        else:
            # Це **bold:** маркер — додаємо як новий рядок
            body = body.rstrip() + f"\n\n{marker} {augmented}\n"
    return body


async def augment_card(card_path: Path, dry_run: bool = False, max_fields: int = 5) -> dict:
    """Augment одну картку. Повертає stats."""
    content = card_path.read_text(encoding="utf-8")
    parsed = parse_card(content)
    fm = parsed["frontmatter"]
    body = parsed["body"]
    product_name = fm.get("product_name") or card_path.stem
    product_canonical = fm.get("product_canonical", "")
    section = fm.get("section", "clinical")
    if section != "clinical":
        # Sales-картки не доповнюємо — це робота Sales Director
        return {"file": card_path.name, "skipped": "sales section"}
    incomplete = find_incomplete_fields(body)
    if not incomplete:
        return {"file": card_path.name, "skipped": "all fields complete"}
    print(f"\n📂 {card_path.name}: {len(incomplete)} неповних/порожніх полів", flush=True)
    augmented_count = 0
    additions = []
    # Sort: empty fields first (length 0), then partial (by length asc)
    incomplete.sort(key=lambda x: x[2])
    # Limit max_fields per card to control cost
    for field_key, current_content, current_len in incomplete[:max_fields]:
        marker_query = next(((m, q) for k, m, q, _ in AUGMENTABLE_FIELDS if k == field_key), None)
        if not marker_query:
            continue
        marker, query = marker_query
        result = await extract_field(product_name, product_canonical, field_key, query, current_content)
        new_value = result.get("value", "")
        # Skip if нове значення коротше або однакове з поточним (нічого не доповнили)
        if new_value and len(new_value) > current_len + 20:
            print(f"  ✅ {field_key} ({current_len}→{len(new_value)} chars) ← {new_value[:80]}...", flush=True)
            additions.append((field_key, marker, new_value, result.get("source", "")))
            augmented_count += 1
        elif new_value:
            print(f"  ↷ {field_key} — знайдене НЕ кращe ({current_len}→{len(new_value)})", flush=True)
        else:
            print(f"  ⏭ {field_key} — нема в базі", flush=True)
    # Apply additions
    if additions and not dry_run:
        for field_key, marker, value, source in additions:
            body = insert_into_card(body, field_key, marker, value, source)
        # Re-save
        new_content = "---\n"
        for k, v in fm.items():
            new_content += f"{k}: '{v}'\n"
        new_content += "---\n\n" + body
        card_path.write_text(new_content, encoding="utf-8")
    return {
        "file": card_path.name,
        "empty_fields": len(incomplete),
        "augmented": augmented_count,
        "additions": [{"field": f, "value": v[:60], "source": s} for f, _, v, s in additions],
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", help="Filter cards by product name substring (case insensitive)")
    parser.add_argument("--dry-run", action="store_true", help="Don't save, just preview")
    parser.add_argument("--max-fields", type=int, default=5, help="Max fields to augment per card")
    parser.add_argument("--output", default="/app/data/augment_report.json")
    args = parser.parse_args()

    cards = sorted(CARDS_DIR.glob("*__clinical.md"))
    if args.product:
        cards = [c for c in cards if args.product.lower() in c.name.lower()]
    print(f"📦 Знайдено {len(cards)} clinical карток для augmentation")
    if args.dry_run:
        print("⚠️ DRY RUN — нічого не зберігається")

    results = []
    for card in cards:
        try:
            r = await augment_card(card, dry_run=args.dry_run, max_fields=args.max_fields)
            results.append(r)
        except Exception as e:
            print(f"❌ {card.name}: {e}", flush=True)
            results.append({"file": card.name, "error": str(e)[:200]})

    # Summary
    total_augmented = sum(r.get("augmented", 0) for r in results)
    skipped = sum(1 for r in results if "skipped" in r)
    failed = sum(1 for r in results if "error" in r)
    print(f"\n{'='*60}")
    print(f"📊 ПІДСУМОК")
    print(f"  Карток оброблено: {len(results)}")
    print(f"  Полів додано:     {total_augmented}")
    print(f"  Пропущено:        {skipped}")
    print(f"  Помилок:          {failed}")
    print(f"{'='*60}")

    # Save report
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 Звіт: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
