# Isolated Agent Worker sandbox: git + test toolchain + Playwright (VLM screening). No internet route in compose.
FROM python:3.13-slim
ARG INSTALL_PLAYWRIGHT=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-agdd.txt ./
RUN pip install --no-cache-dir -r requirements-agdd.txt pytest==8.4.1 \
    && if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
         pip install --no-cache-dir playwright==1.64.1 \
         && PLAYWRIGHT_BROWSERS_PATH=/ms-playwright playwright install --with-deps chromium \
         && rm -rf /var/lib/apt/lists/*; fi
COPY irade_agent/ ./irade_agent/
RUN groupadd --system --gid 10001 agdd \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /sbin/nologin agdd \
    && git config --system --add safe.directory '*'
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    REPO_ROOT=/workspace WORKER_SQLITE=/tmp/agdd-worker.sqlite
USER 10001:10001
CMD ["python", "-m", "irade_agent.agdd.worker"]
