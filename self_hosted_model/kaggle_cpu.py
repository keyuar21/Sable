# ==========================================================================
# ONE-SHOT (CPU ONLY): Qwen2.5-3B-Instruct on a Kaggle / any Linux CPU notebook
# served through an OpenAI-compatible API, behind your Cloudflare Worker.
#
# Paste this whole file into ONE cell of a fresh notebook and run it.
# Settings: Accelerator = "None" (CPU), Internet = ON.
#
# Why this differs from the GPU script: the vLLM wheel and the sharded model used
# there are built for Tesla T4 GPUs only and cannot run on a CPU. This script uses
# llama.cpp (llama-server) with the official 4-bit GGUF of the SAME model family
# (Qwen/Qwen2.5-3B-Instruct). Everything after the server is identical: same auth
# proxy (port 8002), same Cloudflare quick tunnel, same Worker registration, same
# model name "qwen2.5-3b-kaggle-t4x2" - so your apps and Worker need no changes.
#
# What it does:
#   1. Downloads the prebuilt llama.cpp CPU server (falls back to the
#      llama-cpp-python package if the prebuilt binary cannot be used).
#   2. Downloads the Qwen2.5-3B-Instruct Q4_K_M GGUF (~2.1 GB) from Hugging Face.
#   3. Starts the server in the BACKGROUND (the cell finishes, the server keeps
#      running) and sends a test chat request.
#   4. Starts the API-key proxy + Cloudflare quick tunnel, registers the tunnel
#      URL with your Worker, tests the gateway and prints URL + key at the end.
#
# First run takes roughly 5-10 minutes. Re-running in the same session is safe:
# finished steps are skipped and a running server is reused.
# Expect roughly 3-8 tokens/second on a 4-core Kaggle CPU (slower than the GPUs).
# ==========================================================================
import os, sys, re, json, time, shutil, subprocess, tarfile, zipfile, urllib.request, urllib.error

# ---------------------------- configuration -------------------------------
WORK = "/tmp/llama-cpu"
BIN_DIR = f"{WORK}/llama.cpp"
MODEL_DIR = f"{WORK}/model"
LOG = f"{WORK}/llama_server.log"

PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
SERVED_NAME = "qwen2.5-3b-kaggle-t4x2"   # kept identical so the Worker/apps need no change

MODEL_URL = ("https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
             "qwen2.5-3b-instruct-q4_k_m.gguf")
MODEL_FILE = f"{MODEL_DIR}/qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_MIN_BYTES = 1_800_000_000          # file is ~2.1 GB; smaller = incomplete download

CTX = 4096                                # context window (prompt + answer)
THREADS = max(1, os.cpu_count() or 4)

LLAMA_REPO = "ggml-org/llama.cpp"

# ------------------------------- helpers ----------------------------------
def log(msg):
    print(f"\n=== {msg}", flush=True)

def run(cmd, **kw):
    print("$ " + " ".join(str(c) for c in cmd)[:220], flush=True)
    subprocess.run(cmd, check=True, **kw)

def api_get(path, data=None, timeout=5):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def server_up():
    try:
        api_get("/v1/models", timeout=3)
        return True
    except Exception:
        return False

def tail(path, n=60):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return "(no log)"

def curl_get(url, out, tries=4):
    """Download with resume + retries; returns True on exit code 0."""
    for _ in range(tries):
        r = subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "3",
                            "-C", "-", "-o", out, url])
        if r.returncode == 0:
            return True
        if r.returncode == 33:      # range not supported -> restart from scratch
            try:
                os.remove(out)
            except OSError:
                pass
    return False

# --------------------------- 0. preflight ---------------------------------
log("0/9 Preflight (internet + disk + CPU)")
os.makedirs(WORK, exist_ok=True)
print(f"CPU threads: {THREADS}")
free_gb = shutil.disk_usage(WORK).free / 1e9
print(f"Free disk in {WORK}: {free_gb:.1f} GB")
if free_gb < 6:
    raise RuntimeError("Less than 6 GB free disk space; the model + server need about 4 GB.")
try:
    urllib.request.urlopen("https://huggingface.co", timeout=15)
    urllib.request.urlopen("https://github.com", timeout=15)
except Exception as e:
    raise RuntimeError("No internet (or GitHub/Hugging Face blocked). Turn on "
                       "Settings -> Internet and restart the session.") from e

already_serving = server_up()
if already_serving:
    print("A model server is already answering on", BASE, "- skipping install and launch.")

# ----------------------- 1-3. llama.cpp server binary ---------------------
def pick_asset(assets):
    """Choose the plain Ubuntu x64 CPU build (not vulkan/rocm/cuda/openvino/etc)."""
    for a in assets:
        if re.search(r"bin-ubuntu-x64\.(zip|tar\.gz)$", a["name"]):
            return a
    bad = ("vulkan", "rocm", "cuda", "sycl", "openvino", "arm", "s390x", "hip", "musa")
    for a in assets:
        n = a["name"].lower()
        if "ubuntu" in n and "x64" in n and n.endswith((".zip", ".tar.gz")) \
                and not any(b in n for b in bad):
            return a
    return None

def candidate_urls():
    """Download URLs for the latest llama.cpp CPU build: GitHub API first, then guess from the tag."""
    urls = []
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{LLAMA_REPO}/releases/latest",
            headers={"User-Agent": "kaggle-cpu-oneshot/1.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read())
        a = pick_asset(rel.get("assets", []))
        if a:
            print("Latest llama.cpp release:", rel.get("tag_name"), "->", a["name"])
            urls.append((a["name"], a["browser_download_url"]))
    except Exception as e:
        print("GitHub API unavailable (", type(e).__name__, ") - trying release page instead.")
    if not urls:
        r = subprocess.run(["curl", "-sIL", "-o", "/dev/null", "-w", "%{url_effective}",
                            f"https://github.com/{LLAMA_REPO}/releases/latest"],
                           capture_output=True, text=True)
        m = re.search(r"/tag/([^/\s]+)$", r.stdout.strip())
        if m:
            tag = m.group(1)
            print("Latest llama.cpp tag:", tag)
            for ext in ("zip", "tar.gz"):
                name = f"llama-{tag}-bin-ubuntu-x64.{ext}"
                urls.append((name, f"https://github.com/{LLAMA_REPO}/releases/download/{tag}/{name}"))
    return urls

def find_server_binary(root):
    for d, _dirs, files in os.walk(root):
        if "llama-server" in files:
            return os.path.join(d, "llama-server")
    return None

def lib_env(binary):
    env = dict(os.environ)
    d = os.path.dirname(binary)
    env["LD_LIBRARY_PATH"] = d + ":" + env.get("LD_LIBRARY_PATH", "")
    env["CUDA_VISIBLE_DEVICES"] = ""          # CPU only
    return env

def extract(archive, dest):
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            try:
                t.extractall(dest, filter="data")
            except TypeError:                 # Python < 3.12 has no extraction filters
                t.extractall(dest)

def install_llama_cpp_binary():
    """Returns the llama-server path, or None if no usable prebuilt binary could be set up."""
    existing = find_server_binary(BIN_DIR) if os.path.isdir(BIN_DIR) else None
    if existing:
        ok = subprocess.run([existing, "--version"], capture_output=True, env=lib_env(existing))
        if ok.returncode == 0:
            print("llama-server already present:", existing)
            return existing
    for name, url in candidate_urls():
        archive = f"{WORK}/{name}"
        print("Downloading", url)
        if not curl_get(url, archive, tries=3):
            print("  download failed, trying next candidate")
            continue
        try:
            extract(archive, BIN_DIR)
        except Exception as e:
            print("  could not extract:", e)
            continue
        binary = find_server_binary(BIN_DIR)
        if not binary:
            print("  llama-server not found inside the archive")
            continue
        os.chmod(binary, 0o755)
        for f in os.listdir(os.path.dirname(binary)):      # make bundled tools executable
            p = os.path.join(os.path.dirname(binary), f)
            if os.path.isfile(p) and not f.endswith((".so", ".txt", ".md")) and ".so" not in f:
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
        t = subprocess.run([binary, "--version"], capture_output=True, text=True,
                           env=lib_env(binary))
        print((t.stdout + t.stderr).strip().splitlines()[-1:] or "")
        if t.returncode == 0:
            return binary
        print("  binary does not run on this machine:", (t.stderr or t.stdout)[-300:])
    return None

SERVER_KIND = None      # "binary" or "python"
SERVER_BIN = None

def install_python_fallback():
    log("llama.cpp binary unavailable - falling back to the llama-cpp-python package")
    run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "llama-cpp-python[server]",
         "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu"])
    run([sys.executable, "-c", "import llama_cpp; print('llama_cpp', llama_cpp.__version__)"])

if not already_serving:
    log("1/9 llama.cpp CPU server")
    SERVER_BIN = install_llama_cpp_binary()
    if SERVER_BIN:
        SERVER_KIND = "binary"
        print("Using prebuilt llama-server:", SERVER_BIN)
    else:
        install_python_fallback()
        SERVER_KIND = "python"

# --------------------------- 2. model download ----------------------------
def model_ok():
    try:
        if os.path.getsize(MODEL_FILE) < MODEL_MIN_BYTES:
            return False
        with open(MODEL_FILE, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False

if not already_serving:
    log("2/9 Model download (Qwen2.5-3B-Instruct Q4_K_M, ~2.1 GB)")
    os.makedirs(MODEL_DIR, exist_ok=True)
    if model_ok():
        print("Model already present:", MODEL_FILE)
    else:
        if not curl_get(MODEL_URL, MODEL_FILE, tries=5) or not model_ok():
            raise RuntimeError("Model download failed or is incomplete. Check the internet "
                               "setting and re-run; the download resumes where it stopped.")
    print(f"Model OK: {os.path.getsize(MODEL_FILE) / 1e9:.2f} GB")

# ------------------------- 3. launch + test -------------------------------
def build_cmd():
    if SERVER_KIND == "binary":
        return [SERVER_BIN, "-m", MODEL_FILE, "--alias", SERVED_NAME,
                "--host", "127.0.0.1", "--port", str(PORT),
                "-c", str(CTX), "-t", str(THREADS), "--parallel", "1"]
    return [sys.executable, "-m", "llama_cpp.server", "--model", MODEL_FILE,
            "--model_alias", SERVED_NAME, "--host", "127.0.0.1", "--port", str(PORT),
            "--n_ctx", str(CTX), "--n_threads", str(THREADS)]

def launch_server():
    subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
    subprocess.run(["pkill", "-f", "llama_cpp.server"], capture_output=True)
    time.sleep(2)
    env = lib_env(SERVER_BIN) if SERVER_KIND == "binary" else dict(os.environ, CUDA_VISIBLE_DEVICES="")
    cmd = build_cmd()
    print("$ " + " ".join(cmd))
    with open(LOG, "w") as lf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=lf,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    print(f"Model server started in the background (PID {proc.pid}). Log: {LOG}")
    return proc

log("3/9 Starting the server and testing it")
if not already_serving:
    proc = launch_server()
    deadline = time.time() + 10 * 60
    while time.time() < deadline:
        if server_up():
            break
        if proc.poll() is not None:
            print(tail(LOG, 80))
            raise RuntimeError("The model server exited during startup. Last log lines are "
                               "above - send me the first error in them.")
        time.sleep(3)
    else:
        print(tail(LOG, 80))
        raise RuntimeError("The model server did not become ready within 10 minutes.")

print("API READY:", [m["id"] for m in api_get("/v1/models")["data"]])

def chat(prompt, max_tokens=200, temperature=0.2):
    """Ask the local model something: chat('your question')"""
    res = api_get("/v1/chat/completions", timeout=300, data={
        "model": SERVED_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": temperature})
    return res["choices"][0]["message"]["content"]

t0 = time.time()
print("\nTEST REPLY:\n" + chat("Explain Kubernetes in three simple sentences.", 100))
print(f"(took {time.time() - t0:.1f}s on CPU)")

# The auth proxy below is a small aiohttp app; run it with this notebook's Python.
try:
    import aiohttp  # noqa: F401
except ImportError:
    run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "aiohttp"])
PY = sys.executable

# ------------- 10. API-key proxy + Cloudflare tunnel + Worker gateway -----
# Your apps -> Cloudflare Worker (fixed URL) -> quick tunnel (URL changes every
# session) -> auth proxy (port 8002, needs Bearer key) -> llama.cpp server (8001, localhost).
# Each session the script registers the new tunnel URL with the Worker, so the
# Worker URL and API key never change.
#
# SECRETS: these must match the secrets stored in your Cloudflare Worker.
#   API_KEY        = Worker secret API_KEY   (what your apps send as the Bearer key)
#   REGISTER_TOKEN = Worker secret REGISTER_TOKEN (only used to register the tunnel)
# (Hardcoded on purpose for now.)
# If you rotate them in Cloudflare, change them here too.
WORKER_URL = "https://ai-builder.keyaurbhlonde.workers.dev"
API_KEY = "srkxOGWqn454X4GtmzxMtdOJHpLe8t_KV6KFvmXnLvE"
REGISTER_TOKEN = "KqQYN-RpPqUAkowK1zvUcGsbDzwKqN1HnA-X9wT6DCk"

PROXY_PORT = 8002
PROXY_BASE = f"http://127.0.0.1:{PROXY_PORT}"
KEY_FILE = f"{WORK}/.api_key"
PROXY_PY = f"{WORK}/auth_proxy.py"
PROXY_LOG = f"{WORK}/auth_proxy.log"
CF_BIN = f"{WORK}/cloudflared"
CF_LOG = f"{WORK}/cloudflared.log"
CF_DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               "cloudflared-linux-amd64")

AUTH_PROXY_CODE = '''
import os, hmac, json
import aiohttp
from aiohttp import web

UPSTREAM = os.environ["UPSTREAM"]
PORT = int(os.environ["PROXY_PORT"])
with open(os.environ["KEY_FILE"]) as f:
    KEY = f.read().strip()

DROP_REQ = {"host", "authorization", "content-length", "connection", "accept-encoding",
            "transfer-encoding"}
KEEP_RESP = {"content-type", "cache-control"}

def flatten_content(content):
    """Chat apps often send content as [{"type":"text","text":"hi"}]. llama-cpp-python's
    Qwen template needs a plain string, so join the text parts."""
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type", "text") == "text":
                parts.append(str(p.get("text", "")))
        return "\\n".join(parts)
    return content

def normalize_chat_body(body):
    try:
        data = json.loads(body)
    except Exception:
        return body
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return body
    for m in data["messages"]:
        if isinstance(m, dict):
            m["content"] = flatten_content(m.get("content"))
    return json.dumps(data).encode()

async def handle(request):
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not (token and hmac.compare_digest(token, KEY)):
        return web.json_response(
            {"error": {"message": "Unauthorized: missing or invalid API key",
                       "type": "AuthenticationError", "code": 401}},
            status=401, headers={"WWW-Authenticate": "Bearer"})
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ}
    body = await request.read()
    if request.method == "POST" and request.path.rstrip("/").endswith("/chat/completions"):
        body = normalize_chat_body(body)
    timeout = aiohttp.ClientTimeout(total=900, sock_connect=10)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
        try:
            async with session.request(request.method, UPSTREAM + str(request.rel_url),
                                       headers=headers, data=body) as up:
                resp = web.StreamResponse(status=up.status)
                for k, v in up.headers.items():
                    if k.lower() in KEEP_RESP:
                        resp.headers[k] = v
                resp.enable_chunked_encoding()
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except aiohttp.ClientError as e:
            return web.json_response(
                {"error": {"message": f"Upstream model server unreachable: {e}",
                           "type": "BadGateway", "code": 502}}, status=502)

app = web.Application(client_max_size=64 * 1024 * 1024)
app.router.add_route("*", "/{tail:.*}", handle)
web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)
'''

def http_status(url, headers=None, data=None, timeout=15):
    """Return (status, body_text) without raising on 4xx/5xx."""
    # Explicit User-Agent: Cloudflare can reject urllib's default one (error 1010).
    hdrs = {"User-Agent": "kaggle-cpu-oneshot/1.0"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, headers=hdrs,
        data=json.dumps(data).encode() if data is not None else None)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

def proxy_alive():
    try:
        return http_status(f"{PROXY_BASE}/v1/models", timeout=3)[0] == 401
    except Exception:
        return False

def read_tunnel_url():
    """URL of the most recently created quick tunnel in the cloudflared log."""
    try:
        with open(CF_LOG, errors="ignore") as f:
            logs = f.read()
    except Exception:
        return None
    blocks = logs.split("Your quick Tunnel has been created!")
    if len(blocks) > 1:
        m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", blocks[-1])
        if m:
            return m.group(0)
    return None

def tunnel_running():
    return subprocess.run(["pgrep", "-f", f"{CF_BIN} tunnel"],
                          capture_output=True).returncode == 0

log("10/10 Auth proxy + Cloudflare tunnel + Worker registration")

# --- API key file read by the auth proxy (fixed key = the Worker's API_KEY secret) ---
_old_key = open(KEY_FILE).read().strip() if os.path.exists(KEY_FILE) else None
fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(API_KEY)
if _old_key is not None and _old_key != API_KEY:
    # the running proxy loaded the old key at startup; restart it with the new one
    subprocess.run(["pkill", "-f", "auth_proxy.py"], capture_output=True)
    time.sleep(2)

# --- auth proxy ---
_old_code = open(PROXY_PY).read() if os.path.exists(PROXY_PY) else None
if _old_code is not None and _old_code != AUTH_PROXY_CODE:
    # an older proxy is running with outdated code: stop it so the new code is used
    subprocess.run(["pkill", "-f", PROXY_PY], capture_output=True)
    time.sleep(2)
if not proxy_alive():
    subprocess.run(["pkill", "-f", PROXY_PY], capture_output=True)
    with open(PROXY_PY, "w") as f:
        f.write(AUTH_PROXY_CODE)
    penv = dict(os.environ, KEY_FILE=KEY_FILE,
                UPSTREAM=BASE, PROXY_PORT=str(PROXY_PORT))
    with open(PROXY_LOG, "w") as lf:
        subprocess.Popen([PY, PROXY_PY], stdin=subprocess.DEVNULL, stdout=lf,
                         stderr=subprocess.STDOUT, env=penv, start_new_session=True)
    for _ in range(30):
        if proxy_alive():
            break
        time.sleep(1)
    else:
        print(tail(PROXY_LOG, 40))
        raise RuntimeError("Auth proxy did not start (log above).")
print(f"Auth proxy OK on {PROXY_BASE} (no key -> 401).")

# --- cloudflared ---
if not os.path.exists(CF_BIN):
    print("Downloading cloudflared ...")
    for attempt in range(1, 4):
        subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "3",
                        "-o", CF_BIN, CF_DOWNLOAD])
        if os.path.exists(CF_BIN) and os.path.getsize(CF_BIN) > 10_000_000:
            break
    else:
        raise RuntimeError("Could not download cloudflared from GitHub.")
    os.chmod(CF_BIN, 0o755)

PUBLIC_URL = read_tunnel_url() if tunnel_running() else None
if not PUBLIC_URL:
    subprocess.run(["pkill", "-f", f"{CF_BIN} tunnel"], capture_output=True)
    time.sleep(2)
    with open(CF_LOG, "w") as lf:
        subprocess.Popen([CF_BIN, "tunnel", "--no-autoupdate", "--url", PROXY_BASE],
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                         start_new_session=True)
    for _ in range(60):
        PUBLIC_URL = read_tunnel_url()
        if PUBLIC_URL:
            break
        time.sleep(2)
    else:
        print(tail(CF_LOG, 40))
        raise RuntimeError("cloudflared did not report a tunnel URL (log above).")
print("Tunnel URL:", PUBLIC_URL)

# --- register the tunnel URL with the Cloudflare Worker ---
# (the Worker itself reaches the tunnel from Cloudflare's side, so Kaggle's slow DNS
#  for new trycloudflare.com names does not matter here)
registered = False
for attempt in range(1, 7):
    try:
        st, body = http_status(f"{WORKER_URL}/_register",
                               headers={"Authorization": f"Bearer {REGISTER_TOKEN}"},
                               data={"url": PUBLIC_URL}, timeout=30)
        print(f"Worker registration (attempt {attempt}): HTTP {st} {body[:200]}")
        if 200 <= st < 300:
            registered = True
            break
        if st in (401, 403):
            raise RuntimeError(
                "Worker rejected REGISTER_TOKEN (HTTP %d). It must match the Worker's "
                "REGISTER_TOKEN secret. Update it in the SECRETS block and re-run." % st)
    except RuntimeError:
        raise
    except Exception as e:
        print(f"Worker registration (attempt {attempt}): {type(e).__name__}: {e}")
    time.sleep(5)
if not registered:
    raise RuntimeError("Could not register the tunnel with the Worker (see messages above).")

# --- end-to-end check through the Worker (Mac/apps -> Worker -> tunnel -> proxy -> model server) ---
verified = False
for attempt in range(1, 25):   # up to ~2 minutes: the new tunnel needs a moment to resolve
    try:
        st, body = http_status(f"{WORKER_URL}/v1/chat/completions",
                               headers={"Authorization": f"Bearer {API_KEY}"},
                               data={"model": SERVED_NAME,
                                     "messages": [{"role": "user",
                                                   "content": "Reply with exactly: CPU SERVER WORKS"}],
                                     "max_tokens": 20, "stream": False}, timeout=60)
        if st == 200:
            verified = True
            print("Gateway check OK (HTTP 200). Model said:",
                  json.loads(body)["choices"][0]["message"]["content"])
            break
        if st in (401, 403):
            raise RuntimeError(
                "Worker rejected API_KEY (HTTP %d). It must match the Worker's API_KEY "
                "secret. Update it in the SECRETS block and re-run." % st)
        print(f"Attempt {attempt}: gateway HTTP {st} {body[:150]} (tunnel still warming up?)")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"Attempt {attempt}: {type(e).__name__}: {e}")
    time.sleep(5)
if not verified:
    print("NOTE: the gateway did not return 200 yet. The tunnel may still be warming up - "
          "re-run just this cell in a minute, or test from your Mac with the curl below.")

print(f"""
==========================================================================
DONE. Everything is running in the background.

Local (inside Kaggle) : {BASE}/v1          (no key)
Gateway (fixed URL)   : {WORKER_URL}/v1
Current tunnel        : {PUBLIC_URL}   (changes every session; registered with the Worker)
Model name            : {SERVED_NAME}

In any OpenAI-compatible app use exactly:
  Base URL: {WORKER_URL}/v1     API key: your Worker API_KEY     Model: {SERVED_NAME}

Test from your Mac:
  export API_KEY_ENV='<your API_KEY>'
  curl -sS {WORKER_URL}/v1/chat/completions \\
    -H "Authorization: Bearer $API_KEY_ENV" -H "Content-Type: application/json" \\
    -d '{{"model": "{SERVED_NAME}", "messages": [{{"role": "user", "content": "Say hello"}}], "max_tokens": 30}}'

Notes
  - Only the tunnel URL changes between sessions; the script re-registers it automatically.
  - Streaming works through the local proxy; whether it works end to end depends on your Worker.
  - Logs: {LOG}  {PROXY_LOG}  {CF_LOG}
  - Stop everything: !pkill -f llama-server; pkill -f llama_cpp.server; pkill -f auth_proxy.py; pkill -f cloudflared
==========================================================================
""")

# ------------------- final printout: token + URLs -------------------------
# Re-reads the key file and the cloudflared log (nothing is modified), so the
# very last lines of the cell output always show the current values.
from pathlib import Path

_key_path = Path(KEY_FILE)
print("AUTH TOKEN:", _key_path.read_text().strip() if _key_path.exists()
      else "(auth token file not found!)")

print("GATEWAY URL:", WORKER_URL)
print("API BASE URL:", WORKER_URL + "/v1")
print("CHAT COMPLETIONS:", WORKER_URL + "/v1/chat/completions")
print("MODEL NAME:", SERVED_NAME)
print("CURRENT TUNNEL (registered with the Worker):", read_tunnel_url() or "not found in logs")