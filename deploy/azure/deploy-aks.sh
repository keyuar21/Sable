#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Deploy Sable Voice UPI Simulator on Azure Kubernetes Service (AKS)
#  Target: Fixed-cost, zero-surprise setup within Azure Student Credits
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Configuration
RG="${AZURE_RG:-sable-rg}"
LOCATION="${AZURE_LOCATION:-centralindia}"  # Change to eastus, westeurope, etc. if preferred
CLUSTER="${AKS_NAME:-sable-aks}"
VM_SIZE="${AKS_VM_SIZE:-Standard_D4s_v5}"   # 4 vCPU, 16 GB RAM (fixed 1 node)
REPO="keyuar/sable"
NS="upi"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELM_DIR="$ROOT/deploy/helm"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
NC="\033[0m"

echo -e "${CYAN}================================================================${NC}"
echo -e "${CYAN}   Deploying Sable UPI Stack to Azure Kubernetes Service (AKS)  ${NC}"
echo -e "${CYAN}================================================================${NC}"

# ── 1. Create Resource Group ─────────────────────────────────────────────────
echo -e "\n${GREEN}[1/7] Ensuring Resource Group '${RG}' in ${LOCATION}...${NC}"
az group create --name "$RG" --location "$LOCATION" -o table

# ── 2. Create Locked-Down Fixed-Cost AKS Cluster ──────────────────────────────
echo -e "\n${GREEN}[2/7] Checking/Creating AKS cluster '${CLUSTER}'...${NC}"
if az aks show --resource-group "$RG" --name "$CLUSTER" >/dev/null 2>&1; then
  echo -e "Cluster '${CLUSTER}' already exists. Skipping creation."
else
  echo -e "${YELLOW}Creating locked-down AKS cluster (Free Tier, 1 node ${VM_SIZE}, no telemetry)...${NC}"
  az aks create \
    --resource-group "$RG" \
    --name "$CLUSTER" \
    --tier free \
    --node-count 1 \
    --node-vm-size "$VM_SIZE" \
    --os-disk-size 64 \
    --os-sku Ubuntu \
    --enable-managed-identity \
    --disable-monitoring \
    --generate-ssh-keys \
    -o table
fi

# ── 3. Connect kubectl to AKS ────────────────────────────────────────────────
echo -e "\n${GREEN}[3/7] Fetching AKS credentials for kubectl...${NC}"
az aks get-credentials --resource-group "$RG" --name "$CLUSTER" --overwrite-existing

# ── 4. Install Ingress Nginx ─────────────────────────────────────────────────
echo -e "\n${GREEN}[4/7] Setting up Ingress-Nginx Controller...${NC}"
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
helm repo update ingress-nginx

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/azure-load-balancer-health-probe-request-path"=/healthz \
  --wait --timeout 5m

echo "Waiting for Ingress External Public IP..."
EXTERNAL_IP=""
for i in {1..30}; do
  EXTERNAL_IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -n "$EXTERNAL_IP" ]; then
    break
  fi
  sleep 5
done

if [ -z "$EXTERNAL_IP" ]; then
  echo -e "${YELLOW}Public IP is still provisioning. Run 'kubectl get svc -n ingress-nginx' in a moment.${NC}"
else
  echo -e "${GREEN}✓ Ingress Public IP provisioned: ${EXTERNAL_IP}${NC}"
fi

# ── 5. Namespaces & Secrets ──────────────────────────────────────────────────
echo -e "\n${GREEN}[5/7] Creating namespaces and runtime secrets...${NC}"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
kubectl get ns ai-sandbox >/dev/null 2>&1 || kubectl create ns ai-sandbox

# 5a. Apply Network Policy for Code Sandbox
if [ -f "$ROOT/deploy/k8s/sandbox-lockdown-netpol.yaml" ]; then
  kubectl apply -f "$ROOT/deploy/k8s/sandbox-lockdown-netpol.yaml"
fi

# 5b. Read .env if present for credentials
ENV_FILE="$ROOT/.env"
LLM_KEY=""
SUPA_URL=""
SUPA_KEY=""

if [ -f "$ENV_FILE" ]; then
  LLM_KEY="$(sed -n 's/^LLM_API_KEY=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
  [ -z "$LLM_KEY" ] && LLM_KEY="$(sed -n 's/^NVIDIA_API_KEY=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
  SUPA_URL="$(sed -n 's/^SUPABASE_URL=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
  SUPA_KEY="$(sed -n 's/^SUPABASE_SECRET_KEY=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
fi

# Seed nim-api
kubectl -n "$NS" create secret generic nim-api \
  --from-literal=LLM_API_KEY="${LLM_KEY:-none}" \
  --from-literal=LLM_BASE_URL="${LLM_BASE_URL:-https://integrate.api.nvidia.com/v1}" \
  --from-literal=LLM_MODEL="${LLM_MODEL:-nvidia/nemotron-3.5-lightning-30b-a3b}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Seed upi-supabase
kubectl -n "$NS" create secret generic upi-supabase \
  --from-literal=SUPABASE_URL="${SUPA_URL:-}" \
  --from-literal=SUPABASE_SECRET_KEY="${SUPA_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Seed upi-recharge-keys
kubectl -n "$NS" get secret upi-recharge-keys >/dev/null 2>&1 || \
  kubectl -n "$NS" create secret generic upi-recharge-keys

# 5c. Generate TLS certificate for ingress
CERT_DIR="$(mktemp -d)"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.crt" \
  -subj "/CN=upi-sim.local" \
  -addext "subjectAltName=DNS:upi-sim.local,IP:${EXTERNAL_IP:-127.0.0.1}" >/dev/null 2>&1
kubectl -n "$NS" create secret tls upi-sim-tls \
  --cert="$CERT_DIR/tls.crt" --key="$CERT_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -rf "$CERT_DIR"

# ── 6. Deploy All 10 Services from Docker Hub ─────────────────────────────────
echo -e "\n${GREEN}[6/7] Deploying all 10 microservices from ${REPO}...${NC}"

SERVICES="switch auth payer-bank payee-bank psp recharge ops-console frontend"
for s in $SERVICES; do
  echo -e "  -> Deploying upi-$s"
  helm upgrade --install "upi-$s" "$HELM_DIR/upi-service" \
    -f "$HELM_DIR/values/$s.yaml" \
    --set image.repository="$REPO" \
    --set image.tag="$s-latest" \
    -n "$NS"
done

# Deploy Sable Agent and Code Sandbox
echo -e "  -> Deploying upi-agent"
helm upgrade --install upi-agent "$HELM_DIR/upi-service" \
  -f "$HELM_DIR/values/agent.yaml" \
  --set image.repository="$REPO" \
  --set image.tag="agent-latest" \
  -n "$NS"

echo -e "  -> Deploying upi-code-sandbox"
helm upgrade --install upi-code-sandbox "$HELM_DIR/upi-service" \
  -f "$HELM_DIR/values/sandbox.yaml" \
  --set image.repository="$REPO" \
  --set image.tag="sandbox-latest" \
  -n ai-sandbox

# ── 7. Wait and Status ────────────────────────────────────────────────────────
echo -e "\n${GREEN}[7/7] Checking pod status...${NC}"
kubectl -n "$NS" get pods -o wide

echo -e "\n${CYAN}================================================================${NC}"
echo -e "${GREEN}✓ All services deployed successfully on AKS!${NC}"
echo -e "${CYAN}================================================================${NC}"
if [ -n "$EXTERNAL_IP" ]; then
  echo -e "Ingress Public IP: ${YELLOW}https://${EXTERNAL_IP}${NC}"
  echo -e "\nTo access with domain name upi-sim.local, add this line to your /etc/hosts:"
  echo -e "  ${YELLOW}${EXTERNAL_IP} upi-sim.local${NC}"
fi
echo -e "\nUseful commands:"
echo -e "  View pods:       ${YELLOW}kubectl -n upi get pods${NC}"
echo -e "  View logs:       ${YELLOW}kubectl -n upi logs -f deploy/upi-agent${NC}"
echo -e "  Pause cluster:   ${YELLOW}az aks stop --name ${CLUSTER} --resource-group ${RG}${NC} (Saves credits when idle)"
echo -e "  Resume cluster:  ${YELLOW}az aks start --name ${CLUSTER} --resource-group ${RG}${NC}"
