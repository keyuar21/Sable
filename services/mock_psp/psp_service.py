import asyncio
import uuid
import httpx
import time
import os
import sys
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import supabase_db

app = FastAPI(
    title="Mock PSP (Payment Service Provider)",
    description="Orchestrates the full UPI payment flow, manages txn IDs, velocity limits, and runs the settlement batch job.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Service URLs ----------
SWITCH_URL      = "http://localhost:5002"
PAYER_BANK_URL  = "http://localhost:5003"
PAYEE_BANK_URL  = "http://localhost:5004"

MAX_DAILY_LIMIT = 100_000   # ₹1 lakh/day (real UPI rule)
SETTLEMENT_INTERVAL_SECS = 30  # Every 30s in demo (real: multiple times/day)

# ---------- In-memory state ----------
txn_store: dict[str, dict] = {}           # txn_id → full transaction state
velocity_tracker: dict[str, dict] = {}    # vpa → {date: str, total: float}
settlement_history: list[dict] = []
settlement_cycle_count: int = 0
next_settlement_at: float = 0.0           # epoch seconds


# ---------- Models ----------

class InitiateRequest(BaseModel):
    payer_vpa: str
    payee_vpa: str
    amount: float
    pin: Optional[str] = None            # legacy path
    step_up_token: Optional[str] = None  # new path — from WebAuthn or /pin/verify via mock_auth
    currency: str = "INR"


# ---------- Helpers ----------

def generate_txn_id() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    suffix = str(uuid.uuid4())[:6].upper()
    return f"PSP{ts}{suffix}"

def check_velocity(vpa: str, amount: float) -> tuple[bool, str]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rec = velocity_tracker.get(vpa, {})
    if rec.get("date") != today:
        # New day — reset
        velocity_tracker[vpa] = {"date": today, "total": 0.0}
        rec = velocity_tracker[vpa]
    daily_total = rec.get("total", 0.0)
    if daily_total + amount > MAX_DAILY_LIMIT:
        remaining = MAX_DAILY_LIMIT - daily_total
        return False, f"Daily UPI limit exceeded. Remaining today: ₹{remaining:,.0f}"
    return True, ""

def update_velocity(vpa: str, amount: float):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rec = velocity_tracker.setdefault(vpa, {"date": today, "total": 0.0})
    if rec.get("date") != today:
        rec["date"] = today
        rec["total"] = 0.0
    rec["total"] = rec.get("total", 0.0) + amount


# ---------- Background payment processor ----------

async def process_payment(txn_id: str, req: InitiateRequest):
    """
    Async task: orchestrates the full UPI hop sequence and updates txn_store
    at each step so the frontend can poll for real-time status.
    """
    store = txn_store[txn_id]

    try:
        # ── Step 1: Route through NPCI Switch ───────────────────────────────
        await asyncio.sleep(0.4)
        store["status"] = "routing"
        store["hops"]["switch"] = "active"
        store["hop_log"].append({"hop": "switch", "event": "Routing request to NPCI Switch", "ts": datetime.utcnow().isoformat()})

        async with httpx.AsyncClient(timeout=8.0) as client:
            switch_resp = await client.post(f"{SWITCH_URL}/route", json={
                "txn_id": txn_id,
                "payer_vpa": req.payer_vpa,
                "payee_vpa": req.payee_vpa,
            })

        if switch_resp.status_code != 200:
            raise ValueError(f"Switch routing failed: {switch_resp.json().get('detail', 'Unknown')}")

        routing = switch_resp.json()
        payer_bank_url = routing["payer_bank_url"]
        payee_bank_url = routing["payee_bank_url"]

        store["hops"]["switch"] = "completed"
        store["hop_log"].append({"hop": "switch", "event": "VPAs resolved ✓", "ts": datetime.utcnow().isoformat()})

        # ── Step 2: Debit from Payer Bank ────────────────────────────────────
        await asyncio.sleep(0.5)
        store["status"] = "debiting"
        store["hops"]["payer_bank"] = "active"
        store["hop_log"].append({"hop": "payer_bank", "event": "Sending debit + PIN to Payer Bank", "ts": datetime.utcnow().isoformat()})
        supabase_db.sync_transaction(store)

        async with httpx.AsyncClient(timeout=10.0) as client:
            debit_resp = await client.post(f"{payer_bank_url}/debit", json={
                "txn_id": txn_id,
                "vpa": req.payer_vpa,
                "payee_vpa": req.payee_vpa,
                "amount": req.amount,
                "pin": req.pin,
                "step_up_token": req.step_up_token,
            })

        if debit_resp.status_code != 200:
            err = debit_resp.json().get("detail", "Debit failed")
            store["hops"]["payer_bank"] = "failed"
            store["hop_log"].append({"hop": "payer_bank", "event": f"FAILED: {err}", "ts": datetime.utcnow().isoformat()})
            store["status"] = "failed"
            store["error"] = err
            store["error_stage"] = "payer_bank"
            supabase_db.sync_transaction(store)
            return

        debit_data = debit_resp.json()
        store["hops"]["payer_bank"] = "completed"
        store["new_balance"] = debit_data.get("new_balance")
        store["hop_log"].append({"hop": "payer_bank", "event": f"Debited ₹{req.amount:,.0f} ✓ | New balance: ₹{debit_data.get('new_balance', 0):,.0f}", "ts": datetime.utcnow().isoformat()})

        # ── Step 3: Credit to Payee Bank ─────────────────────────────────────
        await asyncio.sleep(0.4)
        store["status"] = "crediting"
        store["hops"]["payee_bank"] = "active"
        store["hop_log"].append({"hop": "payee_bank", "event": "Sending credit instruction to Payee Bank", "ts": datetime.utcnow().isoformat()})
        supabase_db.sync_transaction(store)

        async with httpx.AsyncClient(timeout=10.0) as client:
            credit_resp = await client.post(f"{payee_bank_url}/credit", json={
                "txn_id": txn_id,
                "vpa": req.payee_vpa,
                "payer_vpa": req.payer_vpa,
                "amount": req.amount,
            })

        if credit_resp.status_code != 200:
            # ⚠️  Partial success: debited but not credited → reversal needed
            err = credit_resp.json().get("detail", "Credit failed")
            store["hops"]["payee_bank"] = "failed"
            store["hop_log"].append({"hop": "payee_bank", "event": f"FAILED (reversal needed): {err}", "ts": datetime.utcnow().isoformat()})
            store["status"] = "partial_failure"
            store["error"] = f"Credit leg failed after debit — reversal initiated. ({err})"
            store["error_stage"] = "payee_bank"
            supabase_db.sync_transaction(store)
            return

        credit_data = credit_resp.json()
        store["hops"]["payee_bank"] = "completed"
        store["hop_log"].append({"hop": "payee_bank", "event": f"Credited ₹{req.amount:,.0f} ✓", "ts": datetime.utcnow().isoformat()})

        # ── Step 4: Mark success ─────────────────────────────────────────────
        store["status"] = "success"
        store["completed_at"] = datetime.utcnow().isoformat()
        store["hop_log"].append({"hop": "psp", "event": "Transaction complete ✓ — awaiting settlement batch", "ts": datetime.utcnow().isoformat()})
        supabase_db.sync_transaction(store)
        update_velocity(req.payer_vpa, req.amount)

    except httpx.TimeoutException as e:
        store["status"] = "timeout"
        store["error"] = "A service did not respond in time. Timeout ≠ failed — status check required."
        store["hop_log"].append({"hop": "unknown", "event": f"TIMEOUT: {str(e)}", "ts": datetime.utcnow().isoformat()})
        supabase_db.sync_transaction(store)

    except Exception as e:
        store["status"] = "failed"
        store["error"] = str(e)
        store["hop_log"].append({"hop": "unknown", "event": f"ERROR: {str(e)}", "ts": datetime.utcnow().isoformat()})
        supabase_db.sync_transaction(store)


# ---------- Settlement batch loop ----------

async def settlement_loop():
    global settlement_cycle_count, next_settlement_at
    await asyncio.sleep(SETTLEMENT_INTERVAL_SECS)
    while True:
        next_settlement_at = time.time() + SETTLEMENT_INTERVAL_SECS
        await run_settlement_cycle()
        await asyncio.sleep(SETTLEMENT_INTERVAL_SECS)

async def run_settlement_cycle():
    global settlement_cycle_count
    settlement_cycle_count += 1
    cycle_id = f"SETTLE-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-C{settlement_cycle_count:03d}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            payer_resp  = await client.get(f"{PAYER_BANK_URL}/all-transactions")
            payee_resp  = await client.get(f"{PAYEE_BANK_URL}/all-transactions")

        payer_txns = payer_resp.json() if payer_resp.status_code == 200 else []
        payee_txns = payee_resp.json() if payee_resp.status_code == 200 else []

        # Unsettled debits (status=debited, no settlement_batch_id)
        unsettled_debits  = [t for t in payer_txns if t["status"] == "debited"  and not t.get("settlement_batch_id")]
        unsettled_credits = [t for t in payee_txns if t["status"] == "credited" and not t.get("settlement_batch_id")]

        debit_count  = len(unsettled_debits)
        credit_count = len(unsettled_credits)
        total_debit  = sum(t["amount"] for t in unsettled_debits)
        total_credit = sum(t["amount"] for t in unsettled_credits)

        cycle = {
            "cycle_id": cycle_id,
            "cycle_number": settlement_cycle_count,
            "timestamp": datetime.utcnow().isoformat(),
            "debit_count": debit_count,
            "credit_count": credit_count,
            "total_debit_settled": total_debit,
            "total_credit_settled": total_credit,
            "net_obligation": total_debit,
            "from_bank": "Mock Payer Bank (@mockbank)",
            "to_bank": "Mock Payee Bank (@mockbank2)",
            "status": "SETTLED" if (debit_count > 0 or credit_count > 0) else "EMPTY_CYCLE",
        }
        settlement_history.append(cycle)
        supabase_db.insert_settlement_batch(cycle)

        # Mark all settled transactions
        async with httpx.AsyncClient(timeout=5.0) as client:
            for t in unsettled_debits:
                await client.post(f"{PAYER_BANK_URL}/update-status", json={
                    "txn_id": t["txn_id"], "status": "success", "settlement_batch_id": cycle_id
                })
            for t in unsettled_credits:
                await client.post(f"{PAYEE_BANK_URL}/update-status", json={
                    "txn_id": t["txn_id"], "status": "success", "settlement_batch_id": cycle_id
                })

        # Also update PSP's in-memory store
        for txn_id, s in txn_store.items():
            if s["status"] == "success" and not s.get("settlement_batch_id"):
                s["settlement_batch_id"] = cycle_id
                s["status"] = "settled"
                supabase_db.sync_transaction(s)

    except Exception as e:
        settlement_history.append({
            "cycle_id": cycle_id,
            "cycle_number": settlement_cycle_count,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "ERROR",
            "error": str(e),
        })


# ---------- Startup ----------

@app.on_event("startup")
async def startup():
    global next_settlement_at
    next_settlement_at = time.time() + SETTLEMENT_INTERVAL_SECS
    asyncio.create_task(settlement_loop())


# ---------- Endpoints ----------

@app.get("/health")
def health():
    remaining = max(0, next_settlement_at - time.time())
    return {
        "status": "ok",
        "service": "PSP",
        "port": 5001,
        "settlement_cycle": settlement_cycle_count,
        "next_settlement_in_secs": round(remaining),
    }

@app.get("/supabase-status")
def get_supabase_status():
    h = supabase_db.check_supabase_health()
    return {
        "configured": supabase_db.is_supabase_configured(),
        "supabase_url": supabase_db.SUPABASE_URL,
        "health": h,
        "schema_file": "schema.sql"
    }

@app.post("/initiate")
async def initiate(req: InitiateRequest, background_tasks: BackgroundTasks):
    # Velocity limit check
    ok, reason = check_velocity(req.payer_vpa, req.amount)
    if not ok:
        raise HTTPException(429, reason)

    # Basic sanity
    if req.amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    if req.amount > MAX_DAILY_LIMIT:
        raise HTTPException(400, f"Amount exceeds single-txn limit of ₹{MAX_DAILY_LIMIT:,}")
    if not req.pin and not req.step_up_token:
        raise HTTPException(400, "Must authenticate first (WebAuthn biometric or PIN) — no step_up_token or pin provided")

    txn_id = generate_txn_id()

    txn_store[txn_id] = {
        "txn_id": txn_id,
        "payer_vpa": req.payer_vpa,
        "payee_vpa": req.payee_vpa,
        "amount": req.amount,
        "currency": req.currency,
        "status": "initiated",
        "hops": {
            "psp":        "completed",   # PSP received it — first hop done
            "switch":     "pending",
            "payer_bank": "pending",
            "payee_bank": "pending",
        },
        "hop_log": [
            {"hop": "psp", "event": f"Payment initiated (₹{req.amount:,.0f} | {req.payer_vpa} → {req.payee_vpa})", "ts": datetime.utcnow().isoformat()}
        ],
        "created_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "new_balance": None,
        "error": None,
        "error_stage": None,
        "settlement_batch_id": None,
    }

    background_tasks.add_task(process_payment, txn_id, req)
    supabase_db.upsert_transaction(txn_store[txn_id])

    return {"txn_id": txn_id, "status": "initiated", "message": "Payment processing started"}

@app.get("/txn/{txn_id}")
def get_txn(txn_id: str):
    store = txn_store.get(txn_id)
    if not store:
        sb_txn = supabase_db.get_transaction(txn_id)
        if sb_txn:
            return sb_txn
        raise HTTPException(404, f"Transaction not found: {txn_id}")
    return store

@app.get("/velocity/{vpa}")
def get_velocity(vpa: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rec = velocity_tracker.get(vpa, {"date": today, "total": 0.0})
    used = rec.get("total", 0.0) if rec.get("date") == today else 0.0
    return {
        "vpa": vpa,
        "date": today,
        "used_today": used,
        "limit": MAX_DAILY_LIMIT,
        "remaining": MAX_DAILY_LIMIT - used,
    }

@app.get("/settlement/history")
def get_settlement_history():
    sb_batches = supabase_db.list_settlement_batches(limit=20)
    if sb_batches:
        return {
            "cycles": sb_batches,
            "total_cycles": max(settlement_cycle_count, len(sb_batches)),
            "interval_secs": SETTLEMENT_INTERVAL_SECS,
        }
    return {
        "cycles": list(reversed(settlement_history[-20:])),
        "total_cycles": settlement_cycle_count,
        "interval_secs": SETTLEMENT_INTERVAL_SECS,
    }

@app.get("/settlement/next")
def get_next_settlement():
    remaining = max(0, next_settlement_at - time.time())
    return {
        "next_in_secs": round(remaining),
        "cycle_number": settlement_cycle_count + 1,
        "interval_secs": SETTLEMENT_INTERVAL_SECS,
    }

@app.get("/transactions")
def list_transactions(payer_vpa: str = None):
    sb_txns = supabase_db.list_transactions(limit=100)
    if sb_txns:
        if payer_vpa:
            sb_txns = [t for t in sb_txns if t.get("payer_vpa") == payer_vpa]
        return sb_txns
    all_txns = list(reversed(list(txn_store.values())))
    if payer_vpa:
        all_txns = [t for t in all_txns if t.get("payer_vpa") == payer_vpa]
    return all_txns

@app.post("/reset")
async def reset():
    """Full system reset — clears all in-memory state and calls reset on all services."""
    txn_store.clear()
    velocity_tracker.clear()
    settlement_history.clear()
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(f"{PAYER_BANK_URL}/reset")
        await client.post(f"{PAYEE_BANK_URL}/reset")
        await client.post(f"{SWITCH_URL}/reset")
    return {"success": True, "message": "Full system reset complete — balances restored to initial values"}
