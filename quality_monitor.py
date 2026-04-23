"""
quality_monitor.py — Daily quality monitoring for EMET Bot.

Analyses dialogs from the last 24 hours with two perspectives:
  1. METHODOLOGIST: factual accuracy, product knowledge, completeness
  2. TECH LEAD: RAG quality, prompt compliance, architecture issues

Run: docker exec emet_bot_app python /app/quality_monitor.py
Or via bot: auto-scheduled daily at 8:00 AM
"""
import os, sys, json, re, random, textwrap
from datetime import datetime, timedelta

# UTF-8 для Windows-терміналу. reconfigure безпечний при повторному виклику.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

import db

# ── Configuration ─────────────────────────────────────────────────────────────

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
HOURS_BACK = int(os.getenv("MONITOR_HOURS", "24"))
LLM_JUDGE_SAMPLE = int(os.getenv("MONITOR_LLM_SAMPLE", "10"))  # 0 = disable
LLM_JUDGE_MODEL = os.getenv("MONITOR_LLM_MODEL", "gpt-4o-mini")

# Known bad patterns — expand as new issues are found
BAD_PATTERNS = {
    "competitor_bleed": {
        "patterns": [
            r"CellResearch", r"Calicim", r"Сінгапур.*виробник",
        ],
        "severity": "P0",
        "description": "Competitor data presented as our product info",
    },
    "double_asterisks": {
        "patterns": [r"\*\*[^*]+\*\*"],
        "severity": "P2",
        "description": "**Double asterisks** — Telegram won't render",
    },
    "hallucinated_storage": {
        "patterns": [r"-20\s*[°ºC]", r"-20С", r"-20°"],
        "severity": "P0",
        "description": "Hallucinated storage temperature -20°C",
    },
    "osmotic_modulator": {
        "patterns": [r"осмотичн\w+ модулятор"],
        "severity": "P1",
        "description": "Wrong NaCl description (osmotic modulator)",
    },
    "vitaran_exosome_shortcut": {
        "patterns": [r"Vitaran [Ee]xosome(?!\s*[-–])"],
        "severity": "P1",
        "description": "Non-existent product name 'Vitaran Exosome'",
    },
    "rounded_duration": {
        "patterns": [
            r"Ellans[ée]\s+S.*(?:1\s+рік|1\s+року|близько року|~\s*1\s*рік)",
            r"Ellans[ée]\s+M.*(?:2\s+рок|близько 2\s*рок)",
        ],
        "severity": "P1",
        "description": "Rounded Ellanse duration (should be 18/24 months)",
    },
}

# Refusal phrases — not errors per se, but indicate RAG gaps
REFUSAL_PHRASES = [
    "немає інформації", "не знайдено", "немає цієї інформації",
    "не вказано", "уточніть у керівництва", "уточніть у технічній",
    "не містить даних", "відсутня інформація",
]

# Cross-sell map — check if bot recommended alternatives
CROSS_SELL_MAP = {
    "волосся": "IUSE HAIR",
    "алопеція": "IUSE HAIR",
    "пігментація": "Whitening",
    "кола під очима": "Tox Eye",
    "зволоження": "SKINBOOSTER",
}


def get_dialogs(hours_back=24):
    """Fetch dialogs from last N hours."""
    since = (datetime.now() - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.query_dict(
        "SELECT id, date, user_id, username, mode, model, found_in_db, "
        "question, answer, tokens_in, tokens_out, failover_depth "
        "FROM logs WHERE date >= %s ORDER BY id",
        (since,)
    )
    return rows


def analyze_dialog(dialog):
    """Analyze a single dialog for issues. Returns list of findings."""
    findings = []
    answer = dialog.get("answer", "") or ""
    question = dialog.get("question", "") or ""
    q_lower = question.lower()
    a_lower = answer.lower()
    d_id = dialog["id"]

    # 1. Check bad patterns
    for issue_key, issue in BAD_PATTERNS.items():
        for pattern in issue["patterns"]:
            if re.search(pattern, answer, re.IGNORECASE):
                findings.append({
                    "type": issue_key,
                    "severity": issue["severity"],
                    "description": issue["description"],
                    "dialog_id": d_id,
                    "match": re.search(pattern, answer, re.IGNORECASE).group()[:60],
                })
                break  # one match per issue type per dialog

    # 2. Check refusals
    refusal_count = sum(1 for phrase in REFUSAL_PHRASES if phrase in a_lower)
    if refusal_count > 0:
        findings.append({
            "type": "refusal",
            "severity": "INFO",
            "description": f"Refusal ({refusal_count} phrases)",
            "dialog_id": d_id,
            "match": question[:60],
        })

    # 3. Check missed cross-sell
    for trigger, product in CROSS_SELL_MAP.items():
        if trigger in q_lower and product.lower() not in a_lower:
            findings.append({
                "type": "cross_sell_miss",
                "severity": "P2",
                "description": f"Missed cross-sell: '{trigger}' → {product}",
                "dialog_id": d_id,
                "match": question[:60],
            })

    # 4. Check empty/very short answers
    if len(answer.strip()) < 50:
        findings.append({
            "type": "empty_answer",
            "severity": "P1",
            "description": "Very short answer (<50 chars)",
            "dialog_id": d_id,
            "match": answer[:60],
        })

    return findings


def detect_contradictions(dialogs):
    """Find contradictions: same user, opposite facts within 5 minutes."""
    contradictions = []
    by_user = {}
    for d in dialogs:
        uid = d["user_id"]
        by_user.setdefault(uid, []).append(d)

    for uid, user_dialogs in by_user.items():
        for i in range(len(user_dialogs) - 1):
            d1 = user_dialogs[i]
            d2 = user_dialogs[i + 1]
            a1 = (d1.get("answer") or "").lower()
            a2 = (d2.get("answer") or "").lower()

            # Check: "contains X" in one, "doesn't contain X" in other
            contains_patterns = re.findall(r"містить\s+(\w+)", a1)
            for ingredient in contains_patterns:
                if f"не містить {ingredient}" in a2 or f"відсутн" in a2 and ingredient in a2:
                    contradictions.append({
                        "type": "contradiction",
                        "severity": "P0",
                        "description": f"Contradiction: '{ingredient}' contains→doesn't contain",
                        "dialog_id": f"{d1['id']}→{d2['id']}",
                        "match": f"#{d1['id']}: містить / #{d2['id']}: не містить",
                    })

    return contradictions


def build_report(dialogs, findings, llm_summary=None):
    """Build structured report text."""
    # Stats
    total = len(dialogs)
    users = len(set(d["user_id"] for d in dialogs))
    modes = {}
    for d in dialogs:
        m = d.get("mode", "?")
        modes[m] = modes.get(m, 0) + 1
    refusals = sum(1 for f in findings if f["type"] == "refusal")
    p0 = [f for f in findings if f["severity"] == "P0"]
    p1 = [f for f in findings if f["severity"] == "P1"]
    p2 = [f for f in findings if f["severity"] == "P2"]

    # Cost
    total_in = sum(d.get("tokens_in", 0) or 0 for d in dialogs)
    total_out = sum(d.get("tokens_out", 0) or 0 for d in dialogs)
    cost_approx = (total_in * 2.5 + total_out * 10.0) / 1_000_000  # gpt-4o pricing

    # Refusal rate
    refusal_rate = round(refusals / total * 100) if total else 0

    lines = []
    lines.append(f"*Daily Quality Report*")
    lines.append(f"_{datetime.now().strftime('%d.%m.%Y %H:%M')}_")
    lines.append("")

    # KPI
    lines.append(f"*Статистика ({HOURS_BACK}h):*")
    lines.append(f"  Діалогів: {total} | Юзерів: {users}")
    mode_str = ", ".join(f"{k}:{v}" for k, v in sorted(modes.items()))
    lines.append(f"  Режими: {mode_str}")
    lines.append(f"  Tokens: {total_in:,}in + {total_out:,}out")
    lines.append(f"  Cost: ~${cost_approx:.2f}")
    lines.append("")

    # Quality score
    error_score = len(p0) * 2 + len(p1) * 0.5 + len(p2) * 0.1
    quality = max(1, round(10 - error_score, 1))
    emoji = "🟢" if quality >= 8 else "🟡" if quality >= 5 else "🔴"
    lines.append(f"*Якість: {emoji} {quality}/10*")
    lines.append(f"  Refusal rate: {refusal_rate}%")
    lines.append(f"  P0 (critical): {len(p0)}")
    lines.append(f"  P1 (important): {len(p1)}")
    lines.append(f"  P2 (minor): {len(p2)}")
    lines.append("")

    # Critical issues
    if p0:
        lines.append("*P0 — КРИТИЧНІ:*")
        for f in p0[:5]:
            lines.append(f"  #{f['dialog_id']} {f['description']}")
            lines.append(f"    `{f['match']}`")
        lines.append("")

    if p1:
        lines.append("*P1 — ВАЖЛИВІ:*")
        for f in p1[:5]:
            lines.append(f"  #{f['dialog_id']} {f['description']}")
        lines.append("")

    # Top refusal queries (methodologist perspective)
    refusal_findings = [f for f in findings if f["type"] == "refusal"]
    if refusal_findings:
        lines.append(f"*Питання без відповіді ({len(refusal_findings)}):*")
        for f in refusal_findings[:5]:
            lines.append(f"  #{f['dialog_id']}: {f['match']}")
        if len(refusal_findings) > 5:
            lines.append(f"  ...та ще {len(refusal_findings) - 5}")
        lines.append("")

    # Cross-sell misses
    cross_sell = [f for f in findings if f["type"] == "cross_sell_miss"]
    if cross_sell:
        lines.append(f"*Пропущений крос-сейл ({len(cross_sell)}):*")
        for f in cross_sell[:3]:
            lines.append(f"  #{f['dialog_id']}: {f['description']}")
        lines.append("")

    # LLM judge — semantic evaluation
    if llm_summary:
        lines.append(f"*LLM judge ({llm_summary['n']} діалогів, {LLM_JUDGE_MODEL}):*")
        lines.append(f"  Helpfulness:    {llm_summary['avg_helpfulness']}/10")
        lines.append(f"  Factual:        {llm_summary['avg_factual']}/10")
        lines.append(f"  Format:         {llm_summary['avg_format']}/10")
        lines.append(f"  Role awareness: {llm_summary['avg_role']}/10")
        if llm_summary["errors"]:
            lines.append(f"  ⚠️ Judge errors: {llm_summary['errors']}")
        if llm_summary["low_score"]:
            lines.append(f"  Низькі бали ({len(llm_summary['low_score'])}):")
            for r in llm_summary["low_score"][:3]:
                lines.append(f"    #{r['dialog_id']} ({r.get('issue', '?')[:80]})")
        lines.append("")

    # Recommendation
    if quality < 8:
        lines.append("*Рекомендації:*")
        if refusal_rate > 20:
            lines.append("  - RAG gap: багато відмов. Перевірте контент курсів.")
        if p0:
            lines.append("  - Є P0 помилки! Перевірте RAG-індекс на конкурентний bleed.")
        if cross_sell:
            lines.append("  - Додайте крос-сейл правила для пропущених показань.")

    return "\n".join(lines)


# ── LLM judge ─────────────────────────────────────────────────────────────────

JUDGE_PROMPT = """Ти — експерт з оцінки якості відповідей AI-помічника для менеджерів продажу косметичних препаратів EMET.

Оціни 1 відповідь бота за 4 критеріями (1-10):
1. helpfulness — чи відповідь реально допомагає менеджеру (а не просто заповнює простір шаблоном)
2. factual_accuracy — чи факти/цифри/назви коректні (без галюцинацій, плутанини варіантів продуктів)
3. format_compliance — чи дотриманий формат режиму (SOS-шаблон / INFO-секції / VERBATIM-цитування / FEEDBACK-визнання помилки)
4. role_awareness — чи бот діє як коуч (аналізує запит менеджера, ставить уточнення, реагує на скаргу) vs тупо генерує шаблон без аналізу

Також одне коротке речення з головною проблемою (або "OK" якщо все добре).

Відповідь — виключно валідний JSON:
{"helpfulness": <int>, "factual_accuracy": <int>, "format_compliance": <int>, "role_awareness": <int>, "issue": "<коротко>"}"""


def _judge_one(dialog):
    """Оцінка одного діалогу через LLM. Повертає dict зі score або {"error": "..."}."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY, timeout=30)
        question = (dialog.get("question") or "")[:1500]
        answer = (dialog.get("answer") or "")[:3000]
        mode = dialog.get("mode", "?")
        resp = client.chat.completions.create(
            model=LLM_JUDGE_MODEL,
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": f"MODE: {mode}\n\nЗАПИТ:\n{question}\n\nВІДПОВІДЬ БОТА:\n{answer}"}
            ]
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        return {"error": str(e)[:120]}


def llm_judge_sample(dialogs, sample_size=None):
    """LLM-based semantic judge на випадковій вибірці діалогів."""
    if sample_size is None:
        sample_size = LLM_JUDGE_SAMPLE
    if sample_size <= 0 or not dialogs:
        return []
    sample = random.sample(dialogs, min(sample_size, len(dialogs)))
    results = []
    for d in sample:
        score = _judge_one(d)
        results.append({"dialog_id": d["id"], "question": (d.get("question") or "")[:80], **score})
    return results


def _summarize_llm_scores(judge_results):
    """Середні бали + список low-score діалогів."""
    valid = [r for r in judge_results if "error" not in r]
    if not valid:
        return None
    avg = lambda key: round(sum(r.get(key, 0) for r in valid) / len(valid), 1)
    return {
        "n": len(valid),
        "errors": len(judge_results) - len(valid),
        "avg_helpfulness": avg("helpfulness"),
        "avg_factual": avg("factual_accuracy"),
        "avg_format": avg("format_compliance"),
        "avg_role": avg("role_awareness"),
        "avg_total": avg("helpfulness") + avg("factual_accuracy") + avg("format_compliance") + avg("role_awareness"),
        "low_score": [r for r in valid if min(r.get(k, 10) for k in ["helpfulness","factual_accuracy","format_compliance","role_awareness"]) <= 5],
    }


PREMIUM_PRODUCTS = ["ellans", "елансе", "vitaran", "вітаран", "iuse"]  # для margin_at_risk


def _compute_sd_metrics(dialogs, judge_results):
    """Sales Director метрики: correction_rate, mode_mismatch, margin_at_risk, model_distribution."""
    total = len(dialogs)
    if not total:
        return dict(correction_rate=0, mode_mismatch=0, margin_at_risk=0, pct_openai=100, pct_gemini=0, pct_claude=0)
    # correction_rate — за останні 24h з knowledge_gaps
    try:
        row = db.query("SELECT COUNT(*) FROM knowledge_gaps WHERE detected_at >= NOW() - INTERVAL '24 hours'", fetchone=True)
        corrections = row[0] if row else 0
        correction_rate = round(corrections / total * 100, 1)
    except Exception:
        correction_rate = 0
    # mode_mismatch — KB-режим перейшов у fallback (визначаємо за відповідями що не схожі на KB-стандарт)
    # Простий evristic: KB-режим але mode у запиту НЕ спрацював коректно — рахуємо діалоги в kb-режимі
    # де мав бути продуктовий запит. Точно це визначити складно без KB→Coach fallback логу,
    # тому беремо суррогат: KB-діалоги де відповідь >300 chars (KB-стандарт коротший) — fallback спрацював
    mode_mismatch = sum(
        1 for d in dialogs
        if (d.get("mode") == "kb" and len((d.get("answer") or "")) > 300)
    )
    # margin_at_risk — діалоги про преміум-продукти + низький LLM-judge score
    judged_low = {r["dialog_id"] for r in judge_results
                   if "error" not in r
                   and min(r.get(k, 10) for k in ["helpfulness", "factual_accuracy"]) <= 6}
    margin_at_risk = sum(
        1 for d in dialogs
        if d["id"] in judged_low and any(p in (d.get("question", "") or "").lower() for p in PREMIUM_PRODUCTS)
    )
    # model_distribution з failover_depth
    fd_counts = {0: 0, 1: 0, 2: 0}
    for d in dialogs:
        fd = d.get("failover_depth", 0) or 0
        fd_counts[fd] = fd_counts.get(fd, 0) + 1
    return dict(
        correction_rate=correction_rate,
        mode_mismatch=mode_mismatch,
        margin_at_risk=margin_at_risk,
        pct_openai=round(fd_counts[0] / total * 100, 1),
        pct_gemini=round(fd_counts[1] / total * 100, 1),
        pct_claude=round(fd_counts[2] / total * 100, 1),
    )


def _save_quality_history(dialogs, findings, llm_summary, judge_results=None):
    """Зберігає денний агрегований запис у quality_history з SD метриками. Не падає при помилці."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        total = len(dialogs)
        refusals = sum(1 for f in findings if f["type"] == "refusal")
        p0 = sum(1 for f in findings if f["severity"] == "P0")
        p1 = sum(1 for f in findings if f["severity"] == "P1")
        refusal_rate = round(refusals / total * 100, 1) if total else 0
        total_in = sum(d.get("tokens_in", 0) or 0 for d in dialogs)
        total_out = sum(d.get("tokens_out", 0) or 0 for d in dialogs)
        cost = round((total_in * 2.5 + total_out * 10.0) / 1_000_000, 2)
        sd = _compute_sd_metrics(dialogs, judge_results or [])
        params_base = (today, total)
        params_sd = (sd["correction_rate"], sd["mode_mismatch"], sd["margin_at_risk"],
                     sd["pct_openai"], sd["pct_gemini"], sd["pct_claude"])
        if llm_summary:
            db.execute(
                "INSERT INTO quality_history (date, dialogs_total, dialogs_judged, "
                "avg_helpfulness, avg_factual, avg_format, avg_role, "
                "p0_count, p1_count, refusal_rate, cost_usd, "
                "correction_rate, mode_mismatch_count, margin_at_risk, pct_openai, pct_gemini, pct_claude) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (date) DO UPDATE SET "
                "dialogs_total=EXCLUDED.dialogs_total, dialogs_judged=EXCLUDED.dialogs_judged, "
                "avg_helpfulness=EXCLUDED.avg_helpfulness, avg_factual=EXCLUDED.avg_factual, "
                "avg_format=EXCLUDED.avg_format, avg_role=EXCLUDED.avg_role, "
                "p0_count=EXCLUDED.p0_count, p1_count=EXCLUDED.p1_count, "
                "refusal_rate=EXCLUDED.refusal_rate, cost_usd=EXCLUDED.cost_usd, "
                "correction_rate=EXCLUDED.correction_rate, mode_mismatch_count=EXCLUDED.mode_mismatch_count, "
                "margin_at_risk=EXCLUDED.margin_at_risk, pct_openai=EXCLUDED.pct_openai, "
                "pct_gemini=EXCLUDED.pct_gemini, pct_claude=EXCLUDED.pct_claude",
                (today, total, llm_summary["n"],
                 llm_summary["avg_helpfulness"], llm_summary["avg_factual"],
                 llm_summary["avg_format"], llm_summary["avg_role"],
                 p0, p1, refusal_rate, cost, *params_sd)
            )
        else:
            db.execute(
                "INSERT INTO quality_history (date, dialogs_total, dialogs_judged, "
                "p0_count, p1_count, refusal_rate, cost_usd, "
                "correction_rate, mode_mismatch_count, margin_at_risk, pct_openai, pct_gemini, pct_claude) "
                "VALUES (%s,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (date) DO UPDATE SET dialogs_total=EXCLUDED.dialogs_total, "
                "p0_count=EXCLUDED.p0_count, p1_count=EXCLUDED.p1_count, "
                "refusal_rate=EXCLUDED.refusal_rate, cost_usd=EXCLUDED.cost_usd, "
                "correction_rate=EXCLUDED.correction_rate, mode_mismatch_count=EXCLUDED.mode_mismatch_count, "
                "margin_at_risk=EXCLUDED.margin_at_risk, pct_openai=EXCLUDED.pct_openai, "
                "pct_gemini=EXCLUDED.pct_gemini, pct_claude=EXCLUDED.pct_claude",
                (today, total, p0, p1, refusal_rate, cost, *params_sd)
            )
    except Exception as e:
        print(f"_save_quality_history error: {e}")


def run_monitor():
    """Main entry point."""
    print(f"Quality Monitor: analyzing last {HOURS_BACK}h of dialogs...")

    dialogs = get_dialogs(HOURS_BACK)
    if not dialogs:
        print("No dialogs found.")
        return None, "No dialogs"

    print(f"Found {len(dialogs)} dialogs")

    # Analyze each dialog
    all_findings = []
    for d in dialogs:
        findings = analyze_dialog(d)
        all_findings.extend(findings)

    # Detect contradictions
    contradictions = detect_contradictions(dialogs)
    all_findings.extend(contradictions)

    print(f"Findings: {len(all_findings)} total")

    # Knowledge integrity check
    try:
        from tests.test_knowledge_integrity import run_integrity_check
        integrity_ok, integrity_report = run_integrity_check(verbose=False)
        if not integrity_ok:
            all_findings.append({
                "type": "knowledge_loss",
                "severity": "P0",
                "description": "Knowledge integrity check FAILED — data loss detected",
                "dialog_id": "system",
                "match": integrity_report[:200],
            })
    except Exception as e:
        print(f"Integrity check error: {e}")

    # LLM judge на випадковій вибірці
    print(f"LLM judge: оцінка {min(LLM_JUDGE_SAMPLE, len(dialogs))} діалогів...")
    judge_results = llm_judge_sample(dialogs)
    llm_summary = _summarize_llm_scores(judge_results)
    if llm_summary and llm_summary["low_score"]:
        for r in llm_summary["low_score"]:
            all_findings.append({
                "type": "llm_low_score", "severity": "P1",
                "description": f"LLM judge: {r.get('issue', 'low score')}",
                "dialog_id": r["dialog_id"], "match": r["question"][:80],
            })

    # Зберегти агрегат у quality_history (для тренду)
    _save_quality_history(dialogs, all_findings, llm_summary, judge_results)

    # Build report
    report = build_report(dialogs, all_findings, llm_summary)
    print("\n" + report)

    return report, all_findings


def run_monitor_safe():
    """Same as run_monitor but without print() — safe for async tasks and background threads."""
    dialogs = get_dialogs(HOURS_BACK)
    if not dialogs:
        return None, []

    all_findings = []
    for d in dialogs:
        all_findings.extend(analyze_dialog(d))
    all_findings.extend(detect_contradictions(dialogs))

    try:
        from tests.test_knowledge_integrity import run_integrity_check
        integrity_ok, integrity_report = run_integrity_check(verbose=False)
        if not integrity_ok:
            all_findings.append({
                "type": "knowledge_loss", "severity": "P0",
                "description": "Knowledge integrity check FAILED",
                "dialog_id": "system", "match": integrity_report[:200],
            })
    except Exception:
        pass

    judge_results = llm_judge_sample(dialogs)
    llm_summary = _summarize_llm_scores(judge_results)
    if llm_summary and llm_summary["low_score"]:
        for r in llm_summary["low_score"]:
            all_findings.append({
                "type": "llm_low_score", "severity": "P1",
                "description": f"LLM judge: {r.get('issue', 'low score')}",
                "dialog_id": r["dialog_id"], "match": r["question"][:80],
            })

    _save_quality_history(dialogs, all_findings, llm_summary, judge_results)

    report = build_report(dialogs, all_findings, llm_summary)
    return report, all_findings


if __name__ == "__main__":
    report, findings = run_monitor()
    if report:
        print("\n\nReport generated successfully.")
        # Save to file for reference
        with open("data/last_quality_report.txt", "w", encoding="utf-8") as f:
            f.write(report)
        print("Saved to data/last_quality_report.txt")
