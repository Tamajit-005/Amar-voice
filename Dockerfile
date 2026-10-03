FROM python:3.10-slim

# ffmpeg is required for uploads (pydub transcode) and MP3 downloads
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY test_suite.py ./test_suite.py
# NOTE: assets/ (voice clips) is gitignored and created at runtime via makedirs;
# do NOT COPY it — the build fails on fresh clones where the dir doesn't exist.

ENV PORT=8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
