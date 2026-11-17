FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils nodejs ca-certificates fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin worker
COPY --chown=worker:worker . ./

ENV HOME=/home/worker \
    PYTHONUNBUFFERED=1 \
    PROJECT_ROOT=/app

USER worker
CMD ["python", "main.py"]