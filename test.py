import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

FOLDER_ID = '1RBXHGXOIc2kkSAw-LqzLaRqEE3Ix7L-m'
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def get_all_files_recursive(service, folder_id):
    all_files = []
    page_token = None
    
    while True:
        # Добавляем page_token для получения ВСЕХ страниц списка файлов
        query = f"'{folder_id}' in parents and trashed = false"
        results = service.files().list(
            q=query, 
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token
        ).execute()
        
        items = results.get('files', [])
        for item in items:
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                # Если нашли папку — идем внутрь (рекурсия)
                all_files.extend(get_all_files_recursive(service, item['id']))
            else:
                all_files.append(item)
        
        page_token = results.get('nextPageToken')
        if not page_token:
            break
    return all_files

def run_full_test():
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('drive', 'v3', credentials=creds)

        print("--- Начинаю полное сканирование папки ---")
        files = get_all_files_recursive(service, FOLDER_ID)

        if not files:
            print("Файлы не найдены. Проверьте права доступа для сервисного аккаунта.")
        else:
            print(f"Итого найдено объектов: {len(files)}")
            for f in files:
                print(f"- {f['name']} | Тип: {f['mimeType']}")
                
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    run_full_test()