FROM python:3.12-slim

# ffmpeg  — video processing and audio extraction
# curl    — needed by the NodeSource setup script
# fonts-liberation — Liberation fonts for Pillow thumbnail text rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS — required by yt-dlp-ejs (JS challenge solver)
# and bgutil-ytdlp-pot-provider (PO token generator, script mode)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD exec gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 300
