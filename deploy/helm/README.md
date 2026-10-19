# Helm deployment

One chart, `upi-service`, installed once per microservice. The chart is
identical for every service — only the values file differs.

```
deploy/helm/
├── upi-service/            the shared chart
│   ├── Chart.yaml
│   ├── values.yaml         defaults + documentation for every key
│   └── templates/          deployment, service, configmap, pvc, hpa, pdb, ingress
├── values/
│   ├── psp.yaml            ─┐
│   ├── switch.yaml          │  one per service; this is the only thing
│   ├── payer-bank.yaml      │  that differs between releases
│   ├── payee-bank.yaml      │
│   ├── auth.yaml            │
│   ├── ops-console.yaml     │
│   └── frontend.yaml       ─┘
└── Makefile
```

## The per-service config file

Each service gets its own runtime config file, named after the service and
mounted into the container:

| Service | ConfigMap key | Mounted at | Service DNS |
|---|---|---|---|
| psp | `psp.yaml` | `/etc/upi/psp.yaml` | `upi-psp:5001` |
| switch | `switch.yaml` | `/etc/upi/switch.yaml` | `upi-switch:5002` |
| payer-bank | `payer-bank.yaml` | `/etc/upi/payer-bank.yaml` | `upi-payer-bank:5003` |
| payee-bank | `payee-bank.yaml` | `/etc/upi/payee-bank.yaml` | `upi-payee-bank:5004` |
| auth | `auth.yaml` | `/etc/upi/auth.yaml` | `upi-auth:5005` |
| ops-console | `ops-console.yaml` | `/etc/upi/ops-console.yaml` | `upi-ops-console:5010` |
| frontend | `frontend.yaml` | `/etc/upi/frontend.yaml` | `upi-frontend:8080` |

Whatever you put under `app.config` in a values file is copied verbatim into
that file. The chart does not care about its shape, so a service can change its
own config without touching the chart.

The container finds it through `$APP_CONFIG`, which the chart sets to the full
path. Nothing is hardcoded.

### Changing config at runtime

Edit the values file and upgrade:

```bash
helm upgrade --install upi-psp upi-service -f values/psp.yaml -n upi
```

The Deployment carries `checksum/config`, a hash of the rendered ConfigMap, so
a config change rolls the pods automatically and they come up reading the new
file. Without that annotation the ConfigMap would change but the running pods
would never notice.

Read back what is actually deployed:

```bash
kubectl -n upi get cm upi-psp-config -o jsonpath='{.data.psp\.yaml}'
```

## Images

Each service owns its Dockerfile, next to its code:

| Service | Dockerfile | Image | Weight |
|---|---|---|---|
| switch | `services/mock_switch/Dockerfile` | `upi-switch` | FastAPI only |
| payer-bank | `services/mock_payer_bank/Dockerfile` | `upi-payer-bank` | FastAPI + stdlib sqlite3 |
| payee-bank | `services/mock_payee_bank/Dockerfile` | `upi-payee-bank` | FastAPI + stdlib sqlite3 |
| ops-console | `services/ops_console/Dockerfile` | `upi-ops-console` | lightest — no HTTP client at all |
| psp | `services/mock_psp/Dockerfile` | `upi-psp` | adds piper-tts and the voice model |
| auth | `services/mock_auth/Dockerfile` | `upi-auth` | adds torch, librosa, onnxruntime |
| frontend | `frontend/Dockerfile` | `upi-frontend` | nginx-unprivileged |

Each has its own `requirements.txt` listing only what that service imports. The
switch does not carry torch; the ops console does not even carry httpx.

```bash
make images                 # all seven, tagged :1.0.0
make images TAG=1.1.0
make images-push REGISTRY=your.registry/ns
```

**Build context.** The Python services import shared modules (`ledger.py`,
`supabase_db.py`) from `services/`, so they build from the repository root:

```bash
docker build -f services/mock_switch/Dockerfile -t upi-switch:1.0.0 .
```

The frontend has no such dependency and builds from its own directory:

```bash
docker build -f frontend/Dockerfile -t upi-frontend:1.0.0 frontend
```

Each image copies only its own service directory plus the shared modules it
actually imports — nothing else from the repo.

Python images run as uid 10001, the frontend as uid 101, matching the
`podSecurityContext` in each values file. `ENTRYPOINT ["python"]` with a default
`CMD`, so `docker run upi-switch:1.0.0` works standalone and the chart's
`-m uvicorn … --port …` args still override it.

Models are **baked in** — piper's voice into `upi-psp`, `aasist.onnx` into
`upi-auth`. A pod that downloads a 60MB model on every restart fails the first
time the network does.

### How the frontend gets its URLs

The browser cannot read a mounted YAML file, so the frontend's config ships as
JavaScript. `app.extraFiles` in `values/frontend.yaml` puts a `config.js` in the
same ConfigMap, and an `extraVolumeMount` with `subPath` drops it into the web
root over the image's default copy:

```
/usr/share/nginx/html/config.js   ->  ConfigMap key "config.js"
```

`app.js` reads `window.__UPI_CONFIG__` and falls back per key to localhost, so
local development works with no container and one image serves every
environment.

## Install

```bash
make install            # all seven, in dependency order
make lint template      # check without a cluster
```

Order matters on first install: `switch` and `auth` come up before the banks,
and `psp` after them, so readiness probes pass on the first try.

## Ingress and the local kind cluster

Each release can publish itself through the chart's `templates/ingress.yaml`.
Everything is served from the **same host** — including the APIs, under
`/api/<svc>` — and the prefix is stripped before the request reaches the pod
(`rewrite-target`: `/api/psp/initiate -> /initiate`):

| Host | Path | Backend |
|---|---|---|
| `upi-sim.local` | `/` | `upi-frontend:8080` |
| `upi-sim.local` | `/api/psp` | `upi-psp:5001` |
| | `/api/switch` | `upi-switch:5002` |
| | `/api/payer` | `upi-payer-bank:5003` |
| | `/api/payee` | `upi-payee-bank:5004` |
| | `/api/auth` | `upi-auth:5005` |
| | `/api/recharge` | `upi-recharge:5007` |

One host, not two, is deliberate. A separate API host needs its own certificate
exception, and `fetch()` fails silently when the certificate is untrusted — the
browser reports "Failed to fetch" while **nothing** reaches the Ingress, so the
logs look empty. Same-origin also removes CORS entirely.

`frontend/config.js` holds the matching relative paths (the `extraFiles` block
in `values/frontend.yaml`); the ops console deliberately has no ingress.

The services are served over **https** with a self-signed cert (`upi-sim-tls`).
That is not decoration: Voice ID records through `getUserMedia`, and browsers
only expose the microphone on a secure context — `http://upi-sim.local` is not
one. Your browser will warn once, for `upi-sim.local`, on first visit.

`deploy/helm/kind-up.sh` brings the whole stack up on a local kind cluster —
cluster (with 80/443 published and the node labelled `ingress-ready=true`, see
`kind-config.yaml`), image builds, `kind load`, ingress-nginx, the secrets and
the TLS cert, and all eight releases:

```bash
deploy/helm/kind-up.sh
```

It prints the one host entry you must add yourself:

```bash
echo '127.0.0.1 upi-sim.local' | sudo tee -a /etc/hosts
```

Then open <https://upi-sim.local>.

## Logs

Application logs from every service are collected into Loki and queried in
Grafana by **cluster**, **namespace**, **app** and **pod**:

```bash
deploy/observability/install.sh
```

Then open <https://upi-sim.local/grafana> (**admin / upi-admin**) and pick the
**UPI — Service Logs** dashboard. See `deploy/observability/README.md`.

### Reading a sign-in failure out of the logs

The Ingress is the sharpest tool here: if a request never appears in the
`ingress-nginx` logs, the browser never sent it, and no amount of application
logging will show the cause. Several failures stood between the UI and a working
sign-in, and each one looks like the others from the browser:

| Symptom | Real cause | Fix |
|---|---|---|
| "Failed to fetch", and *nothing* in the Ingress logs | the JS still called a second host (`api.upi-sim.local`); `fetch()` fails silently when that host's certificate is untrusted | serve everything from `upi-sim.local` — one host, one exception — and let `config.js` supply the URLs |
| "No account for alice@…" although the account exists | the auth service looked the account up at a hardcoded `http://localhost:5003`, which inside the pod is the auth container itself | `PAYER_BANK_URL` (see `values/auth.yaml`) |
| "No PIN set for this account yet" right after setting one | the PIN hash was written only to Supabase, which is unconfigured, so `/pin/set` failed and the bank silently swallowed the error | keep the hash in the auth service's own SQLite store (`/data/voice_biometrics.db`) |
| "No account for 9090909090@hdfcbank" although the row is plainly in Supabase | the `upi-supabase` secret was empty *and* the auth deployment had no `envFrom`, so no service ever saw the project URL | populate the secret (below) and reference it from `values/auth.yaml` |
| "No PIN set for this account yet" for a row that *does* have a PIN | the seeded table stores a plaintext `pin` column; the code only looked for `pin_hash` | `verify_pin` accepts the legacy column, then caches a bcrypt hash locally so the plaintext is read once |
| a Supabase-only account signs in but every payment is refused with "no ledger account" | the journal can only post balanced entries for accounts in its chart of accounts; a Supabase row was never opened locally | the payer bank imports the row with an opening-balance entry on first debit |
| `GET /balance/{vpa}` returns the account's PIN | the Supabase fall-through returned the row whole, and the row carries `pin`, `pin_hash` and the lockout counters — on an endpoint with no authentication | project only `vpa`, `holder_name`, `balance` and the empty ledger fields |
| a rebuilt image never reaches the pods even though the load was run | the load failed and its output was discarded (`>/dev/null 2>&1`), so nothing reported it — the tag still resolves, the rollout still goes green, and the pods keep running the old code | `make load` now names each image and aborts on failure; prove it with `kubectl -n upi exec deploy/upi-auth -- grep -c <symbol> /app/services/mock_auth/auth_service.py` |
| everything is gone the next morning: `docker info` hangs and the socket answers nothing | the Mac slept and Docker Desktop's Linux VM was killed; the backend process stays up, so `ps -o etime` looks normal, but its VM (`com.docker.virtualization`/`qemu`) is gone and the engine never returns on its own | quit and reopen Docker Desktop, then `docker start upi-helm-test-control-plane` — it has no restart policy — and re-check `kubectl get pods -A` |
| `docker build` fails with `lookup registry-1.docker.io: no such host` just after Docker Desktop restarts | the VM's DNS proxy has not come up yet, so BuildKit cannot resolve the `# syntax=docker/dockerfile:1.7` frontend image before it even reads the Dockerfile | retry in a minute; if it persists restart Docker Desktop again, or build with `DOCKER_BUILDKIT=0` — the legacy builder needs no frontend image and reuses the cached `pip install` layer |

**Prove the code, not the tag.** `imagePullPolicy: IfNotPresent` means a pod can
be Running, Ready and green in `helm status` while running an image from an hour
ago. Before concluding that a change did not work, check that a symbol from the
change is actually in the container:

```sh
kubectl -n upi exec deploy/upi-auth -- grep -c legacy_pin /app/services/mock_auth/auth_service.py
```

To follow one VPA through every service:

```logql
{namespace="upi"} |~ "loluser@hdfcbank"
```

## Pointing the stack at your Supabase project

The services fall back to their own SQLite data unless `SUPABASE_URL` and
`SUPABASE_SECRET_KEY` are present in their environment. Those come from one
pre-existing secret, `upi-supabase`, injected with `envFrom`, so every key in it
becomes an environment variable. Create it from the values in the repo's `.env`:

```sh
set -a; . ./.env; set +a
kubectl -n upi create secret generic upi-supabase \
  --from-literal=SUPABASE_URL="$SUPABASE_URL" \
  --from-literal=SUPABASE_SECRET_KEY="$SUPABASE_SECRET_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -
```

`SUPABASE_URL` / `SUPABASE_SECRET_KEY` are read when each service starts, so a
changed secret only takes effect after a restart:

```sh
for s in auth psp payer-bank payee-bank switch; do
  kubectl -n upi rollout restart deploy/upi-$s
done
```

`values/auth.yaml` sets `existingSecret: upi-supabase`; the other four reference
it already. If you add a service that talks to Supabase, give it the same key —
a deployment with no `envFrom` gets no credentials at all, which is exactly how
the "No account for …" failure above happened.

Two consequences of using the project as the source of accounts:

- **PINs.** Rows that predate `pin_hash` carry a plaintext `pin` column. Those
  are accepted on first verification and a bcrypt hash is cached in the auth
  service's SQLite store, so the plaintext is only ever read once.
- **Ledger.** The Supabase balance is copied into the payer bank's journal as an
  opening balance the first time that account pays. From then on the local
  ledger is authoritative for that VPA and the two balances can drift; re-import
  means deleting the local account, not editing Supabase.

## Still to do before this runs for real

Written to be kept, not yet deployed. Two things still need code changes:

1. **The Python services do not read `$APP_CONFIG` yet.** They use module-level
   constants and environment variables today. The config files here describe the
   shape to move to; wiring it up is a change in each service. The frontend is
   already done — it reads `config.js` at runtime.

2. **The ops console reads the banks' SQLite files directly.** That works on one
   host but not across pods — a pod cannot open another pod's volume. Its values
   file has `mode: http` for this reason, since each bank already exposes
   `/ledger/trial-balance`, but the console does not use that path yet.

3. **The Supabase mirror is exercised but one-directional.** PIN hashes, WebAuthn
   public keys and voiceprints are authoritative in the auth service's own SQLite
   file (`/data/voice_biometrics.db`), which is what lets a standalone stack sign
   in and enrol Face ID without Supabase at all. When `SUPABASE_URL` /
   `SUPABASE_SECRET_KEY` are set the writes are mirrored there as well. Reads are
   local-first, so a row edited directly in Supabase is only seen on a cold
   service start — restart rather than edit while testing.

4. **PIN lockout is counted in the mirrored table.** The failed-attempt counter and
   lockout timestamp go through `_mirror_account_update`, so they persist only
   when Supabase is configured. Without it a wrong PIN is simply rejected and the
   counter resets each request. Fine for a demo; wire the counter into the local
   store if it matters.

The images are built and the whole stack runs on a local kind cluster
(`deploy/helm/kind-up.sh`). `helm lint` and `helm template` still cover the
charts on their own.

Also worth knowing: the bank services and auth use `ReadWriteOnce` volumes and
are pinned to `replicaCount: 1`. SQLite admits one writer, and the ledger file
is the source of truth for every balance — do not scale them.
