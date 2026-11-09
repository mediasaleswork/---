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

# Verify the same Linux Chromium and code that will run on Railway. These
# fixtures are local; no Telegram/Google/OpenAI credentials or live calls.
COPY tests ./tests
RUN PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()') \
    python -m unittest discover -s tests -q

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "deploy/railway_entrypoint.py"]

