# ─────────────────────────────────────────────────────────────────────────────
# CODE-SANDBOX (:8080, namespace ai-sandbox)
#
# Executes LLM-generated analysis code for the Ops MCP. Hard guarantees:
#   • subprocess-style isolation per run (spawned process, daemon-killed)
#   • hard 5.0s wall clock kill
#   • container runs non-root (UID 10001) with a read-only root filesystem
#   • NetworkPolicy (see deploy/k8s/sandbox-lockdown-netpol.yaml) allows
#     egress ONLY to Loki :3100 (+ DNS). No banking ports, no internet.
#   • stdout is capped; code size capped; no files persist across runs.
#
# The one deliberate doorway is loki_query(): injected into every run's
# globals, dollar-cost secure by construction (read-only HTTP GET at Loki).
# ─────────────────────────────────────────────────────────────────────────────
import os
import multiprocessing as mp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sandbox_worker import run as _worker_run, LOKI_URL as _WORKER_LOKI_URL

LIMIT_CODE_BYTES = 20000
DEFAULT_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 9.0

app = FastAPI(title="Sable code-sandbox", version="1.0.0")


class ExecIn(BaseModel):
    code: str
    timeout_s: float = DEFAULT_TIMEOUT_S


def _run_in_worker(code: str, q: "mp.Queue") -> None:
    """Thin shim — the real worker lives in sandbox_worker (heavy-import-free
    so each spawn starts fast)."""
    _worker_run(code, q)


@app.get("/health")
def health():
    return {"service": "code-sandbox", "loki": _WORKER_LOKI_URL, "ok": True}


@app.post("/execute")
def execute(req: ExecIn):
    if len(req.code.encode()) > LIMIT_CODE_BYTES:
        raise HTTPException(413, f"code exceeds {LIMIT_CODE_BYTES} bytes")
    timeout = max(0.2, min(float(req.timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))

    ctx = mp.get_context("spawn")
    q: "mp.Queue" = ctx.Queue()
    p = ctx.Process(target=_run_in_worker, args=(req.code, q), daemon=True)
    p.start()
    try:
        out = q.get(timeout=timeout)
    except Exception:                                            # queue.Empty et al — treat as hard timeout
        out = {"ok": False, "error": f"execution exceeded {timeout:.1f}s and was killed"}
    finally:
        if p.is_alive():
            p.terminate()
        p.join(0.5)
    return out
