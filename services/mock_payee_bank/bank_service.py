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

app = FastAPI(title="Mock Payee Bank Service", description="Simulated beneficiary/merchant bank for UPI flow demo", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payee_ledger.db")


# ---------- Pydantic models ----------

class CreditRequest(BaseModel):
    txn_id: str
    vpa: str
    payer_vpa: str
    amount: float

class StatusUpdateRequest(BaseModel):
    txn_id: str
    status: str
    settlement_batch_id: Optional[str] = None


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
            merchant_name TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            category TEXT DEFAULT 'general',
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
        "INSERT OR IGNORE INTO accounts (vpa, merchant_name, balance, category) VALUES (?,?,?,?)",
        [
            ("sharmastore@mockbank2", "Sharma Store",    0.0, "grocery"),
            ("blinkit@mockbank2",     "Blinkit",         0.0, "delivery"),
            ("swiggy@mockbank2",      "Swiggy",          0.0, "food"),
            ("zomato@mockbank2",      "Zomato",          0.0, "food"),
            ("amazon@mockbank2",      "Amazon",          0.0, "ecommerce"),
            ("petrolpump@mockbank2",  "HP Petrol Pump",  0.0, "fuel"),
        ]
    )
    conn.commit()
    conn.close()

init_db()


# ---------- Endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok", "service": "Payee Bank", "port": 5004}

@app.get("/accounts")
def list_accounts():
    # SQLite is the live ledger; Supabase is a sync target
    conn = get_db()
    rows = conn.execute("SELECT vpa, merchant_name, balance, category FROM accounts").fetchall()
    conn.close()
    if rows:
        return [dict(r) for r in rows]
    return supabase_db.list_payee_accounts()

@app.get("/balance/{vpa}")
def get_balance(vpa: str):
    conn = get_db()
    row = conn.execute("SELECT vpa, merchant_name, balance FROM accounts WHERE vpa=?", (vpa,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    sb_acc = supabase_db.get_payee_account(vpa)
    if sb_acc:
        return sb_acc
    raise HTTPException(404, "Merchant account not found")

@app.get("/ledger/{vpa}")
def get_ledger(vpa: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE payee_vpa=? ORDER BY timestamp DESC LIMIT 25",
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

@app.post("/credit")
def credit(req: CreditRequest):
    conn = get_db()

    # --- Idempotency ---
    existing = conn.execute("SELECT * FROM transactions WHERE txn_id=?", (req.txn_id,)).fetchone()
    if existing:
        conn.close()
        return {"success": True, "status": "credited", "duplicate": True, "message": "Already processed (idempotent)"}

    # --- Merchant exists? ---
    account = conn.execute("SELECT balance FROM accounts WHERE vpa=?", (req.vpa,)).fetchone()
    if not account:
        conn.close()
        raise HTTPException(404, f"Merchant account not found: {req.vpa}")

    # --- Credit ---
    conn.execute("UPDATE accounts SET balance = balance + ? WHERE vpa=?", (req.amount, req.vpa))
    conn.execute(
        "INSERT INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp) VALUES (?,?,?,?,?,?)",
        (req.txn_id, req.payer_vpa, req.vpa, req.amount, "credited", datetime.utcnow().isoformat())
    )
    conn.commit()

    new_balance = conn.execute("SELECT balance FROM accounts WHERE vpa=?", (req.vpa,)).fetchone()["balance"]
    conn.close()

    # Sync authoritative SQLite balance to Supabase
    supabase_db.credit_payee_account(req.vpa, req.amount, new_balance=new_balance)

    return {
        "success": True,
        "status": "credited",
        "txn_id": req.txn_id,
        "amount": req.amount,
        "new_balance": new_balance,
        "message": f"₹{req.amount:,.0f} credited to {req.vpa}"
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
    """Reset all merchant balances to zero — useful for demo restarts."""
    conn = get_db()
    conn.execute("UPDATE accounts SET balance=0")
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    supabase_db.reset_payee_accounts()
    return {"success": True, "message": "Payee bank reset to initial state"}
