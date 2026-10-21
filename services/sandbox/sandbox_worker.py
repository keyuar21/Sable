# Worker executed inside the spawned analysis child. Kept free of heavy
# imports (no fastapi/pydantic here) so each sandbox run starts in ~10ms of
# interpreter spin-up instead of paying the service stack's import cost.
import io
import os
import json
import time
from contextlib import redirect_stdout
from urllib import request as _rq
from urllib.parse import quote

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.observability.svc:3100")


def loki_query(logql: str, minutes_back: int = 90, limit: int = 2000):
    """Injected into every analysis run. Read-only Loki read via urllib."""
    end_ns = int(time.time() * 1e9)
    start_ns = end_ns - int(minutes_back * 60 * 1e9)
    url = (f"{LOKI_URL}/loki/api/v1/query_range"
           f"?query={quote(logql, safe='=')}&start={start_ns}&end={end_ns}&limit={int(limit)}")
    with _rq.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    out = []
    for stream in (data.get("data", {}).get("result") or []):
        labels = stream.get("stream", {})
        pod = labels.get("pod") or labels.get("app") or ""
        for ts, line in (stream.get("values") or []):
            out.append({"ts": ts, "pod": pod, "line": line})
    return out


def run(code: str, q) -> None:  # noqa: ANN001 — q is a multiprocessing Queue
    """Fresh globals, captured stdout. exec() here is deliberate: the sandbox
    itself is the containment boundary (read-only fs, non-root, netpol)."""
    import contextlib
    import builtins
    buf = io.StringIO()

    def _print(*a, **k):
        k.pop("file", None)
        norm = []
        for x in a:
            # objects like big DataFrames print huge — cap each arg
            s = str(x)
            norm.append(s if len(s) < 8000 else s[:8000] + "…<truncated>")
        return builtins.print(*norm, file=buf, **k)

    g = {"__builtins__": builtins, "print": _print, "loki_query": loki_query}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, "<analysis>", "exec"), g, g)  # noqa: S102 — the sandbox IS the containment
        out = buf.getvalue()
        q.put({"ok": True, "result": out[-20000:]})
    except BaseException as e:                                   # noqa: BLE001 — reported to the LLM
        q.put({"ok": False, "result": buf.getvalue()[-2000:],
               "error": f"{type(e).__name__}: {e}"})
