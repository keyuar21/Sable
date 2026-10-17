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

app = FastAPI(title="Mock Recharge & Billers Service", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recharge.db")
VERIPHONE_URL = "https://api.veriphone.io/v2/verify"
VERIPHONE_KEY = os.environ.get("VERIPHONE_API_KEY", "").strip()
CACHE_DAYS = 30

# ── Tariff plan source ───────────────────────────────────────────────────────
# "builtin" serves the sample catalogue below. "rapidapi" pulls live plans from
# nixinfo's tariff API, which needs BOTH a RapidAPI key and a nixinfo ctrlkey.
# Without both, the service stays on builtin rather than failing — a missing
# key should not take the recharge screen down.
PLAN_PROVIDER = os.environ.get("PLAN_PROVIDER", "builtin").strip().lower()
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
# The exact host and path come from the RapidAPI playground's "Code snippet"
# tab. RapidAPI's gateway rejects an unknown host with 404 "API doesn't exists"
# before it ever checks the key, so these must match that snippet exactly.
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "mobile-recharge-plans-api-tariff-plans-free.p.rapidapi.com").strip()
RAPIDAPI_PATH = os.environ.get("RAPIDAPI_PATH", "/").strip() or "/"
NIXINFO_CTRLKEY = os.environ.get("NIXINFO_CTRLKEY", "").strip()
PLAN_CACHE_HOURS = 24


def live_plans_configured() -> bool:
    return bool(PLAN_PROVIDER == "rapidapi" and RAPIDAPI_KEY and NIXINFO_CTRLKEY)


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

PLANS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plans")

# Plans live in JSON data files, not in this module. Updating a price is then
# editing a file and restarting — no code change, no redeploy — and the file
# records when it was last checked and where the figures came from, which a
# hardcoded dict never could.
_catalogue: dict[str, dict] = {}
_circles: list[dict] = []
_boards: list[dict] = []


def _load_catalogue() -> None:
    global _catalogue, _circles
    import json as _json
    try:
        with open(os.path.join(PLANS_DIR, "circles.json")) as f:
            _circles = _json.load(f)["circles"]
    except Exception as e:
        print(f"[recharge] could not load circles.json: {e}", flush=True)
        _circles = []

    for key in OPERATORS:
        path = os.path.join(PLANS_DIR, f"{key}.json")
        try:
            with open(path) as f:
                _catalogue[key] = _json.load(f)
        except Exception as e:
            print(f"[recharge] could not load {key}.json: {e}", flush=True)
            _catalogue[key] = {"national": [], "circle_overrides": {}}

    global _boards
    try:
        with open(os.path.join(PLANS_DIR, "electricity_boards.json")) as f:
            _boards = _json.load(f)["boards"]
    except Exception as e:
        print(f"[billers] could not load electricity_boards.json: {e}", flush=True)
        _boards = []

    total = sum(len(v.get("national", [])) for v in _catalogue.values())
    print(f"[recharge] catalogue: {total} plans across {len(_catalogue)} operators, "
          f"{len(_circles)} circles, {len(_boards)} electricity boards", flush=True)


def catalogue_plans(operator: str, circle: str = "") -> tuple[list[dict], dict]:
    """Plans for an operator, with any circle-specific list taking precedence."""
    entry = _catalogue.get(operator) or {}
    overrides = entry.get("circle_overrides") or {}
    rows = overrides.get(circle) if circle and circle in overrides else entry.get("national", [])
    meta = {
        "updated_on": entry.get("updated_on"),
        "source": entry.get("source"),
        "note": entry.get("note"),
        "circle_specific": bool(circle and circle in overrides),
    }
    return sorted(rows, key=lambda p: p["price"]), meta


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
        CREATE TABLE IF NOT EXISTS plan_cache (
            cache_key   TEXT PRIMARY KEY,
            payload     TEXT NOT NULL,
            source      TEXT NOT NULL,
            fetched_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS electricity_bills (
            bill_id      TEXT PRIMARY KEY,
            board        TEXT NOT NULL,
            consumer_no  TEXT NOT NULL,
            consumer_name TEXT NOT NULL,
            units        INTEGER NOT NULL,
            amount       REAL NOT NULL,
            bill_date    TEXT NOT NULL,
            due_date     TEXT NOT NULL,
            period       TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'DUE',
            paid_at      TEXT,
            txn_id       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bills_consumer
            ON electricity_bills(board, consumer_no);
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
_load_catalogue()


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
    rows, _ = plans_for(operator)
    return next((p for p in rows if p["id"] == plan_id), None)


@app.get("/circles")
def circles():
    """The 22 licensed telecom service areas, for the circle picker."""
    return {"circles": _circles}


# ── Live tariff plans ────────────────────────────────────────────────────────

def _parse_validity_days(text: str) -> int:
    """'28 days' / '5 Days' / '1 year' -> an integer number of days."""
    t = (text or "").strip().lower()
    m = re.search(r"(\d+)", t)
    n = int(m.group(1)) if m else 0
    if "year" in t:
        return n * 365
    if "month" in t:
        return n * 30
    return n


def _normalise_live_plan(raw: dict, idx: int, operator: str) -> dict | None:
    """Map the provider's row onto the shape the app already renders.

    Their documented row is {"Detail","Amount","Validity"} with everything
    interesting buried in one free-text Detail string, so data/calls/sms are
    pulled out heuristically and the original text is kept for display.
    """
    try:
        amount = float(re.sub(r"[^\d.]", "", str(raw.get("Amount", "")) or "0"))
    except ValueError:
        return None
    if amount <= 0:
        return None

    detail = str(raw.get("Detail", "")).strip()
    days = _parse_validity_days(str(raw.get("Validity", "")))

    data_m = re.search(r"(\d+(?:\.\d+)?\s*(?:GB|MB)\s*(?:/\s*day|per day)?)", detail, re.I)
    return {
        "id": f"{operator}-live-{idx}-{int(amount)}",
        "price": amount,
        "validity_days": days,
        "data": data_m.group(1) if data_m else "—",
        "calls": "Unlimited" if re.search(r"unlimited", detail, re.I) else "—",
        "sms": re.search(r"(\d+\s*SMS[^.]*)", detail, re.I).group(1) if re.search(r"\d+\s*SMS", detail, re.I) else "—",
        "extras": [],
        "detail": detail,          # the provider's own wording, shown verbatim
        "popular": False,
    }


def fetch_live_plans(operator: str, circle: str = "") -> list[dict] | None:
    """Pull plans from the RapidAPI tariff provider. None if it cannot be used."""
    if not live_plans_configured():
        return None
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"https://{RAPIDAPI_HOST}{RAPIDAPI_PATH}",
                headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST},
                params={
                    "tariff-plan-api": OPERATORS.get(operator, {}).get("name", operator),
                    "ctrlkey": NIXINFO_CTRLKEY,
                    "demo": circle or "",
                },
            )
        if resp.status_code != 200:
            print(f"[recharge] live plans HTTP {resp.status_code} for {operator}", flush=True)
            return None
        body = resp.json()
        rows = body if isinstance(body, list) else (
            body.get("plans") or body.get("data") or body.get("result") or [])
        if not isinstance(rows, list):
            return None
        out = [p for i, r in enumerate(rows) if isinstance(r, dict)
               and (p := _normalise_live_plan(r, i, operator))]
        return sorted(out, key=lambda p: p["price"]) or None
    except Exception as e:
        print(f"[recharge] live plan fetch failed for {operator}: {e}", flush=True)
        return None


def plans_for(operator: str, circle: str = "") -> tuple[list[dict], str]:
    """Plans plus where they came from. Falls back to the builtin catalogue."""
    key = f"{operator}:{circle or '-'}"
    conn = get_db()
    row = conn.execute("SELECT * FROM plan_cache WHERE cache_key=?", (key,)).fetchone()
    conn.close()
    if row:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        if age < timedelta(hours=PLAN_CACHE_HOURS):
            import json as _json
            return _json.loads(row["payload"]), f"{row['source']}-cache"

    live = fetch_live_plans(operator, circle)
    if live:
        import json as _json
        with db_txn() as conn:
            conn.execute(
                """INSERT INTO plan_cache (cache_key, payload, source, fetched_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     payload=excluded.payload, source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                (key, _json.dumps(live), "live", datetime.now(timezone.utc).isoformat()),
            )
        return live, "live"

    rows, _ = catalogue_plans(operator, circle)
    return rows, "catalogue"


# ── Electricity bills ────────────────────────────────────────────────────────
#
# Bills are GENERATED here, not fetched. No board portal is contacted: those are
# session-based consumer websites, not APIs, and real bill fetch runs over BBPS
# which needs a licensed biller agent. Generating locally keeps the simulator
# honest and self-contained.
#
# The figures are derived deterministically from the consumer number, so the
# same number always returns the same bill — a demo where the amount changed on
# every refresh would be obviously fake.

def _board(board_id: str) -> dict | None:
    return next((b for b in _boards if b["id"] == board_id), None)


def _apply_tariff(units: int, tariff: dict) -> dict:
    """Cost the units against the board's own slab tariff.

    Slabs are cumulative, as real Indian domestic billing works: the first 100
    units are charged at the first rate whatever the total, not the whole bill
    at a single rate.
    """
    remaining, prev_cap, energy, breakdown = units, 0, 0.0, []
    for slab in tariff.get("slabs", []):
        cap = slab.get("upto")
        width = (cap - prev_cap) if cap else remaining
        take = min(remaining, width)
        if take <= 0:
            break
        cost = take * slab["rate"]
        energy += cost
        breakdown.append({
            "range": f"{prev_cap + 1}–{cap}" if cap else f"{prev_cap + 1}+",
            "units": take, "rate": slab["rate"], "amount": round(cost, 2),
        })
        remaining -= take
        prev_cap = cap or prev_cap
        if remaining <= 0:
            break

    wheeling = units * float(tariff.get("wheeling_per_unit", 0.0))
    fixed = float(tariff.get("fixed_charge", 0.0))
    subtotal = energy + wheeling + fixed
    duty = subtotal * float(tariff.get("duty_percent", 0.0)) / 100.0

    return {
        "energy_charge": round(energy, 2),
        "wheeling_charge": round(wheeling, 2),
        "fixed_charge": round(fixed, 2),
        "duty": round(duty, 2),
        "total": round(subtotal + duty, 2),
        "slab_breakdown": breakdown,
        "tariff_name": tariff.get("name"),
        "tariff_note": tariff.get("note"),
    }


def _bill_for(board_id: str, consumer_no: str) -> dict:
    """Derive this consumer's current bill. Stable for a given number."""
    import hashlib
    seed = int(hashlib.sha256(f"{board_id}:{consumer_no}".encode()).hexdigest()[:8], 16)

    units = 90 + (seed % 310)                      # 90–400 units
    board = _board(board_id) or {}
    costed = _apply_tariff(units, board.get("tariff") or {})

    today = datetime.now(timezone.utc).date()
    bill_date = today.replace(day=1)
    due = bill_date + timedelta(days=14 + (seed % 7))
    names = ["R. Sharma", "A. Patil", "S. Kulkarni", "M. Desai", "V. Joshi",
             "N. Iyer", "P. Deshmukh", "K. Nair"]

    return {
        "bill_id": f"{board_id.upper()}-{consumer_no}-{bill_date:%Y%m}",
        "board": board_id,
        "consumer_no": consumer_no,
        "consumer_name": names[seed % len(names)],
        "units": units,
        "amount": costed["total"],
        "charges": costed,
        "bill_date": bill_date.isoformat(),
        "due_date": due.isoformat(),
        "period": f"{bill_date:%b %Y}",
    }


@app.get("/electricity/boards")
def electricity_boards(state: str = ""):
    rows = [b for b in _boards if not state or b["state"].lower() == state.lower()]
    return {
        "boards": rows,
        "states": sorted({b["state"] for b in _boards}),
        "note": "Bills are generated locally — no board portal is contacted.",
    }


@app.get("/electricity/bill")
def electricity_bill(board: str, consumer: str):
    """The outstanding bill for a consumer number."""
    b = _board(board)
    if not b:
        raise HTTPException(404, f"Unknown board '{board}'")
    digits = re.sub(r"\D", "", consumer or "")
    if not digits:
        raise HTTPException(400, f"Enter your {b['label']}")
    if len(digits) != b["digits"]:
        raise HTTPException(
            400,
            f"{b['name']} {b['label']}s are {b['digits']} digits — you entered {len(digits)}",
        )

    bill = _bill_for(board, digits)
    conn = get_db()
    row = conn.execute(
        "SELECT status, paid_at, txn_id FROM electricity_bills WHERE bill_id=?",
        (bill["bill_id"],),
    ).fetchone()
    conn.close()

    due = datetime.fromisoformat(bill["due_date"]).date()
    days_to_due = (due - datetime.now(timezone.utc).date()).days
    paid = bool(row and row["status"] == "PAID")

    return {
        **bill,
        "board_name": b["name"],
        "state": b["state"],
        "label": b["label"],
        "portal": b["portal"],
        "status": "PAID" if paid else ("OVERDUE" if days_to_due < 0 else "DUE"),
        "paid_at": row["paid_at"] if row else None,
        "txn_id": row["txn_id"] if row else None,
        "days_to_due": days_to_due,
        "due_soon": (not paid) and 0 <= days_to_due <= 5,
        "overdue": (not paid) and days_to_due < 0,
    }


class PayBillRequest(BaseModel):
    board: str
    consumer_no: str
    txn_id: str | None = None


@app.post("/electricity/pay")
def electricity_pay(req: PayBillRequest):
    """Mark a bill settled. Called once the UPI payment has succeeded."""
    b = _board(req.board)
    if not b:
        raise HTTPException(404, f"Unknown board '{req.board}'")
    digits = re.sub(r"\D", "", req.consumer_no or "")
    bill = _bill_for(req.board, digits)
    now = datetime.now(timezone.utc)

    with db_txn() as conn:
        existing = conn.execute(
            "SELECT status FROM electricity_bills WHERE bill_id=?", (bill["bill_id"],)
        ).fetchone()
        if existing and existing["status"] == "PAID":
            return {"success": True, "duplicate": True,
                    "message": "This bill is already paid"}
        conn.execute(
            """INSERT INTO electricity_bills
               (bill_id, board, consumer_no, consumer_name, units, amount,
                bill_date, due_date, period, status, paid_at, txn_id)
               VALUES (?,?,?,?,?,?,?,?,?,'PAID',?,?)
               ON CONFLICT(bill_id) DO UPDATE SET
                 status='PAID', paid_at=excluded.paid_at, txn_id=excluded.txn_id""",
            (bill["bill_id"], req.board, digits, bill["consumer_name"], bill["units"],
             bill["amount"], bill["bill_date"], bill["due_date"], bill["period"],
             now.isoformat(), req.txn_id),
        )
    return {
        "success": True,
        "bill_id": bill["bill_id"],
        "amount": bill["amount"],
        "message": f"{b['name']} bill for {bill['period']} paid",
    }


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
        "plan_source": "rapidapi" if live_plans_configured() else "catalogue",
        "plan_count": sum(len(v.get("national", [])) for v in _catalogue.values()),
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
def plans(operator: str, circle: str = ""):
    op = operator.strip().lower()
    if op not in OPERATORS:
        raise HTTPException(404, f"No plans for operator '{operator}'")
    rows, source = plans_for(op, circle)
    _, meta = catalogue_plans(op, circle)
    return {
        "operator": op,
        "operator_name": OPERATORS[op]["name"],
        "colour": OPERATORS[op]["colour"],
        "circle": circle or None,
        "plans": rows,
        "categories": sorted({p.get("category", "other") for p in rows}),
        "source": source,
        "updated_on": meta.get("updated_on"),
        "reference": meta.get("source"),
        "circle_specific": meta.get("circle_specific"),
        "disclaimer": (
            "Live tariffs from the provider." if source.startswith("live")
            else f"Prices as of {meta.get('updated_on')} — confirm with your operator."
        ),
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


@app.get("/reminders")
def reminders(phones: str = "", bills: str = "", days: int = 5):
    """Which of these numbers need recharging soon?

    `phones` is a comma-separated list the device already knows about — its own
    number plus any it has recharged. The device asks; the service never has to
    keep a list of who to pester.
    """
    wanted = [p.strip() for p in phones.split(",") if p.strip()]
    # `bills` is "board:consumer_no" pairs the device has looked up before.
    wanted_bills = [b.strip() for b in bills.split(",") if b.strip()]
    if not wanted and not wanted_bills:
        return {"reminders": []}

    out = []
    conn = get_db()
    try:
        for raw in wanted[:20]:
            try:
                e164 = to_e164(raw)
            except HTTPException:
                continue
            row = conn.execute("SELECT * FROM subscriptions WHERE phone=?", (e164,)).fetchone()
            if not row:
                continue
            expires = datetime.fromisoformat(row["expires_at"])
            days_left = (expires - datetime.now(timezone.utc)).days
            if days_left > days:
                continue
            plan = plan_by_id(row["operator"], row["plan_id"]) or {}
            out.append({
                "kind": "recharge",
                "phone": e164,
                "operator": row["operator"],
                "operator_name": OPERATORS.get(row["operator"], {}).get("name"),
                "plan_id": row["plan_id"],
                "price": row["price"],
                "data": plan.get("data"),
                "expires_at": row["expires_at"],
                "days_left": days_left,
                "expired": days_left < 0,
                "urgency": "expired" if days_left < 0 else "today" if days_left == 0 else "soon",
            })
    finally:
        conn.close()

    # Electricity bills, same window and the same shape, so the app can sort
    # recharges and bills together and surface whichever is most pressing.
    conn = get_db()
    try:
        for ref in wanted_bills[:20]:
            board_id, _, consumer = ref.partition(":")
            b = _board(board_id.strip())
            if not b or not consumer.strip():
                continue
            bill = _bill_for(board_id.strip(), re.sub(r"\D", "", consumer))
            row = conn.execute(
                "SELECT status FROM electricity_bills WHERE bill_id=?", (bill["bill_id"],)
            ).fetchone()
            if row and row["status"] == "PAID":
                continue
            due = datetime.fromisoformat(bill["due_date"]).date()
            days_left = (due - datetime.now(timezone.utc).date()).days
            if days_left > days:
                continue
            out.append({
                "kind": "electricity",
                "board": board_id.strip(),
                "board_name": b["name"],
                "consumer_no": bill["consumer_no"],
                "amount": bill["amount"],
                "period": bill["period"],
                "due_date": bill["due_date"],
                "days_left": days_left,
                "expired": days_left < 0,
                "urgency": "overdue" if days_left < 0 else "today" if days_left == 0 else "soon",
            })
    finally:
        conn.close()

    # Most urgent first, so the one banner the app shows is the right one.
    out.sort(key=lambda r: r["days_left"])
    return {"reminders": out, "window_days": days}


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
