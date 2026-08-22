# Sandbox security model

## Runtime isolation tier: gVisor (runsc) — CONFIRMED

The fallback ladder defined during planning was:

1. **`runsc` / gVisor** — preferred
2. `crun` + `hostUsers: false` (user namespaces)
3. `runc` + `restricted` Pod Security Admission

**Tier 1 is in effect.** Verified 2026-08-22 on the target host.

### Evidence

`deploy/bootstrap/smoke-gvisor.yaml` run on a kind cluster built from
`deploy/kind/node-image` (gVisor `release-20260817.0`):

```
kernel: Linux version 4.19.0-gvisor #1 SMP Sun Jan 10 15:06:54 PST 2016
GVISOR-OK
```

This matters more than it looks. The stack nests four deep —
macOS Virtualization.framework → Rancher Desktop Linux VM (aarch64) →
kind node container → containerd → `runsc` — and arm64 gVisor under that much
nesting is the least-tested corner of the support matrix. It works.

### Why the smoke test asserts on `/proc/version`

A misconfigured `RuntimeClass` handler does **not** always fail loudly: on
several setups containerd silently falls back to `runc`, producing a green,
Ready pod with no isolation whatsoever — a fake security story that looks
identical to a real one from the outside. gVisor's sentry reports a
distinctive kernel string (`4.19.0-gvisor`), so the gate greps for it and
fails the build if it is absent. **Never weaken this assertion to a readiness
check.**

### Host-specific notes

- gVisor publishes release artifacts under `aarch64` / `x86_64`, **not**
  `arm64` / `amd64`. The Go-style names 404.
- `platform = "systrap"` (the default) is used deliberately: it is a
  userspace platform and needs no KVM, which is unavailable to us inside the
  macOS VM.
- `ignore-cgroups = "true"` keeps runsc from contending with the kind node
  over cgroup ownership.

### Portability

`sandbox.runtimeClassName` is a Helm value and `gvisor` is never hardcoded in
a template, so dropping to tier 2 or 3 on a host that cannot run runsc is a
one-line values change. Nightly CI runs the same gate on amd64 runners.

### Durability across host restarts

Planning flagged a concern that a runsc setup might not survive a Rancher
Desktop restart. It does. After `rdctl set --kubernetes.enabled=false`
reconfigured and restarted the VM backend, both kind nodes came back, the
CNPG cluster and its pgvector ImageVolume were intact, and both gates passed
again unchanged. The runsc binary and containerd handler live in the kind
*node image*, so they are rebuilt only by `make node-image`, not by anything
the host does.

### Reaching a sandbox from the backend

`Sandbox.spec.service: true` makes the controller publish a per-sandbox
Service, and `Sandbox.status.serviceFQDN` carries its DNS name. Verified: a
pod in the `meetings` namespace reaches a sandbox in `meetings-sandboxes` at
`<name>.<ns>.svc.cluster.local:8080` and gets HTTP 200.

This simplifies the design as planned. The backend runs in-cluster, so
backend → persona-runtime calls use `serviceFQDN` directly. The **Sandbox
Router is only needed for access from outside the cluster**, where
`kubectl port-forward` is unusable because it is incompatible with gVisor.
