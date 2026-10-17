# ============================================================================
# FINAL ONE-SHOT Kaggle CPU LLM + Cloudflare Worker gateway
# Paste this entire file into ONE Kaggle notebook code cell and run it.
# Kaggle settings: Accelerator=None (CPU), Internet=ON.
#
# This setup uses the tested CPU llama-cpp-python server (no GPU required).
# It downloads Qwen2.5-3B-Instruct Q4_K_M if not already complete, starts a
# local API-key proxy and Cloudflare quick tunnel, registers the tunnel with
# the existing Worker, then requires BOTH end-to-end tests to pass.
#
# The existing hardcoded Worker credentials and model alias are retained for
# this initial test. They are not printed in output; do not publish this file.
# CPU generation can be slow; long prompts may still exceed the context window.
# ============================================================================

import os
import sys
import re
import json
import time
import shutil
import signal
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------- hardcoded config -------------------------------
WORK = Path("/tmp/llama-cpu")
BIN_DIR = WORK / "llama.cpp"
MODEL_DIR = WORK / "model"
MODEL_FILE = MODEL_DIR / "qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
    "qwen2.5-3b-instruct-q4_k_m.gguf"
)
MODEL_MIN_BYTES = 1_800_000_000

PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
SERVED_NAME = "qwen2.5-3b-kaggle-t4x2"  # preserve existing client model name
CTX = 8192  # allow the 7k-token prompt seen in your logs, plus room for the answer
THREADS = max(1, min(4, os.cpu_count() or 4))
LLAMA_REPO = "ggml-org/llama.cpp"

PROXY_PORT = 8002
PROXY_BASE = f"http://127.0.0.1:{PROXY_PORT}"
KEY_FILE = WORK / ".api_key"
PROXY_PY = WORK / "auth_proxy.py"
PROXY_LOG = WORK / "auth_proxy.log"
MODEL_LOG = WORK / "llama_server.log"
CF_BIN = WORK / "cloudflared"
CF_LOG = WORK / "cloudflared.log"
CF_DOWNLOAD = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-linux-amd64"
)
WORKER_URL = "https://ai-builder.keyaurbhlonde.workers.dev"

# Keeping the exact values from your current notebook for now, as requested.
# They must match the Worker secrets API_KEY and REGISTER_TOKEN.
API_KEY = "srkxOGWqn454X4GtmzxMtdOJHpLe8t_KV6KFvmXnLvE"
REGISTER_TOKEN = "KqQYN-RpPqUAkowK1zvUcGsbDzwKqN1HnA-X9wT6DCk"

# -------------------------------- helpers ------------------------------------
def log(message):
    print(f"\n=== {message}", flush=True)


def tail(path, lines=60):
    try:
        return "".join(Path(path).read_text(errors="replace").splitlines(True)[-lines:])
    except Exception:
        return "(log unavailable)"


def run(cmd, **kwargs):
    print("$ " + " ".join(map(str, cmd))[:260], flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def http_json(url, data=None, headers=None, timeout=30):
    # Use an explicit application User-Agent. The default urllib User-Agent was
    # blocked by Cloudflare Error 1010 in the streaming test.
    hdrs = {"User-Agent": "qwen-kaggle-client/1.0"}
    hdrs.update(headers or {})
    payload = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    if payload is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=payload, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def wait_for(check, seconds=60, interval=2):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def get_models():
    try:
        status, body = http_json(f"{BASE}/v1/models", timeout=3)
        if status != 200:
            return []
        payload = json.loads(body)
        return [str(item.get("id", "")) for item in payload.get("data", [])]
    except Exception:
        return []


def server_up_with_expected_model():
    return SERVED_NAME in get_models()


def lib_env(binary):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(Path(binary).parent) + ":" + env.get("LD_LIBRARY_PATH", "")
    env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def curl_download(url, output, tries=5):
    output = str(output)
    for attempt in range(1, tries + 1):
        cmd = [
            "curl", "-fL", "--connect-timeout", "20", "--retry", "3",
            "--retry-delay", "2", "-C", "-", "-o", output, url,
        ]
        result = subprocess.run(cmd)
        if result.returncode == 0:
            return True
        if result.returncode == 33:  # server refused resume; retry from scratch
            try:
                os.remove(output)
            except OSError:
                pass
        print(f"Download attempt {attempt}/{tries} failed (curl exit {result.returncode}).", flush=True)
        time.sleep(min(attempt * 2, 8))
    return False


def proc_cmdlines():
    """Yield (pid, argv text) for accessible processes, without matching this cell itself."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            if raw:
                yield int(entry.name), raw.replace(b"\0", b" ").decode(errors="ignore")
        except (OSError, PermissionError):
            continue


def stop_processes(predicate, wait_seconds=8):
    """Safely stop only processes whose actual /proc command line matches predicate."""
    pids = [pid for pid, cmd in proc_cmdlines() if predicate(cmd)]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        if not alive:
            return
        time.sleep(0.5)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def terminate_old_model_servers():
    # Restrict cleanup to llama processes associated with this model/port, not unrelated jobs.
    def is_ours(cmd):
        is_llama = "llama-server" in cmd or "llama_cpp.server" in cmd
        is_our_model = str(MODEL_FILE) in cmd or str(WORK) in cmd
        is_our_port = re.search(r"(?:--port\s+|--port=)8001(?:\s|$)", cmd) is not None
        return is_llama and (is_our_model or is_our_port)
    stop_processes(is_ours)
    time.sleep(1)


def model_file_ok():
    try:
        if MODEL_FILE.stat().st_size < MODEL_MIN_BYTES:
            return False
        with MODEL_FILE.open("rb") as model:
            return model.read(4) == b"GGUF"
    except OSError:
        return False


# -------------------------- 0. preflight ------------------------------------
log("0/9 Preflight: internet, disk and CPU")
WORK.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)
print("CPU threads:", THREADS)
free_gb = shutil.disk_usage(WORK).free / 1e9
print(f"Free disk in {WORK}: {free_gb:.1f} GB")
if free_gb < 6:
    raise RuntimeError("Less than 6 GB free disk space; model and runtime need about 4 GB.")
try:
    urllib.request.urlopen("https://huggingface.co", timeout=15).close()
    urllib.request.urlopen("https://github.com", timeout=15).close()
except Exception as exc:
    raise RuntimeError("Internet check failed. Enable Kaggle Internet and restart the session.") from exc

# -------------------------- 1-3. model server -------------------------------
log("1/9 Check model server")
models = get_models()
if models:
    print("Existing model server detected:", models)
else:
    print("No responding local model server detected.")
print("Restarting only this setup's previous model server to apply the configured context size.")
terminate_old_model_servers()

log("2/9 Install/check llama-cpp-python CPU server")
# This CPU wheel path was verified in the current Kaggle environment and
# avoids the stale GitHub release asset URLs that returned 404 previously.
run([
    sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
    "llama-cpp-python[server]", "--extra-index-url",
    "https://abetlen.github.io/llama-cpp-python/whl/cpu",
])
run([sys.executable, "-c", "import llama_cpp; print('llama_cpp', llama_cpp.__version__)"])

log("3/9 Download/check Qwen2.5-3B-Instruct Q4_K_M")
if model_file_ok():
    print(f"Model file OK ({MODEL_FILE.stat().st_size / 1e9:.2f} GB):", MODEL_FILE)
else:
    print("Downloading model (~2.1 GB); curl will resume an interrupted download.")
    if not curl_download(MODEL_URL, MODEL_FILE, tries=5) or not model_file_ok():
        raise RuntimeError("Model download is incomplete. Check Kaggle Internet and rerun this cell.")
    print(f"Model download verified ({MODEL_FILE.stat().st_size / 1e9:.2f} GB).")

log("4/9 Launch model server")
server_cmd = [
    sys.executable, "-m", "llama_cpp.server", "--model", str(MODEL_FILE),
    "--model_alias", SERVED_NAME, "--host", "127.0.0.1", "--port", str(PORT),
    "--n_ctx", str(CTX), "--n_threads", str(THREADS),
]
server_env = dict(os.environ, CUDA_VISIBLE_DEVICES="")

with MODEL_LOG.open("w", encoding="utf-8") as log_file:
    proc = subprocess.Popen(
        server_cmd, stdin=subprocess.DEVNULL, stdout=log_file,
        stderr=subprocess.STDOUT, env=server_env, start_new_session=True,
    )
print("Model server PID:", proc.pid, "| log:", MODEL_LOG)

if not wait_for(server_up_with_expected_model, seconds=600, interval=3):
    if proc.poll() is not None:
        print(tail(MODEL_LOG, 100))
        raise RuntimeError("The model server exited before it became ready; see log above.")
    print(tail(MODEL_LOG, 100))
    raise RuntimeError("Model server did not become ready in 10 minutes; see log above.")

print("API READY:", get_models())

# Local non-stream test before touching Cloudflare.
log("5/9 Local model smoke test")
local_status, local_body = http_json(
    f"{BASE}/v1/chat/completions",
    data={
        "model": SERVED_NAME,
        "messages": [{"role": "user", "content": "Explain Kubernetes in two simple sentences."}],
        "max_tokens": 80,
        "temperature": 0.2,
        "stream": False,
    },
    timeout=240,
)
if local_status != 200:
    raise RuntimeError(f"Local model test failed: HTTP {local_status}: {local_body[:1000]}\n{tail(MODEL_LOG, 40)}")
try:
    local_answer = json.loads(local_body)["choices"][0]["message"]["content"]
except Exception as exc:
    raise RuntimeError(f"Local model returned invalid JSON: {local_body[:1000]}") from exc
if not isinstance(local_answer, str) or not local_answer.strip():
    raise RuntimeError("Local model returned an empty answer; inspect llama_server.log.")
print("LOCAL TEST REPLY:", local_answer[:700])

# ------------------------ 6. auth proxy implementation ----------------------
try:
    import aiohttp  # noqa: F401
except ImportError:
    run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "aiohttp"])

AUTH_PROXY_CODE = r'''import asyncio, hmac, json, os
from contextlib import suppress
import aiohttp
from aiohttp import web

UPSTREAM = os.environ["UPSTREAM"].rstrip("/")
PORT = int(os.environ["PROXY_PORT"])
with open(os.environ["KEY_FILE"], encoding="utf-8") as f:
    KEY = f.read().strip()

DROP_REQ = {
    "host", "authorization", "content-length", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "accept-encoding"
}
KEEP_RESP = {"content-type", "cache-control", "retry-after", "www-authenticate"}
HEARTBEAT_SECONDS = 12


def normalize_content(content):
    # Text-only models need OpenAI text-part arrays normalized to plain strings.
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type", "text") == "text":
            parts.append(str(part.get("text", "")))
    return "\n".join(parts)


def normalize_body(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw, False
    if not isinstance(data, dict):
        return raw, False
    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                message["content"] = normalize_content(message["content"])
    streaming = data.get("stream") is True
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), streaming


async def relay_body(upstream, response, is_sse):
    iterator = upstream.content.iter_any().__aiter__()
    pending = None
    try:
        while True:
            pending = asyncio.create_task(iterator.__anext__())
            while True:
                done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
                if done:
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        return
                    if chunk:
                        await response.write(chunk)
                    break
                # SSE comments are ignored by clients but keep the downstream stream alive
                # while CPU prompt evaluation produces no model tokens.
                if is_sse:
                    await response.write(b": keep-alive\n\n")
    except (ConnectionResetError, BrokenPipeError):
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending


async def handle(request):
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, KEY):
        return web.json_response(
            {"error": {"message": "Missing or invalid API key", "type": "authentication_error"}},
            status=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    if not request.path.startswith("/v1/"):
        return web.json_response({"error": {"message": "Not found", "type": "not_found"}}, status=404)

    raw = await request.read()
    requested_stream = False
    if request.method == "POST" and request.path.rstrip("/").endswith(("/chat/completions", "/completions")):
        raw, requested_stream = normalize_body(raw)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ}
    headers["Accept-Encoding"] = "identity"
    timeout = aiohttp.ClientTimeout(total=3600, sock_connect=15, sock_read=None)
    response = None
    try:
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            async with session.request(
                request.method,
                UPSTREAM + str(request.rel_url),
                headers=headers,
                data=raw if request.method not in ("GET", "HEAD") else None,
                allow_redirects=False,
            ) as upstream:
                upstream_type = upstream.headers.get("Content-Type", "")
                is_sse = requested_stream and "text/event-stream" in upstream_type.lower() and upstream.status < 400
                resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() in KEEP_RESP}
                resp_headers["Cache-Control"] = "no-store, no-cache, no-transform"
                resp_headers["X-Accel-Buffering"] = "no"
                if is_sse:
                    resp_headers["Content-Type"] = "text/event-stream; charset=utf-8"

                response = web.StreamResponse(status=upstream.status, headers=resp_headers)
                # Use close-delimited streaming instead of explicitly forcing HTTP/1.1
                # chunked transfer encoding, which triggered cloudflared framing errors.
                response.force_close()
                await response.prepare(request)
                await relay_body(upstream, response, is_sse)
                with suppress(ConnectionResetError, BrokenPipeError, RuntimeError):
                    await response.write_eof()
                return response
    except asyncio.CancelledError:
        # The client may have navigated away or cancelled generation.
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
        if response is not None and response.prepared:
            print("Upstream stream interrupted:", type(exc).__name__, flush=True)
            with suppress(ConnectionResetError, BrokenPipeError, RuntimeError):
                await response.write_eof()
            return response
        return web.json_response(
            {"error": {"message": f"Upstream model server unavailable: {type(exc).__name__}", "type": "bad_gateway"}},
            status=502,
            headers={"Cache-Control": "no-store", "Retry-After": "3"},
        )


app = web.Application(client_max_size=64 * 1024 * 1024)
app.router.add_route("*", "/{tail:.*}", handle)
web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)
'''

log("6/9 Write/restart the hardened auth proxy")
WORK.mkdir(parents=True, exist_ok=True)
KEY_FILE.write_text(API_KEY, encoding="utf-8")
os.chmod(KEY_FILE, 0o600)
PROXY_PY.write_text(AUTH_PROXY_CODE, encoding="utf-8")
os.chmod(PROXY_PY, 0o600)
# Kill only a previous copy of this exact proxy script (not the model process).
stop_processes(lambda cmd: str(PROXY_PY) in cmd and "auth_proxy.py" in cmd)
time.sleep(1)
proxy_env = dict(os.environ, KEY_FILE=str(KEY_FILE), UPSTREAM=BASE, PROXY_PORT=str(PROXY_PORT))
with PROXY_LOG.open("w", encoding="utf-8") as log_file:
    subprocess.Popen(
        [sys.executable, str(PROXY_PY)], stdin=subprocess.DEVNULL,
        stdout=log_file, stderr=subprocess.STDOUT, env=proxy_env, start_new_session=True,
    )

# Missing key must fail; correct key should reach the local model server.
if not wait_for(lambda: http_json(f"{PROXY_BASE}/v1/models", timeout=3)[0] == 401, seconds=30):
    print(tail(PROXY_LOG, 60))
    raise RuntimeError("Auth proxy failed to start. See auth_proxy.log above.")
print("Auth proxy ready on", PROXY_BASE, "(unauthenticated request correctly returns 401).")

# ---------------------- 7. Cloudflare tunnel -------------------------------
def read_tunnel_url():
    try:
        text = CF_LOG.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
    return matches[-1] if matches else None


def is_tunnel_running():
    # Inspect /proc to avoid matching a pgrep command against itself.
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            if str(CF_BIN) in cmd and "tunnel" in cmd and "--url" in cmd:
                return True
        except (OSError, PermissionError):
            continue
    return False


log("7/9 Start/reuse Cloudflare quick tunnel")
if not CF_BIN.exists() or CF_BIN.stat().st_size < 10_000_000:
    print("Downloading cloudflared binary...")
    if not curl_download(CF_DOWNLOAD, CF_BIN, tries=3) or CF_BIN.stat().st_size < 10_000_000:
        raise RuntimeError("Could not download cloudflared from GitHub.")
    os.chmod(CF_BIN, 0o755)

PUBLIC_URL = read_tunnel_url() if is_tunnel_running() else None
if not PUBLIC_URL:
    # Stop only the previous quick tunnel process owned by this setup.
    stop_processes(lambda cmd: str(CF_BIN) in cmd and "tunnel" in cmd and "--url" in cmd)
    time.sleep(1)
    with CF_LOG.open("w", encoding="utf-8") as log_file:
        subprocess.Popen(
            [str(CF_BIN), "tunnel", "--no-autoupdate", "--url", PROXY_BASE],
            stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    if not wait_for(lambda: bool(read_tunnel_url()), seconds=120, interval=2):
        print(tail(CF_LOG, 60))
        raise RuntimeError("cloudflared did not create a quick tunnel.")
    PUBLIC_URL = read_tunnel_url()
print("Current quick tunnel:", PUBLIC_URL)

# ----------------------- 8. Register with Worker ----------------------------
log("8/9 Register tunnel with the existing Cloudflare Worker")
registered = False
for attempt in range(1, 7):
    status, body = http_json(
        WORKER_URL + "/_register",
        data={"url": PUBLIC_URL},
        headers={"Authorization": f"Bearer {REGISTER_TOKEN}"},
        timeout=30,
    )
    print(f"Worker registration attempt {attempt}: HTTP {status} {body[:180]}")
    if 200 <= status < 300:
        registered = True
        break
    if status in (401, 403):
        raise RuntimeError("Worker rejected REGISTER_TOKEN. It must match the current Worker secret.")
    time.sleep(4)
if not registered:
    raise RuntimeError("Could not register the tunnel with the Worker; see errors above.")
print("MODEL AND ROUTING: PASS ->", SERVED_NAME, "registered via the Worker")

# --------------------- 9. End-to-end gateway tests --------------------------
log("9/9 End-to-end tests through the fixed Worker URL")

# Non-stream test. The higher timeout is intentional for a CPU model.
status, body = http_json(
    WORKER_URL + "/v1/chat/completions",
    data={
        "model": SERVED_NAME,
        "messages": [{"role": "user", "content": "Reply exactly: NONSTREAM_OK"}],
        "max_tokens": 24,
        "temperature": 0,
        "stream": False,
    },
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "qwen-kaggle-client/1.0",
    },
    timeout=300,
)
if status != 200:
    print("Worker returned a non-200 response. Recent tunnel/proxy logs follow:")
    print("--- auth_proxy.log ---\n", tail(PROXY_LOG, 40))
    print("--- cloudflared.log ---\n", tail(CF_LOG, 30))
    raise RuntimeError(f"Non-stream gateway test failed: HTTP {status}: {body[:1500]}")
try:
    nonstream_answer = json.loads(body)["choices"][0]["message"]["content"]
except Exception as exc:
    raise RuntimeError(f"Worker returned HTTP 200 but invalid chat JSON: {body[:1000]}") from exc
if not isinstance(nonstream_answer, str) or not nonstream_answer.strip():
    raise RuntimeError("Non-stream gateway returned an empty answer. See logs below.")
if "NONSTREAM_OK" not in nonstream_answer:
    raise RuntimeError(f"Non-stream request returned HTTP 200 but not the expected marker: {nonstream_answer[:500]}")
print("NON-STREAMING INFERENCE: PASS ->", nonstream_answer.strip()[:250])

# Streaming test. SSE heartbeat comment lines are ignored; actual token deltas are joined.
stream_payload = {
    "model": SERVED_NAME,
    "messages": [{"role": "user", "content": "Reply exactly: STREAM_OK"}],
    "max_tokens": 24,
    "temperature": 0,
    "stream": True,
}
request = urllib.request.Request(
    WORKER_URL + "/v1/chat/completions",
    data=json.dumps(stream_payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        # Do not set Accept: text/event-stream here; this exact shape succeeded
        # in the end-to-end test after the default urllib request was blocked.
        "User-Agent": "qwen-kaggle-client/1.0",
    },
)
stream_parts = []
stream_done = False
try:
    with urllib.request.urlopen(request, timeout=300) as response:
        if response.status != 200:
            raise RuntimeError(f"Streaming gateway test failed: HTTP {response.status}")
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                stream_done = True
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    stream_parts.append(delta)
except urllib.error.HTTPError as exc:
    print(f"Streaming HTTP error: {exc.code} {exc.reason}; body: {exc.read().decode('utf-8', errors='replace')[:1500]}")
    print("Streaming test encountered an error. Recent proxy/server/tunnel logs follow:")
    print("--- auth_proxy.log ---\n", tail(PROXY_LOG, 40))
    print("--- llama_server.log ---\n", tail(MODEL_LOG, 40))
    print("--- cloudflared.log ---\n", tail(CF_LOG, 30))
    raise
except Exception:
    print("Streaming test encountered an error. Recent proxy/server/tunnel logs follow:")
    print("--- auth_proxy.log ---\n", tail(PROXY_LOG, 40))
    print("--- llama_server.log ---\n", tail(MODEL_LOG, 40))
    print("--- cloudflared.log ---\n", tail(CF_LOG, 30))
    raise

stream_answer = "".join(stream_parts).strip()
if not stream_answer:
    print("--- auth_proxy.log ---\n", tail(PROXY_LOG, 40))
    print("--- llama_server.log ---\n", tail(MODEL_LOG, 40))
    raise RuntimeError("Streaming request returned no text. Non-stream passed, so inspect the client/SSE path.")
if "STREAM_OK" not in stream_answer:
    raise RuntimeError(f"Streaming request returned text but not the expected marker: {stream_answer[:500]}")
if not stream_done:
    raise RuntimeError("Streaming response did not end with data: [DONE]. Check the proxy/tunnel logs.")
print("STREAMING INFERENCE: PASS ->", stream_answer[:250])
print("Streaming terminator: PASS -> data: [DONE]")

print("\n" + "=" * 76)
print("ALL CHECKS PASSED — Kaggle CPU model + Cloudflare Worker gateway is responding.")
print("Local API base  :", BASE + "/v1")
print("Fixed API base  :", WORKER_URL + "/v1")
print("Chat endpoint   :", WORKER_URL + "/v1/chat/completions")
print("Quick tunnel    :", PUBLIC_URL)
print("Model name      :", SERVED_NAME)
print("Inference mode  : CPU (llama-cpp-python)")
print("Context size    :", CTX)
print("Logs            :", MODEL_LOG, PROXY_LOG, CF_LOG)
print("Credentials are hardcoded in this cell but are not printed in the output.")
print("=" * 76)
