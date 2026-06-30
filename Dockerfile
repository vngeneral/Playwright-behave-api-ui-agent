# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────
# Stage 1: base image with Python + system deps
# ─────────────────────────────────────────────
FROM python:3.12-slim AS base

# Playwright needs these system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl ca-certificates gnupg \
        # Playwright Chromium deps
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        # Allure CLI deps
        openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Install Allure CLI
ARG ALLURE_VERSION=2.27.0
RUN wget -qO /tmp/allure.tgz \
        "https://github.com/allure-framework/allure2/releases/download/${ALLURE_VERSION}/allure-${ALLURE_VERSION}.tgz" \
    && tar -xzf /tmp/allure.tgz -C /opt \
    && ln -s "/opt/allure-${ALLURE_VERSION}/bin/allure" /usr/local/bin/allure \
    && rm /tmp/allure.tgz

WORKDIR /app

# ─────────────────────────────────────────────
# Stage 2: install Python deps + Playwright browsers
# ─────────────────────────────────────────────
FROM base AS deps

COPY resources/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium --with-deps \
    && playwright install firefox --with-deps

# ─────────────────────────────────────────────
# Stage 3: runtime image
# ─────────────────────────────────────────────
FROM deps AS runtime

COPY . .

# Default environment
ENV PYTHONUNBUFFERED=1 \
    HEADLESS=True \
    BROWSER=chromium \
    ENV=dev

# Reports are mounted as a volume at runtime
VOLUME ["/app/reports"]

ENTRYPOINT ["python", "run_tests.py"]
CMD ["--headless", "--parallel"]
