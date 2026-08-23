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

Expect `blocked` for baseline and counsel. Allow ~30s after a pod is created
for the CNI to program its policy; testing immediately gives false passes.

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

## The gates

```bash
make smoke
```

- **gate 1** asserts `/proc/version` reports gVisor, because a misconfigured
  RuntimeClass silently falls back to runc
- **gate 2** drives a real Sandbox through the controller and reaches it over
  cluster DNS
- **gate 3** asserts a deny-all NetworkPolicy actually blocks traffic, because
  some CNIs accept policies without enforcing them
