# CLAUDE.md — Project Instructions for AI

## Project Overview
EMET Telegram bot — AI sales coach for aesthetic medicine company.
RAG-based assistant with 8 modes, LMS system, daily quality monitoring.

## Key Files
- `main.py` — Bot core (3267 lines): FSM, handlers, RAG, LLM failover
- `admin_panel.py` — Flask admin panel (2208 lines): 23 routes
- `prompts.py` — System prompts (480 lines): PROMPT_COACH is the main one
- `quality_monitor.py` — Daily quality analysis
- `sync_manager.py` — Google Drive → ChromaDB sync
- See `ARCHITECTURE.md` for full project structure

## Architecture
- **3-zone RAG**: products_openai (our data) / competitors_openai (comparisons) / kb_openai (HR docs)
- **LLM failover**: GPT-4o → Gemini 2.0 Flash → Claude Sonnet
- **Docker**: 3 services (postgres, emet-bot, emet-admin), shared ./data volume
- **CI/CD**: GitHub Actions → SSH deploy to 49.12.81.83:33222

## Critical Rules
1. Products index = ONLY our product data (LMS courses + sales docs)
2. Competitors index = ONLY comparison data (Competitors_MASTER files)
3. Never mix competitor specs with our product specs
4. Ellanse S = 18 months, M = 24 months (never round to years)
5. Exoxe storage = room temperature (never -20°C)
6. CE certification = ONLY injectable medical devices, not cosmeceuticals
7. "Vitaran Exosome" is NOT a product name — use full names from catalog

## Commands
- Deploy bot: `ssh -i ~/.ssh/id_rsa -p 33222 emet@49.12.81.83 "cd /opt/emet-bot && git pull origin main && docker compose restart emet-bot"`
- Deploy admin: `ssh ... "cd /opt/emet-bot && docker rm -f emet_admin_panel && docker compose up -d --build emet-admin"`
- Bot logs: `ssh ... "docker logs emet_bot_app --tail=50"`
- DB access: `ssh ... "docker exec emet_postgres psql -U emet -d emet_bot"`

## Testing
- Always run `python -c "import py_compile; py_compile.compile('FILE', doraise=True)"` before committing
- Test RAG queries inside container: `docker exec emet_bot_app python -c "..."`
- Quality monitor: `docker exec emet_bot_app python /app/quality_monitor.py`

## User Preferences
- Speak Russian/Ukrainian mix
- Show screenshots from real Telegram bot as feedback
- The user is a Technical Product Owner, not a developer
- Always deploy after code changes (don't just commit)
- Quality report goes ONLY to ADMIN_ID, never to all users
- Don't invent product facts — only from course content or RAG
- Don't question product positioning from courses (if it says "premium" — it's premium)
