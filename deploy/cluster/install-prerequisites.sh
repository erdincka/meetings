#!/usr/bin/env bash
# Install what this project needs onto a cluster that does not have it yet.
#
# Optional, and deliberately granular. Most clusters already have some of this,
# and an installer that insists on owning the whole platform is one you cannot
# run at all. Install only what `deploy/cluster/preflight.sh` says is missing.
#
#   install-prerequisites.sh gvisor        RuntimeClass + runsc on every node
#   install-prerequisites.sh agent-sandbox the CRDs and controller
#   install-prerequisites.sh cnpg          CloudNativePG operator
#   install-prerequisites.sh gateway       Envoy Gateway + a GatewayClass
#   install-prerequisites.sh metallb       a LoadBalancer for bare metal
#   install-prerequisites.sh registry      an in-cluster registry
#   install-prerequisites.sh all           everything above, in order
#
# Nothing here is specific to a hypervisor, a distribution or a cloud. Each
# subcommand is idempotent and safe to re-run.
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
ENV_FILE="$HERE/cluster.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

KUBECTL=${KUBECTL:-kubectl}
HELM=${HELM:-helm}
[ -n "${KCTX:-}" ] && { KUBECTL="$KUBECTL --context $KCTX"; HELM="$HELM --kube-context $KCTX"; }

AGENT_SANDBOX_VER=${AGENT_SANDBOX_VER:-v0.5.6}

say() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }

need() {
  command -v "$1" >/dev/null || { echo "$1 not found on PATH" >&2; exit 1; }
}

# ---------------------------------------------------------------- gVisor
#
# The only step that touches nodes rather than the API. It needs SSH access as a
# user who can sudo, because a container runtime is installed on the host, not
# in the cluster — there is no Kubernetes-native way to add one.
#
# Managed control planes (GKE, EKS, AKS) do this differently and better: GKE
# Sandbox is a node-pool setting, and on EKS/AKS a DaemonSet-based installer or
# a custom AMI is the usual route. See docs/requirements.md.
install_gvisor() {
  local nodes=${GVISOR_NODES:-}
  if [ -z "$nodes" ]; then
    cat >&2 <<'EOF'
GVISOR_NODES is not set.

gVisor installs a container runtime on each node, which is a host operation
rather than a cluster one. Set GVISOR_NODES to the SSH targets of the nodes that
will run sandboxes, for example:

    GVISOR_NODES="ubuntu@10.0.0.11 ubuntu@10.0.0.12" \
      deploy/cluster/install-prerequisites.sh gvisor

On a managed cluster, use the provider's mechanism instead -- GKE Sandbox is a
node-pool setting -- and then apply only the RuntimeClass:

    kubectl apply -f deploy/cluster/templates/runtimeclass.yaml
EOF
    exit 1
  fi

  for target in $nodes; do
    say "installing runsc on $target"
    # gVisor publishes release artifacts under the uname architecture
    # ("x86_64", "aarch64"), not the Go names. The Go-style names 404.
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$target" 'sudo bash -c "
      set -e
      if command -v runsc >/dev/null 2>&1; then echo \"  runsc already present\"; exit 0; fi
      B=https://storage.googleapis.com/gvisor/releases/release/latest/\$(uname -m)
      curl -fsSL -o /usr/local/bin/runsc \$B/runsc
      curl -fsSL -o /usr/local/bin/containerd-shim-runsc-v1 \$B/containerd-shim-runsc-v1
      chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1"'

    say "registering the runsc handler with containerd on $target"
    # Two containerd layouts, because the file that must be edited differs and
    # editing the wrong one produces a handler that silently never takes effect.
    #
    # k3s regenerates its containerd config on every start, so the handler goes
    # in a template drop-in it merges; editing config.toml directly does not
    # survive a restart. Everything else uses the config file itself.
    ssh -o BatchMode=yes "$target" 'sudo bash -c "
      set -e
      if [ -d /var/lib/rancher/k3s ]; then
        D=/var/lib/rancher/k3s/agent/etc/containerd
        mkdir -p \$D
        cat > \$D/config-v3.toml.tmpl <<TMPL
{{ template \"base\" . }}

[plugins.'\''io.containerd.cri.v1.runtime'\''.containerd.runtimes.runsc]
  runtime_type = \"io.containerd.runsc.v1\"
TMPL
        systemctl restart k3s 2>/dev/null || systemctl restart k3s-agent
      else
        grep -q containerd.runtimes.runsc /etc/containerd/config.toml || cat >> /etc/containerd/config.toml <<TOML

[plugins.\"io.containerd.grpc.v1.cri\".containerd.runtimes.runsc]
  runtime_type = \"io.containerd.runsc.v1\"
TOML
        systemctl restart containerd
      fi"'
  done

  say "declaring the RuntimeClass"
  $KUBECTL apply -f "$HERE/templates/runtimeclass.yaml"

  echo
  echo "Now prove it, rather than assuming it:"
  echo "    make preflight"
}

# ---------------------------------------------------------------- Agent Sandbox
install_agent_sandbox() {
  say "Agent Sandbox $AGENT_SANDBOX_VER"
  # Server-side apply: the bundle is large enough that the client-side
  # last-applied annotation exceeds the metadata size limit on some CRDs.
  $KUBECTL apply --server-side -f \
    "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VER}/sandbox-with-extensions.yaml"
  $KUBECTL -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=6m
}

# ---------------------------------------------------------------- CloudNativePG
install_cnpg() {
  need helm
  say "CloudNativePG"
  $HELM repo add cnpg https://cloudnative-pg.github.io/charts --force-update >/dev/null 2>&1
  $HELM upgrade --install cnpg cnpg/cloudnative-pg \
    -n cnpg-system --create-namespace --wait --timeout 8m
}

# ---------------------------------------------------------------- Gateway API
install_gateway() {
  need helm
  say "cert-manager (Envoy Gateway depends on it)"
  $HELM repo add jetstack https://charts.jetstack.io --force-update >/dev/null 2>&1
  $HELM upgrade --install cert-manager jetstack/cert-manager \
    -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 8m

  say "Envoy Gateway"
  # Envoy Gateway ships the Gateway API CRDs itself. Applying them separately as
  # well collides on field-manager ownership and fails the install.
  $HELM upgrade --install eg oci://docker.io/envoyproxy/gateway-helm \
    -n envoy-gateway-system --create-namespace --wait --timeout 8m

  say "GatewayClass"
  # Envoy Gateway installs a controller but deliberately creates no
  # GatewayClass; that is left to the platform owner. Without one the entire
  # Gateway API surface is present and inert -- CRDs installed, nothing watching.
  python3 "$REPO/scripts/render.py" "$HERE/templates/gatewayclass.yaml.tmpl"
  $KUBECTL apply -f "$HERE/templates/gatewayclass.yaml"
}

# ---------------------------------------------------------------- MetalLB
install_metallb() {
  need helm
  say "MetalLB"
  $HELM repo add metallb https://metallb.github.io/metallb --force-update >/dev/null 2>&1
  $HELM upgrade --install metallb metallb/metallb \
    -n metallb-system --create-namespace --wait --timeout 8m
  python3 "$REPO/scripts/render.py" "$HERE/templates/metallb-pool.yaml.tmpl"
  $KUBECTL apply -f "$HERE/templates/metallb-pool.yaml"
}

# ---------------------------------------------------------------- registry
install_registry() {
  say "in-cluster registry"
  python3 "$REPO/scripts/render.py" "$HERE/templates/registry.yaml.tmpl"
  $KUBECTL apply -f "$HERE/templates/registry.yaml"
  $KUBECTL -n registry rollout status deploy/registry --timeout=5m
  cat <<EOF

The registry serves plain HTTP. Each node's container runtime has to be told to
trust it, which is a host-level change this script cannot make for you:

  # k3s, on every node
  /etc/rancher/k3s/registries.yaml:
    mirrors:
      "${REGISTRY_IP:-<address>}:5000":
        endpoint: ["http://${REGISTRY_IP:-<address>}:5000"]

  # containerd elsewhere: configure an insecure mirror for the same address
EOF
}

case "${1:-}" in
  gvisor)        install_gvisor ;;
  agent-sandbox) install_agent_sandbox ;;
  cnpg)          install_cnpg ;;
  gateway)       install_gateway ;;
  metallb)       install_metallb ;;
  registry)      install_registry ;;
  all)
    install_metallb
    install_gateway
    install_cnpg
    install_agent_sandbox
    echo
    echo "gVisor is not included in 'all': it installs a runtime on your nodes"
    echo "and needs SSH access or your provider's own mechanism. Run:"
    echo "    deploy/cluster/install-prerequisites.sh gvisor"
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
