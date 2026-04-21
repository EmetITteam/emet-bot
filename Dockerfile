# Используем легкий образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# procps — для pgrep у healthcheck (не входить у python:slim)
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости и чистим кэш для уменьшения размера
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект в контейнер
COPY . .

# Создаем папку для базы данных, если её нет
RUN mkdir -p data

# Запускаем бота
CMD ["python", "main.py"]