#!/usr/bin/env bash
# GATE 4: reaching a sandbox is an authorisation decision, and admission
# refuses an unsandboxed workload.
#
# Needs the app deployed: the probe runs in the backend pod, which holds the
# signing key and is the only thing the persona router admits.
set -euo pipefail

KCTX="${1:-}"
HERE=$(cd "$(dirname "$0")" && pwd)
k() { if [ -n "$KCTX" ]; then kubectl --context "$KCTX" "$@"; else kubectl "$@"; fi; }

k -n meetings exec -i deploy/meetings-backend -- python - <"$HERE/smoke-router.py" 2>/dev/null \
  | grep -v '^{' || { echo "FAIL: router gate"; exit 1; }

# Server-side dry run: the apiserver evaluates admission and creates nothing.
for ns in meetings-sandboxes meetings-exec; do
  out=$(cat <<POD | k apply --dry-run=server -f - 2>&1 || true
apiVersion: v1
kind: Pod
metadata: {name: smoke-not-sandboxed, namespace: $ns}
spec:
  securityContext: {runAsNonRoot: true, runAsUser: 1001, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: probe
      image: busybox:1.37
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
POD
)
  if grep -q "must set runtimeClassName" <<<"$out"; then
    echo "  ok   admission refuses an unsandboxed pod in $ns"
  else
    echo "  FAIL an unsandboxed pod would be admitted to $ns: $out"
    exit 1
  fi
done
echo "ROUTER-OK"
