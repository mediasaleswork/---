FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY sales_agent ./sales_agent
COPY tests ./tests
RUN python -m unittest discover -s tests -q
CMD ["python", "-m", "sales_agent", "run"]
