# ─────────────────────────────────────────────────────────────────────────────
# LLM client — the seam where the self-hosted vLLM used to live.
#
# The architecture called for a self-hosted vLLM (Qwen-2.5). It was replaced,
# per decision, by NVIDIA NIM's hosted endpoint (integrate.api.nvidia.com)
# serving nvidia/nemotron-3.5-lightning-30b-a3b. NIM speaks the same
# OpenAI /v1/chat/completions contract including native tool calling, so this
# module is deliberately shaped exactly like the vllm_client.py the spec
# describes — only the base URL and credentials moved.
#
# Credentials never live in source: LLM_API_KEY arrives from the
# `nim-api` Kubernetes Secret (deploy/helm/values/agent.yaml wires it).
# ─────────────────────────────────────────────────────────────────────────────
import os
import httpx

BASE_URL = os.environ.get("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")

# Nemotron's hidden "thinking" phase is wonderful for math proofs and useless
# here: the tool loop does zero arithmetic, and thinking once ate the whole
# completion budget and returned an empty round. Off by default; flip with
# LLM_THINKING=true if a future prompt discovers it actually needs it.
THINKING = os.environ.get("LLM_THINKING", "false").strip().lower() in ("1", "true", "yes")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1600"))


class LLMError(RuntimeError):
    pass


async def chat(messages: list[dict], tools: list[dict] | None = None,
               temperature: float = 0.2, max_tokens: int | None = None) -> dict:
    """One round of the OpenAI-style conversation. Returns:
      { content: str, tool_calls: [ {id, type:'function', function:{name,arguments}} ] }

    Nemotron returns either narration (content) or a tool_calls list; empty
    content with tool_calls is the normal tool-turn shape."""
    if not API_KEY:
        raise LLMError("LLM_API_KEY not configured — create the nim-api secret")
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or MAX_TOKENS,
        "stream": False,
    }
    if not THINKING:
        body["chat_template_kwargs"] = {"thinking": False}
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
        body["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=90.0) as cli:
        r = await cli.post(
            f"{BASE_URL}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            raise LLMError(f"LLM {r.status_code}: {r.text[:300]}")
        d = r.json()

    choice = d["choices"][0]
    msg = choice.get("message", {}) or {}
    tool_calls = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {}) or {}
        tool_calls.append({
            "id": tc.get("id", ""),
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "{}")},
        })
    return {"content": msg.get("content") or "", "tool_calls": tool_calls}
