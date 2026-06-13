# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8081

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 4 --timeout 90 server:APP"]
