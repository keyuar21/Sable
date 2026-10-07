# syntax=docker/dockerfile:1.7
#
# upi-ops-console
#
# Build from the REPOSITORY ROOT — this service imports shared modules from
# services/, so the context has to include them:
#
#   docker build -f services/ops_console/Dockerfile -t upi-ops-console:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/ops_console/requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/services \
    PATH="/opt/venv/bin:$PATH"

# uid/gid match podSecurityContext in deploy/helm/values/*.yaml
RUN groupadd --gid 10001 upi \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin upi \
 && mkdir -p /app/services /data /etc/upi \
 && chown -R 10001:10001 /app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv

# Only this service's code plus the shared modules it imports — nothing else.
COPY --chown=10001:10001 services/ledger.py      /app/services/
COPY --chown=10001:10001 services/ops_console/ /app/services/ops_console/

USER 10001:10001
EXPOSE 5010

# The Helm chart appends: -m uvicorn ops_console.ops_service:app --host 0.0.0.0 --port 5010
ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "ops_console.ops_service:app", "--host", "0.0.0.0", "--port", "5010"]

LABEL org.opencontainers.image.title="upi-ops-console" \
      org.opencontainers.image.description="Read-only back office — live journal, trial balance, conservation check"
