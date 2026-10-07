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

Nothing here has been built or deployed: no Docker daemon was available, so the
Dockerfiles are written and reviewed but unbuilt. The charts are verified with
`helm lint` and `helm template` for all seven services.

Also worth knowing: the bank services and auth use `ReadWriteOnce` volumes and
are pinned to `replicaCount: 1`. SQLite admits one writer, and the ledger file
is the source of truth for every balance — do not scale them.
