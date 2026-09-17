"""
Supabase Database Adapter for UPI Flow Simulator
Handles all CRUD operations for Payer Bank, Payee Bank, NPCI Switch, and PSP.
Uses httpx to interact directly with Supabase REST API via SUPABASE_SECRET_KEY.
"""

import os
import httpx
from datetime import datetime
from typing import Optional, Any

# Supabase credentials — set via environment / .env (never hardcode secrets)
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY") or ""
REST_URL = f"{SUPABASE_URL}/rest/v1" if SUPABASE_URL else ""

HEADERS = {
    "apikey": SUPABASE_SECRET_KEY,
    "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _log(msg: str) -> None:
    print(f"[supabase] {msg}", flush=True)


def is_supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SECRET_KEY)

_health_cache: dict[str, Any] = {}
_health_cache_ts: float = 0.0

def check_supabase_health() -> dict[str, Any]:
    """Check if Supabase REST API is reachable and tables are accessible with 10s cache."""
    global _health_cache, _health_cache_ts
    import time
    now = time.time()
    if _health_cache and (now - _health_cache_ts < 10.0):
        return _health_cache

    if not is_supabase_configured():
        _health_cache = {"status": "unconfigured", "reachable": False, "tables_ready": False}
        _health_cache_ts = now
        return _health_cache
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(f"{REST_URL}/payer_accounts?select=vpa&limit=1", headers=HEADERS)
            if resp.status_code == 200:
                _health_cache = {"status": "ok", "reachable": True, "tables_ready": True}
            elif resp.status_code in (404, 400):
                _health_cache = {"status": "schema_pending", "reachable": True, "tables_ready": False, "detail": resp.json()}
            else:
                _health_cache = {"status": "error", "reachable": True, "tables_ready": False, "code": resp.status_code}
    except Exception as e:
        _health_cache = {"status": "unreachable", "reachable": False, "tables_ready": False, "error": str(e)}

    _health_cache_ts = now
    return _health_cache


# ─────────────────────────────────────────────────────────────────────────────
# Payer Bank Operations
# ─────────────────────────────────────────────────────────────────────────────

def get_payer_account(vpa: str) -> Optional[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/payer_accounts?vpa=eq.{vpa}&select=*", headers=HEADERS)
            if r.status_code == 200 and r.json():
                return r.json()[0]
    except Exception as e:
        _log(f"Error fetching payer account: {e}")
    return None

def list_payer_accounts() -> list[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/payer_accounts?select=*&order=vpa.asc", headers=HEADERS)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        _log(f"Error listing payer accounts: {e}")
    return []

def set_payer_balance(vpa: str, balance: float) -> Optional[dict]:
    """Write the authoritative balance (from SQLite) into Supabase."""
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.patch(
                f"{REST_URL}/payer_accounts?vpa=eq.{vpa}",
                headers=HEADERS,
                json={"balance": round(float(balance), 2)},
            )
            if r.status_code in (200, 204):
                body = r.json() if r.content else []
                if body:
                    return body[0]
                # PATCH ok but empty representation — re-fetch
                return get_payer_account(vpa)
            _log(f"set_payer_balance failed {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error setting payer balance in Supabase: {e}")
    return None

def debit_payer_account(vpa: str, amount: float, new_balance: Optional[float] = None) -> Optional[dict]:
    """Sync payer debit to Supabase. Prefer authoritative new_balance from SQLite."""
    if new_balance is not None:
        return set_payer_balance(vpa, new_balance)
    acc = get_payer_account(vpa)
    if not acc:
        _log(f"debit_payer_account: account not found for {vpa}")
        return None
    return set_payer_balance(vpa, round(float(acc["balance"]) - amount, 2))

def reset_payer_accounts():
    seeds = [
        {"vpa": "you@mockbank",   "holder_name": "Keyur Arbhelonde", "balance": 50000.00, "pin": "1234"},
        {"vpa": "priya@mockbank", "holder_name": "Priya Sharma",      "balance": 35000.00, "pin": "1234"},
        {"vpa": "rahul@mockbank", "holder_name": "Rahul Mehta",        "balance": 22000.00, "pin": "1234"},
    ]
    try:
        with httpx.Client(timeout=4.0) as client:
            headers_upsert = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
            r = client.post(
                f"{REST_URL}/payer_accounts?on_conflict=vpa",
                headers=headers_upsert,
                json=seeds,
            )
            if r.status_code not in (200, 201):
                _log(f"reset_payer_accounts failed {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error resetting payer accounts: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Payee Bank Operations
# ─────────────────────────────────────────────────────────────────────────────

def get_payee_account(vpa: str) -> Optional[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/payee_accounts?vpa=eq.{vpa}&select=*", headers=HEADERS)
            if r.status_code == 200 and r.json():
                return r.json()[0]
    except Exception as e:
        _log(f"Error fetching payee account: {e}")
    return None

def list_payee_accounts() -> list[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/payee_accounts?select=*&order=merchant_name.asc", headers=HEADERS)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        _log(f"Error listing payee accounts: {e}")
    return []

def set_payee_balance(vpa: str, balance: float) -> Optional[dict]:
    """Write the authoritative balance (from SQLite) into Supabase."""
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.patch(
                f"{REST_URL}/payee_accounts?vpa=eq.{vpa}",
                headers=HEADERS,
                json={"balance": round(float(balance), 2)},
            )
            if r.status_code in (200, 204):
                body = r.json() if r.content else []
                if body:
                    return body[0]
                return get_payee_account(vpa)
            _log(f"set_payee_balance failed {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error setting payee balance in Supabase: {e}")
    return None

def credit_payee_account(vpa: str, amount: float, new_balance: Optional[float] = None) -> Optional[dict]:
    """Sync payee credit to Supabase. Prefer authoritative new_balance from SQLite."""
    if new_balance is not None:
        return set_payee_balance(vpa, new_balance)
    acc = get_payee_account(vpa)
    if not acc:
        _log(f"credit_payee_account: account not found for {vpa}")
        return None
    return set_payee_balance(vpa, round(float(acc["balance"]) + amount, 2))

def reset_payee_accounts():
    seeds = [
        {"vpa": "sharmastore@mockbank2", "merchant_name": "Sharma Store",   "balance": 0.00, "category": "grocery"},
        {"vpa": "blinkit@mockbank2",     "merchant_name": "Blinkit",        "balance": 0.00, "category": "delivery"},
        {"vpa": "swiggy@mockbank2",      "merchant_name": "Swiggy",         "balance": 0.00, "category": "food"},
        {"vpa": "zomato@mockbank2",      "merchant_name": "Zomato",         "balance": 0.00, "category": "food"},
        {"vpa": "amazon@mockbank2",      "merchant_name": "Amazon",         "balance": 0.00, "category": "ecommerce"},
        {"vpa": "petrolpump@mockbank2",  "merchant_name": "HP Petrol Pump", "balance": 0.00, "category": "fuel"}
    ]
    try:
        with httpx.Client(timeout=4.0) as client:
            headers_upsert = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
            r = client.post(
                f"{REST_URL}/payee_accounts?on_conflict=vpa",
                headers=headers_upsert,
                json=seeds,
            )
            if r.status_code not in (200, 201):
                _log(f"reset_payee_accounts failed {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error resetting payee accounts: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Transactions (E2E Distributed Ledger)
# ─────────────────────────────────────────────────────────────────────────────

def get_transaction(txn_id: str) -> Optional[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/transactions?txn_id=eq.{txn_id}&select=*", headers=HEADERS)
            if r.status_code == 200 and r.json():
                return r.json()[0]
    except Exception as e:
        _log(f"Error fetching transaction: {e}")
    return None

def _clean_txn_payload(txn_data: dict) -> dict:
    clean_data = {
        "txn_id": txn_data["txn_id"],
        "payer_vpa": txn_data["payer_vpa"],
        "payee_vpa": txn_data["payee_vpa"],
        "amount": txn_data["amount"],
        "currency": txn_data.get("currency", "INR"),
        "status": txn_data["status"],
        "hops": txn_data.get("hops") or {},
        "hop_log": txn_data.get("hop_log") or [],
        "error": txn_data.get("error"),
        "error_stage": txn_data.get("error_stage"),
        "settlement_batch_id": txn_data.get("settlement_batch_id"),
        "payer_balance_after": txn_data.get("new_balance")
            if txn_data.get("new_balance") is not None
            else txn_data.get("payer_balance_after"),
        "created_at": txn_data.get("created_at") or datetime.utcnow().isoformat(),
        "completed_at": txn_data.get("completed_at"),
    }
    # Drop nulls so PostgREST uses column defaults / leaves nullable cols unset
    return {k: v for k, v in clean_data.items() if v is not None}

def upsert_transaction(txn_data: dict) -> Optional[dict]:
    try:
        clean_data = _clean_txn_payload(txn_data)
        with httpx.Client(timeout=4.0) as client:
            headers_upsert = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
            r = client.post(
                f"{REST_URL}/transactions?on_conflict=txn_id",
                headers=headers_upsert,
                json=clean_data,
            )
            if r.status_code in (200, 201):
                body = r.json() if r.content else []
                if body:
                    return body[0]
                return get_transaction(clean_data["txn_id"])
            _log(f"upsert_transaction status {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error upserting transaction: {e}")
    return None

def update_transaction(txn_id: str, updates: dict) -> Optional[dict]:
    """PATCH status fields; if no row exists yet, fall back to a full upsert when possible."""
    try:
        patch_body = {k: v for k, v in updates.items() if v is not None}
        # Map in-memory key onto DB column
        if "new_balance" in patch_body:
            patch_body["payer_balance_after"] = patch_body.pop("new_balance")
        with httpx.Client(timeout=4.0) as client:
            r = client.patch(
                f"{REST_URL}/transactions?txn_id=eq.{txn_id}",
                headers=HEADERS,
                json=patch_body,
            )
            if r.status_code in (200, 204):
                body = r.json() if r.content else []
                if body:
                    return body[0]
            else:
                _log(f"update_transaction status {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error updating transaction: {e}")
    return None

def sync_transaction(txn_data: dict) -> Optional[dict]:
    """Always upsert full txn row so status updates never no-op on a missing insert."""
    return upsert_transaction(txn_data)

def list_transactions(limit: int = 50, vpa: Optional[str] = None) -> list[dict]:
    try:
        query = f"{REST_URL}/transactions?select=*&order=created_at.desc&limit={limit}"
        if vpa:
            query += f"&or=(payer_vpa.eq.{vpa},payee_vpa.eq.{vpa})"
        with httpx.Client(timeout=4.0) as client:
            r = client.get(query, headers=HEADERS)
            if r.status_code == 200:
                return r.json()
            _log(f"list_transactions status {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error listing transactions: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Settlement Batches
# ─────────────────────────────────────────────────────────────────────────────

def insert_settlement_batch(batch_data: dict) -> Optional[dict]:
    try:
        clean_batch = {
            "cycle_id": batch_data["cycle_id"],
            "cycle_number": batch_data["cycle_number"],
            "timestamp": batch_data.get("timestamp") or datetime.utcnow().isoformat(),
            "debit_count": batch_data.get("debit_count", 0),
            "credit_count": batch_data.get("credit_count", 0),
            "total_debit_settled": batch_data.get("total_debit_settled", 0.0),
            "total_credit_settled": batch_data.get("total_credit_settled", 0.0),
            "net_obligation": batch_data.get("net_obligation", 0.0),
            "from_bank": batch_data.get("from_bank", "Mock Payer Bank (@mockbank)"),
            "to_bank": batch_data.get("to_bank", "Mock Payee Bank (@mockbank2)"),
            "status": batch_data.get("status", "SETTLED")
        }
        with httpx.Client(timeout=4.0) as client:
            r = client.post(f"{REST_URL}/settlement_batches", headers=HEADERS, json=clean_batch)
            if r.status_code in (200, 201) and r.json():
                return r.json()[0]
            _log(f"insert_settlement_batch status {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error inserting settlement batch: {e}")
    return None

def list_settlement_batches(limit: int = 20) -> list[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/settlement_batches?select=*&order=timestamp.desc&limit={limit}", headers=HEADERS)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        _log(f"Error listing settlement batches: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Switch Route Logs
# ─────────────────────────────────────────────────────────────────────────────

def insert_route_log(log_data: dict) -> Optional[dict]:
    try:
        clean_log = {
            "txn_id": log_data["txn_id"],
            "payer_vpa": log_data["payer_vpa"],
            "payee_vpa": log_data["payee_vpa"],
            "payer_bank_url": log_data.get("payer_bank_url"),
            "payee_bank_url": log_data.get("payee_bank_url"),
            "status": log_data.get("status", "resolved"),
            "timestamp": log_data.get("timestamp") or datetime.utcnow().isoformat()
        }
        with httpx.Client(timeout=4.0) as client:
            r = client.post(f"{REST_URL}/route_logs", headers=HEADERS, json=clean_log)
            if r.status_code in (200, 201) and r.json():
                return r.json()[0]
            _log(f"insert_route_log status {r.status_code}: {r.text}")
    except Exception as e:
        _log(f"Error inserting route log: {e}")
    return None

def list_route_logs(limit: int = 50) -> list[dict]:
    try:
        with httpx.Client(timeout=4.0) as client:
            r = client.get(f"{REST_URL}/route_logs?select=*&order=timestamp.desc&limit={limit}", headers=HEADERS)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        _log(f"Error listing route logs: {e}")
    return []
