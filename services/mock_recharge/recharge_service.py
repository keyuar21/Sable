"""
Recharge / biller service.

Two jobs:

  1. Work out which operator a mobile number belongs to, via Veriphone.
  2. Serve that operator's prepaid plans and track when the active one expires.

The Veriphone key lives in the environment and is used ONLY here, server side.
It is never returned to the browser and never written into a response: a key
shipped to the client is a key anyone can read and spend.

Lookups are cached in SQLite because Veriphone charges a credit per call and
a mobile number's operator does not change minute to minute.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

app = FastAPI(title="Mock Recharge Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recharge.db")
VERIPHONE_URL = "https://api.veriphone.io/v2/verify"
VERIPHONE_KEY = os.environ.get("VERIPHONE_API_KEY", "").strip()
CACHE_DAYS = 30


# ── Operators ────────────────────────────────────────────────────────────────
# Veriphone returns the operator's trading name, which varies ("Reliance Jio",
# "Bharti Airtel", "Vi"). Normalise to one key so plans resolve either way.

OPERATORS = {
    "jio":    {"name": "Jio",    "colour": "#0a2885", "aliases": ["jio", "reliance"]},
    "airtel": {"name": "Airtel", "colour": "#e40000", "aliases": ["airtel", "bharti"]},
    "vi":     {"name": "Vi",     "colour": "#e60000", "aliases": ["vi", "vodafone", "idea"]},
    "bsnl":   {"name": "BSNL",   "colour": "#f47216", "aliases": ["bsnl", "sanchar"]},
}


def normalise_operator(carrier: str) -> str | None:
    c = (carrier or "").strip().lower()
    if not c:
        return None
    for key, meta in OPERATORS.items():
        if any(a in c for a in meta["aliases"]):
            return key
    return None


# ── Plan catalogue ───────────────────────────────────────────────────────────
# SAMPLE DATA. Indian prepaid tariffs change often and there is no single
# open feed for them, so these are representative rather than authoritative.
# Swap this dict for a real feed when one is available; nothing else changes.

PLANS: dict[str, list[dict]] = {
    "jio": [
        {"id": "jio-199",  "price": 199,  "validity_days": 28,  "data": "1.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["JioTV", "JioCinema"], "popular": False},
        {"id": "jio-299",  "price": 299,  "validity_days": 28,  "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["JioTV", "JioCinema"], "popular": True},
        {"id": "jio-399",  "price": 399,  "validity_days": 56,  "data": "1.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["JioTV"], "popular": False},
        {"id": "jio-899",  "price": 899,  "validity_days": 90,  "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["JioTV", "JioCinema"], "popular": False},
        {"id": "jio-3599", "price": 3599, "validity_days": 365, "data": "2.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["JioTV", "JioCinema"], "popular": False},
    ],
    "airtel": [
        {"id": "air-209",  "price": 209,  "validity_days": 28,  "data": "1 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Airtel Xstream"], "popular": False},
        {"id": "air-299",  "price": 299,  "validity_days": 28,  "data": "1.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Airtel Xstream", "Wynk"], "popular": True},
        {"id": "air-479",  "price": 479,  "validity_days": 56,  "data": "1.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Wynk Music"], "popular": False},
        {"id": "air-979",  "price": 979,  "validity_days": 84,  "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Airtel Xstream"], "popular": False},
        {"id": "air-3599", "price": 3599, "validity_days": 365, "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Airtel Xstream"], "popular": False},
    ],
    "vi": [
        {"id": "vi-199",  "price": 199,  "validity_days": 28,  "data": "1 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Vi Movies & TV"], "popular": False},
        {"id": "vi-299",  "price": 299,  "validity_days": 28,  "data": "1.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Weekend Data Rollover"], "popular": True},
        {"id": "vi-479",  "price": 479,  "validity_days": 56,  "data": "1.5 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Binge All Night"], "popular": False},
        {"id": "vi-901",  "price": 901,  "validity_days": 90,  "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Vi Movies & TV"], "popular": False},
        {"id": "vi-3799", "price": 3799, "validity_days": 365, "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": ["Vi Movies & TV"], "popular": False},
    ],
    "bsnl": [
        {"id": "bsnl-187", "price": 187, "validity_days": 28, "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": [], "popular": True},
        {"id": "bsnl-397", "price": 397, "validity_days": 70, "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": [], "popular": False},
        {"id": "bsnl-797", "price": 797, "validity_days": 180, "data": "2 GB/day",
         "calls": "Unlimited", "sms": "100/day", "extras": [], "popular": False},
    ],
}


# ── Storage ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


@contextmanager
def db_txn():
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
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS carrier_cache (
            phone        TEXT PRIMARY KEY,
            operator     TEXT,
            carrier_raw  TEXT,
            phone_valid  INTEGER NOT NULL DEFAULT 0,
            phone_type   TEXT,
            region       TEXT,
            country_code TEXT,
            looked_up_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            phone        TEXT PRIMARY KEY,
            operator     TEXT NOT NULL,
            plan_id      TEXT NOT NULL,
            price        REAL NOT NULL,
            activated_at TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            txn_id       TEXT
        );
    """)
    conn.close()


init_db()


# ── Helpers ──────────────────────────────────────────────────────────────────

def to_e164(phone: str) -> str:
    """Accept what people actually type and return +91XXXXXXXXXX."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise HTTPException(400, "Enter a mobile number")
    if digits.startswith("91") and len(digits) == 12:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+91{digits}"
    if digits.startswith("0") and len(digits) == 11:
        return f"+91{digits[1:]}"
    return f"+{digits}"


def plan_by_id(operator: str, plan_id: str) -> dict | None:
    return next((p for p in PLANS.get(operator, []) if p["id"] == plan_id), None)


# ── Models ───────────────────────────────────────────────────────────────────

class ActivateRequest(BaseModel):
    phone: str
    operator: str
    plan_id: str
    txn_id: str | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Recharge",
        "port": 5007,
        # Report only whether a key is configured — never the key itself.
        "carrier_lookup": "veriphone" if VERIPHONE_KEY else "unconfigured",
    }


@app.get("/carrier")
def carrier(phone: str, refresh: bool = False):
    """Which operator does this number belong to?

    Cached for CACHE_DAYS because Veriphone bills per lookup and an operator
    does not change from one minute to the next. Pass refresh=true to force it.
    """
    e164 = to_e164(phone)

    if not refresh:
        conn = get_db()
        row = conn.execute("SELECT * FROM carrier_cache WHERE phone=?", (e164,)).fetchone()
        conn.close()
        if row:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["looked_up_at"])
            if age < timedelta(days=CACHE_DAYS):
                return {
                    "phone": e164,
                    "valid": bool(row["phone_valid"]),
                    "operator": row["operator"],
                    "operator_name": OPERATORS.get(row["operator"], {}).get("name"),
                    "carrier_raw": row["carrier_raw"],
                    "phone_type": row["phone_type"],
                    "region": row["region"],
                    "source": "cache",
                }

    if not VERIPHONE_KEY:
        raise HTTPException(
            503,
            "Carrier lookup is not configured — set VERIPHONE_API_KEY in the environment.",
        )

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(VERIPHONE_URL, params={"key": VERIPHONE_KEY, "phone": e164})
    except Exception as e:
        raise HTTPException(502, f"Carrier lookup unavailable: {e}")

    if resp.status_code != 200:
        # Deliberately do not echo the upstream body — it can contain the key.
        raise HTTPException(502, f"Carrier lookup failed (HTTP {resp.status_code})")

    data = resp.json()
    if data.get("status") != "success":
        raise HTTPException(502, "Carrier lookup returned an error")

    operator = normalise_operator(data.get("carrier", ""))
    is_mobile = data.get("phone_type") == "mobile"

    with db_txn() as conn:
        conn.execute(
            """INSERT INTO carrier_cache
               (phone, operator, carrier_raw, phone_valid, phone_type, region, country_code, looked_up_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(phone) DO UPDATE SET
                 operator=excluded.operator, carrier_raw=excluded.carrier_raw,
                 phone_valid=excluded.phone_valid, phone_type=excluded.phone_type,
                 region=excluded.region, country_code=excluded.country_code,
                 looked_up_at=excluded.looked_up_at""",
            (e164, operator, data.get("carrier"), int(bool(data.get("phone_valid"))),
             data.get("phone_type"), data.get("phone_region"), data.get("country_code"),
             datetime.now(timezone.utc).isoformat()),
        )

    return {
        "phone": e164,
        "valid": bool(data.get("phone_valid")) and is_mobile,
        "operator": operator,
        "operator_name": OPERATORS.get(operator, {}).get("name"),
        "carrier_raw": data.get("carrier") or None,
        "phone_type": data.get("phone_type"),
        "region": data.get("phone_region"),
        "source": "veriphone",
        # Veriphone's static mode reports the range holder, so a ported number
        # shows its original operator. Say so rather than quietly being wrong.
        "note": "Original operator for this number range; a ported number may differ.",
    }


@app.get("/operators")
def operators():
    return {
        "operators": [
            {"key": k, "name": v["name"], "colour": v["colour"], "plan_count": len(PLANS.get(k, []))}
            for k, v in OPERATORS.items()
        ]
    }


@app.get("/plans/{operator}")
def plans(operator: str):
    op = operator.strip().lower()
    if op not in PLANS:
        raise HTTPException(404, f"No plans for operator '{operator}'")
    return {
        "operator": op,
        "operator_name": OPERATORS[op]["name"],
        "colour": OPERATORS[op]["colour"],
        "plans": sorted(PLANS[op], key=lambda p: p["price"]),
        "disclaimer": "Sample tariffs for the simulator — not live operator pricing.",
    }


@app.get("/subscription/{phone}")
def subscription(phone: str):
    """The active plan and how long is left on it."""
    e164 = to_e164(phone)
    conn = get_db()
    row = conn.execute("SELECT * FROM subscriptions WHERE phone=?", (e164,)).fetchone()
    conn.close()
    if not row:
        return {"phone": e164, "active": False}

    expires = datetime.fromisoformat(row["expires_at"])
    days_left = (expires - datetime.now(timezone.utc)).days
    plan = plan_by_id(row["operator"], row["plan_id"]) or {}
    return {
        "phone": e164,
        "active": True,
        "operator": row["operator"],
        "operator_name": OPERATORS.get(row["operator"], {}).get("name"),
        "plan_id": row["plan_id"],
        "plan": plan,
        "price": row["price"],
        "activated_at": row["activated_at"],
        "expires_at": row["expires_at"],
        "days_left": days_left,
        "expiring_soon": days_left <= 3,
        "expired": days_left < 0,
    }


@app.post("/subscription/activate")
def activate(req: ActivateRequest):
    """Start a plan. Called after the recharge payment has succeeded."""
    e164 = to_e164(req.phone)
    op = req.operator.strip().lower()
    plan = plan_by_id(op, req.plan_id)
    if not plan:
        raise HTTPException(404, f"Unknown plan '{req.plan_id}' for operator '{op}'")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=plan["validity_days"])
    with db_txn() as conn:
        conn.execute(
            """INSERT INTO subscriptions
               (phone, operator, plan_id, price, activated_at, expires_at, txn_id)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(phone) DO UPDATE SET
                 operator=excluded.operator, plan_id=excluded.plan_id, price=excluded.price,
                 activated_at=excluded.activated_at, expires_at=excluded.expires_at,
                 txn_id=excluded.txn_id""",
            (e164, op, req.plan_id, float(plan["price"]), now.isoformat(),
             expires.isoformat(), req.txn_id),
        )
    return {
        "success": True,
        "phone": e164,
        "operator": op,
        "plan_id": req.plan_id,
        "expires_at": expires.isoformat(),
        "days_left": plan["validity_days"],
        "message": f"{OPERATORS[op]['name']} plan active for {plan['validity_days']} days",
    }
