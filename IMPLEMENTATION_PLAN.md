# EMET Bot — План розвитку

## Поточний статус: v2.3 (квітень 2026)

### Реалізовано

| Компонент | Статус | Деталі |
|-----------|--------|--------|
| Telegram-бот (8 режимів) | ✅ Prod | FSM, streaming, voice, photo |
| RAG 3-zone search | ✅ Prod | Products 598 / Competitors 599 / KB 470 + scope metadata (line/product/ingredient/protocol) |
| Product-locked retrieval | ✅ Prod | Фільтр по product_canonical (16 продуктів) |
| Classifier-based routing | ✅ Prod | LLM classifier із 19 інтентами + 5 few-shot examples |
| LLM failover | ✅ Prod | GPT-4o → Gemini → Claude + tagging failover_depth у logs |
| Admin Panel | ✅ Prod | ~26 маршрутів, дашборд, LMS, доступи, /quality, /gaps |
| LMS система | ✅ Prod | 13 курсів, 48 тем, тести, scoring |
| Quality Monitor | ✅ Prod | Щоденний звіт о 08:00 + LLM-judge на вибірці + SD метрики |
| Quality history | ✅ Prod | quality_history таблиця з 30-day тренд + бізнес-метрики SD |
| Knowledge gaps | ✅ Prod | Авто-фіксація виправлень + admin /gaps + heavy-correctors alert (>2/день) |
| Eval harness | ✅ Prod | tests/regression_fixtures.json (15 кейсів) + run_regression.py |
| Anti-sycophancy | ✅ Prod | Бот перевіряє виправлення через RAG, не погоджується автоматично |
| DialogState tracker | ✅ Prod | Скид chat_history при topic shift, comparison_target tracking |
| Google Drive sync | ✅ Prod | Auto-sync 60 хв + retry на rate-limit + sanity check + admin notify |
| Backup automation | ✅ Prod | PG dump + ChromaDB tar, cron 02:00, 7-day rotation |
| Rate limit | ✅ Prod | Burst 10/хв + daily 50/день/користувач (admin role exempt) |
| CI/CD | ✅ Prod | GitHub Actions → SSH deploy |
| Docker Compose | ✅ Prod | 3 сервіси, healthcheck (procps fix) |

### Не реалізовано (backlog) — оновлено 24.04.2026

**P0 — контент-блокери (медвідділ):**
| Задача | Блокер | Оцінка |
|--------|--------|--------|
| Курси ESSE та Magnox 520 | Контент від медвідділу | 2 год |
| Протипоказання для 10 курсів | Контент від медвідділу | 3 год |
| Умови зберігання для 11 курсів | Контент від медвідділу | 2 год |
| Матриця "показання → продукт EMET" | Контент від медвідділу | 1 год |
| Таблиця виробників препаратів | Контент від медвідділу | 1 год |
| Розширити product mapping в classifier (Refining Cleanser, Bakuchiol Serum etc — 23 ESSE products) | Розробник | 30 хв |

**P1 — UX / Quality:**
| Задача | Блокер | Оцінка |
|--------|--------|--------|
| Inline keyboard сценаріїв у Coach (див. docs/scope_C_to_consider.md) | Розробник + UX рішення | 3 год |
| Junior/senior промпт-сегментація (див. scope_C) | Розробник + аудит users.level | 3 год |
| Whitelist критичних фактів — окремий strict prompt для composition/concentration | Розробник | 2 год |
| Rich quality dashboard (графіки замість таблиці) | Розробник | 4 год |
| Inline keyboard "бот помилився" в Telegram | Розробник | 2 год |

**P2 — техдолг:**
| Задача | Блокер | Оцінка |
|--------|--------|--------|
| Рефакторинг main.py на модулі (3300+ рядків) | Тільки розробник, поки відкладено | 16 год |
| DB migrations (Alembic) | Тільки розробник | 8 год |
| REST API для admin panel | Тільки розробник | 20 год |
| Розширити eval harness до 30+ кейсів | Збір знань з продакшену | 4 год |

**P3 — nice-to-have:**
| Задача | Статус | Оцінка |
|--------|--------|--------|
| Sentry інтеграція | ❌ Відмовились (24.04 — у нас є Telegram алерти, ROI низький при 9 юзерах) | — |
| 60-секунд режим (короткі відповіді) | ❌ Відмовились (24.04 — менеджери не просять) | — |
| Webhook замість polling | ❌ Відмовились — long polling достатньо для <100 юзерів | — |

---

## Спринти

### ✅ Спринт A (квітень 2026) — Quality + observability — ЗАВЕРШЕНО
- ✅ Класифікатор-based routing (19 інтентів)
- ✅ Product-locked retrieval (16 продуктів через product_canonical)
- ✅ Combo synergy (dual product-locked RAG для синергії)
- ✅ 3-zone RAG split (products / competitors / kb)
- ✅ Knowledge integrity guard після кожного sync
- ✅ Quality Monitor + LLM-judge (gpt-4o-mini, 10 вибірка/день)
- ✅ Auto-backup PG + ChromaDB (cron 02:00)
- ✅ Daily rate limit (50/день/користувач)

### ✅ Спринт B (24.04) — Anti-galloicination + state — ЗАВЕРШЕНО
- ✅ Anti-sycophancy + knowledge_gaps tracking
- ✅ Few-shot для mono vs combo classifier
- ✅ KB→Coach automatic fallback
- ✅ Scope metadata (line/product/ingredient/protocol)
- ✅ DialogState module (topic shift detection)
- ✅ Price-comparative SOS doctrine (Petaran vs Ellansé)
- ✅ Failover_depth tagging
- ✅ Heavy-correctors alert (>2 виправлень/день)
- ✅ SD metrics у quality_history
- ✅ STRICT-MODE для verbatim
- ✅ Differential diagnosis на low confidence
- ✅ Eval harness (15 fixtures + runner)

### Спринт C (на черзі, чекає тригерів) — UX optimisation
**Тригери:** >20 активних менеджерів АБО прямий фідбек "довго писати"
- [ ] Inline keyboard сценаріїв у Coach
- [ ] Junior/senior промпт-сегментація
- Деталі: [docs/scope_C_to_consider.md](docs/scope_C_to_consider.md)

### Спринт D (квітень-травень) — Контент від медвідділу
**Блокер: медичний відділ**

- [ ] ESSE — повний курс + 23 продукти у classifier mapping
- [ ] Magnox 520 — повний курс
- [ ] Протипоказання для 10 курсів
- [ ] Умови зберігання для 11 курсів
- [ ] Таблиця виробників + дистриб'юторів

---

## Критичний шлях

```
Медвідділ (контент)  ──→  Спринт 1 (завантаження)  ──→  Спринт 2 (RAG якість)
                                                              │
Розробник ─────────────────────────────────────────→  Спринт 3 (рефакторинг)
                                                              │
                                                         Спринт 4 (інфра)
```

**Блокер #1:** Контент від медичного відділу — без нього протипоказання, зберігання, ESSE, Magnox залишаться порожніми.

**Блокер #2:** main.py 3267 рядків — кожна нова фіча збільшує ризик регресії. Рефакторинг на модулі знизить цей ризик.

---

## Метрики успіху (оновлено 24.04)

| Метрика | 23.04 (фактично) | Ціль | Де дивитись |
|---------|-------------------|------|-------------|
| LLM-judge avg helpfulness | 6.6/10 | >8.0 | quality_history |
| LLM-judge avg factual | 7.6/10 | >9.0 | quality_history |
| LLM-judge avg role_awareness | 5.8/10 | >7.5 | quality_history |
| Refusal rate | 12% | <5% | quality_history.refusal_rate |
| Correction_rate (виправлень/день) | TBD (новий метрик з 24.04) | <2% | quality_history.correction_rate |
| Margin at risk (преміум-діалоги з низькою оцінкою) | TBD | <1/день | quality_history.margin_at_risk |
| pct_openai (без failover) | 100% | ≥95% | quality_history.pct_openai |
| Eval harness PASS-rate | 12/15 = 80% | 15/15 = 100% | run_regression.py |
| Покриття контентом (продукти) | 10/12 курсів | 12/12 | manual review |
| Протипоказання в RAG | 2/12 курсів | 12/12 | manual review |
| Час відповіді (latency) | 5-7 сек | <5 сек | logs.latency |
