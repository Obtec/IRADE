# AGDD control-plane services: api / llm-gateway / research / scheduler / analytics.
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt requirements-agdd.txt ./
RUN pip install --no-cache-dir -r requirements-agdd.txt
COPY irade_agent/ ./irade_agent/
RUN groupadd --system --gid 10001 agdd \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /sbin/nologin agdd
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
# Role is chosen by the compose service: python -m irade_agent.agdd.<api|llm_gateway|research_service|scheduler|analytics>
CMD ["python", "-m", "irade_agent.agdd.api"]
