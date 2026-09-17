from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import supabase_db

app = FastAPI(
    title="Mock NPCI UPI Switch",
    description="Pure routing layer — resolves VPAs to bank URLs, logs all routes. Holds no money.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- VPA → Bank URL routing table ----------
# In real UPI, NPCI maintains this; @-suffix maps to a member bank's API
ROUTING_TABLE: dict[str, str] = {
    "@mockbank":   "http://localhost:5003",   # Payer Bank
    "@mockbank2":  "http://localhost:5004",   # Payee Bank
    "@hdfcbank":   "http://localhost:5003",   # HDFC Bank (Payer Bank simulator)
    "@okhdfcbank": "http://localhost:5003",   # HDFC Bank handle
}

route_log: list[dict] = []   # In-memory route audit log


# ---------- Helpers ----------

def resolve_bank(vpa: str) -> str | None:
    for suffix, url in ROUTING_TABLE.items():
        if vpa.endswith(suffix):
            return url
    return None


# ---------- Models ----------

class RouteRequest(BaseModel):
    txn_id: str
    payer_vpa: str
    payee_vpa: str


# ---------- Endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok", "service": "NPCI Switch", "port": 5002}

@app.get("/routing-table")
def get_routing_table():
    return {
        "routing_table": ROUTING_TABLE,
        "description": "Maps VPA suffix (@handle) → bank service URL",
        "total_routes": len(ROUTING_TABLE)
    }

@app.get("/resolve/{vpa}")
def resolve(vpa: str):
    """Resolve a single VPA to its bank service URL."""
    bank_url = resolve_bank(vpa)
    if not bank_url:
        raise HTTPException(404, f"No bank registered for VPA: {vpa}")
    suffix = [s for s in ROUTING_TABLE if vpa.endswith(s)][0]
    return {"vpa": vpa, "bank_url": bank_url, "bank_suffix": suffix}

@app.post("/route")
def route(req: RouteRequest):
    """
    NPCI's core job: resolve payer and payee VPAs to bank URLs.
    The PSP then uses these URLs to call the banks directly.
    This keeps the Switch as a pure routing/messaging layer.
    """
    payer_bank_url = resolve_bank(req.payer_vpa)
    payee_bank_url = resolve_bank(req.payee_vpa)

    entry = {
        "txn_id": req.txn_id,
        "payer_vpa": req.payer_vpa,
        "payee_vpa": req.payee_vpa,
        "timestamp": datetime.utcnow().isoformat(),
        "payer_bank_url": payer_bank_url,
        "payee_bank_url": payee_bank_url,
        "status": "resolved" if (payer_bank_url and payee_bank_url) else "failed"
    }
    route_log.append(entry)
    supabase_db.insert_route_log(entry)

    if not payer_bank_url:
        raise HTTPException(400, f"Cannot resolve payer bank for VPA: {req.payer_vpa}")
    if not payee_bank_url:
        raise HTTPException(400, f"Cannot resolve payee bank for VPA: {req.payee_vpa}")

    return {
        "success": True,
        "txn_id": req.txn_id,
        "payer_bank_url": payer_bank_url,
        "payee_bank_url": payee_bank_url,
        "payer_vpa": req.payer_vpa,
        "payee_vpa": req.payee_vpa,
    }

@app.get("/npci/discover-accounts")
def discover_accounts(identifier: str = ""):
    """
    Simulates NPCI Central Mapper lookup for bank accounts linked to a phone number or email.
    Returns HDFC Bank account ready for linking.
    """
    cleaned = identifier.strip()
    digits = [c for c in cleaned if c.isdigit()]
    last4 = "".join(digits[-4:]) if len(digits) >= 4 else "4920"
    return {
        "status": "success",
        "identifier": cleaned or "+91 98765 43210",
        "device_binding": "verified",
        "accounts": [
            {
                "bank_name": "HDFC Bank",
                "bank_code": "HDFC",
                "vpa_suffix": "@hdfcbank",
                "account_mask": f"•••• •••• •••• {last4}",
                "account_type": "Savings Account",
                "ifsc": "HDFC0001234",
                "branch": "Mumbai Central Branch",
                "default_balance": 50000.0,
                "icon": "🏦"
            }
        ]
    }

@app.get("/route-log")
def get_route_log():
    """Audit log of all routing decisions made by the switch."""
    sb_logs = supabase_db.list_route_logs(limit=50)
    if sb_logs:
        return sb_logs
    return list(reversed(route_log[-50:]))

@app.post("/reset")
def reset():
    route_log.clear()
    return {"success": True, "message": "Route log cleared"}
