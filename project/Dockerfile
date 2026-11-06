FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml config.example.yaml ./
COPY partner_monitor ./partner_monitor
COPY deploy/railway_entrypoint.py ./deploy/railway_entrypoint.py
RUN pip install --no-cache-dir --no-deps .

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "deploy/railway_entrypoint.py"]

