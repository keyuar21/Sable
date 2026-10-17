# ==========================================================================
# ONE-SHOT: vLLM + Qwen2.5-3B (TP=2) on Kaggle 2x Tesla T4
#
# Paste this whole file into ONE cell of a FRESH Kaggle notebook and run it.
# Before running:  Settings -> Accelerator = "GPU T4 x2",  Internet = ON.
#
# What it does (nothing from earlier attempts is needed):
#   1. Builds a clean Python 3.12 virtualenv (Kaggle's system Python is 3.13,
#      but the validated vLLM wheel is cp312-only).
#   2. Downloads the validated vLLM wheel from Hugging Face and verifies SHA256.
#   3. Installs PyTorch 2.10.0 (cu128) + EVERY dependency vLLM needs in the venv,
#      so there is no overlay / PYTHONPATH juggling and no more
#      "No module named uvloop / psutil / distro / jmespath" one-at-a-time errors.
#   4. Self-heals: if an import still fails with ModuleNotFoundError, it pip
#      installs that package and retries.
#   5. Downloads the sharded Qwen2.5-3B model.
#   6. Starts the OpenAI-compatible server in the BACKGROUND (the cell finishes,
#      the server keeps running) and sends a test chat request.
#   7. Starts an authenticating proxy (port 8002, fixed API_KEY) and a Cloudflare
#      quick tunnel, registers the new tunnel URL with your Cloudflare Worker,
#      tests the Worker gateway end to end and prints the URL + key at the end.
#      Fill in / check the SECRETS block (WORKER_URL, API_KEY, REGISTER_TOKEN).
#
# First run takes roughly 10-20 minutes. Re-running the cell in the same
# session is safe: finished steps are skipped and a running server is reused.
# ==========================================================================
import os, sys, re, json, time, shutil, hashlib, subprocess, urllib.request, zipfile
from email.parser import Parser

# ---------------------------- configuration -------------------------------
WORK = "/tmp/vllm-kaggle"            # /tmp keeps big files out of /kaggle/working output
VENV = f"{WORK}/venv"
PY = f"{VENV}/bin/python"
WHEEL_DIR = f"{WORK}/wheels"
MODEL_DIR = f"{WORK}/model"
LOG = f"{WORK}/vllm_server.log"

PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
SERVED_NAME = "qwen2.5-3b-kaggle-t4x2"

# Validated runtime profile "kaggle-t4x2-cu128" (kaggle-vllm 0.1.2 / 0.2.0)
WHEEL_REPO = "waqasm86/kaggle-vllm-binaries"
WHEEL_REV = "f6b4f10de54924ed6fe9e28cceab84eca7276ab6"
WHEEL_FILE = "vllm-0.18.2.dev0+ga26e8dc7f.d20260822.cu128-cp312-cp312-linux_x86_64.whl"
WHEEL_SHA256 = "5a9bd710b8a19fdd23abb3442baad892da977466f996334decd533a225f5fd0c"
WHEEL_URL = (f"https://huggingface.co/{WHEEL_REPO}/resolve/{WHEEL_REV}/"
             + WHEEL_FILE.replace("+", "%2B"))

MODEL_REPO = "waqasm86/kaggle-vllm-models"
MODEL_REV = "0e64784d460ecb2d8f0a6ebe695e2efa01226678"

TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
TORCH_DEFAULTS = {"torch": "==2.10.0", "torchvision": "==0.25.0", "torchaudio": "==2.10.0"}

# Runtime dependencies from the validated profile (overlay-requirements.txt)
OVERLAY_REQUIREMENTS = """
regex
cachetools
psutil
sentencepiece
requests>=2.26.0
tqdm
blake3
py-cpuinfo
transformers<5,>=4.56.0
tokenizers>=0.21.1
protobuf!=6.30.*,!=6.31.*,!=6.32.*,!=6.33.0.*,!=6.33.1.*,!=6.33.2.*,!=6.33.3.*,!=6.33.4.*,>=5.29.6
fastapi[standard]>=0.115.0
aiohttp>=3.13.3
openai<2.25.0,>=1.99.1
pydantic>=2.12.0
prometheus_client>=0.18.0
pillow
prometheus-fastapi-instrumentator>=7.0.0
tiktoken>=0.6.0
lm-format-enforcer==0.11.3
llguidance<1.4.0,>=1.3.0
outlines_core==0.2.11
diskcache==5.6.3
lark==1.2.2
xgrammar<1.0.0,>=0.1.32
typing_extensions>=4.10
filelock>=3.16.1
partial-json-parser
pyzmq>=25.0.0
msgspec
gguf>=0.17.0
mistral_common[image]>=1.10.0
opencv-python-headless>=4.13.0
pyyaml
six>=1.16.0
einops
compressed-tensors==0.13.0
depyf==0.20.0
cloudpickle
watchfiles
python-json-logger
pybase64
cbor2
ijson
setproctitle
openai-harmony>=0.0.3
anthropic>=0.71.0
model-hosting-container-standards<1.0.0,>=0.1.13
mcp
opentelemetry-sdk>=1.27.0
opentelemetry-api>=1.27.0
opentelemetry-exporter-otlp>=1.27.0
opentelemetry-semantic-conventions-ai>=0.4.1
numba==0.61.2
"""

# Extra packages vLLM's server imports that were missing last time, plus safety nets
EXTRA_REQUIREMENTS = """
numpy
psutil
uvloop
distro
jmespath
httpx
uvicorn[standard]
huggingface_hub
packaging
"""

# Versions validated by the profile (used only as constraints, never forced)
OVERLAY_LOCK = """
anthropic==1.0.0
apache-tvm-ffi==0.1.13.post3
astor==0.8.1
cbor2==6.1.4
compressed-tensors==0.13.0
depyf==0.20.0
diskcache==5.6.3
gguf==0.19.0
huggingface_hub==0.36.2
ijson==3.5.1
interegular==0.3.3
lark==1.2.2
llguidance==1.3.0
llvmlite==0.44.0
lm-format-enforcer==0.11.3
mistral_common==1.11.7
model-hosting-container-standards==0.1.16
msgspec==0.21.1
numba==0.61.2
openai-harmony==0.0.8
openai==2.24.0
outlines_core==0.2.11
partial-json-parser==0.2.1.1.post7
prometheus-fastapi-instrumentator==8.1.0
protobuf==6.33.6
pybase64==1.5.0
setproctitle==1.3.7
transformers==4.57.6
xgrammar==0.2.3
"""

# import-name -> pip-name for the self-healing import check
MODULE_TO_PIP = {
    "google": "protobuf", "yaml": "pyyaml", "cv2": "opencv-python-headless",
    "PIL": "pillow", "zmq": "pyzmq", "sklearn": "scikit-learn", "attr": "attrs",
    "dotenv": "python-dotenv", "jose": "python-jose", "multipart": "python-multipart",
    "msgpack": "msgpack", "tvm_ffi": "apache-tvm-ffi", "opentelemetry": "opentelemetry-api",
}

# ------------------------------- helpers ----------------------------------
def log(msg):
    print(f"\n=== {msg}", flush=True)

def run(cmd, **kw):
    print("$ " + " ".join(str(c) for c in cmd)[:220], flush=True)
    subprocess.run(cmd, check=True, **kw)

def pip(*args):
    run([PY, "-m", "pip", "install", "--disable-pip-version-check",
         "--progress-bar", "off", "-q", *args])

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

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

# --------------------------- 0. preflight ---------------------------------
log("0/9 Preflight (GPUs + internet)")
os.makedirs(WORK, exist_ok=True)
try:
    gpus = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                          capture_output=True, text=True, check=True).stdout.strip().splitlines()
except Exception as e:
    raise RuntimeError("nvidia-smi failed. Set Settings -> Accelerator = 'GPU T4 x2' and restart.") from e
print("GPUs:", gpus)
if len(gpus) < 2:
    raise RuntimeError(f"Found {len(gpus)} GPU(s) but tensor-parallel-size 2 needs 2. "
                       "Choose Accelerator = 'GPU T4 x2'.")
try:
    urllib.request.urlopen("https://huggingface.co", timeout=15)
    urllib.request.urlopen("https://pypi.org", timeout=15)
except Exception as e:
    raise RuntimeError("No internet. Turn on Settings -> Internet and restart the session.") from e

already_serving = server_up()
if already_serving:
    print("A vLLM server is already answering on", BASE, "- skipping install and launch.")

# ------------------- 1-8. environment (skipped if healthy) ----------------
def env_is_healthy():
    if not os.path.exists(PY):
        return False
    r = subprocess.run([PY, "-c",
                        "import sys,torch,vllm; assert sys.version_info[:2]==(3,12); "
                        "assert torch.cuda.is_available()"], capture_output=True)
    return r.returncode == 0

def make_venv():
    # Kaggle's /usr/bin/python3.12 has no ensurepip, so plain `python3.12 -m venv`
    # fails ("apt install python3.12-venv"). `uv venv --seed` is what worked.
    shutil.rmtree(VENV, ignore_errors=True)
    try:
        run([sys.executable, "-m", "pip", "install", "-q", "uv"])
        run([sys.executable, "-m", "uv", "venv", "--clear", "--python", "3.12",
             "--seed", VENV], env=dict(os.environ, UV_LINK_MODE="copy"))
        if os.path.exists(PY):
            return
    except subprocess.CalledProcessError as e:
        print("uv venv failed:", e)
    print("Falling back to python3.12 -m venv")
    shutil.rmtree(VENV, ignore_errors=True)
    exe = shutil.which("python3.12") or "/usr/bin/python3.12"
    run([exe, "-m", "venv", VENV])

def get_wheel():
    os.makedirs(WHEEL_DIR, exist_ok=True)
    path = f"{WHEEL_DIR}/{WHEEL_FILE}"
    for attempt in range(1, 5):
        if os.path.exists(path) and sha256_of(path) == WHEEL_SHA256:
            print("Wheel OK, SHA256 verified.")
            return path
        print(f"Downloading vLLM wheel (attempt {attempt}) ...")
        subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "3",
                        "-o", path, WHEEL_URL])
    raise RuntimeError("Could not download a SHA256-valid vLLM wheel from Hugging Face.")

def wheel_requirements(path):
    try:
        from packaging.requirements import Requirement
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "-q", "packaging"])
        from packaging.requirements import Requirement
    with zipfile.ZipFile(path) as z:
        meta_name = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")][0]
        meta = Parser().parsestr(z.read(meta_name).decode())
    env = {"python_version": "3.12", "python_full_version": "3.12.3",
           "platform_machine": "x86_64", "sys_platform": "linux",
           "platform_system": "Linux", "os_name": "posix",
           "implementation_name": "cpython", "extra": ""}
    torch_pins, others = {}, []
    # torch family is installed separately from the CUDA index; flashinfer/xformers are
    # optional on T4 (vLLM uses TRITON_ATTN there); triton comes with torch.
    skip = {"flashinfer-python", "xformers", "triton"}
    for line in meta.get_all("Requires-Dist") or []:
        r = Requirement(line)
        if r.marker is not None and not r.marker.evaluate(env):
            continue
        n = r.name.lower().replace("_", "-")
        if n in TORCH_DEFAULTS:
            torch_pins[n] = str(r.specifier)
            continue
        if n in skip or r.url:
            continue
        extras = f"[{','.join(sorted(r.extras))}]" if r.extras else ""
        others.append(f"{r.name}{extras}{r.specifier}")
    return torch_pins, others

def build_environment():
    log("1/9 Python 3.12 virtualenv")
    make_venv()
    run([PY, "-m", "pip", "install", "--disable-pip-version-check", "-q",
         "--upgrade", "pip", "setuptools", "wheel"])
    print(subprocess.run([PY, "--version"], capture_output=True, text=True).stdout.strip())

    log("2/9 vLLM wheel (SHA256 verified)")
    wheel_path = get_wheel()

    log("3/9 Reading the wheel's own dependency list")
    torch_pins, wheel_reqs = wheel_requirements(wheel_path)
    pins = {}
    for name, default in TORCH_DEFAULTS.items():
        spec = torch_pins.get(name)
        pins[name] = spec if (spec and spec.startswith("==")) else default
    # only install torchvision/torchaudio if the wheel actually asks for them
    wanted = ["torch"] + [n for n in ("torchvision", "torchaudio") if n in torch_pins]
    torch_specs = [f"{n}{pins[n]}" for n in wanted]
    print("PyTorch family:", torch_specs)
    print(f"{len(wheel_reqs)} other requirements from the wheel metadata")

    log("4/9 PyTorch (CUDA 12.8)")
    pip("--index-url", TORCH_INDEX, "--extra-index-url", "https://pypi.org/simple", *torch_specs)

    log("5/9 All other dependencies (torch pinned so pip cannot replace it)")
    with open(f"{WORK}/req_overlay.txt", "w") as f:
        f.write(OVERLAY_REQUIREMENTS)
    with open(f"{WORK}/req_wheel.txt", "w") as f:
        f.write("\n".join(wheel_reqs) + "\n")
    with open(f"{WORK}/req_extra.txt", "w") as f:
        f.write(EXTRA_REQUIREMENTS)
    # transformers 4.57.x refuses tokenizers newer than 0.23.0 (0.23.3 broke the server
    # on the first attempt), so tokenizers is always held in that range.
    always = "tokenizers>=0.22.0,<=0.23.0\n"
    with open(f"{WORK}/constraints_torch.txt", "w") as f:
        f.write("\n".join(f"{n}{pins[n]}" for n in wanted) + "\n" + always)
    with open(f"{WORK}/constraints_full.txt", "w") as f:
        f.write("\n".join(f"{n}{pins[n]}" for n in wanted) + "\n" + always + OVERLAY_LOCK)
    reqs = ["-r", f"{WORK}/req_overlay.txt", "-r", f"{WORK}/req_wheel.txt",
            "-r", f"{WORK}/req_extra.txt"]
    try:
        pip(*reqs, "-c", f"{WORK}/constraints_full.txt")
    except subprocess.CalledProcessError:
        print("Validated version pins conflicted; retrying with only the torch pins.")
        pip(*reqs, "-c", f"{WORK}/constraints_torch.txt")

    log("6/9 Installing the vLLM wheel (no-deps, so torch is untouched)")
    pip("--no-deps", "--force-reinstall", wheel_path)

    out = subprocess.run([PY, "-c",
                          "import torch; print('torch', torch.__version__, "
                          "'cuda', torch.cuda.is_available(), 'gpus', torch.cuda.device_count())"],
                         capture_output=True, text=True)
    print(out.stdout.strip() or out.stderr[-500:])
    if "cuda True" not in out.stdout:
        raise RuntimeError("PyTorch cannot see CUDA in the venv. Output above shows why.")

def heal_imports(max_rounds=20):
    log("7/9 Import check with auto-install of any missing module")
    check = ("import vllm, vllm._C, tokenizers, transformers; "
             "from transformers import AutoConfig, AutoTokenizer; "
             "import vllm.entrypoints.openai.api_server; print('IMPORTS OK')")
    tried = set()
    for _ in range(max_rounds):
        env = dict(os.environ, PYTHONNOUSERSITE="1")
        env.pop("PYTHONPATH", None)
        r = subprocess.run([PY, "-c", check], capture_output=True, text=True, env=env)
        if "IMPORTS OK" in r.stdout:
            print("IMPORTS OK")
            return
        m = re.search(r"No module named '([\w\.\-]+)'", r.stderr)
        if not m:
            print(r.stderr[-3000:])
            raise RuntimeError("vLLM import failed for a reason other than a missing module "
                               "(traceback above).")
        mod = m.group(1).split(".")[0]
        name = MODULE_TO_PIP.get(mod, mod.replace("_", "-"))
        if name in tried:
            print(r.stderr[-3000:])
            raise RuntimeError(f"Installed '{name}' but import still fails.")
        tried.add(name)
        print(f"Missing module '{mod}' -> pip install {name}")
        pip(name, "-c", f"{WORK}/constraints_torch.txt")
    raise RuntimeError("Too many missing modules; see output above.")

if not already_serving:
    if env_is_healthy():
        log("1-6/9 Environment already built in this session - skipping reinstall")
    else:
        build_environment()
    heal_imports()

# --------------------------- 8. model download ----------------------------
def model_ready():
    if not os.path.isdir(MODEL_DIR):
        return False
    files = os.listdir(MODEL_DIR)
    return "config.json" in files and any(f.endswith(".safetensors") for f in files)

if not already_serving:
    log("8/9 Model download (Qwen2.5-3B, TP=2 sharded state)")
    if model_ready():
        print("Model already present:", MODEL_DIR)
    else:
        code = f"""
from huggingface_hub import snapshot_download
try:
    p = snapshot_download({MODEL_REPO!r}, revision={MODEL_REV!r}, local_dir={MODEL_DIR!r})
except Exception as e:
    print('Pinned revision failed (' + repr(e)[:200] + '); using latest.')
    p = snapshot_download({MODEL_REPO!r}, local_dir={MODEL_DIR!r})
print(p)
"""
        run([PY, "-c", code])
    names = sorted(os.listdir(MODEL_DIR))
    print("Model files:", [n for n in names if not n.startswith(".")][:20])
    if not any("rank" in n for n in names):
        print("WARNING: no rank-specific shard files found; the sharded_state load may fail.")

# ------------------------- 9. launch + test -------------------------------
def launch_server():
    subprocess.run(["pkill", "-f", "vllm.entrypoints"], capture_output=True)
    time.sleep(3)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = f"{VENV}/bin:" + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = ("/usr/local/nvidia/lib64:/usr/local/cuda/lib64:"
                              + env.get("LD_LIBRARY_PATH", ""))
    cmd = [PY, "-m", "vllm.entrypoints.openai.api_server",
           "--model", MODEL_DIR,
           "--served-model-name", SERVED_NAME,
           "--load-format", "sharded_state",
           "--tensor-parallel-size", "2",
           "--dtype", "half",
           "--enforce-eager",
           "--disable-custom-all-reduce",
           "--max-model-len", "2048",
           "--gpu-memory-utilization", "0.70",
           "--host", "127.0.0.1",
           "--port", str(PORT)]
    with open(LOG, "w") as lf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=lf,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    print(f"vLLM started in the background (PID {proc.pid}). Log: {LOG}")
    return proc

log("9/9 Starting the server and testing it")
if not already_serving:
    proc = launch_server()
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        if server_up():
            break
        if proc.poll() is not None:
            print(tail(LOG, 80))
            raise RuntimeError("vLLM exited during startup. Last log lines are above - "
                               "send me the first error in them.")
        time.sleep(5)
    else:
        print(tail(LOG, 80))
        raise RuntimeError("vLLM did not become ready within 20 minutes.")

print("API READY:", [m["id"] for m in api_get("/v1/models")["data"]])

def chat(prompt, max_tokens=200, temperature=0.2):
    """Ask the local model something: chat('your question')"""
    res = api_get("/v1/chat/completions", timeout=180, data={
        "model": SERVED_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": temperature})
    return res["choices"][0]["message"]["content"]

print("\nTEST REPLY:\n" + chat("Explain Kubernetes in three simple sentences.", 100))

# ------------- 10. API-key proxy + Cloudflare tunnel + Worker gateway -----
# Your apps -> Cloudflare Worker (fixed URL) -> quick tunnel (URL changes every
# session) -> auth proxy (port 8002, needs Bearer key) -> vLLM (8001, localhost).
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
KEY_FILE = f"{WORK}/.vllm_api_key"
PROXY_PY = f"{WORK}/auth_proxy.py"
PROXY_LOG = f"{WORK}/auth_proxy.log"
CF_BIN = f"{WORK}/cloudflared"
CF_LOG = f"{WORK}/cloudflared.log"
CF_DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               "cloudflared-linux-amd64")

AUTH_PROXY_CODE = '''
import os, hmac
import aiohttp
from aiohttp import web

UPSTREAM = os.environ["UPSTREAM"]
PORT = int(os.environ["PROXY_PORT"])
with open(os.environ["KEY_FILE"]) as f:
    KEY = f.read().strip()

DROP_REQ = {"host", "authorization", "content-length", "connection", "accept-encoding",
            "transfer-encoding"}
KEEP_RESP = {"content-type", "cache-control"}

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
                {"error": {"message": f"Upstream vLLM unreachable: {e}",
                           "type": "BadGateway", "code": 502}}, status=502)

app = web.Application(client_max_size=64 * 1024 * 1024)
app.router.add_route("*", "/{tail:.*}", handle)
web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)
'''

def http_status(url, headers=None, data=None, timeout=15):
    """Return (status, body_text) without raising on 4xx/5xx."""
    # Explicit User-Agent: Cloudflare can reject urllib's default one (error 1010).
    hdrs = {"User-Agent": "kaggle-vllm-oneshot/1.0"}
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
if not proxy_alive():
    subprocess.run(["pkill", "-f", PROXY_PY], capture_output=True)
    with open(PROXY_PY, "w") as f:
        f.write(AUTH_PROXY_CODE)
    penv = dict(os.environ, PYTHONNOUSERSITE="1", KEY_FILE=KEY_FILE,
                UPSTREAM=BASE, PROXY_PORT=str(PROXY_PORT))
    penv.pop("PYTHONPATH", None)
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

# --- end-to-end check through the Worker (Mac/apps -> Worker -> tunnel -> proxy -> vLLM) ---
verified = False
for attempt in range(1, 25):   # up to ~2 minutes: the new tunnel needs a moment to resolve
    try:
        st, body = http_status(f"{WORKER_URL}/v1/chat/completions",
                               headers={"Authorization": f"Bearer {API_KEY}"},
                               data={"model": SERVED_NAME,
                                     "messages": [{"role": "user",
                                                   "content": "Reply with exactly: VLLM WORKS"}],
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
  export VLLM_API_KEY='<your API_KEY>'
  curl -sS {WORKER_URL}/v1/chat/completions \\
    -H "Authorization: Bearer $VLLM_API_KEY" -H "Content-Type: application/json" \\
    -d '{{"model": "{SERVED_NAME}", "messages": [{{"role": "user", "content": "Say hello"}}], "max_tokens": 30}}'

Notes
  - Only the tunnel URL changes between sessions; the script re-registers it automatically.
  - Streaming works through the local proxy; whether it works end to end depends on your Worker.
  - Logs: {LOG}  {PROXY_LOG}  {CF_LOG}
  - Stop everything: !pkill -f vllm.entrypoints; pkill -f auth_proxy.py; pkill -f cloudflared
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