#!/usr/bin/env bash
#
# Grafana + Loki + Promtail, so application logs from every service can be
# queried by cluster, namespace and app name.
#
#   Promtail (DaemonSet)  tails /var/log/pods/**  ->  Loki (log store)
#                                                        ^
#                                          Grafana  -----+-- Loki datasource
#
# Run it after the app is up (deploy/helm/kind-up.sh): Grafana shares the app's
# host and TLS cert at https://upi-sim.local/grafana.
#
set -euo pipefail

NS=observability
REPO=grafana
CHART_REPO=https://grafana.github.io/helm-charts
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> namespace $NS"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

echo "==> helm repo $REPO"
helm repo add "$REPO" "$CHART_REPO" >/dev/null 2>&1 || true
helm repo update "$REPO" >/dev/null

echo "==> dashboard ConfigMap"
kubectl -n "$NS" create configmap upi-grafana-dashboards \
  --from-file="$HERE/dashboards" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Grafana's Ingress is on the same host as the app, so it needs the same TLS
# secret. Secrets are namespaced, so copy it from the app namespace.
echo "==> TLS secret upi-sim-tls"
if ! kubectl -n "$NS" get secret upi-sim-tls >/dev/null 2>&1; then
  kubectl -n upi get secret upi-sim-tls -o yaml \
    | sed "s/namespace: upi/namespace: $NS/" \
    | kubectl apply -f - >/dev/null
fi

echo "==> loki"
helm upgrade --install loki "$REPO/loki" -n "$NS" \
  -f "$HERE/values/loki.yaml" --wait --timeout 10m

echo "==> promtail"
helm upgrade --install promtail "$REPO/promtail" -n "$NS" \
  -f "$HERE/values/promtail.yaml" --wait --timeout 10m

echo "==> grafana"
helm upgrade --install grafana "$REPO/grafana" -n "$NS" \
  -f "$HERE/values/grafana.yaml" --wait --timeout 10m

echo
echo "Grafana:  https://upi-sim.local/grafana"
echo "Login:    admin / upi-admin"
echo "Dashboard 'UPI — Service Logs' has cluster / namespace / app / pod filters."
