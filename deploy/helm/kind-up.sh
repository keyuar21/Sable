#!/usr/bin/env bash
#
# Bring the whole UPI simulator up on a local kind cluster, Ingress included.
#
# Result:
#   http://upi-sim.local              -> frontend
#   http://api.upi-sim.local/psp      -> PSP
#   http://api.upi-sim.local/switch   -> NPCI switch
#   http://api.upi-sim.local/payer    -> payer bank
#   http://api.upi-sim.local/payee    -> payee bank
#   http://api.upi-sim.local/auth     -> auth
#   http://api.upi-sim.local/recharge -> recharge
#
# Usage:
#   deploy/helm/kind-up.sh            # recreate the cluster from scratch, then deploy
#   deploy/helm/kind-up.sh --keep     # reuse the cluster if it already exists
#
# The equivalent Makefile targets are `make images`, `make load`, `make install`
# (run from deploy/helm). This script spells the steps out so they can be read,
# copied, or run end to end.
#
set -euo pipefail

CLUSTER="${CLUSTER:-upi-helm-test}"
NS="${NS:-upi}"
TAG="${TAG:-1.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELM_DIR="$ROOT/deploy/helm"

SERVICES="switch auth payer-bank payee-bank psp recharge ops-console frontend"
IMAGES="upi-switch upi-payer-bank upi-payee-bank upi-ops-console upi-psp upi-auth upi-recharge upi-frontend"

# ── 1. Cluster ──────────────────────────────────────────────────────────────
# kind-config.yaml publishes the node's 80/443 on the host and labels the node
# ingress-ready=true. Both are required for ingress-nginx to be reachable:
# without the port mappings http://upi-sim.local hits nothing, and without the
# label the controller Pod stays Pending (its manifest selects on that label).
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  if [[ "${1:-}" != "--keep" ]]; then
    echo "==> deleting existing kind cluster '$CLUSTER'"
    kind delete cluster --name "$CLUSTER"
  fi
fi
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "==> creating kind cluster '$CLUSTER'"
  kind create cluster --config "$HELM_DIR/kind-config.yaml"
fi

# ── 2. Build the images ─────────────────────────────────────────────────────
# Each service owns its Dockerfile. The Python services build from the repo root
# because they import shared modules from services/; the frontend builds from
# its own directory. upi-auth is the slow one (torch + speechbrain + the ECAPA
# model fetched at build time).
echo "==> building images"
docker build -f "$ROOT/services/mock_switch/Dockerfile"     -t upi-switch:$TAG      "$ROOT"
docker build -f "$ROOT/services/mock_payer_bank/Dockerfile" -t upi-payer-bank:$TAG  "$ROOT"
docker build -f "$ROOT/services/mock_payee_bank/Dockerfile" -t upi-payee-bank:$TAG  "$ROOT"
docker build -f "$ROOT/services/ops_console/Dockerfile"     -t upi-ops-console:$TAG "$ROOT"
docker build -f "$ROOT/services/mock_psp/Dockerfile"        -t upi-psp:$TAG         "$ROOT"
docker build -f "$ROOT/services/mock_auth/Dockerfile"       -t upi-auth:$TAG        "$ROOT"
docker build -f "$ROOT/services/mock_recharge/Dockerfile"   -t upi-recharge:$TAG    "$ROOT"
docker build -f "$ROOT/frontend/Dockerfile"                 -t upi-frontend:$TAG    "$ROOT/frontend"

# ── 3. Load them into the cluster ───────────────────────────────────────────
# kind runs its own containerd, so host-built images are invisible to it until
# loaded. Skip this and pods keep running an older image with the same tag --
# imagePullPolicy: IfNotPresent matches the tag and never re-checks the host.
echo "==> loading images into '$CLUSTER'"
for i in $IMAGES; do
  kind load docker-image "$i:$TAG" --name "$CLUSTER"
done

# ── 4. Namespace + secrets ──────────────────────────────────────────────────
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

# recharge loads this via envFrom. If it is missing the pod is stuck in
# CreateContainerConfigError. Empty is fine: its plan source is "builtin".
# upi-supabase is the same story for the banks, switch and psp -- the Supabase
# mirror is reporting-only, so an empty secret is enough to boot. Fill it from
# .env if you want the mirror populated.
for secret in upi-supabase upi-recharge-keys; do
  kubectl -n "$NS" get secret "$secret" >/dev/null 2>&1 || \
    kubectl -n "$NS" create secret generic "$secret"
done

# TLS for the Ingress. Voice ID needs a secure context: browsers only expose
# getUserMedia (the microphone) on https:// -- http://upi-sim.local is not one,
# so the recorder fails even though the page loads. A self-signed cert is enough
# to satisfy that; the browser warns once per host. Swap in mkcert/an internal CA
# if you want the warning gone.
CERT_DIR="$(mktemp -d)"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" \
  -subj "/CN=upi-sim.local" \
  -addext "subjectAltName=DNS:upi-sim.local,DNS:api.upi-sim.local"
kubectl -n "$NS" create secret tls upi-sim-tls \
  --cert="$CERT_DIR/tls.crt" --key="$CERT_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -rf "$CERT_DIR"

# ── 5. ingress-nginx ────────────────────────────────────────────────────────
# The kind flavour binds hostPort 80/443 and selects node ingress-ready=true.
echo "==> installing ingress-nginx"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
kubectl -n ingress-nginx wait --for=condition=ready pod \
  -l app.kubernetes.io/component=controller --timeout=300s

# ── 6. Deploy every service ─────────────────────────────────────────────────
# Order matters on a cold start: switch and auth first, then the banks, then
# psp. Each values file carries its own Ingress -- the API services are
# published under api.upi-sim.local with a path prefix and a rewrite that strips
# it (see the ingress: block in each values/*.yaml).
echo "==> installing releases"
for s in $SERVICES; do
  echo "  -> upi-$s"
  helm upgrade --install "upi-$s" "$HELM_DIR/upi-service" \
    -f "$HELM_DIR/values/$s.yaml" -n "$NS"
done

# ── 7. Host names ───────────────────────────────────────────────────────────
# The Ingress dispatches on the Host header, so these names must resolve to the
# ingress controller (reachable on the host's port 80 through the mappings).
if ! grep -q "upi-sim.local" /etc/hosts 2>/dev/null; then
  echo
  echo "Add these host entries once (needs sudo):"
  echo "  echo '127.0.0.1 upi-sim.local api.upi-sim.local' | sudo tee -a /etc/hosts"
  echo
fi

# ── 8. Wait and report ──────────────────────────────────────────────────────
echo "==> waiting for pods"
kubectl -n "$NS" wait --for=condition=ready pod --all --timeout=300s || true
kubectl -n "$NS" get pods
echo
echo "frontend:     https://upi-sim.local"
echo "APIs:         https://api.upi-sim.local/{psp,switch,payer,payee,auth,recharge}"
echo "ops console:  kubectl -n $NS port-forward svc/upi-ops-console 5010:5010"
echo
echo "teardown:     kind delete cluster --name $CLUSTER"
