#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Voice UPI Simulator — One-command launcher
#  Starts all 4 FastAPI microservices + serves the frontend on port 3000
# ─────────────────────────────────────────────────────────────────────────────

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
NC="\033[0m"

echo -e "${CYAN}"
echo "  ██╗   ██╗██████╗ ██╗    ███████╗██╗███╗   ███╗"
echo "  ██║   ██║██╔══██╗██║    ██╔════╝██║████╗ ████║"
echo "  ██║   ██║██████╔╝██║    ███████╗██║██╔████╔██║"
echo "  ██║   ██║██╔═══╝ ██║    ╚════██║██║██║╚██╔╝██║"
echo "  ╚██████╔╝██║     ██║    ███████║██║██║ ╚═╝ ██║"
echo "   ╚═════╝ ╚═╝     ╚═╝    ╚══════╝╚═╝╚═╝     ╚═╝"
echo -e "${NC}"
echo -e "${YELLOW}  UPI Payment Flow Simulator — Voice Edition${NC}"
echo ""

# ── Detect functional Python binary ──────────────────────────────────────────
if [ -x "/usr/local/bin/python3" ]; then
    PY="/usr/local/bin/python3"
else
    PY="python3"
fi

# The macOS framework Python can lack the system CA bundle. Use certifi so
# Piper's first-run voice-model download remains certificate-verified.
PIPER_CA_FILE="$($PY -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
if [ -n "$PIPER_CA_FILE" ]; then
    export SSL_CERT_FILE="$PIPER_CA_FILE"
fi

# ── Load Supabase .env if present ─────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
    echo -e "${GREEN}✓ Supabase credentials loaded from .env${NC}"
fi

# ── Install Python deps ───────────────────────────────────────────────────────
echo -e "${GREEN}[1/6] Installing Python dependencies...${NC}"
# Voice biometrics: Resemblyzer (speaker ID) + onnxruntime (AASIST anti-spoof)
$PY -m pip install -q fastapi "uvicorn[standard]" httpx pydantic webauthn bcrypt python-multipart numpy onnxruntime librosa soundfile webrtcvad-wheels piper-tts 2>/dev/null || true
$PY -m pip install -q --no-deps resemblyzer 2>/dev/null || true

# ── Local open-source assistant voice ────────────────────────────────────────
# Piper is used so every user hears the same default assistant voice, instead
# of depending on a browser or operating-system voice being installed.
PIPER_VOICE_DIR="$SCRIPT_DIR/models/piper"
mkdir -p "$PIPER_VOICE_DIR"
echo -e "${GREEN}Preparing local assistant voice (first run downloads once)...${NC}"
$PY -m piper.download_voices --download-dir "$PIPER_VOICE_DIR" en_US-amy-medium || \
  echo -e "${YELLOW}⚠ Piper voice download failed — browser voice fallback will be used.${NC}"

# ── Kill any lingering services on our ports ──────────────────────────────────
echo -e "${GREEN}[2/6] Clearing ports 5001-5005, 3000...${NC}"
for port in 5001 5002 5003 5004 5005 3000; do
    lsof -ti:"$port" | xargs kill -9 2>/dev/null || true
done
sleep 0.5

# ── Start microservices ───────────────────────────────────────────────────────
SVC_DIR="$SCRIPT_DIR/services"

echo -e "${GREEN}[3/6] Starting Mock Payer Bank     → http://localhost:5003${NC}"
cd "$SVC_DIR"
PYTHONUNBUFFERED=1 PYTHONPATH="$SVC_DIR" $PY -m uvicorn mock_payer_bank.bank_service:app --port 5003 --log-level info > /tmp/payer_bank.log 2>&1 &
PAYER_PID=$!

echo -e "${GREEN}[4/6] Starting Mock Payee Bank     → http://localhost:5004${NC}"
PYTHONUNBUFFERED=1 PYTHONPATH="$SVC_DIR" $PY -m uvicorn mock_payee_bank.bank_service:app --port 5004 --log-level info > /tmp/payee_bank.log 2>&1 &
PAYEE_PID=$!

echo -e "${GREEN}[4/6] Starting Mock NPCI Switch    → http://localhost:5002${NC}"
PYTHONUNBUFFERED=1 PYTHONPATH="$SVC_DIR" $PY -m uvicorn mock_switch.switch_service:app --port 5002 --log-level info > /tmp/switch.log 2>&1 &
SWITCH_PID=$!

echo -e "${GREEN}[4/6] Starting Mock Auth (PIN+WebAuthn+Voice) → http://localhost:5005${NC}"
PYTHONUNBUFFERED=1 PYTHONPATH="$SVC_DIR" $PY -m uvicorn mock_auth.auth_service:app --port 5005 --log-level info > /tmp/auth.log 2>&1 &
AUTH_PID=$!

# Give auth service a moment, then seed demo PINs so each user has a hashed PIN stored
sleep 1
echo -e "${GREEN}Seeding demo PINs (calls /pin/set)...${NC}"
${PY} "$SCRIPT_DIR/scripts/init_pins.py" || echo "PIN seeding script failed (continue)"

# Wait for banks and switch to be ready before starting PSP
sleep 2

echo -e "${GREEN}[5/6] Starting Mock PSP            → http://localhost:5001${NC}"
PIPER_DATA_DIR="$PIPER_VOICE_DIR" PYTHONUNBUFFERED=1 PYTHONPATH="$SVC_DIR" $PY -m uvicorn mock_psp.psp_service:app --port 5001 --log-level info > /tmp/psp.log 2>&1 &
PSP_PID=$!

# ── Start frontend server ─────────────────────────────────────────────────────
sleep 1
echo -e "${GREEN}[6/6] Serving Frontend             → http://localhost:3000${NC}"
cd "$SCRIPT_DIR/frontend"
$PY -m http.server 3000 --bind 127.0.0.1 > /tmp/frontend.log 2>&1 &
FRONTEND_PID=$!

sleep 2

# ── Health check ──────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}── Service Health ──────────────────────────────────────────${NC}"
for svc in "PSP:5001" "Switch:5002" "PayerBank:5003" "PayeeBank:5004" "Auth:5005"; do
    name="${svc%%:*}"
    port="${svc##*:}"
    status=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/health" 2>/dev/null || echo "000")
    if [ "$status" = "200" ]; then
        echo -e "  ${GREEN}✓${NC} $name (port $port)"
    else
        echo -e "  ✗ $name (port $port) — may still be starting"
    fi
done

# ── Supabase Health ───────────────────────────────────────────────────────────
sb_status=$(curl -s "http://localhost:5001/supabase-status" 2>/dev/null || echo "{}")
if echo "$sb_status" | grep -q '"tables_ready":true'; then
    echo -e "  ${GREEN}✓${NC} Supabase DB: Connected & Live (sanozvdqvezownhvhpdq)"
elif echo "$sb_status" | grep -q '"status":"schema_pending"'; then
    echo -e "  ${YELLOW}⚠${NC} Supabase DB: Reachable, pending schema execution (schema.sql)"
else
    echo -e "  ${YELLOW}ℹ${NC} Supabase DB: Ready to sync"
fi

echo ""
echo -e "${CYAN}── Ready! ──────────────────────────────────────────────────${NC}"
echo -e "  ${GREEN}▶  Open http://localhost:3000 in Chrome/Edge${NC}"
echo -e "     (Web Speech API works best in Chrome)"
echo ""
echo -e "  PIDs → PSP:$PSP_PID  Switch:$SWITCH_PID  Payer:$PAYER_PID  Payee:$PAYEE_PID  Auth:$AUTH_PID  Frontend:$FRONTEND_PID"
echo -e "  Logs → /tmp/psp.log  /tmp/switch.log  /tmp/payer_bank.log  /tmp/payee_bank.log  /tmp/auth.log"
echo ""
echo -e "${YELLOW}  Press Ctrl+C to stop all services${NC}"
echo ""

# ── Trap to kill all services on exit ────────────────────────────────────────
cleanup() {
    echo -e "\n${YELLOW}Stopping all services...${NC}"
    kill $PSP_PID $SWITCH_PID $PAYER_PID $PAYEE_PID $AUTH_PID $FRONTEND_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

wait $PSP_PID
