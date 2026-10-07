from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import supabase_db
import ledger as L

app = FastAPI(title="Mock Payer Bank Service", description="Simulated payer-side bank for UPI flow demo", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payer_ledger.db")
BANK_CODE = "MOCKBANK"          # this service is one bank institution
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
    # isolation_level=None puts the driver in autocommit mode so that WE control
    # transaction boundaries explicitly via db_txn(), rather than letting the
    # driver open an implicit DEFERRED transaction on the first DML statement.
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db_txn():
    """A serialised write transaction.

    BEGIN IMMEDIATE takes SQLite's write lock up front, so a read-check-write
    sequence (read balance -> verify funds -> debit) cannot interleave with a
    concurrent one. Without this, five simultaneous debits against a Rs 1,000
    account all pass the balance check and all post, driving it to -Rs 2,000.

    Never hold this across a network call - do auth/HTTP work before entering.
    """
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

    # ---- Ledger bootstrap -------------------------------------------------
    L.init_ledger(conn)
    L.system_accounts(conn, BANK_CODE)
    _migrate_balances_to_journal(conn)
    conn.close()


def acct_id(vpa: str) -> str:
    """Chart-of-accounts id for a customer VPA held at this bank."""
    return f"{BANK_CODE}:{norm_vpa(vpa)}"

def norm_vpa(v: str) -> str:
    """VPAs are case-insensitive, like email local parts in practice.

    Registration already lower-cased on write, so any lookup that did not
    normalise could never find a handle the user typed with capitals.
    Normalise on every boundary instead of only on write.
    """
    return (v or "").strip().lower()


def _migrate_balances_to_journal(conn):
    """Give every legacy account an opening-balance entry.

    The old design kept truth in accounts.balance. The journal is now truth,
    so each existing balance is booked once as
        DR <bank> opening-contra / CR <customer>
    which makes the journal reproduce the balance the customer already had.
    Idempotent: an account that already has postings is skipped.
    """
    for row in conn.execute("SELECT vpa, holder_name, balance FROM accounts").fetchall():
        aid = acct_id(row["vpa"])
        L.ensure_account(conn, aid, BANK_CODE, L.CUSTOMER, row["holder_name"], row["vpa"])
        already = conn.execute(
            "SELECT 1 FROM postings WHERE account_id=? LIMIT 1", (aid,)
        ).fetchone()
        if already:
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
    return {"status": "ok", "service": "Payer Bank", "port": 5003}

# NOTE: there is deliberately no "list every account" endpoint.
# It used to back the sign-in screen's account picker, which meant any visitor
# could read every customer's VPA, holder name and balance before
# authenticating. The client now only offers accounts already bound to the
# device, so look-ups go through /balance/{vpa} with a VPA the caller knows.

@app.post("/accounts/register")
def register_account(req: RegisterAccountRequest):
    vpa = req.vpa.strip().lower()
    if not vpa or "@" not in vpa:
        raise HTTPException(400, "Valid VPA is required (e.g. user@hdfcbank)")
    if not (4 <= len(req.pin) <= 6) or not req.pin.isdigit():
        raise HTTPException(400, "PIN must be 4-6 digits")

    # 1. Open the account in BOTH the identity table and the ledger.
    #
    # The ledger is the source of truth, so an account that exists only in
    # `accounts` is unusable: every balance read and every debit would fail
    # looking up its chart-of-accounts entry. An opening balance is money
    # entering the books and must be a balanced journal entry like any other.
    opening = float(req.balance)
    with db_txn() as conn:
        aid = acct_id(vpa)
        existing_postings = conn.execute(
            "SELECT 1 FROM postings WHERE account_id=? LIMIT 1", (aid,)
        ).fetchone()

        conn.execute(
            "INSERT OR REPLACE INTO accounts (vpa, holder_name, balance) VALUES (?, ?, ?)",
            (vpa, req.holder_name.strip(), opening)
        )
        L.ensure_account(conn, aid, BANK_CODE, L.CUSTOMER, req.holder_name.strip(), vpa)

        if existing_postings:
            # Re-registering an account that already has history. The journal
            # wins — never silently reset a balance that has transactions
            # behind it. Re-sync the cache column to the ledger instead.
            opening = L.to_rupees(L.ledger_balance(conn, aid))
            conn.execute("UPDATE accounts SET balance=? WHERE vpa=?", (opening, vpa))
        elif L.to_paise(opening) > 0:
            paise = L.to_paise(opening)
            L.post_entry(
                conn,
                txn_ref=f"OPENING-{vpa}",
                event_type="OPENING_BALANCE",
                narration=f"Account opened for {vpa} with ₹{opening:,.2f}",
                legs=[(f"{BANK_CODE}:OPENING", "DR", paise), (aid, "CR", paise)],
            )

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
        "balance": opening,
        "bank_name": req.bank_name,
        "message": f"Account linked successfully to {req.bank_name}"
    }

@app.get("/balance/{vpa}")
def get_balance(vpa: str):
    """Balance is DERIVED from the journal, never read from a stored column."""
    vpa = norm_vpa(vpa)
    conn = get_db()
    try:
        row = conn.execute("SELECT vpa, holder_name FROM accounts WHERE vpa=?", (vpa,)).fetchone()
        if not row:
            sb_acc = supabase_db.get_payer_account(vpa)
            if sb_acc:
                return sb_acc
            raise HTTPException(404, "Account not found")

        aid = acct_id(vpa)
        try:
            ledger_paise = L.ledger_balance(conn, aid)
        except KeyError:
            # In `accounts` but not in the chart of accounts — an account
            # opened before the ledger existed, or by a path that forgot to
            # open it. Report zero rather than a 500; /accounts/register and
            # the startup migration are what repair it.
            raise HTTPException(
                409,
                f"{vpa} has no ledger account. Re-register it so an opening "
                f"balance entry can be posted."
            )
        held_paise = L.active_holds(conn, aid)
        return {
            "vpa": row["vpa"],
            "holder_name": row["holder_name"],
            # `balance` stays the available figure so existing callers keep working
            "balance": L.to_rupees(ledger_paise - held_paise),
            "ledger_balance": L.to_rupees(ledger_paise),
            "available_balance": L.to_rupees(ledger_paise - held_paise),
            "held": L.to_rupees(held_paise),
        }
    finally:
        conn.close()


@app.get("/ledger/trial-balance")
def get_trial_balance():
    """Every account's balance recomputed from the journal, plus the proof
    that total debits equal total credits."""
    conn = get_db()
    try:
        return {
            "bank_code": BANK_CODE,
            "integrity": L.assert_books_balance(conn),
            "accounts": L.trial_balance(conn),
        }
    finally:
        conn.close()


@app.get("/ledger/statement/{vpa}")
def get_statement(vpa: str):
    """The customer's real statement: every posting that touched their account."""
    vpa = norm_vpa(vpa)
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT je.entry_id, je.txn_ref, je.rrn, je.event_type, je.reverses,
                      je.narration, je.posted_at, p.direction, p.amount_paise
               FROM postings p JOIN journal_entries je ON je.entry_id = p.entry_id
               WHERE p.account_id = ?
               ORDER BY je.posted_at ASC, p.posting_id ASC""",
            (acct_id(vpa),),
        ).fetchall()
        running = 0
        out = []
        for r in rows:
            running += r["amount_paise"] if r["direction"] == "CR" else -r["amount_paise"]
            out.append({
                "entry_id": r["entry_id"], "txn_ref": r["txn_ref"], "rrn": r["rrn"],
                "event_type": r["event_type"], "reverses": r["reverses"],
                "narration": r["narration"], "posted_at": r["posted_at"],
                "direction": r["direction"], "amount": L.to_rupees(r["amount_paise"]),
                "running_balance": L.to_rupees(running),
            })
        return {"vpa": vpa, "entries": out, "closing_balance": L.to_rupees(running)}
    finally:
        conn.close()


class SettlementRequest(BaseModel):
    cycle_id: str
    txn_ids: list[str] = []


class ReversalRequest(BaseModel):
    txn_id: str
    reason: str = "Credit leg failed"


@app.post("/settlement/post")
def settlement_post(req: SettlementRequest):
    """Settle an explicit set of transactions against our RBI account (NOSTRO).

        DR POOL (we owe the network less) / CR NOSTRO (cash leaves our RBI account)

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
                legs=[(f"{BANK_CODE}:POOL",   "DR", total_paise),
                      (f"{BANK_CODE}:NOSTRO", "CR", total_paise)],
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


@app.post("/reverse")
def reverse(req: ReversalRequest):
    """Return funds sitting in the pool back to the payer.

    This is what makes a failed credit leg recoverable. The original debit
    entry is never touched — a new, opposite entry is posted against it, so
    the customer's statement correctly shows both the debit and the reversal.
    """
    with db_txn() as conn:
        orig = conn.execute(
            """SELECT entry_id, txn_ref FROM journal_entries
               WHERE txn_ref=? AND event_type='DEBIT_LEG'""",
            (req.txn_id,),
        ).fetchone()
        if not orig:
            raise HTTPException(404, f"No debit leg found for {req.txn_id}")

        already = conn.execute(
            "SELECT 1 FROM journal_entries WHERE reverses=?", (orig["entry_id"],)
        ).fetchone()
        if already:
            return {"success": True, "duplicate": True, "message": "Already reversed (idempotent)"}

        txn = conn.execute(
            "SELECT payer_vpa, amount FROM transactions WHERE txn_id=?", (req.txn_id,)
        ).fetchone()
        aid = acct_id(txn["payer_vpa"])
        amount_paise = L.to_paise(txn["amount"])

        entry_id = L.post_entry(
            conn,
            txn_ref=req.txn_id,
            event_type="REVERSAL",
            reverses=orig["entry_id"],
            narration=f"Reversal of ₹{txn['amount']:,.2f} — {req.reason}",
            legs=[
                (f"{BANK_CODE}:POOL", "DR", amount_paise),
                (aid,                 "CR", amount_paise),
            ],
        )
        new_balance = L.to_rupees(L.ledger_balance(conn, aid))
        conn.execute("UPDATE accounts SET balance=? WHERE vpa=?", (new_balance, txn["payer_vpa"]))
        conn.execute("UPDATE transactions SET status='reversed' WHERE txn_id=?", (req.txn_id,))

    return {
        "success": True,
        "txn_id": req.txn_id,
        "reversal_entry_id": entry_id,
        "reverses": orig["entry_id"],
        "new_balance": new_balance,
        "message": f"₹{txn['amount']:,.2f} returned to {txn['payer_vpa']}",
    }


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
    req.vpa = norm_vpa(req.vpa)
    req.payee_vpa = norm_vpa(req.payee_vpa)

    # ------------------------------------------------------------------
    # Phase 0: cheap idempotency pre-check (re-checked under lock below).
    # ------------------------------------------------------------------
    conn = get_db()
    existing = conn.execute("SELECT * FROM transactions WHERE txn_id=?", (req.txn_id,)).fetchone()
    conn.close()
    if existing:
        ex = dict(existing)
        if ex["status"] == "debited":
            return {"success": True, "status": "debited", "duplicate": True,
                    "message": "Already processed (idempotent)"}
        raise HTTPException(409, f"Transaction already exists with status: {ex['status']}")

    # ------------------------------------------------------------------
    # Phase 1: authenticate. This makes a network call to the auth service,
    # so it MUST happen before we take the DB write lock.
    # ------------------------------------------------------------------
    auth_method = None

    if req.step_up_token:
        import httpx as _httpx
        try:
            resp = _httpx.post(f"{AUTH_SERVICE_URL}/step-up/verify", json={
                "vpa": req.vpa, "token": req.step_up_token
            }, timeout=5.0)
        except Exception as e:
            raise HTTPException(503, f"Auth service unreachable: {e}")

        if resp.status_code != 200:
            detail = resp.json().get("detail", "Step-up token rejected") if resp.content else "Step-up token rejected"
            raise HTTPException(401, detail)

        auth_method = resp.json().get("method", "webauthn")

    elif req.pin:
        # Legacy raw-PIN path — kept only for backward compatibility during migration.
        # New clients should go through mock_auth (/pin/verify or /webauthn/auth/complete) instead.
        attempts = pin_attempts.get(req.vpa, 0)
        if attempts >= 3:
            raise HTTPException(403, "Account locked: 3 wrong PIN attempts. Contact your bank.")

        if req.pin != CORRECT_PIN:
            pin_attempts[req.vpa] = attempts + 1
            remaining = 3 - pin_attempts[req.vpa]
            with db_txn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp,failure_reason) VALUES (?,?,?,?,?,?,?)",
                    (req.txn_id, req.vpa, req.payee_vpa, req.amount, "failed",
                     datetime.utcnow().isoformat(), f"PIN mismatch — {remaining} attempt(s) left")
                )
            raise HTTPException(401, f"Incorrect PIN. {remaining} attempt(s) remaining")

        pin_attempts[req.vpa] = 0
        auth_method = "pin_legacy"
    else:
        raise HTTPException(400, "Must provide either step_up_token (WebAuthn/PIN) or a legacy pin")

    # ------------------------------------------------------------------
    # Phase 2: the money move, under a single write lock.
    #
    # Idempotency re-check, funds check and the debit posting all happen
    # inside BEGIN IMMEDIATE. This is what makes concurrent debits safe:
    # the second request blocks until the first commits, then sees the
    # already-reduced balance and is correctly rejected.
    # ------------------------------------------------------------------
    insufficient = None
    not_found = False
    duplicate_status = None
    new_balance = None
    entry_id = None

    with db_txn() as conn:
        # Re-check under the lock — a concurrent request may have inserted
        # this txn_id between our pre-check and acquiring the lock.
        dup = conn.execute("SELECT status FROM transactions WHERE txn_id=?", (req.txn_id,)).fetchone()
        if dup:
            duplicate_status = dup["status"]
        else:
            aid = acct_id(req.vpa)
            account = conn.execute("SELECT vpa FROM accounts WHERE vpa=?", (req.vpa,)).fetchone()
            has_ledger = conn.execute(
                "SELECT 1 FROM ledger_accounts WHERE account_id=?", (aid,)
            ).fetchone()
            if not account or not has_ledger:
                # No chart-of-accounts entry means we cannot post a balanced
                # entry for this account, so refuse rather than 500.
                not_found = True
            else:
                amount_paise = L.to_paise(req.amount)

                # Earmark first, then check: available = ledger - active holds.
                # The hold is what stops a second authorisation against the
                # same funds while this one is still in flight.
                hold_id = L.place_hold(conn, aid, req.txn_id, amount_paise)
                available = L.available_balance(conn, aid)

                if available < 0:
                    L.settle_hold(conn, hold_id, "RELEASED")
                    insufficient = L.to_rupees(L.ledger_balance(conn, aid))
                    conn.execute(
                        "INSERT OR IGNORE INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp,failure_reason) VALUES (?,?,?,?,?,?,?)",
                        (req.txn_id, req.vpa, req.payee_vpa, req.amount, "failed",
                         datetime.utcnow().isoformat(),
                         f"Insufficient balance (available ₹{insufficient:,.0f}, needed ₹{req.amount:,.0f})")
                    )
                else:
                    # The debit leg. Money does NOT go to the payee's bank — it
                    # moves into THIS bank's UPI pool account, where it sits as
                    # an inter-bank obligation until the settlement cycle.
                    # That is why a failed credit leg is recoverable: the funds
                    # are still on our books, in the pool.
                    entry_id = L.post_entry(
                        conn,
                        txn_ref=req.txn_id,
                        event_type="DEBIT_LEG",
                        narration=f"UPI debit ₹{req.amount:,.2f} {req.vpa} → {req.payee_vpa}",
                        legs=[
                            (aid,                     "DR", amount_paise),
                            (f"{BANK_CODE}:POOL",     "CR", amount_paise),
                        ],
                    )
                    L.settle_hold(conn, hold_id, "CAPTURED")

                    new_balance = L.to_rupees(L.ledger_balance(conn, aid))
                    # accounts.balance is now only a denormalised cache of the
                    # journal, kept in step for legacy readers. Never truth.
                    conn.execute("UPDATE accounts SET balance=? WHERE vpa=?", (new_balance, req.vpa))
                    conn.execute(
                        "INSERT INTO transactions (txn_id,payer_vpa,payee_vpa,amount,status,timestamp) VALUES (?,?,?,?,?,?)",
                        (req.txn_id, req.vpa, req.payee_vpa, req.amount, "debited", datetime.utcnow().isoformat())
                    )

    # ------------------------------------------------------------------
    # Phase 3: outcomes (outside the lock).
    # ------------------------------------------------------------------
    if duplicate_status is not None:
        if duplicate_status == "debited":
            return {"success": True, "status": "debited", "duplicate": True,
                    "message": "Already processed (idempotent)"}
        raise HTTPException(409, f"Transaction already exists with status: {duplicate_status}")

    if not_found:
        raise HTTPException(404, "Account not found")

    if insufficient is not None:
        raise HTTPException(
            402,
            f"Insufficient balance. Available: ₹{insufficient:,.2f}, Required: ₹{req.amount:,.2f}"
        )

    # Sync authoritative SQLite balance to Supabase
    supabase_db.debit_payer_account(req.vpa, req.amount, new_balance=new_balance)

    return {
        "success": True,
        "status": "debited",
        "txn_id": req.txn_id,
        "amount": req.amount,
        "new_balance": new_balance,
        "entry_id": entry_id,
        "auth_method": auth_method,
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
