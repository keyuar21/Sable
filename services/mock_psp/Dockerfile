# syntax=docker/dockerfile:1.7
#
# upi-psp — payment orchestrator.
#
# Drives the payment through the switch and both banks, runs the settlement
# cycle and the reconciliation sweep, and speaks the confirmation prompt.
#
# Heavier than the other coordinator services because it shells out to `piper`
# for local text-to-speech, which pulls onnxruntime.
#
# Build from the REPOSITORY ROOT:
#   docker build -f services/mock_psp/Dockerfile -t upi-psp:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/mock_psp/requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/services \
    PATH="/opt/venv/bin:$PATH" \
    PIPER_DATA_DIR=/opt/models/piper

# espeak-ng is piper's phonemiser; libsndfile1 backs the wav writer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 upi \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin upi \
 && mkdir -p /app/services /data /etc/upi \
 && chown -R 10001:10001 /app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv

# The voice is baked in, not downloaded on boot: a pod that fetches a 60MB
# model on every restart fails the first time the network does.
COPY --chown=10001:10001 models/piper/ /opt/models/piper/

COPY --chown=10001:10001 services/supabase_db.py /app/services/
COPY --chown=10001:10001 services/mock_psp/ /app/services/mock_psp/

USER 10001:10001
EXPOSE 5001

ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "mock_psp.psp_service:app", "--host", "0.0.0.0", "--port", "5001"]

LABEL org.opencontainers.image.title="upi-psp" \
      org.opencontainers.image.description="UPI payment orchestrator with settlement and reconciliation"
