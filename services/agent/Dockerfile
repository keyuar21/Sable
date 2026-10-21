# syntax=docker/dockerfile:1.7
#
# upi-agent — the Unified Agent Service. Customer tools are in-process (they
# import services/mcp/customer_mcp and talk to the banking microservices over
# HTTP); ops tooling dispatches to the ai-sandbox pod. LLM calls go to the
# NVIDIA NIM endpoint — the key arrives at runtime from the nim-api secret,
# never inside the image.
#
# Build from the REPOSITORY ROOT:
#   docker build -f services/agent/Dockerfile -t upi-agent:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/agent/requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/services \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --gid 10001 upi \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin upi \
 && mkdir -p /app/services \
 && chown -R 10001:10001 /app

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=10001:10001 services/mcp/           /app/services/mcp/
COPY --chown=10001:10001 services/agent/         /app/services/agent/

USER 10001:10001
EXPOSE 5008

# The Helm chart appends its own args: -m uvicorn agent.agent_service:app --host 0.0.0.0 --port 5008
ENTRYPOINT ["python"]

LABEL org.opencontainers.image.title="upi-agent" \
      org.opencontainers.image.description="Unified agent: customer MCP (voice banking) + ops MCP (code mode)"
