# ─────────────────────────────────────────────────────────────────────────────
# PLANE 1 — Customer MCP (Standard Mode)
#
# Session-locked, deterministic tools for the voice banking agent. Every tool
# resolves the account from ctx["vpa"] — the value the agent service pulled
# from the authenticated X-VPA header — never from tool arguments. A prompt
# injection inside the LLM therefore cannot point these tools at another
# user's account: the argument does not exist.
#
# Tools are pre-compiled (fixed signatures), all arithmetic/date math happens
# here in Python or in SQL — the LLM never computes a number.
#
# This module is in-process by design: the agent imports it and talks to it
# through the same list_tools()/call_tool() contract an out-of-process MCP
# server would expose. Keeping it in-process buys sub-second tool latency for
# the real-time voice path; ops_mcp (Code Mode) stays out-of-process because
# it dispatches to an isolated sandbox pod, where latency is fine and
# isolation is the point.
# ─────────────────────────────────────────────────────────────────────────────
import os
import time
import uuid
import httpx
from datetime import datetime, timezone

PAYER_URL = os.environ.get("AGENT_PAYER_URL", "http://upi-payer-bank:5003")
RECHARGE_URL = os.environ.get("AGENT_RECHARGE_URL", "http://upi-recharge:5007")

# Operator keys accepted by the recharge service (see its OPERATORS table).
BILLER_KEYS = {
    "jio": "jio", "airtel": "airtel", "vi": "vi", "vodafone": "vi",
    "idea": "vi", "bsnl": "bsnl", "mtnl": "mtnl",
}

# Payee-vpa prefixes -> spending categories. Frontend MERCHANTS mirrors this.
CATEGORY_BY_PREFIX = [
    ("swiggy", "Food"), ("zomato", "Food"),
    ("blinkit", "Groceries"), ("grofer", "Groceries"),
    ("amazon", "Shopping"), ("flipkart", "Shopping"),
    ("sharma", "Shopping"),
    ("petrol", "Travel"), ("fuel", "Travel"),
    ("electricity", "Utilities"), ("jio", "Utilities"),
    ("rent", "Housing"),
]

# The staged payment register. stage_payment NEVER moves money — it records
# the intent and returns a descriptor the browser turns into the existing
# 4-digit voice-challenge flow. Funds move only after mock-auth verifies a
# live human voiceprint and issues a single-use step-up token that the payer
# bank checks again server-side.
STAGED_TTL_SECS = 300
_staged: dict[str, dict] = {}


def _flex(x) -> str:
    return str(x).strip().lower()


def _category(payee_vpa: str) -> str:
    pv = _flex(payee_vpa)
    for prefix, cat in CATEGORY_BY_PREFIX:
        if prefix in pv:
            return cat
    return "Other"


def list_tools() -> list[dict]:
    """OpenAI function-calling definitions for the agent loop."""
    return [
        {
            "name": "get_my_balances",
            "description": (
                "Balances of the signed-in customer's accounts. Zero arguments: "
                "the account is bound to the session, never accepted as input."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "check_my_biller",
            "description": (
                "Status of the customer's mobile/biller subscription: days left, "
                "plan price, operator. Pass phone only if the customer told you "
                "their number; otherwise call without phone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "biller": {"type": "string", "description": "e.g. jio, airtel, vi, electricity"},
                    "phone": {"type": "string", "description": "10-digit customer mobile, optional"},
                },
                "required": ["biller"],
            },
        },
        {
            "name": "calculate_split",
            "description": (
                "Deterministically split an amount between N people. "
                "All arithmetic is done here — never do division yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "total in rupees"},
                    "participants": {"type": "integer", "description": "number of people"},
                },
                "required": ["amount", "participants"],
            },
        },
        {
            "name": "get_my_spending_breakdown",
            "description": (
                "Category totals of the customer's debited payments over the "
                "last N days. Use for 'where did my money go' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "window length", "default": 30},
                },
                "required": [],
            },
        },
        {
            "name": "stage_payment",
            "description": (
                "STAGE a payment for the signed-in customer. This does not move "
                "money; the customer must pass the 4-digit spoken voice "
                "challenge before the bank debits. Use it only after the "
                "customer said 'yes/confirm' to a concrete payee+amount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payee": {"type": "string", "description": "payee name or vpa, e.g. Swiggy"},
                    "amount": {"type": "number", "description": "amount in rupees"},
                },
                "required": ["payee", "amount"],
            },
        },
    ]


# ── tool bodies ──────────────────────────────────────────────────────────────
async def _get_balances(ctx) -> dict:
    vpa = ctx["vpa"]
    async with httpx.AsyncClient(timeout=5.0) as cli:
        r = await cli.get(f"{PAYER_URL}/balance/{vpa}")
        r.raise_for_status()
        d = r.json()
    # Shape the one account the session owns as a primary account; the ledger
    # may carry mirrors for other banks but the session never sees them here.
    bal = d.get("balance", d.get("amount", 0))
    return {
        "accounts": [{"name": "Primary", "vpa": vpa, "balance_rupees": round(float(bal), 2)}],
        "note": "This session is bound to this one account by design.",
    }


async def _check_biller(ctx, biller: str, phone: str | None) -> dict:
    op = BILLER_KEYS.get(_flex(biller))
    if not op:
        return {"error": f"unknown biller '{biller}'. Known: {sorted(BILLER_KEYS)}"}
    if not phone:
        return {
            "need_phone": True,
            "note": "Ask the customer for their 10-digit mobile number to look up this plan.",
        }
    digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
    async with httpx.AsyncClient(timeout=5.0) as cli:
        r = await cli.get(f"{RECHARGE_URL}/subscription/{digits}")
        r.raise_for_status()
        d = r.json()
    if not d.get("active"):
        return {"biller": op, "phone": digits, "active": False,
                "note": "No active plan on this number. Offer to recharge."}
    return {
        "biller": op,
        "operator": d.get("operator"),
        "phone": digits,
        "active": True,
        "plan": d.get("plan") or d.get("plan_id"),
        "price_rupees": round(float(d.get("price", 0)), 2),
        "expires_in_days": d.get("days_left"),
        "expiring_soon": d.get("expiring_soon"),
        "expired": d.get("expired"),
    }


def _split(amount: float, participants: int) -> dict:
    if participants < 1 or amount < 0:
        return {"error": "participants must be >= 1 and amount >= 0"}
    total_paise = round(amount * 100)
    base, rem = divmod(total_paise, participants)
    head = (base + 1) / 100
    tail = (total_paise - base - 1) if rem else 0
    tail = (total_paise - (base + 1)) / 100  # last person absorbs the paisa
    return {
        "total_rupees": round(amount, 2),
        "participants": participants,
        "each_rupees": round((base + 1) / 100 if rem else base / 100, 2),
        "last_rupees": round(tail, 2),
        "note": "Computed in integer paise; no floats were harmed.",
    }


async def _spending(ctx, days: int) -> dict:
    vpa = ctx["vpa"]
    days = max(1, min(int(days or 30), 365))
    async with httpx.AsyncClient(timeout=5.0) as cli:
        r = await cli.get(f"{PAYER_URL}/spending-summary/{vpa}", params={"days": days})
        r.raise_for_status()
        d = r.json()
    cats: dict[str, float] = {}
    for row in d.get("rows", []):
        cat = _category(row["payee_vpa"])
        cats[cat] = round(cats.get(cat, 0.0) + float(row["total"]), 2)
    return {
        "vpa": vpa,
        "days": days,
        "categories_rupees": cats,
        "total_rupees": round(sum(cats.values()), 2),
    }


async def _stage(ctx, payee: str, amount: float) -> dict:
    vpa = ctx["vpa"]
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    if amt <= 0 or amt > 100000:
        return {"error": "amount must be between ₹0.01 and ₹100000"}
    payee = payee.strip()
    payee_vpa = payee if "@" in payee else None
    if not payee_vpa:
        return {"error": f"resolve payee '{payee}' to a vpa first — pass the vpa directly here"}
    stage_id = uuid.uuid4().hex[:12]
    _staged[stage_id] = {
        "stage_id": stage_id,
        "payer_vpa": vpa,
        "payee_vpa": payee_vpa,
        "amount": amt,
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": time.time() + STAGED_TTL_SECS,
    }
    # prune
    for k in [k for k, v in _staged.items() if v["expires"] < time.time()]:
        _staged.pop(k, None)
    return {
        "staged": True,
        "stage_id": stage_id,
        "payer_vpa": vpa,
        "payee_vpa": payee_vpa,
        "amount_rupees": amt,
        "note": ("Staged only. The customer must pass the spoken 4-digit voice "
                 "challenge; the bank debits only with the resulting step-up token."),
    }


# ── dispatcher ───────────────────────────────────────────────────────────────
async def call_tool(name: str, args: dict, ctx: dict):
    """Dispatch one tool call. ctx is the session context — the ONLY source of
    identity. Raises ValueError for unknown tools (the agent loop surfaces it
    to the LLM as an error result)."""
    args = args or {}
    if name == "get_my_balances":
        return await _get_balances(ctx)
    if name == "check_my_biller":
        return await _check_biller(ctx, args.get("biller", ""), args.get("phone"))
    if name == "calculate_split":
        return _split(args.get("amount", 0), int(args.get("participants", 0) or 0))
    if name == "get_my_spending_breakdown":
        return await _spending(ctx, args.get("days", 30))
    if name == "stage_payment":
        return await _stage(ctx, args.get("payee", ""), args.get("amount"))
    raise ValueError(f"unknown tool: {name}")
