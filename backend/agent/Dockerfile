FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY irade_agent/ ./irade_agent/
RUN groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /sbin/nologin agent \
    && mkdir -p /run/irade-agent \
    && chown 10001:10001 /run/irade-agent \
    && chmod 0750 /run/irade-agent
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp AGENT_SOCKET=/run/irade-agent/agent.sock
USER 10001:10001
CMD ["python", "-m", "irade_agent.daemon"]
