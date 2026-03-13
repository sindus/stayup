FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

WORKDIR /app

COPY check_changelog.py .
COPY tests/ tests/

ENTRYPOINT ["python", "check_changelog.py"]
