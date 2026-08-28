#!/usr/bin/env bash
# Does this cluster meet the requirements?
#
#   deploy/cluster/preflight.sh          check everything
#   deploy/cluster/preflight.sh --quiet  exit status only, for scripting
#
# Every check provokes the behaviour it cares about rather than asking
# Kubernetes what it intends. That distinction is the whole design of this
# script, and it is not pedantry: the two failures that cost this project the
# most were both controls that read correctly and did nothing.
#
#   * A misconfigured RuntimeClass handler does not fail loudly. On several
#     container runtimes it silently falls back to runc, producing a green,
#     Ready pod with no isolation whatsoever — a fake security story that is
#     indistinguishable from a real one to anything that only checks pod status.
#     So the gVisor check greps /proc/version inside a pod that actually ran.
#
#   * Every CNI accepts NetworkPolicy objects. Not every CNI enforces them, and
#     one that quietly ignores them looks identical from the outside: the
#     policies apply cleanly and `kubectl get networkpolicy` is reassuring. So
#     the policy check opens a socket and observes whether it connects.
#
# Checks that cannot be provoked cheaply (CRD presence, operator readiness) are
# read from the API, and are marked as such below.
set -uo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

QUIET=false
[ "${1:-}" = "--quiet" ] && QUIET=true

# Read the same settings the deployment will use. Without this the RuntimeClass
# check always tested `gvisor`, whatever cluster.env said -- so a cluster
# deliberately running a weaker tier failed a check for a class it does not
# declare and was never going to use, while the class it does use went untested.
#
# An explicit environment variable still wins, so a one-off check needs no file
# edit. That matches scripts/render.py.
_env_runtime_class=${SANDBOX_RUNTIME_CLASS:-}
ENV_FILE=$(cd "$(dirname "$0")" && pwd)/cluster.env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

KUBECTL=${KUBECTL:-kubectl}
[ -n "${KCTX:-}" ] && KUBECTL="$KUBECTL --context $KCTX"
NS=${PREFLIGHT_NAMESPACE:-default}
RUNTIME_CLASS=${_env_runtime_class:-${SANDBOX_RUNTIME_CLASS:-gvisor}}

pass=0
fail=0
warn=0

say()  { $QUIET || printf '%s\n' "$*"; }
ok()   { pass=$((pass+1)); $QUIET || printf '  \033[32m✓\033[0m %-34s %s\n' "$1" "${2:-}"; }
bad()  { fail=$((fail+1)); $QUIET || printf '  \033[31m✗\033[0m %-34s %s\n' "$1" "${2:-}"; }
soft() { warn=$((warn+1)); $QUIET || printf '  \033[33m!\033[0m %-34s %s\n' "$1" "${2:-}"; }

command -v kubectl >/dev/null || { echo "kubectl not found on PATH" >&2; exit 2; }
$KUBECTL version -o json >/dev/null 2>&1 || {
  echo "cannot reach a cluster. Check your kubeconfig context." >&2
  exit 2
}

say ""
say "Cluster: $($KUBECTL config current-context 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- versions
say ""
say "Kubernetes"
server=$($KUBECTL version -o json 2>/dev/null | sed -n 's/.*"gitVersion": *"v\([0-9]*\.[0-9]*\)[^"]*".*/\1/p' | tail -1)
if [ -n "$server" ]; then
  major=${server%%.*}; minor=${server##*.}
  if [ "$major" -gt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -ge 31 ]; }; then
    ok "version $server" "(1.31+ required)"
  else
    bad "version $server" "1.31 or newer is required"
  fi
else
  soft "version" "could not be determined"
fi

# ---------------------------------------------------------------- CRDs
# Read from the API rather than provoked: a CRD either exists or it does not,
# and there is no failure mode where it is present and inert.
say ""
say "Required APIs"
check_crd() {
  if $KUBECTL get crd "$1" >/dev/null 2>&1; then
    ok "$2" "$1"
  else
    bad "$2" "missing: $1"
  fi
}
check_crd sandboxes.agents.x-k8s.io                         "Agent Sandbox"
check_crd sandboxclaims.extensions.agents.x-k8s.io          "  SandboxClaim"
check_crd sandboxtemplates.extensions.agents.x-k8s.io       "  SandboxTemplate"
check_crd sandboxwarmpools.extensions.agents.x-k8s.io       "  SandboxWarmPool"
check_crd clusters.postgresql.cnpg.io                       "CloudNativePG"
check_crd gateways.gateway.networking.k8s.io                "Gateway API"

# A GatewayClass is what makes the Gateway API surface live. This is precisely
# the failure worth naming: the CRDs can be installed by one component while no
# controller is actually watching them, and the whole surface is inert.
if [ "$($KUBECTL get gatewayclass -o name 2>/dev/null | wc -l | tr -d ' ')" != "0" ]; then
  ok "GatewayClass" "$($KUBECTL get gatewayclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
else
  bad "GatewayClass" "CRDs present but no GatewayClass -- the API is inert"
fi

# ---------------------------------------------------------------- operators
say ""
say "Controllers"
# Matched on the Deployment rather than on pod labels. Upstream label
# conventions differ between these two projects and change between releases; the
# Deployment name is what their installers actually create and is stable.
running() {
  local ns=$1 deploy=$2 label=$3
  local ready
  ready=$($KUBECTL -n "$ns" get deploy "$deploy" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  if [ -n "$ready" ] && [ "$ready" != "0" ]; then
    ok "$label" "$ready ready"
  else
    bad "$label" "deploy/$deploy is not ready in $ns"
  fi
}
running agent-sandbox-system agent-sandbox-controller "Agent Sandbox controller"
running cnpg-system cnpg-cloudnative-pg               "CloudNativePG operator"

# ---------------------------------------------------------------- RuntimeClass
say ""
say "Sandbox isolation"
if $KUBECTL get runtimeclass "$RUNTIME_CLASS" >/dev/null 2>&1; then
  ok "RuntimeClass '$RUNTIME_CLASS'" "declared"
else
  bad "RuntimeClass '$RUNTIME_CLASS'" "not declared on this cluster"
fi

# The check that matters. A RuntimeClass can exist, be schedulable, and produce
# a pod running under plain runc.
if $KUBECTL get runtimeclass "$RUNTIME_CLASS" >/dev/null 2>&1; then
  $KUBECTL -n "$NS" delete pod preflight-gvisor --ignore-not-found >/dev/null 2>&1
  cat <<YAML | $KUBECTL -n "$NS" apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: preflight-gvisor
spec:
  runtimeClassName: ${RUNTIME_CLASS}
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.37
      command: ["sh", "-c", "cat /proc/version"]
YAML
  if $KUBECTL -n "$NS" wait --for=condition=Ready pod/preflight-gvisor --timeout=90s >/dev/null 2>&1 ||
     $KUBECTL -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/preflight-gvisor --timeout=90s >/dev/null 2>&1; then
    kernel=$($KUBECTL -n "$NS" logs preflight-gvisor 2>/dev/null)
    if printf '%s' "$kernel" | grep -qi gvisor; then
      ok "kernel boundary is real" "$(printf '%s' "$kernel" | head -c 48)"
    else
      bad "kernel boundary is NOT real" "fell back to the host kernel: ${kernel:-no output}"
    fi
  else
    bad "isolation probe" "pod never ran; check node taints and the runtime handler"
  fi
  $KUBECTL -n "$NS" delete pod preflight-gvisor --ignore-not-found >/dev/null 2>&1
fi

# ---------------------------------------------------------------- NetworkPolicy
say ""
say "NetworkPolicy enforcement"
$KUBECTL delete namespace preflight-netpol --ignore-not-found --wait >/dev/null 2>&1
$KUBECTL create namespace preflight-netpol >/dev/null 2>&1
cat <<'YAML' | $KUBECTL -n preflight-netpol apply -f - >/dev/null 2>&1
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-egress
spec:
  podSelector: {}
  policyTypes: [Egress]
YAML
cat <<'YAML' | $KUBECTL -n preflight-netpol apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: probe
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.37
      # Literal addresses, never names: with egress denied DNS fails too, and a
      # failed lookup would pass this check for the wrong reason.
      #
      # Both an in-cluster and an external destination, because they are not the
      # same test. Some CNIs enforce one direction or one destination class and
      # not the other, and the in-cluster one is what layers 3 and 4 rest on --
      # it is the difference between a persona being unable to reach Postgres
      # and merely being told not to.
      command:
        - sh
        - -c
        - |
          # Enforcement is programmed when the pod appears, not when the
          # policy is written -- so the settle window belongs here, inside the
          # pod, not before it is created. Probing immediately reports an
          # enforcing cluster as unenforced.
          sleep 15
          printf 'in-cluster='
          nc -w 4 -z "$KUBERNETES_SERVICE_HOST" 443 && echo REACHED || echo BLOCKED
          printf 'external='
          nc -w 4 -z 1.1.1.1 443 && echo REACHED || echo BLOCKED
YAML
if $KUBECTL -n preflight-netpol wait --for=jsonpath='{.status.phase}'=Succeeded pod/probe --timeout=120s >/dev/null 2>&1; then
  result=$($KUBECTL -n preflight-netpol logs probe 2>/dev/null)
  internal=$(printf '%s' "$result" | sed -n 's/^in-cluster=//p')
  external=$(printf '%s' "$result" | sed -n 's/^external=//p')

  if [ "$internal" = "BLOCKED" ]; then
    ok "egress deny-all, in-cluster" "traffic was actually stopped"
  else
    bad "egress deny-all, in-cluster" "policy accepted and NOT enforced -- layers 3 and 4 are decorative here"
  fi
  if [ "$external" = "BLOCKED" ]; then
    ok "egress deny-all, external" "traffic was actually stopped"
  else
    bad "egress deny-all, external" "a sandbox can reach the internet regardless of policy"
  fi
else
  soft "policy probe" "did not complete; no egress to pull busybox?"
fi
$KUBECTL delete namespace preflight-netpol --ignore-not-found --wait=false >/dev/null 2>&1

# ---------------------------------------------------------------- storage
say ""
say "Platform"
default_sc=$($KUBECTL get storageclass -o json 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    sys.exit(0)
for item in items:
    annotations = item["metadata"].get("annotations") or {}
    if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
        print(item["metadata"]["name"])
        break
')
if [ -n "$default_sc" ]; then
  ok "default StorageClass" "$default_sc"
else
  bad "default StorageClass" "CloudNativePG needs one to provision its volumes"
fi

# ImageVolume (Kubernetes 1.31+, beta and on by default from 1.33) is how
# pgvector reaches Postgres without a custom-baked image. Its absence is not
# fatal to the app, but it is fatal to the database as configured.
if $KUBECTL get --raw /api/v1 >/dev/null 2>&1 && \
   $KUBECTL get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null | grep -qE 'v1\.(3[3-9]|[4-9][0-9])'; then
  ok "ImageVolume support" "kubelet is 1.33+"
else
  soft "ImageVolume support" "needs 1.33+, or the feature gate enabled on 1.31/1.32"
fi

if $KUBECTL get svc -A -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].metadata.name}' 2>/dev/null | grep -q .; then
  ok "LoadBalancer" "an implementation is assigning addresses"
else
  soft "LoadBalancer" "none seen; install MetalLB or expose the Gateway another way"
fi

# ---------------------------------------------------------------- verdict
say ""
if [ "$fail" -ne 0 ]; then
  say "$(printf '\033[31m%s failed\033[0m, %s passed, %s warnings' "$fail" "$pass" "$warn")"
  say ""
  say "Install what is missing:  deploy/cluster/install-prerequisites.sh all"
  say "Or see the requirement and the reasoning:  docs/requirements.md"
  exit 1
fi
say "$(printf '\033[32mall %s checks passed\033[0m, %s warnings' "$pass" "$warn")"
say ""
say "Next:  make deploy && make seed"
exit 0
