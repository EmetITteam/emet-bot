import sqlite3
import pandas as pd # Нужно будет установить: pip install pandas openpyxl

def export_to_excel():
    try:
        conn = sqlite3.connect('data/bot_usage.db')
        # Читаем данные из базы прямо в таблицу Pandas
        df = pd.read_sql_query("SELECT * FROM logs", conn)
        conn.close()

        # Сохраняем в Excel
        report_name = f"report_emet_bot_{pd.Timestamp.now().strftime('%Y-%m-%d')}.xlsx"
        df.to_excel(report_name, index=False)
        print(f"✅ Отчет успешно выгружен в файл: {report_name}")
    except Exception as e:
        print(f"Ошибка выгрузки: {e}")

if __name__ == "__main__":
    export_to_excel()