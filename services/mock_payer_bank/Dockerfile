# syntax=docker/dockerfile:1.7
#
# upi-payer-bank
#
# Build from the REPOSITORY ROOT — this service imports shared modules from
# services/, so the context has to include them:
#
#   docker build -f services/mock_payer_bank/Dockerfile -t upi-payer-bank:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/mock_payer_bank/requirements.txt /tmp/
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
COPY --chown=10001:10001 services/supabase_db.py /app/services/
COPY --chown=10001:10001 services/ledger.py      /app/services/
COPY --chown=10001:10001 services/mock_payer_bank/ /app/services/mock_payer_bank/

USER 10001:10001
EXPOSE 5003

# The Helm chart appends: -m uvicorn mock_payer_bank.bank_service:app --host 0.0.0.0 --port 5003
ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "mock_payer_bank.bank_service:app", "--host", "0.0.0.0", "--port", "5003"]

LABEL org.opencontainers.image.title="upi-payer-bank" \
      org.opencontainers.image.description="Remitter bank — payer accounts and the payer-side ledger"
