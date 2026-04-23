# CLAUDE.md — Project Instructions for AI

## Project Overview
EMET Telegram bot — AI sales coach for aesthetic medicine company.
RAG-based assistant with 8 modes, LMS system, daily quality monitoring.

## Key Files
- `main.py` — Bot core (~3300 lines): FSM, handlers, RAG, LLM failover, dialog_state integration
- `admin_panel.py` — Flask admin panel (~2300 lines): ~26 routes
- `prompts.py` + `prompts_v2.py` — System prompts (BASE із anti-sycophancy + scope discipline + STRICT-MODE)
- `classifier.py` — LLM classifier (19 intents) + product_canonical normalization + few-shot examples
- `dialog_state.py` — NEW (24.04): per-turn DialogState tracker (intent, product, comparison_target)
- `quality_monitor.py` — Daily quality analysis + LLM-judge sample + SD metrics
- `sync_manager.py` — Google Drive → ChromaDB sync + scope tagging + retry + admin notify
- `tests/regression_fixtures.json` + `tests/run_regression.py` — eval harness, 15 known-bad cases
- See `ARCHITECTURE.md` for full project structure

## Architecture
- **3-zone RAG**: products_openai (598) / competitors_openai (599) / kb_openai (470)
  - Each chunk: `product_canonical` (16 products) + `scope` (line/product/ingredient/protocol)
- **LLM failover**: GPT-4o → Gemini → Claude (logs.failover_depth = 0/1/2)
- **Docker**: 3 services (postgres, emet-bot, emet-admin), shared ./data volume
- **CI/CD**: GitHub Actions → SSH deploy to 49.12.81.83:33222
- **Quality layer (24.04)**: anti-sycophancy → knowledge_gaps → DialogState → product-locked RAG with scope labels → BASE+subtype prompt → LLM → log с failover_depth
- **Daily tasks**: 08:00 quality + heavy-correctors alert | 22:00 cost digest (quiet 22-09) | 02:00 cron backup | 03:00 TTL cleanup

## Critical Rules
1. Products index = ONLY our product data (LMS courses + sales docs)
2. Competitors index = ONLY comparison data (Competitors_MASTER files)
3. Never mix competitor specs with our product specs
4. Ellanse S = 18 months, M = 24 months (never round to years)
5. Exoxe storage = room temperature (never -20°C)
6. CE certification = ONLY injectable medical devices, not cosmeceuticals
7. "Vitaran Exosome" is NOT a product name — use full names from catalog
8. Pain Relief Technology + Ionization-Adjusted PN — correct terms for Vitaran. NEVER say "NaCl osmotic modulator"
9. Hidden courses (visible=false) = internal RAG data, NOT shown to managers in LMS. Never create visible courses without user approval
10. Never delete RAG chunks manually — add prompt rules instead. Deleting chunks causes data loss
11. RAG_K: products=12, competitors=8. Always search both and merge
12. After reindexing — bot needs restart OR VDB auto-refresh (mtime check) to see new data
13. ESSE manufacturer = Esse Skincare, South Africa (ПАР)
14. **Anti-sycophancy** (24.04): bot NEVER auto-agrees with manager corrections. Recheck via RAG → defend with citation OR honestly say "не знаю, до медвідділу + я зафіксую"
15. **Scope discipline** (24.04): scope=LINE chunk facts can be used but MUST be labeled "характеристика лінії в цілому" — never auto-attribute to specific product without product-scope chunk confirmation
16. **Side effects → med dept redirect** (23.04): never explain causes/treatment of побічки. Only canned response with reference to instruction + redirect to medical department
17. **Petaran + Ellansé combo**: same zone → 6 months interval, different zones → same procedure OK (override removed once Drive doc fixed)
18. **Eval harness before big prompt commits**: run `docker exec emet_bot_app python /app/tests/run_regression.py --no-generate` — if PASS-count drops vs prior, there's regression

## Commands
- Deploy bot: `ssh -i ~/.ssh/id_rsa -p 33222 emet@49.12.81.83 "cd /opt/emet-bot && git pull origin main && docker compose restart emet-bot"`
- Deploy admin: `ssh ... "cd /opt/emet-bot && docker rm -f emet_admin_panel && docker compose up -d --build emet-admin"`
- Bot logs: `ssh ... "docker logs emet_bot_app --tail=50"`
- DB access: `ssh ... "docker exec emet_postgres psql -U emet -d emet_bot"`

## Testing
- Always run `python -c "import py_compile; py_compile.compile('FILE', doraise=True)"` before committing
- Test RAG queries inside container: `docker exec emet_bot_app python -c "..."`
- Quality monitor: `docker exec emet_bot_app python /app/quality_monitor.py`
- **Regression harness** (run before big prompt/classifier commits):
  - Quick (~30 sec): `docker exec emet_bot_app python /app/tests/run_regression.py --no-generate`
  - Full (~3 min, ~$0.10): `docker exec emet_bot_app python /app/tests/run_regression.py`
  - One case: `docker exec emet_bot_app python /app/tests/run_regression.py --case <id>`
  - By category: `docker exec emet_bot_app python /app/tests/run_regression.py --category esse`

## User Preferences
- Speak Russian/Ukrainian mix
- Show screenshots from real Telegram bot as feedback
- The user is a Technical Product Owner, not a developer
- Always deploy after code changes (don't just commit)
- Quality report goes ONLY to ADMIN_ID, never to all users
- Don't invent product facts — only from course content or RAG
- Don't question product positioning from courses (if it says "premium" — it's premium)
- Never create LMS courses visible to managers without user approval — use visible=false for internal RAG data
- After ANY reindex/migration — run tests/test_knowledge_integrity.py to verify zero data loss
- Tests must match real bot behavior — bot caches VDB in memory, test scripts create fresh connections
- **Plan in 2 formats before non-trivial work** — користувач: понятним языком + ти: технічно. Wait for "добро" (з global CLAUDE.md правила)
- **Force rebuild контейнерів** після правок промптів/класифікатора: `docker compose up -d --build --force-recreate emet-bot` (інакше Docker може використати cache і не оновити код)
- Manager corrections auto-logged до knowledge_gaps — не задавати уточнюючі питання користувачу про "що саме треба було відповісти"; це для медвідділу через /gaps
