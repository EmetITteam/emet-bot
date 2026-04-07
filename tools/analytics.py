"""
Аналитика бота EMET.
Запуск: python analytics.py
Спросит дату начала и конца, выведет сводку и сохранит Excel.
"""

import pandas as pd
from datetime import datetime, date
import db


def ask_date(prompt, default=None):
    while True:
        val = input(prompt).strip()
        if not val and default is not None:
            return default
        try:
            return datetime.strptime(val, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print("  Неверный формат. Введите дату как ГГГГ-ММ-ДД")


def run():
    print("\n=== Аналитика бота EMET ===\n")

    today = date.today().isoformat()
    date_from = ask_date(f"Дата С (ГГГГ-ММ-ДД), Enter = начало всего: ", default="2000-01-01")
    date_to   = ask_date(f"Дата ПО (ГГГГ-ММ-ДД), Enter = сегодня {today}: ", default=today)

    print(f"\nПериод: {date_from} — {date_to}\n")

    rows = db.query_dict(
        "SELECT * FROM logs WHERE date >= %s AND date <= %s ORDER BY date",
        (date_from + " 00:00:00", date_to + " 23:59:59"),
    )

    if not rows:
        print("За этот период записей нет.")
        return

    df = pd.DataFrame(rows)

    if df.empty:
        print("За этот период записей нет.")
        return

    # Заменяем None/NaN username на user_id
    df["username"] = df["username"].fillna("").astype(str)
    df["display"] = df.apply(
        lambda r: f"@{r['username']}" if r["username"] not in ("", "None", "nan") else f"id:{r['user_id']}",
        axis=1
    )

    total = len(df)
    print(f"Всего запросов: {total}")
    print(f"Уникальных пользователей: {df['user_id'].nunique()}\n")

    # --- 1. Активность по пользователям ---
    print("─" * 50)
    print("ТОП ПОЛЬЗОВАТЕЛЕЙ (по кол-ву запросов):")
    print("─" * 50)
    users = (
        df.groupby(["display", "user_id"])
        .agg(запросов=("question", "count"),
             последний=("date", "max"))
        .reset_index()
        .sort_values("запросов", ascending=False)
        .head(20)
    )
    users = users[["display", "запросов", "последний"]]
    users.columns = ["Пользователь", "Запросов", "Последний запрос"]
    print(users.to_string(index=False))

    # --- 2. Разбивка по режимам ---
    print("\n" + "─" * 50)
    print("ЗАПРОСЫ ПО РЕЖИМАМ:")
    print("─" * 50)
    modes = df["mode"].value_counts().reset_index()
    modes.columns = ["Режим", "Запросов"]
    print(modes.to_string(index=False))

    # --- 3. Использование AI движков ---
    print("\n" + "─" * 50)
    print("AI ДВИЖКИ (OpenAI / Google / Claude):")
    print("─" * 50)
    engines = df["ai_engine"].value_counts().reset_index()
    engines.columns = ["Движок", "Запросов"]
    print(engines.to_string(index=False))

    # --- 4. Найдено в базе vs не найдено ---
    print("\n" + "─" * 50)
    print("КАЧЕСТВО БАЗЫ ЗНАНИЙ:")
    print("─" * 50)
    found = df["found_in_db"].value_counts().reset_index()
    found.columns = ["found_in_db", "Кол-во"]
    found["Статус"] = found["found_in_db"].map({1: "✅ Найдено в базе", 0: "❌ Не найдено"})
    print(found[["Статус", "Кол-во"]].to_string(index=False))

    # --- 5. Самые частые вопросы ---
    print("\n" + "─" * 50)
    print("ТОП-20 ЧАСТЫХ ВОПРОСОВ:")
    print("─" * 50)
    top_q = df["question"].value_counts().head(20).reset_index()
    top_q.columns = ["Вопрос", "Кол-во"]
    for _, row in top_q.iterrows():
        q = str(row["Вопрос"])[:80]
        print(f"  {row['Кол-во']:>3}x  {q}")

    # --- 6. Активность по дням ---
    print("\n" + "─" * 50)
    print("АКТИВНОСТЬ ПО ДНЯМ:")
    print("─" * 50)
    df["day"] = df["date"].astype(str).str[:10]
    by_day = df.groupby("day").size().reset_index(name="запросов")
    by_day.columns = ["Дата", "Запросов"]
    print(by_day.to_string(index=False))

    # --- Сохранение в Excel ---
    filename = f"analytics_{date_from}_{date_to}.xlsx"
    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        df.drop(columns=["display", "day"], errors="ignore").to_excel(writer, sheet_name="Все запросы", index=False)
        users.to_excel(writer, sheet_name="Пользователи", index=False)
        modes.to_excel(writer, sheet_name="Режимы", index=False)
        top_q.to_excel(writer, sheet_name="Частые вопросы", index=False)
        by_day.to_excel(writer, sheet_name="По дням", index=False)

    print(f"\n✅ Excel сохранён: {filename}\n")


if __name__ == "__main__":
    run()
