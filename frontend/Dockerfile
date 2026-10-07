# syntax=docker/dockerfile:1.7
#
# upi-frontend — the customer app, static files behind nginx.
#
# API base URLs are NOT baked in. config.js is replaced at runtime by the copy
# mounted from the Helm ConfigMap, so one image serves every environment; the
# config.js in this directory is only the local-development default.
#
# This one builds from its own directory:
#   docker build -f frontend/Dockerfile -t upi-frontend:1.0.0 frontend

FROM nginxinc/nginx-unprivileged:1.27-alpine

USER root
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --chown=101:101 index.html app.js styles.css voicebio.js webauthn.js config.js \
     /usr/share/nginx/html/
USER 101

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1

LABEL org.opencontainers.image.title="upi-frontend" \
      org.opencontainers.image.description="Voice UPI simulator customer app"
