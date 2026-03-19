import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

"""
Запуск тестів + надсилання результатів адміну в Telegram.

Запуск (після деплою або після пересборки індексу):
    python tests/notify_results.py routing          # тільки routing
    python tests/notify_results.py rag              # тільки RAG smoke
    python tests/notify_results.py all              # всі тести
"""

import os
import subprocess
import httpx
from datetime import datetime

# Підключаємо .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")

TESTS = {
    "routing": {
        "label": "Routing (логіка режимів)",
        "script": "tests/test_routing.py",
    },
    "rag": {
        "label": "RAG Smoke (якість пошуку)",
        "script": "tests/rag_smoke.py",
    },
    "health": {
        "label": "Health Check (сервіси)",
        "script": "tests/health_check.py",
    },
}


def run_test(script: str) -> tuple[bool, str]:
    """Запускає скрипт, повертає (ok, output)."""
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def send_telegram(text: str):
    if not TOKEN or not ADMIN_ID:
        print("⚠️ TELEGRAM_TOKEN або ADMIN_ID не знайдено в .env")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Помилка надсилання в Telegram: {e}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "all":
        selected = list(TESTS.keys())
    elif mode in TESTS:
        selected = [mode]
    else:
        print(f"Невідомий режим: {mode}. Доступні: routing, rag, health, all")
        sys.exit(1)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    results = []
    all_ok = True

    for key in selected:
        cfg = TESTS[key]
        print(f"\n▶ Запуск: {cfg['label']}...")
        ok, output = run_test(cfg["script"])
        if not ok:
            all_ok = False
        icon = "✅" if ok else "❌"
        status = "PASSED" if ok else "FAILED"
        results.append((icon, cfg["label"], status, output))
        print(output)

    # Формуємо повідомлення для Telegram
    header = "✅ *Всі тести пройшли*" if all_ok else "❌ *Є проблеми в тестах*"
    lines = [f"{header}\n_{now}_\n"]

    for icon, label, status, output in results:
        lines.append(f"{icon} *{label}*: {status}")
        # Додаємо останній рядок виводу як деталь
        last_line = [l for l in output.splitlines() if l.strip()]
        if last_line:
            lines.append(f"  _{last_line[-1]}_")

    message = "\n".join(lines)
    send_telegram(message)
    print(f"\n{'='*50}")
    print("Результати надіслано адміну в Telegram.")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
