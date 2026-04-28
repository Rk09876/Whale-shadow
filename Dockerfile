FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

WORKDIR /code

# System deps for httpx[http2] (h2/hpack are pure-python, but we keep curl
# and tzdata for ops convenience).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl tzdata && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persistent storage for the SQLite db. Render mounts a disk at /code/data
# when configured via render.yaml.
RUN mkdir -p /code/data
VOLUME ["/code/data"]

EXPOSE 8000

# Render passes $PORT
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
