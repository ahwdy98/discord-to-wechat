FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBHOOK_HOST=0.0.0.0 \
    WEBHOOK_PORT=8080 \
    WEBHOOK_DATA_DIR=/data

WORKDIR /app
COPY app.py /app/app.py

RUN mkdir -p /data

EXPOSE 8080
CMD ["python", "/app/app.py"]
