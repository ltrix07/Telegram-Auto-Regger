FROM python:3.10-slim

# Устанавливаем adb и curl (curl нужен для скачивания докера)
RUN apt-get update && apt-get install -y adb curl && rm -rf /var/lib/apt/lists/*

# Устанавливаем Docker CLI и Docker Compose
RUN curl -fsSL https://get.docker.com | sh
RUN curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose && chmod +x /usr/local/bin/docker-compose

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p sessions logs debug data

ENTRYPOINT ["python", "cli_main.py"]