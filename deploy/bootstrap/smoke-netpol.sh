#!/usr/bin/env bash
# GATE 3: NetworkPolicy is enforced, not merely accepted. See smoke-netpol.yaml.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

KCTX="${1:-kind-meetings}"
NS=netpol-smoke
k() { kubectl --context "$KCTX" "$@"; }

cleanup() { k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
k wait --for=delete namespace/"$NS" --timeout=60s >/dev/null 2>&1 || true
k apply -f "$(dirname "$0")/smoke-netpol.yaml" >/dev/null
k -n "$NS" wait --for=condition=Ready pod/netpol-target --timeout=180s >/dev/null

target_ip=$(k -n "$NS" get pod netpol-target -o jsonpath='{.status.podIP}')
echo "  target: $target_ip (deny-all ingress)"

# Give the CNI a moment to program the policy for a freshly created pod.
sleep 10

if k -n "$NS" run netpol-probe --rm -i --restart=Never --image=busybox:1.37 \
     --command -- wget -q -T 8 -O /dev/null "http://${target_ip}:8080/" >/dev/null 2>&1; then
  echo "FAIL: a deny-all NetworkPolicy did not block traffic."
  echo "      The CNI is accepting policies without enforcing them."
  exit 1
fi

echo "  deny-all blocked the connection: enforcement is real"
echo "NETPOL-OK"
