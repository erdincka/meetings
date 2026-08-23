#!/usr/bin/env bash
# Build the lab cluster from nothing, idempotently.
#
# Everything here was done by hand once; this exists so it can be done again
# without remembering any of it. Each stage is separately runnable, because the
# expensive parts (VM creation, k3s install) rarely need repeating when only the
# last stage failed.
#
#   bootstrap.sh provision   create or refresh the VMs
#   bootstrap.sh k3s         install k3s and gVisor
#   bootstrap.sh platform    MetalLB, cert-manager, CNPG, Gateway API, Agent Sandbox
#   bootstrap.sh all         all of the above
set -euo pipefail
export PATH="$HOME/.rd/bin:/opt/homebrew/bin:$PATH"

HERE=$(cd "$(dirname "$0")" && pwd)
ENV_FILE="$HERE/lab.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "deploy/k3s/lab.env not found." >&2
  echo "Copy deploy/k3s/lab.env.example to it and edit for your network." >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$ENV_FILE"

STORAGE=${PVE_STORAGE:-data}
TEMPLATE=${PVE_TEMPLATE:-9000}
BRIDGE=${PVE_BRIDGE:-vmbr0}
GATEWAY=${LAB_GATEWAY}
NETMASK=${LAB_NETMASK:-24}
KCTX=${KCTX:-k3s-lab}
REGISTRY="${REGISTRY_IP}:5000"
SSH_KEY=${SSH_KEY:-$HOME/.ssh/id_rsa.pub}

# id:name:ip:cores:memoryMB:diskGB
NODES=(
  "1030:k3s-cp:${NODE_CP_IP}:8:16384:80"
  "1031:k3s-w1:${NODE_W1_IP}:16:65536:200"
  "1032:k3s-w2:${NODE_W2_IP}:16:65536:200"
)
BUILDER="1033:k3s-builder:${BUILDER_IP}:16:32768:200"
SERVER_IP=${NODE_CP_IP}
SANDBOX_NODE=k3s-w2

pve() { ssh -o BatchMode=yes "$PVE_HOST" "$@"; }
node() { ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "ubuntu@$1" "${@:2}"; }
say() { printf '\n\033[1m>> %s\033[0m\n' "$*"; }

wait_for_ssh() {
  local ip=$1
  for _ in $(seq 1 60); do
    ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new \
      "ubuntu@$ip" true 2>/dev/null && return 0
    sleep 10
  done
  echo "timed out waiting for ssh on $ip" >&2
  return 1
}

ensure_template() {
  if pve "qm status $TEMPLATE" >/dev/null 2>&1; then
    echo "  template $TEMPLATE present"
    return
  fi
  say "building cloud-init template $TEMPLATE"
  pve "set -e
    cd /var/lib/vz/template/iso
    [ -f noble-server-cloudimg-amd64.img ] || \
      wget -q -O noble-server-cloudimg-amd64.img \
        https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
    qm create $TEMPLATE --name ubuntu-24.04-cloud --memory 2048 --cores 2 \
      --net0 virtio,bridge=$BRIDGE --scsihw virtio-scsi-single --ostype l26 --agent enabled=1
    qm importdisk $TEMPLATE /var/lib/vz/template/iso/noble-server-cloudimg-amd64.img $STORAGE
    qm set $TEMPLATE --scsi0 $STORAGE:vm-$TEMPLATE-disk-0,discard=on,ssd=1
    qm set $TEMPLATE --ide2 $STORAGE:cloudinit --boot order=scsi0 --serial0 socket --vga serial0
    qm template $TEMPLATE" >/dev/null
}

provision() {
  ensure_template
  scp -q -o BatchMode=yes "$SSH_KEY" "$PVE_HOST:/tmp/lab-key.pub"

  for spec in "${NODES[@]}" "$BUILDER"; do
    IFS=: read -r id name ip cores mem disk <<<"$spec"
    if pve "qm status $id" >/dev/null 2>&1; then
      echo "  $name ($id) exists"
      continue
    fi
    say "creating $name ($id) at $ip"
    # cloud-init applies the ssh key only on first boot, so it must be right
    # at clone time -- changing it later and rebooting does nothing.
    pve "set -e
      qm clone $TEMPLATE $id --name $name --full --storage $STORAGE
      qm set $id --cores $cores --memory $mem --cpu host --ciuser ubuntu \
        --ipconfig0 ip=$ip/$NETMASK,gw=$GATEWAY --nameserver $GATEWAY \
        --sshkeys /tmp/lab-key.pub
      qm resize $id scsi0 ${disk}G
      qm start $id" >/dev/null
    ssh-keygen -R "$ip" >/dev/null 2>&1 || true
    wait_for_ssh "$ip"
    echo "  $name up"
  done
}

install_gvisor() {
  local ip=$1
  # gVisor publishes artifacts under the uname arch ("x86_64"), not the Go name.
  node "$ip" 'sudo bash -c "
    set -e
    command -v runsc >/dev/null 2>&1 && exit 0
    B=https://storage.googleapis.com/gvisor/releases/release/latest/\$(uname -m)
    curl -fsSL -o /usr/local/bin/runsc \$B/runsc
    curl -fsSL -o /usr/local/bin/containerd-shim-runsc-v1 \$B/containerd-shim-runsc-v1
    chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1"'
}

configure_runsc_handler() {
  local ip=$1
  # k3s regenerates its containerd config on every start, so the handler goes in
  # a template drop-in it merges. Editing config.toml directly does not survive.
  node "$ip" 'sudo bash -c "
    set -e
    D=/var/lib/rancher/k3s/agent/etc/containerd
    mkdir -p \$D
    cat > \$D/config-v3.toml.tmpl <<TMPL
{{ template \"base\" . }}

[plugins.'\''io.containerd.cri.v1.runtime'\''.containerd.runtimes.runsc]
  runtime_type = \"io.containerd.runsc.v1\"
TMPL
    systemctl restart k3s 2>/dev/null || systemctl restart k3s-agent"'
}

install_k3s() {
  say "installing k3s server on $SERVER_IP"
  # traefik and servicelb are disabled because this project uses Envoy Gateway
  # and MetalLB. NetworkPolicy is left enabled and proved by the smoke gate.
  node "$SERVER_IP" "sudo bash -c '
    command -v k3s >/dev/null 2>&1 && exit 0
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC=\"server --disable=traefik --disable=servicelb --node-ip=$SERVER_IP --tls-san=$SERVER_IP --write-kubeconfig-mode=644\" sh -'" >/dev/null 2>&1
  sleep 15

  local token
  token=$(node "$SERVER_IP" 'sudo cat /var/lib/rancher/k3s/server/node-token')

  for spec in "${NODES[@]}"; do
    IFS=: read -r _ name ip _ _ _ <<<"$spec"
    [[ "$ip" == "$SERVER_IP" ]] && continue
    say "joining $name"
    node "$ip" "sudo bash -c '
      command -v k3s >/dev/null 2>&1 && exit 0
      curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC=\"agent --node-ip=$ip\" \
        K3S_URL=https://$SERVER_IP:6443 K3S_TOKEN=$token sh -'" >/dev/null 2>&1
  done

  say "installing gVisor and trusting the registry"
  for spec in "${NODES[@]}"; do
    IFS=: read -r _ name ip _ _ _ <<<"$spec"
    install_gvisor "$ip"
    node "$ip" "sudo bash -c '
      mkdir -p /etc/rancher/k3s
      printf \"mirrors:\n  \\\"$REGISTRY\\\":\n    endpoint:\n      - \\\"http://$REGISTRY\\\"\nconfigs:\n  \\\"$REGISTRY\\\":\n    tls:\n      insecure_skip_verify: true\n\" > /etc/rancher/k3s/registries.yaml'"
    configure_runsc_handler "$ip"
    echo "  $name configured"
  done
  sleep 20

  say "writing kubeconfig context '$KCTX'"
  node "$SERVER_IP" 'sudo cat /etc/rancher/k3s/k3s.yaml' \
    | sed "s|127.0.0.1|$SERVER_IP|; s|default|$KCTX|g" > /tmp/k3s-lab.yaml
  cp "$HOME/.kube/config" "$HOME/.kube/config.bak.$(date +%s)" 2>/dev/null || true
  KUBECONFIG="$HOME/.kube/config:/tmp/k3s-lab.yaml" kubectl config view --flatten > /tmp/merged
  mv /tmp/merged "$HOME/.kube/config"
  chmod 600 "$HOME/.kube/config"

  kubectl --context "$KCTX" label node "$SANDBOX_NODE" node-role=sandbox --overwrite >/dev/null
  kubectl --context "$KCTX" taint node "$SANDBOX_NODE" node-role=sandbox:NoSchedule --overwrite >/dev/null
  kubectl --context "$KCTX" get nodes
}

install_builder() {
  IFS=: read -r _ _ bip _ _ _ <<<"$BUILDER"
  say "installing docker on the builder"
  node "$bip" 'sudo bash -c "
    set -e
    command -v docker >/dev/null 2>&1 && exit 0
    export DEBIAN_FRONTEND=noninteractive
    cloud-init status --wait >/dev/null 2>&1 || true
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo \"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin"' >/dev/null 2>&1
  node "$bip" "sudo bash -c '
    mkdir -p /etc/docker
    printf \"{\\\"insecure-registries\\\": [\\\"$REGISTRY\\\"]}\" > /etc/docker/daemon.json
    usermod -aG docker ubuntu
    systemctl enable --now docker >/dev/null 2>&1
    systemctl restart docker'"
  echo "  builder ready: $(node "$bip" 'docker version --format "{{.Server.Version}} {{.Server.Arch}}"')"
}

platform() {
  local k="kubectl --context $KCTX"
  local h="helm --kube-context $KCTX"
  local here; here=$(cd "$(dirname "$0")" && pwd)

  say "rendering templates from lab.env"
  python3 "$here/../../scripts/render.py" \
    "$here/metallb-pool.yaml.tmpl" \
    "$here/registry.yaml.tmpl" \
    "$here/../bootstrap/gatewayclass.yaml.tmpl" \
    "$here/../charts/meetings/values-lab.yaml.tmpl"

  say "MetalLB"
  $h repo add metallb https://metallb.github.io/metallb --force-update >/dev/null 2>&1
  $h upgrade --install metallb metallb/metallb -n metallb-system --create-namespace \
    --wait --timeout 8m >/dev/null
  $k apply -f "$here/metallb-pool.yaml" >/dev/null

  say "cert-manager, CloudNativePG, Envoy Gateway"
  $h repo add jetstack https://charts.jetstack.io --force-update >/dev/null 2>&1
  $h repo add cnpg https://cloudnative-pg.github.io/charts --force-update >/dev/null 2>&1
  $h upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
    --set crds.enabled=true --wait --timeout 8m >/dev/null
  $h upgrade --install cnpg cnpg/cloudnative-pg -n cnpg-system --create-namespace \
    --wait --timeout 8m >/dev/null
  # Envoy Gateway ships the Gateway API CRDs; applying them separately as well
  # collides on field-manager ownership and fails the install.
  $h upgrade --install eg oci://docker.io/envoyproxy/gateway-helm -n envoy-gateway-system \
    --create-namespace --wait --timeout 8m >/dev/null
  $k apply -f "$here/../bootstrap/gatewayclass.yaml" >/dev/null

  say "Agent Sandbox"
  $k apply --server-side -f \
    https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.5.6/sandbox-with-extensions.yaml >/dev/null
  $k -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=6m

  say "registry"
  $k apply -f "$here/registry.yaml" >/dev/null
  $k -n registry rollout status deploy/registry --timeout=5m

  say "namespaces and Postgres"
  # The sandbox namespaces are declared by the chart, but the smoke gates need
  # them before the chart is ever installed. Creating them here with Helm's
  # ownership metadata already applied lets the chart adopt them instead of
  # refusing to install -- otherwise the first `make deploy` after a fresh
  # bootstrap fails on "cannot be imported into the current release".
  for ns in meetings meetings-sandboxes meetings-exec; do
    $k create namespace "$ns" --dry-run=client -o yaml | $k apply -f - >/dev/null
    $k label namespace "$ns" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
    $k annotate namespace "$ns" \
      meta.helm.sh/release-name=meetings \
      meta.helm.sh/release-namespace=meetings --overwrite >/dev/null
  done
  $k apply -f "$here/../bootstrap/cnpg-cluster.yaml" >/dev/null
  $k -n meetings wait --for=condition=Ready cluster/meetings-postgres --timeout=10m
}

case "${1:-all}" in
  provision) provision ;;
  k3s)       install_k3s; install_builder ;;
  platform)  platform ;;
  all)       provision; install_k3s; install_builder; platform ;;
  *) echo "usage: $0 {provision|k3s|platform|all}" >&2; exit 1 ;;
esac
