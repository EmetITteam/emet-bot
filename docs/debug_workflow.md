# 🐛 Debug Workflow — як дебажити FAIL'и в боті

**Створено:** 24.04.2026
**Контекст:** доказано на практиці — швидко знаходить корінь проблеми у 3-шаровій pipeline (alias detection → classifier → RAG → prompt → LLM)

---

## Коли використовувати

- FAIL у `tests/run_regression.py`
- Скарга менеджера у Telegram (бот не зрозумів запит)
- Нова знахідка у `quality_history.correction_rate` (зросла correction_rate)
- Запис у `knowledge_gaps` що повторюється

---

## 3-крокова процедура

### Крок 1 — Швидкий isolated run

Якщо це FAIL у regression — є готовий case:
```bash
docker exec emet_bot_app python /app/tests/run_regression.py --case <fail-id>
```

Покаже: classifier output + чи відповідь містить must_contain/must_not_contain.

Якщо це скарга з реального діалогу — переходь одразу до Кроку 2.

---

### Крок 2 — Debug 3 шарів (golden script)

Створити простий debug-скрипт і виконати в контейнері. **Цей шаблон я зберіг тут — використовувати завжди:**

```bash
ssh -p 33222 emet@49.12.81.83 "docker exec emet_bot_app python -c \"
import asyncio, os, sys
sys.path.insert(0, '/app')
os.chdir('/app')
from openai import AsyncOpenAI
from classifier import classify, normalize_product
from aliases import detect_products_in_text, map_to_canonical_brand, expand_query
client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Підстав свій запит сюди:
QUERIES = [
    'твій тестовий запит 1',
    'твій тестовий запит 2',
]

async def debug(q):
    print(f'\\n=== Q: {q} ===')
    detected = detect_products_in_text(q)
    expanded = expand_query(q)
    canonical = map_to_canonical_brand(detected[0]) if detected else None
    print(f'  ШАР 1 (detection):')
    print(f'    detect_products: {detected}')
    print(f'    canonical_brand: {canonical}')
    print(f'  ШАР 2 (query expansion):')
    print(f'    expanded_query:  {expanded}')
    cls = await classify(client, q)
    print(f'  ШАР 3 (classifier):')
    print(f'    intent={cls[\\\"intent\\\"]} product={cls.get(\\\"primary_product\\\")} variant={cls.get(\\\"product_variant\\\")} conf={cls.get(\\\"confidence\\\")}')

async def main():
    for q in QUERIES:
        await debug(q)
asyncio.run(main())
\""
```

**Що дивитись:**

| Шар | Що показує | Що означає якщо «погано» |
|---|---|---|
| **1. Detection** | `detect_products_in_text(q)` — чи бот розпізнав product name | `[]` для очевидного продукту → product/alias відсутній у `aliases.py` |
| **2. Query expansion** | Що додано до query перед embedding | Не додав canonical EN-форму → `ALIAS_MAP` не покриває цей варіант |
| **3. Classifier** | intent + product + confidence | `out_of_scope` з `conf=0.0` → hint не спрацював. `intent=info_about_product` але `product=None` → hint не extract'ив product |

---

### Крок 3 — Якщо потрібен **глибший** debug (RAG + LLM)

Якщо classifier OK але відповідь погана — додай RAG + LLM до debug script:

```python
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from prompts_v2 import PROMPT_COACH_BASE, PROMPT_COACH_INFO

emb = OpenAIEmbeddings(model='text-embedding-3-small', api_key=os.getenv('OPENAI_API_KEY'))
db = Chroma(persist_directory='/app/data/db_index_products_openai', embedding_function=emb)

async def debug_full(q):
    # ... шари 1-3 як вище ...
    # ШАР 4: RAG retrieval
    expanded = expand_query(q)
    docs = db.similarity_search(expanded, k=8, filter={'product_canonical': product} if product else None)
    print(f'  ШАР 4 (RAG): {len(docs)} chunks')
    for d in docs[:3]:
        print(f'    - [{d.metadata.get("scope","?")}] {d.metadata.get("source","?")[:50]}')
    # ШАР 5: prompt + LLM
    context = '\\n\\n'.join(f'{d.page_content[:300]}' for d in docs[:6])
    sys_prompt = PROMPT_COACH_BASE + PROMPT_COACH_INFO
    resp = await client.chat.completions.create(
        model='gpt-4o', timeout=30, temperature=0.0, max_tokens=400,
        messages=[{'role':'system','content':sys_prompt},
                  {'role':'user','content':f'КОНТЕКСТ:\\n{context}\\n\\nВОПРОС:\\n{q}'}]
    )
    print(f'  ШАР 5 (LLM):')
    print('  ' + resp.choices[0].message.content[:400])
```

---

## Decision matrix — куди фіксити

Після того як знайшов винний шар:

| Симптом | Винний шар | Куди фіксити |
|---|---|---|
| `detect_products = []` для очевидного product name | Detection | [aliases.py](../aliases.py) — додати в `EMET_PRODUCTS` / `ESSE_PRODUCTS` / `ALIAS_MAP` |
| `detect → ['X']` але classifier дає `product=None` | Hint не сприймається classifier'ом | [classifier.py](../classifier.py) — посилити hint директиву (явно: «ВИСТАВ примусово primary_product=X») |
| Classifier OK але RAG повертає не релевантні chunks | Embedding/retrieval | Подивитись `expand_query` output — чи додано EN canonical. Якщо ні — додати alias |
| RAG returns chunks але всі з `scope=line` | Scope filter не зробив свою роботу | [sync_manager.py](../sync_manager.py) `_detect_scope()` — переглянути heuristic для цього типу chunks |
| RAG OK, scope OK, але LLM відповідь генерична | Prompt | [prompts_v2.py](../prompts_v2.py) — переглянути правила `BASE` або subtype-prompt |
| LLM caps хороший але не слідує format | Prompt order/тон | Підсилити правило format compliance або перенести вище |
| Все OK але відповідь повільна (>10 sec) | LLM/network | Перевірити `failover_depth` у `logs` — чи не Gemini/Claude. Перевірити token count (`extract_facts` skip для verbatim) |

---

## Реальний приклад (з 24.04 сесії)

**Query:** `Refining cleanser` (одне слово, з #739 діалогу Ілони)

**Запуск debug:**
```python
detect_products: ['Refining Cleanser C6']  # ✅ працює
canonical_brand: ESSE                       # ✅ працює
expanded_query: Refining cleanser           # OK (нічого додавати не треба)
classifier: intent=out_of_scope conf=0.0    # ❌ ПРОБЛЕМА — classifier all-out
```

**Діагноз:** Classifier ігнорує hint для дуже коротких запитів, дає out_of_scope з confidence=0.0.

**Дія:** Conf-fallback (`Fix #7`) спрацьовує downstream — детектує продукт у raw query → override на `coach + info_about_product`. Бот в результаті дає повний опис Refining Cleanser.

**Висновок:** classifier-level fix складно, але **fallback layer** покрив проблему. Defense-in-depth архітектура працює.

---

## Workflow для нових fail'ів

```
1. Запустити debug script (Шари 1-3)
        ↓
2. Знайти винний шар по таблиці симптомів
        ↓
3. Виправити в правильному файлі
        ↓
4. Прогнати regression: docker exec emet_bot_app python /app/tests/run_regression.py --no-generate
        ↓
5. Якщо регресія в інших кейсах — переробити підхід
        ↓
6. Якщо все OK — commit + push + deploy
        ↓
7. Прогнати regression на проді ще раз → переконатись 100% pass
        ↓
8. Опціонально: додати fixture у regression_fixtures.json для майбутнього захисту
```

---

## Anti-patterns (не робити)

❌ **Не правити прямо в продакшен** через docker cp — пиши через git/deploy
❌ **Не міняти RAG_K** як рішення консистенцій — це майже завжди симптом
❌ **Не додавати правила в prompt** для проблем classifier — це окремі шари
❌ **Не пропускати regression** після фікса — 1 нова правка може зламати 3 інші кейси
❌ **Не вимикати classifier** в обхід для коротких запитів — втратиш CLASSIFIER log і observability

---

## Реальні fix'и зроблені цим workflow (24.04)

- Fix #1: alias dictionary (виявлено через debug Шару 1 — `detect_products = []`)
- Fix #4: pre-classifier hint (виявлено через Шар 3 — classifier ігнорує product у короткому запиті)
- Fix #7: conf-fallback (виявлено через Шар 3 — `out_of_scope conf=0.0` для очевидних запитів)
- Hotfix canonical_brand mapping (виявлено через Шар 3 — `product=None` хоча detection знайшов)
- ESSE standalone fix (виявлено через Шар 1 — «ESSE» одне слово не в `EMET_PRODUCTS`)

Workflow доказав себе — **5 фіксів за 1 сесію**, кожен направлений саме в правильний шар без здогадок.
