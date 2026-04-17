"""
classifier.py — LLM-based інтент класифікатор.
Замість 200+ keywords + regex — один виклик gpt-4o-mini визначає:
  intent, primary_product, secondary_product, variant, competitor, needs_verbatim.

Використовується в main.py замість keywords-based routing.
"""
import json
import logging
from openai import AsyncOpenAI

logger = logging.getLogger("classifier")

# ═══════════════════════════════════════════════════════════════════════
# Канонічні назви (закриті списки — LLM обирає тільки з них)
# ═══════════════════════════════════════════════════════════════════════

CANONICAL_PRODUCTS = [
    "Ellansé",
    "Neuramis",
    "Petaran",
    "EXOXE",
    "ESSE",
    "Neuronox",
    "Magnox",
    "Vitaran",              # generic — коли варіант не вказаний
    "HP Cell Vitaran",      # базовий
    "HP Cell Vitaran Whitening",
    "HP Cell Vitaran Tox Eye",
    "Vitaran Skin Healer",
    "IUSE SKINBOOSTER HA 20",
    "IUSE HAIR REGROWTH",
    "IUSE Collagen",
]

VITARAN_VARIANTS = ["i", "iII", "Whitening", "Tox", "Skin Healer"]

COMPETITORS = [
    "Sculptra", "Radiesse", "Juvederm", "Teosyal", "Restylane",
    "Rejuran", "Aesthefill", "Plinest", "Nucleofill", "Mastelli",
    "Juvelook", "Profhilo", "Cellular Matrix", "Benev",
]

# ═══════════════════════════════════════════════════════════════════════
# 19 інтентів з опису (синхронізовано з docs/INTENTS_FOR_VALIDATION.md)
# ═══════════════════════════════════════════════════════════════════════

INTENTS = {
    # A: Sales-заперечення
    "objection_price": "Лікар каже що дорого, ціна висока, не може дозволити",
    "objection_competitor": "Лікар лояльний до конкурента (Sculptra/Juvederm/Radiesse тощо), не хоче міняти",
    "objection_doubt": "Лікар сумнівається у ефективності або якості препарату",
    "objection_no_need": "Лікар каже що не потрібно, не цікаво, не актуально",
    "objection_grey_market": "Лікар посилається на сірий ринок (дешевше неофіційно)",

    # B: Клінічні
    "clinical_why": "Позитивне клінічне питання 'чому безболісний', 'чому менш болючий', 'за рахунок чого працює' — коротка відповідь про механізм, НЕ повний INFO огляд 8 секцій",
    "clinical_side_effect": "Скарга на побічний ефект — пече, болить, набряк, папули, почервоніння, ускладнення",
    "clinical_no_result": "Скарга що не бачить результату, інші препарати дають кращий ефект",
    "clinical_long_recovery": "Тривала реабілітація після процедури",
    "clinical_contraindication": "Питання про протипоказання (вагітність, поєднання з іншими процедурами)",

    # C: Інформаційні
    "info_about_product": "Загальний запит розкажи/опиши продукт",
    "info_composition": "Запит складу препарату",
    "info_storage": "Як зберігати препарат, температура, термін",
    "info_protocol": "Протокол, кількість процедур, інтервал, дозування",
    "info_comparison": "Порівняти 2 продукти (наш vs наш, наш vs конкурент, варіанти одного препарату)",
    "info_indications": "Показання, для кого підходить, при якому стані",

    # D: Робота менеджера
    "evaluate_my_answer": "Менеджер ДАВ свою відповідь лікарю і просить оцінити (не запит SOS)",
    "script_request": "Запит готового скрипта продажу, діалогу менеджер-лікар",
    "visit_prep": "Підготовка до візиту до лікаря",
    "correction": "Менеджер виправляє бота (точна цифра неправильна, концентрація не та тощо)",

    # E: Композиція
    "combo_with_product": "Комбо-протоколи з конкретним препаратом (комбо з Petaran)",
    "combo_for_indication": "Комбо/протоколи для показання (протоколи для пігментації)",

    # F: Мета-запити
    "source_question": "Менеджер питає З ЯКОГО ДОКУМЕНТА / ДЖЕРЕЛА бот взяв інформацію ('из какого документа', 'откуда инфо', 'з якого джерела')",

    # Спецвипадки
    "unclear_no_product": "Заперечення без згаданого продукту, без контексту попередніх повідомлень",
    "out_of_scope": "Не про продукти EMET (погода, політика, тощо)",
    "greeting": "Привітання, розмова не по суті",
}

# Intents де потрібна дослівна цитата з документа (verbatim)
VERBATIM_INTENTS = {
    "info_protocol",
    "info_composition",
    "info_storage",
    "info_indications",
    "clinical_contraindication",
    "combo_with_product",
    "combo_for_indication",
}

# ═══════════════════════════════════════════════════════════════════════
# Classifier промпт
# ═══════════════════════════════════════════════════════════════════════

_INTENT_LIST = "\n".join(f"  - {k}: {v}" for k, v in INTENTS.items())
_PRODUCTS_LIST = ", ".join(CANONICAL_PRODUCTS)
_COMPETITORS_LIST = ", ".join(COMPETITORS)

CLASSIFIER_PROMPT = f"""\
Ти — класифікатор запитів менеджерів EMET до AI-бота. Визначаєш ТИП ЗАПИТУ та ПРОДУКТИ.

Твоя робота: прочитати запит менеджера (+ історію 2 останніх повідомлень якщо є) і повернути JSON.

## ІНТЕНТИ (обери РІВНО ОДИН):
{_INTENT_LIST}

## КАНОНІЧНІ ПРОДУКТИ EMET (обирай ТІЛЬКИ з цього списку):
{_PRODUCTS_LIST}

## ВАРІАНТИ VITARAN (якщо продукт Vitaran-серії):
i, iII, Whitening, Tox, Skin Healer

## КОНКУРЕНТИ (розпізнавання):
{_COMPETITORS_LIST}

## ПРАВИЛА КЛАСИФІКАЦІЇ ПРОДУКТУ:

1. Якщо в запиті згадано продукт EMET — використовуй канонічну назву:
   "ellanse"/"еланс"/"elansé" → "Ellansé"
   "vitaran"/"вітаран"/"витаран" (без варіанту) → "Vitaran"
   "vitaran whitening"/"вайтенінг"/"вайтенинг" → "HP Cell Vitaran Whitening"
   "vitaran tox"/"токс ай"/"тохтай" → "HP Cell Vitaran Tox Eye"
   "iuse sb"/"скін бустер"/"skinbooster" → "IUSE SKINBOOSTER HA 20"
   "iuse hair" → "IUSE HAIR REGROWTH"
   "exoxe"/"ексоксе"/"экзосомы" → "EXOXE"
   "петаран"/"polymolocna" → "Petaran"
   "эсс"/"ессе"/"esse" → "ESSE"  (коротка назва!)
   "нейронокс"/"neuronox" → "Neuronox"  (ботулотоксин, НЕ плутати з Neuramis!)
   "магнокс"/"magnox" → "Magnox"
   "neuramis"/"нейрамис"/"нейраміс" → "Neuramis"  (філер, НЕ Neuronox!)

⚠️ УВАГА НА СХОЖІ НАЗВИ:
- Neuramis = філер (гіалуронова кислота)
- Neuronox = ботулотоксин (різні препарати!)
Не плутай — читай запит буквально.

2bis. "комбо з X" / "комбинация с X" / "поєднати X з Y" → primary_product = X

2. Якщо є варіант Vitaran ("Whitening" / "Tox" / "i" / "iII") — ОБОВ'ЯЗКОВО вкажи variant.

3. Для combo/comparison — використовуй secondary_product.

4. Якщо продукт НЕ згадано в запиті:
   - Перевір історію (chat_history) — можливо продукт з попередніх повідомлень
   - Якщо і там немає — primary_product = null

5. Якщо заперечення "дорого"/"не хоче" БЕЗ продукту і БЕЗ історії → intent = "unclear_no_product"

## ПРАВИЛА ВИЗНАЧЕННЯ INTENT:

**Клінічні (categorize them correctly, NOT as objection):**
- "чому пече/болить/набряк" → clinical_side_effect (НЕ objection! скарга/побічка)
- "чому безболісний / менш болючий / за рахунок чого працює" → clinical_why (ПОЗИТИВНЕ питання, не скарга)
- "не бачу результату" → clinical_no_result
- "можна при вагітності" → clinical_contraindication

**Sales-заперечення (розрізняй типи!):**
- "дорого" / "ціна висока" → objection_price
- "лояльна до Juvederm" / "працює з X" / "вже використовує" → objection_competitor
(ТІЛЬКИ якщо згадано конкретного конкурента)
- "не хоче купувати X" / "не потрібно" / "не цікаво" (БЕЗ конкурента) → objection_no_need
- "сірий ринок" / "дешевше купити неофіційно" → objection_grey_market

**Робота менеджера (НЕ SOS):**
- "менеджер відповів..." / "я сказала ..., оціни" → evaluate_my_answer (менеджер дав СВОЮ розгорнуту відповідь і просить оцінку)
- "не 20, а 10" / "не 6 міс, а 3" / "ти помилився" / "я відповіла краще ніж ти" → correction
  (виправлення бота — менеджер каже що бот помилився)
- "готуюсь до візиту" → visit_prep
- "дай скрипт" → script_request

**Мета-запити:**
- "из какого документа" / "з якого джерела" / "откуда информация" / "какой источник" → source_question
  ⚠️ УВАГА: "из какого документа" це НЕ evaluate_my_answer! Це НЕ correction!
  Це питання про ДЖЕРЕЛО попередньої відповіді бота.

**Важливо розрізняти:**
- "Ellanse пече" → clinical_side_effect (скарга)
- "поєднується Ellanse з лазером?" → clinical_contraindication (питання сумісності з ПРОЦЕДУРОЮ/препаратом НЕ-EMET)
- "комбо Ellanse + Petaran" → combo_with_product (комбо з ІНШИМ EMET-препаратом)

**Інформаційні:**
- "розкажи про X" → info_about_product
- "склад X" → info_composition
- "скільки процедур" → info_protocol
- "чим відрізняється X від Y" → info_comparison
- "конкуренти X" / "що можна протиставити X" → info_comparison
  (це запит менеджера ПОРІВНЯТИ/ОТРИМАТИ СПИСОК конкурентів, НЕ заперечення лікаря!)

## ФОРМАТ ВІДПОВІДІ (ТІЛЬКИ валідний JSON):

```json
{{
  "intent": "один з 19 інтентів",
  "primary_product": "Ellansé" або null,
  "secondary_product": "Petaran" або null,
  "product_variant": "Whitening" або null,
  "competitor": "Sculptra" або null,
  "needs_verbatim": true або false,
  "confidence": 0.0-1.0
}}
```

**needs_verbatim** = true для інтентів: info_protocol, info_composition, info_storage,
info_indications, clinical_contraindication, combo_with_product, combo_for_indication.

**confidence:**
- 0.95-1.0 — очевидний запит
- 0.80-0.94 — впевнений
- 0.60-0.79 — неоднозначно (треба додаткова перевірка)
- <0.60 — неясно (можливо unclear_no_product)

ВАЖЛИВО:
- Повертай ТІЛЬКИ JSON, без пояснень.
- НЕ вигадуй продукти що не в списку канонічних.
- Якщо не впевнений — знижуй confidence, не вгадуй.
"""


# ═══════════════════════════════════════════════════════════════════════
# Функція класифікації
# ═══════════════════════════════════════════════════════════════════════

async def classify(client: AsyncOpenAI, query: str, chat_history: list[dict] = None, model: str = "gpt-4o-mini") -> dict:
    """
    Класифікує запит менеджера.

    Args:
        client: AsyncOpenAI клієнт
        query: текст запиту
        chat_history: список {"role", "content"} останніх повідомлень (max 4)
        model: LLM модель (за замовчуванням gpt-4o-mini — дешево і швидко)

    Returns:
        {
            "intent": str,
            "primary_product": str | None,
            "secondary_product": str | None,
            "product_variant": str | None,
            "competitor": str | None,
            "needs_verbatim": bool,
            "confidence": float
        }

    На помилку повертає fallback {intent: "info_about_product", ...} з confidence=0.0.
    """
    # Формуємо вхідний текст: історія + новий запит
    history_text = ""
    if chat_history:
        last_msgs = chat_history[-4:]  # максимум 4 повідомлення
        history_lines = []
        for m in last_msgs:
            role = "Менеджер" if m.get("role") == "user" else "Бот"
            content = (m.get("content") or "")[:300]  # обрізаємо довгі
            history_lines.append(f"{role}: {content}")
        history_text = "ІСТОРІЯ ДІАЛОГУ:\n" + "\n".join(history_lines) + "\n\n"

    user_message = f"{history_text}ПОТОЧНИЙ ЗАПИТ МЕНЕДЖЕРА:\n{query}"

    try:
        response = await client.chat.completions.create(
            model=model,
            timeout=15,
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": user_message}
            ]
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)

        # Валідація
        intent = data.get("intent")
        if intent not in INTENTS:
            logger.warning("Classifier: unknown intent '%s', fallback to info_about_product", intent)
            intent = "info_about_product"

        primary = data.get("primary_product")
        if primary and primary not in CANONICAL_PRODUCTS:
            logger.warning("Classifier: unknown product '%s', ignoring", primary)
            primary = None

        secondary = data.get("secondary_product")
        if secondary and secondary not in CANONICAL_PRODUCTS:
            secondary = None

        variant = data.get("product_variant")
        if variant and variant not in VITARAN_VARIANTS:
            variant = None

        competitor = data.get("competitor")
        if competitor and competitor not in COMPETITORS:
            competitor = None

        return {
            "intent": intent,
            "primary_product": primary,
            "secondary_product": secondary,
            "product_variant": variant,
            "competitor": competitor,
            "needs_verbatim": intent in VERBATIM_INTENTS,
            "confidence": float(data.get("confidence", 0.5)),
        }
    except Exception as e:
        logger.error("Classifier failed: %s", e)
        return {
            "intent": "info_about_product",
            "primary_product": None,
            "secondary_product": None,
            "product_variant": None,
            "competitor": None,
            "needs_verbatim": False,
            "confidence": 0.0,
        }


# ═══════════════════════════════════════════════════════════════════════
# Маппінг intent → coach sub-prompt
# (для сумісності зі старою архітектурою _COACH_PROMPT_MAP)
# ═══════════════════════════════════════════════════════════════════════

def normalize_product(primary: str | None, variant: str | None) -> str | None:
    """
    Нормалізує пару (primary, variant) → повна канонічна назва продукту.
    Використовується для RAG metadata фільтра.

    Приклади:
      ("Vitaran", "Whitening") → "HP Cell Vitaran Whitening"
      ("Vitaran", "i")         → "HP Cell Vitaran i"
      ("Vitaran", None)        → "Vitaran"  (generic, без варіанту)
      ("Ellansé", None)        → "Ellansé"
    """
    if not primary:
        return None

    # Vitaran-серія: primary=Vitaran + variant → повна назва
    if primary == "Vitaran" and variant:
        mapping = {
            "i": "HP Cell Vitaran i",
            "iII": "HP Cell Vitaran iII",
            "Whitening": "HP Cell Vitaran Whitening",
            "Tox": "HP Cell Vitaran Tox Eye",
            "Skin Healer": "Vitaran Skin Healer",
        }
        return mapping.get(variant, primary)

    return primary


def product_search_keywords(primary: str | None, variant: str | None) -> list[str]:
    """
    Повертає список ключових слів для фільтрації RAG-чанків по продукту.
    Використовується для пошуку в metadata.source або в тексті чанку.
    """
    if not primary:
        return []

    if primary == "Vitaran":
        if variant == "i":
            return ["Vitaran i", "Vitaran_i", "HP Cell Vitaran i", "полінуклеотиди", "PDRN"]
        elif variant == "iII":
            return ["Vitaran iII", "HP Cell Vitaran iII"]
        elif variant == "Whitening":
            return ["Whitening", "Вайтенинг", "Вайтенінг", "глутатіон", "транексамова"]
        elif variant == "Tox":
            return ["Tox Eye", "Тохтай", "Токс", "пептиди"]
        elif variant == "Skin Healer":
            return ["Skin Healer", "Скін Хілер"]
        # generic Vitaran — все
        return ["Vitaran", "Вітаран", "Витаран", "HP Cell"]

    if primary == "Ellansé":
        return ["Ellansé", "Ellanse", "Елансе", "полікапролактон", "PCL"]

    if primary == "Petaran":
        return ["Petaran", "Петаран", "полі-L-молочна", "PLLA"]

    if primary == "EXOXE":
        return ["EXOXE", "Ексоксе", "екзосоми", "экзосомы"]

    if primary == "Neuramis":
        return ["Neuramis", "Нейрамис", "Нейраміс"]

    if primary == "Neuronox":
        return ["Neuronox", "Нейронокс", "ботулотоксин"]

    if primary == "IUSE SKINBOOSTER HA 20":
        return ["IUSE SKINBOOSTER", "IUSE Skin Booster", "Скінбустер", "скін бустер"]

    if primary == "IUSE HAIR REGROWTH":
        return ["IUSE HAIR", "Hair Regrowth", "відновлення росту волосся"]

    if primary == "IUSE Collagen":
        return ["IUSE Collagen", "Колаген"]

    if primary == "ESSE":
        return ["ESSE", "Ессе", "Essе Skincare", "пробіотик"]

    if primary == "Magnox":
        return ["Magnox", "Магнокс", "магній"]

    # Generic IUSE
    if primary == "IUSE":
        return ["IUSE"]

    return [primary]


VALIDATOR_PROMPT = """\
Ти — валідатор відповідей EMET-бота. Перевіряєш відповідь бота на відповідність запиту.

ВХІДНІ ДАНІ:
- Запит менеджера
- Згенерована відповідь бота
- Очікуваний продукт (з classifier)

ПЕРЕВІРЯЙ 3 КРИТЕРІЇ:
1. Чи відповідь про той самий продукт що в запиті? (не плутає продукти)
2. Чи зафіксовано правильні цифри (концентрація, місяці, процедури)? Вкажи явні цифри у відповіді.
3. Чи не пропущені критичні нюанси (наприклад "не в одну зону" замість "не можна")?

ФОРМАТ ВІДПОВІДІ (JSON):
{
  "valid": true/false,
  "issues": ["список виявлених проблем коротко"],
  "severity": "low" | "medium" | "high"  // high — треба перегенерувати
}

Якщо все ОК — valid=true, issues=[], severity="low".
Якщо відповідь про не той продукт — severity="high".
Якщо цифри правильні але формулювання можна покращити — severity="low" і issues з коментарями.
"""


async def validate_answer(client, query, answer, expected_product, model="gpt-4o-mini"):
    """
    Валідатор відповіді. Повертає {valid: bool, issues: list, severity: str}.
    Використовується тільки для low-confidence інтентів або critical verbatim.
    """
    import json as _json
    try:
        user_msg = (
            f"ЗАПИТ МЕНЕДЖЕРА:\n{query}\n\n"
            f"ОЧІКУВАНИЙ ПРОДУКТ: {expected_product or 'не визначено'}\n\n"
            f"ВІДПОВІДЬ БОТА:\n{answer[:3000]}"  # обрізаємо щоб не перевитрачати токени
        )
        resp = await client.chat.completions.create(
            model=model,
            timeout=15,
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": VALIDATOR_PROMPT},
                {"role": "user", "content": user_msg}
            ]
        )
        return _json.loads(resp.choices[0].message.content.strip())
    except Exception as e:
        logger.warning("Validator failed: %s", e)
        return {"valid": True, "issues": [], "severity": "low"}


INTENT_TO_COACH_SUBTYPE = {
    # Sales
    "objection_price": "sos",
    "objection_competitor": "sos",
    "objection_doubt": "sos",
    "objection_no_need": "sos",
    "objection_grey_market": "sos",

    # Clinical
    "clinical_why": "sos",  # short clinical answer via SOS clinical block
    "clinical_side_effect": "sos",
    "clinical_no_result": "sos",
    "clinical_long_recovery": "sos",
    "clinical_contraindication": "info",

    # Info
    "info_about_product": "info",
    "info_composition": "info",
    "info_storage": "info",
    "info_protocol": "info",
    "info_comparison": "info",
    "info_indications": "info",

    # Менеджер
    "evaluate_my_answer": "evaluate",
    "script_request": "script",
    "visit_prep": "visit",
    "correction": "feedback",

    # Combo — окремий режим (PROMPT_COMBO з prompts.py)
    "combo_with_product": "combo",
    "combo_for_indication": "combo",

    # Мета
    "source_question": "source",

    # Спец
    "unclear_no_product": "ask_product",
    "out_of_scope": "out_of_scope",
    "greeting": "greeting",
}
