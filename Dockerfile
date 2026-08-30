# Algo ELD Alert Bot — container image (used by Railway / any container host).
FROM python:3.12-slim

# Don't buffer logs (so Railway shows them live), no .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app

# Fonts for the HOS ring-card image (slim base ships none -> text wouldn't render).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install deps first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Persistent state dir. On Railway, attach a Volume mounted at /data via the
# dashboard (the Dockerfile VOLUME instruction is NOT supported by Railway).
RUN mkdir -p /data

# Web service: scheduler runs on a background thread, dashboard serves $PORT.
CMD ["python", "main.py", "--serve"]
