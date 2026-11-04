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

import json
import os
import secrets
import time
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Form, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import ledger as L  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# The banks this console can see. In the real world an ops console reads a
# warehouse replica; here it reads each bank's own file, read-only. When the
# file is NOT beside us (in its own pod), the console falls back to each bank's
# read-only /ledger/ops/* HTTP mirror over the cluster network instead — same
# SQL results, different transport.
BANKS = {
    "MOCKBANK": {
        "label": "Remitter Bank (payer side)",
        "path": os.path.join(HERE, "..", "mock_payer_bank", "payer_ledger.db"),
        "port": 5003,
        "url": os.getenv("PAYER_BANK_URL", "http://upi-payer-bank.upi.svc:5003"),
    },
    "MOCKBANK2": {
        "label": "Beneficiary Bank (payee side)",
        "path": os.path.join(HERE, "..", "mock_payee_bank", "payee_ledger.db"),
        "port": 5004,
        "url": os.getenv("PAYEE_BANK_URL", "http://upi-payee-bank.upi.svc:5004"),
    },
}

# The PSP owns the transaction store. The ledger only records payments that
# completed at least one leg, so a failed attempt never appears in the journal
# at all — the PSP is the only place to ask "did anything fail?".
PSP_URL = os.getenv("PSP_URL", "http://upi-psp.upi.svc:5001")

app = FastAPI(title="Bank Operations Console", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Routes live on a router so the whole console can be mounted twice: at "/"
# (port-forward, kind) and at "/ops" (behind an ingress on a shared hostname).
# The ingress rule is a plain Prefix match with NO rewrite, so those paths are
# what the browser actually asks for.
router = APIRouter()

# The console shows every balance and can trigger the ops agent, so it gates
# itself instead of trusting the ingress. Basic auth works on any ingress
# controller (some ignore the nginx auth annotations). Browsers cache the
# credentials for the origin after the first prompt, so the page's own fetch()
# calls keep working.
OPS_BASIC_USER = os.getenv("OPS_BASIC_USER", "admin")
OPS_BASIC_PASS = os.getenv("OPS_BASIC_PASS", "admin")

# Signed-cookie session. A cookie is friendlier on a bare-IP deployment than
# native Basic auth: browsers don't enter credential-retry loops for it.
SESSION_COOKIE = "sable_ops_session"
SESSION_TTL_S = 12 * 60 * 60

def _session_token() -> str:
    issued = int(time.time())
    signature = secrets.token_hex(32)
    return f"{issued}.{signature}"


def _valid_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        issued = int(value.split(".", 1)[0])
    except ValueError:
        return False
    return time.time() - issued <= SESSION_TTL_S


def _console_prefix(path: str) -> str:
    return "/ops" if path == "/ops" or path.startswith("/ops/") else ""


def _login_page(prefix: str) -> HTMLResponse:
    return HTMLResponse(f"""
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ops Console sign in</title>
<style>
body{{font:16px system-ui;background:#0a0a0b;color:#f2efe9;display:grid;place-items:center;min-height:100vh;margin:0}}
form{{width:min(92vw,360px);background:#131315;border:1px solid rgba(255,255,255,.12);border-radius:14px;padding:24px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#9b978e;margin:0 0 20px}}
label{{display:block;font-size:13px;margin:12px 0 5px}}input{{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333;background:#1a1a1d;color:#fff}}
button{{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;background:#e84743;color:#fff;font-weight:700}}a{{color:#7aa7ff}}
</style></head><body><form method="post" action="{prefix}/login">
<h1>Bank Operations Console</h1><p>Operator sign in</p>
<label>Username</label><input name="username" autocomplete="username" required autofocus>
<label>Password</label><input name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>
""")


@app.middleware("http")
async def _session_auth(request: Request, call_next):
    # k8s probes and the login page itself must stay unauthenticated.
    if request.url.path in ("/health", "/ops/health", "/login", "/ops/login"):
        return await call_next(request)

    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)

    prefix = _console_prefix(request.url.path)
    if request.url.path.startswith(f"{prefix}/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return _login_page(prefix)


@router.post("/login", include_in_schema=False)
async def login(username: str = Form(default=""), password: str = Form(default="")):
    if not (secrets.compare_digest(username, OPS_BASIC_USER)
            and secrets.compare_digest(password, OPS_BASIC_PASS)):
        return HTMLResponse("<h1>Invalid credentials</h1><p><a href='./login'>Try again</a></p>", status_code=401)
    # This router is included twice; redirect relative to the login request.
    destination = "/"
    response = RedirectResponse(destination)
    response.set_cookie(SESSION_COOKIE, _session_token(), httponly=True, samesite="lax", max_age=SESSION_TTL_S)
    return response


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


def _bank_read(code: str, route: str, query: dict | None = None):
    """One per-bank read, two transports.

    • Repo checkout: the ledger SQLite file sits beside this module — open it
      read-only (unchanged behaviour, the queries still run locally).
    • In-cluster: the console is its own pod; the file is unreachable. Each
      bank then serves the identical read-only SQL over /ledger/ops/* (see
      the ops-console mirror section of the bank services) and this returns
      ('http', that payload).

    Caller pattern:
        mode, data = _bank_read("MOCKBANK", "/ledger/ops/summary")
    data is an open sqlite connection for 'conn', a parsed dict for 'http'.
    """
    cfg = BANKS.get(code)
    if not cfg:
        raise HTTPException(404, f"Unknown bank: {code}")
    if os.path.exists(os.path.abspath(cfg["path"])):
        return "conn", open_ro(code)
    base = cfg["url"].rstrip("/")
    qs = ""
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            qs = "?" + urllib.parse.urlencode(clean)
    try:
        with urllib.request.urlopen(base + route + qs, timeout=10) as resp:
            return "http", json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(503, f"{code} not reachable via {base}{route}: {exc}") from exc


# ── API ──────────────────────────────────────────────────────────────────────

@router.get("/health")
def health():
    return {"status": "ok", "service": "Ops Console", "port": 5010}


@router.get("/api/overview")
def overview():
    """Per-bank trial balance plus the cross-bank conservation check."""
    out = {"banks": [], "system": {}}
    pool_total_paise = 0
    reachable = True

    for code, cfg in BANKS.items():
        try:
            mode, opened = _bank_read(code, "/ledger/ops/summary")
        except (HTTPException, OSError):
            reachable = False
            out["banks"].append({"bank_code": code, "label": cfg["label"], "online": False})
            continue
        try:
            if mode == "http":
                # The bank's mirror already produced this exact shape — just
                # attach the display label and it is the same row the local
                # queries built.
                opened |= {"label": cfg["label"]}
                out["banks"].append(opened)
                pool_total_paise += round(opened["pool_balance"] * 100)
                continue
            conn = opened
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
            conn.close()
        except Exception:
            if mode != "http":
                conn.close()
            raise

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


@router.get("/api/journal")
def journal(limit: int = 40):
    """Recent journal entries from both banks, each with its DR/CR legs.

    The legs are returned together so the page can show, side by side, that
    every entry balances.
    """
    entries = []
    for code in BANKS:
        try:
            mode, opened = _bank_read(code, "/ledger/ops/journal", {"limit": limit})
        except HTTPException:
            continue
        if mode == "http":
            # Mirror entries carry bank_code and all computed legs already.
            entries.extend(opened.get("entries", []))
            continue
        conn = opened
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


@router.get("/api/trace/{txn_ref}")
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
            mode, opened = _bank_read(code, f"/ledger/ops/trace/{urllib.parse.quote(txn_ref)}")
        except HTTPException:
            continue
        if mode == "http":
            legs_by_bank[code] = {
                "label": BANKS[code]["label"],
                "entries": opened.get("entries", []),
                "transaction_row": opened.get("transaction_row"),
            }
            found = found or bool(opened.get("entries"))
            continue
        conn = opened
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


@router.get("/api/statement/{bank_code}/{vpa}")
def statement(bank_code: str, vpa: str):
    """One account's full statement, straight from the postings table."""
    account_id = f"{bank_code}:{vpa}"
    mode, opened = _bank_read(
        bank_code, f"/ledger/ops/statement/{urllib.parse.quote(account_id, safe='')}"
    )
    if mode == "http":
        return opened
    conn = opened
    try:
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


@router.get("/api/holds")
def holds():
    """Active earmarks — the gap between ledger and available balance."""
    out = []
    for code in BANKS:
        try:
            mode, opened = _bank_read(code, "/ledger/ops/holds")
        except HTTPException:
            continue
        if mode == "http":
            out.extend(opened.get("holds", []))
            continue
        conn = opened
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


@router.get("/api/transactions")
def transactions(limit: int = 25):
    """Proxy PSP's transaction store (read-only).

    This is the ground truth for transaction outcomes: status is one of
    initiated/routing/debiting/crediting/success/failed/reversed/
    partial_failure/settled. The Diagnose box forwards these facts to the
    agent so it can answer "did any transaction fail?" without guessing from
    logs."""
    url = f"{PSP_URL.rstrip('/')}/transactions"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            txns = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(503, f"PSP not reachable via {url}: {exc}") from exc
    if isinstance(txns, dict):
        txns = txns.get("transactions", [])
    # Newest first, always — the store's own order is not a contract.
    txns = sorted(txns, key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"transactions": list(txns)[: max(1, min(limit, 100))]}


# ── Static page ──────────────────────────────────────────────────────────────
# Mounted twice for the same reason as the API: "/" for a port-forward and
# "/ops/..." when the console is published under a subpath. The page derives
# that prefix from the address bar, so its fetch() calls follow automatically.

@router.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.include_router(router)                     # /            and /api/...
app.include_router(router, prefix="/ops")      # /ops         and /ops/api/...


@app.get("/ops/", include_in_schema=False)
def index_trailing_slash():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/ops/static", StaticFiles(directory=STATIC), name="static-ops")
