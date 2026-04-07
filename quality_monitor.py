"""
quality_monitor.py — Daily quality monitoring for EMET Bot.

Analyses dialogs from the last 24 hours with two perspectives:
  1. METHODOLOGIST: factual accuracy, product knowledge, completeness
  2. TECH LEAD: RAG quality, prompt compliance, architecture issues

Run: docker exec emet_bot_app python /app/quality_monitor.py
Or via bot: auto-scheduled daily at 8:00 AM
"""
import os, sys, json, re, textwrap
from datetime import datetime, timedelta

sys.stdout = __import__('io').TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

import db

# ── Configuration ─────────────────────────────────────────────────────────────

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
HOURS_BACK = int(os.getenv("MONITOR_HOURS", "24"))

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
        "question, answer, tokens_in, tokens_out "
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


def build_report(dialogs, findings):
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

    # Build report
    report = build_report(dialogs, all_findings)
    print("\n" + report)

    return report, all_findings


if __name__ == "__main__":
    report, findings = run_monitor()
    if report:
        print("\n\nReport generated successfully.")
        # Save to file for reference
        with open("data/last_quality_report.txt", "w", encoding="utf-8") as f:
            f.write(report)
        print("Saved to data/last_quality_report.txt")
