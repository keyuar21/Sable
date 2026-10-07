"""
Bank Operations Console — a read-only window onto the real ledgers.

This is deliberately NOT the customer app. It is the back-office view: the
thing a bank's operations or reconciliation team would actually stare at.

It opens each bank's SQLite file in READ-ONLY mode and queries the journal
directly. Nothing here is mocked or cached — every figure on the page is a
live SUM over the postings table. If the dashboard says an account holds
Rs 46,150 it is because the double-entry journal adds up to that.

Port 5010. (5006 is reserved for the payee PSP in the architecture spec.)
"""

from __future__ import annotations

import os
import sqlite3
import sys

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import ledger as L  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# The banks this console can see. In the real world an ops console reads a
# warehouse replica; here it reads each bank's own file, read-only.
BANKS = {
    "MOCKBANK": {
        "label": "Remitter Bank (payer side)",
        "path": os.path.join(HERE, "..", "mock_payer_bank", "payer_ledger.db"),
        "port": 5003,
    },
    "MOCKBANK2": {
        "label": "Beneficiary Bank (payee side)",
        "path": os.path.join(HERE, "..", "mock_payee_bank", "payee_ledger.db"),
        "port": 5004,
    },
}

app = FastAPI(title="Bank Operations Console", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def open_ro(bank_code: str) -> sqlite3.Connection:
    """Read-only connection. The console must never be able to write."""
    cfg = BANKS.get(bank_code)
    if not cfg:
        raise HTTPException(404, f"Unknown bank: {bank_code}")
    path = os.path.abspath(cfg["path"])
    if not os.path.exists(path):
        raise HTTPException(503, f"{bank_code} ledger not found — has the stack been started?")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "Ops Console", "port": 5010}


@app.get("/api/overview")
def overview():
    """Per-bank trial balance plus the cross-bank conservation check."""
    out = {"banks": [], "system": {}}
    pool_total_paise = 0
    reachable = True

    for code, cfg in BANKS.items():
        try:
            conn = open_ro(code)
        except HTTPException:
            reachable = False
            out["banks"].append({"bank_code": code, "label": cfg["label"], "online": False})
            continue
        try:
            accounts = L.trial_balance(conn)
            try:
                integrity = L.assert_books_balance(conn)
                integrity["error"] = None
            except AssertionError as e:
                integrity = {"balanced": False, "error": str(e)}

            by_type: dict[str, list] = {}
            for a in accounts:
                by_type.setdefault(a["account_type"], []).append(a)

            pool = next((a for a in accounts if a["account_type"] == "POOL"), None)
            if pool:
                pool_total_paise += pool["balance_paise"]

            customer_total = sum(
                a["balance_paise"] for a in accounts
                if a["account_type"] in ("CUSTOMER", "MERCHANT")
            )

            out["banks"].append({
                "bank_code": code,
                "label": cfg["label"],
                "online": True,
                "integrity": integrity,
                "accounts_by_type": by_type,
                "pool_balance": L.to_rupees(pool["balance_paise"]) if pool else 0.0,
                "customer_total": L.to_rupees(customer_total),
                "entry_count": conn.execute("SELECT COUNT(*) c FROM journal_entries").fetchone()["c"],
                "posting_count": conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"],
            })
        finally:
            conn.close()

    # THE systemic invariant. Every rupee that left a payer sits either in the
    # remitter's pool (awaiting settlement) or has been drawn from the
    # beneficiary's pool to fund a credit. Those two pools must cancel.
    # A non-zero figure is money in flight — or a reconciliation break.
    out["system"] = {
        "net_pool_position": L.to_rupees(pool_total_paise),
        "reconciled": pool_total_paise == 0,
        "all_banks_online": reachable,
        "explanation": (
            "Payer POOL + Payee POOL must equal zero. Anything else is value "
            "in flight between the two banks, or an unreconciled break."
        ),
    }
    return out


@app.get("/api/journal")
def journal(limit: int = 40):
    """Recent journal entries from both banks, each with its DR/CR legs.

    The legs are returned together so the page can show, side by side, that
    every entry balances.
    """
    entries = []
    for code in BANKS:
        try:
            conn = open_ro(code)
        except HTTPException:
            continue
        try:
            rows = conn.execute(
                """SELECT entry_id, txn_ref, rrn, event_type, reverses, narration, posted_at
                   FROM journal_entries ORDER BY posted_at DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            for r in rows:
                legs = conn.execute(
                    """SELECT p.account_id, p.direction, p.amount_paise, la.account_type
                       FROM postings p
                       LEFT JOIN ledger_accounts la ON la.account_id = p.account_id
                       WHERE p.entry_id = ? ORDER BY p.direction DESC, p.posting_id""",
                    (r["entry_id"],),
                ).fetchall()
                dr = sum(x["amount_paise"] for x in legs if x["direction"] == "DR")
                cr = sum(x["amount_paise"] for x in legs if x["direction"] == "CR")
                entries.append({
                    "bank_code": code,
                    "entry_id": r["entry_id"],
                    "txn_ref": r["txn_ref"],
                    "rrn": r["rrn"],
                    "event_type": r["event_type"],
                    "reverses": r["reverses"],
                    "narration": r["narration"],
                    "posted_at": r["posted_at"],
                    "balanced": dr == cr,
                    "amount": L.to_rupees(dr),
                    "legs": [
                        {
                            "account_id": x["account_id"],
                            "account_type": x["account_type"],
                            "direction": x["direction"],
                            "amount": L.to_rupees(x["amount_paise"]),
                        }
                        for x in legs
                    ],
                })
        finally:
            conn.close()

    entries.sort(key=lambda e: e["posted_at"], reverse=True)
    return {"entries": entries[:limit]}


@app.get("/api/trace/{txn_ref}")
def trace(txn_ref: str):
    """Follow one transaction across BOTH banks.

    This is the view that shows the architecture: a debit leg on the remitter's
    books and a credit leg on the beneficiary's books, with no entry ever
    spanning the two.
    """
    legs_by_bank = {}
    found = False
    for code in BANKS:
        try:
            conn = open_ro(code)
        except HTTPException:
            continue
        try:
            rows = conn.execute(
                """SELECT je.entry_id, je.event_type, je.reverses, je.narration, je.posted_at,
                          p.account_id, p.direction, p.amount_paise, la.account_type
                   FROM journal_entries je
                   JOIN postings p ON p.entry_id = je.entry_id
                   LEFT JOIN ledger_accounts la ON la.account_id = p.account_id
                   WHERE je.txn_ref = ?
                   ORDER BY je.posted_at, p.posting_id""",
                (txn_ref,),
            ).fetchall()
            if rows:
                found = True
            grouped: dict[str, dict] = {}
            for r in rows:
                e = grouped.setdefault(r["entry_id"], {
                    "entry_id": r["entry_id"], "event_type": r["event_type"],
                    "reverses": r["reverses"], "narration": r["narration"],
                    "posted_at": r["posted_at"], "legs": [],
                })
                e["legs"].append({
                    "account_id": r["account_id"], "account_type": r["account_type"],
                    "direction": r["direction"], "amount": L.to_rupees(r["amount_paise"]),
                })

            txn = conn.execute(
                "SELECT status, amount, timestamp, failure_reason, settlement_batch_id "
                "FROM transactions WHERE txn_id=?", (txn_ref,)
            ).fetchone()

            legs_by_bank[code] = {
                "label": BANKS[code]["label"],
                "entries": list(grouped.values()),
                "transaction_row": dict(txn) if txn else None,
            }
        finally:
            conn.close()

    if not found:
        raise HTTPException(404, f"No journal entries for txn_ref {txn_ref}")
    return {"txn_ref": txn_ref, "banks": legs_by_bank}


@app.get("/api/statement/{bank_code}/{vpa}")
def statement(bank_code: str, vpa: str):
    """One account's full statement, straight from the postings table."""
    conn = open_ro(bank_code)
    try:
        account_id = f"{bank_code}:{vpa}"
        rows = conn.execute(
            """SELECT je.entry_id, je.txn_ref, je.event_type, je.reverses,
                      je.narration, je.posted_at, p.direction, p.amount_paise
               FROM postings p JOIN journal_entries je ON je.entry_id = p.entry_id
               WHERE p.account_id = ? ORDER BY je.posted_at, p.posting_id""",
            (account_id,),
        ).fetchall()
        running = 0
        out = []
        for r in rows:
            running += r["amount_paise"] if r["direction"] == "CR" else -r["amount_paise"]
            out.append({
                "entry_id": r["entry_id"], "txn_ref": r["txn_ref"],
                "event_type": r["event_type"], "reverses": r["reverses"],
                "narration": r["narration"], "posted_at": r["posted_at"],
                "direction": r["direction"], "amount": L.to_rupees(r["amount_paise"]),
                "running_balance": L.to_rupees(running),
            })
        return {"account_id": account_id, "entries": out,
                "closing_balance": L.to_rupees(running)}
    finally:
        conn.close()


@app.get("/api/holds")
def holds():
    """Active earmarks — the gap between ledger and available balance."""
    out = []
    for code in BANKS:
        try:
            conn = open_ro(code)
        except HTTPException:
            continue
        try:
            for r in conn.execute(
                """SELECT hold_id, account_id, txn_ref, amount_paise, status, created_at, expires_at
                   FROM holds ORDER BY created_at DESC LIMIT 25"""
            ).fetchall():
                out.append({
                    "bank_code": code, "hold_id": r["hold_id"], "account_id": r["account_id"],
                    "txn_ref": r["txn_ref"], "amount": L.to_rupees(r["amount_paise"]),
                    "status": r["status"], "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                })
        finally:
            conn.close()
    return {"holds": out}


# ── Static page ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
