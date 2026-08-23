# Agentic Meetings

A multi-agent meeting simulator that runs each AI participant inside its own
**gVisor-isolated Kubernetes sandbox**, where what each agent is allowed to do
is enforced by Kubernetes — ServiceAccounts, RBAC, NetworkPolicy and per-template
Secrets — rather than by asking it nicely in a prompt.

Built on the [Kubernetes Agent Sandbox](https://agent-sandbox.sigs.k8s.io/)
project (SIG Apps), CloudNativePG, and Gateway API.

> **Status:** actively being rebuilt. Phases 0–2 are complete: every agent turn
> now executes inside its own gVisor-isolated Sandbox pod. The per-persona
> least-privilege model lands in Phase 3. See [Roadmap](#roadmap).

## The idea

A supervisor agent runs a meeting: it picks who speaks next, each participant
argues from their role using retrieval over company documents, and the meeting
produces a transcript and conclusions.

That much is an LLM demo. The interesting part is what happens underneath.

Each attendee's reasoning loop executes in a **separate sandbox pod** under a
gVisor kernel. Agents that need to analyse data write Python, and that code runs
in a **second sandbox tier with no network access at all**. Which tools a given
persona can reach is decided by the cluster, at five layers:

| Layer | Control | What it stops |
|---|---|---|
| Prompt | Only granted tools are registered | Honest mistakes |
| Runtime | Grants intersected with a mounted capability list | A compromised backend over-granting |
| Secret | Credentials mounted only into templates that need them | Nothing to steal |
| NetworkPolicy | Default-deny egress per profile | A stolen credential being *used* |
| RBAC | Only some ServiceAccounts may claim an exec sandbox | The agent itself — with a 403 from the apiserver |

The demonstrable moment: a General Counsel persona asked to run code gets a
**403 from the Kubernetes API server**, surfaced in the UI's audit matrix.
Least privilege you can screenshot, not least privilege you assert.

## Architecture

```
Browser ──HTTP/WS──▶ Gateway API (Envoy) ──▶ FastAPI backend
                                              │  LangGraph supervisor + router
                                              │  (graph state never leaves here)
                                              ▼
                              ┌───────────────────────────────┐
                              │ Tier A: persona sandboxes     │  gVisor
                              │ one per attendee, warm-pooled │  runtimeClass
                              │ runs the ReAct loop           │
                              └───────────────┬───────────────┘
                                              │ claims (RBAC-gated)
                                              ▼
                              ┌───────────────────────────────┐
                              │ Tier B: exec sandboxes        │  gVisor +
                              │ model-authored Python         │  deny-all
                              │ ephemeral, 60s deadline       │  network
                              └───────────────────────────────┘

CloudNativePG (Postgres 18 + pgvector via ImageVolume) ── retrieval, artifacts, state
```

Two design rules make this coherent:

1. **The LangGraph graph never leaves the backend.** Sandboxes are turn
   executors, not graph participants. Distributed checkpointing across sandboxes
   is a research project, not a demo.
2. **Sandboxes never hold the application database credential.** Everything they
   need goes through a scoped internal API; the one exception is a read-only DSN
   for a separate metrics schema, mounted only where it is granted.

## Quick start

Requires Docker (any provider), and an Apple Silicon or amd64 host.

```bash
brew install kubectl helm kubeconform uv
make cluster-up   # provisions the VMs, installs k3s + gVisor + the platform, runs the gates
make images       # builds on the amd64 builder, pushes to the in-cluster registry
make deploy       # installs the chart; migrations run as a pre-upgrade hook
make seed         # loads reference personas, documents and templates
```

The cluster is three k3s nodes plus a builder on Proxmox — see
[deploy/k3s/README.md](deploy/k3s/README.md) for the layout and why it replaced
a laptop-hosted `kind` cluster. Once deployed the app is on a real LAN address
rather than behind a port-forward.

`make cluster-up` refuses to continue unless three gates pass:

- **`smoke-gvisor`** asserts `/proc/version` reports gVisor. A misconfigured
  RuntimeClass handler *silently falls back to runc* on some setups, producing a
  green pod with no isolation — a fake security story that looks exactly like a
  real one. Never weaken this to a readiness check.
- **`smoke-sandbox`** drives a real `Sandbox` through the controller and reaches
  it over cluster DNS, exercising the CRD, controller, warm path and SDK before
  a line of application code is involved.
- **`smoke-netpol`** asserts a deny-all NetworkPolicy actually blocks traffic.
  Every CNI accepts policy objects; not every CNI enforces them, and one that
  quietly ignores them looks identical from the outside.

### Inference

Any OpenAI-compatible endpoint. There is no in-cluster model server: serving a
model well is a different problem from orchestrating agents, and running both on
one small machine makes the demo compete with the sandboxes it exists to serve.

For local development, run Ollama on your own machine — where it also gets GPU
acceleration:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

`values-lab.yaml` points at it. Credentials are optional; local providers need
none.

For a hosted endpoint:

```bash
kubectl -n meetings create secret generic meetings-runtime \
  --from-literal=INFERENCE_API_KEY=... --from-literal=EMBEDDING_API_KEY=...
helm upgrade meetings deploy/charts/meetings -n meetings \
  --set inference.endpoint=https://... --set inference.modelName=...
```

One sharp edge worth knowing: sandboxes run under a default-deny egress policy,
and **NetworkPolicy matches addresses, not DNS names**. Set
`inference.egressCIDRs` to whatever your endpoint resolves to, or the backend
will work while every sandbox turn fails with a bare connection error.

## Platform

| Component | Choice | Why |
|---|---|---|
| Cluster | 3-node k3s on Proxmox VMs | Real nodes with their own kernels, so `runsc` is simply the runtime |
| Sandboxes | Agent Sandbox v0.5.6 (`agents.x-k8s.io/v1beta1`) | The emerging standard for agent isolation on Kubernetes |
| Database | CloudNativePG 1.30, Postgres 18 | pgvector arrives as a declarative **ImageVolume** extension, not a custom-baked image |
| Ingress | Gateway API + Envoy Gateway + MetalLB | A real LoadBalancer address on the LAN, and the WebSocket upgrade the transcript stream needs |
| Migrations | Alembic, as a Helm pre-upgrade hook | Schema changes are explicit and reviewable, not a startup side effect |

## Development

`make images` builds on the amd64 builder, tags each image by its content
digest, and pushes to the in-cluster registry; `make deploy` passes those tags
through. A mutable `:latest` leaves the Deployment spec unchanged
when an image is rebuilt, so `helm upgrade` finds nothing to roll and the pod
keeps serving stale code — a deploy that reports success and changed nothing.
Identical content produces the same tag, so a no-op redeploy does not churn pods
either.

```bash
make check          # ruff, format, mypy, pytest, helm lint, kubeconform
make migrate-check  # fails if the ORM has drifted from the migrations
make status         # cluster at a glance
tilt up             # live-reload inner loop
```

Configuration is environment-only — there is no config file. Credentials come
from the `meetings-runtime` Secret; operator-tunable values (prompts, turn
limits, temperatures) live in a `system_settings` table and are editable through
the UI. Attempting to set a credential through the settings API is a 422.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | kind + gVisor + Agent Sandbox + CNPG/pgvector, fail-fast gates | ✅ done |
| 1 | Config/secrets, Alembic, typed models, probes, CI, first tests | ✅ done |
| 2 | Persona runtime image; agent turns execute inside sandboxes | ✅ done |
| 3 | Full tool suite; Kubernetes-enforced least privilege; audit matrix | next |
| 4 | Persona depth — every editable field actually reaches a prompt | |
| 5 | OpenTelemetry across all three tiers; Prometheus + Grafana | |
| 6 | Integration/e2e depth, operator auth, signed multi-arch images | |

## Documentation

- [Sandbox security model](docs/sandbox-security-model.md) — isolation tiers,
  what was verified, and the fallback ladder for hosts without gVisor.

## Acknowledgements

Originally built as an HPE Private Cloud AI demo; rebuilt as a portable,
vendor-neutral project.
