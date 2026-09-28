# Cluster requirements

This project deploys onto a Kubernetes cluster you already have. It does not
build one, and does not assume a particular distribution, hypervisor or cloud.

Run the check first:

```bash
make preflight
```

It reports what is present, what is missing, and — for the two requirements that
can be present and inert — whether the thing actually works. Everything below is
what that check is checking, and why.

---

## Summary

| Requirement | Why | If it is missing |
|---|---|---|
| Kubernetes 1.34+ | `ImageVolume` for pgvector, and the floor CloudNativePG 1.30 supports | Hard requirement |
| A RuntimeClass with kernel-level isolation | The boundary the whole design rests on | [Fallback tiers](#isolation-tiers) |
| Agent Sandbox v1.0.4+ (`agents.x-k8s.io/v1beta1`) | Sandbox, SandboxClaim, SandboxTemplate, SandboxWarmPool | Hard requirement |
| CloudNativePG 1.30+ | Postgres 18 with pgvector as a declarative extension | Hard requirement |
| Gateway API + a GatewayClass with a running controller | WebSocket upgrade for the transcript stream | Hard requirement |
| A CNI that **enforces** NetworkPolicy | Two of the enforcement layers | Two layers become decorative |
| A default StorageClass | CNPG provisions its own volumes | Hard requirement |
| A LoadBalancer implementation | Reaching the Gateway | Optional — see [Ingress](#ingress) |

Resource floor for a full demo: roughly **8 CPU and 16 GiB allocatable**, most
of it warm-pool sandboxes that sit idle so no turn ever pays a cold start. It
runs smaller by reducing `sandbox.profiles.*.warmReplicas`, at the cost of a
several-second pause the first time each persona speaks.

---

## Isolation tiers

Sandboxes run under whatever `sandbox.runtimeClassName` names. It is a Helm
value and `gvisor` appears in no template, so moving between tiers is a
one-value change.

| Tier | RuntimeClass | Boundary | What you give up |
|---|---|---|---|
| 1 | gVisor (`runsc`) | A separate kernel implementation in userspace; the host kernel's syscall surface is not directly reachable | Nothing; this is the design target |
| 2 | `crun` with `hostUsers: false` | User namespaces — root in the container is not root on the host | Container and host still share one kernel, so a kernel bug is a shared bug |
| 3 | `runc` with `restricted` Pod Security Admission | Policy only | The demo's central claim. Say so if you present on this tier |

Tier 1 is the one the security model is written against. Tiers 2 and 3 exist so
the application still runs on a cluster that cannot offer a kernel boundary —
they are not equivalent, and the docs do not pretend otherwise.

### Installing gVisor

This is the one requirement that touches nodes rather than the API, because a
container runtime is installed on a host. There is no Kubernetes-native way to
add one.

**Managed clusters** have their own mechanism and it is better than anything a
script can do from outside:

- **GKE** — GKE Sandbox is a node-pool setting; create a pool with
  `--sandbox type=gvisor`. Google manages the runtime.
- **EKS / AKS** — a custom AMI or node image with `runsc` installed, or a
  DaemonSet-based installer. Both are provider-specific.

Then apply only the RuntimeClass:

```bash
kubectl apply -f deploy/cluster/templates/runtimeclass.yaml
```

**Self-managed nodes** — the helper installs `runsc`, registers the containerd
handler and declares the RuntimeClass:

```bash
GVISOR_NODES="ubuntu@10.0.0.11 ubuntu@10.0.0.12" \
  deploy/cluster/install-prerequisites.sh gvisor
```

It handles both containerd layouts. The distinction matters: k3s regenerates its
containerd configuration on every start, so the handler has to go in a template
drop-in it merges rather than in `config.toml`, which does not survive a
restart.

Two things worth knowing before you debug anything here:

- gVisor publishes release artifacts under the `uname` architecture — `x86_64`,
  `aarch64` — not the Go names. `amd64` and `arm64` return 404.
- `platform = "systrap"`, the default, is a userspace platform and needs no KVM.
  It is the right choice anywhere nested virtualisation is unavailable.

### Then prove it

```bash
make preflight        # or: make smoke-gvisor
```

This is not a formality. **A misconfigured RuntimeClass handler does not fail
loudly.** On several container runtimes it silently falls back to `runc`, and
the result is a green, `Ready` pod with no isolation whatsoever — a fake
security story that is indistinguishable from a real one to anything that only
checks pod status. The check greps `/proc/version` inside a pod that actually
ran, looking for gVisor's sentry string (`4.19.0-gvisor`).

Never weaken that assertion to a readiness check.

---

## NetworkPolicy enforcement

Two of the enforcement layers are NetworkPolicy. **Every CNI accepts
NetworkPolicy objects; not every CNI enforces them**, and one that quietly
ignores them is invisible from outside — the policies apply cleanly and
`kubectl get networkpolicy` is reassuring.

Enforcing CNIs include Calico, Cilium, Antrea, kube-router (k3s default), and
the managed network policy add-ons on GKE, EKS and AKS. `kindnetd`, kind's
default, implements none of it.

```bash
make preflight        # or: make smoke-netpol
```

The check applies a deny-all egress policy and then opens a socket, to both an
in-cluster and an external address — they are not the same test, and some CNIs
implement one and not the other. It fails on what happened, not on what was
configured.

The probe waits after its pod is running before it connects. Most CNIs program a
pod's policy when the pod appears rather than when the policy is written, so a
probe that fires immediately measures the window before enforcement starts and
reports an enforcing cluster as unenforced.

If your CNI does not enforce policy, the app still runs and the RBAC layer —
which is the authoritative control for code execution — still holds. Say plainly
which layers are inert rather than presenting all seven.

---

## Agent Sandbox

```bash
deploy/cluster/install-prerequisites.sh agent-sandbox
```

Installs the CRDs and controller from the upstream release bundle with
server-side apply. `v1.0.4` or newer. The APIs are still `agents.x-k8s.io/v1beta1`: 1.0 is a product
version, and what it removed is `v1alpha1` and the conversion webhook.

The release bundle installs the controller and the CRDs. It does not install
the Sandbox Router, which this project runs itself -- see below.

### What the controller does by default, and what this project does instead

Sandboxes created from a SandboxTemplate default to `networkPolicyManagement:
Managed`. Where a template declares no `networkPolicy`, the controller
synthesises one: ingress only from the Sandbox Router in `agent-sandbox-system`,
and egress to the **whole public internet**, with the private ranges excluded
and cluster DNS deliberately blocked.

That default is built for a sandbox that browses the web and is reached through
a shared router. Neither half suits this project, so every template declares its
policy explicitly (see
[`sandbox-templates.yaml`](../deploy/charts/meetings/templates/sandbox-templates.yaml)):
ingress from this release's own router and nothing else, and egress to the
backend's internal API, DNS and the apiserver. Model calls go through the
backend's proxy, so no sandbox has a route off the cluster.

### The Sandbox Router

Upstream's Go router (`registry.k8s.io/agent-sandbox/sandbox-router-go`),
unmodified, deployed by this chart in scoped-token mode. There is nothing to
install beforehand. Two instances run in the release namespace, one in front of
each sandbox tier, and `make deploy` creates the signing keypair on first run.

It needs the cluster to let a ServiceAccount list pods in the two sandbox
namespaces, which the chart grants as namespaced Roles. Why it runs per release
rather than as the shared instance upstream's manifests install is in
[sandbox-security-model.md](sandbox-security-model.md#one-router-per-release-not-one-per-cluster).

---

## Database

```bash
deploy/cluster/install-prerequisites.sh cnpg
```

CloudNativePG 1.30+, and Kubernetes 1.34+. pgvector arrives as
a declarative extension mounted from an OCI image rather than baked into a
custom Postgres build — the extension version becomes a value in
[`cnpg-cluster.yaml`](../deploy/bootstrap/cnpg-cluster.yaml) instead of a
Dockerfile you own and must rebuild.

Two things set that floor. `ImageVolume` went alpha in Kubernetes 1.32 behind a
feature gate, beta and on by default in 1.33, and by 1.35 the gate is gone -- so on
its own it would put the floor at 1.33. CloudNativePG raises it: 1.30 lists
Kubernetes 1.34, 1.35 and 1.36 as supported, with 1.31 through 1.33 tested but
explicitly not supported. 1.34+ is the higher of the two.

If you would rather bake pgvector into a Postgres image the conventional way, the
Kubernetes floor drops considerably -- but this repo ships the ImageVolume path.

Verify pgvector is genuinely loaded, rather than merely declared:

```bash
make smoke-pgvector
```

---

## Ingress

```bash
deploy/cluster/install-prerequisites.sh gateway
```

Gateway API with Envoy Gateway, for the WebSocket upgrade the transcript stream
depends on.

Envoy Gateway ships the Gateway API CRDs and its controller but deliberately
creates **no GatewayClass** — that is left to the platform owner. This is worth
stating because of how it fails: the CRDs are present, `kubectl get gateways`
works, and nothing at all is watching. A Gateway API surface with no
GatewayClass is inert, and looks installed. `make preflight` checks for the
GatewayClass separately for exactly this reason.

If your cluster already has a Gateway API implementation, set `GATEWAY_CLASS`
to your GatewayClass and skip this. If it already has a *Gateway* that projects
share -- one address, one wildcard DNS record -- set `GATEWAY_NAME` and
`GATEWAY_NAMESPACE` as well: the chart then creates no Gateway of its own and
attaches a route to yours. That Gateway must admit routes from the `meetings`
namespace and have a listener whose hostname covers `meetings.${APP_DOMAIN}`;
its certificate and any redirect are its owner's, so the `APP_TLS_*` settings
are ignored in this mode. If it has an Ingress
controller instead, set `gateway.enabled=false` and route to the
`meetings-frontend` and `meetings-backend` Services yourself — the only
requirement the app places on ingress is that `/api/v1` reaches the backend with
WebSocket upgrade and no request timeout.

### LoadBalancer

Optional. On a bare-metal cluster with no LoadBalancer implementation:

```bash
deploy/cluster/install-prerequisites.sh metallb
```

`METALLB_RANGE` must sit outside your DHCP range. An overlap produces address
conflicts that present as intermittent, unexplained outages rather than as
anything network-shaped.

Without a LoadBalancer, expose the Gateway however your cluster normally does —
a NodePort, an existing ingress, or `kubectl port-forward` for a single-user
demo.

---

## Images

The five images are published, multi-arch, signed and provenance-attested.
Verify them before you deploy:

```bash
make verify-images
```

This checks that each image was signed by this repository's CI, keyless via its
OIDC identity, and that a SLSA provenance attestation exists naming the commit
it was built from. See [operations.md](operations.md#images-and-supply-chain).

To build your own instead — a fork, or an air-gapped cluster — see
[operations.md](operations.md#building-your-own-images). An in-cluster registry
is available if you need one:

```bash
deploy/cluster/install-prerequisites.sh registry
```

---

## Inference

Any OpenAI-compatible endpoint: a hosted provider, a self-hosted vLLM, or Ollama
on your own machine. There is no in-cluster model server, and that is a design
decision rather than an omission — serving a model well is a different problem
from orchestrating agents, and running both on one cluster makes the demo
compete for CPU with the sandboxes it exists to serve.

Only the backend needs to reach the endpoint. Persona sandboxes have no egress
off the cluster at all: they call the model through the backend's
`/internal/v1/llm` proxy, authenticated by their ServiceAccount token.

This used to be the single most common cause of a deployment where the backend
worked and every agent turn failed, because sandboxes called the provider
directly under a default-deny egress policy and **NetworkPolicy matches
addresses, not DNS names**. Naming the addresses is impossible for a hosted
provider behind a CDN, and the only rule that worked was `0.0.0.0/0` -- the
whole internet, opened for the least-trusted component in the system. Proxying
removed the setting and the failure mode together.

---

## Namespaces and permissions

Installing the chart creates three namespaces and the RBAC that separates them:

| Namespace | Holds |
|---|---|
| `meetings` | Backend, frontend, the two Sandbox Routers, the CNPG cluster, and the Gateway unless you attach to an existing one |
| `meetings-sandboxes` | Tier A — one persona pod per attendee, warm-pooled per profile |
| `meetings-exec` | Tier B — the code-execution warm pool, deny-all egress |

You need cluster-admin to install, because the chart creates namespaces,
ClusterRoles, a ValidatingAdmissionPolicy and CRDs' custom resources. It does not need cluster-admin to run.
