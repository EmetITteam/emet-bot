#!/usr/bin/env python3
"""
Analyze today's conversation chains for routing/mode-switch issues.
Run on server: docker exec -it emet_bot_app python3 /app/analyze_chains.py
"""
import os, sys
import psycopg2
from datetime import datetime, date
from collections import defaultdict

DB_DSN = os.getenv("DATABASE_URL", "postgresql://emet:emet2026@localhost:5432/emet_bot")

def get_conn():
    return psycopg2.connect(DB_DSN)

def fetch_today_logs(conn, target_date=None):
    if target_date is None:
        target_date = date.today().isoformat()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, date, user_id, username, mode, question, answer
        FROM logs
        WHERE date::date = %s
        ORDER BY user_id, date
    """, (target_date,))
    return cur.fetchall()

ROLE_LABELS = {
    "kb": "База знань",
    "coach": "Sales Coach",
    "coach_free": "Coach Free",
    "coach_sos": "Coach SOS",
    "coach_objections": "Coach Objections",
    "coach_combo": "Coach Combo",
    "coach_certs": "Coach Certs",
    "coach_seasonal": "Coach Seasonal",
    "lms": "LMS",
    "unknown": "?",
}

# Keywords that should trigger certain modes
SCRIPT_KW = ["скрипт", "презентац", "покроково", "як продати", "алгоритм"]
FOLLOWUP_KW = ["ще", "додай", "розкажи більше", "а якщо", "а що", "чому", "хочу 5", "хочу 3",
               "дай 5", "дай 3", "наведи приклад", "розшир", "детальніш"]
OBJECTION_KW = ["дорого", "подумаю", "не зараз", "не потрібно", "в іншому місці", "заперечення"]
SOS_KW = ["відмова", "клієнт відмовляєть", "клієнт пішов", "розлючений", "скандал", "sos"]

def classify_expected_mode(question: str, prev_mode: str) -> str:
    q = question.lower()
    if any(k in q for k in SOS_KW):
        return "coach_sos"
    if any(k in q for k in OBJECTION_KW):
        return "coach_objections"
    if any(k in q for k in SCRIPT_KW):
        return "coach"
    if any(k in q for k in FOLLOWUP_KW):
        return prev_mode  # Should stay in same mode
    return None  # Unknown / cannot determine

def analyze_chains(logs):
    by_user = defaultdict(list)
    for row in logs:
        by_user[row[2]].append(row)  # group by user_id

    issues = []
    stats = {"total": len(logs), "users": len(by_user), "mode_switches": 0, "follow_up_broken": 0, "wrong_mode": 0}

    for user_id, rows in by_user.items():
        username = rows[0][3] or user_id
        prev_mode = None
        prev_question = None

        for i, row in enumerate(rows):
            log_id, ts, uid, uname, mode, question, answer = row
            ts_str = str(ts)[11:16]

            # Detect mode switches
            if prev_mode and mode != prev_mode:
                stats["mode_switches"] += 1

                # Check follow-up broken: prev question is follow-up but mode changed
                if prev_question and any(k in question.lower() for k in FOLLOWUP_KW):
                    stats["follow_up_broken"] += 1
                    issues.append({
                        "type": "FOLLOW-UP BROKE MODE",
                        "user": username,
                        "time": ts_str,
                        "log_id": log_id,
                        "from_mode": prev_mode,
                        "to_mode": mode,
                        "question": question[:120],
                        "answer_preview": answer[:100] if answer else "",
                    })

            # Check if expected mode matches actual
            expected = classify_expected_mode(question, prev_mode or mode)
            if expected and expected != mode:
                stats["wrong_mode"] += 1
                issues.append({
                    "type": "WRONG MODE",
                    "user": username,
                    "time": ts_str,
                    "log_id": log_id,
                    "expected": expected,
                    "actual": mode,
                    "question": question[:120],
                    "answer_preview": answer[:100] if answer else "",
                })

            prev_mode = mode
            prev_question = question

    return stats, issues, by_user

def print_report(stats, issues, by_user, target_date):
    print(f"\n{'='*70}")
    print(f"  АНАЛІЗ ДІАЛОГІВ — {target_date}")
    print(f"{'='*70}")
    print(f"  Всього запитів: {stats['total']}")
    print(f"  Унікальних користувачів: {stats['users']}")
    print(f"  Перемикань режимів: {stats['mode_switches']}")
    print(f"  Follow-up зламав режим: {stats['follow_up_broken']}")
    print(f"  Неправильний режим (визначений): {stats['wrong_mode']}")

    # Print full chains by user
    print(f"\n{'='*70}")
    print("  ЛАНЦЮЖКИ ДІАЛОГІВ ПО КОРИСТУВАЧАХ")
    print(f"{'='*70}")
    for user_id, rows in by_user.items():
        username = rows[0][3] or user_id
        print(f"\n  USER: {username} ({user_id}) — {len(rows)} повідомлень")
        print(f"  {'-'*60}")
        prev_mode = None
        for row in rows:
            log_id, ts, uid, uname, mode, question, answer = row
            ts_str = str(ts)[11:16]
            switch_flag = " <<< SWITCH" if prev_mode and mode != prev_mode else ""
            label = ROLE_LABELS.get(mode, mode)
            print(f"  [{ts_str}] [{label:18s}]{switch_flag}")
            print(f"    Q: {question[:110]}")
            if answer:
                # Show last part of answer (often contains "хочеш X варіантів?" type endings)
                a_tail = answer[-200:].replace('\n', ' ')
                print(f"    A(tail): ...{a_tail[:110]}")
            prev_mode = mode

    # Print issues
    if issues:
        print(f"\n{'='*70}")
        print(f"  ЗНАЙДЕНІ ПРОБЛЕМИ ({len(issues)})")
        print(f"{'='*70}")
        for i, iss in enumerate(issues, 1):
            print(f"\n  [{i}] {iss['type']}")
            print(f"      User: {iss['user']}  Time: {iss['time']}  log_id: {iss['log_id']}")
            if iss['type'] == "FOLLOW-UP BROKE MODE":
                print(f"      Режим: {iss['from_mode']} -> {iss['to_mode']}")
            elif iss['type'] == "WRONG MODE":
                print(f"      Очікувався: {iss['expected']}, отримано: {iss['actual']}")
            print(f"      Q: {iss['question']}")
            print(f"      A: {iss['answer_preview']}")
    else:
        print("\n  Явних проблем з маршрутизацією не виявлено автоматично.")
        print("  Перегляньте ланцюжки вище вручну.")

    print(f"\n{'='*70}\n")

def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    print(f"Connecting to DB... (date: {target_date})")
    try:
        conn = get_conn()
    except Exception as e:
        print(f"ERROR: Cannot connect to DB: {e}")
        sys.exit(1)

    logs = fetch_today_logs(conn, target_date)
    print(f"Fetched {len(logs)} log entries for {target_date}")

    if not logs:
        print("No logs found. Try: python3 analyze_chains.py 2026-03-23")
        # Show available dates
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT date::date FROM logs ORDER BY date::date DESC LIMIT 10")
        dates = [str(r[0]) for r in cur.fetchall()]
        print(f"Available dates: {dates}")
        conn.close()
        return

    stats, issues, by_user = analyze_chains(logs)
    print_report(stats, issues, by_user, target_date)
    conn.close()

if __name__ == "__main__":
    main()
