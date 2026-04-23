"""dialog_state.py — мінімальний state-tracker діалогу.

Чисті функції без сайд-ефектів. Один call-site у main.py перед збиранням промпту.

Що робить:
- compute_state(classifier_out, prev_state, query_text) — будує DialogState поточного ходу
- should_reset_history(prev, curr) — true якщо тема діалогу змінилась і chat_history
  з минулих ходів стане шумом замість контексту
- detect_comparison(query_text, prev_state) — витягує (продукт_a, продукт_b) для
  порівняльних запитів і зберігає для follow-up'ів («а по дії однакові?»)

Інтеграція в main.py:
    state_data = await state.get_data()
    prev_state = state_data.get("dialog_state") or {}
    curr_state = compute_state(classifier_result, prev_state, text)
    if should_reset_history(prev_state, curr_state):
        chat_history = []  # стара історія недоречна
    await state.update_data(dialog_state=curr_state)
"""

# Інтенти що зміщують тему діалогу — після них chat_history попередніх ходів стає шумом
_TOPIC_SHIFT_INTENTS = {
    "objection_price", "objection_competitor", "objection_no_need", "objection_grey_market",
    "objection_doubt", "clinical_side_effect", "clinical_no_result", "clinical_long_recovery",
    "clinical_contraindication",
}

# Двослівні маркери порівняння — для detect_comparison
_COMPARISON_MARKERS = ["різниця", "vs", "проти", "відрізня", "порівня", "чим відрізн", "у чому різн"]


def compute_state(classifier_result: dict, prev_state: dict, query_text: str) -> dict:
    """Будує state поточного ходу. Зберігає comparison_target з попереднього ходу
    якщо це follow-up на той самий продукт без явного скиду."""
    if not isinstance(prev_state, dict):
        prev_state = {}
    if not isinstance(classifier_result, dict):
        classifier_result = {}

    intent = classifier_result.get("intent", "")
    primary_product = classifier_result.get("primary_product")
    variant = classifier_result.get("product_variant")
    objection_type = _extract_objection_type(intent)

    # Comparison target — ловимо явні порівняння, інакше успадковуємо з prev (для follow-up'ів)
    comparison_target = _detect_comparison(query_text, primary_product) or prev_state.get("comparison_target")

    # Якщо тема явно змінилась — обнуляємо comparison_target
    prev_intent = prev_state.get("intent", "")
    if prev_intent and intent and _is_topic_shift(prev_intent, intent, prev_state.get("primary_product"), primary_product):
        comparison_target = None

    return {
        "intent": intent,
        "primary_product": primary_product,
        "variant": variant,
        "objection_type": objection_type,
        "comparison_target": comparison_target,
        "query_excerpt": (query_text or "")[:120],
    }


def should_reset_history(prev_state: dict, curr_state: dict) -> bool:
    """True якщо chat_history попередніх ходів треба скинути.

    Скидаємо коли:
    - intent змінився на topic-shift тип і продукт змінився
    - або intent змінився на той самий topic-shift тип з ІНШИМ під-типом
      (наприклад objection_price → objection_competitor — це «нове заперечення»)
    """
    if not isinstance(prev_state, dict) or not prev_state:
        return False
    prev_intent = prev_state.get("intent", "")
    curr_intent = curr_state.get("intent", "")
    if not prev_intent or not curr_intent:
        return False

    # Якщо intent той самий і продукт той самий — це валідний follow-up, історію не чіпаємо
    if prev_intent == curr_intent and prev_state.get("primary_product") == curr_state.get("primary_product"):
        return False

    # Topic shift на новий objection / clinical → скидаємо
    if curr_intent in _TOPIC_SHIFT_INTENTS and prev_intent in _TOPIC_SHIFT_INTENTS:
        # Різні objection-типи на один продукт = нова тема
        if prev_intent != curr_intent:
            return True
        # Той самий objection_*, але інший продукт
        if prev_state.get("primary_product") != curr_state.get("primary_product"):
            return True

    # Явне переключення з заперечення на info і навпаки — НЕ скидаємо (інформаційне уточнення в межах теми OK)
    return False


def _is_topic_shift(prev_intent: str, curr_intent: str, prev_product, curr_product) -> bool:
    """Внутрішній: чи змінилась тема настільки що comparison_target треба обнулити."""
    if prev_intent == curr_intent:
        return False
    if prev_product and curr_product and prev_product != curr_product:
        # Перейшли на інший продукт — старе порівняння неактуальне
        return True
    if curr_intent in _TOPIC_SHIFT_INTENTS and prev_intent not in _TOPIC_SHIFT_INTENTS:
        # Заперечення відкрилось — уточнюючий контекст порівняння більше не потрібен
        return True
    return False


def _detect_comparison(query_text: str, primary_product) -> tuple | None:
    """Витягує пару (a, b) для порівняння з тексту запиту, якщо явно згадані 2 продукти."""
    if not query_text:
        return None
    t = query_text.lower()
    if not any(m in t for m in _COMPARISON_MARKERS):
        return None
    # Простий лексичний детектор (не AI) — шукаємо названі продукти у тексті
    products_seen = []
    name_map = [
        ("petaran", "Petaran"), ("петаран", "Petaran"),
        ("ellans", "Ellansé"), ("елансе", "Ellansé"), ("эллансе", "Ellansé"),
        ("vitaran", "Vitaran"), ("вітаран", "Vitaran"), ("витаран", "Vitaran"),
        ("neuramis", "Neuramis"), ("нейрамис", "Neuramis"), ("нейраміс", "Neuramis"),
        ("exoxe", "EXOXE"), ("ексоксе", "EXOXE"),
        ("iuse", "IUSE"), ("esse", "ESSE"), ("ессе", "ESSE"),
        ("neuronox", "Neuronox"), ("нейронокс", "Neuronox"),
    ]
    for needle, canonical in name_map:
        if needle in t and canonical not in products_seen:
            products_seen.append(canonical)
        if len(products_seen) >= 2:
            break
    if len(products_seen) >= 2:
        return (products_seen[0], products_seen[1])
    if primary_product and len(products_seen) == 1 and products_seen[0] != primary_product:
        return (primary_product, products_seen[0])
    return None


def _extract_objection_type(intent: str) -> str | None:
    """Нормалізує intent до objection_type для трекінгу."""
    if not intent:
        return None
    if intent.startswith("objection_"):
        return intent.replace("objection_", "")
    if intent.startswith("clinical_"):
        return intent.replace("clinical_", "")
    return None
