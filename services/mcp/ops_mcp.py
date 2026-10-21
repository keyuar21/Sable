# ─────────────────────────────────────────────────────────────────────────────
# PLANE 2 — Ops MCP (Code Mode)
#
# One tool: run_analysis(python). It dispatches the code to the isolated
# ai-sandbox runner pod over HTTP (POST /execute). The sandbox executes with a
# 5s timeout, read-only filesystem, non-root UID, and a NetworkPolicy that
# only lets it reach Loki on :3100 (plus DNS).
#
# The customer plane has zero awareness this module exists: /agent/voice never
# imports ops_mcp, so no customer-session prompt can ever reach logging,
# cluster state, or another tenant's data.
# ─────────────────────────────────────────────────────────────────────────────
import os
import httpx

SANDBOX_URL = os.environ.get("AGENT_SANDBOX_URL", "http://upi-code-sandbox.ai-sandbox.svc:8080")


def list_tools() -> list[dict]:
    return [{
        "name": "run_analysis",
        "description": (
            "Run a short Python analysis script in the locked ai-sandbox pod. "
            "Available: python stdlib, pandas, AND a built-in async-hostile "
            "helper `loki_query(logql, minutes_back=90, limit=2000)` that "
            "returns a list of {'ts','pod','line'} log dicts. PUT EVERYTHING "
            "IN ONE CALL — the sandbox cannot read files you wrote across "
            "calls (read-only filesystem). print() your findings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "python_code": {"type": "string", "description": "the python source to run"},
            },
            "required": ["python_code"],
        },
    }]


async def call_tool(name: str, args: dict, ctx: dict):
    args = args or {}
    if name != "run_analysis":
        raise ValueError(f"unknown tool: {name}")
    code = args.get("python_code") or ""
    if len(code) > 20000:
        return {"error": "code too long (20k cap)"}
    async with httpx.AsyncClient(timeout=15.0) as cli:
        try:
            r = await cli.post(f"{SANDBOX_URL}/execute", json={"code": code, "timeout_s": 5.0})
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"error": f"sandbox unreachable: {e}"}
    d = r.json()
    out = {"ok": d.get("ok", False)}
    if d.get("ok"):
        out["stdout"] = (d.get("result") or "")[-4000:]
    else:
        out["error"] = d.get("error", "sandbox returned no error text")
        if d.get("result"):
            out["stdout"] = d["result"][-2000:]
    return out
