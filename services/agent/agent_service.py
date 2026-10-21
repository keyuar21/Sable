# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED AGENT SERVICE (:5008)
#
# ONE agent engine, TWO strictly separated entrypoints:
#
#   POST /agent/voice   — customer plane. Reads the account identity ONLY from
#                         the X-VPA header the browser sends after sign-in and
#                         binds it into the MCP session context. It never
#                         imports or touches the ops plane.
#   POST /agent/ops     — ops plane (Code Mode). Requires X-Admin-Token. The
#                         only tool is run_analysis → ai-sandbox pod.
#
# Service names stay honest: this is the customer EXPERIENCE here — "agent"
# — while the repo keeps its internal `upi-` chart prefix. The "upi" string is
# never customer-visible (index.html says Sable).
#
# The LLM is plan/narrate only; every number, date and balance comes from a
# tool. Money never moves from agent output: stage_payment just records the
# intent, and the browser's existing 4-digit live-voice challenge produces the
# step-up token the payer bank re-verifies server-side.
# ─────────────────────────────────────────────────────────────────────────────
import os
import re
import mcp.customer_mcp as customer_mcp
import mcp.ops_mcp as ops_mcp
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from agent.mcp_client import run_agent, AgentResult
from agent.llm_client import MODEL

OPS_TOKEN = os.environ.get("AGENT_OPS_TOKEN", "sable-ops-admin")

app = FastAPI(title="Sable Unified Agent", version="1.0.0")

# The Bank Ops Console lives on its own origin (it has no Ingress), so its
# on-call prompt box calls /agent/ops cross-origin. Scope: POST only, and the
# ops route is additionally gated by X-Admin-Token. Nothing else — the
# customer voice route stays same-origin only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["Content-Type", "X-Admin-Token", "X-VPA"],
)

CUSTOMER_SYSTEM = """You are Sable, the voice banking assistant inside the Sable UPI app.
Rules:
- NEVER invent or calculate numbers yourself. Every balance, price, date, split — get it from a tool.
- The user's account is bound to the session; never ask for or accept another account.
- Decide a payee's vpa ONLY from this known table (exact names):
    Sharmas store: sharmastore@mockbank2 | Blinkit: blinkit@mockbank2
    Swiggy: swiggy@mockbank2 | Zomato: zomato@mockbank2
    Amazon: amazon@mockbank2 | HP Petrol Pump: petrolpump@mockbank2
    Any biller recharge (jio/airtel/vi): pay via stage_payment to biller vpa "<operator>@recharge" after checking the plan.
- Your reply is SPOKEN to the customer through a speech synthesizer:
  * 2-3 short sentences maximum, warm and human, plain words, no markdown, no emoji, no lists.
  * Say amounts as words people can hear: "rupees two hundred and thirty nine" is ok, "₹239.00" is ok too but keep it few.
- For conditional requests (e.g. "pay my bill if it is expiring, use X if balance is low"):
  1. check_my_biller to learn days_left and price
  2. get_my_balances for the balance
  3. compare and pick the account, then stage_payment ONCE with the chosen vpa and amount
  4. explain the routing in your spoken reply and end with "say your voice code to confirm".
- If the customer's phone number is needed and unknown, ask for it in ONE short question.
- For spending questions: get_my_spending_breakdown, then narrate the total and the top two categories.
- If a tool returned an error, say briefly what went wrong and stop — do not retry more than twice.
"""

OPS_SYSTEM = """You are the Sable Ops on-call analyst with Code Mode.
You have exactly one tool, run_analysis(python). Inside the sandbox you have:
- python stdlib, pandas (import pandas as pd)
- a built-in helper `loki_query(logql, minutes_back=90, limit=2000)` returning
  a list of {"ts", "pod", "line"} dicts from Loki. Pod labels look like app="upi-payee-bank".
- each log "line" is a raw text line from the service; failure reasons appear
  as phrases like '504', '500', 'timeout', 'failed', 'error', often key=value
  (error=..., reason=..., status=...).
Working rules:
- Call run_analysis AT MOST TWICE. The second call is allowed only if the
  first failed or needs one refinement. After that you MUST stop and write
  the final summary — never loop pointlessly.
- In your script: query with LogQL selectors like {app="upi-payee-bank"}, then
  aggregate with pure python (Counter + regex). print() explicit named counts
  (e.g. "504: 16", "timeout: 9") and two sample raw lines.
- If a query returns zero lines, say so honestly — do not invent numbers.
- Final answer: 3-5 sentences of plain engineering language with the exact
  numbers you found, then one concrete suggested fix. No markdown, no JSON.
"""


class AgentIn(BaseModel):
    text: str
    days: Optional[int] = 30


def _normalize_vpa(raw: str) -> str:
    v = (raw or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,64}@[a-z0-9._-]{2,64}", v):
        raise HTTPException(422, "X-VPA must look like user@bank")
    return v


class _CustomerSession:
    role = "customer"
    ctx: dict = {}

    def list_tools(self):
        return customer_mcp.list_tools()

    async def call_tool(self, name, args, ctx):
        return await customer_mcp.call_tool(name, args, ctx)


class _OpsSession:
    role = "ops"
    max_tool_rounds = 2   # code mode: two analysis calls, then summarize

    def list_tools(self):
        return ops_mcp.list_tools()

    async def call_tool(self, name, args, ctx):
        return await ops_mcp.call_tool(name, args, ctx)


def _customer_shape(res: AgentResult, *, vpa: str) -> dict:
    """Turn the raw agent result into the dual-payload contract the browser
    expects: spoken script + optional staged action + optional donut chart.

    action/chart come straight from tool RESULTS (deterministic data), never
    from the LLM's narration text."""
    action = None
    chart = None
    for step in res.tool_trail:
        r = step.get("result") or {}
        if step["tool"] == "stage_payment":
            if r.get("staged"):
                action = {
                    "type": "stage_payment",
                    "stage_id": r["stage_id"],
                    "payee_vpa": r["payee_vpa"],
                    "amount": r["amount_rupees"],
                }
        elif step["tool"] == "get_my_spending_breakdown" and r.get("categories_rupees"):
            cats = r["categories_rupees"]
            if cats:
                chart = {
                    "type": "donut",
                    "title": f"Spending — last {r['days']} days",
                    "unit": "INR",
                    "total": r.get("total_rupees"),
                    "data": cats,
                }
    return {
        "reply": res.reply,
        "action": action,
        "chart": chart,
        "model": MODEL,
        "tool_trail": res.tool_trail,
        "errored": res.errored,
    }


@app.get("/health")
def health():
    return {"service": "agent", "model": MODEL, "ok": bool(os.environ.get("LLM_API_KEY"))}


@app.post("/agent/voice")
async def agent_voice(body: AgentIn, x_vpa: str = Header(default="")):
    vpa = _normalize_vpa(x_vpa)
    if not body.text.strip():
        raise HTTPException(422, "text is required")
    sess = _CustomerSession()
    sess.ctx = {"vpa": vpa}
    res = await run_agent(sess, CUSTOMER_SYSTEM, body.text.strip())
    return _customer_shape(res, vpa=vpa)


@app.post("/agent/ops")
async def agent_ops(body: AgentIn, x_admin_token: str = Header(default="")):
    if not x_admin_token or x_admin_token != OPS_TOKEN:
        raise HTTPException(401, "admin token required (X-Admin-Token)")
    if not body.text.strip():
        raise HTTPException(422, "text is required")
    sess = _OpsSession()
    res = await run_agent(sess, OPS_SYSTEM, body.text.strip())
    return {"reply": res.reply, "model": MODEL, "tool_trail": res.tool_trail,
            "errored": res.errored}
