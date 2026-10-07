# syntax=docker/dockerfile:1.7
#
# upi-recharge
#
# Build from the REPOSITORY ROOT — this service imports shared modules from
# services/, so the context has to include them:
#
#   docker build -f services/mock_recharge/Dockerfile -t upi-recharge:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/mock_recharge/requirements.txt /tmp/
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

# This service imports nothing from services/ — only its own package.
COPY --chown=10001:10001 services/mock_recharge/ /app/services/mock_recharge/

USER 10001:10001
EXPOSE 5007

# The Helm chart appends: -m uvicorn mock_recharge.recharge_service:app --host 0.0.0.0 --port 5007
ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "mock_recharge.recharge_service:app", "--host", "0.0.0.0", "--port", "5007"]

LABEL org.opencontainers.image.title="upi-recharge" \
      org.opencontainers.image.description="Recharge — carrier lookup and prepaid plans"
