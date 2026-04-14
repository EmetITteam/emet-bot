# Старт: Середа 15.04.2026

Повний план: [ARCHITECTURE_REFACTOR_PLAN.md](ARCHITECTURE_REFACTOR_PLAN.md)

## Контекст коротко
Бот дає галюцинації (Vitaran Whitening 20 мг/мл замість 10), неправильні продукти (Exoxe → Juvelook), втрату нюансів ("в одну зону" → "взагалі"). Корінь — keywords-routing + RAG без продуктового фільтру + LLM-перефразування.

## Рішення — 4-етапний pipeline
1. **Classifier** (gpt-4o-mini): intent + product + variant + needs_verbatim
2. **Product-Locked RAG**: фільтр чанків по metadata.product_canonical
3. **Verbatim mode** для protocol/composition/clinical
4. **Validator** при confidence < 0.85

## План на середу (4 год)
- [ ] Написати classifier-промпт (1 год)
- [ ] Інтегрувати classifier паралельно з legacy keywords (canary, 1 год)
- [ ] Додати metadata.product_canonical у sync_manager.py + reindex (1.5 год)
- [ ] Тест на 50 реальних запитах з логів (30 хв)
- [ ] Деплой в canary режимі ввечері

## План на четвер (4 год)
- [ ] Переписати sub-prompts по draft-таблиці (2 год)
- [ ] Verbatim mode для protocol/composition/clinical (1 год)
- [ ] Validator для low-confidence (1 год)
- [ ] Деплой повного pipeline
- [ ] Паралельно: Sales + Med переглядають 5 червоних інтентів

## Що від користувача потрібно
- Завтра ранок: написати Sales Dir + Med Dir щоб були готові переглянути 5 структур у четвер (1-2 год їх часу)

## Файли в фокусі
- `main.py` (process_text_query, get_context)
- `prompts_v2.py` (всі sub-prompts)
- `sync_manager.py` (додати metadata)
- Новий: `classifier.py` або інлайн в main.py

## НЕ робимо завтра
- Не чіпаємо legacy keywords (видалимо тільки коли pipeline стабільний)
- Не валідуємо ВСІ 19 інтентів — тільки 5 червоних
- Не пишемо документацію — тільки робочий код

## Метрики до перевірки в п'ятницю
- Hallucination на 20 тестових запитах: <5%
- Wrong product retrieval: 0%
- Дизлайки Александри: ≤1 за день
