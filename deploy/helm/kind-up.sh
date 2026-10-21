#!/usr/bin/env bash
#
# Bring the whole UPI simulator up on a local kind cluster, Ingress included.
#
# Result:
#   https://upi-sim.local                     -> frontend
#   https://upi-sim.local/api/psp             -> PSP
#   https://upi-sim.local/api/switch          -> NPCI switch
#   https://upi-sim.local/api/payer           -> payer bank
#   https://upi-sim.local/api/payee           -> payee bank
#   https://upi-sim.local/api/auth            -> auth
#   https://upi-sim.local/api/recharge        -> recharge
#
# Everything is served from ONE host on purpose. A second API host (the earlier
# api.upi-sim.local) needs its own certificate exception; fetch() fails silently
# when the cert is untrusted, which shows up in the browser as "Failed to fetch"
# with nothing at all in the Ingress logs. Same origin also means no CORS.
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
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELM_DIR="$ROOT/deploy/helm"

SERVICES="switch auth payer-bank payee-bank psp recharge ops-console frontend"

# Every values file pins (or omits) its own image tag. Resolve the tag Helm
# will actually deploy per service — building a different one silently breaks
# the run: with a match the pod starts, with a mismatch you either get
# ImagePullBackOff (tag never loaded) or a stale image served under the same
# tag. The common baseline lives in Chart.yaml appVersion; the agent-era
# services (agent/sandbox/frontend/psp/payer-bank/ops-console) bump their own.
CHART_APPVERSION="$(awk '$1 == "appVersion:" { gsub(/"/, "", $2); print $2 }' "$HELM_DIR/upi-service/Chart.yaml")"
tagof() {
  # "tag: ""        # defaults..." → empty → appVersion; "tag: "1.0.6" ..." → 1.0.6
  local t
  t="$(grep -m1 -E '^[[:space:]]*tag:' "$HELM_DIR/values/$1.yaml" 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)+' | head -1)"
  echo "${t:-$CHART_APPVERSION}"
}

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
# model fetched at build time) — schedule it when nothing interactive needs the
# machine; a parallel build starves the VM and every response in the app.
build_img() {  # build_img <image> <values-name> <dockerfile> [context]
  local img="$1" tag; tag="$(tagof "$2")"
  echo "  -> $img:$tag"
  docker build -f "$3" -t "$img:$tag" "${4:-$ROOT}"
}
echo "==> building images"
build_img upi-switch     switch      "$ROOT/services/mock_switch/Dockerfile"
build_img upi-auth       auth        "$ROOT/services/mock_auth/Dockerfile"
build_img upi-payer-bank payer-bank  "$ROOT/services/mock_payer_bank/Dockerfile"
build_img upi-payee-bank payee-bank  "$ROOT/services/mock_payee_bank/Dockerfile"
build_img upi-psp        psp         "$ROOT/services/mock_psp/Dockerfile"
build_img upi-recharge   recharge    "$ROOT/services/mock_recharge/Dockerfile"
build_img upi-ops-console ops-console "$ROOT/services/ops_console/Dockerfile"
build_img upi-frontend   frontend    "$ROOT/frontend/Dockerfile" "$ROOT/frontend"

# Sable agent plane
build_img upi-agent         agent   "$ROOT/services/agent/Dockerfile"
build_img upi-code-sandbox  sandbox "$ROOT/services/sandbox/Dockerfile"

# ── 3. Load them into the cluster ───────────────────────────────────────────
# kind runs its own containerd, so host-built images are invisible to it until
# loaded. Skip this and pods keep running an older image with the same tag --
# imagePullPolicy: IfNotPresent matches the tag and never re-checks the host.
echo "==> loading images into '$CLUSTER'"
load_pair() {  # load_pair <image> <values-name>
  local tag; tag="$(tagof "$2")"
  echo "  -> $1:$tag"
  kind load docker-image "$1:$tag" --name "$CLUSTER"
}
for pair in "upi-switch switch" "upi-auth auth" "upi-payer-bank payer-bank" \
            "upi-payee-bank payee-bank" "upi-psp psp" "upi-recharge recharge" \
            "upi-ops-console ops-console" "upi-frontend frontend" \
            "upi-agent agent" "upi-code-sandbox sandbox"; do
  load_pair $pair
done

# ── 4. Namespace + secrets ──────────────────────────────────────────────────
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

# recharge loads this via envFrom. If it is missing the pod is stuck in
# CreateContainerConfigError. Empty is fine: its plan source is "builtin".
kubectl -n "$NS" get secret upi-recharge-keys >/dev/null 2>&1 || \
  kubectl -n "$NS" create secret generic upi-recharge-keys

# nim-api carries the NVIDIA NIM credentials the Sable agent lives on
# (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL, read via envFrom). The key never
# lives in this repo: it comes from the environment or the repo's .env
# (LLM_API_KEY=...). Without it the agent boots degraded ("not configured"
# replies) and the frontend quietly falls back to the regex parser — better
# to warn loudly here than shop-broken.
LLM_KEY="${LLM_API_KEY:-}"
if [ -z "$LLM_KEY" ] && [ -f "$ROOT/.env" ]; then
  LLM_KEY="$(sed -n 's/^LLM_API_KEY=//p' "$ROOT/.env" | head -1 | tr -d '\r')"
  [ -z "$LLM_KEY" ] && LLM_KEY="$(sed -n 's/^NVIDIA_API_KEY=//p' "$ROOT/.env" | head -1 | tr -d '\r')"
fi
if [ -n "$LLM_KEY" ]; then
  kubectl -n "$NS" create secret generic nim-api \
    --from-literal=LLM_API_KEY="$LLM_KEY" \
    --from-literal=LLM_BASE_URL="${LLM_BASE_URL:-https://integrate.api.nvidia.com/v1}" \
    --from-literal=LLM_MODEL="${LLM_MODEL:-nvidia/nemotron-3.5-lightning-30b-a3b}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "    nim-api seeded (key from $([ -n "${LLM_API_KEY:-}" ] && echo env || echo .env))"
elif kubectl -n "$NS" get secret nim-api >/dev/null 2>&1; then
  echo "    nim-api already exists (kept as-is)"
else
  kubectl -n "$NS" create secret generic nim-api
  echo "    WARNING: nim-api created EMPTY — set LLM_API_KEY=... and rerun, or the agent stays degraded."
fi

# upi-supabase carries the project URL and service key for the banks, switch, psp
# and auth; values/auth.yaml references it too. Seed it from the repo's .env when
# there is one, so a fresh cluster can sign in the accounts that live in the
# project. An empty secret still boots -- the SQLite stores are authoritative and
# the mirror is reporting-only -- but then only local accounts exist and sign-in
# fails with "No account for <vpa>". These are read once at start-up, so a changed
# secret needs `kubectl rollout restart` on those five deployments.
ENV_FILE="$ROOT/.env"
if [ -f "$ENV_FILE" ] && grep -q '^SUPABASE_URL=.\+' "$ENV_FILE"; then
  SUPA_URL="$(sed -n 's/^SUPABASE_URL=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
  SUPA_KEY="$(sed -n 's/^SUPABASE_SECRET_KEY=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
  kubectl -n "$NS" create secret generic upi-supabase \
    --from-literal=SUPABASE_URL="$SUPA_URL" \
    --from-literal=SUPABASE_SECRET_KEY="$SUPA_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "    upi-supabase seeded from .env"
else
  kubectl -n "$NS" get secret upi-supabase >/dev/null 2>&1 || \
    kubectl -n "$NS" create secret generic upi-supabase
  echo "    upi-supabase left empty (no SUPABASE_URL in .env)"
fi

# TLS for the Ingress. Voice ID needs a secure context: browsers only expose
# getUserMedia (the microphone) on https:// -- http://upi-sim.local is not one,
# so the recorder fails even though the page loads. A self-signed cert is enough
# to satisfy that; the browser warns once for upi-sim.local. Swap in mkcert/an
# internal CA if you want the warning gone. Only one host needs an exception
# because every service is served from upi-sim.local.
CERT_DIR="$(mktemp -d)"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" \
  -subj "/CN=upi-sim.local" \
  -addext "subjectAltName=DNS:upi-sim.local"
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
# published on upi-sim.local under /api/<svc> with a rewrite that strips the
# prefix (see the ingress: block in each values/*.yaml).
echo "==> installing releases"
for s in $SERVICES; do
  echo "  -> upi-$s"
  helm upgrade --install "upi-$s" "$HELM_DIR/upi-service" \
    -f "$HELM_DIR/values/$s.yaml" -n "$NS"
done

# ── 6b. Sable agent plane ───────────────────────────────────────────────────
# The Unified Agent Service (:5008, POST /agent/voice + /agent/ops) and the
# locked Code-Mode runner pod (ai-sandbox namespace, Loki-only egress).
#   • agent values expect the `nim-api` Secret — seeded automatically in
#     section 4 from the environment or $ROOT/.env (LLM_API_KEY=...).
#   • sandbox lives in its own namespace; install AFTER both namespaces exist.
kubectl create ns ai-sandbox --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$ROOT/deploy/k8s/sandbox-lockdown-netpol.yaml"
helm upgrade --install upi-agent "$HELM_DIR/upi-service" \
  -f "$HELM_DIR/values/agent.yaml" -n "$NS"
helm upgrade --install upi-code-sandbox "$HELM_DIR/upi-service" \
  -f "$HELM_DIR/values/sandbox.yaml" -n ai-sandbox

# ── 7. Host names ───────────────────────────────────────────────────────────
# The Ingress dispatches on the Host header, so these names must resolve to the
# ingress controller (reachable on the host's port 80 through the mappings).
if ! grep -q "upi-sim.local" /etc/hosts 2>/dev/null; then
  echo
  echo "Add this host entry once (needs sudo):"
  echo "  echo '127.0.0.1 upi-sim.local' | sudo tee -a /etc/hosts"
  echo
fi

# ── 8. Wait and report ──────────────────────────────────────────────────────
echo "==> waiting for pods"
kubectl -n "$NS" wait --for=condition=ready pod --all --timeout=300s || true
kubectl -n "$NS" get pods
echo
echo "frontend:     https://upi-sim.local"
echo "APIs:         https://upi-sim.local/api/{psp,switch,payer,payee,auth,recharge}"
echo "agent voice:  https://upi-sim.local/agent/voice   (X-VPA header; used by the app automatically)"
echo "agent ops:    https://upi-sim.local/agent/ops     (X-Admin-Token header; ops console prompt box)"
echo "ops console:  kubectl -n $NS port-forward svc/upi-ops-console 5010:5010"
echo
echo "teardown:     kind delete cluster --name $CLUSTER"
