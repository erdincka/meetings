# Verifying the enforcement layers

Every claim in [sandbox-security-model.md](sandbox-security-model.md) is
measurable. These are the commands. Run them after changing any profile, and
after any cluster rebuild.

Nothing here inspects a YAML file or asks Kubernetes what it *intends*. Each
command provokes the behaviour and observes what actually happens, because the
interesting failures are precisely the ones where the configuration reads
correctly and does nothing.

## Layer 5 — RBAC: who may execute code

```bash
for p in baseline counsel strategist analyst quant chief; do
  printf "  %-11s %s\n" "$p" \
    "$(kubectl auth can-i create sandboxclaims.extensions.agents.x-k8s.io \
        --as="system:serviceaccount:meetings-sandboxes:persona-${p}" \
        -n meetings-exec)"
done
```

Expect `yes` for `quant` only. This is the control the demo turns on: a
General Counsel persona asked to run analysis is refused by the API server, not
by a prompt.

## Layer 4 — NetworkPolicy: who may reach the database

```bash
for p in baseline counsel analyst quant chief; do
  pod=$(kubectl -n meetings-sandboxes get pods -l meetings/profile=$p \
        --field-selector=status.phase=Running -o name | head -1 | cut -d/ -f2)
  printf "  %-10s %s\n" "$p" "$(kubectl -n meetings-sandboxes exec "$pod" -- python -c "
import socket
s=socket.socket(); s.settimeout(5)
try: s.connect(('meetings-postgres-rw.meetings.svc.cluster.local',5432)); print('OPEN')
except Exception: print('blocked')")"
done
```

Expect `blocked` for baseline and counsel.

**Wait ~15s after the pod is Running before probing.** Most CNIs program a
pod's policy when the pod appears, not when the policy is written, so a probe
that fires immediately measures the window before enforcement starts. Waiting
before *creating* the pod does not help and is the natural mistake to make: the
settle time belongs after the pod exists. Getting this wrong reports an
enforcing cluster as unenforced, which is the expensive direction — it sends you
looking for a CNI problem you do not have.

## Layer 3 — Secret: who holds the credential

```bash
kubectl -n meetings-sandboxes exec "$pod" -- \
  sh -c 'test -f /etc/sandbox/secrets/metrics-dsn && echo present || echo absent'
```

Expect `absent` for baseline and counsel. Combined with layer 4, a leaked DSN
is useless to a profile that has no route to the database anyway.

## Layer 2 — Capability file: what the runtime will register

```bash
kubectl -n meetings-sandboxes exec "$pod" -- cat /etc/sandbox/capabilities
```

Mounted from a ConfigMap the SandboxTemplate references, so changing it requires
changing a Kubernetes object. The runtime intersects the backend's grant with
this list; a compromised backend cannot enable a tool the template did not
provision.

## Layer 1 — Prompt: what the persona was actually told it has

```bash
kubectl -n meetings-sandboxes logs "$pod" | grep persona_bound
```

The weakest layer, listed for completeness. It is what stops an honest mistake
and nothing else — a model can attempt any tool call it can name, which is
exactly why the four layers above it do not consult the prompt.

## The gates

```bash
make smoke
```

- **gate 1** asserts `/proc/version` reports gVisor inside a pod that actually
  ran, because a misconfigured RuntimeClass silently falls back to runc and
  produces a green, unisolated pod
- **gate 2** drives a real Sandbox through the controller and reaches it over
  cluster DNS, exercising the CRD, controller, warm path and SDK before a line
  of application code is involved
- **gate 3** asserts a deny-all NetworkPolicy actually blocks traffic, because
  every CNI accepts policy objects and not every CNI enforces them

## Before any of this

```bash
make preflight
```

Checks that the cluster can support the controls at all: the RuntimeClass really
isolates, the CNI really enforces, the CRDs are present and their controllers
are running. Verifying a profile on a cluster that fails preflight measures
nothing.
