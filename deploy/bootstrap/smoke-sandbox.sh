#!/usr/bin/env bash
# GATE 2: Agent Sandbox control-plane round trip.
#
# Exercises the CRD, the controller, gVisor isolation, the per-sandbox Service,
# and in-cluster DNS reachability -- the exact path the backend uses to talk to
# a persona runtime. Runs in ~20s, before any application code exists.
set -euo pipefail

KCTX="${1:-kind-meetings}"
NS=meetings-sandboxes
NAME=smoke-sandbox
k() { kubectl --context "$KCTX" "$@"; }

cleanup() { k -n "$NS" delete sandbox "$NAME" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
k apply -f "$(dirname "$0")/smoke-sandbox.yaml" >/dev/null

if ! k -n "$NS" wait --for=condition=Ready "sandbox/$NAME" --timeout=180s >/dev/null; then
  echo "FAIL: sandbox never became Ready"
  k -n "$NS" describe sandbox "$NAME" | tail -30
  exit 1
fi

# 1. Isolation is real, not a silent runc fallback.
kernel=$(k -n "$NS" exec "$NAME" -- cat /proc/version 2>/dev/null || true)
echo "  kernel: $kernel"
if ! grep -qi gvisor <<<"$kernel"; then
  echo "FAIL: sandbox is not running under gVisor"
  exit 1
fi

# 2. The controller published a Service and it resolves cross-namespace.
fqdn=$(k -n "$NS" get sandbox "$NAME" -o jsonpath='{.status.serviceFQDN}')
[[ -n "$fqdn" ]] || { echo "FAIL: no status.serviceFQDN"; exit 1; }
echo "  serviceFQDN: $fqdn"

# Run the probe as a Job and read its exit code, rather than `kubectl run
# --rm -i`. That form races with a pod that exits quickly -- the container is
# gone before the log stream attaches, and the gate reports a failure that never
# happened. The probe is also hardened, so it runs unchanged in a namespace
# with restricted Pod Security admission.
k -n meetings delete job smoke-dnsprobe --ignore-not-found >/dev/null 2>&1
cat <<PROBE | k apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: smoke-dnsprobe
  namespace: meetings
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        runAsGroup: 65534
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: probe
          image: busybox:1.37
          # Retries briefly: cluster DNS for a freshly created headless
          # Service can lag the pod becoming ready by a second or two.
          command:
            - sh
            - -c
            - |
              for i in \$(seq 1 15); do
                wget -q -T 5 -O /dev/null "http://${fqdn}:8080/" && exit 0
                sleep 2
              done
              exit 1
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: ["ALL"]}
PROBE

if ! k -n meetings wait --for=condition=complete job/smoke-dnsprobe --timeout=90s >/dev/null 2>&1; then
  echo "FAIL: sandbox not reachable at ${fqdn}:8080"
  k -n meetings logs job/smoke-dnsprobe 2>&1 | tail -5
  k -n meetings delete job smoke-dnsprobe --ignore-not-found >/dev/null 2>&1
  exit 1
fi
k -n meetings delete job smoke-dnsprobe --ignore-not-found >/dev/null 2>&1

echo "  reachable over cluster DNS: yes"

echo "SANDBOX-OK"
