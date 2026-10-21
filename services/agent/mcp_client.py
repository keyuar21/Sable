# ─────────────────────────────────────────────────────────────────────────────
# mcp_client — the reusable agent reasoning loop.
#
# One loop, two flavours, chosen by which MCP "session" you hand it:
#   • customer session (services/mcp/customer_mcp.py): in-process, fast.
#   • ops session (services/mcp/ops_mcp.py): code-mode, out-of-process.
# The loop itself never inspects which one it got — it only follows the
# list_tools()/call_tool() contract. Role isolation therefore lives at the
# entrypoint (see agent_service.py), not in the loop.
#
# Termination: the loop stops when Nemotron returns narration with no
# tool_calls, or when max_turns is exhausted (then whatever narration exists,
# or an error, is returned — the simulator is fail-closed: staged payments
# expire, nothing loops forever burning tokens).
# ─────────────────────────────────────────────────────────────────────────────
import json
from agent import llm_client
from agent.llm_client import LLMError

MAX_TURNS = 8
TURN_BUDGET_SECS = 120.0   # whole-loop wall clock: stop the token burn


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


class AgentResult:
    def __init__(self, reply: str, tool_trail: list[dict], errored: bool = False):
        self.reply = reply
        self.tool_trail = tool_trail
        self.errored = errored


async def run_agent(session, system_prompt: str, user_text: str) -> AgentResult:
    """session must provide: list_tools() -> [openai tool defs]; call_tool(name,
    args, ctx) -> any. ctx is injected by the entrypoint (for the customer
    plane it is the session-locked {"vpa": ...})."""
    import time as _time

    ctx = getattr(session, "ctx", {})
    tools = session.list_tools()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    trail: list[dict] = []
    deadline = _time.time() + TURN_BUDGET_SECS
    nudged = False
    tool_rounds = 0
    tool_cap = getattr(session, "max_tool_rounds", None)   # ops plane: 2

    for turn in range(MAX_TURNS):
        if _time.time() > deadline and trail:
            break
        try:
            out = await llm_client.chat(messages, tools=tools)
        except LLMError as e:
            if trail:            # a retry after tools did real work beats an error
                break
            return AgentResult(str(e), trail, errored=True)

        if not out["tool_calls"]:
            if out["content"].strip():
                return AgentResult(out["content"].strip(), trail)
            # NIM occasionally returns a blank round (hidden reasoning burned
            # the budget). Nudge once; if tools already produced data, finish
            # from the trail instead of looping.
            if not nudged:
                nudged = True
                messages.append({"role": "user",
                                 "content": "Continue: either call a tool or give your final spoken answer now."})
                continue
            break

        if tool_cap is not None and tool_rounds >= tool_cap:
            # Hard stop: the plane wants analysis capped (e.g. Ops Code Mode).
            # Feed everything back and demand a narration with tools removed.
            final = await llm_client.chat(
                messages + [{"role": "user",
                             "content": "Analysis tool budget is now exhausted. Summarize what you found in your final answer using ONLY the data already returned."}],
                tools=None)
            if final["content"].strip():
                return AgentResult(final["content"].strip(), trail)
            break

        messages.append({"role": "assistant", "content": out["content"] or None,
                         "tool_calls": out["tool_calls"]})
        tool_rounds += 1

        for tc in out["tool_calls"]:
            name = tc["function"]["name"]
            args = _parse_args(tc["function"]["arguments"])
            trail.append({"tool": name, "args": args})
            try:
                result = await session.call_tool(name, args, ctx)
                t_out = json.dumps(result, indent=2, default=str)
                trail[-1]["result"] = result
            except Exception as e:                      # noqa: BLE001 — surfaced to the LLM
                t_out = json.dumps({"error": f"{type(e).__name__}: {e}"})
                trail[-1]["result"] = {"error": t_out}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or f"{name}::{turn}",
                "content": t_out[:6000],
            })

    # Turn/time budget exhausted. Prefer the last narration; otherwise return
    # the last tool output so the caller still sees the analysis.
    last_tool_output = ""
    for step in reversed(trail):
        r = step.get("result") or {}
        if r.get("stdout"):
            last_tool_output = r["stdout"]
            break
    reply = last_tool_output or "I couldn't work that out — try rephrasing the request."
    return AgentResult(reply, trail, errored=True)
