# План архітектурного рефакторингу EMET бота

**Дата:** 14.04.2026
**Тригер:** ~12 дизлайків Александри + ~5 галюцинацій за 2 дні
**Стан:** 200+ keywords-милиць, кожен новий приклад ламає логіку

---

## Корінь проблеми (одна причина — три симптоми)

```
Запит → RAG (semantic search) → LLM (синтезує те що отримав)
              ↑                          ↑
       Повертає не той продукт    Не валідує відповідність
       або не той варіант           запит ↔ контекст
```

**Прикладі:**
- "Exoxe протокол" → RAG повернув Juvelook → бот відповів про Juvelook
- "Vitaran Whitening пече" → RAG повернув і Vitaran i (20 мг) і Whitening (10 мг) → бот написав 20 для Whitening
- "Petaran+Ellanse" → бот перефразував "не можна в одну зону" → "не можна взагалі"

---

## Архітектурне рішення: 4-етапний pipeline

### Етап 1: INTENT CLASSIFIER (gpt-4o-mini, ~0.5с, ~$0.0001)

**Замість 200+ keywords + regex** — один LLM визначає intent + продукт.

```python
async def classify(query: str, history: list[dict]) -> dict:
    """
    Returns:
    {
      "intent": "objection_price" | "clinical_side_effect" | "info_protocol" | ...,
      "primary_product": "Petaran" | None,        # canonical name
      "secondary_product": "Ellansé" | None,       # для combo/comparison
      "product_variant": "Whitening" | "i" | None, # для Vitaran
      "competitor": "Sculptra" | None,
      "needs_verbatim": bool,                      # для protocol/dosage
      "confidence": 0.0-1.0
    }
    """
```

**Що зникає:**
- `_OBJECTION_KEYWORDS` (60 слів)
- `_TYPE_B_KEYWORDS`, `_TYPE_C_KEYWORDS` + 3 regex
- `_VISIT_KEYWORDS`, `_SCRIPT_KEYWORDS`, `_FOLLOWUP_KEYWORDS`
- `_EMET_PRODUCTS` substring detection (60 aliases)
- Substring-баги ("esse" в "radiesse")
- Плутанина варіантів Vitaran

### Етап 2: PRODUCT-LOCKED RAG

Замість семантичного пошуку на сирому запиті:

```python
chunks = rag.search(
    query=enriched_query,                    # формується з intent + extracted keywords
    metadata_filter={                        # ChromaDB metadata filter
        "product_canonical": classifier.primary_product,
        "variant": classifier.product_variant or {"$exists": False}
    },
    k=12
)
```

**Що це усуває:**
- "Exoxe запит → Juvelook відповідь" (Juvelook чанки фізично відсутні)
- Плутанина варіантів (фільтр `variant=Whitening`)

**Передумова:** додати `metadata.product_canonical` у chunks при індексації (sync_manager.py)

### Етап 3: VERBATIM MODE для критичних інтентів

Для intent ∈ {`info_protocol`, `info_composition`, `clinical_side_effect`, `info_dosage`, `info_contraindication`}:

```
Промпт:
"Це запит на ТОЧНУ інформацію. Цитуй ДОСЛІВНО з контексту з джерелом.
НЕ перефразовуй. НЕ скорочуй нюанси.
Формат: «[з документа SOURCE.docx]: <цитата>»

❌ 'не можна комбінувати' — втрата нюансу, заборонено
✅ 'З Petaran_Combo.docx: «не можна в одну процедуру в одну зону через ризик фіброзу»'"
```

**Що це усуває:**
- Втрата нюансу ("в одну зону" → "взагалі")
- Перефразування медичних протоколів

### Етап 4: VALIDATOR (тільки якщо confidence < 0.85, ~$0.0001)

Один швидкий gpt-4o-mini виклик ПІСЛЯ генерації:

```
Question: "Vitaran Whitening пече"
Generated answer: "{answer}"
Source chunks used: {chunks_summary}

Verify:
1. Чи відповідь про той самий продукт що в питанні? YES/NO
2. Чи всі цифри з контексту збережені правильно? YES/NO + список розбіжностей
3. Чи нюанси (умови, інтервали, протипоказання) збережено? YES/NO

Return: {valid: bool, issues: [str]}
```

Якщо `valid=false` → перегенерувати з посиленим промптом і списком виявлених issues.

---

## Структури відповідей по intent (DRAFT для валідації)

### Категорія A: Заперечення (sales) — Sales Director перевіряє

| Intent | Опис | Структура (моя пропозиція) | Перевірка |
|--------|------|---------------------------|-----------|
| `objection_price` | "дорого", "не можу дозволити" | SOS-ціна: матем. маржі (грн/процедура) → killer phrase ≤15 слів → next step | Sales |
| `objection_competitor` | "є Sculptra", "лояльна до X" | SOS-конкурент: диференціація з фактами, БЕЗ критики → killer → next step | Sales + Med |
| `objection_doubt` | "не вірю", "не впевнений" | SOS-сумнів: клінічний кейс + дослідження → killer → next step | Med |
| `objection_no_need` | "не потрібно", "не цікаво" | SOS-no-need: створити потребу через SPIN → next step | Sales |
| `objection_grey_market` | "сірий ринок дешевше" | SOS-grey: fear-based (юридика, гарантія, ризик клініки) | Legal + Sales |

### Категорія B: Клінічні (med) — Med Director перевіряє

| Intent | Опис | Структура (моя пропозиція) | Перевірка |
|--------|------|---------------------------|-----------|
| `clinical_side_effect` | "пече", "набряк", "папули" | 1) Причина (компоненти з контексту) → 2) Норма? → 3) Як зменшити → 4) Як подати лікарю | **Med** |
| `clinical_no_result` | "не бачу результату" | 1) Перевірка дотримання протоколу → 2) Реалістичні очікування за термінами → 3) Як підсилити (комбо) | **Med** |
| `clinical_long_recovery` | "тривала реабілітація" | VERBATIM норми реабілітації + порівняння з конкурентом якщо є | **Med** |
| `clinical_contraindication` | "можна вагітним?", "поєднується з X" | VERBATIM з протоколу. Disclaimer "лікар обирає". | **Med + Legal** |

### Категорія C: Інформація — Med + Sales

| Intent | Опис | Структура | Перевірка |
|--------|------|-----------|-----------|
| `info_about_product` | "розкажи про X" | 8-секційний формат: що → склад → механізм → для кого → переваги → заперечення → важливо → контрольні питання | Sales |
| `info_composition` | "склад X" | VERBATIM склад з документа з точними цифрами | **Med** |
| `info_storage` | "як зберігати" | VERBATIM з інструкції | **Med** |
| `info_protocol` | "скільки процедур", "протокол" | **VERBATIM** з протоколу + посилання | **Med** |
| `info_comparison` | "X vs Y" | Таблиця: параметр / X / Y. Тільки факти з контексту | Med + Sales |
| `info_indications` | "для кого" | Перелік показань з контексту | **Med** |

### Категорія D: Робота менеджера — Sales

| Intent | Опис | Структура | Перевірка |
|--------|------|-----------|-----------|
| `evaluate_my_answer` | "я сказав... оціни" | Оцінка X/10 → червоні прапори → що добре → що покращити (з втратою) → діалог-версія | Sales |
| `script_request` | "дай скрипт" | Діалог М↔Л↔М↔Л↔М (антимонолог: max 2 речення) → killer phrase | Sales |
| `visit_prep` | "готуюсь до візиту" | Профіль → 1-3 продукти → відкр. фраза → 3 питання → план B | Sales + Med |
| `correction` | "не 20, а 10" | Коротко: визнай → повтори правильно → дякую | IT (technical) |

### Категорія E: Композиція — Med

| Intent | Опис | Структура | Перевірка |
|--------|------|-----------|-----------|
| `combo_with_product` | "комбо з Petaran" | Список протоколів VERBATIM з документа без перефразування | **Med** |
| `combo_for_indication` | "протоколи для пігментації" | Список комбо що підходять для показання | **Med** |

### Спецвипадки

| Intent | Опис | Дія |
|--------|------|-----|
| `unclear_no_product` | "дорого" без продукту | Запит уточнення (вже зроблено) |
| `out_of_scope` | "погода", "політика" | Стандартна відмова |
| `kb_question` | "регламент відпустки" | Mode=KB (без змін) |

---

## План впровадження — СТИСНУТИЙ (4 дні: 15-18.04)

### Середа 15.04 — 4 год: Classifier + Product-Locked RAG
| Крок | Час | Що |
|------|-----|-----|
| 1.1 | 1 год | Написати classifier-промпт (intent + product + variant + needs_verbatim) |
| 1.2 | 1 год | Інтегрувати в `process_text_query` (паралельно з legacy — canary) |
| 1.3 | 1.5 год | Додати `metadata.product_canonical` у sync_manager.py + перебудова |
| 1.4 | 30 хв | Тест на 50 реальних запитах з логів |
| 1.5 | вечір | Деплой в canary — classifier логує, відповіді ще legacy |

### Четвер 16.04 — 4 год: Sub-prompts + Verbatim + Validator
| Крок | Час | Що |
|------|-----|-----|
| 2.1 | 2 год | Переписати sub-prompts по draft-таблиці інтентів (мій draft + покращення) |
| 2.2 | 1 год | Verbatim mode для protocol/composition/clinical |
| 2.3 | 1 год | Validator (gpt-4o-mini) для confidence < 0.85 |
| 2.4 | вечір | Деплой повного pipeline на production |
| 2.5 | паралельно | Sales + Med Director переглядають **5 червоних інтентів** (1-2 год їх часу) |

### П'ятниця 17.04 — повний день моніторингу
- Ранок: спостереження логів Александри + ще 2-3 менеджерів
- Кожні 2 год: bug-fix → redeploy
- Інтегрую коментарі Sales/Med коли надійдуть

### Понеділок 21.04 — фінал (буфер на доопрацювання)
- Чистка legacy keywords (~250 рядків)
- Регресійні тести
- Коротка документація

**Загалом:** ~10-12 годин IT за 4 дні + 2 год часу Sales/Med Director

---

## Що видаляємо (чистка)

```python
# main.py — видалити:
- _OBJECTION_KEYWORDS (60+ слів)
- _TYPE_B_KEYWORDS, _TYPE_C_KEYWORDS, _CORRECTION_PATTERNS
- _VISIT_KEYWORDS, _SCRIPT_KEYWORDS, _FOLLOWUP_COACH_KEYWORDS
- _EMET_PRODUCTS, _PRODUCT_CANONICAL, _SHORT_KEYS
- _COMPETITORS, _COMPETITOR_TO_CANONICAL
- _VITARAN_VARIANTS hint
- _early_type_b, _early_type_c
- _is_short_affirmation, _is_mid_session_rhetorical
- _RHETORICAL_MID_SESSION
- TYPE_B_OVERRIDE_TO_SOS

# Залишається тільки:
- detect_intent → classify (LLM-based)
- get_context → product_locked_search
- Sub-prompts (з валідованими структурами)
- Post-processing (regex ** → *)
```

**Видаляємо ~250 рядків коду, додаємо ~80 рядків.**

---

## Метрики успіху (як перевіряти)

| Метрика | Зараз | Ціль |
|---------|-------|------|
| Routing accuracy (52 test queries) | 100% (з нагородою keyword-милиць) | 95%+ (без них) |
| Hallucination rate (галюцинація цифр) | ~10% (з логів) | <1% |
| Wrong product retrieval | ~5% | 0% (фізично заблоковано) |
| Verbatim accuracy для протоколів | ~60% | 95%+ |
| Дизлайки Александри / тиждень | ~10 | <2 |

---

## Файл для відділів (виходить окремо)

`intents_response_structures_FOR_VALIDATION.md` — таблиця з draft структурами, де кожен intent має:
- 2-3 реальних приклади з логів
- Моя пропонована структура
- Приклад "ідеальної" відповіді
- Порожня секція "Коментар відділу"

Цей файл передається:
- **Sales Director** (Александра) — категорії A, D
- **Med Director** — категорії B, C, E
- **Legal** — `objection_grey_market`, `clinical_contraindication`

---

## Ризики

| Ризик | Імовірність | Мітигація |
|-------|-------------|-----------|
| Classifier помиляється на edge case | Середня | Validator + fallback на keywords для критичних |
| Затримка +1 сек | Низька | Розпаралелити classifier з RAG |
| Reindex втрачає дані | Низька | Knowledge integrity check (вже є) |
| Vendor lock-in на gpt-4o-mini | Низька | Classifier prompt portable на будь-який LLM |

---

## Рішення для затвердження

**Затверджуєш повний план?**

Якщо так — мої дії:
1. Зараз: створити `intents_response_structures_FOR_VALIDATION.md` для відділів
2. Завтра-післязавтра: чекаємо коментарі Sales/Med Director
3. Після валідації: 14 год імплементації за 2-3 робочі дні
4. Тиждень canary в продакшені
5. Перехід + чистка

**Альтернатива (швидкий MVP за 4 год):**
- Тільки classifier + product-locked RAG (без validator + verbatim)
- Покриває 70% проблем
- Без валідації відділів — використовую поточні sub-prompts

Що обираєш — повний план чи MVP?
