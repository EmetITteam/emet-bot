"""
Replay всіх 11 запитів Александри (дизлайки) з новим роутингом + промптами.
Запуск у Docker: docker exec emet_bot_app python /app/tests/test_alexandra_replay.py
"""
import asyncio
import os
import sys
import time
import re
sys.path.insert(0, "/app")

from openai import AsyncOpenAI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from prompts_v2 import (
    PROMPT_EXTRACT, PROMPT_COACH_BASE,
    PROMPT_COACH_SOS, PROMPT_COACH_EVALUATE, PROMPT_COACH_FEEDBACK,
    PROMPT_COACH_INFO, PROMPT_COACH_SCRIPT,
)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Keywords з main.py (копія) ---
_OBJECTION_KEYWORDS = [
    "дорого", "дорогой", "дорога", "дорогий", "дорогі", "дороге", "дорогуват",
    "цін", "не можу дозволити", "бюджет не", "задорого", "дешевле", "дешевше",
    "не хоче", "не хочет", "не купує", "відмовля", "отказ", "не потрібно", "не нужно",
    "не цікаво", "не интересно", "надає перевагу", "предпочитает",
    "вже працює з", "уже работает с", " роками", " годами",
    "моно препарат", "монопрепарат", "мульти склад", "мульти состав",
    "пече", "жжет", "болить", "больно", "побічк", "побочк", "ускладненн",
    "не вірю", "не верю", "сумнів", "сомнен", "не впевнен", "не уверен",
    "подумаю", "подумать", "потім", "потом", "пізніше",
    "не бачу результат", "не вижу результат", "нема ефекту",
    "не працює", "не работает", "слабкий результат",
    "інші препарати даю", "інші краще", "інші кращ", "виражен ліфтинг",
    "тривала реабілітаці", "тривала постпроцедур",
]

_TYPE_B_KEYWORDS = [
    "оціни мою відповідь", "я відповіла", "я сказала",
    "менеджер відповів", "менеджер відповіла", "менеджер сказав",
]

_TYPE_C_KEYWORDS = [
    "ти помилився", "неправильно", "не правильно", "маєте рацію",
    "виправлення", "не так", "переплутав",
]

def detect_type(t_lower):
    if any(kw in t_lower for kw in _TYPE_C_KEYWORDS):
        return "feedback"
    if re.search(r'\s(-\s*не|,\s*не|,\s*а\s+не)\s+\w', t_lower):
        return "feedback"
    if any(kw in t_lower for kw in _TYPE_B_KEYWORDS):
        return "evaluate"
    if any(kw in t_lower for kw in _OBJECTION_KEYWORDS):
        return "sos"
    return "info"

PROMPTS = {
    "sos": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_SOS,
    "evaluate": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_EVALUATE,
    "feedback": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_FEEDBACK,
    "info": PROMPT_COACH_BASE + "\n\n" + PROMPT_COACH_INFO,
}

# --- RAG через існуючу ChromaDB ---
emb = OpenAIEmbeddings(model="text-embedding-3-small", api_key=os.getenv("OPENAI_API_KEY"))
db_products = Chroma(persist_directory="/app/data/db_index_products_openai", embedding_function=emb)
db_comp = Chroma(persist_directory="/app/data/db_index_competitors_openai", embedding_function=emb)

def get_rag_context(query, has_competitor=False):
    try:
        docs_p = db_products.similarity_search(query, k=12)
        docs_c = db_comp.similarity_search(query, k=8) if has_competitor else []
        parts = []
        for d in docs_p:
            src = d.metadata.get("source", "?")
            parts.append(f"[📘 НАВЧАЛЬНИЙ КУРС EMET | {src}]\n{d.page_content}")
        for d in docs_c:
            src = d.metadata.get("source", "?")
            parts.append(f"[⚠️ КОНКУРЕНТ | {src}]\n{d.page_content}")
        return "\n\n".join(parts)
    except Exception as e:
        return f"[RAG ERROR: {e}]"


async def extract_facts(context, query):
    resp = await client.chat.completions.create(
        model="gpt-4o-mini", timeout=15, temperature=0.0, max_tokens=800,
        messages=[
            {"role": "system", "content": PROMPT_EXTRACT},
            {"role": "user", "content": f"КОНТЕКСТ:\n{context}\n\nЗАПИТ:\n{query}"}
        ]
    )
    return resp.choices[0].message.content.strip()


async def run_query(query, subtype, context, extracted=""):
    system_prompt = PROMPTS[subtype]
    ctx = f"ВИТЯГНУТІ ФАКТИ:\n{extracted}\n\n{context}" if extracted else context
    resp = await client.chat.completions.create(
        model="gpt-4o", timeout=30, temperature=0.3, max_tokens=1500,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"КОНТЕКСТ:\n{ctx}\n\nВОПРОС:\n{query}"}
        ]
    )
    text = resp.choices[0].message.content.strip()
    # ** → * як в production
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    return text


# --- 11 реальних дизлайків ---
QUERIES = [
    (611, "дорого"),
    (608, "дай повний протокол - скільки потрібно всього процедур?"),
    (606, "косметолог не хоче купувати IUSE скін бустер тому що це моно препарат, надає перевагу препаратам с мулті складом"),
    (605, "косметолог не хоче купувати IUSE SB тому що це моно препарат, надає перевагу препаратам с мулті складом"),
    (604, "клієнт питає чому Vitaran вайтенінг сильно пече"),
    (600, "у клієнтів тривала постпроцедурна реабілітація після процедури Petaran, на Sculptra немає такого"),
    (599, "Petaran - не бачу результату, інші препарати дають більш виражений ліфтинг"),
    (575, "фахівець не хоче працювати з IUSE skin booster, бо вважає що монокомпонентні препарати вже не в тренді. менеджер відповів що гіалуронова кислота це основа косметології і в деяких випадках є необхідним"),
    (574, "комунікація з косметологом: косметолог не хоче працювати з Neuramis, бо його багато на сірому ринку за більш низькою ціною"),
    (573, "інтервал Ellanse і Petaran 6 міс. - не 3 міс."),
    (570, "Petaran - не бачу результату, інші препарати дають більш виражений ліфтинг"),
]


async def main():
    print("=" * 80)
    print("REPLAY: 11 дизлайків Александри з новим роутингом")
    print("=" * 80)

    for log_id, query in QUERIES:
        print(f"\n{'═' * 80}")
        print(f"LOG #{log_id}")
        print(f"QUERY: {query}")

        t = query.lower().strip()
        subtype = detect_type(t)
        has_comp = any(c in t for c in ["sculptra", "скульптра", "juvederm", "ювідерм", "rejuran", "реджуран", "radiesse", "радіесс"])

        print(f"ROUTING: {subtype.upper()}  |  RAG competitors: {has_comp}")

        if subtype == "feedback":
            # Feedback не потребує RAG/extract
            answer = await run_query(query, "feedback", "", "")
        else:
            context = get_rag_context(query, has_comp)
            extracted = await extract_facts(context, query) if subtype in ("sos", "evaluate") else ""
            answer = await run_query(query, subtype, context, extracted)

        print(f"\nANSWER:")
        print(answer[:1200])
        print()


if __name__ == "__main__":
    asyncio.run(main())
