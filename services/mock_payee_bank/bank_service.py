from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
from contextlib import contextmanager
import os
from datetime import datetime
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import supabase_db
import ledger as L

app = FastAPI(title="Mock Payee Bank Service", description="Simulated beneficiary/merchant bank for UPI flow demo", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payee_ledger.db")
BANK_CODE = "MOCKBANK2"         # the beneficiary bank institution


# ---------- Pydantic models ----------

class CreditRequest(BaseModel):
    txn_id: str
    vpa: str
    payer_vpa: str
    amount: float

class SettlementRequest(BaseModel):
    cycle_id: str
    txn_ids: list[str] = []


class StatusUpdateRequest(BaseModel):
    txn_id: str
    status: str
    settlement_batch_id: Optional[str] = None


# ---------- DB helpers ----------

def get_db():
    # Autocommit: transaction boundaries are controlled explicitly by db_txn().
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db_txn():
    """Serialised write transaction — see the payer bank for the rationale."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

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

    # ---- Ledger bootstrap -------------------------------------------------
    L.init_ledger(conn)
    L.system_accounts(conn, BANK_CODE)
    _migrate_balances_to_journal(conn)
    conn.close()


def acct_id(vpa: str) -> str:
    return f"{BANK_CODE}:{norm_vpa(vpa)}"

def norm_vpa(v: str) -> str:
    """VPAs are case-insensitive, like email local parts in practice.

    Registration already lower-cased on write, so any lookup that did not
    normalise could never find a handle the user typed with capitals.
    Normalise on every boundary instead of only on write.
    """
    return (v or "").strip().lower()


def _migrate_balances_to_journal(conn):
    """Book each legacy merchant balance once as an opening-balance entry,
    so the journal reproduces the balance the merchant already had."""
    for row in conn.execute("SELECT vpa, merchant_name, balance FROM accounts").fetchall():
        aid = acct_id(row["vpa"])
        L.ensure_account(conn, aid, BANK_CODE, L.MERCHANT, row["merchant_name"], row["vpa"])
        if conn.execute("SELECT 1 FROM postings WHERE account_id=? LIMIT 1", (aid,)).fetchone():
            continue
        paise = L.to_paise(row["balance"])
        if paise == 0:
            continue
        L.post_entry(
            conn,
            txn_ref=f"OPENING-{row['vpa']}",
            event_type="OPENING_BALANCE",
            narration=f"Opening balance migrated for {row['vpa']}",
            legs=[(f"{BANK_CODE}:OPENING", "DR", paise), (aid, "CR", paise)],
        )
    conn.commit()
    report = L.assert_books_balance(conn)
    print(f"[ledger] {BANK_CODE} books balanced: {report['total_dr_paise']} paise DR/CR", flush=True)


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
    """Derived from the journal, never read from a stored column."""
    vpa = norm_vpa(vpa)
    conn = get_db()
    try:
        row = conn.execute("SELECT vpa, merchant_name FROM accounts WHERE vpa=?", (vpa,)).fetchone()
        if not row:
            raise HTTPException(404, "Merchant account not found")
        try:
            paise = L.ledger_balance(conn, acct_id(vpa))
        except KeyError:
            # Seeded merchant with no ledger account yet — it is opened lazily
            # on its first credit, so report zero rather than erroring.
            paise = 0
        return {
            "vpa": row["vpa"],
            "merchant_name": row["merchant_name"],
            "balance": L.to_rupees(paise),
            "ledger_balance": L.to_rupees(paise),
        }
    finally:
        conn.close()

@app.get("/ledger/trial-balance")
def get_trial_balance():
    conn = get_db()
    try:
        return {
            "bank_code": BANK_CODE,
            "integrity": L.assert_books_balance(conn),
            "accounts": L.trial_balance(conn),
        }
    finally:
        conn.close()

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
    req.vpa = norm_vpa(req.vpa)
    req.payer_vpa = norm_vpa(req.payer_vpa)
    not_found = False
    duplicate = False
    new_balance = None
    entry_id = None

    with db_txn() as conn:
        existing = conn.execute("SELECT 1 FROM transactions WHERE txn_id=?", (req.txn_id,)).fetchone()
        if existing:
            duplicate = True
        else:
            aid = acct_id(req.vpa)
            account = conn.execute("SELECT vpa, merchant_name FROM accounts WHERE vpa=?", (req.vpa,)).fetchone()
            if not account:
                not_found = True
            else:
                L.ensure_account(conn, aid, BANK_CODE, L.MERCHANT, account["merchant_name"], req.vpa)
                amount_paise = L.to_paise(req.amount)

                # The credit leg is funded from THIS bank's pool account, not
                # from the payer's bank. The two banks never touch each other's
                # books — NPCI reconciles the pools at settlement.
                entry_id = L.post_entry(
                    conn,
                    txn_ref=req.txn_id,
                    event_type="CREDIT_LEG",
                    narration=f"UPI credit ₹{req.amount:,.2f} {req.payer_vpa} → {req.vpa}",
                    legs=[
                        (f"{BANK_CODE}:POOL", "DR", amount_paise),
                        (aid,                 "CR", amount_paise),
                    ],
                )
                new_balance = L.to_rupees(L.ledger_balance(conn, aid))
                conn.execute("UPDATE accounts SET balance=? WHERE vpa=?", (new_balance, req.vpa))
                conn.execute(
                    "INSERT INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp) VALUES (?,?,?,?,?,?)",
                    (req.txn_id, req.payer_vpa, req.vpa, req.amount, "credited", datetime.utcnow().isoformat())
                )

    if duplicate:
        return {"success": True, "status": "credited", "duplicate": True,
                "message": "Already processed (idempotent)"}
    if not_found:
        raise HTTPException(404, f"Merchant account not found: {req.vpa}")

    supabase_db.credit_payee_account(req.vpa, req.amount, new_balance=new_balance)

    return {
        "success": True,
        "status": "credited",
        "txn_id": req.txn_id,
        "amount": req.amount,
        "new_balance": new_balance,
        "entry_id": entry_id,
        "message": f"₹{req.amount:,.0f} credited to {req.vpa}"
    }


@app.post("/settlement/post")
def settlement_post(req: SettlementRequest):
    """Settle an explicit set of transactions against our RBI account (NOSTRO).

        DR NOSTRO (cash arrives in our RBI account) / CR POOL (our shortfall is made good)

    The transaction set is chosen by the switch, not by this bank, and contains
    only payments whose BOTH legs completed. That matters: a payment that was
    debited but never credited must keep its value in the pool, where a
    reversal can still return it. Settling it out would strand the money in
    NOSTRO with no way back — the money-destruction bug, one layer down.

    Idempotent per cycle_id.
    """
    duplicate = False
    settled, total_paise, entry_id = [], 0, None

    with db_txn() as conn:
        if conn.execute(
            "SELECT 1 FROM journal_entries WHERE txn_ref=? AND event_type='SETTLEMENT'",
            (req.cycle_id,),
        ).fetchone():
            duplicate = True
        elif req.txn_ids:
            placeholders = ",".join("?" * len(req.txn_ids))
            rows = conn.execute(
                f"""SELECT txn_id, amount FROM transactions
                    WHERE txn_id IN ({placeholders}) AND settlement_batch_id IS NULL""",
                req.txn_ids,
            ).fetchall()
            settled = [r["txn_id"] for r in rows]
            total_paise = sum(L.to_paise(r["amount"]) for r in rows)

            if total_paise > 0:
                entry_id = L.post_entry(
                    conn,
                    txn_ref=req.cycle_id,
                    event_type="SETTLEMENT",
                    narration=f"Net settlement of {len(settled)} completed txn(s)",
                legs=[(f"{BANK_CODE}:NOSTRO", "DR", total_paise),
                      (f"{BANK_CODE}:POOL",   "CR", total_paise)],
                )
            for txn_id in settled:
                conn.execute(
                    "UPDATE transactions SET status='success', settlement_batch_id=? WHERE txn_id=?",
                    (req.cycle_id, txn_id),
                )

    if duplicate:
        return {"success": True, "duplicate": True, "cycle_id": req.cycle_id,
                "message": "Cycle already settled (idempotent)"}

    return {
        "success": True,
        "cycle_id": req.cycle_id,
        "bank_code": BANK_CODE,
        "settled_count": len(settled),
        "settled_amount": L.to_rupees(total_paise),
        "entry_id": entry_id,
        "txn_ids": settled,
    }


@app.get("/txn/{txn_id}")
def get_txn(txn_id: str):
    """Status enquiry for one transaction — the ReqChkTxn equivalent.

    The PSP calls this after a timeout to find out whether the credit leg
    actually landed, because a timeout is not a failure.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT txn_id, payer_vpa, payee_vpa, amount, status, timestamp, settlement_batch_id "
            "FROM transactions WHERE txn_id=?", (txn_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No such transaction at this bank: {txn_id}")
        return dict(row)
    finally:
        conn.close()


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
