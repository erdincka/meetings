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

if ! k -n meetings run smoke-dnsprobe --rm -i --restart=Never --image=busybox:1.37 \
     --command -- wget -q -T 10 -O /dev/null "http://${fqdn}:8080/" 2>/dev/null; then
  echo "FAIL: sandbox not reachable at ${fqdn}:8080"
  exit 1
fi
echo "  reachable over cluster DNS: yes"

echo "SANDBOX-OK"
