from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os
from datetime import datetime
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import supabase_db

app = FastAPI(title="Mock Payer Bank Service", description="Simulated payer-side bank for UPI flow demo", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payer_ledger.db")
CORRECT_PIN = "1234"  # legacy fallback only — real PIN verification now lives in mock_auth (hashed)
AUTH_SERVICE_URL = os.environ.get("AUTH_SERVICE_URL", "http://localhost:5005")
pin_attempts: dict[str, int] = {}


# ---------- Pydantic models ----------

class DebitRequest(BaseModel):
    txn_id: str
    vpa: str
    payee_vpa: str
    amount: float
    pin: Optional[str] = None            # legacy path — still supported
    step_up_token: Optional[str] = None  # new path — issued after WebAuthn/PIN via mock_auth

class StatusUpdateRequest(BaseModel):
    txn_id: str
    status: str
    settlement_batch_id: Optional[str] = None

class RegisterAccountRequest(BaseModel):
    vpa: str
    holder_name: str
    balance: float = 50000.0
    pin: str = "1234"
    bank_name: Optional[str] = "HDFC Bank"


# ---------- DB helpers ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            vpa TEXT PRIMARY KEY,
            holder_name TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id TEXT UNIQUE NOT NULL,
            payer_vpa TEXT NOT NULL,
            payee_vpa TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            settlement_batch_id TEXT,
            failure_reason TEXT
        )
    """)
    c.executemany(
        "INSERT OR IGNORE INTO accounts (vpa, holder_name, balance) VALUES (?,?,?)",
        [
            ("you@mockbank",   "Keyur Arbhelonde", 50000.0),
            ("priya@mockbank", "Priya Sharma",      35000.0),
            ("rahul@mockbank", "Rahul Mehta",        22000.0),
        ]
    )
    conn.commit()
    conn.close()

init_db()


# ---------- Endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok", "service": "Payer Bank", "port": 5003}

@app.get("/accounts")
def list_accounts():
    # SQLite is the live ledger; Supabase is a sync target
    conn = get_db()
    rows = conn.execute("SELECT vpa, holder_name, balance FROM accounts").fetchall()
    conn.close()
    if rows:
        return [dict(r) for r in rows]
    return supabase_db.list_payer_accounts()

@app.post("/accounts/register")
def register_account(req: RegisterAccountRequest):
    vpa = req.vpa.strip().lower()
    if not vpa or "@" not in vpa:
        raise HTTPException(400, "Valid VPA is required (e.g. user@hdfcbank)")
    if not (4 <= len(req.pin) <= 6) or not req.pin.isdigit():
        raise HTTPException(400, "PIN must be 4-6 digits")

    # 1. Store in SQLite
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO accounts (vpa, holder_name, balance) VALUES (?, ?, ?)",
        (vpa, req.holder_name.strip(), float(req.balance))
    )
    conn.commit()
    conn.close()

    # 2. Sync to Supabase if reachable
    try:
        import httpx as _httpx
        _httpx.post(
            f"{supabase_db.REST_URL}/payer_accounts",
            headers=supabase_db.HEADERS,
            json={
                "vpa": vpa,
                "holder_name": req.holder_name.strip(),
                "balance": round(float(req.balance), 2),
                "pin": req.pin,
            },
            timeout=4.0
        )
    except Exception:
        pass

    # 3. Register hashed PIN in mock_auth service
    try:
        import httpx as _httpx
        _httpx.post(
            f"{AUTH_SERVICE_URL}/pin/set",
            json={"vpa": vpa, "pin": req.pin},
            timeout=4.0
        )
    except Exception as e:
        print(f"[bank] Failed to set PIN in auth service: {e}", flush=True)

    return {
        "success": True,
        "vpa": vpa,
        "holder_name": req.holder_name.strip(),
        "balance": req.balance,
        "bank_name": req.bank_name,
        "message": f"Account linked successfully to {req.bank_name}"
    }

@app.get("/balance/{vpa}")
def get_balance(vpa: str):
    conn = get_db()
    row = conn.execute("SELECT vpa, holder_name, balance FROM accounts WHERE vpa=?", (vpa,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    sb_acc = supabase_db.get_payer_account(vpa)
    if sb_acc:
        return sb_acc
    raise HTTPException(404, "Account not found")

@app.get("/ledger/{vpa}")
def get_ledger(vpa: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE payer_vpa=? ORDER BY timestamp DESC LIMIT 25",
        (vpa,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/all-transactions")
def all_transactions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM transactions ORDER BY timestamp DESC LIMIT 100").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/debit")
def debit(req: DebitRequest):
    conn = get_db()

    # --- Idempotency ---
    existing = conn.execute("SELECT * FROM transactions WHERE txn_id=?", (req.txn_id,)).fetchone()
    if existing:
        conn.close()
        ex = dict(existing)
        if ex["status"] == "debited":
            return {"success": True, "status": "debited", "duplicate": True, "message": "Already processed (idempotent)"}
        raise HTTPException(409, f"Transaction already exists with status: {ex['status']}")

    # --- Authentication: step-up token (WebAuthn/biometric, or PIN-issued) preferred, raw PIN as legacy fallback ---
    auth_method = None

    if req.step_up_token:
        import httpx as _httpx
        try:
            resp = _httpx.post(f"{AUTH_SERVICE_URL}/step-up/verify", json={
                "vpa": req.vpa, "token": req.step_up_token
            }, timeout=5.0)
        except Exception as e:
            conn.close()
            raise HTTPException(503, f"Auth service unreachable: {e}")

        if resp.status_code != 200:
            conn.close()
            detail = resp.json().get("detail", "Step-up token rejected") if resp.content else "Step-up token rejected"
            raise HTTPException(401, detail)

        auth_method = resp.json().get("method", "webauthn")

    elif req.pin:
        # Legacy raw-PIN path — kept only for backward compatibility during migration.
        # New clients should go through mock_auth (/pin/verify or /webauthn/auth/complete) instead.
        attempts = pin_attempts.get(req.vpa, 0)
        if attempts >= 3:
            conn.close()
            raise HTTPException(403, "Account locked: 3 wrong PIN attempts. Contact your bank.")

        if req.pin != CORRECT_PIN:
            pin_attempts[req.vpa] = attempts + 1
            remaining = 3 - pin_attempts[req.vpa]
            conn.execute(
                "INSERT INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp,failure_reason) VALUES (?,?,?,?,?,?,?)",
                (req.txn_id, req.vpa, req.payee_vpa, req.amount, "failed",
                 datetime.utcnow().isoformat(), f"PIN mismatch — {remaining} attempt(s) left")
            )
            conn.commit()
            conn.close()
            raise HTTPException(401, f"Incorrect PIN. {remaining} attempt(s) remaining")

        pin_attempts[req.vpa] = 0
        auth_method = "pin_legacy"
    else:
        conn.close()
        raise HTTPException(400, "Must provide either step_up_token (WebAuthn/PIN) or a legacy pin")

    # --- Balance check ---
    account = conn.execute("SELECT balance FROM accounts WHERE vpa=?", (req.vpa,)).fetchone()
    if not account:
        conn.close()
        raise HTTPException(404, "Account not found")

    if account["balance"] < req.amount:
        conn.execute(
            "INSERT INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp,failure_reason) VALUES (?,?,?,?,?,?,?)",
            (req.txn_id, req.vpa, req.payee_vpa, req.amount, "failed",
             datetime.utcnow().isoformat(),
             f"Insufficient balance (available ₹{account['balance']:,.0f}, needed ₹{req.amount:,.0f})")
        )
        conn.commit()
        conn.close()
        raise HTTPException(402, f"Insufficient balance. Available: ₹{account['balance']:,.2f}, Required: ₹{req.amount:,.2f}")

    # --- Debit ---
    conn.execute("UPDATE accounts SET balance = balance - ? WHERE vpa=?", (req.amount, req.vpa))
    conn.execute(
        "INSERT INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp) VALUES (?,?,?,?,?,?)",
        (req.txn_id, req.vpa, req.payee_vpa, req.amount, "debited", datetime.utcnow().isoformat())
    )
    conn.commit()

    new_balance = conn.execute("SELECT balance FROM accounts WHERE vpa=?", (req.vpa,)).fetchone()["balance"]
    conn.close()

    # Sync authoritative SQLite balance to Supabase
    supabase_db.debit_payer_account(req.vpa, req.amount, new_balance=new_balance)

    return {
        "success": True,
        "status": "debited",
        "txn_id": req.txn_id,
        "amount": req.amount,
        "new_balance": new_balance,
        "message": f"₹{req.amount:,.0f} debited from {req.vpa}"
    }

@app.post("/update-status")
def update_status(req: StatusUpdateRequest):
    conn = get_db()
    if req.settlement_batch_id:
        conn.execute("UPDATE transactions SET status=?, settlement_batch_id=? WHERE txn_id=?",
                     (req.status, req.settlement_batch_id, req.txn_id))
    else:
        conn.execute("UPDATE transactions SET status=? WHERE txn_id=?", (req.status, req.txn_id))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/reset")
def reset():
    """Reset all balances to initial seed values — useful for demo restarts."""
    conn = get_db()
    conn.execute("UPDATE accounts SET balance=50000 WHERE vpa='you@mockbank'")
    conn.execute("UPDATE accounts SET balance=35000 WHERE vpa='priya@mockbank'")
    conn.execute("UPDATE accounts SET balance=22000 WHERE vpa='rahul@mockbank'")
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    pin_attempts.clear()
    supabase_db.reset_payer_accounts()
    return {"success": True, "message": "Payer bank reset to initial state"}
