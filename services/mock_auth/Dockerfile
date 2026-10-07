# syntax=docker/dockerfile:1.7
#
# upi-auth — credential service.
#
# PIN hashes, WebAuthn passkeys, and voice biometrics: Resemblyzer for "who is
# speaking" and AASIST for "is this live or a recording". By far the heaviest
# image in the stack — torch alone is most of a gigabyte — which is exactly why
# every other service has its own slimmer Dockerfile.
#
# Build from the REPOSITORY ROOT:
#   docker build -f services/mock_auth/Dockerfile -t upi-auth:1.0.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore

# Some wheels in the audio stack still compile on arm64.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY services/mock_auth/requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/services \
    PATH="/opt/venv/bin:$PATH" \
    AASIST_MODEL_PATH=/opt/models/aasist.onnx \
    ECAPA_CACHE=/opt/models/ecapa \
    HF_HOME=/opt/models/ecapa \
    HUGGINGFACE_HUB_CACHE=/opt/models/ecapa

# libsndfile1 backs soundfile/librosa; libgomp1 is required by onnxruntime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 upi \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin upi \
 && mkdir -p /app/services /data /etc/upi \
 && chown -R 10001:10001 /app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv

# The anti-spoof model ships with the image.
COPY --chown=10001:10001 models/aasist.onnx /opt/models/aasist.onnx

# Pre-fetch ECAPA-TDNN at build time. A pod that downloads its speaker model on
# first request fails the first time the network does, and the first payment of
# the day would otherwise pay the download cost.
RUN mkdir -p /opt/models/ecapa \
 && python -c "import os; os.environ.setdefault('HF_HOME','/opt/models/ecapa'); \
from speechbrain.inference.speaker import EncoderClassifier; \
EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', \
savedir='/opt/models/ecapa/spkrec-ecapa', run_opts={'device':'cpu'})" \
 && chown -R 10001:10001 /opt/models

COPY --chown=10001:10001 services/supabase_db.py /app/services/
COPY --chown=10001:10001 services/mock_auth/ /app/services/mock_auth/

USER 10001:10001
EXPOSE 5005

ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "mock_auth.auth_service:app", "--host", "0.0.0.0", "--port", "5005"]

LABEL org.opencontainers.image.title="upi-auth" \
      org.opencontainers.image.description="PIN, WebAuthn and voice biometric authentication"
