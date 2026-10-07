"""
Double-entry ledger core — shared by every bank service in the simulator.

Design rules, all of which mirror real core banking:

1.  **Money is stored in integer paise.** Never floats. 0.1 + 0.2 != 0.3 in
    binary floating point, and a ledger that cannot add up is not a ledger.
    Rupees exist only at the API boundary (see to_paise / to_rupees).

2.  **The journal is append-only.** Nothing ever UPDATEs or DELETEs a posting.
    A mistake is corrected by posting a new, reversing entry that points back
    at the original via journal_entries.reverses.

3.  **Every entry balances.** post_entry() rejects any entry where
    SUM(DR) != SUM(CR). This is the invariant that makes it structurally
    impossible for the system to create or destroy money.

4.  **Balances are derived, never stored as truth.** ledger_balance() sums the
    postings. A cached figure may exist for speed, but assert_books_balance()
    recomputes from the journal and is run at startup.

5.  **Sign convention.** A customer deposit is a LIABILITY of the bank: the
    bank owes the customer. So for liability accounts balance = CR - DR, and
    for asset accounts (the bank's own cash at RBI) balance = DR - CR.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

# ── Account taxonomy ─────────────────────────────────────────────────────────

CUSTOMER = "CUSTOMER"    # retail payer account        — liability
MERCHANT = "MERCHANT"    # merchant account            — liability
POOL     = "POOL"        # the bank's UPI pool account — liability
SUSPENSE = "SUSPENSE"    # unapplied / under investigation — liability
NOSTRO   = "NOSTRO"      # the bank's current a/c with RBI — asset
OPENING  = "OPENING"     # opening-balance contra account  — asset
INCOME   = "INCOME"      # interchange, fees           — income
PENALTY  = "PENALTY"     # TAT compensation paid out   — expense

LIABILITY_TYPES = {CUSTOMER, MERCHANT, POOL, SUSPENSE, INCOME}
ASSET_TYPES     = {NOSTRO, OPENING, PENALTY}

# ── Money ────────────────────────────────────────────────────────────────────

def to_paise(rupees) -> int:
    """Rupees (float/str/Decimal) -> integer paise, half-up at the paisa."""
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_rupees(paise: int) -> float:
    """Integer paise -> rupees, for the API boundary only."""
    return round(paise / 100.0, 2)


# ── Schema ───────────────────────────────────────────────────────────────────

def init_ledger(conn: sqlite3.Connection) -> None:
    """Create the ledger tables. Safe to call on every boot."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ledger_accounts (
            account_id   TEXT PRIMARY KEY,
            bank_code    TEXT NOT NULL,
            account_type TEXT NOT NULL,
            vpa          TEXT UNIQUE,
            holder_name  TEXT NOT NULL,
            currency     TEXT NOT NULL DEFAULT 'INR',
            status       TEXT NOT NULL DEFAULT 'ACTIVE',
            opened_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS journal_entries (
            entry_id   TEXT PRIMARY KEY,
            txn_ref    TEXT NOT NULL,
            rrn        TEXT,
            event_type TEXT NOT NULL,
            reverses   TEXT REFERENCES journal_entries(entry_id),
            narration  TEXT NOT NULL,
            posted_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_je_txn_ref ON journal_entries(txn_ref);
        CREATE INDEX IF NOT EXISTS idx_je_rrn     ON journal_entries(rrn);

        CREATE TABLE IF NOT EXISTS postings (
            posting_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id     TEXT NOT NULL REFERENCES journal_entries(entry_id),
            account_id   TEXT NOT NULL REFERENCES ledger_accounts(account_id),
            direction    TEXT NOT NULL CHECK (direction IN ('DR','CR')),
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            currency     TEXT NOT NULL DEFAULT 'INR'
        );
        CREATE INDEX IF NOT EXISTS idx_postings_account ON postings(account_id);
        CREATE INDEX IF NOT EXISTS idx_postings_entry   ON postings(entry_id);

        CREATE TABLE IF NOT EXISTS holds (
            hold_id      TEXT PRIMARY KEY,
            account_id   TEXT NOT NULL REFERENCES ledger_accounts(account_id),
            txn_ref      TEXT NOT NULL,
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            status       TEXT NOT NULL CHECK (status IN ('ACTIVE','CAPTURED','RELEASED','EXPIRED')),
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_holds_account ON holds(account_id, status);
    """)


def ensure_account(conn, account_id, bank_code, account_type, holder_name, vpa=None):
    """Idempotently open an account in the chart of accounts."""
    conn.execute(
        """INSERT OR IGNORE INTO ledger_accounts
           (account_id, bank_code, account_type, vpa, holder_name, opened_at)
           VALUES (?,?,?,?,?,?)""",
        (account_id, bank_code, account_type, vpa, holder_name, datetime.utcnow().isoformat()),
    )
    return account_id


def system_accounts(conn, bank_code):
    """Open the bank's own internal accounts. Every bank needs these."""
    ensure_account(conn, f"{bank_code}:POOL",     bank_code, POOL,     f"{bank_code} UPI Pool Account")
    ensure_account(conn, f"{bank_code}:SUSPENSE", bank_code, SUSPENSE, f"{bank_code} Suspense Account")
    ensure_account(conn, f"{bank_code}:NOSTRO",   bank_code, NOSTRO,   f"{bank_code} Current A/c with RBI")
    ensure_account(conn, f"{bank_code}:OPENING",  bank_code, OPENING,  f"{bank_code} Opening Balance Contra")
    ensure_account(conn, f"{bank_code}:PENALTY",  bank_code, PENALTY,  f"{bank_code} TAT Compensation Expense")
    ensure_account(conn, f"{bank_code}:INCOME",   bank_code, INCOME,   f"{bank_code} Fee & Interchange Income")


# ── Posting ──────────────────────────────────────────────────────────────────

class UnbalancedEntry(ValueError):
    """Raised when an entry's debits do not equal its credits."""


def post_entry(conn, *, txn_ref, event_type, legs, narration,
               rrn=None, reverses=None, entry_id=None) -> str:
    """Write one balanced journal entry.

    `legs` is a list of (account_id, 'DR'|'CR', amount_paise).

    Raises UnbalancedEntry unless SUM(DR) == SUM(CR). This is the single
    guarantee that the system can neither create nor destroy money, and it is
    checked on every write rather than by a nightly job.

    Must be called inside an open write transaction.
    """
    if not legs:
        raise UnbalancedEntry("an entry needs at least two legs")

    debits  = sum(a for _, d, a in legs if d == "DR")
    credits = sum(a for _, d, a in legs if d == "CR")
    if debits != credits:
        raise UnbalancedEntry(
            f"entry does not balance: DR {debits} paise != CR {credits} paise "
            f"(txn_ref={txn_ref}, event={event_type})"
        )
    if any(a <= 0 for _, _, a in legs):
        raise UnbalancedEntry("every leg must have a positive amount")

    entry_id = entry_id or f"JE-{uuid.uuid4().hex[:16].upper()}"
    conn.execute(
        """INSERT INTO journal_entries
           (entry_id, txn_ref, rrn, event_type, reverses, narration, posted_at)
           VALUES (?,?,?,?,?,?,?)""",
        (entry_id, txn_ref, rrn, event_type, reverses, narration,
         datetime.utcnow().isoformat()),
    )
    conn.executemany(
        "INSERT INTO postings (entry_id, account_id, direction, amount_paise) VALUES (?,?,?,?)",
        [(entry_id, acc, d, amt) for acc, d, amt in legs],
    )
    return entry_id


# ── Balances ─────────────────────────────────────────────────────────────────

def ledger_balance(conn, account_id) -> int:
    """Balance in paise, computed from the journal. Never read from a column."""
    row = conn.execute(
        "SELECT account_type FROM ledger_accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    if not row:
        raise KeyError(f"no such ledger account: {account_id}")

    sums = conn.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN direction='DR' THEN amount_paise END),0) AS dr,
             COALESCE(SUM(CASE WHEN direction='CR' THEN amount_paise END),0) AS cr
           FROM postings WHERE account_id=?""",
        (account_id,),
    ).fetchone()

    acct_type = row["account_type"] if isinstance(row, sqlite3.Row) else row[0]
    dr, cr = sums["dr"], sums["cr"]
    return (cr - dr) if acct_type in LIABILITY_TYPES else (dr - cr)


def active_holds(conn, account_id) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_paise),0) AS h FROM holds WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone()
    return row["h"]


def available_balance(conn, account_id) -> int:
    """What the customer can actually spend: ledger balance minus earmarks."""
    return ledger_balance(conn, account_id) - active_holds(conn, account_id)


# ── Holds / earmarks ─────────────────────────────────────────────────────────

def place_hold(conn, account_id, txn_ref, amount_paise, ttl_seconds=300) -> str:
    hold_id = f"HLD-{uuid.uuid4().hex[:12].upper()}"
    now = datetime.utcnow()
    conn.execute(
        """INSERT INTO holds (hold_id, account_id, txn_ref, amount_paise, status, created_at, expires_at)
           VALUES (?,?,?,?,'ACTIVE',?,?)""",
        (hold_id, account_id, txn_ref, amount_paise, now.isoformat(),
         (now + timedelta(seconds=ttl_seconds)).isoformat()),
    )
    return hold_id


def settle_hold(conn, hold_id, status):
    """Close a hold: CAPTURED once the debit posts, RELEASED if it is abandoned."""
    assert status in ("CAPTURED", "RELEASED", "EXPIRED")
    conn.execute("UPDATE holds SET status=? WHERE hold_id=? AND status='ACTIVE'", (status, hold_id))


def expire_stale_holds(conn) -> int:
    cur = conn.execute(
        "UPDATE holds SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at < ?",
        (datetime.utcnow().isoformat(),),
    )
    return cur.rowcount


# ── Integrity ────────────────────────────────────────────────────────────────

def assert_books_balance(conn) -> dict:
    """The books must balance globally: total debits == total credits.

    Run at startup. If this fails, something wrote to the journal outside
    post_entry() and the ledger can no longer be trusted.
    """
    row = conn.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN direction='DR' THEN amount_paise END),0) AS dr,
             COALESCE(SUM(CASE WHEN direction='CR' THEN amount_paise END),0) AS cr
           FROM postings"""
    ).fetchone()
    dr, cr = row["dr"], row["cr"]
    if dr != cr:
        raise AssertionError(
            f"LEDGER CORRUPT: total DR {dr} paise != total CR {cr} paise (diff {dr - cr})"
        )

    unbalanced = conn.execute(
        """SELECT entry_id,
                  SUM(CASE WHEN direction='DR' THEN amount_paise ELSE -amount_paise END) AS delta
           FROM postings GROUP BY entry_id HAVING delta != 0"""
    ).fetchall()
    if unbalanced:
        raise AssertionError(
            f"LEDGER CORRUPT: {len(unbalanced)} unbalanced entries, "
            f"first={unbalanced[0]['entry_id']}"
        )

    return {"total_dr_paise": dr, "total_cr_paise": cr, "balanced": True}


def trial_balance(conn) -> list[dict]:
    """Per-account balances, computed from the journal. The recon starting point."""
    out = []
    for r in conn.execute(
        "SELECT account_id, account_type, vpa, holder_name FROM ledger_accounts ORDER BY account_type, account_id"
    ).fetchall():
        bal = ledger_balance(conn, r["account_id"])
        out.append({
            "account_id": r["account_id"],
            "account_type": r["account_type"],
            "vpa": r["vpa"],
            "holder_name": r["holder_name"],
            "balance_paise": bal,
            "balance": to_rupees(bal),
        })
    return out
