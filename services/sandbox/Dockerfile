# syntax=docker/dockerfile:1.7
#
# upi-code-sandbox — the locked runner pod for Ops MCP Code Mode.
#
# Hardening is layered: non-root UID 10001, readOnlyRootFilesystem + /tmp
# tmpfs + /dev/shm in the pod spec, no service account token, no probes that
# need writes, and a NetworkPolicy (deploy/k8s/sandbox-lockdown-netpol.yaml)
# whose egress whitelist is Loki :3100 and DNS only. pandas is baked into the
# image so analysis runs never need the network for packages.
#
# Build from the REPOSITORY ROOT:
#   docker build -f services/sandbox/Dockerfile -t upi-code-sandbox:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/sandbox/requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/services \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
 && mkdir -p /app/services /tmp/work \
 && chown -R 10001:10001 /app /tmp/work

WORKDIR /tmp/work
COPY --from=build /opt/venv /opt/venv
COPY --chown=10001:10001 services/sandbox/sandbox_worker.py  /app/services/sandbox_worker.py
COPY --chown=10001:10001 services/sandbox/sandbox_service.py /app/services/sandbox_service.py

USER 10001:10001
EXPOSE 8080

# The Helm chart appends its own args: -m uvicorn sandbox_service:app --host 0.0.0.0 --port 8080
ENTRYPOINT ["python"]

LABEL org.opencontainers.image.title="upi-code-sandbox" \
      org.opencontainers.image.description="Isolated analysis runner for Ops MCP (Code Mode)"
