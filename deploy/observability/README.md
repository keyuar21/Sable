# Logs: Grafana, Loki and Promtail

Application logs from every service, queryable by **cluster**, **namespace**,
**app** and **pod**.

```
                        node /var/log/pods/**
                                │
                    ┌───────────▼───────────┐
                    │  promtail (DaemonSet) │   labels: cluster, namespace,
                    └───────────┬───────────┘           app, pod, container
                                │ push
                    ┌───────────▼───────────┐
                    │  loki (single binary) │   store + query
                    └───────────┬───────────┘
                                │ LogQL
                    ┌───────────▼───────────┐
                    │  grafana              │   https://upi-sim.local/grafana
                    └───────────────────────┘
```

## Install

```bash
deploy/observability/install.sh
```

Run it after the app (`deploy/helm/kind-up.sh`) — Grafana reuses the app's
Ingress host and TLS secret.

Then open <https://upi-sim.local/grafana> and log in as **admin / upi-admin**.

## Querying

The dashboard **UPI → UPI — Service Logs** has `cluster`, `namespace`, `app` and
`pod` dropdowns at the top; pick any combination and the panels follow. In
Explore, the same filters are just LogQL:

```logql
# everything in one service
{cluster="upi-helm-test", namespace="upi", app="upi-psp"}

# any service, any namespace, in this cluster
{cluster="upi-helm-test", namespace=~".+", app=~".+"}

# one pod
{app="upi-auth", pod="upi-auth-594cdcd844-8trgd"}

# errors only
{app=~"upi-.+"} |~ "(?i)error|exception|traceback"

# log rate per service
sum by (app) (count_over_time({namespace="upi", app=~"upi-.+"}[1m]))
```

### Labels

| label | example | source |
|---|---|---|
| `cluster` | `upi-helm-test` | static, set in `values/promtail.yaml` |
| `namespace` | `upi` | the pod |
| `app` | `upi-psp` | the pod's `app.kubernetes.io/instance` label |
| `pod` | `upi-psp-ff57fc6c4-8nwx7` | the pod |
| `container` | `psp` | the container |
| `job` | `upi/upi-psp` | `namespace/app` |

The chart derives `app` from `app.kubernetes.io/name`, which is the generic
`upi-service` on **every** pod here — so `values/promtail.yaml` overrides it with
`app.kubernetes.io/instance` to get the real per-service name. That is the one
relabel to keep in mind if service names ever change.

## Files

| path | what |
|---|---|
| `values/loki.yaml` | Loki, single-binary, filesystem, 7-day retention |
| `values/promtail.yaml` | the collector and its `cluster`/`app` relabels |
| `values/grafana.yaml` | Grafana, Loki datasource, Ingress on `/grafana` |
| `dashboards/upi-logs.json` | the provisioned dashboard |
| `install.sh` | installs all three and wires the ConfigMap + TLS secret |

## Notes

- **Retention** is 7 days (`values/loki.yaml` → `loki.limits_config.retention_period`).
  Loki's PVC is 5Gi; on a kind cluster that lives inside the Docker VM.
- **Logs are labelled, not indexed by content** — queries scan by label first, so
  always lead with `{...}` selectors.
- Grafana's admin password is the literal `upi-admin` in `values/grafana.yaml`.
  Fine for a local sim; use a secret (`admin.existingSecret`) anywhere real.
