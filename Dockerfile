FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN addgroup --system --gid 1000 app \
    && adduser --system --uid 1000 --ingroup app app \
    && mkdir -p /app/logs \
    && chown -R 1000:1000 /app

USER 1000:1000
