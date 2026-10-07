import asyncio
import uuid
import httpx
import time
import os
import sys
import tempfile
import subprocess
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

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


class TTSRequest(BaseModel):
    text: str


PIPER_VOICE = os.getenv("PIPER_VOICE", "en_US-amy-medium")
PIPER_DATA_DIR = os.getenv(
    "PIPER_DATA_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "models", "piper")),
)


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


# ---------- Local assistant voice (open-source Piper) ----------

def _remove_file(path: str):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _render_assistant_voice(text: str, output_path: str):
    """Render the default assistant voice locally with Piper TTS."""
    subprocess.run(
        ["piper", "--model", PIPER_VOICE, "--data-dir", PIPER_DATA_DIR, "--output_file", output_path],
        input=text,
        text=True,
        check=True,
        capture_output=True,
        timeout=90,
    )


@app.post("/assistant/voice")
async def assistant_voice(req: TTSRequest):
    """Generate a WAV using the app's default feminine Piper voice."""
    text = " ".join(req.text.split())
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="Prompt is too long")

    output = tempfile.NamedTemporaryFile(prefix="upi-assistant-", suffix=".wav", delete=False)
    output.close()
    try:
        await asyncio.to_thread(_render_assistant_voice, text, output.name)
    except FileNotFoundError:
        _remove_file(output.name)
        raise HTTPException(status_code=503, detail="Local voice is installing. Restart start.sh and try again.")
    except subprocess.TimeoutExpired:
        _remove_file(output.name)
        raise HTTPException(status_code=504, detail="Local voice took too long to respond")
    except subprocess.CalledProcessError as exc:
        _remove_file(output.name)
        detail = (exc.stderr or "Piper voice generation failed").strip()
        raise HTTPException(status_code=503, detail=detail[-300:])

    return FileResponse(
        output.name,
        media_type="audio/wav",
        filename="assistant.wav",
        background=BackgroundTask(_remove_file, output.name),
    )


# ---------- Background payment processor ----------

async def auto_reverse(txn_id: str, payer_bank_url: str, store: dict, reason: str) -> bool:
    """Return a stranded debit to the payer.

    When the credit leg fails, the money is sitting in the remitter bank's
    pool account. Leaving it there is the bug that used to destroy value.
    This is the trigger that was missing: the reversal endpoint existed but
    nothing ever called it.
    """
    store["hop_log"].append({
        "hop": "payer_bank",
        "event": f"Auto-reversal initiated — {reason}",
        "ts": datetime.utcnow().isoformat(),
    })
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{payer_bank_url}/reverse",
                                     json={"txn_id": txn_id, "reason": reason})
        if resp.status_code == 200:
            data = resp.json()
            store["new_balance"] = data.get("new_balance", store.get("new_balance"))
            store["reversal_entry_id"] = data.get("reversal_entry_id")
            store["hop_log"].append({
                "hop": "payer_bank",
                "event": f"Reversed ✓ — {data.get('message', 'funds returned to payer')}",
                "ts": datetime.utcnow().isoformat(),
            })
            return True
        store["hop_log"].append({
            "hop": "payer_bank",
            "event": f"REVERSAL FAILED: {resp.text[:160]} — funds held in pool, needs manual recon",
            "ts": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        store["hop_log"].append({
            "hop": "payer_bank",
            "event": f"REVERSAL ERROR: {e} — funds held in pool, needs manual recon",
            "ts": datetime.utcnow().isoformat(),
        })
    return False


async def resolve_stranded(txn_id: str, store: dict, cause: str):
    """Decide what to do when a payment broke after the debit went through.

    Any failure between the debit and the credit leaves value stranded in the
    remitter's pool. We must not guess: ask the beneficiary bank whether the
    credit actually landed (the ReqChkTxn pattern), and only reverse if it did
    not. A timeout or a dropped connection is not the same as a failure.
    """
    credited = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            chk = await client.get(f"{PAYEE_BANK_URL}/txn/{txn_id}")
        if chk.status_code == 200:
            credited = chk.json().get("status") == "credited"
        elif chk.status_code == 404:
            credited = False
    except Exception as chk_err:
        store["hop_log"].append({
            "hop": "payee_bank",
            "event": f"Status check unavailable ({chk_err}) — cannot confirm the credit leg",
            "ts": datetime.utcnow().isoformat(),
        })

    debited = store["hops"].get("payer_bank") == "completed"

    if credited:
        store["hops"]["payee_bank"] = "completed"
        store["status"] = "success"
        store["hop_log"].append({"hop": "payee_bank",
            "event": "Status check: the credit did land ✓ — transaction is good",
            "ts": datetime.utcnow().isoformat()})
    elif credited is False and debited:
        store["hop_log"].append({"hop": "payee_bank",
            "event": "Status check: no credit found — the debit is stranded",
            "ts": datetime.utcnow().isoformat()})
        if await auto_reverse(txn_id, PAYER_BANK_URL, store, cause):
            store["status"] = "reversed"
            store["error"] = f"Payment failed and was refunded automatically. ({cause})"
        else:
            store["status"] = "partial_failure"
            store["error"] = f"Payment failed and the refund did not go through. ({cause})"
    elif debited:
        # Could not reach the beneficiary bank to ask. Do NOT reverse blindly —
        # the credit may have landed. Flag for reconciliation instead.
        store["status"] = "partial_failure"
        store["error"] = (f"Debited but the credit leg is unconfirmed — awaiting "
                          f"status check. ({cause})")
    else:
        store["status"] = "failed"
        store["error"] = cause

    store["completed_at"] = datetime.utcnow().isoformat()
    supabase_db.sync_transaction(store)


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
            # Debited but not credited. The funds are in the remitter's pool —
            # recoverable, not lost — so return them to the payer immediately.
            err = credit_resp.json().get("detail", "Credit failed")
            store["hops"]["payee_bank"] = "failed"
            store["hop_log"].append({"hop": "payee_bank", "event": f"FAILED: {err}", "ts": datetime.utcnow().isoformat()})
            store["error_stage"] = "payee_bank"

            reversed_ok = await auto_reverse(txn_id, payer_bank_url, store,
                                             f"Credit leg failed: {err}")
            if reversed_ok:
                store["status"] = "reversed"
                store["error"] = f"Payment failed and was refunded automatically. ({err})"
            else:
                store["status"] = "partial_failure"
                store["error"] = f"Credit failed and the reversal did not go through. ({err})"
            store["completed_at"] = datetime.utcnow().isoformat()
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
        store["hop_log"].append({"hop": "unknown",
            "event": f"TIMEOUT: {e} — running status check",
            "ts": datetime.utcnow().isoformat()})
        await resolve_stranded(txn_id, store, f"Timed out: {e}")

    except Exception as e:
        # Includes ConnectError when a downstream bank is simply down. If the
        # debit already went through, this is a stranded payment, not a plain
        # failure — it must be resolved, not just logged.
        store["hop_log"].append({"hop": "unknown",
            "event": f"ERROR: {e}",
            "ts": datetime.utcnow().isoformat()})
        await resolve_stranded(txn_id, store, str(e))


# ---------- Reconciliation sweep ----------

RECON_INTERVAL_SECS = 20


async def recon_sweep():
    """Retry payments that were left unconfirmed because a bank was unreachable.

    `partial_failure` means we debited but could not find out whether the credit
    landed, so we deliberately did not reverse. Once the beneficiary bank is
    back, ask again and finish the job — either confirm the credit or refund.
    This is the ReqChkTxn sweep: no payment is allowed to stay stranded.
    """
    while True:
        await asyncio.sleep(RECON_INTERVAL_SECS)
        stuck = [t for t, s in txn_store.items() if s.get("status") == "partial_failure"]
        for txn_id in stuck:
            store = txn_store[txn_id]
            store["hop_log"].append({
                "hop": "psp",
                "event": "Reconciliation sweep: re-checking the credit leg",
                "ts": datetime.utcnow().isoformat(),
            })
            try:
                await resolve_stranded(txn_id, store, store.get("error", "unconfirmed"))
            except Exception as e:
                print(f"[recon] sweep failed for {txn_id}: {e}", flush=True)


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

        # Tell each bank to settle its own position. The bank posts the
        # POOL -> NOSTRO journal entry AND stamps the batch id on the
        # transactions it covered, in one transaction. Without this the pool
        # accounts only ever grow: the money never actually leaves them.
        # Only payments whose BOTH legs completed may be settled. A debit with
        # no matching credit is still recoverable from the pool and must stay
        # there until it is credited or reversed.
        matched = sorted({t["txn_id"] for t in unsettled_debits}
                         & {t["txn_id"] for t in unsettled_credits})
        cycle["matched_count"] = len(matched)
        cycle["unmatched_debits"] = sorted(
            {t["txn_id"] for t in unsettled_debits} - set(matched))

        settlement_posts = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            for name, url in (("payer", PAYER_BANK_URL), ("payee", PAYEE_BANK_URL)):
                try:
                    resp = await client.post(f"{url}/settlement/post",
                                             json={"cycle_id": cycle_id, "txn_ids": matched})
                    settlement_posts[name] = resp.json() if resp.status_code == 200 else {
                        "error": resp.text[:200]}
                except Exception as post_err:
                    settlement_posts[name] = {"error": str(post_err)}

        cycle["ledger_postings"] = settlement_posts

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
    asyncio.create_task(recon_sweep())


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
